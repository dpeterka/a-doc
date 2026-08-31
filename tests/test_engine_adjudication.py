"""Adjudicating the phenotype engines (PLAN.md phase 3, criterion 1).

The model supplies a direction; everything that direction DOES to the ledger
is plain code. This file pins the plain code, because that is where a wrong
answer is silent — a mistaken `opposes` becomes counter-evidence, and
`casefile.retirement` retires on accumulated counter-evidence.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from adoc.casefile.schema import (
    AddEvidence,
    AddHypothesis,
    Evidence,
    Hypothesis,
    Ledger,
    Provenance,
)
from adoc.knowledge.lirical_divergence import LiricalComparison, LiricalFinding
from adoc.reason.engine_adjudication import (
    MAX_ADOPTIONS_PER_REVIEW,
    EngineAdjudicationResult,
    EngineVerdictPayload,
    agreement_evidence,
    build_engine_diff,
    collect_divergences,
    render_engine_adjudication,
    verdicts_to_ops,
)

_TODAY = date(2026, 8, 31)


def _h(hid: str, *, name: str = "", evidence: list[Evidence] | None = None) -> Hypothesis:
    return Hypothesis(
        id=hid,
        name=name or hid.replace("-", " ").title(),
        tier="expanded",
        probability="low",
        status="active",
        origin="model",
        first_proposed=_TODAY,
        evidence_for=evidence or [],
    )


def _ledger(*hypotheses: Hypothesis) -> Ledger:
    return Ledger(version=4, updated=datetime(2026, 8, 31, tzinfo=UTC), hypotheses=list(hypotheses))


def _finding(kind: str, **kwargs: object) -> LiricalFinding:
    return LiricalFinding(kind=kind, **kwargs)  # type: ignore[arg-type]


def _ran(*findings: LiricalFinding) -> LiricalComparison:
    return LiricalComparison(ran=True, findings=list(findings))


def _provenance() -> Provenance:
    return Provenance(
        app_version="0.0.0",
        prompt_template_version="engine_adjudicator@v1",
        model_id="test",
        dag_node="apply_engine_diff",
        timestamp=datetime(2026, 8, 31, tzinfo=UTC),
    )


# -- collecting -------------------------------------------------------------


def test_agreements_are_not_put_to_the_model() -> None:
    """An engine agreeing with the ledger is a fact to record, not a
    judgement to make. Spending a reasoning call to be told "yes, they agree"
    would be waste."""
    comparison = _ran(
        _finding("agreement", disease_name="Sjogren", ledger_hypothesis_id="sjogren"),
        _finding("engine_only", disease_name="Behcet", rank=2, composite_lr=8.0),
    )

    divergences = collect_divergences(comparison, LiricalComparison())

    assert [d.kind for d in divergences] == ["engine_only"]


def test_an_engine_that_did_not_run_contributes_nothing() -> None:
    """`ran=False` is the ordinary state when the sidecar is unreachable."""
    down = LiricalComparison(ran=False, error="sidecar timeout")

    assert collect_divergences(down, down) == []


def test_each_engine_score_is_labelled_in_its_own_units() -> None:
    """Both engines store their score in `composite_lr`, but LIRICAL's is a
    likelihood ratio and the index's is a Resnik similarity. Calling a
    similarity an LR is the unit-blindness the research note argues against.
    """
    lirical = _ran(_finding("engine_only", disease_name="Behcet", composite_lr=12.4))
    semsim = _ran(_finding("engine_only", disease_name="Takayasu", composite_lr=3.81))

    divergences = collect_divergences(lirical, semsim)
    labels = {d.engine: d.score_label for d in divergences}

    assert labels["lirical"] == "LR 12.4"
    assert labels["semsim"] == "similarity 3.81"


# -- agreement evidence (no model call) -------------------------------------


def test_agreement_becomes_supporting_evidence() -> None:
    """24 of 25 hypotheses in production carried no evidence at all. An
    independent engine ranking one is exactly what should survive."""
    ledger = _ledger(_h("sjogren"))
    comparison = _ran(
        _finding(
            "agreement",
            disease_name="Sjogren",
            ledger_hypothesis_id="sjogren",
            rank=2,
            composite_lr=9.5,
        )
    )

    ops = agreement_evidence(comparison, LiricalComparison(), ledger, today=_TODAY)

    assert len(ops) == 1
    op = ops[0]
    assert isinstance(op, AddEvidence)
    assert op.for_or_against == "for"
    assert op.evidence.source == "engine:lirical:2026-08-31"
    assert "rank 2" in op.evidence.claim


def test_agreement_is_not_re_added_every_review() -> None:
    """The engines run weekly and their findings are stable. Without this the
    hypothesis card becomes nothing but engine refs.

    Matched on the engine name, not the whole ref: the ref carries the
    review's date and so differs every single time.
    """
    already = Evidence(
        claim="Independently ranked by lirical.",
        source="engine:lirical:2026-08-24",
        strength="moderate",
    )
    ledger = _ledger(_h("sjogren", evidence=[already]))
    comparison = _ran(
        _finding("agreement", disease_name="Sjogren", ledger_hypothesis_id="sjogren", rank=1)
    )

    assert agreement_evidence(comparison, LiricalComparison(), ledger, today=_TODAY) == []


def test_engine_evidence_is_never_strong() -> None:
    """`casefile.retirement` counts strong evidence double. Two engines must
    not be able to retire a hypothesis between them with no human or lab
    involved."""
    ledger = _ledger(_h("sjogren"))
    comparison = _ran(_finding("agreement", disease_name="Sjogren", ledger_hypothesis_id="sjogren"))

    ops = agreement_evidence(comparison, LiricalComparison(), ledger, today=_TODAY)

    assert all(op.evidence.strength == "moderate" for op in ops)  # type: ignore[union-attr]


# -- directions -------------------------------------------------------------


def test_neutral_changes_nothing() -> None:
    """The correct answer whenever the engine is out of its depth — a
    hypothesis resting on serology can be right and still score zero."""
    ledger = _ledger(_h("sjogren"))
    divergences = collect_divergences(
        _ran(_finding("ledger_only", disease_name="Sjogren", ledger_hypothesis_id="sjogren")),
        LiricalComparison(),
    )
    verdicts = [
        EngineVerdictPayload(
            divergence=divergences[0].id,
            direction="neutral",
            rationale="Diagnosed on serology, which a phenotype-only engine cannot see.",
        )
    ]

    ops, notes = verdicts_to_ops(divergences, verdicts, ledger, today=_TODAY)

    assert ops == []
    assert notes == []


def test_opposes_becomes_counter_evidence() -> None:
    ledger = _ledger(_h("sjogren"))
    divergences = collect_divergences(
        _ran(_finding("ledger_only", disease_name="Sjogren", ledger_hypothesis_id="sjogren")),
        LiricalComparison(),
    )
    verdicts = [
        EngineVerdictPayload(
            divergence=divergences[0].id,
            direction="opposes",
            rationale="None of the characteristic sicca features are recorded.",
        )
    ]

    ops, _ = verdicts_to_ops(divergences, verdicts, ledger, today=_TODAY)

    assert len(ops) == 1
    assert isinstance(ops[0], AddEvidence)
    assert ops[0].for_or_against == "against"


def test_corroborates_on_a_ledger_only_item_is_refused() -> None:
    """Incoherent by construction: `ledger_only` means the engine did NOT
    rank it, so there is nothing to corroborate. Treated as neutral and said
    out loud rather than trusted."""
    ledger = _ledger(_h("sjogren"))
    divergences = collect_divergences(
        _ran(_finding("ledger_only", disease_name="Sjogren", ledger_hypothesis_id="sjogren")),
        LiricalComparison(),
    )
    verdicts = [
        EngineVerdictPayload(
            divergence=divergences[0].id,
            direction="corroborates",
            rationale="The engine strongly supports this hypothesis.",
        )
    ]

    ops, notes = verdicts_to_ops(divergences, verdicts, ledger, today=_TODAY)

    assert ops == []
    assert any("cannot be acted on" in note for note in notes)


# -- adopting a candidate ---------------------------------------------------


def _engine_only(name: str, rank: int = 1) -> list:
    return collect_divergences(
        _ran(_finding("engine_only", disease_name=name, rank=rank, composite_lr=10.0)),
        LiricalComparison(),
    )


def test_a_corroborated_candidate_is_adopted_with_its_citation() -> None:
    divergences = _engine_only("Behcet Disease")
    verdicts = [
        EngineVerdictPayload(
            divergence=divergences[0].id,
            direction="corroborates",
            rationale="Recurrent oral and genital ulceration with uveitis is on file.",
            rule_out="Absence of recurrent oral ulceration over 12 months.",
        )
    ]

    ops, _ = verdicts_to_ops(divergences, verdicts, _ledger(), today=_TODAY)

    assert len(ops) == 1
    op = ops[0]
    assert isinstance(op, AddHypothesis)
    assert op.hypothesis.tier == "expanded", "an engine ranking is a reason to look, not to lead"
    assert op.hypothesis.rule_out
    assert op.hypothesis.evidence_for[0].source == "engine:lirical:2026-08-31"


def test_a_candidate_with_no_rule_out_is_not_adopted() -> None:
    """A hypothesis with no stated way to die will not die — the whole reason
    the ledger stopped converging (research note, part 3a)."""
    divergences = _engine_only("Behcet Disease")
    verdicts = [
        EngineVerdictPayload(
            divergence=divergences[0].id,
            direction="corroborates",
            rationale="Ulceration and uveitis both present.",
            rule_out="   ",
        )
    ]

    ops, notes = verdicts_to_ops(divergences, verdicts, _ledger(), today=_TODAY)

    assert ops == []
    assert any("rule-out" in note for note in notes)


def test_adoptions_are_capped_and_the_rest_are_reported() -> None:
    """Two engines on a broad phenotype can surface a dozen plausible rare
    diseases at once. Adding twelve is the inflation this node exists to
    counteract."""
    divergences = collect_divergences(
        _ran(
            *[
                _finding("engine_only", disease_name=f"Disease {n}", rank=n, composite_lr=9.0)
                for n in range(1, MAX_ADOPTIONS_PER_REVIEW + 4)
            ]
        ),
        LiricalComparison(),
    )
    verdicts = [
        EngineVerdictPayload(
            divergence=d.id,
            direction="corroborates",
            rationale=f"Features consistent with {d.name} are recorded.",
            rule_out=f"A negative confirmatory test for {d.name}.",
        )
        for d in divergences
    ]

    ops, notes = verdicts_to_ops(divergences, verdicts, _ledger(), today=_TODAY)

    assert len(ops) == MAX_ADOPTIONS_PER_REVIEW
    assert len([n for n in notes if "cap" in n]) == len(divergences) - MAX_ADOPTIONS_PER_REVIEW


def test_a_candidate_already_on_the_ledger_is_not_duplicated() -> None:
    divergences = _engine_only("Behcet Disease")
    verdicts = [
        EngineVerdictPayload(
            divergence=divergences[0].id,
            direction="corroborates",
            rationale="Ulceration and uveitis both present.",
            rule_out="No recurrent ulceration in 12 months.",
        )
    ]

    ops, _ = verdicts_to_ops(divergences, verdicts, _ledger(_h("behcet-disease")), today=_TODAY)

    assert ops == []


def test_a_verdict_for_an_unknown_divergence_is_ignored_and_noted() -> None:
    """The model returns ids; a hallucinated one must not become a silent
    no-op."""
    ops, notes = verdicts_to_ops(
        _engine_only("Behcet Disease"),
        [
            EngineVerdictPayload(
                divergence="lirical:engine_only:not-a-real-divergence",
                direction="corroborates",
                rationale="...",
                rule_out="...",
            )
        ],
        _ledger(),
        today=_TODAY,
    )

    assert ops == []
    assert any("unknown divergence" in note for note in notes)


# -- the diff ---------------------------------------------------------------


def test_no_ops_means_no_diff() -> None:
    """An ordinary review. `None` rather than an empty diff, so the caller
    does not write a ledger version that changed nothing."""
    result = EngineAdjudicationResult(ran=True)

    diff, _ = build_engine_diff(
        result,
        LiricalComparison(),
        LiricalComparison(),
        _ledger(_h("sjogren")),
        today=_TODAY,
        provenance=_provenance(),
    )

    assert diff is None


def test_the_diff_carries_agreement_and_verdicts_together() -> None:
    ledger = _ledger(_h("sjogren"), _h("lupus"))
    lirical = _ran(
        _finding("agreement", disease_name="Sjogren", ledger_hypothesis_id="sjogren", rank=1),
        _finding("ledger_only", disease_name="Lupus", ledger_hypothesis_id="lupus"),
    )
    divergences = collect_divergences(lirical, LiricalComparison())
    result = EngineAdjudicationResult(
        ran=True,
        divergences=divergences,
        verdicts=[
            EngineVerdictPayload(
                divergence=divergences[0].id,
                direction="opposes",
                rationale="No malar rash, photosensitivity or serositis on file.",
            )
        ],
    )

    diff, _ = build_engine_diff(
        result, lirical, LiricalComparison(), ledger, today=_TODAY, provenance=_provenance()
    )

    assert diff is not None
    kinds = {(op.id, op.for_or_against) for op in diff.ops}  # type: ignore[union-attr]
    assert kinds == {("sjogren", "for"), ("lupus", "against")}


# -- rendering --------------------------------------------------------------


def test_nothing_to_say_renders_nothing() -> None:
    assert render_engine_adjudication(EngineAdjudicationResult(), []) == []


def test_the_report_says_what_was_considered_and_not_acted_on() -> None:
    """A verdict that changed nothing is a real outcome. Dropping it silently
    makes the stage look like it did less than it did."""
    divergences = _engine_only("Behcet Disease")
    result = EngineAdjudicationResult(
        ran=True,
        divergences=divergences,
        verdicts=[
            EngineVerdictPayload(
                divergence=divergences[0].id,
                direction="neutral",
                rationale="Phenotype overlap is generic and shared with much of the ledger.",
            )
        ],
    )

    rendered = "\n".join(render_engine_adjudication(result, ["Behcet Disease: not adopted"]))

    assert "neutral" in rendered
    assert "Considered and not acted on" in rendered
    assert "Behcet Disease" in rendered


# -- the DAG contract -------------------------------------------------------
#
# The node's postcondition, exercised directly. Its sibling
# `adjudication_covers_every_divergence` is pinned the same way in
# tests/test_review.py; this is the engine-side equivalent.

from adoc.reason.review import _engine_adjudication_completeness_contract  # noqa: E402


def _contract_error(result: EngineAdjudicationResult) -> str | None:
    return _engine_adjudication_completeness_contract().check({}, result)


def _two_divergences() -> list:
    return collect_divergences(
        _ran(
            _finding("engine_only", disease_name="Behcet", rank=1, composite_lr=9.0),
            _finding("engine_only", disease_name="Takayasu", rank=2, composite_lr=7.0),
        ),
        LiricalComparison(),
    )


def test_the_contract_passes_when_every_divergence_is_covered() -> None:
    divergences = _two_divergences()
    result = EngineAdjudicationResult(
        ran=True,
        divergences=divergences,
        verdicts=[
            EngineVerdictPayload(
                divergence=d.id,
                direction="neutral",
                rationale=f"The phenotype overlap with {d.name} is generic and non-specific.",
            )
            for d in divergences
        ],
    )

    assert _contract_error(result) is None


def test_the_contract_fires_when_a_divergence_is_skipped() -> None:
    """A stage allowed to skip the awkward divergences is not adjudicating."""
    divergences = _two_divergences()
    result = EngineAdjudicationResult(
        ran=True,
        divergences=divergences,
        verdicts=[
            EngineVerdictPayload(
                divergence=divergences[0].id,
                direction="neutral",
                rationale="The phenotype overlap here is generic and non-specific.",
            )
        ],
    )

    assert "no verdict for engine divergence" in (_contract_error(result) or "")


def test_the_contract_fires_on_one_rationale_reused_everywhere() -> None:
    """The specific failure mode for engine output: a model that finds it hard
    to reason about will answer "neutral, the engine did not rank it" for
    every item, which is a restatement of the input."""
    divergences = _two_divergences()
    result = EngineAdjudicationResult(
        ran=True,
        divergences=divergences,
        verdicts=[
            EngineVerdictPayload(
                divergence=d.id,
                direction="neutral",
                rationale="The engine did not rank this hypothesis highly enough.",
            )
            for d in divergences
        ],
    )

    assert "identical across every divergence" in (_contract_error(result) or "")


def test_the_contract_fires_on_a_throwaway_rationale() -> None:
    divergences = _two_divergences()
    result = EngineAdjudicationResult(
        ran=True,
        divergences=divergences,
        verdicts=[
            EngineVerdictPayload(divergence=divergences[0].id, direction="neutral", rationale="no"),
            EngineVerdictPayload(
                divergence=divergences[1].id,
                direction="neutral",
                rationale="Generic overlap only; nothing specific to this condition is recorded.",
            ),
        ],
    )

    assert "too short" in (_contract_error(result) or "")


def test_the_contract_tolerates_the_engines_being_down() -> None:
    """Unlike its panel-side sibling. A sidecar timeout is an ordinary review,
    not a contract breach — there is nothing to cover."""
    assert _contract_error(EngineAdjudicationResult(ran=False, error="sidecar timeout")) is None
    assert _contract_error(EngineAdjudicationResult(ran=True)) is None


def test_the_contract_rejects_a_wrong_artifact_type() -> None:
    assert "did not produce" in (_engine_adjudication_completeness_contract().check({}, None) or "")


# -- reference paths must not need a data repo -------------------------------
#
# This bug landed three separate times before it was named, each time
# swallowed by a broad `except` that then reported the wrong cause.


def test_a_reference_path_resolves_with_no_data_repo_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`Settings` has no default for `data_dir` and raises without one.

    Every ontology and index path on it is an absolute build artifact with
    nothing to do with patient data, so reading one must not be coupled to
    having a configured repo. It was: the review's LIRICAL node called
    `Settings().mondo_index_path` INSIDE the try block that must never fail a
    review, so with no data dir the comparison reported "the phenotype engine
    did not run" — naming the wrong cause entirely.
    """
    import adoc.config as config

    class _NeedsADataRepo:
        model_fields = config.Settings.model_fields

        def __init__(self, *_a: object, **_k: object) -> None:
            raise ValueError("data_dir: Field required")

    monkeypatch.setattr(config, "Settings", _NeedsADataRepo)

    for field in ("mondo_index_path", "semsim_index_path", "hpo_index_path"):
        resolved = config.reference_path(field)
        assert resolved == Path(str(config.Settings.model_fields[field].default))
        assert resolved.is_absolute(), "a reference artifact path must not depend on the cwd"
