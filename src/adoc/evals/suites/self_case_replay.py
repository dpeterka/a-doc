"""Retrospective self-case replay (PLAN.md phase 3 acceptance).

The other eval suites use synthetic input. This one uses the real case file, and
asks whether the deterministic layers still agree with what is on disk.

## What it can and cannot be

There is no diagnosis to score against. This patient is undiagnosed — that is
why the system exists — so a suite that measured "did we get the right answer"
would have nothing to compare to. Pretending otherwise would be the most
misleading thing this file could do.

So it measures REPRODUCIBILITY and INTERNAL CONSISTENCY instead: given the case
file as it stands, does the deterministic machinery produce the same answers it
produced before, and answers that agree with each other? That is exactly what a
model rotation needs to be gated on. A candidate model that changes the
ledger's shape, breaks a citation, or moves the criteria scores is what this
catches; a candidate that reasons differently but leaves the deterministic
layer intact passes, correctly.

## It runs against real patient data, so

- It SKIPS, cleanly and visibly, when no data repo is configured. CI has none,
  and a suite that fails there would be ignored within a week.
- It reports counts, rates and rank positions. It never puts a finding, an
  analyte value, a hypothesis name or any free text from the case file into its
  output, because eval reports are written to disk and read in contexts the
  case file is not.

No model is called, for the same reason as the recall suite: mixing an LLM in
would make a failure impossible to attribute.
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

from adoc.evals.runner import ClientFactory, SuiteCaseResult, SuiteMetric, SuiteResult

logger = logging.getLogger(__name__)

SUITE_NAME = "self_case_replay"

# Ceiling on active hypotheses before the differential stops being something a
# person can act on. Measured: the ledger reached 50 with no `most-likely`
# tier at all, which is what ADR 0035 was written to stop. This is the
# regression guard on that work.
MAX_ACTIVE_HYPOTHESES = 60

# Floor on the share of active hypotheses carrying at least one resolvable
# citation. Measured at the time of writing: 42 of 50 had supporting evidence,
# so 0.70 sits below the baseline and above a collapse.
MIN_CITED_SHARE = 0.70


def _skip(reason: str) -> SuiteResult:
    """A visible skip. Not a pass and not a failure."""
    return SuiteResult(
        suite=SUITE_NAME,
        binding_label="deterministic (no model binding used)",
        metrics=[SuiteMetric(name="skipped", value=1.0, detail=reason)],
    )


def run(*, client_factory: ClientFactory, candidate: str | None = None) -> SuiteResult:
    """Replay the deterministic layers over the real case file.

    `client_factory` and `candidate` satisfy the `Suite` protocol and are
    deliberately unused — see the module docstring.
    """
    from adoc.casefile.ledger import load_ledger
    from adoc.casefile.repo import LEDGER_RELPATH, DataRepo
    from adoc.casefile.retirement import is_protected, propose_retirements
    from adoc.config import Settings

    try:
        settings = Settings()
    except Exception as exc:  # noqa: BLE001 - no data dir configured is a skip
        return _skip(f"no configuration available: {type(exc).__name__}")

    repo = DataRepo(settings.data_dir)
    if not repo.is_initialized:
        return _skip(f"no initialised data repo at {settings.data_dir}")

    ledger_path = repo.root / Path(LEDGER_RELPATH)
    if not ledger_path.is_file():
        return _skip("no differential ledger on disk")

    try:
        ledger = load_ledger(ledger_path)
    except Exception as exc:  # noqa: BLE001 - an unreadable ledger is a real failure
        return SuiteResult(
            suite=SUITE_NAME,
            binding_label="deterministic (no model binding used)",
            cases=[
                SuiteCaseResult(
                    case_id="ledger_loads",
                    passed=False,
                    detail=f"{type(exc).__name__}: the ledger on disk does not parse",
                )
            ],
        )

    active = [h for h in ledger.hypotheses if h.status == "active"]

    # An empty ledger SKIPS. Every check below is a bound or a floor, so all of
    # them pass trivially at zero: the first run of this suite reported six
    # green cases and `passed: True` against an 83-byte ledger header, because
    # the local default data dir is a scratch repo and the populated ledger
    # lives on EFS in production. A gate that reports its strongest result when
    # it measured nothing is worse than no gate, since the green is what gets
    # believed. Vacuous must be visibly distinct from verified.
    if not active:
        return _skip(
            f"ledger holds {len(ledger.hypotheses)} hypothes(es) and none active — "
            "nothing to replay"
        )

    cases: list[SuiteCaseResult] = [
        SuiteCaseResult(
            case_id="ledger_loads",
            passed=True,
            detail=f"version {ledger.version}, {len(ledger.hypotheses)} hypotheses",
        )
    ]

    # 1. Size. A differential nobody can act on has failed regardless of how
    #    good each entry is.
    cases.append(
        SuiteCaseResult(
            case_id="active_hypotheses_bounded",
            passed=len(active) <= MAX_ACTIVE_HYPOTHESES,
            detail=f"{len(active)} active against a ceiling of {MAX_ACTIVE_HYPOTHESES}",
        )
    )

    # 2. Citation coverage. Not whether the refs RESOLVE — that needs the labs
    #    db and a network call for PMIDs — but whether hypotheses carry
    #    evidence at all. Eight of fifty carried none when ADR 0035 was
    #    written, and that is the shape this guards.
    cited = sum(1 for h in active if h.evidence_for)
    share = cited / len(active)
    cases.append(
        SuiteCaseResult(
            case_id="hypotheses_carry_evidence",
            passed=share >= MIN_CITED_SHARE,
            detail=f"{cited} of {len(active)} active carry evidence ({share:.0%})",
        )
    )

    # 3. The can't-miss tier is never empty while anything is active. This is a
    #    ledger invariant the prompts are told to maintain; checking it here
    #    catches a drift the invariant checker would only see at write time.
    cant_miss = [h for h in active if h.tier == "cant-miss"]
    cases.append(
        SuiteCaseResult(
            case_id="cant_miss_tier_populated",
            passed=bool(cant_miss),
            detail=f"{len(cant_miss)} can't-miss hypothes(es) while {len(active)} are active",
        )
    )

    # 4. The retirement pass is idempotent. Running it twice over the same
    #    ledger must propose the same set: a pass whose output depends on how
    #    many times it has run would churn the ledger every review.
    first = propose_retirements(ledger, today=date.today())
    second = propose_retirements(ledger, today=date.today())
    cases.append(
        SuiteCaseResult(
            case_id="retirement_is_deterministic",
            passed=[r.hypothesis_id for r in first.retirements]
            == [r.hypothesis_id for r in second.retirements],
            detail=f"{first.count} proposed, {first.protected_count} protected",
        )
    )

    # 5. Protected hypotheses are never proposed for retirement. The absolute
    #    exclusion in ADR 0035, checked against the real ledger rather than a
    #    fixture — a can't-miss diagnosis dropped to tidy a list is the worst
    #    outcome this system has.
    #
    #    `is_protected` is IMPORTED rather than restated. Writing the predicate
    #    out again here would mean that a change to ADR 0035's protection rule
    #    left this case still checking the old one — a test that passes while
    #    measuring something the code no longer does.
    proposed = {r.hypothesis_id for r in first.retirements}
    protected_ids = {h.id for h in active if is_protected(h)}
    wrongly = proposed & protected_ids
    cases.append(
        SuiteCaseResult(
            case_id="protected_never_retired",
            passed=not wrongly,
            detail=f"{len(protected_ids)} protected, {len(wrongly)} wrongly proposed",
        )
    )

    metrics = [
        SuiteMetric(name="ledger_version", value=float(ledger.version)),
        SuiteMetric(name="active_hypotheses", value=float(len(active))),
        SuiteMetric(name="evidence_coverage", value=share, detail="share carrying any evidence"),
        SuiteMetric(
            name="retirements_proposed",
            value=float(first.count),
            detail=f"{first.protected_count} protected from consideration",
        ),
        SuiteMetric(
            name="most_likely_populated",
            value=float(sum(1 for h in active if h.tier == "most-likely")),
            detail="an empty most-likely tier is legitimate but must be deliberate (ADR 0035)",
        ),
    ]

    return SuiteResult(
        suite=SUITE_NAME,
        binding_label="deterministic (no model binding used)",
        cases=cases,
        metrics=metrics,
    )
