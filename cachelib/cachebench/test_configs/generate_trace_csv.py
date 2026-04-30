#!/usr/bin/env python3
"""
Generate a CacheLib FDP trace CSV.

Two modes:
  1. Standalone simulation (no cachebench needed)
  2. Parse a real bpftrace trace_output.csv from run_test.sh

Usage:
  python3 generate_trace_csv.py                          # simulate with defaults
  python3 generate_trace_csv.py --run 2                  # run 002: 60 small, 20 large, seed 99
  python3 generate_trace_csv.py --small 80 --large 15    # custom counts
  python3 generate_trace_csv.py --seed 7 --out my.csv    # custom seed + output file
  python3 generate_trace_csv.py trace_output.csv         # parse real bpftrace output
"""
from __future__ import annotations

import argparse
import csv
import random
import sys

COLUMNS = [
    "Timestamp (ms)",
    "Thread ID",
    "Function",
    "Engine",
    "Item Size",
    "Offset / LBA",
    "FDP PID",
    "Notes",
]


# ── helpers ──────────────────────────────────────────────────────────────────
def _fmt_size(n_bytes: int) -> str:
    if n_bytes >= 1024 * 1024:
        return f"{n_bytes / (1024 * 1024):.1f} MB"
    if n_bytes >= 1024:
        return f"{n_bytes / 1024:.1f} KB"
    return f"{n_bytes} B"


def _random_offset() -> str:
    return f"0x{random.randint(0x10000, 0xFFFFFF):X}"


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
    fdp_pid_bh = 1
    fdp_pid_bc = 2

    ops = (
        [(random.randint(64, 1800), "small")] * n_small
        + [(random.randint(3 * 1024 * 1024, 6 * 1024 * 1024), "large")] * n_large
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
            "Engine": "N/A",
            "Item Size": _fmt_size(size),
            "Offset / LBA": "-",
            "FDP PID": "-",
            "Notes": f"Dispatching {'small' if kind == 'small' else 'large'} item",
        })
        t += random.uniform(0.001, 0.003)

        if kind == "small":
            rows.append({
                "Timestamp (ms)": round(t, 3),
                "Thread ID": tid,
                "Function": "BigHash::insert",
                "Engine": "BigHash",
                "Item Size": _fmt_size(size),
                "Offset / LBA": "-",
                "FDP PID": "-",
                "Notes": "Item < 2048 B → routed to BigHash",
            })
            t += random.uniform(0.001, 0.005)
            bh_pending[tid] += size

            if bh_pending[tid] >= bh_bucket_size:
                offset = _random_offset()
                rows.append({
                    "Timestamp (ms)": round(t, 3),
                    "Thread ID": tid,
                    "Function": "BigHash::writeBucket",
                    "Engine": "BigHash",
                    "Item Size": _fmt_size(bh_bucket_size),
                    "Offset / LBA": offset,
                    "FDP PID": fdp_pid_bh,
                    "Notes": "Bucket full → write 4 KB sector",
                })
                t += random.uniform(0.001, 0.003)
                rows.append({
                    "Timestamp (ms)": round(t, 3),
                    "Thread ID": tid,
                    "Function": "prepFdpUringCmdSqe",
                    "Engine": "BigHash",
                    "Item Size": _fmt_size(bh_bucket_size),
                    "Offset / LBA": offset,
                    "FDP PID": fdp_pid_bh,
                    "Notes": "Build io_uring FDP NVMe cmd (hint ignored on non-FDP hw)",
                })
                t += random.uniform(0.002, 0.008)
                bh_pending[tid] %= bh_bucket_size

        else:
            rows.append({
                "Timestamp (ms)": round(t, 3),
                "Thread ID": tid,
                "Function": "BlockCache::insert",
                "Engine": "BlockCache",
                "Item Size": _fmt_size(size),
                "Offset / LBA": "-",
                "FDP PID": "-",
                "Notes": "Item >= 2048 B → routed to BlockCache",
            })
            t += random.uniform(0.001, 0.005)
            bc_pending += size

            if bc_pending >= bc_region_size:
                offset = _random_offset()
                rows.append({
                    "Timestamp (ms)": round(t, 3),
                    "Thread ID": tid,
                    "Function": "RegionManager::doFlush",
                    "Engine": "BlockCache",
                    "Item Size": _fmt_size(bc_region_size),
                    "Offset / LBA": offset,
                    "FDP PID": fdp_pid_bc,
                    "Notes": "Region full (16 MB) → flush to storage",
                })
                t += random.uniform(0.5, 2.0)
                rows.append({
                    "Timestamp (ms)": round(t, 3),
                    "Thread ID": tid,
                    "Function": "prepFdpUringCmdSqe",
                    "Engine": "BlockCache",
                    "Item Size": _fmt_size(bc_region_size),
                    "Offset / LBA": offset,
                    "FDP PID": fdp_pid_bc,
                    "Notes": "Build io_uring FDP NVMe cmd for region flush",
                })
                t += random.uniform(1.0, 5.0)
                bc_pending %= bc_region_size

    rows.append({
        "Timestamp (ms)": round(t, 3),
        "Thread ID": kernel_thread,
        "Function": "io_uring_submit",
        "Engine": "Kernel",
        "Item Size": "-",
        "Offset / LBA": "-",
        "FDP PID": "-",
        "Notes": "Kernel submits batched io_uring SQEs to NVMe driver",
    })
    return rows


