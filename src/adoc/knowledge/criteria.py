"""Hand-encoded classification-criteria scorers, computed from stored labs.

**These are classification criteria, not diagnostic criteria.** They were
built to define homogeneous cohorts for research, not to diagnose a patient
in front of you, and a person can have the disease while failing them. Every
surface that renders a result must say so; `CriteriaResult.disclaimer`
carries the sentence so no caller has to remember to write it.

Design notes that matter more than the arithmetic:

**Three states, never two.** Each item is `met`, `not_met`, or
`not_assessed`. Most of these criteria mix laboratory items with clinical
ones (fever, arthritis, oral ulcers, biopsy classes) that no lab row can
answer. A scorer that scored an unseen item as `not_met` would report a
confident low total that is really an artifact of missing input — the
failure mode most likely to talk a reader out of a real diagnosis. Unseen
items are `not_assessed`, the total is explicitly a **floor**, and
`points_not_assessed` states how much is unaccounted for.

**Cite everything.** Every met item carries the `labs:<slug>:<date>` ref it
was decided from, so a criteria result is checkable by the same citation
machinery as any model claim (ADR 0028).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from adoc.casefile.regimen import Regimen
from adoc.labs.models import LabResult, flag_is_high, flag_is_low
from adoc.labs.validate import convert_value

CLASSIFICATION_DISCLAIMER = (
    "Classification criteria, not diagnostic criteria: they exist to define "
    "comparable groups for research, and a person can have the condition "
    "without meeting them."
)


_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def _normalize_slug(text: str) -> str:
    """Case/punctuation-insensitive key for analyte matching.

    A deliberately local implementation, for the reason `reason.citations`
    states about its own copy: a module should not reach into another
    module's private helper. The first version of this file imported
    `citations._normalize_slug` and so broke the very convention that
    function's docstring exists to record.
    """
    return _NON_ALNUM_RE.sub("", text.lower())


PhenotypeLookup = dict[str, tuple[str, bool, str]]
"""`{term_id: (label, present, context)}` — the minimum a scorer needs from a
phenotype profile, passed as a plain mapping so `knowledge` never imports
`casefile` and the scorers stay unit-testable without a repo."""


ItemState = str
"""One of `met`, `not_met`, `not_assessed`, `possible`.

`possible` exists because of a specific near-miss. The phenotype profile is
built by matching text, and text matching cannot know ATTRIBUTION. Two terms
in this patient's profile would have scored as met SLE criteria:

    Seizure   <- "clonic grand mal seizure while taking wellbutrin"
    Arthritis <- "mitochondrial dysfunction psoriatic arthritis metabolic..."

The first is a bupropion-induced seizure — a well-known adverse effect, not a
neuropsychiatric manifestation of lupus. The second reads as an item in a list
of conditions being CONSIDERED, not a confirmed finding. Together they are 11
points against a threshold of 10.

The 2019 criteria forbid exactly this in their own text: a criterion counts
only if there is **no more likely explanation**. An automated matcher cannot
make that judgement, so it must not claim the criterion. `possible` says what
is true — something matching this item appears in the record — and leaves the
attribution to a clinician."""


class CriterionItem(BaseModel):
    """One scored line of a criteria set."""

    domain: str
    name: str
    weight: int
    state: ItemState
    basis: str = ""
    """Why this state, in a phrase a patient could read."""
    sources: list[str] = Field(default_factory=list)
    """`labs:<slug>:<date>` refs the state was decided from."""

    met_ever: bool = False
    """Met by a HISTORICAL value while the most recent one does not meet it
    (ADR 0042). Classification criteria are cumulative — the 2019 EULAR/ACR
    set states that criteria need not occur simultaneously — so this is a
    genuine met, not a weaker one. It is flagged because the pair of facts is
    what a clinician needs: positive then, normal now."""

    superseded: str = ""
    """The later value, and the regimen context for it if any is on file.
    Empty unless `met_ever`."""


class CriteriaResult(BaseModel):
    """A whole criteria set applied to this patient's stored data."""

    key: str
    name: str
    citation: str
    """The published criteria this encodes, so a doctor can look it up."""

    entry_met: bool | None = None
    """Whether an entry criterion (if the set has one) is satisfied. `None`
    when the set has none. `False` means the set does not apply and the score
    below is reported for transparency only."""
    entry_note: str = ""

    items: list[CriterionItem] = Field(default_factory=list)
    threshold: int
    points: int
    """Points from `met` items only — a FLOOR, not an estimate. `possible`
    items are deliberately excluded: they are unattributed."""
    points_possible: int = 0
    """What the `possible` items would add IF a clinician attributed them to
    this condition. Reported separately so the reader can see that the total
    is one confirmation away from crossing a threshold, without the scorer
    ever claiming it has."""
    points_not_assessed: int
    """Weight sitting in items no stored data could answer."""
    meets_threshold: bool
    """Computed from `points` alone. A `possible` item can never carry a
    patient over a classification threshold — that is the whole reason the
    state exists."""
    requires_clinical_item: bool = False
    clinical_item_met: bool = False
    disclaimer: str = CLASSIFICATION_DISCLAIMER

    @property
    def assessable(self) -> bool:
        """Whether enough was answerable for the total to mean anything."""
        return any(i.state != "not_assessed" for i in self.items)


# --- reading the labs -------------------------------------------------------------------


Lookback = Literal["ever", "current"]
"""Whether a criterion reads the whole record or only the latest draw.

`ever` is the default for every laboratory criterion here, because every set
encoded in this module is a *classification* set and classification sets are
cumulative by construction. `current` exists so a criterion that genuinely
describes a present state can say so explicitly rather than by omission.
"""

# Drugs that suppress the markers these criteria read. Deliberately a short,
# named list rather than a drug database: its only job is to add context to a
# criterion already met historically, so a miss costs a sentence of
# explanation and never a scored point. Not exhaustive, and documented as
# such — a name not here simply produces no note (ADR 0042).
IMMUNOSUPPRESSANT_PATTERNS: tuple[str, ...] = (
    r"prednis(one|olone)",
    r"methylprednisolone",
    r"dexamethasone",
    r"hydroxychloroquine",
    r"chloroquine",
    r"methotrexate",
    r"azathioprine",
    r"mycophenolat",
    r"leflunomide",
    r"cyclophosphamide",
    r"ciclosporin|cyclosporin",
    r"tacrolimus",
    r"sirolimus",
    r"rituximab",
    r"belimumab",
    r"anifrolumab",
    r"abatacept",
    r"tocilizumab",
    r"(inflix|adalim|golim|certoliz)umab",
    r"etanercept",
    r"(tofa|bari|upada)citinib",
    r"ivig|immunoglobulin",
)

_IMMUNOSUPPRESSANT_RE = re.compile("|".join(IMMUNOSUPPRESSANT_PATTERNS), re.IGNORECASE)


def suppressants_active_on(regimen: Regimen | None, when: date) -> list[str]:
    """Immunosuppressant names in force on `when`, per `case/regimen.yaml`.

    Uses `Regimen.active_on`, which reports `unknown` rather than guessing for
    an undated entry — so an entry that cannot be placed in time contributes
    nothing here instead of a confident wrong answer.
    """
    if regimen is None:
        return []
    return sorted(
        {e.name.strip() for e in regimen.active_on(when) if _IMMUNOSUPPRESSANT_RE.search(e.name)}
    )


