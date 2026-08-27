"""Tests for `adoc.reason.citations`: the Phase-2 deterministic citation
checker. No LLM, no network (`EutilsPmidVerifier`'s one narrow real-transport
test mocks the transport function — never live NCBI).
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from adoc.casefile.repo import DataRepo
from adoc.casefile.schema import (
    AddEvidence,
    AddHypothesis,
    Evidence,
    Hypothesis,
    LedgerDiff,
    Provenance,
)
from adoc.labs.db import LabsDb
from adoc.labs.models import LabDocument, LabResult
from adoc.reason.citations import (
    CITATION_LOG_RELPATH,
    EutilsPmidVerifier,
    _extract_quoted_numbers,
    build_retry_feedback,
    check_diff_citations,
    check_evidence_citations,
    check_ops_citations,
    log_citation_report,
)

SHA = "d" * 64


@pytest.fixture
def repo(tmp_path: Path) -> DataRepo:
    return DataRepo.init_at(tmp_path / "data")


@pytest.fixture
def db(tmp_path: Path) -> LabsDb:
    return LabsDb(tmp_path / "labs.sqlite")


def _seed_document(db: LabsDb, *, filename: str = "quest.pdf", page_count: int = 2) -> None:
    db.upsert_document(
        LabDocument(sha256=SHA, filename=filename, doc_type="lab-result", page_count=page_count)
    )


def _seed_lab_row(
    db: LabsDb,
    *,
    name: str,
    name_raw: str | None = None,
    value: float | None = None,
    value_text: str | None = None,
    on: date = date(2026, 5, 2),
) -> None:
    _ensure_document(db)
    db.insert_results(
        [
            LabResult(
                date=on,
                name=name,
                name_raw=name_raw or name,
                value=value,
                value_text=value_text,
                source_doc=SHA,
                raw_json=json.dumps({"name_raw": name_raw or name}),
            )
        ]
    )


def _ensure_document(db: LabsDb) -> None:
    if db.get_document(SHA) is None:
        _seed_document(db)


def _evidence_op(source: str, claim: str = "a claim") -> AddHypothesis:
    return AddHypothesis(
        hypothesis=Hypothesis(
            id="h-01",
            name="Test hypothesis",
            tier="expanded",
            probability="low",
            status="active",
            origin="model",
            first_proposed=date(2026, 1, 1),
            evidence_for=[Evidence(claim=claim, source=source, strength="moderate")],
        )
    )


# --- labs: ref resolution -----------------------------------------------------------------


def test_labs_ref_resolved_when_row_exists_and_claim_quotes_no_number(
    db: LabsDb, repo: DataRepo
) -> None:
    _seed_lab_row(db, name="ana-titer", value_text="1:640")
    op = _evidence_op("labs:ana-titer:2026-05-02", claim="ANA is elevated")

    report = check_ops_citations([op], db, repo)

    assert len(report.checks) == 1
    assert report.checks[0].outcome == "resolved"
    assert report.failing == []


def test_labs_ref_resolved_when_quoted_number_matches_stored_value(
    db: LabsDb, repo: DataRepo
) -> None:
    _seed_lab_row(db, name="CRP", value=8.5)
    op = _evidence_op("labs:crp:2026-05-02", claim="CRP was 8.5 mg/L, notably elevated")

    report = check_ops_citations([op], db, repo)

    assert report.checks[0].outcome == "resolved"


def test_labs_ref_mismatched_when_quoted_number_disagrees(db: LabsDb, repo: DataRepo) -> None:
    _seed_lab_row(db, name="CRP", value=1.23)
    op = _evidence_op("labs:crp:2026-05-02", claim="CRP was 12.3 mg/L")

    report = check_ops_citations([op], db, repo)

    check = report.checks[0]
    assert check.outcome == "mismatched"
    assert "12.3" in check.reason
    assert "1.23" in check.reason
    assert report.failing == [check]


def test_labs_ref_unresolved_when_no_such_analyte(db: LabsDb, repo: DataRepo) -> None:
    op = _evidence_op("labs:made-up-analyte:2026-05-02", claim="fabricated result")

    report = check_ops_citations([op], db, repo)

    assert report.checks[0].outcome == "unresolved"
    assert "made-up-analyte" in report.checks[0].reason


def test_labs_ref_unresolved_when_right_analyte_wrong_date(db: LabsDb, repo: DataRepo) -> None:
    _seed_lab_row(db, name="CRP", value=8.5, on=date(2026, 5, 2))
    op = _evidence_op("labs:crp:2026-06-01", claim="CRP elevated")

    report = check_ops_citations([op], db, repo)

    assert report.checks[0].outcome == "unresolved"


def test_labs_ref_resolved_via_canonicalize_for_an_uncanonicalized_stored_row(
    db: LabsDb, repo: DataRepo
) -> None:
    """A row stored under its raw pre-canonicalization spelling still
    resolves a ref cited against the `ANALYTE_SPECS` canonical short name
    (PLAN.md: "also accept via labs.validate.canonicalize")."""
    _seed_lab_row(db, name="C-REACTIVE PROTEIN", name_raw="C-REACTIVE PROTEIN", value=8.5)
    op = _evidence_op("labs:crp:2026-05-02", claim="CRP elevated")

    report = check_ops_citations([op], db, repo)

    assert report.checks[0].outcome == "resolved"


def test_labs_ref_ignores_date_and_range_digits_when_extracting_quoted_numbers(
    db: LabsDb, repo: DataRepo
) -> None:
    _seed_lab_row(db, name="CRP", value=8.5)
    op = _evidence_op(
        "labs:crp:2026-05-02",
        claim="As of 2026-05-02, CRP was 8.5 mg/L (reference range 0.0-3.0)",
    )

    report = check_ops_citations([op], db, repo)

    assert report.checks[0].outcome == "resolved"


def test_labs_ref_no_numeric_value_to_compare_still_resolves(db: LabsDb, repo: DataRepo) -> None:
    """A titer/text-only row can't be numerically contradicted — a quoted
    number in the claim with nothing to compare it against resolves rather
    than manufacturing a false mismatch."""
    _seed_lab_row(db, name="ana-titer", value_text="1:640")
    op = _evidence_op("labs:ana-titer:2026-05-02", claim="ANA titer reported at 640")

    report = check_ops_citations([op], db, repo)

    assert report.checks[0].outcome == "resolved"


# --- doc: ref resolution --------------------------------------------------------------------


def test_doc_ref_resolved(db: LabsDb, repo: DataRepo) -> None:
    _seed_document(db, filename="report.pdf", page_count=3)
    op = _evidence_op("doc:report.pdf#p2", claim="see page 2")

    report = check_ops_citations([op], db, repo)

    assert report.checks[0].outcome == "resolved"


def test_doc_ref_unresolved_when_no_such_document(db: LabsDb, repo: DataRepo) -> None:
    op = _evidence_op("doc:nonexistent.pdf#p1", claim="fabricated doc")

    report = check_ops_citations([op], db, repo)

    assert report.checks[0].outcome == "unresolved"


def test_doc_ref_unresolved_when_page_beyond_page_count(db: LabsDb, repo: DataRepo) -> None:
    _seed_document(db, filename="report.pdf", page_count=3)
    op = _evidence_op("doc:report.pdf#p5", claim="see page 5")

    report = check_ops_citations([op], db, repo)

    check = report.checks[0]
    assert check.outcome == "unresolved"
    assert "3 page" in check.reason


# --- encounter: ref resolution --------------------------------------------------------------


def test_encounter_ref_resolved_when_file_exists(db: LabsDb, repo: DataRepo) -> None:
    encounters_dir = repo.root / "case" / "encounters"
    encounters_dir.mkdir(parents=True, exist_ok=True)
    (encounters_dir / "2026-05-02--visit.md").write_text("---\n---\n", encoding="utf-8")
    op = _evidence_op("encounter:2026-05-02--visit.md", claim="per the visit note")

    report = check_ops_citations([op], db, repo)

    assert report.checks[0].outcome == "resolved"


def test_encounter_ref_unresolved_when_file_missing(db: LabsDb, repo: DataRepo) -> None:
    op = _evidence_op("encounter:2026-05-02--missing.md", claim="per a visit that never happened")

    report = check_ops_citations([op], db, repo)

    assert report.checks[0].outcome == "unresolved"


# --- patient-report: ref resolution ----------------------------------------------------------


def test_patient_report_ref_always_resolved(db: LabsDb, repo: DataRepo) -> None:
    op = _evidence_op("patient-report:2026-05-02", claim="patient reports fatigue")

    report = check_ops_citations([op], db, repo)

    assert report.checks[0].outcome == "resolved"


# --- pmid: ref resolution (fake verifier) -----------------------------------------------------


class _FakeVerifier:
    def __init__(self, status: str) -> None:
        self.status = status
        self.calls = 0

    def verify(self, pmid: str) -> str:
        self.calls += 1
        return self.status  # type: ignore[return-value]


def test_pmid_ref_resolved_when_verifier_finds_it(db: LabsDb, repo: DataRepo) -> None:
    op = _evidence_op("pmid:12345678", claim="per the cited literature")
    verifier = _FakeVerifier("found")

    report = check_ops_citations([op], db, repo, pmid_verifier=verifier)

    assert report.checks[0].outcome == "resolved"
    assert verifier.calls == 1


def test_pmid_ref_unresolved_when_verifier_says_not_found(db: LabsDb, repo: DataRepo) -> None:
    op = _evidence_op("pmid:99999999", claim="a fabricated citation")
    verifier = _FakeVerifier("not_found")

    report = check_ops_citations([op], db, repo, pmid_verifier=verifier)

    assert report.checks[0].outcome == "unresolved"
    assert report.failing == report.checks


def test_pmid_ref_unverifiable_and_passes_on_network_error(db: LabsDb, repo: DataRepo) -> None:
    op = _evidence_op("pmid:12345678", claim="per the cited literature")
    verifier = _FakeVerifier("error")

    report = check_ops_citations([op], db, repo, pmid_verifier=verifier)

    assert report.checks[0].outcome == "unverifiable"
    assert report.failing == []  # unverifiable never blocks


def test_pmid_ref_unverifiable_when_no_verifier_configured(db: LabsDb, repo: DataRepo) -> None:
    op = _evidence_op("pmid:12345678", claim="per the cited literature")

    report = check_ops_citations([op], db, repo, pmid_verifier=None)

    assert report.checks[0].outcome == "unverifiable"
    assert report.failing == []


# --- check_diff_citations / AddEvidence coverage ---------------------------------------------


def test_check_diff_citations_covers_add_evidence_ops(db: LabsDb, repo: DataRepo) -> None:
    _seed_lab_row(db, name="CRP", value=8.5)
    diff = LedgerDiff(
        provenance=Provenance(
            app_version="test",
            prompt_template_version="x@v1",
            model_id="fake",
            dag_node="ledger_maintainer",
            timestamp=datetime.now(UTC),
        ),
        rationale="test",
        ops=[
            AddEvidence(
                id="h-01",
                for_or_against="for",
                evidence=Evidence(claim="CRP 8.5", source="labs:crp:2026-05-02", strength="strong"),
            ),
            _evidence_op("labs:made-up:2026-05-02"),
        ],
    )

    report = check_diff_citations(diff, db, repo)

    assert report.counts["resolved"] == 1
    assert report.counts["unresolved"] == 1


def test_check_ops_citations_ignores_ops_without_evidence(db: LabsDb, repo: DataRepo) -> None:
    from adoc.casefile.schema import UpdateHypothesis

    report = check_ops_citations([UpdateHypothesis(id="h-01", tier="expanded")], db, repo)

    assert report.checks == []


# --- build_retry_feedback / log_citation_report -----------------------------------------------


def test_build_retry_feedback_names_failed_refs_and_instructs_a_fix(
    db: LabsDb, repo: DataRepo
) -> None:
    op = _evidence_op("labs:made-up-analyte:2026-05-02", claim="fabricated result")
    report = check_ops_citations([op], db, repo)

    feedback = build_retry_feedback(report)

    assert "labs:made-up-analyte:2026-05-02" in feedback
    assert "unresolved" in feedback
    assert "drop the claim" in feedback.lower() or "drop" in feedback.lower()


def test_log_citation_report_appends_a_jsonl_line_with_counts(db: LabsDb, repo: DataRepo) -> None:
    op = _evidence_op("labs:made-up-analyte:2026-05-02", claim="fabricated result")
    report = check_ops_citations([op], db, repo)

    log_citation_report(repo, report, dag_node="ledger_maintainer")

    log_path = repo.root / CITATION_LOG_RELPATH
    assert log_path.exists()
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["dag_node"] == "ledger_maintainer"
    assert record["counts"]["unresolved"] == 1
    assert len(record["failing"]) == 1


# --- EutilsPmidVerifier (mocked transport only, never live network) --------------------------


def _esummary_json(pmid: str, *, found: bool) -> bytes:
    if found:
        payload = {"result": {"uids": [pmid], pmid: {"title": "A paper"}}}
    else:
        payload = {"result": {"uids": []}}
    return json.dumps(payload).encode("utf-8")


def test_eutils_verifier_found_is_cached_forever(tmp_path: Path) -> None:
    cache_path = tmp_path / "pmid-cache.json"
    calls: list[str] = []

    def transport(url: str) -> bytes:
        calls.append(url)
        return _esummary_json("111", found=True)

    verifier = EutilsPmidVerifier(cache_path, transport=transport)

    assert verifier.verify("111") == "found"
    assert verifier.verify("111") == "found"
    assert len(calls) == 1  # second call served entirely from cache


def test_eutils_verifier_not_found_rejects_and_is_cached_within_ttl(tmp_path: Path) -> None:
    cache_path = tmp_path / "pmid-cache.json"

    def transport(url: str) -> bytes:
        return _esummary_json("222", found=False)

    verifier = EutilsPmidVerifier(cache_path, transport=transport)

    assert verifier.verify("222") == "not_found"

    # Second call must not hit the transport again (cached within the TTL).
    def _exploding_transport(url: str) -> bytes:
        raise AssertionError("must not re-verify a cached not_found within the TTL")

    verifier2 = EutilsPmidVerifier(cache_path, transport=_exploding_transport)
    assert verifier2.verify("222") == "not_found"


def test_eutils_verifier_stale_not_found_cache_entry_is_rechecked(tmp_path: Path) -> None:
    cache_path = tmp_path / "pmid-cache.json"
    stale_checked_at = (datetime.now(UTC) - timedelta(days=31)).isoformat()
    cache_path.write_text(
        json.dumps({"333": {"status": "not_found", "checked_at": stale_checked_at}}),
        encoding="utf-8",
    )
    calls: list[str] = []

    def transport(url: str) -> bytes:
        calls.append(url)
        return _esummary_json("333", found=True)

    verifier = EutilsPmidVerifier(cache_path, transport=transport)

    assert verifier.verify("333") == "found"
    assert len(calls) == 1


def test_eutils_verifier_network_failure_is_error_and_never_cached(tmp_path: Path) -> None:
    cache_path = tmp_path / "pmid-cache.json"

    def exploding_transport(url: str) -> bytes:
        raise TimeoutError("simulated network timeout")

    verifier = EutilsPmidVerifier(cache_path, transport=exploding_transport)

    assert verifier.verify("444") == "error"
    assert verifier.verify("444") == "error"  # never cached; re-tried every time
    assert not cache_path.exists()


def test_doc_ref_without_a_page_resolves_for_an_unpaginated_document(tmp_path: Path) -> None:
    """`#p<int>` is optional. Requiring it assumed every citable document is
    a paginated scan; the document-text corpus made `.docx`/`.txt` records
    citable, and a real run died rejecting a ref to the patient's own
    narrative history document because it has no pages."""
    db = LabsDb(tmp_path / "labs.sqlite")
    db.upsert_document(
        LabDocument(
            sha256="d" * 64,
            filename="Longitudinal Health History.docx",
            doc_type="clinical-note",
            page_count=1,
        )
    )
    repo = DataRepo.init_at(tmp_path / "data")

    ok = check_ops_citations([_evidence_op("doc:Longitudinal Health History.docx")], db, repo)
    assert not ok.failing

    missing = check_ops_citations([_evidence_op("doc:Never Ingested.docx")], db, repo)
    assert missing.failing


# --- narrative-report rows whose "analyte name" is a prose lead-in --------------------
#
# A DEXA/FRAX summary yields rows named like a sentence with the value at the
# end: "10-year probability of hip fracture IS". A model cites the sensible
# slug and drops the dangling connective. That failed to resolve and cost a
# real diagnostic turn 203 seconds at the ledger-maintainer's citation check,
# for a row that was present and correctly cited.


def test_labs_ref_resolves_when_the_stored_name_ends_in_a_connective(
    db: LabsDb, repo: DataRepo
) -> None:
    _seed_lab_row(db, name="10-year probability of hip fracture is", value=0.7)
    op = _evidence_op(
        "labs:10-year-probability-of-hip-fracture:2026-05-02",
        claim="10-year hip fracture probability is low",
    )

    report = check_ops_citations([op], db, repo)

    assert report.checks[0].outcome == "resolved"
    assert report.failing == []


def test_shedding_a_connective_cannot_resolve_onto_a_different_analyte(
    db: LabsDb, repo: DataRepo
) -> None:
    """Only trailing connectives are shed, and the remainder must still match
    in full — so this narrowing cannot let a cited slug land on the wrong
    row, which is the whole point of the citation check."""
    _seed_lab_row(db, name="CRP", value=8.5)
    op = _evidence_op("labs:crp-ratio:2026-05-02", claim="the CRP ratio is high")

    report = check_ops_citations([op], db, repo)

    assert report.checks[0].outcome == "unresolved"


def test_an_exact_analyte_name_still_resolves(db: LabsDb, repo: DataRepo) -> None:
    _seed_lab_row(db, name="CRP", value=8.5)
    op = _evidence_op("labs:crp:2026-05-02", claim="CRP is elevated")

    report = check_ops_citations([op], db, repo)

    assert report.checks[0].outcome == "resolved"


@pytest.mark.parametrize(
    ("claim", "expected"),
    [
        # A number hyphenated to a word names something; it is not a reading.
        ("10-year hip fracture probability is low", []),
        ("6-minute walk test was abnormal", []),
        # ... but a real value in the same claim is still extracted.
        ("25-hydroxy vitamin D was 24.1 ng/mL", [24.1]),
        ("CRP was 8.5 mg/L", [8.5]),
        # A negative T-score is a genuine value, not a compound modifier.
        ("T-score of -1.1", [-1.1]),
        # The MIRROR case: digits at the END of an analyte's name. `-?\d+`
        # read the hyphen as a minus, so this quoted -125.0 and a real
        # citation of a real CA-125 row was dropped on a live review.
        ("CA-125 was normal at 27.7 U/mL", [27.7]),
        ("HLA-B27 negative", []),
        ("IL-6 and CD4 were unremarkable", []),
        # A year introduced by a temporal preposition is a date, not a result.
        ("lumbar spine percent change vs 2024 was -8.2%", [-8.2]),
        ("stable since 2021", []),
        # ...but an unqualified number in that range is still a value: real
        # analytes live there (B12 in the 2000s pg/mL), and discarding those
        # would trade one false positive for a worse false negative.
        ("vitamin B12 measured 2024 pg/mL", [2024.0]),
    ],
)
def test_compound_modifiers_are_not_quoted_values(claim: str, expected: list[float]) -> None:
    """ "10-year probability" against a stored 0.7 is a meaningless
    comparison — the digits are part of a name. Same reason dates and titer
    ratios are stripped."""
    assert _extract_quoted_numbers(claim) == expected


def _dxa_db(tmp_path: Path) -> LabsDb:
    """A stored percent-change row, signed — the shape that broke three real
    citations at once."""
    db = LabsDb(tmp_path / "dxa.sqlite")
    db.upsert_document(
        LabDocument(sha256="d" * 64, filename="dxa.pdf", doc_type="lab-result", page_count=1)
    )
    db.insert_results(
        [
            LabResult(
                date=date(2026, 8, 4),
                name="lumbar-spine-percent-change-vs-2024",
                name_raw="Lumbar Spine % Change vs 2024",
                value=-8.0,
                source_doc="d" * 64,
                raw_json="{}",
            )
        ]
    )
    return db


def test_a_decline_stated_in_words_matches_a_negative_stored_value(
    tmp_path: Path, repo: DataRepo
) -> None:
    """The claim can carry the sign in prose while the row carries it as a
    minus. Three real DXA citations were dropped for quoting 8.0, 6.7 and 7.0
    against stored -8.0, -6.7 and -7.0."""
    db = _dxa_db(tmp_path)
    report = check_evidence_citations(
        [
            Evidence(
                claim="Lumbar spine shows a decline of 8% versus the prior scan",
                source="labs:lumbar-spine-percent-change-vs-2024:2026-08-04",
                strength="moderate",
            )
        ],
        db,
        repo,
    )
    assert not report.failing


def test_a_claimed_rise_still_fails_against_a_stored_fall(tmp_path: Path, repo: DataRepo) -> None:
    """Magnitude is accepted only when the claim says the value fell. A claim
    that it ROSE by 8% is a different assertion from a stored -8.0, and
    must still be caught — otherwise the sign check buys nothing."""
    db = _dxa_db(tmp_path)
    report = check_evidence_citations(
        [
            Evidence(
                claim="Lumbar spine density increased by 8% versus the prior scan",
                source="labs:lumbar-spine-percent-change-vs-2024:2026-08-04",
                strength="moderate",
            )
        ],
        db,
        repo,
    )
    assert report.failing
