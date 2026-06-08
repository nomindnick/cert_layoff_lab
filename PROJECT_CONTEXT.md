# Project Context: OAH Teacher-Layoff Decision Corpus & Tooling

This document orients you (Claude Code) to a new experimental project. Read it before
we start. It explains who I am, what we're building, why, and concretely what I want to
accomplish in this repo over the next ~week.

---

## Who I am

I'm a California attorney representing public agencies — primarily school districts,
plus special districts and JPAs. I'm also a builder: I taught myself Python and AI
tooling and I've shipped real things. I work in legal-AI as a hybrid attorney-engineer,
not a hobbyist. Assume I can read code, reason about architecture, and make my own
technical calls — but that my deepest expertise is the *domain* (California education
law, certificated employee layoffs under Education Code §§ 44949/44955), and that
domain knowledge is the actual moat for this project.

Practical implications for how to work with me:
- Don't over-explain basic programming. Do explain tradeoffs and flag where a technical
  decision has downstream consequences I might not see.
- I value honest pushback over agreement. If a plan is weak, say so.
- I have plenty of time for this right now and low external pressure, so I'd rather do
  the load-bearing parts (schema, extraction quality) deliberately than rush to a demo.

## Relevant prior work: the FPPC project

There is an FPPC opinion search project in a repo on this same system — you can look at
it directly rather than relying on my description. Short version: I built a corpus of
~14,100 California FPPC advisory opinions (one JSON per opinion), with a hybrid
BM25 + embedding retrieval engine, a fine-tuned domain embedding model, an MCP server
wrapper, and a public web app deployed on Railway. Patterns from that project that
carry over here:
- One-JSON-per-document corpus design.
- Configurable corpus paths, build-or-load index pattern, self-contained portable files.
- Hybrid retrieval (BM25 + embeddings).
- An OCR-quality-improvement pass (I used olmOCR on the worst documents).

Please reuse those patterns where they fit rather than reinventing. The retrieval/app
layer here is largely a port of FPPC; the *novel* work is the corpus and the extraction.

## Hardware / inference context

I have a Framework Desktop (AMD Strix Halo, 128GB unified memory, Fedora Linux) running
local LLM inference. This matters: **local inference is effectively free** for this
project, bounded only by wall-clock time, which I have. That changes the economics
versus the FPPC project, where I rationed paid API calls (Claude Haiku) for metadata
extraction and leaned on regex. Here I can afford:
- Rich, multi-field LLM extraction across the whole corpus.
- Multi-model extraction with reconciliation (run N models, compare, flag disagreements).
- Experimentation with multiple OCR approaches, including vision-LLM OCR.

Assume "just run a model over all of it" is on the table. The constraint is fidelity,
not cost.

---

## What this project is

### The domain artifact

California school districts lay off certificated employees (teachers) via a statutory
process under Education Code §§ 44949/44955. Disputes go to the Office of Administrative
Hearings (OAH), where an ALJ issues a proposed decision. These decisions turn on a
recurring, well-defined set of issues: seniority, competency, bumping, skipping,
tie-breaking, credentials, sufficiency of the resolution/notice, etc.

These decisions are **not** centrally published or searchable anywhere. My firm obtained
a large set via CPRA (Public Records Act) requests. That makes a systematically
collected, indexed corpus a genuinely proprietary asset — nobody else has it assembled.

Note the risk profile is favorable: this is layoff/RIF, not teacher *discipline*. The
subject matter isn't misconduct, the PII is far less sensitive, and — importantly — the
canonical human work product (see below) already cites decisions de-identified, by
district and ALJ only (e.g. "San Diego Unified (Levy)"), never by the teacher's name.
So the analytic layer keys on district/ALJ/issue, and respondent names are incidental.

### The two corpora

There are **two distinct corpora**, and both matter:

1. **The decisions corpus.** ~2,800 individual files obtained via CPRA. Mostly
   individual OAH proposed decisions (typically short — 5–8 pages, much shorter than
   FPPC opinions), but the set also includes many of the summary documents described
   below. Many are scanned PDFs of varying OCR quality, some going back decades. OCR and
   extraction are the hard, load-bearing work here.

