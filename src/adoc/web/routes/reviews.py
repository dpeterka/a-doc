"""Deep-review surface (PLAN.md session loop (c)): per-review permalink
rendering of `case/reviews/*.md`.

`/reviews` (the old index of every review) is now a redirect to `/ledger`
(docs/adr/0019-event-triggered-review.md "UI merge") — the live
differential and the latest review live on one merged screen there, with
every prior review still linked from its "Prior reviews" history section.
Kept as a redirect rather than removed/404 because a link to `/reviews`
may exist in a committed review's markdown or an old chat transcript entry
(mirrors `web.routes.onboard`'s `/onboard` -> `/chat` redirect for the
same reason). `/reviews/{filename}` — the actual permalink each review is
reached by, the audit trail — is UNCHANGED.

`reviews_detail` re-gates the persisted markdown at render time
(`reason.tools.redact_gated_text`, CLAUDE.md rule 5) in addition to
`reason.review.render_review_markdown` gating it at generation time —
reviews written before that fix (or by any future writer that forgets to
gate) still get covered, since this is the one place every review is
actually read.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Request
from starlette.responses import RedirectResponse, Response

from adoc.casefile.repo import DataRepo
from adoc.reason.tools import redact_gated_text
from adoc.web.deps import get_repo
from adoc.web.templating import templates

router = APIRouter(prefix="/reviews")

_SAFE_REVIEW_FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]+\.md$")


@router.get("")
def reviews_index(_request: Request) -> Response:
    """Permanent redirect: the review index is now part of `/ledger`
    (docs/adr/0019-event-triggered-review.md)."""
    return RedirectResponse(url="/ledger", status_code=301)


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
    content = redact_gated_text(path.read_text(encoding="utf-8"))
    return templates.TemplateResponse(
        request, "reviews_detail.html", {"filename": filename, "content": content}
    )
