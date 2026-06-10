#!/usr/bin/env python3
"""Deterministic respondent-roster extraction (REFINEMENTS #6).

Mass-layoff decisions carry their respondent roster as a structured name
table (appendix/attachment/exhibit) -- a flat alphabetical list of 200-400
names. Transcribing that through the LLM dispositions pass is slow,
overflow-prone (output scales O(roster); both models truncated to invalid
JSON on 2009030327), and a transposition surface on mechanically
extractable data. This module parses the two table shapes the corpus
actually contains, leaving the LLM pass to the per-respondent dispositions
the decision discusses individually:

  1. Pipe-delimited tables (RTF/table text extraction renders cells as
     `|`): a header row whose leading cells name the columns
     (`Last|First|Site`, `LAST NAME|FIRST NAME|`), then one or more
     (last, first) groups per line -- including the "folded" two-column
     layout where each printed row carries two roster entries
     (LAST|FIRST|LAST|FIRST|, observed in 2009030327).
  2. Line lists: runs of `Last, First [Middle]` lines, optionally numbered.

Precision-first: a pipe table is parsed only under an explicit header, a
line list only as an 8+-line run; anything else returns None and the model
roster stands. Names are recorded verbatim, OCR damage intact ("extract
raw, normalize later"). Inline status annotations ("ADKINS rescinded") are
split off and surfaced, never dropped.

CLI (verification against cached model output, no model calls):
    .venv/bin/python pipeline/roster.py compare --year 2009
"""

import argparse
import json
import re
import sys
import unicodedata

# --------------------------------------------------------------- name keys

WORD = re.compile(r"[A-Za-z][\w'’\-]+")
NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}

_NAME_TRANS = str.maketrans({
    "‘": "'", "’": "'", "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-",
})


def _norm(s: str) -> str:
    return unicodedata.normalize("NFKC", s or "").translate(_NAME_TRANS)


