# Workload Distribution Analysis

Two analysis modes in `generate_trace_csv.py` that report BigHash vs BlockCache
routing — without needing bpftrace event counts passed in manually.

---

## Why this matters

The bpftrace trace tells you *what happened* at the I/O level. The distribution
analysis tells you *why* — how the workload config routes items between the two
Navy engines, what write volumes to expect, and how the actual runtime deviates
from the theoretical model due to factors like the admission rate limiter.

---

## Prerequisites

**For `--analyze-config` only** — just Python 3, no cachebench run needed.

**For `--from-stats`** — add one line to `mixed_workload.json` cache_config:

```json
"printNvmCounters": true
```

This makes cachebench emit a `== NVM Counters Map ==` block in its final output
containing per-engine counters (`navy_bh_inserts`, `navy_bc_inserts`,
`navy_bh_physical_written`, etc.). The `run_test.sh` script already saves this
output to `cachebench_stats.txt` via `tee`.

---

## Mode A: Static analysis from config

Reads `mixed_workload.json` and computes the expected routing split from
`valSizeRange`, `valSizeRangeProbability`, and `navySmallItemMaxSize`.
**No cachebench run required.**

```bash
python3 cachelib/cachebench/test_configs/generate_trace_csv.py \
  --analyze-config cachelib/cachebench/test_configs/mixed_workload.json
```

### Example output

```
=== Workload Distribution Analysis — from config ===
Config : cachelib/cachebench/test_configs/mixed_workload.json
Params : navySmallItemMaxSize=2048B  bucketSize=4.0 KB  regionSize=16MB  deviceMaxWriteSize=1.0 MB
Ops    : 400,000  (100,000 ops × 4 threads)

Item size     Weight  Engine        Expected inserts
------------------------------------------------------------------------
64 B           50.0%  BigHash                200,000
512 B          25.0%  BigHash                100,000
4.0 KB         15.0%  BlockCache              60,000
64.0 KB         7.0%  BlockCache              28,000
1.0 MB          3.0%  BlockCache              12,000

Engine        Share   Exp. inserts    Avg item     Logical    Physical   Write amp
------------------------------------------------------------------------
BigHash       75.0%        300,000       213 B     61.0 MB     61.0 MB       1.00x
BlockCache    25.0%        100,000    143.2 KB  13984.4 MB  13984.4 MB       1.00x

Engine        Flush/chunk unit   Expected flushes/chunks
------------------------------------------------------------------------
BigHash                 4.0 KB                    15,625  bucket flushes
BlockCache              1.0 MB                    13,984  region chunks  (874.0 full regions)
```

### How the routing split is computed

Items are routed by size against `navySmallItemMaxSize`:

| Condition | Engine |
|-----------|--------|
| item size ≤ `navySmallItemMaxSize` | BigHash |
| item size > `navySmallItemMaxSize` | BlockCache |

For the default config (`navySmallItemMaxSize=2048`, discrete distribution):

```
BigHash   = P(64B) + P(512B)  = 50% + 25% = 75%
BlockCache = P(4KB) + P(64KB) + P(1MB) = 15% + 7% + 3% = 25%
```

Average item sizes are probability-weighted means within each engine's range:

```
avg_small = (64 × 0.50 + 512 × 0.25) / 0.75  ≈  213 B
avg_large = (4096 × 0.15 + 65536 × 0.07 + 1048576 × 0.03) / 0.25  ≈  143.2 KB
```

Expected flush/chunk counts come from dividing total logical bytes written by
the flush unit size (`navyBigHashBucketSize` for BigHash; `deviceMaxWriteSize`
for BlockCache).

### Note on theoretical write amplification

The static model assumes write amplification of 1.00x for both engines because
it only accounts for logical-to-physical at the region/bucket granularity. In
practice, BigHash WA is much higher (see Mode B below) because small items
leave empty space inside each 4KB bucket, and the full bucket is always written
even when partially filled.

---

## Mode B: Runtime stats from cachebench output

Parses `cachebench_stats.txt` (the saved cachebench stdout) and reports actual
counters from the `== NVM Counters Map ==` section.

```bash
# Step 1: run the workload (saves stats automatically)
bash cachelib/cachebench/test_configs/run_test.sh

# Step 2: analyze
python3 cachelib/cachebench/test_configs/generate_trace_csv.py \
  --from-stats cachebench_stats.txt
```

### Example output

```
=== Workload Distribution Analysis — from runtime stats ===
Stats  : cachebench_stats.txt

Engine          Inserts   Share   Succ inserts   Items cached
------------------------------------------------------------------------
BigHash          10,652   72.2%         10,652          6,650
BlockCache        4,111   27.8%          4,111          2,465
Total            14,763  100.0%

  Driver: 14,763 accepted  /  193,288 rejected (back-pressure)

Engine        Logical written   Physical written   Write amp   BH evictions
------------------------------------------------------------------------
BigHash                3.9 MB            57.2 MB      14.63x              0
BlockCache           149.7 MB           144.0 MB       0.96x
```