# ── parse real bpftrace output ────────────────────────────────────────────────
def parse_csv(path: str) -> list[dict]:
    rows = []
    with open(path) as f:
        for line in f:
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 2:
                continue
            func = parts[1]
            engine = (
                "BigHash" if "BigHash" in func
                else "BlockCache" if "BlockCache" in func or "RegionManager" in func
                else "Kernel" if "io_uring" in func
                else "N/A"
            )
            rows.append({
                "Timestamp (ms)": parts[0],
                "Thread ID": "-",
                "Function": func,
                "Engine": engine,
                "Item Size": parts[2].replace("Size: ", "") if len(parts) > 2 else "-",
                "Offset / LBA": parts[3].replace("Offset: ", "") if len(parts) > 3 else "-",
                "FDP PID": parts[4].replace("PID: ", "") if len(parts) > 4 else "-",
                "Notes": "",
            })
    return rows


# ── write CSV ─────────────────────────────────────────────────────────────────
def write_csv(rows: list[dict], out_path: str) -> None:
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"CSV saved → {out_path}  ({len(rows)} rows)")


# ── entry point ───────────────────────────────────────────────────────────────
# Preset run profiles — add more here as needed
RUN_PROFILES = {
    1: dict(n_small=100, n_large=5,  seed=42),
    2: dict(n_small=60,  n_large=20, seed=99),
}

BASE_DIR = "/home/rsebenchtop2/CacheLib/cachelib/cachebench/test_configs"

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate CacheLib FDP trace CSV")
    parser.add_argument("input", nargs="?", help="Real bpftrace CSV to parse instead of simulating")
    parser.add_argument("--run",   type=int,  default=None, help="Preset run number (1 or 2)")
    parser.add_argument("--small", type=int,  default=None, help="Number of small items (< 2 KB)")
    parser.add_argument("--large", type=int,  default=None, help="Number of large items (>= 2 KB)")
    parser.add_argument("--seed",  type=int,  default=None, help="Random seed for reproducibility")
    parser.add_argument("--out",   type=str,  default=None, help="Output CSV filename")
    args = parser.parse_args()

    if args.input:
        # Mode 2: parse a real bpftrace file
        print(f"Parsing real trace: {args.input}")
        rows = parse_csv(args.input)
        out = args.out or args.input.replace(".csv", "_parsed.csv")
    else:
        # Mode 1: simulation — apply preset then override with explicit flags
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
