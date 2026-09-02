# a-doc — Personal Longitudinal Medical Diagnostic Assistant

## Context

A single-user interactive diagnostic-support agent for the shape of a long diagnostic odyssey: a complex, still-undiagnosed condition (this build focuses on the autoimmune domain), care fragmented across specialist referrals where no one holds the whole picture, and a real risk of anchoring on self-diagnosis, which the system is built to counteract rather than indulge. The operator is the (non-technical) patient herself, via a web chat UI. The tool is the "whole-picture holder": it ingests lab PDFs and scanned doctor reports (via Dropbox) plus structured records, maintains a longitudinal case file, and produces evidence-linked diagnostic *leads*, test suggestions, and specialist recommendations — framed as material to bring to doctors, never as diagnoses.

Design lineage — Claude native PDF/vision ingestion for scanned reports; files+git+SQLite over vector/graph stores at N=1 (auditable, revertible, no infra); free knowledge sources for the Phase 3 knowledge layer (UMLS, Orphanet/Orphadata, HPO `phenotype.hpoa`, Mondo, Monarch KG, LIRICAL phenotype-only mode, PubMed E-utilities/PMC OA, StatPearls/GeneReviews) with UpToDate/Merck/DynaMed excluded as license-blocked; functional/cognitive decomposition of a single reasoner (Ledger-Maintainer / Challenger / Test-Chooser / Composer) over specialty-persona multi-agent designs, which the literature shows adds no accuracy for medical QA; a mandatory cross-family Challenger and blind re-differential panel as the primary defense against anchoring, the dominant failure mode for this use case. Expectation-setting: on real diagnostic-odyssey cases, frontier models land an exact diagnosis in the low double digits of percent and a useful lead somewhat more often — outputs are leads, not answers.

## Headline architecture decisions