2. **The summaries corpus.** For years, attorneys (originally a multi-firm effort —
   see history below) produced annual "Layoff Decision Summaries": expert-written
   documents that read every certificated layoff decision for a given year and
   catalogued the legal issues by category, with one-paragraph holdings each tagged to
   a district + ALJ, plus citations. These exist going back to the **1980s** and up
   through at least the early 2010s. **This corpus is gold.** It is, simultaneously:
   - a pre-built **issue taxonomy** authored by domain experts;
   - dozens of worked **few-shot examples** of "decision → issue-tagged holding";
   - an **eval gold standard** (did my pipeline recover the same holdings, filed under
     the same issue?);
   - a **completeness index** (the appendices list every decision by OAH case number
     for covered years, so completeness becomes knowable for those years);
   - mineable **data in its own right** (issue-category drift across ~30 years is a
     real, interesting result — e.g. Common Core competency fights are clearly a
     2010s artifact).

   A representative example summary (2015–2017 Master Compilation) and a couple of
   sample individual decisions are included in this repo. Study the compilation closely:
   its Roman-numeral structure is the seed taxonomy, and its district/ALJ citation
   convention is the de-identification model.

   **Eval caveat:** the summaries are *editorial*, not exhaustive. Humans pulled the
   noteworthy holdings and dropped routine ones (this is why a few districts/ALJs
   dominate any given volume — they had the interesting fights). So when the pipeline
   surfaces a holding the human summary omitted, that's often correct over-recovery,
   not a false positive. Design eval so a human reviews disagreements; recall-against-
   summary is a soft signal, not ground truth.

### Who wants this and what they've tried

The interested partner is **a firm partner**. Teacher layoffs are her #1 priority (the firm also
has teacher-*discipline* and classified-employee layoff/discipline decisions, which are
lower priority and explicitly out of scope for now — though note the architecture below
should clone cleanly to them later; only the issue vocabulary changes).

The project actually predates my firm: it originated with an attorney (now at my firm)
who ran the annual-summary effort at his prior firm. The partner's goal is to recreate that
annual-summary work using AI. Her attempts so far: (1) feeding decisions one-by-one
through a consumer AI product manually — too tedious; (2) batching 1,000+ pages of
decisions into a consumer AI and asking for a report — didn't work (context overload;
the model was asked to do extraction, clustering, and synthesis simultaneously across
a context far beyond where fidelity holds). She stopped there.

**Important:** the partner doesn't know what's technically possible here, so I don't want her
prior attempts to bound the vision. Treat her stopping point as the floor, not the spec.

---

## The long-term vision: a capability ladder

I see an escalating-autonomy ladder. Each rung delivers standalone value and degrades
gracefully — if a higher rung doesn't pan out, the lower rungs are still useful. We are
NOT betting the project on the speculative top.

1. **Searchable database (attorney predicts).** FPPC-style hybrid search over the
   decisions corpus + structured filtering by issue/ALJ/district/outcome. Puts the data
   in front of the attorney; the attorney draws the insight. Largely a port of FPPC.

2. **Annual summary regeneration.** LLM loop over the structured corpus to regenerate
   the traditional summary documents — but ideally NOT as a static Word doc. More
   interesting: an interactive web app for exploring the summarized data (issue drift
   over time, by ALJ, by district, etc.). Key architectural point: if extraction is
   designed around the *holding* (not the document), this rung is a `GROUP BY` over the
   same structured data that powers rung 1 — a query, not a separate heroic synthesis
   pass. This is also *why* the partner's batch approach failed and map-reduce (careful
   per-decision extraction → deterministic aggregation) succeeds.

3. **System predicts (case insight).** The real use case: an attorney working a live
   layoff matter wants insight into their own case — how similar matters resolved, what
   issues will arise, what opposing counsel will argue, how *their specific ALJ* tends
   to rule on those issues. This is where the system moves from presenting data to
   helping make the prediction.
   - **Honest constraint:** this is probably NOT an ML-model play. ~2,800 decisions
     conditioned on (specific ALJ × specific issue) leaves many cells in single digits.
     Frame it as **retrieval-and-reason over a case base**, not a trained classifier:
     "here are the N most similar prior matters, here's how they went, here's why yours
     may differ," with cited cases the attorney can verify. More honest about the data,
     more legally defensible, more useful than an untrusted probability score.

