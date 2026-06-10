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
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator

from bakeoff import (call_ollama, holdings_pass_schema, load_record_schema,
                     parse_json_loose, strip_unsupported)
from inventory import KIND_RANK, cached_text
from roster import RefResolver, fuzzy_same_person, name_key, parse_roster

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "output" / "corpus" / "raw"
DECISIONS = ROOT / "output" / "corpus" / "decisions"
FAILED = ROOT / "output" / "corpus" / "failed"
CACHE = ROOT / "output" / "cache" / "text"

MODELS = ["qwen3.6:27b", "gemma4:31b"]   # primary first
PASS_PROMPTS = {"identity": "identity_v1", "dispositions": "dispositions_v2",
                "holdings": "holdings_v3"}
# When the deterministic appendix parser (roster.py) recovers the name table,
# the dispositions pass switches to the body-only prompt: roster transcription
# is mechanical work the LLM should never do (REFINEMENTS #6), and the output
# collapses from O(roster) back to O(litigated-respondents). The per-case
# prompt actually used is recorded in the raw file and provenance.
DISPOSITIONS_BODY_ONLY = "dispositions_v2_body"
SCHEMA_VERSION = "0.2.0"

# Per-pass generation budget overrides (else call_ollama defaults 12288/32768).
# Dispositions enumerates one entry per respondent; mass-layoff appendices
# (200-400 names) overflow the default num_predict mid-array and truncate to
# invalid JSON, so this pass needs a much larger token budget and context.
PASS_BUDGET = {"dispositions": {"num_predict": 24576, "num_ctx": 49152}}

# Dynamic context sizing (REFINEMENTS #1): the prompt must never be silently
# truncated. Both production models are 128k-class; KV cost of a larger window
# is transient and within GTT headroom. CHARS_PER_TOKEN=3 is deliberately
# conservative (2009 measured ~3.8) so the estimate errs toward a roomier
# window, and ollama's actual prompt_eval_count is recorded for calibration.
DEFAULT_NUM_PREDICT = 12288
DEFAULT_NUM_CTX = 32768
MAX_NUM_CTX = 131072
CHARS_PER_TOKEN = 3


