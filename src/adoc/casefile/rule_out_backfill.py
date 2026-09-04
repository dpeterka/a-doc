"""Give the leads already on the board a way to end (ADR 0047).

ADR 0035 required every new hypothesis to state what would rule it out, and
`rule_out.strip_ops_missing_rule_out` enforces it — on the diagnostic chat
path only. The review path never applied it, and 43 of the ledger's 46
active hypotheses were created there. Measured in production on 2026-09-02:

    rule_out_check populated   0 / 54
    rule_out prose populated   0 / 54
    ever retired               0

The retirement pass has been evaluating a field nothing writes. ADR 0047
closes the writer for NEW leads; this closes it for the ones already there,
which would otherwise sit unfalsifiable forever.

## One model call per batch, and the result is proposed, not applied

The rule-outs are written by the challenger role, in batches, and land as an
ordinary `LedgerDiff` through `apply_and_save` — so the ledger invariants
check them like any other write and the change is visible in the history.

Nothing is invented for a lead the model will not commit on: an entry it
declines, or answers with one of `rule_out`'s empty phrases, is left alone
and counted. A wrong rule-out is worse than none, because a wrong one can
retire a live lead.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from ruamel.yaml import YAML

from adoc import __version__
from adoc.casefile.rule_out import is_usable_rule_out
from adoc.casefile.schema import (
    Hypothesis,
    Ledger,
    LedgerDiff,
    Provenance,
    RuleOutCheck,
    UpdateHypothesis,
)
from adoc.reason.client import LlmClient, Message

logger = logging.getLogger(__name__)

BATCH_SIZE = 8
"""Leads per model call. Small enough that one bad response costs little,
large enough that 46 leads do not become 46 calls."""

_SYSTEM = (
    "You are helping a single-patient diagnostic case file converge.\n\n"
    "Its differential holds leads that were added without stating what would "
    "take them off the board, so none of them can ever be ruled out and the "
    "list only grows. For each lead below, give the single result that would "
    "end it.\n\n"
    "Name a result someone could actually get back — 'a normal serum "
    "metanephrines', 'a negative anti-dsDNA', 'a temporal-bone CT showing no "
    "bone erosion'. Do NOT write 'further testing', 'more information', "
    "'clinical correlation' or anything else that names the wish for a "
    "result rather than a result: a requirement any hypothesis can satisfy "
    "is not a requirement.\n\n"
    "If you cannot honestly name one for a lead, return an empty string for "
    "it. That is a real answer and it is better than a wrong one — a wrong "
    "rule-out retires a live lead.\n\n"
    "A SINGLE NORMAL RESULT IS NOT A RULE-OUT for three kinds of lead, and "
    "these are where a plausible-sounding rule-out does the most damage:\n"
    "- **Episodic conditions**, where the abnormality appears only during an "
    "attack. A normal baseline tryptase does not exclude mast-cell "
    "activation syndrome; most patients have one.\n"
    "- **Historical or cumulative processes**, where the damage is already "
    "done. A normal estradiol today does not undo bone loss that happened "
    "under years of deficiency, and does not exclude it as its cause.\n"
    "- **Conditions whose published criteria require REPEAT testing**, or a "
    "specifically timed draw. Primary ovarian insufficiency wants an "
    "elevated FSH on two occasions at least four weeks apart; a single "
    "normal FSH does not settle it, and FSH swings widely in "
    "perimenopause.\n\n"
    "For any of those, either name the result that WOULD settle it — the "
    "acute-episode measurement, the imaging that shows the damage, the "
    "repeat draw — or return an empty string. Do not substitute the "
    "convenient single value.\n\n"
    "SECOND, and separately: when — and only when — that result is one of "
    "the lab analytes listed below, also give the machine-checkable form: "
    "`analyte` copied EXACTLY from the list, and `operator` as one of "
    "`negative` (a qualitative result reading negative), `normal` (within "
    "the lab's own reference range), `below` or `above` (with a numeric "
    "`threshold` and its `unit`).\n\n"
    "Leave `analyte` empty when the rule-out is imaging, a biopsy, an "
    "examination finding, or anything else not in that list. Do not "
    "approximate a name to make it fit: an analyte nobody has measured "
    "cannot be evaluated, and an invented one is silently useless.\n\n"
    "THE CHECK MUST TEST EXACTLY WHAT YOUR PROSE SAYS, and the check can "
    "only read the most recent stored value for one analyte. It cannot know "
    "how a specimen was provoked, when it was drawn relative to anything "
    "else, whether it was a repeat, or what tube it came in. So if your "
    "rule-out depends on a STIMULATED value, a SAME-DAY pairing, a REPEAT "
    "draw, a specific TUBE, a FASTING or MORNING sample, or a measurement "
    "DURING AN EPISODE — leave `analyte` empty and keep the condition in "
    "the prose alone. A check looser than its own prose retires a lead on "
    "evidence that rule-out does not accept: a baseline cortisol is not a "
    "cosyntropin-stimulated cortisol."
)


class RuleOutProposal(BaseModel):
    """One lead's proposed falsification condition, in both halves.

    Prose alone does not retire anything. `retirement._rule_out_met` returns
    immediately unless `rule_out_check` is set — it never reads the prose —
    so a backfill that wrote only `rule_out` would satisfy ADR 0035 and
    still retire nothing. Both halves or the exercise is decorative.
    """

    id: str
    rule_out: str = ""
    analyte: str = ""
    """The stored lab name this turns on, or empty when the rule-out is not
    a lab at all (imaging, biopsy, an examination finding). Validated
    against the analytes actually on file: an analyte nobody has measured
    makes the check unevaluable, and `evaluate_rule_out` treats
    cannot-tell as not-met, so an invented name is silently inert."""
    operator: Literal["negative", "normal", "below", "above", ""] = ""
    threshold: float | None = None
    unit: str = ""


class RuleOutProposals(BaseModel):
    proposals: list[RuleOutProposal] = Field(default_factory=list)


class BackfillReport(BaseModel):
    """What the backfill did, and what it declined to do."""

    considered: int = 0
    proposed: int = 0
    unusable: int = 0
    """Returned, but vacuous — `further testing` and friends."""
    declined: int = 0
    """The model returned nothing for this lead, deliberately."""
    checkable: int = 0
    """Of the proposed, how many also carry a `rule_out_check`. This is the
    number that decides whether anything can ever retire: prose alone
    satisfies ADR 0035 and `retirement._rule_out_met` never reads it."""
    unknown_analytes: list[str] = Field(default_factory=list)
    """Analytes named that are not on file — an unevaluable check, recorded
    rather than written."""
    inexpressible: list[str] = Field(default_factory=list)
    """Rule-outs whose prose asks for something a `RuleOutCheck` cannot hold
    — a stimulated draw, a same-day pairing, a repeat, a tube type. The
    prose is kept; the check is refused, because a check looser than its own
    prose retires a lead on the wrong evidence."""
    unknown_ids: list[str] = Field(default_factory=list)
    applied: int = 0


PROPOSALS_RELPATH = "case/proposed-rule-outs.yaml"
"""Where `--propose-to` writes by default. Inside the data repo so the
review is git-tracked next to the diff it produces."""

_FILE_HEADER = """\
# Proposed rule-outs, awaiting review. NOTHING HERE IS ON THE LEDGER YET.
#
# Delete any entry you do not accept, then apply exactly what is left:
#
#     adoc rule-out-backfill --apply-from {relpath}
#
# `--apply-from` makes NO model call. What this file says is what gets
# written, byte for byte — which is the point: the proposals are not
# reproducible. Four runs over the same ledger gave 46/18, 44/16, 43/18
# and 40/13 proposals, declining different leads each time. Approving from
# one run and re-proposing at apply time would write something you never
# read.
#
# `retires_on_next_review: true` means the check is ALREADY MET: applying
# it ends that lead the next time a review runs. Those are the entries
# worth a second look — a wrong one ends a live lead.
"""


class ReviewableProposal(BaseModel):
    """One proposal, with everything a human needs to accept or delete it."""

    id: str
    name: str
    """Carried for review only — `apply_from` matches on `id`."""
    rule_out: str
    check: RuleOutCheck | None = None
    retires_on_next_review: bool = False
    """The check is already met against the labs on file. Advisory: the
    retirement itself happens in the review's `retirement_pass`, evaluated
    fresh at that time, not here."""
    evaluates_to: str = ""
    """Why, in the evaluator's own words."""


