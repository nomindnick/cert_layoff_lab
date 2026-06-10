#!/usr/bin/env python3
"""Stage 1a: Extract the top-level issue taxonomy from each annual summary.

Reads the inventory manifest + cached text (run pipeline/inventory.py first),
finds section headings in each summary document using three strategies
(Roman-numeral body headings, TOC lines, bare all-caps known headings),
maps them to a canonical issue vocabulary with OCR-tolerant rules, and emits:

  - taxonomy.json        per-summary: raw headings + canonical categories
  - taxonomy_drift.md    canonical-category × year matrix + unmapped headings

Headings that match no rule land in the "unmapped" report rather than being
dropped silently — extend CANONICAL_RULES as new variants surface.

Usage:
    .venv/bin/python pipeline/summaries_taxonomy.py [--manifest PATH] [--cache DIR] [--out DIR]
"""

import argparse
import hashlib
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# ------------------------------------------------------------ canonical map
#
# Rules run against a normalized heading: uppercase, common OCR confusions
# fixed (0->O, 1->I in words), non-alpha stripped, so e.g.
# "SENIORI'T'Y" -> "SENIORITY", "XI I I . COMPETENCY" -> "COMPETENCY".
# A heading may map to several canonicals ("SKIPPING AND BUMPING").

CANONICAL_RULES: list[tuple[str, list[str]]] = [
    (r"PROCEDURAL", ["procedural_issues"]),
    (r"CALCULATIONS|ADAANDFTE|ADACALCULATIONS|ADALAYOFFS|FTECALCULATIONS",
     ["calculations_ada_fte"]),
    (r"ATTRITION", ["attrition"]),
    # PKS / reductions in services (loose gaps absorb OCR damage like
    # DISCONTTNUANCES / DISCUNTINUANCES / "REDUCTION OF")
    (r"REDUCTION.{0,4}DISC.{1,6}NUANCE.{0,4}SERVICESN[OU]T|"
     r"SERVICESFOUNDNOTTOBEPARTICULAR|NOTPARTICULARKINDS",
     ["pks_not_allowed"]),
    (r"REDUCTION.{0,4}DISC.{1,6}NUANCE.{0,4}SERVICESALLOWED|"
     r"SUBJECTSFOUNDTOBEPARTICULARKINDS|FOUNDTOBEPARTICULARKINDS",
     ["pks_allowed"]),
    (r"SENIOR[IL]TY(?!TIES|DATE)", ["seniority"]),
    (r"TEMPORAR", ["temporary_employees"]),
    (r"CATEGOR[IY]CAL", ["categorically_funded"]),
    (r"SKIPPING", ["skipping"]),
    (r"BUMPING", ["bumping"]),
    (r"ASSIGNMENTSANDREASSIGNMENTS|REASSIGNMENT", ["assignments_reassignments"]),
    (r"CREDENT[IL]ALS?", ["credentials"]),
    (r"COMPETENC[YE]", ["competency"]),
    (r"CRITERIA.*(TIES|SENIORITYDATE|SAMEDATE)|BREAKING.*TIES|^CRITERIA$|"
     r"TIEBREAK|CH[OU]SINGAMONG|ORDEROFEMPLOYMENT|SAMESENIORITYDATE",
     ["tie_breaking"]),
    (r"DOMINO", ["domino_theory"]),
    (r"EERA|COLLECTIVEBARGAINING|AFFIRMATIVEACTION", ["eera_cba_aa"]),
    (r"CONTRACTUAL", ["contractual_issues"]),
    (r"COUNTYOFFICE", ["county_office_issues"]),
    (r"DISCRIMINATION", ["discrimination"]),
    (r"SUBSTITUTE", ["substitutes"]),
    (r"ADULTED", ["adult_education"]),
    (r"REHIRE|REEMPLOYMENT|RIGHTSOFREEMPLOYMENT", ["reemployment_rights"]),
    (r"MISCELLANEOUS", ["miscellaneous"]),
]

