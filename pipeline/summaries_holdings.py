#!/usr/bin/env python3
"""Stage 1b: Parse each annual summary into per-holding records.

Splits every summary volume into individual holdings — the atomic unit of the
whole project — using a line-by-line state machine. The reliable delimiter
across all format generations (1979 typewriter OCR through 2017 PDFs) is the
trailing citation: every holding ends with "District (ALJ)". Numbered/lettered
headings supply the category path where present.

Reads the inventory manifest + cached text (run inventory.py first). Emits:

  - holdings.jsonl       one JSON per holding: volume, category path
                         (roman/canonical/letter), text, cites, QA flags
  - case_index.jsonl     case-number index lines found in volumes (old
                         N-/L- numbers and OAH numbers) for completeness work
  - report_holdings.md   per-volume parse quality + canonical×volume counts

Holdings that close without a recognizable cite are kept and flagged
(no_cite) rather than dropped — they are the QA queue, not noise.

Usage:
    .venv/bin/python pipeline/summaries_holdings.py [--manifest PATH] [--cache DIR] [--out DIR]
"""

import argparse
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from summaries_taxonomy import map_canonical, year_label

# ------------------------------------------------------------ line patterns

NOISE_RES = [
    re.compile(r"^\s*-?\s*\d{1,3}\s*-?\s*$"),            # bare page number
    re.compile(r"^\s*[ivxlc]{1,6}\s*$"),                  # lowercase roman page
    re.compile(r"^\s*[IVXLC]{1,5}\s*$"),                  # caps roman, no dot
    re.compile(r"^\s*\d{5,6}\.\d{4,5}\s*$"),              # DMS footer 000840.00000
    re.compile(r"^\s*\d{7,9}\.\d\s*$"),                   # DMS footer 17452757.1
    re.compile(r"MASTER COMPILATION|TABLE OF CONTENTS", re.I),
    re.compile(r"LAYOFF (DECISIONS? )?SUMMAR", re.I),
    re.compile(r"^\s*PAGE\s*$", re.I),
    re.compile(r"[. ·]{6,}[lI\d]{1,4}\s*$"),              # TOC dot-leader line
]

# index entries: "Davis Joint Unified School District, N-16484" — old volumes
# pack several per line, so harvest with finditer rather than a ^...$ match
CASE_INDEX_RE = re.compile(
    r"(?P<entity>[A-Z][A-Za-z .,'&\-]{3,70}?),?\s+"
    r"(?P<case>[NL]-\d{4,6}|(?:OAH\s*(?:Case\s*)?No\.?\s*)?(?:19|20)\d{8})(?!\d)"
)
# trailing junk after a cite: OAH case number (2009 volume appends these —
# capture, they link holding -> decision) and DMS footers like 928163.2
TRAIL_CASE_RE = re.compile(r"[.,;]?\s*((?:OAH\s*(?:Case\s*)?No\.?\s*)?((?:19|20)\d{8}))\s*[.,]?\s*$")
TRAIL_DMS_RE = re.compile(r"(?:\s+\d{4,7}\.\d{1,3})+\s*$")

# roman heading anywhere in the line (volumes glue headers/subheads onto the
# same line: "...DECISIONS I. PROCEDURAL ISSUES", "V. CREDENTIALS A. Late
# Receipt"); anchored to EOL after page-trail stripping, gated by
# map_canonical so body text can't false-positive
ROMAN_ANY_RE = re.compile(
    r"(?<![A-Za-z.])([IVX1l]{1,5})\s*[.\-–]{1,2}[ \t]*"
    r"([A-Z][A-Z0-9 \t&/,.'’‘()\[\]\-–—]{5,})$"
)
# a letter subheading glued mid-line: " A. Services Not Particular..."
LETTER_GLUE_RE = re.compile(r"\s+([A-Z])[.\)][ \t]+(?=[A-Z][a-z])")
PAGE_TRAIL_RE = re.compile(r"[ \t]+\d{1,3}[ \t]*$")
ROMAN_ALONE_RE = re.compile(r"^\s*([IVX]{1,5})\.\s*$")
ALLCAPS_TITLE_RE = re.compile(r"^[ \t]*([A-Z][A-Z0-9 \t&/,.'’‘()\-–—]{5,})[ \t]*$")
LETTER_INLINE_RE = re.compile(r"^[ \t]*([A-Z])[.\)][ \t]+(\S.{2,90})$")
LETTER_ALONE_RE = re.compile(r"^\s*([A-Z])\.\s*$")
NUM_INLINE_RE = re.compile(r"^[ \t]*(\d{1,3})[.\)][ \t]+(\S.*)$")
NUM_ALONE_RE = re.compile(r"^\s*(\d{1,3})\.\s*$")
# sentence-style sub-headings in 1980s volumes: "Estoppel.", "Brown Act."
SENTENCE_HEAD_RE = re.compile(r"^\s*([A-Z][A-Za-z ,'\-]{2,45}[a-z])[.:]\s*$")