class ProposalFile(BaseModel):
    """A frozen set of proposals. Reviewed by deleting entries."""

    generated: date
    app_version: str = ""
    model_id: str = ""
    proposals: list[ReviewableProposal] = Field(default_factory=list)


def write_proposals(path: Path, proposals: ProposalFile) -> None:
    """Write the file with its instructions at the top.

    Comments rather than a separate README: the person reading this is
    reading it because they are about to change a differential, and the
    instructions belong where their eyes are."""
    path.parent.mkdir(parents=True, exist_ok=True)
    yaml = YAML()
    yaml.default_flow_style = False
    with path.open("w", encoding="utf-8") as fh:
        fh.write(_FILE_HEADER.format(relpath=PROPOSALS_RELPATH))
        yaml.dump(proposals.model_dump(mode="json"), fh)


def load_proposals(path: Path) -> ProposalFile:
    """Load a reviewed proposal file.

    Raises rather than returning empty on a malformed file: an
    `--apply-from` that silently applied nothing would look exactly like a
    successful run, and the operator would believe a review had landed.
    """
    if not path.is_file():
        raise FileNotFoundError(f"no proposal file at {path}")
    yaml = YAML(typ="safe")
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.load(fh)
    if not raw:
        raise ValueError(f"proposal file at {path} is empty")
    return ProposalFile.model_validate(raw)


