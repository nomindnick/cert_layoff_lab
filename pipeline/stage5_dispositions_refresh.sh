#!/bin/bash
# Stage 5: dispositions refresh after the roster parser expansion.
# The wider deterministic rosters expose v1-era dispositions outputs that
# never covered appendix names (Gate 1) and 2004 outputs produced with the
# full-roster prompt before the parser knew their formats. Re-running the
# dispositions pass (now body-only + general_order on det-roster cases) is
# cheap and closes both.
set -u
cd "$(dirname "$0")/.."
PY=.venv/bin/python

until grep -q "^\[chain\] DONE" output/corpus/chain.log 2>/dev/null; do sleep 120; done

echo "[chain] stage 5: dispositions refresh (Gate-1 2009 + det-roster 2004)"
$PY - <<'EOF'
import json, glob, sys
sys.path.insert(0, 'pipeline')
from pathlib import Path
from inventory import cached_text
from extract import CACHE, RAW, select_cases
from roster import parse_roster

todo = set()
for p in glob.glob('output/corpus/decisions/2009*.json'):
    d = json.load(open(p))
    res = {x['resolution'] for x in d['provenance']['reconciliation']['disagreements']}
    if 'ROSTER_REF_BIJECTION_VIOLATION' in res:
        todo.add(Path(p).stem)
for c in select_cases('2004', None):
    if parse_roster(cached_text(c['best']['sha1'], CACHE)):
        todo.add(c['case_no'])
n = 0
for case in sorted(todo):
    for f in RAW.glob(f"{case}__dispositions__*.json"):
        f.unlink(); n += 1
print(f"stage5: cleared {n} dispositions raws across {len(todo)} cases")
EOF
$PY pipeline/extract.py run --year 2009 >> output/corpus/rerun_2009_stage2.log 2>&1 \
  || echo "[chain] STAGE 5 (2009) FAILED"
$PY pipeline/extract.py run --year 2004 >> output/corpus/run_2004.log 2>&1 \
  || echo "[chain] STAGE 5 (2004) FAILED"
echo "[chain] stage 5 merges done; final evals"
$PY pipeline/eval_year.py --year 2009 > /dev/null 2>&1 || echo "[chain] eval 2009 FAILED"
$PY pipeline/triage_missed_gold.py --year 2009 > /dev/null 2>&1 || true
$PY pipeline/eval_year.py --year 2004 > /dev/null 2>&1 || echo "[chain] eval 2004 FAILED"
$PY pipeline/extract.py status --year 2009 | tail -1
$PY pipeline/extract.py status --year 2004 | tail -1
echo "[chain] STAGE 5 DONE"