# display order for the drift matrix
CANONICAL_ORDER = [
    "procedural_issues", "calculations_ada_fte", "attrition",
    "pks_not_allowed", "pks_allowed", "seniority", "temporary_employees",
    "categorically_funded", "substitutes", "skipping", "bumping",
    "assignments_reassignments", "credentials", "competency", "tie_breaking",
    "domino_theory", "eera_cba_aa", "contractual_issues",
    "county_office_issues", "discrimination", "adult_education",
    "reemployment_rights", "miscellaneous",
]

# Bare all-caps headings (no Roman numeral) are only trusted when they look
# like known section names; this keyword list gates strategy C.
BARE_HEADING_HINT = re.compile(
    r"PROCEDUR|SENIOR|COMPETEN|BUMP|SKIP|ATTRITION|CREDENT|DOMINO|REDUCTION"
    r"|CALCULAT|TEMPORAR|CATEGOR|CRITERIA|MISCELL|CONTRACT|DISCRIMIN|COUNTY"
    r"|ASSIGNMENT|EERA|SUBSTITUTE|ADULT|REEMPLOY|ADA"
)

# numeral class includes 1/l: OCR renders "I." as "1." or "l." routinely
ROMAN_HEAD_RE = re.compile(
    r"^[ \t]*([IVX1l][IVX1l \t]{0,8})[.\-–][ \t]*"
    r"([A-Z][A-Z0-9 \t&/,.'’‘()\[\]\-–—]{6,})$"
)
ALLCAPS_LINE_RE = re.compile(r"^[ \t|]*([A-Z][A-Z0-9 \t&/,.'’‘()\-–—]{5,})[ \t|]*$")
TRAIL_PAGE_RE = re.compile(r"[ .\t·]*[lI\d]{1,4}[ \t|]*$")  # dot leaders + page no.


def normalize(heading: str) -> str:
    s = heading.upper()
    s = TRAIL_PAGE_RE.sub("", s)
    s = s.replace("0", "O").replace("1", "I")
    return re.sub(r"[^A-Z]", "", s)


def clean_display(heading: str) -> str:
    s = TRAIL_PAGE_RE.sub("", heading)
    s = re.sub(r"[ \t|]+", " ", s)
    return s.strip(" .|-–")


def map_canonical(heading: str) -> list[str]:
    norm = normalize(heading)
    candidates = [norm]
    # striprtf renders underlines as U...U wrappers ("USENIORITYU")
    if len(norm) > 7 and norm.startswith("U") and norm.endswith("U"):
        candidates.append(norm[1:-1])
    out = []
    for n in candidates:
        for pat, canons in CANONICAL_RULES:
            if re.search(pat, n):
                out.extend(c for c in canons if c not in out)
        if out:
            break
    return out


def extract_headings(text: str) -> list[str]:
    """Return cleaned candidate top-level headings, de-duplicated."""
    seen: dict[str, str] = {}  # normalized -> display
    for line in text.splitlines():
        line = line.replace("|", " ").rstrip()  # flatten RTF/Word pipe tables
        m = ROMAN_HEAD_RE.match(line)
        if m:
            head = clean_display(m.group(2))
        else:
            m2 = ALLCAPS_LINE_RE.match(line)
            if not (m2 and BARE_HEADING_HINT.search(m2.group(1))):
                continue
            head = clean_display(m2.group(1))
        norm = normalize(head)
        if len(norm) < 6:
            continue
        # keep the longest variant of the same heading (TOC vs body, wrapped)
        if norm not in seen or len(head) > len(seen[norm]):
            seen[norm] = head
    return sorted(seen.values())