4. **Novel theory generation ("Move 37").** System explores the issue/holding space to
   surface novel theories or under-explored issue combinations a human might not think
   of. The human compilations already do a hand-version of this (they flag things like
   "the ALJ appears to have conflated 'competent' with 'special training and
   experience'" and "appears to have implicitly applied the domino theory"). Automating
   that noticing is the aspiration.
   - **Honest constraint:** hold this one loosely. Unlike chess, there's no oracle — the
     system can verify a theory is *unattested in the corpus*, but "unattested" can mean
     genuinely novel OR known-loser. So the realistic framing is **candidate generation
     / hypothesis generation for an expert to evaluate**, NOT hypothesis validation. I
     (domain expert) stay the verifier.

**The crucial cross-cutting insight: corpus richness *is* the ladder.** Whether we can
climb past rung 1 is determined almost entirely by what the extraction captured, not by
app code. So we extract for rung 3+ even while only building rung 1. Specifically, the
per-holding schema must capture not just issue/outcome/ALJ but the **arguments made**
(by district and by respondent), the **facts relied on**, the **authorities cited and
how they were used**, and the **reasoning chain**. Re-extracting across 2,800 files
later is the thing that would actually sink this; capturing rich fields now (cheap,
because local inference is free) avoids it entirely. Do the expensive pass once,
rung-3-ready.

---

## Concretely: what I want to do in THIS repo (vacation spike)

This is an **experiment-focused repo** — a deliberate throwaway spike to de-risk the
unknowns before I commit to a production repo. The unknowns are: (a) how bad is the OCR
and how much cleaning is needed, (b) what's the right schema for both corpora, (c) what
does the ingestion pipeline look like. Success condition: a validated extraction schema
and, ideally, the **full corpus built from the partial dataset I've already downloaded**.

Important constraint on the dataset: I was only able to download files uploaded through
**2010 (by upload date, not decision date)** before our document management system
choked. So this partial set is a *temporally scrambled* slice, not a clean run — good
for stress-testing schema/OCR against format variety, but **don't draw completeness
conclusions from it**, and key all date logic on **decision date parsed from the
document**, not upload date.

### Priorities, in order

0. **Inventory first.** Before any pipeline: inventory what's actually in the repo.
   Filename patterns, file types, rough **decision-vs-summary** classification per file,
   the date situation (and how far upload-date diverges from parsed document-date), and
   OCR-quality distribution. An hour of "what do I actually have" will reshape every
   later decision. Specifically flag any **overlap years** where I have BOTH a summary
   AND its underlying decisions — those are where we develop/validate extraction against
   gold labels during the spike.

1. **Summaries corpus first.** It's clean text (no OCR battle), smaller, and it yields
   the taxonomy + eval before I sink time into the hard corpus. Mining it for
   issue-drift over the years is a real, shippable mini-result that builds momentum.
   Build the coverage map here (year × {have summary? have decisions?}).

2. **Decisions corpus.** This is where OCR + extraction is the whole game.
   - OCR: experiment with multiple approaches (different OCR models, multiple
     independent outputs, vision LLMs). Reuse the FPPC olmOCR pattern. Extraction must
     be robust to garbled text — note the sample files have real OCR errors ("ALJ"→"All",
     "Romo"→"Rorno").
   - Extraction: the load-bearing decision is **schema richness** (see ladder insight
     above). Design the per-holding schema to carry the rich rung-3 fields even though
     nothing in the spike consumes them yet.
   - Use **multi-model extraction with reconciliation** on the overlap-year decisions
     and eyeball where models disagree — that disagreement signal tells us whether the
     schema reliably captures the *hard* fields (arguments, reasoning) or only the easy
     ones (date, ALJ).

3. **(Only if time remains) a thin search prototype** — but I'd rather spend surplus
   time hardening extraction than building a search box. The app is the easy, fun,
   low-risk part and it'll still be easy when I'm back. Resist the pull to ship a UI
   while the corpus is hot.

### Engineering discipline that makes the spike transfer cleanly

The whole point is to lift this pipeline into a production repo and re-run it on the
FULL dataset (which I'll pull from the document management system when I'm back in the
office). So:
- **Separate pipeline from data.** Config-driven corpus paths (FPPC pattern). No
  hardcoded paths, no assumptions baked in about the partial set.
- **Every cleanup step is code, not handwork.** If I clean something by hand, it won't
  survive the transfer. If it's a script, it will.
- **Idempotent, re-runnable, resumable stages.** Build-or-load pattern. I want to point
  the same pipeline at the full corpus later and have it just work.
- **One JSON per document** corpus output (FPPC pattern), portable and self-contained.

### Eventual production plan (context, not for this repo)

When I'm back: transfer the pipeline to a clean production repo, pull the full dataset
from the firm's document management system, build out both full corpora, then start
building the actual applications (the ladder above). This spike is purely to make that
transfer fast and low-risk.

---

## TL;DR for you, Claude Code

1. Start by **inventorying the files** and building a coverage map; flag overlap years.
2. Build the **summaries corpus** first (clean, yields taxonomy + eval + a quick win).
3. Tackle the **decisions corpus**: OCR experimentation, then **rich** per-holding
   extraction (issue, outcome, ALJ, district, dates AND arguments, facts, authorities,
   reasoning) with multi-model reconciliation, validated against overlap-year gold.
4. Keep **pipeline and data cleanly separated**, every step **as code**, stages
   **idempotent/resumable**, output **one-JSON-per-doc** — so this lifts into the
   production repo and re-runs on the full dataset unchanged.
5. The seed taxonomy and de-identification model both live in the included **2015–2017
   Master Compilation** — read it carefully.
6. Reuse FPPC patterns (in a repo on this system) for retrieval/indexing; the novel work
   is corpus + extraction.
7. Push back on me. Flag tradeoffs. The schema is the load-bearing decision — get it
   rich enough for rung 3 now so we never re-extract.