def _superseded_note(*, met_row: LabResult, latest: LabResult, regimen: Regimen | None) -> str:
    """The two facts a clinician needs when a criterion is met historically.

    CLN-03's second half: a normalised marker under active immunosuppression
    is an expected treatment effect, not evidence against the diagnosis. The
    note says which drug and leaves the inference to the reader — this module
    never claims causation.
    """
    shown_met = met_row.value if met_row.value is not None else met_row.value_text
    shown_now = latest.value if latest.value is not None else latest.value_text
    note = (
        f"Met by {shown_met} on {met_row.date.isoformat()}; the most recent value is "
        f"{shown_now} on {latest.date.isoformat()}."
    )
    active = suppressants_active_on(regimen, latest.date)
    if active:
        note += (
            f" On {latest.date.isoformat()} the record has {', '.join(active)} in use, which "
            "can suppress this marker."
        )
    return note


@dataclass
class LabView:
    """The stored rows, indexed for criteria lookup.

    Keeps BOTH the most recent row per analyte and the full history, because
    the two answer different questions and criteria need the second one.

    This class used to keep only the latest row, on the reasoning that "a
    criteria set describes a patient's current classifiable state; an
    abnormality from four years ago that has since resolved should not keep
    scoring points forever." That reasoning is wrong for a *classification*
    set and it produced a measurable false negative (ADR 0042). The 2019
    EULAR/ACR SLE criteria state that criteria need not occur simultaneously,
    and `score_sle_2019`'s own docstring says the entry criterion is
    "ANA ≥1:80 **ever**" while the code read the latest ANA.

    Measured on a suppressed-disease timeline — ANA 1:640, anti-dsDNA
    positive and C3 48 in 2024, all normal in 2026:

        history only   entry_met=True   6/10 points
        with follow-up entry_met=False  0/10 points, "criteria do not apply"

    Adding a later NORMAL result erased the entire historical basis. Under
    treatment that is the expected trajectory, so the old behaviour scored
    successful suppression as evidence the disease was never there.
    """

    rows: Sequence[LabResult]
    _latest: dict[str, LabResult] = field(default_factory=dict, init=False)
    _history: dict[str, list[LabResult]] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        for row in sorted(self.rows, key=lambda r: r.date):
            for key in self._keys(row):
                self._latest[key] = row
                self._history.setdefault(key, []).append(row)

    def history(self, *patterns: str) -> list[LabResult]:
        """Every matching row over all time, most recent first.

        Unlike `find_all`, which returns one row per analyte, this returns
        every DRAW — the sequence a cumulative criterion has to read.
        """
        seen: dict[int, LabResult] = {}
        for key, rows in self._history.items():
            if any(re.search(pattern, key) for pattern in patterns):
                for row in rows:
                    seen[id(row)] = row
        return sorted(seen.values(), key=lambda r: r.date, reverse=True)

    @staticmethod
    def _keys(row: LabResult) -> set[str]:
        return {_normalize_slug(n) for n in (row.name, row.name_raw) if n}

    def find(self, *patterns: str) -> LabResult | None:
        """The most recent row whose normalized name matches any pattern."""
        found = self.find_all(*patterns)
        return found[0] if found else None

    def find_all(self, *patterns: str) -> list[LabResult]:
        """Every distinct analyte matching any pattern, most recent first.

        Patterns are regexes against the NORMALIZED name (lowercase,
        non-alphanumerics stripped), not literal names. Enumerating spellings
        was tried first and failed on the real corpus within minutes: the
        stored names are `Complement C4c` and `Smith (Sm) Antibody`, which no
        reasonable hand-written list of aliases contains. A criteria scorer
        that silently reports `not_assessed` for an analyte the patient
        actually had measured is worse than one that errors — it looks like an
        answer.
        """
        seen: dict[int, LabResult] = {}
        for key, row in self._latest.items():
            if any(re.search(pattern, key) for pattern in patterns):
                seen[id(row)] = row
        return sorted(seen.values(), key=lambda r: r.date, reverse=True)

    @staticmethod
    def ref(row: LabResult) -> str:
        slug = _slugify(row.name)
        return f"labs:{slug}:{row.date.isoformat()}"


def _slugify(name: str) -> str:
    out = "".join(c if c.isalnum() else "-" for c in name.lower())
    return "-".join(part for part in out.split("-") if part)


def _is_positive(row: LabResult) -> bool:
    """Whether a qualitative row reads as positive/reactive/detected.

    Serology is stored as text far more often than as a number, and the
    wording varies by lab. Negation is checked FIRST because "not detected"
    contains "detected" — the substring order is the whole correctness of
    this function.
    """
    text = (row.value_text or "").strip().lower()
    if not text:
        return False
    negative_markers = (
        "not detected",
        "non-reactive",
        "nonreactive",
        "negative",
        "none seen",
        "absent",
        "not present",
    )
    if any(marker in text for marker in negative_markers):
        return False
    positive_markers = ("positive", "reactive", "detected", "present", "abnormal")
    return any(marker in text for marker in positive_markers)


def _below_reference(row: LabResult) -> bool:
    """Whether the row is flagged low by the lab itself.

    Complement thresholds are lab- and method-specific, so the lab's own
    reference range is a better authority than any constant this file could
    hardcode.
    """
    return flag_is_low(row.flag)


def _numeric_below(row: LabResult, threshold: float) -> bool:
    return row.value is not None and row.value < threshold


# --- SLE 2019 EULAR/ACR ------------------------------------------------------------------


# Regexes over NORMALIZED analyte names (lowercase, non-alphanumerics
# stripped). Anchored where a bare token would be ambiguous, loose where the
# real corpus proved it has to be — `Complement C4c` and `Smith (Sm)
# Antibody` are both real stored spellings that defeated an alias list.
_SLE_ANA_NAMES = (r"^ana$", r"^ana(titer|screen|ifa|pattern)", r"antinuclearantib")
_SLE_DSDNA_NAMES = (r"dsdna", r"doublestrandeddna")
_SLE_SMITH_NAMES = (r"smith", r"^antism\b", r"^sm(ith)?antibody")
_SLE_C3_NAMES = (r"^(complement)?c3[a-z]?$",)
_SLE_C4_NAMES = (r"^(complement)?c4[a-z]?$",)
_SLE_WBC_NAMES = (r"^wbc$", r"^whitebloodcell", r"^leukocytes?$")
_SLE_PLATELET_NAMES = (r"^plateletcount$", r"^platelets$", r"^plt$")
# The 2019 criteria name anti-cardiolipin IgG/IgM and anti-β2GP1 IgG/IgM.
# IgA isotypes are deliberately NOT matched: the published set does not
# include them, and this patient has a stored `Cardiolipin Antibody IgA` that
# would otherwise score a point the criteria do not award.
_SLE_APL_NAMES = (
    r"cardiolipin.*ig[gm]$",
    r"beta2glycoprotein.*ig[gm]$",
    r"b2glycoprotein.*ig[gm]$",
    r"lupusanticoagulant",
)


def _clinical_item(
    domain: str, name: str, weight: int, phenotype: PhenotypeLookup | None
) -> CriterionItem:
    """A clinical criterion, raised to `possible` when the phenotype record
    contains a matching term — never to `met`.

    Attribution is the reason. The published criteria count an item only when
    there is no more likely explanation, and a term matched out of narrative
    text carries no such judgement: a seizure on bupropion and a seizure from
    lupus produce the same HPO term.
    """
    term_ids = _SLE_CLINICAL_TERMS.get(name, ())
    if phenotype is None or not term_ids:
        return CriterionItem(
            domain=domain,
            name=name,
            weight=weight,
            state="not_assessed",
            basis="Needs a symptom/phenotype record; not computable from labs.",
        )

    for term_id in term_ids:
        found = phenotype.get(term_id)
        if found is None:
            continue
        label, present, context = found
        if not present:
            return CriterionItem(
                domain=domain,
                name=name,
                weight=weight,
                state="not_met",
                basis=f"Recorded as excluded ({label}).",
                sources=[f"phenotype:{term_id}"],
            )
        excerpt = f' from "{context}"' if context else ""
        return CriterionItem(
            domain=domain,
            name=name,
            weight=weight,
            state="possible",
            basis=(
                f"{label} appears in the record{excerpt}. NOT counted: these "
                "criteria require that no more likely explanation exists, and "
                "that judgement is a clinician's."
            ),
            sources=[f"phenotype:{term_id}"],
        )

    return CriterionItem(
        domain=domain,
        name=name,
        weight=weight,
        state="not_assessed",
        basis="Nothing matching this appears in the phenotype record.",
    )


