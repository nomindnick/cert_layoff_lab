# Production quality assessment (2009 slice)

Status as of 2026-06-09, based on the completed 2009 extraction run (192/193
decisions) plus a 39-case stratified deep audit graded against each record's own
`full_text`. Scope caveat: one year, a temporally-scrambled slice, directional
not census. Numbers below will move on the full corpus, but the *failure modes*
are expected to generalize.

## Bottom line

**Close to production. The hard part works; the gap is a localized, mechanical
one in the structured disposition/remedy layer, and all of it is
deterministically detectable.** Content extraction is production-grade: zero
hallucinated holdings across 112 audited, 191/191 quote anchors both locatable
and actually supporting their claim, identity backbone clean in 37/39 cases. No
model change is recommended anywhere — the remaining work is post-process gates
and prompt tightening.

## How quality was measured

Two independent lenses, because they answer different questions:

1. **Gold-overlap eval** (`pipeline/eval_2009.py`) — *recall* against the
   expert-written 2009 Layoff Decision Summaries. Structurally can only measure
   "did we recover what the editors catalogued."
2. **39-case deep audit** (multi-agent, graded against `full_text`) — *precision
   and faithfulness*, which the gold eval cannot see. Each record carries its own
   source text, so an auditor can check an extraction against its own decision
   with no external gold. Stratified across mega-roster / mixed-outcome contested
   / zero-holding default / holding-rich / typical.

## Scorecard

| Dimension | Sample | Raw defect | Severity-weighted | High-sev | Verdict |
|---|---|---|---|---|---|
| Anchor faithfulness | 191 anchors | 0.5% | ~0% | 0 | **Ready** |
| Holding precision | 112 holdings | 16.1% | ~5.1% | 0 | Minor fixes |
| Identity + disposition | 186 items | 17.7% | — | 9 | Minor fixes |
| Gold recovery (recall) | 277 scored | — | — | — | 81% overall / **85% exact-cite** |

## Findings by dimension

### Anchor faithfulness — ready
191/191 anchors both locate verbatim and support the claim they are attached to.
Zero contradictions, misattachments, or absent quotes. The brittle cases held:
pipe-delimited FTE table-row anchors and `...`-elided spans all graded faithful,
and over-recovery anchors carried the *same* 0% defect rate as gold-matched ones.
**This validates the anti-hallucination architecture** (quote anchors verified
deterministically at merge). The only recommended action is not to regress the
verifier.

### Holding precision — minor fixes
Zero hallucinated holdings across 112. 94 sound, 14 sound-but-flagged (faithful,
minor structured-field nit), 2 miscategorized, 2 partially-supported; zero
high-severity. The 16.1% raw item-defect rate is ~5.1% severity-weighted.

- **Over-recovery is faithful, as designed.** Defect density is ~2× on
  over-recovery (19.7% vs 10.9% matched) but these are substantively grounded
  editorial over-recoveries, not false positives. Net precision on the
  load-bearing gold-matched holdings is **~89%**.
- Main defect locus is the structured `ruling.remedies` enum contradicting the
  disposition (prose and `prevailing_party` correct, only the enum wrong) — now
  gated (see below). Secondary: `affected_respondents` ref drift on mega-rosters;
  a thin band of administrative boilerplate elevated to holding status; rare
  miscategorization on legally-adjacent pairs (skipping/bumping,
  tie_breaking/seniority).

### Identity + disposition — minor fixes (weakest dimension)
Identity backbone near-perfect (district / ALJ / case-no / date clean in 37/39).
The real issue is the **dispositions-pass ref-binding bug**: adjacent
same-surname respondents (Smith/Smith, Becker/Becker, Jackson/Jackson…) cause one
`R`-ref to be duplicated and another dropped in **~26% of cases**. The model
*reads the people correctly*; the structured ref array drifts. Aggregate tallies
usually survive; **per-respondent disposition lookups corrupt** on the affected
cases. One case had genuinely wrong-direction dispositions (6 retained
respondents coded terminated) — and it contradicted its own holdings layer, which
is exactly what Gate 2 keys on.

### Gold recovery (recall) — 81% / 85% exact-cite
After removing measurement noise (curly-quote anchor false-fails, editorial
citation lines, competency label-scheme mismatch) and segregating
out-of-scope default rule-statements, recovery is **223/277 (81%)** overall and
**44/52 (85%)** on the trustworthy exact-OAH-cite tier. The earlier "69%" was
mostly measurement artifact, not extraction failure.

## What was hardened in this pass

All in `pipeline/extract.py` and `pipeline/eval_2009.py`, re-merged over the full
2009 set, no schema change:

1. **Anchor verifier Unicode/whitespace normalization** — unverified anchors
   13.8% → **2.9%**; 401 reclaimed were verbatim text failing only on curly
   quotes / dashes / collapsed table whitespace. Confirmed **zero fabrications**.
2. **Eval honesty** — competency↔{bumping,skipping} cross-credit; recovery split
   by exact-cite vs fuzzy match tier; editorial citation lines and default
   rule-statements segregated (and audited, not silently dropped); fuzzy matcher
   tightened to require ALJ agreement.
3. **Gate 1 — roster-ref bijection** (`roster_ref_violations`): flags any case
   whose `respondent_dispositions` refs are not a bijection with `roster` refs.
   **50/192 cases (26%)**, matching the audit estimate case-for-case.
4. **Gate 2 — remedy↔disposition contradiction**
   (`remedy_disposition_contradictions`): flags a holding whose remedy keeps the
   respondent (`retain_employee`/`rescind_notice`) while *every* affected
   respondent was terminated. **29 holdings / 17 cases (9%)**.

Both gates **flag, never delete** (mark `reconciliation.status = disputed`,
recorded in `reconciliation.disagreements`) — the holdings/identity/anchor layers
on a flagged case stay trustworthy, so quarantining would discard good data.

## Production readiness verdict

This is a **"harden the merge layer"** task, not a re-architecture. The
extraction reasoning and quote-grounding are sound. The path to production:

- **Blocker for per-respondent analytics:** until the dispositions-pass prompt
  fix lands (REFINEMENTS #8), any analytic keyed on individual respondent
  outcomes must filter out `ROSTER_REF_BIJECTION_VIOLATION` cases (~26%).
  Aggregate tallies and holdings-level analytics are unaffected.
- **Detection vs fix:** the two gates *detect* the corruption and make it
  queryable; they do not *fix* it. The Tier-2 prompt changes reduce how often it
  occurs; the gates remain as the safety net.
- **No model change indicated.** The ensemble is fine; remaining items are
  prompt and deterministic post-process (REFINEMENTS #8–#11).