def name_parts(name: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(surname_tokens, given_tokens), casefolded, suffixes stripped.
    Handles both 'Last, First M.' and 'First M. Last' orderings. Single-
    letter initials never survive WORD, which conveniently ignores middle
    initials for matching purposes."""
    s = _norm(name).strip()
    if "," in s:
        last, _, first = s.partition(",")
        lt = [t.casefold() for t in WORD.findall(last)]
        gt = [t.casefold() for t in WORD.findall(first)]
    else:
        toks = [t.casefold() for t in WORD.findall(s)]
        lt, gt = toks[-1:], toks[:-1]
    lt = [t for t in lt if t.rstrip(".") not in NAME_SUFFIXES] or lt
    gt = [t for t in gt if t.rstrip(".") not in NAME_SUFFIXES]
    return tuple(lt), tuple(gt)


def name_key(name: str) -> tuple:
    """Order-insensitive comparison key: (final surname token, first given
    token). Multi-token surnames ('Van Heerde', 'Alcaide Tubio') key on the
    final token so 'Suzanne Van Heerde' == 'Van Heerde, Suzanne'."""
    lt, gt = name_parts(name)
    return (lt[-1] if lt else "", gt[0] if gt else "")


def _ed1(a: str, b: str) -> bool:
    """True when edit distance between a and b is exactly 1 (one
    substitution, insertion, or deletion) -- the OCR name-split signature
    (Cabrera/Cabrer, Vollmer/Vollmar)."""
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    if len(a) > len(b):
        a, b = b, a
    i = 0
    while i < len(a) and a[i] == b[i]:
        i += 1
    return a[i:] == b[i + 1:]


def fuzzy_same_person(n1: str, n2: str) -> bool:
    """Same given first token AND surname within edit distance 1: treated
    as one person spelled two ways (phantom-respondent guard, REFINEMENTS
    #11). Distinct given names never fuzzy-match."""
    (s1, g1), (s2, g2) = name_key(n1), name_key(n2)
    if not (g1 and g2 and s1 and s2) or g1 != g2:
        return False
    return s1 != s2 and _ed1(s1, s2)


class RefResolver:
    """Bind free-text respondent names to roster refs.

    The 2009 audit's dominant disposition defect (~26% of cases) was not a
    model error: merge keyed name->ref by surname alone, so same-surname
    roster pairs (Smith/Smith) collided in the dict -- one ref duplicated,
    the adjacent one dropped. Resolution order here: exact (surname, given)
    match; unique surname; surname narrowed by given-token prefix overlap.
    Ambiguous names resolve to None with a reason, never to a guess.
    """

    def __init__(self, roster: list[tuple[str, str]]):  # [(ref, name)]
        self.by_full: dict[tuple, list[str]] = {}
        self.by_surname: dict[str, list[tuple[str, tuple]]] = {}
        for ref, nm in roster:
            lt, gt = name_parts(nm)
            sk = lt[-1] if lt else ""
            self.by_full.setdefault((sk, gt[0] if gt else ""), []).append(ref)
            self.by_surname.setdefault(sk, []).append((ref, gt))

    def resolve(self, name: str) -> tuple[str | None, str]:
        """Returns (ref, status); status in {ok, ambiguous, not_in_roster}."""
        lt, gt = name_parts(name)
        sk = lt[-1] if lt else ""
        full = self.by_full.get((sk, gt[0] if gt else ""), [])
        if len(full) == 1:
            return full[0], "ok"
        cands = self.by_surname.get(sk, [])
        if len(cands) == 1:
            return cands[0][0], "ok"
        if len(cands) > 1 and gt:
            # several roster entries share the surname: need given-name
            hits = [r for r, g in cands if g and (
                g[0] == gt[0] or g[0].startswith(gt[0]) or gt[0].startswith(g[0]))]
            if len(hits) == 1:
                return hits[0], "ok"
        if cands:
            return None, "ambiguous"
        if gt:  # phantom guard: unique near-duplicate surname, same given
            fuzz = []
            for sk2, lst in self.by_surname.items():
                if not _ed1(sk, sk2):
                    continue
                for ref, g in lst:
                    if g and g[0] == gt[0]:
                        fuzz.append(ref)
            if len(fuzz) == 1:
                return fuzz[0], "fuzzy"
        return None, "not_in_roster"


# ------------------------------------------------------------- pipe tables

HEADER_LAST = re.compile(r"^last(\s*name)?$", re.I)
HEADER_FIRST = re.compile(r"^first(\s*name)?$", re.I)
AUX_HEADER = re.compile(
    r"^(site|school|location|name|middle(\s*initial)?|m\.?i\.?|grade|"
    r"position|assignment|status|fte)s?\.?$", re.I)
ANNOT_RE = re.compile(
    r"[\s‐-―-]+(rescinded?|dismissed|withdrawn|withdrew|resigned|"
    r"retired|settled)\.?\s*$", re.I)
# verbatim name cell: letters (any case), spaces, apostrophes, hyphens,
# periods; no digits. OCR mid-name spaces ("DAN IELLE") pass by design.
NAME_CELL = re.compile(r"^[A-Za-z][A-Za-z'’.‐-―\- ]{0,39}$")
# cells that are plainly not personal names even when they match NAME_CELL
NONNAME_TOKENS = re.compile(
    r"\b(school|district|teacher|services?|education|elementary|grade|"
    r"counselor|nurse|fte|total|page|exhibit|appendix|attachment|order|"
    r"respondents?)\b", re.I)


def _name_cell(cell: str) -> tuple[str, str | None] | None:
    """Validate a verbatim table cell as a name part; split a trailing
    status annotation. Returns (name_part, annotation) or None."""
    cell = cell.strip()
    annot = None
    m = ANNOT_RE.search(cell)
    if m:
        annot = m.group(1).lower()
        cell = cell[:m.start()].strip()
    if not cell or not NAME_CELL.match(cell) or NONNAME_TOKENS.search(cell):
        return None
    return cell, annot


def _prejoin_hyphen_breaks(lines: list[str]) -> list[str]:
    """Rejoin a name split across lines inside a table cell
    ('Watson-\\nRodgers|Leah|...'): a short pipe-less line ending in a
    hyphen merges into the following pipe line."""
    out, i = [], 0
    while i < len(lines):
        ln = lines[i]
        if ("|" not in ln and len(ln.strip()) <= 25 and i + 1 < len(lines)
                and "|" in lines[i + 1]
                and re.search(r"[‐-―-]$", ln.strip())):
            out.append(ln.strip() + lines[i + 1].lstrip())
            i += 2
            continue
        out.append(ln)
        i += 1
    return out


def _parse_pipe_tables(lines: list[str]) -> list[dict]:
    entries: list[dict] = []
    i = 0
    while i < len(lines):
        cells = [c.strip() for c in lines[i].split("|")]
        if not (len(cells) >= 2 and HEADER_LAST.match(cells[0])
                and HEADER_FIRST.match(cells[1])):
            i += 1
            continue
        # column period: leading run of recognized header cells (>= 2).
        # The folded layout puts data right after the header on the same
        # line ('LAST NAME|FIRST NAME|BARTLETT rescinded|TRACY|').
        period = 2
        while period < len(cells) and cells[period] and AUX_HEADER.match(cells[period]):
            period += 1
        misses = 0
        row_cells = cells[period:]  # header-line remainder is data (folded)
        j = i
        while True:
            while row_cells and not row_cells[-1]:
                row_cells.pop()
            got = 0
            for k in range(0, max(len(row_cells) - 1, 0), period):
                chunk = row_cells[k:k + period]
                if len(chunk) < 2:
                    break
                last = _name_cell(chunk[0])
                first = _name_cell(chunk[1])
                if last and first:
                    entries.append({
                        "last": last[0], "first": first[0],
                        "name": f"{first[0]} {last[0]}".strip(),
                        "annotation": last[1] or first[1],
                    })
                    got += 1
            j += 1
            if j >= len(lines):
                break
            nxt = lines[j]
            if not nxt.strip():
                continue  # blank lines inside tables are common
            row_cells = [c.strip() for c in nxt.split("|")]
            if not got and "|" not in nxt:
                misses += 1
            elif not got:
                misses += 1
            else:
                misses = 0
            if misses >= 3:
                break
        i = j
    return entries


# -------------------------------------------------------------- line lists

LASTFIRST_LINE = re.compile(
    r"^\s*(?:\d{1,3}[.)]\s*)?"
    r"([A-Z][A-Za-z'’‐-―\- ]{1,30}),\s+"
    r"([A-Z][A-Za-z'’.‐-―\- ]{1,40})\s*$")


def _parse_line_lists(lines: list[str], min_run: int = 8) -> list[dict]:
    entries, run, gaps = [], [], 0
    out: list[dict] = []

    def flush():
        nonlocal run
        if len(run) >= min_run:
            out.extend(run)
        run = []

    for ln in lines:
        m = LASTFIRST_LINE.match(ln)
        ok = None
        if m:
            last = _name_cell(m.group(1))
            first = _name_cell(m.group(2))
            if last and first:
                ok = {"last": last[0], "first": first[0],
                      "name": f"{first[0]} {last[0]}".strip(),
                      "annotation": last[1] or first[1]}
        if ok:
            run.append(ok)
            gaps = 0
        elif run and not ln.strip() and gaps < 1:
            gaps += 1  # tolerate one blank line inside a list
        else:
            flush()
            gaps = 0
    flush()
    return out


# -------------------------------------------------------------- entry point


def parse_roster(text: str) -> dict | None:
    """Deterministic roster from a decision text, or None when no
    structured name table is confidently present. Dedupes on
    (surname, given) key, keeping first occurrence (document order)."""
    lines = _prejoin_hyphen_breaks(_norm(text).splitlines())
    entries = _parse_pipe_tables(lines)
    fmt = "pipe_table"
    if not entries:
        entries = _parse_line_lists(lines)
        fmt = "line_list"
    if not entries:
        return None
    seen, uniq = set(), []
    for e in entries:
        k = name_key(e["name"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(e)
    return {"format": fmt, "entries": uniq}


# --------------------------------------------------------------------- CLI


def cmd_compare(year: str) -> int:
    """Compare deterministic parses against cached model rosters."""
    from extract import CACHE, MODELS, RAW, select_cases
    from inventory import cached_text
    primary = MODELS[0].replace(":", "_")
    n_parsed = n_model_only_big = 0
    print(f"{'case':<12} {'det':>4} {'model':>5} {'∩':>4}  notes")
    for c in select_cases(year, None):
        text = cached_text(c['best']['sha1'], CACHE)
        det = parse_roster(text)
        raw = RAW / f"{c['case_no']}__dispositions__{primary}.json"
        model_names = []
        if raw.exists():
            parsed = json.loads(raw.read_text()).get("parsed") or {}
            model_names = [e.get("name", "") for e in parsed.get("roster", [])]
        if not det and len(model_names) < 30:
            continue  # uninteresting: small roster, no table — model stands
        det_keys = {name_key(e["name"]) for e in (det or {"entries": []})["entries"]}
        mod_keys = {name_key(n) for n in model_names}
        inter = len(det_keys & mod_keys)
        notes = []
        if det:
            n_parsed += 1
            if det_keys - mod_keys:
                sample = [e["name"] for e in det["entries"]
                          if name_key(e["name"]) in det_keys - mod_keys][:3]
                notes.append(f"det-only {len(det_keys - mod_keys)} e.g. {sample}")
            if mod_keys - det_keys:
                sample = [n for n in model_names
                          if name_key(n) in mod_keys - det_keys][:3]
                notes.append(f"model-only {len(mod_keys - det_keys)} e.g. {sample}")
        else:
            n_model_only_big += 1
            notes.append("NO TABLE PARSED but model roster is large")
        print(f"{c['case_no']:<12} {len(det_keys):>4} {len(mod_keys):>5} "
              f"{inter:>4}  {'; '.join(notes)}")
    print(f"\n{n_parsed} cases with deterministic roster; "
          f"{n_model_only_big} large model rosters with no parsed table")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("cmd", choices=["compare"])
    ap.add_argument("--year", default="2009")
    args = ap.parse_args()
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
    return cmd_compare(args.year)


if __name__ == "__main__":
    sys.exit(main())
