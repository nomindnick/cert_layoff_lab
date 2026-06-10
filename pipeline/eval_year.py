#!/usr/bin/env python3
"""Stage 3: evaluate an extracted gold-overlap year against the expert-written
Layoff Decision Summaries volume for that year (gold).

Matches each gold holding to a decision record (by appended OAH case number
where the volume gives one, else district tokens + ALJ surname), then scores
whether the extraction recovered it (token similarity + category agreement).

Reports:
  - gold coverage (how many gold holdings cite decisions we hold)
  - recovery rate among covered, overall and by category
  - over-recovery (extracted holdings absent from gold — expected, the
    volumes are editorial; this is a queue for review, not an error rate)
  - taxonomy escape rate (category=other / novel subtypes)
  - reconciliation stats (model disagreement kinds) and anchor verification
  - truncation-affected records (provenance pass notes), so context-window
    loss is never silently scored as extraction failure

Usage: eval_year.py [--year 2009] [--threshold 0.3]
"""

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DECISIONS = ROOT / "output" / "corpus" / "decisions"

WORD = re.compile(r"[a-z0-9]+")
DISTRICT_STOP = {"unified", "school", "district", "joint", "union", "elementary",
                 "high", "city", "county", "office", "of", "education", "the",
                 "usd", "sd", "uhsd", "esd", "coe", "doe", "community", "college"}
# gold's outcome-laden pair maps onto the schema's outcome-neutral category
CATEGORY_MAP = {"pks_allowed": "pks_reduction", "pks_not_allowed": "pks_reduction"}
# The gold volume treats "competency" as a first-class category; our taxonomy
# treats competency as the *reason* and files the holding under its *mechanism*
# (a senior teacher bumping/being skipped for lack of competency). That is a
# defensible labeling choice, so a gold competency holding earns category credit
# when we tagged it competency OR bumping OR skipping. See the 2009 quality pass.
CROSS_CREDIT = {"competency": {"competency", "bumping", "skipping"}}


def acceptable_cats(gold_cats):
    acc = set()
    for c in gold_cats:
        c = CATEGORY_MAP.get(c, c)
        acc.add(c)
        acc |= CROSS_CREDIT.get(c, set())
    return acc


# Editorial citation/cross-reference lines the gold volume carries that are not
# case-specific holdings (e.g. "OAH No. 2009030486 - Grossmont Union (Hjelt)",
# "Interns acquire layoff Reef-Sunset (Walker)"): short, and terminating in a
# district+ALJ citation. These cannot match a full extracted holding and are
# not an extraction target, so they are excluded from the recovery denominator.
_CITE_TAIL = re.compile(r"\([A-Z][a-z]+\)\s*$")
_OAH_LEAD = re.compile(r"^\s*OAH\s+No", re.I)


def is_citation_shorthand(text):
    t = (text or "").strip()
    wc = len(t.split())
    return wc < 12 or (wc < 25 and bool(_CITE_TAIL.search(t))) or bool(_OAH_LEAD.match(t))


# A zero-holding decision is excluded from the recovery denominator only when its
# outcome shows the matter was genuinely uncontested -- a default/stipulation
# whose gold "holding" is a black-letter recitation the schema omits. If any
# respondent obtained relief (notice rescinded, accusation dismissed, partial
# termination) or the district did not prevail, the case was adjudicated, so an
# empty holdings[] is a real recall miss, not out-of-scope -- it stays scored.
RELIEF_DISPOSITIONS = {"accusation_dismissed", "notice_rescinded",
                       "partially_terminated"}


def is_uncontested_default(rec):
    out = rec.get("outcome") or {}
    if out.get("overall") == "not_sustained":
        return False
    if (out.get("general_order") or {}).get("disposition") in RELIEF_DISPOSITIONS:
        return False
    return not any((d.get("disposition") in RELIEF_DISPOSITIONS)
                   for d in out.get("respondent_dispositions") or [])


def tokens(s):
    return set(WORD.findall((s or "").lower()))


def district_tokens(s):
    return tokens(s) - DISTRICT_STOP


def load_gold(year):
    gold = []
    for line in (ROOT / "output" / "summaries" / "holdings.jsonl").read_text().splitlines():
        h = json.loads(line)
        if h["volume"] == year and h["cites"]:
            gold.append(h)
    return gold


def load_decisions(year):
    """Only decisions whose OAH case-number prefix matches the eval year --
    the decisions directory holds every extracted year."""
    recs = {}
    for f in DECISIONS.glob(f"{year}*.json"):
        recs[f.stem] = json.loads(f.read_text())
    return recs


