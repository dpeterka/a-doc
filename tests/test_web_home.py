"""Home surface tests: the two dashboard states (intake incomplete vs.
complete), the "what's already on file" strip (including the zero-document
variant), the pending/failed banners, and the "what's new since your last
visit" bookmark.

Owner-observed feedback this suite pins down: a fresh install with
documents and labs already ingested but no diagnostic conversation yet must
show real counts, not render as if nothing exists.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

from fastapi.testclient import TestClient
from web_support import build_app, login, mark_intake_complete

from adoc.casefile.ledger import save_ledger
from adoc.casefile.repo import HISTORY_RELPATH, LEDGER_RELPATH
from adoc.casefile.schema import Hypothesis, Ledger, Provenance
from adoc.intake.facts import AddFact, IntakeFactsStore, NewFact
from adoc.labs.models import ExtractionStatus, LabDocument, LabResult
from adoc.web.casefile_helpers import append_chat_entry, summarize_diff_ops, write_last_seen

SHA = "c" * 64


def _seed_ledger(repo, tiers: dict[str, str]) -> None:
    hypotheses = [
        Hypothesis(
            id=hyp_id,
            name=f"Hypothesis {hyp_id}",
            tier=tier,  # type: ignore[arg-type]
            probability="moderate",
            status="active",
            origin="model",
            first_proposed=date(2026, 1, 1),
        )
        for hyp_id, tier in tiers.items()
    ]
    ledger = Ledger(
        version=3,
        updated=datetime(2026, 6, 1, tzinfo=UTC),
        schema_version=1,
        hypotheses=hypotheses,
    )
    save_ledger(repo.root / LEDGER_RELPATH, ledger)


def _add_fact(repo, fact_id: str, *, section: str = "symptoms", reported_on: date) -> None:
    store = IntakeFactsStore(repo.root)
    provenance = Provenance(
        app_version="test",
        prompt_template_version="1",
        model_id="fake-model",
        dag_node="intake-agent",
        timestamp=datetime.combine(reported_on, datetime.min.time(), tzinfo=UTC),
    )
    store.apply_ops(
        [
            AddFact(
                fact=NewFact(
                    id=fact_id,
                    section=section,
                    kind="symptom",
                    statement=f"Statement for {fact_id}",
                )
            )
        ],
        provenance,
    )
    store.save()


def _seed_document_and_labs(db) -> None:
    db.upsert_document(
        LabDocument(sha256=SHA, filename="doc.pdf", doc_type="lab_report", page_count=1)
    )
    db.insert_results(
        [
            LabResult(
                date=date(2026, 3, 1),
                name="crp",
                name_raw="CRP",
                value=8.0,
                source_doc=SHA,
                extraction_status=ExtractionStatus.AUTO,
                raw_json=json.dumps({}),
            ),
            LabResult(
                date=date(2026, 5, 1),
                name="ana-titer",
                name_raw="ANA",
                value_text="1:640",
                source_doc=SHA,
                extraction_status=ExtractionStatus.AUTO,
                raw_json=json.dumps({}),
            ),
        ]
    )


# --- intake-incomplete dashboard ---------------------------------------------------------


def test_home_incomplete_shows_welcome_panel_and_cta(tmp_path: Path) -> None:
    app, _repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app)
    login(client)

    response = client.get("/")

    assert response.status_code == 200
    body = response.text
    assert "Baseline incomplete" in body
    assert "Start your first visit" in body
    assert 'href="/chat"' in body


def test_home_incomplete_with_zero_documents_shows_add_documents_pointer(tmp_path: Path) -> None:
    app, _repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app)
    login(client)

    response = client.get("/")

    assert response.status_code == 200
    body = response.text
    assert "Add your first document" in body
    assert 'href="/upload"' in body
    assert "ingested" not in body


def test_home_incomplete_with_docs_and_labs_shows_on_file_strip(tmp_path: Path) -> None:
    """The owner's exact broken scenario: docs+labs ingested, no onboarding,
    no diagnostic sessions. Home must show real on-file counts, not render
    empty."""
    app, repo, db, _calls = build_app(tmp_path)
    _seed_document_and_labs(db)
    encounters_dir = repo.root / "case" / "encounters"
    encounters_dir.mkdir(parents=True, exist_ok=True)
    (encounters_dir / "2026-03-01--lab-result.md").write_text(
        "---\ndate: 2026-03-01\ntype: lab-result\n---\n", encoding="utf-8"
    )
    client = TestClient(app)
    login(client)

    response = client.get("/")

    assert response.status_code == 200
    normalized = " ".join(response.text.split())
    assert "Add your first document" not in normalized
    assert "<strong>1</strong> document ingested" in normalized
    assert (
        "<strong>2</strong> lab results across <strong>2</strong> analytes, "
        "March 01, 2026 &ndash; May 01, 2026" in normalized
    )
    assert "<strong>1</strong> encounter on file" in normalized


# --- intake-complete dashboard ------------------------------------------------------------


def test_home_complete_shows_last_conversation_date(tmp_path: Path) -> None:
    app, repo, _db, _calls = build_app(tmp_path)
    mark_intake_complete(repo)
    append_chat_entry(
        repo,
        {
            "timestamp": datetime(2026, 8, 1, 12, 0, tzinfo=UTC).isoformat(),
            "role": "patient",
            "text": "hello",
        },
    )
    client = TestClient(app)
    login(client)

    response = client.get("/")

    assert response.status_code == 200
    assert "Last conversation: August 01, 2026" in response.text


def test_home_complete_shows_fact_counts(tmp_path: Path) -> None:
    app, repo, _db, _calls = build_app(tmp_path)
    mark_intake_complete(repo)
    _add_fact(repo, "old-fact", reported_on=date(2020, 1, 1))
    _add_fact(repo, "recent-fact", reported_on=datetime.now(UTC).date())
    client = TestClient(app)
    login(client)

    response = client.get("/")

    assert response.status_code == 200
    normalized = " ".join(response.text.split())
    assert (
        "<strong>2</strong> patient-reported facts on file, <strong>1</strong> new in the "
        "last 14 days." in normalized
    )
    assert 'href="/onboard/review"' in normalized


def test_home_complete_shows_ledger_summary_counts_and_link(tmp_path: Path) -> None:
    app, repo, _db, _calls = build_app(tmp_path)
    mark_intake_complete(repo)
    _seed_ledger(
        repo,
        {"ml-01": "most-likely", "exp-01": "expanded", "cm-01": "cant-miss"},
    )
    client = TestClient(app)
    login(client)

    response = client.get("/")

    assert response.status_code == 200
    body = response.text
    assert "Version 3" in body
    assert "Most Likely: 1" in body
    assert "Expanded: 1" in body
    assert "Can" in body and "Miss: 1" in body
    assert 'href="/ledger"' in body
    # The home dashboard shows a summary, not the full per-hypothesis
    # listing (that's what `/ledger` is for).
    assert "Hypothesis ml-01" not in body


def test_home_complete_hides_ledger_summary_when_no_hypotheses(tmp_path: Path) -> None:
    app, repo, _db, _calls = build_app(tmp_path)
    mark_intake_complete(repo)
    client = TestClient(app)
    login(client)

    response = client.get("/")

    assert response.status_code == 200
    assert "Current differential" not in response.text


def test_home_complete_shows_open_questions_when_nontrivial(tmp_path: Path) -> None:
    app, repo, _db, _calls = build_app(tmp_path)
    mark_intake_complete(repo)
    repo.write("case/questions-open.md", "# Open Questions\n\n- Ask about thyroid panel\n")
    client = TestClient(app)
    login(client)

    response = client.get("/")

    assert response.status_code == 200
    assert "Ask about thyroid panel" in response.text


def test_home_complete_hides_open_questions_when_trivial(tmp_path: Path) -> None:
    app, repo, _db, _calls = build_app(tmp_path)
    mark_intake_complete(repo)
    client = TestClient(app)
    login(client)

    response = client.get("/")

    assert response.status_code == 200
    assert "Open questions for your next appointment" not in response.text


def test_home_complete_shows_latest_review_link(tmp_path: Path) -> None:
    app, repo, _db, _calls = build_app(tmp_path)
    mark_intake_complete(repo)
    reviews_dir = repo.root / "case" / "reviews"
    reviews_dir.mkdir(parents=True, exist_ok=True)
    (reviews_dir / "2026-06-01-weekly.md").write_text("# Weekly review\n", encoding="utf-8")
    client = TestClient(app)
    login(client)

    response = client.get("/")

    assert response.status_code == 200
    normalized = " ".join(response.text.split())
    assert "June 01, 2026" in normalized
    assert 'href="/reviews/2026-06-01-weekly.md"' in normalized


def test_home_complete_hides_weekly_reviews_block_when_none_exist(tmp_path: Path) -> None:
    app, repo, _db, _calls = build_app(tmp_path)
    mark_intake_complete(repo)
    client = TestClient(app)
    login(client)

    response = client.get("/")

    assert response.status_code == 200
    assert "Latest review:" not in response.text


# --- banners shared by both states --------------------------------------------------------


def test_pending_confirmation_banner_shows_the_count(tmp_path: Path) -> None:
    app, repo, db, _calls = build_app(tmp_path)
    _seed_ledger(repo, {"cm-01": "cant-miss"})
    db.upsert_document(
        LabDocument(sha256=SHA, filename="doc.pdf", doc_type="lab_report", page_count=1)
    )
    db.insert_results(
        [
            LabResult(
                date=date(2026, 5, 1),
                name="crp",
                name_raw="CRP",
                value=8.0,
                source_doc=SHA,
                extraction_status=ExtractionStatus.PENDING,
                raw_json=json.dumps({}),
            )
        ]
    )
    client = TestClient(app)
    login(client)

    response = client.get("/")

    assert "1" in response.text
    assert "review queue" in response.text.lower() or "confirm" in response.text.lower()


def test_failed_documents_banner_shows_the_count(tmp_path: Path) -> None:
    app, repo, _db, _calls = build_app(tmp_path)
    _seed_ledger(repo, {"cm-01": "cant-miss"})
    failed_dir = repo.root / "work" / "failed"
    failed_dir.mkdir(parents=True, exist_ok=True)
    (failed_dir / "junk.pdf").write_bytes(b"junk")
    record = {
        "filename": "junk.pdf",
        "failed_at": "2026-05-01T00:00:00+00:00",
        "reason": "not a pdf",
        "original_inbox_path": "junk.pdf",
    }
    (failed_dir / "failures.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
    client = TestClient(app)
    login(client)

    response = client.get("/")

    assert "couldn't be processed" in response.text
    assert "/failed" in response.text


def test_failed_documents_banner_is_hidden_when_there_are_no_failures(tmp_path: Path) -> None:
    app, repo, _db, _calls = build_app(tmp_path)
    _seed_ledger(repo, {"cm-01": "cant-miss"})
    client = TestClient(app)
    login(client)

    response = client.get("/")

    assert "couldn't be processed" not in response.text


def test_whats_new_shows_ledger_history_since_last_visit(tmp_path: Path) -> None:
    app, repo, _db, _calls = build_app(tmp_path)
    _seed_ledger(repo, {"cm-01": "cant-miss"})

    # Simulate a prior visit a while ago.
    write_last_seen(repo, datetime(2020, 1, 1, tzinfo=UTC))

    history_path = repo.root / HISTORY_RELPATH
    history_path.parent.mkdir(parents=True, exist_ok=True)
    with history_path.open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "resulting_version": 2,
                    "resulting_updated": datetime(2026, 6, 1, tzinfo=UTC).isoformat(),
                    "diff": {"rationale": "New lab result changed the picture", "ops": []},
                }
            )
            + "\n"
        )

    client = TestClient(app)
    login(client)

    first_response = client.get("/")
    assert "New lab result changed the picture" in first_response.text

    second_response = client.get("/")
    assert "New lab result changed the picture" not in second_response.text


# --- fresh install must never 500 ---------------------------------------------------------


def test_fresh_install_home_renders_without_error(tmp_path: Path) -> None:
    """A brand-new `adoc init` repo: no ledger hypotheses, no documents, no
    labs, no intake facts, no chat history, no reviews."""
    app, _repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app)
    login(client)

    response = client.get("/")

    assert response.status_code == 200


def test_a_gloss_only_update_is_not_announced_as_a_change() -> None:
    """The challenge sweep backfills `plain_language` for every hypothesis
    lacking one, so without this the first review after that field shipped
    would report all 26 hypotheses as "changed" — the precise noise this
    summary exists to avoid."""
    gloss_only = {
        "ops": [{"op": "update_hypothesis", "id": "sle-01", "plain_language": "A definition."}]
    }
    substantive = {"ops": [{"op": "update_hypothesis", "id": "sle-01", "probability": "high"}]}

    assert summarize_diff_ops(gloss_only)["changed"] == []
    assert summarize_diff_ops(substantive)["changed"] == ["sle-01"]