def score_sle_2019(
    rows: Sequence[LabResult],
    phenotype: PhenotypeLookup | None = None,
    regimen: Regimen | None = None,
) -> CriteriaResult:
    """2019 EULAR/ACR classification criteria for SLE.

    Structure of the published set, all of which this preserves: an **entry
    criterion** (ANA ≥1:80 ever) without which the set does not apply; weighted
    additive items; **only the highest-weighted met item in a domain counts**;
    and a threshold of 10 points that additionally requires at least one
    clinical item.

    Only the laboratory items are computable here. The clinical domains —
    constitutional, mucocutaneous, serosal, musculoskeletal, neuropsychiatric,
    and the renal biopsy classes — need a phenotype profile this system does
    not have yet, so they are `not_assessed` rather than `not_met`. That is
    why `points` is documented everywhere as a floor: for this criteria set in
    particular, a patient can reach the threshold entirely on clinical items
    that are invisible here, and the requirement of at least one clinical item
    means a lab-only score can never actually classify.
    """
    view = LabView(rows)
    items: list[CriterionItem] = []

    # "ANA ≥1:80 EVER" — the docstring above has said so since this scorer
    # was written, while the code read only the latest draw (ADR 0042). A
    # patient whose ANA was 1:640 in 2024 and negative in 2026 had the whole
    # set switched off by the later, better result.
    ana_history = view.history(*_SLE_ANA_NAMES)
    ana_ever = next((row for row in ana_history if _ana_titer_at_least_1_80(row)), None)
    ana_latest = ana_history[0] if ana_history else None
    if ana_latest is None:
        entry_met = None
        entry_note = "No ANA result on file, so the entry criterion cannot be evaluated."
    elif ana_ever is not None:
        entry_met = True
        shown = ana_ever.value_text or ana_ever.value
        entry_note = f"ANA {shown} meets the ≥1:80 entry criterion."
        if ana_ever is not ana_latest:
            entry_note += (
                f" That was on {ana_ever.date.isoformat()}; the most recent ANA is "
                f"{ana_latest.value_text or ana_latest.value} on "
                f"{ana_latest.date.isoformat()}. The published criterion is met EVER, so "
                "the later result does not withdraw it."
            )
            active = suppressants_active_on(regimen, ana_latest.date)
            if active:
                entry_note += (
                    f" {', '.join(active)} was in use on that date, which can suppress it."
                )
    else:
        entry_met = False
        shown = ana_latest.value_text or ana_latest.value
        measured = (
            f"ANA {shown}"
            if len(ana_history) == 1
            else (
                f"No ANA on file reaches 1:80 across {len(ana_history)} draws (most recent {shown})"
            )
        )
        entry_note = f"{measured} does not reach 1:80; the 2019 criteria do not apply without it."

    # --- clinical domains, answered from the phenotype profile when it can --
    for domain, name, weight in _SLE_CLINICAL_ITEMS:
        items.append(_clinical_item(domain, name, weight, phenotype))

    # --- haematologic --------------------------------------------------------
    items.append(
        _threshold_item(
            view,
            domain="Haematologic",
            name="Leukopenia (<4.0 ×10⁹/L)",
            weight=3,
            names=_SLE_WBC_NAMES,
            predicate=lambda row: _numeric_below(row, 4.0),
            regimen=regimen,
        )
    )
    items.append(
        _threshold_item(
            view,
            domain="Haematologic",
            name="Thrombocytopenia (<100 ×10⁹/L)",
            weight=4,
            names=_SLE_PLATELET_NAMES,
            predicate=lambda row: _numeric_below(row, 100.0),
            regimen=regimen,
        )
    )

    # --- antiphospholipid ----------------------------------------------------
    items.append(
        _any_positive_item(
            view,
            domain="Antiphospholipid antibodies",
            name="Anti-cardiolipin, anti-β2GP1, or lupus anticoagulant",
            weight=2,
            names=_SLE_APL_NAMES,
            regimen=regimen,
        )
    )

    # --- complement ----------------------------------------------------------
    items.extend(_sle_complement_items(view, regimen))

    # --- SLE-specific antibodies ---------------------------------------------
    items.append(
        _any_positive_item(
            view,
            domain="SLE-specific antibodies",
            name="Anti-dsDNA or anti-Smith",
            weight=6,
            names=_SLE_DSDNA_NAMES + _SLE_SMITH_NAMES,
            regimen=regimen,
        )
    )

    return _finalize(
        key="sle-2019",
        name="SLE — 2019 EULAR/ACR classification criteria",
        citation="Aringer M, et al. Arthritis Rheumatol. 2019;71(9):1400-1412.",
        items=items,
        threshold=10,
        entry_met=entry_met,
        entry_note=entry_note,
        requires_clinical_item=True,
        clinical_domains=_SLE_CLINICAL_DOMAINS,
    )


# HPO terms that would match each clinical item, used ONLY to raise it to
# `possible`. Never to `met` — see `ItemState`.
_SLE_CLINICAL_TERMS: dict[str, tuple[str, ...]] = {
    "Fever": ("HP:0001945",),
    "Delirium": ("HP:0031258",),
    "Psychosis": ("HP:0000709",),
    "Seizure": ("HP:0001250",),
    "Non-scarring alopecia": ("HP:0002293", "HP:0001596"),
    "Oral ulcers": ("HP:0000155",),
    "Subacute cutaneous or discoid lupus": ("HP:0001056", "HP:0005306"),
    "Acute cutaneous lupus": ("HP:0011110",),
    "Pleural or pericardial effusion": ("HP:0002202", "HP:0001698"),
    "Acute pericarditis": ("HP:0001701",),
    "Joint involvement": ("HP:0001369",),
    "Proteinuria >0.5 g/24 h": ("HP:0000093",),
    "Autoimmune haemolysis": ("HP:0004854",),
}

_SLE_CLINICAL_ITEMS: tuple[tuple[str, str, int], ...] = (
    ("Constitutional", "Fever", 2),
    ("Neuropsychiatric", "Delirium", 2),
    ("Neuropsychiatric", "Psychosis", 3),
    ("Neuropsychiatric", "Seizure", 5),
    ("Mucocutaneous", "Non-scarring alopecia", 2),
    ("Mucocutaneous", "Oral ulcers", 2),
    ("Mucocutaneous", "Subacute cutaneous or discoid lupus", 4),
    ("Mucocutaneous", "Acute cutaneous lupus", 6),
    ("Serosal", "Pleural or pericardial effusion", 5),
    ("Serosal", "Acute pericarditis", 6),
    ("Musculoskeletal", "Joint involvement", 6),
    ("Renal", "Proteinuria >0.5 g/24 h", 4),
    ("Renal", "Class II or V lupus nephritis", 8),
    ("Renal", "Class III or IV lupus nephritis", 10),
    ("Haematologic", "Autoimmune haemolysis", 4),
)

_SLE_CLINICAL_DOMAINS = frozenset(
    {
        "Constitutional",
        "Neuropsychiatric",
        "Mucocutaneous",
        "Serosal",
        "Musculoskeletal",
        "Renal",
        "Haematologic",
    }
)


