"""Authenticated source-page-image and original-document serving.

Never a public static mount of the data repo (PLAN.md "confirm" surface):
these routes sit behind the same `SessionAuthMiddleware` as everything
else (neither is in the middleware's public-prefix allowlist), and
`resolve_page_image_path`/`resolve_original_document_path` refuse any
`sha`/`filename` that isn't a bare, safe path component — no `..`, no
`/`, nothing that could escape `sources/pages/<sha>/` or `sources/`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from starlette.requests import Request
from starlette.responses import FileResponse, Response

from adoc.casefile.repo import DataRepo
from adoc.web.casefile_helpers import resolve_original_document_path, resolve_page_image_path
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


@router.get("/original/{sha}")
def original_document(
    request: Request,  # noqa: ARG001 - required for the auth middleware/route symmetry
    sha: str,
    repo: DataRepo = Depends(get_repo),
) -> Response:
    """Serve the immutable archived original PDF for `sha`, inline (not a
    download prompt) so a browser tab can preview it directly — the
    confirm queue's "view the full original PDF" source-reference link.
    """
    resolved = resolve_original_document_path(repo, sha)
    if resolved is None:
        return Response(status_code=404)
    filename = resolved.name.split("__", 1)[1] if "__" in resolved.name else resolved.name
    return FileResponse(
        resolved,
        media_type="application/pdf",
        filename=filename,
        content_disposition_type="inline",
    )
