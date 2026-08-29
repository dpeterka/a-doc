"""Informational-turn tool support (PLAN.md "Session loops (b)", informational route).

`query_labs`/`search_case`/`search_documents`/`list_encounters` are
deterministic, no-LLM retrieval helpers — `search_documents`
(docs/adr/0015-document-text-corpus.md) is the document-TEXT-layer one,
over `LabsDb.search_document_text`'s FTS5 index. `answer_informational` is a
thin wrapper around `informational_llm_result`: every deterministic
retrieval helper runs unconditionally over the question text, and their
results are folded into ONE LLM call alongside a full context pack
(`include_ledger=True` — informational answers are read-only and never
mutate the ledger, but the patient may reasonably ask about it). There is
no automated emergency screening anywhere in this module (see
`docs/adr/0021*.md` for why).

`informational_llm_result` itself runs `safety.treatment_gate` on the
model's answer (CLAUDE.md rule 5) — see its docstring for the
gate-guided-rewrite-then-withhold shape, mirroring `stages.composer_stage`.
Gating lives here, at the source, rather than in `answer_informational` or
in `reason/stages.py`'s `run_informational_turn`, so every caller of
`informational_llm_result` inherits the gate automatically.

This module also exposes `redact_gated_text`, a render/generation-time
helper used outside this module's own call path (`web/routes/ledger.py`,
`web/routes/reviews.py`, `reason/review.py`) to gate OTHER model-written
free text that reaches the patient outside the Composer/informational
paths — ledger evidence claims, discriminators, challenger notes, and
weekly-review markdown. Unlike the withhold-the-whole-reply shape above,
it replaces only the offending span(s) with a short marker, since that
text sits alongside a lot of legitimate, unrelated content a blanket
withhold would needlessly destroy.

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
from adoc.reason.safety import GateResult, treatment_gate

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


# Bump on any semantic edit (see CLAUDE.md "Prompt versioning"). This prompt
# is not stamped onto a persisted artifact today — an informational reply goes
# to the chat transcript, not to the ledger — so the constant exists to date
# the wording, not to satisfy a provenance contract.
INFORMATIONAL_PROMPT_VERSION = "2"

# Voice matters as much as content here. The first version of this prompt was
# task and safety constraints only, with nothing about WHO is being addressed;
# it said "the patient's own case file" but never "you are talking to the
# patient". The model filled the gap by describing the system to a peer, and
# she was told things like "the differential ledger holds 28 active entries"
# and that four documents were "marked pending review" — internal vocabulary
# that means nothing to her and, in the second case, was not even true of what
# she could ask for.
_INFORMATIONAL_SYSTEM = (
    "You are a-doc, talking directly to the patient about their own case "
    "file. They are not a clinician and not an engineer. Write in the second "
    "person, in plain language, the way a careful person would explain "
    "something to a friend who is worried and paying close attention.\n\n"
    "Never expose the machinery. Do not mention the ledger, the context pack, "
    "retrieval, nodes or stages, encounter IDs, file names, internal status "
    "strings, or how you are built. If you need to refer to what is on file, "
    "name the thing itself — 'your August blood work', 'the MRI report' — not "
    "the record that holds it. Do not describe your own capabilities as a list "
    "of functions unless asked what you can do, and then answer in one or "
    "two sentences about what they can ask for.\n\n"
    "Expand any medical term the first time you use it, in a few words, "
    "without being condescending. Prefer their words for their symptoms.\n\n"
    "This is a read-only, informational question. Answer only from what you "
    "are given; never produce a diagnosis, a probability judgment, or any "
    "treatment or dosing advice. If what you have does not answer the "
    "question, say so plainly rather than guessing — and say what would "
    "answer it, if you can tell."
)

_GATE_BLOCKED_MESSAGE = (
    "I drafted an answer, but it included specific treatment or dosing language, "
    "which this tool never passes on directly — so I'm withholding it rather than "
    "showing it to you. {rewrite_instruction}"
)

# How many completions `informational_llm_result` may spend on one answer:
# the first attempt plus one gate-guided rewrite — same budget and shape as
# `stages.composer_stage`'s `_COMPOSER_GATE_ATTEMPTS` (see that constant's
# comment for why one rewrite pass is worth it before withholding: a
# legitimate answer that merely restates a dose the patient already takes
# can usually be rephrased without it).
_INFORMATIONAL_GATE_ATTEMPTS = 2

_WITHHELD_MARKER = "[withheld: this passage failed a-doc's dosing/treatment safety gate]"


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
    deterministic retrieval results. This function always makes at least
    one call.

    `safety.treatment_gate` screens the answer HERE, at the source
    (CLAUDE.md rule 5 — no treatment/dosing advice may reach the patient),
    rather than leaving it to whichever caller happens to invoke this
    function: `reason.stages.run_informational_turn` (the production
    `web/routes/chat.py` path) previously called this function directly
    with no gate at all. Gating at the source means every current and
    future caller inherits it.

    Mirrors `stages.composer_stage`'s gate-guided rewrite loop: on a gate
    failure, the model gets ONE rewrite attempt (fed the blocked phrases
    and `GateResult.rewrite_instruction` as targeted feedback) before the
    answer is withheld outright. This is a quality loop, not the
    enforcement point — the returned `LlmResult.text` is guaranteed to
    pass `treatment_gate` (or be the fixed withheld message) either way,
    so no caller needs to gate it again.
    """
    context_pack = build_context(repo, db, include_ledger=True)
    retrieval = _deterministic_retrieval(repo, db, question)
    user_content = (
        f"{context_pack.render()}\n\n## Deterministic Retrieval Results\n\n{retrieval}\n\n"
        f"## Patient Question\n\n{question}\n"
    )
    messages: list[Message] = [Message(role="user", content=user_content)]
    result: LlmResult | None = None
    gate: GateResult | None = None
    for _attempt in range(_INFORMATIONAL_GATE_ATTEMPTS):
        result = client.complete(
            "primary_reasoner",
            system=_INFORMATIONAL_SYSTEM,
            messages=messages,
        )
        gate = treatment_gate(result.text)
        if gate.passed:
            return result
        offending = "; ".join(f"{span.text!r} ({span.reason})" for span in gate.spans)
        messages = [
            *messages,
            Message(role="assistant", content=result.text),
            Message(
                role="user",
                content=(
                    f"{gate.rewrite_instruction} The blocked phrases were: {offending}. "
                    "Reporting a dose the patient already takes counts as blocked dosing "
                    "language - describe the medication or supplement WITHOUT its dose. "
                    "Return the complete corrected answer."
                ),
            ),
        ]

    assert result is not None
    assert gate is not None
    withheld_text = _GATE_BLOCKED_MESSAGE.format(rewrite_instruction=gate.rewrite_instruction or "")
    return result.model_copy(update={"text": withheld_text})