def _ana_titer_at_least_1_80(row: LabResult) -> bool:
    """Whether an ANA row reaches the 1:80 entry titer.

    Titres are stored as text (`1:640`), and the denominator is what matters:
    1:640 is a HIGHER titre than 1:80 despite the larger number reading like
    a bigger fraction. A row with no parseable titre falls back to the lab's
    own positive/negative wording, which is the best available signal when a
    lab reports ANA qualitatively.
    """
    text = (row.value_text or "").strip()
    if ":" in text:
        _, _, denominator = text.partition(":")
        digits = "".join(c for c in denominator if c.isdigit())
        if digits:
            return int(digits) >= 80
    return _is_positive(row)


def _threshold_item(
    view: LabView,
    *,
    domain: str,
    name: str,
    weight: int,
    names: tuple[str, ...],
    predicate: Callable[[LabResult], bool],
    lookback: Lookback = "ever",
    regimen: Regimen | None = None,
) -> CriterionItem:
    """One threshold criterion, read over the whole record by default.

    `lookback="ever"` scans every draw and is met if ANY satisfies the
    predicate (ADR 0042). When the value that met it is not the latest one,
    the item carries both — `met_ever` plus a `superseded` note naming the
    later value and any immunosuppressant in force when it was drawn.
    """
    timeline = (
        view.history(*names) if lookback == "ever" else ([r] if (r := view.find(*names)) else [])
    )
    if not timeline:
        return CriterionItem(
            domain=domain,
            name=name,
            weight=weight,
            state="not_assessed",
            basis="No result on file.",
        )

    latest = timeline[0]
    meeting = [row for row in timeline if predicate(row)]
    if not meeting:
        shown = latest.value if latest.value is not None else latest.value_text
        measured = (
            f"Most recent value {shown} on {latest.date.isoformat()}"
            if len(timeline) == 1
            else f"{len(timeline)} draws on file, none meeting it; most recent {shown} "
            f"on {latest.date.isoformat()}"
        )
        return CriterionItem(
            domain=domain,
            name=name,
            weight=weight,
            state="not_met",
            basis=f"{measured}.",
            sources=[LabView.ref(latest)],
        )

    # Newest first, so the first match is the most recent one that met it.
    met_row = meeting[0]
    shown = met_row.value if met_row.value is not None else met_row.value_text
    if met_row is latest:
        return CriterionItem(
            domain=domain,
            name=name,
            weight=weight,
            state="met",
            basis=f"Most recent value {shown} on {met_row.date.isoformat()}.",
            sources=[LabView.ref(met_row)],
        )
    return CriterionItem(
        domain=domain,
        name=name,
        weight=weight,
        state="met",
        basis=f"Met on {met_row.date.isoformat()} ({shown}); not by the most recent draw.",
        sources=[LabView.ref(met_row), LabView.ref(latest)],
        met_ever=True,
        superseded=_superseded_note(met_row=met_row, latest=latest, regimen=regimen),
    )


def _any_positive_item(
    view: LabView,
    *,
    domain: str,
    name: str,
    weight: int,
    names: tuple[str, ...],
    lookback: Lookback = "ever",
    regimen: Regimen | None = None,
) -> CriterionItem:
    """Met when ANY of `names` reads positive at ANY point on the record;
    `not_met` only when at least one was actually measured and none were
    positive.

    Autoantibody criteria are the clearest case for `ever` (ADR 0042): a
    seroconversion to negative under treatment does not un-happen the
    positive result the published criteria count.
    """
    found = view.history(*names) if lookback == "ever" else view.find_all(*names)
    if not found:
        return CriterionItem(
            domain=domain, name=name, weight=weight, state="not_assessed", basis="None on file."
        )
    positives = [row for row in found if _is_positive(row) or _numeric_above_ref(row)]
    if positives:
        latest = found[0]
        met_ever = latest not in positives
        return CriterionItem(
            domain=domain,
            name=name,
            weight=weight,
            state="met",
            basis="; ".join(
                f"{row.name} {row.value_text or row.value} ({row.date.isoformat()})"
                for row in positives[:4]
            ),
            sources=[LabView.ref(row) for row in positives],
            met_ever=met_ever,
            superseded=(
                _superseded_note(met_row=positives[0], latest=latest, regimen=regimen)
                if met_ever
                else ""
            ),
        )
    return CriterionItem(
        domain=domain,
        name=name,
        weight=weight,
        state="not_met",
        basis=f"{len(found)} measured, none positive.",
        sources=[LabView.ref(row) for row in found],
    )


def _numeric_above_ref(row: LabResult) -> bool:
    return flag_is_high(row.flag)


def _lowest_on_record(view: LabView, names: tuple[str, ...]) -> LabResult | None:
    """The lowest recorded value for an analyte, or the latest if none has a
    number (ADR 0042).

    "Lowest" and not "first below reference": reference ranges differ between
    labs, so the comparison that decides `_below_reference` has to happen on
    the row that carries its own range. Picking the numeric minimum keeps that
    property and answers the criterion — was complement EVER consumed.
    """
    rows = view.history(*names)
    if not rows:
        return None
    numeric = [row for row in rows if row.value is not None]
    if not numeric:
        return rows[0]
    below = [row for row in numeric if _below_reference(row)]
    pool = below or numeric
    return min(pool, key=lambda r: r.value if r.value is not None else float("inf"))


def _sle_complement_items(view: LabView, regimen: Regimen | None = None) -> list[CriterionItem]:
    """The complement domain, whose two items are mutually exclusive by
    construction: low C3 *or* low C4 scores 3, low C3 *and* low C4 scores 4.
    Domain de-duplication would keep only the 4 anyway, but scoring both as
    `met` when only one complement is low would misreport WHY.

    Read over the whole record (ADR 0042). Consumed complement is the
    textbook example of a marker that normalises on treatment: it is what
    CLN-03 named first, and reading only the latest draw scored successful
    suppression as evidence against the diagnosis.
    """
    c3 = _lowest_on_record(view, _SLE_C3_NAMES)
    c4 = _lowest_on_record(view, _SLE_C4_NAMES)
    c3_latest = view.find(*_SLE_C3_NAMES)
    c4_latest = view.find(*_SLE_C4_NAMES)
    measured = [row for row in (c3, c4) if row is not None]
    if not measured:
        return [
            CriterionItem(
                domain="Complement",
                name="Low C3 or low C4",
                weight=3,
                state="not_assessed",
                basis="Neither complement on file.",
            ),
            CriterionItem(
                domain="Complement",
                name="Low C3 and low C4",
                weight=4,
                state="not_assessed",
                basis="Neither complement on file.",
            ),
        ]
    c3_low = c3 is not None and _below_reference(c3)
    c4_low = c4 is not None and _below_reference(c4)
    sources = [LabView.ref(row) for row in measured]
    historical = [
        (low, row, latest)
        for low, row, latest in ((c3_low, c3, c3_latest), (c4_low, c4, c4_latest))
        if low and row is not None and latest is not None and row is not latest
    ]
    met_ever = bool(historical)
    superseded = (
        "; ".join(
            _superseded_note(met_row=row, latest=latest, regimen=regimen)
            for _low, row, latest in historical
        )
        if met_ever
        else ""
    )
    if met_ever:
        sources = sources + [
            LabView.ref(latest) for _low, _row, latest in historical if latest not in measured
        ]
    both_state = "met" if (c3_low and c4_low) else "not_met"
    # "Either" is recorded as met only when it is the HIGHEST met item — when
    # both are low the 4-point item supersedes it, and marking both met would
    # double-count a single biological finding in the itemised display.
    either_state = "met" if (c3_low or c4_low) and not (c3_low and c4_low) else "not_met"
    if c3 is None or c4 is None:
        both_state = "not_assessed"
    return [
        CriterionItem(
            domain="Complement",
            name="Low C3 or low C4",
            weight=3,
            state=either_state,
            basis=_complement_basis(c3, c4, c3_low, c4_low),
            sources=sources,
            met_ever=met_ever and either_state == "met",
            superseded=superseded if either_state == "met" else "",
        ),
        CriterionItem(
            domain="Complement",
            name="Low C3 and low C4",
            weight=4,
            state=both_state,
            basis=_complement_basis(c3, c4, c3_low, c4_low),
            sources=sources,
            met_ever=met_ever and both_state == "met",
            superseded=superseded if both_state == "met" else "",
        ),
    ]


