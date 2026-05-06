#!/bin/bash
# Run a mixed-workload cachebench trace capturing BigHash, BlockCache, and FDP dispatch events.
# Requires: bpftrace, a built cachebench binary.
# Output: trace_output.csv and cachebench_run_TIMESTAMP.txt written next to this script.

set -euo pipefail

# Auto-detect cachebench from getdeps install dir if not set
if [ -z "${CACHEBENCH_BIN:-}" ]; then
  GETDEPS_INST=$(python3 "$(dirname "$0")/../../../build/fbcode_builder/getdeps.py" \
    --allow-system-packages show-inst-dir cachelib 2>/dev/null || true)
  if [ -n "$GETDEPS_INST" ] && [ -f "$GETDEPS_INST/bin/cachebench" ]; then
    CACHEBENCH_BIN="$GETDEPS_INST/bin/cachebench"
  else
    echo "ERROR: cachebench binary not found. Set CACHEBENCH_BIN=/path/to/cachebench"
    exit 1
  fi
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG_FILE="${CONFIG_FILE:-$SCRIPT_DIR/mixed_workload.json}"
OUTPUT_CSV="$SCRIPT_DIR/trace_output.csv"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
COMBINED_FILE="$SCRIPT_DIR/cachebench_run_${TIMESTAMP}.txt"
_STATS_TMP=$(mktemp /tmp/cachebench_stats_XXXXXX.txt)
_RESULTS_TMP=$(mktemp /tmp/cachebench_results_XXXXXX.txt)

echo "Starting bpftrace capture -> $OUTPUT_CSV"
sudo /usr/bin/bpftrace -e '
uprobe:'"$CACHEBENCH_BIN"':_ZN8facebook8cachelib4navy7BigHash6insertENS0_9HashedKeyENS1_11BufferViewTIKhEEhjj {
    printf("%u, BigHash::insert, %d\n", elapsed / 1000000, tid);
}
uprobe:'"$CACHEBENCH_BIN"':_ZN8facebook8cachelib4navy10BlockCache6insertENS0_9HashedKeyENS1_11BufferViewTIKhEEhjj {
    printf("%u, BlockCache::insert, %d\n", elapsed / 1000000, tid);
}
uprobe:'"$CACHEBENCH_BIN"':_ZN8facebook8cachelib4navy6Driver6insertENS0_9HashedKeyENS1_11BufferViewTIKhEEhjj {
    printf("%u, Driver::insert, %d\n", elapsed / 1000000, tid);
}
uprobe:'"$CACHEBENCH_BIN"':_ZN8facebook8cachelib4navy12_GLOBAL__N_110FileDevice9writeImplEmjPKvi {
    printf("%u, FileDevice::write, %d, Size: %u, Offset: %lu\n",
           elapsed / 1000000, tid, (uint32)arg2, arg1);
}
' > "$OUTPUT_CSV" &

BPF_PID=$!

# Wait for bpftrace to compile and attach uprobes before starting the workload.
sleep 3

echo "Running cachebench with config: $CONFIG_FILE"
echo "  Output: $COMBINED_FILE"
"$CACHEBENCH_BIN" --json_test_config "$CONFIG_FILE" \
  --progress_stats_file "$_STATS_TMP" \
  2>&1 | tee "$_RESULTS_TMP"

kill "$BPF_PID" 2>/dev/null || true

# Merge progress stats and full results into one file.
{
  echo "=== Cachebench Progress Stats ==="
  cat "$_STATS_TMP"
  echo ""
  echo "=== Cachebench Full Results ==="
  cat "$_RESULTS_TMP"
} > "$COMBINED_FILE"
rm -f "$_STATS_TMP" "$_RESULTS_TMP"

echo "Test complete."
echo "  Trace : $OUTPUT_CSV"
echo "  Output: $COMBINED_FILE"
echo "To convert trace run:"
echo "  python3 $SCRIPT_DIR/generate_trace_csv.py $OUTPUT_CSV"
