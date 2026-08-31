"""Tests for adoc.reason.context: the deterministic, fixed-order context-pack builder."""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from adoc.casefile.disputes import DISPUTES_RELPATH, Dispute, Disputes, save_disputes
from adoc.casefile.encounters import Encounter, EncounterFrontmatter, write_encounter
from adoc.casefile.ledger import apply_diff, save_ledger
from adoc.casefile.regimen import (
    REGIMEN_RELPATH,
    Regimen,
    RegimenEntry,
    save_regimen,
)
from adoc.casefile.repo import LEDGER_RELPATH, DataRepo
from adoc.casefile.reported import (
    REPORTED_RESULTS_RELPATH,
    ReportedResult,
    ReportedResults,
    save_reported_results,
)
from adoc.casefile.schema import (
    AddHypothesis,
    Evidence,
    Hypothesis,
    Ledger,
    LedgerDiff,
    Provenance,
    validate_source_ref,
)
from adoc.intake.facts import (
    INTAKE_FACTS_RELPATH,
    IntakeFact,
    load_intake_facts,
    save_intake_facts,
)
from adoc.labs.db import DocumentTextPage, LabsDb
from adoc.labs.models import LabDocument, LabFlag, LabResult
from adoc.reason.citations import check_evidence_citations
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
        "regimen",
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
    # Open questions render from the STORE now, not `questions-open.md` — a
    # markdown file nothing regenerated, so it could not know what had been
    # answered. The empty state says so in the store's own words.
    assert open_questions == "_No open questions._"


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
        "regimen",
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
        "regimen",
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


def test_labs_rows_carry_a_ref_the_citation_checker_resolves(db: LabsDb, repo: DataRepo) -> None:
    """Every lab row in the pack is rendered beside the ref that cites it,
    and that ref must actually resolve.

    Document excerpts always carried their own `doc:<file>#p<page>` ref, but
    lab rows did not — so a model asked to cite a value had to construct
    `labs:<slug>:<date>` by guessing the slug. A live blind panel guessed the
    prefix from the visible section heading and emitted
    `other:monospot_(heterophile)_screen:2026-03-17` (ADR 0028). Rendering
    the ref makes citing a copy rather than an invention — but only if what
    is rendered is what `citations` accepts, which is what this pins.
    """
    pack = build_context(repo, db, include_ledger=False)
    labs_text = next(s.content for s in pack.sections if s.key == "labs")

    refs = re.findall(r"`(labs:[^`]+)`", labs_text)
    assert refs, f"no citable ref rendered in the labs section:\n{labs_text}"

    for ref in refs:
        report = check_evidence_citations(
            [Evidence(claim="rendered ref resolves", source=ref, strength="moderate")],
            db,
            repo,
        )
        assert not report.failing, f"pack rendered a ref that does not resolve: {ref}"


def test_a_lab_name_with_spaces_and_colons_still_renders_a_resolvable_ref(
    tmp_path: Path, repo: DataRepo
) -> None:
    """Analyte names are display strings, not slugs, and the source-ref
    grammar forbids whitespace and colons in a slug.

    The first version of `_labs_ref` interpolated `row.name` directly. It
    passed every synthetic test in this file because the fixtures happened to
    use already-slugified names (`ana-titer`, `potassium`), and then failed on
    the first real row it saw — `IGF-1 Z-Score`. 1178 of 2079 real rows have a
    name that is not a legal slug. This fixture is deliberately hostile so
    that gap cannot reopen.
    """
    db = LabsDb(tmp_path / "hostile.sqlite")
    db.upsert_document(
        LabDocument(sha256="a" * 64, filename="h.pdf", doc_type="lab-result", page_count=1)
    )
    db.insert_results(
        [
            LabResult(
                date=date(2024, 9, 11),
                name=name,
                name_raw=name,
                value=1.0,
                source_doc="a" * 64,
                raw_json=json.dumps({"name_raw": name}),
            )
            for name in ("IGF-1 Z-Score", "Free T4:T3 Ratio", "Monospot (Heterophile) Screen")
        ]
    )

    pack = build_context(repo, db, include_ledger=False)
    labs_text = next(s.content for s in pack.sections if s.key == "labs")
    refs = re.findall(r"`(labs:[^`]+)`", labs_text)
    assert len(refs) >= 3, f"expected a ref per row, got {refs}"

    for ref in refs:
        validate_source_ref(ref)  # raises if the slug is not grammar-legal
        report = check_evidence_citations(
            [Evidence(claim="hostile name resolves", source=ref, strength="moderate")],
            db,
            repo,
        )
        assert not report.failing, f"rendered ref does not resolve: {ref}"