def _complement_basis(
    c3: LabResult | None, c4: LabResult | None, c3_low: bool, c4_low: bool
) -> str:
    parts = []
    for label, row, low in (("C3", c3, c3_low), ("C4", c4, c4_low)):
        if row is None:
            parts.append(f"{label} not on file")
        else:
            parts.append(
                f"{label} {row.value if row.value is not None else row.value_text}"
                f" ({'low' if low else 'not low'})"
            )
    return "; ".join(parts)


# --- assembly ----------------------------------------------------------------------------


def _finalize(
    *,
    key: str,
    name: str,
    citation: str,
    items: list[CriterionItem],
    threshold: int,
    entry_met: bool | None,
    entry_note: str,
    requires_clinical_item: bool,
    clinical_domains: frozenset[str],
) -> CriteriaResult:
    """Apply the highest-item-per-domain rule and total the result.

    The domain rule is the part most easily got wrong: these sets are additive
    ACROSS domains but take only the single highest-weighted met item WITHIN
    one. Summing every met item would inflate a patient whose one affected
    organ system happens to have several graded entries.
    """
    best_by_domain: dict[str, CriterionItem] = {}
    for item in items:
        if item.state != "met":
            continue
        current = best_by_domain.get(item.domain)
        if current is None or item.weight > current.weight:
            best_by_domain[item.domain] = item

    points = sum(item.weight for item in best_by_domain.values())

    # `possible` points, per-domain maxima like the met ones, and reduced by
    # whatever that domain already scores — confirming a possible item only
    # gains the difference, not its full weight.
    best_possible: dict[str, int] = {}
    for item in items:
        if item.state != "possible":
            continue
        best_possible[item.domain] = max(best_possible.get(item.domain, 0), item.weight)
    points_possible = sum(
        max(0, weight - best_by_domain[domain].weight) if domain in best_by_domain else weight
        for domain, weight in best_possible.items()
    )

    # Unassessed weight is also counted per-domain: the most a domain could
    # still contribute is its single heaviest unanswered item, not their sum.
    not_assessed_by_domain: dict[str, int] = {}
    for item in items:
        if item.state != "not_assessed":
            continue
        not_assessed_by_domain[item.domain] = max(
            not_assessed_by_domain.get(item.domain, 0), item.weight
        )
    # A domain that already scored can only gain the difference, never its full weight.
    points_not_assessed = sum(
        max(0, weight - best_by_domain[domain].weight) if domain in best_by_domain else weight
        for domain, weight in not_assessed_by_domain.items()
    )

    clinical_met = any(domain in clinical_domains for domain in best_by_domain)
    meets = points >= threshold and (clinical_met or not requires_clinical_item)
    if entry_met is False:
        meets = False

    return CriteriaResult(
        key=key,
        name=name,
        citation=citation,
        entry_met=entry_met,
        entry_note=entry_note,
        items=items,
        threshold=threshold,
        points=points,
        points_possible=points_possible,
        points_not_assessed=points_not_assessed,
        meets_threshold=meets,
        requires_clinical_item=requires_clinical_item,
        clinical_item_met=clinical_met,
    )


# --- Sjögren 2016 ACR/EULAR --------------------------------------------------------------


_SJOGREN_SSA = (r"^ss-?a", r"\bro\b", r"ro52", r"ro60", r"sjogren.*a\b")


def score_sjogren_2016(
    rows: Sequence[LabResult],
    phenotype: PhenotypeLookup | None = None,
    regimen: Regimen | None = None,
) -> CriteriaResult:
    """2016 ACR/EULAR classification criteria for primary Sjögren's syndrome.

    Five weighted items, threshold 4.

    **Anti-SSB/La is deliberately absent.** It scored in the older AECG
    criteria and is NOT an item in the 2016 set — its inclusion was dropped
    because isolated anti-La adds little specificity. Adding it back because
    the lab reports it would inflate every score by a point against a
    threshold of four.

    Three of the five items are ophthalmic and salivary measurements — ocular
    staining score, Schirmer's, unstimulated salivary flow — that no
    general-purpose record contains. They report `not_assessed`, which is what
    makes this scorer's headroom the useful output: it says exactly which
    three tests would settle the question.
    """
    view = LabView(rows)
    items: list[CriterionItem] = []

    entry_met: bool | None = None
    entry_note = "Entry requires dryness symptoms; nothing on file records them."
    if phenotype is not None:
        dry = [t for t in ("HP:0001097", "HP:0000217") if t in phenotype and phenotype[t][1]]
        if dry:
            entry_met = True
            entry_note = "Dryness recorded (" + ", ".join(phenotype[t][0] for t in dry) + ")."

    items.append(
        _any_positive_item(
            view,
            domain="Serology",
            name="Anti-SSA/Ro positive",
            weight=3,
            names=_SJOGREN_SSA,
            regimen=regimen,
        )
    )
    items.append(
        CriterionItem(
            domain="Histopathology",
            name="Labial gland focal lymphocytic sialadenitis, focus score ≥1",
            weight=3,
            state="not_assessed",
            basis="Requires a labial salivary gland biopsy report.",
        )
    )
    for name in (
        "Ocular staining score ≥5 (or van Bijsterveld ≥4)",
        "Schirmer's test ≤5 mm/5 min",
        "Unstimulated whole saliva flow ≤0.1 mL/min",
    ):
        items.append(
            CriterionItem(
                domain="Ocular/salivary tests",
                name=name,
                weight=1,
                state="not_assessed",
                basis="Requires a measurement no general record contains.",
            )
        )

    return _finalize(
        key="sjogren-2016",
        name="Sjögren's — 2016 ACR/EULAR classification criteria",
        citation="Shiboski CH, et al. Ann Rheum Dis. 2017;76(1):9-16.",
        items=items,
        threshold=4,
        entry_met=entry_met,
        entry_note=entry_note,
        requires_clinical_item=False,
        clinical_domains=frozenset({"Ocular/salivary tests", "Histopathology"}),
    )


# --- RA 2010 ACR/EULAR -------------------------------------------------------------------


_RA_ACPA = (r"ccp", r"citrullinat")
# `r"rheumatoid factor"` — a literal space — never matched anything: names
# are matched against `_normalize_slug`'s output, which strips ALL
# non-alphanumerics INCLUDING spaces, so the target string never contains
# one. "Rheumatoid Factor" normalizes to "rheumatoidfactor". This criterion
# could never be satisfied from lab data at all, silently, since RA 2010
# shipped - caught with zero test coverage on this specific match. `^rf$`
# added too: real lab panels report the bare abbreviation as often as the
# full name.
_RA_RF = (r"rheumatoidfactor", r"^rf$")
_RA_ACUTE = (r"^crp$", r"hs-?crp", r"^esr$", r"sedimentation")


