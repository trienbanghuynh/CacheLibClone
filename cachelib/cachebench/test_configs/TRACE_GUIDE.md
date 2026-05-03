# CacheLib Navy I/O Trace Guide

Capture and analyze CacheLib Navy's write path — BigHash bucket flushes,
BlockCache region writes, and kernel I/O events — using bpftrace uprobes on
a live `cachebench` workload.

---

## Files

| File | Purpose |
|------|---------|
| `run_test.sh` | Runs cachebench + bpftrace simultaneously; writes raw `trace_output.csv` |
| `mixed_workload.json` | Cachebench workload config — NVM cache size, BigHash/BlockCache split, op ratios |
| `generate_trace_csv.py` | Parses raw bpftrace output into an enriched 11-column CSV; also has a standalone simulator mode |
| `trace_output.csv` | **Output** — raw bpftrace lines (written to the directory where you run the script) |
| `trace_output_parsed.csv` | **Output** — enriched CSV ready for analysis |

---

## Prerequisites

### 1. Build cachebench

```bash
cd ~/CacheLib
python3 build/fbcode_builder/getdeps.py --allow-system-packages build cachelib
```

> This takes 30–60 minutes on first build. Subsequent builds are incremental.

Verify the binary exists:

```bash
python3 build/fbcode_builder/getdeps.py \
  --allow-system-packages show-inst-dir cachelib
# Append /bin/cachebench to the printed path to get the full binary path
```

### 2. Install bpftrace (apt version)

```bash
sudo apt install bpftrace
/usr/bin/bpftrace --version   # should print 0.9.x or later
```

> **Important:** Use `/usr/bin/bpftrace` (the apt package). The AppImage version
> sometimes installed at `/usr/local/bin/bpftrace` can fail with a Python
> init error when running uprobe programs on systems without a Nix store.

### 3. Kernel requirements

- Linux kernel 4.14+ with BPF and uprobe support enabled (standard on Ubuntu 20.04+)
- `sudo` access (bpftrace requires root to attach uprobes)

---

## Step-by-Step: Run the Trace

### Step 1 — Run cachebench with bpftrace capture

```bash
cd ~/CacheLib

# Option A: auto-detect cachebench (uses getdeps install dir)
bash cachelib/cachebench/test_configs/run_test.sh

# Option B: point to binary explicitly
CACHEBENCH_BIN=/path/to/cachebench \
  bash cachelib/cachebench/test_configs/run_test.sh

# Option C: use a custom workload config
CONFIG_FILE=/path/to/my_config.json \
  bash cachelib/cachebench/test_configs/run_test.sh
```

The script will:
1. Pre-allocate a 512 MB backing file on `/dev/shm` (RAM disk)
2. Start bpftrace in the background and wait 3 seconds for uprobes to attach
3. Run cachebench with `mixed_workload.json` (100,000 ops, 4 threads)
4. Kill bpftrace and clean up the backing file
5. Print the path to the raw trace

Expected runtime: **~30–60 seconds**.

### Step 2 — Parse into enriched CSV

```bash
python3 cachelib/cachebench/test_configs/generate_trace_csv.py trace_output.csv
```

Output: `trace_output_parsed.csv` in the current directory.

### Step 3 — Inspect results

```bash
head trace_output_parsed.csv
grep "FileDevice" trace_output_parsed.csv | head -20
grep "BigHash bucket" trace_output_parsed.csv | wc -l    # count BigHash flushes
grep "BlockCache region" trace_output_parsed.csv | wc -l # count BlockCache chunks
```

---

## Output CSV Format

`trace_output_parsed.csv` has 11 columns:

