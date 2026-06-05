# Worked examples

Hand-filled decision records created *before* any LLM extraction. Each serves
three purposes at once: schema stress-test, few-shot exemplar for extraction
prompts, and eval fixture.

## Two layers per example

| layer | location | contents |
|---|---|---|
| private | `output/examples/` (untracked) | full fidelity: respondent names in roster, verbatim quote anchors, `full_text`. Built and machine-verified by a `build_*.py` script alongside it — every quote anchor is asserted verbatim against the cached extracted text. **This is the version prompts consume.** |
| public | `schema/examples/` (this dir) | same record with respondent names replaced by `[Rn]` placeholders in all strings, roster names and `full_text` nulled. Quote anchors are therefore NOT verbatim here; a `redaction` provenance pass records that. |

Counsel, ALJ, superintendent, and district names are retained in both layers —
public actors, consistent with the annual compilations' citation convention
(district + ALJ, never respondent names).

## Examples

- `lemon_grove_2011030915.json` — Lemon Grove School District (Matyszewski,
  2011). Chosen as the first example because one short decision exercises an
  unusual amount of the schema: a § 44955(d)(1) BCLAD/dual-immersion skip,
  verbatim competency definition and skip criteria (tie-breaker mentioned but
  not quoted — all three artifact statuses), a three-employee bump cascade,
  per-respondent dispositions spanning five patterns (terminated / dismissed /
  precautionary-converted / self-represented / default), and a blank DATED
  line in the native DOC (decision_date null; signed PDF twin is the
  production source for dates).
