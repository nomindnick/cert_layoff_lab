#!/usr/bin/env python3
"""Model bake-off: holdings-pass extraction vs hand-filled fixtures.

Runs the holdings extraction prompt across N ollama models x 3 fixture
decisions, in three modes:

  constrained    ollama structured outputs (decode constrained to pass schema)
  unconstrained  free generation, JSON parsed/repaired after the fact
  mega           whole-record single prompt (the anti-architecture baseline)

Each run is saved to output/bakeoff/runs/ and skipped on re-run (resumable;
delete a file to re-run it). `score` builds output/bakeoff/report.md.

Usage:
  bakeoff.py run [--models m1,m2] [--cases c1,c2] [--mode constrained] [--force]
  bakeoff.py score
"""

import argparse
import copy
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "output" / "bakeoff" / "runs"
FIXTURES = {
    "lemon_grove_2011030915": "Lemon Grove",
    "placentia_yorba_linda_2009030040": "Placentia-Yorba Linda",
    "los_altos_2011030660": "Los Altos",
}
DEFAULT_MODELS = ["qwen3.5:9b", "gemma4:31b", "qwen3.6:27b",
                  "gpt-oss:120b", "qwen3.5:122b", "mistral-medium-3.5:128b"]
PROMPT_VERSION = "holdings_v1"
# /api/generate, NOT /api/chat: on ollama 0.23.2 the chat endpoint silently
# ignores `format` (verified empirically — even a flat enum schema and
# format:"json" pass through unenforced). generate enforces schemas
# correctly, including $ref/$defs and oneOf.
OLLAMA = "http://localhost:11434/api/generate"

# ------------------------------------------------------------- pass schemas


def load_record_schema() -> dict:
    return json.loads((ROOT / "schema" / "decision_record.schema.json").read_text())


def holdings_pass_schema(strict_quotes: bool = False) -> dict:
    """Holdings + related_proceedings only; affected_respondents become names
    (ref assignment happens at merge time, once a roster exists).

    strict_quotes (prompt v2+): facts[].quote is required and non-null —
    constrained decoding then forces an anchor for every asserted fact."""
    rec = load_record_schema()
    defs = copy.deepcopy(rec["$defs"])
    if strict_quotes:
        facts = defs["holding"]["properties"]["facts"]["items"]
        facts["required"] = ["summary", "quote"]
        facts["properties"]["quote"] = {"$ref": "#/$defs/quote_anchor"}
    ruling = defs["holding"]["properties"]["ruling"]
    del ruling["properties"]["affected_respondents"]
    ruling["properties"]["affected_respondents_names"] = {
        "type": "array", "items": {"type": "string"},
        "description": "Surnames of respondents this ruling specifically affects."}
    del defs["respondent_ref"]
    for k in ("normalized_entity", "counsel_entry", "resolution_artifact",
              "disposition_entry"):
        del defs[k]
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object", "additionalProperties": False,
        "required": ["holdings", "related_proceedings"],
        "properties": {
            "holdings": {"type": "array", "items": {"$ref": "#/$defs/holding"}},
            "related_proceedings": copy.deepcopy(rec["properties"]["related_proceedings"]),
        },
        "$defs": defs,
    }


def mega_pass_schema() -> dict:
    """Whole record in one shot, minus pipeline-owned fields."""
    rec = load_record_schema()
    schema = copy.deepcopy(rec)
    for k in ("schema_version", "full_text", "provenance"):
        del schema["properties"][k]
    schema["required"] = ["identity", "holdings"]
    ruling = schema["$defs"]["holding"]["properties"]["ruling"]
    del ruling["properties"]["affected_respondents"]
    ruling["properties"]["affected_respondents_names"] = {
        "type": "array", "items": {"type": "string"}}
    return schema


