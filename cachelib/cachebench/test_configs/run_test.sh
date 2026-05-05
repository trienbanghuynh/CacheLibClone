#!/bin/bash
# Run a mixed-workload cachebench trace capturing BigHash, BlockCache, and FDP dispatch events.
# Requires: bpftrace, a built cachebench binary.
# Output: trace_output.csv in the current directory.

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

CONFIG_FILE="${CONFIG_FILE:-$(dirname "$0")/mixed_workload.json}"
OUTPUT_CSV="trace_output.csv"
STORAGE_FILE="/dev/shm/cachelib_navy.bin"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
STATS_FILE="cachebench_stats_${TIMESTAMP}.txt"
RESULTS_FILE="cachebench_results_${TIMESTAMP}.txt"

# Pre-allocate the backing file on the RAM disk.
truncate -s 512M "$STORAGE_FILE"

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
echo "  Stats file : $STATS_FILE"
echo "  Results log: $RESULTS_FILE"
"$CACHEBENCH_BIN" --json_test_config "$CONFIG_FILE" \
  --progress_stats_file "$STATS_FILE" \
  2>&1 | tee "$RESULTS_FILE"

kill "$BPF_PID" 2>/dev/null || true
rm -f "$STORAGE_FILE"

echo "Test complete."
echo "  Trace  : $OUTPUT_CSV"
echo "  Stats  : $STATS_FILE"
echo "  Results: $RESULTS_FILE"
echo "To convert trace run:"
echo "  python3 $(dirname "$0")/generate_trace_csv.py $OUTPUT_CSV"
