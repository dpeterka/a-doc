"""The "review wanted" marker (docs/adr/0019-event-triggered-review.md):
the coalescing signal `reason.review`'s event-triggered deep review reads
to decide whether a marker-driven full review is due.

Set by two call sites, both material to the ledger's picture (never by an
informational chat turn or a duplicate/errored ingest, which change
nothing):

- `ingest.pipeline` — any `ingest_file`/`ingest_inbox`/`ingest_directory`
  run that actually ingested at least one document (new lab rows or not —
  a new encounter/document is material even with zero rows). Called ONCE
  per pipeline invocation over the whole batch, not once per file, so a
  20-file Dropbox drop coalesces into one marker update rather than firing
  20 review considerations (ADR 0019's "thrash and cost" rationale).
- `reason.stages.run_diagnostic_turn` — only when the diagnostic DAG's
  `apply` node actually committed a ledger diff (checked via `sink["apply"]`
  being populated, not merely "the turn didn't raise") — an informational
  turn never reaches this at all, and a diagnostic turn whose `apply`
  precondition rejected the diff before it could commit correctly does not
  mark either.

Storage: `work/review-wanted.json`, deliberately gitignored (`casefile.
repo._GITIGNORE` already excludes all of `work/`) rather than committed —
this is a derived scheduling signal, not part of the patient's case-file
record (contrast `case/reviews/*.md`, which IS the durable audit trail).
Losing this file only means the next scheduled tick reconsiders slightly
conservatively (no marker on file -> gated on the floor window alone,
`reason.review.should_run_full_review`), never a correctness problem, and
it survives a container restart on the same EFS mount exactly like
`work/entailment-cache.json`/`work/entailment-deferred.json` already do.

Load/save mirrors `reason.verify`'s `_load_deferred_claims`/
`_save_deferred_claims` pattern deliberately (same `work/`-scoped,
whole-file JSON round-trip, no locking beyond what `DataRepo` already
provides for git operations) — this is one more instance of an established
pattern, not a new one.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from adoc.casefile.repo import DataRepo

REVIEW_MARKER_RELPATH = "work/review-wanted.json"

# Generous cap on how many reasons accumulate between full reviews: at a
# 6h cooldown and a 7-day floor (`reason.review`'s constants), the marker
# is cleared at least weekly, and realistically far more often once
# anything is happening in the case file — 50 is headroom, not a tight
# budget, and trimming the OLDEST entries (keep the most recent 50) means a
# pathological run of updates still shows the review report what most
# recently prompted it rather than silently growing the file forever.
_MAX_REASONS = 50


class ReviewMarkerReason(BaseModel):
    """One thing that happened that made a review worth running, with when
    it happened — so the eventual full review's report can say what
    prompted it (task requirement: "Record why it was set and when")."""

    reason: str
    at: datetime


class ReviewMarker(BaseModel):
    """The "review wanted" signal: a non-empty `reasons` list on disk means
    a full review is wanted; the file's absence (`load_review_marker`
    returning `None`) means nothing has happened since the marker was last
    cleared."""

    reasons: list[ReviewMarkerReason] = Field(default_factory=list)

    @property
    def first_set_at(self) -> datetime | None:
        return self.reasons[0].at if self.reasons else None

    @property
    def last_set_at(self) -> datetime | None:
        return self.reasons[-1].at if self.reasons else None

    def summary(self) -> str:
        """A short, human-readable rollup of every accumulated reason, for
        the review report's "what triggered this review" section — e.g.
        `"ingest: 3 new document(s), 12 new lab row(s); chat turn applied a
        ledger diff (1 op(s))"`. Deduplicates consecutive identical
        reasons (a coalesced multi-tick accumulation of the same event)
        into one entry rather than repeating it."""
        if not self.reasons:
            return "no reasons recorded"
        seen: list[str] = []
        for entry in self.reasons:
            if not seen or seen[-1] != entry.reason:
                seen.append(entry.reason)
        return "; ".join(seen)


def _marker_path(repo: DataRepo) -> Path:
    return repo.root / REVIEW_MARKER_RELPATH


def load_review_marker(repo: DataRepo) -> ReviewMarker | None:
    """`None` if no marker is set (file absent) or the file is unreadable
    (corrupt JSON never crashes a scheduled tick — treated the same as "no
    marker", which is the conservative/safe direction: a lost marker can
    only delay a review to the next material event or the floor window,
    never silently apply an unwanted one)."""
    path = _marker_path(repo)
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
        return ReviewMarker.model_validate(json.loads(raw))
    except (OSError, ValueError):
        return None


def mark_review_wanted(repo: DataRepo, reason: str, *, at: datetime | None = None) -> ReviewMarker:
    """Set (or extend, if already set) the "review wanted" marker with one
    more `reason`. Idempotent to call repeatedly — every call just appends
    another timestamped reason, so coalescing falls naturally out of "the
    marker is a list, and only its non-emptiness gates a review" rather
    than needing any de-dup logic at write time."""
    at = at if at is not None else datetime.now(UTC)
    marker = load_review_marker(repo) or ReviewMarker()
    marker.reasons.append(ReviewMarkerReason(reason=reason, at=at))
    if len(marker.reasons) > _MAX_REASONS:
        marker.reasons = marker.reasons[-_MAX_REASONS:]
    path = _marker_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(marker.model_dump_json(indent=2), encoding="utf-8")
    return marker


def clear_review_marker(repo: DataRepo) -> None:
    """Remove the marker file — called ONLY after a full review has
    successfully committed (`reason.review.run_review_tick`), never before
    or during, so a full review that crashes partway through leaves the
    marker standing and the next tick simply tries again (task requirement:
    "marker survives a failed run and is cleared after a successful one").
    A no-op if the file doesn't exist."""
    _marker_path(repo).unlink(missing_ok=True)


__all__ = [
    "REVIEW_MARKER_RELPATH",
    "ReviewMarker",
    "ReviewMarkerReason",
    "clear_review_marker",
    "load_review_marker",
    "mark_review_wanted",
]
