#!/bin/bash
# Run mixed-workload cachebench across navySmallItemMaxSize variants, capturing
# BigHash, BlockCache, and FDP dispatch events via bpftrace per run.
# Requires: bpftrace, a built cachebench binary.
# Output per config: trace_output_<size>.csv and cachebench_run_<size>_TIMESTAMP.txt

set -euo pipefail
CACHEBENCH_BIN="/home/rsebenchtop2/cachelib_build/build/cachelib/cachebench/cachebench"

if [ ! -f "$CACHEBENCH_BIN" ]; then
    echo "ERROR: cachebench binary not found. Set CACHEBENCH_BIN= $CACHEBENCH_BIN"
    exit 1
fi


SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

CONFIGS=(
  "mixed_workload_navySmall_512.json:512"
  "mixed_workload_navySmall_1024.json:1024"
  "mixed_workload_navySmall_2048.json:2048"
  "mixed_workload_navySmall_3072.json:3072"
  "mixed_workload_navySmall_4096.json:4096"
  "mixed_workload_navySmall_8192.json:8148"
)

for entry in "${CONFIGS[@]}"; do
  CONFIG="${entry%%:*}"
  SIZE="${entry##*:}"
  TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
  OUTPUT_CSV="$SCRIPT_DIR/trace_output_${SIZE}.csv"
  PROGRESS_LOG="$SCRIPT_DIR/cachebench-progress-${SIZE}.log"
  COMBINED_FILE="$SCRIPT_DIR/cachebench_run_${SIZE}_${TIMESTAMP}.txt"
  _STATS_TMP=$(mktemp /tmp/cachebench_stats_XXXXXX.txt)
  _RESULTS_TMP=$(mktemp /tmp/cachebench_results_XXXXXX.txt)

  echo "=========================================="
  echo "Running: $CONFIG (navySmallItemMaxSize=$SIZE)"
  echo "  Trace  : $OUTPUT_CSV"
  echo "  Output : $COMBINED_FILE"
  echo "=========================================="
  

  sudo env PATH="$PATH" LD_LIBRARY_PATH="${LD_LIBRARY_PATH:-}" "$CACHEBENCH_BIN" \
    --json_test_config="$SCRIPT_DIR/$CONFIG" \
    --progress=600 \
    --progress_stats_file="$_STATS_TMP" \
    2>&1 | tee "$_RESULTS_TMP"

  # kill "$BPF_PID" 2>/dev/null || true

  # Merge progress stats and full results into one file.
  {
    echo "=== Cachebench Progress Stats ==="
    cat "$_STATS_TMP"
    echo ""
    echo "=== Cachebench Full Results ==="
    cat "$_RESULTS_TMP"
  } > "$COMBINED_FILE"
  rm -f "$_STATS_TMP" "$_RESULTS_TMP"

  echo "Done: $CONFIG"
  # echo "  Trace  : $OUTPUT_CSV"
  # echo "  Output : $COMBINED_FILE"
  # echo "To convert trace run:"
  # echo "  python3 $SCRIPT_DIR/generate_trace_csv.py $OUTPUT_CSV"
  echo ""
done

echo "All runs complete."
