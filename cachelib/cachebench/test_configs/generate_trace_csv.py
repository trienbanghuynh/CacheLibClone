#!/usr/bin/env python3
"""
Generate or analyze a CacheLib Navy trace.

Modes:
  1. Standalone simulation (no cachebench needed)
  2. Parse a real bpftrace trace_output.csv from run_test.sh
  3. Analyze workload distribution from mixed_workload.json (static, no run)
  4. Analyze workload distribution from cachebench runtime stats (requires printNvmCounters: true)

Usage:
  python3 generate_trace_csv.py                               # simulate with defaults
  python3 generate_trace_csv.py --run 2                       # run 002: 60 small, 20 large, seed 99
  python3 generate_trace_csv.py --small 80 --large 15         # custom counts
  python3 generate_trace_csv.py --seed 7 --out my.csv         # custom seed + output file
  python3 generate_trace_csv.py trace_output.csv              # parse real bpftrace output
  python3 generate_trace_csv.py --analyze-config mixed_workload.json
  python3 generate_trace_csv.py --from-stats cachebench_stats.txt
  python3 generate_trace_csv.py --analyze-config mixed_workload.json --from-stats cachebench_stats.txt
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import re
import sys

COLUMNS = [
    "Seq",
    "Timestamp (ms)",
    "Delta (ms)",
    "Thread ID",
    "Function",
    "Engine",
    "Routing",
    "Write Size (B)",
    "Write Size (Human)",
    "File Offset (B)",
    "Notes",
]

# Boundary below which items go to BigHash (matches navySmallItemMaxSize in the config)
SMALL_ITEM_MAX = 2048


# ── helpers ──────────────────────────────────────────────────────────────────
def _fmt_size(n_bytes) -> str:
    if n_bytes == "-" or n_bytes is None:
        return "-"
    n = int(n_bytes)
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n} B"


def _routing(func: str) -> str:
    if "BigHash" in func:
        return f"small →BigHash (≤{SMALL_ITEM_MAX}B)"
    if "BlockCache" in func:
        return f"large →BlockCache (>{SMALL_ITEM_MAX}B)"
    if "Driver" in func:
        return "dispatch"
    return ""


def _engine(func: str) -> str:
    if "BigHash" in func:
        return "BigHash"
    if "BlockCache" in func or "RegionManager" in func:
        return "BlockCache"
    if "FileDevice" in func or "io_uring" in func:
        return "Kernel/IO"
    return "N/A"


def _add_derived(rows: list[dict]) -> list[dict]:
    """Add Seq, Delta, and normalise Timestamp to 0-based."""
    if not rows:
        return rows
    try:
        t0 = float(rows[0]["Timestamp (ms)"])
    except (ValueError, KeyError):
        t0 = 0.0

    prev_t = t0
    for i, row in enumerate(rows):
        try:
            t = float(row["Timestamp (ms)"])
        except (ValueError, KeyError):
            t = prev_t
        row["Seq"] = i + 1
        row["Timestamp (ms)"] = round(t - t0, 3)
        row["Delta (ms)"] = round(t - prev_t, 3)
        prev_t = t
    return rows


def _random_offset() -> int:
    return random.randint(0x10000, 0xFFFFFF)


def _strip_json_comments(text: str) -> str:
    """Strip // line comments so json.loads() can parse cachebench configs."""
    return re.sub(r"//[^\n]*", "", text)


