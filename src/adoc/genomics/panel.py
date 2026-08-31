"""The genomic panel — confirmatory-test leads from the raw array (ADR 0030).

ADR 0030 decided which of 431MB of genomic data is admissible and on what
terms. This builds the artifact it specified.

**Only the raw array export is read.** The phased exports are excluded (the
vendor states they are not for medical use and they cover fewer relevant loci
than the file they derive from) and the imputed BCFs are excluded (no
per-variant quality score, so a confidently imputed common variant is
indistinguishable from a coin flip, on a mismatched genome build).

**The output is leads, not findings.** 23andMe individually validates only a
subset of the markers on the raw file. So each entry says "the array suggests
X; the clinical test that settles it is Y", which is input to the Test-Chooser
rather than a conclusion.

Three properties ADR 0030 required, all implemented here rather than asked for
in a prompt:

1. **A bounded curated panel, never a dump.** The blind panel's context budget
   is 31,232 tokens; a variant dump is useless to a model and is also the only
   genuinely re-identifying form this data takes.
2. **A fixed header stating that absence is not exclusion.** A model reading a
   missing pathogenic variant as an exclusion is the most dangerous misreading
   available here.
3. **Citable claims** — `genomic:<gene>:<rsid>` refs, checked by the same
   machinery as every other claim.

No new dependencies: this parses one tab-separated text file.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

GENOMIC_PANEL_RELPATH = "case/genomics-panel.md"

# The array's no-call encodings. 23andMe writes "--" for a failed call and
# sometimes "DD"/"II" for indels it will not resolve. Treating a no-call as a
# homozygous reference would silently invent a negative result, which is the
# error this whole artifact is built to avoid.
_NO_CALLS = frozenset({"--", "DD", "II", "-", ""})

Interpretation = Literal["risk_allele_present", "no_risk_allele", "no_call"]


class PanelMarker(BaseModel):
    """One curated marker: what it is, what it would mean, what settles it."""

    rsid: str
    gene: str
    condition: str
    risk_alleles: frozenset[str]
    """Alleles that carry the association. Compared per-allele rather than as
    a genotype string, because the array reports genotypes unordered and on
    whichever strand the chip used."""
    meaning: str
    confirmatory_test: str
    """The clinical test that actually settles it. ADR 0030's whole posture:
    the array produces a lead for the Test-Chooser, never a conclusion."""

    @property
    def source_ref(self) -> str:
        return f"genomic:{self.gene}:{self.rsid}"


# The panel. Deliberately short, and every entry has to justify its place:
# a marker the array genuinely measures, an association that is not disputed,
# and a real clinical test that resolves it. Anything failing one of those
# three is noise in a document a model will read beside a case file.
PANEL: tuple[PanelMarker, ...] = (
    PanelMarker(
        rsid="rs2187668",
        gene="HLA-DQA1",
        condition="Coeliac disease (HLA-DQ2.5)",
        risk_alleles=frozenset({"T"}),
        meaning=(
            "Tags the HLA-DQ2.5 haplotype. Coeliac disease is close to "
            "impossible without DQ2.5 or DQ8, so this pair of markers is "
            "unusually informative when BOTH are negative."
        ),
        confirmatory_test="HLA-DQ2/DQ8 typing, with tTG-IgA and total IgA",
    ),
    PanelMarker(
        rsid="rs7454108",
        gene="HLA-DQB1",
        condition="Coeliac disease (HLA-DQ8)",
        risk_alleles=frozenset({"C"}),
        meaning=("Tags the HLA-DQ8 haplotype, the second of the two coeliac permissive types."),
        confirmatory_test="HLA-DQ2/DQ8 typing, with tTG-IgA and total IgA",
    ),
    PanelMarker(
        rsid="rs4349859",
        gene="HLA-B",
        condition="HLA-B27-associated spondyloarthritis",
        risk_alleles=frozenset({"A"}),
        meaning=(
            "Tags HLA-B27. Relevant to ankylosing spondylitis, reactive "
            "arthritis and acute anterior uveitis. A tag SNP is not the "
            "typing test and disagrees with it in a minority of people."
        ),
        confirmatory_test="HLA-B27 typing by PCR or flow cytometry",
    ),
    PanelMarker(
        rsid="rs1800562",
        gene="HFE",
        condition="Hereditary haemochromatosis (C282Y)",
        risk_alleles=frozenset({"A"}),
        meaning=(
            "The C282Y variant. Homozygosity is the common cause of "
            "hereditary haemochromatosis; carrying one copy rarely causes "
            "iron overload on its own."
        ),
        confirmatory_test="Ferritin with transferrin saturation, then HFE genotyping",
    ),
    PanelMarker(
        rsid="rs1799945",
        gene="HFE",
        condition="Hereditary haemochromatosis (H63D)",
        risk_alleles=frozenset({"G"}),
        meaning=(
            "The H63D variant. Much weaker than C282Y and mainly of interest compound with it."
        ),
        confirmatory_test="Ferritin with transferrin saturation, then HFE genotyping",
    ),
)

# Loci a genotyping array cannot see at all, listed so that "we have genome
# data on file" does not read as though the question has been covered. ADR
# 0030 names FMR1 specifically: the ledger raised FXPOI, and a CGG repeat
# expansion is invisible to both an array and imputation.
UNREACHABLE_BY_ARRAY: tuple[tuple[str, str], ...] = (
    (
        "FMR1 premutation (FXPOI)",
        "A CGG repeat expansion. Invisible to a genotyping array and to "
        "imputation alike; it needs targeted PCR or Southern blot sizing.",
    ),
    (
        "Any rare or private variant",
        "An array measures a fixed set of common markers. It does not "
        "sequence, so a rare pathogenic variant is simply not looked for.",
    ),
    (
        "Copy-number and structural variants",
        "Not resolvable from this data.",
    ),
)

HEADER = """\
## What this is, and what it is not

