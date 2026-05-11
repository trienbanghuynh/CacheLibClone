# CacheLib Navy I/O Trace Guide

Capture and analyze CacheLib Navy's write path — BigHash bucket flushes,
BlockCache region writes, and kernel I/O events — using bpftrace uprobes on
a live `cachebench` workload.

---

## Files

| File | Purpose |
|------|---------|
| `run_test.sh` | Loops over six navySmallItemMaxSize configs; runs cachebench for each and writes a combined `cachebench_run_<size>_<timestamp>.txt` (progress stats + full results merged). No bpftrace. |
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

### 2. Kernel requirements

- Linux kernel 4.14+ (standard on Ubuntu 20.04+)
- `sudo` access (required to run cachebench against raw NVMe devices; not needed for in-memory mode)

---

## Step-by-Step: Run the Tests

### Step 1 — Run cachebench across all navySmallItemMaxSize configs

Edit `CACHEBENCH_BIN` at the top of `run_test.sh` to point to your built binary, then:

```bash
cd ~/CacheLib
bash cachelib/cachebench/test_configs/run_test.sh
```

The script will, for each of the six configs (`navySmallItemMaxSize` = 512, 1024, 2048, 3072, 4096, 8148):
1. Run cachebench with `--progress=600` and capture periodic progress stats to a temp file
2. Tee the full results (stdout + stderr) to a second temp file
3. Merge progress stats and full results into one `cachebench_run_<size>_<timestamp>.txt` next to the script
4. Delete the temp files

Expected runtime: **~5–15 seconds per config** (in-memory mode; no disk I/O).

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

## `run_test.sh` Line-by-Line Walkthrough

### Bash strict mode

```bash
set -euo pipefail
```

| Flag | Meaning |
|------|---------|
| `-e` | Exit immediately if any command returns a non-zero exit code |
| `-u` | Treat unset variables as errors (prevents silent empty-string bugs) |
| `-o pipefail` | A pipeline fails if **any** command in it fails, not just the last one |

### Binary path check

```bash
CACHEBENCH_BIN="/home/rsebenchtop2/cachelib_build/build/cachelib/cachebench/cachebench"

if [ ! -f "$CACHEBENCH_BIN" ]; then
    echo "ERROR: cachebench binary not found. Set CACHEBENCH_BIN= $CACHEBENCH_BIN"
    exit 1
fi
```

The binary path is hardcoded at the top of the script. Edit `CACHEBENCH_BIN` to
point to your own build before running. The `if [ ! -f ... ]` guard exits early
with a clear error rather than failing inside the loop.

### Config array and output file paths

```bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

CONFIGS=(
  "mixed_workload_navySmall_512.json:512"
  "mixed_workload_navySmall_1024.json:1024"
  ...
)
```

`SCRIPT_DIR` resolves the absolute path to the directory containing `run_test.sh`
so that output files always land next to the script regardless of which directory
you run it from.

`CONFIGS` pairs each JSON config filename with its `navySmallItemMaxSize` label
using a `filename:label` format. The loop splits on `:` to get both values:

```bash
CONFIG="${entry%%:*}"   # everything before the first ':'
SIZE="${entry##*:}"     # everything after the last ':'
```

Inside the loop, per-run output paths are derived:

```bash
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
COMBINED_FILE="$SCRIPT_DIR/cachebench_run_${SIZE}_${TIMESTAMP}.txt"
_STATS_TMP=$(mktemp /tmp/cachebench_stats_XXXXXX.txt)
_RESULTS_TMP=$(mktemp /tmp/cachebench_results_XXXXXX.txt)
```

`mktemp` creates two uniquely-named temporary files under `/tmp/` for the
intermediate stats and results output. Using `/tmp/` (instead of writing
directly to the final file) prevents a partial write from corrupting the
combined output if cachebench crashes mid-run. The `XXXXXX` suffix is replaced
by a random string by the OS.

### Running cachebench and capturing output

```bash
sudo env PATH="$PATH" LD_LIBRARY_PATH="${LD_LIBRARY_PATH:-}" "$CACHEBENCH_BIN" \
  --json_test_config="$SCRIPT_DIR/$CONFIG" \
  --progress=600 \
  --progress_stats_file="$_STATS_TMP" \
  2>&1 | tee "$_RESULTS_TMP"
```