def year_label(filename: str) -> tuple[int, str]:
    years = sorted({int(y) for y in re.findall(r"(?<!\d)(19|20)\d{2}(?!\d)", filename)
                    if 1975 <= int(y) <= 2030}
                   | {int(m) for m in re.findall(r"(?<!\d)((?:19|20)\d{2})(?!\d)", filename)
                      if 1975 <= int(m) <= 2030})
    if not years:
        return (0, "unknown")
    if len(years) == 1:
        return (years[0], str(years[0]))
    return (years[0], f"{years[0]}-{years[-1]}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).resolve().parent.parent
    ap.add_argument("--manifest", type=Path,
                    default=root / "output" / "inventory" / "manifest.json")
    ap.add_argument("--cache", type=Path,
                    default=root / "output" / "cache" / "text")
    ap.add_argument("--out", type=Path, default=root / "output" / "summaries")
    ap.add_argument("--min-chars", type=int, default=1000)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    manifest = json.loads(args.manifest.read_text())
    cands = [r for r in manifest["files"]
             if r["doc_class"] == "summary" and r["extract_status"] == "ok"
             and r["text_chars"] >= args.min_chars]

    # drop byte-identical or text-identical duplicates (e.g. decrypted twin)
    by_texthash: dict[str, dict] = {}
    for r in cands:
        text = cached_text(r['sha1'], args.cache)
        th = hashlib.sha1(text.encode()).hexdigest()
        if th not in by_texthash:
            r["_text"] = text
            by_texthash[th] = r

    records = []
    for r in sorted(by_texthash.values(), key=lambda r: year_label(r["filename"])):
        sort_year, label = year_label(r["filename"])
        headings = extract_headings(r["_text"])
        canon: set[str] = set()
        unmapped = []
        for h in headings:
            cs = map_canonical(h)
            canon.update(cs)
            if not cs:
                unmapped.append(h)
        records.append({
            "path": r["path"],
            "year_label": label,
            "sort_year": sort_year,
            "text_chars": r["text_chars"],
            "headings_raw": headings,
            "canonical": sorted(canon),
            "unmapped": unmapped,
        })

    (args.out / "taxonomy.json").write_text(json.dumps({
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "canonical_order": CANONICAL_ORDER,
        "summaries": records,
    }, indent=2))

    # ----- drift matrix: union canonicals per year label
    by_label: dict[str, set[str]] = defaultdict(set)
    label_order: list[str] = []
    for rec in records:
        if rec["year_label"] not in label_order:
            label_order.append(rec["year_label"])
        by_label[rec["year_label"]].update(rec["canonical"])

    lines = [f"# Taxonomy drift across summary years",
             f"\nGenerated {datetime.now(timezone.utc).isoformat(timespec='seconds')} · "
             f"{len(records)} unique summary docs\n"]
    short = {lab: lab.replace("Layoff", "").strip() for lab in label_order}
    header = "| issue | " + " | ".join(short[l].replace("-", "-<br>") for l in label_order) + " |"
    lines.append(header)
    lines.append("|---" * (len(label_order) + 1) + "|")
    for canon in CANONICAL_ORDER:
        row = [canon]
        for lab in label_order:
            row.append("●" if canon in by_label[lab] else "·")
        lines.append("| " + " | ".join(row) + " |")

    lines.append("\n## Per-document detail\n")
    for rec in records:
        lines.append(f"### {rec['year_label']} — `{rec['path']}`")
        lines.append(f"- headings found: {len(rec['headings_raw'])}, "
                     f"canonical: {len(rec['canonical'])}, "
                     f"unmapped: {len(rec['unmapped'])}")
        for h in rec["unmapped"]:
            lines.append(f"  - UNMAPPED: {h}")
        lines.append("")

    (args.out / "taxonomy_drift.md").write_text("\n".join(lines))
    n_unmapped = sum(len(r["unmapped"]) for r in records)
    print(f"{len(records)} summaries -> {args.out / 'taxonomy.json'}")
    print(f"unmapped headings: {n_unmapped} (see taxonomy_drift.md)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
