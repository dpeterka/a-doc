"""The printable appointment agenda — `GET /export/agenda` (ADR 0041).

One page, deterministic, no model call. `casefile.export` builds and bounds
it; this route only loads the three sources and renders.

The gate runs here, not only at build time. `casefile.export.
agenda_gate_failures` re-checks the assembled page the way
`routes.ledger._gate_hypothesis_text` re-gates evidence claims at render
time: model-written text in the ledger has no gate on its WRITE path, so a
claim written before some future redaction fix is still covered. A page that
fails is refused outright rather than rendered with holes — it is about to
be printed and handed to a clinician, and there is no version of this
artifact where partial is better than absent.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from starlette.responses import PlainTextResponse, Response

from adoc.casefile.export import agenda_gate_failures as gate_failures
from adoc.casefile.export import build_agenda, render_agenda_markdown
from adoc.casefile.ledger import load_ledger
from adoc.casefile.regimen import REGIMEN_RELPATH, load_regimen
from adoc.casefile.repo import LEDGER_RELPATH, DataRepo
from adoc.labs.db import LabsDb
from adoc.labs.queries import abnormal_summary
from adoc.web.deps import get_db, get_repo
from adoc.web.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/export")

_REFUSED = (
    "This page could not be prepared: one of its lines failed a-doc's treatment/dosing "
    "safety check, so nothing was rendered rather than printing a page with gaps in it. "
    "Nothing is wrong with your account. Please mention it at your next review."
)


@router.get("/agenda")
def agenda_view(
    request: Request,
    repo: DataRepo = Depends(get_repo),
    db: LabsDb = Depends(get_db),
) -> Response:
    ledger = load_ledger(repo.root / LEDGER_RELPATH)
    regimen_path = repo.root / Path(REGIMEN_RELPATH)
    regimen = load_regimen(regimen_path) if regimen_path.exists() else None

    agenda = build_agenda(
        ledger=ledger,
        abnormal=abnormal_summary(db),
        regimen=regimen,
        today=datetime.now(UTC).date(),
    )

    failures = gate_failures(agenda)
    if failures:
        # Reasons only, never the offending text: the same log hygiene
        # `routes.chat` keeps for a `ContractViolation`, whose message is
        # built from real patient-facing content.
        logger.warning(
            "agenda export refused: %d gate failure(s), reasons=%s",
            len(failures),
            sorted({f.split("(")[-1].rstrip(")") for f in failures}),
        )
        return templates.TemplateResponse(
            request, "export_agenda.html", {"agenda": None, "refused": _REFUSED}, status_code=200
        )

    return templates.TemplateResponse(
        request,
        "export_agenda.html",
        {"agenda": agenda, "refused": None},
    )


@router.get("/agenda.md")
def agenda_markdown(
    repo: DataRepo = Depends(get_repo),
    db: LabsDb = Depends(get_db),
) -> Response:
    """The same page as markdown, for anyone who wants to paste it into a
    portal message rather than print it."""
    ledger = load_ledger(repo.root / LEDGER_RELPATH)
    regimen_path = repo.root / Path(REGIMEN_RELPATH)
    regimen = load_regimen(regimen_path) if regimen_path.exists() else None

    agenda = build_agenda(
        ledger=ledger,
        abnormal=abnormal_summary(db),
        regimen=regimen,
        today=datetime.now(UTC).date(),
    )
    if gate_failures(agenda):
        logger.warning("agenda markdown export refused on a gate failure")
        return PlainTextResponse(_REFUSED, status_code=200)
    return PlainTextResponse(render_agenda_markdown(agenda))
