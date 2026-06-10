#!/usr/bin/env python3
"""Stage 0: Inventory the raw document drop.

Walks the corpus root, extracts text from every file (cached by content hash),
and emits:
  - manifest.json   one record per file: identity, format, text quality,
                    case numbers, dates, classification
  - report.md       human-readable summary: counts, dedupe groups, coverage
                    map, OCR-quality distribution, date divergence

Idempotent and resumable: text extraction is cached under --cache keyed by
SHA1 of file contents, so re-runs only process new/changed files.

Usage:
    .venv/bin/python pipeline/inventory.py [--corpus-root DIR] [--out DIR] [--cache DIR]
"""

import argparse
import concurrent.futures
import hashlib
import json
import re
import subprocess
import sys
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from striprtf.striprtf import rtf_to_text

# ---------------------------------------------------------------- constants

CASE_NO_RE = re.compile(r"(?<!\d)((?:19|20)\d{8})(?!\d)")
YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
SUMMARY_NAME_RE = re.compile(r"summar|compilation", re.I)

MONTHS = (
    "January|February|March|April|May|June|July|August|September|October|"
    "November|December"
)
DATE_RE = re.compile(rf"({MONTHS})\s+(\d{{1,2}}),?\s+((?:19|20)\d{{2}})", re.I)
# "DATED: April 25, 2012" / "Dated this 25th day of April, 2012" etc.
DATED_LINE_RE = re.compile(
    rf"DATED[:\s].{{0,40}}?({MONTHS})\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+((?:19|20)\d{{2}})"
    rf"|DATED[:\s].{{0,20}}?(\d{{1,2}})(?:st|nd|rd|th)?\s+day\s+of\s+({MONTHS}),?\s+((?:19|20)\d{{2}})",
    re.I | re.S,
)
ALJ_RE = re.compile(
    r"(?:Administrative Law Judge[,:][ \t]+)([A-Z][\w.'’-]+(?:[ \t]+[A-Z][\w.'’-]+){1,3})"
    r"|([A-Z][\w.'’-]+(?:[ \t]+[A-Z][\w.'’-]+){1,3}),?[ \t]+Administrative Law Judge",
)

# Known PDF owner/user passwords (CCSA-era summary docs); tried in order when
# a PDF reports "Incorrect password".
PDF_PASSWORDS = ["CCSArespect2008", "CCSArally2009"]

NATIVE_EXTS = {".rtf", ".doc", ".docx"}
PDF_TEXT_CPP = 200     # chars/page >= this -> real text layer
PDF_PARTIAL_CPP = 20   # below this -> effectively scanned

# Rank for picking the best representative of a duplicate group. pdf_ocr
# (rapidocr text recovered from an image-only scan) ranks below any native
# text layer but above the unusable kinds.
KIND_RANK = {"native_docx": 0, "native_doc": 1, "native_rtf": 2,
             "pdf_text": 3, "pdf_partial": 4, "pdf_ocr": 5,
             "pdf_scanned": 6, "error": 9}

# ---------------------------------------------------------------- extraction