| Decision | Choice |
|---|---|
| Agent topology | **One primary frontier reasoner, zero specialty personas.** Functional stages as code-controlled passes: Ledger-Maintainer → Challenger (mandatory, separate call, **different model family**) → Test-Chooser → Composer/Steward. Adaptive depth: fused single call by default; multi-call deliberation when the ledger is stuck/churning. Adaptive thinking; `effort: high` for chat, `xhigh` for the (event-triggered) deep review. |
| Reasoning pipeline as a **typed DAG with contracts** | Each loop (ingest, chat-diagnostic, deep review) is an explicit DAG (own runner in `reason/dag.py`, no framework): nodes = stages, edges carry Pydantic-validated artifacts, and every node declares **pre/postcondition contracts enforced by code** — e.g. Challenger's postcondition requires ≥1 substantive counter-argument per `most-likely` hypothesis or the run fails; the Blind-Reviewer node's precondition asserts the ledger is absent from its context pack; the Composer's precondition asserts a Challenger node completed this run. Every node execution is logged with input/output hashes → replayable, auditable, testable node-by-node. |
| Reasoner integration | Provider-agnostic adapter in `reason/client.py`: Anthropic SDK + OpenAI-compatible client (covers OpenAI and Featherless) behind one interface with structured-output support, PHI scrub, cost/audit logging. Role→model bindings live in `models.yaml`, not code. Not the Claude Agent SDK — stage order and context assembly are enforced by the DAG, and model-driven file tools must never reach PHI. Chat tool-use (`query_labs`, `search_case`, `search_documents`, web search) runs inside a single whitelisted-tools node. `reason/context.py`'s `build_context` takes an optional `query` (diagnostic turns pass the patient's raw turn text); when given, it appends a capped, ranked "Relevant Document Excerpts" section (verbatim FTS5 snippets over ingested documents' extracted text, `doc:<filename>#p<page>`-style source refs) last in the fixed section order, so its per-turn variability never invalidates prompt caching over the earlier, query-independent sections (ADR 0015). `reason/tools.py`'s `search_documents` exposes the same retrieval on-demand for informational turns. |
| State | **Two git repos: code vs data.** Data repo (`/data/a-doc-data` on the instance) is system of record: markdown case files + `differential-ledger.yaml` + immutable `sources/` + committed `doc-text/<sha256>.txt` per-document extracted text (ADR 0015) + `labs.sqlite` (rebuildable from committed `labs-export.jsonl` plus `doc-text/`'s committed `.txt` files). Every mutation is one git commit; full reviews are tagged. SQLite FTS5, no vectors, no graph DB. |
| UI | **FastAPI + Jinja2 + HTMX + SSE + Plotly.js** (not a chat framework): most of the surface (confirm queue with page images, ledger dashboard, trend charts) is page CRUD, not chat. |
| Ingestion | Dropbox app folder → `rclone move` on a timer → content-sniffed intake gate (`ingest/filetypes.py`, never by filename extension) routes by kind: **PDF** — double-pass Claude vision extraction (PDF block pass + page-PNG pass, strict Pydantic schema) → deterministic validation (per-analyte unit whitelist, physiologic bounds, trend-outlier check) → agree+valid auto-accepts, else human-confirm queue (extracted row beside source page image). **`.docx`** (real OOXML) — read directly as TEXT via `python-docx`, no LLM vision call, no LibreOffice/PDF conversion; if lab-classified, goes through the same double-pass/reconcile/confirm-queue gates as a PDF. **Plain text** (`.txt`/`.md`) — read verbatim. **Zip archives** — expanded and each member re-classified. **Genomic files** (23andMe raw export, `.vcf`/`.bcf`/BAM/FASTQ) — archived byte-for-byte under `sources/genomics/` and **never sent to any LLM** (no vision/text extraction call is ever made against them — CRITICAL DESIGN RULE, see ADR 0010), folded instead into one regenerated `case/genomics-inventory.md` summary so imputed per-chromosome files don't become one encounter each. **Document-text layer (ADR 0015):** immediately after any non-genomic document is archived, `ingest/pipeline.py` extracts its full plain text (`ingest/doctext.py`: `pdftotext` for PDF, `python-docx` for docx, verbatim read for `.txt`/`.md` — no LLM call, never fails the ingest) and stores it (`doc-text/<sha>.txt`, committed, plus `labs.sqlite`'s `document_text`/`document_text_fts` FTS5 index); `adoc backfill-doc-text` covers documents ingested before this layer existed or whose extraction failed. Apple Health Records FHIR import is a planned secondary path (Phase 4). |
| Safety | **No automated emergency detection anywhere in the system** — a deterministic keyword screen for emergency presentations was tried and removed (ADR 0021): in live use it only ever produced false positives (most memorably flagging a patient's plumbing — "our home has a septic system and a well" — as sepsis) and never caught a real one, on the reasoning that a patient having a genuine medical emergency does not type it into this app. Deterministic output gate (`reason/safety.py::treatment_gate`) still blocks dosing/treatment instructions in every patient-facing reply — see ADR 0020 for its narrowing so a bare clinical measurement (e.g. an ultrasound volume in mL) is not mistaken for a dose. All framing: "leads to discuss with your doctor." |
| Anti-anchoring (code-enforced, not prompt-hoped) | Ledger invariants in `ledger.py`: Can't-Miss tier never empty; `origin: patient` hypotheses cannot be promoted to most-likely in the diff that creates them (Challenger must run first); every hypothesis must be re-challenged within 2 ledger versions; doctor-confirmed diagnoses stay challengeable at a raised bar (new contradicting evidence required). The Challenger always runs on a different model family than the Ledger-Maintainer (shared-model blind spots don't survive a cross-family attack); the deep review runs a **blind re-differential panel** — 2–3 diverse models each produce a de novo differential without seeing the ledger, and divergences from the ledger are adjudicated by a Challenger node with the DAG contract that every divergence gets an explicit accept/reject rationale. Phase 2 adds LIRICAL/Monarch as independent *non-LLM* differential engines for a third, mechanistically different check. |
| Model strategy & self-evaluation | See "Model strategy" below: `models.yaml` role bindings, `adoc eval` benchmark harness, gated model rotation. |
| Lab confirm-queue maintenance | Three no-new-LLM-call maintenance sweeps (`adoc labs-reclassify`, `adoc labs-dedupe-twins`, `adoc labs-recanonicalize`) re-run current validation/reconcile/canonicalization logic over already-extracted rows to drain false-positive queue entries and merge/rename rows as canonicalization rules evolve. `ingest/reconcile.py`'s comparators are the single source of truth, reused rather than duplicated. See README's "Lab maintenance commands" for usage. |
| Deployment (AWS, **CloudFormation-managed**) | **ECS Fargate + EFS** (ADR 0006): a container image (root `Dockerfile`) runs as a single always-on Fargate web service (SQLite + git want one writer — enforced via `DeploymentConfiguration` max 100%/min 0%, never two tasks concurrently) plus scheduled Fargate tasks (`AWS::Events::Rule`) for ingest (10 min), review (a `rate(30 minutes)` tick that itself decides whether to run a full review — event-triggered by a marker + 6h cooldown, with a 7-day floor as a worst case; ADR 0019), and backup (nightly), all mounting a shared EFS filesystem at `/data`. `labs.sqlite`'s journal mode is `TRUNCATE` in this deployment (not the local/dev default `WAL`, which is unsafe on EFS/NFS — see `labs/db.py`). A second, non-application image exists at `deploy/lirical/` (the LIRICAL phenotype-only sidecar, ADR 0029) — validated locally, **not yet given an ECR repository, a CI build step or a task definition**. All infra as CloudFormation stacks in `deploy/cfn/`: `network.yaml` (VPC/2 public subnets/SGs), `alb.yaml` (public ALB + ACM cert + Route53 alias for `adoc.petabloc.io`, `TargetType: ip`), `ecs.yaml` (EFS, ECS cluster/service/task-defs/roles, scheduled-task rules), `backup.yaml` (versioned SSE-KMS S3 bucket + lifecycle), `ci.yaml` (OIDC role for GitHub Actions deploys + the ECR repo the image is pushed to). **Patient access is via the public ALB** (ADR 0007) at `https://adoc.petabloc.io`; app auth is username/password (scrypt-hashed) with in-app rate limiting — no VPN/tailnet gate, no WAF, no TOTP. User provisioning and one-off shells go through `aws ecs execute-command` (`EnableExecuteCommand: true`). Backup: `adoc backup` (git bundle + `labs-export.jsonl` + `sources/` sync to S3) on the nightly scheduled task. **Seed/restore** (ADR 0009): the onboarding path is curated-local-first (`adoc onboard`/`backfill`/`ingest` against a local `ADOC_DATA_DIR`) → `adoc backup` ships that history to `s3://$ADOC_BACKUP_BUCKET/latest/`; the remote side never runs onboarding itself — `docker-entrypoint.sh` runs `adoc bootstrap-data` on every container start, which restores from that S3 backup (`adoc restore`) whenever `ADOC_DATA_DIR` is empty and `ADOC_BACKUP_BUCKET` is set, else falls back to `adoc init` on a genuinely first-ever boot. Restore stages the full checkout in a sibling `.restore-staging` dir and only renames it into place once every step succeeds, so a mid-restore failure can never leave a half-restored repo that a later boot would mistake for already-initialized. Stack changes go through PRs like code (`aws cloudformation deploy` from CI or locally via change sets). |
| Privacy | API only (never consumer apps); direct identifiers (name/DOB/MRN/address/phone/email) stripped from all outbound TEXT paths (system prompt + every chat message) in `privacy.py`, matched against `case/identifiers.yaml` — `LlmClient.from_settings` (`reason/client.py`) builds this scrubber by default (ADR 0017); a caller must opt into `Scrubber.noop()` explicitly (tests/dev only) rather than getting no scrubbing by omission. `adoc init` scaffolds an empty `case/identifiers.yaml` template; `adoc identifiers show\|add\|remove` populates it; a missing/unpopulated file is a loud startup warning, never a silent no-op. **Accepted limitation:** vision extraction (`ingest/vision.py`) sends page images/PDFs of scanned documents natively — the patient's name/DOB/address as printed on the page are pixels, not text, so the scrubber cannot and does not touch them; this is a deliberate decision (ADR 0017), not an oversight. Avoid retention-extending API features (Files/Batch) in v1. |

## Model strategy & self-evaluation

**Role→model bindings (`models.yaml`, config not code; every role swappable without a release):**

| Role | Initial binding | Rationale |
|---|---|---|
| Primary reasoner (Ledger-Maintainer, Composer, chat) | `claude-opus-5` (Anthropic API) | Strongest current hard-case medical reasoning; adaptive thinking + effort dial. |
| Challenger / adversarial passes | `gpt-5.2` (OpenAI API), `reasoning_effort: high` | Cross-family by design — a different model attacks the primary's reasoning (ADR 0005; thinking depth is the `reasoning_effort` request parameter). |
| Blind-review panel (deep review) | claude-opus-5 + gpt-5.2 (`reasoning_effort: high`) + `deepseek-ai/DeepSeek-R1-0528` via Featherless ($25/mo flat) | Three families, three independent de novo differentials. |
| Extraction pass A / pass B | `claude-sonnet-5` (PDF block) / `gpt-5.2` vision (page PNGs) | Cross-family double-pass makes correlated extraction errors far less likely than same-model-twice. |
| Classifier/router | `claude-haiku-4-5` | Cheap, latency-sensitive. |

**Self-evaluation (`adoc eval`, run on demand and monthly by timer):**
- **Benchmark suite**: (a) a 302-case rare-disease dataset — score differential recall@10 against known diagnoses; (b) golden extraction fixtures (synthetic + real anonymized lab PDFs) — field-level F1; (c) the red-team transcript — safety-gate pass rate, anchor-resistance (planted "I'm sure I have X" turns must not be adopted); (d) **retrospective self-cases**: once any finding is doctor-confirmed, replay the case file as of earlier dates and check whether/when the system surfaced it — the only eval that measures *this patient's* effectiveness.
- **Ops metrics logged continuously** (per-call in `logs/api-audit`, aggregated in each full review): cost/tokens per role, extraction auto-accept vs queue rate, ledger churn, hypothesis age, challenger kill-rate, blind-panel divergence rate.
- **Model rotation**: when a new model releases, `adoc eval --candidate <provider:model>` runs the full suite against the incumbent binding and emits a comparison report; rebinding is a human-approved edit to `models.yaml` (git-tracked, so every model change is dated and revertible). No silent upgrades.

## Onboarding & end-user experience

**First run = one continuous "initial visit" conversation, on the same `/chat` surface as everything else** (ADR 0011: fact model and completion gates; ADR 0012: the conversation shape — no visible stepper; ADR 0018: the clinical progression + post-intake continuity below). `web.routes.chat`'s send route checks `intake.agent.intake_is_complete(repo)` on every turn: while false, the turn runs through `run_intake_turn`; once the deterministic wrap-up gate accepts completion, every later turn runs through the normal diagnostic/informational pipeline, forever after — one transcript, no seam the patient can see between "onboarding" and "chat." A deterministic constant (`INTAKE_OPENER_MESSAGE`, never an LLM call) opens the very first conversation: a greeting, one framing sentence, then the first concrete question — age and sex at birth — rather than an open "what brings you in," which invited exactly the wall-of-text answer ADR 0014 had to work around. While incomplete, the page shows a modest banner ("Initial visit — I'll ask about your history as we talk") — no progress bar, no section list, no percentage.

The `intake_agent` model role (`models.yaml`, same model as `primary_reasoner`) follows the patient's own narrative first and always — nothing volunteered out of order is ever deferred or refused — but is steered by an intended clinical arc (ADR 0018, `intake/agent.py`'s `_ARC_STEERING_ORDER`) once a thread winds down: individual stats → family history → geography → a review of the existing record (events/prior diagnoses/documents, citing what's already on file rather than re-asking) → what's recent, last. Along the way it probes indistinct answers ("my dad has allergies" → which allergens, what reactions, how severe, dad's age), establishes timing for every event/diagnosis once (recording `precision=unknown_after_probe` rather than nagging twice when the patient doesn't know), classifies a stated condition's attribution (doctor-diagnosed, with who/when, vs. the patient's own conclusion, with their reasoning — never conflating the two), cross-references already-ingested documents/encounters via a deterministic digest ("I have a record of an ER note dated 2024-03-02 — is that this visit, or a different one?"), and sees a small, capped, query-matched set of verbatim excerpts from documents she has already provided, retrieved against her current message (ADR 0015), so it can say "your history mentions X — is that still going on?" instead of asking her to retype what she already wrote. Every patient statement becomes a typed `IntakeFact` (`intake/facts.py`) applied by plain deterministic code, never written to the case file directly by the model. Internally, the case file organizes what's captured under eleven fixed topic keys (`intake/sections.py`, which also drives the writers below) — nothing patient-facing ever names, numbers, lists, or steps through them:
1. Basics — age, sex at birth, height/weight, occupation/exposures
2. Current symptoms — free narrative, model extracts a structured symptom list (→ seeds the Phase-2 HPO profile) and asks targeted follow-ups (onset, frequency, triggers)
3. Major medical event history — timeline walk; each event becomes an `encounters/` file
4. Prior diagnoses & workups — including the patient's own suspected diagnoses, which are recorded straight into the ledger as `origin: patient`
5. Family history — structured relative/condition entries (autoimmune, cancer, cardiac, early deaths) → `case/family-history.md`
6. Geography & environmental exposure (ADR 0018) — current + prior residences, travel, and environmental/occupational exposure tied to a place or trip (tick-borne and other regional infectious risk, well water, farm work) → `case/geography.md`
7. Medications — current + past-significant, dose-free is fine → `case/medications.md`
8. Supplements — same file, flagged separately (interacts with labs, e.g. biotin)
9. Allergies & reactions
10. Care team & insurance — current doctors, specialists seen, insurer → `case/care-team.md`
11. Document drop — existing PDFs/scans, worked into the conversation rather than a fixed final step. Auto-covered when `sources/` is already non-empty (a seeded/curated deployment, or a local repo that already ran a backfill).

