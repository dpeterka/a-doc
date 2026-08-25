"""Git plumbing for the data repo (PLAN.md "State": "Two git repos: code vs data").

`DataRepo` never performs a remote operation — the data repo has no remote
by design (CLAUDE.md PHI boundary rule 1). Every mutation to the data repo
is meant to be one commit; `commit()` is the only write-and-record primitive
other modules should use.

`apply_ledger_diff()` is the ONLY safe way for a running DAG stage (or any
other concurrent caller) to persist a `LedgerDiff` to `differential-
ledger.yaml` — see its docstring for the durability defect it fixes
(CONFIRMED: a diagnostic-turn ledger write could be silently lost, and was
never committed until the weekly review swept it up, up to a week later).
`casefile.ledger.apply_and_save` remains the lock-free load-apply-save-
append primitive this method calls; calling it directly bypasses both the
lock and the commit, and is safe only for single-threaded, non-concurrent
callers (tests, and this method itself) — every real DAG/route caller must
go through `apply_ledger_diff` instead.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from git import Actor, Repo
from git.exc import GitCommandError

from adoc.casefile.ledger import apply_and_save, save_ledger
from adoc.casefile.schema import Ledger, LedgerDiff

logger = logging.getLogger(__name__)

_COMMIT_ACTOR = Actor("adoc", "adoc@localhost")

# Bounded retry-with-backoff for cross-process git index/ref-lock
# contention on `commit()` (moved here from `ingest/pipeline.py`'s
# `_commit_with_retry`, CONFIRMED bug fix: this data repo is shared, on
# EFS, by the web task's `DataRepo._lock`-serialized THREADS and by
# separate scheduled-task OS PROCESSES (ingest/review/backup) — an
# in-process `threading.RLock` cannot serialize across processes, so two
# processes committing at the same moment can collide on git's own
# index/ref lock files. Every caller of `commit()` benefits now, not just
# ingest.
_COMMIT_RETRY_ATTEMPTS = 4
_COMMIT_RETRY_BASE_DELAY_SECONDS = 0.25

_CASE_SUBDIRS = ("encounters", "reviews")
_TOP_LEVEL_DIRS = ("sources", "doc-text", "inbox", "work", "logs")
"""`doc-text/` (docs/adr/0015-document-text-corpus.md) holds one committed,
human-diffable `<sha256>.txt` per ingested non-genomic document's extracted
full text — derived from `sources/`, but its own top-level directory rather
than living inside it: `sources/` is documented as immutable originals only,
and `doc-text/`'s files are re-derivable (re-running extraction reproduces
them), which `sources/`'s never are. Like `sources/`, it is NOT gitignored
(see `_GITIGNORE` below) — it must be committed, since re-deriving every
document's text on every fresh checkout is not free."""

_GITIGNORE = (
    "labs.sqlite\nlabs.sqlite-shm\nlabs.sqlite-wal\nlabs.sqlite-journal\n"
    "inbox/\nwork/\nlogs/\nsources/genomics/\n"
)
"""`sources/genomics/` (real patient genotype files - up to ~400MB across a
23andMe export plus imputed per-chromosome BCFs) must never enter the data
repo's git history/bundle, unlike the rest of `sources/`, which IS tracked
(immutable originals). `ingest.genomics.archive_genomic_file` also
lazy-appends this line for a data repo initialized before this line
existed - see that module's `_ensure_gitignore_excludes_genomics`."""

_PLACEHOLDER_FILES = {
    "case/case-summary.md": "# Case Summary\n\n_Not yet populated._\n",
    "case/questions-open.md": "# Open Questions for Next Appointment\n\n_None yet._\n",
    "case/family-history.md": "# Family History\n\n_Not yet populated._\n",
    "case/medications.md": "# Medications & Supplements\n\n_Not yet populated._\n",
    "case/care-team.md": "# Care Team\n\n_Not yet populated._\n",
}

LEDGER_RELPATH = "case/differential-ledger.yaml"
HISTORY_RELPATH = "case/ledger-history.jsonl"


def _default_ledger_commit_message(diff: LedgerDiff, ledger: Ledger) -> str:
    """Conventional-commit-style message for `DataRepo.apply_ledger_diff`,
    naming the new ledger version and op count (task requirement: keep it
    informative and consistent with the data repo's existing convention —
    `ingest/pipeline.py`'s `"ingest: ..."`, `intake/agent.py`'s
    `"feat(intake): ..."`, `reason/review.py`'s `"review: ..."`)."""
    op_count = len(diff.ops)
    op_word = "op" if op_count == 1 else "ops"
    return f"feat(ledger): apply diff -> v{ledger.version} ({op_count} {op_word})"