def score_ra_2010(
    rows: Sequence[LabResult],
    phenotype: PhenotypeLookup | None = None,
    regimen: Regimen | None = None,
) -> CriteriaResult:
    """2010 ACR/EULAR classification criteria for rheumatoid arthritis.

    Threshold 6 of 10, over four domains: joint involvement (0–5), serology
    (0–3), acute-phase reactants (0–1), symptom duration (0–1).

    **This set cannot reach its threshold from stored data, by construction.**
    Joint involvement is worth 5 of the 10 points and requires a counted joint
    examination; symptom duration requires a history. Serology and acute-phase
    reactants together cap at 4.

    That is not a reason to omit the scorer — it is the scorer's most useful
    output. It says precisely what a clinician would have to supply for the
    question to be answerable at all, which is exactly the kind of item the
    next-appointment list exists to carry.

    High-positive means >3× the upper limit of normal in the published
    criteria. Stored rows carry a high/low flag rather than a multiple, so a
    flagged-positive result scores the LOW-positive 2 points rather than 3 —
    understating rather than overstating.
    """
    view = LabView(rows)
    items: list[CriterionItem] = []

    items.append(
        _clinical_item_for(
            "Joint involvement",
            domain="Joints",
            weight=5,
            term_ids=("HP:0001369",),
            phenotype=phenotype,
            missing_basis="Requires a counted joint examination.",
        )
    )

    serology = _any_positive_item(
        view,
        domain="Serology",
        name="RF or ACPA positive",
        weight=2,
        names=_RA_ACPA + _RA_RF,
        regimen=regimen,
    )
    if serology.state == "met":
        serology.basis += (
            " Scored as LOW-positive (2): the criteria award 3 only above 3× the "
            "upper limit of normal, which a high/low flag cannot establish."
        )
    items.append(serology)

    items.append(
        _threshold_item_any(
            view,
            domain="Acute-phase reactants",
            name="Abnormal CRP or ESR",
            weight=1,
            names=_RA_ACUTE,
            regimen=regimen,
        )
    )
    items.append(
        CriterionItem(
            domain="Duration",
            name="Symptoms ≥6 weeks",
            weight=1,
            state="not_assessed",
            basis="Requires a symptom-duration history.",
        )
    )

    return _finalize(
        key="ra-2010",
        name="Rheumatoid arthritis — 2010 ACR/EULAR classification criteria",
        citation="Aletaha D, et al. Arthritis Rheum. 2010;62(9):2569-2581.",
        items=items,
        threshold=6,
        entry_met=None,
        entry_note=(
            "Target population is a patient with at least one clinically swollen "
            "joint not better explained by another disease — a clinical judgement."
        ),
        requires_clinical_item=False,
        clinical_domains=frozenset({"Joints", "Duration"}),
    )


# --- ANCA-associated vasculitis (GPA) 2022 ACR/EULAR --------------------------------------


_ANCA_PR3 = (r"proteinase", r"\bpr3\b", r"c-?anca")
_ANCA_MPO = (r"myeloperoxidase", r"\bmpo\b", r"p-?anca")
_ANCA_EOS = (r"eosinophil",)


def score_gpa_2022(
    rows: Sequence[LabResult],
    phenotype: PhenotypeLookup | None = None,
    regimen: Regimen | None = None,
) -> CriteriaResult:
    """2022 ACR/EULAR classification criteria for granulomatosis with
    polyangiitis. Threshold 5.

    The only set here with NEGATIVELY weighted items: a positive MPO/pANCA
    scores −1 and a raised eosinophil count −4, because both point at a
    different vasculitis. `_finalize`'s domain-maximum rule would discard a
    negative item, so the negatives are totalled separately and always applied.
    """
    view = LabView(rows)
    items: list[CriterionItem] = []

    items.append(
        _any_positive_item(
            view,
            domain="Serology",
            name="PR3-ANCA or c-ANCA positive",
            weight=5,
            names=_ANCA_PR3,
            regimen=regimen,
        )
    )
    items.append(
        _clinical_item_for(
            "Nasal or sinus inflammation",
            domain="ENT",
            weight=1,
            term_ids=("HP:0000246", "HP:0001742"),
            phenotype=phenotype,
            missing_basis="No nasal or sinus finding on file.",
        )
    )
    items.append(
        _clinical_item_for(
            "Conductive or sensorineural hearing loss",
            domain="ENT-hearing",
            weight=1,
            term_ids=("HP:0000365",),
            phenotype=phenotype,
            missing_basis="No hearing finding on file.",
        )
    )
    items.append(
        _clinical_item_for(
            "Pauci-immune glomerulonephritis",
            domain="Renal",
            weight=1,
            term_ids=("HP:0000099",),
            phenotype=phenotype,
            missing_basis="Requires a renal biopsy or urinary findings.",
        )
    )
    for name, weight, basis in (
        ("Pulmonary nodules, mass or cavitation", 2, "Requires chest imaging."),
        ("Granuloma on biopsy", 2, "Requires a biopsy report."),
        ("Cartilaginous involvement", 2, "Requires an examination finding."),
    ):
        items.append(
            CriterionItem(
                domain="Imaging/biopsy",
                name=name,
                weight=weight,
                state="not_assessed",
                basis=basis,
            )
        )

    result = _finalize(
        key="gpa-2022",
        name="Granulomatosis with polyangiitis — 2022 ACR/EULAR criteria",
        citation="Robson JC, et al. Ann Rheum Dis. 2022;81(3):315-320.",
        items=items,
        threshold=5,
        entry_met=None,
        entry_note=(
            "Applies only once a diagnosis of small- or medium-vessel vasculitis "
            "is established and mimics are excluded — a clinical judgement."
        ),
        requires_clinical_item=False,
        clinical_domains=frozenset(),
    )

    # Negative items, applied outside the domain-maximum rule.
    penalty = 0
    mpo = _any_positive_item(
        view,
        domain="Serology-negative",
        name="MPO-ANCA or p-ANCA positive",
        weight=-1,
        names=_ANCA_MPO,
        regimen=regimen,
    )
    if mpo.state == "met":
        penalty -= 1
    result.items.append(mpo)
    eos = _count_threshold_item(
        view,
        domain="Serology-negative",
        name="Eosinophil count ≥1×10⁹/L",
        weight=-4,
        names=_ANCA_EOS,
        # 1×10⁹/L is 1000 cells/µL. Expressed in the unit her rows actually
        # use, so the conversion is visible rather than implied.
        threshold=1000.0,
        unit="cells/ul",
        regimen=regimen,
    )
    if eos.state == "met":
        penalty -= 4
    result.items.append(eos)

    result.points += penalty
    result.meets_threshold = result.points >= result.threshold
    return result


def _count_threshold_item(
    view: LabView,
    *,
    domain: str,
    name: str,
    weight: int,
    names: tuple[str, ...],
    threshold: float,
    unit: str,
    lookback: Lookback = "ever",
    regimen: Regimen | None = None,
) -> CriterionItem:
    """A numeric criterion whose threshold carries a UNIT.

    Every earlier threshold in this module compared a stored number against a
    bare constant, which is safe only while every row happens to share one
    unit. Eosinophils do not: this patient has them as `4.5 %` and
    `320 cells/uL`, and a naive `value >= 1.0` against the criteria's
    1×10⁹/L matched BOTH — scoring a -4 penalty for a real count of
    0.32×10⁹/L.

    That is the third time unit-blindness has produced a wrong clinical
    conclusion in this system (a trajectory once read `eosinophils rising
    319,900%` across a unit change). Rows whose unit cannot be converted to
    the criterion's unit are EXCLUDED rather than compared — a percentage is
    not a concentration, and treating it as one is how the bug happened.
    """
    found = view.history(*names) if lookback == "ever" else view.find_all(*names)
    comparable: list[tuple[LabResult, float]] = []
    for row in found:
        if row.value is None or not row.ucum_unit:
            continue
        converted = convert_value(row.value, row.ucum_unit, unit)
        if converted is None:
            continue
        comparable.append((row, converted))

    if not comparable:
        return CriterionItem(
            domain=domain,
            name=name,
            weight=weight,
            state="not_assessed",
            basis=(
                f"No result on file in a unit comparable to {unit}." if found else "None on file."
            ),
        )

    latest_row, latest_value = max(comparable, key=lambda pair: pair[0].date)
    # The published EGPA criterion is a blood eosinophil count ≥1×10⁹/L, which
    # in practice means the highest recorded count — a patient on steroids has
    # a normal one today and had 4.2 before treatment (ADR 0042).
    meeting = [pair for pair in comparable if pair[1] >= threshold]
    if not meeting:
        return CriterionItem(
            domain=domain,
            name=name,
            weight=weight,
            state="not_met",
            basis=(
                f"Most recent {latest_row.name} is {latest_row.value} "
                f"{latest_row.ucum_unit} = {latest_value:g} {unit} on "
                f"{latest_row.date.isoformat()}."
                + (
                    f" {len(comparable)} comparable draws on file, none reaching "
                    f"{threshold:g} {unit}."
                    if len(comparable) > 1
                    else ""
                )
            ),
            sources=[LabView.ref(latest_row)],
        )

    row, value = max(meeting, key=lambda pair: pair[1])
    if row is latest_row:
        return CriterionItem(
            domain=domain,
            name=name,
            weight=weight,
            state="met",
            basis=(
                f"Most recent {row.name} is {row.value} {row.ucum_unit} "
                f"= {value:g} {unit} on {row.date.isoformat()}."
            ),
            sources=[LabView.ref(row)],
        )
    return CriterionItem(
        domain=domain,
        name=name,
        weight=weight,
        state="met",
        basis=(
            f"Peak {row.name} was {row.value} {row.ucum_unit} = {value:g} {unit} on "
            f"{row.date.isoformat()}; not the most recent draw."
        ),
        sources=[LabView.ref(row), LabView.ref(latest_row)],
        met_ever=True,
        superseded=_superseded_note(met_row=row, latest=latest_row, regimen=regimen),
    )


