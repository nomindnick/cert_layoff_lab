#!/bin/bash
# One-shot driver for the 2026-06-09 hardening session GPU queue.
# Stage 1 (already running when this starts): truncation-case re-extraction.
# Stage 2: holdings_v3 re-run on the missed-gold triage cases.
# Stage 3: 2009 eval + triage refresh.
# Stage 4: 2004 extraction (74 cases: 17 native + 57 OCR) + eval.
set -u
cd "$(dirname "$0")/.."
PY=.venv/bin/python

echo "[chain] waiting for in-flight 2009 re-extraction to finish"
while pgrep -f "extract.py run --year 2009" > /dev/null; do sleep 60; done

echo "[chain] stage 2: holdings_v3 re-run on triage cases"
for c in 2009020299 2009020379 2009020382 2009020539 2009020656 2009030009 \
         2009030024 2009030040 2009030049 2009030199 2009030265 2009030288 \
         2009030746 2009030793 2009030896 2009030899 2009031140 2009031289 \
         2009031322 2009031351 2009040120; do
  rm -f "output/corpus/raw/${c}__holdings__"*.json
done
$PY pipeline/extract.py run --year 2009 > output/corpus/rerun_2009_stage2.log 2>&1 \
  || { echo "[chain] STAGE 2 FAILED"; exit 1; }

echo "[chain] stage 3: 2009 eval + triage"
$PY pipeline/eval_year.py --year 2009 > /dev/null 2>&1 || echo "[chain] eval 2009 FAILED"
$PY pipeline/triage_missed_gold.py --year 2009 > /dev/null 2>&1 || echo "[chain] triage FAILED"
$PY pipeline/extract.py status --year 2009 | tail -2

echo "[chain] stage 4: 2004 extraction"
$PY pipeline/extract.py run --year 2004 > output/corpus/run_2004.log 2>&1 \
  || { echo "[chain] STAGE 4 FAILED"; exit 1; }
$PY pipeline/eval_year.py --year 2004 > /dev/null 2>&1 || echo "[chain] eval 2004 FAILED"
$PY pipeline/extract.py status --year 2004 | tail -2
echo "[chain] DONE"