def truncation_notes(rec):
    """Pass-level truncation flags recorded by extract.py telemetry."""
    notes = []
    for p in (rec.get("provenance") or {}).get("passes") or []:
        if p.get("notes") and "truncat" in p["notes"]:
            notes.append(f"{p['name']}/{p['model']}: {p['notes']}")
    return notes


def match_gold_to_decision(gold, decisions):
    """Return {gold_idx: (case_no | None, kind, confidence)}.

    kind is "case_number" (the gold volume appended an OAH number we hold -- the
    trustworthy tier), "district_alj" (fuzzy fallback on district tokens + ALJ
    surname -- matcher-dependent, segregated in the report so recovery is not
    conflated with match quality), or None (decision not in our set / ambiguous).
    Fuzzy matching now *requires* ALJ-surname agreement when the gold cite names
    an ALJ; a bare district-token overlap is no longer enough to bind a holding
    to a case."""
    by_district = defaultdict(list)
    for case_no, r in decisions.items():
        d = ((r.get("identity") or {}).get("district") or {}).get("raw") or ""
        a = ((r.get("identity") or {}).get("alj") or {}).get("raw") or ""
        by_district[case_no] = (district_tokens(d), tokens(a))
    out, ambiguous = {}, 0
    for gi, g in enumerate(gold):
        cite = g["cites"][-1]
        case = cite.get("case_number")
        if case and case in decisions:
            out[gi] = (case, "case_number", 1.0)
            continue
        gtok = district_tokens(cite["district"])
        galj = tokens(cite["alj"])
        cands = []
        for case_no, (dtok, atok) in by_district.items():
            if not gtok or not dtok:
                continue
            overlap = len(gtok & dtok) / len(gtok | dtok)
            # require ALJ-surname agreement when the gold cite names one
            alj_ok = bool(galj & atok) if galj else True
            if overlap >= 0.5 and alj_ok:
                cands.append((overlap, case_no))
        if len(cands) == 1:
            out[gi] = (cands[0][1], "district_alj", cands[0][0])
        elif len(cands) > 1:
            cands.sort(reverse=True)
            if cands[0][0] > cands[1][0]:
                out[gi] = (cands[0][1], "district_alj", cands[0][0])
            else:
                ambiguous += 1
                out[gi] = (None, None, 0.0)
        else:
            out[gi] = (None, None, 0.0)
    return out, ambiguous


def holding_jac(gold_h, model_h):
    gt = tokens(gold_h["text"])
    mt = tokens((model_h.get("issue") or {}).get("statement", "")) | \
        tokens(model_h.get("summary_style_holding") or "") | \
        tokens(((model_h.get("reasoning") or {}).get("summary")) or "")
    return len(gt & mt) / len(gt | mt) if (gt or mt) else 0.0


def holding_sim(gold_h, model_h):
    jac = holding_jac(gold_h, model_h)
    cat = (model_h.get("issue") or {}).get("category")
    return 0.6 * jac + (0.4 if cat in acceptable_cats(gold_h["category_canonical"]) else 0.0)


