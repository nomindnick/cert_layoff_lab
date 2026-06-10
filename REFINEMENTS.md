# Refinements queue

Known issues and planned improvements that should carry into the production
build. Found during/after the first full-year extraction run (2009, started
2026-06-05); none are urgent enough to interrupt that run.

> **Status update (2026-06-09 hardening session):** #1 ✅ (dynamic num_ctx +
> telemetry in extract.py), #6 ✅ (deterministic rosters via roster.py +
> general_order + body-only dispositions prompt), #7 ✅ (token telemetry in
> raw records; eval surfaces truncation flags), #8 ✅ **root cause was the
> merge, not the model** — the surname-keyed name→ref dict collided on
> same-surname pairs; RefResolver fix dropped Gate 1 from 50 to 5 cases by
> re-merge alone (prompts hardened anyway), #9 ✅ (holdings_v3 anchor gate +
> administrative exclusion + remedies-from-order), #10 ✅ (disambiguation
> block in holdings_v3; worked fixture examples in schema/examples/ still
> open), #5 ✅ resolved by ocr_audit.py — the 1999–2001 OCR layers are
> largely usable (mean 0.16 artifacts/10k chars; 5-file re-OCR queue), but
> **2004 is image-only scans** (57 decisions + the gold volume), OCR'd via
> ocr_pass.py/rapidocr. #11 partially done (edit-distance-1 phantom guard,
> general-order coverage); #2/#3/#4 (throughput, scheduling, tiering) open.

## 1. Context-window truncation on outlier cases (correctness — fix before production)

`call_ollama` uses a fixed `num_ctx: 32768` / `num_predict: 12288`
(`bakeoff.py`), and `extract.py` sends the full decision text with no length
check. For 2009 (193 cases, est. ~3.8 chars/token):

- median case ≈ 6k tokens — fine; p90 ≈ 11.5k — fine.
- **5 cases exceed the safe budget** (32k minus the 12,288 output
  reservation): `2009070348` (~20.6k), `2009020437` (~22.9k), `2009020838`
  (~23.3k), `2009030709` (~28.3k), `2009030719` (~38.2k / 136,834 chars).
- `2009030719` exceeds `num_ctx` outright → ollama silently truncates the
  prompt. The marginal four fit at prompt time but can hit the ceiling
  mid-generation on long outputs (dispositions on big rosters).

**Why this is insidious:** truncation is invisible. The model extracts
nothing from text it never saw; quote anchors all verify (they only come
from seen text); the loss shows up as recall failure on exactly the
highest-value consolidated cases, and the eval would blame extraction
quality instead of truncation.

**Fix (~10 lines):**
1. Size `num_ctx` dynamically: `est = len(text)//3 + prompt_overhead;
   num_ctx = max(32768, est + num_predict + margin)`. Both production models
   are 128k-class; a 64k KV cache costs ~5–8GB extra per model, transient,
   well within the ~46GB GTT headroom.
2. Log loudly whenever a case exceeds the default budget — truncation must
   never be silent. The full production corpus will have bigger outliers
   than this slice.
3. After patching, delete the raw files for the 5 cases above and let the
   resumable design re-extract (~2–3h). Do this AFTER the in-flight 2009 run
   finishes — the running process holds the old code.

## 2. Throughput restructuring (production run is ~19 days as configured)

Observed: ~27 min/case sequential (3 passes × 2 models, one request at a
time), GPU 100% busy. Decode on Strix Halo is memory-bandwidth-bound
(~256 GB/s shared); both models are already co-resident (51GB of 96GB GTT),
so capacity is not the constraint — bandwidth is.

- **Don't expect 2× from simply running both models' requests in parallel** —
  they contend for the same bandwidth.
- **The real lever is batched requests per model** (`OLLAMA_NUM_PARALLEL=2-4`):
  batched decode amortizes each weight read across in-flight sequences.
  Restructure from "per case: model A then model B" to **two worker queues**
  (all qwen passes through one, all gemma through the other, 2–3 concurrent
  requests each). Realistic gain: ~1.5–2.5×, not 4×.
