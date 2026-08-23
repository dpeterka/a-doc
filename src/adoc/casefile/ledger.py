"""Ledger persistence and diff-application invariants.

`apply_diff` is the deterministic gatekeeper described in PLAN.md
"Anti-anchoring": it is the only way a `LedgerDiff` (produced by an LLM
stage) is allowed to mutate a `Ledger`, and it enforces the five invariants
below as code, never as a prompt. `load_ledger`/`save_ledger` round-trip
`differential-ledger.yaml` via ruamel.yaml so the file stays human-diffable
in git. `apply_and_save` is the thin persistence wrapper used by real
callers (DAG nodes, `repo.py`): it loads, applies, saves, and appends the
diff to `ledger-history.jsonl` in one call.

Invariants enforced by `apply_diff` (raise `LedgerInvariantError`):
  a. Can't-miss non-empty while any hypothesis is active.
  b. A patient-origin hypothesis cannot reach tier=most-likely in the diff
     that creates it; it must have been substantively challenged (a
     `RecordChallenge` op) in a strictly earlier diff first.
  c. Staleness: reject a diff if any active hypothesis's freshness clock
     (`last_challenged_version`) is older than `new_version - 2`, unless
     this diff records a challenge for it. See `stale_hypotheses`.
  d. `confirmed-by-doctor` hypotheses may only be touched by a diff that
     both (i) includes a NEW `evidence_against` item for that hypothesis in
     the same diff and (ii) gives a non-empty rationale.
  e. Every applied diff bumps `version`, sets `updated`, records
     `prior_probability` on any hypothesis whose probability moves, and (via
     `apply_and_save`) is appended verbatim (with provenance) to
     `ledger-history.jsonl`.
"""

from __future__ import annotations

import json
from pathlib import Path

from ruamel.yaml import YAML

from adoc.casefile.schema import (
    AddEvidence,
    AddHypothesis,
    Hypothesis,
    Ledger,
    LedgerDiff,
    RecordChallenge,
    UpdateHypothesis,
)

ACTIVE_STATUSES = frozenset({"active", "patient-proposed", "challenged"})
CANT_MISS_TIER = "cant-miss"
STALENESS_HORIZON = 2


class LedgerInvariantError(Exception):
    """Raised when applying a `LedgerDiff` would violate a ledger invariant."""


# --- persistence --------------------------------------------------------------------


def load_ledger(path: Path) -> Ledger:
    """Load `differential-ledger.yaml`. A missing file is not an error at this
    layer (callers such as `DataRepo.init_at` decide whether to create an
    empty v0 ledger); use `Ledger.model_validate` directly if you want that.
    """
    yaml = YAML(typ="safe")
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.load(fh) or {}
    return Ledger.model_validate(data)


def save_ledger(path: Path, ledger: Ledger) -> None:
    """Write `ledger` to `path` as stable, human-diffable YAML.

    Dumping `model_dump(mode="json")` (plain dicts/lists/strings, in field
    declaration order) through ruamel gives byte-identical output for
    identical content across repeated saves.
    """
    data = ledger.model_dump(mode="json")
    yaml = YAML()
    yaml.default_flow_style = False
    yaml.indent(mapping=2, sequence=4, offset=2)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        yaml.dump(data, fh)


def append_history(history_path: Path, diff: LedgerDiff, ledger: Ledger) -> None:
    """Append one JSON line recording an applied diff (never rewritten/deleted)."""
    record = {
        "resulting_version": ledger.version,
        "resulting_updated": ledger.updated.isoformat(),
        "diff": diff.model_dump(mode="json"),
    }
    history_path.parent.mkdir(parents=True, exist_ok=True)
    with history_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record))
        fh.write("\n")


def apply_and_save(ledger_path: Path, history_path: Path, diff: LedgerDiff) -> Ledger:
    """Load, apply (invariant-checked), save, and record history in one call."""
    ledger = load_ledger(ledger_path)
    new_ledger = apply_diff(ledger, diff)
    save_ledger(ledger_path, new_ledger)
    append_history(history_path, diff, new_ledger)
    return new_ledger


# --- staleness -----------------------------------------------------------------------


def _is_stale(hypothesis: Hypothesis, new_version: int, horizon: int = STALENESS_HORIZON) -> bool:
    if hypothesis.status not in ACTIVE_STATUSES:
        return False
    threshold = new_version - horizon
    if hypothesis.last_challenged_version is None:
        return True
    return hypothesis.last_challenged_version < threshold


def stale_hypotheses(ledger: Ledger, *, horizon: int = STALENESS_HORIZON) -> list[Hypothesis]:
    """Active hypotheses that would trigger invariant (c) on the next diff."""
    hypothetical_next_version = ledger.version + 1
    return [
        h for h in ledger.hypotheses if _is_stale(h, hypothetical_next_version, horizon=horizon)
    ]


# --- apply_diff ------------------------------------------------------------------------