`sudo env PATH=... LD_LIBRARY_PATH=...` preserves the current user's `PATH` and
`LD_LIBRARY_PATH` when escalating to root. Without this, `sudo` uses a minimal
environment that may not find shared libraries the binary needs.

`--progress=600` prints an in-progress summary line to stdout every 600 seconds.
For short in-memory runs this fires at most once, but it keeps the terminal from
going silent on long real-device runs.

`--progress_stats_file` tells cachebench to write periodic in-progress statistics
to a separate file so they are not interleaved with the full results.

`2>&1` merges stderr (where cachebench writes log lines like `I0505 ...`) into
stdout so both are captured by `tee`. `tee` writes the combined output to
`_RESULTS_TMP` while also printing it to the terminal in real time.

### Merging output

```bash
{
  echo "=== Cachebench Progress Stats ==="
  cat "$_STATS_TMP"
  echo ""
  echo "=== Cachebench Full Results ==="
  cat "$_RESULTS_TMP"
} > "$COMBINED_FILE"
rm -f "$_STATS_TMP" "$_RESULTS_TMP"
```

The `{ ... } > file` pattern redirects the output of an entire block to a
single file. The two temp files are concatenated into one
`cachebench_run_<size>_<timestamp>.txt` then deleted. This is why the combined
output has both the progress stats and the full results (including
`== NVM Write Distribution ==`) in one place.

---

## How the bpftrace Probe Symbols Were Found

The uprobe strings in `run_test.sh` use **C++ mangled symbol names** — the
internal names the compiler assigns to every function in the binary. Here is
how each symbol was derived and how to find new ones.

### What C++ name mangling is

The C++ compiler encodes a function's full qualified name (namespace, class,
method, argument types) into a single string the linker can use. For example:

```
facebook::cachelib::navy::(anonymous)::FileDevice::writeImpl(size_t, uint32_t, const void*, int)
```

becomes:

```
_ZN8facebook8cachelib4navy12_GLOBAL__N_110FileDevice9writeImplEmjPKvi
```

Breaking it down:

| Segment | Meaning |
|---------|---------|
| `_ZN` | Start of a namespaced C++ symbol |
| `8facebook` | Namespace `facebook` (8 = length) |
| `8cachelib` | Namespace `cachelib` |
| `4navy` | Namespace `navy` |
| `12_GLOBAL__N_1` | Anonymous namespace `(anonymous)` — compiler-generated name |
| `10FileDevice` | Class `FileDevice` |
| `9writeImpl` | Method `writeImpl` |
| `EmjPKvi` | Parameter types: `m`=size_t, `j`=uint32_t, `PKv`=const void*, `i`=int |

### How to look up any symbol in the cachebench binary

```bash
CACHEBENCH_BIN=$(python3 build/fbcode_builder/getdeps.py \
  --allow-system-packages show-inst-dir cachelib)/bin/cachebench

# List all Navy-related symbols (demangled, human-readable)
nm "$CACHEBENCH_BIN" | c++filt | grep "facebook::cachelib::navy" | grep -v " U "

# Find the mangled name for a specific function
nm "$CACHEBENCH_BIN" | grep writeImpl
nm "$CACHEBENCH_BIN" | grep BigHash | grep insert

# Verify a mangled name demangles correctly
echo "_ZN8facebook8cachelib4navy12_GLOBAL__N_110FileDevice9writeImplEmjPKvi" | c++filt
```

### The four probes in `run_test.sh` and why each was chosen

| Mangled symbol | Demangled name | Why probed |
|----------------|---------------|------------|
| `_ZN8facebook8cachelib4navy7BigHash6insertENS0_9HashedKeyENS1_11BufferViewTIKhEEhjj` | `BigHash::insert(HashedKey, BufferView, ...)` | Fires every time an item is routed to BigHash; counts small-item writes |
| `_ZN8facebook8cachelib4navy10BlockCache6insertENS0_9HashedKeyENS1_11BufferViewTIKhEEhjj` | `BlockCache::insert(HashedKey, BufferView, ...)` | Fires every time an item is routed to BlockCache; counts large-item writes |
| `_ZN8facebook8cachelib4navy6Driver6insertENS0_9HashedKeyENS1_11BufferViewTIKhEEhjj` | `Driver::insert(HashedKey, BufferView, ...)` | The dispatcher that calls BigHash or BlockCache; **not captured** — LTO inlines it so no stable uprobe point exists |
| `_ZN8facebook8cachelib4navy12_GLOBAL__N_110FileDevice9writeImplEmjPKvi` | `FileDevice::writeImpl(size_t offset, uint32_t size, const void* data, int)` | Fires on every physical NVM write; exposes write size and byte offset into the device file |