- Caveat: `OLLAMA_NUM_PARALLEL` multiplies KV-cache per model (each slot gets
  its own context). At 32k × 3 slots × 2 models it still fits; combined with
  the dynamic-`num_ctx` fix above, check `ollama ps` after changing it.
- **Validate cheaply before committing:** same 5 cases sequential vs.
  `OLLAMA_NUM_PARALLEL=3`, compare wall-clock.

## 3. Production-run scheduling: duty cycle, box availability, rental

Options for the full ~1,000-case (eventually ~2,800) production run:

- **Continuous local run (~19 days sequential, ~8–12 with batching).**
  Hardware note: sustained 100% GPU on this class of hardware is not
  harmful — it's designed for it, and thermal management throttles
  protectively. The real costs are (a) the box is unavailable for other
  work for weeks, and (b) power draw. "GPU at 100% for 19 days" is a
  workload-scheduling problem, not a hardware-health problem.
- **Year-by-year runs** (`extract.py run --year YYYY` is already shaped for
  this): run a year, pause, use the box, resume. Stretches the calendar
  (~a month+) but costs nothing in engineering and the raw-file cache makes
  it perfectly resumable. Also matches the eval cadence — each completed
  year can be evaluated while the next runs.
- **Nice middle path: nighttime cron.** Kick off at end of workday, SIGINT
  in the morning (resumable by design). Box is free during working hours;
  run proceeds ~12h/day.
- **GPU rental** (RunPod/Lambda/etc.): a single high-bandwidth card
  (~1–2 TB/s vs 256 GB/s) runs the same models roughly 4–8× faster; the
  whole corpus is plausibly a weekend and ~$50–150 of rental. **But:** the
  corpus is proprietary CPRA material containing respondent names — sending
  it to a rented cloud GPU trades away the data-locality guarantee that
  motivated local inference. If considered, scope it to a provider/terms the
  firm is comfortable with, or restrict rental to the public-layer-safe
  subset. Decide deliberately, not for throughput convenience.

## 4. Production-run tiering: uncovered years first, single-model for gold-covered years

Full three-pass × two-model extraction over everything is ~19 days for the
spike slice and ~7 weeks for the full ~2,800-file corpus. Tier instead:

- **Tier 1 (run first): years with NO summary volume** — 1999–2003,
  2018–2025 (~379 cases in the spike slice, ~7 days sequential). No human
  layer exists; these are also the product-critical years (the annual-summary
  effort died ~2017; regenerating it for recent years is the deliverable).
  Keep the two-model ensemble here — no independent check exists.
- **Tier 2 (deferrable): gold-covered years 2004–2017** (~621 cases).
  Candidate for **single-model extraction with the gold volume as the
  reconciliation partner** — the human holdings are a better QA signal than
  a second model, and gold disagreements are already the eval's review
  queue. Roughly halves Tier 2 compute.
- Do NOT skip covered years entirely: the volumes are editorial (noteworthy
  holdings only, routine decisions omitted) and carry none of the rung-3
  fields (arguments, facts, authorities, reasoning, per-respondent
  dispositions). Skipping = re-extraction later, the named failure mode.
- Note: 27 min/case was measured on 2009, a mass-layoff year heavy with
  consolidated cases; off-peak years should run faster.

## 5. `pdf_text` is two populations — era-stratified OCR audit needed

The inventory's "only 60 cases need OCR" undercounts. Sampling shows the
`pdf_text` kind splits by era:

- **1999–2004 (~50 files):** scanned PDFs with embedded OCR text layers —
  extract as "native text" but carry real OCR artifacts ("James a Allen",
  "James R_ Collins", floating paragraph numbers).
- **2011+ (~440 files):** born-digital (OAH e-filing era), clean.

Production plan: era-stratify the OCR-quality audit; pre-2010 `pdf_text`
files are candidates for the olmOCR re-pass alongside the 60 `pdf_scanned`
cases. `alpha_ratio` in the manifest doesn't separate the populations
(0.92–0.95 in both); a better heuristic is decision-year < 2011 + artifact
density (mid-word case flips, stray underscores, orphaned numerals).

## 6. Don't transcribe name appendices through the LLM (architecture — output side of #1)