def sha1_of(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def run(cmd: list[str], timeout: int = 120) -> str:
    out = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if out.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {out.stderr.decode(errors='replace')[:200]}")
    return out.stdout.decode("utf-8", errors="replace")


def extract_pdf(path: Path) -> str:
    try:
        return run(["pdftotext", str(path), "-"])
    except RuntimeError as e:
        if "password" not in str(e).lower():
            raise
        for pw in PDF_PASSWORDS:
            try:
                return run(["pdftotext", "-upw", pw, str(path), "-"])
            except RuntimeError:
                continue
        raise


def extract_doc(path: Path) -> str:
    return run(["antiword", "-w", "0", str(path)])


def extract_rtf(path: Path) -> str:
    raw = path.read_bytes().decode("latin-1", errors="replace")
    return rtf_to_text(raw, errors="ignore")


def extract_docx(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", errors="replace")
    xml = re.sub(r"</w:p>", "\n", xml)
    return re.sub(r"<[^>]+>", "", xml)


EXTRACTORS = {".pdf": extract_pdf, ".doc": extract_doc,
              ".rtf": extract_rtf, ".docx": extract_docx}


def pdf_page_count(path: Path) -> int | None:
    try:
        info = run(["pdfinfo", str(path)])
        m = re.search(r"^Pages:\s+(\d+)", info, re.M)
        return int(m.group(1)) if m else None
    except Exception:
        return None


def cached_text(sha1: str, cache_dir: Path) -> str:
    """Best-available cached text for a content hash, for downstream stages
    that look up text by sha1: the OCR sidecar wins only when the native
    text is effectively empty (same rule as get_text)."""
    native = cache_dir / f"{sha1}.txt"
    text = native.read_text(errors="replace") if native.exists() else ""
    ocr = cache_dir / f"{sha1}.ocr.txt"
    if ocr.exists() and len(text.strip()) < 1000:
        t2 = ocr.read_text(errors="replace")
        if len(t2.strip()) > len(text.strip()):
            return t2
    return text


def get_text(path: Path, cache_dir: Path) -> tuple[str, str, str, str]:
    """Return (text, sha1, status, source). Cached by content hash.

    source is "native" or "ocr": when ocr_pass.py has left a {sha}.ocr.txt
    sidecar AND native extraction was effectively empty (an image-only scan),
    the OCR text is preferred and the caller marks the file kind=pdf_ocr.
    A real native text layer always wins over OCR."""
    sha = sha1_of(path)
    txt_file = cache_dir / f"{sha}.txt"
    ocr_file = cache_dir / f"{sha}.ocr.txt"

    def prefer_ocr(native_text: str):
        if ocr_file.exists() and len(native_text.strip()) < 1000:
            ocr_text = ocr_file.read_text(errors="replace")
            if len(ocr_text.strip()) > len(native_text.strip()):
                return ocr_text
        return None

    if txt_file.exists():
        text = txt_file.read_text(errors="replace")
        ocr = prefer_ocr(text)
        return (ocr, sha, "ok", "ocr") if ocr else (text, sha, "ok", "native")
    extractor = EXTRACTORS.get(path.suffix.lower())
    if extractor is None:
        return "", sha, "skipped: no extractor", "native"
    try:
        text = extractor(path)
        txt_file.write_text(text)
        ocr = prefer_ocr(text)
        return (ocr, sha, "ok", "ocr") if ocr else (text, sha, "ok", "native")
    except Exception as e:  # failures are NOT cached: retried every run
        ocr = prefer_ocr("")
        if ocr:
            return ocr, sha, "ok", "ocr"
        return "", sha, f"error: {type(e).__name__}: {e}", "native"


# ---------------------------------------------------------------- analysis


def classify_pdf_kind(text: str, pages: int | None) -> str:
    cpp = len(text.strip()) / max(pages or 1, 1)
    if cpp >= PDF_TEXT_CPP:
        return "pdf_text"
    if cpp >= PDF_PARTIAL_CPP:
        return "pdf_partial"
    return "pdf_scanned"


def alpha_ratio(text: str) -> float | None:
    stripped = re.sub(r"\s", "", text)
    if not stripped:
        return None
    return sum(c.isalpha() for c in stripped) / len(stripped)


def parse_dated_line(text: str) -> str | None:
    m = DATED_LINE_RE.search(text)
    if not m:
        return None
    if m.group(1):
        month, day, year = m.group(1), m.group(2), m.group(3)
    else:
        day, month, year = m.group(4), m.group(5), m.group(6)
    try:
        dt = datetime.strptime(f"{month} {day} {year}", "%B %d %Y")
        return dt.date().isoformat()
    except ValueError:
        return None


def parse_alj(text: str) -> str | None:
    m = ALJ_RE.search(text)
    if not m:
        return None
    name = (m.group(1) or m.group(2)).strip()
    # filter obvious false captures
    if re.search(r"Office|State|California|Hearings|DECISION|PROPOSED|Respondent",
                 name, re.I):
        return None
    return name


def district_from_filename(stem: str) -> str | None:
    # strip case numbers, parentheticals, trailing separators
    s = CASE_NO_RE.sub("", stem)
    s = re.sub(r"\(\d*\)", "", s)
    s = re.sub(r"OAH(\s+Case)?(\s+No\.?)?", "", s, flags=re.I)
    s = s.split(" - ")[0]
    s = s.strip(" -_.")
    return s or None


def analyze_file(path: Path, corpus_root: Path, cache_dir: Path) -> dict:
    rel = path.relative_to(corpus_root)
    stem = path.stem
    ext = path.suffix.lower()
    stat = path.stat()
    text, sha, status, source = get_text(path, cache_dir)

    pages = pdf_page_count(path) if ext == ".pdf" else None
    if status != "ok":
        kind = "error"
    elif source == "ocr":
        kind = "pdf_ocr"
    elif ext == ".pdf":
        kind = classify_pdf_kind(text, pages)
    else:
        kind = f"native_{ext[1:]}"

    fn_cases = sorted(set(CASE_NO_RE.findall(stem)))
    text_head = text[:4000]
    text_cases = sorted(set(CASE_NO_RE.findall(text_head)))

    is_summary = bool(SUMMARY_NAME_RE.search(stem))
    if is_summary:
        doc_class = "summary"
    elif fn_cases or re.search(r"PROPOSED\s+DECISION", text_head, re.I):
        doc_class = "decision"
    else:
        doc_class = "unknown"

    all_dates = [f"{y}-{datetime.strptime(mo[:3], '%b').month:02d}-{int(d):02d}"
                 for mo, d, y in DATE_RE.findall(text)]
    return {
        "path": str(rel),
        "batch": rel.parts[0],
        "filename": path.name,
        "ext": ext,
        "size": stat.st_size,
        "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).date().isoformat(),
        "sha1": sha,
        "extract_status": status,
        "kind": kind,
        "pdf_pages": pages,
        "text_chars": len(text.strip()),
        "alpha_ratio": round(r, 3) if (r := alpha_ratio(text)) is not None else None,
        "doc_class": doc_class,
        "case_numbers_filename": fn_cases,
        "case_numbers_text": text_cases,
        "district_guess": district_from_filename(stem) if doc_class == "decision" else None,
        "years_in_filename": sorted(set(YEAR_RE.findall(stem))),
        "decision_date": parse_dated_line(text),
        "alj_guess": parse_alj(text) if doc_class == "decision" else None,
        "date_count_in_text": len(all_dates),
    }


# ---------------------------------------------------------------- reporting


def best_of_group(members: list[dict]) -> dict:
    return min(members, key=lambda r: (KIND_RANK.get(r["kind"], 9), -r["text_chars"]))


def build_report(records: list[dict], corpus_root: Path) -> str:
    decisions = [r for r in records if r["doc_class"] == "decision"]
    summaries = [r for r in records if r["doc_class"] == "summary"]
    unknown = [r for r in records if r["doc_class"] == "unknown"]

    lines = []
    w = lines.append
    w(f"# Inventory Report — {corpus_root.name}")
    w(f"\nGenerated {datetime.now(timezone.utc).isoformat(timespec='seconds')} · "
      f"{len(records)} files\n")

    # --- overall counts
    w("## Overall\n")
    w(f"- decisions: {len(decisions)}  ·  summaries: {len(summaries)}  ·  "
      f"unclassified: {len(unknown)}")
    by_batch = Counter(r["batch"] for r in records)
    w("- by batch: " + ", ".join(f"`{b}` {n}" for b, n in sorted(by_batch.items())))
    by_kind = Counter(r["kind"] for r in records)
    w("- by kind: " + ", ".join(f"{k} {n}" for k, n in by_kind.most_common()))
    errs = [r for r in records if r["kind"] == "error"]
    if errs:
        w(f"- **extraction errors: {len(errs)}** (see manifest, `extract_status`)")

    # --- text quality
    w("\n## Decision text quality (per unique case, best available format)\n")
    groups: dict[str, list[dict]] = defaultdict(list)
    no_case = []
    for r in decisions:
        cases = r["case_numbers_filename"] or r["case_numbers_text"][:1]
        if not cases:
            no_case.append(r)
        for c in cases:
            groups[c].append(r)
    best = {c: best_of_group(ms) for c, ms in groups.items()}
    best_kinds = Counter(b["kind"] for b in best.values())
    w(f"- unique case numbers: {len(groups)}")
    w("- best-format distribution: " +
      ", ".join(f"**{k}** {n}" for k, n in best_kinds.most_common()))
    need_ocr = [c for c, b in best.items() if b["kind"] in ("pdf_scanned", "pdf_partial", "error")]
    w(f"- cases whose ONLY copy needs OCR/repair: **{len(need_ocr)}** "
      f"({100 * len(need_ocr) / max(len(groups), 1):.0f}%)")
    if no_case:
        w(f"- decision files with no case number anywhere: {len(no_case)}")

    # --- duplicates
    dupes = {c: ms for c, ms in groups.items() if len(ms) > 1}
    w(f"\n## Duplicates\n\n- case numbers with >1 file: {len(dupes)}")
    sig = Counter(tuple(sorted(m["kind"] for m in ms)) for ms in dupes.values())
    for combo, n in sig.most_common(12):
        w(f"  - {' + '.join(combo)}: {n}")

    # --- coverage map
    w("\n## Coverage map (decision year = OAH case-number prefix)\n")
    summary_years: set[str] = set()
    for s in summaries:
        summary_years.update(s["years_in_filename"])
    dec_years = Counter(c[:4] for c in groups)
    all_years = sorted(set(dec_years) | summary_years)
    w("| year | decisions (unique) | summary doc? | overlap |")
    w("|------|-------------------:|:------------:|:-------:|")
    for y in all_years:
        n = dec_years.get(y, 0)
        has_sum = "yes" if y in summary_years else "—"
        overlap = "**GOLD**" if n and y in summary_years else ""
        w(f"| {y} | {n} | {has_sum} | {overlap} |")

    # --- summaries listing
    w("\n## Summary documents\n")
    for s in sorted(summaries, key=lambda r: r["filename"]):
        w(f"- `{s['path']}` — {s['kind']}, {s['text_chars']:,} chars"
          + (f", {s['pdf_pages']}pp" if s["pdf_pages"] else ""))

    # --- dates
    w("\n## Dates\n")
    dated = [r for r in decisions if r["decision_date"]]
    w(f"- decisions with a parsed `DATED:` line: {len(dated)}/{len(decisions)}")
    diverge = Counter()
    for r in dated:
        for c in r["case_numbers_filename"]:
            diverge[int(r["decision_date"][:4]) - int(c[:4])] += 1
    if diverge:
        w("- decision-date year minus case-number year: " +
          ", ".join(f"{k:+d}: {n}" for k, n in sorted(diverge.items())))
    mt_years = Counter(r["mtime"][:4] for r in records)
    w("- file mtimes (download/extract proxy, NOT upload date): " +
      ", ".join(f"{y}: {n}" for y, n in sorted(mt_years.items())))

    # --- ALJs
    w("\n## ALJ guesses (top 15, regex pass — sanity check only)\n")
    aljs = Counter(r["alj_guess"] for r in decisions if r["alj_guess"])
    for name, n in aljs.most_common(15):
        w(f"- {name}: {n}")
    w(f"\n(decisions with an ALJ match: {sum(aljs.values())}/{len(decisions)})")

    # --- unclassified
    if unknown:
        w("\n## Unclassified files\n")
        for r in sorted(unknown, key=lambda r: r["path"]):
            w(f"- `{r['path']}` ({r['kind']}, {r['text_chars']:,} chars)")
    w("")
    return "\n".join(lines)


# ---------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).resolve().parent.parent
    ap.add_argument("--corpus-root", type=Path, default=root / "Cert_Layoffs_Docs_V1")
    ap.add_argument("--out", type=Path, default=root / "output" / "inventory")
    ap.add_argument("--cache", type=Path, default=root / "output" / "cache" / "text")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    args.cache.mkdir(parents=True, exist_ok=True)

    files = sorted(p for p in args.corpus_root.rglob("*")
                   if p.is_file() and p.suffix.lower() in EXTRACTORS)
    skipped = sorted(str(p.relative_to(args.corpus_root))
                     for p in args.corpus_root.rglob("*")
                     if p.is_file() and p.suffix.lower() not in EXTRACTORS)
    print(f"{len(files)} files to inventory ({len(skipped)} skipped: "
          f"{', '.join(skipped) or 'none'})")

    records = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(analyze_file, p, args.corpus_root, args.cache): p
                for p in files}
        for i, fut in enumerate(concurrent.futures.as_completed(futs), 1):
            records.append(fut.result())
            if i % 100 == 0:
                print(f"  {i}/{len(files)}")
    records.sort(key=lambda r: r["path"])

    manifest = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "corpus_root": str(args.corpus_root),
        "skipped_files": skipped,
        "files": records,
    }
    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    report = build_report(records, args.corpus_root)
    (args.out / "report.md").write_text(report)
    print(f"\nWrote {args.out / 'manifest.json'} and {args.out / 'report.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
