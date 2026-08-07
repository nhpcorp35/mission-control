#!/usr/bin/env bash
set -u
set -o pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="$(command -v python3)"
NODE_BIN="$(command -v node || true)"

mkdir -p pipeline/logs

STAMP="$(date +%Y%m%d_%H%M%S)"
RUN_LOG="pipeline/logs/incremental_${STAMP}.log"

SCRAPER_LOG="$(mktemp)"
COPY_LOG="$(mktemp)"
EXTRACT_LOG="$(mktemp)"

echo "== Phase 12 incremental run ==" | tee "$RUN_LOG"
echo "Started: $(date)" | tee -a "$RUN_LOG"
echo "" | tee -a "$RUN_LOG"

SCRAPER_EXIT=0
COPY_EXIT=0
EXTRACT_EXIT=0

if [ -n "$NODE_BIN" ]; then
  echo "-- Step 1: scraper recent mode --" | tee -a "$RUN_LOG"
  TEST_LIMIT=0 "$NODE_BIN" scraper/scraper.js --mode recent 2>&1 | tee "$SCRAPER_LOG" | tee -a "$RUN_LOG"
  SCRAPER_EXIT=${PIPESTATUS[0]}
else
  echo "-- Step 1: scraper recent mode --" | tee -a "$RUN_LOG"
  echo "⚠️ node not found; scraper step skipped" | tee "$SCRAPER_LOG" | tee -a "$RUN_LOG"
  echo "SUMMARY_NEW_PDFS=0" | tee -a "$SCRAPER_LOG" | tee -a "$RUN_LOG"
  echo "SUMMARY_ERRORS=0" | tee -a "$SCRAPER_LOG" | tee -a "$RUN_LOG"
  SCRAPER_EXIT=0
fi
echo "" | tee -a "$RUN_LOG"

echo "-- Step 2: Phase A copy --" | tee -a "$RUN_LOG"
"$PYTHON_BIN" phase_a_copy.py 2>&1 | tee "$COPY_LOG" | tee -a "$RUN_LOG"
COPY_EXIT=${PIPESTATUS[0]}
echo "" | tee -a "$RUN_LOG"

echo "-- Step 3: Phase B extract incremental --" | tee -a "$RUN_LOG"
"$PYTHON_BIN" extract_text.py --mode incremental 2>&1 | tee "$EXTRACT_LOG" | tee -a "$RUN_LOG"
EXTRACT_EXIT=${PIPESTATUS[0]}
echo "" | tee -a "$RUN_LOG"

new_pdfs="$(grep -E '^SUMMARY_NEW_PDFS=' "$SCRAPER_LOG" | tail -1 | cut -d= -f2 | tr -d '[:space:]')"
copied_pdfs="$(grep -E '^SUMMARY_COPIED=' "$COPY_LOG" | tail -1 | cut -d= -f2 | tr -d '[:space:]')"
appended_rows="$(grep -E '^SUMMARY_APPENDED_ROWS=' "$EXTRACT_LOG" | tail -1 | cut -d= -f2 | tr -d '[:space:]')"
total_rows="$(grep -E '^SUMMARY_TOTAL_ROWS=' "$EXTRACT_LOG" | tail -1 | cut -d= -f2 | tr -d '[:space:]')"

scraper_errors="$(grep -E '^SUMMARY_ERRORS=' "$SCRAPER_LOG" | tail -1 | cut -d= -f2 | tr -d '[:space:]')"
copy_errors="$(grep -E '^SUMMARY_ERRORS=' "$COPY_LOG" | tail -1 | cut -d= -f2 | tr -d '[:space:]')"
extract_errors="$(grep -E '^SUMMARY_ERRORS=' "$EXTRACT_LOG" | tail -1 | cut -d= -f2 | tr -d '[:space:]')"
missing_pdf="$(grep -E '^SUMMARY_MISSING_PDF=' "$EXTRACT_LOG" | tail -1 | cut -d= -f2 | tr -d '[:space:]')"

new_pdfs="${new_pdfs:-0}"
copied_pdfs="${copied_pdfs:-0}"
appended_rows="${appended_rows:-0}"
total_rows="${total_rows:-0}"
scraper_errors="${scraper_errors:-0}"
copy_errors="${copy_errors:-0}"
extract_errors="${extract_errors:-0}"
missing_pdf="${missing_pdf:-0}"

pipeline_ok=1

if [ "$COPY_EXIT" -ne 0 ] || [ "$copy_errors" != "0" ]; then
  pipeline_ok=0
fi

if [ "$EXTRACT_EXIT" -ne 0 ] || [ "$extract_errors" != "0" ]; then
  pipeline_ok=0
fi

if [ "$missing_pdf" != "0" ]; then
  pipeline_ok=0
fi

echo "== End-of-run summary ==" | tee -a "$RUN_LOG"
echo "New PDFs downloaded: $new_pdfs" | tee -a "$RUN_LOG"
echo "PDFs copied: $copied_pdfs" | tee -a "$RUN_LOG"
echo "New CSV rows appended: $appended_rows" | tee -a "$RUN_LOG"
echo "Total cases now in CSV: $total_rows" | tee -a "$RUN_LOG"
echo "Errors:" | tee -a "$RUN_LOG"
echo "  scraper: $scraper_errors (exit $SCRAPER_EXIT)" | tee -a "$RUN_LOG"
echo "  phase_a_copy: $copy_errors (exit $COPY_EXIT)" | tee -a "$RUN_LOG"
echo "  extract_text: $extract_errors (exit $EXTRACT_EXIT)" | tee -a "$RUN_LOG"
echo "  missing_pdf: $missing_pdf" | tee -a "$RUN_LOG"

if [ -z "$NODE_BIN" ]; then
  echo "Scraper status: SKIPPED (node not installed)" | tee -a "$RUN_LOG"
fi

if [ "$pipeline_ok" -eq 1 ]; then
  echo "Pipeline status: OK" | tee -a "$RUN_LOG"
  exit_code=0
else
  echo "Pipeline status: FAILED" | tee -a "$RUN_LOG"
  exit_code=1
fi

echo "" | tee -a "$RUN_LOG"
echo "Finished: $(date)" | tee -a "$RUN_LOG"

rm -f "$SCRAPER_LOG" "$COPY_LOG" "$EXTRACT_LOG"
exit "$exit_code"
