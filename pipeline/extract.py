#!/usr/bin/env python3
"""Stage 2: multi-pass, multi-model decision extraction.

For each decision (best-available text per OAH case number), runs three
extraction passes (identity, dispositions, holdings) across an ensemble of
models, then merges into one schema-valid DecisionRecord per case:

  output/corpus/raw/{case}__{pass}__{model}.json   raw model output (cache)
  output/corpus/decisions/{case}.json              merged, validated record
  output/corpus/failed/{case}.json                 records failing validation

Reconciliation policy: the PRIMARY model (first in --models) supplies values;
the secondary fills primary nulls (flagged) and every material divergence is
recorded in provenance.reconciliation.disagreements. Quote anchors are
verified verbatim against the text; unverified anchors are flagged, not
deleted. Everything is resumable: delete a raw file to re-run one
(case, pass, model); delete a decision file to re-merge.

Usage:
  extract.py run   --year 2009 [--limit N] [--models a,b] [--merge-only]
  extract.py status --year 2009
"""

import argparse
import copy
import json
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator

from bakeoff import (call_ollama, holdings_pass_schema, load_record_schema,
                     parse_json_loose, strip_unsupported)
from inventory import KIND_RANK

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "output" / "corpus" / "raw"
DECISIONS = ROOT / "output" / "corpus" / "decisions"
FAILED = ROOT / "output" / "corpus" / "failed"
CACHE = ROOT / "output" / "cache" / "text"

MODELS = ["qwen3.6:27b", "gemma4:31b"]   # primary first
PASS_PROMPTS = {"identity": "identity_v1", "dispositions": "dispositions_v1",
                "holdings": "holdings_v2"}
SCHEMA_VERSION = "0.1.0"

# Per-pass generation budget overrides (else call_ollama defaults 12288/32768).
# Dispositions enumerates one entry per respondent; mass-layoff appendices
# (200-400 names) overflow the default num_predict mid-array and truncate to
# invalid JSON, so this pass needs a much larger token budget and context.
PASS_BUDGET = {"dispositions": {"num_predict": 24576, "num_ctx": 49152}}

# ------------------------------------------------------------ pass schemas


def identity_pass_schema() -> dict:
    rec = load_record_schema()
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object", "additionalProperties": False,
        "required": ["identity"],
        "properties": {k: copy.deepcopy(rec["properties"][k])
                       for k in ("identity", "procedure", "board_action")},
        "$defs": {k: copy.deepcopy(rec["$defs"][k])
                  for k in ("normalized_entity", "counsel_entry",
                            "resolution_artifact", "quote_anchor",
                            "quote_anchor_or_null")},
    }


def dispositions_pass_schema() -> dict:
    rec = load_record_schema()
    qa = copy.deepcopy(rec["$defs"]["quote_anchor"])
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object", "additionalProperties": False,
        "required": ["overall", "roster", "roster_completeness",
                     "respondent_dispositions"],
        "properties": {
            "overall": {"enum": ["sustained", "sustained_in_part",
                                 "not_sustained", "unknown"]},
            "roster": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "required": ["name", "source"],
                "properties": {"name": {"type": "string"},
                               "source": {"enum": ["appendix", "body",
                                                   "exhibit", "unknown"]}}}},
            "roster_completeness": {"enum": ["complete", "partial", "unknown"]},
            "respondent_dispositions": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "required": ["name", "disposition"],
                "properties": {
                    "name": {"type": "string"},
                    "representation_kind": {"enum": ["firm", "self", "none",
                                                     "unknown"]},
                    "counsel_firm": {"type": ["string", "null"]},
                    "disposition": {"enum": [
                        "terminated", "partially_terminated", "notice_rescinded",
                        "accusation_dismissed", "released_temporary",
                        "resigned_or_withdrew", "other", "unknown"]},
                    "detail": {"type": ["string", "null"]},
                    "reason": {"type": ["string", "null"]},
                    "quote": {"oneOf": [{"$ref": "#/$defs/quote_anchor"},
                                        {"type": "null"}]}}}},
        },
        "$defs": {"quote_anchor": qa},
    }


PASS_SCHEMAS = {
    "identity": identity_pass_schema,
    "dispositions": dispositions_pass_schema,
    "holdings": lambda: holdings_pass_schema(strict_quotes=True),
}

# ------------------------------------------------------------------ corpus


