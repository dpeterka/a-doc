"""adoc command-line entrypoint.

Subcommands mirror PLAN.md's phasing: `init` does real work (validates
Settings/models.yaml, then creates the data-repo layout via
`DataRepo.init_at`); `ingest` (scan `<data_dir>/inbox/`, optional
`--reason` post-ingest reasoning pass) and `backfill <directory>` are
wired to the real `ingest.pipeline` with a real `LlmClient`/`VisionClient`
(`_build_llm_client`/`_build_vision_client` are the seams tests override
with fakes); `review` runs the real weekly deep review
(`reason.review.run_weekly_review`); `eval` runs the offline self-eval
suites (`evals.runner`). `onboard` is likewise real; `serve` remains a
stubbed scaffold placeholder for a later slice.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from adoc.casefile.repo import LEDGER_RELPATH, DataRepo
from adoc.config import Settings, load_model_bindings
from adoc.evals.report import write_comparison_report, write_report
from adoc.evals.runner import known_suites, run_suite
from adoc.ingest.archive import PageRenderer, pdftoppm_renderer
from adoc.ingest.pipeline import IngestReport, ingest_directory, ingest_inbox
from adoc.ingest.vision import VisionClient
from adoc.intake.cli import run_onboarding_session
from adoc.intake.wizard import IntakeWizard
from adoc.labs.db import LabsDb
from adoc.privacy import Scrubber
from adoc.reason.client import LlmClient, LlmError
from adoc.reason.review import run_weekly_review
from adoc.reason.stages import render_new_evidence_note, run_post_ingest_dag


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


def _run_ingest(settings: Settings, directory: Path | None, *, reason: bool = False) -> int:
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

        if reason:
            evidence_note = render_new_evidence_note(report)
            if evidence_note is None:
                print("ingest: --reason given but no rows were added; skipping reasoning pass")
            else:
                ledger_path = repo.root / LEDGER_RELPATH
                new_ledger = run_post_ingest_dag(llm_client, repo, db, ledger_path, evidence_note)
                print(f"ingest: reasoning pass updated the ledger to version {new_ledger.version}")

    _print_ingest_report(report)
    return 1 if any(f.outcome == "error" for f in report.files) else 0


def _cmd_ingest(args: argparse.Namespace) -> int:
    try:
        settings = Settings()
    except Exception as exc:  # noqa: BLE001 - surface any config error to the user
        print(f"ingest: configuration error: {exc}", file=sys.stderr)
        return 1
    return _run_ingest(settings, directory=None, reason=args.reason)


def _cmd_review(_args: argparse.Namespace) -> int:
    try:
        settings = Settings()
    except Exception as exc:  # noqa: BLE001 - surface any config error to the user
        print(f"review: configuration error: {exc}", file=sys.stderr)
        return 1

    repo = DataRepo(settings.data_dir)
    if not repo.is_initialized:
        print(
            f"review: data repo not initialized at {settings.data_dir} - run `adoc init` first",
            file=sys.stderr,
        )
        return 1

    try:
        client = _build_llm_client(settings)
    except Exception as exc:  # noqa: BLE001 - surface any config error to the user
        print(f"review: configuration error: {exc}", file=sys.stderr)
        return 1

    db_path = settings.data_dir / "labs.sqlite"
    with LabsDb(db_path) as db:
        report = run_weekly_review(repo, db, client)

    print(
        f"review: {report.review_date.isoformat()} — ledger {report.ledger_version_before} "
        f"-> {report.ledger_version_after}"
    )
    print(f"review: committed {report.commit_sha[:12]}, tagged {report.tag}")
    print(f"review: report written to {report.markdown_path}")
    return 0


def _cmd_serve(_args: argparse.Namespace) -> int:
    return _stub("serve")


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


def _eval_out_dir(explicit: str | None) -> Path:
    """`--out`, if given; otherwise `<data_dir>/work/eval` when a
    configured data repo is available, else `./work/eval` (PLAN.md's
    `eval.yml` CI run always passes `--out` explicitly, so it never
    depends on `Settings`/`ADOC_DATA_DIR` being set at all)."""
    if explicit is not None:
        return Path(explicit)
    try:
        settings = Settings()
    except Exception:  # noqa: BLE001 - fall back rather than fail eval on missing config
        return Path("work") / "eval"
    return settings.data_dir / "work" / "eval"


def _eval_client_factory() -> LlmClient:
    """Both current suites (`extraction`, `redteam`) never call the client
    this builds — see `evals.runner`'s module docstring — so this raises
    rather than silently returning something misleading if a future suite
    ever does call it without a real one wired in.
    """
    raise LlmError("no real LlmClient is wired into `adoc eval` for this suite")


def _cmd_eval(args: argparse.Namespace) -> int:
    suite_names: list[str] = args.suites or known_suites()
    out_dir = _eval_out_dir(args.out)

    all_passed = True
    for name in sorted(suite_names):
        incumbent = run_suite(name, client_factory=_eval_client_factory)
        write_report(incumbent, out_dir)
        print(f"eval: {name}: {incumbent.pass_rate:.0%} pass rate ({len(incumbent.cases)} cases)")
        for case in incumbent.cases:
            if not case.passed:
                print(f"  eval: {name}: FAIL {case.case_id}: {case.detail}")
        all_passed = all_passed and incumbent.passed

        if args.candidate:
            candidate = run_suite(
                name, client_factory=_eval_client_factory, candidate=args.candidate
            )
            write_comparison_report(incumbent, candidate, out_dir)
            print(f"eval: {name}: comparison report written for candidate {args.candidate}")

    print(f"eval: reports written to {out_dir}")
    return 0 if all_passed else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="adoc", description="a-doc CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="validate configuration and models.yaml").set_defaults(
        func=_cmd_init
    )
    subparsers.add_parser("onboard", help="run the onboarding intake wizard").set_defaults(
        func=_cmd_onboard
    )
    ingest_parser = subparsers.add_parser("ingest", help="run the document ingestion pipeline")
    ingest_parser.add_argument(
        "--reason",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "run the diagnostic reasoning DAG over newly-ingested evidence to update the "
            "ledger (default: --no-reason)"
        ),
    )
    ingest_parser.set_defaults(func=_cmd_ingest)
    subparsers.add_parser("review", help="run the weekly deep review").set_defaults(
        func=_cmd_review
    )
    subparsers.add_parser("serve", help="run the web UI").set_defaults(func=_cmd_serve)
    backfill_parser = subparsers.add_parser("backfill", help="backfill historical documents")
    backfill_parser.add_argument("directory", help="directory of documents to ingest")
    backfill_parser.set_defaults(func=_cmd_backfill)
    eval_parser = subparsers.add_parser("eval", help="run the self-evaluation benchmark suite")
    eval_parser.add_argument(
        "--suite",
        action="append",
        dest="suites",
        choices=known_suites(),
        help="suite to run (repeatable; default: all known suites)",
    )
    eval_parser.add_argument(
        "--candidate",
        default=None,
        help="provider:model to compare against the incumbent binding",
    )
    eval_parser.add_argument(
        "--out",
        default=None,
        help="output directory for the report (default: <data_dir>/work/eval)",
    )
    eval_parser.set_defaults(func=_cmd_eval)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    sys.exit(main())