**Deterministic coverage/wrap-up gates** (`intake.agent.run_intake_turn`, the core safety mechanism of this feature) are the only thing that may mark a topic covered or the whole intake complete — the model may propose `topics_covered`/`intake_complete`, but code vetoes any topic where `section_completion_blockers` is non-empty, silently, without surfacing gate mechanics on routine turns; a premature `intake_complete` gets ONE deterministic, conversationally-phrased steering line naming what's still missing, exactly the way a ledger invariant outranks the Ledger-Maintainer's proposed diff (CLAUDE.md rule 3, same shape applied to intake). "Nothing to report" is legitimate, complete coverage of a topic (zero active facts filed to it trivially has zero blockers). Facts are editable at any time, during or after the initial visit — a correction to an already-covered topic regenerates that topic's case-file artifact(s) immediately. `intake-state.yaml` is a flat per-topic coverage map (`covered`/`covered_at`) plus a monotonic `intake_complete` flag (`intake/coverage.py`). A legacy form-style state-machine wizard is retained as `adoc onboard --legacy-wizard` and as the shared output layer: `intake/convert.py` maps active facts onto the same per-section schemas the wizard writes through, so every onboarding path produces identical case-file artifacts. `/onboard` and `/onboard/send` redirect to `/chat`; `/onboard/review` survives as "Intake record," a read-only page grouping facts by topic.

Mechanics: fully resumable (coverage state in the data repo); every turn = one git commit. Until intake is complete, diagnostic answers carry a visible "baseline incomplete — these leads may shift" banner (home screen), and every chat turn routes through the intake engine rather than the diagnostic pipeline. Correctable any time afterward through the same one chat surface ("update my medications").

**The record keeps growing after the initial visit, and is checked against what's already on file (ADR 0013).** Every post-intake chat turn that completes successfully (not a withheld or error turn) also runs a silent `intake.agent.run_visit_capture` pass: a second, dedicated `intake_agent`-role call that emits fact ops only for genuinely new or changed patient-reported information, with no reply of its own (the patient never sees this pass; a failed call is caught and swallowed, never breaking the chat turn whose real reply already succeeded). Every `IntakeFact` carries `reported_on` (the date it was created/last touched) and a deterministic `corroboration` state (`intake/corroborate.py`, no LLM): checked against already-ingested document dates, encounter files, and lab rows on the same turn that adds/changes it, and re-sweepable on demand via `adoc intake-corroborate`. Corroboration is conservative by design — absence of a match is `"unverified"`, never `"contradicted"`; `"contradicted"` is reserved for a fact whose stated timing is flatly impossible (e.g. a diagnosis year in the future). The Intake record page shows each fact's corroboration badge and a "Since last visit" strip of recently-reported facts; the `intake_agent` prompt is told a fact's corroboration status and raises a contradiction with the patient conversationally, once, rather than silently overwriting or ignoring it.

**Post-intake continuity (ADR 0018).** `IntakeFact.follow_up` is a model-settable flag (via `add_fact`/`update_fact`, never inferred) marking something worth explicitly checking back on later. `intake.agent.build_continuity_info` assembles what a returning visit should already know — flagged follow-ups, still-`needs_probe` facts, recently-reported facts, and `case/questions-open.md` — and `render_continuity_note` turns it into a short, deterministic, code-composed greeting (fixed text the model cannot suppress or soften), prepended by `web.routes.chat` to the first successful reply of a new visit (a gap since the last chat entry past `VISIT_GAP_THRESHOLD_HOURS`). The same three things — last spoke, current state, follow-ups — are also shown structurally on the Intake record page.