def plan_budget(pass_name: str, prompt_chars: int) -> dict:
    """num_predict/num_ctx for one call, sized so est_input + num_predict
    fits with margin. Returns {"num_predict", "num_ctx", "est_input_tokens",
    "input_may_truncate"}."""
    base = PASS_BUDGET.get(pass_name, {})
    num_predict = base.get("num_predict", DEFAULT_NUM_PREDICT)
    num_ctx = base.get("num_ctx", DEFAULT_NUM_CTX)
    est_input = prompt_chars // CHARS_PER_TOKEN + 512
    need = est_input + num_predict + 512
    if need > num_ctx:
        num_ctx = min(MAX_NUM_CTX, -(-need // 4096) * 4096)
    return {"num_predict": num_predict, "num_ctx": num_ctx,
            "est_input_tokens": est_input,
            "input_may_truncate": need > MAX_NUM_CTX}

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
                     "respondent_dispositions", "general_order"],
        "properties": {
            "overall": {"enum": ["sustained", "sustained_in_part",
                                 "not_sustained", "unknown"]},
            # The blanket order covering respondents the decision never
            # discusses individually ("notice may be given to all [other]
            # respondents..."). Lets the dispositions array stay
            # O(litigated-respondents) without losing the rest of the roster.
            "general_order": {"oneOf": [{"type": "null"}, {
                "type": "object", "additionalProperties": False,
                "required": ["disposition", "applies_to"],
                "properties": {
                    "disposition": {"enum": [
                        "terminated", "partially_terminated",
                        "notice_rescinded", "accusation_dismissed",
                        "released_temporary", "other", "unknown"]},
                    "applies_to": {"enum": ["all_rostered",
                                            "all_other_rostered", "unknown"]},
                    "quote": {"oneOf": [{"$ref": "#/$defs/quote_anchor"},
                                        {"type": "null"}]}}}]},
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


def prompt_version_for(pass_name: str, text: str) -> str:
    """Per-case prompt selection: the body-only dispositions prompt is used
    exactly when the deterministic parser recovers the appendix table (same
    check re-runs at merge, so runner and merge can never disagree)."""
    if pass_name == "dispositions" and parse_roster(text):
        return DISPOSITIONS_BODY_ONLY
    return PASS_PROMPTS[pass_name]


def run_extractions(cases, models):
    RAW.mkdir(parents=True, exist_ok=True)
    versions = set(PASS_PROMPTS.values()) | {DISPOSITIONS_BODY_ONLY}
    prompts = {v: (ROOT / "pipeline" / "prompts" / f"{v}.txt").read_text()
               for v in versions}
    schemas = {p: strip_unsupported(fn()) for p, fn in PASS_SCHEMAS.items()}
    todo = [(c, p, m) for c in cases for p in PASS_PROMPTS for m in models
            if not (RAW / f"{c['case_no']}__{p}__{m.replace(':', '_')}.json").exists()]
    print(f"{len(todo)} extractions to run "
          f"({len(cases)} cases x {len(PASS_PROMPTS)} passes x {len(models)} models, "
          f"minus cached)", flush=True)
    for i, (c, pass_name, model) in enumerate(todo, 1):
        out = RAW / f"{c['case_no']}__{pass_name}__{model.replace(':', '_')}.json"
        text = cached_text(c['best']['sha1'], CACHE)
        version = prompt_version_for(pass_name, text)
        user = "DECISION TEXT:\n\n" + text
        budget = plan_budget(pass_name, len(prompts[version]) + len(user))
        if budget["num_ctx"] > PASS_BUDGET.get(pass_name, {}).get(
                "num_ctx", DEFAULT_NUM_CTX):
            print(f"!! CONTEXT {c['case_no']} {pass_name}: est "
                  f"~{budget['est_input_tokens']} input tokens exceeds the "
                  f"default window; num_ctx={budget['num_ctx']}", flush=True)
        if budget["input_may_truncate"]:
            print(f"!! TRUNCATION RISK {c['case_no']} {pass_name}: input + "
                  f"output exceed the {MAX_NUM_CTX}-token model maximum -- "
                  f"the prompt WILL be cut; this case needs chunking",
                  flush=True)
        t0 = time.time()
        rec = {"case_no": c["case_no"], "pass": pass_name, "model": model,
               "prompt_version": version,
               "source_sha1": c["best"]["sha1"],
               "num_ctx": budget["num_ctx"],
               "num_predict": budget["num_predict"],
               "est_input_tokens": budget["est_input_tokens"]}
        if budget["input_may_truncate"]:
            rec["input_may_truncate"] = True
        try:
            resp = call_ollama(model, prompts[version], user,
                               schemas[pass_name],
                               num_predict=budget["num_predict"],
                               num_ctx=budget["num_ctx"])
            rec["duration_s"] = round(time.time() - t0, 1)
            # direct (not estimated) telemetry: input size for budget
            # calibration, output size + done_reason for overflow detection
            rec["prompt_eval_count"] = resp.get("prompt_eval_count")
            rec["eval_count"] = resp.get("eval_count")
            rec["done_reason"] = resp.get("done_reason")
            if resp.get("done_reason") == "length":
                rec["output_truncated"] = True
                print(f"!! OUTPUT TRUNCATED {c['case_no']} {pass_name} "
                      f"{model}: hit num_predict={budget['num_predict']}",
                      flush=True)
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
# Name->ref binding lives in roster.RefResolver. The previous surname-keyed
# dict was the root cause of the audit's dominant disposition defect: two
# roster entries sharing a surname collided, duplicating one ref and
# dropping the other in ~26% of 2009 cases (Gate 1).


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


# Anchor verification normalizes both sides before the containment test:
# PDF text carries curly quotes / en–em dashes / NBSP and columnar whitespace
# that the model silently regularizes when it echoes a quote back. A raw
# substring check fails ~80% of those as "unverified" though the span is
# verbatim-present. Normalization only ever *reclaims* a flagged anchor; it
# cannot make genuinely-absent text appear present (the words must still be
# contiguous in the normalized source). Table reconstructions the model emits
# as "A|B|C" are reclaimed by folding `|` to whitespace; "..."-elided spans are
# verified fragment-by-fragment.
_ANCHOR_TRANS = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-",
    "—": "-", "―": "-", "−": "-",
    "|": " ",
})