def _clinical_item_for(
    name: str,
    *,
    domain: str,
    weight: int,
    term_ids: tuple[str, ...],
    phenotype: PhenotypeLookup | None,
    missing_basis: str,
) -> CriterionItem:
    """A clinical criterion answered from the phenotype record, `possible`
    only — the attribution rule in `ItemState` applies to every set, not just
    SLE."""
    if phenotype is None:
        return CriterionItem(
            domain=domain, name=name, weight=weight, state="not_assessed", basis=missing_basis
        )
    for term_id in term_ids:
        found = phenotype.get(term_id)
        if found is None:
            continue
        label, present, context = found
        if not present:
            return CriterionItem(
                domain=domain,
                name=name,
                weight=weight,
                state="not_met",
                basis=f"Recorded as excluded ({label}).",
                sources=[f"phenotype:{term_id}"],
            )
        excerpt = f' from "{context}"' if context else ""
        return CriterionItem(
            domain=domain,
            name=name,
            weight=weight,
            state="possible",
            basis=(
                f"{label} appears in the record{excerpt}. NOT counted: attribution "
                "to this condition is a clinician's judgement."
            ),
            sources=[f"phenotype:{term_id}"],
        )
    return CriterionItem(
        domain=domain, name=name, weight=weight, state="not_assessed", basis=missing_basis
    )


def _threshold_item_any(
    view: LabView,
    *,
    domain: str,
    name: str,
    weight: int,
    names: tuple[str, ...],
    lookback: Lookback = "ever",
    regimen: Regimen | None = None,
) -> CriterionItem:
    """Met when ANY matching analyte is flagged abnormal, at any point."""
    found = view.history(*names) if lookback == "ever" else view.find_all(*names)
    if not found:
        return CriterionItem(
            domain=domain, name=name, weight=weight, state="not_assessed", basis="None on file."
        )
    abnormal = [row for row in found if _numeric_above_ref(row)]
    if abnormal:
        latest = found[0]
        met_ever = latest not in abnormal
        return CriterionItem(
            domain=domain,
            name=name,
            weight=weight,
            state="met",
            basis="; ".join(f"{r.name} {r.value} ({r.date.isoformat()})" for r in abnormal[:4]),
            sources=[LabView.ref(r) for r in abnormal],
            met_ever=met_ever,
            superseded=(
                _superseded_note(met_row=abnormal[0], latest=latest, regimen=regimen)
                if met_ever
                else ""
            ),
        )
    return CriterionItem(
        domain=domain,
        name=name,
        weight=weight,
        state="not_met",
        basis=f"{len(found)} measured, none flagged abnormal.",
        sources=[LabView.ref(r) for r in found],
    )


# --- EGPA / MPA 2022 ACR/EULAR (GPA's two siblings) --------------------------------------
#
# The 2022 ACR/EULAR vasculitis criteria come as a set of three, and encoding
# only GPA left the other two arms of the same decision unmodelled. They reuse
# GPA's analyte mappings exactly — the same ANCA and eosinophil rows already
# proven to resolve against this patient's data — so the marginal cost is the
# item table, not new plumbing.
#
# Both carry negatively weighted items, like GPA: a finding that points at one
# sibling counts AGAINST the others. `_finalize`'s domain-maximum rule would
# discard a negative item, so they are totalled separately.


def _negative_penalty(result: CriteriaResult, items: list[CriterionItem]) -> None:
    """Apply negatively weighted items to an already-finalized result.

    Only `met` items count, exactly as for positive ones. A negative clinical
    item read from the text-matched phenotype can be `possible` at most (the
    attribution rule in `ItemState` applies to negatives too), so it will not
    subtract — which is the conservative direction: it declines to talk a
    score DOWN on an unattributed finding, just as it declines to talk one up.
    """
    penalty = sum(item.weight for item in items if item.state == "met")
    result.items.extend(items)
    result.points += penalty
    result.meets_threshold = result.points >= result.threshold
    if result.entry_met is False:
        result.meets_threshold = False


def score_egpa_2022(
    rows: Sequence[LabResult],
    phenotype: PhenotypeLookup | None = None,
    regimen: Regimen | None = None,
) -> CriteriaResult:
    """2022 ACR/EULAR criteria for eosinophilic granulomatosis with
    polyangiitis (Churg-Strauss). Threshold 6.

    The eosinophil count is the heaviest single item here (+5) and the same
    row scores −4 under GPA. That opposition is the point of encoding all
    three: one CBC differential moves the three sets in different directions,
    which is far more informative than any of them alone.
    """
    view = LabView(rows)
    items: list[CriterionItem] = [
        _clinical_item_for(
            "Obstructive airway disease",
            domain="Airway",
            weight=3,
            term_ids=("HP:0006536",),
            phenotype=phenotype,
            missing_basis="No airway-obstruction finding on file.",
        ),
        _clinical_item_for(
            "Nasal polyps",
            domain="ENT",
            weight=3,
            term_ids=("HP:0100582",),
            phenotype=phenotype,
            missing_basis="No nasal polyp finding on file.",
        ),
        _clinical_item_for(
            "Mononeuritis multiplex",
            domain="Neuro",
            weight=1,
            term_ids=("HP:0032018", "HP:0009831"),
            phenotype=phenotype,
            missing_basis="No multiple-mononeuropathy finding on file.",
        ),
        _count_threshold_item(
            view,
            domain="Eosinophils",
            name="Eosinophil count ≥1×10⁹/L",
            weight=5,
            names=_ANCA_EOS,
            threshold=1000.0,
            unit="cells/ul",
            regimen=regimen,
        ),
        CriterionItem(
            domain="Biopsy",
            name="Extravascular eosinophil-predominant inflammation",
            weight=2,
            state="not_assessed",
            basis="Requires a biopsy report.",
        ),
    ]

    result = _finalize(
        key="egpa-2022",
        name="Eosinophilic granulomatosis with polyangiitis — 2022 ACR/EULAR criteria",
        citation="Grayson PC, et al. Ann Rheum Dis. 2022;81(3):309-314.",
        items=items,
        threshold=6,
        entry_met=None,
        entry_note=(
            "Applies only once a diagnosis of small- or medium-vessel vasculitis "
            "is established and mimics are excluded — a clinical judgement."
        ),
        requires_clinical_item=False,
        clinical_domains=frozenset(),
    )
    _negative_penalty(
        result,
        [
            _any_positive_item(
                view,
                domain="Serology-negative",
                name="PR3-ANCA or c-ANCA positive",
                weight=-3,
                names=_ANCA_PR3,
                regimen=regimen,
            ),
            _clinical_item_for(
                "Haematuria",
                domain="Renal-negative",
                weight=-1,
                term_ids=("HP:0000790",),
                phenotype=phenotype,
                missing_basis="No haematuria finding on file.",
            ),
        ],
    )
    return result