def select_cases(year: str, limit: int | None) -> list[dict]:
    """Best-available text per unique case number filed in `year`."""
    manifest = json.loads(
        (ROOT / "output" / "inventory" / "manifest.json").read_text())
    groups: dict[str, list[dict]] = {}
    for r in manifest["files"]:
        if r["doc_class"] != "decision" or r["extract_status"] != "ok":
            continue
        for c in r["case_numbers_filename"]:
            if c.startswith(year):
                groups.setdefault(c, []).append(r)
    cases = []
    for case_no, members in sorted(groups.items()):
        best = min(members, key=lambda r: (KIND_RANK.get(r["kind"], 9),
                                           -r["text_chars"]))
        if best["kind"] in ("pdf_scanned", "error") or best["text_chars"] < 2000:
            continue  # OCR residue: out of scope for this stage
        cases.append({"case_no": case_no, "best": best, "all": members})
    return cases[:limit] if limit else cases


# ------------------------------------------------------------------ runner


def run_extractions(cases, models):
    RAW.mkdir(parents=True, exist_ok=True)
    prompts = {p: (ROOT / "pipeline" / "prompts" / f"{v}.txt").read_text()
               for p, v in PASS_PROMPTS.items()}
    schemas = {p: strip_unsupported(fn()) for p, fn in PASS_SCHEMAS.items()}
    todo = [(c, p, m) for c in cases for p in PASS_PROMPTS for m in models
            if not (RAW / f"{c['case_no']}__{p}__{m.replace(':', '_')}.json").exists()]
    print(f"{len(todo)} extractions to run "
          f"({len(cases)} cases x {len(PASS_PROMPTS)} passes x {len(models)} models, "
          f"minus cached)", flush=True)
    for i, (c, pass_name, model) in enumerate(todo, 1):
        out = RAW / f"{c['case_no']}__{pass_name}__{model.replace(':', '_')}.json"
        text = (CACHE / f"{c['best']['sha1']}.txt").read_text(errors="replace")
        t0 = time.time()
        rec = {"case_no": c["case_no"], "pass": pass_name, "model": model,
               "prompt_version": PASS_PROMPTS[pass_name],
               "source_sha1": c["best"]["sha1"]}
        try:
            resp = call_ollama(model, prompts[pass_name],
                               "DECISION TEXT:\n\n" + text, schemas[pass_name],
                               **PASS_BUDGET.get(pass_name, {}))
            rec["duration_s"] = round(time.time() - t0, 1)
            try:
                rec["parsed"] = parse_json_loose(resp.get("response", ""))
            except Exception as e:
                rec["error"] = f"parse: {e}"
                rec["raw"] = resp.get("response", "")[:20000]
        except Exception as e:
            rec["duration_s"] = round(time.time() - t0, 1)
            rec["error"] = f"run: {e}"
        out.write_text(json.dumps(rec))
        status = "ok" if "parsed" in rec else "ERROR"
        print(f"[{i}/{len(todo)}] {c['case_no']} {pass_name} {model} "
              f"{rec['duration_s']}s {status}", flush=True)


# ------------------------------------------------------------------- merge

WORD = re.compile(r"[A-Za-z][\w'’\-]+")


def surname(name: str) -> str:
    """Last capitalized token; handles 'Last, First' order too."""
    if "," in name:
        return name.split(",")[0].strip().lower()
    toks = WORD.findall(name)
    return toks[-1].lower() if toks else name.lower()


def walk_disagreements(primary, secondary, path, out):
    """Field-wise scalar comparison; fills primary nulls from secondary."""
    if isinstance(primary, dict) and isinstance(secondary, dict):
        for k in primary:
            if k in secondary:
                primary[k] = walk_disagreements(primary[k], secondary[k],
                                                f"{path}.{k}", out)
        return primary
    if isinstance(primary, (str, int, float, bool)) or primary is None:
        if primary is None and secondary not in (None, [], {}):
            out.append({"field_path": path, "values": [None, secondary],
                        "resolution": "filled_from_secondary"})
            return secondary
        if (secondary is not None and primary != secondary
                and not isinstance(secondary, (list, dict))):
            out.append({"field_path": path, "values": [primary, secondary],
                        "resolution": "kept_primary"})
    return primary


def collect_quotes(obj):
    out = []
    if isinstance(obj, dict):
        q = obj.get("quote")
        if isinstance(q, str) and "section" in obj:
            out.append(q)
        elif isinstance(q, dict) and isinstance(q.get("quote"), str):
            out.append(q["quote"])
        for k, v in obj.items():
            if k != "quote":
                out.extend(collect_quotes(v))
    elif isinstance(obj, list):
        for v in obj:
            out.extend(collect_quotes(v))
    return out