| Column | Description | Example |
|--------|-------------|---------|
| `Seq` | Row number (1-based) | `1`, `2`, `3` |
| `Timestamp (ms)` | Milliseconds since first event | `0.0`, `5.0`, `38.0` |
| `Delta (ms)` | Time since previous event | `0.0`, `1.0` |
| `Thread ID` | OS thread ID that fired the probe | `68477`, `68478` |
| `Function` | Probed function | `BigHash::insert`, `BlockCache::insert`, `FileDevice::write` |
| `Engine` | CacheLib engine classification | `BigHash`, `BlockCache`, `Kernel/IO` |
| `Routing` | Item routing label | `small →BigHash (≤2048B)`, `large →BlockCache (>2048B)` |
| `Write Size (B)` | Physical write size in bytes | `4096`, `1048576`, `-` |
| `Write Size (Human)` | Human-readable write size | `4.0 KB`, `1.0 MB`, `-` |
| `File Offset (B)` | Byte offset into the NVM device file | `506929152`, `-` |
| `Notes` | Descriptive annotation | `BigHash bucket flush (4 KB)` |

### Notes field values

| Notes value | Meaning |
|-------------|---------|
| `BigHash bucket flush (4 KB)` | BigHash accumulated enough items to fill a 4 KB bucket and flushed it to device |
| `BlockCache region chunk (1 MB, split by deviceMaxWriteSize)` | One 1 MB chunk of a 16 MB BlockCache region write (split because `deviceMaxWriteSize=1048576`) |
| `Kernel submits batched io_uring SQEs` | io_uring submission (simulation mode only) |

### Typical event counts (100k-op run)

| Event | Count | Why |
|-------|-------|-----|
| `BlockCache::insert` | ~55,000 | Large items (>2048B) routed to BlockCache; count reflects NVM insertions driven by RAM evictions |
| `BigHash::insert` | ~10,000 | Small items (≤2048B) routed to BigHash |
| `FileDevice::write` (4 KB) | ~13,000 | BigHash bucket flushes |
| `FileDevice::write` (1 MB) | ~144 | BlockCache region chunks (9 full 16 MB regions = 144 × 1 MB) |

> `Driver::insert` events are **not captured** — the compiler inlines this
> function with LTO optimization so no stable uprobe point exists.

---

## Workload Configuration (`mixed_workload.json`)

Key parameters you may want to tune:

| Parameter | Default | Effect |
|-----------|---------|--------|
| `cacheSizeMB` | 512 | DRAM cache size |
| `nvmCacheSizeMB` | 512 | NVM cache size |
| `navySmallItemMaxSize` | 2048 | Items ≤ this go to BigHash; larger go to BlockCache |
| `navyBigHashBucketSize` | 4096 | BigHash flush granularity (one `FileDevice::write` per bucket) |
| `navyRegionSizeMB` | 16 | BlockCache region size; each region = 16 × 1 MB writes |
| `navyBigHashSizePct` | 10 | Percent of NVM space reserved for BigHash |
| `numOps` | 100000 | Operations per thread |
| `numThreads` | 4 | Concurrent workload threads |
| `valSizeRange` | 64–1MB | Item size distribution |
| `valSizeRangeProbability` | 50/25/15/7/3% | Probability weights for each size range |

---

## Simulator Mode (no cachebench required)

`generate_trace_csv.py` can produce a synthetic trace without running cachebench:

```bash
# Default: 100 small + 5 large items, seed 42 → fdp_trace_run001.csv
python3 cachelib/cachebench/test_configs/generate_trace_csv.py

# Predefined run profiles
python3 cachelib/cachebench/test_configs/generate_trace_csv.py --run 1  # 100 small, 5 large
python3 cachelib/cachebench/test_configs/generate_trace_csv.py --run 2  # 60 small, 20 large

# Custom counts and seed
python3 cachelib/cachebench/test_configs/generate_trace_csv.py --small 80 --large 15 --seed 7

# Custom output file
python3 cachelib/cachebench/test_configs/generate_trace_csv.py --out my_trace.csv
```

---

## Using a Real NVMe Device or Enabling io_uring / FDP

### Current setup (this repo's default)

| Setting | Value | Reason |
|---------|-------|--------|
| `nvmCachePaths` | `/dev/shm/cachelib_navy.bin` | tmpfs RAM disk — no hardware needed |
| `navyEnableIoUring` | `false` | tmpfs does not support `O_DIRECT` which io_uring requires |
| `deviceEnableFDP` | `false` | FDP requires NVMe FDP hardware |

