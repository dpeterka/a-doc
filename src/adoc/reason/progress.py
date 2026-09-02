"""Which stage a slow turn is on, for the page that is waiting (ADR 0046).

A diagnostic chat turn runs four DAG stages and several frontier calls, and
takes minutes. `chat.html` already says so honestly — "can take a few
minutes — you can leave this page open" — so PAT-06's claim of "only a
static loading spinner" is not right. What is missing is *which* minute:
whether the wait is nearly over or has barely started, and whether anything
is happening at all.

## Why a registry and not a stream

The obvious implementation is Server-Sent Events, which PAT-06 proposes. It
would mean a second transport for one form that htmx already handles, a
background thread to run the DAG so the request can stream, and a new
failure mode where the reply is produced but the connection that carries it
has gone. This keeps the existing request/response shape and adds a
2-second poll against a dict.

## One slot, deliberately

The registry holds ONE in-flight turn, not a map keyed by request. The web
service runs a single ECS task at a time (CLAUDE.md), there is one patient,
and htmx disables the Send button and queues a second submit behind the
first — so two concurrent diagnostic turns is not a state this application
reaches. A per-request key would need the page to know its own id before
the POST that creates it, which is a JavaScript problem invented to solve a
concurrency problem that does not exist here.

If it ever does exist, the failure is that two turns overwrite each other's
*progress label*. No reply, no ledger write and no audit record depends on
this module — it is a status line.

## Nothing here is patient content

Only stage names and their fixed labels, so this registry can never leak the
text of a turn. `logger` output from it carries the same.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

# Node name -> what to tell someone who is waiting. Keyed on the diagnostic
# DAG's node names (`reason.stages.build_diagnostic_dag`); an unmapped node
# falls back to a neutral line rather than showing an internal identifier.
STAGE_LABELS: dict[str, str] = {
    "ledger_maintainer": "Reading your history and recent results...",
    "challenger": "Arguing the other side, with a different model...",
    "apply": "Recording what changed in your case file...",
    "composer": "Writing your reply...",
}

FALLBACK_LABEL = "Working..."
STARTING_LABEL = "Starting..."

_STALE_AFTER_SECONDS = 900.0
"""How long a turn may sit without an update before its progress is treated
as abandoned. A turn that dies between stages would otherwise leave the page
reporting a stage forever; longer than the longest plausible turn, short
enough that a stuck one eventually reads as finished."""


@dataclass(frozen=True)
class Progress:
    """What to show a page that is waiting."""

    label: str
    step: int
    total: int
    finished: bool

    @property
    def visible(self) -> bool:
        return not self.finished and self.step > 0


class ProgressTracker:
    """The in-flight turn's stage, if there is one.

    Every method is safe to call from another thread: the poll endpoint and
    the DAG run are different requests, and FastAPI serves sync endpoints
    from a threadpool.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._label = ""
        self._step = 0
        self._total = 0
        self._finished = True
        self._updated = 0.0
        # Two independent reasons a fresh tracker shows nothing: `finished`
        # starts True, and `visible` additionally requires `step > 0`.
        # Either alone is sufficient; both are kept because a status line
        # that appears on a quiet page is the failure this class exists to
        # avoid.

    def start(self, total: int) -> None:
        with self._lock:
            self._label = STARTING_LABEL
            self._step = 0
            self._total = total
            self._finished = False
            self._updated = time.monotonic()

    def note(self, node_name: str, step: int) -> None:
        """Record which stage is starting.

        The `_finished` guard is defence in depth, not the mechanism:
        `read` already gates on `_finished`, so a late note cannot make a
        completed turn visible even without it. What the guard adds is that
        the object does not carry a stale label between turns. Measured —
        removing it alone breaks no test, which is why the test below asserts
        its local effect rather than only the visible one.
        """
        with self._lock:
            if self._finished:
                return
            self._label = STAGE_LABELS.get(node_name, FALLBACK_LABEL)
            self._step = step
            self._updated = time.monotonic()

    def finish(self) -> None:
        with self._lock:
            self._finished = True
            self._label = ""
            self._step = 0
            self._updated = time.monotonic()

    def read(self) -> Progress:
        with self._lock:
            stale = self._updated and (time.monotonic() - self._updated) > _STALE_AFTER_SECONDS
            if self._finished or stale:
                return Progress(label="", step=0, total=self._total, finished=True)
            return Progress(label=self._label, step=self._step, total=self._total, finished=False)


TRACKER = ProgressTracker()
"""The process-wide tracker. A module-level singleton for the same reason
the registry holds one slot: one task, one patient, one turn at a time."""