**Steady state UX**: home screen = current three-tier differential + "what changed since you last looked" + open questions for the next appointment; chat for anything; upload/confirm queue when documents arrive; trend charts per analyte; `/ledger` ("Full picture") merges the live differential with the latest deep review inline and a history of prior ones (ADR 0019 — this used to be two nav entries, `/ledger` and `/reviews`, before reviews could fire on new evidence). Everything phrased as leads and preparation for doctors, with source links back to the patient's own documents.

## Engineering practice (this is a GitHub software project)

- **Repo docs**: `PLAN.md` (this file, kept current as phases complete); `CLAUDE.md` for the coding agent — commands, hard rules, GitFlow, prompt-edit policy, PHI boundary, `models.yaml` change procedure, ADR requirement.
- **GitFlow**: `main` = released/deployed (every merge tagged semver, auto-deployed via CI); `develop` = integration; `feature/*` branch from develop, PR back with required CI; `release/*` for stabilization; `hotfix/*` from main. Branch protection on main and develop; conventional commits; CHANGELOG per release. GitHub Issues/Milestones mirror the phases below.
- **Infrastructure as code**: CloudFormation stacks in `deploy/cfn/` (see Deployment row) — reviewed via PR, deployed by change set; no console-created resources. GitHub Actions assumes an OIDC deploy role.
- **CI (GitHub Actions)**: ruff (lint+format), mypy, pytest with coverage gate on every PR; the golden red-team transcript and ledger-invariant/DAG-contract tests are required checks — prompt edits cannot merge if they weaken safety behavior. Merge to main → build, cfn deploy (change set), release. Monthly scheduled `adoc eval` run against fixtures (no PHI in CI).
- **Hygiene**: pre-commit hooks (ruff, gitleaks secret scan); Dependabot; `.env` never committed; **PHI lives only in the data repo, which has no remote** — CI and the code repo see synthetic fixtures only.
- **Docs**: README (setup/deploy/runbook), `docs/adr/` architecture decision records for the choices in this plan, prompt templates versioned in-repo and reviewed like code.

## Provenance & re-evaluation policy (software/model changes vs persisted state)

Every persisted LLM-derived artifact (ledger diff, extraction, encounter summary, review) is stamped with **provenance**: `{app_version (git sha), prompt_template_version, model_id, dag_node, timestamp}` — stored in the artifact (ledger diffs record it per applied diff; labs rows carry it in `raw_json`). Raw sources are immutable, so every derived artifact is rebuildable.

Re-trigger rules (implemented as a staleness scanner run at the start of each full review — full reviews are event-triggered, not weekly; see "Session loops (c)" and ADR 0019):
- **Reasoner model rebinding or major-version prompt change** → the next full review automatically runs the full blind panel + a complete challenge sweep over all `active` hypotheses (not just stale ones), and the review report notes "re-evaluated under <model/prompt>".
- **Extraction model/prompt change** → **rebuild from sources** (ADR 0026). There is no incremental re-extraction path and deliberately so: `sources/` is immutable and complete, so the honest operation is to wipe the derived store and re-ingest the same files. Human review decisions live in `case/review-decisions.jsonl` as source data, not inside the derived store, so a rebuild no longer destroys them. Measured on the first real rebuild: 532 of 587 prior decisions were attached to rows the improved extractor no longer produces at all, which is why replaying them is usually the wrong instinct — the queue should be re-reviewed, not restored.
- **Schema changes** (ledger YAML, labs DDL) → versioned migration scripts in-repo; ledger schema version recorded in the file; migrations are commits in the data repo, so state and software version travel together and old software refuses newer state (version check at load).
- **Staleness policy**: artifacts whose provenance model/prompt is ≥2 bindings old and which still influence `active` hypotheses are queued for re-evaluation, highest-probability hypotheses first; every full review prints the stale-artifact count so drift is visible, never silent — and the 7-day floor (ADR 0019) guarantees this check itself still runs at least weekly even during a quiet period.

## Repo layout (code repo: `/home/dpeterka/src/a-doc`)

