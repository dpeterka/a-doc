"""adoc command-line entrypoint.

Subcommands mirror PLAN.md's phasing: `init` does real work (validates
Settings/models.yaml, then creates the data-repo layout via
`DataRepo.init_at`); `ingest` (scan `<data_dir>/inbox/`, optional
`--reason` post-ingest reasoning pass) and `backfill <directory>` are
wired to the real `ingest.pipeline` with a real `LlmClient`/`VisionClient`
(`_build_llm_client`/`_build_vision_client` are the seams tests override
with fakes); `review` runs the real weekly deep review
(`reason.review.run_weekly_review`); `eval` runs the offline self-eval
suites (`evals.runner`); `serve` builds the real `web.app.create_app()`
and runs it under uvicorn (`_run_uvicorn` is the seam tests override so a
test run never actually binds a socket). `onboard` is wired to the real
intake wizard. `user add|list|remove` manages the web login credential
store (`web.users`) — `add`'s password prompts go through `_getpass`, a
seam tests override so a test run never blocks on stdin. `backup` runs
`adoc.backup.run_backup` against a real S3 client (`_build_s3_client` is
the seam tests override with a fake); `restore` runs its inverse,
`adoc.backup.restore_from_bucket`, against the same seam. `bootstrap-data`
is the decide-and-run command `docker-entrypoint.sh` delegates to at
container start (restore if `ADOC_BACKUP_BUCKET` is set and a backup
exists, `init` otherwise) — kept in Python rather than shell so the
decision logic is unit-testable. No stubs remain.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI

from adoc.backup import (
    BackupError,
    NoBackupError,
    RestoreError,
    S3Client,
    build_s3_client,
    restore_from_bucket,
    run_backup,
)
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
from adoc.labs.recanonicalize import recanonicalize_rows
from adoc.labs.reclassify import reclassify_pending
from adoc.labs.specimen import infer_unknown_specimens
from adoc.labs.twins import sweep_twins, write_sweep_summary
from adoc.privacy import Scrubber
from adoc.reason.client import LlmClient, LlmError
from adoc.reason.review import run_weekly_review
from adoc.reason.stages import render_new_evidence_note, run_post_ingest_dag
from adoc.web.users import USERS_RELPATH, add_user, list_users, remove_user


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


def _getpass(prompt: str) -> str:  # pragma: no cover - exercises the real terminal
    """Real wiring for `adoc user add`'s password prompts. Overridden by
    tests (`monkeypatch.setattr(cli, "_getpass", ...)`) so a test run never
    blocks on stdin."""
    import getpass

    return getpass.getpass(prompt)


def _users_path(settings: Settings) -> Path:
    return settings.data_dir / USERS_RELPATH


def _cmd_user_add(args: argparse.Namespace) -> int:
    try:
        settings = Settings()
    except Exception as exc:  # noqa: BLE001 - surface any config error to the user
        print(f"user add: configuration error: {exc}", file=sys.stderr)
        return 1

    password = _getpass("Password: ")
    confirm = _getpass("Confirm password: ")
    if password != confirm:
        print("user add: passwords did not match", file=sys.stderr)
        return 1
    if not password:
        print("user add: password must not be empty", file=sys.stderr)
        return 1

    add_user(_users_path(settings), args.username, password)
    print(f"user add: added user {args.username!r}")
    return 0


def _cmd_user_list(_args: argparse.Namespace) -> int:
    try:
        settings = Settings()
    except Exception as exc:  # noqa: BLE001 - surface any config error to the user
        print(f"user list: configuration error: {exc}", file=sys.stderr)
        return 1

    users = list_users(_users_path(settings))
    if not users:
        print("user list: no users configured")
        return 0
    for username in users:
        print(username)
    return 0


def _cmd_user_remove(args: argparse.Namespace) -> int:
    try:
        settings = Settings()
    except Exception as exc:  # noqa: BLE001 - surface any config error to the user
        print(f"user remove: configuration error: {exc}", file=sys.stderr)
        return 1

    if remove_user(_users_path(settings), args.username):
        print(f"user remove: removed user {args.username!r}")
        return 0
    print(f"user remove: no such user {args.username!r}", file=sys.stderr)
    return 1


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


def _build_s3_client() -> S3Client:
    """Real wiring for `adoc backup`. Overridden by tests to inject a fake
    S3 client so a test run never talks to AWS."""
    return build_s3_client()


def _cmd_backup(_args: argparse.Namespace) -> int:
    try:
        settings = Settings()
    except Exception as exc:  # noqa: BLE001 - surface any config error to the user
        print(f"backup: configuration error: {exc}", file=sys.stderr)
        return 1

    repo = DataRepo(settings.data_dir)
    if not repo.is_initialized:
        print(
            f"backup: data repo not initialized at {settings.data_dir} - run `adoc init` first",
            file=sys.stderr,
        )
        return 1

    try:
        report = run_backup(settings.data_dir, settings.backup_bucket or "", _build_s3_client())
    except BackupError as exc:
        print(f"backup: {exc}", file=sys.stderr)
        return 1

    print(f"backup: bundle uploaded to s3://{report.bucket}/latest/a-doc-data.bundle")
    print(
        f"backup: sources/ - {report.sources_uploaded} uploaded, {report.sources_skipped} unchanged"
    )
    print(f"backup: labs-export.jsonl uploaded: {report.jsonl_uploaded}")
    return 0


def _cmd_restore(args: argparse.Namespace) -> int:
    try:
        settings = Settings()
    except Exception as exc:  # noqa: BLE001 - surface any config error to the user
        print(f"restore: configuration error: {exc}", file=sys.stderr)
        return 1

    bucket = args.bucket or settings.backup_bucket or ""
    try:
        report = restore_from_bucket(
            bucket,
            settings.data_dir,
            s3_client=_build_s3_client(),
            sqlite_journal_mode=settings.sqlite_journal_mode,
        )
    except RestoreError as exc:
        print(f"restore: {exc}", file=sys.stderr)
        return 1

    print(f"restore: cloned bundle (commit {report.bundle_commit_sha[:12]}) into {report.data_dir}")
    print(f"restore: sources/ - {report.sources_restored} restored")
    print(f"restore: labs.sqlite rebuilt from labs-export.jsonl - {report.lab_rows_rebuilt} rows")
    for warning in report.warnings:
        print(f"restore: warning: {warning}")
    return 0


def _cmd_bootstrap_data(_args: argparse.Namespace) -> int:
    """The decide-and-run logic `docker-entrypoint.sh` used to guess at in
    shell: on a missing/empty data dir, restore from `ADOC_BACKUP_BUCKET`
    if one is configured, falling back to `adoc init` only when the
    bucket genuinely has no backup yet (`NoBackupError`) — any other
    failure (bad credentials, a network hiccup, a real S3 error) must
    fail loudly rather than silently initializing an empty case file over
    what might just be a transient problem reaching a real backup. A data
    dir that already has *something* in it is left untouched either way
    (matches the entrypoint's prior `[ -z "$(ls -A ...)" ]` check).
    """
    try:
        settings = Settings()
    except Exception as exc:  # noqa: BLE001 - surface any config error to the user
        print(f"bootstrap-data: configuration error: {exc}", file=sys.stderr)
        return 1

    data_dir = settings.data_dir
    if data_dir.is_dir() and any(data_dir.iterdir()):
        print(f"bootstrap-data: {data_dir} already has data; nothing to do")
        return 0

    if settings.backup_bucket:
        try:
            report = restore_from_bucket(
                settings.backup_bucket,
                data_dir,
                s3_client=_build_s3_client(),
                sqlite_journal_mode=settings.sqlite_journal_mode,
            )
        except NoBackupError as exc:
            print(f"bootstrap-data: {exc}; falling back to 'adoc init'")
        except Exception as exc:  # noqa: BLE001 - a real error must fail loudly, never silent-init
            print(f"bootstrap-data: restore failed: {exc}", file=sys.stderr)
            return 1
        else:
            print(
                f"bootstrap-data: restored from s3://{report.bucket}/latest/ "
                f"(commit {report.bundle_commit_sha[:12]}, {report.lab_rows_rebuilt} lab rows)"
            )
            for warning in report.warnings:
                print(f"bootstrap-data: warning: {warning}")
            return 0

    try:
        already_initialized = DataRepo(data_dir).is_initialized
        DataRepo.init_at(data_dir)
    except OSError as exc:
        print(f"bootstrap-data: cannot create data dir: {exc}", file=sys.stderr)
        return 1
    if already_initialized:
        print(f"bootstrap-data: data repo already initialized at {data_dir}")
    else:
        print(f"bootstrap-data: initialized data repo at {data_dir}")
    return 0


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
    with LabsDb(db_path, journal_mode=settings.sqlite_journal_mode) as db:
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


def _cmd_labs_infer_specimen(_args: argparse.Namespace) -> int:
    """Deterministic maintenance pass (`labs/specimen.py`; NO LLM calls):
    back-fill `specimen` for existing rows still at the pre-migration
    default `"unknown"`, from their source document's filename/doc_type
    keywords only. Idempotent - a row it updates simply won't be
    `"unknown"` on the next run.
    """
    try:
        settings = Settings()
    except Exception as exc:  # noqa: BLE001 - surface any config error to the user
        print(f"labs-infer-specimen: configuration error: {exc}", file=sys.stderr)
        return 1

    repo = DataRepo(settings.data_dir)
    if not repo.is_initialized:
        print(
            f"labs-infer-specimen: data repo not initialized at {settings.data_dir} "
            "- run `adoc init` first",
            file=sys.stderr,
        )
        return 1

    db_path = settings.data_dir / "labs.sqlite"
    with LabsDb(db_path, journal_mode=settings.sqlite_journal_mode) as db:
        report = infer_unknown_specimens(db)
        if report.updated:
            db.export_jsonl(repo.root / "labs-export.jsonl")
            breakdown = ", ".join(f"{k}={v}" for k, v in sorted(report.by_specimen.items()))
            repo.commit(
                f"labs: inferred specimen for {report.updated} row(s) ({breakdown})",
                paths=["labs-export.jsonl"],
            )

    print(f"labs-infer-specimen: updated {report.updated} row(s)")
    for specimen, count in sorted(report.by_specimen.items()):
        print(f"  - {specimen}: {count}")
    if report.skipped_serum_panel:
        print(
            f"labs-infer-specimen: {report.skipped_serum_panel} row(s) left unknown "
            "(serum-panel analyte)"
        )
    if report.mixed_panel_docs:
        print(
            f"labs-infer-specimen: {len(report.mixed_panel_docs)} document(s) "
            "mixed panel - left unknown"
        )
    print(f"labs-infer-specimen: {report.remaining_unknown} row(s) remain unknown")
    return 0


def _cmd_labs_dedupe_twins(args: argparse.Namespace) -> int:
    """Queue-ergonomics slice item 4: sweep legacy single-pass PENDING rows
    for a duplicate ("twin") already-resolved row in the same document and
    auto-reject the duplicate half (`labs/twins.py`'s `sweep_twins`).
    `--dry-run` computes and reports the same thing without mutating
    anything - no rejects, no export, no commit, no summary write.
    """
    try:
        settings = Settings()
    except Exception as exc:  # noqa: BLE001 - surface any config error to the user
        print(f"labs-dedupe-twins: configuration error: {exc}", file=sys.stderr)
        return 1

    repo = DataRepo(settings.data_dir)
    if not repo.is_initialized:
        print(
            f"labs-dedupe-twins: data repo not initialized at {settings.data_dir} "
            "- run `adoc init` first",
            file=sys.stderr,
        )
        return 1

    try:
        client = _build_llm_client(settings)
    except Exception as exc:  # noqa: BLE001 - surface any config error to the user
        print(f"labs-dedupe-twins: configuration error: {exc}", file=sys.stderr)
        return 1

    db_path = settings.data_dir / "labs.sqlite"
    with LabsDb(db_path, journal_mode=settings.sqlite_journal_mode) as db:
        report = sweep_twins(db, client, dry_run=args.dry_run)

        if args.dry_run:
            print(f"labs-dedupe-twins: dry-run - checked {report.checked} single_pass row(s)")
            print(
                f"labs-dedupe-twins: dry-run - would reject {report.rejected} "
                f"({report.rejected_rule} rule, {report.rejected_llm} llm)"
            )
            return 0

        if report.rejected:
            db.export_jsonl(repo.root / "labs-export.jsonl")
            repo.commit(
                f"labs: auto-rejected {report.rejected} twin row(s) as duplicates "
                f"({report.rejected_rule} rule, {report.rejected_llm} llm)",
                paths=["labs-export.jsonl"],
            )
        write_sweep_summary(repo.root, report, at=datetime.now(UTC))

    print(f"labs-dedupe-twins: checked {report.checked} single_pass row(s)")
    print(
        f"labs-dedupe-twins: rejected {report.rejected} "
        f"({report.rejected_rule} rule, {report.rejected_llm} llm)"
    )
    return 0


def _cmd_labs_reclassify(args: argparse.Namespace) -> int:
    """Deterministic maintenance pass (`labs/reclassify.py`; NO LLM calls):
    recompute every still-PENDING matched-pair row's reasons under the
    current semantic comparators (`ingest.reconcile`'s
    `ref_ranges_equivalent`/`units_equivalent`/`flags_equivalent`) and the
    current `labs.sqlite`/`ANALYTE_SPECS` - flips a row whose reasons all
    turn out to be comparator false positives to `auto`, and rewrites
    still-PENDING rows' reason lists so confirm-queue bucketing reflects
    the current comparators. Idempotent - see `reclassify_pending`'s
    docstring.
    """
    try:
        settings = Settings()
    except Exception as exc:  # noqa: BLE001 - surface any config error to the user
        print(f"labs-reclassify: configuration error: {exc}", file=sys.stderr)
        return 1

    repo = DataRepo(settings.data_dir)
    if not repo.is_initialized:
        print(
            f"labs-reclassify: data repo not initialized at {settings.data_dir} "
            "- run `adoc init` first",
            file=sys.stderr,
        )
        return 1

    db_path = settings.data_dir / "labs.sqlite"
    with LabsDb(db_path, journal_mode=settings.sqlite_journal_mode) as db:
        report = reclassify_pending(db, dry_run=args.dry_run)

        if args.dry_run:
            print(f"labs-reclassify: dry-run - checked {report.checked} row(s)")
            print(
                f"labs-reclassify: dry-run - would auto-flip {report.auto_flipped}, "
                f"re-bucket {report.rebucketed_agreed} agreed, "
                f"leave {report.still_disagreed} disagreed"
            )
            return 0

        if report.rewritten:
            db.export_jsonl(repo.root / "labs-export.jsonl")
            repo.commit(
                f"labs: reclassified {report.rewritten} pending row(s) under the current "
                f"comparators (auto={report.auto_flipped}, agreed={report.rebucketed_agreed}, "
                f"disagreed={report.still_disagreed})",
                paths=["labs-export.jsonl"],
            )

    print(f"labs-reclassify: checked {report.checked} row(s)")
    print(f"labs-reclassify: auto-flipped {report.auto_flipped}")
    print(f"labs-reclassify: re-bucketed agreed {report.rebucketed_agreed}")
    print(f"labs-reclassify: still disagreed {report.still_disagreed}")
    return 0


def _cmd_labs_recanonicalize(args: argparse.Namespace) -> int:
    """Deterministic maintenance pass (`labs/recanonicalize.py`; NO LLM
    calls): re-run `labs.validate.canonicalize` against every non-rejected
    row's current spec table (`ANALYTE_SPECS` has grown well past what
    existed when many rows were first ingested) and update `name` wherever
    it now resolves differently - renaming outright, merging an exact-
    reading duplicate, or queuing a differing-reading collision for human
    review. `--dry-run` computes and reports the same counts without
    mutating anything - no renames, no rejects, no export, no commit.
    """
    try:
        settings = Settings()
    except Exception as exc:  # noqa: BLE001 - surface any config error to the user
        print(f"labs-recanonicalize: configuration error: {exc}", file=sys.stderr)
        return 1

    repo = DataRepo(settings.data_dir)
    if not repo.is_initialized:
        print(
            f"labs-recanonicalize: data repo not initialized at {settings.data_dir} "
            "- run `adoc init` first",
            file=sys.stderr,
        )
        return 1

    db_path = settings.data_dir / "labs.sqlite"
    with LabsDb(db_path, journal_mode=settings.sqlite_journal_mode) as db:
        report = recanonicalize_rows(db, dry_run=args.dry_run)

        if args.dry_run:
            print(f"labs-recanonicalize: dry-run - checked {report.checked} row(s)")
            print(
                f"labs-recanonicalize: dry-run - would rename {report.renamed}, "
                f"merge {report.merged_duplicates} duplicate(s), "
                f"queue {report.conflicts_queued} conflict(s), "
                f"leave {report.untouched} untouched, "
                f"block {report.blocked_by_tombstone} on rejected-row key(s)"
            )
            return 0

        changed = report.renamed + report.merged_duplicates + report.conflicts_queued
        if changed:
            db.export_jsonl(repo.root / "labs-export.jsonl")
            repo.commit(
                f"labs: recanonicalized {changed} row(s) "
                f"(renamed={report.renamed}, merged={report.merged_duplicates}, "
                f"conflicts_queued={report.conflicts_queued})",
                paths=["labs-export.jsonl"],
            )

    print(f"labs-recanonicalize: checked {report.checked} row(s)")
    print(f"labs-recanonicalize: renamed {report.renamed}")
    print(f"labs-recanonicalize: merged {report.merged_duplicates} duplicate(s)")
    print(f"labs-recanonicalize: queued {report.conflicts_queued} conflict(s)")
    print(f"labs-recanonicalize: left {report.untouched} untouched")
    if report.blocked_by_tombstone:
        print(
            f"labs-recanonicalize: {report.blocked_by_tombstone} rename(s) blocked - "
            "target key held by a rejected row (stored name kept)"
        )
    return 0


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
    with LabsDb(db_path, journal_mode=settings.sqlite_journal_mode) as db:
        report = run_weekly_review(repo, db, client)

    print(
        f"review: {report.review_date.isoformat()} — ledger {report.ledger_version_before} "
        f"-> {report.ledger_version_after}"
    )
    print(f"review: committed {report.commit_sha[:12]}, tagged {report.tag}")
    print(f"review: report written to {report.markdown_path}")
    return 0


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
    subparsers.add_parser(
        "labs-infer-specimen",
        help=(
            "deterministically back-fill specimen for existing 'unknown' rows from their "
            "source document's filename/doc_type keywords (no LLM calls)"
        ),
    ).set_defaults(func=_cmd_labs_infer_specimen)
    labs_dedupe_twins_parser = subparsers.add_parser(
        "labs-dedupe-twins",
        help=(
            "sweep legacy single-pass PENDING rows for a duplicate already-resolved row in "
            "the same document and auto-reject the duplicate half"
        ),
    )
    labs_dedupe_twins_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what the sweep would do without mutating anything",
    )
    labs_dedupe_twins_parser.set_defaults(func=_cmd_labs_dedupe_twins)
    labs_reclassify_parser = subparsers.add_parser(
        "labs-reclassify",
        help=(
            "recompute still-PENDING rows' reasons under the current semantic reconcile "
            "comparators, flipping comparator false-positive mismatches to auto (no LLM calls)"
        ),
    )
    labs_reclassify_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what the sweep would do without mutating anything",
    )
    labs_reclassify_parser.set_defaults(func=_cmd_labs_reclassify)
    labs_recanonicalize_parser = subparsers.add_parser(
        "labs-recanonicalize",
        help=(
            "re-run canonical_rename_target() against every non-rejected row's current spec "
            "table, renaming/merging/queuing rows whose EXACT-alias canonical name has "
            "changed (no LLM calls)"
        ),
    )
    labs_recanonicalize_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what the sweep would do without mutating anything",
    )
    labs_recanonicalize_parser.set_defaults(func=_cmd_labs_recanonicalize)
    subparsers.add_parser(
        "backup",
        help="git-bundle the data repo and upload it (+ sources/, labs-export.jsonl) to S3",
    ).set_defaults(func=_cmd_backup)
    restore_parser = subparsers.add_parser(
        "restore",
        help="seed an uninitialized data repo from an S3 backup (the inverse of `adoc backup`)",
    )
    restore_parser.add_argument(
        "--bucket",
        default=None,
        help="backup bucket name (default: ADOC_BACKUP_BUCKET / Settings.backup_bucket)",
    )
    restore_parser.set_defaults(func=_cmd_restore)
    subparsers.add_parser(
        "bootstrap-data",
        help=(
            "restore from ADOC_BACKUP_BUCKET if set and a backup exists, else `adoc init` "
            "(what docker-entrypoint.sh runs on container start)"
        ),
    ).set_defaults(func=_cmd_bootstrap_data)
    serve_parser = subparsers.add_parser("serve", help="run the web UI")
    serve_parser.add_argument("--host", default="127.0.0.1", help="bind host (default: 127.0.0.1)")
    serve_parser.add_argument("--port", type=int, default=8080, help="bind port (default: 8080)")
    serve_parser.set_defaults(func=_cmd_serve)
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

    user_parser = subparsers.add_parser("user", help="manage web login users")
    user_subparsers = user_parser.add_subparsers(dest="user_command", required=True)
    user_add_parser = user_subparsers.add_parser(
        "add", help="add (or reset the password of) a web login user"
    )
    user_add_parser.add_argument("username")
    user_add_parser.set_defaults(func=_cmd_user_add)
    user_list_parser = user_subparsers.add_parser("list", help="list web login usernames")
    user_list_parser.set_defaults(func=_cmd_user_list)
    user_remove_parser = user_subparsers.add_parser("remove", help="remove a web login user")
    user_remove_parser.add_argument("username")
    user_remove_parser.set_defaults(func=_cmd_user_remove)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    sys.exit(main())