This comes from a consumer genotyping array, not from sequencing. Three things
follow, and they matter more than any result below.

**Absence is not exclusion.** A marker reported as negative here rules nothing
out. The array measures a fixed set of common positions; it does not sequence,
so a variant it does not carry a probe for is simply not looked for.

**These are leads, not findings.** The manufacturer individually validates
only a subset of the markers on this file. Every entry below names the clinical
test that would actually settle the question. Nothing here is a diagnosis and
nothing here should be acted on before that test.

**A tag marker is not the typing test.** Where a marker tags an HLA type it
agrees with proper typing in most people and disagrees in some.
"""


class MarkerResult(BaseModel):
    """One marker, as read from this patient's array."""

    marker: PanelMarker
    genotype: str = ""
    interpretation: Interpretation = "no_call"

    @property
    def source_ref(self) -> str:
        return self.marker.source_ref


class PanelResult(BaseModel):
    results: list[MarkerResult] = Field(default_factory=list)
    markers_sought: int = 0
    markers_found: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


def find_raw_array(repo_root: Path) -> Path | None:
    """The raw array export under `sources/genomics/`, or `None`.

    ADR 0030 admits the raw export and excludes the phased ones, so a filename
    containing "phased" is skipped rather than ranked lower: the vendor states
    those are not for medical use, and they cover fewer of the relevant loci
    than the file they derive from.

    Largest remaining candidate wins. The archive stores files under a
    content-hash prefix, so name-based ordering says nothing useful, and the
    full export is the biggest text file in there.
    """
    directory = repo_root / "sources" / "genomics"
    if not directory.is_dir():
        return None
    candidates = [
        p for p in directory.glob("*.txt") if p.is_file() and "phased" not in p.name.lower()
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_size)


def parse_array(lines: Iterable[str], wanted: Iterable[str]) -> dict[str, str]:
    """`rsid -> genotype` for the wanted markers only.

    Streamed and filtered as it goes: the export is ~17MB and hundreds of
    thousands of rows, and nothing outside the curated panel is ever held in
    memory — which is also the point of a panel rather than a dump.
    """
    targets = set(wanted)
    found: dict[str, str] = {}
    for line in lines:
        if not line or line[0] == "#":
            continue
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 4:
            continue
        rsid = parts[0].strip()
        if rsid in targets:
            found[rsid] = parts[3].strip().upper()
            if len(found) == len(targets):
                break
    return found


def interpret(marker: PanelMarker, genotype: str) -> Interpretation:
    """Whether the risk allele is present, absent, or was not called.

    Per-allele rather than by genotype string: the array reports genotypes
    unordered and on whichever strand the chip used, so comparing "AG" to an
    expected "GA" would produce a false negative.
    """
    if genotype in _NO_CALLS:
        return "no_call"
    if any(allele in marker.risk_alleles for allele in genotype):
        return "risk_allele_present"
    return "no_risk_allele"


def build_panel(array_path: Path, *, panel: Iterable[PanelMarker] = PANEL) -> PanelResult:
    """Read the array and interpret the curated panel.

    Never raises: a missing or unreadable export yields an empty result with a
    reason, and the caller writes an artifact saying the panel could not be
    built rather than one that silently omits it.
    """
    markers = list(panel)
    try:
        with array_path.open("r", encoding="utf-8", errors="replace") as handle:
            genotypes = parse_array(handle, (m.rsid for m in markers))
    except OSError as exc:
        return PanelResult(markers_sought=len(markers), error=f"could not read the array: {exc}")

    results = [
        MarkerResult(
            marker=marker,
            genotype=genotypes.get(marker.rsid, ""),
            interpretation=interpret(marker, genotypes.get(marker.rsid, "")),
        )
        for marker in markers
    ]
    return PanelResult(
        results=results,
        markers_sought=len(markers),
        markers_found=len(genotypes),
    )


def render_panel(result: PanelResult) -> str:
    """The artifact. Header first, always, whether or not anything was read."""
    lines = [HEADER, ""]

    if not result.ok:
        lines.append(f"_The panel could not be built this run: {result.error}._")
        lines.append("")
    else:
        lines.append("## Curated panel")
        lines.append("")
        lines.append(
            f"_{result.markers_found} of {result.markers_sought} panel markers were "
            "present on the array._"
        )
        lines.append("")
        for item in result.results:
            marker = item.marker
            lines.append(f"**{marker.condition}** — `{marker.gene}` {marker.rsid}")
            if item.interpretation == "no_call":
                lines.append(
                    "- Not called on this array. That is not a negative result — the "
                    "marker was not measured."
                )
            else:
                verdict = (
                    "risk allele present"
                    if item.interpretation == "risk_allele_present"
                    else "risk allele not present"
                )
                lines.append(f"- Genotype {item.genotype} — {verdict}  `{item.source_ref}`")
            lines.append(f"- {marker.meaning}")
            lines.append(f"- Test that settles it: {marker.confirmatory_test}")
            lines.append("")

    lines.append("## What this data cannot answer")
    lines.append("")
    for name, why in UNREACHABLE_BY_ARRAY:
        lines.append(f"- **{name}** — {why}")
    lines.append("")
    return "\n".join(lines)
