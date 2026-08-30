"""Compare a LIRICAL ranking against the differential ledger.

LIRICAL is deliberately NOT folded into a combined score. Its composite
likelihood ratio is the only genuine LR in this system; the criteria scorers
produce points against a threshold, the panel produces uncalibrated buckets,
and averaging those together is the unit-blindness that has already produced
three wrong clinical conclusions here (see
`docs/research/scoring-across-engines.md`). The engines also all read the same
case file, so their errors are correlated and multiplying their scores would
overstate confidence exactly where they are most likely to be wrong together.

What LIRICAL is good for is DISAGREEMENT. Two questions are worth a
clinician's attention:

  - What does LIRICAL rank highly that the ledger does not hold at all?
  - What does the ledger hold that LIRICAL scores at or below zero?

Both are adjudication targets. Neither is a verdict.

Matching is by normalised name, which is the weak point and is stated rather
than hidden: LIRICAL emits OMIM/ORPHA curies and a ledger hypothesis carries
an optional MONDO id, and nothing here yet maps between those vocabularies.
Mondo xrefs would make this exact, and that is part of the Monarch work; until
then a rename on either side reads as a divergence. `matched_by` records which
route produced each pairing so a reader can tell a real disagreement from a
vocabulary miss.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

from adoc.casefile.schema import Hypothesis, Ledger
from adoc.knowledge.lirical import LiricalDisease, LiricalResult

# How far down LIRICAL's ranking counts as "ranked highly". LIRICAL returns a
# long tail; past the top handful its own composite LR is at or below zero on
# this patient's profile, and treating rank 30 as a finding would manufacture
# divergences out of noise.
ENGINE_TOP_N = 10

# A composite LR at or below zero is evidence AGAINST, not weak evidence for.
# LIRICAL reports log10 likelihood ratios, so zero is "this disease explains
# the phenotype no better than chance".
SUPPORTIVE_LR_FLOOR = 0.0

MatchRoute = Literal["name", "none"]
LiricalDivergenceKind = Literal["engine_only", "ledger_only", "agreement"]

_NORMALISE_RE = re.compile(r"[^a-z0-9]+")

# Possessives are stripped BEFORE tokenising, so "Sjögren's" does not leave a
# stray "s" token. Dropping every one-character token instead would collapse
# "Hepatitis B" into "Hepatitis A".
_POSSESSIVE_RE = re.compile(r"['\u2019]s\b")

# Words that carry no discriminating power in a disease name and only make two
# names for the same thing look different.
#
# Kept deliberately SHORT. A false divergence is visible and a reviewer can
# dismiss it; a false agreement silently merges two different diseases, so the
# error is asymmetric and the list errs toward keeping words. Three that were
# in the first draft and had to come out:
#
#   "a"         — collapses "Hepatitis A" into "Hepatitis"
#   "primary"   \
#   "secondary" /  primary and secondary adrenal insufficiency are not the
#                  same disease, and neither are the biliary cholangitides
#
# "type" is safe because the number beside it survives tokenising.
_STOPWORDS = frozenset(
    {
        "disease",
        "disorder",
        "syndrome",
        "type",
        "of",
        "with",
        "and",
        "the",
    }
)


def normalise_disease_name(name: str) -> str:
    """A comparison key for a disease name.

    Deliberately crude and deliberately visible. It lowercases, strips
    punctuation and drops words that carry no discriminating power, so
    "Sjogren syndrome" and "Sjögren's syndrome, primary" collapse together. It
    does NOT do synonym resolution — that needs an ontology, not a regex.
    """
    folded = name.lower()
    folded = folded.replace("ö", "o").replace("é", "e").replace("ü", "u")
    folded = _POSSESSIVE_RE.sub("", folded)
    tokens = [t for t in _NORMALISE_RE.split(folded) if t and t not in _STOPWORDS]
    return " ".join(sorted(tokens))


class LiricalFinding(BaseModel):
    """One engine-versus-ledger comparison, in both scales, never merged."""

    kind: LiricalDivergenceKind
    disease_name: str = ""
    curie: str = ""
    rank: int | None = None
    composite_lr: float | None = None
    posttest_probability: float | None = None

    ledger_hypothesis_id: str | None = None
    ledger_hypothesis_name: str = ""
    ledger_probability: str = ""
    ledger_tier: str = ""

    matched_by: MatchRoute = "none"
    note: str = ""


class LiricalComparison(BaseModel):
    """The engine's contribution to one review.

    `ran=False` with an `error` is an ordinary outcome, not an exception: the
    sidecar may be unreachable and a review must complete regardless.
    """

    ran: bool = False
    error: str = ""
    terms_used: list[str] = Field(default_factory=list)
    terms_excluded: list[str] = Field(default_factory=list)
    findings: list[LiricalFinding] = Field(default_factory=list)

    def of_kind(self, kind: LiricalDivergenceKind) -> list[LiricalFinding]:
        return [f for f in self.findings if f.kind == kind]

    @property
    def divergence_count(self) -> int:
        return len([f for f in self.findings if f.kind != "agreement"])


def compare_to_ledger(
    result: LiricalResult,
    ledger: Ledger,
    *,
    terms_used: list[str] | None = None,
    terms_excluded: list[str] | None = None,
    top_n: int = ENGINE_TOP_N,
    lr_floor: float = SUPPORTIVE_LR_FLOOR,
) -> LiricalComparison:
    """Where the engine and the ledger disagree.

    Three outcomes per item:

    - `engine_only` — LIRICAL ranks it in the top `top_n` with a supportive
      LR, and the ledger holds no matching hypothesis. A candidate the human
      differential missed.
    - `ledger_only` — an active hypothesis LIRICAL either never ranked or
      scored at or below `lr_floor`. Not a refutation: LIRICAL only knows
      phenotype, so a hypothesis resting on serology or imaging can be right
      and still score nothing. Worth an explicit note either way.
    - `agreement` — both hold it. Recorded rather than dropped, because
      agreement between independent methods is the strongest signal available
      and reporting only disagreement throws it away.
    """
    active = [h for h in ledger.hypotheses if h.status == "active"]
    by_key: dict[str, Hypothesis] = {}
    for hypothesis in active:
        by_key.setdefault(normalise_disease_name(hypothesis.name), hypothesis)

    findings: list[LiricalFinding] = []
    matched_ids: set[str] = set()

    ranked: list[LiricalDisease] = sorted(result.diseases, key=lambda d: d.rank)[:top_n]
    for disease in ranked:
        key = normalise_disease_name(disease.name)
        matched = by_key.get(key)
        supportive = disease.composite_lr > lr_floor

        if matched is not None:
            matched_ids.add(matched.id)
            findings.append(
                LiricalFinding(
                    kind="agreement",
                    disease_name=disease.name,
                    curie=disease.curie,
                    rank=disease.rank,
                    composite_lr=disease.composite_lr,
                    posttest_probability=disease.posttest_probability,
                    ledger_hypothesis_id=matched.id,
                    ledger_hypothesis_name=matched.name,
                    ledger_probability=matched.probability,
                    ledger_tier=matched.tier,
                    matched_by="name",
                    note="Both the phenotype engine and the differential hold this.",
                )
            )
            continue

        if not supportive:
            # Unranked-and-unsupported is the ordinary state of most of a
            # long tail. Reporting it would bury the real findings.
            continue

        findings.append(
            LiricalFinding(
                kind="engine_only",
                disease_name=disease.name,
                curie=disease.curie,
                rank=disease.rank,
                composite_lr=disease.composite_lr,
                posttest_probability=disease.posttest_probability,
                matched_by="none",
                note=(
                    "Ranked by the phenotype engine; no active hypothesis matches it. "
                    "Matching is by name, so a vocabulary mismatch can look like this."
                ),
            )
        )

    engine_by_key = {normalise_disease_name(d.name): d for d in result.diseases}
    for hypothesis in active:
        if hypothesis.id in matched_ids:
            continue
        scored = engine_by_key.get(normalise_disease_name(hypothesis.name))
        lr = scored.composite_lr if scored is not None else None
        findings.append(
            LiricalFinding(
                kind="ledger_only",
                disease_name=hypothesis.name,
                rank=scored.rank if scored is not None else None,
                composite_lr=lr,
                ledger_hypothesis_id=hypothesis.id,
                ledger_hypothesis_name=hypothesis.name,
                ledger_probability=hypothesis.probability,
                ledger_tier=hypothesis.tier,
                matched_by="name" if scored is not None else "none",
                note=(
                    "The phenotype engine does not support this. LIRICAL sees only "
                    "phenotype, so a hypothesis resting on serology, imaging or "
                    "treatment response can be correct and still score nothing here."
                ),
            )
        )

    return LiricalComparison(
        ran=True,
        terms_used=list(terms_used or []),
        terms_excluded=list(terms_excluded or []),
        findings=findings,
    )


def render_comparison(comparison: LiricalComparison) -> list[str]:
    """The report section. Both scales shown, never merged into one number."""
    if not comparison.ran:
        return [f"_The phenotype engine did not run this week: {comparison.error}._", ""]

    lines: list[str] = [
        "An independent phenotype-only engine (LIRICAL) ranked candidate diseases "
        "from the current findings alone — no labs, no ledger, no model. It is shown "
        "beside the differential rather than blended into it: its likelihood ratio "
        "and the differential's probability are different measurements and averaging "
        "them would mean nothing.",
        "",
    ]
    if comparison.terms_used:
        lines.append(f"_Ran on {len(comparison.terms_used)} current findings._")
        lines.append("")

    agreements = comparison.of_kind("agreement")
    if agreements:
        lines.append("**Both agree on**")
        for f in agreements:
            lines.append(
                f"- {f.disease_name} — engine rank {f.rank}, likelihood ratio "
                f"{f.composite_lr:+.2f}; differential holds it as {f.ledger_probability}"
            )
        lines.append("")

    engine_only = comparison.of_kind("engine_only")
    if engine_only:
        lines.append("**The engine raises, the differential does not hold**")
        for f in engine_only:
            lines.append(
                f"- {f.disease_name} (`{f.curie}`) — rank {f.rank}, "
                f"likelihood ratio {f.composite_lr:+.2f}"
            )
        lines.append("")

    ledger_only = comparison.of_kind("ledger_only")
    if ledger_only:
        lines.append("**The differential holds, the engine does not support**")
        lines.append("")
        lines.append(
            "_Not a refutation. This engine sees only the phenotype, so anything "
            "resting on blood work or imaging can be right and still score nothing._"
        )
        for f in ledger_only:
            lr = f"{f.composite_lr:+.2f}" if f.composite_lr is not None else "not ranked"
            lines.append(f"- {f.ledger_hypothesis_name} — {lr}")
        lines.append("")

    return lines
