# CacheLib Navy I/O Trace Guide

Capture and analyze CacheLib Navy's write path — BigHash bucket flushes,
BlockCache region writes, and kernel I/O events — using bpftrace uprobes on
a live `cachebench` workload.

---

## Files

| File | Purpose |
|------|---------|
| `run_test.sh` | Runs cachebench + bpftrace simultaneously; writes `trace_output.csv` and a combined `cachebench_run_<timestamp>.txt` (stats + results merged) |
| `mixed_workload.json` | Cachebench workload config — NVM cache size, BigHash/BlockCache split, op ratios |
| `generate_trace_csv.py` | Parses and enriches `trace_output.csv` in place; auto-runs `run_test.sh` if no trace is found |
| `trace_output.csv` | **Primary output** — starts as raw bpftrace lines; overwritten in place with enriched 10-column CSV after parsing |
| `cachebench_run_<timestamp>.txt` | **Combined run output** — progress stats followed by full cachebench results, including the `== NVM Write Distribution ==` section |

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
1. Start bpftrace in the background and wait 3 seconds for uprobes to attach
2. Run cachebench with `mixed_workload.json` (300,000 ops, 4 threads)
3. Kill bpftrace and merge progress stats + full results into one `cachebench_run_<timestamp>.txt`
4. Write two output files next to the script: `trace_output.csv` and `cachebench_run_<timestamp>.txt`

Expected runtime: **~5–15 seconds** (in-memory mode; no disk I/O).

### Step 2 — Parse into enriched CSV

```bash
python3 cachelib/cachebench/test_configs/generate_trace_csv.py
```

The script auto-detects `trace_output.csv` and enriches it **in place** — raw
bpftrace lines are replaced with the structured 10-column CSV. If no trace file
is found, the script automatically runs `run_test.sh` to capture one first.

You can also pass a path explicitly:

```bash
python3 cachelib/cachebench/test_configs/generate_trace_csv.py /path/to/trace_output.csv
```

### Step 3 — Inspect results

```bash
# View the combined run output (stats + NVM write distribution)
cat cachelib/cachebench/test_configs/cachebench_run_<timestamp>.txt

# Inspect the enriched trace
head trace_output.csv
grep "BigHash bucket" trace_output.csv | wc -l    # count BigHash flushes
grep "BlockCache region" trace_output.csv | wc -l # count BlockCache chunks
```

---

## NVM Write Distribution Output

Every cachebench run prints a `== NVM Write Distribution ==` section that shows
exactly how many items were routed to each NVM engine:

```
== NVM Write Distribution ==
  BigHash   (items <= smallItemMaxSize) :     41,865 inserts ( 96.6%)
  BlockCache (items > smallItemMaxSize) :      1,470 inserts (  3.4%)
  Total NVM engine inserts              :     43,335
```

This section appears in both the terminal output and in `cachebench_run_<timestamp>.txt`.
It is populated from the `navy_bh_inserts` and `navy_bc_inserts` Navy counters,
which count every item actually written into each engine (after DRAM eviction and
NVM admission).

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
| `Routing` | Item routing label | `small →BigHash (≤4252B)`, `large →BlockCache (>4252B)` |
| `Write Size` | Human-readable physical write size | `8.0 KB`, `1.0 MB`, `-` |
| `File Offset (B)` | Byte offset into the NVM device file | `506929152`, `-` |
| `Notes` | Descriptive annotation | `BigHash bucket flush (8 KB)` |

> `Driver::insert` events are **not captured** — the compiler inlines this
> function with LTO optimization so no stable uprobe point exists.

---

## How the BigHash / BlockCache Distribution Works

CacheLib Navy routes every incoming item to one of two engines based on
**effective item size** compared to a fixed threshold:

```
key.size() + nvmItem.totalSize()  <=  navySmallItemMaxSize  →  BigHash
key.size() + nvmItem.totalSize()   >  navySmallItemMaxSize  →  BlockCache
```

> **Important:** The routing uses `nvmItem.totalSize()`, not the raw value size.
> `nvmItem.totalSize()` includes the NvmItem struct header overhead:
> ```
> nvmItem.totalSize() = sizeof(NvmItem) + sizeof(BlobInfo) + raw_value_bytes
>                     =       20        +        8         + raw_value_bytes
>                     = raw_value_bytes + 28
> ```
> So to route a 4096 B value with a max 128 B key to BigHash, the threshold must
> be at least `128 + (4096 + 28) = 4252`. This is exactly what the current config uses.

There is no randomness in the routing decision. The split is entirely determined
by your workload config.

### How to calculate your expected split

The workload config defines item sizes and probability weights:

```json
"valSizeRange":            [64,  512, 4096, 65536],
"valSizeRangeProbability": [0.90, 0.07, 0.02, 0.01]
```

With `navySmallItemMaxSize: 4252`, any value ≤ 4096 B fits under the threshold
(since `4096 + 28 + max_key ≤ 4252`), so:

```
BigHash    = P(64 B) + P(512 B) + P(4096 B)  = 0.90 + 0.07 + 0.02 = 0.99  →  99%
BlockCache = P(65536 B)                       = 0.01                →   1%
```

