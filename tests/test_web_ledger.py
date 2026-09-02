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

from adoc.casefile.ledger import load_ledger, save_ledger
from adoc.casefile.repo import LEDGER_RELPATH, DataRepo
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
    # The chip is labelled "raised by:" rather than "origin:" — the machine
    # word was in the patient's face. `data-origin` above is the stable hook;
    # this asserts the human-readable label is present, not its exact phrasing.
    assert "raised by:" in response.text
    assert "you" in response.text.lower()


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

    # most-likely first, then substantiated leads, and an UNCITED can't-miss
    # last within the group (ADR 0037). `h2` is can't-miss at `minimal` with
    # no evidence — a placeholder the challenger raised as a safety net. It
    # stays in the leading group and is never hidden; it is simply not what
    # she reads first.
    assert [h.id for h in groups["leading"]] == ["h4", "h3", "h2"]
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


def test_challenge_notes_render_as_separate_labelled_blocks(tmp_path: Path) -> None:
    """`challenger_notes` accumulates one entry per review, joined by "\\n",
    and the card used to dump the whole accumulation into a single `<p>`.

    Three challenges from three different weeks arrived as one unbroken
    paragraph, each opening with the same 60-character stem — the "barely
    readable blobs of text" the patient reported. Each entry now gets its own
    labelled block, and only the most recent is unfolded.
    """
    app, repo, _db, _calls = build_app(tmp_path)
    hyp = Hypothesis(
        id="est-01",
        name="Exogenous estrogen therapy",
        tier="expanded",
        probability="moderate",
        status="active",
        origin="challenger",
        first_proposed=date(2026, 1, 1),
        challenger_notes=(
            "Added from weekly blind-panel divergence adjudication: high SHBG makes this plausible."
            "\nChallenge: this is an inference without a medication history."
            "\nChallenge: estradiol could reflect endogenous surges instead."
        ),
    )
    ledger = Ledger(version=1, updated=datetime.now(UTC), schema_version=1, hypotheses=[hyp])
    save_ledger(repo.root / LEDGER_RELPATH, ledger)

    client = TestClient(app)
    login(client)
    response = client.get("/ledger")

    assert response.status_code == 200
    # One block per entry, not one paragraph for all three.
    assert response.text.count('class="note-entry"') == 3
    # The repeated bureaucratic stem is replaced by a short label.
    assert "Added by review" in response.text
    assert "Added from weekly blind-panel divergence adjudication:" not in response.text
    # Older notes are folded; the newest is not.
    assert "2 earlier notes" in response.text
    assert "estradiol could reflect endogenous surges" in response.text


def test_a_labs_ref_is_shown_in_words_not_slug_syntax(tmp_path: Path) -> None:
    """The card printed the machine ref verbatim beside every claim —
    `(labs:lumbar-spine-percent-change-vs-2024:2026-08-04)`. That is the
    citation's identity, not its presentation."""
    app, repo, _db, _calls = build_app(tmp_path)
    hyp = Hypothesis(
        id="dxa-01",
        name="DXA measurement artifact",
        tier="expanded",
        probability="moderate",
        status="active",
        origin="challenger",
        first_proposed=date(2026, 1, 1),
        evidence_for=[
            Evidence(
                claim="Similar magnitude of decline at spine and both hips",
                source="labs:lumbar-spine-percent-change-vs-2024:2026-08-04",
                strength="moderate",
            )
        ],
    )
    ledger = Ledger(version=1, updated=datetime.now(UTC), schema_version=1, hypotheses=[hyp])
    save_ledger(repo.root / LEDGER_RELPATH, ledger)

    client = TestClient(app)
    login(client)
    response = client.get("/ledger")

    assert response.status_code == 200
    assert "lumbar spine percent change vs 2024 · 2026-08-04" in response.text
    # The raw ref survives only as the link's title attribute, for anyone who
    # needs the exact identity; it is no longer body text.
    assert ">(labs:lumbar-spine-percent-change-vs-2024:2026-08-04)<" not in response.text
    assert 'title="labs:lumbar-spine-percent-change-vs-2024:2026-08-04"' in response.text


def test_a_hypothesis_card_carries_an_anchor_id(tmp_path: Path) -> None:
    """The next-appointment page links each test to the hypotheses it answers
    via `/ledger#<id>`, so the card has to be a jump target."""
    app, repo, _db, _calls = build_app(tmp_path)
    hyp = Hypothesis(
        id="poi-01",
        name="Primary ovarian insufficiency",
        plain_language="The ovaries stop releasing eggs earlier than expected.",
        tier="expanded",
        probability="high",
        status="active",
        origin="challenger",
        first_proposed=date(2026, 1, 1),
    )
    save_ledger(
        repo.root / LEDGER_RELPATH,
        Ledger(version=1, updated=datetime.now(UTC), schema_version=1, hypotheses=[hyp]),
    )

    client = TestClient(app)
    login(client)
    response = client.get("/ledger")

    assert 'id="poi-01"' in response.text
    # And the plain-language gloss is rendered, since a name alone is not
    # communication.
    assert "The ovaries stop releasing eggs earlier than expected." in response.text


def test_an_empty_evidence_section_says_why(tmp_path: Path) -> None:
    """Silently omitting it reads as "no evidence was looked for"; the real
    reason is that the blind panel never independently raised the lead."""
    app, repo, _db, _calls = build_app(tmp_path)
    hyp = Hypothesis(
        id="x-01",
        name="Some lead",
        tier="expanded",
        probability="low",
        status="active",
        origin="challenger",
        first_proposed=date(2026, 1, 1),
    )
    save_ledger(
        repo.root / LEDGER_RELPATH,
        Ledger(version=1, updated=datetime.now(UTC), schema_version=1, hypotheses=[hyp]),
    )

    client = TestClient(app)
    login(client)
    response = client.get("/ledger")

    assert "Evidence for" in response.text
    assert "No citations recorded yet" in response.text