Surfaced 2026-06-08 when the 2009 run was resumed after a disconnect: 11 cases
failed the dispositions pass with unparseable JSON. Not the disconnect, not the
*input* truncation of #1 — the **output** overflowed. The dispositions pass
emits one entry per rostered respondent, and mass-layoff appendices carry
200–400 names. Under grammar-constrained decoding the only way to get invalid
JSON is length truncation, so the array ran past `num_predict` and closed
mid-entry. qwen's pretty-printed output (~250 chars/entry vs gemma's ~150
compact) burned the budget ~2× faster, so even ~15k-char-input cases overflowed
on a dense appendix.

**Why this is the wrong tool, not just an undersized budget:** transcribing a
flat alphabetical name table is deterministic-parse work — the same category as
district/ALJ/firm normalization, which the architecture already does
deterministically and out-of-band. Spending a 27B model 20–28 minutes to retype
300 names verbatim is slow, token-bloated, overflow-prone, and a fresh
hallucination/transposition surface on data that is mechanically extractable.
The LLM's value is the reasoning-dense holdings pass, not roster stenography.

**Spike patch already applied (gets capstone numbers, NOT the production
answer):**
- `call_ollama` parameterized `num_predict`/`num_ctx`/`seed` (defaults
  unchanged); dispositions pass bumped to 24576/49152 via `PASS_BUDGET` in
  `extract.py`.
- Merge falls back to the secondary model when a primary pass fails to parse,
  logged as `provenance.reconciliation … primary_pass_failed_used_secondary`
  (rescued the single-model failures with no re-run, since gemma's compact JSON
  stayed under the cap).

**Production fix:** extract the roster deterministically. The appendix is a
structured name list (caption/appendix/exhibit) — parse it directly into
`outcome.roster` (and the `R1..Rn` refs) without a model call. Reserve the
dispositions *pass* for the per-respondent disposition/reason/quote on the
subset the decision actually discusses individually, which is small even in
mass-layoff cases (most appendix names are an undifferentiated "terminated"
block covered by a single general order). That collapses the output from
O(roster) back to O(litigated-respondents), eliminates the overflow class
entirely, and removes the transcription-error surface. Pairs with #1: dynamic
`num_ctx` protects the input, deterministic rostering shrinks the output.

## 7. Smaller items

- Raw extraction files don't record ollama's `prompt_eval_count`/
  `eval_count` — capture them in `extract.py`'s raw records. They are the
  direct (not estimated) signal for issue #1 and free throughput telemetry
  for issue #2.
- `eval_2009.py` should exclude/flag the 5 oversized cases until they are
  re-extracted post-fix, so truncation-induced recall loss doesn't pollute
  the recovery metrics.

---

Items #8–#11 surfaced from the 2026-06-09 production-quality audit (39-case
stratified deep audit graded against `full_text`; see `PRODUCTION_QUALITY.md`).
The two highest-leverage gates from that audit are **already implemented** as
merge-time integrity flags in `extract.py` (`roster_ref_violations`,
`remedy_disposition_contradictions`) — they *detect and flag*, never delete. The
items below *reduce the occurrence* of what those gates catch, plus the smaller
structured-field cleanups.

## 8. Dispositions-pass ref-binding bug — same-surname collision (correctness — fix before production)

The dominant disposition-layer defect (audit: ~26% of cases; Gate 1
`ROSTER_REF_BIJECTION_VIOLATION` confirms 50/192). When two roster entries share
a surname (Smith/Smith, Becker/Becker, Myer/Myers, Jackson/Jackson, Sweda/Sweda,
Henderson/Henderson, Vollmer/Vollmar), the dispositions pass duplicates one
`R`-ref and drops the adjacent one. The model **reads the people correctly** — it
is a structured keying failure, not a reading error. When both colliding entries
share a disposition the aggregate tally survives (low severity); when they cross
a disposition bucket or carry a person-specific detail it becomes a wrong-person
attribution that corrupts the record (medium–high).