> `FileDevice` lives in an **anonymous namespace** (`_GLOBAL__N_1`) inside
> `Device.cpp`. The compiler gives it this mangled prefix to prevent name
> collisions across translation units. This is why the symbol looks unusual
> compared to `BigHash` and `BlockCache`.

### Adding a new probe

1. Find the mangled symbol: `nm "$CACHEBENCH_BIN" | grep <keyword>`
2. Verify it: `echo "<mangled>" | c++filt`
3. Check argument count: bpftrace 0.9.x on x86_64 only supports up to `arg5`
   (the first 6 registers). Arguments beyond that are on the stack and require
   bpftrace ≥ 0.14.
4. Add the uprobe block to `run_test.sh` following the existing pattern.

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
workload where value sizes are in a narrow range.

---

## Controlling Total NVM Engine Inserts

The `Total NVM engine inserts` line in `== NVM Write Distribution ==` is the
number of items actually written into BigHash or BlockCache. It is always much
smaller than the number of DRAM evictions because two stages independently
filter items before they reach an NVM engine:

```
SET → DRAM → [eviction] → NVM put attempt → [admission] → NVM engine insert
```

### Stage 1 — How many items are evicted from DRAM

| Setting | Effect on evictions |
|---------|-------------------|
| `cacheSizeMB` ↓ | Less DRAM → fills faster → more evictions |
| `numOps` ↑ | More writes → more churn → more evictions |
| `numKeys` ↓ | Fewer unique keys → keys overwrite each other in DRAM instead of evicting old items |
| Larger value sizes | Fill DRAM slab classes faster → more evictions |

### Stage 2 — How many evicted items are admitted to NVM

`navyAdmissionWriteRate` is the main gate. It caps how many bytes per second
can be written to NVM. Once the limit is hit, further inserts are dropped.

| Setting | Effect on admission |
|---------|-------------------|
| `navyAdmissionWriteRate` ↑ | More bytes/sec allowed → higher admission |
| `navyAdmissionWriteRate: 0` | **Disables rate limiting entirely** — every item evicted from DRAM is written to NVM |
| `navyMaxConcurrentInserts` ↑ | More in-flight inserts allowed before queuing backs up |

### Example: maximise NVM inserts (test routing distribution accurately)

```json
"navyAdmissionWriteRate": 0,
"cacheSizeMB": 128,
"numOps": 300000
```

Setting `navyAdmissionWriteRate: 0` admits everything evicted from DRAM. Useful
when you want the `== NVM Write Distribution ==` percentages to reflect actual
routing rather than admission-filtered routing.

### Example: minimise NVM inserts (simulate a write-endurance-limited drive)

```json
"navyAdmissionWriteRate": 10485760,
"cacheSizeMB": 512
```

A lower rate (10 MB/s here) combined with a larger DRAM cache means few items
are evicted and fewer still are admitted — matching a scenario where NVM write
endurance is a hard constraint.

### Observed values from the threshold runs

| Config | DRAM evictions | NVM put attempts | Admitted | Total NVM inserts |
|--------|---------------|-----------------|----------|------------------|
| `threshold_low` (220) | 1,010,851 | 1,010,851 | 3.5% | 35,622 |
| `threshold_mid` (668) | ~1,010,000 | ~1,010,000 | ~3.9% | 39,310 |
| `threshold_high` (4252) | ~1,010,000 | ~1,010,000 | ~3.8% | 38,409 |

The admission rate (~3–4% success) is consistent across all three because the
value size distribution and `navyAdmissionWriteRate` are identical — only the
routing threshold changes, which affects *where* admitted items go, not *how many*.

---

## Workload Configuration (`mixed_workload.json`)

### `cache_config` parameters

