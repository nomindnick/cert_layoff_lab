# CLAUDE.md

Spike repo: corpus + extraction pipeline over California OAH certificated-employee
(teacher) layoff decisions (Ed. Code §§ 44949/44955) and the expert-written annual
"Layoff Decision Summaries" that catalog them.

**Read `PROJECT_CONTEXT.md` first.** It is the full project brief — who the user is,
the two corpora, the capability ladder (search → summary regeneration → case insight →
theory generation), and why schema richness is the load-bearing decision. This file is
only the operational quick reference.

## Data hygiene (non-negotiable)

- `Cert_Layoffs_Docs_V1/` (corpus) and `output/` (derived) are **untracked and must
  stay that way** — proprietary CPRA documents containing respondent names.
- Respondent names are private-layer only: analytics and anything published key on
  pseudonymous refs (`R1..Rn`), district, and ALJ — matching the human volumes'
  de-identification convention.
- Never hand-edit data; every cleanup step is a script (the spike must transfer to a
  production repo and re-run unchanged on the full dataset).

## Pipeline stages (run in order; all idempotent/resumable)

| Stage | Script | Output |
|---|---|---|
| 0 Inventory | `pipeline/inventory.py` | `output/inventory/` — manifest, coverage map, gold-overlap years |
| 1a Summary taxonomy | `pipeline/summaries_taxonomy.py` | `output/summaries/taxonomy.json`, drift matrix |
| 1b Summary holdings | `pipeline/summaries_holdings.py` | `output/summaries/holdings.jsonl` (3,678 gold holdings), `case_index.jsonl` |
| — Model bake-off | `pipeline/bakeoff.py` | `output/bakeoff/report.md` — scored vs `schema/examples/` fixtures |
| 2 Extraction | `pipeline/extract.py run --year YYYY` | `output/corpus/decisions/{case}.json` (merged), `raw/` (per case/pass/model cache), `failed/` (quarantine) |
| 3 Eval | `pipeline/eval_2009.py` | recovery vs gold 2009 volume, over-recovery queue, taxonomy escape rate |

Run with `.venv/bin/python` (deps: striprtf, jsonschema; system: poppler, antiword,
libreoffice). Text extraction is cached by content hash under `output/cache/text/`.
Resumability granularity: delete a `raw/` file to re-run one (case, pass, model);
delete a `decisions/` file to re-merge.

## Extraction architecture

- **N small passes, never a mega-prompt**: identity / dispositions / holdings, each
  with its own prompt in `pipeline/prompts/` (versioned: `holdings_v2.txt` etc.).
- **Two-model ensemble** (`qwen3.6:27b` primary, `gemma4:31b` secondary — chosen by
  bake-off). Primary supplies values; secondary fills nulls (flagged); divergences
  logged in `provenance.reconciliation.disagreements`.
- **Quote anchors**: every assertion carries a verbatim string locatable in
  `full_text`. Verified deterministically at merge; unverified anchors are flagged,
  never deleted.
- **Extract raw, normalize later**: the LLM never canonicalizes districts/ALJs/firms
  inline; `canonical`/`canonical_id` fields are filled by deterministic passes from
  curated maps.
- Schema: `schema/decision_record.schema.json` (v0.1.x). **`schema/DESIGN.md` records
  the rationale for every schema decision — read it before changing the schema.**
  `additionalProperties: false` throughout; nullable fields, not absent keys.
- Taxonomy governance: Level-1 categories are frozen (from the 1979–2017 volumes;
  `pks_allowed`/`pks_not_allowed` deliberately merged into outcome-neutral
  `pks_reduction`). The model proposes (`category_proposed`, `subtype_is_novel`);
  only a human mints new vocabulary.
- `schema/examples/` holds hand-filled worked examples (built before any LLM ran) —
  they are simultaneously schema tests, few-shot exemplars, and eval fixtures.

## Local inference gotchas (ollama 0.23.2)

- `/api/chat` **silently ignores** `format` (structured outputs) — use
  `/api/generate`. Wire helpers live in `bakeoff.py` (`call_ollama`).
- `pattern` keywords crash ollama's JSON-schema→grammar converter —
  `strip_unsupported()` removes them from wire schemas.
- qwen models need `think: false` or thinking tokens eat the output budget.
- gpt-oss:120b cannot do constrained decoding at all; mistral-medium/qwen-122b
  over-split holdings under the v2 prompt (precision collapse) — see
  `output/bakeoff/report.md` before changing the model mix.

## Domain conventions

- Decision year = OAH case-number prefix; decision date parsed from the document's
  `DATED:` line — **never** file mtime or upload date (the spike dataset is a
  temporally scrambled slice; no completeness conclusions from it).
- Gold summaries are *editorial*, not exhaustive: extraction recovering holdings the
  volume omitted is expected over-recovery, not a false positive. Empty `holdings[]`
  on stipulated/default matters is signal, not failure.
- Citation convention is district + ALJ surname (e.g. "San Diego Unified (Levy)").

## Working with the user

See "Who I am" in PROJECT_CONTEXT.md: California public-agency attorney and
hybrid attorney-engineer. Don't over-explain basics; do flag tradeoffs and
downstream consequences; honest pushback is valued over agreement. The schema and
extraction quality are the deliberate, load-bearing work — resist shortcuts there.