```
PLAN.md CLAUDE.md         # this plan; coding-agent instructions
pyproject.toml            # uv; deps: anthropic, openai, fastapi, uvicorn, pydantic(+settings), jinja2,
                          #   ruamel.yaml, pdf2image, GitPython, fhir.resources (phase 4)
.github/workflows/        # ci.yml (ruff, mypy, pytest+coverage), deploy.yml (cfn change sets, OIDC),
                          #   eval.yml (monthly fixture eval)
.pre-commit-config.yaml   # ruff, gitleaks
models.yaml               # role→model bindings (see Model strategy)
deploy/cfn/               # network.yaml, ecs.yaml (EFS+Fargate), alb.yaml, backup.yaml, ci.yaml (+ECR)
deploy/container/         # run-ingest.sh (rclone pull + adoc ingest, the scheduled ingest task's command)
Dockerfile docker-entrypoint.sh   # application image (see ADR 0006)
docs/adr/                 # architecture decision records
src/adoc/
  config.py cli.py        # pydantic-settings; `adoc <init|onboard|ingest|review|serve|backfill|
                          #   backfill-doc-text|eval|user|backup|restore|bootstrap-data|
                          #   labs-infer-specimen|labs-dedupe-twins|labs-reclassify|
                          #   labs-recanonicalize|intake-corroborate>` (17 subcommands;
                          #   `user` has add/list/remove)
  privacy.py              # identifier scrub hook applied in the API client wrapper
  backup.py               # `adoc backup`/`adoc restore`/`bootstrap-data`: git-bundle + labs-export.jsonl +
                          #   sources/ <-> S3 (ADR 0009); restore also rebuilds document_text/
                          #   document_text_fts from committed doc-text/ files (ADR 0015)
  casefile/               # repo.py (git plumbing — `_TOP_LEVEL_DIRS` includes `doc-text/`, ADR 0015),
                          #   schema.py, ledger.py (invariants+diff apply), encounters.py
  labs/                   # db.py (DDL/FTS5/migrations/JSONL export; document_text/document_text_fts,
                          #   migration 3, ADR 0015), models.py, validate.py (validation +
                          #   semantic ref-range/unit/flag comparators), queries.py, specimen.py
                          #   (`labs-infer-specimen`), twins.py (`labs-dedupe-twins`), reclassify.py
                          #   (`labs-reclassify`), recanonicalize.py (`labs-recanonicalize`), panels.py
  ingest/                 # pipeline.py, extract.py (cross-model double-pass), reconcile.py (semantic
                          #   comparators + RESCUE pass), vision.py (binary/PDF/page-image request layer),
                          #   archive.py, docx.py (deterministic `.docx` text intake, no LLM), doctext.py
                          #   (document-TEXT layer: pdftotext/docx/plain-text extraction + doc-text/
                          #   storage + `search_document_text` backing + `adoc backfill-doc-text`, no LLM,
                          #   genomics structurally excluded — ADR 0015), filetypes.py
                          #   (content-sniffed intake-kind gate: pdf/docx/text/genomic/zip), genomics.py
                          #   (non-LLM genomic archive + `case/genomics-inventory.md`), schema.py (extraction
                          #   Pydantic models), failures.py (inbox failure log + `/failed` page);
                          #   apple_health.py planned for phase 4 (FHIR zip importer)
  intake/                 # onboarding (ADR 0011 fact model/gates, ADR 0012 conversation shape) + fact
                          #   corroboration/interval history (ADR 0013): facts.py (IntakeFact store +
                          #   typed ops + deterministic section_completion_blockers gates +
                          #   corroboration/corroboration_source/corroboration_note/reported_on fields +
                          #   apply_corroboration), corroborate.py (`corroborate_facts` — deterministic
                          #   period/series corroboration against ingested documents/labs/encounters, no
                          #   LLM; `adoc intake-corroborate`), coverage.py (per-topic coverage map +
                          #   monotonic intake_complete flag), agent.py (the initial-visit engine:
                          #   intake_agent model call, topics_covered/intake_complete gating,
                          #   INTAKE_OPENER_MESSAGE constant, doc-digest cross-referencing, own audit
                          #   transcript; plus `run_visit_capture` — the silent post-intake pass that
                          #   grows the record on ordinary chat turns), convert.py (facts_to_section_data
                          #   — maps facts onto the wizard's section schemas), sections.py (schemas —
                          #   internal topic registry, never patient-facing), wizard.py (legacy state
                          #   machine: playback confirm loop, document-drop auto-skip on seeded
                          #   deployments, section writers every onboarding path shares via
                          #   `write_section`), cli.py (conversational REPL default + `--legacy-wizard`
                          #   escape hatch)
  reason/                 # client.py (provider adapter: scrub+audit+bindings), dag.py (typed DAG runner +
                          #   node contracts), context.py (deterministic context packs), stages.py,
                          #   citations.py (Phase 2 deterministic citation checker), verify.py (Phase 2
                          #   entailment verifier + Composer quantitative check), tools.py, safety.py,
                          #   review.py (the deep-review DAG + `run_review_tick`'s event-triggered
                          #   gating — ADR 0019), review_trigger.py (the "review wanted" marker ADR
                          #   0019 reads), prompts.py (versioned `<!-- version: N -->` template
                          #   loader), prompts/*.md
  evals/                  # runner.py, suites/{extraction,redteam,hallucination}.py (rare-disease-cohort +
                          #   retrospective suites planned for phase 3 — see "Model strategy" above),
                          #   report.py
  knowledge/              # phase 2: phenotype.py (HPO profile), lirical.py, monarch.py, pubmed.py, criteria/*
                          #   (currently an empty stub package)
  web/                    # app.py, deps.py (FastAPI dependency seams), security.py (session cookie + login
                          #   rate limiting), templating.py, users.py (username/password store),
                          #   casefile_helpers.py, markdown_lite.py (dependency-free renderer),
                          #   routes/{auth,home,chat,upload,confirm,labs,ledger,onboard,reviews,failed,
                          #   files}.py, templates/, static/
tests/                    # fixtures: synthetic lab PDFs + golden extractions + golden ledger diffs +
                          #   red-team transcript; test_{validate,ledger,reconcile,safety,criteria,dag,intake}.py
```

Data repo layout (no remote, PHI-only): `case/{case-summary.md, differential-ledger.yaml, questions-open.md, family-history.md, medications.md, care-team.md, intake-state.yaml, encounters/, reviews/}`, `sources/` (immutable originals + per-page PNGs), `doc-text/<sha256>.txt` (committed, per-document extracted plain text — ADR 0015), `labs.sqlite` (gitignored) + committed `labs-export.jsonl`, `inbox/ work/ logs/` (gitignored). Post-ingest inbox hygiene (`ingest.pipeline`): a file ingested/duplicated out of `inbox/` is deleted (the `sources/` archive is authoritative); one that errors is moved to `work/failed/` and logged to `work/failed/failures.jsonl` (`{filename, failed_at, reason, original_inbox_path}`), surfaced on the web `/failed` page (retry / remove). This hygiene applies ONLY to `inbox/` — `adoc backfill <external dir>` never deletes or moves a file in the directory it's given, success or failure.

## Key schemas

**`differential-ledger.yaml`** (YAML, machine-mutated via structured-output diffs, human-diffable in git):
```yaml
hypotheses:
  - id: sle-01
    name: "Systemic lupus erythematosus"
    mondo: "MONDO:0007915"            # phase 2
    tier: most-likely                  # most-likely | expanded | cant-miss
                                       # `cant-miss` renders as "Safety
                                       # checklist" — ADR 0039. The schema
                                       # value never changes.
    probability: moderate              # buckets: high|moderate|low|minimal (+ prior_probability for movement)
    status: active                     # active|patient-proposed|challenged|ruled-out|confirmed-by-doctor|parked
    origin: patient                    # model|patient|doctor|challenger
    evidence_for:  [{claim: "ANA 1:640 homogeneous", source: "labs:ana:2026-05-02", strength: strong}]
    evidence_against: [{claim: "Anti-dsDNA negative x2", source: "labs:anti-dsdna:2026-07-10", strength: moderate}]
    discriminators: ["Complement C3/C4"]   # feeds Test-Chooser
    challenger_notes: "..."
    last_challenged: "2026-08-16"
```
Source-ref grammar (mandatory on every claim): `labs:<analyte>:<date>` | `doc:<file>#p<page>` | `encounter:<file>` | `pmid:<id>` | `patient-report:<date>`.

**`labs` table**: `documents(sha256 PK, filename, doc_type, doc_date, status)` + `labs(id, date, loinc_code NULL, name /*canonical*/, name_raw, value REAL, value_text /*titers, "positive"*/, ucum_unit, ref_low, ref_high, ref_text, flag, specimen serum|plasma|whole_blood|urine|stool|csf|saliva|other|unknown (default unknown), source_doc FK, source_page, extraction_status auto|confirmed|corrected|pending|rejected, raw_json, UNIQUE(date,name,specimen,source_doc))` + FTS5. Confirm queue = `extraction_status='pending'` rows. LOINC nullable; canonical-name mapping table for this patient's ~100 recurring analytes — never block ingestion on coding.

`specimen` disambiguates same-named analytes measured in different fluids (e.g. urinalysis glucose vs. serum glucose) so they don't share one trend series: read from the report's own section headers/labels only, defaulting to `unknown` rather than guessed. `labs/queries.py`'s `trend_series`/`LabsDb.series` accept an optional specimen filter, and `trend_outlier`/`trend_deviation` (`labs/validate.py`) compare a candidate row only against priors of the SAME specimen. `adoc labs-infer-specimen` deterministically back-fills `specimen` for pre-existing `unknown` rows from their source document's filename/doc_type keywords only (no LLM).

Analytes may be flagged `kind="score"` (e.g. FRAX 10-year fracture probability, T-scores) in `validate.py`'s spec table: a printed score by nature has no unit or reference range, so `validate_row` skips those checks for `kind="score"` rows rather than flagging them as missing, and a suffix-match table resolves a score's canonical name even when a passage names it awkwardly.