def answer_informational(client: LlmClient, repo: DataRepo, db: LabsDb, question: str) -> str:
    """Entry point for the informational-turn MVP tool loop (PLAN.md loop (b)).

    Delegates entirely to `informational_llm_result`, which runs
    `safety.treatment_gate` itself — this function does not duplicate that
    check. No automated emergency screening (see `docs/adr/0021*.md` for
    why).
    """
    return informational_llm_result(client, repo, db, question).text


def redact_gated_text(text: str) -> str:
    """Run `safety.treatment_gate` over `text` and replace only the
    offending span(s) with a short, fixed marker, preserving everything
    else — unlike `informational_llm_result`'s withhold-the-whole-answer
    shape, this is for model-written free text that is rendered ALONGSIDE
    a lot of unrelated, legitimate content: ledger evidence claims and
    discriminators, hypothesis challenger notes (`web/routes/ledger.py`),
    and weekly-review markdown, both at generation time (`reason/review.py`)
    and at render time for already-persisted reviews
    (`web/routes/reviews.py`) — see CLAUDE.md rule 5.

    Adjacent/overlapping spans (e.g. a dosage-pattern span nested inside a
    wider imperative-treatment-instruction span) are merged into one
    replacement so the marker never appears twice back-to-back.
    """
    if not text:
        return text
    gate = treatment_gate(text)
    if gate.passed:
        return text

    merged: list[list[int]] = []
    for span in gate.spans:
        if merged and span.start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], span.end)
        else:
            merged.append([span.start, span.end])

    pieces: list[str] = []
    cursor = 0
    for start, end in merged:
        pieces.append(text[cursor:start])
        pieces.append(_WITHHELD_MARKER)
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces)


__all__ = [
    "answer_informational",
    "informational_llm_result",
    "list_encounters",
    "query_labs",
    "redact_gated_text",
    "search_case",
    "search_documents",
]
