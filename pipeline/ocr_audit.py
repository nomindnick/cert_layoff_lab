#!/usr/bin/env python3
"""Era-stratified OCR-quality audit of pdf_text decisions (REFINEMENTS #5).

`pdf_text` is two populations: born-digital e-filing era files (clean) and
pre-2010 scans carrying an embedded OCR text layer that extracts as "native
text" but is riddled with artifacts ("James a Allen", "James R_ Collins",
floating paragraph numerals). `alpha_ratio` cannot separate them; this audit
scores artifact density directly so the re-OCR queue is chosen on evidence:

  case_flip     mid-name lowercase token between capitalized words
  underscore    underscores fused into words (OCR of dot leaders/rules)
  digit_in_word digits embedded inside alphabetic tokens (l/1, O/0 damage)
  orphan_num    short numeral-only lines away from page breaks
  frag_line     one/two-character lines (column shredding)

Writes output/inventory/ocr_audit.md: per-year medians, the worst files, and
a suggested re-OCR queue (score above --reocr-threshold).

Usage: ocr_audit.py [--doc-class decision] [--reocr-threshold 3.0]
"""

import argparse
import json
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from inventory import cached_text

ROOT = Path(__file__).resolve().parent.parent

# Only unambiguous damage signatures -- anything that legitimate prose or
# ordinary page furniture can produce (orphan page numbers, 'Office of
# Administrative', form-field underscore runs) is excluded; the first cut of
# this audit scored 1999 and 2012 identically because of those.
# single lowercase letter between two capitalized words, excluding the
# article 'a' and pronoun-ish 'i' ("James r Collins" <- "James R. Collins")
CASE_FLIP = re.compile(r"\b[A-Z][a-z]+ [b-hj-z] [A-Z][a-z]+\b")
# underscore fused INTO a word ("R_", "Co_llins"), not form-field runs
UNDERSCORE = re.compile(r"\b[A-Za-z]+_(?:[A-Za-z]+\b)?")
# digits embedded inside an alphabetic token (l/1, O/0 confusion)
DIGIT_IN_WORD = re.compile(r"\b[A-Za-z]{2,}\d+[A-Za-z]{2,}\b")
# lowercase letter glued after a capitalized word boundary split ("DAN IELLE")
SPLIT_CAPS = re.compile(r"\b[A-Z]{2,}\s[A-Z]{2,}(?=[a-z])")


def artifact_score(text: str) -> dict:
    """Artifact counts per 10k chars, plus a combined score."""
    n = max(len(text), 1)
    per10k = lambda c: round(c * 10000 / n, 2)
    m = {
        "case_flip": per10k(len(CASE_FLIP.findall(text))),
        "underscore": per10k(len(UNDERSCORE.findall(text))),
        "digit_in_word": per10k(len(DIGIT_IN_WORD.findall(text))),
        "split_caps": per10k(len(SPLIT_CAPS.findall(text))),
    }
    m["score"] = round(sum(m.values()), 2)
    return m


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--manifest", type=Path,
                    default=ROOT / "output" / "inventory" / "manifest.json")
    ap.add_argument("--cache", type=Path,
                    default=ROOT / "output" / "cache" / "text")
    ap.add_argument("--doc-class", default="decision")
    ap.add_argument("--reocr-threshold", type=float, default=0.5)
    args = ap.parse_args()

    manifest = json.loads(args.manifest.read_text())
    rows = []
    for r in manifest["files"]:
        if (r["doc_class"] != args.doc_class or r["kind"] != "pdf_text"
                or r["extract_status"] != "ok"):
            continue
        year = (r["case_numbers_filename"] or ["?"])[0][:4]
        text = cached_text(r["sha1"], args.cache)
        if not text.strip():
            continue
        m = artifact_score(text)
        rows.append({"path": r["path"], "sha1": r["sha1"], "year": year,
                     "chars": len(text), **m})

    by_year = defaultdict(list)
    for r in rows:
        by_year[r["year"]].append(r)

    lines = ["# OCR-quality audit -- pdf_text decisions", "",
             f"{len(rows)} files scored; score = case_flip + underscore + "
             "digit_in_word + split_caps, per 10k chars (unambiguous OCR "
             "damage signatures only).", "",
             "## Artifact density by decision year (mean / p90 of score)",
             "",
             "| year | files | mean | p90 | max |",
             "|---|---:|---:|---:|---:|"]
    for y in sorted(by_year):
        ss = sorted(r["score"] for r in by_year[y])
        lines.append(f"| {y} | {len(ss)} | {statistics.mean(ss):.2f} | "
                     f"{ss[int(len(ss) * 0.9)]:.2f} | {ss[-1]:.2f} |")

    queue = sorted((r for r in rows if r["score"] >= args.reocr_threshold),
                   key=lambda r: -r["score"])
    lines += ["", f"## Re-OCR queue (score >= {args.reocr_threshold}): "
              f"{len(queue)} files", ""]
    lines += [f"- {r['score']:>6.2f}  {r['year']}  {r['path']}"
              for r in queue[:60]]

    out = ROOT / "output" / "inventory" / "ocr_audit.md"
    out.write_text("\n".join(lines) + "\n")
    print("\n".join(lines[:16]))
    print(f"\nre-OCR queue: {len(queue)} files; wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