### Counter sources

| Counter | Source in codebase |
|---------|-------------------|
| `navy_bh_inserts` / `navy_bh_succ_inserts` | `BigHash.cpp` → `insertCount_` |
| `navy_bc_inserts` / `navy_bc_succ_inserts` | `BlockCache.cpp` → `insertCount_` |
| `navy_bh_logical_written` | `BigHash.cpp` → `logicalWrittenCount_` |
| `navy_bh_physical_written` | `BigHash.cpp` → `physicalWrittenCount_` |
| `navy_bc_logical_written` | `BlockCache.cpp` → `logicalWrittenCount_` |
| `navy_bc_physical_written` | `RegionManager.cpp` → `physicalWrittenCount_` |
| `navy_accepted` / `navy_rejected` | `Driver.cpp` — admission control |

---

## Mode C: Combined — theory vs actual

Run both flags together to get a side-by-side comparison:

```bash
python3 cachelib/cachebench/test_configs/generate_trace_csv.py \
  --analyze-config cachelib/cachebench/test_configs/mixed_workload.json \
  --from-stats cachebench_stats.txt
```

### Example theory vs actual table

```
=== Theory vs Actual ===
Metric                              Expected        Actual       Delta
------------------------------------------------------------------------
BigHash insert share                   75.0%         72.2%      -2.8pp
BlockCache insert share                25.0%         27.8%      +2.8pp
BigHash logical written              61.0 MB        3.9 MB        -94%
BigHash physical written             61.0 MB       57.2 MB         -6%
BigHash write amplification            1.00x        14.63x      +13.63
BlockCache logical written        13984.4 MB      149.7 MB        -99%
BlockCache physical written       13984.4 MB      144.0 MB        -99%
BlockCache write amplification         1.00x         0.96x       -0.04
```

### Interpreting the deltas

**Routing split (~75/25 expected, ~72/28 actual): small delta, expected**

The admission controller (`navyAdmissionWriteRate: 100 MB/s`) rejected
193,288 out of ~207,000 total NVM-bound inserts due to write rate back-pressure.
Because large items consume more bytes, they hit the rate limit harder, slightly
shifting the mix toward BigHash. The routing proportions are still close to
theoretical.

**Logical written: -94% to -99% vs expected**

The theoretical model assumes all 400,000 ops reach NVM. In practice only
14,763 were admitted (~3.7%). The rest were rejected by the `navyAdmissionWriteRate`
write-rate limiter and stayed in DRAM. This is expected and intentional — the
admission policy protects NVM from being overwhelmed.

To see more inserts reach NVM: increase `navyAdmissionWriteRate`, reduce
`numOps`, or reduce item sizes so the rate limit is hit less quickly.

**BigHash physical written close to theoretical (-6%): coincidental**

BigHash physical writes (bucket flushes) depend on how many buckets fill up,
not how many items were admitted. With a 4KB bucket and 213B average item, one
flush per ~19 logical inserts. The total physical bytes match the theoretical
value closely even though logical volume is 94% lower.

**BigHash write amplification: 1.00x expected → 14.63x actual**

This is the most important finding. The theoretical model only counts the
bucket-level write (4KB per flush). The actual WA of 14.63x means that each
logical byte written to BigHash causes 14.63 bytes of physical I/O. This
happens because:

1. BigHash buckets (4KB) are only partially filled by small items (avg 213B)
2. A full bucket write is issued even when the bucket is sparse
3. Each 4KB flush carries only 4096 ÷ 14.63 ≈ 280B of new logical data on average

To reduce BigHash WA: increase `navyBigHashBucketSize` (more items per flush
reduces per-item overhead), or skew the workload toward larger small items
closer to `navySmallItemMaxSize`.

**BlockCache write amplification: 1.00x expected → 0.96x actual**

BlockCache WA is nearly 1.0x as expected — it writes full 16MB regions
sequentially, so physical ≈ logical. The slight sub-1.0x value reflects
that some region space is reclaimed before being fully written (region
reclaim thread was active).

---

## Quick reference

| Goal | Command |
|------|---------|
| Expected distribution from config alone | `--analyze-config mixed_workload.json` |
| Actual distribution after a run | `--from-stats cachebench_stats.txt` |
| Both, with delta table | `--analyze-config mixed_workload.json --from-stats cachebench_stats.txt` |
| Parse bpftrace events to enriched CSV | `generate_trace_csv.py trace_output.csv` |
| Simulate trace without cachebench | `generate_trace_csv.py` (no args) |
