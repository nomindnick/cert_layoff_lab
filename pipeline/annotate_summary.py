#!/usr/bin/env python3
"""Annotated verification edition of the rendered annual summary.

Spike scaffolding for gold-covered years (it also remains useful in
production as the eval-presentation layer for any year with a human volume):
takes the entries manifest written by render_summary.py and the structured
alignment written by eval_year.py, and renders a self-contained HTML report
in which every entry is color-coded by its *deterministic* eval class:

  confirmed   exact/fuzzy gold match (alignment status: matched)
  divergent   same content, filed under a different section than the volume
  new         over-recovery — not catalogued by the human volume
  missed      gold holdings the extraction did not recover (appendix)

plus the by-design exclusion classes (editorial citation lines, rule
statements on default matters) and the dataset-coverage note. Matched entries
quote the human volume's paragraph verbatim so the comparison is per-holding
and self-verifying.

Classification is NEVER done by an LLM here — it comes from the alignment.
The optional per-block commentary (commentary_{year}.json, written by Claude
subagents from the skeleton this script emits) only *explains* an
already-made classification; blocks without commentary get a class-default
sentence.

Usage:
  annotate_summary.py --year 2009 --skeleton   write commentary_skeleton_{year}.json
  annotate_summary.py --year 2009              render annotated_summary_{year}.html
"""

import argparse
import datetime
import html
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


CLASS_META = {
    "confirmed": ("Confirmed against the human volume", "#2e7d32", "#f3f9f3"),
    "divergent": ("Matched — filed under a different section", "#b07d10", "#fdf8ec"),
    "new": ("Not in the human volume (addition)", "#1565c0", "#f2f6fc"),
    "missed": ("In the human volume, not recovered", "#c62828", "#fdf3f3"),
}

DEFAULT_COMMENT = {
    "confirmed": "Substantively the same holding as the human volume entry quoted above.",
    "divergent": "Same substance as the human volume entry quoted above; the system filed it under a different issue section than the volume's editors.",
    "new": "Not catalogued in the human volume.",
    "missed": "Not recovered by the extraction for this case.",
}


def load(year):
    entries_p = ROOT / "output" / "reports" / year / f"entries_{year}.json"
    align_p = ROOT / "output" / "eval" / f"alignment_{year}.json"
    if not entries_p.exists():
        sys.exit(f"missing {entries_p} — run render_summary.py --year {year} first")
    if not align_p.exists():
        sys.exit(f"missing {align_p} — run eval_year.py --year {year} first")
    return json.loads(entries_p.read_text()), json.loads(align_p.read_text())


def classify(entries, align):
    """Attach a verification class and matched gold texts to every entry."""
    sys_map = {(s["case_no"], s["holding_idx"]): s for s in align["system"]}
    gold = align["gold"]
    for sec in entries["sections"]:
        for e in sec["entries"]:
            s = sys_map.get((e["case_no"], e["holding_idx"]))
            status = s["status"] if s else "over_recovery"
            e["klass"] = {"matched": "confirmed",
                          "category_divergent_match": "divergent",
                          "over_recovery": "new"}[status]
            e["gold_texts"] = [gold[gi]["text"] for gi in
                               (s["matched_gold_idxs"] if s else [])]
            e["block_id"] = f"{e['case_no']}:{e['holding_idx']}"
    missed = [g for g in gold if g["status"] == "missed"]
    excluded_cite = [g for g in gold if g["status"] == "excluded_citation_line"]
    excluded_default = [g for g in gold
                        if g["status"] == "excluded_default_rule_statement"]
    not_in_set = [g for g in gold
                  if g["status"] in ("decision_not_in_set", "ambiguous_match")]
    return missed, excluded_cite, excluded_default, not_in_set


