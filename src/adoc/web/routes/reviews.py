"""Weekly review surface (PLAN.md session loop (c)): list + render of
`case/reviews/*.md`.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Request
from starlette.responses import Response

from adoc.casefile.repo import DataRepo
from adoc.web.deps import get_repo
from adoc.web.templating import templates

router = APIRouter(prefix="/reviews")

_SAFE_REVIEW_FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]+\.md$")

# Human phrasing of the real weekly-review schedule — derived from
# `deploy/cfn/ecs.yaml`'s `ReviewRule.Properties.ScheduleExpression`:
# `"cron(0 6 ? * SUN *)"` (AWS cron: minute=0, hour=6 UTC, every Sunday).
# Hardcoded here rather than parsed at runtime since it's an infrastructure
# fact, not app config — if the schedule in ecs.yaml ever changes, this
# string needs a matching edit.
REVIEW_SCHEDULE_PHRASE = "every Sunday at 06:00 UTC"


@router.get("")
def reviews_index(request: Request, repo: DataRepo = Depends(get_repo)) -> Response:
    reviews_dir = repo.root / "case" / "reviews"
    filenames: list[str] = []
    if reviews_dir.is_dir():
        filenames = sorted(
            (p.name for p in reviews_dir.iterdir() if p.suffix == ".md"), reverse=True
        )
    return templates.TemplateResponse(
        request,
        "reviews_index.html",
        {"filenames": filenames, "review_schedule_phrase": REVIEW_SCHEDULE_PHRASE},
    )


@router.get("/{filename}")
def reviews_detail(request: Request, filename: str, repo: DataRepo = Depends(get_repo)) -> Response:
    if not _SAFE_REVIEW_FILENAME_RE.match(filename):
        return templates.TemplateResponse(
            request, "reviews_detail.html", {"filename": filename, "content": None}, status_code=404
        )
    path = repo.root / "case" / "reviews" / filename
    if path.parent != repo.root / "case" / "reviews" or not path.is_file():
        return templates.TemplateResponse(
            request, "reviews_detail.html", {"filename": filename, "content": None}, status_code=404
        )
    content = path.read_text(encoding="utf-8")
    return templates.TemplateResponse(
        request, "reviews_detail.html", {"filename": filename, "content": content}
    )