def anchor_norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").translate(_ANCHOR_TRANS)
    return re.sub(r"\s+", " ", s).strip().casefold()


def anchor_present(quote: str, text_norm: str) -> bool:
    """True if `quote` is verbatim-locatable in already-normalized `text_norm`.
    Splits "..."/"…"-elided quotes and requires every fragment to be present."""
    if not text_norm:
        return False
    if "..." in quote or "…" in quote:
        frags = [anchor_norm(p) for p in re.split(r"\.\.\.|…", quote)]
        frags = [f for f in frags if f]
        return bool(frags) and all(f in text_norm for f in frags)
    qn = anchor_norm(quote)
    return bool(qn) and qn in text_norm


# Merge-time integrity gates (flag, never delete -- the disposition/remedy
# structured layer is where the 2009 audit found defects, all deterministically
# detectable; the holdings/identity/anchor layers stay trustworthy, so a tripped
# gate marks the record disputed rather than quarantining otherwise-good data).
# Remedies that unambiguously mean the respondent KEEPS the job, so they
# contradict a "terminated" disposition. dismiss_accusation is excluded: it is
# frequently mislabeled on district wins (where termination is correct) and is
# too noisy to gate on. correct_seniority_list is excluded too: a corrected
# seniority date coexists with termination (Manhattan Beach 2009031189).
KEPT_REMEDIES = {"retain_employee", "rescind_notice"}


def roster_ref_violations(record):
    """Gate 1: respondent_dispositions refs must be a bijection with roster
    refs -- unless outcome.general_order covers the rest of the roster, in
    which case dispositions are a legitimate subset (duplicates and refs
    outside the roster stay violations). Returns (duplicate_refs,
    roster_without_disposition, disposition_without_roster); all empty ==
    clean."""
    out = record.get("outcome") or {}
    roster_refs = [r.get("ref") for r in (out.get("roster") or []) if r.get("ref")]
    disp_refs = [d.get("ref") for d in (out.get("respondent_dispositions") or [])
                 if d.get("ref")]
    if not roster_refs or not disp_refs:
        return [], [], []
    seen, dups = set(), set()
    for r in disp_refs:
        (dups if r in seen else seen).add(r)
    roster_set, disp_set = set(roster_refs), set(disp_refs)
    missing = [] if out.get("general_order") else sorted(roster_set - disp_set)
    return (sorted(dups), missing, sorted(disp_set - roster_set))


