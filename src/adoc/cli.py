"""adoc command-line entrypoint.

Subcommands mirror PLAN.md's phasing: `init` does real work now (it
validates that Settings and models.yaml load cleanly, then creates the
data-repo layout via `DataRepo.init_at` — idempotent, safe to re-run);
everything else (`onboard`, `ingest`, `review`, `serve`, `backfill`, `eval`)
is Phase-1+ functionality and is stubbed here as a scaffold placeholder.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from adoc.casefile.repo import DataRepo
from adoc.config import Settings, load_model_bindings
from adoc.intake.cli import run_onboarding_session
from adoc.intake.wizard import IntakeWizard
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


def _cmd_ingest(_args: argparse.Namespace) -> int:
    return _stub("ingest")


def _cmd_review(_args: argparse.Namespace) -> int:
    return _stub("review")


def _cmd_serve(_args: argparse.Namespace) -> int:
    return _stub("serve")


def _cmd_backfill(_args: argparse.Namespace) -> int:
    return _stub("backfill")


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
    subparsers.add_parser("serve", help="run the web UI").set_defaults(func=_cmd_serve)
    subparsers.add_parser("backfill", help="backfill historical documents").set_defaults(
        func=_cmd_backfill
    )
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
