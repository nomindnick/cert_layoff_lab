#!/usr/bin/env python3
"""Stage 3 (spike capstone): evaluate extracted 2009 decisions against the
expert-written 2009 Layoff Decision Summaries (gold).

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

Usage: eval_2009.py [--threshold 0.3]
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


def tokens(s):
    return set(WORD.findall((s or "").lower()))


def district_tokens(s):
    return tokens(s) - DISTRICT_STOP


def load_gold():
    gold = []
    for line in (ROOT / "output" / "summaries" / "holdings.jsonl").read_text().splitlines():
        h = json.loads(line)
        if h["volume"] == "2009" and h["cites"]:
            gold.append(h)
    return gold


def load_decisions():
    recs = {}
    for f in DECISIONS.glob("*.json"):
        recs[f.stem] = json.loads(f.read_text())
    return recs


def match_gold_to_decision(gold, decisions):
    """Return {gold_idx: case_no | None}; None = decision not in our set."""
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
            out[gi] = case
            continue
        gtok = district_tokens(cite["district"])
        galj = tokens(cite["alj"])
        cands = []
        for case_no, (dtok, atok) in by_district.items():
            if not gtok or not dtok:
                continue
            overlap = len(gtok & dtok) / len(gtok | dtok)
            alj_ok = bool(galj & atok) or not galj
            if overlap >= 0.5 and alj_ok:
                cands.append((overlap, case_no))
        if len(cands) == 1:
            out[gi] = cands[0][1]
        elif len(cands) > 1:
            cands.sort(reverse=True)
            if cands[0][0] > cands[1][0]:
                out[gi] = cands[0][1]
            else:
                ambiguous += 1
                out[gi] = None
        else:
            out[gi] = None
    return out, ambiguous


def holding_sim(gold_h, model_h):
    gt = tokens(gold_h["text"])
    mt = tokens((model_h.get("issue") or {}).get("statement", "")) | \
        tokens(model_h.get("summary_style_holding") or "") | \
        tokens(((model_h.get("reasoning") or {}).get("summary")) or "")
    jac = len(gt & mt) / len(gt | mt) if (gt or mt) else 0.0
    gcats = {CATEGORY_MAP.get(c, c) for c in gold_h["category_canonical"]}
    cat = (model_h.get("issue") or {}).get("category")
    return 0.6 * jac + (0.4 if cat in gcats else 0.0)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--threshold", type=float, default=0.3)
    args = ap.parse_args()

    gold = load_gold()
    decisions = load_decisions()
    print(f"{len(gold)} gold 2009 holdings; {len(decisions)} extracted decisions")
    gmap, ambiguous = match_gold_to_decision(gold, decisions)
    covered = {gi: c for gi, c in gmap.items() if c}

    recovered, missed = [], []
    for gi, case_no in covered.items():
        g = gold[gi]
        hs = decisions[case_no].get("holdings") or []
        best = max((holding_sim(g, h) for h in hs), default=0.0)
        (recovered if best >= args.threshold else missed).append((gi, case_no, best))

    by_cat_total, by_cat_rec = Counter(), Counter()
    for gi, case_no in covered.items():
        for c in gold[gi]["category_canonical"]:
            c = CATEGORY_MAP.get(c, c)
            by_cat_total[c] += 1
            if any(g == gi for g, _, _ in recovered):
                by_cat_rec[c] += 1

    # over-recovery + escapes + reconciliation + anchors
    matched_model = defaultdict(set)
    for gi, case_no in covered.items():
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

    lines = ["# 2009 overlap-year evaluation", ""]
    w = lines.append
    w(f"- gold holdings (2009 volume, cited): **{len(gold)}**")
    w(f"- matched to an extracted decision: **{len(covered)}** "
      f"({ambiguous} ambiguous, {len(gold) - len(covered) - ambiguous} cite decisions outside our set)")
    w(f"- **recovered: {len(recovered)}/{len(covered)} "
      f"({100 * len(recovered) / max(len(covered), 1):.0f}%)** at threshold {args.threshold}")
    w(f"- extracted holdings across {len(decisions)} decisions: {total_extracted} "
      f"({matched_extracted} matched gold; {total_extracted - matched_extracted} over-recovery / review queue)")
    w(f"- taxonomy escapes: {len(escapes)} category=other, {novel_subtypes} novel subtypes")
    w(f"- records with any disagreement: {disputed}/{len(decisions)}")
    w(f"- anchors: {anchor_total} total, {anchor_bad} unverified "
      f"({100 * anchor_bad / max(anchor_total, 1):.1f}%)")
    w(f"- roster completeness: {dict(roster_completeness)}")
    w("\n## Recovery by gold category\n")
    w("| category | covered | recovered | rate |")
    w("|---|---:|---:|---:|")
    for c, n in by_cat_total.most_common():
        r = by_cat_rec.get(c, 0)
        w(f"| {c} | {n} | {r} | {100 * r / n:.0f}% |")
    w("\n## Disagreement kinds\n")
    for k, n in disagreement_kinds.most_common():
        w(f"- {k}: {n}")
    w("\n## Missed gold holdings (review queue)\n")
    for gi, case_no, best in sorted(missed, key=lambda x: x[2])[:25]:
        g = gold[gi]
        w(f"- [{case_no}] best_sim={best:.2f} [{'/'.join(g['category_canonical'])}] "
          f"{g['text'][:140]}")
    w("\n## Taxonomy escape samples\n")
    for case_no, prop in escapes[:15]:
        w(f"- [{case_no}] proposed: {prop}")

    out = ROOT / "output" / "eval"
    out.mkdir(parents=True, exist_ok=True)
    (out / "eval_2009.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines[:12]))
    print(f"\nwrote {out / 'eval_2009.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