# ── simulation ────────────────────────────────────────────────────────────────
def simulate_trace(n_small: int = 100, n_large: int = 5) -> list[dict]:
    rows = []
    t = 100.0
    small_threads = [1402, 1403]
    large_threads = [1405, 1406]
    kernel_thread = 1408
    bh_bucket_size = 4096
    bh_pending = {tid: 0 for tid in small_threads}
    bc_region_size = 16 * 1024 * 1024
    bc_pending = 0

    ops = (
        [(random.randint(64, SMALL_ITEM_MAX), "small")] * n_small
        + [(random.randint(SMALL_ITEM_MAX + 1, 6 * 1024 * 1024), "large")] * n_large
    )
    random.shuffle(ops)

    small_idx = large_idx = 0
    for size, kind in ops:
        if kind == "small":
            tid = small_threads[small_idx % len(small_threads)]
            small_idx += 1
        else:
            tid = large_threads[large_idx % len(large_threads)]
            large_idx += 1

        rows.append({
            "Timestamp (ms)": round(t, 3),
            "Thread ID": tid,
            "Function": "Driver::insert",
            "Engine": _engine("Driver::insert"),
            "Routing": _routing("Driver::insert"),
            "Write Size (B)": "-",
            "Write Size (Human)": "-",
            "File Offset (B)": "-",
            "Notes": f"Dispatching {'small' if kind == 'small' else 'large'} item ({_fmt_size(size)})",
        })
        t += random.uniform(0.001, 0.003)

        func = "BigHash::insert" if kind == "small" else "BlockCache::insert"
        rows.append({
            "Timestamp (ms)": round(t, 3),
            "Thread ID": tid,
            "Function": func,
            "Engine": _engine(func),
            "Routing": _routing(func),
            "Write Size (B)": "-",
            "Write Size (Human)": "-",
            "File Offset (B)": "-",
            "Notes": "",
        })
        t += random.uniform(0.001, 0.005)

        if kind == "small":
            bh_pending[tid] += size
            if bh_pending[tid] >= bh_bucket_size:
                offset = _random_offset()
                rows.append({
                    "Timestamp (ms)": round(t, 3),
                    "Thread ID": tid,
                    "Function": "FileDevice::write",
                    "Engine": "Kernel/IO",
                    "Routing": "",
                    "Write Size (B)": bh_bucket_size,
                    "Write Size (Human)": _fmt_size(bh_bucket_size),
                    "File Offset (B)": offset,
                    "Notes": "BigHash bucket flush (4 KB)",
                })
                t += random.uniform(0.001, 0.008)
                bh_pending[tid] %= bh_bucket_size
        else:
            bc_pending += size
            if bc_pending >= bc_region_size:
                chunk_size = 1024 * 1024  # deviceMaxWriteSize splits 16MB into 1MB chunks
                base_offset = _random_offset()
                for chunk_i in range(bc_region_size // chunk_size):
                    rows.append({
                        "Timestamp (ms)": round(t, 3),
                        "Thread ID": tid,
                        "Function": "FileDevice::write",
                        "Engine": "Kernel/IO",
                        "Routing": "",
                        "Write Size (B)": chunk_size,
                        "Write Size (Human)": _fmt_size(chunk_size),
                        "File Offset (B)": base_offset + chunk_i * chunk_size,
                        "Notes": "BlockCache region chunk (1 MB, split by deviceMaxWriteSize)",
                    })
                    t += random.uniform(0.02, 0.1)
                bc_pending %= bc_region_size

    rows.append({
        "Timestamp (ms)": round(t, 3),
        "Thread ID": kernel_thread,
        "Function": "io_uring_submit",
        "Engine": "Kernel/IO",
        "Routing": "",
        "Write Size (B)": "-",
        "Write Size (Human)": "-",
        "File Offset (B)": "-",
        "Notes": "Kernel submits batched io_uring SQEs",
    })
    return _add_derived(rows)


# ── parse real bpftrace output ────────────────────────────────────────────────
def parse_csv(path: str) -> list[dict]:
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            # skip bpftrace header lines
            if not line or line.startswith("Attaching") or ":" in line.split(",")[0]:
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 2:
                continue

            try:
                ts = int(parts[0])
            except ValueError:
                continue

            func = parts[1].strip()
            tid = parts[2].strip() if len(parts) > 2 else "-"
            engine = _engine(func)
            routing = _routing(func)

            write_size = "-"
            write_size_h = "-"
            file_offset = "-"
            notes = ""

            # FileDevice::write has Size: and Offset: fields
            if "FileDevice" in func and len(parts) >= 5:
                for part in parts[3:]:
                    p = part.strip()
                    if p.startswith("Size:"):
                        write_size = p.replace("Size:", "").strip()
                        write_size_h = _fmt_size(write_size)
                        sz = int(write_size)
                        if sz == 4096:
                            notes = "BigHash bucket flush (4 KB)"
                        elif sz == 16 * 1024 * 1024:
                            notes = "BlockCache region flush (16 MB)"
                        elif sz == 1024 * 1024:
                            notes = "BlockCache region chunk (1 MB, split by deviceMaxWriteSize)"
                        else:
                            notes = f"Device write ({_fmt_size(sz)})"
                    elif p.startswith("Offset:"):
                        file_offset = p.replace("Offset:", "").strip()

            rows.append({
                "Timestamp (ms)": ts,
                "Thread ID": tid,
                "Function": func,
                "Engine": engine,
                "Routing": routing,
                "Write Size (B)": write_size,
                "Write Size (Human)": write_size_h,
                "File Offset (B)": file_offset,
                "Notes": notes,
            })
    return _add_derived(rows)


# ── write CSV ─────────────────────────────────────────────────────────────────
def write_csv(rows: list[dict], out_path: str) -> None:
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"CSV saved → {out_path}  ({len(rows)} rows)")