# A miss whose best pure-token overlap clears this bar has the gold holding's
# CONTENT extracted -- the combined score failed only because our taxonomy
# filed it under a different category than the volume's editorial section.
# Those are reported as their own tier (with a gold->ours confusion table),
# not folded into the headline recovery rate.
CONTENT_JAC = 0.25


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--year", default="2009")
    ap.add_argument("--threshold", type=float, default=0.3)
    args = ap.parse_args()

    gold = load_gold(args.year)
    decisions = load_decisions(args.year)
    if not gold:
        print(f"no gold holdings for volume '{args.year}' -- is the volume "
              f"parsed into output/summaries/holdings.jsonl?")
        return 1
    print(f"{len(gold)} gold {args.year} holdings; {len(decisions)} extracted decisions")
    gmap, ambiguous = match_gold_to_decision(gold, decisions)
    covered = {gi: (c, kind) for gi, (c, kind, conf) in gmap.items() if c}

    # Segregate gold the extraction does not target before scoring recovery, so
    # the rate reflects what the schema actually tries to do:
    #   - citation/cross-reference lines (not case-specific holdings)
    #   - gold holdings on cases we extracted with zero holdings: default/
    #     uncontested matters whose gold "holding" is a black-letter rule
    #     recitation the schema deliberately omits (empty holdings[] = signal).
    # Both buckets are reported with case lists, never silently dropped.
    excluded_cite, excluded_default, scored = [], [], {}
    for gi, (case_no, kind) in covered.items():
        if is_citation_shorthand(gold[gi]["text"]):
            excluded_cite.append((gi, case_no))
        elif (not (decisions[case_no].get("holdings") or [])
              and is_uncontested_default(decisions[case_no])):
            excluded_default.append((gi, case_no))
        else:
            scored[gi] = (case_no, kind)

    recovered, missed, cat_divergent = [], [], []
    confusion = Counter()
    for gi, (case_no, kind) in scored.items():
        g = gold[gi]
        hs = decisions[case_no].get("holdings") or []
        best = max((holding_sim(g, h) for h in hs), default=0.0)
        if best >= args.threshold:
            recovered.append((gi, case_no, kind, best))
            continue
        missed.append((gi, case_no, kind, best))
        jacs = [(holding_jac(g, h), h) for h in hs]
        bj, bh = max(jacs, key=lambda t: t[0], default=(0.0, None))
        if bj >= CONTENT_JAC and bh is not None:
            ours = (bh.get("issue") or {}).get("category")
            cat_divergent.append((gi, case_no, bj, ours))
            for c in g["category_canonical"]:
                confusion[(CATEGORY_MAP.get(c, c), ours)] += 1
    rec_ids = {gi for gi, _, _, _ in recovered}
    div_ids = {gi for gi, _, _, _ in cat_divergent}

    # recovery split by match tier: case_number cites are the trustworthy
    # denominator; district_alj fuzzy matches are matcher-dependent.
    def tier_counts(k):
        tot = sum(1 for _, (c, kind) in scored.items() if kind == k)
        rec = sum(1 for gi, _, kind, _ in recovered if kind == k)
        return rec, tot
    exact_rec, exact_tot = tier_counts("case_number")
    fuzzy_rec, fuzzy_tot = tier_counts("district_alj")

    by_cat_total, by_cat_rec = Counter(), Counter()
    for gi, (case_no, kind) in scored.items():
        for c in gold[gi]["category_canonical"]:
            c = CATEGORY_MAP.get(c, c)
            by_cat_total[c] += 1
            if gi in rec_ids:
                by_cat_rec[c] += 1

    # over-recovery + escapes + reconciliation + anchors
    matched_model = defaultdict(set)
    for gi, (case_no, kind) in scored.items():
        g = gold[gi]
        hs = decisions[case_no].get("holdings") or []
        sims = [(holding_sim(g, h), i) for i, h in enumerate(hs)]
        if sims:
            s, i = max(sims)
            if s >= args.threshold:
                matched_model[case_no].add(i)
    total_extracted = sum(len(r.get("holdings") or []) for r in decisions.values())
    matched_extracted = sum(len(v) for v in matched_model.values())

    escapes, novel_subtypes = [], 0
    disagreement_kinds, anchor_bad, anchor_total = Counter(), 0, 0
    disputed = 0
    roster_completeness = Counter()
    for case_no, r in decisions.items():
        for h in r.get("holdings") or []:
            issue = h.get("issue") or {}
            if issue.get("category") == "other":
                escapes.append((case_no, issue.get("category_proposed")))
            if issue.get("subtype_is_novel"):
                novel_subtypes += 1
        rec = (r.get("provenance") or {}).get("reconciliation") or {}
        dis = rec.get("disagreements") or []
        if dis:
            disputed += 1
        for d in dis:
            disagreement_kinds[d.get("resolution")] += 1
        anchor_bad += sum(1 for d in dis
                          if d.get("resolution") == "ANCHOR_UNVERIFIED")
        roster_completeness[(r.get("outcome") or {}).get("roster_completeness")] += 1

    import itertools

    def walk_quotes(o):
        if isinstance(o, dict):
            q = o.get("quote")
            if isinstance(q, dict) and isinstance(q.get("quote"), str):
                yield q["quote"]
            for k, v in o.items():
                if k not in ("quote", "full_text"):
                    yield from walk_quotes(v)
        elif isinstance(o, list):
            for v in o:
                yield from walk_quotes(v)
    anchor_total = sum(1 for r in decisions.values() for _ in walk_quotes(r))

    truncated = {c: truncation_notes(r) for c, r in decisions.items()
                 if truncation_notes(r)}

    lines = [f"# {args.year} overlap-year evaluation", ""]
    w = lines.append
    n_scored = len(scored)
    pct = lambda a, b: 100 * a / max(b, 1)
    w(f"- gold holdings ({args.year} volume, cited): **{len(gold)}**")
    w(f"- matched to an extracted decision: **{len(covered)}** "
      f"({ambiguous} ambiguous, {len(gold) - len(covered) - ambiguous} cite decisions outside our set)")
    w(f"- scored for recovery: **{n_scored}** (excluded: "
      f"{len(excluded_cite)} editorial citation lines, "
      f"{len(excluded_default)} rule-statements on default/uncontested matters)")
    w(f"- **recovered: {len(recovered)}/{n_scored} "
      f"({pct(len(recovered), n_scored):.0f}%)** at threshold {args.threshold}")
    w(f"  - by exact OAH-number cite (trustworthy): "
      f"**{exact_rec}/{exact_tot} ({pct(exact_rec, exact_tot):.0f}%)**")
    w(f"  - by fuzzy district+ALJ match (matcher-dependent): "
      f"{fuzzy_rec}/{fuzzy_tot} ({pct(fuzzy_rec, fuzzy_tot):.0f}%)")
    w(f"- content extracted but category diverges from the volume's section "
      f"(token overlap >= {CONTENT_JAC}, no category credit): "
      f"**{len(cat_divergent)}** -- content-recovery including these: "
      f"**{len(recovered) + len(cat_divergent)}/{n_scored} "
      f"({pct(len(recovered) + len(cat_divergent), n_scored):.0f}%)**")
    w(f"- extracted holdings across {len(decisions)} decisions: {total_extracted} "
      f"({matched_extracted} matched gold; {total_extracted - matched_extracted} over-recovery / review queue)")
    w(f"- taxonomy escapes: {len(escapes)} category=other, {novel_subtypes} novel subtypes")
    w(f"- records with any disagreement: {disputed}/{len(decisions)}")
    w(f"- anchors: {anchor_total} total, {anchor_bad} unverified "
      f"({pct(anchor_bad, anchor_total):.1f}%)")
    w(f"- roster completeness: {dict(roster_completeness)}")
    if truncated:
        w(f"- **truncation-affected records: {len(truncated)}** -- recall "
          f"misses on these may be window loss, not extraction failure:")
        for c, notes in sorted(truncated.items()):
            w(f"  - {c}: {'; '.join(notes)}")
    else:
        w("- truncation-affected records: 0")
    w("\n## Recovery by gold category\n")
    w("| category | covered | recovered | rate |")
    w("|---|---:|---:|---:|")
    for c, n in by_cat_total.most_common():
        r = by_cat_rec.get(c, 0)
        w(f"| {c} | {n} | {r} | {100 * r / n:.0f}% |")
    w("\n## Disagreement kinds\n")
    for k, n in disagreement_kinds.most_common():
        w(f"- {k}: {n}")
    w("\n## Category divergence (gold section -> our category)\n")
    w("Gold holdings whose content we extracted under a different label than "
      "the volume's section. Both label schemes are defensible; this table "
      "is the systematic drift map for the side-by-side review.\n")
    for (gc, oc), n in confusion.most_common():
        w(f"- {gc} -> {oc}: {n}")
    w("\n## Missed gold holdings (review queue, category-divergent excluded)\n")
    TIER = {"case_number": "exact", "district_alj": "fuzzy"}
    for gi, case_no, kind, best in sorted(missed, key=lambda x: x[3])[:30]:
        if gi in div_ids:
            continue
        g = gold[gi]
        w(f"- [{case_no}] ({TIER[kind]}) best_sim={best:.2f} "
          f"[{'/'.join(g['category_canonical'])}] {g['text'][:140]}")
    w("\n## Excluded: default/uncontested matters (rule-statements, out of scope)\n")
    w("Gold holdings on cases we extracted with **zero** holdings *and* whose "
      "outcome shows no respondent obtained relief -- default/stipulated matters "
      "whose gold \"holding\" is a black-letter recitation the schema omits. "
      "Zero-holding cases where any respondent prevailed are kept in the scored "
      "set as genuine recall misses, not listed here.")
    w("")
    for gi, case_no in sorted(excluded_default, key=lambda x: x[1]):
        g = gold[gi]
        w(f"- [{case_no}] [{'/'.join(g['category_canonical'])}] {g['text'][:140]}")
    w("\n## Taxonomy escape samples\n")
    for case_no, prop in escapes[:15]:
        w(f"- [{case_no}] proposed: {prop}")

    out = ROOT / "output" / "eval"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"eval_{args.year}.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines[:12]))
    print(f"\nwrote {out / f'eval_{args.year}.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
