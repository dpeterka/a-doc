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

import logging
import re
from datetime import date
from pathlib import Path

from pydantic import BaseModel, Field

from adoc.casefile.disputes import DISPUTES_RELPATH, load_disputes
from adoc.casefile.encounters import (
    DatePrecision,
    Encounter,
    EncounterFrontmatter,
    read_encounter,
)
from adoc.casefile.ledger import load_ledger
from adoc.casefile.regimen import REGIMEN_RELPATH, Regimen, RegimenEntry, load_regimen
from adoc.casefile.repo import LEDGER_RELPATH, DataRepo
from adoc.casefile.reported import REPORTED_RESULTS_RELPATH, load_reported_results
from adoc.casefile.schema import Ledger
from adoc.intake.facts import IntakeFactsStore
from adoc.labs.db import LabsDb
from adoc.labs.models import LabResult
from adoc.labs.panels import derived_from_note, panel_sort_key
from adoc.labs.queries import abnormal_summary
from adoc.labs.validate import canonical_unit, convert_value

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


# `ingest.pipeline` writes this as the summary of every non-lab document it
# archives: it means "nobody has written a summary yet", NOT "this document is
# empty". Rendered bare, it read to a model as a status claim — a chat reply
# told the patient four of her documents were "still marked pending review, so
# I have no content from them yet", when two of the four carried 2,040 and
# 38,965 characters of extracted text that a targeted question would have
# retrieved. Same failure as ADR 0032's addendum: a placeholder stood in for
# the state, and the model reported the placeholder.
_PENDING_SUMMARY = "(pending review)"


def _encounter_summary(encounter: Encounter) -> str:
    """What to show for an encounter whose summary has not been written.

    Says which it is — text on file and searchable, or genuinely nothing
    extracted — so a reasoner can tell "unsummarised" from "empty" instead of
    guessing from a placeholder.
    """
    summary = encounter.summary.strip()
    if summary and summary != _PENDING_SUMMARY:
        return summary
    if (encounter.extracted_text or "").strip():
        return (
            "_no summary written yet — the full text of this document IS on file "
            "and searchable; ask about its contents to retrieve it_"
        )
    return "_no summary, and no text could be extracted from this document_"


def _recent_encounters_section(repo: DataRepo, limit: int) -> ContextSection:
    disputed_targets = load_disputes(repo.root / Path(DISPUTES_RELPATH)).open_targets()
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
        summary = _encounter_summary(encounter)
        # A patient-reported date is often "2021" or "spring 2022", which the
        # parser resolves to a January 1st. Rendering that bare invites a
        # reasoning stage to treat fabricated precision as real, and to order
        # events by a day nobody stated.
        when = _render_encounter_date(fm)
        # The citable ref, for the same reason lab rows carry theirs
        # (ADR 0028): a model that is shown only a date invents
        # `encounter:2026-08-04`, and encounter files are named
        # `YYYY-MM-DD--<slug>.md`. That exact miss cost two citations on a
        # live review.
        # A disputed item is still shown — the archived record is the source
        # of truth and she may be misremembering — but it must never be read
        # as established fact. Marking it here is what stops a phantom study
        # shaping a differential exactly as a real one would.
        disputed = f"encounter:{filename}" in disputed_targets
        mark = "  ⚠ DISPUTED BY THE PATIENT — do not treat as established" if disputed else ""
        lines.append(f"- **{when}** [{fm.type}]{provider}: {summary}  `encounter:{filename}`{mark}")

    return ContextSection(
        key="recent_encounters", title="Recent Encounters", content="\n".join(lines)
    )


