"""Home surface (PLAN.md "Steady state UX"): current three-tier differential,
what's new since the last visit, open questions for the next appointment,
the pending-confirmation banner, and the baseline-incomplete banner.

Owner-observed feedback (fresh install, docs+labs ingested, no onboarding,
no diagnostic sessions yet): the page rendered as if nothing existed. Home
now renders two genuinely different states rather than one page that's
merely empty-looking when there's nothing diagnostic to show yet:

- **Intake incomplete**: a welcome panel + a CTA into the one continuous
  chat surface, plus a server-computed "what's already on file" strip
  (`casefile_helpers.on_file_summary`) — because a seeded/restored
  deployment may already have documents and labs ingested even though no
  conversation has happened. A zero-document install gets an "add
  documents" pointer instead of the strip.
- **Intake complete**: an actual dashboard — last conversation date,
  patient-reported fact counts (total + recently reported), a ledger *summary*
  (counts per tier + version/last-updated, linking to the full picture —
  not a duplicate of `/ledger`'s full listing) when any hypotheses exist,
  open questions when the file has something non-trivial in it, the latest
  weekly review when any exist, and the same on-file strip. Each block
  renders only when it has content — no empty boxes.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Request
from starlette.responses import Response

from adoc.casefile.ledger import load_ledger
from adoc.casefile.repo import LEDGER_RELPATH, DataRepo
from adoc.casefile.schema import Hypothesis, Ledger, Tier
from adoc.ingest.failures import read_failures
from adoc.intake.agent import intake_is_complete
from adoc.intake.facts import IntakeFactsStore
from adoc.labs.db import LabsDb
from adoc.web.casefile_helpers import (
    last_chat_date,
    ledger_history_since,
    on_file_summary,
    read_last_seen,
    write_last_seen,
)
from adoc.web.deps import get_db, get_repo
from adoc.web.templating import templates

router = APIRouter()

_TIER_ORDER: tuple[Tier, ...] = ("most-likely", "expanded", "cant-miss")

# Mirrors `web.routes.onboard.RECENT_WINDOW_DAYS` (the "since last visit"
# window for the intake-record page) — kept as its own constant rather than
# a cross-import so this module's fact-count computation doesn't couple to
# onboard.py's route wiring for an incidental shared number.
RECENT_FACT_WINDOW_DAYS = 14

# A repo freshly created by `DataRepo.init_at` seeds `case/questions-open.md`
# with exactly this placeholder (`casefile.repo._SCAFFOLD` /
# `_SEED_FILES` equivalent) — used to tell "nothing to show yet" apart from
# genuine content without re-parsing the scaffold.
_OPEN_QUESTIONS_PLACEHOLDER = "_None yet._"

_REVIEW_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")


def _group_by_tier(ledger: Ledger) -> list[tuple[Tier, list[Hypothesis]]]:
    by_tier: dict[Tier, list[Hypothesis]] = {tier: [] for tier in _TIER_ORDER}
    for hyp in ledger.hypotheses:
        by_tier.setdefault(hyp.tier, []).append(hyp)
    return [(tier, by_tier[tier]) for tier in _TIER_ORDER]


def _open_questions_text(repo: DataRepo) -> str:
    try:
        return repo.read("case/questions-open.md")
    except FileNotFoundError:
        return f"_{_OPEN_QUESTIONS_PLACEHOLDER}_"


def _open_questions_nontrivial(text: str) -> bool:
    """Whether `case/questions-open.md` has real content — as opposed to
    the untouched scaffold (or a future re-emptied file). Used to decide
    whether the home dashboard's "open questions" block is worth a box at
    all (CLAUDE.md-adjacent product rule: "no empty boxes")."""
    return _OPEN_QUESTIONS_PLACEHOLDER not in text


def _latest_review(repo: DataRepo) -> dict[str, Any] | None:
    """The most recently dated `case/reviews/*.md` file, or `None` if none
    exist yet — same filename-sort convention as `web.routes.reviews`."""
    reviews_dir = repo.root / "case" / "reviews"
    if not reviews_dir.is_dir():
        return None
    filenames = sorted((p.name for p in reviews_dir.iterdir() if p.suffix == ".md"), reverse=True)
    if not filenames:
        return None
    filename = filenames[0]
    match = _REVIEW_DATE_RE.match(filename)
    review_date = datetime.strptime(match.group(1), "%Y-%m-%d").date() if match else None
    return {"filename": filename, "date": review_date}


@router.get("/")
def home(
    request: Request,
    repo: DataRepo = Depends(get_repo),
    db: LabsDb = Depends(get_db),
) -> Response:
    ledger = load_ledger(repo.root / LEDGER_RELPATH)
    tiers = _group_by_tier(ledger)

    last_seen = read_last_seen(repo)
    whats_new = ledger_history_since(repo, last_seen)
    write_last_seen(repo, datetime.now(UTC))

    baseline_incomplete = not intake_is_complete(repo)

    pending_count = len(db.pending())
    failed_count = len(read_failures(repo))
    on_file = on_file_summary(repo, db)

    context: dict[str, Any] = {
        "baseline_incomplete": baseline_incomplete,
        "pending_count": pending_count,
        "failed_count": failed_count,
        "whats_new": whats_new,
        "on_file": on_file,
    }

    if baseline_incomplete:
        return templates.TemplateResponse(request, "home.html", context)

    facts_store = IntakeFactsStore(repo.root)
    active_facts = facts_store.active_facts()
    cutoff = datetime.now(UTC).date() - timedelta(days=RECENT_FACT_WINDOW_DAYS)
    recent_fact_count = sum(
        1 for fact in active_facts if fact.reported_on is not None and fact.reported_on >= cutoff
    )

    ledger_summary: dict[str, Any] | None = None
    if ledger.hypotheses:
        ledger_summary = {
            "version": ledger.version,
            "updated": ledger.updated,
            "tier_counts": [(tier, len(hyps)) for tier, hyps in tiers],
        }

    open_questions_text = _open_questions_text(repo)

    context.update(
        {
            "last_chat_date": last_chat_date(repo),
            "fact_count": len(active_facts),
            "recent_fact_count": recent_fact_count,
            "recent_fact_window_days": RECENT_FACT_WINDOW_DAYS,
            "ledger_summary": ledger_summary,
            "open_questions_text": open_questions_text,
            "open_questions_nontrivial": _open_questions_nontrivial(open_questions_text),
            "latest_review": _latest_review(repo),
        }
    )
    return templates.TemplateResponse(request, "home.html", context)
