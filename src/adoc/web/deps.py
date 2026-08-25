"""FastAPI dependency seams: pull the shared repo/db/client/vision/settings
off `request.app.state`, where `app.py` puts them (real wiring or the
fakes a test's `create_app(...)` call injected)."""

from __future__ import annotations

from starlette.requests import Request

from adoc.casefile.repo import DataRepo
from adoc.config import Settings
from adoc.ingest.archive import PageRenderer
from adoc.ingest.vision import VisionClient
from adoc.labs.db import LabsDb
from adoc.reason.client import LlmClient


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_repo(request: Request) -> DataRepo:
    repo: DataRepo = request.app.state.repo
    return repo


def get_db(request: Request) -> LabsDb:
    db: LabsDb = request.app.state.db
    return db


def get_client(request: Request) -> LlmClient:
    client: LlmClient = request.app.state.client
    return client


def get_vision(request: Request) -> VisionClient:
    vision: VisionClient = request.app.state.vision
    return vision


def get_renderer(request: Request) -> PageRenderer:
    renderer: PageRenderer = request.app.state.renderer
    return renderer


def get_privacy_warning(request: Request) -> str | None:
    """`None` once `case/identifiers.yaml` has at least one name configured;
    otherwise the loud warning `app.py` also prints at startup (see its
    docstring) — a seam for any route/template that wants to show this to
    the operator (e.g. a dismissible banner), without every such call site
    reaching into `app.state` directly."""
    warning: str | None = request.app.state.privacy_warning
    return warning
