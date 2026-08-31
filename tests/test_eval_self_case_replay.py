"""The retrospective self-case replay suite.

This suite is unusual among the evals: it reads the REAL case file. Everything
here therefore builds a synthetic data repo in `tmp_path` and points the
settings at it — no test in this file may depend on patient data existing.

The first three tests pin the property the suite got wrong on its first run:
it reported six passing cases and an overall `passed: True` against an
83-byte empty ledger, because every check it makes is a bound or a floor and
all of them hold trivially at zero.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from adoc.casefile.ledger import save_ledger
from adoc.casefile.repo import LEDGER_RELPATH
from adoc.casefile.schema import Evidence, Hypothesis, Ledger
from adoc.evals.suites import self_case_replay as suite

_TODAY = date(2026, 8, 30)


def _h(
    hid: str,
    *,
    tier: str = "expanded",
    origin: str = "model",
    probability: str = "low",
    evidence: bool = True,
) -> Hypothesis:
    return Hypothesis(
        id=hid,
        name=hid.replace("-", " ").title(),
        tier=tier,  # type: ignore[arg-type]
        probability=probability,  # type: ignore[arg-type]
        status="active",
        origin=origin,  # type: ignore[arg-type]
        first_proposed=_TODAY,
        evidence_for=(
            [Evidence(claim="c", source="pmid:12345", strength="moderate")] if evidence else []
        ),
    )


def _repo(root: Path, *hypotheses: Hypothesis) -> Path:
    """A data repo the suite will accept: `.git` plus a ledger on disk."""
    (root / ".git").mkdir(parents=True, exist_ok=True)
    ledger_path = root / LEDGER_RELPATH
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    save_ledger(
        ledger_path,
        Ledger(version=12, updated=datetime(2026, 8, 30, tzinfo=UTC), hypotheses=list(hypotheses)),
    )
    return root


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ADOC_DATA_DIR", str(tmp_path))
    return tmp_path


def _case(result: object, case_id: str) -> object:
    return next(c for c in result.cases if c.case_id == case_id)  # type: ignore[attr-defined]


def _skipped(result: object) -> bool:
    return any(m.name == "skipped" for m in result.metrics)  # type: ignore[attr-defined]


# -- vacuity ----------------------------------------------------------------


def test_an_empty_ledger_skips_rather_than_passing(data_dir: Path) -> None:
    """The bug this suite shipped with for one run.

    Every check is a ceiling or a floor, so zero hypotheses satisfies all of
    them. Reporting that as a pass means the gate is loudest exactly when it
    measured nothing, and green is what gets believed.
    """
    _repo(data_dir)

    result = suite.run(client_factory=None)  # type: ignore[arg-type]

    assert _skipped(result)
    assert result.cases == []  # type: ignore[attr-defined]


def test_a_ledger_with_no_ACTIVE_hypotheses_also_skips(data_dir: Path) -> None:
    """Retired-only is just as vacuous as empty, and much easier to reach:
    one aggressive retirement pass gets there."""
    retired = _h("old-lead")
    retired.status = "ruled-out"  # type: ignore[assignment]
    _repo(data_dir, retired)

    assert _skipped(suite.run(client_factory=None))  # type: ignore[arg-type]


def test_no_data_repo_skips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CI has no data repo. A suite that fails there is ignored within a
    week, so absence must be a skip and not a failure."""
    monkeypatch.setenv("ADOC_DATA_DIR", str(tmp_path / "nothing-here"))

    result = suite.run(client_factory=None)  # type: ignore[arg-type]

    assert _skipped(result)
    assert result.passed  # type: ignore[attr-defined]


# -- the checks themselves --------------------------------------------------


def test_a_populated_ledger_is_actually_measured(data_dir: Path) -> None:
    """The positive control for all three tests above: given real content, the
    suite reports cases rather than skipping."""
    _repo(data_dir, _h("sarcoidosis", tier="cant-miss"), _h("sjogren"))

    result = suite.run(client_factory=None)  # type: ignore[arg-type]

    assert not _skipped(result)
    assert result.passed  # type: ignore[attr-defined]
    assert _case(result, "ledger_loads").passed  # type: ignore[attr-defined]


