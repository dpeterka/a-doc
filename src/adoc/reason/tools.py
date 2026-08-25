"""Informational-turn tool support (PLAN.md "Session loops (b)", informational route).

`query_labs`/`search_case`/`search_documents`/`list_encounters` are
deterministic, no-LLM retrieval helpers — `search_documents`
(docs/adr/0015-document-text-corpus.md) is the document-TEXT-layer one,
over `LabsDb.search_document_text`'s FTS5 index. `answer_informational` is the
red-flag screen runs first (zero API calls on a match), then every
deterministic retrieval helper runs unconditionally over the question
text, and their results are folded into ONE LLM call alongside a full
context pack (`include_ledger=True` — informational answers are read-only
and never mutate the ledger, but the patient may reasonably ask about it).
`safety.treatment_gate` screens the model's answer before it is returned.

This is deliberately the simplest thing that satisfies PLAN.md's "one
streamed tool-runner call (query_labs / search_case / web literature)"
requirement for the informational route: every tool always runs and the
model never chooses which one to invoke or issues a follow-up call. A full
multi-step tool-runner (model-directed tool selection, possibly across
several turns) is future work — see PLAN.md "Reasoner integration": "Chat
tool-use ... runs inside a single whitelisted-tools node." This module is
that node's MVP body.
"""

from __future__ import annotations

import re

from adoc.casefile.encounters import read_encounter
from adoc.casefile.repo import DataRepo
from adoc.labs.db import LabsDb
from adoc.labs.models import LabResult
from adoc.labs.queries import trend_series
from adoc.labs.validate import canonicalize
from adoc.reason.client import LlmClient, LlmResult, Message
from adoc.reason.context import build_context
from adoc.reason.safety import RedFlagResult, guarded_turn, treatment_gate

_MAX_SERIES_POINTS = 8
_MAX_GREP_HITS = 10
_MAX_DOCUMENT_HITS = 5

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9\-]*")


def _candidate_tokens(question: str) -> list[str]:
    """1-, 2-, and 3-word n-grams from `question`, as candidates for
    `labs.validate.canonicalize`. Deliberately over-generates — canonicalize
    is a cheap dict lookup and a candidate that isn't a real analyte name
    simply fails to match, so there is no cost to trying too many."""
    words = _TOKEN_RE.findall(question)
    tokens: list[str] = list(words)
    tokens.extend(f"{a} {b}" for a, b in zip(words, words[1:], strict=False))
    tokens.extend(f"{a} {b} {c}" for a, b, c in zip(words, words[1:], words[2:], strict=False))
    return tokens


def _render_series(name: str, series: list[LabResult]) -> str:
    recent = series[-_MAX_SERIES_POINTS:]
    lines = [f"### {name}"]
    for row in recent:
        value = row.value_text if row.value is None else str(row.value)
        unit = f" {row.ucum_unit}" if row.ucum_unit else ""
        flag = f" [{row.flag.value}]" if row.flag else ""
        lines.append(f"- {row.date.isoformat()}: {value}{unit}{flag}")
    return "\n".join(lines)


def query_labs(db: LabsDb, question: str) -> str:
    """Deterministic lab lookup. No LLM call.

    Canonicalizes every candidate analyte token found in `question` via
    `labs.validate.canonicalize` and, for each match, returns that
    analyte's time-ordered series summary (most recent
    `_MAX_SERIES_POINTS` points).
    """
    matched: dict[str, str] = {}
    for token in _candidate_tokens(question):
        canonical = canonicalize(token)
        if canonical is None or canonical in matched:
            continue
        series = trend_series(db, canonical)
        if not series:
            continue
        matched[canonical] = _render_series(canonical, series)

    if not matched:
        return "No recognized lab analyte names were found in the question."
    return "\n\n".join(matched[name] for name in sorted(matched))


def _grep_case_files(repo: DataRepo, text: str, *, max_hits: int = _MAX_GREP_HITS) -> list[str]:
    needle = text.strip().lower()
    if not needle:
        return []
    hits: list[str] = []
    case_dir = repo.root / "case"
    if not case_dir.is_dir():
        return hits
    encounters_dir = case_dir / "encounters"
    paths = sorted(case_dir.glob("*.md"))
    if encounters_dir.is_dir():
        paths += sorted(encounters_dir.glob("*.md"))

    for path in paths:
        if len(hits) >= max_hits:
            break
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for lineno, line in enumerate(content.splitlines(), start=1):
            if needle in line.lower():
                hits.append(f"- `{path.relative_to(repo.root)}`:{lineno}: {line.strip()}")
                if len(hits) >= max_hits:
                    break
    return hits


def search_case(repo: DataRepo, db: LabsDb, text: str) -> str:
    """Labs FTS5 search (`LabsDb.search`) plus a plain substring grep over
    `case/*.md` and `case/encounters/*.md`. No LLM call."""
    sections: list[str] = []

    lab_hits = db.search(text)
    if lab_hits:
        lines = [f"### Labs matching {text!r}"]
        for row in lab_hits[:_MAX_SERIES_POINTS]:
            value = row.value_text if row.value is None else str(row.value)
            lines.append(f"- {row.date.isoformat()} {row.name}: {value}")
        sections.append("\n".join(lines))

    doc_hits = _grep_case_files(repo, text)
    if doc_hits:
        sections.append(f"### Case file matches for {text!r}\n" + "\n".join(doc_hits))

    if not sections:
        return f"No matches found in labs or case files for {text!r}."
    return "\n\n".join(sections)


