"""The open-question store (ADR 0033).

Before this store existed, a next-appointment question was a rendering, not a
thing: `questions-open.md` was rewritten wholesale by every review, so a
question had no identity and nothing could record that the patient had
answered it. She would answer in chat, the answer was captured as a fact, and
the next review asked again.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from adoc.casefile.questions import (
    ANSWER_NOTE_MAX,
    MAX_ANSWERED_SHOWN,
    MAX_CHAT_ASKS,
    QUESTIONS_RELPATH,
    OpenQuestion,
    OpenQuestions,
    load_questions,
    mark_answered,
    merge_proposed,
    next_question_to_ask,
    question_id,
    record_chat_ask,
    render_for_context,
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

    answered, _, _closed = mark_answered(store, [question.id], on=_LATER, note="Takes biotin 10mg.")
    after_review = merge_proposed(answered, [question], asked_on=_LATER)

    assert after_review.by_id(question.id).status == "answered"
    assert after_review.open_questions() == []


def test_reproposing_refreshes_wording_but_not_state() -> None:
    """Newer phrasing is the better one to show if it ever reopens, but the
    status is the store's to keep, never the model's to overwrite."""
    question = _question("Your supplement labels")
    answered, _, _closed = mark_answered(
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
    answered, _, _closed = mark_answered(
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

    updated, unknown, _closed = mark_answered(store, ["invented-id", real.id], on=_LATER)

    assert unknown == ["invented-id"]
    assert updated.by_id(real.id).status == "answered"


def test_answering_twice_keeps_the_first_answer() -> None:
    """A later message touching the same topic must not overwrite the date
    and note that actually closed it."""
    question = _question("Your supplement labels")
    once, _, _closed = mark_answered(
        OpenQuestions(questions=[question]), [question.id], on=_LATER, note="first"
    )
    twice, _, _closed = mark_answered(once, [question.id], on=date(2026, 10, 1), note="second")

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
    answered, _, _closed = mark_answered(
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


# --- the loop that was never closed -----------------------------------------


def test_context_offers_open_questions_and_reports_answered_ones() -> None:
    """The reasoning stages read the STORE, not `questions-open.md`.

    The markdown was a rendering nothing regenerated, so it could not know
    what had been answered: production held 43 questions, all `open`, none
    ever answered, while the answers sat in the record as facts. Every review
    re-read the stale list and asked again.

    Ids are shown because a model that must close a question has to name it.
    """
    from adoc.casefile.questions import render_for_context

    mine = _question("Every supplement you take", audience="you")
    theirs = _question("ACTH stimulation test", audience="doctor")
    done = _question("Already answered", audience="you")
    store, _, _closed = mark_answered(
        OpenQuestions(questions=[mine, theirs, done]),
        [done.id],
        on=date(2026, 9, 1),
        note="told us",
    )

    rendered = render_for_context(store, today=date(2026, 9, 2))

    assert f"`{mine.id}`" in rendered
    assert f"`{theirs.id}`" in rendered
    assert rendered.index(mine.id) < rendered.index(theirs.id), "hers should come first"

    # The property this pins changed in ADR 0055, and the change is narrow.
    # It used to assert `done.id not in rendered` — the answered question must
    # not appear AT ALL. It now appears, in its own block, labelled answered
    # and carrying what she said.
    #
    # What must still hold is the thing that assertion was protecting: an
    # answered question is never offered as OPEN and never re-asked. The old
    # assertion enforced that by making the answer invisible, and the cost was
    # that no review could tell "she said no" from "we stopped asking".
    open_half = rendered.split("**Recently answered")[0]
    assert done.id not in open_half, "an answered question was offered as open"
    assert "told us" in rendered, "her answer is not in the pack"


def test_resolve_answered_closes_and_survives_a_bad_id(tmp_path: Path) -> None:
    """Shared by intake and the ordinary diagnostic turn. Never raises: a
    chat turn must not fail because a question could not be closed."""
    from adoc.casefile.questions import resolve_answered

    mine = _question("Every supplement you take")
    path = tmp_path / QUESTIONS_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    save_questions(path, OpenQuestions(questions=[mine]))

    closed = resolve_answered(
        tmp_path, [mine.id, "not-a-question"], on=date(2026, 9, 1), note="Answered in chat."
    )

    assert closed == [mine.id]
    reloaded = load_questions(path)
    assert reloaded.by_id(mine.id).status == "answered"
    assert reloaded.open_questions() == []


def test_resolve_answered_on_an_unwritable_store_returns_nothing(tmp_path: Path) -> None:
    """A turn must survive a broken store."""
    from adoc.casefile.questions import resolve_answered

    assert resolve_answered(tmp_path / "nope", ["x"], on=date(2026, 9, 1), note="") == []


# --- ADR 0048 §1: the chat follows up on questions that already exist -----------------------------


def _q(
    qid: str,
    *,
    audience: str = "you",
    status: str = "open",
    hypothesis_ids: list[str] | None = None,
    chat_asks: int = 0,
    first_asked: date = date(2026, 8, 29),
) -> OpenQuestion:
    return OpenQuestion(
        id=qid,
        panel=f"Panel {qid}",
        ask=f"Ask about {qid}?",
        audience=audience,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        hypothesis_ids=hypothesis_ids or [],
        chat_asks=chat_asks,
        first_asked_on=first_asked,
        last_asked_on=first_asked,
    )


def test_a_question_bearing_on_a_changed_lead_wins() -> None:
    """The most useful question is about the thing just being reasoned over.
    All 55 stored questions carry `hypothesis_ids`, so this is the primary
    key rather than an aspiration."""
    store = OpenQuestions(
        questions=[
            _q("older", first_asked=date(2026, 8, 1)),
            _q("relevant", hypothesis_ids=["sle-01"]),
        ]
    )

    chosen = next_question_to_ask(store, changed_hypothesis_ids={"sle-01"})

    assert chosen is not None and chosen.id == "relevant"


def test_a_doctor_question_is_never_asked_in_chat() -> None:
    """ "What did your rheumatologist say" is not something she can answer,
    and putting it at the end of a chat reply wastes the turn."""
    store = OpenQuestions(questions=[_q("doc-one", audience="doctor")])

    assert next_question_to_ask(store) is None


def test_an_answered_question_is_not_re_asked() -> None:
    store = OpenQuestions(questions=[_q("done", status="answered")])

    assert next_question_to_ask(store) is None


def test_the_backlog_is_spread_rather_than_hammered() -> None:
    """Fewest chat-asks first, so the top of the list is not asked three
    times while question 19 is never reached."""
    store = OpenQuestions(
        questions=[
            _q("asked-once", chat_asks=1, first_asked=date(2026, 8, 1)),
            _q("never-asked", chat_asks=0, first_asked=date(2026, 8, 29)),
        ]
    )

    chosen = next_question_to_ask(store)

    assert chosen is not None and chosen.id == "never-asked"


def test_the_oldest_breaks_a_tie() -> None:
    store = OpenQuestions(
        questions=[
            _q("newer", first_asked=date(2026, 9, 2)),
            _q("older", first_asked=date(2026, 8, 29)),
        ]
    )

    chosen = next_question_to_ask(store)

    assert chosen is not None and chosen.id == "older"


def test_a_question_asked_to_exhaustion_stops_being_asked() -> None:
    """There is no signal for a decline — she may answer something else, or
    nothing. So the chat stops after `MAX_CHAT_ASKS` and the question stays
    open for the doctor list. A third ask is nagging; dropping it from the
    record would lose a real question."""
    store = OpenQuestions(questions=[_q("tired", chat_asks=MAX_CHAT_ASKS)])

    assert next_question_to_ask(store) is None
    assert store.questions[0].is_open


def test_recording_an_ask_moves_it_down_the_queue() -> None:
    """Without this the same question is asked every turn forever."""
    store = OpenQuestions(questions=[_q("a"), _q("b")])

    first = next_question_to_ask(store)
    assert first is not None
    record_chat_ask(store, first.id, on=date(2026, 9, 4))
    second = next_question_to_ask(store)

    assert second is not None
    assert second.id != first.id
    assert next(q for q in store.questions if q.id == first.id).chat_asks == 1


def test_nothing_to_ask_returns_none_rather_than_inventing() -> None:
    """An empty backlog must not produce a filler question — that is what
    ADR 0048 §2's `gap_scan` is for, and it is a separate decision."""
    assert next_question_to_ask(OpenQuestions(questions=[])) is None


