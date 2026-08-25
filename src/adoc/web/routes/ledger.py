"""The merged "full picture" surface (docs/adr/0019-event-triggered-review.md
"UI merge"): the live differential ledger AND the latest deep review, on
one screen — this used to be `/ledger` (live state) and `/reviews` (a
separate index of dated review artifacts), which made sense when a review
was a weekly event distinct from the running state. Now that a review can
fire on new evidence, the latest review IS approximately the current
picture, so two nav entries for one thing was confusing (owner feedback,
folded into ADR 0019 rather than a separate ADR).

`/reviews` (the old index) is kept as a redirect here, not removed
(`web.routes.reviews`), since links to it may exist in committed review
markdown or the chat transcript; `/reviews/{filename}` (the actual
per-review permalink, the audit trail) is UNCHANGED and still how every
prior review is reached from this page's history list.

Evidence claims, discriminators, and challenger notes are all model-written
free text with no gate on their write path (`casefile/ledger.py`'s
`apply_diff` never runs `safety.treatment_gate`) — so a hypothesis added
before this fix, or one added by a code path outside this repo's own
gate-guided composer/challenger loops, can carry ungated text at rest.
`_gate_hypothesis_text` re-gates it at render time (CLAUDE.md rule 5),
via `reason.tools.redact_gated_text`, so every render is covered
regardless of when or how the text was written. This mutates only the
in-memory `Ledger` built by this one request's `load_ledger` call — never
saved back, so the on-disk ledger and its evidence/provenance are
untouched. The latest review's persisted markdown is re-gated the same way
at render time (mirrors the old `reviews_detail`'s rationale) — a review
written before some future redaction fix is still covered here too.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from starlette.responses import Response

from adoc.casefile.ledger import load_ledger
from adoc.casefile.repo import LEDGER_RELPATH, DataRepo
from adoc.casefile.schema import Ledger
from adoc.labs.db import LabsDb
from adoc.labs.models import LabDocument
from adoc.reason.review import FULL_REVIEW_COOLDOWN, FULL_REVIEW_FLOOR
from adoc.reason.tools import redact_gated_text
from adoc.web.casefile_helpers import find_document_by_filename, page_image_url
from adoc.web.deps import get_db, get_repo
from adoc.web.templating import templates

router = APIRouter(prefix="/ledger")

_LABS_REF_RE = re.compile(r"^labs:(?P<slug>[a-z0-9-]+):(?P<date>\d{4}-\d{2}-\d{2})$")
_DOC_REF_RE = re.compile(r"^doc:(?P<filename>[^\s#]+)#p(?P<page>\d+)$")
_PMID_REF_RE = re.compile(r"^pmid:(?P<pmid>\d+)$")

# Human phrasing of the event-triggered review mechanism (docs/adr/0019),
# derived from `reason.review`'s actual cooldown/floor constants rather
# than hardcoded, so this never drifts out of sync with the real gating
# logic the way the old cron-derived `REVIEW_SCHEDULE_PHRASE` could only
# ever describe a fixed weekly time.
_COOLDOWN_HOURS = int(FULL_REVIEW_COOLDOWN.total_seconds() // 3600)
_FLOOR_DAYS = FULL_REVIEW_FLOOR.days
REVIEW_TRIGGER_PHRASE = (
    "automatically, whenever something new arrives — a new document, or a conversation that "
    f"changes your leads — at most once every {_COOLDOWN_HOURS} hours, and at least once every "
    f"{_FLOOR_DAYS} days even if nothing has changed"
)


def _source_ref_href(
    source: str,
    *,
    repo: DataRepo,
    db: LabsDb,
    documents: list[LabDocument],
    image_cache: dict[str, list[Path]],
) -> str | None:
    if match := _LABS_REF_RE.match(source):
        return f"/labs/{quote(match.group('slug'), safe='')}"
    if match := _DOC_REF_RE.match(source):
        doc = find_document_by_filename(db, match.group("filename"), documents=documents)
        if doc is None:
            return None
        return page_image_url(repo, doc.sha256, int(match.group("page")), cache=image_cache)
    if match := _PMID_REF_RE.match(source):
        return f"https://pubmed.ncbi.nlm.nih.gov/{match.group('pmid')}/"
    return None


def _gate_hypothesis_text(ledger: Ledger) -> Ledger:
    """Redact any dosing/treatment-instruction span out of every
    model-written free-text field the template renders — evidence claims,
    discriminators, challenger notes — in place, on this request's
    in-memory `Ledger` only (see module docstring)."""
    for h in ledger.hypotheses:
        for evidence in (*h.evidence_for, *h.evidence_against):
            evidence.claim = redact_gated_text(evidence.claim)
        h.discriminators = [redact_gated_text(d) for d in h.discriminators]
        h.challenger_notes = redact_gated_text(h.challenger_notes)
    return ledger


def _review_filenames(repo: DataRepo) -> list[str]:
    """Every committed `case/reviews/*.md` filename, most recent first.
    Same sort convention as the old `web.routes.reviews.reviews_index` and
    `web.routes.home._latest_review` — sorting the filenames themselves
    works because every filename starts with an ISO date (`YYYY-MM-DD`,
    optionally `THHMMSS` for a same-day collision, `reason.review.
    _review_relpath_and_tag`'s docstring), which sorts lexicographically in
    chronological order."""
    reviews_dir = repo.root / "case" / "reviews"
    if not reviews_dir.is_dir():
        return []
    return sorted((p.name for p in reviews_dir.iterdir() if p.suffix == ".md"), reverse=True)


def _latest_review(repo: DataRepo, filenames: list[str]) -> dict[str, Any] | None:
    """The most recent review's filename and redacted markdown content
    (re-gated at render time, module docstring), or `None` if none exist
    yet."""
    if not filenames:
        return None
    filename = filenames[0]
    content = redact_gated_text(
        (repo.root / "case" / "reviews" / filename).read_text(encoding="utf-8")
    )
    return {"filename": filename, "content": content}


@router.get("")
def ledger_view(
    request: Request,
    repo: DataRepo = Depends(get_repo),
    db: LabsDb = Depends(get_db),
) -> Response:
    ledger = _gate_hypothesis_text(load_ledger(repo.root / LEDGER_RELPATH))

    # Fetched/listed once per page render, not once per evidence source-ref:
    # a ledger with many hypotheses/evidence items would otherwise re-run
    # `db.list_documents()` (a full-table query) and re-list a document's
    # page-image directory (a filesystem `iterdir()`) once per `doc:` ref,
    # even when several refs point at the same document — see
    # `find_document_by_filename`/`list_page_images`'s docstrings.
    documents = db.list_documents()
    image_cache: dict[str, list[Path]] = {}

    def href(source: str) -> str | None:
        return _source_ref_href(
            source, repo=repo, db=db, documents=documents, image_cache=image_cache
        )

    filenames = _review_filenames(repo)
    latest_review = _latest_review(repo, filenames)
    prior_review_filenames = filenames[1:]

    return templates.TemplateResponse(
        request,
        "ledger.html",
        {
            "ledger": ledger,
            "source_ref_href": href,
            "latest_review": latest_review,
            "prior_review_filenames": prior_review_filenames,
            "review_trigger_phrase": REVIEW_TRIGGER_PHRASE,
        },
    )
