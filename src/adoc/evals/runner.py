"""Self-evaluation benchmark harness (PLAN.md "Model strategy & self-evaluation").

`run_suite` dispatches by name to one of `evals/suites/*.py`'s `run()`
function. Every suite is offline and deterministic in this slice
(`extraction` replays fixtures through `ingest.reconcile` with no model
call at all; `redteam` drives `reason.safety`/`reason.stages` against a
scripted FAKE client, per PLAN.md's own red-team-suite design — see
`suites/redteam.py`) — CI (`eval.yml`) runs both with no network and no
data repo.

`client_factory` and `candidate` are accepted by every suite for a
uniform dispatch signature and for the incumbent-vs-candidate comparison
report (`report.py`) to have something to label columns with. Neither
current suite makes a real model call, so `--candidate provider:model`
does not change either suite's pass/fail outcome today — it changes the
label recorded on the `SuiteResult` (and would change behavior for a
future suite that actually calls the client `client_factory` builds, e.g.
a phase-3 differential-recall suite). This is a deliberate, documented
scope choice, not an oversight: PLAN.md's rare-302/retrospective suites
(which *would* need a real model) are explicitly out of scope for this
slice.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from pydantic import BaseModel, Field

from adoc.reason.client import LlmClient

ClientFactory = Callable[[], LlmClient]


class SuiteCaseResult(BaseModel):
    """One case within a suite run."""

    case_id: str
    passed: bool
    detail: str = ""


class SuiteMetric(BaseModel):
    """One named, numeric metric a suite computed (precision, pass rate,
    a confusion-matrix count, ...)."""

    name: str
    value: float
    detail: str = ""


class SuiteResult(BaseModel):
    """The result of one `run_suite` call."""

    suite: str
    binding_label: str
    cases: list[SuiteCaseResult] = Field(default_factory=list)
    metrics: list[SuiteMetric] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.cases)

    @property
    def pass_rate(self) -> float:
        if not self.cases:
            return 1.0
        return sum(1 for c in self.cases if c.passed) / len(self.cases)

    def metric(self, name: str) -> float | None:
        for m in self.metrics:
            if m.name == name:
                return m.value
        return None


class Suite(Protocol):
    """Structural protocol every `evals/suites/*.py` module satisfies."""

    def run(
        self, *, client_factory: ClientFactory, candidate: str | None = None
    ) -> SuiteResult: ...


def _suites() -> dict[str, Suite]:
    # Imported lazily (not at module level) so importing `evals.runner`
    # never requires the suites' own dependencies to already be wired up,
    # and to keep this module import-cycle-safe against `evals.suites.*`.
    from adoc.evals.suites import extraction, redteam

    return {"extraction": extraction, "redteam": redteam}


def known_suites() -> list[str]:
    return sorted(_suites())


def run_suite(
    name: str, *, client_factory: ClientFactory, candidate: str | None = None
) -> SuiteResult:
    """Run one named suite (`"extraction"` or `"redteam"`) and return its
    `SuiteResult`. Raises `ValueError` for an unknown suite name."""
    suites = _suites()
    suite = suites.get(name)
    if suite is None:
        raise ValueError(f"unknown eval suite {name!r} (known: {sorted(suites)})")
    return suite.run(client_factory=client_factory, candidate=candidate)


__all__ = [
    "ClientFactory",
    "Suite",
    "SuiteCaseResult",
    "SuiteMetric",
    "SuiteResult",
    "known_suites",
    "run_suite",
]
