"""Authenticated source-page-image serving.

Never a public static mount of the data repo (PLAN.md "confirm" surface):
this route sits behind the same `SessionAuthMiddleware` as everything else
(it is not in the middleware's public-prefix allowlist), and
`resolve_page_image_path` refuses any `sha`/`filename` that isn't a bare,
safe path component — no `..`, no `/`, nothing that could escape
`sources/pages/<sha>/`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from starlette.requests import Request
from starlette.responses import FileResponse, Response

from adoc.casefile.repo import DataRepo
from adoc.web.casefile_helpers import resolve_page_image_path
from adoc.web.deps import get_repo

router = APIRouter(prefix="/files")


@router.get("/pages/{sha}/{filename}")
def page_image(
    request: Request,  # noqa: ARG001 - required for the auth middleware/route symmetry
    sha: str,
    filename: str,
    repo: DataRepo = Depends(get_repo),
) -> Response:
    resolved = resolve_page_image_path(repo, sha, filename)
    if resolved is None:
        return Response(status_code=404)
    return FileResponse(resolved, media_type="image/png")
