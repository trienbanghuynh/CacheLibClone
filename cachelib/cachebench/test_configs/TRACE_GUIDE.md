# CacheLib Navy I/O Trace Guide

Capture and analyze CacheLib Navy's write path — BigHash bucket flushes,
BlockCache region writes, and kernel I/O events — using bpftrace uprobes on
a live `cachebench` workload.

---

## Files

| File | Purpose |
|------|---------|
| `run_test.sh` | Runs cachebench + bpftrace simultaneously; writes raw `trace_output.csv` plus timestamped stats/results files |
| `mixed_workload.json` | Cachebench workload config — NVM cache size, BigHash/BlockCache split, op ratios |
| `generate_trace_csv.py` | Parses and enriches `trace_output.csv` in place; auto-runs `run_test.sh` if no trace is found |
| `trace_output.csv` | **Primary output** — starts as raw bpftrace lines; overwritten in place with enriched 10-column CSV after parsing |
| `fdp_trace.csv` | **Simulator output (legacy)** — synthetic trace produced by `simulate_trace()` inside `generate_trace_csv.py`; not written by the default CLI workflow |

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
1. Pre-allocate the backing storage (RAM disk by default; skipped when using a raw block device)
2. Start bpftrace in the background and wait 3 seconds for uprobes to attach
3. Run cachebench with `mixed_workload.json` (100,000 ops, 4 threads)
4. Kill bpftrace and clean up the backing file
5. Write three output files: `trace_output.csv`, `cachebench_stats_<timestamp>.txt`, `cachebench_results_<timestamp>.txt`

Expected runtime: **~30–60 seconds**.

### Step 2 — Parse into enriched CSV

```bash
python3 cachelib/cachebench/test_configs/generate_trace_csv.py
```

The script auto-detects `trace_output.csv` (in the current directory or `~/CacheLib/`) and enriches it **in place** — the raw bpftrace lines are replaced with the structured 10-column CSV. If no trace file is found, the script automatically runs `run_test.sh` to capture one first.

You can also pass a path explicitly:

```bash
python3 cachelib/cachebench/test_configs/generate_trace_csv.py /path/to/trace_output.csv
```

### Step 3 — Inspect results

```bash
head trace_output.csv
grep "FileDevice" trace_output.csv | head -20
grep "BigHash bucket" trace_output.csv | wc -l    # count BigHash flushes
grep "BlockCache region" trace_output.csv | wc -l # count BlockCache chunks
```

---

## Output CSV Format

After parsing, `trace_output.csv` has 10 columns:

| Column | Description | Example |
|--------|-------------|---------|
| `Seq` | Row number (1-based) | `1`, `2`, `3` |
| `Timestamp (ms)` | Milliseconds since first event | `0.0`, `5.0`, `38.0` |
| `Delta (ms)` | Time since previous event | `0.0`, `1.0` |
| `Thread ID` | OS thread ID that fired the probe | `68477`, `68478` |
| `Function` | Probed function | `BigHash::insert`, `BlockCache::insert`, `FileDevice::write` |
| `Engine` | CacheLib engine classification | `BigHash`, `BlockCache`, `Kernel/IO` |
| `Routing` | Item routing label | `small →BigHash (≤2048B)`, `large →BlockCache (>2048B)` |
| `Write Size` | Human-readable physical write size | `4.0 KB`, `1.0 MB`, `-` |
| `File Offset (B)` | Byte offset into the NVM device file | `506929152`, `-` |
| `Notes` | Descriptive annotation | `BigHash bucket flush (4 KB)` |

### Notes field values

| Notes value | Meaning |
|-------------|---------|
| `BigHash bucket flush (4 KB)` | BigHash accumulated enough items to fill a 4 KB bucket and flushed it to device |
| `BlockCache region chunk (1 MB, split by deviceMaxWriteSize)` | One 1 MB chunk of a 16 MB BlockCache region write (split because `deviceMaxWriteSize=1048576`) |
| `Kernel submits batched io_uring SQEs` | io_uring submission (appears in simulator output only) |

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

## How the BigHash / BlockCache Distribution Works

CacheLib Navy routes every incoming item to one of two engines based purely on
**item size** compared to a fixed threshold:

```
item size ≤ navySmallItemMaxSize  →  BigHash   (optimised for small items)
item size >  navySmallItemMaxSize  →  BlockCache (optimised for large items)
```

There is no randomness in the routing decision. The split percentage is
entirely determined by your workload config — specifically by how many items
fall on each side of the `navySmallItemMaxSize` boundary.

### How to calculate your split

The workload config defines item sizes and their probability weights:

```json
"valSizeRange":            [64,   512,  4096, 65536, 1048576],
"valSizeRangeProbability": [0.50, 0.25, 0.15,  0.07,    0.03]
```

With `navySmallItemMaxSize: 2048`, sum the weights of every size ≤ 2048 B:

