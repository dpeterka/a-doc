"""Enforcing that a new hypothesis states what would kill it (ADR 0035).

The prompts require a `rule_out` on every `add_hypothesis`. A prompt is a
request, not a guarantee, and ADR 0035 recorded the gap honestly: a model that
ignores the requirement produced an empty field and nothing rejected it.

This closes it in code, following the two rules this codebase already settled:

**ADR 0028 — no single field of one item may fail a payload.** A missing
`rule_out` on one hypothesis must never discard a whole Challenger verdict.
That is the exact defect fixed in v0.21.0 and it is not being reintroduced.

**ADR 0016 (revised) — strip, don't reject.** The established handling for
model output that cannot be repaired is to remove the offending item and keep
the rest, loudly. So an `add_hypothesis` still missing its `rule_out` after
the stage's retry is DROPPED rather than accepted or fatal.

Dropping a lead is a real cost and worth being clear about. It is accepted
because it is the lesser of three evils: accepting it silently is the status
quo this work exists to end; failing the payload loses everything; and a
hypothesis the model will not state a falsification condition for is exactly
the speculative addition ADR 0035 set out to stop. Nothing is permanent — the
next review can re-add it with a `rule_out`.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from adoc.casefile.retirement import is_protected
from adoc.casefile.schema import AddHypothesis, LedgerOp

# Phrases that look like a falsification condition and are not one. A rule-out
# has to name a result someone could actually get back; "further testing"
# names only the wish for one, and accepting it would make the requirement
# satisfiable by any hypothesis at all.
_EMPTY_PHRASES = (
    "further testing",
    "further evaluation",
    "further workup",
    "clinical correlation",
    "additional testing",
    "more data",
    "more information",
    "as above",
    "n/a",
    "none",
    "tbd",
    "unknown",
    "unclear",
)

# Short enough that it cannot be naming anything at all. Deliberately LOW,
# and the phrase list above does the real work.
#
# The errors here are asymmetric: a false negative DROPS a real hypothesis,
# while a false positive merely lets a weak rule-out through for a clinician
# to judge. So this errs permissive. A first draft used 15, which rejected
# "negative ANA" — twelve characters and a perfectly good falsification
# condition for lupus.
MIN_RULE_OUT_CHARS = 8

_WHITESPACE_RE = re.compile(r"\s+")


def is_usable_rule_out(text: str) -> bool:
    """Whether `text` names a result rather than gesturing at one.

    Deliberately crude. This cannot judge clinical validity and does not try —
    it catches the empty string, the placeholder and the hedge, which is what
    a model reaches for when it has nothing. Anything that survives is the
    reviewing clinician's to assess.
    """
    normalised = _WHITESPACE_RE.sub(" ", text or "").strip().lower().rstrip(".")
    if len(normalised) < MIN_RULE_OUT_CHARS:
        return False
    return not any(
        normalised == phrase or normalised.startswith(phrase) for phrase in _EMPTY_PHRASES
    )


def hypotheses_missing_rule_out(ops: Sequence[LedgerOp]) -> list[tuple[str, str]]:
    """`(hypothesis_id, name)` for every `add_hypothesis` that must supply a
    `rule_out` and has not.

    The same two exclusions as the retirement pass (`casefile.retirement`),
    and for the same reasons — a rule that can silently drop one of these is
    not a rule worth having:

    **Patient-origin is never subject to this.** Requiring the patient to
    supply a falsification condition for her own theory is absurd, and
    stripping it would stop it reaching the ledger to be quarantined at all.
    The red-team suite caught exactly that: `patient_theory_anchoring` failed
    `wired=True quarantined=False` because the theory was dropped before the
    quarantine could see it.

    **`cant-miss` is never subject to this.** The cost of dropping a
    dangerous-but-unlikely diagnosis is catastrophic and asymmetric, which is
    the whole point of the tier.
    """
    return [
        (op.hypothesis.id, op.hypothesis.name)
        for op in ops
        if isinstance(op, AddHypothesis)
        and not is_protected(op.hypothesis)
        and not is_usable_rule_out(op.hypothesis.rule_out)
    ]


def build_rule_out_retry_feedback(missing: Sequence[tuple[str, str]]) -> str:
    """Feedback for a same-generation retry, mirroring the citation retry.

    Names the offending hypotheses and says what a usable answer looks like,
    because "add a rule_out" without an example is what produced "further
    testing" in the first place.
    """
    lines = ["The following new hypotheses have no usable `rule_out`:"]
    lines += [f"- {name} (`{hid}`)" for hid, name in missing]
    lines.append(
        "A `rule_out` must name the specific finding that would END the hypothesis — "
        'a result you could actually get back, such as "a normal repeat FSH on a draw '
        'four or more weeks later" or "a negative cartilage biopsy from an affected '
        'site". "Further testing", "clinical correlation" and similar are not '
        "rule-outs and will be rejected. Return the same output with a real `rule_out` "
        "on each, or drop any hypothesis you cannot state one for."
    )
    return "\n".join(lines)


def strip_ops_missing_rule_out(
    ops: Sequence[LedgerOp],
) -> tuple[list[LedgerOp], list[tuple[str, str]]]:
    """`(kept_ops, dropped)`.

    Dropping the `add_hypothesis` is not enough on its own: a verdict that
    adds a hypothesis usually also carries `add_evidence` and
    `record_challenge` ops pointing at it, and leaving those behind produces a
    diff that references an id nothing creates. The ledger invariants reject
    that outright — `diff references unknown hypothesis` — which would turn a
    contained strip back into the whole-payload failure this is written to
    avoid. So every op targeting a dropped id goes with it.

    Only a NEW hypothesis is subject to the requirement. An
    `update_hypothesis` against an id this diff does not create is left alone:
    it is usually adjusting a tier or a probability on something that already
    exists, and requiring the field on every edit would make routine
    maintenance impossible.
    """
    dropped = hypotheses_missing_rule_out(ops)
    if not dropped:
        return list(ops), []

    dropped_ids = {hid for hid, _ in dropped}

    def targets_dropped(op: LedgerOp) -> bool:
        if isinstance(op, AddHypothesis):
            return op.hypothesis.id in dropped_ids
        # Every other op kind identifies its target with `id`.
        return getattr(op, "id", None) in dropped_ids

    return [op for op in ops if not targets_dropped(op)], dropped
