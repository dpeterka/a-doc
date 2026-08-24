# 0005. Cross-family model bindings for Challenger and blind panel

Status: Accepted (amended 2026-08-23: `gpt-5.2-thinking` does not exist as a model id — thinking depth is the `reasoning_effort` request parameter on `gpt-5.2`; the Featherless panelist is `deepseek-ai/DeepSeek-R1-0528`. All bindings live-verified returning parsed structured output.)

## Context

Anchoring is the #1 predicted failure mode for this use case (PLAN.md
"Research conclusions" #3): LLMs measurably anchor on and are sycophantic
toward a user-presented theory. A Challenger stage implemented with the
*same* model family as the Ledger-Maintainer risks sharing that model
family's blind spots — a model is unlikely to reliably catch its own
correlated failure modes. Cross-family adversarial review (a different
model family attacking the first model's reasoning) does not share those
blind spots by construction.

## Decision

Model role -> provider/model bindings live in `models.yaml` (config, not
code — CLAUDE.md rule 4), and the roles are deliberately assigned across
model families:

- `primary_reasoner` (Ledger-Maintainer, Composer, chat): Anthropic
  (`claude-opus-5`).
- `challenger`: OpenAI (`gpt-5.2`) — a different family from the
  primary reasoner, by design, so it can attack the primary's reasoning
  without inheriting its blind spots.
- `blind_panel` (weekly review): three families — Anthropic, OpenAI, and a
  Featherless-hosted open model — each producing an independent de novo
  differential with the ledger withheld from its context (enforced by a DAG
  node precondition, see ADR 0002).
- `extractor_pass_a` / `extractor_pass_b`: cross-family double-pass
  extraction (Anthropic PDF-block pass, OpenAI page-PNG pass), so
  correlated extraction errors are far less likely than running the same
  model twice.
- `classifier`: a cheap, fast model (Anthropic Haiku) for latency-sensitive
  routing, where cross-family adversarial value doesn't apply.

`config.load_model_bindings()` normalizes every role to a list of bindings
(`dict[str, list[ModelBinding]]`), even single-model roles, so the loader
has one uniform contract regardless of whether a role is single- or
multi-bound.

## Consequences

- Changing any binding is a git-tracked, human-approved edit to
  `models.yaml`, backed by an `adoc eval --candidate` comparison report —
  never a silent model upgrade (CLAUDE.md rule 4, PLAN.md "Model rotation").
- The provider adapter (`reason/client.py`) must support both the
  Anthropic SDK and an OpenAI-compatible client (covering OpenAI and
  Featherless) behind one interface, including structured output, PHI
  scrubbing, and cost/audit logging — this is Phase-1 scope, not Phase 0.
- Running three model families in the weekly blind panel is a deliberate,
  budgeted cost (PLAN.md estimates $8-20/deep review) in exchange for a
  mechanistically independent anti-anchoring check, on top of the
  per-turn Challenger.
