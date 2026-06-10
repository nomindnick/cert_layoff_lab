# Decision Record Schema — Design Rationale

Schema: `decision_record.schema.json` (v0.1.x). This doc records *why* the
schema looks the way it does, so the production build inherits the reasoning,
not just the field list.

## The question the schema optimizes for

> "I'm working a live layoff for District Y before ALJ X, with issues Z on the
> table. What happened in similar matters, what arguments worked, and why?"

That is the rung-3 query (retrieval-and-reason over a case base). Rung 1
(search) and rung 2 (summary regeneration) are projections of whatever serves
rung 3, so every field earns its place by either **retrieving a comparable
matter** or **explaining/predicting how it resolved**. Corpus richness is the
ladder: re-extracting 2,800 documents later is the failure mode to avoid, so
anything that requires *reading the text* is captured now, even if nothing
consumes it yet.

The now-vs-later criterion: **anything that joins on identifiers can be
deferred forever; anything that requires reading the texts cannot.** Board
adoption of proposed decisions and appellate outcomes join on case number —
deferred, no schema slot needed (additive fields are cheap later). In-text
mentions of related litigation require reading — `related_proceedings[]`
exists now.

## Architecture: composable extraction passes

The pipeline is **N small, independent extraction passes** writing into one
per-decision JSON — never one mega-prompt:

| pass | fields | reconciliation policy |
|---|---|---|
| identity | identity.*, procedure.counsel | exact match |
| board_action | board_action.* (incl. artifacts) | exact on enums, semantic on text |
| dispositions | outcome.* | exact match (this is the most error-prone pass; appendix OCR) |
| holdings | holdings[], related_proceedings[] | semantic; many-to-many alignment |
| normalization | *.canonical, *.canonical_id | deterministic (curated maps), no LLM |

Rationale: smaller prompts hold fidelity better; a prompt improvement re-runs
one pass, not the corpus; reconciliation policy can differ per pass. This is
also the structural answer to why batch-everything-into-one-context failed.

## Span grounding

Every extracted assertion (arguments, facts, reasoning, artifacts,
dispositions) carries a `quote_anchor`: a **verbatim string** (OCR errors
included) locatable in `full_text`, plus a section hint. Strings, not
character offsets — offsets break when text is re-extracted. This is the
anti-hallucination control, makes multi-model reconciliation checkable (do
two models anchor to the same passage?), and gives the application verifiable
citations. Legally defensible by construction.

## Extract free, normalize later

Extraction fills `raw` fields verbatim; corpus-wide normalization passes fill
`canonical`/`canonical_id` from curated maps:

- **Districts** → CDS codes (CDE publishes the authoritative list).
- **ALJs** → roster slugs (handles `E. Sawyer`/`Sawyer`, OCR `Engemen`/`Engeman`).
- **Firms** → curated table (mergers and renames across 25 years; OCR variants
  like `Fulmost`/`Fulfrost` already observed).
- **Authorities** → canonical citations.

The LLM never normalizes inline: extraction prompts stay stable, and
normalization re-runs without re-extraction.

## Issue taxonomy governance