def proposals_to_ops(
    proposals: ProposalFile, ledger: Ledger
) -> tuple[list[UpdateHypothesis], list[str]]:
    """`(ops, skipped)` for a reviewed file. No model call, no invention.

    An entry naming a hypothesis the ledger no longer holds is SKIPPED and
    reported — never created. The file is a review of leads that existed
    when it was written, and a retirement or a rebuild in between must not
    resurrect one.
    """
    existing = {h.id for h in ledger.hypotheses}
    ops: list[UpdateHypothesis] = []
    skipped: list[str] = []
    for proposal in proposals.proposals:
        if proposal.id not in existing:
            skipped.append(proposal.id)
            continue
        if not is_usable_rule_out(proposal.rule_out):
            # A hand-edited file can carry anything. The same bar the
            # model's output has to clear.
            skipped.append(f"{proposal.id} (rule-out not usable)")
            continue
        ops.append(
            UpdateHypothesis(
                id=proposal.id, rule_out=proposal.rule_out.strip(), rule_out_check=proposal.check
            )
        )
    return ops, skipped


def needs_rule_out(ledger: Ledger) -> list[Hypothesis]:
    """Active leads with nothing that could end them."""
    return [
        h
        for h in ledger.hypotheses
        if h.status in {"active", "monitoring"}
        and not (h.rule_out or "").strip()
        and h.rule_out_check is None
    ]


def _render_analytes(analytes: Sequence[str]) -> str:
    """The analytes actually on file, so a proposed check can be evaluated.

    Without this the model names textbook analytes and `evaluate_rule_out`
    answers "no X result on file" forever — not-met, safe, and inert."""
    if not analytes:
        return "## Lab analytes on file\n\n(none — leave `analyte` empty for every lead)\n"
    return "## Lab analytes on file (copy exactly)\n\n" + "\n".join(
        f"- {name}" for name in analytes
    )


