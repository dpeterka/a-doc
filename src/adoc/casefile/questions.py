"""Open next-appointment questions as a durable, resolvable record.

The next-appointment list used to exist only as `case/questions-open.md`,
rewritten wholesale by every review. That made a question a rendering, not a
thing: it had no identity, so nothing could say "she answered that one". She
would answer in chat, the answer was captured as a fact, and the next review
regenerated the list from the ledger and asked again — because nothing had
ever recorded that the question was closed.

This module makes the list a store. A question gets a stable id, a status,
and (when answered) the date and a short note. `questions-open.md` becomes a
RENDERING of this store rather than the record itself — the rule from ADR
0032's addendum: a derived artifact is never the read path for data that has
a source of truth.

The id is derived from the panel text rather than assigned randomly, so a
review that re-proposes the same panel lands on the same question and
inherits its answered state instead of opening a duplicate. Rewording the
panel does break the link — that is a real limit, and the merge below is
deliberately conservative about it: an unrecognised id opens a new question
rather than guessing which old one it meant.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from ruamel.yaml import YAML

QUESTIONS_RELPATH = "case/questions-open.yaml"

_SLUG_RE = re.compile(r"[^a-z0-9]+")

QuestionStatus = Literal["open", "answered"]
Audience = Literal["doctor", "you"]


def question_id(panel: str) -> str:
    """A stable id for a panel.

    Deterministic so the same panel proposed by two consecutive reviews is
    the same question. Truncated because these ids are shown to a model and
    echoed back — ADR 0028's rule that a model asked to reproduce an
    identifier must be shown a short, copyable one.
    """
    slug = _SLUG_RE.sub("-", panel.strip().lower()).strip("-")
    return slug[:60] or "unnamed"


class OpenQuestion(BaseModel):
    """One next-appointment item, with the state the markdown could not hold."""

    id: str
    panel: str
    ask: str = ""
    why: str = ""
    audience: Audience = "doctor"
    hypothesis_ids: list[str] = Field(default_factory=list)

    status: QuestionStatus = "open"
    first_asked_on: date
    last_asked_on: date
    answered_on: date | None = None
    answer_note: str = ""
    """One patient-grounded sentence recording what closed it — not the
    model's gloss on what the answer implies clinically."""

    @property
    def is_open(self) -> bool:
        return self.status == "open"


class OpenQuestions(BaseModel):
    """The full `questions-open.yaml` document."""

    schema_version: Literal[1] = 1
    questions: list[OpenQuestion] = Field(default_factory=list)

    def open_questions(self, audience: Audience | None = None) -> list[OpenQuestion]:
        return [
            q for q in self.questions if q.is_open and (audience is None or q.audience == audience)
        ]

    def by_id(self, question_id: str) -> OpenQuestion | None:
        return next((q for q in self.questions if q.id == question_id), None)


def load_questions(path: Path) -> OpenQuestions:
    """Load the store; a missing file is an empty one."""
    if not path.is_file():
        return OpenQuestions()
    yaml = YAML(typ="safe")
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.load(fh)
    return OpenQuestions.model_validate(raw or {})


def save_questions(path: Path, questions: OpenQuestions) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    yaml = YAML()
    yaml.default_flow_style = False
    with path.open("w", encoding="utf-8") as fh:
        yaml.dump(questions.model_dump(mode="json"), fh)


def merge_proposed(
    existing: OpenQuestions,
    proposed: list[OpenQuestion],
    *,
    asked_on: date,
) -> OpenQuestions:
    """Fold a review's proposed items into the store, preserving state.

    An already-answered question that the chooser proposes again stays
    answered — that is the entire point of this store. Its wording is still
    refreshed, because the newer phrasing is the better one to show if it
    ever reopens, but `status`, `answered_on` and `answer_note` are the
    store's to keep, never the model's to overwrite.

    A question the chooser stops proposing is NOT deleted. The ledger moves
    between reviews and an item can drop out of one run and return in the
    next; deleting would lose the answer and re-ask from scratch.
    """
    merged = {q.id: q.model_copy(deep=True) for q in existing.questions}

    for item in proposed:
        current = merged.get(item.id)
        if current is None:
            merged[item.id] = item.model_copy(
                update={"first_asked_on": asked_on, "last_asked_on": asked_on}
            )
            continue
        current.panel = item.panel
        current.ask = item.ask
        current.why = item.why
        current.audience = item.audience
        current.hypothesis_ids = item.hypothesis_ids
        current.last_asked_on = asked_on

    return OpenQuestions(questions=sorted(merged.values(), key=lambda q: q.id))


def mark_answered(
    questions: OpenQuestions,
    ids: list[str],
    *,
    on: date,
    note: str = "",
) -> tuple[OpenQuestions, list[str]]:
    """Close the named questions. Returns the updated store and the ids that
    matched nothing.

    Unknown ids are REPORTED, not raised: ADR 0028's rule that one bad
    identifier costs its own claim and never the rest of the payload. A model
    that invents an id must not be able to discard the four real answers
    alongside it.
    """
    updated = questions.model_copy(deep=True)
    unknown: list[str] = []
    for question_id in ids:
        question = updated.by_id(question_id)
        if question is None:
            unknown.append(question_id)
            continue
        if question.status == "answered":
            continue
        question.status = "answered"
        question.answered_on = on
        question.answer_note = note.strip()
    return updated, unknown
