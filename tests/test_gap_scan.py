"""ADR 0048 §2: the chat invents a question when the backlog runs dry.

§1 shipped first and picks one open, patient-answerable question per turn,
capped at `MAX_CHAT_ASKS` attempts each. That cap is what makes this stage
necessary: once every open question has been put to her twice, §1 correctly
returns `None` and the chat quietly stops asking anything at all. Nothing
would report that — a mechanism that has run out of input looks exactly like
a mechanism that is working and has nothing to say.

The value here is in what the stage refuses to do: re-ask something already
on the list, invent a hypothesis id, ask a question only a doctor can answer,
or fail a turn.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from adoc.casefile.questions import MAX_CHAT_ASKS, OpenQuestion, OpenQuestions, question_id
from adoc.casefile.schema import Hypothesis, Ledger
from adoc.config import ModelBinding
from adoc.reason.client import LlmClient, TransportRequest, TransportResponse
from adoc.reason.context import ContextPack
from adoc.reason.stages import MAX_GAP_QUESTIONS, gap_scan_stage

_TODAY = date(2026, 9, 8)


def _client(tool_input: dict[str, Any] | None = None, *, boom: bool = False) -> LlmClient:
    def transport(request: TransportRequest) -> TransportResponse:
        if boom:
            raise RuntimeError("the provider is down")
        return TransportResponse(
            text="", tool_input=tool_input or {"questions": []}, input_tokens=1, output_tokens=1
        )

    from adoc.reason.client import AnthropicProvider

    return LlmClient(
        {"test_chooser": [ModelBinding(provider="anthropic", model="fake-chooser")]},
        {"anthropic": AnthropicProvider(api_key=None, transport=transport)},
    )


def _ledger(*ids: str) -> Ledger:
    return Ledger(
        version=1,
        updated=_TODAY,
        hypotheses=[
            Hypothesis(
                id=hid,
                name=f"Condition {hid}",
                tier="expanded",
                probability="moderate",
                status="active",
                origin="model",
                first_proposed=_TODAY,
            )
            for hid in ids
        ],
    )


def _ctx() -> ContextPack:
    return ContextPack(include_ledger=True)


def _payload(*panels: str, hypothesis_ids: list[str] | None = None) -> dict[str, Any]:
    return {
        "questions": [
            {
                "panel": panel,
                "ask": f"Can you tell us about {panel}?",
                "why": "it would move a lead",
                "hypothesis_ids": hypothesis_ids or [],
            }
            for panel in panels
        ]
    }


def _store(*questions: OpenQuestion) -> OpenQuestions:
    return OpenQuestions(questions=list(questions))


def _existing(panel: str, *, status: str = "open") -> OpenQuestion:
    return OpenQuestion(
        id=question_id(panel),
        panel=panel,
        ask=f"Tell us about {panel}.",
        audience="you",
        status=status,  # type: ignore[arg-type]
        first_asked_on=date(2026, 8, 1),
        last_asked_on=date(2026, 8, 1),
        chat_asks=MAX_CHAT_ASKS,
    )


# --- what it produces ---------------------------------------------------------


def test_a_proposal_lands_in_the_store_as_hers_to_answer() -> None:
    store = _store()

    result = gap_scan_stage(
        _client(_payload("When the rash appears")), _ledger("a"), _ctx(), store, today=_TODAY
    )

    assert result.ran and not result.error
    assert len(store.questions) == 1
    assert store.questions[0].audience == "you"
    assert store.questions[0].panel == "When the rash appears"
    assert result.accepted_ids == [question_id("When the rash appears")]


def test_the_audience_is_not_the_models_to_choose() -> None:
    """Constraint 2. The stage exists to find something SHE can answer; a
    prompt that merely asks for that has no way to be held to it, and a
    doctor-audience question at the end of a chat reply wastes the turn."""
    store = _store()
    payload = _payload("Repeat FSH and LH")
    payload["questions"][0]["audience"] = "doctor"  # the model trying anyway

    gap_scan_stage(_client(payload), _ledger("a"), _ctx(), store, today=_TODAY)

    assert store.questions[0].audience == "you"


def test_at_most_two_are_taken() -> None:
    """The prompt says a third is by its own admission not good enough."""
    store = _store()

    gap_scan_stage(
        _client(_payload("one", "two", "three", "four")),
        _ledger("a"),
        _ctx(),
        store,
        today=_TODAY,
    )

    assert len(store.questions) == MAX_GAP_QUESTIONS


# --- what it refuses to do ----------------------------------------------------


def test_a_question_already_on_the_list_is_not_reopened() -> None:
    """Constraint 3, one store. A second id for the same panel would ask her
    again for something the store already holds — the failure ADR 0033
    exists to stop."""
    store = _store(_existing("Every supplement you take"))

    result = gap_scan_stage(
        _client(_payload("Every supplement you take")), _ledger("a"), _ctx(), store, today=_TODAY
    )

    assert len(store.questions) == 1, "the question was re-opened under a second id"
    assert result.accepted_ids == []
    assert result.proposed, "the proposal is still recorded, just not accepted"


def test_a_question_she_already_answered_is_not_asked_again() -> None:
    """The same rule, and the case that matters most: re-asking something she
    has told us is the behaviour the whole question store was built to end."""
    answered = _existing("Every supplement you take", status="answered")
    store = _store(answered)

    gap_scan_stage(
        _client(_payload("Every supplement you take")), _ledger("a"), _ctx(), store, today=_TODAY
    )

    assert len(store.questions) == 1
    assert store.questions[0].status == "answered"


def test_an_invented_hypothesis_id_is_dropped_and_the_question_kept() -> None:
    """ADR 0028: one bad identifier costs its own reference, never the
    payload that carries it."""
    store = _store()

    gap_scan_stage(
        _client(_payload("Morning stiffness", hypothesis_ids=["real-01", "invented-99"])),
        _ledger("real-01"),
        _ctx(),
        store,
        today=_TODAY,
    )

    assert store.questions[0].hypothesis_ids == ["real-01"]


def test_an_empty_proposal_is_not_stored() -> None:
    store = _store()
    payload = {"questions": [{"panel": "  ", "ask": "", "why": "", "hypothesis_ids": []}]}

    result = gap_scan_stage(_client(payload), _ledger("a"), _ctx(), store, today=_TODAY)

    assert store.questions == []
    assert result.accepted_ids == []


# --- it never costs a turn ----------------------------------------------------


def test_a_provider_failure_does_not_fail_the_turn() -> None:
    """Constraint 5. A turn that produced a real reply must not be lost
    because a question could not be invented."""
    store = _store()

    result = gap_scan_stage(_client(boom=True), _ledger("a"), _ctx(), store, today=_TODAY)

    assert result.ran and result.error
    assert result.proposed == []
    assert store.questions == []


def test_finding_nothing_is_recorded_as_finding_nothing() -> None:
    """ "Did not run", "ran and found nothing", and "ran and failed" have to be
    three different readings. Collapsed into one, a stage that stopped working
    is indistinguishable from one with nothing to say — which is how this
    codebase has lost mechanisms before."""
    result = gap_scan_stage(_client(), _ledger("a"), _ctx(), _store(), today=_TODAY)

    assert result.ran is True
    assert result.error == ""
    assert result.proposed == []


def test_a_stage_that_never_ran_says_so() -> None:
    from adoc.reason.stages import GapScanResult

    assert GapScanResult().ran is False


# --- it only runs when there is nothing left to ask ---------------------------


def test_the_turn_only_reaches_the_gap_scan_when_the_backlog_is_exhausted() -> None:
    """The fifth model call of a turn that already takes minutes. Picking from
    the backlog costs nothing; inventing costs a call, which is the whole
    reason the two decisions are separate stages."""
    import inspect

    from adoc.reason import stages as module

    source = inspect.getsource(module.build_diagnostic_dag)
    guard = source.index("if ask_question is None:")
    call = source.index("gap_scan_stage(")

    assert guard < call, "the gap scan runs before the backlog is checked"


def test_the_gap_scan_reads_the_ledger_this_turn_produced() -> None:
    """Constraint 4, the ordering argument of ADR 0043: the question is about
    the differential this turn produced, so it cannot run before `apply`."""
    import inspect

    from adoc.reason import stages as module

    source = inspect.getsource(module.build_diagnostic_dag)
    composer_fn = source.index("def _composer_fn")
    apply_fn = source.index("def _apply_fn")
    gap_call = source.index("gap_scan_stage(")

    assert apply_fn < composer_fn < gap_call
    assert 'ledger = ctx["apply"]' in source[composer_fn:gap_call]