def test_encounters_carry_a_ref_the_citation_checker_resolves(repo: DataRepo, db: LabsDb) -> None:
    """Encounters are cited by FILENAME (`YYYY-MM-DD--<slug>.md`), but the
    pack rendered only the date — so a panel wrote `encounter:2026-08-04` and
    two citations were dropped. Same defect as the lab rows in ADR 0028, one
    section up."""
    _write_encounter(repo, date(2026, 8, 4), "dxa-scan", "DXA scan performed.")

    pack = build_context(repo, db, include_ledger=False)
    text = next(s.content for s in pack.sections if s.key == "recent_encounters")

    refs = re.findall(r"`(encounter:[^`]+)`", text)
    assert refs, f"no citable encounter ref rendered:\n{text}"
    for ref in refs:
        validate_source_ref(ref)
        report = check_evidence_citations(
            [Evidence(claim="encounter ref resolves", source=ref, strength="moderate")], db, repo
        )
        assert not report.failing, f"pack rendered an encounter ref that does not resolve: {ref}"


def test_the_ledger_section_states_whether_a_gloss_is_missing(repo: DataRepo, db: LabsDb) -> None:
    """The challenge sweep writes a plain-language gloss only for hypotheses
    that lack one, and the first review after that field shipped produced ZERO
    across 28 hypotheses: the prompt told the model to check a state the pack
    never showed it. ADR 0028's rule one step out — if a model must act on a
    state, show it the state."""
    hypotheses = [
        Hypothesis(
            id="with-01",
            name="Has a gloss",
            plain_language="A condition explained plainly.",
            tier="expanded",
            probability="low",
            status="active",
            origin="model",
            first_proposed=date(2026, 1, 1),
        ),
        Hypothesis(
            id="without-01",
            name="Has none",
            tier="expanded",
            probability="low",
            status="active",
            origin="model",
            first_proposed=date(2026, 1, 1),
        ),
    ]
    save_ledger(
        repo.root / LEDGER_RELPATH,
        Ledger(version=1, updated=datetime.now(UTC), schema_version=1, hypotheses=hypotheses),
    )

    pack = build_context(repo, db, include_ledger=True)
    text = next(s.content for s in pack.sections if s.key == "ledger")

    assert "plain-language: A condition explained plainly." in text
    assert "plain-language: MISSING" in text


def test_the_regimen_section_flags_lab_draws_taken_during_interference(
    repo: DataRepo, db: LabsDb
) -> None:
    """The clinically decisive alignment: high-dose biotin distorts many
    hormone and antibody immunoassays, so a result drawn while it was active
    must be read differently.

    The reasoner previously could not answer this at all — the regimen was a
    110-line encounter contributing 107 characters to the pack, and
    `still_taking` is a boolean.
    """
    save_regimen(
        repo.root / Path(REGIMEN_RELPATH),
        Regimen(
            entries=[
                RegimenEntry(
                    name="Biotin",
                    dose="10000 mcg",
                    started=date(2026, 1, 1),
                    started_precision="day",
                )
            ]
        ),
    )

    pack = build_context(repo, db, include_ledger=False)
    section = next(s.content for s in pack.sections if s.key == "regimen")

    assert "Biotin 10000 mcg" in section
    assert "since 2026-01-01" in section
    # The db fixture's rows are dated 2026-05-02, inside the interval.
    assert "Draws while active" in section
    assert "2026-05-02" in section


def test_the_regimen_section_states_how_many_entries_cannot_be_placed(
    repo: DataRepo, db: LabsDb
) -> None:
    """Undated entries are what make an overlap answer incomplete. Saying so
    is the difference between a partial answer and a misleading one."""
    save_regimen(
        repo.root / Path(REGIMEN_RELPATH),
        Regimen(entries=[RegimenEntry(name="Selenium"), RegimenEntry(name="Zinc")]),
    )

    pack = build_context(repo, db, include_ledger=False)
    section = next(s.content for s in pack.sections if s.key == "regimen")

    assert "2 entries have no start or stop date" in section


def test_an_empty_regimen_says_so_rather_than_vanishing(repo: DataRepo, db: LabsDb) -> None:
    pack = build_context(repo, db, include_ledger=False)
    section = next(s.content for s in pack.sections if s.key == "regimen")

    assert "Nothing recorded yet" in section


