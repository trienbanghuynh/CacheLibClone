#!/usr/bin/env python3
"""
Generate a CacheLib trace CSV.

Usage:
  python3 generate_trace_csv.py                  # auto-detect or capture trace_output.csv
  python3 generate_trace_csv.py trace_output.csv # parse a specific bpftrace output file

To customise the simulation, edit the values in the '__main__' block at the
bottom of this file:

  n_small   Number of small items (64 B – 2 KB). Each goes to BigHash.
            Increase this to stress the BigHash bucket-flush path.

  n_large   Number of large items (2 KB – 6 MB). Each goes to BlockCache.
            Increase this to trigger more BlockCache region flushes.

  seed      Random seed. Change it to get a different shuffle / size
            distribution while keeping the run reproducible.

  out       Output CSV path. Defaults to fdp_trace.csv in this directory.

The boundary between small and large items is controlled by SMALL_ITEM_MAX
(currently 2048 B), which must match navySmallItemMaxSize in your cachebench
JSON config.
"""
from __future__ import annotations

import csv
import os
import random
import subprocess
import sys

COLUMNS = [
    "Seq",
    "Timestamp (ms)",
    "Delta (ms)",
    "Thread ID",
    "Function",
    "Engine",
    "Routing",
    "Write Size",
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
            "Write Size": "-",
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
            "Write Size": "-",
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
                    "Write Size": _fmt_size(bh_bucket_size),
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
                        "Write Size": _fmt_size(chunk_size),
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
        "Write Size": "-",
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
            write_size_fmt = "-"
            file_offset = "-"
            notes = ""

            # FileDevice::write has Size: and Offset: fields
            if "FileDevice" in func and len(parts) >= 5:
                for part in parts[3:]:
                    p = part.strip()
                    if p.startswith("Size:"):
                        write_size = p.replace("Size:", "").strip()
                        write_size_fmt = _fmt_size(write_size)
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
                "Write Size": write_size_fmt,
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


# ── entry point ───────────────────────────────────────────────────────────────
BASE_DIR = "/home/rsebenchtop2/CacheLib/cachelib/cachebench/test_configs"

# Locations to search for a real bpftrace CSV when none is specified.
TRACE_SEARCH_PATHS = [
    "trace_output.csv",
    os.path.expanduser("~/CacheLib/trace_output.csv"),
]


RUN_TEST_SH = os.path.join(BASE_DIR, "run_test.sh")
GENERATED_TRACE = "trace_output.csv"


def _find_trace() -> str | None:
    for path in TRACE_SEARCH_PATHS:
        if os.path.exists(path):
            return path
    return None


def _capture_trace() -> str:
    """Run run_test.sh to capture a real bpftrace trace, return path to output CSV."""
    print(f"No trace found — running {RUN_TEST_SH} to capture one...")
    result = subprocess.run(["bash", RUN_TEST_SH], cwd=BASE_DIR)
    if result.returncode != 0:
        print("Error: run_test.sh failed. Check that cachebench is built and bpftrace is installed.")
        sys.exit(1)
    # run_test.sh writes trace_output.csv into cwd (BASE_DIR)
    return os.path.join(BASE_DIR, GENERATED_TRACE)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        input_path = sys.argv[1]
        if not os.path.exists(input_path):
            print(f"Warning: '{input_path}' not found — capturing a new trace.")
            input_path = _capture_trace()
        print(f"Parsing real trace: {input_path}")
        rows = parse_csv(input_path)
        out = input_path
    else:
        trace = _find_trace()
        if not trace:
            trace = _capture_trace()
        print(f"Parsing real trace: {trace}")
        rows = parse_csv(trace)
        out = trace

    write_csv(rows, out)