def write_skeleton(year, entries, missed, out_dir):
    """The commentary work-list consumed by Claude subagents. Each block
    carries everything needed to write its note; agents must not reclassify."""
    by_case = defaultdict(list)
    for sec in entries["sections"]:
        for e in sec["entries"]:
            by_case[e["case_no"]].append(e["text"])
    blocks = []
    for sec in entries["sections"]:
        for e in sec["entries"]:
            blocks.append({
                "id": e["block_id"],
                "kind": e["klass"],
                "section": sec["title"],
                "category": e["category"],
                "subtype": e.get("subtype"),
                "notable": e.get("notable", False),
                "system_text": e["text"],
                "gold_texts": e["gold_texts"],
            })
    for g in missed:
        blocks.append({
            "id": f"gold:{g['gold_idx']}",
            "kind": "missed",
            "categories": g["categories"],
            "gold_text": g["text"],
            "case_no": g["case_no"],
            "extracted_holdings_for_case": by_case.get(g["case_no"], []),
        })
    p = out_dir / f"commentary_skeleton_{year}.json"
    p.write_text(json.dumps(blocks, indent=1, ensure_ascii=False))
    print(f"wrote {p} ({len(blocks)} blocks)")
    # Split into kind-grouped batch files sized for one subagent each, so the
    # commentary fan-out is mechanical: one agent per in_NN.json, each writes
    # the matching out_NN.json; --merge validates and assembles them.
    parts_dir = out_dir / "commentary_parts"
    parts_dir.mkdir(exist_ok=True)
    for old in parts_dir.glob("in_*.json"):
        old.unlink()
    groups = defaultdict(list)
    for b in blocks:
        groups[b["kind"]].append(b)
    i = 0
    for kind in ("new", "confirmed", "divergent", "missed"):
        items = groups.get(kind, [])
        for j in range(0, len(items), 42):
            i += 1
            pp = parts_dir / f"in_{i:02d}.json"
            pp.write_text(json.dumps(items[j:j + 42], indent=1,
                                     ensure_ascii=False))
            print(f"  {pp.name}: {len(items[j:j+42])} {kind}")


def merge_commentary(year, out_dir):
    """Assemble subagent part files into commentary_{year}.json, validating
    full coverage against the skeleton and privacy-scanning every note
    against its case's roster (notes are written from redacted inputs, but
    the scan runs anyway — leaks must be loud)."""
    skel = json.loads((out_dir / f"commentary_skeleton_{year}.json").read_text())
    want = {b["id"] for b in skel}
    merged = {}
    parts = sorted((out_dir / "commentary_parts").glob("out_*.json"))
    for p in parts:
        d = json.loads(p.read_text())
        dup = set(d) & set(merged)
        if dup:
            sys.exit(f"duplicate ids across parts ({p.name}): {sorted(dup)[:5]}")
        merged.update(d)
    missing, extra = want - set(merged), set(merged) - want
    if missing or extra:
        sys.exit(f"coverage mismatch: {len(missing)} missing "
                 f"(e.g. {sorted(missing)[:5]}), {len(extra)} extra")
    empty = [k for k, v in merged.items() if not (v or "").strip()]
    if empty:
        sys.exit(f"{len(empty)} empty notes, e.g. {empty[:5]}")
    sys.path.insert(0, str(Path(__file__).parent))
    from render_summary import alj_surname, load_decisions
    recs = load_decisions(year)
    skel_by_id = {b["id"]: b for b in skel}
    leaks = []
    for k, note in merged.items():
        case = skel_by_id[k].get("case_no") or k.split(":")[0]
        rec = recs.get(case)
        if not rec:
            continue
        ident = rec.get("identity") or {}
        alj = alj_surname((ident.get("alj") or {}).get("raw") or "").lower()
        dist = ((ident.get("district") or {}).get("raw") or "").lower()
        import re
        for r in (rec.get("outcome") or {}).get("roster") or []:
            nm = (r.get("name") or "").strip()
            if not nm:
                continue
            last = nm.split(",")[0].strip() if "," in nm else nm.split()[-1]
            pats = [nm]
            if len(last) >= 3 and last.lower() != alj and last.lower() not in dist:
                pats.append(last)
            if any(re.search(rf"\b{re.escape(p)}\b", note, re.I) for p in pats):
                leaks.append((k, nm))
                break
    if leaks:
        sys.exit(f"!! PRIVACY: {len(leaks)} roster-name leak(s) in notes: "
                 f"{leaks[:5]}")
    p = out_dir / f"commentary_{year}.json"
    p.write_text(json.dumps(merged, indent=1, ensure_ascii=False))
    print(f"wrote {p} ({len(merged)} notes from {len(parts)} parts; "
          f"coverage complete, privacy scan clean)")


