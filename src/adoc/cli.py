"""adoc command-line entrypoint.

Subcommands mirror PLAN.md's phasing: `init` does real work (validates
Settings/models.yaml, then creates the data-repo layout via
`DataRepo.init_at`); `ingest` (scan `<data_dir>/inbox/`) and `backfill
<directory>` are wired to the real `ingest.pipeline` with a real
`LlmClient`/`VisionClient` (`_build_llm_client`/`_build_vision_client` are
the seams tests override with fakes); `serve` builds the real
`web.app.create_app()` and runs it under uvicorn (`_run_uvicorn` is the
seam tests override so a test run never actually binds a socket).
`onboard` is wired to the real intake wizard. `review`, `eval` remain
stubbed scaffold placeholders for a later slice.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from fastapi import FastAPI

from adoc.casefile.repo import DataRepo
from adoc.config import Settings, load_model_bindings
from adoc.ingest.archive import PageRenderer, pdftoppm_renderer
from adoc.ingest.pipeline import IngestReport, ingest_directory, ingest_inbox
from adoc.ingest.vision import VisionClient
from adoc.intake.cli import run_onboarding_session
from adoc.intake.wizard import IntakeWizard
from adoc.labs.db import LabsDb
from adoc.privacy import Scrubber
from adoc.reason.client import LlmClient


def _cmd_init(_args: argparse.Namespace) -> int:
    """Validate config, then create (or confirm) the data-repo layout."""
    try:
        settings = Settings()
        bindings = load_model_bindings(settings.models_file)
    except Exception as exc:  # noqa: BLE001 - surface any config error to the user
        print(f"init: configuration error: {exc}", file=sys.stderr)
        return 1

    print(f"init: data_dir={settings.data_dir}")
    print(f"init: loaded {len(bindings)} model role bindings from {settings.models_file}")

    try:
        already_initialized = DataRepo(settings.data_dir).is_initialized
        DataRepo.init_at(settings.data_dir)
    except OSError as exc:
        print(f"init: configuration error: cannot create data dir: {exc}", file=sys.stderr)
        return 1
    if already_initialized:
        print(f"init: data repo already initialized at {settings.data_dir}")
    else:
        print(f"init: initialized data repo at {settings.data_dir}")
    return 0


def _stub(name: str) -> int:
    print(f"{name}: not implemented (phase 1)")
    return 0


def _cmd_onboard(_args: argparse.Namespace) -> int:
    try:
        settings = Settings()
    except Exception as exc:  # noqa: BLE001 - surface any config error to the user
        print(f"onboard: configuration error: {exc}", file=sys.stderr)
        return 1

    repo = DataRepo(settings.data_dir)
    if not repo.is_initialized:
        print("onboard: data repo not initialized; run `adoc init` first", file=sys.stderr)
        return 1

    try:
        client = LlmClient.from_settings(settings)
    except Exception as exc:  # noqa: BLE001 - surface any config error to the user
        print(f"onboard: configuration error: {exc}", file=sys.stderr)
        return 1

    wizard = IntakeWizard(repo, client, dropbox_folder=settings.dropbox_folder)
    # `input`/`print` are looked up here (not bound as a default parameter
    # value at import time) so tests can monkeypatch `builtins.input`.
    return run_onboarding_session(wizard, input_fn=input, print_fn=print)


def _build_llm_client(settings: Settings) -> LlmClient:
    """Real wiring for `LlmClient`: bindings from `models.yaml`, scrubbing,
    and an audit log under the data repo's (gitignored) `logs/` dir.
    """
    scrubber = Scrubber.from_file(settings.data_dir / "case" / "identifiers.yaml")
    audit_log_path = settings.data_dir / "logs" / "api-audit.jsonl"
    return LlmClient.from_settings(settings, scrubber=scrubber, audit_log_path=audit_log_path)


def _build_vision_client(llm_client: LlmClient) -> VisionClient:
    """Real wiring for `VisionClient`. Overridden by tests to inject fakes."""
    return VisionClient(llm_client)


def _build_renderer() -> PageRenderer:
    """Real wiring for the page renderer (`pdftoppm`). Overridden by tests
    so CI never depends on poppler being installed.
    """
    return pdftoppm_renderer


def _print_ingest_report(report: IngestReport) -> None:
    for outcome in report.files:
        print(
            f"ingest: {outcome.path}: {outcome.outcome}"
            f" (auto={outcome.rows_auto} pending={outcome.rows_pending})"
        )
        for issue in outcome.issues:
            print(f"  - {issue}")
    print(
        f"ingest: {len(report.files)} file(s), "
        f"{report.total_auto} auto, {report.total_pending} pending"
    )


def _run_ingest(settings: Settings, directory: Path | None) -> int:
    repo = DataRepo(settings.data_dir)
    if not repo.is_initialized:
        print(
            f"ingest: data repo not initialized at {settings.data_dir} - run `adoc init` first",
            file=sys.stderr,
        )
        return 1

    llm_client = _build_llm_client(settings)
    vision = _build_vision_client(llm_client)
    renderer = _build_renderer()
    db_path = settings.data_dir / "labs.sqlite"
    with LabsDb(db_path) as db:
        if directory is not None:
            report = ingest_directory(directory, repo=repo, db=db, vision=vision, renderer=renderer)
        else:
            report = ingest_inbox(repo=repo, db=db, vision=vision, renderer=renderer)

    _print_ingest_report(report)
    return 1 if any(f.outcome == "error" for f in report.files) else 0


def _cmd_ingest(_args: argparse.Namespace) -> int:
    try:
        settings = Settings()
    except Exception as exc:  # noqa: BLE001 - surface any config error to the user
        print(f"ingest: configuration error: {exc}", file=sys.stderr)
        return 1
    return _run_ingest(settings, directory=None)


def _cmd_review(_args: argparse.Namespace) -> int:
    return _stub("review")


def _run_uvicorn(app: FastAPI, *, host: str, port: int) -> None:  # pragma: no cover - real server
    """Real wiring for `serve`. Overridden by tests so a test run never
    actually binds a socket or blocks."""
    import uvicorn

    uvicorn.run(app, host=host, port=port)


def _cmd_serve(args: argparse.Namespace) -> int:
    try:
        settings = Settings()
    except Exception as exc:  # noqa: BLE001 - surface any config error to the user
        print(f"serve: configuration error: {exc}", file=sys.stderr)
        return 1

    from adoc.web.app import create_app

    app = create_app(settings)
    print(f"serve: starting on http://{args.host}:{args.port}")
    _run_uvicorn(app, host=args.host, port=args.port)
    return 0


def _cmd_backfill(args: argparse.Namespace) -> int:
    try:
        settings = Settings()
    except Exception as exc:  # noqa: BLE001 - surface any config error to the user
        print(f"backfill: configuration error: {exc}", file=sys.stderr)
        return 1
    directory = Path(args.directory)
    if not directory.is_dir():
        print(f"backfill: not a directory: {directory}", file=sys.stderr)
        return 1
    return _run_ingest(settings, directory=directory)


def _cmd_eval(_args: argparse.Namespace) -> int:
    return _stub("eval")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="adoc", description="a-doc CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="validate configuration and models.yaml").set_defaults(
        func=_cmd_init
    )
    subparsers.add_parser("onboard", help="run the onboarding intake wizard").set_defaults(
        func=_cmd_onboard
    )
    subparsers.add_parser("ingest", help="run the document ingestion pipeline").set_defaults(
        func=_cmd_ingest
    )
    subparsers.add_parser("review", help="run the weekly deep review").set_defaults(
        func=_cmd_review
    )
    serve_parser = subparsers.add_parser("serve", help="run the web UI")
    serve_parser.add_argument("--host", default="127.0.0.1", help="bind host (default: 127.0.0.1)")
    serve_parser.add_argument("--port", type=int, default=8080, help="bind port (default: 8080)")
    serve_parser.set_defaults(func=_cmd_serve)
    backfill_parser = subparsers.add_parser("backfill", help="backfill historical documents")
    backfill_parser.add_argument("directory", help="directory of documents to ingest")
    backfill_parser.set_defaults(func=_cmd_backfill)
    subparsers.add_parser("eval", help="run the self-evaluation benchmark suite").set_defaults(
        func=_cmd_eval
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    sys.exit(main())