MEGA_EXTRA = """
ADDITIONALLY extract the full decision record per the enforced schema: identity
(case number, district, ALJ, dates, decision_kind, hearing, school year),
procedure (counsel and firms, respondent counts, stipulated, consolidations),
board_action (resolutions, FTEs, services reduced, statutory basis, and the
resolution artifacts — skip criteria, tie-break criteria, competency definition
— each with status quoted_verbatim/summarized_by_alj/mentioned_only/absent/unknown),
and outcome (overall, respondent roster with names as printed, roster_completeness,
per-respondent dispositions with representation). Leave canonical/canonical_id
fields null; they are filled by a later normalization pass. Use null for anything
the decision does not state.
"""

def strip_unsupported(schema):
    """Remove JSON Schema keywords ollama's grammar converter cannot handle.
    `pattern` crashes the runner with 'failed to load model vocabulary
    required for format' (verified empirically). The full schema is still
    used for post-hoc validation, so stripped constraints are checked there."""
    if isinstance(schema, dict):
        return {k: strip_unsupported(v) for k, v in schema.items()
                if k != "pattern"}
    if isinstance(schema, list):
        return [strip_unsupported(v) for v in schema]
    return schema


# ------------------------------------------------------------------ runner


def call_ollama(model: str, system: str, user: str, fmt: dict | None,
                timeout: int = 5400) -> dict:
    payload = {
        "model": model,
        "system": system,
        "prompt": user,
        "stream": False,
        # num_predict is a hard cap, not a target: a model that loops at
        # temperature 0 gets truncated -> invalid JSON -> scored as failure,
        # instead of generating forever (observed with num_predict=-1).
        "options": {"temperature": 0, "seed": 7, "num_ctx": 32768,
                    "num_predict": 12288},
        "keep_alive": "15m",
    }
    if fmt is not None:
        payload["format"] = fmt
    # Hybrid-reasoning models (qwen3.5 etc.) can burn the entire num_predict
    # budget in the thinking channel and emit zero content under constrained
    # decoding. Disable thinking by default; strip the flag for models that
    # reject it (non-thinking models, and gpt-oss which can't disable).
    payload["think"] = False
    for attempt in (0, 1):
        req = urllib.request.Request(
            OLLAMA, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            if attempt == 0 and e.code == 400 and "think" in body.lower():
                payload.pop("think", None)
                continue
            raise RuntimeError(f"HTTP {e.code}: {body[:300]}") from e


def parse_json_loose(s: str):
    s = s.strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s)
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        i, j = s.find("{"), s.rfind("}")
        if i >= 0 and j > i:
            return json.loads(s[i:j + 1])
        raise


def run_matrix(models, cases, mode, force=False, prompt_version=PROMPT_VERSION):
    RUNS.mkdir(parents=True, exist_ok=True)
    system = (ROOT / "pipeline" / "prompts" / f"{prompt_version}.txt").read_text()
    strict = prompt_version != "holdings_v1"
    if mode == "mega":
        system += MEGA_EXTRA
        fmt = mega_pass_schema()
    else:
        fmt = holdings_pass_schema(strict_quotes=strict)
    if mode == "unconstrained":
        send_fmt = None
        system += ("\nOutput ONLY the JSON object, no prose, no code fences, "
                   "matching the structure described above.")
    else:
        send_fmt = strip_unsupported(fmt)

    for model in models:                      # outer loop: load each model once
        for case in cases:
            safe = model.replace(':', '_').replace('/', '_')
            suffix = "" if prompt_version == "holdings_v1" else f"__{prompt_version}"
            out = RUNS / f"{safe}__{case}__{mode}{suffix}.json"
            if out.exists() and not force:
                print(f"skip (exists): {out.name}")
                continue
            fixture = json.loads((ROOT / "output" / "examples" / f"{case}.json").read_text())
            user = "DECISION TEXT:\n\n" + fixture["full_text"]
            print(f"run: {model} / {case} / {mode} / {prompt_version} ...", flush=True)
            t0 = time.time()
            rec = {"model": model, "case": case, "mode": mode,
                   "prompt_version": prompt_version}
            try:
                resp = call_ollama(model, system, user, send_fmt)
                rec["duration_s"] = round(time.time() - t0, 1)
                content = resp.get("response", "")
                rec["raw"] = content
                rec["eval_counts"] = {k: resp.get(k) for k in
                                      ("prompt_eval_count", "eval_count")}
                try:
                    rec["parsed"] = parse_json_loose(content)
                except Exception as e:
                    rec["parse_error"] = f"{type(e).__name__}: {e}"
            except Exception as e:
                rec["duration_s"] = round(time.time() - t0, 1)
                rec["run_error"] = f"{type(e).__name__}: {e}"
            out.write_text(json.dumps(rec, indent=2))
            print(f"  done in {rec['duration_s']}s "
                  f"({'ok' if 'parsed' in rec else 'ERROR'})", flush=True)


