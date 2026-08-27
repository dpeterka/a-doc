"""LIRICAL as a non-LLM differential engine (ADR 0029).

LIRICAL ranks candidate diseases by likelihood ratio against each disease's
known phenotype profile in `phenotype.hpoa`. It is the third mechanistically
independent check in the anti-anchoring design (PLAN.md), alongside the
cross-family Challenger and the ledger-blind panel: it has no memory of the
ledger, nothing to anchor on, and cannot be argued into a conclusion.

**This module never runs the ranking itself.** It builds the invocation and
parses the result. The upstream Java implementation is the authority on the
arithmetic — a reimplementation would have made this system responsible for
the correctness of medical likelihood ratios, and a subtly wrong LR does not
crash, it emits confidently wrong disease rankings.

Everything here is pure and offline, so the whole surface is unit-testable
against a recorded fixture with no JVM and no network.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, Field

Sex = Literal["MALE", "FEMALE", "UNKNOWN"]

HPO_TERM_RE = re.compile(r"^HP:\d{7}$")
"""An HPO term id. Validated before it reaches the command line: these terms
come from a phenotype-extraction step, and an unvalidated one would either be
silently ignored by LIRICAL or turn into a CLI parsing error blamed on the
sidecar."""


class LiricalRequest(BaseModel):
    """One phenotype-only prioritization."""

    observed: list[str] = Field(default_factory=list)
    negated: list[str] = Field(default_factory=list)
    """Explicitly EXCLUDED phenotypes. LIRICAL folds these into the
    likelihood ratio rather than ignoring them, which is a real advantage
    over a naive phenotype matcher: "ANA negative" is evidence, and a ranker
    that can only consume present findings throws it away."""
    sample_id: str = "subject"
    sex: Sex = "UNKNOWN"
    age: str | None = None

    def validated_terms(self) -> tuple[list[str], list[str]]:
        """`(observed, negated)` with malformed ids dropped.

        Dropping rather than raising follows ADR 0028's posture: one bad term
        should cost that term, not the whole engine run.
        """
        good_observed = [t for t in self.observed if HPO_TERM_RE.match(t.strip())]
        good_negated = [t for t in self.negated if HPO_TERM_RE.match(t.strip())]
        return good_observed, good_negated


class LiricalDisease(BaseModel):
    """One ranked candidate."""

    rank: int
    name: str
    curie: str
    """`OMIM:154700`, `ORPHA:558` — the disease's stable identifier."""
    pretest_probability: str
    """Kept as LIRICAL renders it (`1/8621`): it is a rendered fraction, and
    reformatting it as a float would imply a precision the string does not
    carry."""
    posttest_probability: float
    """Percent, 0-100, as LIRICAL reports it."""
    composite_lr: float


class LiricalResult(BaseModel):
    """A parsed LIRICAL run."""

    version: str = ""
    sample_id: str = ""
    observed: list[str] = Field(default_factory=list)
    negated: list[str] = Field(default_factory=list)
    diseases: list[LiricalDisease] = Field(default_factory=list)

    def top(self, count: int) -> list[LiricalDisease]:
        return self.diseases[:count]


def build_prioritize_args(
    request: LiricalRequest, *, data_dir: str, out_dir: str, prefix: str = "lirical"
) -> list[str]:
    """The `lirical prioritize` argv for a phenotype-only run.

    `--assembly` and `--vcf` are deliberately absent: LIRICAL's own help says
    "Leave unset to run in phenotype-only mode", and that is the only mode
    this system uses. The patient's genomic data is a genotyping array plus
    imputation with no per-variant quality metric, which cannot support the
    rare-variant reasoning LIRICAL's genotype mode assumes (ADR 0030).
    """
    observed, negated = request.validated_terms()
    if not observed:
        raise ValueError("lirical: at least one valid observed HPO term is required")

    args = [
        "prioritize",
        "-d",
        data_dir,
        "-p",
        ",".join(observed),
        "-o",
        out_dir,
        "-x",
        prefix,
        "-f",
        "tsv",
        "--sample-id",
        request.sample_id,
        "--sex",
        request.sex,
    ]
    if negated:
        args += ["-n", ",".join(negated)]
    if request.age:
        args += ["--age", request.age]
    return args


_VERSION_RE = re.compile(r"LIRICAL TSV Output \(([^)]+)\)")
_SAMPLE_RE = re.compile(r"^!\s*Sample:\s*(.+)$")
_TERM_RE = re.compile(r"(HP:\d{7})")
_PERCENT_RE = re.compile(r"^([0-9.]+)\s*%?$")


def parse_lirical_tsv(text: str) -> LiricalResult:
    """Parse LIRICAL's TSV output.

    The format is a `!`-prefixed metadata preamble (version, sample, the
    observed and excluded terms echoed back with HTML links to hpo.jax.org),
    then a header row and one row per ranked disease. The echoed terms are
    read back rather than assumed, so a result always carries the input that
    actually produced it.
    """
    result = LiricalResult()
    section: str | None = None
    header: list[str] | None = None

    for raw in text.splitlines():
        line = raw.rstrip("\n")
        if not line.strip():
            continue

        if line.startswith("!"):
            version = _VERSION_RE.search(line)
            if version:
                result.version = version.group(1)
                continue
            sample = _SAMPLE_RE.match(line)
            if sample:
                result.sample_id = sample.group(1).strip()
                continue
            lowered = line.lower()
            if "observed hpo" in lowered:
                section = "observed"
                continue
            if "excluded hpo" in lowered or "negated hpo" in lowered:
                section = "negated"
                continue
            term = _TERM_RE.search(line)
            if term and section == "observed":
                result.observed.append(term.group(1))
            elif term and section == "negated":
                result.negated.append(term.group(1))
            continue

        columns = line.split("\t")
        if header is None:
            header = [c.strip() for c in columns]
            continue

        disease = _row_to_disease(header, columns)
        if disease is not None:
            result.diseases.append(disease)

    return result


def _row_to_disease(header: Sequence[str], columns: Sequence[str]) -> LiricalDisease | None:
    """One TSV row, or `None` if it cannot be read as a ranked disease.

    Columns are looked up by NAME rather than position. LIRICAL's TSV gains
    columns between versions, and a positional parser would silently start
    reading the wrong field rather than failing.
    """
    row = dict(zip(header, columns, strict=False))
    try:
        rank = int(row["rank"])
        posttest = _percent(row["posttestprob"])
        composite = float(row["compositeLR"])
    except (KeyError, ValueError, TypeError):
        return None

    return LiricalDisease(
        rank=rank,
        name=row.get("diseaseName", "").strip(),
        curie=row.get("diseaseCurie", "").strip(),
        pretest_probability=row.get("pretestprob", "").strip(),
        posttest_probability=posttest,
        composite_lr=composite,
    )


def _percent(text: str) -> float:
    match = _PERCENT_RE.match(text.strip())
    if not match:
        raise ValueError(f"not a percentage: {text!r}")
    return float(match.group(1))
