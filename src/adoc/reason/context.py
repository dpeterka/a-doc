"""Deterministic context-pack builder (PLAN.md "fixed section order for caching").

`build_context` assembles the material every reasoning stage sees into a
`ContextPack` with a **stable, fixed section order** — case summary,
patient theories (if any), recent encounters, labs, open questions, and
(only when explicitly requested) the differential ledger itself. Fixed
ordering matters for prompt caching (PLAN.md "Reasoner integration") and is
what makes `ContextPack.keys` a reliable way to check whether the ledger
section was included — the blind-review DAG's `forbid_context_key`
contract (ADR 0002) depends on that being checkable.

Nothing here calls a model. This module only reads from the data repo and
the labs database and renders plain text.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from adoc.casefile.encounters import read_encounter
from adoc.casefile.ledger import load_ledger
from adoc.casefile.repo import LEDGER_RELPATH, DataRepo
from adoc.casefile.schema import Ledger
from adoc.labs.db import LabsDb
from adoc.labs.models import LabResult
from adoc.labs.panels import derived_from_note, panel_sort_key
from adoc.labs.queries import abnormal_summary

CASE_SUMMARY_RELPATH = "case/case-summary.md"
PATIENT_THEORIES_RELPATH = "case/patient-theories.md"
OPEN_QUESTIONS_RELPATH = "case/questions-open.md"
ENCOUNTERS_RELDIR = "case/encounters"

DEFAULT_RECENT_ENCOUNTERS = 5

LEDGER_SECTION_KEY = "ledger"
PATIENT_THEORIES_SECTION_KEY = "patient_theories"
GENOMICS_INVENTORY_RELPATH = "case/genomics-inventory.md"

DOCUMENT_EXCERPTS_SECTION_KEY = "document_excerpts"

# Hard cap on the TOTAL characters of document-text excerpts folded into one
# context pack (docs/adr/0015-document-text-corpus.md "Retrieval"). 121+
# documents of full text cannot go into a prompt - this keeps one turn's
# excerpt budget small and predictable regardless of how many/how long the
# FTS5 matches are, while still being generous enough for several genuinely
# relevant quoted passages (a few hundred words). A module constant, not a
# per-call parameter, so the cap is one place to retune.
MAX_DOCUMENT_EXCERPT_CHARS = 4000

MAX_DOCUMENT_EXCERPTS = 5


class ContextSection(BaseModel):
    """One rendered section of a `ContextPack`, in a fixed, stable order."""

    key: str
    title: str
    content: str


class ContextPack(BaseModel):
    """The full, ordered context handed to a reasoning stage.

    `keys` is the ordered list of section keys actually included — this is
    what a DAG contract like `forbid_context_key("ledger")` needs to check
    blindness: a pack built with `include_ledger=False` never has `"ledger"`
    in `keys`, and its rendered text never mentions the ledger at all.
    """

    sections: list[ContextSection] = Field(default_factory=list)
    include_ledger: bool

    @property
    def keys(self) -> list[str]:
        return [section.key for section in self.sections]

    def render(self) -> str:
        """Render all sections, each as a `## <title>` markdown block, in order."""
        blocks = [f"## {section.title}\n\n{section.content.strip()}" for section in self.sections]
        return "\n\n".join(blocks) + "\n"


def _read_or_placeholder(repo: DataRepo, relpath: str, placeholder: str) -> str:
    try:
        return repo.read(relpath)
    except FileNotFoundError:
        return placeholder


def _recent_encounters_section(repo: DataRepo, limit: int) -> ContextSection:
    encounters_dir = repo.root / ENCOUNTERS_RELDIR
    if not encounters_dir.is_dir():
        return ContextSection(
            key="recent_encounters", title="Recent Encounters", content="_None recorded yet._"
        )

    # Filenames are `YYYY-MM-DD--<slug>.md` (encounters.py), so sorting the
    # filename descending sorts most-recent-first without parsing dates.
    filenames = sorted(
        (p.name for p in encounters_dir.iterdir() if p.suffix == ".md"), reverse=True
    )
    if not filenames:
        return ContextSection(
            key="recent_encounters", title="Recent Encounters", content="_None recorded yet._"
        )

    lines: list[str] = []
    for filename in filenames[:limit]:
        encounter = read_encounter(encounters_dir / filename)
        fm = encounter.frontmatter
        provider = f" ({fm.provider})" if fm.provider else ""
        summary = encounter.summary.strip() or "_no summary_"
        lines.append(f"- **{fm.date.isoformat()}** [{fm.type}]{provider}: {summary}")

    return ContextSection(
        key="recent_encounters", title="Recent Encounters", content="\n".join(lines)
    )


def _labs_label(row: LabResult) -> str:
    """`row.name`, suffixed with its specimen when that's not `"unknown"`
    (e.g. "Glucose (urine)") - the same canonical name can legitimately
    carry two different specimens (a urinalysis GLUCOSE reading and a
    serum glucose reading), and the context pack must not present them as
    if they were one result."""
    if row.specimen == "unknown":
        return row.name
    return f"{row.name} ({row.specimen})"


def _group_rows_by_panel(rows: list[LabResult]) -> list[tuple[str, list[LabResult]]]:
    """`rows` grouped by curated clinical panel (`labs.panels.panel_sort_key`),
    in that helper's fixed, deterministic order - "Other" (no curated panel)
    always last. Grouping labs by panel here (not just alphabetically by
    name) keeps this section's output stable across builds - it depends
    only on `ANALYTE_SPECS`'s curated panel assignment and each row's own
    name/date, never on incidental dict/set ordering - which matters for
    prompt caching (module docstring)."""
    ordered = sorted(rows, key=lambda row: panel_sort_key(row.name))
    groups: list[tuple[str, list[LabResult]]] = []
    for row in ordered:
        panel = panel_sort_key(row.name)[1]
        if groups and groups[-1][0] == panel:
            groups[-1][1].append(row)
        else:
            groups.append((panel, [row]))
    return groups


def _labs_section(db: LabsDb) -> ContextSection:
    abnormal = abnormal_summary(db)
    latest = db.latest_panel()

    lines: list[str] = ["### Abnormal (most recent per analyte)"]
    if abnormal:
        for panel, panel_rows in _group_rows_by_panel(abnormal):
            lines.append(f"**{panel}**")
            for row in panel_rows:
                value = row.value_text if row.value is None else str(row.value)
                unit = f" {row.ucum_unit}" if row.ucum_unit else ""
                flag = f" [{row.flag}]" if row.flag else ""
                lines.append(f"- {_labs_label(row)}: {value}{unit}{flag} — {row.date.isoformat()}")
    else:
        lines.append("- _None currently flagged._")

    lines.append("")
    lines.append("### Latest panel (all analytes)")
    if latest:
        # Derived analytes (e.g. TSAT, A/G Ratio) get a short "(calculated
        # from ...)" note so a reasoning stage never mistakes a computed
        # value for an independently-measured one.
        for panel, panel_rows in _group_rows_by_panel(latest):
            lines.append(f"**{panel}**")
            for row in panel_rows:
                value = row.value_text if row.value is None else str(row.value)
                unit = f" {row.ucum_unit}" if row.ucum_unit else ""
                note = derived_from_note(row.name)
                note_suffix = f" ({note})" if note else ""
                lines.append(
                    f"- {_labs_label(row)}: {value}{unit} — {row.date.isoformat()}{note_suffix}"
                )
    else:
        lines.append("- _No labs recorded yet._")

    return ContextSection(
        key="labs", title="Labs — Abnormal Results & Latest Panel", content="\n".join(lines)
    )


def _document_excerpts_section(db: LabsDb, query: str | None) -> ContextSection | None:
    """Relevant excerpts from ingested documents' full text
    (docs/adr/0015-document-text-corpus.md), ranked by
    `LabsDb.search_document_text` against `query` (typically the current
    chat turn's text). Returns `None` — never an empty section — when
    `query` is falsy or nothing matches, so a turn with no relevant
    document text looks exactly like it did before this feature existed.

    Excerpts are quoted VERBATIM with their `doc:<filename>#p<page>`-style
    source ref, never paraphrased (module docstring: "the point is verbatim
    text the model can cite and a later verifier can check"), and the total
    rendered content is capped at `MAX_DOCUMENT_EXCERPT_CHARS` characters —
    a snippet that would blow the remaining budget is truncated with a
    trailing ellipsis rather than dropped outright, so at least a partial
    quote survives.
    """
    if not query or not query.strip():
        return None
    hits = db.search_document_text(query, limit=MAX_DOCUMENT_EXCERPTS)
    if not hits:
        return None

    blocks: list[str] = []
    budget = MAX_DOCUMENT_EXCERPT_CHARS
    for hit in hits:
        snippet = hit.snippet.strip()
        if not snippet or budget <= 0:
            continue
        block = f"> {snippet}\n— {hit.source_ref}"
        if len(block) > budget:
            block = block[: max(budget - 1, 0)].rstrip() + "…"
        blocks.append(block)
        budget -= len(block)

    if not blocks:
        return None
    return ContextSection(
        key=DOCUMENT_EXCERPTS_SECTION_KEY,
        title="Relevant Document Excerpts",
        content="\n\n".join(blocks),
    )


def _render_ledger_section(ledger: Ledger) -> ContextSection:
    if not ledger.hypotheses:
        content = "_No hypotheses on the ledger yet._"
    else:
        lines: list[str] = []
        for h in ledger.hypotheses:
            lines.append(
                f"- **{h.id}** ({h.name}) — tier={h.tier}, probability={h.probability}, "
                f"status={h.status}, origin={h.origin}"
            )
        content = "\n".join(lines)
    return ContextSection(key=LEDGER_SECTION_KEY, title="Differential Ledger", content=content)


def build_context(
    repo: DataRepo,
    db: LabsDb,
    *,
    include_ledger: bool,
    recent_encounters_limit: int = DEFAULT_RECENT_ENCOUNTERS,
    query: str | None = None,
) -> ContextPack:
    """Build a `ContextPack` in the fixed PLAN.md section order.

    Order (stable, for prompt caching):
      1. case-summary.md
      2. patient theories (`case/patient-theories.md`), only if that file exists
      3. recent encounters (last `recent_encounters_limit`)
      4. abnormal labs summary + latest panel
      5. genomics inventory (`case/genomics-inventory.md`), only if present
      6. open questions (`case/questions-open.md`)
      7. differential-ledger.yaml, ONLY when `include_ledger=True`
      8. relevant document excerpts (`query`-dependent), ONLY when `query`
         is given and something matches (docs/adr/0015)

    A blind-review caller passes `include_ledger=False`, which both omits
    the ledger section from `.render()`'s text and keeps `"ledger"` out of
    `.keys` — the property a `forbid_context_key("ledger")` DAG contract
    can check for blindness.

    `query` (typically the current chat turn's raw text) drives the
    document-excerpts section — deliberately LAST, after every other
    section (including the ledger): every other section is fully
    determined by repo/db state alone, so keeping the one query-dependent,
    per-turn-variable section at the very end means its variability never
    invalidates a prompt cache prefix built over the earlier, stable
    sections. `query=None` (the default) omits the section entirely —
    every existing caller that doesn't pass `query` sees the exact same
    `ContextPack` as before this parameter existed.
    """
    sections: list[ContextSection] = []

    case_summary = _read_or_placeholder(repo, CASE_SUMMARY_RELPATH, "_Not yet populated._")
    sections.append(ContextSection(key="case_summary", title="Case Summary", content=case_summary))

    if (repo.root / PATIENT_THEORIES_RELPATH).exists():
        patient_theories = repo.read(PATIENT_THEORIES_RELPATH)
        sections.append(
            ContextSection(
                key=PATIENT_THEORIES_SECTION_KEY,
                title="Patient Theories",
                content=patient_theories,
            )
        )

    sections.append(_recent_encounters_section(repo, recent_encounters_limit))
    sections.append(_labs_section(db))

    if (repo.root / GENOMICS_INVENTORY_RELPATH).exists():
        sections.append(
            ContextSection(
                key="genomics_inventory",
                title="Genomic Data On File",
                content=repo.read(GENOMICS_INVENTORY_RELPATH),
            )
        )

    open_questions = _read_or_placeholder(repo, OPEN_QUESTIONS_RELPATH, "_None yet._")
    sections.append(
        ContextSection(key="open_questions", title="Open Questions", content=open_questions)
    )

    if include_ledger:
        ledger = load_ledger(repo.root / LEDGER_RELPATH)
        sections.append(_render_ledger_section(ledger))

    excerpts_section = _document_excerpts_section(db, query)
    if excerpts_section is not None:
        sections.append(excerpts_section)

    return ContextPack(sections=sections, include_ledger=include_ledger)