# ------------------------------------------------------------------ scorer

WORD_RE = re.compile(r"[a-z0-9]+")


def tokens(s: str) -> set:
    return set(WORD_RE.findall((s or "").lower()))


def collect_anchors(obj) -> list[str]:
    """All quote-anchor strings anywhere in a parsed result."""
    out = []
    if isinstance(obj, dict):
        q = obj.get("quote")
        if isinstance(q, str):
            out.append(q)
        elif isinstance(q, dict) and isinstance(q.get("quote"), str):
            out.append(q["quote"])
        for k, v in obj.items():
            if k != "quote":
                out.extend(collect_anchors(v))
    elif isinstance(obj, list):
        for v in obj:
            out.extend(collect_anchors(v))
    return out


def norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def anchor_stats(anchors: list[str], text: str) -> dict:
    text_n = norm_ws(text)
    exact = fuzzy = miss = 0
    misses = []
    for a in anchors:
        if a in text:
            exact += 1
        elif norm_ws(a) in text_n:
            fuzzy += 1
        else:
            miss += 1
            misses.append(a[:100])
    return {"n": len(anchors), "exact": exact, "fuzzy": fuzzy,
            "miss": miss, "miss_samples": misses[:5]}


def holding_sim(mh: dict, fh: dict) -> float:
    mt = tokens((mh.get("issue") or {}).get("statement", "")) | tokens(mh.get("summary_style_holding") or "")
    ft = tokens(fh["issue"]["statement"]) | tokens(fh.get("summary_style_holding") or "")
    jac = len(mt & ft) / len(mt | ft) if (mt or ft) else 0.0
    s = 0.5 * jac
    if (mh.get("issue") or {}).get("category") == fh["issue"]["category"]:
        s += 0.3
    if (mh.get("ruling") or {}).get("prevailing_party") == fh["ruling"]["prevailing_party"]:
        s += 0.2
    return s


def match_holdings(model_hs: list, fixture_hs: list, thresh=0.35):
    pairs = sorted(((holding_sim(m, f), i, j)
                    for i, m in enumerate(model_hs)
                    for j, f in enumerate(fixture_hs)), reverse=True)
    used_m, used_f, matches = set(), set(), []
    for s, i, j in pairs:
        if s < thresh or i in used_m or j in used_f:
            continue
        used_m.add(i); used_f.add(j); matches.append((s, i, j))
    return matches