```
BigHash    = P(64 B) + P(512 B)              = 0.50 + 0.25 = 0.75  →  75%
BlockCache = P(4 KB) + P(64 KB) + P(1 MB)   = 0.15 + 0.07 + 0.03 = 0.25  →  25%
```

### To change the split, change the config

| Goal | What to change |
|------|---------------|
| More items to BigHash | Lower `navySmallItemMaxSize` OR shift `valSizeRangeProbability` weight toward smaller sizes |
| More items to BlockCache | Raise `navySmallItemMaxSize` OR shift weight toward larger sizes |
| Exact target split | Adjust `valSizeRangeProbability` so weights ≤ threshold sum to your target |

**Example — change to 60% BigHash / 40% BlockCache:**

```json
"valSizeRange":            [64,   512,  4096, 65536, 1048576],
"valSizeRangeProbability": [0.40, 0.20, 0.20,  0.12,    0.08]
```

Check: `P(64) + P(512) = 0.40 + 0.20 = 0.60` → 60% BigHash.

### Why actual counts may differ slightly from the configured split

The theoretical split assumes every item reaches NVM. In practice two factors
can shift the admitted mix:

1. **Admission rate limiter** (`navyAdmissionWriteRate`): limits NVM write
   throughput. Large items consume more bytes per insert, so they are rejected
   proportionally more under pressure, nudging the admitted mix slightly toward
   BigHash.

2. **RAM eviction policy**: items only reach NVM when evicted from DRAM. If
   the DRAM cache is large enough to hold all items, few reach NVM regardless
   of the configured split.

The configured `valSizeRangeProbability` always defines the *intended* routing
split. Monitor the actual split by checking `NVM Puts` counts in the cachebench
output after a run.

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

> **`valSizeRange` and `valSizeRangeProbability` are required.** There is no
> built-in default distribution. If omitted, cachebench passes the startup
> validation check (both fields being empty satisfies `size() == size()`) but
> crashes with undefined behavior at runtime when it tries to sample an item
> size from an empty vector. Always set both fields explicitly.

---

## Simulator Mode (no cachebench required)

`generate_trace_csv.py` contains a `simulate_trace()` function that generates a
synthetic trace without running cachebench. The CLI no longer exposes simulator
flags — calling the script always works with real bpftrace output. To use the
simulator directly, call it from a Python script or interactive session:

```python
from cachelib.cachebench.test_configs.generate_trace_csv import simulate_trace, write_csv

rows = simulate_trace(n_small=80, n_large=15)
write_csv(rows, "fdp_trace.csv")
```

The `fdp_trace_*.csv` files you may see in the repo were produced this way.
They are synthetic — sizes and offsets are randomly generated — and exist only
for illustration. **Prefer real traces from `run_test.sh`** for any meaningful
analysis.

To customise the simulation, edit these values inside `simulate_trace()`:

| Parameter | Default | Effect |
|-----------|---------|--------|
| `n_small` | 100 | Number of small items (64 B – 2 KB); each goes to BigHash |
| `n_large` | 5 | Number of large items (2 KB – 6 MB); each goes to BlockCache |
| `SMALL_ITEM_MAX` | 2048 | Size threshold (must match `navySmallItemMaxSize` in JSON config) |

---

## Using a Real NVMe Device or Enabling io_uring / FDP

### Current setup (this repo's default)

The repo is currently configured to run on a real NVMe device with io_uring and
FDP disabled (the safest tested combination on kernel 6.1):

| Setting | Value | Reason |
|---------|-------|--------|
| `nvmCachePaths` | `/dev/nvme0n1` | Raw NVMe block device |
| `nvmCacheSizeMB` | `102400` | 100 GB slice of the NVMe device |
| `navyEnableIoUring` | `false` | io_uring segfaults on raw block devices under kernel 6.1 (see note below) |
| `deviceEnableFDP` | `false` | FDP requires io_uring, which is disabled |

> **io_uring on raw block devices (kernel 6.1):** Enabling `navyEnableIoUring: true`
> with a raw block device path (`/dev/nvme0n1`) causes a segfault inside
> `FileDevice::getIoContext()` → `FileDevice::writeImpl` → `RegionManager::doFlushInternal`.
> This has been observed on kernel 6.1 and is likely a compatibility issue between
> CacheLib's io_uring implementation and the kernel version. Disable io_uring
> (`navyEnableIoUring: false`) to work around it. The bpftrace probes are unaffected.

To revert to a RAM disk (no hardware required), change `mixed_workload.json`:

```json
"nvmCachePaths": ["/dev/shm/cachelib_navy.bin"],
"nvmCacheSizeMB": 512,
"navyEnableIoUring": false,
"deviceEnableFDP": false
```

And in `run_test.sh`, ensure the `truncate` line reads:

```bash
truncate -s 512M /dev/shm/cachelib_navy.bin
```

