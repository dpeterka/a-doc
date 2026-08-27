"""Ledger surface tests: the read-only full ledger view renders tier/
status/origin chips (including an `origin: patient` chip) and links
evidence source-refs back to their documents where resolvable — and, when
no hypotheses exist yet, an explanation state instead of the "complete,
unfiltered record" framing (which is only true once there's a record).

Also (docs/adr/0019-event-triggered-review.md "UI merge"): `/ledger` now
also shows the latest deep review inline and links prior reviews as
history — the merge of what used to be a separate `/reviews` index page.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from web_support import build_app, login

from adoc.casefile.ledger import save_ledger
from adoc.casefile.repo import LEDGER_RELPATH
from adoc.casefile.schema import Evidence, Hypothesis, Ledger
from adoc.labs.models import LabDocument
from adoc.web.casefile_helpers import group_hypotheses


def _hypothesis(hid: str, *, tier: str, probability: str) -> Hypothesis:
    return Hypothesis(
        id=hid,
        name=f"Lead {hid}",
        tier=tier,
        probability=probability,
        status="active",
        origin="model",
        first_proposed=date(2026, 8, 27),
    )


def test_ledger_view_empty_state_explains_instead_of_claiming_a_record(tmp_path: Path) -> None:
    app, _repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app)
    login(client)

    response = client.get("/ledger")

    assert response.status_code == 200
    body = response.text
    assert "Nothing recorded yet." not in body
    assert "read-only." not in body
    assert "diagnostic conversation" in body
    assert 'href="/chat"' in body


def test_ledger_view_redacts_dosing_language_but_keeps_surrounding_content(tmp_path: Path) -> None:
    """Violation 2 regression: `web/routes/ledger.py` never gated
    `evidence_for[].claim`, `discriminators`, or `challenger_notes` — all
    model-written free text with no gate on their write path. Only the
    offending span should be replaced; the rest of the record stays
    visible and legible."""
    app, repo, _db, _calls = build_app(tmp_path)
    hyp = Hypothesis(
        id="sle-01",
        name="Systemic lupus erythematosus",
        tier="most-likely",
        probability="moderate",
        status="active",
        origin="model",
        first_proposed=date(2026, 1, 1),
        evidence_for=[
            Evidence(
                claim="Patient should take 20 mg prednisone daily per prior records",
                source="labs:ana-titer:2026-05-02",
                strength="strong",
            )
        ],
        discriminators=["Consider tapering prednisone 10 mg to confirm the flare pattern"],
        challenger_notes="Recommend starting 500 mg metformin twice daily",
    )
    ledger = Ledger(version=1, updated=datetime.now(UTC), schema_version=1, hypotheses=[hyp])
    save_ledger(repo.root / LEDGER_RELPATH, ledger)

    client = TestClient(app)
    login(client)

    response = client.get("/ledger")

    assert response.status_code == 200
    body = response.text
    assert "20 mg prednisone" not in body
    assert "10 mg" not in body
    assert "500 mg metformin" not in body
    assert "withheld" in body.lower()
    # Surrounding, non-offending content is preserved, not blanked.
    assert "Systemic lupus erythematosus" in body
    assert "per prior records" in body
    assert "confirm the flare pattern" in body


def test_ledger_view_shows_origin_patient_chip(tmp_path: Path) -> None:
    app, repo, _db, _calls = build_app(tmp_path)
    hyp = Hypothesis(
        id="mcas-01",
        name="Mast cell activation syndrome",
        tier="expanded",
        probability="low",
        status="patient-proposed",
        origin="patient",
        first_proposed=date(2026, 1, 1),
        evidence_for=[
            Evidence(
                claim="Flushing after meals",
                source="patient-report:2026-01-01",
                strength="weak",
            )
        ],
    )
    ledger = Ledger(version=1, updated=datetime.now(UTC), schema_version=1, hypotheses=[hyp])
    save_ledger(repo.root / LEDGER_RELPATH, ledger)

    client = TestClient(app)
    login(client)

    response = client.get("/ledger")

    assert response.status_code == 200
    assert 'data-origin="patient"' in response.text
    assert "origin: patient" in response.text


def test_ledger_view_links_a_labs_source_ref(tmp_path: Path) -> None:
    app, repo, _db, _calls = build_app(tmp_path)
    hyp = Hypothesis(
        id="sle-01",
        name="Systemic lupus erythematosus",
        tier="most-likely",
        probability="moderate",
        status="active",
        origin="model",
        first_proposed=date(2026, 1, 1),
        evidence_for=[
            Evidence(claim="ANA elevated", source="labs:ana-titer:2026-05-02", strength="strong")
        ],
    )
    ledger = Ledger(version=1, updated=datetime.now(UTC), schema_version=1, hypotheses=[hyp])
    save_ledger(repo.root / LEDGER_RELPATH, ledger)

    client = TestClient(app)
    login(client)

    response = client.get("/ledger")

    assert response.status_code == 200
    assert 'href="/labs/ana-titer"' in response.text


# --------------------------------------------------------------------------
# Perf regression guard: resolving `doc:` evidence refs is O(1) in
# `db.list_documents()` queries and page-image directory listings, not
# O(evidence refs).
# --------------------------------------------------------------------------


def test_ledger_view_resolves_many_doc_refs_without_requerying_per_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_source_ref_href` used to call `find_document_by_filename` (a full
    `db.list_documents()` query) and `page_image_url` (a filesystem
    `iterdir()`) fresh for EVERY `doc:` evidence ref — a ledger with many
    hypotheses citing the same document re-ran both once per ref instead
    of once per page render. Both `labs.sqlite` and the data repo live on
    EFS/NFS in the deployed app, where each round trip costs milliseconds.
    """
    app, repo, db, _calls = build_app(tmp_path)
    sha = "c" * 64
    db.upsert_document(
        LabDocument(sha256=sha, filename="doc.pdf", doc_type="lab_report", page_count=1)
    )
    page_dir = repo.root / "sources" / "pages" / sha
    page_dir.mkdir(parents=True, exist_ok=True)
    (page_dir / "p-1.png").write_bytes(b"\x89PNG\r\n\x1a\nfakepngbytes")

    hypotheses = [
        Hypothesis(
            id=f"hyp-{i}",
            name=f"Hypothesis {i}",
            tier="expanded",
            probability="low",
            status="active",
            origin="model",
            first_proposed=date(2026, 1, 1),
            evidence_for=[Evidence(claim=f"Finding {i}", source="doc:doc.pdf#p1", strength="weak")],
        )
        for i in range(15)
    ]
    ledger = Ledger(version=1, updated=datetime.now(UTC), schema_version=1, hypotheses=hypotheses)
    save_ledger(repo.root / LEDGER_RELPATH, ledger)

    document_selects: list[str] = []
    db._conn.set_trace_callback(
        lambda stmt: document_selects.append(stmt) if "FROM documents" in stmt else None
    )
    listings: list[Path] = []
    original_iterdir = Path.iterdir

    def counting_iterdir(self: Path) -> Any:
        if self == page_dir:
            listings.append(self)
        return original_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", counting_iterdir)

    try:
        client = TestClient(app)
        login(client)
        response = client.get("/ledger")
    finally:
        db._conn.set_trace_callback(None)

    assert response.status_code == 200
    assert response.text.count('href="/files/pages/') == 15
    # 15 evidence refs all citing the same document: a per-ref
    # implementation issues 15 `list_documents()` queries and 15 directory
    # listings. The memoized implementation issues exactly one of each.
    assert len(document_selects) == 1, f"expected 1 documents query, got {len(document_selects)}"
    assert len(listings) == 1, f"expected 1 page-image directory listing, got {len(listings)}"


