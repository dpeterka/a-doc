"""Rare-disease differential-recall suite (PLAN.md phase 3 acceptance).

Asks one question of the phenotype engine: given a patient built from a known
disease's own annotations, does that disease come back near the top?

## Ground truth

Simulated patients from HPO disease annotations. This is how LIRICAL and
Exomiser are themselves benchmarked, and it is the only ground truth available
without a labelled patient cohort — which this project does not have and
cannot ethically assemble for one patient.

The construction is deliberately unkind to the engine:

**The query is a SUBSET of the disease's terms**, not all of them. Handing an
engine the complete annotation set asks whether it can look up an exact
key — it would score near 100% and measure nothing. Dropping terms simulates
a real presentation, where only some features have appeared or been recorded.

**Noise terms from an unrelated disease are added.** A real phenotype record
carries findings that belong to comorbidity or to nothing at all. An engine
that collapses under a few irrelevant terms is not usable on the 90-term
profile this patient actually has.

**Cases are chosen deterministically**, by sorted disease id from a fixed
stratum, so a run is reproducible and a regression is attributable to a code
change rather than to a lucky sample.

## What it does NOT do

No model is called. This measures the deterministic phenotype engine, and
mixing an LLM into it would make a failure impossible to attribute. That is
also why it is safe to run in CI when the index exists, and why it skips
cleanly when it does not.
"""

from __future__ import annotations

import logging
from pathlib import Path

from adoc.evals.runner import ClientFactory, SuiteCaseResult, SuiteMetric, SuiteResult
from adoc.knowledge.semsim import SemSimIndex, load_index

logger = logging.getLogger(__name__)

SUITE_NAME = "rare_disease_recall"

# How many simulated patients. Enough that a one-case regression moves the
# rate visibly, small enough that the suite runs in seconds.
CASE_COUNT = 40

# Of the disease's own terms, how many the simulated patient presents with.
# Four is the shape of a real referral: a handful of findings, not a complete
# textbook entry.
TERMS_PER_CASE = 4

# Irrelevant terms borrowed from an unrelated disease.
NOISE_TERMS_PER_CASE = 2

# A disease needs at least this many annotations to be usable: fewer and the
# subset IS the whole annotation set, which is the lookup test this suite is
# built to avoid.
MIN_ANNOTATIONS = 8

# Recall is measured at these ranks. Top-1 is the headline; top-10 is what a
# clinician would actually read, and a differential that contains the answer
# at rank 8 has done its job.
RECALL_AT = (1, 3, 10)

# The rate below which this suite FAILS rather than merely reporting.
#
# Set from the measured baseline, with margin. The first committed run over
# the 2026-06-23 release gave recall@1 0.225, recall@3 0.400 and recall@10
# 0.525 on 40 simulated patients. The gate is 0.40: comfortably below what the
# engine does today, so an ontology release that shifts a few cases does not
# cry wolf, and far enough above zero that a genuine collapse trips it.
#
# A first draft of this constant said 0.70, which was aspiration and would
# have shipped a suite that failed on the day it was written.
MIN_RECALL_AT_10 = 0.40


def _index_path() -> Path:
    """Where the phenotype index lives, without requiring a data repo.

    This suite is entirely synthetic — it needs the ontology and no patient
    data at all — but `Settings()` has no default for `data_dir` and raises
    when none is configured. Reading the path through a full `Settings()`
    therefore made the suite unrunnable in CI, and it reported the reason as
    "no phenotype-similarity index in this environment" when the truth was a
    missing data dir. Three tests that monkeypatched `load_index` passed
    locally and failed in CI for exactly that reason: the skip fired before
    the patched function was ever called.

    So: use the configured path when there is a configuration, and fall back
    to the field's own default when there is not. The default is a fixed
    absolute path that does not depend on `data_dir`.
    """
    from adoc.config import Settings

    try:
        return Settings().semsim_index_path
    except Exception:  # noqa: BLE001 - no data repo configured; the ontology is elsewhere
        default = Settings.model_fields["semsim_index_path"].default
        return Path(str(default))


def _usable_diseases(index: SemSimIndex) -> list[str]:
    """Diseases with enough annotations to simulate from, sorted for
    reproducibility."""
    return sorted(
        disease_id
        for disease_id, _ in index.iter_diseases()
        if len(index.disease_terms(disease_id)) >= MIN_ANNOTATIONS
    )