### io_uring on a real filesystem or device

Change `mixed_workload.json`:

```json
"navyEnableIoUring": true,
"nvmCachePaths": ["/tmp/cachelib_navy.bin"]
```

And in `run_test.sh`, replace the `truncate` line:

```bash
truncate -s 512M /tmp/cachelib_navy.bin
```

> `O_DIRECT` is required by io_uring mode. It works on ext4, xfs, and block
> devices but **not** on tmpfs (`/dev/shm`). The bpftrace probes are unaffected
> by which I/O engine is used — they sit in userspace above the I/O layer.

### Raw NVMe block device (e.g. `/dev/nvme0n1`)

Change `mixed_workload.json`:

```json
"nvmCachePaths": ["/dev/nvme0n1"],
"nvmCacheSizeMB": <device size in MB>,
"navyEnableIoUring": true
```

Remove the `truncate` line from `run_test.sh` (block devices don't need pre-allocation).

The bpftrace probes work identically — `FileDevice::writeImpl` is called
regardless of backing storage. Write sizes and offsets in the trace reflect
CacheLib's logical view (4 KB BigHash buckets, 1 MB BlockCache chunks); actual
NAND-level writes depend on the drive's internal FTL.

### FDP (Flexible Data Placement) on an FDP-capable NVMe drive

FDP allows the host to hint which data stream a write belongs to, reducing
write amplification by keeping hot and cold data in separate NAND blocks.

Change `mixed_workload.json`:

```json
"navyEnableIoUring": true,
"deviceEnableFDP": true,
"nvmCachePaths": ["/dev/ng0n1"]
```

> FDP uses the NVMe character device (`ng0n1`), not the block device (`nvme0n1`),
> because placement hints require NVMe passthrough commands via io_uring.

**bpftrace with FDP:** The `FileDevice::write` probe still captures all writes
with correct sizes and offsets. The FDP placement hint is attached to the
io_uring SQE by `prepFdpUringCmdSqe` — this function exists in the binary but
**bpftrace 0.9.x on x86_64 cannot read its arguments** (they are passed on the
stack past the 6-register limit). Capturing FDP stream IDs requires
bpftrace ≥ 0.14 (which adds stack-argument support) or a custom BPF program.

### Compatibility summary

| Scenario | Probes work? | Notes |
|----------|-------------|-------|
| tmpfs `/dev/shm`, no io_uring, no FDP | Yes | Default config; no hardware needed |
| Real filesystem (ext4/xfs), io_uring | Yes | Set `navyEnableIoUring: true`; change path off `/dev/shm` |
| Raw NVMe block device, io_uring | Yes | Remove `truncate` line; set path to `/dev/nvme0n1` |
| NVMe FDP device, io_uring + FDP | Mostly yes | `FileDevice::write` captured; FDP stream IDs need bpftrace ≥ 0.14 |

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `trace_output.csv` is empty or has only `Attaching N probes...` | bpftrace took longer than 3 s to attach; cachebench finished before probes were live | Increase the `sleep 3` in `run_test.sh` to `sleep 5` |
| `Fatal Python error: init_fs_encoding` from bpftrace | `/usr/local/bin/bpftrace` AppImage tries to use a Nix Python store that doesn't exist | Use `sudo /usr/bin/bpftrace` (apt package) |
| `ERROR: x86_64 doesn't support arg6` | A probe reads an argument passed on the stack (arg6+), not supported in bpftrace 0.9.x | Remove or rewrite that probe; use a different probe point |
| `Failed to open with o-direct` log line | The backing path is on tmpfs which doesn't support `O_DIRECT` | Expected and harmless when io_uring is disabled; fix by moving to a real filesystem if io_uring is needed |
| `Created 0 keys` / segfault in cachebench | `numKeys` missing from `test_config` in the JSON | Add `"numKeys": 1000000` to `test_config` |
| Stale bpftrace processes filling up disk | Previous runs were killed without cleaning up their bpftrace child processes | Run `sudo kill $(pgrep bpftrace)` to clear them |