def search_documents(db: LabsDb, query: str) -> str:
    """Full-text search over every ingested document's extracted text
    (`LabsDb.search_document_text`, docs/adr/0015-document-text-corpus.md).
    No LLM call. Snippets are returned VERBATIM with their `doc:<filename>
    #p<page>`-style source ref — never paraphrased, so the model can quote
    them and a later verifier can check them against the source.
    """
    hits = db.search_document_text(query, limit=_MAX_DOCUMENT_HITS)
    if not hits:
        return f"No document text matches found for {query!r}."
    lines = [f"### Document text matching {query!r}"]
    for hit in hits:
        snippet = hit.snippet.strip()
        lines.append(f"- {hit.source_ref}: {snippet}")
    return "\n".join(lines)


def list_encounters(repo: DataRepo, n: int) -> str:
    """The `n` most recent encounters, filename-descending — the same
    recency convention as `reason.context._recent_encounters_section`. No
    LLM call.
    """
    encounters_dir = repo.root / "case" / "encounters"
    if not encounters_dir.is_dir():
        return "_No encounters recorded yet._"
    filenames = sorted(
        (p.name for p in encounters_dir.iterdir() if p.suffix == ".md"), reverse=True
    )
    if not filenames:
        return "_No encounters recorded yet._"

    lines: list[str] = []
    for filename in filenames[:n]:
        encounter = read_encounter(encounters_dir / filename)
        fm = encounter.frontmatter
        provider = f" ({fm.provider})" if fm.provider else ""
        summary = encounter.summary.strip() or "_no summary_"
        lines.append(f"- **{fm.date.isoformat()}** [{fm.type}]{provider}: {summary}")
    return "\n".join(lines)


_INFORMATIONAL_SYSTEM = (
    "You are answering a read-only, informational question about the patient's own "
    "case file. You are given a context pack and this question's deterministic "
    "retrieval results (lab lookups, case-file search, recent encounters). Answer "
    "only from what is given; never produce a diagnosis, a probability judgment, or "
    "any treatment/dosing advice. If the context does not support an answer, say so "
    "plainly rather than guessing."
)

_GATE_BLOCKED_MESSAGE = (
    "I drafted an answer, but it included specific treatment or dosing language, "
    "which this tool never passes on directly — so I'm withholding it rather than "
    "showing it to you. {rewrite_instruction}"
)


def _deterministic_retrieval(repo: DataRepo, db: LabsDb, question: str) -> str:
    """Run every deterministic retrieval helper unconditionally (the MVP
    tool loop — see module docstring) and fold the results into one block."""
    return "\n\n".join(
        [
            f"### query_labs\n\n{query_labs(db, question)}",
            f"### search_case\n\n{search_case(repo, db, question)}",
            f"### search_documents\n\n{search_documents(db, question)}",
            f"### list_encounters (last 5)\n\n{list_encounters(repo, 5)}",
        ]
    )


def informational_llm_result(
    client: LlmClient, repo: DataRepo, db: LabsDb, question: str
) -> LlmResult:
    """The single LLM call behind the informational-turn MVP tool loop:
    a full context pack (`include_ledger=True`) plus this question's
    deterministic retrieval results. Callers are responsible for running
    the red-flag screen first (see `answer_informational` /
    `reason.stages.run_informational_turn`) — this function always makes
    the call.
    """
    context_pack = build_context(repo, db, include_ledger=True)
    retrieval = _deterministic_retrieval(repo, db, question)
    user_content = (
        f"{context_pack.render()}\n\n## Deterministic Retrieval Results\n\n{retrieval}\n\n"
        f"## Patient Question\n\n{question}\n"
    )
    return client.complete(
        "primary_reasoner",
        system=_INFORMATIONAL_SYSTEM,
        messages=[Message(role="user", content=user_content)],
    )


def answer_informational(client: LlmClient, repo: DataRepo, db: LabsDb, question: str) -> str:
    """Entry point for the informational-turn MVP tool loop (PLAN.md loop (b)).

    Red-flag screen first (zero API calls on a match — the flagged
    template is returned as plain text). On pass: one LLM call
    (`informational_llm_result`) over the context pack + deterministic
    retrieval results; `safety.treatment_gate` screens the answer before
    it is ever returned, exactly as CLAUDE.md rule 5 requires for any
    patient-facing output.
    """

    def _proceed() -> str:
        result = informational_llm_result(client, repo, db, question)
        gate = treatment_gate(result.text)
        if gate.passed:
            return result.text
        return _GATE_BLOCKED_MESSAGE.format(rewrite_instruction=gate.rewrite_instruction or "")

    outcome = guarded_turn(question, _proceed)
    if isinstance(outcome, RedFlagResult):
        return outcome.message or ""
    return outcome


__all__ = [
    "answer_informational",
    "informational_llm_result",
    "list_encounters",
    "query_labs",
    "search_case",
    "search_documents",
]
