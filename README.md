# cert_layoff_lab

Experimental spike for building a corpus + extraction pipeline over California
OAH certificated-employee (teacher) layoff decisions — Education Code
§§ 44949/44955 — and the expert-written annual "Layoff Decision Summaries"
that catalog them.

This repo is **pipeline code only**. The underlying documents (obtained via
CPRA) and all derived outputs are deliberately untracked: the corpus is
proprietary and contains respondent names. See `PROJECT_CONTEXT.md` for the
full project brief, goals, and architecture.

## Pipeline stages

| Stage | Script | Output |
|---|---|---|
| 0. Inventory | `pipeline/inventory.py` | `output/inventory/manifest.json`, `report.md` — per-file format/quality/classification, dedupe groups, coverage map |
| 1a. Summary taxonomy | `pipeline/summaries_taxonomy.py` | `output/summaries/taxonomy.json`, `taxonomy_drift.md` — top-level issue taxonomy per annual summary, canonical mapping, drift matrix |
| 1b. Summary holdings | `pipeline/summaries_holdings.py` | `output/summaries/holdings.jsonl`, `case_index.jsonl`, `report_holdings.md` — per-holding records (category path, text, district/ALJ cites, QA flags) parsed from every volume |

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install striprtf
# system tools: pdftotext/pdfinfo (poppler), antiword, libreoffice
```

Run stages in order; each is idempotent and caches extraction by content hash
under `output/cache/`:

```bash
.venv/bin/python pipeline/inventory.py
.venv/bin/python pipeline/summaries_taxonomy.py
```

Corpus location defaults to `Cert_Layoffs_Docs_V1/` and is configurable via
`--corpus-root` (FPPC pattern: pipeline and data stay separate, so the same
code re-runs unchanged on the full production dataset).