# ── config analysis ───────────────────────────────────────────────────────────
def analyze_config(config_path: str) -> dict:
    """
    Compute expected BigHash/BlockCache routing split from mixed_workload.json.
    Returns a dict of results for optional combined reporting.
    """
    with open(config_path) as f:
        cfg = json.loads(_strip_json_comments(f.read()))

    cc = cfg.get("cache_config", {})
    tc = cfg.get("test_config", {})

    small_max   = cc.get("navySmallItemMaxSize", 2048)
    bucket_sz   = cc.get("navyBigHashBucketSize", 4096)
    region_mb   = cc.get("navyRegionSizeMB", 16)
    region_sz   = region_mb * 1024 * 1024
    max_write   = 1024 * 1024          # deviceMaxWriteSize default

    val_sizes = tc.get("valSizeRange", [])
    val_probs = tc.get("valSizeRangeProbability", [])
    num_ops     = tc.get("numOps", 0)
    num_threads = tc.get("numThreads", 1)
    total_ops   = num_ops * num_threads

    # Discrete distribution: equal number of sizes and probabilities
    # Piecewise: len(val_sizes) == len(val_probs) + 1 (bin edges)
    if len(val_sizes) == len(val_probs):
        total_w   = sum(val_probs)
        norm      = [p / total_w for p in val_probs]
        breakdown = list(zip(val_sizes, norm))
    else:
        total_w   = sum(val_probs)
        norm      = [p / total_w for p in val_probs]
        mids      = [(val_sizes[i] + val_sizes[i + 1]) / 2 for i in range(len(val_probs))]
        breakdown = list(zip(mids, norm))

    bh_pairs = [(s, p) for s, p in breakdown if s <= small_max]
    bc_pairs = [(s, p) for s, p in breakdown if s > small_max]

    bh_prob = sum(p for _, p in bh_pairs)
    bc_prob = sum(p for _, p in bc_pairs)
    avg_small = sum(s * p for s, p in bh_pairs) / bh_prob if bh_prob > 0 else 0
    avg_large = sum(s * p for s, p in bc_pairs) / bc_prob if bc_prob > 0 else 0

    exp_bh_inserts  = total_ops * bh_prob
    exp_bc_inserts  = total_ops * bc_prob
    exp_bh_logical  = exp_bh_inserts * avg_small
    exp_bc_logical  = exp_bc_inserts * avg_large
    exp_bh_flushes  = exp_bh_logical / bucket_sz if bucket_sz else 0
    exp_bh_physical = exp_bh_flushes * bucket_sz
    exp_bc_regions  = exp_bc_logical / region_sz if region_sz else 0
    exp_bc_chunks   = exp_bc_regions * (region_sz // max_write)
    exp_bc_physical = exp_bc_regions * region_sz
    bh_write_amp    = exp_bh_physical / exp_bh_logical if exp_bh_logical > 0 else 0
    bc_write_amp    = exp_bc_physical / exp_bc_logical if exp_bc_logical > 0 else 0

    SEP = "-" * 72

    print()
    print("=== Workload Distribution Analysis — from config ===")
    print(f"Config : {config_path}")
    print(f"Params : navySmallItemMaxSize={small_max}B  "
          f"bucketSize={_fmt_size(bucket_sz)}  "
          f"regionSize={region_mb}MB  "
          f"deviceMaxWriteSize={_fmt_size(max_write)}")
    print(f"Ops    : {total_ops:,}  ({num_ops:,} ops × {num_threads} threads)")
    print()

    print(f"{'Item size':<12} {'Weight':>7}  {'Engine':<11}  {'Expected inserts':>17}")
    print(SEP)
    for s, p in breakdown:
        eng = "BigHash" if s <= small_max else "BlockCache"
        marker = " ← routing boundary" if s == small_max else ""
        print(f"{_fmt_size(int(s)):<12} {p*100:>6.1f}%  {eng:<11}  {total_ops*p:>17,.0f}{marker}")

    print()
    print(f"{'Engine':<11}  {'Share':>6}  {'Exp. inserts':>13}  {'Avg item':>10}  {'Logical':>10}  {'Physical':>10}  {'Write amp':>10}")
    print(SEP)
    print(f"{'BigHash':<11}  {bh_prob*100:>5.1f}%  {exp_bh_inserts:>13,.0f}  {_fmt_size(int(avg_small)):>10}  {_fmt_size(int(exp_bh_logical)):>10}  {_fmt_size(int(exp_bh_physical)):>10}  {bh_write_amp:>9.2f}x")
    print(f"{'BlockCache':<11}  {bc_prob*100:>5.1f}%  {exp_bc_inserts:>13,.0f}  {_fmt_size(int(avg_large)):>10}  {_fmt_size(int(exp_bc_logical)):>10}  {_fmt_size(int(exp_bc_physical)):>10}  {bc_write_amp:>9.2f}x")

    print()
    print(f"{'Engine':<11}  {'Flush/chunk unit':>17}  {'Expected flushes/chunks':>24}")
    print(SEP)
    print(f"{'BigHash':<11}  {_fmt_size(bucket_sz):>17}  {exp_bh_flushes:>24,.0f}  bucket flushes")
    print(f"{'BlockCache':<11}  {_fmt_size(max_write):>17}  {exp_bc_chunks:>24,.0f}  region chunks  ({exp_bc_regions:.1f} full regions)")

    return {
        "bh_prob": bh_prob, "bc_prob": bc_prob,
        "avg_small": avg_small, "avg_large": avg_large,
        "exp_bh_inserts": exp_bh_inserts, "exp_bc_inserts": exp_bc_inserts,
        "exp_bh_logical": exp_bh_logical, "exp_bc_logical": exp_bc_logical,
        "exp_bh_physical": exp_bh_physical, "exp_bc_physical": exp_bc_physical,
        "bh_write_amp": bh_write_amp, "bc_write_amp": bc_write_amp,
        "exp_bh_flushes": exp_bh_flushes, "exp_bc_regions": exp_bc_regions,
        "exp_bc_chunks": exp_bc_chunks,
    }


# ── runtime stats analysis ────────────────────────────────────────────────────
def analyze_stats(stats_path: str, theory: dict | None = None) -> None:
    """
    Parse cachebench stdout (saved to a file) and report actual routing distribution.
    Requires "printNvmCounters": true in cache_config of mixed_workload.json.
    Optionally compares against theoretical values from analyze_config().
    """
    counters: dict[str, float] = {}
    in_nvm = False

    with open(stats_path) as f:
        for line in f:
            line = line.rstrip()
            if "== NVM Counters Map ==" in line:
                in_nvm = True
                continue
            if in_nvm:
                if line.startswith("==") or (line.strip() == "" and len(counters) > 0):
                    in_nvm = False
                    continue
                m = re.match(r"\s*(\S+)\s+:\s+(\S+)", line)
                if m:
                    try:
                        counters[m.group(1)] = float(m.group(2))
                    except ValueError:
                        pass

    if not counters:
        print()
        print("ERROR: No NVM counters found in stats file.")
        print('       Add "printNvmCounters": true to cache_config in mixed_workload.json,')
        print("       then re-run: bash run_test.sh  (output is saved to cachebench_stats.txt)")
        return

    def g(key: str) -> int:
        return int(counters.get(key, 0))

    bh_inserts   = g("navy_bh_inserts")
    bc_inserts   = g("navy_bc_inserts")
    bh_succ      = g("navy_bh_succ_inserts")
    bc_succ      = g("navy_bc_succ_inserts")
    bh_evict     = g("navy_bh_evictions")
    bh_items     = g("navy_bh_items")
    bc_items     = g("navy_bc_items")
    bh_logical   = g("navy_bh_logical_written")
    bh_physical  = g("navy_bh_physical_written")
    bc_logical   = g("navy_bc_logical_written")
    bc_physical  = g("navy_bc_physical_written")  # from RegionManager
    nav_accepted = g("navy_accepted")
    nav_rejected = g("navy_rejected")

    total = bh_inserts + bc_inserts
    bh_pct = bh_inserts / total * 100 if total else 0
    bc_pct = bc_inserts / total * 100 if total else 0
    bh_write_amp = bh_physical / bh_logical if bh_logical else 0
    bc_write_amp = bc_physical / bc_logical if bc_logical else 0

    SEP = "-" * 72

    print()
    print("=== Workload Distribution Analysis — from runtime stats ===")
    print(f"Stats  : {stats_path}")
    print()

    print(f"{'Engine':<11}  {'Inserts':>10}  {'Share':>6}  {'Succ inserts':>13}  {'Items cached':>13}")
    print(SEP)
    print(f"{'BigHash':<11}  {bh_inserts:>10,}  {bh_pct:>5.1f}%  {bh_succ:>13,}  {bh_items:>13,}")
    print(f"{'BlockCache':<11}  {bc_inserts:>10,}  {bc_pct:>5.1f}%  {bc_succ:>13,}  {bc_items:>13,}")
    print(f"{'Total':<11}  {total:>10,}  {'100.0%':>6}")
    if nav_accepted or nav_rejected:
        print(f"\n  Driver: {nav_accepted:,} accepted  /  {nav_rejected:,} rejected (back-pressure)")

    print()
    print(f"{'Engine':<11}  {'Logical written':>16}  {'Physical written':>17}  {'Write amp':>10}  {'BH evictions':>13}")
    print(SEP)
    print(f"{'BigHash':<11}  {_fmt_size(bh_logical):>16}  {_fmt_size(bh_physical):>17}  {bh_write_amp:>9.2f}x  {bh_evict:>13,}")
    print(f"{'BlockCache':<11}  {_fmt_size(bc_logical):>16}  {_fmt_size(bc_physical):>17}  {bc_write_amp:>9.2f}x")

    # Comparison with theory when both are available
    if theory:
        print()
        print("=== Theory vs Actual ===")
        print(f"{'Metric':<30}  {'Expected':>12}  {'Actual':>12}  {'Delta':>10}")
        print(SEP)
        rows_cmp = [
            ("BigHash insert share",
             f"{theory['bh_prob']*100:.1f}%", f"{bh_pct:.1f}%",
             f"{bh_pct - theory['bh_prob']*100:+.1f}pp"),
            ("BlockCache insert share",
             f"{theory['bc_prob']*100:.1f}%", f"{bc_pct:.1f}%",
             f"{bc_pct - theory['bc_prob']*100:+.1f}pp"),
            ("BigHash logical written",
             _fmt_size(int(theory['exp_bh_logical'])), _fmt_size(bh_logical),
             f"{(bh_logical/theory['exp_bh_logical']-1)*100:+.0f}%" if theory['exp_bh_logical'] else "N/A"),
            ("BigHash physical written",
             _fmt_size(int(theory['exp_bh_physical'])), _fmt_size(bh_physical),
             f"{(bh_physical/theory['exp_bh_physical']-1)*100:+.0f}%" if theory['exp_bh_physical'] else "N/A"),
            ("BigHash write amplification",
             f"{theory['bh_write_amp']:.2f}x", f"{bh_write_amp:.2f}x",
             f"{bh_write_amp - theory['bh_write_amp']:+.2f}"),
            ("BlockCache logical written",
             _fmt_size(int(theory['exp_bc_logical'])), _fmt_size(bc_logical),
             f"{(bc_logical/theory['exp_bc_logical']-1)*100:+.0f}%" if theory['exp_bc_logical'] else "N/A"),
            ("BlockCache physical written",
             _fmt_size(int(theory['exp_bc_physical'])), _fmt_size(bc_physical),
             f"{(bc_physical/theory['exp_bc_physical']-1)*100:+.0f}%" if theory['exp_bc_physical'] else "N/A"),
            ("BlockCache write amplification",
             f"{theory['bc_write_amp']:.2f}x", f"{bc_write_amp:.2f}x",
             f"{bc_write_amp - theory['bc_write_amp']:+.2f}"),
        ]
        for label, exp, act, delta in rows_cmp:
            print(f"{label:<30}  {exp:>12}  {act:>12}  {delta:>10}")


# ── entry point ───────────────────────────────────────────────────────────────
RUN_PROFILES = {
    1: dict(n_small=100, n_large=5,  seed=42),
    2: dict(n_small=60,  n_large=20, seed=99),
}

BASE_DIR = "/home/rsebenchtop2/CacheLib/cachelib/cachebench/test_configs"

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate or analyze a CacheLib Navy trace")
    parser.add_argument("input", nargs="?", help="Real bpftrace CSV to parse")
    parser.add_argument("--run",            type=int, default=None)
    parser.add_argument("--small",          type=int, default=None)
    parser.add_argument("--large",          type=int, default=None)
    parser.add_argument("--seed",           type=int, default=None)
    parser.add_argument("--out",            type=str, default=None)
    parser.add_argument("--analyze-config", type=str, default=None,
                        metavar="CONFIG_JSON",
                        help="Compute expected routing distribution from mixed_workload.json")
    parser.add_argument("--from-stats",     type=str, default=None,
                        metavar="STATS_FILE",
                        help="Parse cachebench stdout to report actual routing distribution")
    args = parser.parse_args()

    # Analysis modes (can be combined)
    if args.analyze_config or args.from_stats:
        theory = None
        if args.analyze_config:
            theory = analyze_config(args.analyze_config)
        if args.from_stats:
            analyze_stats(args.from_stats, theory=theory)
        sys.exit(0)

    # Trace modes
    if args.input:
        print(f"Parsing real trace: {args.input}")
        rows = parse_csv(args.input)
        out = args.out or args.input.replace(".csv", "_parsed.csv")
    else:
        profile = RUN_PROFILES.get(args.run, RUN_PROFILES[1])
        n_small = args.small if args.small is not None else profile["n_small"]
        n_large = args.large if args.large is not None else profile["n_large"]
        seed    = args.seed  if args.seed  is not None else profile["seed"]
        run_id  = args.run or 1
        out     = args.out or f"{BASE_DIR}/fdp_trace_run{run_id:03d}.csv"
        random.seed(seed)
        print(f"Simulating run {run_id:03d}: {n_small} small + {n_large} large items  (seed={seed})")
        rows = simulate_trace(n_small=n_small, n_large=n_large)

    write_csv(rows, out)