> `O_DIRECT` is required by io_uring mode. It works on ext4 and xfs but
> **not** on tmpfs (`/dev/shm`). The bpftrace probes are unaffected
> by which I/O engine is used — they sit in userspace above the I/O layer.

### io_uring on a real filesystem

If you want to enable io_uring without the raw block device crash, use a file on
a real filesystem (ext4 or xfs):

```json
"navyEnableIoUring": true,
"nvmCachePaths": ["/tmp/cachelib_navy.bin"],
"nvmCacheSizeMB": 512
```

And in `run_test.sh`:

```bash
truncate -s 512M /tmp/cachelib_navy.bin
```

### Raw NVMe block device (e.g. `/dev/nvme0n1`)

```json
"nvmCachePaths": ["/dev/nvme0n1"],
"nvmCacheSizeMB": <device size in MB>,
"navyEnableIoUring": false
```

Remove the `truncate` line from `run_test.sh` (block devices don't need pre-allocation).

The bpftrace probes work identically — `FileDevice::writeImpl` is called
regardless of backing storage. Write sizes and offsets in the trace reflect
CacheLib's logical view (4 KB BigHash buckets, 1 MB BlockCache chunks); actual
NAND-level writes depend on the drive's internal FTL.

### FDP (Flexible Data Placement) on an FDP-capable NVMe drive

FDP allows the host to hint which data stream a write belongs to, reducing
write amplification by keeping hot and cold data in separate NAND blocks.

To check if your drive supports FDP:

```bash
nvme id-ctrl /dev/nvme0n1 | grep ctratt
# bit 19 set (value & 0x80000 != 0) means FDP is supported
```

Change `mixed_workload.json`:

```json
"navyEnableIoUring": true,
"deviceEnableFDP": true,
"nvmCachePaths": ["/dev/ng0n1"]
```

> FDP uses the NVMe character device (`ng0n1`), not the block device (`nvme0n1`),
> because placement hints require NVMe passthrough commands via io_uring.
> **Note:** FDP requires io_uring to be enabled, which currently segfaults on
> kernel 6.1 with raw block/char devices (see above). FDP cannot be exercised
> until the io_uring crash is resolved.

**bpftrace with FDP:** The `FileDevice::write` probe still captures all writes
with correct sizes and offsets. The FDP placement hint is attached to the
io_uring SQE by `prepFdpUringCmdSqe` — this function exists in the binary but
**bpftrace 0.9.x on x86_64 cannot read its arguments** (they are passed on the
stack past the 6-register limit). Capturing FDP stream IDs requires
bpftrace ≥ 0.14 (which adds stack-argument support) or a custom BPF program.

### Compatibility summary

| Scenario | Probes work? | Notes |
|----------|-------------|-------|
| tmpfs `/dev/shm`, no io_uring, no FDP | Yes | No hardware needed; change `nvmCachePaths` back to `/dev/shm/cachelib_navy.bin` |
| Real filesystem (ext4/xfs), io_uring | Yes | Set `navyEnableIoUring: true`; path must be on ext4/xfs, not tmpfs |
| Raw NVMe block device, no io_uring | Yes | **Tested and working** — current default config on `/dev/nvme0n1` |
| Raw NVMe block device, io_uring | No | Segfaults in `FileDevice::getIoContext()` on kernel 6.1 |
| NVMe FDP device, io_uring + FDP | Blocked | FDP requires io_uring; blocked by the io_uring crash above |

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `trace_output.csv` is empty or has only `Attaching N probes...` | bpftrace took longer than 3 s to attach; cachebench finished before probes were live | Increase the `sleep 3` in `run_test.sh` to `sleep 5` |
| `Fatal Python error: init_fs_encoding` from bpftrace | `/usr/local/bin/bpftrace` AppImage tries to use a Nix Python store that doesn't exist | Use `sudo /usr/bin/bpftrace` (apt package) |
| `ERROR: x86_64 doesn't support arg6` | A probe reads an argument passed on the stack (arg6+), not supported in bpftrace 0.9.x | Remove or rewrite that probe; use a different probe point |
| `Failed to open with o-direct` log line | The backing path is on tmpfs which doesn't support `O_DIRECT` | Expected and harmless when io_uring is disabled; fix by moving to a real filesystem if io_uring is needed |
| `Created 0 keys` / segfault in cachebench | `numKeys` missing from `test_config` in the JSON | Add `"numKeys": 1000000` to `test_config` |
| Segfault in `FileDevice::getIoContext` / `RegionManager::doFlushInternal` | io_uring enabled on a raw block device under kernel 6.1 | Set `"navyEnableIoUring": false` in `mixed_workload.json` |
| Stale bpftrace processes filling up disk | Previous runs were killed without cleaning up their bpftrace child processes | Run `sudo kill $(pgrep bpftrace)` to clear them |