def test_a_combination_product_is_flagged_when_nothing_is_named_biotin(
    repo: DataRepo, db: LabsDb
) -> None:
    """The case that matters, and the one the first implementation missed.

    On the real regimen, biotin is named ZERO times while the patient's biotin
    measured high — because biotin sits inside a B complex at doses well above
    the interference threshold. The first version put this check behind an
    early return that fired when no entry was named "biotin", making it
    unreachable in precisely the situation it was written for.
    """
    save_regimen(
        repo.root / Path(REGIMEN_RELPATH),
        Regimen(entries=[RegimenEntry(name="B complex", attested_on=[date(2026, 5, 2)])]),
    )

    pack = build_context(repo, db, include_ledger=False)
    section = next(s.content for s in pack.sections if s.key == "regimen")

    assert "combination product" in section
    assert "B complex" in section
    # It says what would settle it, rather than asking for everything.
    assert "label" in section


def test_a_substance_named_outright_is_not_double_reported(repo: DataRepo, db: LabsDb) -> None:
    """An entry actually named biotin gets the interval warning, not the
    weaker "contents unknown" one."""
    save_regimen(
        repo.root / Path(REGIMEN_RELPATH),
        Regimen(entries=[RegimenEntry(name="Biotin complex", started=date(2026, 1, 1))]),
    )

    pack = build_context(repo, db, include_ledger=False)
    section = next(s.content for s in pack.sections if s.key == "regimen")

    assert "combination product" not in section
    assert "falsely shift" in section


def test_a_disputed_encounter_is_shown_but_marked(repo: DataRepo, db: LabsDb) -> None:
    """A dispute never deletes. The archived record is the source of truth and
    the patient may be misremembering — but it must not be read as
    established, because a phantom study shapes a differential exactly as a
    real one does."""
    path = write_encounter(
        repo.root / "case" / "encounters",
        Encounter(
            frontmatter=EncounterFrontmatter(date=date(2026, 8, 23), type="imaging"),
            summary="MRI pituitary.",
        ),
        "mripituitary",
    )
    save_disputes(
        repo.root / Path(DISPUTES_RELPATH),
        Disputes(
            entries=[
                Dispute(
                    target=f"encounter:{path.name}",
                    kind="did-not-occur",
                    statement="This did not occur.",
                    reported_on=date(2026, 8, 28),
                )
            ]
        ),
    )

    pack = build_context(repo, db, include_ledger=False)
    section = next(s.content for s in pack.sections if s.key == "recent_encounters")

    # Still present...
    assert "MRI pituitary." in section
    # ...but never as established fact.
    assert "DISPUTED BY THE PATIENT" in section


def test_a_resolved_dispute_stops_marking_the_item(repo: DataRepo, db: LabsDb) -> None:
    """Only an OPEN dispute marks an item; a dismissed one means the record
    stands and the mark would be misleading."""
    path = write_encounter(
        repo.root / "case" / "encounters",
        Encounter(
            frontmatter=EncounterFrontmatter(date=date(2026, 8, 23), type="imaging"),
            summary="MRI pituitary.",
        ),
        "mripituitary",
    )
    save_disputes(
        repo.root / Path(DISPUTES_RELPATH),
        Disputes(
            entries=[
                Dispute(
                    target=f"encounter:{path.name}",
                    statement="This did not occur.",
                    reported_on=date(2026, 8, 28),
                    status="dismissed",
                )
            ]
        ),
    )

    pack = build_context(repo, db, include_ledger=False)
    section = next(s.content for s in pack.sections if s.key == "recent_encounters")

    assert "DISPUTED" not in section


def test_reported_results_are_labelled_and_kept_out_of_the_labs_sections(
    repo: DataRepo, db: LabsDb
) -> None:
    """A remembered value must never be read as a measured one — merging them
    would put an uncitable number into the series the citation checker
    guards."""
    save_reported_results(
        repo.root / Path(REPORTED_RESULTS_RELPATH),
        ReportedResults(
            entries=[
                ReportedResult(
                    analyte="Iron",
                    direction="high",
                    when=date(2024, 11, 1),
                    when_precision="month",
                    reported_on=date(2026, 8, 28),
                )
            ]
        ),
    )

    pack = build_context(repo, db, include_ledger=False)
    reported = next(s.content for s in pack.sections if s.key == "reported_results")
    labs = next(s.content for s in pack.sections if s.key == "labs")

    assert "Iron high" in reported
    assert "2024-11" in reported
    assert "no document on file" in reported
    assert "NOT measured results" in reported
    # ...and nowhere near the measured series.
    assert "Iron" not in labs


def _seed_fact(repo: DataRepo, section: str, kind: str, statement: str) -> None:
    path = repo.root / INTAKE_FACTS_RELPATH
    facts = load_intake_facts(path) if path.exists() else []
    facts.append(
        IntakeFact(
            id=f"{section}-{len(facts)}",
            section=section,
            kind=kind,
            statement=statement,
            provenance=Provenance(
                app_version="test",
                prompt_template_version="1",
                model_id="test-model",
                dag_node="intake",
                timestamp=datetime(2026, 1, 1, tzinfo=UTC),
            ),
        )
    )
    save_intake_facts(path, facts)