# citation: "San Juan Unified (Vorters)" — paren content is a surname-ish
# token (no digits, short), so case-law years "(1981)" never match.
CITE_RE = re.compile(r"([A-Z][A-Za-z0-9 .,&'’\-]{1,70}?)\s*\(([A-Za-z.'’\- ]{2,32})\)")
TRAIL_NOTE_RE = re.compile(r"[;,.]?\s*(?:see|cf\.|but see|accord)[^()]{0,80}$", re.I)

DISTRICT_STOPWORDS = {"of", "the", "and", "de", "del", "la", "los", "el", "san",
                      "santa", "joint", "union", "city"}

TITLE_STOPWORDS = {"of", "or", "and", "to", "the", "not", "for", "on", "in",
                   "a", "an", "by", "with"}


def is_title_case(s: str) -> bool:
    """Letter-subheading titles are Title Case; holding sentences are not."""
    words = [w for w in re.findall(r"[A-Za-z][\w'’\-]*", s)
             if w.lower() not in TITLE_STOPWORDS]
    if not words:
        return False
    return sum(w[0].isupper() for w in words) / len(words) >= 0.6


def is_noise(line: str) -> bool:
    return any(rx.search(line) for rx in NOISE_RES)


def parse_cites(text: str) -> list[dict]:
    """Extract trailing 'District (ALJ)' cites; [] unless text ends on one."""
    tail = text.strip()
    case_number = None
    while True:  # peel trailing case numbers / DMS footers, innermost last
        tail2 = TRAIL_DMS_RE.sub("", tail)
        m = TRAIL_CASE_RE.search(tail2)
        if m:
            case_number = case_number or m.group(2)
            tail2 = tail2[:m.start()]
        if tail2 == tail:
            break
        tail = tail2.strip()
    tail = TRAIL_NOTE_RE.sub("", tail)
    matches = [m for m in CITE_RE.finditer(tail[-400:])]
    if not matches:
        return []
    # the final cite must sit at (or within punctuation of) the very end
    last = matches[-1]
    if len(tail[-400:]) - last.end() > 3:
        return []
    cites = []
    for m in matches:
        alj = m.group(2).strip()
        if len(alj.split()) > 4:
            continue
        # district = trailing capitalized word-run of the pre-paren text
        words = m.group(1).strip().rstrip(",;").split()
        run: list[str] = []
        for w in reversed(words):
            if w[0].isupper() or w.lower() in DISTRICT_STOPWORDS:
                run.append(w)
            else:
                break
        district = " ".join(reversed(run)).strip(",;. ")
        if district:
            cites.append({"district": district, "alj": alj})
    if cites and case_number:
        cites[-1]["case_number"] = case_number
    return cites


