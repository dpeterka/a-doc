"""ADR 0052: the review records its own boundary.

Six releases of the convergence track were each measured by hand, once,
against a number recalled from the release before. Twice the recollection was
wrong — an "8 emerging" projection that predated the corroboration rule it was
projecting, and a "187 of 2079" denominator that was never the denominator the
change acted on. These tests pin the properties that make a snapshot a
boundary rather than another recollection: it is written per review, it is
append-only, it carries the version that produced it, and it refuses to invent
a delta it cannot compute.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from adoc.casefile.convergence import (
    ConvergenceSnapshot,
    append_snapshot,
    latest_snapshot,
    load_snapshots,
    measure,
    render,
)
from adoc.casefile.schema import Evidence, Hypothesis, Ledger

_TODAY = date(2026, 9, 8)


def _hypothesis(
    hid: str,
    *,
    status: str = "active",
    tier: str = "expanded",
    cited: date = date(2025, 1, 1),
) -> Hypothesis:
    return Hypothesis(
        id=hid,
        name=f"Condition {hid}",
        tier=tier,  # type: ignore[arg-type]
        probability="moderate",  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        origin="model",  # type: ignore[arg-type]
        first_proposed=cited,
        evidence_for=[
            Evidence(
                claim="something on file",
                source=f"labs:{hid}:{cited.isoformat()}",
                strength="moderate",
                observed_on=cited,
            )
        ],
    )


def _ledger(*hypotheses: Hypothesis) -> Ledger:
    return Ledger(version=1, updated=_TODAY, hypotheses=list(hypotheses))


def test_counts_the_board_the_review_committed() -> None:
    ledger = _ledger(
        _hypothesis("a", tier="most-likely"),
        _hypothesis("b"),
        _hypothesis("c", status="ruled-out"),
        _hypothesis("d", status="parked"),
    )

    snapshot = measure(ledger, app_version="0.33.0", today=_TODAY)

    assert snapshot.active == 2
    assert (snapshot.ruled_out_total, snapshot.parked_total) == (1, 1)
    assert snapshot.by_tier == {"most-likely": 1, "expanded": 1}


def test_a_recent_lead_counts_as_emerging_not_as_differential() -> None:
    """The differential number is the one the convergence track is trying to
    move. Counting a 3-week-old lead inside it would report the board as
    failing to shrink while the mechanism that separates them worked."""
    ledger = _ledger(
        _hypothesis("old", cited=date(2024, 1, 1)),
        _hypothesis("new", cited=date(2026, 8, 25)),
    )

    snapshot = measure(ledger, app_version="0.33.0", today=_TODAY)

    assert (snapshot.differential, snapshot.emerging) == (1, 1)
    assert snapshot.active == 2, "emerging leads are still on the board"


def test_the_version_that_produced_the_count_is_recorded() -> None:
    """Attribution is the whole point: a count with no version says the board
    changed, not what changed it."""
    snapshot = measure(_ledger(), app_version="0.33.0", today=_TODAY)

    assert snapshot.app_version == "0.33.0"


def test_a_first_snapshot_reports_no_delta_rather_than_a_delta_against_zero() -> None:
    """A new counter's first reading did not ADD the whole board. Reporting
    "+46 leads" would be exactly the false before/after this file exists to
    stop."""
    first = measure(_ledger(_hypothesis("a")), app_version="0.33.0", today=_TODAY)

    assert first.delta(None) == {}


def test_a_delta_names_only_what_moved() -> None:
    before = ConvergenceSnapshot(
        review_date=date(2026, 9, 1), app_version="0.32.0", active=46, differential=44
    )
    after = ConvergenceSnapshot(
        review_date=_TODAY, app_version="0.33.0", active=32, differential=30
    )

    assert after.delta(before) == {"active": -14, "differential": -14}


def test_a_dict_field_is_not_treated_as_a_number() -> None:
    """`by_tier` is a mapping. Subtracting it would raise and take the whole
    review's report with it."""
    before = ConvergenceSnapshot(
        review_date=date(2026, 9, 1), app_version="0.32.0", by_tier={"expanded": 40}
    )
    after = ConvergenceSnapshot(review_date=_TODAY, app_version="0.33.0", by_tier={"expanded": 20})

    assert after.delta(before) == {}


# --- the log ------------------------------------------------------------------


def test_the_log_is_append_only(tmp_path: Path) -> None:
    """A snapshot records what was true on a date. A re-run that overwrote it
    would erase the boundary it came to mark."""
    path = tmp_path / "case" / "convergence.jsonl"
    first = ConvergenceSnapshot(review_date=date(2026, 9, 1), app_version="0.32.0", active=46)
    second = ConvergenceSnapshot(review_date=_TODAY, app_version="0.33.0", active=32)

    append_snapshot(path, first)
    append_snapshot(path, second)

    assert [s.active for s in load_snapshots(path)] == [46, 32]
    assert latest_snapshot(path) is not None
    assert latest_snapshot(path).active == 32  # type: ignore[union-attr]


