"""Tests for `adoc.reason.verify`: the Phase-2 entailment verifier's source
resolution + report scoring, and the deterministic Composer number check.
No network, ever — `verify_claims`'s model call always goes through a fake
`LlmClient` transport built in this file.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
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
from adoc.labs.db import DocumentTextPage, LabsDb
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
    log_stripped_claims,
    log_verification_report,
    strip_not_entailed_ops,
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


def test_resolver_doc_ref_resolves_against_stored_document_text(db: LabsDb, repo: DataRepo) -> None:
    """The document-text corpus (ADR 0015) has landed, so a `doc:` ref now
    resolves to the cited document's real extracted text — and to the cited
    PAGE's text when the corpus stored pages separately, since a claim is
    far easier to judge against one page than against the whole document."""
    _seed_document(db)
    db.replace_document_text(
        SHA,
        [
            DocumentTextPage(page=1, text="Impression: findings consistent with thyroiditis."),
            DocumentTextPage(page=2, text="Addendum: no evidence of malignancy."),
        ],
        extracted_at=datetime(2026, 5, 2, tzinfo=UTC),
    )
    resolver = DefaultSourceTextResolver(db, repo)

    page1 = resolver.resolve("doc:quest.pdf#p1")
    assert page1 is not None
    assert "thyroiditis" in page1
    assert "malignancy" not in page1  # scoped to the cited page, not the whole doc

    page2 = resolver.resolve("doc:quest.pdf#p2")
    assert page2 is not None
    assert "malignancy" in page2

    whole = resolver.resolve("doc:quest.pdf")
    assert whole is not None
    assert "thyroiditis" in whole and "malignancy" in whole


def test_resolver_doc_ref_is_none_when_no_text_was_extracted(db: LabsDb, repo: DataRepo) -> None:
    """An image-only scan with no text layer — and, by construction, every
    genomic file (ADR 0015 never extracts their text) — yields
    `insufficient_source`, never a rejection."""
    _seed_document(db)
    resolver = DefaultSourceTextResolver(db, repo)
    assert resolver.resolve("doc:quest.pdf#p1") is None
    assert resolver.resolve("doc:never-ingested.pdf#p1") is None


def test_resolver_pmid_and_patient_report_refs_stay_none(db: LabsDb, repo: DataRepo) -> None:
    """A PMID's abstract is not stored locally (the citation checker only
    proves the id exists), and a patient-report ref cites the patient's own
    statement — there is no external source text to entail against."""
    resolver = DefaultSourceTextResolver(db, repo)
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


def test_verify_claims_propagates_hypothesis_id_onto_each_check(db: LabsDb, repo: DataRepo) -> None:
    _seed_lab_row(db, name="CRP", value=8.5)
    claim = Claim(
        hypothesis_id="h-01", for_or_against="for", claim="c", source="labs:crp:2026-05-02"
    )
    client = _build_verifier_client({})

    report = verify_claims(client, [claim], db=db, repo=repo)

    assert report.checks[0].hypothesis_id == "h-01"


def test_verify_claims_propagates_hypothesis_id_for_insufficient_source(
    db: LabsDb, repo: DataRepo
) -> None:
    def exploding(_request: TransportRequest) -> TransportResponse:
        raise AssertionError("must not be called: no resolvable source text")

    bindings: dict[str, list[ModelBinding]] = {
        "entailment_verifier": [ModelBinding(provider="featherless", model="fake-verifier")]
    }
    client = LlmClient(bindings, {"featherless": OpenAIProvider(api_key=None, transport=exploding)})
    claim = Claim(
        hypothesis_id="h-02", for_or_against="for", claim="c", source="doc:missing.pdf#p1"
    )

    report = verify_claims(client, [claim], db=db, repo=repo)

    assert report.checks[0].judgment == "insufficient_source"
    assert report.checks[0].hypothesis_id == "h-02"


# --- VerificationReport.all_not_entailed (ADR 0016 revised) -------------------------------------


def test_all_not_entailed_true_when_every_claim_not_entailed() -> None:
    report = VerificationReport(
        checks=[
            ClaimVerification(source="labs:a:2026-05-02", claim="a", judgment="not_entailed"),
            ClaimVerification(source="labs:b:2026-05-02", claim="b", judgment="not_entailed"),
        ]
    )
    assert report.all_not_entailed is True


def test_all_not_entailed_false_when_an_insufficient_source_claim_survives() -> None:
    """A mix of not_entailed and insufficient_source is NOT "nothing
    survives" - insufficient_source is kept, so this must not be treated as
    the pipeline-is-broken case."""
    report = VerificationReport(
        checks=[
            ClaimVerification(source="labs:a:2026-05-02", claim="a", judgment="not_entailed"),
            ClaimVerification(
                source="doc:missing.pdf#p1", claim="b", judgment="insufficient_source"
            ),
        ]
    )
    assert report.all_not_entailed is False


def test_all_not_entailed_false_when_an_entailed_claim_survives() -> None:
    report = VerificationReport(
        checks=[
            ClaimVerification(source="labs:a:2026-05-02", claim="a", judgment="not_entailed"),
            ClaimVerification(source="labs:b:2026-05-02", claim="b", judgment="entailed"),
        ]
    )
    assert report.all_not_entailed is False


def test_all_not_entailed_false_when_no_claims_at_all() -> None:
    assert VerificationReport(checks=[]).all_not_entailed is False


# --- strip_not_entailed_ops (ADR 0016 revised, "strip, don't reject") ---------------------------


def _hyp_op(
    *, evidence_for: list[Evidence] | None = None, evidence_against: list[Evidence] | None = None
) -> AddHypothesis:
    return AddHypothesis(
        hypothesis=Hypothesis(
            id="h-01",
            name="Test",
            tier="most-likely",
            probability="moderate",
            status="active",
            origin="model",
            first_proposed=date(2026, 1, 1),
            evidence_for=evidence_for or [],
            evidence_against=evidence_against or [],
        )
    )


def test_strip_not_entailed_ops_drops_evidence_for_item_but_keeps_hypothesis() -> None:
    good = Evidence(claim="good claim", source="labs:crp:2026-05-02", strength="strong")
    bad = Evidence(claim="bad claim", source="labs:ana:2026-05-02", strength="strong")
    op = _hyp_op(evidence_for=[good, bad])
    report = VerificationReport(
        checks=[
            ClaimVerification(source=good.source, claim=good.claim, judgment="entailed"),
            ClaimVerification(source=bad.source, claim=bad.claim, judgment="not_entailed"),
        ]
    )

    stripped_ops, removed = strip_not_entailed_ops([op], report)

    assert len(stripped_ops) == 1
    stripped_hyp = stripped_ops[0]
    assert isinstance(stripped_hyp, AddHypothesis)
    assert [e.claim for e in stripped_hyp.hypothesis.evidence_for] == ["good claim"]
    assert [c.claim for c in removed] == ["bad claim"]


def test_strip_not_entailed_ops_drops_evidence_against_item() -> None:
    bad = Evidence(claim="bad against", source="labs:ana:2026-05-02", strength="weak")
    op = _hyp_op(evidence_against=[bad])
    report = VerificationReport(
        checks=[ClaimVerification(source=bad.source, claim=bad.claim, judgment="not_entailed")]
    )

    stripped_ops, removed = strip_not_entailed_ops([op], report)

    stripped_hyp = stripped_ops[0]
    assert isinstance(stripped_hyp, AddHypothesis)
    assert stripped_hyp.hypothesis.evidence_against == []
    assert len(removed) == 1


def test_strip_not_entailed_ops_drops_add_evidence_op_entirely() -> None:
    bad = Evidence(claim="bad claim", source="labs:ana:2026-05-02", strength="strong")
    op = AddEvidence(id="h-01", for_or_against="for", evidence=bad)
    report = VerificationReport(
        checks=[ClaimVerification(source=bad.source, claim=bad.claim, judgment="not_entailed")]
    )

    stripped_ops, removed = strip_not_entailed_ops([op], report)

    assert stripped_ops == []
    assert len(removed) == 1


def test_strip_not_entailed_ops_keeps_insufficient_source_and_entailed_unchanged() -> None:
    entailed = Evidence(claim="entailed claim", source="labs:crp:2026-05-02", strength="strong")
    insufficient = Evidence(
        claim="insufficient claim", source="doc:missing.pdf#p1", strength="moderate"
    )
    op = _hyp_op(evidence_for=[entailed, insufficient])
    report = VerificationReport(
        checks=[
            ClaimVerification(source=entailed.source, claim=entailed.claim, judgment="entailed"),
            ClaimVerification(
                source=insufficient.source,
                claim=insufficient.claim,
                judgment="insufficient_source",
            ),
        ]
    )

    stripped_ops, removed = strip_not_entailed_ops([op], report)

    stripped_hyp = stripped_ops[0]
    assert isinstance(stripped_hyp, AddHypothesis)
    assert {e.claim for e in stripped_hyp.hypothesis.evidence_for} == {
        "entailed claim",
        "insufficient claim",
    }
    assert removed == []


def test_strip_not_entailed_ops_no_not_entailed_claims_is_a_no_op() -> None:
    good = Evidence(claim="good claim", source="labs:crp:2026-05-02", strength="strong")
    op = _hyp_op(evidence_for=[good])
    report = VerificationReport(
        checks=[ClaimVerification(source=good.source, claim=good.claim, judgment="entailed")]
    )

    stripped_ops, removed = strip_not_entailed_ops([op], report)

    assert stripped_ops == [op]
    assert removed == []


def test_log_stripped_claims_appends_jsonl(repo: DataRepo) -> None:
    checks = [
        ClaimVerification(
            source="labs:ana:2026-05-02",
            claim="bad claim",
            judgment="not_entailed",
            rationale="does not match",
            hypothesis_id="h-01",
        )
    ]

    log_stripped_claims(repo, checks, dag_node="ledger_maintainer")

    log_path = repo.root / "logs" / "entailment-stripped.jsonl"
    assert log_path.exists()
    record = json.loads(log_path.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert record["dag_node"] == "ledger_maintainer"
    assert record["count"] == 1
    assert record["stripped"][0]["hypothesis_id"] == "h-01"


def test_log_stripped_claims_no_op_when_nothing_stripped(repo: DataRepo) -> None:
    log_stripped_claims(repo, [], dag_node="ledger_maintainer")
    assert not (repo.root / "logs" / "entailment-stripped.jsonl").exists()


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


# --- check_composer_numbers: count/frequency/duration false-positive fix (ADR 0016 revised) ---


def test_composer_numbers_ignores_a_count_of_panels(db: LabsDb) -> None:
    """The exact false positive a code review caught: '3' here is a COUNT
    OF PANELS, not a CRP value, even though it shares a clause with 'CRP'."""
    _seed_lab_row(db, name="CRP", value=8.5)
    check = check_composer_numbers("Your CRP has been elevated across 3 separate panels.", db)
    assert check.passed


def test_composer_numbers_ignores_a_count_of_occasions(db: LabsDb) -> None:
    _seed_lab_row(db, name="CRP", value=8.5)
    check = check_composer_numbers("Your CRP was elevated on 2 occasions this year.", db)
    assert check.passed


def test_composer_numbers_ignores_a_duration_in_weeks(db: LabsDb) -> None:
    _seed_lab_row(db, name="CRP", value=8.5)
    check = check_composer_numbers("Your CRP has stayed high for 6 weeks now.", db)
    assert check.passed


def test_composer_numbers_ignores_a_count_of_times(db: LabsDb) -> None:
    _seed_lab_row(db, name="CRP", value=8.5)
    check = check_composer_numbers("Your CRP has been checked 4 times this year.", db)
    assert check.passed


def test_composer_numbers_ignores_reference_range_restated_in_prose(db: LabsDb) -> None:
    """A range-shaped mention ('0.0-5.0') must not be split into two
    numbers that then get checked as if either were a quoted CRP value
    (mirrors `reason.citations`'s own range-stripping, now reused here)."""
    _seed_lab_row(db, name="CRP", value=8.5)
    check = check_composer_numbers(
        "Your CRP remains above the reference range of 0.0-5.0 mg/L.", db
    )
    assert check.passed


def test_composer_numbers_still_catches_a_fabricated_value_with_no_unit(db: LabsDb) -> None:
    """The genuine catch this check exists for must never regress: a
    fabricated value with no unit and no count/frequency word after it is
    still flagged, exactly as before the false-positive fix."""
    _seed_lab_row(db, name="CRP", value=8.5)
    check = check_composer_numbers("Your CRP was 12.0, notably elevated.", db)
    assert not check.passed
    assert check.mismatches[0].quoted_number == 12.0


def test_composer_numbers_still_catches_a_fabricated_value_with_a_unit(db: LabsDb) -> None:
    """A value directly followed by one of this patient's own recorded
    units is positive evidence it's a value candidate, so a mismatch there
    is still caught (unaffected by the count/frequency exclusion)."""
    _seed_lab_row(db, name="CRP", value=8.5, ucum_unit="mg/L")
    check = check_composer_numbers("Your CRP was 12.0 mg/L, notably elevated.", db)
    assert not check.passed
    assert check.mismatches[0].quoted_number == 12.0


def test_composer_numbers_a_real_value_and_a_count_in_the_same_reply(db: LabsDb) -> None:
    """A value and an unrelated count can appear in the same reply without
    either one contaminating the other's check."""
    _seed_lab_row(db, name="CRP", value=8.5)
    check = check_composer_numbers(
        "Your CRP was 8.5 mg/L, checked on 3 separate occasions this year.", db
    )
    assert check.passed


# --- check_composer_numbers: positive-evidence design (ADR 0016 revised, second pass) ---------
#
# A live diagnostic run was lost when the first-pass exclusion-list design
# flagged BOTH "40" (a percent CHANGE) and "2024" (a YEAR) in "Ferritin
# dropped by 40% since 2024" as claimed Ferritin values. These cases pin
# the inverted, positive-evidence design that fixes it.


def test_composer_numbers_ignores_a_percent_change_and_a_bare_year(db: LabsDb) -> None:
    """The exact production regression: neither the percent CHANGE nor the
    YEAR is the analyte's value, and neither carries positive evidence
    (no unit, no copula tying it to Ferritin)."""
    _seed_lab_row(db, name="Ferritin", value=150.0, ucum_unit="ng/mL")
    check = check_composer_numbers("Ferritin dropped by 40% since 2024.", db)
    assert check.passed


def test_composer_numbers_checks_a_percent_unit_analyte_and_catches_fabrication(
    db: LabsDb,
) -> None:
    """A `%`-suffixed number is NOT unconditionally excluded — when the
    analyte's own recorded unit genuinely is '%' (e.g. an iron saturation
    or a differential count), a %-suffixed number is still checked, and a
    fabricated one is still caught, decided from stored data rather than a
    hardcoded analyte name."""
    _seed_lab_row(db, name="Iron Saturation", value=25.0, ucum_unit="%")
    check = check_composer_numbers("Your Iron Saturation was 40%, notably elevated.", db)
    assert not check.passed
    assert check.mismatches[0].quoted_number == 40.0
    assert check.mismatches[0].stored_values == [25.0]


def test_composer_numbers_percent_unit_analyte_correct_value_passes(db: LabsDb) -> None:
    _seed_lab_row(db, name="Iron Saturation", value=25.0, ucum_unit="%")
    check = check_composer_numbers("Your Iron Saturation was 25%, within range.", db)
    assert check.passed


def test_composer_numbers_ignores_a_duration_even_after_the_copula_at(db: LabsDb) -> None:
    """ "at" is a copula word ("at 22" is the canonical positive-evidence
    example), but "at 6 weeks" must still be excluded by the trailing
    duration-word veto — a copula alone does not override it."""
    _seed_lab_row(db, name="Ferritin", value=150.0, ucum_unit="ng/mL")
    check = check_composer_numbers("Your Ferritin was drawn at 6 weeks.", db)
    assert check.passed


def test_composer_numbers_ignores_a_year_after_a_copula_with_no_unit(db: LabsDb) -> None:
    """Common English date phrasing can borrow a copula word ("...reading
    was 2024") with no unit attached — the year veto guards exactly this
    copula-only case."""
    _seed_lab_row(db, name="Ferritin", value=150.0, ucum_unit="ng/mL")
    check = check_composer_numbers("Your Ferritin reading was 2024 this visit.", db)
    assert check.passed


def test_composer_numbers_still_catches_a_year_range_value_when_a_unit_is_attached(
    db: LabsDb,
) -> None:
    """The year veto only guards copula-only evidence: a number directly
    followed by this analyte's own unit is unambiguous regardless of
    magnitude, so a fabricated 4-digit-looking value with a real unit is
    still caught."""
    _seed_lab_row(db, name="B12", value=500.0, ucum_unit="pg/mL")
    check = check_composer_numbers("Your B12 was 2024 pg/mL, dramatically high.", db)
    assert not check.passed
    assert check.mismatches[0].quoted_number == 2024.0