class VolumeParser:
    def __init__(self, volume: dict):
        self.volume = volume
        self.roman_raw: str | None = None
        self.letter: str | None = None
        self.letter_title: str | None = None
        self.item_number: int | None = None
        self.pending: str | None = None  # "roman" | "letter" awaiting title
        self.pending_val: str | None = None
        self.buf: list[str] = []
        self.holdings: list[dict] = []
        self.case_index: list[dict] = []

    # ---- buffer
    def flush(self):
        text = re.sub(r"[ \t]+", " ", " ".join(self.buf)).strip()
        self.buf = []
        if len(text) < 40:  # fragments: heading debris, stray markers
            return
        cites = parse_cites(text)
        # uncited short Title-Case lines with no sentence flow are heading
        # debris (TOC echoes, wrapped subheads), not holdings
        if not cites and len(text) < 80 and ". " not in text:
            return
        self.holdings.append({
            "volume": self.volume["year_label"],
            "volume_path": self.volume["path"],
            "sort_year": self.volume["sort_year"],
            "category_raw": self.roman_raw,
            "category_canonical": map_canonical(self.roman_raw or ""),
            "letter": self.letter,
            "letter_title": self.letter_title,
            "item_number": self.item_number,
            "text": text,
            "cites": cites,
            "n_chars": len(text),
            "no_cite": not cites,
            "suspect_long": len(text) > 3000,
        })

    def set_roman(self, raw_title: str):
        self.flush()
        self.roman_raw = re.sub(r"[ \t]+", " ", raw_title).strip(" .|-–")
        self.letter = self.letter_title = self.item_number = None

    def set_letter(self, letter: str, title: str):
        self.flush()
        self.letter = letter
        self.letter_title = title.strip(" .")
        self.item_number = None

    # ---- main loop
    def feed(self, line: str):
        line = line.replace("|", " ").rstrip()
        if not line.strip():
            return
        if is_noise(line):
            # page headers sometimes carry the next section heading on the
            # same line ("...LAYOFF DECISION SUMMARIES -- 2010 PROCEDURAL
            # ISSUES"); salvage a mappable all-caps tail before dropping
            mt = re.search(r"([A-Z][A-Z &/,'’\-]{5,})$", line.rstrip())
            if mt and map_canonical(mt.group(1)):
                self.set_roman(mt.group(1))
            return

        idx = list(CASE_INDEX_RE.finditer(line.strip()))
        # index lines are entity+case pairs covering most of the line
        if idx and sum(m.end() - m.start() for m in idx) > 0.7 * len(line.strip()):
            for m in idx:
                self.case_index.append({
                    "volume": self.volume["year_label"],
                    "entity": m.group("entity").strip(),
                    "case": m.group("case").strip(),
                })
            return

        # resolve a pending marker waiting for its title line
        if self.pending == "roman":
            self.pending = None
            mt = ALLCAPS_TITLE_RE.match(line)
            if mt:
                self.set_roman(mt.group(1))
                return
            # fall through: the numeral was page debris, treat line normally
        elif self.pending == "letter":
            self.pending = None
            if (len(line.strip()) <= 90 and is_title_case(line)
                    and not parse_cites(line)):
                self.set_letter(self.pending_val, line.strip())
                return

        work = PAGE_TRAIL_RE.sub("", line).rstrip()
        mg = LETTER_GLUE_RE.search(work)
        if mg:
            mr = ROMAN_ANY_RE.search(work[:mg.start()].rstrip())
            if mr and map_canonical(mr.group(2)):
                self.set_roman(mr.group(2))
                self.set_letter(mg.group(1), work[mg.end():])
                return
        mr = ROMAN_ANY_RE.search(work)
        if mr and map_canonical(mr.group(2)):
            self.set_roman(mr.group(2))
            return
        m = ROMAN_ALONE_RE.match(line)
        if m:
            self.pending = "roman"
            return
        m = ALLCAPS_TITLE_RE.match(line)
        if m and map_canonical(m.group(1)) and len(line.strip()) < 70:
            self.set_roman(m.group(1))
            return
        m = LETTER_ALONE_RE.match(line)
        if m:
            self.pending, self.pending_val = "letter", m.group(1)
            return
        m = LETTER_INLINE_RE.match(line)
        if (m and not self.buf and is_title_case(m.group(2))
                and not parse_cites(line)):
            self.set_letter(m.group(1), m.group(2))
            return
        m = NUM_ALONE_RE.match(line)
        if m:
            self.flush()
            self.item_number = int(m.group(1))
            return
        m = NUM_INLINE_RE.match(line)
        if m:
            self.flush()
            self.item_number = int(m.group(1))
            line = m.group(2)
        elif not self.buf:
            m2 = SENTENCE_HEAD_RE.match(line)
            if m2:
                self.set_letter(self.letter or "", m2.group(1))
                return

        self.buf.append(line.strip())
        # a line ending on a cite closes the holding (primary delimiter for
        # unnumbered 1980s volumes; harmless redundancy for numbered ones)
        if parse_cites(" ".join(self.buf[-3:]) if len(self.buf) >= 3 else line):
            self.flush()

    def finish(self) -> tuple[list[dict], list[dict]]:
        self.flush()
        return self.holdings, self.case_index