### To change the split, change the config

| Goal | What to change |
|------|---------------|
| More items to BigHash | Raise `navySmallItemMaxSize` OR shift `valSizeRangeProbability` weight toward smaller sizes |
| More items to BlockCache | Lower `navySmallItemMaxSize` OR shift weight toward larger sizes |
| Exact target split | Adjust `valSizeRangeProbability` so weights ≤ threshold sum to your target |

**Example — target 75% BigHash / 25% BlockCache:**

```json
"valSizeRange":            [64,   512,  4096, 65536],
"valSizeRangeProbability": [0.50, 0.25, 0.25, 0.25]
```

> Remember to make the probabilities sum to 1.0.

### Why actual NVM counts differ from the configured split

The theoretical split assumes every item reaches NVM. In practice two factors
cause the admitted distribution to differ — often significantly:

1. **DRAM eviction size bias**: items only reach NVM when evicted from DRAM.
   Large items occupy more DRAM slab space and fill their slab class faster,
   so they get evicted at a higher *rate* than small items. A 65536 B item takes
   ~1000× the DRAM space of a 64 B item, so it is evicted far more often even if
   it represents only 1% of writes. This pushes the NVM mix toward BlockCache
   regardless of the configured probability weights.

2. **Admission rate limiter** (`navyAdmissionWriteRate`): limits NVM write
   throughput in bytes/sec. Large items consume more bytes per insert and are
   rejected proportionally more under pressure, which nudges the admitted mix
   back slightly toward BigHash.

To get NVM counts that closely match the configured probability split, use a
small DRAM cache (so all size classes are evicted at similar rates) and a
workload where value sizes are in a narrow range. The example run in this repo
(`cachebench_run_20260505_221431.txt`) achieves 96.6% BigHash / 3.4% BlockCache
with a 99%/1% configured split — the small gap is from the DRAM eviction bias
pushing some large items through.

---

## Workload Configuration (`mixed_workload.json`)

Key parameters you may want to tune:

| Parameter | Current default | Effect |
|-----------|----------------|--------|
| `cacheSizeMB` | 128 | DRAM cache size; smaller = more DRAM evictions, more NVM traffic |
| `nvmCacheSizeMB` | 1024 | NVM cache size (ignored when `nvmCachePaths: []`) |
| `navySmallItemMaxSize` | 4252 | Items with `key+nvmItem.totalSize ≤ this` go to BigHash |
| `navyBigHashBucketSize` | 8192 | BigHash flush granularity; must be > `navySmallItemMaxSize + 44` |
| `navyBigHashSizePct` | 10 | Percent of NVM space reserved for BigHash |
| `navyRegionSizeMB` | 16 | BlockCache region size; each region = 16 × 1 MB writes |
| `numOps` | 300000 | Operations per thread |
| `numThreads` | 4 | Concurrent workload threads |
| `valSizeRange` | 64–65536 | Item value sizes (exact sizes sampled from this list) |
| `valSizeRangeProbability` | 90/7/2/1% | Probability weights for each size |

> **`valSizeRange` and `valSizeRangeProbability` are required.** There is no
> built-in default distribution. If omitted, cachebench passes the startup
> validation check but crashes with undefined behavior at runtime when it tries
> to sample an item size from an empty vector. Always set both fields explicitly.

> **`navyBigHashBucketSize` constraint:** `navySmallItemMaxSize` must be less than
> `navyBigHashBucketSize - 44` (44 bytes of BigHash bucket header overhead).
> With `navyBigHashBucketSize: 8192`, the maximum valid `navySmallItemMaxSize` is 8148.

---

## Setting `nvmCachePaths`

`nvmCachePaths` controls where Navy writes its NVM data. The routing between
BigHash and BlockCache is **identical** across all options — only the physical
write destination changes.

### Option 1 — In-memory mock (current default)

```json
"nvmCachePaths": []
```

Navy uses an internal memory buffer instead of any device. No hardware required,
no pre-allocation needed. Writes are instant. Best for testing routing logic,
distribution tuning, and functional correctness.

### Option 2 — RAM disk file

```json
"nvmCachePaths": ["/dev/shm/cachelib_navy.bin"],
"nvmCacheSizeMB": 512,
"navyEnableIoUring": false
```

Pre-allocate before running:

```bash
truncate -s 512M /dev/shm/cachelib_navy.bin
```

> `O_DIRECT` (required by io_uring) does not work on tmpfs (`/dev/shm`).
> Keep `navyEnableIoUring: false`.

### Option 3 — Regular filesystem file (ext4 / xfs)

```json
"nvmCachePaths": ["/tmp/cachelib_navy.bin"],
"nvmCacheSizeMB": 512,
"navyEnableIoUring": false
```

Pre-allocate before running:

```bash
truncate -s 512M /tmp/cachelib_navy.bin
```

Set `navyEnableIoUring: true` if you want io_uring I/O — ext4/xfs support
`O_DIRECT` correctly. Keep `false` on kernel 6.1 with raw block devices (see below).