def test_intake_history_that_nothing_read_is_now_in_the_pack(repo: DataRepo, db: LabsDb) -> None:
    """Patient-reported topics no other context path carries. On the live
    case file: 11 family-history facts, 5 geography, 4 care team — all
    captured from the patient, all invisible to the review and to every
    post-intake chat turn."""
    _seed_fact(
        repo, "family_history", "relative", "Mother: Hashimoto's thyroiditis in her forties."
    )
    _seed_fact(repo, "geography", "location", "Lived in the Ohio River valley until 2015.")

    pack = build_context(repo, db, include_ledger=False)
    section = next(s.content for s in pack.sections if s.key == "intake_history")

    assert "Hashimoto" in section
    assert "Ohio River valley" in section
    assert "Family history" in section and "Geography" in section


def test_uncovered_topic_facts_are_rendered_anyway(repo: DataRepo, db: LabsDb) -> None:
    """The defect this section was rewritten to close. Section artifacts are
    written only for topics the coverage state marks covered, so reading the
    artifact reproduced that blindness: on the live case file 4 care-team
    facts sat beside a 34-byte artifact holding only its heading, and the
    section skipped care team as empty. Facts are the source of truth, so
    coverage state must not gate what a reasoner sees."""
    _seed_fact(repo, "care_team", "provider", "Dr Smith, endocrinology, at the county clinic.")
    # Exactly what an uncovered topic leaves behind: a heading, no content.
    (repo.root / "case" / "care-team.md").write_text("# Care Team\n")

    pack = build_context(repo, db, include_ledger=False)
    section = next(s.content for s in pack.sections if s.key == "intake_history")

    assert "Dr Smith" in section


def test_no_intake_facts_makes_no_section(repo: DataRepo, db: LabsDb) -> None:
    """A section titled "Family history" with nothing under it costs tokens
    and tells a reasoner nothing."""
    pack = build_context(repo, db, include_ledger=False)

    assert "intake_history" not in pack.keys


def test_medications_are_not_duplicated_into_the_history_section(
    repo: DataRepo, db: LabsDb
) -> None:
    """Medications converge on the regimen record instead (ADR 0031): a prose
    list cannot answer whether she was taking something when a specimen was
    drawn, and the regimen is already rendered against lab dates."""
    _seed_fact(repo, "medications", "medication", "Levothyroxine 125 mcg daily.")
    _seed_fact(repo, "care_team", "provider", "Dr Smith, endocrinology.")

    pack = build_context(repo, db, include_ledger=False)
    section = next(s.content for s in pack.sections if s.key == "intake_history")

    assert "Dr Smith" in section
    assert "Levothyroxine" not in section


def _write_pending_encounter(repo: DataRepo, *, slug: str, text: str) -> None:
    """An encounter exactly as `ingest.pipeline` archives a non-lab document:
    summary is the placeholder, extracted text may or may not be present."""
    write_encounter(
        repo.root / "case" / "encounters",
        Encounter(
            frontmatter=EncounterFrontmatter(date=date(2026, 8, 23), type="imaging"),
            summary="(pending review)",
            extracted_text=text,
        ),
        slug=slug,
    )


def test_unsummarised_document_with_text_says_the_text_is_available(
    repo: DataRepo, db: LabsDb
) -> None:
    """`(pending review)` is `ingest.pipeline`'s "nobody wrote a summary yet",
    not "this document is empty". Rendered bare it read as a status claim: a
    chat reply told the patient four documents were "still marked pending
    review, so I have no content from them yet", when two of them held 2,040
    and 38,965 characters that a targeted question would have retrieved."""
    _write_pending_encounter(repo, slug="pituitary-mri", text="IMPRESSION: no adenoma seen.")

    pack = build_context(repo, db, include_ledger=False)
    section = next(s.content for s in pack.sections if s.key == "recent_encounters")

    assert "pending review" not in section
    assert "IS on file" in section


def test_unsummarised_document_without_text_says_so_plainly(repo: DataRepo, db: LabsDb) -> None:
    """The other half must stay distinguishable — 20 of her 23 unsummarised
    documents genuinely had nothing extracted, and saying otherwise would send
    a reasoner looking for text that is not there."""
    _write_pending_encounter(repo, slug="ultrasound", text="")

    pack = build_context(repo, db, include_ledger=False)
    section = next(s.content for s in pack.sections if s.key == "recent_encounters")

    assert "no text could be extracted" in section
