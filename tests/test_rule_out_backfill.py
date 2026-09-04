"""Tests for `casefile.rule_out_backfill` — ADR 0047.

Fake transports throughout; no network.
"""

from __future__ import annotations

from datetime import date

from adoc.casefile.rule_out_backfill import (
    backfill_diff,
    needs_rule_out,
    propose_rule_outs,
)
from adoc.casefile.schema import Evidence, Hypothesis, Ledger, UpdateHypothesis
from adoc.config import ModelBinding
from adoc.reason.client import AnthropicProvider, LlmClient, TransportRequest, TransportResponse


def _hyp(hid: str, *, rule_out: str = "", status: str = "active") -> Hypothesis:
    return Hypothesis(
        id=hid,
        name=f"Condition {hid}",
        tier="expanded",
        probability="low",
        status=status,
        origin="challenger",
        first_proposed=date(2026, 8, 1),
        rule_out=rule_out,
        evidence_for=[
            Evidence(claim="something", source="labs:crp:2026-05-02", strength="moderate")
        ],
    )


def _ledger(*hyps: Hypothesis) -> Ledger:
    return Ledger(version=1, updated=date(2026, 9, 1), hypotheses=list(hyps))


def _client(payloads: list[dict]) -> tuple[LlmClient, list[TransportRequest]]:
    calls: list[TransportRequest] = []
    remaining = list(payloads)

    def transport(request: TransportRequest) -> TransportResponse:
        calls.append(request)
        return TransportResponse(
            text="", tool_input=remaining.pop(0), input_tokens=5, output_tokens=5
        )

    client = LlmClient(
        {"challenger": [ModelBinding(provider="anthropic", model="fake")]},
        {"anthropic": AnthropicProvider(api_key=None, transport=transport)},
    )
    return client, calls


def test_only_leads_with_no_way_to_end_are_targeted() -> None:
    """46 active hypotheses, 0 with a `rule_out` — measured in production
    2026-09-02. The retirement pass has been evaluating a field nothing
    writes."""
    ledger = _ledger(
        _hyp("a"),
        _hyp("b", rule_out="a negative anti-dsDNA"),
        _hyp("c", status="parked"),
        _hyp("d"),
    )

    assert [h.id for h in needs_rule_out(ledger)] == ["a", "d"]


def test_a_proposed_rule_out_becomes_an_update_op() -> None:
    client, calls = _client(
        [{"proposals": [{"id": "a", "rule_out": "a normal serum metanephrines"}]}]
    )

    ops, report = propose_rule_outs(client, _ledger(_hyp("a")))

    assert ops == [UpdateHypothesis(id="a", rule_out="a normal serum metanephrines")]
    assert (report.considered, report.proposed) == (1, 0 + 1)
    assert len(calls) == 1


def test_a_vacuous_rule_out_is_refused_not_written() -> None:
    """ "Further testing" names the wish for a result, not a result. A
    requirement any hypothesis can satisfy is not a requirement."""
    client, _ = _client([{"proposals": [{"id": "a", "rule_out": "further testing"}]}])

    ops, report = propose_rule_outs(client, _ledger(_hyp("a")))

    assert ops == []
    assert report.unusable == 1
    assert report.proposed == 0


def test_declining_a_lead_is_recorded_not_invented() -> None:
    """A wrong rule-out is worse than none: a wrong one retires a live
    lead."""
    client, _ = _client([{"proposals": [{"id": "a", "rule_out": ""}]}])

    ops, report = propose_rule_outs(client, _ledger(_hyp("a")))

    assert ops == []
    assert report.declined == 1


def test_an_unknown_id_is_reported_and_never_applied() -> None:
    """A model naming a hypothesis that does not exist must not create one,
    and must not be silently dropped either."""
    client, _ = _client([{"proposals": [{"id": "nonexistent", "rule_out": "a normal CT"}]}])

    ops, report = propose_rule_outs(client, _ledger(_hyp("a")))

    assert ops == []
    assert report.unknown_ids == ["nonexistent"]


def test_a_failing_batch_does_not_cost_the_other_batches() -> None:
    """One unusable response must not stop the rest — the posture every
    other stage here takes (ADR 0028)."""
    calls: list[TransportRequest] = []
    responses = [None, {"proposals": [{"id": "i", "rule_out": "a normal CT"}]}]

    def transport(request: TransportRequest) -> TransportResponse:
        calls.append(request)
        payload = responses[len(calls) - 1]
        if payload is None:
            raise RuntimeError("provider blew up")
        return TransportResponse(text="", tool_input=payload, input_tokens=5, output_tokens=5)

    client = LlmClient(
        {"challenger": [ModelBinding(provider="anthropic", model="fake")]},
        {"anthropic": AnthropicProvider(api_key=None, transport=transport)},
    )
    ledger = _ledger(*[_hyp(chr(ord("a") + i)) for i in range(9)])

    ops, report = propose_rule_outs(client, ledger, batch_size=8)

    assert report.considered == 9
    assert len(calls) == 2
    assert [op.id for op in ops] == ["i"]


def test_the_diff_carries_provenance_so_the_invariants_see_it() -> None:
    """It lands as an ordinary `LedgerDiff` through `apply_and_save`, not a
    direct write — the ledger's invariants check it like any other change."""
    diff = backfill_diff([UpdateHypothesis(id="a", rule_out="a normal CT")], model_id="fake")

    assert diff.provenance.dag_node == "rule_out_backfill"
    assert diff.provenance.prompt_template_version == "rule_out_backfill@v1"
    assert "ADR 0047" in diff.rationale
    assert len(diff.ops) == 1


