#!/usr/bin/env python3
"""OCR pass for scanned PDFs the text extractors came up empty on.

The 2004 cluster (57 decisions + the 2004 Layoff Summaries volume) is
image-only scans -- no embedded text layer -- so stage 0 classified them
pdf_scanned and stage 2 skipped them. This pass renders each page at 300dpi
(pdftoppm), OCRs it with rapidocr (onnxruntime, CPU -- deterministic
weights, no LLM in the gold/text layer), reconstructs reading-order lines
from the result boxes, and writes sidecars into the text cache:

  {sha1}.ocr.txt    reconstructed text (consumed by inventory.py, which
                    prefers it when native extraction was effectively empty
                    and marks the file kind=pdf_ocr)
  {sha1}.ocr.json   provenance: engine, page count, mean confidence

Idempotent: a file whose sidecar exists is skipped; delete the sidecar to
re-OCR. Run inventory.py afterwards to fold OCR text into the manifest.

Usage:
    .venv/bin/python pipeline/ocr_pass.py [--doc-class summary|decision]
        [--year YYYY] [--limit N] [--dpi 300]
"""

import argparse
import json
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def lines_from_boxes(result) -> list[str]:
    """Group OCR boxes into reading-order lines: boxes whose vertical
    centers sit within ~60% of the median box height of one another are one
    line, ordered left-to-right."""
    items = []
    for box, text, _conf in result:
        ys = [p[1] for p in box]
        xs = [p[0] for p in box]
        items.append(((min(ys) + max(ys)) / 2, min(xs), max(ys) - min(ys), text))
    if not items:
        return []
    med_h = statistics.median(h for _, _, h, _ in items)
    tol = max(med_h * 0.6, 6)
    items.sort(key=lambda t: (t[0], t[1]))
    lines, cur, cur_y = [], [], None
    for yc, x, _h, text in items:
        if cur_y is None or abs(yc - cur_y) <= tol:
            cur.append((x, text))
            cur_y = yc if cur_y is None else (cur_y + yc) / 2
        else:
            cur.sort()
            lines.append(" ".join(t for _, t in cur))
            cur, cur_y = [(x, text)], yc
    if cur:
        cur.sort()
        lines.append(" ".join(t for _, t in cur))
    return lines


def ocr_pdf(ocr, pdf_path: Path, dpi: int) -> tuple[str, dict]:
    pages_text, confs = [], []
    with tempfile.TemporaryDirectory() as td:
        subprocess.run(["pdftoppm", "-r", str(dpi), "-gray", "-png",
                        str(pdf_path), f"{td}/page"],
                       check=True, capture_output=True, timeout=600)
        pages = sorted(Path(td).glob("page-*.png"))
        for png in pages:
            result, _ = ocr(str(png))
            result = result or []
            confs += [float(c) for _, _, c in result]
            pages_text.append("\n".join(lines_from_boxes(result)))
    text = "\n\n".join(pages_text)
    meta = {"engine": "rapidocr_onnxruntime", "dpi": dpi,
            "pages": len(pages_text),
            "mean_confidence": round(statistics.mean(confs), 4) if confs else None,
            "chars": len(text)}
    return text, meta


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--manifest", type=Path,
                    default=ROOT / "output" / "inventory" / "manifest.json")
    ap.add_argument("--cache", type=Path,
                    default=ROOT / "output" / "cache" / "text")
    ap.add_argument("--doc-class", choices=["summary", "decision"])
    ap.add_argument("--year", help="restrict to files whose case numbers "
                                   "start with this year")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--dpi", type=int, default=300)
    args = ap.parse_args()

    manifest = json.loads(args.manifest.read_text())
    todo = []
    for r in manifest["files"]:
        if r["kind"] not in ("pdf_scanned", "pdf_partial"):
            continue
        if args.doc_class and r["doc_class"] != args.doc_class:
            continue
        if args.year and not any(c.startswith(args.year)
                                 for c in r["case_numbers_filename"]):
            continue
        if (args.cache / f"{r['sha1']}.ocr.txt").exists():
            continue
        todo.append(r)
    # one OCR per content hash -- duplicate files share the sidecar
    todo = list({r["sha1"]: r for r in todo}.values())
    todo = todo[:args.limit] if args.limit else todo
    print(f"{len(todo)} scanned files to OCR", flush=True)
    if not todo:
        return 0

    from rapidocr_onnxruntime import RapidOCR
    ocr = RapidOCR()
    corpus_root = Path(manifest["corpus_root"])
    for i, r in enumerate(todo, 1):
        t0 = time.time()
        pdf = corpus_root / r["path"]
        try:
            text, meta = ocr_pdf(ocr, pdf, args.dpi)
        except Exception as e:
            print(f"[{i}/{len(todo)}] {r['path'][:70]} ERROR {e}", flush=True)
            continue
        (args.cache / f"{r['sha1']}.ocr.txt").write_text(text)
        (args.cache / f"{r['sha1']}.ocr.json").write_text(json.dumps(meta))
        print(f"[{i}/{len(todo)}] {r['path'][:70]} {meta['pages']}p "
              f"{meta['chars']}ch conf={meta['mean_confidence']} "
              f"{time.time() - t0:.0f}s", flush=True)
    print("done; re-run inventory.py to fold OCR text into the manifest")
    return 0


if __name__ == "__main__":
    sys.exit(main())