def _render(batch: Sequence[Hypothesis]) -> str:
    lines: list[str] = []
    for h in batch:
        lines.append(f"### {h.id}")
        lines.append(f"Name: {h.name}")
        lines.append(f"Tier: {h.tier}   Probability: {h.probability}")
        if h.evidence_for:
            lines.append("Evidence for:")
            lines += [f"  - {e.claim}" for e in h.evidence_for[:4]]
        if h.evidence_against:
            lines.append("Evidence against:")
            lines += [f"  - {e.claim}" for e in h.evidence_against[:3]]
        lines.append("")
    return "\n".join(lines)


# Qualifiers a `RuleOutCheck` CANNOT express. Its grammar is one analyte,
# one of four operators, and an optional threshold — it reads the most
# recent stored row and nothing else. It cannot know how a specimen was
# provoked, when it was drawn relative to something else, whether it was a
# repeat, or what tube it came in.
#
# Measured on the real case file, 2026-09-04. The prose was clinically
# sophisticated and the check was a loose approximation of it, and the
# check is what fires:
#
#   prose  "250-ug cosyntropin STIMULATION test shows a STIMULATED cortisol
#           >=18 at 30-60 minutes"
#   check  Cortisol above 18        <- any cortisol, including a baseline
#   stored 18.8 ug/dL               <- a baseline draw
#
# That would have retired a can't-miss adrenal-insufficiency lead on
# evidence its own stated rule-out does not accept. Same shape twice more:
# a biotin check ignoring "on the same day as the questioned assays", and a
# platelet check ignoring "repeated in a sodium-citrate tube".
#
# So when the prose asks for something the grammar cannot hold, the prose is
# kept and the check is refused. A rule-out a machine cannot evaluate is a
# rule-out for a human to evaluate — which is honest. A check that tests
# something LOOSER than its own prose is a lead retired on the wrong
# evidence.
_INEXPRESSIBLE_PATTERNS: tuple[str, ...] = (
    # provocation / dynamic testing
    r"stimulat",
    r"cosyntropin",
    r"\bacth\b.{0,20}\btest",
    r"provoc",
    r"challenge test",
    r"suppression test",
    r"tolerance test",
    r"post[- ]dose",
    # temporal pairing with another event
    r"same[- ]day",
    r"same time as",
    r"concurrent",
    r"simultaneous",
    r"paired with",
    r"while (still )?(on|taking)",
    r"before and after",
    # repeat / confirmatory
    r"\brepeat",
    r"two occasions",
    r"on two\b",
    r"second (draw|sample|measurement)",
    r"confirmatory",
    r"weeks apart",
    r"twice",
    # specimen handling
    r"citrate",
    r"heparin tube",
    r"different tube",
    r"\bedta\b",
    # timed draw
    r"fasting",
    r"\bmorning\b",
    r"\b8 ?a\.?m",
    r"diurnal",
    # episodic
    r"during (an?|the) (attack|episode|flare|crisis)",
    r"acute episode",
    r"symptomatic episode",
)

_INEXPRESSIBLE_RE = re.compile("|".join(_INEXPRESSIBLE_PATTERNS), re.IGNORECASE)


def check_is_expressible(rule_out: str) -> str | None:
    """The first qualifier in `rule_out` a `RuleOutCheck` cannot hold, or
    `None` when the prose is within the grammar.

    Deterministic and deliberately conservative: a false positive costs a
    machine-checkable rule-out that a human keeps in prose, and a false
    negative retires a lead on evidence its own rule-out does not accept.
    """
    match = _INEXPRESSIBLE_RE.search(rule_out)
    return match.group(0) if match else None