def merge_case(case, models, validator, run_at):
    primary_m, secondary_m = models[0], models[1] if len(models) > 1 else None

    def load(pass_name, model):
        f = RAW / f"{case['case_no']}__{pass_name}__{model.replace(':', '_')}.json"
        if not f.exists():
            return None
        d = json.loads(f.read_text())
        return d.get("parsed")

    parts = {p: load(p, primary_m) for p in PASS_PROMPTS}
    sec = ({p: load(p, secondary_m) for p in PASS_PROMPTS}
           if secondary_m else {p: None for p in PASS_PROMPTS})

    # Primary-pass rescue: if the primary model failed to produce parseable
    # output for a pass (e.g. num_predict overflow on a mega-roster, or a
    # degenerate quote-anchor repetition loop), fall back to the secondary's
    # output for that pass and flag it. Consume the secondary so the later
    # reconciliation doesn't compare the borrowed value against itself.
    disagreements = []
    for p in PASS_PROMPTS:
        if parts[p] is None and sec.get(p) is not None:
            parts[p] = sec[p]
            sec[p] = None
            disagreements.append({"field_path": f"pass.{p}",
                                  "values": [primary_m, secondary_m],
                                  "resolution": "primary_pass_failed_used_secondary"})
    if any(v is None for v in parts.values()):
        return None, "missing primary pass output"

    ident = parts["identity"]
    if sec["identity"]:
        ident = walk_disagreements(copy.deepcopy(ident), sec["identity"],
                                   "", disagreements)

    # normalize school-year shorthand ("2009-10" -> "2009-2010"); null
    # anything that still won't fit the schema pattern rather than failing
    sy = (ident.get("identity") or {}).get("school_year_affected")
    if isinstance(sy, str):
        m = re.fullmatch(r"(\d{4})\s*[-–/]\s*(\d{2})", sy.strip())
        if m:
            sy = f"{m.group(1)}-{m.group(1)[:2]}{m.group(2)}"
        sy = sy.replace(" ", "")
        if not re.fullmatch(r"\d{4}-\d{4}", sy):
            disagreements.append({"field_path": "identity.school_year_affected",
                                  "values": [sy],
                                  "resolution": "unparseable_nulled"})
            sy = None
        ident["identity"]["school_year_affected"] = sy

    # ---- outcome: names -> refs
    disp = parts["dispositions"]
    roster_names = [e["name"] for e in disp.get("roster", [])]
    refs = {surname(n): f"R{i}" for i, n in enumerate(roster_names, 1)}
    roster = [{"ref": f"R{i}", "name": n,
               "source": disp["roster"][i - 1].get("source", "unknown")}
              for i, n in enumerate(roster_names, 1)]
    dispositions = []
    for d in disp.get("respondent_dispositions", []):
        ref = refs.get(surname(d.get("name", "")))
        if not ref:
            disagreements.append({"field_path": "outcome.respondent_dispositions",
                                  "values": [d.get("name")],
                                  "resolution": "disposition_name_not_in_roster"})
            continue
        dispositions.append({
            "ref": ref,
            "representation": {"kind": d.get("representation_kind", "unknown"),
                               "counsel_index": None},
            "disposition": d["disposition"],
            "detail": d.get("detail"), "reason": d.get("reason"),
            "quote": d.get("quote")})
    if sec["dispositions"]:
        s = sec["dispositions"]
        if len(s.get("roster", [])) != len(roster_names):
            disagreements.append({
                "field_path": "outcome.roster",
                "values": [len(roster_names), len(s.get("roster", []))],
                "resolution": "kept_primary_roster"})
        sec_by_surname = {surname(d.get("name", "")): d["disposition"]
                          for d in s.get("respondent_dispositions", [])}
        for d in disp.get("respondent_dispositions", []):
            sn = surname(d.get("name", ""))
            if sn in sec_by_surname and sec_by_surname[sn] != d["disposition"]:
                disagreements.append({
                    "field_path": f"outcome.dispositions[{sn}]",
                    "values": [d["disposition"], sec_by_surname[sn]],
                    "resolution": "kept_primary"})

    # ---- holdings: names -> refs; cross-model agreement
    from bakeoff import match_holdings
    holdings = []
    for h in parts["holdings"].get("holdings", []):
        h = copy.deepcopy(h)
        names = (h.get("ruling") or {}).pop("affected_respondents_names", [])
        h["ruling"]["affected_respondents"] = [
            refs[surname(n)] for n in names if surname(n) in refs]
        holdings.append(h)
    if sec["holdings"]:
        ph, sh = parts["holdings"].get("holdings", []), sec["holdings"].get("holdings", [])
        matches = match_holdings(ph, sh)
        matched_p = {i for _, i, _ in matches}
        matched_s = {j for _, _, j in matches}
        for i, h in enumerate(ph):
            if i not in matched_p:
                disagreements.append({
                    "field_path": f"holdings[{i}]",
                    "values": [h.get("issue", {}).get("statement")],
                    "resolution": "primary_only_holding"})
        for j, h in enumerate(sh):
            if j not in matched_s:
                disagreements.append({
                    "field_path": "holdings",
                    "values": [h.get("issue", {}).get("statement")],
                    "resolution": "secondary_only_holding_omitted"})

    text = (CACHE / f"{case['best']['sha1']}.txt").read_text(errors="replace")
    record = {
        "schema_version": SCHEMA_VERSION,
        "identity": ident.get("identity"),
        "procedure": ident.get("procedure"),
        "board_action": ident.get("board_action"),
        "outcome": {"overall": disp.get("overall", "unknown"),
                    "roster": roster,
                    "roster_completeness": disp.get("roster_completeness",
                                                    "unknown"),
                    "respondent_dispositions": dispositions},
        "holdings": holdings,
        "related_proceedings": parts["holdings"].get("related_proceedings", []),
        "full_text": text,
        "provenance": {
            "source_files": [{"path": m["path"], "sha1": m["sha1"],
                              "kind": m["kind"]} for m in case["all"]],
            "text_sha1": case["best"]["sha1"],
            "passes": [{"name": p, "model": m,
                        "prompt_version": PASS_PROMPTS[p],
                        "run_at": run_at, "notes": None}
                       for p in PASS_PROMPTS
                       for m in models],
            "reconciliation": {"status": "disputed" if disagreements
                               else "reconciled",
                               "disagreements": disagreements},
        },
    }
    # drop keys the model pass legitimately omitted entirely
    record = {k: v for k, v in record.items() if v is not None}

    # ---- anchor verification (flag, never delete)
    unverified = [q for q in collect_quotes(record.get("holdings", []))
                  + collect_quotes(record.get("board_action", {}))
                  + collect_quotes(record.get("outcome", {}))
                  if q not in text]
    for q in unverified:
        disagreements.append({"field_path": "anchor", "values": [q[:120]],
                              "resolution": "ANCHOR_UNVERIFIED"})
    if unverified:
        record["provenance"]["reconciliation"]["status"] = "disputed"

    errs = [f"{list(e.absolute_path)}: {e.message[:110]}"
            for e in validator.iter_errors(record)]
    return record, errs


