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

# Pre-allocate the backing file on the RAM disk.
truncate -s 512M "$STORAGE_FILE"

echo "Starting bpftrace capture -> $OUTPUT_CSV"
sudo bpftrace -e '
uprobe:'"$CACHEBENCH_BIN"':*navy*BigHash*insert* {
    printf("%u, BigHash::insert\n", elapsed / 1000000);
}
uprobe:'"$CACHEBENCH_BIN"':*navy*BlockCache*insert* {
    printf("%u, BlockCache::insert\n", elapsed / 1000000);
}
uprobe:'"$CACHEBENCH_BIN"':*prepFdpUringCmdSqe* {
    printf("%u, prepFdpUringCmdSqe, Size: %lu, Offset: %lx, PID: %u\n",
           elapsed / 1000000, arg2, arg3, arg6);
}
uprobe:'"$CACHEBENCH_BIN"':*Driver*insert* {
    printf("%u, Driver::insert\n", elapsed / 1000000);
}
' > "$OUTPUT_CSV" &

BPF_PID=$!

echo "Running cachebench with config: $CONFIG_FILE"
"$CACHEBENCH_BIN" --config "$CONFIG_FILE"

kill "$BPF_PID" 2>/dev/null || true
rm -f "$STORAGE_FILE"

echo "Test complete. Trace saved to $OUTPUT_CSV"
echo "To convert to parsed CSV run:"
echo "  python3 $(dirname "$0")/generate_trace_csv.py $OUTPUT_CSV"
