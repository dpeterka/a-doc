"""Phase 3 knowledge layer: deterministic, non-LLM clinical reasoning.

Everything in this package is plain code with unit tests (CLAUDE.md:
"deterministic logic is never delegated to a model"). Its purpose is to give
the deep review a **mechanistically independent** check on the LLM panel —
the third leg of the anti-anchoring design, alongside the cross-family
Challenger and the ledger-blind panel (PLAN.md "Anti-anchoring").
"""