def test_an_unbounded_differential_fails(data_dir: Path) -> None:
    """A list nobody can read has failed regardless of how good each entry
    is — the regression guard on ADR 0035's reason for existing."""
    many = [_h(f"h{n}", tier="cant-miss" if n == 0 else "expanded") for n in range(70)]
    _repo(data_dir, *many)

    result = suite.run(client_factory=None)  # type: ignore[arg-type]

    assert not _case(result, "active_hypotheses_bounded").passed  # type: ignore[attr-defined]
    assert not result.passed  # type: ignore[attr-defined]


def test_a_differential_with_no_evidence_fails(data_dir: Path) -> None:
    """Hypotheses that cite nothing are the shape ADR 0035 was written
    against: eight of fifty carried no supporting evidence at all."""
    _repo(
        data_dir,
        _h("a", tier="cant-miss", evidence=False),
        _h("b", evidence=False),
        _h("c", evidence=False),
    )

    result = suite.run(client_factory=None)  # type: ignore[arg-type]

    assert not _case(result, "hypotheses_carry_evidence").passed  # type: ignore[attr-defined]


def test_an_empty_cant_miss_tier_fails(data_dir: Path) -> None:
    """A ledger invariant the prompts are told to maintain. Checking it here
    catches drift the write-time checker would not see again."""
    _repo(data_dir, _h("sjogren"), _h("lupus"))

    result = suite.run(client_factory=None)  # type: ignore[arg-type]

    assert not _case(result, "cant_miss_tier_populated").passed  # type: ignore[attr-defined]


def test_protected_hypotheses_are_never_proposed_for_retirement(data_dir: Path) -> None:
    """ADR 0035's absolute exclusion, checked end-to-end through the suite.

    Both protected kinds are present AND both are individually retirable —
    no evidence at all — so the case only passes because the protection held.
    """
    _repo(
        data_dir,
        _h("pulmonary-embolism", tier="cant-miss", evidence=False),
        _h("her-own-theory", origin="patient", evidence=False),
        _h("well-cited"),
    )

    result = suite.run(client_factory=None)  # type: ignore[arg-type]

    assert _case(result, "protected_never_retired").passed  # type: ignore[attr-defined]
    assert _case(result, "retirement_is_deterministic").passed  # type: ignore[attr-defined]


def test_an_unparseable_ledger_fails_rather_than_skipping(data_dir: Path) -> None:
    """The one on-disk problem that is NOT a skip. A ledger that does not load
    is a real failure, and quietly skipping it would hide the corruption."""
    (data_dir / ".git").mkdir(parents=True)
    path = data_dir / LEDGER_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("this: is: not: a: ledger\n", encoding="utf-8")

    result = suite.run(client_factory=None)  # type: ignore[arg-type]

    assert not result.passed  # type: ignore[attr-defined]
    assert not _skipped(result)


# -- what it must never emit ------------------------------------------------


def test_no_hypothesis_name_or_free_text_reaches_the_output(data_dir: Path) -> None:
    """Eval reports are written to disk and read in contexts the case file is
    not. The suite reports counts and rates; it must not carry the content of
    the differential out with them."""
    _repo(
        data_dir,
        _h("granulomatosis-with-polyangiitis", tier="cant-miss"),
        _h("distinctive-condition-name"),
    )

    result = suite.run(client_factory=None)  # type: ignore[arg-type]
    emitted = " ".join(
        [c.case_id + " " + c.detail for c in result.cases]  # type: ignore[attr-defined]
        + [m.name + " " + m.detail for m in result.metrics]  # type: ignore[attr-defined]
    ).lower()

    assert "granulomatosis" not in emitted
    assert "distinctive" not in emitted