def build_report(holdings: list[dict], case_index: list[dict]) -> str:
    vols: dict[str, list[dict]] = defaultdict(list)
    for h in holdings:
        vols[h["volume"] + " · " + h["volume_path"]].append(h)

    lines = ["# Holdings parse report",
             f"\nGenerated {datetime.now(timezone.utc).isoformat(timespec='seconds')} · "
             f"{len(holdings)} holdings · {len(case_index)} case-index lines\n",
             "## Per-volume parse quality\n",
             "| volume | holdings | with cite | no cite | suspect long | median chars |",
             "|---|---:|---:|---:|---:|---:|"]
    for key in sorted(vols, key=lambda k: vols[k][0]["sort_year"]):
        hs = vols[key]
        cited = [h for h in hs if not h["no_cite"]]
        sizes = sorted(h["n_chars"] for h in hs)
        lines.append(f"| {key} | {len(hs)} | {len(cited)} | {len(hs) - len(cited)} | "
                     f"{sum(h['suspect_long'] for h in hs)} | {sizes[len(sizes)//2]} |")

    # canonical × volume counts — the real drift table
    labels, seen = [], set()
    for h in sorted(holdings, key=lambda h: h["sort_year"]):
        if h["volume"] not in seen:
            seen.add(h["volume"])
            labels.append(h["volume"])
    counts: dict[str, Counter] = defaultdict(Counter)
    for h in holdings:
        for c in h["category_canonical"] or ["(uncategorized)"]:
            counts[c][h["volume"]] += 1
    lines += ["\n## Holding counts by issue × volume\n",
              "| issue | " + " | ".join(labels) + " |",
              "|---" * (len(labels) + 1) + "|"]
    for canon in sorted(counts, key=lambda c: -sum(counts[c].values())):
        row = [canon] + [str(counts[canon].get(l, "")) for l in labels]
        lines.append("| " + " | ".join(row) + " |")

    # top ALJs/districts as extraction sanity check
    aljs = Counter(c["alj"] for h in holdings for c in h["cites"])
    lines += ["\n## Top cited ALJs (sanity check)\n"]
    lines += [f"- {a}: {n}" for a, n in aljs.most_common(15)]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).resolve().parent.parent
    ap.add_argument("--manifest", type=Path,
                    default=root / "output" / "inventory" / "manifest.json")
    ap.add_argument("--cache", type=Path, default=root / "output" / "cache" / "text")
    ap.add_argument("--out", type=Path, default=root / "output" / "summaries")
    ap.add_argument("--min-chars", type=int, default=1000)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    manifest = json.loads(args.manifest.read_text())
    cands = [r for r in manifest["files"]
             if r["doc_class"] == "summary" and r["extract_status"] == "ok"
             and r["text_chars"] >= args.min_chars]
    by_texthash: dict[str, dict] = {}
    for r in cands:
        text = (args.cache / f"{r['sha1']}.txt").read_text(errors="replace")
        th = hashlib.sha1(text.encode()).hexdigest()
        if th not in by_texthash:
            r["_text"] = text
            by_texthash[th] = r

    all_holdings, all_index = [], []
    for r in sorted(by_texthash.values(), key=lambda r: year_label(r["filename"])):
        sort_year, label = year_label(r["filename"])
        parser = VolumeParser({"path": r["path"], "year_label": label,
                               "sort_year": sort_year})
        for line in r["_text"].splitlines():
            parser.feed(line)
        holdings, case_index = parser.finish()
        all_holdings.extend(holdings)
        all_index.extend(case_index)
        cited = sum(1 for h in holdings if not h["no_cite"])
        print(f"{label:>10} {r['path'][:60]:<62} {len(holdings):>4} holdings "
              f"({cited} cited)")

    with (args.out / "holdings.jsonl").open("w") as f:
        for h in all_holdings:
            f.write(json.dumps(h) + "\n")
    with (args.out / "case_index.jsonl").open("w") as f:
        for c in all_index:
            f.write(json.dumps(c) + "\n")
    (args.out / "report_holdings.md").write_text(build_report(all_holdings, all_index))
    print(f"\n{len(all_holdings)} holdings, {len(all_index)} case-index lines "
          f"-> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