def _checkable(
    proposal: RuleOutProposal, known: dict[str, str], *, inexpressible: list[str] | None = None
) -> RuleOutCheck | None:
    """The machine-checkable half, or `None` if it cannot be made evaluable.

    Refuses rather than approximates. `evaluate_rule_out` treats an analyte
    with no result on file as not-met, so a check naming an invented analyte
    is indistinguishable from a working one and will never fire — exactly
    the silent-absence shape this repository keeps hitting.
    """
    analyte = proposal.analyte.strip()
    if not analyte or not proposal.operator:
        return None
    qualifier = check_is_expressible(proposal.rule_out)
    if qualifier is not None:
        # The prose asks for something the grammar cannot hold. Keep the
        # prose; refuse the check.
        if inexpressible is not None:
            inexpressible.append(f"{proposal.id} ({qualifier!r})")
        return None
    stored = known.get(analyte.lower())
    if stored is None:
        return None
    if proposal.operator in {"below", "above"} and proposal.threshold is None:
        # `RuleOutCheck`'s own validator requires it; refusing here keeps the
        # failure a counted outcome rather than an exception mid-batch.
        return None
    return RuleOutCheck(
        analyte=stored,
        operator=proposal.operator,
        threshold=proposal.threshold,
        unit=proposal.unit.strip(),
    )


def propose_rule_outs(
    client: LlmClient,
    ledger: Ledger,
    *,
    batch_size: int = BATCH_SIZE,
    analytes: Iterable[str] = (),
) -> tuple[list[UpdateHypothesis], BackfillReport]:
    """Ops setting `rule_out` on every lead that has none, plus a report.

    Never raises for a bad batch: one unusable response must not cost the
    other five batches, the same posture every other stage here takes.
    """
    targets = needs_rule_out(ledger)
    known_analytes = sorted({a.strip() for a in analytes if a.strip()})
    lowered = {a.lower(): a for a in known_analytes}
    report = BackfillReport(considered=len(targets))
    by_id = {h.id: h for h in targets}
    ops: list[UpdateHypothesis] = []
    seen: set[str] = set()

    for start in range(0, len(targets), batch_size):
        batch = targets[start : start + batch_size]
        try:
            result = client.complete(
                "challenger",
                system=_SYSTEM,
                messages=[
                    Message(
                        role="user",
                        content=f"{_render_analytes(known_analytes)}\n\n{_render(batch)}",
                    )
                ],
                schema=RuleOutProposals,
            )
            payload = result.parsed
        except Exception as exc:  # noqa: BLE001 - a bad batch must not stop the rest
            logger.warning("rule-out backfill: batch starting at %d failed: %s", start, exc)
            continue
        if not isinstance(payload, RuleOutProposals):
            logger.warning("rule-out backfill: batch starting at %d returned no proposals", start)
            continue

        for proposal in payload.proposals:
            if proposal.id not in by_id:
                report.unknown_ids.append(proposal.id)
                continue
            if proposal.id in seen:
                continue
            seen.add(proposal.id)
            text = proposal.rule_out.strip()
            if not text:
                report.declined += 1
                continue
            if not is_usable_rule_out(text):
                report.unusable += 1
                continue
            report.proposed += 1
            check = _checkable(proposal, lowered, inexpressible=report.inexpressible)
            if check is not None:
                report.checkable += 1
            elif proposal.analyte.strip():
                # Named an analyte that is not on file. Recorded rather than
                # accepted: `evaluate_rule_out` would answer "no result on
                # file" forever, which is not-met, safe, and inert — a check
                # that looks like a check and can never fire.
                report.unknown_analytes.append(proposal.analyte.strip())
            ops.append(UpdateHypothesis(id=proposal.id, rule_out=text, rule_out_check=check))

    return ops, report


def backfill_diff(ops: Sequence[UpdateHypothesis], *, model_id: str) -> LedgerDiff:
    """Wrap the ops in a diff so the ledger invariants see them."""
    return LedgerDiff(
        provenance=Provenance(
            app_version=__version__,
            prompt_template_version="rule_out_backfill@v1",
            model_id=model_id,
            dag_node="rule_out_backfill",
            timestamp=datetime.now(UTC),
        ),
        rationale=(
            f"ADR 0047: gave {len(ops)} lead(s) a stated way to end. Leads created before "
            "the review path enforced ADR 0035 had no falsification condition, so the "
            "retirement pass could never evaluate them."
        ),
        ops=list(ops),
    )