def test_the_prompt_names_the_vacuous_forms() -> None:
    """Without this the model supplies them — it is the path of least
    resistance for a lead nobody can actually falsify."""
    from adoc.casefile.rule_out_backfill import _SYSTEM

    for phrase in ("further testing", "more information", "clinical correlation"):
        assert phrase in _SYSTEM
    assert "wrong rule-out" in _SYSTEM


# --- the machine-checkable half (without it, nothing retires) -------------------------------------


def test_prose_alone_does_not_make_a_lead_retirable() -> None:
    """The gap this half exists to close. `retirement._rule_out_met` returns
    immediately unless `rule_out_check` is set — it never reads the prose. A
    backfill writing only `rule_out` would satisfy ADR 0035 and still retire
    nothing, which is the same shape as the evaluator-with-no-writer it was
    meant to fix."""
    from adoc.casefile.retirement import _rule_out_met

    prose_only = _hyp("a", rule_out="a normal serum metanephrines")

    assert prose_only.rule_out_check is None
    assert _rule_out_met(prose_only, {}) is None  # type: ignore[arg-type]


def test_a_check_is_built_when_the_analyte_is_on_file() -> None:
    client, _ = _client(
        [
            {
                "proposals": [
                    {
                        "id": "a",
                        "rule_out": "a normal serum metanephrines",
                        "analyte": "Metanephrines",
                        "operator": "normal",
                    }
                ]
            }
        ]
    )

    ops, report = propose_rule_outs(client, _ledger(_hyp("a")), analytes=["Metanephrines", "CRP"])

    (op,) = ops
    assert op.rule_out_check is not None
    assert op.rule_out_check.analyte == "Metanephrines"
    assert op.rule_out_check.operator == "normal"
    assert report.checkable == 1


def test_an_analyte_not_on_file_is_refused_not_written() -> None:
    """`evaluate_rule_out` treats an analyte with no result as NOT MET, so a
    check naming one is indistinguishable from a working check and can never
    fire — the silent-absence shape this repository keeps hitting. The prose
    is still kept; only the unevaluable check is dropped."""
    client, _ = _client(
        [
            {
                "proposals": [
                    {
                        "id": "a",
                        "rule_out": "a normal serum metanephrines",
                        "analyte": "Metanephrines",
                        "operator": "normal",
                    }
                ]
            }
        ]
    )

    ops, report = propose_rule_outs(client, _ledger(_hyp("a")), analytes=["CRP"])

    (op,) = ops
    assert op.rule_out == "a normal serum metanephrines"
    assert op.rule_out_check is None
    assert report.checkable == 0
    assert report.unknown_analytes == ["Metanephrines"]


def test_a_threshold_operator_without_a_threshold_is_refused() -> None:
    """`RuleOutCheck`'s validator requires it. Refusing here keeps the
    failure a counted outcome rather than an exception mid-batch."""
    client, _ = _client(
        [
            {
                "proposals": [
                    {
                        "id": "a",
                        "rule_out": "a CRP below 5",
                        "analyte": "CRP",
                        "operator": "below",
                    }
                ]
            }
        ]
    )

    ops, _report = propose_rule_outs(client, _ledger(_hyp("a")), analytes=["CRP"])

    (op,) = ops
    assert op.rule_out_check is None


def test_a_non_lab_rule_out_keeps_its_prose_and_no_check() -> None:
    """Imaging, biopsies and examination findings are real rule-outs that no
    lab lookup can answer. They must not be forced into a check."""
    client, _ = _client(
        [
            {
                "proposals": [
                    {
                        "id": "a",
                        "rule_out": "a temporal-bone CT showing no bone erosion",
                        "analyte": "",
                        "operator": "",
                    }
                ]
            }
        ]
    )

    ops, report = propose_rule_outs(client, _ledger(_hyp("a")), analytes=["CRP"])

    (op,) = ops
    assert op.rule_out_check is None
    assert report.proposed == 1
    assert report.checkable == 0
    assert report.unknown_analytes == []


def test_the_prompt_asks_for_both_halves() -> None:
    from adoc.casefile.rule_out_backfill import _SYSTEM

    assert "machine-checkable" in _SYSTEM
    for operator in ("negative", "normal", "below", "above"):
        assert operator in _SYSTEM
    assert "cannot be evaluated" in _SYSTEM


def test_the_prompt_warns_about_the_single_normal_result_trap() -> None:
    """Measured on the real case file: with the analyte-matching bug fixed,
    the backfill would have retired 7 leads immediately — and several of
    those rule-outs are clinically wrong in one specific way. Each treats a
    single current normal value as excluding a condition that is episodic
    (MCAS on a baseline tryptase), historical (bone loss on today's
    estradiol), or defined by repeat testing (POI on one FSH).

    The same shape ADR 0042 found in the criteria scorers — a normal draw is
    frequently an expected treatment effect or a between-episode value, not
    evidence the finding never happened.
    """
    from adoc.casefile.rule_out_backfill import _SYSTEM

    assert "SINGLE NORMAL RESULT IS NOT A RULE-OUT" in _SYSTEM
    for trap in ("tryptase", "estradiol", "FSH"):
        assert trap.lower() in _SYSTEM.lower()
    assert "two occasions" in _SYSTEM
    assert "Do not substitute the convenient single value" in _SYSTEM
