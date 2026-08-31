"""ICAP ANA-pattern mapping (PLAN.md phase 3).

The International Consensus on ANA Patterns assigns a code (AC-1 … AC-29) to
each immunofluorescence pattern seen on HEp-2 cells, and each pattern narrows
the antibodies worth chasing. A "centromere" pattern points at CENP-A/B; a
"nucleolar" one points somewhere else entirely. Reading the pattern is how a
clinician turns one positive screen into a short list of confirmatory tests.

Two things this module is careful about.

**A pattern is an association, not a diagnosis**, and every rendering says so
— the same posture the classification scorers take. AC-3 is *associated with*
limited cutaneous systemic sclerosis and primary biliary cholangitis; it does
not mean the patient has either.

**A negative ANA has no pattern.** There is nothing to map, and this module
returns nothing rather than reaching for one. On the case file it was written
against, all seven ANA screens from 2017 to 2025 were negative — three of them
by IFA, the method that produces patterns — and no pattern text appears
anywhere in the document corpus. So this renders nothing today, deliberately,
and starts working the day a positive screen with a reported pattern arrives.
That is why it exists now: the mapping is a fixed reference table, and having
it in place costs nothing while waiting.

Nothing here calls a model. Pattern text comes from the lab report; the code
and its associations come from the table below.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, Field

from adoc.labs.models import LabResult

PATTERN_DISCLAIMER = (
    "An ANA pattern is an association, not a diagnosis: it narrows which "
    "antibodies are worth testing next. A pattern can appear in a healthy "
    "person, and a person can have any of these conditions without it."
)

# ICAP splits patterns by where the staining is and by whether every lab is
# expected to report them. "Competent" patterns are the ones a routine lab
# should name; "expert" ones need a reference laboratory, so seeing one
# reported at all is itself informative.
Compartment = Literal["nuclear", "cytoplasmic", "mitotic"]
Competence = Literal["competent", "expert"]

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


class IcapPattern(BaseModel):
    """One ICAP pattern and what it points at."""

    code: str
    """`AC-1` … `AC-29`."""
    name: str
    compartment: Compartment
    competence: Competence
    antibodies: list[str] = Field(default_factory=list)
    """What to test next. This is the actionable half."""
    associations: list[str] = Field(default_factory=list)
    """Conditions the pattern is associated with — never a diagnosis."""
    note: str = ""


# The reference table. Antibody and condition associations follow the ICAP
# consensus (anapatterns.org); they are a fixed reference, not a judgement
# this system makes, which is why they live in a table rather than a prompt.
ICAP_PATTERNS: tuple[IcapPattern, ...] = (
    # -- nuclear ----------------------------------------------------------
    IcapPattern(
        code="AC-1",
        name="Homogeneous",
        compartment="nuclear",
        competence="competent",
        antibodies=["dsDNA", "nucleosome", "histone"],
        associations=[
            "systemic lupus erythematosus",
            "drug-induced lupus",
            "juvenile idiopathic arthritis",
        ],
    ),
    IcapPattern(
        code="AC-2",
        name="Dense fine speckled",
        compartment="nuclear",
        competence="competent",
        antibodies=["DFS70/LEDGF"],
        associations=[],
        note=(
            "The informative negative. An isolated AC-2 with no other "
            "antibody is found in healthy people and argues AGAINST an "
            "ANA-associated rheumatic disease rather than for one."
        ),
    ),
    IcapPattern(
        code="AC-3",
        name="Centromere",
        compartment="nuclear",
        competence="competent",
        antibodies=["CENP-A/B"],
        associations=[
            "limited cutaneous systemic sclerosis",
            "primary biliary cholangitis",
        ],
    ),
    IcapPattern(
        code="AC-4",
        name="Fine speckled",
        compartment="nuclear",
        competence="competent",
        antibodies=["SS-A/Ro", "SS-B/La", "Mi-2", "TIF1-gamma", "Ku"],
        associations=[
            "Sjogren syndrome",
            "systemic lupus erythematosus",
            "dermatomyositis",
        ],
    ),
    IcapPattern(
        code="AC-5",
        name="Large/coarse speckled",
        compartment="nuclear",
        competence="competent",
        antibodies=["U1RNP", "Sm", "RNA polymerase III"],
        associations=[
            "mixed connective tissue disease",
            "systemic lupus erythematosus",
            "systemic sclerosis",
        ],
    ),
    IcapPattern(
        code="AC-6",
        name="Multiple nuclear dots",
        compartment="nuclear",
        competence="competent",
        antibodies=["Sp100", "PML", "MJ/NXP-2"],
        associations=["primary biliary cholangitis", "dermatomyositis"],
    ),
    IcapPattern(
        code="AC-7",
        name="Few nuclear dots",
        compartment="nuclear",
        competence="competent",
        antibodies=["p80-coilin", "SMN"],
        associations=["Sjogren syndrome", "systemic sclerosis", "polymyositis"],
    ),
    IcapPattern(
        code="AC-8",
        name="Homogeneous nucleolar",
        compartment="nuclear",
        competence="competent",
        antibodies=["PM/Scl", "Th/To", "B23/nucleophosmin"],
        associations=["systemic sclerosis", "systemic sclerosis/polymyositis overlap"],
    ),
    IcapPattern(
        code="AC-9",
        name="Clumpy nucleolar",
        compartment="nuclear",
        competence="competent",
        antibodies=["fibrillarin/U3RNP"],
        associations=["systemic sclerosis"],
    ),
    IcapPattern(
        code="AC-10",
        name="Punctate nucleolar",
        compartment="nuclear",
        competence="competent",
        antibodies=["RNA polymerase I", "NOR-90"],
        associations=["systemic sclerosis", "Sjogren syndrome"],
    ),
    IcapPattern(
        code="AC-11",
        name="Smooth nuclear envelope",
        compartment="nuclear",
        competence="expert",
        antibodies=["lamins A/B/C"],
        associations=["systemic lupus erythematosus", "autoimmune hepatitis"],
    ),
    IcapPattern(
        code="AC-12",
        name="Punctate nuclear envelope",
        compartment="nuclear",
        competence="expert",
        antibodies=["gp210", "nucleoporin p62"],
        associations=["primary biliary cholangitis"],
    ),
    IcapPattern(
        code="AC-13",
        name="PCNA-like",
        compartment="nuclear",
        competence="expert",
        antibodies=["PCNA"],
        associations=["systemic lupus erythematosus"],
    ),
    IcapPattern(
        code="AC-14",
        name="CENP-F-like",
        compartment="nuclear",
        competence="expert",
        antibodies=["CENP-F"],
        associations=["malignancy"],
        note="Reported association with malignancy; a reference-laboratory pattern.",
    ),
    IcapPattern(
        code="AC-29",
        name="Topoisomerase I-like",
        compartment="nuclear",
        competence="expert",
        antibodies=["Scl-70/topoisomerase I"],
        associations=["diffuse cutaneous systemic sclerosis"],
    ),
    # -- cytoplasmic ------------------------------------------------------
    IcapPattern(
        code="AC-15",
        name="Fibrillar linear",
        compartment="cytoplasmic",
        competence="competent",
        antibodies=["actin", "non-muscle myosin"],
        associations=["autoimmune hepatitis", "coeliac disease"],
    ),
    IcapPattern(
        code="AC-16",
        name="Fibrillar filamentous",
        compartment="cytoplasmic",
        competence="expert",
        antibodies=["cytokeratins", "vimentin"],
        associations=[],
    ),
    IcapPattern(
        code="AC-17",
        name="Fibrillar segmental",
        compartment="cytoplasmic",
        competence="expert",
        antibodies=["alpha-actinin", "vinculin"],
        associations=["myasthenia gravis", "inflammatory bowel disease"],
    ),
    IcapPattern(
        code="AC-18",
        name="Discrete dots / GW body-like",
        compartment="cytoplasmic",
        competence="expert",
        antibodies=["GW182", "Su/Ago2"],
        associations=["Sjogren syndrome", "primary biliary cholangitis"],
    ),
    IcapPattern(
        code="AC-19",
        name="Dense fine speckled (cytoplasmic)",
        compartment="cytoplasmic",
        competence="competent",
        antibodies=["PL-7", "PL-12", "ribosomal P"],
        associations=["antisynthetase syndrome", "systemic lupus erythematosus"],
    ),
    IcapPattern(
        code="AC-20",
        name="Fine speckled (cytoplasmic)",
        compartment="cytoplasmic",
        competence="competent",
        antibodies=["Jo-1/histidyl-tRNA synthetase"],
        associations=["antisynthetase syndrome", "polymyositis", "dermatomyositis"],
    ),
    IcapPattern(
        code="AC-21",
        name="Reticular / AMA-like",
        compartment="cytoplasmic",
        competence="competent",
        antibodies=["mitochondrial M2 (PDC-E2)"],
        associations=["primary biliary cholangitis", "systemic sclerosis"],
    ),
    IcapPattern(
        code="AC-22",
        name="Polar / Golgi-like",
        compartment="cytoplasmic",
        competence="expert",
        antibodies=["giantin", "golgin-95"],
        associations=["Sjogren syndrome", "systemic lupus erythematosus"],
    ),
    IcapPattern(
        code="AC-23",
        name="Rods and rings",
        compartment="cytoplasmic",
        competence="expert",
        antibodies=["IMPDH2"],
        associations=["hepatitis C treated with interferon/ribavirin"],
    ),
    # -- mitotic ----------------------------------------------------------
    IcapPattern(
        code="AC-24",
        name="Centrosome",
        compartment="mitotic",
        competence="expert",
        antibodies=["pericentrin", "ninein"],
        associations=["systemic sclerosis", "Raynaud phenomenon"],
    ),
    IcapPattern(
        code="AC-25",
        name="Spindle fibres",
        compartment="mitotic",
        competence="expert",
        antibodies=["HsEg5"],
        associations=[],
    ),
    IcapPattern(
        code="AC-26",
        name="NuMA-like",
        compartment="mitotic",
        competence="expert",
        antibodies=["NuMA"],
        associations=["Sjogren syndrome", "systemic lupus erythematosus"],
    ),
    IcapPattern(
        code="AC-27",
        name="Intercellular bridge",
        compartment="mitotic",
        competence="expert",
        antibodies=["aurora kinase B", "survivin"],
        associations=[],
    ),
    IcapPattern(
        code="AC-28",
        name="Mitotic chromosomal",
        compartment="mitotic",
        competence="expert",
        antibodies=["histone H3", "MCA-1"],
        associations=["discoid lupus", "chronic lymphocytic leukaemia"],
    ),
)

_BY_CODE = {p.code: p for p in ICAP_PATTERNS}

# How pattern text actually arrives on a report. Ordered longest-first at match
# time so "dense fine speckled" is not swallowed by "speckled", and
# "homogeneous nucleolar" is not read as plain "homogeneous" — the difference
# between AC-1 (lupus) and AC-8 (systemic sclerosis) is exactly that word.
_PATTERN_SYNONYMS: tuple[tuple[str, str], ...] = (
    ("dense fine speckled", "AC-2"),
    ("dfs70", "AC-2"),
    ("dfs 70", "AC-2"),
    ("homogeneous nucleolar", "AC-8"),
    ("clumpy nucleolar", "AC-9"),
    ("punctate nucleolar", "AC-10"),
    ("nucleolar", "AC-8"),
    ("centromere", "AC-3"),
    ("multiple nuclear dots", "AC-6"),
    ("few nuclear dots", "AC-7"),
    ("nuclear dots", "AC-6"),
    ("large speckled", "AC-5"),
    ("coarse speckled", "AC-5"),
    ("fine speckled", "AC-4"),
    ("speckled", "AC-4"),
    ("homogeneous", "AC-1"),
    ("smooth nuclear envelope", "AC-11"),
    ("punctate nuclear envelope", "AC-12"),
    ("nuclear envelope", "AC-11"),
    ("nuclear membrane", "AC-11"),
    ("pcna", "AC-13"),
    ("cenp-f", "AC-14"),
    ("topoisomerase", "AC-29"),
    ("scl-70 like", "AC-29"),
    ("reticular", "AC-21"),
    ("mitochondrial", "AC-21"),
    ("ama", "AC-21"),
    ("rods and rings", "AC-23"),
    ("golgi", "AC-22"),
    ("centrosome", "AC-24"),
    ("spindle", "AC-25"),
    ("numa", "AC-26"),
    ("midbody", "AC-27"),
    ("intercellular bridge", "AC-27"),
    ("cytoplasmic speckled", "AC-20"),
)


class PatternMatch(BaseModel):
    """One pattern found in a report, with what produced the match."""

    pattern: IcapPattern
    matched_text: str
    """The phrase in the source that produced this, so a reader can check it."""
    source_ref: str = ""


class IcapReport(BaseModel):
    """What the ANA patterns on file point at.

    An empty `matches` with `ana_negative=True` is the ordinary, expected
    outcome for a seronegative patient and is not a failure.
    """

    matches: list[PatternMatch] = Field(default_factory=list)
    ana_negative: bool = False
    ana_result_count: int = 0
    note: str = ""

    @property
    def antibodies_to_consider(self) -> list[str]:
        """Every antibody the observed patterns point at, deduplicated and
        stable-ordered. This is the actionable output — what to test next."""
        seen: list[str] = []
        for match in self.matches:
            for antibody in match.pattern.antibodies:
                if antibody not in seen:
                    seen.append(antibody)
        return seen


def pattern_for_code(code: str) -> IcapPattern | None:
    return _BY_CODE.get(code.upper().strip())


def match_patterns(text: str, *, source_ref: str = "") -> list[PatternMatch]:
    """Every ICAP pattern named in `text`.

    Matching is longest-phrase-first and each region of the text is consumed
    once, so "homogeneous nucleolar" yields AC-8 alone rather than AC-8 plus a
    spurious AC-1 from the word it contains. That distinction is not cosmetic:
    AC-1 points at lupus and AC-8 at systemic sclerosis.
    """
    if not text:
        return []
    haystack = _NON_ALNUM_RE.sub(" ", text.lower()).strip()
    haystack = re.sub(r"\s+", " ", haystack)

    matches: list[PatternMatch] = []
    consumed: list[tuple[int, int]] = []
    for phrase, code in sorted(_PATTERN_SYNONYMS, key=lambda p: -len(p[0])):
        needle = _NON_ALNUM_RE.sub(" ", phrase).strip()
        start = haystack.find(needle)
        while start != -1:
            end = start + len(needle)
            if not any(s < end and start < e for s, e in consumed):
                consumed.append((start, end))
                pattern = _BY_CODE[code]
                if not any(m.pattern.code == pattern.code for m in matches):
                    matches.append(
                        PatternMatch(pattern=pattern, matched_text=phrase, source_ref=source_ref)
                    )
                break
            start = haystack.find(needle, start + 1)
    return matches


_ANA_NAME_RE = re.compile(r"(^|\b)(ana|antinuclear)\b", re.I)
_NEGATIVE_RE = re.compile(r"\b(negative|non[- ]?reactive|not detected)\b", re.I)


def scan_ana_patterns(rows: Sequence[LabResult]) -> IcapReport:
    """Map every ANA pattern the lab rows report.

    A negative ANA is recorded as such and produces no matches. The pattern is
    a property of a POSITIVE immunofluorescence result; inventing one for a
    negative screen would be worse than saying nothing, and saying nothing
    without saying why would look like a bug.
    """
    ana_rows = [
        r
        for r in rows
        if _ANA_NAME_RE.search(r.name or "") or _ANA_NAME_RE.search(r.name_raw or "")
    ]
    if not ana_rows:
        return IcapReport(note="No ANA result on file.")

    ana_rows = sorted(ana_rows, key=lambda r: r.date)
    matches: list[PatternMatch] = []
    for row in ana_rows:
        for field_text in (row.value_text, row.name_raw, row.ref_text):
            if not field_text:
                continue
            for match in match_patterns(field_text, source_ref=f"labs:{row.name}:{row.date}"):
                if not any(m.pattern.code == match.pattern.code for m in matches):
                    matches.append(match)

    latest = ana_rows[-1]
    negative = bool(latest.value_text and _NEGATIVE_RE.search(latest.value_text))
    note = ""
    if negative and not matches:
        note = (
            f"The most recent ANA ({latest.date}) is negative, and a negative ANA "
            "has no pattern to interpret."
        )
    return IcapReport(
        matches=matches,
        ana_negative=negative,
        ana_result_count=len(ana_rows),
        note=note,
    )


def render_icap(report: IcapReport) -> list[str]:
    """The report section. Renders nothing at all when there is nothing to say
    — an empty heading costs tokens and tells a reader nothing."""
    if not report.matches:
        return []

    lines = ["_" + PATTERN_DISCLAIMER + "_", ""]
    for match in report.matches:
        pattern = match.pattern
        lines.append(f"**{pattern.code} — {pattern.name}** ({pattern.compartment})")
        if pattern.antibodies:
            lines.append(f"- Antibodies worth testing: {', '.join(pattern.antibodies)}")
        if pattern.associations:
            lines.append(f"- Associated with: {', '.join(pattern.associations)}")
        if pattern.note:
            lines.append(f"- {pattern.note}")
        if pattern.competence == "expert":
            lines.append(
                "- A reference-laboratory pattern; that it was reported at all is itself "
                "worth noting."
            )
        lines.append(f'- Reported as "{match.matched_text}"  `{match.source_ref}`')
        lines.append("")
    return lines