# --- ADR-pending Phase 1: her words, not a constant --------------------------


def test_mark_answered_reports_which_ids_newly_closed() -> None:
    """The idempotence key for turning an answer into evidence. Without it a
    re-review re-proposing the same panel would mint the evidence again."""
    first = _question("Your supplement labels")
    second = _question("Morning stiffness")
    store = OpenQuestions(questions=[first, second])

    once, _unknown, closed = mark_answered(store, [first.id], on=_LATER, note="biotin 10mg")
    assert closed == [first.id]

    _twice, _unknown2, closed_again = mark_answered(once, [first.id], on=_LATER, note="again")
    assert closed_again == [], "an already-answered question reported as newly closed"


def test_an_unknown_id_is_not_reported_as_closed() -> None:
    real = _question("Your supplement labels")

    _store, unknown, closed = mark_answered(
        OpenQuestions(questions=[real]), ["invented", real.id], on=_LATER
    )

    assert unknown == ["invented"]
    assert closed == [real.id]


def test_the_diagnostic_path_stores_her_words_not_a_constant() -> None:
    """`stages.py` wrote the literal "Answered in conversation." while the
    intake path wrote her text. `chat.py` runs the diagnostic turn FIRST and
    `mark_answered` skips an already-answered question, so the constant won
    and her words were discarded on every diagnostic turn.
    """
    import inspect

    from adoc.reason import stages as module

    source = inspect.getsource(module.ledger_maintainer_stage)

    assert "Answered in conversation." not in source
    assert "patient_message.strip()" in source


