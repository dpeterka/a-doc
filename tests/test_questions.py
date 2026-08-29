"""The open-question store (ADR 0033).

Before this store existed, a next-appointment question was a rendering, not a
thing: `questions-open.md` was rewritten wholesale by every review, so a
question had no identity and nothing could record that the patient had
answered it. She would answer in chat, the answer was captured as a fact, and
the next review asked again.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from adoc.casefile.questions import (
    OpenQuestion,
    OpenQuestions,
    load_questions,
    mark_answered,
    merge_proposed,
    question_id,
    save_questions,
)

_FIRST = date(2026, 8, 1)
_LATER = date(2026, 9, 5)


def _question(panel: str, *, audience: str = "you") -> OpenQuestion:
    return OpenQuestion(
        id=question_id(panel),
        panel=panel,
        ask=f"Please tell us about {panel}.",
        audience=audience,  # type: ignore[arg-type]
        first_asked_on=_FIRST,
        last_asked_on=_FIRST,
    )


def test_id_is_stable_for_the_same_panel() -> None:
    """Two reviews proposing the same panel must land on the same question,
    or every review opens duplicates of everything it asked last time."""
    assert question_id("Celiac screen: tTG-IgA + total IgA") == question_id(
        "  Celiac Screen: tTG-IgA + Total IgA  "
    )


def test_id_never_empties_out() -> None:
    """A panel of pure punctuation must still produce a usable key rather
    than an empty string that would collide with every other empty one."""
    assert question_id("???") == "unnamed"


def test_answering_survives_the_next_review_reproposing_it() -> None:
    """The whole point. The chooser reasons from the ledger and has no memory
    of what she said between reviews; the store does."""
    question = _question("Your supplement labels")
    store = OpenQuestions(questions=[question])

    answered, _ = mark_answered(store, [question.id], on=_LATER, note="Takes biotin 10mg.")
    after_review = merge_proposed(answered, [question], asked_on=_LATER)

    assert after_review.by_id(question.id).status == "answered"
    assert after_review.open_questions() == []


def test_reproposing_refreshes_wording_but_not_state() -> None:
    """Newer phrasing is the better one to show if it ever reopens, but the
    status is the store's to keep, never the model's to overwrite."""
    question = _question("Your supplement labels")
    answered, _ = mark_answered(
        OpenQuestions(questions=[question]), [question.id], on=_LATER, note="n"
    )

    reworded = question.model_copy(update={"ask": "List every supplement and its dose."})
    after = merge_proposed(answered, [reworded], asked_on=_LATER)

    assert after.by_id(question.id).ask == "List every supplement and its dose."
    assert after.by_id(question.id).status == "answered"
    assert after.by_id(question.id).last_asked_on == _LATER


def test_a_question_the_chooser_stops_proposing_is_not_deleted() -> None:
    """The ledger moves between reviews and an item can drop out of one run
    and return in the next. Deleting would lose the answer and re-ask from
    scratch."""
    question = _question("Your supplement labels")
    answered, _ = mark_answered(
        OpenQuestions(questions=[question]), [question.id], on=_LATER, note="n"
    )

    after = merge_proposed(answered, [], asked_on=_LATER)

    assert after.by_id(question.id) is not None
    assert after.by_id(question.id).status == "answered"


def test_an_unknown_id_is_reported_not_raised() -> None:
    """ADR 0028: one bad identifier costs its own claim and never the rest of
    the payload. A model that invents an id must not discard the real
    answers alongside it."""
    real = _question("Your supplement labels")
    store = OpenQuestions(questions=[real])

    updated, unknown = mark_answered(store, ["invented-id", real.id], on=_LATER)

    assert unknown == ["invented-id"]
    assert updated.by_id(real.id).status == "answered"


def test_answering_twice_keeps_the_first_answer() -> None:
    """A later message touching the same topic must not overwrite the date
    and note that actually closed it."""
    question = _question("Your supplement labels")
    once, _ = mark_answered(
        OpenQuestions(questions=[question]), [question.id], on=_LATER, note="first"
    )
    twice, _ = mark_answered(once, [question.id], on=date(2026, 10, 1), note="second")

    assert twice.by_id(question.id).answered_on == _LATER
    assert twice.by_id(question.id).answer_note == "first"


def test_open_questions_filters_by_audience() -> None:
    """The questions she can answer herself are the ones a chat message
    plausibly closes, so they are shown to the capture pass first."""
    mine = _question("Your supplement labels", audience="you")
    theirs = _question("Repeat FSH/LH/estradiol", audience="doctor")
    store = OpenQuestions(questions=[mine, theirs])

    assert [q.id for q in store.open_questions("you")] == [mine.id]
    assert [q.id for q in store.open_questions("doctor")] == [theirs.id]
    assert len(store.open_questions()) == 2


def test_round_trips_through_disk(tmp_path: Path) -> None:
    question = _question("Your supplement labels")
    answered, _ = mark_answered(
        OpenQuestions(questions=[question]), [question.id], on=_LATER, note="Takes biotin."
    )
    path = tmp_path / "case" / "questions-open.yaml"

    save_questions(path, answered)
    reloaded = load_questions(path)

    assert reloaded.by_id(question.id).status == "answered"
    assert reloaded.by_id(question.id).answered_on == _LATER
    assert reloaded.by_id(question.id).answer_note == "Takes biotin."


def test_a_missing_file_is_an_empty_store(tmp_path: Path) -> None:
    """A repo that has never had a review must not fail to load."""
    assert load_questions(tmp_path / "nope.yaml").questions == []