def _find(hypotheses: list[Hypothesis], hyp_id: str) -> Hypothesis:
    for h in hypotheses:
        if h.id == hyp_id:
            return h
    raise LedgerInvariantError(f"diff references unknown hypothesis id {hyp_id!r}")


def apply_diff(ledger: Ledger, diff: LedgerDiff) -> Ledger:
    """Apply `diff` to `ledger`, returning a *new* `Ledger`. Pure: `ledger` and
    `diff` are never mutated. Raises `LedgerInvariantError` if applying the
    diff would violate any of the invariants documented at module level.
    """
    new_version = ledger.version + 1
    before_by_id = {h.id: h for h in ledger.hypotheses}

    challenged_ids = {op.id for op in diff.ops if isinstance(op, RecordChallenge)}
    new_evidence_against_ids = {
        op.id for op in diff.ops if isinstance(op, AddEvidence) and op.for_or_against == "against"
    }

    # --- invariant (c): staleness, evaluated against the pre-diff ledger -----------
    for h in ledger.hypotheses:
        if h.id in challenged_ids:
            continue
        if _is_stale(h, new_version):
            raise LedgerInvariantError(
                f"cannot apply diff: hypothesis {h.id!r} is stale "
                f"(last_challenged_version={h.last_challenged_version!r}, "
                f"new_version={new_version}) and this diff does not challenge it"
            )

    working = [h.model_copy(deep=True) for h in ledger.hypotheses]

    def require_confirmed_bar(hyp: Hypothesis) -> None:
        """Invariant (d): raised bar for confirmed-by-doctor hypotheses."""
        if hyp.status != "confirmed-by-doctor":
            return
        if hyp.id not in new_evidence_against_ids or not diff.rationale.strip():
            raise LedgerInvariantError(
                f"cannot touch confirmed-by-doctor hypothesis {hyp.id!r}: the diff "
                "must include a new evidence_against item for it and a non-empty "
                "rationale citing it"
            )

    for op in diff.ops:
        if isinstance(op, AddHypothesis):
            if any(h.id == op.hypothesis.id for h in working):
                raise LedgerInvariantError(
                    f"cannot add hypothesis {op.hypothesis.id!r}: id already exists"
                )
            # Invariant (b), add-path: a patient-origin hypothesis can never
            # ENTER the ledger at most-likely — a Challenger pass in a later
            # diff must precede any promotion (anti-anchoring; PLAN.md).
            if op.hypothesis.origin == "patient" and op.hypothesis.tier == "most-likely":
                raise LedgerInvariantError(
                    f"cannot add patient-origin hypothesis {op.hypothesis.id!r} at "
                    "tier=most-likely: it must be challenged before promotion"
                )
            new_hyp = op.hypothesis.model_copy(deep=True)
            new_hyp.last_challenged_version = new_version
            working.append(new_hyp)

        elif isinstance(op, UpdateHypothesis):
            hyp = _find(working, op.id)
            require_confirmed_bar(hyp)

            if op.tier == "most-likely" and hyp.origin == "patient":
                original = before_by_id.get(op.id)
                if original is None or original.last_challenged is None:
                    raise LedgerInvariantError(
                        f"cannot promote patient-origin hypothesis {op.id!r} to "
                        "most-likely: it must be substantively challenged "
                        "(RecordChallenge) in an earlier diff first"
                    )

            if op.probability is not None and op.probability != hyp.probability:
                hyp.prior_probability = hyp.probability
                hyp.probability = op.probability
            if op.tier is not None:
                hyp.tier = op.tier
            if op.status is not None:
                hyp.status = op.status
            if op.discriminators is not None:
                hyp.discriminators = list(op.discriminators)

        elif isinstance(op, AddEvidence):
            hyp = _find(working, op.id)
            require_confirmed_bar(hyp)
            if op.for_or_against == "for":
                hyp.evidence_for.append(op.evidence)
            else:
                hyp.evidence_against.append(op.evidence)

        elif isinstance(op, RecordChallenge):
            hyp = _find(working, op.id)
            require_confirmed_bar(hyp)
            hyp.last_challenged = diff.provenance.timestamp.date()
            hyp.last_challenged_version = new_version
            hyp.challenger_notes = (
                f"{hyp.challenger_notes}\n{op.note}".strip() if hyp.challenger_notes else op.note
            )

        else:  # pragma: no cover - exhaustive over the LedgerOp union
            raise LedgerInvariantError(f"unknown ledger op: {op!r}")

    # --- invariant (a): can't-miss non-empty while any hypothesis is active --------
    any_active = any(h.status in ACTIVE_STATUSES for h in working)
    any_cant_miss_active = any(
        h.status in ACTIVE_STATUSES and h.tier == CANT_MISS_TIER for h in working
    )
    if any_active and not any_cant_miss_active:
        raise LedgerInvariantError(
            "cannot apply diff: the cant-miss tier would be empty while a hypothesis remains active"
        )

    return Ledger(
        version=new_version,
        updated=diff.provenance.timestamp,
        schema_version=ledger.schema_version,
        hypotheses=working,
    )