def test_the_answer_is_capped_at_the_length_intake_uses(tmp_path: Path) -> None:
    """280, matching `intake/agent.py`. Two different caps would be two
    different records of the same sentence."""
    question = _question("Your supplement labels")
    path = tmp_path / QUESTIONS_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    save_questions(path, OpenQuestions(questions=[question]))

    from adoc.casefile.questions import resolve_answered

    resolve_answered(tmp_path, [question.id], on=_LATER, note="x" * 400)

    assert len(load_questions(path).by_id(question.id).answer_note) == ANSWER_NOTE_MAX


def test_there_is_one_closer_not_two() -> None:
    """The two copies diverged, which is how the constant came to overwrite
    her words. `intake/agent.py` now calls the shared one."""
    import inspect

    from adoc.intake import agent

    source = inspect.getsource(agent)

    assert "_resolve_answered_questions" not in source
    assert "resolve_answered(" in source


# --- the answered block (ADR 0055) -------------------------------------------


def _answered(panel: str, *, on: date, note: str) -> OpenQuestion:
    q = _question(panel)
    store, _unknown, _closed = mark_answered(OpenQuestions(questions=[q]), [q.id], on=on, note=note)
    return store.questions[0]


def test_an_answer_inside_the_window_is_shown_with_what_she_said() -> None:
    """The whole point. Before this the review saw a question disappear and
    could not tell whether she had said yes, said no, or simply stopped being
    asked — `answer_note` was written and read nowhere in the codebase."""
    store = OpenQuestions(
        questions=[
            _answered("Your supplements", on=date(2026, 9, 1), note="biotin 10mg, nothing else")
        ]
    )

    rendered = render_for_context(store, today=date(2026, 9, 10))

    assert "Recently answered" in rendered
    assert "biotin 10mg, nothing else" in rendered
    assert "2026-09-01" in rendered


def test_an_old_answer_drops_out() -> None:
    """A year-old answer about a supplement she has since changed is worse
    than silence."""
    store = OpenQuestions(
        questions=[_answered("Your supplements", on=date(2025, 1, 1), note="biotin")]
    )

    assert "Recently answered" not in render_for_context(store, today=date(2026, 9, 10))


def test_an_answer_with_no_recorded_note_is_omitted() -> None:
    """The pre-fix backlog, closed when the diagnostic path stored a constant
    and the intake path's real text was discarded. Rendering `answered: ""`
    would read as though she had answered emptily."""
    store = OpenQuestions(questions=[_answered("Your supplements", on=date(2026, 9, 1), note="")])

    assert "Recently answered" not in render_for_context(store, today=date(2026, 9, 10))


def test_the_newest_answers_survive_the_cap() -> None:
    """Newest first, so the cap drops the stalest rather than an arbitrary
    slice."""
    questions = [
        _answered(f"Panel {i}", on=date(2026, 9, 1) + timedelta(days=i), note=f"answer {i}")
        for i in range(MAX_ANSWERED_SHOWN + 5)
    ]

    rendered = render_for_context(
        OpenQuestions(questions=questions), today=date(2026, 10, 1), max_answered=3
    )

    assert "answer 19" in rendered
    assert "answer 0" not in rendered


def test_the_window_is_measured_from_the_injected_date() -> None:
    """Read from the clock instead, and the pack's contents depend on when it
    happened to be built — the bug `regimen_chat` shipped and had to fix."""
    store = OpenQuestions(
        questions=[_answered("Your supplements", on=date(2026, 9, 1), note="biotin")]
    )

    assert "biotin" in render_for_context(store, today=date(2026, 9, 2))
    assert "biotin" not in render_for_context(store, today=date(2030, 1, 1))