# --------------------------------------------------------------------------
# Merged deep-review section (docs/adr/0019-event-triggered-review.md)
# --------------------------------------------------------------------------


def test_ledger_view_deep_review_empty_state_describes_the_trigger_model(tmp_path: Path) -> None:
    app, _repo, _db, _calls = build_app(tmp_path)
    client = TestClient(app)
    login(client)

    response = client.get("/ledger")

    assert response.status_code == 200
    body = response.text
    assert "blind re-differential panel" in body
    assert "without" in body.lower()
    # Trigger phrasing describes the event-triggered mechanism, not a fixed
    # weekly cron.
    assert "whenever something new arrives" in body
    assert "at most once every 6 hours" in body
    assert "at least once every 7 days" in body
    assert "None have run yet" in body


def test_ledger_view_shows_the_latest_review_inline(tmp_path: Path) -> None:
    app, repo, _db, _calls = build_app(tmp_path)
    reviews_dir = repo.root / "case" / "reviews"
    reviews_dir.mkdir(parents=True, exist_ok=True)
    (reviews_dir / "2026-06-01-review.md").write_text(
        "# Weekly Review — 2026-06-01\n\n_Why this review ran: ingest: 1 new document(s)_\n\n"
        "## What changed this week\n\nNothing new.\n",
        encoding="utf-8",
    )
    client = TestClient(app)
    login(client)

    response = client.get("/ledger")

    assert response.status_code == 200
    body = response.text
    assert "Why this review ran" in body
    assert "ingest: 1 new document(s)" in body
    assert 'href="/reviews/2026-06-01-review.md"' in body
    # The empty-state copy is gone once a real review exists.
    assert "None have run yet" not in body


