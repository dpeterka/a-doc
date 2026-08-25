"""Local-dev maintenance operations, invoked by scripts/local-env.sh.

Not part of the `adoc` package — run directly via
`uv run python scripts/local_dev_ops.py <reindex|intake-reset>` against
whatever `ADOC_DATA_DIR` the caller (scripts/local-env.sh) has already
exported. Never reads or writes `ADOC_SAFE_STORE` itself; the caller is
responsible for ensuring `ADOC_DATA_DIR` is a working copy, not the safe
store, before invoking this.

`reindex` rebuilds derived state without re-ingesting: `labs.sqlite` from
the committed `labs-export.jsonl`, and the `document_text`/
`document_text_fts` tables from the committed `doc-text/*.txt` files
(mirrors what `adoc restore` does after cloning a backup — see
`LabsDb.rebuild_from_jsonl` and `ingest.doctext.rebuild_document_text_from_files`).

`intake-reset` resets intake state so the next `/chat` turn is a fresh
initial visit: clears `case/intake-facts.yaml`, `case/intake-state.yaml`,
`case/intake-transcript.jsonl`, deletes onboarding-only artifacts that
don't exist at the data repo's root commit (`case/patient-theories.md`,
`case/undated-events.md`), restores the 5 onboarding-derived case files
(`case/case-summary.md`, `case/questions-open.md`, `case/family-history.md`,
`case/medications.md`, `case/care-team.md` — the same set `DataRepo.init_at`
writes as stubs, `casefile.repo._PLACEHOLDER_FILES`) to their content at the
repo's root commit, and clears `logs/chat` (already gitignored, so not part
of the commit). Deliberately never touches `sources/`, `doc-text/`,
`labs.sqlite`/`labs-export.jsonl`, `case/encounters/`, the differential
ledger, or `work/users.yaml`. Commits the result (only if something
actually changed) with its own git plumbing rather than
`DataRepo.commit()`, because that helper's `index.add(paths)` can't stage a
deletion — this needs `git add -A` semantics for the paths it touches.
"""

from __future__ import annotations

import argparse
import shutil
import sys

from git import Actor, Repo

from adoc.casefile.repo import _PLACEHOLDER_FILES, DataRepo
from adoc.config import Settings
from adoc.ingest.doctext import rebuild_document_text_from_files
from adoc.intake.agent import INTAKE_TRANSCRIPT_RELPATH
from adoc.intake.facts import INTAKE_FACTS_RELPATH
from adoc.intake.wizard import INTAKE_STATE_RELPATH
from adoc.labs.db import LabsDb

_ONBOARDING_ONLY_RELPATHS = (
    "case/patient-theories.md",
    "case/undated-events.md",
    INTAKE_FACTS_RELPATH,
    INTAKE_STATE_RELPATH,
    INTAKE_TRANSCRIPT_RELPATH,
)


def _load_settings() -> Settings | None:
    try:
        return Settings()
    except Exception as exc:  # noqa: BLE001 - surface any config error to the caller
        print(f"local_dev_ops: configuration error: {exc}", file=sys.stderr)
        return None


def cmd_reindex(_args: argparse.Namespace) -> int:
    settings = _load_settings()
    if settings is None:
        return 1
    data_dir = settings.data_dir
    repo = DataRepo(data_dir)
    if not repo.is_initialized:
        print(f"reindex: data repo not initialized at {data_dir}", file=sys.stderr)
        return 1

    jsonl_path = data_dir / "labs-export.jsonl"
    db = LabsDb(data_dir / "labs.sqlite", journal_mode=settings.sqlite_journal_mode)
    try:
        if jsonl_path.exists():
            db.rebuild_from_jsonl(jsonl_path)
            labs_source = "labs-export.jsonl"
        else:
            labs_source = "(no labs-export.jsonl found; labs.sqlite left as-is)"
        documents = len(db.list_documents())
        lab_rows = len(db.all_non_rejected_rows())
        doc_text_docs = rebuild_document_text_from_files(db, data_dir)
    finally:
        db.close()

    print(f"REPORT: reindex: labs.sqlite rebuilt from {labs_source}")
    print(
        f"REPORT: reindex: documents={documents} lab_rows={lab_rows} "
        f"doc_text_documents_repopulated={doc_text_docs}"
    )
    return 0


def cmd_intake_reset(_args: argparse.Namespace) -> int:
    settings = _load_settings()
    if settings is None:
        return 1
    data_dir = settings.data_dir
    repo = DataRepo(data_dir)
    if not repo.is_initialized:
        print(f"intake-reset: data repo not initialized at {data_dir}", file=sys.stderr)
        return 1

    git_repo = Repo(str(data_dir))
    try:
        root_commit = next(git_repo.iter_commits(reverse=True))
    except StopIteration:
        print("intake-reset: data repo has no commits", file=sys.stderr)
        return 1

    changed: set[str] = set()

    for relpath in sorted(_PLACEHOLDER_FILES):
        try:
            blob = root_commit.tree / relpath
        except KeyError:
            continue
        content = blob.data_stream.read().decode("utf-8")
        path = data_dir / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.read_text(encoding="utf-8") != content:
            path.write_text(content, encoding="utf-8")
        changed.add(relpath)

    for relpath in _ONBOARDING_ONLY_RELPATHS:
        path = data_dir / relpath
        if path.exists():
            path.unlink()
            changed.add(relpath)

    chat_log_dir = data_dir / "logs" / "chat"
    chat_log_removed = chat_log_dir.exists()
    if chat_log_removed:
        shutil.rmtree(chat_log_dir)

    committed = False
    if changed:
        git_repo.git.add("-A", "--", *sorted(changed))
        staged = git_repo.index.diff(git_repo.head.commit)
        if staged:
            actor = Actor("adoc", "adoc@localhost")
            git_repo.index.commit(
                "chore: reset intake for a fresh initial visit (scripts/local-env.sh --intake)",
                author=actor,
                committer=actor,
            )
            committed = True

    print(f"REPORT: intake-reset: touched {len(changed)} path(s): {sorted(changed)}")
    print(f"REPORT: intake-reset: logs/chat removed: {chat_log_removed}")
    print(f"REPORT: intake-reset: committed: {committed}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="local_dev_ops")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser(
        "reindex", help="rebuild labs.sqlite + document-text index without re-ingesting"
    ).set_defaults(func=cmd_reindex)
    subparsers.add_parser(
        "intake-reset", help="reset intake state for a fresh initial visit"
    ).set_defaults(func=cmd_intake_reset)
    args = parser.parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