def score_mpa_2022(
    rows: Sequence[LabResult],
    phenotype: PhenotypeLookup | None = None,
    regimen: Regimen | None = None,
) -> CriteriaResult:
    """2022 ACR/EULAR criteria for microscopic polyangiitis. Threshold 5.

    MPO-ANCA alone (+6) clears the threshold on its own, which is faithful to
    the published set: in an established small-vessel vasculitis, that one
    serology is close to decisive between these three.
    """
    view = LabView(rows)
    items: list[CriterionItem] = [
        _any_positive_item(
            view,
            domain="Serology",
            name="MPO-ANCA or p-ANCA positive",
            weight=6,
            names=_ANCA_MPO,
            regimen=regimen,
        ),
        _clinical_item_for(
            "Pulmonary fibrosis or interstitial lung disease",
            domain="Chest",
            weight=3,
            term_ids=("HP:0002206", "HP:0006515"),
            phenotype=phenotype,
            missing_basis="Requires chest imaging.",
        ),
    ]

    result = _finalize(
        key="mpa-2022",
        name="Microscopic polyangiitis — 2022 ACR/EULAR criteria",
        citation="Suppiah R, et al. Ann Rheum Dis. 2022;81(3):321-326.",
        items=items,
        threshold=5,
        entry_met=None,
        entry_note=(
            "Applies only once a diagnosis of small- or medium-vessel vasculitis "
            "is established and mimics are excluded — a clinical judgement."
        ),
        requires_clinical_item=False,
        clinical_domains=frozenset(),
    )
    _negative_penalty(
        result,
        [
            _clinical_item_for(
                "Nasal involvement (bloody discharge, ulcers, crusting, congestion)",
                domain="ENT-negative",
                weight=-3,
                term_ids=("HP:0000246", "HP:0001742"),
                phenotype=phenotype,
                missing_basis="No nasal or sinus finding on file.",
            ),
            _any_positive_item(
                view,
                domain="Serology-negative",
                name="PR3-ANCA or c-ANCA positive",
                weight=-1,
                names=_ANCA_PR3,
                regimen=regimen,
            ),
            _count_threshold_item(
                view,
                domain="Eosinophils-negative",
                name="Eosinophil count ≥1×10⁹/L",
                weight=-4,
                names=_ANCA_EOS,
                threshold=1000.0,
                unit="cells/ul",
                regimen=regimen,
            ),
        ],
    )
    return result


# --- Behçet ICBD 2014 --------------------------------------------------------------------


def score_behcet_icbd_2014(
    rows: Sequence[LabResult],
    phenotype: PhenotypeLookup | None = None,
    regimen: Regimen | None = None,
) -> CriteriaResult:
    """International Criteria for Behçet's Disease (2014). Threshold 4.

    The first set here that reads NO labs at all — there is no serological
    marker for Behçet, and the diagnosis is made on the pattern of clinical
    findings. It is included precisely because of that: every other scorer is
    gated on analytes, so a condition diagnosed clinically would otherwise be
    invisible to this layer no matter how well the record described it.

    In practice this means the score reports `points_possible` rather than
    `points`, since a text-matched phenotype can only ever reach `possible`.
    That is the honest output: it says "the record contains findings matching
    N points of this set" and leaves attribution to a clinician, which for a
    set with no confirmatory test is exactly where it belongs.
    """
    del rows  # no laboratory item exists in this set
    items: list[CriterionItem] = [
        _clinical_item_for(
            "Ocular lesions (uveitis or retinal vasculitis)",
            domain="Eye",
            weight=2,
            term_ids=("HP:0000554", "HP:0012122", "HP:0025188"),
            phenotype=phenotype,
            missing_basis="No uveitis or retinal-vasculitis finding on file.",
        ),
        _clinical_item_for(
            "Genital aphthosis",
            domain="Genital",
            weight=2,
            term_ids=("HP:0003249",),
            phenotype=phenotype,
            missing_basis="No genital-ulcer finding on file.",
        ),
        _clinical_item_for(
            "Oral aphthosis",
            domain="Oral",
            weight=2,
            term_ids=("HP:0000155", "HP:0032154", "HP:0011107"),
            phenotype=phenotype,
            missing_basis="No oral-ulcer finding on file.",
        ),
        _clinical_item_for(
            "Skin lesions (erythema nodosum or cutaneous vasculitis)",
            domain="Skin",
            weight=1,
            term_ids=("HP:0012219", "HP:0200029"),
            phenotype=phenotype,
            missing_basis="No erythema nodosum or skin vasculitis on file.",
        ),
        _clinical_item_for(
            "Neurological manifestations",
            domain="Neuro",
            weight=1,
            term_ids=("HP:0005305", "HP:0033724"),
            phenotype=phenotype,
            missing_basis="No central-nervous or dural-sinus finding on file.",
        ),
        _clinical_item_for(
            "Vascular manifestations",
            domain="Vascular",
            weight=1,
            term_ids=("HP:0004936", "HP:0002625"),
            phenotype=phenotype,
            missing_basis="No venous-thrombosis finding on file.",
        ),
        _clinical_item_for(
            "Positive pathergy test",
            domain="Pathergy",
            weight=1,
            term_ids=("HP:0025532",),
            phenotype=phenotype,
            missing_basis="Requires a pathergy skin test, which is rarely performed here.",
        ),
    ]

    return _finalize(
        key="behcet-icbd-2014",
        name="Behçet's disease — International Criteria (ICBD) 2014",
        citation=(
            "International Team for the Revision of the International Criteria for "
            "Behçet's Disease. J Eur Acad Dermatol Venereol. 2014;28(3):338-347."
        ),
        items=items,
        threshold=4,
        entry_met=None,
        entry_note="",
        requires_clinical_item=False,
        clinical_domains=frozenset(),
    )


SCORERS: dict[str, Callable[..., CriteriaResult]] = {
    "sle-2019": score_sle_2019,
    "sjogren-2016": score_sjogren_2016,
    "ra-2010": score_ra_2010,
    "gpa-2022": score_gpa_2022,
    "egpa-2022": score_egpa_2022,
    "mpa-2022": score_mpa_2022,
    "behcet-icbd-2014": score_behcet_icbd_2014,
}
"""Registry, keyed by criteria-set slug. PLAN.md Phase 3 calls for ~10 of
these (Sjögren 2016, SLICC, CASPAR, myositis, ANCA vasculitides…); the
framework above — three states, domain maxima, cited items, floor totals —
is shared by all of them."""


def score_all(
    rows: Sequence[LabResult],
    keys: Iterable[str] | None = None,
    phenotype: PhenotypeLookup | None = None,
    regimen: Regimen | None = None,
) -> list[CriteriaResult]:
    """Every registered scorer (or the named subset) against `rows`.

    `regimen` is optional and only ever ADDS context (ADR 0042): a criterion
    met historically whose latest draw is normal says which
    immunosuppressant was in force when that draw was taken. Passing nothing
    costs a sentence of explanation, never a scored point, so a caller with
    no regimen on file is degraded rather than wrong.
    """
    selected = list(SCORERS) if keys is None else [k for k in keys if k in SCORERS]
    return [SCORERS[key](rows, phenotype, regimen) for key in selected]