**Production fix (prompt):** in the dispositions pass, instruct the model to emit
exactly one disposition object per roster ref, **keyed by ref, never by
surname**, and never merge or duplicate two people who share a surname; reinforce
that each `detail`/`reason` string belongs to its specific ref. Add a worked
same-surname few-shot in `schema/examples/`. Pairs with the deterministic roster
extraction in #6 (which removes most of the transcription surface that triggers
the collision in the first place). Gate 1 stays as the safety net regardless.

**Until then:** per-respondent disposition analytics must filter out
`ROSTER_REF_BIJECTION_VIOLATION` cases. Aggregate tallies are unaffected.

## 9. Holding altitude — require an anchor for contested holdings, flag boilerplate (precision — `holdings_v3`)

The audit found a thin band of over-recovery where administrative/boilerplate
dispositions are elevated to litigated-holding status with weak grounding
(positively-assured-attrition recitations, post-hearing-brief housekeeping,
uncontested rescissions — several with empty quote anchors). Faithful, but at the
wrong altitude.

**Fix (prompt, `holdings_v2` → `v3`):** require a non-empty verbatim quote anchor
for any item asserting a *contested adjudicated* ruling (the empty-anchor
requirement auto-catches the boilerplate-as-holding cases), and direct the model
to emit administrative/boilerplate dispositions under a distinct lower-altitude
flag rather than as holdings. This is the largest defect cluster after the
structured-field errors and is concentrated in the over-recovery class (the noted
~2× defect density). Separately, the `ruling.remedies` enum is the single most
reliable precision-defect locus (remedy contradicts disposition while prose and
`prevailing_party` are correct); Gate 2 already flags the `retain`/`rescind`
direction — consider a `v3` instruction to set `remedies` from the holding's
actual order rather than its surface framing.

## 10. Taxonomy boundary disambiguation (precision — low priority)

Rare (2 clear of 112) but systematic where it occurs: miscategorization tracks
two legally-adjacent boundaries — skipping (§44955(c)/(d)) vs bumping/retention
(§44955(b)), and tie_breaking vs seniority. The model conflates them when the
dispositive sub-issue differs from the surface framing (e.g. a "tie-breaking"
caption whose actual disputed issue is whether orientation pay fixes the
seniority date).

**Fix (prompt + example):** add disambiguation guidance keying category on the
*dispositive disputed sub-issue*, with a worked exemplar for each pair in
`schema/examples/`. Cheap given the example-driven design; low frequency so low
priority.

## 11. Structured-field normalizers (deterministic post-process)

Small, mechanical, high-confidence cleanups surfaced by the audit — all
deterministic, all "flag/normalize, preserve raw":

- **Self-consolidation junk:** drop or empty `procedure.consolidated_with` when
  its only element equals the case's own OAH number (≥3 cases).
- **`oah_case_no` format guard:** flag values failing the OAH checksum/format so
  faithfully-copied source typos (`20099030721`) and OCR-space artifacts
  normalize into canonical `case_no` while the raw is preserved.
- **Placeholder-roster coverage gap:** when the adjudicated roster lives in an
  un-reproduced Exhibit A, require `roster_completeness='partial'` AND populate a
  structured `n_adjudicated_estimate` from the decision's own stated count (e.g.
  "20 timely requested hearing") rather than collapsing the roster to a single
  placeholder ref. Forbid a placeholder ref from co-existing with body-named
  litigants who appear in holdings.
- **Phantom-respondent (OCR name-split):** fuzzy near-duplicate surname check
  (edit-distance 1–2 with matching given name, one variant appearing only in an
  exception/appearance line) flags a possible phantom before assigning a distinct
  ref (e.g. Vollmer/Vollmar split into two roster entries).
- **`board_action.artifacts` sibling mismatch:** flag cases where an artifact's
  `text` and `quote` fields describe different subject matter (e.g.
  `skip_criteria.text` holding tiebreak/lottery content while `skip_criteria.quote`
  is correctly skip content). The quote anchor itself is faithful, so this
  escapes anchor verification.
- **Disposition enum precision (nicety, not a blocker):** prefer
  `released_temporary` when the ALJ dismisses "as required by law for temporary
  employees" and `partially_terminated` when only a fraction of FTE is cut, rather
  than defaulting to generic `terminated` (8 coarse-but-right-direction labels in
  the audit).
