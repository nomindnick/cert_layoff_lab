# Annual summary reports — runbook

Two artifacts per gold-covered year, built from the merged decision records:

1. **Clean report** (`Layoff_Decision_Summaries_{year}_AI.docx`) — the
   production artifact. Deterministic assembly over the corpus
   (`render_summary.py`): Roman-numeral sections in taxonomy order, each
   holding's `summary_style_holding` with the District (ALJ) cite, decisions
   appendix. Respondent names are replaced with roster refs (R1..Rn) by a
   deterministic de-identification pass, and every render runs a
   case-insensitive privacy audit (leaks print loudly; check for the
   `privacy audit: no roster names` line before sharing anything).
2. **Annotated verification edition** (`annotated_summary_{year}.html`) —
   spike scaffolding for gold-covered years (and the eval-presentation layer
   in production). Color-codes every entry by its **deterministic** eval
   class from `alignment_{year}.json` (confirmed / matched-different-filing /
   addition / missed appendix), quotes the human volume's paragraph verbatim
   under matched entries, and carries an LLM-written note under each block.
   Classification is never LLM judgment; notes only explain an
   already-computed class.

## Per-year procedure (e.g. 2004, after its extraction run completes)

```bash
.venv/bin/python pipeline/eval_year.py --year 2004        # writes eval_2004.md + alignment_2004.json
.venv/bin/python pipeline/render_summary.py --year 2004   # docx + entries manifest; CHECK PRIVACY AUDIT LINE
.venv/bin/python pipeline/annotate_summary.py --year 2004 --skeleton   # work-list + in_NN.json batch files
# … commentary fan-out (below) writes out_NN.json next to each in_NN.json …
.venv/bin/python pipeline/annotate_summary.py --year 2004 --merge      # validates coverage + privacy, writes commentary_2004.json
.venv/bin/python pipeline/annotate_summary.py --year 2004               # renders annotated_summary_2004.html
```

All idempotent; artifacts land in `output/reports/{year}/` (untracked, like
all of `output/`). Do not commit rendered reports — they are derived from the
proprietary corpus. Deliver via Claude Code's send-file, or email them
yourself.

## Commentary fan-out (Claude Code subagents)

`--skeleton` writes `commentary_parts/in_NN.json`, grouped by kind (`new`,
`confirmed`, `divergent`, `missed`), ≤42 blocks each. Spawn one subagent per
file, in parallel; each must write `out_NN.json` in the same directory as ONE
JSON object mapping every input block `id` → note string. `--merge` hard-fails
on missing/extra/empty notes and privacy-scans every note against its case's
roster, so a sloppy agent cannot silently corrupt the report.

Prompt skeleton for every agent (substitute the file number and year):

> Read `<repo>/output/reports/<year>/commentary_parts/in_NN.json` — a JSON
> array of blocks from an annotated report comparing a machine-generated
> catalog of California teacher-layoff (Ed. Code §§44949/44955) administrative
> decisions against an expert-written annual summary volume.
> [KIND PARAGRAPH — below]
> Rules: 1–2 sentences per note, max ~45 words, plain professional prose, no
> markdown. Never name individuals; use "the district", "respondent(s)", or
> pseudonymous refs (R1, R2…) as they appear. Never question or contradict the
> block's classification. Do not speculate beyond the given text. Vary
> sentence openings.
> Write `<...>/commentary_parts/out_NN.json` as ONE JSON object mapping each
> block's "id" to its note string; every input id exactly once. Reply with
> just the count.

Kind paragraphs:

- **new** — "Every block has kind `new`: the system extracted this holding
  (quote-anchored, verified) but the human volume's editors did not catalog
  it. The volumes are editorial, not exhaustive. Briefly characterize the
  holding and offer a HEDGED explanation of the likely editorial omission
  (routine application of settled law / duplicative of catalogued holdings /
  administrative disposition / case-specific factual ruling). If the holding
  appears genuinely substantive or unusual (especially if `notable` is true),
  say plainly that it appears substantive and the omission may simply be
  editorial — do not force a 'routine' explanation onto a substantive
  holding."
- **confirmed** — "Every block has kind `confirmed`: the eval pipeline already
  matched system_text to the human volume's gold_texts; the match is settled.
  Compare the versions: identical in substance, or same holding with
  different emphasis/scope/detail — and if one version carries a substantive
  element the other lacks (a remedy, qualifier, factual predicate), name it.
  Be specific to the texts."
- **divergent** — "Every block has kind `divergent`: content matches the
  quoted human-volume entry but the system filed it under a different issue
  section. Both filings are defensible (the system files by legal mechanism;
  the volume sometimes groups by theme). Confirm the substance matches and
  explain the filing difference concretely."
- **missed** — "Every block has kind `missed`: the volume catalogued this
  holding (gold_text) for a decision we processed, but extraction did not
  recover it. `extracted_holdings_for_case` shows what WAS extracted. Be
  honest and diagnostic: (a) related content captured — say which holding and
  what was lost; (b) wholly missed; (c) the gold entry reads as editorial
  commentary rather than a case holding (only if it genuinely does). Never
  claim content was captured when the extracted holdings don't show it."

## Partner framing (for the cover email)

- The **docx is a button push** — assembled automatically from the corpus; it
  regenerates for any year, including 2018+ where no human volume exists.
- The **annotated HTML is evaluation scaffolding**, only possible for years
  with a human volume; its purpose is to let the reader judge extraction
  quality holding-by-holding before trusting the system on uncovered years.
- The system deliberately does not produce the volumes' editorial commentary
  ("the ALJ appears to have conflated…"); that stays human work product or a
  future, separately verified generation pass.
- 2009 is the clean-text best case; 2004 is the image-only-scan worst case
  (rapidocr) — together they bracket the quality range expected on the full
  corpus.