def build_cases(index: SemSimIndex, *, count: int = CASE_COUNT) -> list[tuple[str, list[str]]]:
    """`(disease_id, query_terms)` for each simulated patient.

    Terms are taken from the FRONT of the sorted annotation list and noise from
    a disease a fixed distance away in the same sorted order. Both choices are
    arbitrary but fixed: the point is that two runs of the same code produce
    the same cases, so a change in recall means a change in the engine.
    """
    usable = _usable_diseases(index)
    if not usable:
        return []

    cases: list[tuple[str, list[str]]] = []
    # Spread the picks across the whole sorted list rather than taking the
    # first N, which would sample one alphabetical neighbourhood of OMIM.
    stride = max(1, len(usable) // count)
    for position in range(0, len(usable), stride):
        if len(cases) >= count:
            break
        disease_id = usable[position]
        own = sorted(index.disease_terms(disease_id))[:TERMS_PER_CASE]

        # Noise from a disease well away from this one in the ordering, so it
        # is unlikely to be a close relative sharing most annotations.
        noise_source = usable[(position + len(usable) // 2) % len(usable)]
        noise = sorted(index.disease_terms(noise_source))[:NOISE_TERMS_PER_CASE]

        query = own + [t for t in noise if t not in own]
        if len(own) >= TERMS_PER_CASE:
            cases.append((disease_id, query))
    return cases


def run(*, client_factory: ClientFactory, candidate: str | None = None) -> SuiteResult:
    """Measure differential recall. No model is called.

    `client_factory` and `candidate` are accepted to satisfy the `Suite`
    protocol and are deliberately unused: this suite measures deterministic
    code, so a model binding cannot change its result. Reporting a binding
    label it did not use would be misleading, so it says so.
    """
    label = "deterministic (no model binding used)"
    try:
        index = load_index(_index_path())
    except Exception as exc:  # noqa: BLE001 - a bad artifact is a skip, not a crash
        logger.warning("%s: could not load the index: %s", SUITE_NAME, exc)
        index = None

    if index is None:
        # A skip, not a failure. The index is a build artifact; a local
        # checkout does not have one, and reporting that as a failing suite
        # would train everyone to ignore it.
        return SuiteResult(
            suite=SUITE_NAME,
            binding_label=label,
            metrics=[
                SuiteMetric(
                    name="skipped",
                    value=1.0,
                    detail="no phenotype-similarity index in this environment",
                )
            ],
        )

    cases = build_cases(index)
    hits_at: dict[int, int] = dict.fromkeys(RECALL_AT, 0)
    case_results: list[SuiteCaseResult] = []
    ranks: list[int] = []

    for disease_id, query in cases:
        ranked = index.rank(query, top_n=max(RECALL_AT))
        found = [d.disease_id for d in ranked.diseases]
        rank = found.index(disease_id) + 1 if disease_id in found else 0
        if rank:
            ranks.append(rank)
        for cutoff in RECALL_AT:
            if rank and rank <= cutoff:
                hits_at[cutoff] += 1

        name = index.disease_name(disease_id) or disease_id
        case_results.append(
            SuiteCaseResult(
                case_id=f"recall:{disease_id}",
                # A per-case FAILURE here means the engine errored, not that
                # it missed. Recall is a rate to compare between models, and
                # `adoc eval` ANDs every case into its overall verdict
                # (`cli.py`), so marking a miss as a failed case would make
                # this suite fail permanently at any recall below 100% —
                # useless for the gated model rotation it exists to serve.
                # The rate is gated once, below, against a measured threshold.
                passed=bool(ranked.ok),
                detail=(
                    f"{name[:48]} — rank {rank}"
                    if rank
                    else f"{name[:48]} — not in top {max(RECALL_AT)}"
                ),
            )
        )

    total = len(cases) or 1
    metrics = [
        SuiteMetric(
            name=f"recall_at_{cutoff}",
            value=hits_at[cutoff] / total,
            detail=f"{hits_at[cutoff]} of {len(cases)} simulated patients",
        )
        for cutoff in RECALL_AT
    ]
    metrics.append(
        SuiteMetric(
            name="median_rank_when_found",
            value=float(sorted(ranks)[len(ranks) // 2]) if ranks else 0.0,
            detail=f"over {len(ranks)} case(s) where the disease was returned at all",
        )
    )
    metrics.append(
        SuiteMetric(
            name="cases_total",
            value=float(len(cases)),
            detail=(
                f"{TERMS_PER_CASE} own terms + {NOISE_TERMS_PER_CASE} noise terms per patient, "
                f"from diseases with >= {MIN_ANNOTATIONS} annotations"
            ),
        )
    )

    # The one case that actually gates. Everything above it is diagnosis.
    recall_at_10 = hits_at[max(RECALL_AT)] / total
    case_results.append(
        SuiteCaseResult(
            case_id=f"recall_at_{max(RECALL_AT)}_above_threshold",
            passed=recall_at_10 >= MIN_RECALL_AT_10,
            detail=(
                f"recall@{max(RECALL_AT)} {recall_at_10:.3f} "
                f"against a floor of {MIN_RECALL_AT_10:.2f}"
            ),
        )
    )

    return SuiteResult(suite=SUITE_NAME, binding_label=label, cases=case_results, metrics=metrics)
