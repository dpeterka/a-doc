"""Interactive terminal loop for `adoc onboard` (PLAN.md "Onboarding & end-user
experience").

`run_onboarding_session` drives an `IntakeWizard` via injected `input_fn`/
`print_fn` callables (defaulting to the builtins) so it is fully testable
without a real terminal — tests supply a queue of scripted patient replies
and capture printed output instead. Hitting EOF (the queue running out, or a
real Ctrl-D) ends the session cleanly: whatever has been confirmed is
already committed, and an in-progress draft is already saved to
`intake-state.yaml`, so `adoc onboard` run again later resumes exactly here.

Per-turn commands: `skip` moves to the next section without confirming this
one (it stays `pending`/`awaiting_confirmation` for later); `back` moves the
cursor to the previous section. Anything else is treated as free-text
patient input: while the current section has no draft yet it is a fresh
`submit()`; once a draft exists and is awaiting confirmation, an affirmative
reply (`_looks_like_confirmation`) calls `confirm()`, and anything else is
a correction routed through `revise()`.

`run_onboarding_session` remains exactly as-is (the `--legacy-wizard` CLI
escape hatch, `adoc.cli._cmd_onboard`, still calls it directly against an
`IntakeWizard`). `run_conversational_onboarding_session` below is the
default loop (docs/adr/0012-initial-visit-conversation.md): it prints the
deterministic opener (`intake.agent.INTAKE_OPENER_MESSAGE`) once, then
hands every patient line straight to `intake.agent.run_intake_turn`, which
owns all persistence and coverage/completion logic itself — this loop is
just print/input plumbing. There is no section display and no stepping;
the loop ends on real EOF (Ctrl-D, state already saved) or as soon as the
turn just processed leaves the intake marked complete
(`intake.agent.intake_is_complete`) — a fresh REPL run against an
already-complete case file still accepts one more correction/addition
before exiting, since facts stay correctable forever.
"""

from __future__ import annotations

from collections.abc import Callable

from adoc.casefile.repo import DataRepo
from adoc.intake.wizard import IntakeWizard
from adoc.labs.db import LabsDb
from adoc.reason.client import LlmClient, LlmError

InputFn = Callable[[str], str]
PrintFn = Callable[[str], None]

_CONFIRM_WORDS = frozenset(
    {
        "y",
        "yes",
        "yep",
        "yeah",
        "confirm",
        "confirmed",
        "correct",
        "good",
        "ok",
        "okay",
        "looks good",
        "that's right",
        "that looks right",
        "looks right",
    }
)


def _looks_like_confirmation(text: str) -> bool:
    normalized = text.strip().lower().rstrip(".!")
    return normalized in _CONFIRM_WORDS


def run_onboarding_session(
    wizard: IntakeWizard,
    *,
    input_fn: InputFn = input,
    print_fn: PrintFn = print,
) -> int:
    """Run the interactive onboarding loop to completion or EOF.

    Returns 0 in both cases (EOF is a normal, resumable pause — not an
    error); onboarding-flow logic never returns a nonzero exit code.
    """
    while True:
        spec = wizard.current_section()
        if spec is None:
            completed, total = wizard.progress()
            print_fn(f"Onboarding complete — {completed}/{total} sections committed.")
            return 0

        print_fn("")
        print_fn(wizard.prompt_for_current())

        try:
            raw = input_fn("> ")
        except EOFError:
            print_fn("")
            print_fn("(input ended — progress is saved; resume anytime with `adoc onboard`)")
            return 0

        text = raw.strip()
        if not text:
            continue
        lowered = text.lower()

        if lowered == "skip":
            wizard.skip_current()
            continue
        if lowered == "back":
            wizard.go_back()
            continue

        status = wizard.current_status()
        try:
            if status == "awaiting_confirmation" and _looks_like_confirmation(text):
                result = wizard.confirm()
                print_fn(f"Saved. Committed '{result.section_key}' ({result.commit_sha[:8]}).")
            elif status == "awaiting_confirmation":
                playback = wizard.revise(text)
                print_fn("")
                print_fn(playback.text)
            else:
                playback = wizard.submit(text)
                print_fn("")
                print_fn(playback.text)
        except LlmError as exc:
            print_fn(f"Sorry, I couldn't process that: {exc}")


def run_conversational_onboarding_session(
    client: LlmClient,
    repo: DataRepo,
    db: LabsDb,
    *,
    input_fn: InputFn = input,
    print_fn: PrintFn = print,
) -> int:
    """Run the conversational initial-visit loop
    (docs/adr/0012-initial-visit-conversation.md) to completion or EOF.

    Every patient line goes straight to `intake.agent.run_intake_turn`,
    which screens, reasons about, applies, and persists the turn on its
    own; this function is just print/input plumbing around it. There is no
    section/topic display of any kind. Hitting EOF (queue exhausted in a
    test, or a real Ctrl-D) ends the session cleanly and resumably; the
    loop also ends, after printing that turn's reply, the moment
    `intake.agent.intake_is_complete` turns true — matching the CLI spec's
    "exits on intake_complete or Ctrl-D." A repo that is already complete
    when the session starts still gets one more turn before the loop exits
    (facts stay correctable/addable forever), just not an open-ended one.
    """
    # Imported here (not at module level) to avoid `intake.cli` importing
    # `intake.agent` (and everything it pulls in) for callers that only
    # ever use the legacy `run_onboarding_session` loop above.
    from adoc.intake.agent import INTAKE_OPENER_MESSAGE, intake_is_complete, run_intake_turn

    if intake_is_complete(repo):
        print_fn(
            "Your initial visit is already on file — anything you tell me here still "
            "updates your case file."
        )
    else:
        print_fn(INTAKE_OPENER_MESSAGE)

    while True:
        try:
            raw = input_fn("> ")
        except EOFError:
            print_fn("")
            print_fn("(input ended — progress is saved; resume anytime with `adoc onboard`)")
            return 0

        text = raw.strip()
        if not text:
            continue

        outcome = run_intake_turn(client, repo, db, text)
        print_fn("")
        print_fn(outcome.text)

        if intake_is_complete(repo):
            return 0