def test_a_corrupt_line_does_not_lose_the_rest(tmp_path: Path) -> None:
    """This is a measurement log. One bad line must never stop a review, and
    a review that cannot record its boundary is still a review worth having."""
    path = tmp_path / "convergence.jsonl"
    path.write_text(
        json.dumps({"review_date": "2026-09-01", "app_version": "0.32.0", "active": 46})
        + "\n{ not json\n"
        + json.dumps({"review_date": "2026-09-08", "app_version": "0.33.0", "active": 32})
        + "\n",
        encoding="utf-8",
    )

    assert [s.active for s in load_snapshots(path)] == [46, 32]


def test_no_file_is_no_snapshots(tmp_path: Path) -> None:
    assert load_snapshots(tmp_path / "nope.jsonl") == []
    assert latest_snapshot(tmp_path / "nope.jsonl") is None


# --- what the reader sees -----------------------------------------------------


def test_the_report_names_both_ends_of_the_change() -> None:
    """ "14 fewer" is a claim the reader cannot check. "46 → 32" is shorter and
    they can."""
    before = ConvergenceSnapshot(
        review_date=date(2026, 9, 1), app_version="0.32.0", active=46, differential=44
    )
    after = ConvergenceSnapshot(
        review_date=_TODAY, app_version="0.33.0", active=32, differential=30
    )

    rendered = "\n".join(render(after, before))

    assert "46 → 32" in rendered
    assert "0.32.0 → 0.33.0" in rendered


def test_the_report_says_when_nothing_moved(tmp_path: Path) -> None:
    """An unchanged board is a result. A section that renders nothing reads as
    a section that failed to run."""
    same = ConvergenceSnapshot(review_date=date(2026, 9, 1), app_version="0.33.0", active=32)
    after = same.model_copy(update={"review_date": _TODAY})

    rendered = "\n".join(render(after, same))

    assert "Nothing counted here changed." in rendered


def test_the_first_report_does_not_claim_a_comparison() -> None:
    first = ConvergenceSnapshot(review_date=_TODAY, app_version="0.33.0", active=32)

    rendered = "\n".join(render(first, None))

    assert "starting line" in rendered
    assert "→" not in rendered


# --- attribution: which mechanism did it -------------------------------------


def test_the_split_names_the_mechanism_not_the_status() -> None:
    """Three separate rules write `parked` — no supporting evidence, gone
    stale, and the ADR 0045 tier cap. A count of parked leads therefore
    credits whichever mechanism the reader has in mind, which is the failure
    ADR 0052 exists to stop."""
    from adoc.casefile.retirement import Retirement

    proposed = [
        Retirement(
            hypothesis_id="a", hypothesis_name="A", to_status="parked", reason="", cause="stale"
        ),
        Retirement(
            hypothesis_id="b",
            hypothesis_name="B",
            to_status="parked",
            reason="",
            cause="tier-fold",
        ),
        Retirement(
            hypothesis_id="c",
            hypothesis_name="C",
            to_status="parked",
            reason="",
            cause="tier-fold",
        ),
    ]

    snapshot = measure(_ledger(), app_version="0.33.0", today=_TODAY, retirements=proposed)

    assert snapshot.off_board_this_review == 3
    assert snapshot.by_cause == {"stale": 1, "tier-fold": 2}


def test_every_retirement_rule_labels_its_own_cause() -> None:
    """A rule that forgets the label lands in the default bucket and its
    changes are credited to `unsupported`. The count of construction sites is
    small and checkable, so check it."""
    import inspect

    from adoc.casefile import retirement as module

    source = inspect.getsource(module)
    assert source.count("Retirement(\n") == source.count('cause="'), (
        "a Retirement is being built without naming which rule built it"
    )


def test_the_report_names_the_mechanism_in_words() -> None:
    from adoc.casefile.retirement import Retirement

    snapshot = measure(
        _ledger(),
        app_version="0.33.0",
        today=_TODAY,
        retirements=[
            Retirement(
                hypothesis_id="a",
                hypothesis_name="A",
                to_status="parked",
                reason="",
                cause="tier-fold",
            )
        ],
    )

    assert "folded to fit the tier cap" in "\n".join(render(snapshot, None))


def test_the_report_depends_on_the_snapshot_through_the_graph() -> None:
    """ADR 0043: a node whose output the report prints must have an edge
    saying so. Reached through the `results` sink instead, a reordering could
    run the report before the count it prints, and the report would show the
    PREVIOUS review's boundary with this review's date on it.
    """
    import inspect

    from adoc.reason import review as module

    source = inspect.getsource(module.build_review_dag)
    snapshot_decl = source.index('name="convergence_snapshot"')
    report_decl = source.index('name="render_report"')
    deps = source[report_decl : report_decl + 900]

    assert snapshot_decl < report_decl, "the snapshot node must be declared before the report"
    assert '"convergence_snapshot",' in deps, "render_report does not depend on the snapshot"