class DataRepo:
    """Thin wrapper over a GitPython `Repo` for the PHI data repo layout.

    No remote operations are ever performed by this class (no `push`,
    `pull`, `fetch`, `clone`, or remote configuration) — only local init,
    add, and commit.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        # One `DataRepo` is shared process-wide (`app.state.repo`) and its
        # write path is driven from FastAPI's sync-route thread pool, so two
        # requests really can call `commit()` at the same moment — the same
        # shared-singleton-across-threads shape that produced a live
        # `sqlite3.InterfaceError` in `labs.db` (see that module's lock).
        # `commit()` is a read-modify-write over `.git/index` and `HEAD`:
        # concurrent calls either collide on git's own index.lock (a raised
        # `GitCommandError`) or, worse, silently lose one thread's commit by
        # racing the ref update. Every intake turn commits, so a lost commit
        # is lost patient-reported facts. RLock, not Lock: `init_at` and
        # future callers may nest repo operations.
        self._lock = threading.RLock()

    @property
    def is_initialized(self) -> bool:
        """True if `root` is a git repo with the expected case-file layout."""
        return (self.root / ".git").is_dir() and (self.root / LEDGER_RELPATH).exists()

    @classmethod
    def init_at(cls, root: Path) -> DataRepo:
        """Create the PLAN.md data-repo layout at `root` and make the root
        commit. Idempotent: if `root` is already initialized, this is a
        no-op and returns the existing repo untouched.
        """
        data_repo = cls(root)
        root.mkdir(parents=True, exist_ok=True)

        if data_repo.is_initialized:
            return data_repo

        (root / "case").mkdir(parents=True, exist_ok=True)
        for sub in _CASE_SUBDIRS:
            (root / "case" / sub).mkdir(parents=True, exist_ok=True)
            (root / "case" / sub / ".gitkeep").touch()
        for sub in _TOP_LEVEL_DIRS:
            (root / sub).mkdir(parents=True, exist_ok=True)
        # sources/ and doc-text/ are not gitignored (immutable originals /
        # derived text) so each needs a .gitkeep to be committed while still
        # empty.
        (root / "sources" / ".gitkeep").touch()
        (root / "doc-text" / ".gitkeep").touch()

        (root / ".gitignore").write_text(_GITIGNORE, encoding="utf-8")

        for relpath, content in _PLACEHOLDER_FILES.items():
            (root / relpath).write_text(content, encoding="utf-8")

        empty_ledger = Ledger(version=0, updated=datetime.now(UTC), schema_version=1, hypotheses=[])
        save_ledger(root / LEDGER_RELPATH, empty_ledger)

        if not (root / ".git").is_dir():
            Repo.init(root)

        data_repo.commit(
            "chore: initialize case file data repo",
            paths=[
                "case",
                "sources",
                "doc-text",
                ".gitignore",
            ],
        )
        return data_repo

    def read(self, relpath: str) -> str:
        return (self.root / relpath).read_text(encoding="utf-8")

    def write(self, relpath: str, content: str) -> None:
        path = self.root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def commit(
        self,
        message: str,
        paths: list[str] | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> str:
        """Stage `paths` (default: everything) and commit. Returns the new
        commit's hexsha. Never touches a remote.

        Retries with exponential backoff on cross-process git index/ref-lock
        contention (`OSError`/`GitCommandError` from `IndexFile.write()`'s
        internal `LockedFD`, or a shelled-out `git add`) — see the module
        docstring's note on `_COMMIT_RETRY_ATTEMPTS`. `self._lock` already
        serializes calls from multiple THREADS within this process; the
        retry additionally covers collisions with a separate OS PROCESS
        (scheduled ingest/review/backup tasks) writing to the same
        EFS-mounted `.git` at the same moment. `sleep` is an injectable seam
        so tests never actually sleep.
        """
        with self._lock:
            delay = _COMMIT_RETRY_BASE_DELAY_SECONDS
            last_exc: OSError | GitCommandError | None = None
            for attempt in range(1, _COMMIT_RETRY_ATTEMPTS + 1):
                try:
                    return self._commit_once(message, paths)
                except (OSError, GitCommandError) as exc:
                    last_exc = exc
                    if attempt == _COMMIT_RETRY_ATTEMPTS:
                        break
                    logger.warning(
                        "git commit lock contention (attempt %d/%d), retrying in %.2fs: %s",
                        attempt,
                        _COMMIT_RETRY_ATTEMPTS,
                        delay,
                        exc,
                    )
                    sleep(delay)
                    delay *= 2
            assert last_exc is not None  # the loop only exits via `break` after setting it
            raise last_exc

    def _commit_once(self, message: str, paths: list[str] | None) -> str:
        """One (non-retrying) attempt at staging `paths` and committing —
        factored out of `commit()` so tests can monkeypatch just this
        method to simulate transient lock contention without touching real
        git internals."""
        repo = Repo(self.root)
        if paths is None:
            repo.git.add(A=True)
        else:
            repo.index.add(paths)
        commit = repo.index.commit(message, author=_COMMIT_ACTOR, committer=_COMMIT_ACTOR)
        return commit.hexsha

    def apply_ledger_diff(
        self,
        ledger_path: Path,
        history_path: Path,
        diff: LedgerDiff,
        *,
        commit_message: str | None = None,
    ) -> Ledger:
        """Load -> apply (invariant-checked) -> save -> append-history ->
        commit `diff` against `ledger_path`, all inside ONE critical section
        guarded by `self._lock`. This is the ONLY safe way for a running DAG
        stage (or any other concurrent caller) to persist a `LedgerDiff` —
        see the module docstring.

        CONFIRMED durability defect this fixes: `casefile.ledger.
        apply_and_save` (the lock-free primitive this method calls) does a
        plain load -> apply -> save -> append with no locking of its own.
        `web/routes/chat.py`'s `chat_send` is a sync route, so Starlette
        thread-pools it — two overlapping diagnostic turns are genuine
        concurrent callers. Unlocked, both threads `load_ledger` the SAME
        version N, both validate their diff against that identical stale
        snapshot, and both `save_ledger` — the second save silently
        clobbers the first turn's applied diff (last writer wins), while
        `append_history` (opened in append mode) records BOTH diffs
        regardless, so the committed history permanently disagrees with the
        ledger it is supposed to describe. Holding `self._lock` across the
        whole sequence closes that window: a concurrent second caller
        blocks until the first caller's diff is fully applied, saved, and
        committed, then loads THAT result as its own starting point — so
        its own invariant checks run against current state, not a stale
        one. A legitimate invariant rejection at that point (e.g. the
        second diff really is stale against the first's result) is a
        correct rejection, never a silent overwrite.

        Also closes the "no commit for a week" half of the same defect
        (PLAN.md "State": "every state change is a data-repo commit"):
        previously nothing in `reason/stages.py` or `web/routes/chat.py`
        ever committed a diagnostic-turn ledger update — only the weekly
        review's `paths=["case"]` sweep did (`reason/review.py`), so an
        update could sit uncommitted, and therefore un-backed-up
        (`adoc backup` bundles committed git refs only, `backup.py`'s
        `run_backup`), for up to a week. Every call here is one commit,
        made at the moment the turn happens.

        `commit_message` defaults to an informative, conventional-commit-
        style message naming the new version and op count (mirroring
        `ingest/pipeline.py`'s `_commit_message` and `intake/agent.py`'s
        `"feat(intake): ..."` messages).
        """
        with self._lock:
            new_ledger = apply_and_save(ledger_path, history_path, diff)
            message = commit_message or _default_ledger_commit_message(diff, new_ledger)
            self.commit(
                message,
                paths=[self._relpath(ledger_path), self._relpath(history_path)],
            )
            return new_ledger

    def _relpath(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def tag(self, name: str, *, ref: str | None = None, message: str | None = None) -> str:
        """Create a git tag named `name` (PLAN.md "State": "weekly reviews
        tagged"), pointing at `ref` (default: `HEAD`). An annotated tag is
        created when `message` is given, a lightweight tag otherwise.
        Returns `name`. Never touches a remote — same as `commit`.

        This is an addition, not a change, to `DataRepo`'s existing API
        (CLAUDE.md/PLAN.md constraint on the merged foundation): no
        existing method's signature or behavior is touched.
        """
        with self._lock:
            repo = Repo(self.root)
            target = repo.commit(ref) if ref is not None else repo.head.commit
            # Annotated tags need a committer identity; supply the same explicit
            # actor commits use so this never depends on host git config
            # (CI runners have none — "Committer identity unknown").
            with repo.git.custom_environment(
                GIT_COMMITTER_NAME=_COMMIT_ACTOR.name,
                GIT_COMMITTER_EMAIL=_COMMIT_ACTOR.email,
                GIT_AUTHOR_NAME=_COMMIT_ACTOR.name,
                GIT_AUTHOR_EMAIL=_COMMIT_ACTOR.email,
            ):
                if message is not None:
                    repo.create_tag(name, ref=target, message=message)
                else:
                    repo.create_tag(name, ref=target)
        return name