**Lab taxonomy**: `AnalyteSpec` carries `panel: str | None` (hand-curated two-level panel→analyte grouping, conceptually derived from LOINC panel definitions — full LOINC coding is Phase 3) and `derived_from: tuple[str, ...]` (the canonical analyte(s) a calculated value comes from, e.g. TSAT from Iron+TIBC — display-only, nothing recomputes the arithmetic). `ANALYTE_SPECS` covers ~380 curated analyte entries across CBC+differential, CMP, Lipid, Thyroid, Inflammation, Iron Studies, Hormones, Vitamins & Nutrition, Heavy Metals, Autoimmune Serology, Tick-Borne Serology, Immunology/Flow Cytometry, Allergen IgE, Mold IgG, Tumor Markers, Urinalysis, Stool Studies, and Bone Density panels, with spelling-variant merges (lab-specific naming, case differences) folded in; entries that don't cleanly canonicalize (narrative fragments, age-bracket reference-table rows, one-off extraction noise) are deliberately left unmapped rather than forced into a fake panel. `canonicalize` applies a curated generic specimen/method suffix-strip retry (", Serum"/", S"/", Plasma"/", Quant"/",LCMSMS"/" LC/MS/MS", never "RBC" or "Total" — those carry real clinical meaning). `AnalyteSpec.allowed_units=()` means "no unit whitelist" for `kind="numeric"` (unitless by nature for `kind="score"`). `labs/panels.py` provides `PANEL_ORDER`/`panel_for`/`derived_from_note`/`panel_sort_key`, used by the `/labs` index (grouped headings), the detail page (panel + "calculated from ..." note), and `reason/context.py`'s `_labs_section` (grouped, deterministic for prompt caching) — analytes with no curated panel group under "Other", always last. `adoc labs-recanonicalize [--dry-run]` (`labs/recanonicalize.py`) is the maintenance sweep that renames/merges/queues rows whose canonical name has changed since ingestion (no LLM) — run manually against the real data repo, never by an agent.

**Matching vs. renaming**: `canonicalize` stays fully permissive for read-time matching (panel grouping, `validate_row`, trend scoping) — its suffix-strip and score-suffix rules are deliberately loose. Renaming a *stored* row is stricter: `canonical_rename_target(name_raw, name)` requires an EXACT alias match (no suffix-strip, no score-suffix inference), because clinically distinct measurements can share a name after loose matching (e.g. LEFT vs. RIGHT hip BMD, Plasma vs. RBC Manganese, race-stratified eGFR variants) and must never be silently merged onto one trend series by a rename. Only a human-reviewed alias table entry renames a stored row; a suffix/score-assisted match still gets full `canonicalize` benefit at read time but leaves the stored name untouched. `recanonicalize_rows` uses a plan-then-execute design: every row's rename target is computed first, rows are grouped by their final `(date, name, specimen, source_doc)` key, and any key shared by 2+ rows is routed through merge/conflict handling before a single rename is issued — a UNIQUE-constraint violation is structurally impossible by construction, and `--dry-run`/live share the identical in-memory computation.

**Extraction schema** (strict structured output, per document): doc_type, facility, collection/report dates, `results[]` (name_raw, value|value_text, unit_raw, ref_range_raw, flag_raw, specimen, page, confidence), `narrative_findings[]` (serology comments matter for autoimmune workups), `illegible_regions[]`.

**Encounter files**: markdown + frontmatter (date, type: lab-result|specialist-visit|imaging|patient-report|phone|procedure, provider, sources, symptoms→HPO in phase 2). Patient chat reports enter as `type: patient-report` encounters — same door as doctor notes, labeled.

## Session loops

**(a) Document lands:** rclone timer → sha256 dedupe → archive original + render page PNGs → classify call → (labs) double-pass extract → validate → reconcile → auto/pending rows + JSONL append → incremental reasoning: Ledger-Maintainer diff → Challenger (separate call, must attack) → invariant-checked apply → case-summary update → one git commit → UI banner ("12 rows added, 2 need confirmation").

**(b) Chat turn:** route informational vs diagnostic (no automated emergency screening — ADR 0021). Informational: one streamed tool-runner call (query_labs / search_case / web literature). Diagnostic: new info → patient-report encounter; patient theory → logged `origin: patient`, never the frame; Ledger-Maintainer → mandatory Challenger → apply → Composer/Steward renders three-tier differential with source refs + next-most-informative tests, treatment-advice gate on output. Adaptive escalation to multi-call deliberation on ledger churn/stuckness.

**(c) Deep review (event-triggered, `xhigh`; ADR 0019):** `adoc review` runs as a `rate(30 minutes)` scheduled tick; every tick runs the cheap deterministic parts (trend scan, deferred-entailment sweep — no blind panel); a FULL review — **blind re-differential panel** (2–3 model families, each de novo, no ledger in context — DAG contract enforces its absence) → divergence adjudication via cross-family Challenger (contract: every divergence gets an explicit accept/reject rationale) → full challenge sweep → Test-Chooser rewrites `questions-open.md` as a prioritized next-appointment list (tests to request, specialist types) → literature refresh on top-3 hypotheses → ops metrics appended (churn, hypothesis age, challenger kill-rate, cost) → committed + tagged review report; UI notification — runs only when the "review wanted" marker (set by ingest or a ledger-changing chat turn) is set and a 6h cooldown has elapsed, or unconditionally at a 7-day floor (the old weekly guarantee, preserved as a worst case). `--force` always runs a full review.

Estimated spend: $1–3/document ingest, $0.20–1/diagnostic turn, $8–20/deep review (multi-family panel), + $25/mo Featherless flat → ~$70–150/mo.

## Phasing

**Phase 0 — Project scaffold — complete.** `PLAN.md`/`CLAUDE.md`, repo bootstrap (pyproject/uv, ruff/mypy/pytest, pre-commit + gitleaks, GitHub Actions CI, GitFlow branches + protection, README, first ADRs, CloudFormation stack skeletons + OIDC deploy role).

**Phase 1 — MVP — complete.** `adoc init`/`onboard`/`ingest`/`review`/`serve`/`backfill`; full ingestion + confirm queue; web UI (onboarding, chat/SSE, upload, queue with page images, ledger view, Plotly trends); DAG runner with node contracts; staged reasoning with mandatory cross-family Challenger; `models.yaml` + provider adapter; safety gates; deep review with blind panel (weekly cron at the time; event-triggered since ADR 0019); `adoc eval` with extraction + red-team suites; AWS deploy on ECS Fargate + EFS behind a public ALB (ADRs 0006/0007).

**Phase 2 — Grounding & anti-hallucination hardening — complete.** Four layers that make fabrication structurally difficult. Design and rationale live in the ADRs; this is the shape.

