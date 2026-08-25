"""Tests for `adoc.reason.verify`: the Phase-2 entailment verifier's source
resolution + report scoring, and the deterministic Composer number check.
No network, ever — `verify_claims`'s model call always goes through a fake
`LlmClient` transport built in this file.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from adoc.casefile.encounters import (
    Encounter,
    EncounterFrontmatter,
    encounter_filename,
    write_encounter,
)
from adoc.casefile.repo import DataRepo
from adoc.casefile.schema import (
    AddEvidence,
    AddHypothesis,
    Evidence,
    Hypothesis,
    RecordChallenge,
)
from adoc.config import ModelBinding
from adoc.labs.db import LabsDb
from adoc.labs.models import LabDocument, LabResult
from adoc.reason.client import (
    LlmClient,
    OpenAIProvider,
    Provider,
    TransportRequest,
    TransportResponse,
)
from adoc.reason.context import ENCOUNTERS_RELDIR
from adoc.reason.verify import (
    Claim,
    ClaimVerification,
    DefaultSourceTextResolver,
    VerificationReport,
    build_composer_number_retry_feedback,
    build_entailment_retry_feedback,
    check_composer_numbers,
    claims_from_ops,
    log_verification_report,
    verify_claims,
)

SHA = "e" * 64


@pytest.fixture
def repo(tmp_path: Path) -> DataRepo:
    return DataRepo.init_at(tmp_path / "data")


@pytest.fixture
def db(tmp_path: Path) -> LabsDb:
    return LabsDb(tmp_path / "labs.sqlite")


def _seed_document(db: LabsDb, *, filename: str = "quest.pdf") -> None:
    db.upsert_document(
        LabDocument(sha256=SHA, filename=filename, doc_type="lab-result", page_count=1)
    )


def _seed_lab_row(
    db: LabsDb,
    *,
    name: str,
    value: float | None = None,
    value_text: str | None = None,
    on: date = date(2026, 5, 2),
    ucum_unit: str | None = None,
    ref_low: float | None = None,
    ref_high: float | None = None,
    flag: str | None = None,
) -> None:
    if db.get_document(SHA) is None:
        _seed_document(db)
    db.insert_results(
        [
            LabResult(
                date=on,
                name=name,
                name_raw=name,
                value=value,
                value_text=value_text,
                ucum_unit=ucum_unit,
                ref_low=ref_low,
                ref_high=ref_high,
                flag=flag,
                source_doc=SHA,
                raw_json=json.dumps({"name_raw": name}),
            )
        ]
    )


# --- claims_from_ops -------------------------------------------------------------------------


def test_claims_from_ops_pulls_evidence_for_and_against_from_add_hypothesis() -> None:
    op = AddHypothesis(
        hypothesis=Hypothesis(
            id="h-01",
            name="Test",
            tier="expanded",
            probability="low",
            status="active",
            origin="model",
            first_proposed=date(2026, 1, 1),
            evidence_for=[
                Evidence(claim="for-claim", source="labs:crp:2026-05-02", strength="moderate")
            ],
            evidence_against=[
                Evidence(claim="against-claim", source="labs:ana:2026-05-02", strength="weak")
            ],
        )
    )
    claims = claims_from_ops([op])
    assert {(c.for_or_against, c.claim) for c in claims} == {
        ("for", "for-claim"),
        ("against", "against-claim"),
    }
    assert all(c.hypothesis_id == "h-01" for c in claims)


def test_claims_from_ops_pulls_add_evidence_and_skips_record_challenge() -> None:
    ops = [
        AddEvidence(
            id="h-01",
            for_or_against="for",
            evidence=Evidence(claim="c", source="labs:crp:2026-05-02", strength="strong"),
        ),
        RecordChallenge(id="h-01", note="a substantive challenge note, long enough"),
    ]
    claims = claims_from_ops(ops)
    assert len(claims) == 1
    assert claims[0].claim == "c"


# --- DefaultSourceTextResolver -------------------------------------------------------------


def test_resolver_renders_labs_row_deterministically(db: LabsDb, repo: DataRepo) -> None:
    _seed_lab_row(db, name="CRP", value=8.5, ucum_unit="mg/L", ref_high=5.0, flag="H")
    resolver = DefaultSourceTextResolver(db, repo)

    text = resolver.resolve("labs:crp:2026-05-02")

    assert text is not None
    assert "8.5" in text
    assert "mg/L" in text
    assert "H" in text


def test_resolver_labs_ref_with_no_matching_row_is_none(db: LabsDb, repo: DataRepo) -> None:
    resolver = DefaultSourceTextResolver(db, repo)
    assert resolver.resolve("labs:made-up-analyte:2026-05-02") is None


def test_resolver_encounter_ref_returns_file_text_when_it_exists(
    db: LabsDb, repo: DataRepo
) -> None:
    encounters_dir = repo.root / ENCOUNTERS_RELDIR
    encounter = Encounter(
        frontmatter=EncounterFrontmatter(date=date(2026, 5, 2), type="specialist-visit"),
        summary="Rheumatology follow-up.",
        new_findings="ANA titer remains elevated at 1:640.",
        plan="Recheck complement panel in 3 months.",
    )
    write_encounter(encounters_dir, encounter, "rheum-followup")
    filename = encounter_filename(encounter.frontmatter, "rheum-followup")

    resolver = DefaultSourceTextResolver(db, repo)
    text = resolver.resolve(f"encounter:{filename}")

    assert text is not None
    assert "1:640" in text
    assert "complement panel" in text


def test_resolver_encounter_ref_missing_file_is_none(db: LabsDb, repo: DataRepo) -> None:
    resolver = DefaultSourceTextResolver(db, repo)
    assert resolver.resolve("encounter:does-not-exist.md") is None


def test_resolver_doc_pmid_patient_report_refs_are_none_today(db: LabsDb, repo: DataRepo) -> None:
    """PLAN.md Phase 2 seam: these resolve to `None` until the parallel
    document-text-corpus workstream lands a richer resolver."""
    resolver = DefaultSourceTextResolver(db, repo)
    assert resolver.resolve("doc:quest.pdf#p1") is None
    assert resolver.resolve("pmid:12345678") is None
    assert resolver.resolve("patient-report:2026-05-02") is None


# --- verify_claims (fake entailment_verifier transport) -------------------------------------


def _build_verifier_client(judgments_by_claim_index: dict[int, str]) -> LlmClient:
    def transport(request: TransportRequest) -> TransportResponse:
        _preamble, _blank, payload_text = request.messages[-1].content.partition("\n\n")
        pairs = json.loads(payload_text)
        judgments = [
            {
                "claim_index": pair["claim_index"],
                "judgment": judgments_by_claim_index.get(pair["claim_index"], "entailed"),
                "rationale": "scripted",
            }
            for pair in pairs
        ]
        return TransportResponse(
            text="", tool_input={"judgments": judgments}, input_tokens=5, output_tokens=5
        )

    bindings: dict[str, list[ModelBinding]] = {
        "entailment_verifier": [ModelBinding(provider="featherless", model="fake-verifier")]
    }
    providers: dict[str, Provider] = {
        "featherless": OpenAIProvider(api_key=None, transport=transport)
    }
    return LlmClient(bindings, providers)


def test_verify_claims_no_claims_never_calls_the_model(db: LabsDb, repo: DataRepo) -> None:
    def exploding(_request: TransportRequest) -> TransportResponse:
        raise AssertionError("must not be called")

    bindings: dict[str, list[ModelBinding]] = {
        "entailment_verifier": [ModelBinding(provider="featherless", model="fake-verifier")]
    }
    client = LlmClient(bindings, {"featherless": OpenAIProvider(api_key=None, transport=exploding)})

    report = verify_claims(client, [], db=db, repo=repo)
    assert report.checks == []


def test_verify_claims_unresolvable_source_is_insufficient_source_and_not_failing(
    db: LabsDb, repo: DataRepo
) -> None:
    def exploding(_request: TransportRequest) -> TransportResponse:
        raise AssertionError("must not be called: no resolvable source text")

    bindings: dict[str, list[ModelBinding]] = {
        "entailment_verifier": [ModelBinding(provider="featherless", model="fake-verifier")]
    }
    client = LlmClient(bindings, {"featherless": OpenAIProvider(api_key=None, transport=exploding)})
    claim = Claim(hypothesis_id="h-01", for_or_against="for", claim="c", source="doc:quest.pdf#p1")

    report = verify_claims(client, [claim], db=db, repo=repo)

    assert len(report.checks) == 1
    assert report.checks[0].judgment == "insufficient_source"
    assert report.failing == []


def test_verify_claims_entailed_and_not_entailed_scored_correctly(
    db: LabsDb, repo: DataRepo
) -> None:
    _seed_lab_row(db, name="CRP", value=8.5)
    _seed_lab_row(db, name="ANA", value_text="1:640")
    claims = [
        Claim(
            hypothesis_id="h-01",
            for_or_against="for",
            claim="CRP elevated",
            source="labs:crp:2026-05-02",
        ),
        Claim(
            hypothesis_id="h-01",
            for_or_against="for",
            claim="ANA is negative",  # a fabricated/contradicted claim
            source="labs:ana:2026-05-02",
        ),
    ]
    client = _build_verifier_client({1: "not_entailed"})

    report = verify_claims(client, claims, db=db, repo=repo)

    assert report.checks[0].judgment == "entailed"
    assert report.checks[1].judgment == "not_entailed"
    assert report.failing == [report.checks[1]]
    assert report.counts == {"entailed": 1, "not_entailed": 1, "insufficient_source": 0}


def test_verify_claims_missing_judgment_fails_closed(db: LabsDb, repo: DataRepo) -> None:
    """A schema-valid but incomplete verifier response (fewer judgments than
    claims sent) must not be silently treated as entailed."""
    _seed_lab_row(db, name="CRP", value=8.5)
    claim = Claim(
        hypothesis_id="h-01", for_or_against="for", claim="c", source="labs:crp:2026-05-02"
    )

    def transport(_request: TransportRequest) -> TransportResponse:
        return TransportResponse(
            text="", tool_input={"judgments": []}, input_tokens=5, output_tokens=5
        )

    bindings: dict[str, list[ModelBinding]] = {
        "entailment_verifier": [ModelBinding(provider="featherless", model="fake-verifier")]
    }
    client = LlmClient(bindings, {"featherless": OpenAIProvider(api_key=None, transport=transport)})

    report = verify_claims(client, [claim], db=db, repo=repo)

    assert report.checks[0].judgment == "not_entailed"
    assert "no judgment" in report.checks[0].rationale


def test_build_entailment_retry_feedback_names_the_failed_claim() -> None:
    report = VerificationReport(
        checks=[
            ClaimVerification(
                source="labs:crp:2026-05-02",
                claim="CRP was sky-high",
                judgment="not_entailed",
                rationale="source shows a normal value",
            )
        ]
    )
    feedback = build_entailment_retry_feedback(report)
    assert "labs:crp:2026-05-02" in feedback
    assert "sky-high" in feedback
    assert "source shows a normal value" in feedback


def test_log_verification_report_appends_jsonl(db: LabsDb, repo: DataRepo) -> None:
    _seed_lab_row(db, name="CRP", value=8.5)
    claim = Claim(
        hypothesis_id="h-01", for_or_against="for", claim="c", source="labs:crp:2026-05-02"
    )
    client = _build_verifier_client({})

    report = verify_claims(client, [claim], db=db, repo=repo)
    log_verification_report(repo, report, dag_node="ledger_maintainer")

    log_path = repo.root / "logs" / "entailment-checks.jsonl"
    assert log_path.exists()
    record = json.loads(log_path.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert record["dag_node"] == "ledger_maintainer"
    assert record["counts"]["entailed"] == 1


# --- check_composer_numbers ------------------------------------------------------------------


def test_composer_numbers_passes_when_no_analytes_in_db(db: LabsDb) -> None:
    check = check_composer_numbers("Your CRP was 8.5 mg/L, elevated.", db)
    assert check.passed


def test_composer_numbers_passes_when_quoted_number_matches_stored_value(db: LabsDb) -> None:
    _seed_lab_row(db, name="CRP", value=8.5)
    check = check_composer_numbers("Your CRP was 8.5 mg/L on your last panel.", db)
    assert check.passed


def test_composer_numbers_flags_a_mismatched_quote(db: LabsDb) -> None:
    _seed_lab_row(db, name="CRP", value=8.5)
    check = check_composer_numbers("Your CRP was 12.0 mg/L, notably elevated.", db)
    assert not check.passed
    assert check.mismatches[0].quoted_number == 12.0
    assert check.mismatches[0].stored_values == [8.5]


def test_composer_numbers_ignores_numbers_not_near_a_known_analyte(db: LabsDb) -> None:
    _seed_lab_row(db, name="CRP", value=8.5)
    check = check_composer_numbers("Please bring 3 forms of ID to your next appointment.", db)
    assert check.passed


def test_composer_numbers_ignores_dates_and_titers(db: LabsDb) -> None:
    _seed_lab_row(db, name="CRP", value=8.5)
    check = check_composer_numbers(
        "Your CRP from 2026-05-02 remains a lead to discuss (compare with a 1:640 ANA titer).",
        db,
    )
    assert check.passed


def test_build_composer_number_retry_feedback_names_the_mismatch(db: LabsDb) -> None:
    _seed_lab_row(db, name="CRP", value=8.5)
    check = check_composer_numbers("Your CRP was 12.0 mg/L.", db)
    feedback = build_composer_number_retry_feedback(check)
    assert "12.0" in feedback
    assert "crp" in feedback.lower()
    assert "8.5" in feedback