def merge_all(cases, models):
    DECISIONS.mkdir(parents=True, exist_ok=True)
    FAILED.mkdir(parents=True, exist_ok=True)
    validator = Draft202012Validator(load_record_schema())
    run_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    stats = Counter()
    for case in cases:
        record, errs = merge_case(case, models, validator, run_at)
        if record is None:
            stats["missing_passes"] += 1
            continue
        if errs:
            (FAILED / f"{case['case_no']}.json").write_text(json.dumps(
                {"record": record, "validation_errors": errs}, indent=2))
            stats["failed_validation"] += 1
        else:
            (DECISIONS / f"{case['case_no']}.json").write_text(
                json.dumps(record, indent=2))
            stats["ok"] += 1
            n_dis = len(record["provenance"]["reconciliation"]["disagreements"])
            stats["disputed" if n_dis else "clean"] += 1
    print(dict(stats))


# -------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("cmd", choices=["run", "status"])
    ap.add_argument("--year", default="2009")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--models", default=",".join(MODELS))
    ap.add_argument("--merge-only", action="store_true")
    args = ap.parse_args()
    models = args.models.split(",")
    cases = select_cases(args.year, args.limit)
    if args.cmd == "status":
        done = sum(1 for c in cases for p in PASS_PROMPTS for m in models
                   if (RAW / f"{c['case_no']}__{p}__{m.replace(':', '_')}.json").exists())
        print(f"{len(cases)} cases; raw outputs {done}/{len(cases) * len(PASS_PROMPTS) * len(models)}; "
              f"merged {sum(1 for c in cases if (DECISIONS / (c['case_no'] + '.json')).exists())}")
        return 0
    if not args.merge_only:
        run_extractions(cases, models)
    merge_all(cases, models)
    return 0


if __name__ == "__main__":
    sys.exit(main())