def esc(s):
    return html.escape(s or "")


PREAMBLE = """
<p>This is the <b>annotated verification edition</b> of the {year} machine-generated
Layoff Decision Summaries. The companion document (the clean Word report) is the
production artifact: it is assembled automatically from the structured decision
corpus and regenerates on demand for any year. <b>This edition adds an explanation
layer on top of it</b> — color coding and per-entry notes comparing the system's
output against the expert-written {year} volume — so the reader can judge the
quality of the extraction holding by holding. The comparison layer is only
possible for years where a human volume exists; it is evaluation scaffolding,
not part of the production output.</p>
<p><b>How to read it:</b> each entry's classification (confirmed / different
filing / addition / missed) is computed deterministically by the evaluation
pipeline that aligns system output to the human volume; it is not an AI
judgment. For matched entries the human volume's own paragraph is quoted
beneath the system's version, so every comparison can be verified in place.
The short notes beneath entries are AI-written explanations of the
already-computed classification.</p>
<p><b>Two scope notes.</b> First, the human volumes are editorial, not
exhaustive: their authors selected noteworthy holdings and omitted routine
ones. Entries marked as additions are holdings genuinely present in the
underlying decisions (every one is anchored to verbatim decision text) that
the volume's editors did not catalogue — expected, and often useful, rather
than error. Second, the system deliberately does not produce the volumes'
occasional editorial commentary (<i>e.g.</i>, "the ALJ appears to have
conflated competency with special training"); that analytic layer remains
human work product, or a separately designed — and separately verified —
generation pass.</p>
<p>Respondents are identified by pseudonymous references (R1, R2, …) keyed to
each decision's roster, matching the de-identification convention of the
human volumes. {coverage}</p>
"""


