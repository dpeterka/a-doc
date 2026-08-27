"""Tests for adoc.reason.context: the deterministic, fixed-order context-pack builder."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from adoc.casefile.encounters import Encounter, EncounterFrontmatter, write_encounter
from adoc.casefile.ledger import apply_diff, save_ledger
from adoc.casefile.repo import LEDGER_RELPATH, DataRepo
from adoc.casefile.schema import AddHypothesis, Hypothesis, Ledger, LedgerDiff, Provenance
from adoc.labs.db import DocumentTextPage, LabsDb
from adoc.labs.models import LabDocument, LabFlag, LabResult
from adoc.reason.context import (
    DOCUMENT_EXCERPTS_SECTION_KEY,
    LEDGER_SECTION_KEY,
    MAX_DOCUMENT_EXCERPT_CHARS,
    PATIENT_THEORIES_SECTION_KEY,
    _trajectories_section,
    build_context,
)

SHA = "a" * 64


@pytest.fixture
def repo(tmp_path: Path) -> DataRepo:
    return DataRepo.init_at(tmp_path / "data")


@pytest.fixture
def db(tmp_path: Path) -> LabsDb:
    store = LabsDb(tmp_path / "labs.sqlite")
    store.upsert_document(
        LabDocument(sha256=SHA, filename="doc.pdf", doc_type="lab-result", page_count=1)
    )
    store.insert_results(
        [
            LabResult(
                date=date(2026, 5, 2),
                name="ana-titer",
                name_raw="ANA",
                value_text="1:640",
                source_doc=SHA,
                raw_json=json.dumps({"name_raw": "ANA"}),
                flag=LabFlag.ABNORMAL,
            ),
            LabResult(
                date=date(2026, 5, 2),
                name="potassium",
                name_raw="Potassium",
                value=4.1,
                ucum_unit="mmol/L",
                source_doc=SHA,
                raw_json=json.dumps({"name_raw": "Potassium"}),
            ),
        ]
    )
    return store


def _write_encounter(repo: DataRepo, day: date, slug: str, summary: str) -> None:
    encounter = Encounter(
        frontmatter=EncounterFrontmatter(date=day, type="patient-report"),
        summary=summary,
    )
    write_encounter(repo.root / "case" / "encounters", encounter, slug)


def test_section_order_is_fixed_and_stable(repo: DataRepo, db: LabsDb) -> None:
    pack = build_context(repo, db, include_ledger=True)

    assert pack.keys == [
        "case_summary",
        "recent_encounters",
        "labs",
        "trajectories",
        "open_questions",
        LEDGER_SECTION_KEY,
    ]


def test_patient_theories_section_included_only_when_file_exists(
    repo: DataRepo, db: LabsDb
) -> None:
    without = build_context(repo, db, include_ledger=False)
    assert PATIENT_THEORIES_SECTION_KEY not in without.keys

    repo.write("case/patient-theories.md", "# Patient Theories\n\n- MCAS (patient-proposed)\n")

    with_theories = build_context(repo, db, include_ledger=False)
    assert PATIENT_THEORIES_SECTION_KEY in with_theories.keys
    # patient theories section is inserted right after case_summary, before
    # recent_encounters/labs/open_questions — order stays fixed either way.
    assert with_theories.keys.index(PATIENT_THEORIES_SECTION_KEY) == 1
    assert "MCAS" in with_theories.render()


def test_include_ledger_toggle_controls_ledger_section(repo: DataRepo, db: LabsDb) -> None:
    blind_pack = build_context(repo, db, include_ledger=False)
    assert LEDGER_SECTION_KEY not in blind_pack.keys
    assert "differential ledger" not in blind_pack.render().lower()

    sighted_pack = build_context(repo, db, include_ledger=True)
    assert LEDGER_SECTION_KEY in sighted_pack.keys
    assert "differential ledger" in sighted_pack.render().lower()


def test_blind_pack_never_mentions_ledger_hypothesis_ids(repo: DataRepo, db: LabsDb) -> None:
    ledger = Ledger(version=0, updated=datetime(2026, 8, 1, tzinfo=UTC), hypotheses=[])
    diff = LedgerDiff(
        provenance=Provenance(
            app_version="0.1.0",
            prompt_template_version="ledger_maintainer@v1",
            model_id="fake-model",
            dag_node="ledger_maintainer",
            timestamp=datetime(2026, 8, 1, tzinfo=UTC),
        ),
        rationale="seed",
        ops=[
            AddHypothesis(
                hypothesis=Hypothesis(
                    id="mcas-secret-01",
                    name="Mast cell activation syndrome",
                    tier="cant-miss",
                    probability="low",
                    status="active",
                    origin="model",
                    first_proposed=date(2026, 8, 1),
                )
            )
        ],
    )
    new_ledger = apply_diff(ledger, diff)
    save_ledger(repo.root / LEDGER_RELPATH, new_ledger)

    blind_pack = build_context(repo, db, include_ledger=False)
    assert "mcas-secret-01" not in blind_pack.render()

    sighted_pack = build_context(repo, db, include_ledger=True)
    assert "mcas-secret-01" in sighted_pack.render()


def test_recent_encounters_are_most_recent_first_and_limited(repo: DataRepo, db: LabsDb) -> None:
    _write_encounter(repo, date(2026, 1, 1), "first", "Oldest visit.")
    _write_encounter(repo, date(2026, 3, 1), "second", "Middle visit.")
    _write_encounter(repo, date(2026, 6, 1), "third", "Most recent visit.")

    pack = build_context(repo, db, include_ledger=False, recent_encounters_limit=2)
    content = next(s.content for s in pack.sections if s.key == "recent_encounters")

    assert "Most recent visit." in content
    assert "Middle visit." in content
    assert "Oldest visit." not in content  # beyond the limit


def test_labs_section_includes_abnormal_and_latest_panel(repo: DataRepo, db: LabsDb) -> None:
    pack = build_context(repo, db, include_ledger=False)
    content = next(s.content for s in pack.sections if s.key == "labs")

    assert "ana-titer" in content
    assert "1:640" in content
    assert "potassium" in content
    assert "4.1" in content


def test_labs_section_groups_by_panel_in_deterministic_curated_order(
    repo: DataRepo, db: LabsDb
) -> None:
    """`potassium` (Comprehensive Metabolic Panel) and `ana-titer`
    (Autoimmune Serology, seeded by the `db` fixture) must render under
    their curated panel headings, with CMP before Autoimmune Serology per
    `labs.panels.PANEL_ORDER` - not incidental to insertion/db order."""
    pack = build_context(repo, db, include_ledger=False)
    content = next(s.content for s in pack.sections if s.key == "labs")
    # Only `potassium` (CMP) and `ana-titer` (Autoimmune Serology) are
    # non-abnormal/latest - scope the ordering check to the "Latest panel"
    # subsection, since the "Abnormal" subsection above it only ever
    # contains `ana-titer` (the only flagged row) and would otherwise make
    # "Autoimmune Serology" appear to come first for an unrelated reason.
    latest_panel_content = content[content.index("### Latest panel") :]

    assert "**Comprehensive Metabolic Panel**" in latest_panel_content
    assert "**Autoimmune Serology**" in latest_panel_content
    assert latest_panel_content.index("**Comprehensive Metabolic Panel**") < (
        latest_panel_content.index("**Autoimmune Serology**")
    )


def test_labs_section_labels_non_unknown_specimen(repo: DataRepo, db: LabsDb) -> None:
    """A row whose specimen isn't `"unknown"` is labeled e.g. "Glucose
    (urine)" - the same canonical name can carry a serum reading too, and
    the context pack must not present them as if they were one result."""
    db.insert_results(
        [
            LabResult(
                date=date(2026, 5, 2),
                name="glucose",
                name_raw="GLUCOSE",
                value=None,
                value_text="NEGATIVE",
                source_doc=SHA,
                raw_json=json.dumps({"name_raw": "GLUCOSE"}),
                specimen="urine",
            ),
            LabResult(
                date=date(2026, 5, 2),
                name="glucose",
                name_raw="Glucose",
                value=92.0,
                ucum_unit="mg/dL",
                source_doc=SHA,
                raw_json=json.dumps({"name_raw": "Glucose"}),
                specimen="serum",
            ),
        ]
    )
    pack = build_context(repo, db, include_ledger=False)
    content = next(s.content for s in pack.sections if s.key == "labs")

    assert "glucose (urine): NEGATIVE" in content
    assert "glucose (serum): 92.0" in content
    # unknown-specimen rows (e.g. "potassium" from the fixture) are never
    # suffixed
    assert "potassium (unknown)" not in content


def test_render_produces_stable_markdown_headers(repo: DataRepo, db: LabsDb) -> None:
    pack = build_context(repo, db, include_ledger=True)
    text = pack.render()

    assert "## Case Summary" in text
    assert "## Recent Encounters" in text
    assert "## Labs" in text
    assert "## Open Questions" in text
    assert "## Differential Ledger" in text


def test_missing_case_files_fall_back_to_placeholders(tmp_path: Path, db: LabsDb) -> None:
    # A bare DataRepo (not through init_at) has none of the placeholder files.
    bare_repo = DataRepo(tmp_path / "bare")
    (bare_repo.root / "case").mkdir(parents=True)
    save_ledger(
        bare_repo.root / LEDGER_RELPATH,
        Ledger(version=0, updated=datetime(2026, 8, 1, tzinfo=UTC), hypotheses=[]),
    )

    pack = build_context(bare_repo, db, include_ledger=True)

    case_summary = next(s.content for s in pack.sections if s.key == "case_summary")
    open_questions = next(s.content for s in pack.sections if s.key == "open_questions")
    assert case_summary == "_Not yet populated._"
    assert open_questions == "_None yet._"


def test_genomics_inventory_section_included_when_present(repo: DataRepo, db: LabsDb) -> None:
    repo.write("case/genomics-inventory.md", "# Genomic data\n\n| file | kind |\n")

    pack = build_context(repo, db, include_ledger=False)

    assert "genomics_inventory" in pack.keys
    assert "Genomic Data On File" in pack.render()


def test_genomics_inventory_section_absent_when_missing(repo: DataRepo, db: LabsDb) -> None:

    pack = build_context(repo, db, include_ledger=False)

    assert "genomics_inventory" not in pack.keys


# --------------------------------------------------------------------------
# Document excerpts (docs/adr/0015-document-text-corpus.md)
# --------------------------------------------------------------------------


def test_document_excerpts_absent_when_no_query_given(repo: DataRepo, db: LabsDb) -> None:
    db.replace_document_text(
        SHA,
        [DocumentTextPage(page=1, text="Impression: consistent with early arthritis.")],
        extracted_at=datetime(2026, 5, 3, tzinfo=UTC),
    )
    pack = build_context(repo, db, include_ledger=True)
    assert DOCUMENT_EXCERPTS_SECTION_KEY not in pack.keys
    # Existing fixed-order assertion still holds unchanged - `query=None` is
    # a fully backward-compatible default.
    assert pack.keys == [
        "case_summary",
        "recent_encounters",
        "labs",
        "trajectories",
        "open_questions",
        LEDGER_SECTION_KEY,
    ]


def test_document_excerpts_absent_when_nothing_matches(repo: DataRepo, db: LabsDb) -> None:
    db.replace_document_text(
        SHA,
        [DocumentTextPage(page=1, text="Impression: consistent with early arthritis.")],
        extracted_at=datetime(2026, 5, 3, tzinfo=UTC),
    )
    pack = build_context(repo, db, include_ledger=True, query="unrelated-token-xyz")
    assert DOCUMENT_EXCERPTS_SECTION_KEY not in pack.keys


def test_document_excerpts_included_and_last_when_query_matches(repo: DataRepo, db: LabsDb) -> None:
    db.replace_document_text(
        SHA,
        [DocumentTextPage(page=1, text="Impression: consistent with early arthritis.")],
        extracted_at=datetime(2026, 5, 3, tzinfo=UTC),
    )
    pack = build_context(repo, db, include_ledger=True, query="how is my arthritis doing")

    assert pack.keys[-1] == DOCUMENT_EXCERPTS_SECTION_KEY
    assert pack.keys[:-1] == [
        "case_summary",
        "recent_encounters",
        "labs",
        "trajectories",
        "open_questions",
        LEDGER_SECTION_KEY,
    ]
    rendered = pack.render()
    assert "Relevant Document Excerpts" in rendered
    assert "arthritis" in rendered.lower()
    assert "doc.pdf#p1" in rendered


def test_document_excerpts_verbatim_not_paraphrased(repo: DataRepo, db: LabsDb) -> None:
    exact_text = "The specific, exact wording of this passage must survive untouched."
    db.replace_document_text(
        SHA,
        [DocumentTextPage(page=None, text=exact_text)],
        extracted_at=datetime(2026, 5, 3, tzinfo=UTC),
    )
    pack = build_context(repo, db, include_ledger=False, query="specific exact wording")
    section = next(s for s in pack.sections if s.key == DOCUMENT_EXCERPTS_SECTION_KEY)
    # sqlite's snippet() brackets the matched terms but leaves the rest of
    # the text verbatim - the underlying words are never paraphrased/rewritten.
    assert "specific" in section.content
    assert "exact" in section.content
    assert "wording" in section.content


def test_document_excerpts_respect_character_cap(repo: DataRepo, db: LabsDb) -> None:
    long_pages = [
        DocumentTextPage(page=i, text=f"finding number {i}: " + "lupus " * 200) for i in range(1, 6)
    ]
    db.replace_document_text(SHA, long_pages, extracted_at=datetime(2026, 5, 3, tzinfo=UTC))

    pack = build_context(repo, db, include_ledger=False, query="lupus")
    section = next(s for s in pack.sections if s.key == DOCUMENT_EXCERPTS_SECTION_KEY)
    assert len(section.content) <= MAX_DOCUMENT_EXCERPT_CHARS + 200  # small slack for separators


# --- trajectories: the snapshot hides direction -----------------------------------------


def _row(name: str, value: float, day: str, unit: str = "mIU/mL") -> LabResult:
    return LabResult(
        date=date.fromisoformat(day),
        name=name,
        name_raw=name,
        value=value,
        ucum_unit=unit,
        source_doc=SHA,
        raw_json="{}",
    )


def test_a_moving_analyte_is_reported_with_direction_and_magnitude(db: LabsDb) -> None:
    """The rest of the pack is a snapshot — "most recent per analyte" and
    "latest panel". For a diagnostic odyssey the trajectory is often the
    signal: AMH falling 96% over five readings is the ovarian-reserve story,
    and no single row shows it."""
    db.insert_results(
        [
            _row("AMH", 0.57, "2018-03-05", unit="ng/mL"),
            _row("AMH", 0.18, "2021-05-01", unit="ng/mL"),
            _row("AMH", 0.02, "2024-06-11", unit="ng/mL"),
        ]
    )

    content = _trajectories_section(db).content

    assert "AMH" in content
    assert "falling" in content


def test_a_unit_change_mid_history_never_becomes_a_fake_signal(db: LabsDb) -> None:
    """The real corpus stores CBC absolutes under both `x10E3/uL` and
    `cells/uL` — a factor of 1000. Comparing across that boundary reported
    "eosinophils rising 319,900%", which would have gone straight to the
    reasoner as a finding."""
    db.insert_results(
        [
            _row("Eosinophils, Absolute", 0.1, "2017-02-27", unit="x10E3/uL"),
            _row("Eosinophils, Absolute", 0.2, "2018-02-27", unit="x10E3/uL"),
            _row("Eosinophils, Absolute", 92.0, "2019-06-08", unit="cells/uL"),
            _row("Eosinophils, Absolute", 200.0, "2022-06-08", unit="cells/uL"),
            _row("Eosinophils, Absolute", 320.0, "2026-08-14", unit="cells/uL"),
        ]
    )

    content = _trajectories_section(db).content

    # The naive comparison reported ~319,900%. Converting (0.1 x10E3/uL ==
    # 100 cells/uL) gives a real rise over the WHOLE history rather than the
    # truncated span an earlier version produced by discarding the old unit.
    assert "319900" not in content.replace(",", "")
    assert "rising 220%" in content
    assert "5 readings" in content  # nothing dropped: all five are comparable


def test_two_readings_are_not_a_trajectory(db: LabsDb) -> None:
    """Two draws is a coincidence; a direction needs at least three."""
    db.insert_results([_row("FSH", 7.5, "2019-06-08"), _row("FSH", 91.4, "2026-07-15")])

    assert "FSH" not in _trajectories_section(db).content


def test_a_flat_analyte_is_not_reported(db: LabsDb) -> None:
    """The section exists to say "look here" — a stable analyte is noise."""
    db.insert_results(
        [
            _row("Sodium", 140.0, "2019-06-08", unit="mmol/L"),
            _row("Sodium", 141.0, "2022-06-08", unit="mmol/L"),
            _row("Sodium", 140.0, "2026-07-15", unit="mmol/L"),
        ]
    )

    assert "Sodium" not in _trajectories_section(db).content