- **Citation checking** (`reason/citations.py`) — every evidence source ref is resolved by code before it can reach the ledger. A `labs:` ref must match a real row, `doc:`/`encounter:` refs must resolve to the file, `pmid:` refs are verified via E-utilities and cached. Wired as a DAG contract with one objection-guided retry before the gate is final.
- **Claim-level entailment** (ADR 0016) — a third model family judges each `(claim, source text)` pair. A claim that conflicts with its source is stripped, not blocked, so the turn proceeds on verified evidence. Only `most-likely`-tier claims are verified synchronously; the rest are swept by the review (ADR 0019). This is what took a diagnostic turn from 23 minutes to a few.
- **Abstention** — `insufficient_evidence` is a typed field on both the reply and the ledger payload, and `most_likely_requires_resolved_evidence` refuses to commit a most-likely hypothesis with no resolved citation. Saying "I can't support this" is a first-class output, not a failure.
- **Hallucination eval** (`adoc eval --suite hallucination`) — planted-fact probes, fabricated-citation detection, entailment precision/recall against labelled fixtures, and abstention rate with a negative control so the metric cannot be vacuously perfect. Pytest-pinned, so it gates every PR.

Numeric output has a deterministic pass too (`check_composer_numbers`), but it is a **warning, not a gate** — see ADR 0024. Across four narrowings it never caught a real fabrication while repeatedly withholding correct replies; the entailment verifier and citation checker are what actually enforce grounding.

*Acceptance:* zero unresolvable source refs can reach the committed ledger (enforced + tested); planted-fact and fabricated-citation probes pass at 100% in CI.

**Phase 3 — Knowledge layer + full eval — IN PROGRESS (~45–65 h):** *Started 2026-08-27. **Landed:** the deterministic criteria-scorer framework (`knowledge/criteria.py`) with SLE 2019 EULAR/ACR; the LIRICAL v2.4.1 phenotype-only sidecar container and its parser (ADR 0029), now built and pushed by CI into its own ECR repository with an ECS task definition, though **not yet wired into the review DAG**; a measured assessment of which archived genomic data is admissible (ADR 0030 — the raw array only; the imputed BCFs carry no per-variant quality metric and are excluded).*

*Already present from earlier phases and reusable: `adoc eval --candidate` with its comparison report and suite registry (so the eval acceptance criterion needs two new suites, not a harness), and `EutilsPmidVerifier` (so PMID **verification** exists; PubMed **search** does not).*

*Acceptance status: `adoc eval --candidate` (met, pre-existing); criteria render itemized (met — a `criteria_scan` DAG node scores every registered set over labs plus the phenotype record, and the review report renders each criterion with its weight and basis); LLM vs LIRICAL divergence adjudication (**met** — ADR 0036. Both engines run in the review DAG, and an `engine_adjudication` node now requires a direction (corroborates / opposes / neutral) plus a per-divergence rationale, contract-enforced; `apply_engine_diff` maps those to ledger ops in plain code and writes its own diff. Scores are never combined across engines — a likelihood ratio and a Resnik similarity are not commensurable — so combination happens at the level of direction. `neutral` emits no op, deliberately: a phenotype-only engine that never ranked a hypothesis has not refuted it, and reading that as opposition would feed the retirement pass counter-evidence against every hypothesis whose support lives in a modality the engine cannot see); every literature claim carries a PMID (met — `knowledge/pubmed.py` searches E-utilities and a `literature_refresh` node cites the top hypotheses, each line carrying its PMID; citations now come FROM PubMed rather than from model recall).*

*Meeting the four acceptance lines is NOT the same as finishing phase 3 — the scope below is wider than the criteria, and the "Remaining" list is the authoritative one.*

*Remaining: **none** — every item below is done. Phase 3 is complete; phase 4
is next, and its scope is listed under "Phase 4 — Extras" below.*

*Closed items, kept for the reasoning:*