def render_html(year, entries, align, missed, excluded_cite, excluded_default,
                not_in_set, comments, out_dir):
    counts = {"confirmed": 0, "divergent": 0, "new": 0}
    for sec in entries["sections"]:
        for e in sec["entries"]:
            counts[e["klass"]] += 1
    coverage = (
        f"The dataset currently holds {entries['n_decisions']} {year} decisions "
        f"({entries['n_holdings']} catalogued holdings). "
        f"{len(not_in_set)} of the human volume's {len(align['gold'])} entries cite "
        f"decisions not yet in this dataset; those are a download-coverage gap, not "
        f"an extraction result, and are excluded from the comparison.")

    css = """
    body { font-family: Georgia, 'Times New Roman', serif; max-width: 60rem;
           margin: 2rem auto; padding: 0 1.5rem; color: #1a1a1a; line-height: 1.5; }
    h1 { text-align: center; font-size: 1.5rem; }
    .subtitle { text-align: center; font-style: italic; color: #555; margin-bottom: 2rem; }
    .preamble { background: #fafafa; border: 1px solid #ddd; padding: .25rem 1.25rem;
                font-size: .95rem; }
    h2 { margin-top: 2.2rem; border-bottom: 2px solid #333; padding-bottom: .2rem; }
    .legend { display: flex; flex-wrap: wrap; gap: .6rem; margin: 1.2rem 0; }
    .legend span { padding: .25rem .7rem; border-radius: 3px; font-size: .85rem;
                   border-left: 5px solid; background: #fff; }
    .entry { border-left: 5px solid; padding: .6rem 1rem; margin: .9rem 0;
             border-radius: 0 4px 4px 0; }
    .badge { font-size: .72rem; font-weight: bold; letter-spacing: .05em;
             text-transform: uppercase; opacity: .85; }
    .etext { margin: .35rem 0; }
    .gold { border-left: 3px solid #999; background: rgba(255,255,255,.6);
            margin: .5rem 0 .4rem .8rem; padding: .4rem .8rem; font-size: .92rem; }
    .gold .glabel { font-size: .72rem; font-weight: bold; text-transform: uppercase;
                    color: #666; letter-spacing: .05em; }
    .note { font-style: italic; font-size: .9rem; color: #444; margin-top: .35rem; }
    .note .nlabel { font-style: normal; font-size: .72rem; font-weight: bold;
                    text-transform: uppercase; color: #888; letter-spacing: .05em; }
    details { margin: 1rem 0; } summary { cursor: pointer; font-weight: bold; }
    .excl { color: #555; font-size: .92rem; margin: .5rem 0 .5rem 1rem; }
    table.stats { border-collapse: collapse; margin: 1rem auto; }
    table.stats td, table.stats th { border: 1px solid #bbb; padding: .35rem .8rem;
                                     font-size: .9rem; }
    @media print { .entry { break-inside: avoid; } }
    """
    out = [f"<!doctype html><html><head><meta charset='utf-8'>"
           f"<title>{year} Layoff Decision Summaries — annotated verification "
           f"edition</title><style>{css}</style></head><body>"]
    w = out.append
    w(f"<h1>SUMMARY OF {esc(year)} PROPOSED ALJ LAYOFF DECISIONS</h1>")
    w(f"<div class='subtitle'>Annotated verification edition — generated "
      f"{datetime.date.today().isoformat()}, compared against the expert-written "
      f"{esc(year)} volume</div>")
    w("<div class='preamble'>" + PREAMBLE.format(year=esc(year),
                                                 coverage=esc(coverage)) + "</div>")

    n_scored = sum(1 for g in align["gold"]
                   if g["status"] in ("recovered", "category_divergent", "missed"))
    n_rec = sum(1 for g in align["gold"] if g["status"] == "recovered")
    n_div = sum(1 for g in align["gold"] if g["status"] == "category_divergent")
    w("<table class='stats'><tr><th></th><th>count</th></tr>")
    for label, v in [
            ("Human-volume entries compared (decisions we hold, scoreable)", n_scored),
            ("&nbsp;&nbsp;recovered by the system", n_rec),
            ("&nbsp;&nbsp;recovered under a different section", n_div),
            ("&nbsp;&nbsp;missed (appendix below)", len(missed)),
            ("System entries in this report", entries["n_holdings"]),
            ("&nbsp;&nbsp;confirmed against the volume", counts["confirmed"]),
            ("&nbsp;&nbsp;matched, different filing", counts["divergent"]),
            ("&nbsp;&nbsp;additions not in the volume", counts["new"]),
    ]:
        w(f"<tr><td>{label}</td><td style='text-align:right'>{v}</td></tr>")
    w("</table>")

    w("<div class='legend'>")
    for k, (label, color, bg) in CLASS_META.items():
        w(f"<span style='border-color:{color};background:{bg}'>{label}</span>")
    w("</div>")

    for sec in entries["sections"]:
        w(f"<h2>{esc(sec['roman'])}. {esc(sec['title'])}</h2>")
        for n, e in enumerate(sec["entries"], 1):
            label, color, bg = CLASS_META[e["klass"]]
            w(f"<div class='entry' style='border-color:{color};background:{bg}'>")
            w(f"<div class='badge' style='color:{color}'>{label}</div>")
            w(f"<div class='etext'>{n}. {esc(e['text'])}</div>")
            for gt in e["gold_texts"]:
                w(f"<div class='gold'><div class='glabel'>Human volume version"
                  f"</div>{esc(gt)}</div>")
            note = comments.get(e["block_id"]) or DEFAULT_COMMENT[e["klass"]]
            w(f"<div class='note'><span class='nlabel'>Note (AI-generated)</span> "
              f"{esc(note)}</div>")
            w("</div>")

    w("<h2>APPENDIX A — HUMAN-VOLUME ENTRIES NOT RECOVERED</h2>")
    w(f"<p>The {len(missed)} entries below appear in the human {esc(year)} volume, "
      f"cite decisions present in this dataset, and were not recovered by the "
      f"extraction. Each carries an explanation. (Entries whose decisions are not "
      f"in the dataset are a coverage gap, not extraction misses, and are counted "
      f"in the preamble instead.)</p>")
    for g in missed:
        label, color, bg = CLASS_META["missed"]
        cite = g["cite"]
        w(f"<div class='entry' style='border-color:{color};background:{bg}'>")
        w(f"<div class='badge' style='color:{color}'>{label} — "
          f"{esc(cite.get('district') or '')} ({esc(cite.get('alj') or '')}), "
          f"OAH {esc(g['case_no'] or '')}</div>")
        w(f"<div class='etext'>{esc(g['text'])}</div>")
        note = comments.get(f"gold:{g['gold_idx']}") or DEFAULT_COMMENT["missed"]
        w(f"<div class='note'><span class='nlabel'>Note (AI-generated)</span> "
          f"{esc(note)}</div>")
        w("</div>")

    w("<h2>APPENDIX B — HUMAN-VOLUME ENTRIES OUTSIDE THE COMPARISON</h2>")
    w(f"<details><summary>Editorial citation / cross-reference lines "
      f"({len(excluded_cite)})</summary>"
      f"<p class='excl'>Short cross-reference lines in the volume that are not "
      f"case-specific holdings; not an extraction target.</p>")
    for g in excluded_cite:
        w(f"<div class='excl'>• {esc(g['text'])}</div>")
    w("</details>")
    w(f"<details><summary>Rule statements on default/uncontested matters "
      f"({len(excluded_default)})</summary>"
      f"<p class='excl'>Black-letter recitations the volume attaches to "
      f"default or stipulated matters; the schema deliberately records those "
      f"dispositions structurally rather than as holdings.</p>")
    for g in excluded_default:
        w(f"<div class='excl'>• {esc(g['text'])}</div>")
    w("</details>")
    w(f"<details><summary>Entries citing decisions not in this dataset "
      f"({len(not_in_set)})</summary>"
      f"<p class='excl'>Download-coverage gap in the current dataset slice, "
      f"not an extraction result.</p>")
    for g in not_in_set:
        cite = g["cite"]
        w(f"<div class='excl'>• {esc(cite.get('district') or '?')} "
          f"({esc(cite.get('alj') or '?')}) — {esc(g['text'][:160])}…</div>")
    w("</details>")
    w("</body></html>")

    p = out_dir / f"annotated_summary_{year}.html"
    p.write_text("\n".join(out))
    n_custom = sum(1 for sec in entries["sections"] for e in sec["entries"]
                   if e["block_id"] in comments)
    print(f"wrote {p}")
    print(f"commentary: {n_custom}/{entries['n_holdings']} entries custom, "
          f"{sum(1 for g in missed if f'gold:{g['gold_idx']}' in comments)}"
          f"/{len(missed)} missed")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--year", required=True)
    ap.add_argument("--skeleton", action="store_true",
                    help="write the commentary work-list + batch files and exit")
    ap.add_argument("--merge", action="store_true",
                    help="merge subagent out_NN.json parts into commentary_{year}.json")
    args = ap.parse_args()
    entries, align = load(args.year)
    missed, excluded_cite, excluded_default, not_in_set = classify(entries, align)
    out_dir = ROOT / "output" / "reports" / args.year
    if args.skeleton:
        write_skeleton(args.year, entries, missed, out_dir)
        return 0
    if args.merge:
        merge_commentary(args.year, out_dir)
        return 0
    comments_p = out_dir / f"commentary_{args.year}.json"
    comments = json.loads(comments_p.read_text()) if comments_p.exists() else {}
    if not comments:
        print("note: no commentary file; class-default notes will be used")
    render_html(args.year, entries, align, missed, excluded_cite,
                excluded_default, not_in_set, comments, out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
