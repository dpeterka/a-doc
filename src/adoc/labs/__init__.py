"""Labs slice: sqlite store, models, deterministic validation, read queries.

See PLAN.md "Key schemas: labs table" and "State". Submodules: `models`
(Pydantic v2 schemas), `db` (sqlite DDL/FTS5/migrations/JSONL export),
`validate` (deterministic, non-LLM validation), `queries` (read-side
helpers for chat tools + UI).
"""