def score():
    validators = {False: Draft202012Validator(holdings_pass_schema(False)),
                  True: Draft202012Validator(holdings_pass_schema(True))}
    fixtures = {c: json.loads((ROOT / "output" / "examples" / f"{c}.json").read_text())
                for c in FIXTURES}
    rows = []
    for f in sorted(RUNS.glob("*.json")):
        run = json.loads(f.read_text())
        case, mode = run["case"], run["mode"]
        pv = run.get("prompt_version", "holdings_v1")
        validator = validators[pv != "holdings_v1"]
        fixture = fixtures[case]
        row = {"model": run["model"], "case": FIXTURES[case], "mode": mode,
               "prompt": pv.replace("holdings_", ""), "dur": run.get("duration_s")}
        parsed = run.get("parsed")
        if parsed is None:
            row.update(valid="ERR", err=(run.get("run_error") or run.get("parse_error", ""))[:60])
            rows.append(row); continue
        body = parsed if mode != "mega" else {
            "holdings": parsed.get("holdings", []),
            "related_proceedings": parsed.get("related_proceedings", [])}
        errs = list(validator.iter_errors(body))
        row["valid"] = "yes" if not errs else f"no ({len(errs)})"

        a = anchor_stats(collect_anchors(parsed), fixture["full_text"])
        row["anchors"] = f"{a['exact']}/{a['n']}" + (f"+{a['fuzzy']}f" if a["fuzzy"] else "")
        row["anchor_exact_pct"] = round(100 * a["exact"] / a["n"]) if a["n"] else None
        row["_miss_samples"] = a["miss_samples"]

        mh, fh = body.get("holdings") or [], fixture["holdings"]
        row["n_holdings"] = f"{len(mh)} (gold {len(fh)})"
        if fh:
            matches = match_holdings(mh, fh)
            row["recall"] = round(len(matches) / len(fh), 2)
            row["precision"] = round(len(matches) / len(mh), 2) if mh else None
            cat_ok = sum(1 for s, i, j in matches
                         if mh[i].get("issue", {}).get("category") == fh[j]["issue"]["category"])
            row["cat_acc"] = f"{cat_ok}/{len(matches)}" if matches else "-"
        else:  # Los Altos: every holding is a hallucination
            row["recall"] = "n/a"
            row["precision"] = "CLEAN" if not mh else f"{len(mh)} FALSE"
            row["cat_acc"] = "-"
        if mode == "mega":
            ident = parsed.get("identity") or {}
            checks = [
                ident.get("oah_case_no") == fixture["identity"]["oah_case_no"],
                fixture["identity"]["alj"]["raw"].split()[-1].lower()
                in ((ident.get("alj") or {}).get("raw") or "").lower(),
                ident.get("decision_date") == fixture["identity"]["decision_date"],
            ]
            row["mega_identity"] = f"{sum(checks)}/3"
        rows.append(row)

    lines = ["# Bake-off report", "",
             "| model | case | mode | prompt | valid | holdings | recall | precision | cat | anchors exact | dur(s) |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in sorted(rows, key=lambda r: (r["prompt"], r["mode"], r["model"], r["case"])):
        lines.append("| {model} | {case} | {mode} | {pv} | {valid} | {nh} | {rec} | {prec} | {cat} | {anc} | {dur} |".format(
            model=r["model"], case=r["case"], mode=r["mode"], pv=r["prompt"],
            valid=r.get("valid", "-"),
            nh=r.get("n_holdings", "-"), rec=r.get("recall", "-"),
            prec=r.get("precision", "-"), cat=r.get("cat_acc", "-"),
            anc=r.get("anchors", r.get("err", "-")), dur=r.get("dur", "-")))
    lines.append("\n## Hallucinated-anchor samples\n")
    for r in rows:
        for msq in r.get("_miss_samples", []):
            lines.append(f"- {r['model']} / {r['case']} / {r['mode']}: `{msq}`")
    out = ROOT / "output" / "bakeoff" / "report.md"
    out.write_text("\n".join(lines) + "\n")
    print(f"wrote {out} ({len(rows)} runs scored)")


# -------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("cmd", choices=["run", "score"])
    ap.add_argument("--models", default=",".join(DEFAULT_MODELS))
    ap.add_argument("--cases", default=",".join(FIXTURES))
    ap.add_argument("--mode", default="constrained",
                    choices=["constrained", "unconstrained", "mega"])
    ap.add_argument("--prompt-version", default=PROMPT_VERSION)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    if args.cmd == "run":
        run_matrix(args.models.split(","), args.cases.split(","), args.mode,
                   args.force, args.prompt_version)
        score()
    else:
        score()
    return 0


if __name__ == "__main__":
    sys.exit(main())