- ~~***LIRICAL into divergence adjudication.***~~ **Done** (ADR 0036). Both engines ran and were rendered before this; neither could change anything, because both nodes sit after `apply_review_diff`. `engine_adjudication` + `apply_engine_diff` close that: direction-only combination, evidence-only ops, a mandatory rule-out and a cap of 3 on adoptions per review, and engine agreement recorded deterministically with no model call. New `engine:<engine>:<date>` citation scheme, a closed set unlike every other slug in the grammar.
- ~~***ICAP ANA-pattern mapping.***~~ **Done** — `knowledge/icap.py`, riding along with the `criteria_scan` node rather than taking a node of its own, and rendered in the review report.
- ~~***Monarch sem-sim, and the Monarch KG / Orphadata / StatPearls chat tools.***~~ **Done** — sem-sim runs as the second review engine; Mondo, Orphadata, StatPearls and the disease-lookup chat tool are built (PRs #254–#256).
- ~~***The deterministic genomics artifact** (ADR 0030 scoped it; raw array only).*~~ **Done** (PR #257) — 5 markers, all called against the real array.
- ~~***The two eval suites** — rare-disease cohort differential-recall, and retrospective self-case replay.*~~ **Done**, with one deliberate substitution. *Differential-recall* (`adoc eval --suite rare_disease_recall`) builds 40 simulated patients from HPO disease annotations — 4 of the disease's own terms plus 2 from an unrelated disease — the way LIRICAL and Exomiser are themselves benchmarked, since no labelled cohort exists for one patient. Measured on the 2026-06-23 release: **recall@1 0.225, recall@3 0.400, recall@10 0.525**, median rank 2 when found; gated at 0.40, below the baseline so an ontology release does not cry wolf. *Self-case replay* (`--suite self_case_replay`) does **not** do what item (d) above describes: that design waits on a doctor-confirmed finding to replay against, and there is none. Replaying an undiagnosed case against itself would score nothing. It instead pins reproducibility and internal consistency of the deterministic layers over the real ledger — size bound, evidence coverage, can't-miss populated, retirement idempotent, ADR 0035's protections held — which is what a model rotation actually needs gated. It skips visibly when no data repo is present, **and when the ledger is empty**: every check is a bound or a floor, so the first run reported six green cases against an 83-byte ledger header. Vacuous now reads as skipped, not as passed.*
- ~~***Further classification scorers.***~~ **Seven implemented** (SLE 2019, Sjögren 2016, RA 2010, GPA 2022, EGPA 2022, MPA 2022, Behçet ICBD 2014). The 2022 ACR/EULAR vasculitis criteria come as a set of three and encoding only GPA left the other two arms of the same decision unmodelled: they reuse GPA's proven ANCA and eosinophil mappings, and one CBC differential now moves the trio in opposite directions (+5 EGPA, −4 GPA and MPA), which is far more informative than any one of them alone. Behçet ICBD 2014 is the first set reading NO labs — there is no serological marker for it, so a clinically diagnosed condition would otherwise be invisible to this layer however well the record described it; it reports `points_possible` against the threshold and never claims classification from text-matched findings. Still deliberately unwritten: myositis (needs CK, zero analytes on file) and APS (needs β2GP1 and lupus anticoagulant, both absent) would score nothing but blanks.*

**Original scope:**  patient HPO phenotype profile; LIRICAL (phenotype-only) + Monarch sem-sim as independent differential engines rendered alongside the LLM panel in deep reviews with divergence adjudication; Monarch KG SQLite + Orphadata + phenotype.hpoa + Mondo as chat tools; ~10 hand-encoded ACR/EULAR classification scorers (SLE 2019, Sjögren 2016, SLICC, CASPAR, myositis, ANCA vasculitides…) + ICAP ANA-pattern mapping, computed deterministically from labs+phenotype, always labeled "classification, not diagnostic, criteria"; PubMed E-utilities with PMID-linked citations; StatPearls/GeneReviews local FTS5; `adoc eval` gains the rare-disease-cohort differential-recall suite and the retrospective self-case replay, enabling gated model rotation.
*Acceptance:* reviews show LLM vs LIRICAL differentials with explicit divergence adjudication; criteria render itemized (points, threshold); every literature claim carries a PMID; `adoc eval --candidate` produces an incumbent-vs-candidate comparison report from a single command.

**Between phases 3 and 4 — the adversarial-review adoption track.** An
external adversarial review (2026-09-01) produced 20-odd findings across
patient experience and clinical logic. Regrouped by what actually shares
code, and accepted in this order — each one ADR'd, because each changes a
contract:

| ADR | Theme | Findings it closes | Status |
|---|---|---|---|
| 0038 | How a hypothesis ends | CLN-05, CLN-01, PAT-03 | shipped (v0.27.0) |
| 0039 | How a review reads | PAT-01, PAT-02, PAT-04 | merged to develop |
| 0040 | What the composer sounds like | PAT-08 | in review (#293) |
| 0041 | The appointment agenda | PAT-07 | this branch |
| 0042 | Longitudinal lab positivity | CLN-03 | queued |

Three findings were rejected on review rather than adopted, and the ADRs say
why: PAT-01's proposed `patient_summary` LLM node (a fourth frontier call
that could contradict the report beneath it — ADR 0039), averaging engine
scores across LIRICAL and sem-sim (not commensurable — ADR 0036), and
PAT-08's "Your Case Co-Pilot" persona (PLAN.md risk 3 — a co-pilot claims
shared authority over a decision this system must never appear to share;
ADR 0040).

Several premises turned out to be false on measurement, which is why each
item gets its own review before adoption: PAT-01 says the report opens with
operational metrics — it opens with `## What changed this week`. PAT-08 asks
for a persistent footer disclaimer, which `base.html` has rendered on every
page since the web UI shipped, and calls the composer prompt sterile when it
already mandates "plain, compassionate language". Measuring PAT-08 anyway
found two real things it did not mention: the classification disclaimer said
seven times per report, and `CriteriaResult.citation` rendered nowhere.

**Phase 4 — Extras.** Nothing here is started. In rough order of value to
the patient:

1. **Apple Health Records import.** She connects her portals once, exports
   the "Export All Health Data" zip to Dropbox, and `apple_health.py` parses
   `clinical-records/` FHIR into labs and encounters, deduped against PDF
   rows. PDFs stay the richer source for autoimmune serology narratives —
   this is for breadth and for results that never arrived as documents.
2. **One-page appointment-prep export.** The review already produces "what to
   ask your doctor"; this makes it something she can hand over.
   **Delivered early, ADR 0041** — `casefile/export.py` and
   `GET /export/agenda`, one page with a code-enforced line budget.
3. **Specialist finder** via the NPPES NPI registry — taxonomy, location and
   insurance fit.
4. **Medication and supplement interaction flags.** Read-only and
   informational; the output gate in `reason/safety.py` still applies.
5. **Email notifications** (SES) when a review lands or the confirm queue has
   work.
6. **Genetics beyond the curated panel**, only if sequencing ever happens —
   Exomiser needs a VCF; Phen2Gene works from phenotypes today.
7. **Vectors, only if FTS5 demonstrably misses something.** Not before.

*Known open issues carried into phase 4:*

- An intermittent `tests/test_web_*` failure, roughly 1 run in 6 under load,
  a different file each time and always "expected content missing from HTML".
  Test order is not randomised, so it is state or timing. Did not reproduce
  in 10 clean runs on an idle machine.
- ~~`rule_out` is empty on every active hypothesis~~ — ADR 0038 makes it
  evaluable (`rule_out_check`) and fixes `UpdateHypothesis.rule_out`, which
  was declared and silently dropped by `apply_diff`, so existing hypotheses
  can now acquire one. They still have to acquire one: the field is empty on
  all 46 until a review or a human fills it.
- The `most-likely` tier has been empty for 15 ledger versions. The research
  note asks that this be deliberate and stated rather than silent (part 3d).
  ADR 0039's `## The short version` now states it in words when it happens
  ("no lead is currently rated likely enough to lead with") rather than
  rendering an empty heading, but the underlying emptiness is unchanged.
- PubMed returns 0 citations for the current differential; no `pmid:` ref
  exists anywhere in the ledger.
- `depends_on` declares one edge while nodes read many, and execution is
  sequential — see `docs/dag-topology.md`.

## Key risks

1. **Extraction integrity** is the top data risk — design optimizes for zero silent errors (cross-model double-pass + validation + queue). **Measured on the first full rebuild (2026-08-26, 122 documents / 2081 rows):** 32% queued, and of 524 rows the reviewer marked `corrected`, **522 kept a value identical to one of the two extraction passes** — the reviewer was choosing between passes, not fixing wrong values. Extraction accuracy is therefore high; the queue is driven by cross-pass *disagreement*, dominated by `specimen_mismatch` (165 of 673) where one pass read a section header the other missed. That is mechanical and fixable in code, and it is the highest-value reduction in manual work available. Note also that `corrected` conflates "I picked pass B" with "I fixed a wrong value", so it is a poor signal for extraction quality — treat it with suspicion.
2. **Self-anchoring over time** (the ledger anchoring the system): mitigated structurally — DAG contracts make the Challenger and ledger-blind nodes non-skippable, the challenger and blind panel run on different model families, and Phase 2's non-LLM engines (LIRICAL/Monarch/criteria scorers) add a mechanistically independent check; churn/age/kill-rate metrics printed in every review make stagnation visible.
3. **Over-trust/framing drift:** steward language lives in one template + one deterministic gate, both pinned by the red-team suite as a required CI check — prompt edits that weaken safety behavior cannot merge.
4. **PHI:** unredacted documents go to the API (required for vision; per accepted privacy posture). Standard API data isn't trained on; note Fable-tier models mandate 30-day retention — `claude-opus-5` doesn't.
5. **Expectation ceiling:** exact-hit rate on true odyssey cases is low in the best published study — the product's honest job is *leads, structure, and preparation*, and it should say so in the UI.

## Verification

- CI on every PR: ruff, mypy, `pytest` — validators, ledger invariants, DAG node contracts (skipped-Challenger run must fail), reconciliation, safety gates, intake state machine, criteria scorers — against golden fixtures (synthetic lab PDFs + golden extractions + golden diffs + red-team transcript).
- End-to-end: `adoc init` → `adoc onboard` with a scripted persona → drop 5 verified historical PDFs in inbox → `adoc ingest` → confirm-queue precision check → chat session exercising informational + diagnostic turns → `adoc review` → inspect committed review, blind-panel divergence adjudications, and git history → `adoc eval` produces a scored report.
- Deploy check on ECS Fargate: scheduled ingest/review/backup tasks fire, Dropbox round-trip works, UI reachable via the public ALB, S3 backup restores to a working data repo on EFS.
