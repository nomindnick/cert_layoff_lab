#!/usr/bin/env python3
"""Root-cause triage of gold holdings the eval scored as missed.

Classifies every sub-threshold gold holding for a year into:

  zero_holdings_extracted   the record has NO holdings but the outcome shows
                            adjudication -- a real recall miss (holdings-pass
                            re-run candidates)
  category_mismatch_only    pure token overlap >= 0.25 but no category
                            credit: content extracted, label diverges from
                            the volume's editorial section
  weak_text_match           some overlap, neither strong nor categorized
  holding_absent            holdings exist, this one isn't among them -- a
                            true extraction miss
  truncation_flagged        the record carries truncation provenance notes;
                            blame the window, not the model, until re-run

Usage: triage_missed_gold.py [--year 2009] [--threshold 0.3]
"""

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from eval_year import (CONTENT_JAC, acceptable_cats, holding_jac, holding_sim,
                       is_citation_shorthand, is_uncontested_default,
                       load_decisions, load_gold, match_gold_to_decision,
                       truncation_notes)

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--year", default="2009")
    ap.add_argument("--threshold", type=float, default=0.3)
    args = ap.parse_args()

    gold = load_gold(args.year)
    decisions = load_decisions(args.year)
    gmap, _ = match_gold_to_decision(gold, decisions)
    rows = []
    for gi, (case, kind, _conf) in gmap.items():
        if not case:
            continue
        g = gold[gi]
        if is_citation_shorthand(g["text"]):
            continue
        rec = decisions[case]
        hs = rec.get("holdings") or []
        if not hs and is_uncontested_default(rec):
            continue
        best = max((holding_sim(g, h) for h in hs), default=0.0)
        if best >= args.threshold:
            continue
        jacs = [(holding_jac(g, h), h) for h in hs]
        bj, bh = max(jacs, key=lambda t: t[0], default=(0.0, None))
        cats = {(h.get("issue") or {}).get("category") for h in hs}
        if truncation_notes(rec):
            cls = "truncation_flagged"
        elif not hs:
            cls = "zero_holdings_extracted"
        elif bj >= CONTENT_JAC and not (cats & acceptable_cats(g["category_canonical"])):
            cls = "category_mismatch_only"
        elif bj >= CONTENT_JAC:
            cls = "weak_text_match"
        else:
            cls = "holding_absent"
        rows.append((cls, case, kind, round(best, 2), round(bj, 2),
                     "/".join(g["category_canonical"]), g["text"][:120]))

    lines = [f"# Missed-gold triage -- {args.year}", "",
             f"classes: {dict(Counter(r[0] for r in rows))}", ""]
    for cls in ("truncation_flagged", "zero_holdings_extracted",
                "holding_absent", "weak_text_match", "category_mismatch_only"):
        sub = [r for r in rows if r[0] == cls]
        if not sub:
            continue
        lines.append(f"\n## {cls} ({len(sub)})\n")
        lines += [f"- [{c}] ({k}) sim={s} jac={j} [{cat}] {txt}"
                  for _, c, k, s, j, cat, txt in sub]
    holdings_rerun = sorted({c for cls, c, *_ in rows
                             if cls in ("zero_holdings_extracted",
                                        "holding_absent")})
    lines.append("\n## holdings-pass re-run candidates\n")
    lines.append(" ".join(holdings_rerun))
    out = ROOT / "output" / "eval" / f"missed_gold_triage_{args.year}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n")
    print("\n".join(lines[:4]))
    print(f"holdings-pass re-run candidates: {' '.join(holdings_rerun)}")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
