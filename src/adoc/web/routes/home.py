"""Home surface (PLAN.md "Steady state UX"): current three-tier differential,
what's new since the last visit, open questions for the next appointment,
the pending-confirmation banner, and the baseline-incomplete banner.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from starlette.responses import Response

from adoc.casefile.ledger import load_ledger
from adoc.casefile.repo import LEDGER_RELPATH, DataRepo
from adoc.casefile.schema import Hypothesis, Ledger, Tier
from adoc.ingest.failures import read_failures
from adoc.intake.agent import intake_is_complete
from adoc.labs.db import LabsDb
from adoc.web.casefile_helpers import ledger_history_since, read_last_seen, write_last_seen
from adoc.web.deps import get_db, get_repo
from adoc.web.templating import templates

router = APIRouter()

_TIER_ORDER: tuple[Tier, ...] = ("most-likely", "expanded", "cant-miss")


def _group_by_tier(ledger: Ledger) -> list[tuple[Tier, list[Hypothesis]]]:
    by_tier: dict[Tier, list[Hypothesis]] = {tier: [] for tier in _TIER_ORDER}
    for hyp in ledger.hypotheses:
        by_tier.setdefault(hyp.tier, []).append(hyp)
    return [(tier, by_tier[tier]) for tier in _TIER_ORDER]


def _open_questions_text(repo: DataRepo) -> str:
    try:
        return repo.read("case/questions-open.md")
    except FileNotFoundError:
        return "_None yet._"


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
    open_questions_html = _open_questions_text(repo)
    failed_count = len(read_failures(repo))

    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "tiers": tiers,
            "whats_new": whats_new,
            "open_questions_text": open_questions_html,
            "pending_count": pending_count,
            "failed_count": failed_count,
            "baseline_incomplete": baseline_incomplete,
            "ledger_version": ledger.version,
        },
    )
