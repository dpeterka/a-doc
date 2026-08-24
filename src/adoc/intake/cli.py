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
`IntakeWizard`). `run_conversational_onboarding_session` below is the new
default loop (docs/adr/0011-conversational-agentic-onboarding.md): each
patient line is handed straight to `intake.agent.run_intake_turn`, which
owns all persistence and section-completion logic itself, so this loop is
just print/input plumbing — unlike the legacy loop, it never exits once
every section is complete, since facts (and therefore the underlying case
file) may be corrected or added to at any time, during or after onboarding
(only EOF ends the session).
"""

from __future__ import annotations

from collections.abc import Callable

from adoc.casefile.repo import DataRepo
from adoc.intake.wizard import INTAKE_STATE_RELPATH, IntakeWizard, load_intake_state
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
    """Run the conversational onboarding loop (docs/adr/0011) to EOF.

    Every patient line goes straight to `intake.agent.run_intake_turn`,
    which screens, reasons about, applies, and persists the turn on its
    own; this function is just print/input plumbing around it. Hitting
    EOF (queue exhausted in a test, or a real Ctrl-D) ends the session
    cleanly and resumably, same as `run_onboarding_session`'s EOF handling
    — but unlike that loop, this one never exits early just because every
    section is complete, since facts stay correctable/addable forever.
    """
    # Imported here (not at module level) to avoid `intake.cli` importing
    # `intake.agent` (and everything it pulls in) for callers that only
    # ever use the legacy `run_onboarding_session` loop above.
    from adoc.intake.agent import run_intake_turn
    from adoc.intake.sections import SECTIONS

    state = load_intake_state(repo.root / INTAKE_STATE_RELPATH)
    if state.cursor is not None:
        spec = next(s for s in SECTIONS if s.key == state.cursor)
        print_fn(f"Let's talk about {spec.title.lower()}.")
        print_fn(spec.intro)
    else:
        print_fn("Onboarding is already complete — you can still correct or add anything here.")

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
