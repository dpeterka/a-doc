"""Tests for adoc.intake.cli: the interactive `adoc onboard` terminal loop.

The loop is driven entirely through injected `input_fn`/`print_fn`
callables — no real terminal, no network (a `ScriptedTransport`, same as
`test_intake_wizard.py`, stands in for the LLM).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from adoc.casefile.repo import DataRepo
from adoc.config import ModelBinding
from adoc.intake.agent import IntakeTurnResult
from adoc.intake.cli import run_conversational_onboarding_session, run_onboarding_session
from adoc.intake.wizard import IntakeWizard
from adoc.labs.db import LabsDb
from adoc.reason.client import AnthropicProvider, LlmClient, TransportRequest, TransportResponse


class ScriptedTransport:
    def __init__(self, queues: dict[str, list[dict[str, Any]]]) -> None:
        self._queues = {name: list(items) for name, items in queues.items()}

    def __call__(self, request: TransportRequest) -> TransportResponse:
        assert request.schema is not None
        name = request.schema.__name__
        queue = self._queues.get(name)
        if not queue:
            raise AssertionError(f"no scripted response left for schema {name!r}")
        return TransportResponse(text="", tool_input=queue.pop(0), input_tokens=1, output_tokens=1)


def _make_wizard(root: Path, queues: dict[str, list[dict[str, Any]]] | None = None) -> IntakeWizard:
    repo = DataRepo.init_at(root)
    provider = AnthropicProvider(api_key=None, transport=ScriptedTransport(queues or {}))
    client = LlmClient(
        {"primary_reasoner": [ModelBinding(provider="anthropic", model="claude-opus-5")]},
        {"anthropic": provider},
    )
    return IntakeWizard(repo, client)


def _make_io(
    lines: list[str],
) -> tuple[list[str], Callable[[str], str], Callable[[str], None]]:
    """Returns (printed_lines, input_fn, print_fn) where `input_fn` yields
    `lines` in order and then raises EOFError, exactly like a real
    interactive session running out of input (Ctrl-D)."""
    printed: list[str] = []
    remaining = iter(lines)

    def input_fn(_prompt: str = "") -> str:
        try:
            return next(remaining)
        except StopIteration:
            raise EOFError from None

    def print_fn(text: str) -> None:
        printed.append(text)

    return printed, input_fn, print_fn


def _run(wizard: IntakeWizard, lines: list[str]) -> list[str]:
    printed, input_fn, print_fn = _make_io(lines)
    code = run_onboarding_session(wizard, input_fn=input_fn, print_fn=print_fn)
    assert code == 0
    return printed


def test_immediate_eof_ends_session_cleanly_and_shows_first_section(tmp_path: Path) -> None:
    wizard = _make_wizard(tmp_path / "a-doc-data")
    printed = _run(wizard, [])

    joined = "\n".join(printed)
    assert "[1/11] Basics" in joined
    assert "resume anytime with `adoc onboard`" in joined


def test_skip_moves_cursor_forward_without_completing_the_section(tmp_path: Path) -> None:
    wizard = _make_wizard(tmp_path / "a-doc-data")
    printed = _run(wizard, ["skip"])

    joined = "\n".join(printed)
    assert "[1/11] Basics" in joined
    assert "[2/11] Current symptoms" in joined
    assert wizard.current_status() == "pending"  # symptoms, not yet touched
    completed, total = wizard.progress()
    assert (completed, total) == (0, 11)


def test_back_returns_cursor_to_the_previous_section(tmp_path: Path) -> None:
    wizard = _make_wizard(tmp_path / "a-doc-data")
    printed = _run(wizard, ["skip", "back"])

    joined = "\n".join(printed)
    # basics -> (skip) -> symptoms -> (back) -> basics again
    assert joined.count("[1/11] Basics") == 2


def test_submit_then_confirmation_phrase_commits_the_section(tmp_path: Path) -> None:
    basics = {
        "age": 41,
        "sex_at_birth": "female",
        "height_cm": 165.0,
        "weight_kg": 63.0,
        "occupation": "software engineer",
        "exposures": [],
    }
    wizard = _make_wizard(tmp_path / "a-doc-data", {"BasicsSection": [basics]})

    printed = _run(wizard, ["41yo female software engineer", "looks good"])

    joined = "\n".join(printed)
    assert "Here's what I've noted" in joined
    assert "Saved. Committed 'basics'" in joined
    assert wizard.current_status() == "pending"  # advanced to "symptoms"
    completed, _total = wizard.progress()
    assert completed == 1


def test_correction_text_after_playback_revises_instead_of_confirming(tmp_path: Path) -> None:
    basics_1 = {
        "age": 41,
        "sex_at_birth": "female",
        "height_cm": 165.0,
        "weight_kg": 63.0,
        "occupation": None,
        "exposures": [],
    }
    basics_2 = {
        "age": None,
        "sex_at_birth": None,
        "height_cm": None,
        "weight_kg": None,
        "occupation": "software engineer",
        "exposures": [],
    }
    wizard = _make_wizard(tmp_path / "a-doc-data", {"BasicsSection": [basics_1, basics_2]})

    printed = _run(
        wizard,
        ["41yo female, 165cm, 63kg", "oh also I'm a software engineer", "looks good"],
    )

    joined = "\n".join(printed)
    assert "software engineer" in joined
    assert "Saved. Committed 'basics'" in joined


def test_llm_error_is_reported_without_crashing_the_loop(tmp_path: Path) -> None:
    repo = DataRepo.init_at(tmp_path / "a-doc-data")

    def broken_transport(request: TransportRequest) -> TransportResponse:
        return TransportResponse(
            text="no tool call", tool_input=None, input_tokens=1, output_tokens=1
        )

    provider = AnthropicProvider(api_key=None, transport=broken_transport)
    client = LlmClient(
        {"primary_reasoner": [ModelBinding(provider="anthropic", model="claude-opus-5")]},
        {"anthropic": provider},
        max_retries=1,
    )
    wizard = IntakeWizard(repo, client)

    printed = _run(wizard, ["41yo female software engineer"])

    joined = "\n".join(printed)
    assert "Sorry, I couldn't process that" in joined
    # the loop kept going and hit EOF cleanly rather than raising
    assert "resume anytime with `adoc onboard`" in joined


def test_blank_input_is_ignored(tmp_path: Path) -> None:
    wizard = _make_wizard(tmp_path / "a-doc-data")
    _run(wizard, ["   ", ""])

    # blank input is a pure no-op: still on "basics", nothing submitted
    assert wizard.current_section() is not None
    assert wizard.current_section().key == "basics"  # type: ignore[union-attr]
    assert wizard.current_status() == "pending"


# --- run_conversational_onboarding_session: the new default REPL loop -----------------


def _make_intake_agent_client(queue: list[dict]) -> LlmClient:
    class _ScriptedTransport:
        def __init__(self, items: list[dict]) -> None:
            self._items = list(items)

        def __call__(self, request: TransportRequest) -> TransportResponse:
            assert request.schema is IntakeTurnResult
            return TransportResponse(
                text="", tool_input=self._items.pop(0), input_tokens=1, output_tokens=1
            )

    provider = AnthropicProvider(api_key=None, transport=_ScriptedTransport(queue))
    return LlmClient(
        {"intake_agent": [ModelBinding(provider="anthropic", model="claude-opus-5")]},
        {"anthropic": provider},
    )


def test_conversational_session_immediate_eof_prints_the_opener(tmp_path: Path) -> None:
    from adoc.intake.agent import INTAKE_OPENER_MESSAGE

    repo = DataRepo.init_at(tmp_path / "a-doc-data")
    db = LabsDb(":memory:")
    client = _make_intake_agent_client([])

    printed, input_fn, print_fn = _make_io([])
    code = run_conversational_onboarding_session(
        client, repo, db, input_fn=input_fn, print_fn=print_fn
    )

    assert code == 0
    joined = "\n".join(printed)
    assert INTAKE_OPENER_MESSAGE in joined
    assert "resume anytime with `adoc onboard`" in joined
    # No section display of any kind.
    assert "[1/11]" not in joined
    assert "Basics" not in joined


def test_conversational_session_runs_a_turn_and_prints_the_reply(tmp_path: Path) -> None:
    repo = DataRepo.init_at(tmp_path / "a-doc-data")
    db = LabsDb(":memory:")
    client = _make_intake_agent_client(
        [
            {
                "message": "Got it, 41 and female. What's your occupation?",
                "ops": [
                    {
                        "op": "add_fact",
                        "fact": {
                            "id": "basic-age",
                            "section": "basics",
                            "kind": "basic",
                            "statement": "Patient is 41, female.",
                            "fields": {"age": 41, "sex_at_birth": "female"},
                        },
                    }
                ],
                "topics_covered": [],
                "intake_complete": False,
            }
        ]
    )

    printed, input_fn, print_fn = _make_io(["I'm 41 and female."])
    code = run_conversational_onboarding_session(
        client, repo, db, input_fn=input_fn, print_fn=print_fn
    )

    assert code == 0
    joined = "\n".join(printed)
    assert "What's your occupation?" in joined
    # EOF still ends the (not-yet-complete) session cleanly.
    assert "resume anytime with `adoc onboard`" in joined


def test_conversational_session_exits_as_soon_as_intake_completes(tmp_path: Path) -> None:
    """CLI spec: "exits on intake_complete or Ctrl-D" — once the
    deterministic wrap-up gate accepts `intake_complete`, the REPL prints
    that turn's reply and returns without waiting for another prompt."""
    from adoc.intake.coverage import INTAKE_STATE_RELPATH, CoverageState, TopicCoverage
    from adoc.intake.coverage import save_coverage_state as save_state
    from adoc.intake.sections import SECTIONS

    repo = DataRepo.init_at(tmp_path / "a-doc-data")
    db = LabsDb(":memory:")
    # Every topic already covered, so this turn's intake_complete=True is
    # actually accepted rather than vetoed.
    save_state(
        repo.root / INTAKE_STATE_RELPATH,
        CoverageState(topics={spec.key: TopicCoverage(covered=True) for spec in SECTIONS}),
    )
    repo.commit("chore: seed all-covered state for test")

    client = _make_intake_agent_client(
        [
            {
                "message": "Thank you — I have a good picture now.",
                "ops": [],
                "topics_covered": [],
                "intake_complete": True,
            }
        ]
    )

    # Only ONE line is queued: if the loop tried to prompt again after
    # completion, `input_fn` would raise EOFError anyway, but the
    # `resume anytime` EOF message must NOT appear — the session ended
    # because intake completed, not because it ran out of input.
    printed, input_fn, print_fn = _make_io(["I think that covers everything."])
    code = run_conversational_onboarding_session(
        client, repo, db, input_fn=input_fn, print_fn=print_fn
    )

    assert code == 0
    joined = "\n".join(printed)
    assert "good picture" in joined
    assert "resume anytime with `adoc onboard`" not in joined


def test_conversational_session_already_complete_still_accepts_one_more_turn(
    tmp_path: Path,
) -> None:
    """A repo whose initial visit already finished (a prior session) still
    lets the patient add or correct something through the CLI — facts stay
    correctable/addable forever — before the session exits again."""
    from adoc.intake.coverage import INTAKE_STATE_RELPATH, CoverageState, save_coverage_state

    repo = DataRepo.init_at(tmp_path / "a-doc-data")
    db = LabsDb(":memory:")
    save_coverage_state(repo.root / INTAKE_STATE_RELPATH, CoverageState(intake_complete=True))
    repo.commit("chore: seed intake-complete state for test")

    client = _make_intake_agent_client(
        [
            {
                "message": "Got it — updated your allergy note.",
                "ops": [],
                "topics_covered": [],
                "intake_complete": True,
            }
        ]
    )

    printed, input_fn, print_fn = _make_io(["Actually, my allergy reaction was hives."])
    code = run_conversational_onboarding_session(
        client, repo, db, input_fn=input_fn, print_fn=print_fn
    )

    assert code == 0
    joined = "\n".join(printed)
    assert "already on file" in joined
    assert "updated your allergy note" in joined
