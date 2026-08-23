"""FastAPI application factory for the patient-facing web UI (PLAN.md "UI").

`create_app` takes `settings`/`repo`/`db`/`client` as dependency seams so
tests can inject fakes (a fake `DataRepo` over a `tmp_path`, an in-memory
`LabsDb`, a fake-transport `LlmClient`) without touching the real data
repo, sqlite file, or network. `vision`/`renderer` are additional,
optional seams for the same reason (the upload route's ingestion pipeline
needs both) — real callers (`cli.py`'s `serve` command) never pass them.

Everything except `/login` and `/static/*` requires a valid session
cookie (see `web.security.SessionAuthMiddleware`).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from starlette.staticfiles import StaticFiles

from adoc.casefile.repo import DataRepo
from adoc.config import Settings
from adoc.ingest.archive import PageRenderer, pdftoppm_renderer
from adoc.ingest.vision import VisionClient
from adoc.labs.db import LabsDb
from adoc.reason.client import LlmClient
from adoc.web.routes import auth, chat, confirm, files, home, labs, ledger, onboard, reviews, upload
from adoc.web.security import SessionAuthMiddleware, load_or_create_session_secret

STATIC_DIR = Path(__file__).parent / "static"


def create_app(
    settings: Settings | None = None,
    *,
    repo: DataRepo | None = None,
    db: LabsDb | None = None,
    client: LlmClient | None = None,
    vision: VisionClient | None = None,
    renderer: PageRenderer | None = None,
) -> FastAPI:
    resolved_settings = settings if settings is not None else Settings()
    resolved_repo = repo if repo is not None else DataRepo(resolved_settings.data_dir)
    resolved_db = db if db is not None else LabsDb(resolved_settings.data_dir / "labs.sqlite")
    resolved_client = client if client is not None else LlmClient.from_settings(resolved_settings)
    resolved_vision = vision if vision is not None else VisionClient(resolved_client)
    resolved_renderer = renderer if renderer is not None else pdftoppm_renderer

    app = FastAPI(title="a-doc")
    app.state.settings = resolved_settings
    app.state.repo = resolved_repo
    app.state.db = resolved_db
    app.state.client = resolved_client
    app.state.vision = resolved_vision
    app.state.renderer = resolved_renderer
    app.state.session_secret = load_or_create_session_secret(resolved_repo)

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.add_middleware(SessionAuthMiddleware)

    app.include_router(auth.router)
    app.include_router(home.router)
    app.include_router(onboard.router)
    app.include_router(chat.router)
    app.include_router(upload.router)
    app.include_router(confirm.router)
    app.include_router(files.router)
    app.include_router(labs.router)
    app.include_router(ledger.router)
    app.include_router(reviews.router)

    return app