| Parameter | Current default | Effect |
|-----------|----------------|--------|
| `cacheSizeMB` | 128 | DRAM cache size. Smaller = fills faster = more evictions to NVM. Must be large enough for at least one slab per allocation class (~96 MB minimum with 65536 B values). |
| `poolRebalanceIntervalSec` | 1 | How often (seconds) the slab allocator rebalances memory across allocation classes. Lower = more responsive to workload shifts; set to 0 to disable. |
| `moveOnSlabRelease` | false | When a slab is released during rebalancing, move its live items to another slab (`true`) or evict them (`false`). `false` is faster but loses the items. |
| `nvmCacheSizeMB` | 1024 | Total NVM cache size in MB. Ignored when `nvmCachePaths: []` (in-memory mock). |
| `navyBlockSize` | 4096 | I/O alignment granularity in bytes. All NVM reads and writes are aligned to this size. Must match the device's physical sector size (4096 for NVMe). |
| `navyRegionSizeMB` | 16 | BlockCache region size. Each region holds items until full, then is written as a unit. Larger = fewer but bigger writes; smaller = more frequent writes. |
| `navyBigHashSizePct` | 10 | Percent of total NVM space reserved for BigHash. The remaining 90% goes to BlockCache. |
| `navySmallItemMaxSize` | 4252 | Routing threshold. Items where `key.size() + nvmItem.totalSize() ≤ this` go to BigHash; larger items go to BlockCache. See routing section for the size formula. |
| `navyBigHashBucketSize` | 8192 | Physical BigHash bucket size in bytes. Each bucket flush = one NVM write of this size. Must satisfy `navySmallItemMaxSize < navyBigHashBucketSize - 44`. |
| `navyAdmissionWriteRate` | 104857600 | NVM write rate limit in bytes/sec (100 MB/s here). Controls how many evicted items are actually admitted to NVM. Set to `0` to disable and admit everything. |
| `navyMaxConcurrentInserts` | 16 | Maximum number of NVM insert operations in-flight at once. Inserts beyond this limit are queued or dropped under heavy load. |
| `navyReaderThreads` | 4 | Thread pool size for NVM read operations (GET path). |
| `navyWriterThreads` | 2 | Thread pool size for NVM write operations (eviction path). |
| `navyEnableIoUring` | false | Use io_uring instead of libaio for NVM I/O. Faster on modern kernels but segfaults on kernel 6.1 with raw block devices. See `nvmCachePaths` section. |
| `deviceEnableFDP` | false | Enable NVMe FDP (Flexible Data Placement) placement hints. Requires `navyEnableIoUring: true` and an FDP-capable NVMe character device. |
| `navyQDepth` | 32 | I/O submission queue depth. Number of I/O requests that can be outstanding to the device at once. Higher = more parallelism but more memory. |

### `test_config` parameters

| Parameter | Current default | Effect |
|-----------|----------------|--------|
| `numOps` | 300000 | Operations **per thread**. Total ops = `numOps × numThreads` (300,000 × 4 = 1,200,000 here). |
| `numThreads` | 4 | Number of concurrent workload threads. Each runs `numOps` operations independently. |
| `numKeys` | 1000000 | Size of the key pool. All operations draw from this fixed set of keys. Controls key reuse: `numKeys` >> total ops = almost no reuse; `numKeys` << total ops = heavy reuse and high DRAM churn. With 1,200,000 total SETs and 1,000,000 keys, each key is written ~1.2 times on average — minimal reuse. |
| `keySizeRange` | [8, 32, 64, 128] | Key size bucket boundaries in bytes. Each bucket covers the range [left, right). |
| `keySizeRangeProbability` | [0.6, 0.3, 0.1] | Probability of sampling a key from each bucket. Must have one fewer entry than `keySizeRange`. Keys are drawn uniformly within the chosen bucket: [8–32), [32–64), or [64–128). |
| `valSizeRange` | [64, 512, 4096, 65536] | Exact value sizes in bytes to sample from. Unlike `keySizeRange`, these are discrete exact values — not ranges. |
| `valSizeRangeProbability` | [0.90, 0.07, 0.02, 0.01] | Probability weight for each exact value size. Must have the same length as `valSizeRange`. Determines both the write mix and (indirectly) which items reach NVM via DRAM eviction. |
| `getRatio` | 0.0 | Fraction of operations that are GET (cache read). Must sum to 1.0 with `setRatio` and `delRatio`. |
| `setRatio` | 1.0 | Fraction of operations that are SET (cache write). |
| `delRatio` | 0.0 | Fraction of operations that are DELETE. |

> **`valSizeRange` and `valSizeRangeProbability` are required.** If omitted,
> cachebench crashes with undefined behavior at runtime when sampling an item size
> from an empty vector. Always set both fields explicitly.

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