def _render_encounter_date(fm: EncounterFrontmatter) -> str:
    """The encounter's date, stated no more precisely than it is known.

    `2021` parses to `2021-01-01` and `spring 2022` to `2022-01-01`; printing
    those bare asserts a day the patient never gave. Precision qualifies the
    date, and `reported_on` is shown when it differs from the event date so
    a stage can tell recall from contemporaneous record.
    """
    if fm.date_precision == "year":
        shown = str(fm.date.year)
    elif fm.date_precision == "month":
        shown = fm.date.strftime("%Y-%m")
    elif fm.date_precision == "approximate":
        shown = f"~{fm.date.year}"
    else:
        shown = fm.date.isoformat()
    if fm.reported_on and fm.reported_on != fm.date:
        shown = f"{shown} (reported {fm.reported_on.isoformat()})"
    return shown


_SLUG_PUNCT_RE = re.compile(r"[^a-z0-9]+")
"""Runs of non-alphanumerics in a lowercased analyte name, collapsed to `-`
by `_labs_ref` so a rendered ref satisfies the source-ref grammar."""


def _labs_ref(row: LabResult) -> str | None:
    """The source ref this row is cited by, rendered beside it.

    Document excerpts have always carried their own `doc:<file>#p<page>`
    ref, but lab rows did not — so a model asked to cite a lab value had to
    CONSTRUCT `labs:<slug>:<date>` from a name and a date, guessing the slug.
    A live blind panel guessed the prefix from the visible section heading
    and emitted `other:monospot_(heterophile)_screen:2026-03-17`, which is a
    real analyte on a real date with an invented prefix. Four such refs
    failed schema validation and took a 14-node review down with them.

    Showing the ref makes citing a matter of copying rather than inventing —
    but only if what is shown is what the checker accepts, and `row.name` on
    its own is NOT. The grammar's slug is `[^\\s:]+`: no whitespace, no
    colons. Real stored names break both rules (`IGF-1 Z-Score`,
    `Free T4:T3 Ratio`), so interpolating the raw name rendered an invalid
    ref for most of the real corpus — the same class of bug one layer down.
    Measured: 1178 of 2079 stored rows have a name that is not a legal slug,
    so the naive version would have printed an invalid ref beside more than
    half of them.

    So punctuation runs collapse to a single `-`. That satisfies the grammar
    and is *normalization-preserving*: `citations._normalize_slug` strips
    every non-alphanumeric character and lowercases, so `igf-1-z-score` and
    `IGF-1 Z-Score` reduce to the identical key `igf1zscore` and the ref
    resolves back to this row.

    Returns `None` for a name with no alphanumeric content at all, which
    cannot form a legal slug; the caller omits the ref rather than printing
    a broken one.
    """
    slug = _SLUG_PUNCT_RE.sub("-", row.name.lower()).strip("-")
    if not slug:
        return None
    return f"labs:{slug}:{row.date.isoformat()}"


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


# A trajectory needs at least this many readings to be a direction rather
# than a coincidence of two draws.
MIN_TRAJECTORY_POINTS = 3

# Only report movement that is unlikely to be assay noise. 20% is coarse on
# purpose: this section exists to say "look here", not to quantify.
TRAJECTORY_MIN_CHANGE = 0.20

# Cap the section so it stays predictable for prompt caching and cannot grow
# with the corpus. Ranked by magnitude, so the cap keeps the steepest moves.
MAX_TRAJECTORIES = 12


def _comparable_unit_key(unit: str | None) -> str:
    """A key two readings must share before their values may be compared.

    `labs.validate.canonical_unit` resolves cosmetic variants (`mcg/dL` and
    `ug/dL` both to `mcg/dl`) but returns `None` for plenty of real units —
    `IU/L`, `cells/uL`, `x10(9)/L`. Two `None`s must NOT be treated as
    equal: `cells/uL` and `x10(9)/L` differ by a factor of a billion, and
    26 of this patient's 461 analytes are stored under more than one unit.

    So: the canonical form where one exists, otherwise the raw string,
    case- and whitespace-normalized. Conservative by construction — an
    unrecognized unit only ever matches itself.
    """
    canonical = canonical_unit(unit) if unit else None
    if canonical:
        return canonical
    return (unit or "").strip().lower()