def remedy_disposition_contradictions(record):
    """Gate 2: a holding whose remedy keeps the respondent (retain/rescind) while
    EVERY affected respondent was terminated is an internal contradiction between
    the holdings and dispositions layers (e.g. Oroville 2009031010, where the
    disposition pass coded retained respondents as terminated). Requiring *all*
    affected refs terminated excludes the benign bumping case (senior retained,
    junior terminated -> mixed dispositions). Returns a list of
    (holding_index, affected_refs, kept_remedies)."""
    out = record.get("outcome") or {}
    disp_by_ref = {}
    for d in out.get("respondent_dispositions") or []:
        if d.get("ref"):
            disp_by_ref.setdefault(d["ref"], d.get("disposition"))
    hits = []
    for i, h in enumerate(record.get("holdings") or []):
        ru = h.get("ruling") or {}
        kept = set(ru.get("remedies") or []) & KEPT_REMEDIES
        aff = ru.get("affected_respondents") or []
        if kept and aff and all(disp_by_ref.get(ref) == "terminated" for ref in aff):
            hits.append((i, list(aff), sorted(kept)))
    return hits


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

    raws: dict[tuple, dict | None] = {}
    for p in PASS_PROMPTS:
        for m in models:
            f = RAW / f"{case['case_no']}__{p}__{m.replace(':', '_')}.json"
            raws[(p, m)] = json.loads(f.read_text()) if f.exists() else None

    def load(pass_name, model):
        d = raws.get((pass_name, model))
        return d.get("parsed") if d else None

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
        # every selected case must end up in decisions/ OR failed/ -- a
        # silently absent record is the one unacceptable outcome (2009030327
        # vanished this way when both models' dispositions passes overflowed)
        detail = {}
        for p in PASS_PROMPTS:
            if parts[p] is not None:
                detail[p] = "ok"
                continue
            detail[p] = {m: ("missing_raw" if raws[(p, m)] is None
                             else raws[(p, m)].get("error", "no parsed output")[:160])
                         for m in models}
        return None, {"missing_pass_output": detail}

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

    # ---- outcome: roster (deterministic-first), then names -> refs
    disp = parts["dispositions"]
    model_roster = disp.get("roster", []) or []
    text = cached_text(case['best']['sha1'], CACHE)
    det = parse_roster(text)
    entries: list[dict] = []
    if det:
        # appendix table parsed deterministically; model entries that don't
        # key-match are body-named respondents and are unioned in. Nothing
        # the model read is dropped.
        entries = [{"name": e["name"], "source": "appendix"}
                   for e in det["entries"]]
        det_keys = {name_key(e["name"]) for e in det["entries"]}
        det_names = [e["name"] for e in det["entries"]]
        added, fuzzy_dropped = [], []
        for e in model_roster:
            nm = e.get("name", "")
            if name_key(nm) in det_keys:
                continue
            # phantom guard: a model spelling within edit distance 1 of a
            # table entry (same given name) is the same person, not a 2nd one
            if any(fuzzy_same_person(nm, dn) for dn in det_names):
                fuzzy_dropped.append(nm)
                continue
            added.append(e)
        entries += [{"name": e.get("name", ""),
                     "source": e.get("source", "unknown")} for e in added]
        disagreements.append({
            "field_path": "outcome.roster",
            "values": [{"deterministic": len(det["entries"]),
                        "format": det["format"],
                        "model": len(model_roster),
                        "model_only_added": len(added),
                        "fuzzy_duplicates_dropped": fuzzy_dropped}],
            "resolution": "roster_deterministic_union"})
        annots = [[e["name"], e["annotation"]] for e in det["entries"]
                  if e.get("annotation")]
        if annots:
            disagreements.append({"field_path": "outcome.roster",
                                  "values": annots,
                                  "resolution": "roster_appendix_annotations"})
    else:
        entries = [{"name": e.get("name", ""),
                    "source": e.get("source", "unknown")} for e in model_roster]
    roster = [{"ref": f"R{i}", **e} for i, e in enumerate(entries, 1)]
    resolver = RefResolver([(r["ref"], r["name"]) for r in roster])

    def bind(name: str, where: str) -> str | None:
        ref, status = resolver.resolve(name or "")
        if ref is None:
            disagreements.append({
                "field_path": where, "values": [name],
                "resolution": ("disposition_name_ambiguous"
                               if status == "ambiguous"
                               else "disposition_name_not_in_roster")})
        elif status == "fuzzy":
            disagreements.append({"field_path": where,
                                  "values": [name, ref],
                                  "resolution": "name_fuzzy_matched"})
        return ref

    dispositions = []
    for d in disp.get("respondent_dispositions", []):
        ref = bind(d.get("name", ""), "outcome.respondent_dispositions")
        if not ref:
            continue
        dispositions.append({
            "ref": ref,
            "representation": {"kind": d.get("representation_kind", "unknown"),
                               "counsel_index": None},
            "disposition": d["disposition"],
            "detail": d.get("detail"), "reason": d.get("reason"),
            "quote": d.get("quote")})
    general_order = disp.get("general_order") or None
    if sec["dispositions"]:
        s = sec["dispositions"]
        if len(s.get("roster", [])) != len(model_roster):
            disagreements.append({
                "field_path": "outcome.roster",
                "values": [len(model_roster), len(s.get("roster", []))],
                "resolution": "kept_primary_roster"})
        prim_by_ref = {d["ref"]: d["disposition"] for d in dispositions}
        for d in s.get("respondent_dispositions", []):
            ref, _ = resolver.resolve(d.get("name", ""))
            if (ref in prim_by_ref and d.get("disposition")
                    and prim_by_ref[ref] != d["disposition"]):
                disagreements.append({
                    "field_path": f"outcome.dispositions[{ref}]",
                    "values": [prim_by_ref[ref], d["disposition"]],
                    "resolution": "kept_primary"})

    # ---- holdings: names -> refs; cross-model agreement
    from bakeoff import match_holdings
    holdings = []
    for hi, h in enumerate(parts["holdings"].get("holdings", [])):
        h = copy.deepcopy(h)
        names = (h.get("ruling") or {}).pop("affected_respondents_names", [])
        bound = [bind(n, f"holdings[{hi}].ruling.affected_respondents")
                 for n in names]
        h["ruling"]["affected_respondents"] = [r for r in bound if r]
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

    record = {
        "schema_version": SCHEMA_VERSION,
        "identity": ident.get("identity"),
        "procedure": ident.get("procedure"),
        "board_action": ident.get("board_action"),
        "outcome": {"overall": disp.get("overall", "unknown"),
                    "roster": roster,
                    "roster_completeness": disp.get("roster_completeness",
                                                    "unknown"),
                    "respondent_dispositions": dispositions,
                    "general_order": general_order},
        "holdings": holdings,
        "related_proceedings": parts["holdings"].get("related_proceedings", []),
        "full_text": text,
        "provenance": {
            "source_files": [{"path": m["path"], "sha1": m["sha1"],
                              "kind": m["kind"]} for m in case["all"]],
            "text_sha1": case["best"]["sha1"],
            "passes": [{"name": p, "model": m,
                        "prompt_version": (raws[(p, m)] or {}).get(
                            "prompt_version", PASS_PROMPTS[p]),
                        "run_at": run_at,
                        "notes": " ".join(k for k in ("output_truncated",
                                                      "input_may_truncate")
                                          if (raws[(p, m)] or {}).get(k)) or None}
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
    text_norm = anchor_norm(text)
    unverified = [q for q in collect_quotes(record.get("holdings", []))
                  + collect_quotes(record.get("board_action", {}))
                  + collect_quotes(record.get("outcome", {}))
                  if not anchor_present(q, text_norm)]
    for q in unverified:
        disagreements.append({"field_path": "anchor", "values": [q[:120]],
                              "resolution": "ANCHOR_UNVERIFIED"})
    if unverified:
        record["provenance"]["reconciliation"]["status"] = "disputed"

    # ---- integrity gates (flag, never delete)
    dups, missing, extra = roster_ref_violations(record)
    if dups or missing or extra:
        disagreements.append({
            "field_path": "outcome.respondent_dispositions",
            "values": [{"duplicate_refs": dups,
                        "roster_without_disposition": missing,
                        "disposition_without_roster": extra}],
            "resolution": "ROSTER_REF_BIJECTION_VIOLATION"})
        record["provenance"]["reconciliation"]["status"] = "disputed"
    for i, aff, kept in remedy_disposition_contradictions(record):
        disagreements.append({
            "field_path": f"holdings[{i}].ruling",
            "values": [{"affected_respondents": aff, "remedies": kept,
                        "disposition": "terminated"}],
            "resolution": "REMEDY_DISPOSITION_CONTRADICTION"})
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
            (FAILED / f"{case['case_no']}.json").write_text(json.dumps(
                {"case_no": case["case_no"], "error": errs}, indent=2))
            stats["missing_passes"] += 1
            continue
        if errs:
            (FAILED / f"{case['case_no']}.json").write_text(json.dumps(
                {"record": record, "validation_errors": errs}, indent=2))
            stats["failed_validation"] += 1
        else:
            (DECISIONS / f"{case['case_no']}.json").write_text(
                json.dumps(record, indent=2))
            (FAILED / f"{case['case_no']}.json").unlink(missing_ok=True)
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
        merged = [c["case_no"] for c in cases
                  if (DECISIONS / (c["case_no"] + ".json")).exists()]
        failed = [c["case_no"] for c in cases
                  if (FAILED / (c["case_no"] + ".json")).exists()]
        # completeness invariant: every selected case is merged or quarantined
        unaccounted = [c["case_no"] for c in cases
                       if c["case_no"] not in set(merged) | set(failed)]
        print(f"{len(cases)} cases; raw outputs {done}/{len(cases) * len(PASS_PROMPTS) * len(models)}; "
              f"merged {len(merged)}; quarantined {len(failed)}")
        if unaccounted:
            print(f"!! UNACCOUNTED ({len(unaccounted)}): neither merged nor "
                  f"quarantined: {unaccounted[:20]}")
        return 0 if not unaccounted else 1
    if not args.merge_only:
        run_extractions(cases, models)
    merge_all(cases, models)
    return 0


if __name__ == "__main__":
    sys.exit(main())
