# 0001. Single reasoner with functional stages, not specialty personas

Status: Accepted

## Context

An obvious design for a diagnostic assistant is a multi-agent system of
specialty "personas" (e.g. a rheumatologist agent, a cardiologist agent)
that debate a case. The literature does not support this for this use case:
persona-ablation studies and MedAgentBoard show specialty personas add no
knowledge the base model didn't already have and can degrade accuracy;
debate-style multi-agent gains are roughly matched by self-consistency at
equal compute. What does measurably help is *functional* (cognitive)
decomposition of a single strong model's process — Microsoft's MAI-DxO
(arXiv 2506.22405) structures one model's reasoning as a Hypothesis
ledger / Test-Chooser / Challenger / Stewardship loop and beats the bare
model (81.9% vs 78.6% on NEJM cases) at lower cost, and clinically it beats
physician performance. Google AMIE's longitudinal-management work applies
a similar dialogue-agent-plus-deep-reasoning-pass split. MDAgents further
shows this decomposition should be applied *adaptively* — single-pass by
default, deliberation only when a case is hard or the process is stuck —
rather than always running the full multi-stage loop.

## Decision

a-doc runs **one primary frontier reasoner** through **functional stages
implemented as code-controlled passes**, not specialty personas:
Ledger-Maintainer → Challenger (mandatory, a separate call on a different
model family) → Test-Chooser → Composer/Steward. Depth is adaptive: a fused
single call by default, escalating to multi-call deliberation only when the
differential ledger is stuck or churning. Reasoning effort is also
adaptive: `high` for interactive chat, `xhigh` for the weekly deep review.

## Consequences

- Stage order and context assembly are enforced by the DAG runner
  (`reason/dag.py`), not by the model choosing to "act as" a persona — see
  ADR 0002.
- The Challenger stage exists specifically to counter anchoring (this
  patient's single biggest predicted failure mode) and must run on a
  different model family than the Ledger-Maintainer (see ADR 0005).
- Because there is no persona fan-out, per-turn cost stays bounded even
  though the weekly review's blind panel (multiple model families, each
  producing a de novo differential) intentionally reintroduces multi-model
  cost at a lower cadence, where it buys anti-anchoring value instead of
  persona theater.