def test_ledger_view_lists_prior_reviews_as_history_excluding_the_latest(tmp_path: Path) -> None:
    app, repo, _db, _calls = build_app(tmp_path)
    reviews_dir = repo.root / "case" / "reviews"
    reviews_dir.mkdir(parents=True, exist_ok=True)
    (reviews_dir / "2026-05-01-review.md").write_text("# Weekly Review — 2026-05-01\n", "utf-8")
    (reviews_dir / "2026-06-01-review.md").write_text("# Weekly Review — 2026-06-01\n", "utf-8")
    client = TestClient(app)
    login(client)

    response = client.get("/ledger")

    assert response.status_code == 200
    body = response.text
    # Latest review is rendered inline (its own permalink still appears
    # once, as the "full report" link) plus once more in history — the
    # OLDER review appears as a plain history link.
    assert 'href="/reviews/2026-05-01-review.md"' in body
    assert 'href="/reviews/2026-06-01-review.md"' in body


def test_reviews_index_redirect_still_reaches_the_review_list(tmp_path: Path) -> None:
    """Old bookmarked `/reviews` links must not 404 (they may be embedded
    in committed review markdown or a chat transcript entry) — following
    the redirect lands on the same merged page."""
    app, repo, _db, _calls = build_app(tmp_path)
    reviews_dir = repo.root / "case" / "reviews"
    reviews_dir.mkdir(parents=True, exist_ok=True)
    (reviews_dir / "2026-06-01-review.md").write_text("# Weekly Review — 2026-06-01\n", "utf-8")
    client = TestClient(app)
    login(client)

    response = client.get("/reviews")

    assert response.status_code == 200
    assert response.url.path == "/ledger"
    assert 'href="/reviews/2026-06-01-review.md"' in response.text


# --- readability of a real-sized differential -------------------------------------------


def test_a_large_ledger_leads_with_what_matters_and_folds_the_tail() -> None:
    """Production carried 24 hypotheses rendered flat, in file order, with no
    signal about which mattered. For a patient reading her own case file that
    is not merely untidy — it reads as "you might have 24 things"."""
    hyps = [
        _hypothesis("h1", tier="expanded", probability="minimal"),
        _hypothesis("h2", tier="cant-miss", probability="minimal"),
        _hypothesis("h3", tier="expanded", probability="high"),
        _hypothesis("h4", tier="most-likely", probability="moderate"),
        _hypothesis("h5", tier="expanded", probability="low"),
    ]

    groups = group_hypotheses(hyps)

    # most-likely first, then can't-miss at ANY probability, then high/moderate
    assert [h.id for h in groups["leading"]] == ["h4", "h2", "h3"]
    assert [h.id for h in groups["secondary"]] == ["h5", "h1"]


def test_folding_never_drops_a_hypothesis() -> None:
    """A lead stays on the list until evidence rules it out — folding is a
    statement about prominence, not about what the record contains."""
    hyps = [
        _hypothesis(f"h{i}", tier="expanded", probability=p)
        for i, p in enumerate(["high", "low", "minimal", "moderate", "low"])
    ]

    groups = group_hypotheses(hyps)

    assert len(groups["leading"]) + len(groups["secondary"]) == len(hyps)


def test_a_cant_miss_lead_is_never_folded_however_unlikely() -> None:
    """Can't-miss is the tier that exists precisely because low probability
    does not mean low consequence."""
    groups = group_hypotheses([_hypothesis("cm", tier="cant-miss", probability="minimal")])

    assert [h.id for h in groups["leading"]] == ["cm"]
    assert groups["secondary"] == []
