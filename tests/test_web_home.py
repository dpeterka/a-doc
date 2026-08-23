"""Home surface tests: tiers render, baseline-incomplete banner, pending
banner, and the "what's new since your last visit" bookmark.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

from fastapi.testclient import TestClient
from web_support import build_app, login

from adoc.casefile.ledger import save_ledger
from adoc.casefile.repo import HISTORY_RELPATH, LEDGER_RELPATH
from adoc.casefile.schema import Hypothesis, Ledger
from adoc.labs.models import ExtractionStatus, LabDocument, LabResult
from adoc.web.casefile_helpers import write_last_seen

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
    ledger = Ledger(version=1, updated=datetime.now(UTC), schema_version=1, hypotheses=hypotheses)
    save_ledger(repo.root / LEDGER_RELPATH, ledger)


def test_home_renders_three_tiers(tmp_path: Path) -> None:
    app, repo, _db, calls = build_app(tmp_path)
    _seed_ledger(
        repo,
        {"ml-01": "most-likely", "exp-01": "expanded", "cm-01": "cant-miss"},
    )
    client = TestClient(app)
    login(client)

    response = client.get("/")

    assert response.status_code == 200
    body = response.text
    assert "Hypothesis ml-01" in body
    assert "Hypothesis exp-01" in body
    assert "Hypothesis cm-01" in body
    assert "Most Likely" in body
    assert "Expanded" in body
    assert "Can" in body and "Miss" in body
    assert calls == []  # home never calls the LLM


def test_baseline_incomplete_banner_shows_before_onboarding_finishes(
    tmp_path: Path,
) -> None:
    app, repo, _db, _calls = build_app(tmp_path)
    _seed_ledger(repo, {"cm-01": "cant-miss"})
    client = TestClient(app)
    login(client)

    response = client.get("/")

    assert "Baseline incomplete" in response.text


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