def test_an_uncited_cant_miss_placeholder_does_not_lead_the_page() -> None:
    """ "Empty priority items appear prioritized" — the owner, on seeing a
    can't-miss lead with no citations sitting above the supported ones.

    The can't-miss tier is a safety net, so the challenger raises entries
    there speculatively before anything supports them; that is the tier
    working. What is wrong is such an entry reading with the same weight as a
    lead the labs actually point at. It stays on the page and stays in the
    lead group — it is simply not what she reads first.
    """
    from types import SimpleNamespace

    from adoc.web.casefile_helpers import group_hypotheses

    def h(name: str, tier: str, probability: str, evidence: list[str]) -> SimpleNamespace:
        return SimpleNamespace(name=name, tier=tier, probability=probability, evidence_for=evidence)

    placeholder = h("Hereditary breast and ovarian cancer", "cant-miss", "low", [])
    supported = h("Pulmonary embolism", "cant-miss", "moderate", ["labs:d-dimer:2026-01-01"])
    likely = h("Sjogren syndrome", "expanded", "high", ["labs:ssa:2026-01-01"])

    groups = group_hypotheses([placeholder, supported, likely])
    leading = [x.name for x in groups["leading"]]

    assert placeholder.name in leading, "a can't-miss lead must never be hidden"
    assert leading[-1] == placeholder.name, "the uncited placeholder is still leading the page"
    assert leading[0] == supported.name


# --- PAT-03 / ADR 0038: patient-directed retirement -----------------------------------


def _seed_one(repo: DataRepo, hid: str = "pheochromocytoma", *, tier: str = "cant-miss") -> None:
    from adoc.casefile.ledger import apply_and_save
    from adoc.casefile.repo import HISTORY_RELPATH
    from adoc.casefile.schema import AddHypothesis, LedgerDiff, Provenance

    apply_and_save(
        repo.root / LEDGER_RELPATH,
        repo.root / HISTORY_RELPATH,
        LedgerDiff(
            provenance=Provenance(
                app_version="test",
                prompt_template_version="t@v1",
                model_id="m",
                dag_node="seed",
                timestamp=datetime(2026, 1, 1, tzinfo=UTC),
            ),
            rationale="seed",
            ops=[AddHypothesis(hypothesis=_hypothesis(hid, tier=tier, probability="low"))],
        ),
    )


def test_a_patient_can_retire_a_cant_miss_lead(tmp_path: Path) -> None:
    """The only path by which a protected lead can leave the list short of a
    lab settling it. Until this route existed `/ledger` was a single GET, so a
    lead her doctor had definitively excluded stayed "worth discussing now"
    forever."""
    app, repo, _db, _calls = build_app(tmp_path)
    _seed_one(repo)
    client = TestClient(app)
    login(client)

    response = client.post(
        "/ledger/hypotheses/pheochromocytoma/retire",
        data={"reason": "Metanephrines came back clear on 12 August", "clinician": "Dr Alvarez"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    ledger = load_ledger(repo.root / LEDGER_RELPATH)
    hypothesis = next(h for h in ledger.hypotheses if h.id == "pheochromocytoma")
    assert hypothesis.status == "ruled-out"
    evidence = hypothesis.evidence_against[-1]
    assert evidence.strength == "definitive-exclusion"
    assert evidence.source.startswith("patient-report:")
    assert "Dr Alvarez" in evidence.claim


def test_retiring_writes_through_the_invariant_checked_path(tmp_path: Path) -> None:
    """No private back door (ADR 0035's rule, kept): the change lands as a
    diff in `ledger-history.jsonl`, not as a direct write."""
    from adoc.casefile.repo import HISTORY_RELPATH

    app, repo, _db, _calls = build_app(tmp_path)
    _seed_one(repo)
    client = TestClient(app)
    login(client)
    before = (repo.root / HISTORY_RELPATH).read_text(encoding="utf-8").count("\n")

    client.post(
        "/ledger/hypotheses/pheochromocytoma/retire",
        data={"reason": "Biopsy clear"},
        follow_redirects=False,
    )

    after = (repo.root / HISTORY_RELPATH).read_text(encoding="utf-8").count("\n")
    assert after == before + 1


def test_an_empty_reason_changes_nothing(tmp_path: Path) -> None:
    """A retirement with no stated reason is not an audit trail."""
    app, repo, _db, _calls = build_app(tmp_path)
    _seed_one(repo)
    client = TestClient(app)
    login(client)

    client.post(
        "/ledger/hypotheses/pheochromocytoma/retire",
        data={"reason": "   "},
        follow_redirects=False,
    )

    ledger = load_ledger(repo.root / LEDGER_RELPATH)
    assert next(h for h in ledger.hypotheses if h.id == "pheochromocytoma").status == "active"


def test_retiring_an_unknown_hypothesis_is_a_redirect_not_a_500(tmp_path: Path) -> None:
    app, repo, _db, _calls = build_app(tmp_path)
    _seed_one(repo)
    client = TestClient(app)
    login(client)

    response = client.post(
        "/ledger/hypotheses/not-a-real-id/retire",
        data={"reason": "x"},
        follow_redirects=False,
    )

    assert response.status_code == 303


def test_the_retire_control_is_offered_on_an_active_lead(tmp_path: Path) -> None:
    app, repo, _db, _calls = build_app(tmp_path)
    _seed_one(repo)
    client = TestClient(app)
    login(client)

    body = client.get("/ledger").text

    assert "My doctor ruled this out" in body
    assert "/ledger/hypotheses/pheochromocytoma/retire" in body