### Option 4 — Raw NVMe block device

```json
"nvmCachePaths": ["/dev/nvme0n1"],
"nvmCacheSizeMB": 102400,
"navyEnableIoUring": false,
"deviceEnableFDP": false
```

No pre-allocation needed (block devices don't require it). Navy writes directly
to the raw device, bypassing the filesystem.

> **io_uring on kernel 6.1:** Enabling `navyEnableIoUring: true` with a raw block
> device causes a segfault inside `FileDevice::getIoContext()` on kernel 6.1.
> Keep `navyEnableIoUring: false` unless you are on a newer kernel.

### Option 5 — NVMe character device with FDP

```json
"nvmCachePaths": ["/dev/ng0n1"],
"nvmCacheSizeMB": 102400,
"navyEnableIoUring": true,
"deviceEnableFDP": true
```

FDP (Flexible Data Placement) allows the host to hint which data stream a write
belongs to, reducing write amplification by keeping hot and cold data in separate
NAND blocks. FDP uses the NVMe character device (`ng0n1`), not the block device
(`nvme0n1`), because placement hints require NVMe passthrough commands via io_uring.

Check if your drive supports FDP:

```bash
nvme id-ctrl /dev/nvme0n1 | grep ctratt
# bit 19 set (value & 0x80000 != 0) means FDP is supported
```

> **Note:** FDP requires io_uring, which currently segfaults on kernel 6.1 with
> raw block/char devices. FDP cannot be exercised until the io_uring crash is resolved.

### Option 6 — RAID-0 stripe across multiple devices

```json
"nvmCachePaths": ["/dev/nvme0n1", "/dev/nvme1n1"],
"nvmCacheSizeMB": 204800,
"navyEnableIoUring": false
```

Navy stripes writes across all listed devices. All paths must point to devices
of equal size. `nvmCacheSizeMB` should reflect the total combined capacity.

### nvmCachePaths compatibility summary

| `nvmCachePaths` value | io_uring | FDP | Notes |
|-----------------------|----------|-----|-------|
| `[]` (in-memory) | N/A | No | No hardware needed; current default |
| `/dev/shm/...` (tmpfs) | Must be `false` | No | `O_DIRECT` not supported on tmpfs |
| `/tmp/...` (ext4/xfs file) | `true` or `false` | No | Safest option for io_uring testing |
| `/dev/nvme0n1` (block device) | `false` on kernel 6.1 | No | Segfaults with io_uring on kernel 6.1 |
| `/dev/ng0n1` (char device) | `true` required | Yes | Blocked by io_uring crash on kernel 6.1 |
| Multiple paths (RAID-0) | `false` on kernel 6.1 | No | Equal-size devices required |

The bpftrace probes work identically across all options — `FileDevice::writeImpl`
is called regardless of backing storage (except in-memory mode, where no
`FileDevice::write` events are generated since the Device layer is bypassed).

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

To customise the simulation, edit these values inside `simulate_trace()`:

| Parameter | Default | Effect |
|-----------|---------|--------|
| `n_small` | 100 | Number of small items (64 B – 4 KB); each goes to BigHash |
| `n_large` | 5 | Number of large items (> 4 KB); each goes to BlockCache |
| `SMALL_ITEM_MAX` | 4252 | Size threshold (must match `navySmallItemMaxSize` in JSON config) |

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `trace_output.csv` is empty or has only `Attaching N probes...` | bpftrace took longer than 3 s to attach; cachebench finished before probes were live | Increase the `sleep 3` in `run_test.sh` to `sleep 5` |
| `Fatal Python error: init_fs_encoding` from bpftrace | `/usr/local/bin/bpftrace` AppImage tries to use a Nix Python store that doesn't exist | Use `sudo /usr/bin/bpftrace` (apt package) |
| `ERROR: x86_64 doesn't support arg6` | A probe reads an argument passed on the stack (arg6+), not supported in bpftrace 0.9.x | Remove or rewrite that probe; use a different probe point |
| `Failed to open with o-direct` log line | The backing path is on tmpfs which doesn't support `O_DIRECT` | Expected and harmless when io_uring is disabled; fix by moving to ext4/xfs if io_uring is needed |
| `Created 0 keys` / segfault in cachebench | `numKeys` missing from `test_config` in the JSON | Add `"numKeys": 1000000` to `test_config` |
| Segfault in `FileDevice::getIoContext` / `RegionManager::doFlushInternal` | io_uring enabled on a raw block device under kernel 6.1 | Set `"navyEnableIoUring": false` in `mixed_workload.json` |
| `small item max size should not exceed N` error at startup | `navySmallItemMaxSize` exceeds `navyBigHashBucketSize - 44` | Lower `navySmallItemMaxSize` or raise `navyBigHashBucketSize` |
| NVM Write Distribution section missing from output | No items reached the NVM engines (DRAM cache held everything) | Reduce `cacheSizeMB` or increase `numOps` to force DRAM evictions |
| Stale bpftrace processes filling up disk | Previous runs were killed without cleaning up their bpftrace child processes | Run `sudo kill $(pgrep bpftrace)` to clear them |
