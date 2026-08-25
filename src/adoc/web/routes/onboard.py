"""The former onboarding chat surface (`docs/adr/0012-initial-visit-conversation.md`,
superseding 0011's sectioned stepping): onboarding is now the SAME chat
surface as `/chat` — every turn while intake is incomplete is routed
through `intake.agent.run_intake_turn` from there. `/onboard` and
`/onboard/send` are kept only as redirects for any bookmarked/old link;
`/onboard/review` stays as a real page ("Intake record") — a read-only
record of every fact captured, grouped by internal topic, which is fine to
show even though the topics themselves are never surfaced as a stepper
during the conversation itself.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Request
from starlette.responses import RedirectResponse, Response

from adoc.casefile.repo import DataRepo
from adoc.intake.agent import intake_is_complete
from adoc.intake.facts import IntakeFact, IntakeFactsStore
from adoc.intake.sections import SECTIONS
from adoc.web.deps import get_repo
from adoc.web.templating import templates

router = APIRouter(prefix="/onboard")

# "Since last visit" strip (docs/adr/0013-fact-corroboration.md, "interval
# history"): facts reported within this many days of today.
RECENT_WINDOW_DAYS = 14


def _sort_key(fact: IntakeFact) -> int:
    """Contradicted facts sort first within their topic — everything else
    keeps its original (insertion) order, since `sorted` is stable."""
    return 0 if fact.corroboration == "contradicted" else 1


@router.get("")
def onboard_page(_request: Request) -> Response:
    """Permanent redirect: onboarding now happens on `/chat`."""
    return RedirectResponse(url="/chat", status_code=301)


@router.post("/send")
def onboard_send(_request: Request) -> Response:
    """Redirect (Post/Redirect/Get): any old bookmarked form still lands
    the patient on the one real chat surface."""
    return RedirectResponse(url="/chat", status_code=303)


@router.get("/review")
def onboard_review(request: Request, repo: DataRepo = Depends(get_repo)) -> Response:
    facts_store = IntakeFactsStore(repo.root)

    sections: dict[str, dict[str, Any]] = {}
    for spec in SECTIONS:
        section_facts = [f for f in facts_store.facts if f.section == spec.key]
        sections[spec.key] = {
            "title": spec.title,
            "active": sorted((f for f in section_facts if f.status == "active"), key=_sort_key),
            "retracted": [f for f in section_facts if f.status == "retracted"],
        }

    cutoff = datetime.now(UTC).date() - timedelta(days=RECENT_WINDOW_DAYS)
    recent_facts = sorted(
        (
            f
            for f in facts_store.facts
            if f.status == "active" and f.reported_on is not None and f.reported_on >= cutoff
        ),
        key=lambda f: f.reported_on,  # type: ignore[arg-type, return-value]
        reverse=True,
    )

    return templates.TemplateResponse(
        request,
        "onboard_review.html",
        {
            "sections": sections,
            "baseline_incomplete": not intake_is_complete(repo),
            "recent_facts": recent_facts,
            "recent_window_days": RECENT_WINDOW_DAYS,
        },
    )