def _comparable_series(rows: list[LabResult]) -> list[tuple[LabResult, float]]:
    """Readings paired with their value expressed in the MOST RECENT unit.

    Two kinds of mixed-unit history exist in this corpus (ADR 0027). A
    cosmetic difference (`IU/L` vs `U/L`) is resolved by
    `labs.validate`'s synonym families and needs no conversion. A genuine
    magnitude difference — the CBC absolutes report `x10E3/uL` at some
    points and `cells/uL` at others, a factor of 1000 — is CONVERTED where
    an exact factor is known.

    A reading whose unit cannot be converted to the current one is dropped
    rather than compared: `convert_value` returns `None` instead of guessing,
    and comparing incomparable numbers is what produced "eosinophils rising
    319,900%".

    Converting rather than discarding matters: scoping to the latest unit
    alone threw away five of this patient's readings per CBC absolute, which
    is most of the early history.
    """
    numeric = [row for row in rows if row.value is not None]
    if not numeric:
        return []
    target = numeric[-1].ucum_unit
    converted: list[tuple[LabResult, float]] = []
    for row in numeric:
        assert row.value is not None
        value = convert_value(row.value, row.ucum_unit, target)
        if value is None:
            continue
        converted.append((row, value))
    return converted


def _trajectories_section(db: LabsDb) -> ContextSection:
    """Analytes that are MOVING, oldest-to-newest with the net change.

    The rest of this pack is a snapshot — "abnormal, most recent per analyte"
    and "latest panel". For a diagnostic odyssey the trajectory is often the
    clinical signal, not the level: bone density falling 8% in a year matters
    more than a T-score of −1.1, and a thyroid that failed and recovered is
    invisible in a single row.

    Before this, the only way a reasoning stage could see movement was to
    call the `query_labs` tool, or to read it out of a document that
    happened to narrate its own comparison (which is exactly how the blind
    panel knew about the DEXA decline — the report did the arithmetic, not
    a-doc). An analyte no document comments on had no visible slope at all.

    Deterministic: no model call, no interpretation. Direction and percent
    change only — whether a rise is good or bad is the reasoner's judgement,
    not this function's.
    """
    moving: list[tuple[float, str]] = []
    for name in db.distinct_analyte_names():
        series = _comparable_series(db.series(name))
        if len(series) < MIN_TRAJECTORY_POINTS:
            continue
        (first_row, first_value), (last_row, last_value) = series[0], series[-1]
        if first_value == 0:
            continue
        change = (last_value - first_value) / abs(first_value)
        if abs(change) < TRAJECTORY_MIN_CHANGE:
            continue
        unit = f" {last_row.ucum_unit}" if last_row.ucum_unit else ""
        direction = "rising" if change > 0 else "falling"
        moving.append(
            (
                abs(change),
                f"- {name}: {direction} {abs(change) * 100:.0f}% — "
                f"{first_value:g}{unit} ({first_row.date.isoformat()}) → "
                f"{last_value:g}{unit} ({last_row.date.isoformat()}), "
                f"{len(series)} readings",
            )
        )

    if not moving:
        return ContextSection(
            key="trajectories",
            title="Trajectories (analytes that are moving)",
            content="_No analyte has ≥3 readings with a net change over 20%._",
        )
    moving.sort(key=lambda item: item[0], reverse=True)
    lines = [line for _, line in moving[:MAX_TRAJECTORIES]]
    if len(moving) > MAX_TRAJECTORIES:
        lines.append(f"- _…and {len(moving) - MAX_TRAJECTORIES} more moving less steeply._")
    return ContextSection(
        key="trajectories",
        title="Trajectories (analytes that are moving)",
        content="\n".join(lines),
    )


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
                ref = _labs_ref(row)
                ref_suffix = f"  `{ref}`" if ref else ""
                lines.append(
                    f"- {_labs_label(row)}: {value}{unit}{flag} — "
                    f"{row.date.isoformat()}{ref_suffix}"
                )
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
                ref = _labs_ref(row)
                ref_suffix = f"  `{ref}`" if ref else ""
                lines.append(
                    f"- {_labs_label(row)}: {value}{unit} — "
                    f"{row.date.isoformat()}{note_suffix}{ref_suffix}"
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
    # Documents AND encounters. An encounter body was previously unreachable
    # once it fell outside the recent-encounters window, which meant anything
    # the patient said in a chat turn stopped being retrievable — the same
    # blindness that made a 110-line regimen document read as "on file but
    # not yet reconciled".
    hits = db.search_document_text(query, limit=MAX_DOCUMENT_EXCERPTS)
    hits += db.search_encounter_text(query, limit=MAX_DOCUMENT_EXCERPTS)
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
            # The plain-language gloss, and — when it is missing — the fact
            # that it is missing.
            #
            # The challenge sweep is asked to write a gloss only for
            # hypotheses that lack one, and the first review after that field
            # shipped produced ZERO glosses across 28 hypotheses: the prompt
            # told the model to check a state the pack never showed it, so it
            # could not tell which needed one and wrote none. This is ADR
            # 0028's rule one step out: if a model must act on a state, show
            # it the state.
            if h.plain_language.strip():
                lines.append(f"  plain-language: {h.plain_language.strip()}")
            else:
                lines.append("  plain-language: MISSING — needs a one-or-two-sentence gloss")
        content = "\n".join(lines)
    return ContextSection(key=LEDGER_SECTION_KEY, title="Differential Ledger", content=content)


REGIMEN_SECTION_KEY = "regimen"

# Substances known to distort immunoassays badly enough that a result drawn
# while they were active must be read differently. Deliberately short and
# hand-curated: this drives an explicit warning next to lab values, and a
# broad list would put a caveat on everything and so on nothing.
_ASSAY_INTERFERING = {
    "biotin": "can falsely shift many hormone and antibody immunoassays",
}

# Products whose NAME does not reveal what is inside them. Measured on the
# real case: the regimen document names 24 items and mentions biotin zero
# times, while the patient's biotin measured high — because biotin is
# routinely inside a B-complex at doses well above the interference
# threshold. Flagging the product is the honest move: the system cannot know
# the contents from the name, and saying so is what turns a vague "bring your
# bottles" into a specific question about one label.
_COMPOUND_PRODUCTS = ("complex", "multivitamin", "multi-vitamin", "hair", "skin", "nail")


def _regimen_section(repo: DataRepo, db: LabsDb) -> ContextSection:
    """What the patient takes, WITH dates, and which lab draws it overlaps.

    Exists because the reasoner could previously see that a supplement
    document was on file and nothing more. Measured on the real case: a
    110-line regimen encounter contributed 107 characters to the pack — its
    title — and 3,446 characters never reached any model. The review then
    reported the document as "on file but not yet reconciled", which was an
    accurate description of its own blindness, and the top next-appointment
    item asked the patient to bring supplement bottles so someone could
    establish what she takes.

    The alignment is the point. `still_taking` could never answer "was she
    taking biotin when this assay ran", and that question decides whether a
    hormone or antibody result is real or artefactual.
    """
    regimen = load_regimen(repo.root / Path(REGIMEN_RELPATH))
    if not regimen.entries:
        return ContextSection(
            key=REGIMEN_SECTION_KEY,
            title="Medications & Supplements",
            content="_Nothing recorded yet._",
        )

    lines: list[str] = []
    current = [e for e in regimen.entries if e.stopped is None]
    stopped = [e for e in regimen.entries if e.stopped is not None]

    if current:
        lines.append("**Currently taking**")
        lines += [f"- {_regimen_line(e)}" for e in current]
    if stopped:
        lines.append("")
        lines.append("**Stopped**")
        lines += [f"- {_regimen_line(e)}" for e in stopped]

    undated = regimen.undated()
    if undated:
        # Stated, not hidden: these are what make the overlap answers below
        # incomplete, and a reader deserves to know before relying on them.
        lines.append("")
        lines.append(
            f"_{len(undated)} entr{'y has' if len(undated) == 1 else 'ies have'} no "
            "start or stop date, so overlap with a lab draw cannot be determined "
            "for them._"
        )

    overlaps = _interference_warnings(regimen, db)
    if overlaps:
        lines.append("")
        lines.append("**Possible assay interference**")
        lines += overlaps

    return ContextSection(
        key=REGIMEN_SECTION_KEY,
        title="Medications & Supplements",
        content="\n".join(lines),
    )


def _regimen_line(entry: RegimenEntry) -> str:
    dose = f" {entry.dose}" if entry.dose else ""
    freq = f" {entry.frequency}" if entry.frequency else ""
    span = _regimen_span(entry)
    attribution = "" if entry.attribution == "unknown" else f" [{entry.attribution}]"
    return f"{entry.name}{dose}{freq}{attribution}{span}"


def _regimen_span(entry: RegimenEntry) -> str:
    """The interval, stated no more precisely than it is known — the same
    rule `_render_encounter_date` applies to encounters (ADR 0027)."""
    started = _precise(entry.started, entry.started_precision)
    stopped = _precise(entry.stopped, entry.stopped_precision)
    if started and stopped:
        return f" — {started} to {stopped}"
    if started:
        return f" — since {started}"
    if stopped:
        return f" — until {stopped}"
    return " — dates unknown"


def _precise(when: date | None, precision: DatePrecision | None) -> str:
    if when is None:
        return ""
    if precision == "year":
        return str(when.year)
    if precision == "month":
        return when.strftime("%Y-%m")
    if precision == "approximate":
        return f"~{when.year}"
    return when.isoformat()


def _interference_warnings(regimen: Regimen, db: LabsDb) -> list[str]:
    """Lab dates that fall inside an interfering substance's interval.

    Reported per DATE rather than per row: the point is that everything drawn
    that day is suspect, and listing hundreds of individual analytes would
    bury that.
    """
    intervals = [
        (entry, note)
        for entry in regimen.entries
        for key, note in _ASSAY_INTERFERING.items()
        if key in entry.name.lower()
    ]
    dates = sorted({row.date for row in db.all_non_rejected_rows()}, reverse=True)
    out: list[str] = []
    # Deliberately NOT behind an "is anything named biotin" early return: the
    # combination-product case exists precisely because nothing is. On the
    # real regimen, biotin is named zero times while the patient's biotin
    # measured high — it is inside a B complex.
    for entry in regimen.entries:
        lowered = entry.name.lower()
        if any(hint in lowered for hint in _COMPOUND_PRODUCTS) and not any(
            key in lowered for key in _ASSAY_INTERFERING
        ):
            out.append(
                f"- {entry.name} — a combination product; its contents are not known from "
                "the name. If it contains biotin, results drawn while taking it may be "
                "affected. The label settles it."
            )
    for entry, note in intervals:
        hits = [d for d in dates if entry.overlaps(d) == "active"]
        if hits:
            shown = ", ".join(d.isoformat() for d in hits[:8])
            more = f" (+{len(hits) - 8} more)" if len(hits) > 8 else ""
            out.append(f"- {entry.name} — {note}. Draws while active: {shown}{more}")
    return out


REPORTED_RESULTS_SECTION_KEY = "reported_results"


def _reported_results_section(repo: DataRepo) -> ContextSection | None:
    """Results the patient remembers but has no document for.

    Rendered in its own section, never mixed into the labs sections, and
    labelled on every line. The whole reason this record exists separately is
    that a remembered value must not be read as a measured one — merging them
    would put an uncitable number into the series the citation checker
    guards.

    Returns `None` when there are none, so a case with nothing reported looks
    exactly as it did before the feature existed.
    """
    results = load_reported_results(repo.root / Path(REPORTED_RESULTS_RELPATH))
    if not results.entries:
        return None

    lines = [
        "_The patient reports these from memory; no document is on file for them. "
        "They are NOT measured results and must be described as reported, not as findings._",
        "",
    ]
    for entry in results.entries:
        when = _precise(entry.when, entry.when_precision) or "date unknown"
        value = ""
        if entry.value is not None:
            value = f" {entry.value}{(' ' + entry.unit) if entry.unit else ''}"
        elif entry.direction != "unknown":
            value = f" {entry.direction}"
        status = {
            "unverified": "no document on file",
            "corroborated": "matches a measured result",
            "contradicted": "DISAGREES with the measured result",
        }[entry.verification]
        ref = f" ({entry.corroborating_source})" if entry.corroborating_source else ""
        lines.append(f"- {entry.analyte}{value} — {when} · {status}{ref}")
        if entry.note.strip():
            lines.append(f"  {entry.note.strip()}")

    return ContextSection(
        key=REPORTED_RESULTS_SECTION_KEY,
        title="Results the patient reports (unverified)",
        content="\n".join(lines),
    )


logger = logging.getLogger(__name__)

INTAKE_HISTORY_SECTION_KEY = "intake_history"

# Patient-reported topics no other context path carries. The rest are already
# covered: prior diagnoses reach `patient-theories.md`, allergies a
# `case-summary.md` block, events become encounters, symptoms the phenotype
# record, and medications/supplements the regimen (ADR 0031).
_INTAKE_HISTORY_SECTIONS: tuple[tuple[str, str], ...] = (
    ("family_history", "Family history"),
    ("geography", "Geography & exposures"),
    ("care_team", "Care team"),
)


def _intake_history_section(repo: DataRepo) -> ContextSection | None:
    """Patient-reported history, rendered from the FACTS rather than from the
    markdown artifacts derived from them.

    The artifacts are written only for topics the coverage state marks
    covered, so an uncovered topic leaves its facts on disk with nothing to
    read them. Measured on the live case file: 4 care-team facts against a
    34-byte artifact containing only its heading, and one supplement fact
    with no artifact at all. Reading the artifact reproduced that blindness
    exactly — the section skipped care team as empty while four facts sat
    beside it.

    Facts are the source of truth everywhere else in this system (see
    `intake.agent`'s "facts are the source of truth, a case-file artifact is
    always DERIVED from them"), so this reads them directly and coverage
    state can no longer gate what a reasoner sees.

    Each fact's `statement` is the patient-grounded sentence the intake
    recorded, so rendering it verbatim keeps her own words rather than a
    paraphrase built from parsed fields.
    """
    try:
        store = IntakeFactsStore(repo.root)
    except Exception:  # noqa: BLE001 - a missing/unreadable store is not fatal
        logger.warning("context: intake facts unavailable; skipping the history section")
        return None

    parts: list[str] = []
    for section_key, title in _INTAKE_HISTORY_SECTIONS:
        facts = store.active_facts(section_key)
        if not facts:
            continue
        lines = [f"- {fact.statement.strip()}" for fact in facts if fact.statement.strip()]
        if lines:
            parts.append(f"**{title}**\n" + "\n".join(lines))

    if not parts:
        return None
    return ContextSection(
        key=INTAKE_HISTORY_SECTION_KEY,
        title="Patient-reported history",
        content="\n\n".join(parts),
    )


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

    intake_history = _intake_history_section(repo)
    if intake_history is not None:
        sections.append(intake_history)

    sections.append(_recent_encounters_section(repo, recent_encounters_limit))
    sections.append(_labs_section(db))
    # After the snapshot, deliberately: the reader needs to know what the
    # current values ARE before being told which of them are moving.
    sections.append(_trajectories_section(db))
    sections.append(_regimen_section(repo, db))
    reported_section = _reported_results_section(repo)
    if reported_section is not None:
        sections.append(reported_section)

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