Level 1 is the **frozen** canonical vocabulary from the 1979–2017 annual
summaries (35 years of stability is the evidence it's right), with one
deliberate deviation: the outcome-laden pair `pks_allowed`/`pks_not_allowed`
merges into outcome-neutral **`pks_reduction`** — the gold taxonomy conflates
issue with outcome there, which would force the model to pre-judge the ruling
to pick the category. Outcome lives in `ruling.prevailing_party`; eval maps
back deterministically (`pks_reduction` + district prevails ≈ `pks_allowed`).

Level 2 (`subtype`) is a living vocabulary **seeded from the summaries'
letter-subheadings** (already parsed in `output/summaries/holdings.jsonl`).

Governance rule: **the model proposes, only a human mints.** `category` must
come from the enum (`other` is the escape hatch with `category_proposed`);
novel subtypes set `subtype_is_novel`. Both feed a review queue.

Spike deliverable: the **escape rate**. If the historical taxonomy absorbs
≥95% of overlap-year holdings cleanly, it's validated for production; where
it leaks shows exactly what changed post-2017 (e.g. Common Core competency
fights enter as subtypes, not new top levels).

## Respondent handling

- Per-respondent dispositions are worth the extraction cost: bumping chains
  (Griffith → Slade → Farnsworth in Lemon Grove) are *the* interesting fights,
  and "N noticed / M terminated" is an outcome metric holding-level extraction
  misses.
- **Pseudonymous refs** (`R1..Rn`, roster order): analytics key on refs, never
  names — matching the de-identification convention the human volumes
  established (cite by district + ALJ only). `outcome.roster` maps ref→name
  for QA against appendices and is **private-layer only**; any published or
  derived layer drops names.
- `representation` sits on the disposition entry, not the decision: one
  decision routinely has firm-represented, self-represented, and defaulting
  respondents at once, and represented-vs-not is likely a major outcome
  covariate.
- No cross-decision respondent identity graph: PII-heavy, low value. Repeat
  litigation surfaces instead via `related_proceedings` (e.g. collateral
  estoppel from a prior year's writ).
- **Deterministic rosters + `outcome.general_order`** (v0.2.0): mass-layoff
  appendices are flat name tables of 200–400 entries; transcribing them
  through the LLM dispositions pass was the overflow class (both models
  truncated to invalid JSON on 2009030327) and a transposition surface on
  mechanically-extractable data. `pipeline/roster.py` parses the table
  shapes the corpus actually contains; when it succeeds, the dispositions
  pass (prompt `dispositions_v2`) covers only respondents the decision
  discusses individually, and the blanket order ("notice may be given to
  all other respondents…") lands in `outcome.general_order`
  {disposition, applies_to, quote}. The analytic contract: a respondent
  with no individual disposition entry and a non-null `general_order` is
  covered by it; integrity Gate 1 enforces roster↔disposition bijection
  only when `general_order` is null. Table names are recorded verbatim
  (OCR damage intact) — extract raw, normalize later.

## Resolution artifacts

Skip criteria, tie-break criteria, and competency definitions quoted in
decisions are a drafting-practice database nobody has — which language
survives challenge. Caveat recorded so it isn't oversold: **decisions quote
only what is litigated** (Lemon Grove quotes the contested competency
definition verbatim but never the unused tie-breaker), so this is a database
of *challenged* provisions. `status` (`quoted_verbatim` / `summarized_by_alj`
/ `mentioned_only` / `absent`) carries that missingness. The unbiased source
would be board resolutions themselves — public records, future data source,
out of scope (like appellate decisions and board adoptions).

## Holdings

- `holdings[]` empty on uncontested/default matters is **signal, not
  failure** — the rate of uncontested layoffs is itself a result, and it's
  the flip side of the gold volumes' editorial selection (they skipped
  routine decisions; over-recovery against gold is expected, not error).
- `facts` may repeat across holdings; forced cross-references are worse than
  redundancy.
- `summary_style_holding` is extracted directly (one paragraph in the
  compilation house style, taught few-shot from the 3,678 gold holdings).
  This makes rung 2 literally `GROUP BY issue` + concatenate, and eval a
  comparison of like artifacts.
- Eval alignment is **many-to-many**: one ALJ ruling can map to several gold
  holdings and vice versa; the matcher scores semantic overlap, never 1:1.

## Validation strictness

`additionalProperties: false` throughout: model output that invents fields
fails validation loudly. Most fields are nullable — extraction robustness
comes from explicit nulls, not absent keys.

## Worked examples

`schema/examples/` holds hand-filled records (Lemon Grove 2011030915 first)
created *before* any LLM extraction. They are simultaneously schema bug
detection, the prompts' few-shot exemplars, and the first eval fixtures.
