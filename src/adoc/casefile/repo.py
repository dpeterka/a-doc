"""Git plumbing for the data repo (PLAN.md "State": "Two git repos: code vs data").

`DataRepo` never performs a remote operation — the data repo has no remote
by design (CLAUDE.md PHI boundary rule 1). Every mutation to the data repo
is meant to be one commit; `commit()` is the only write-and-record primitive
other modules should use.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path

from git import Actor, Repo

from adoc.casefile.ledger import save_ledger
from adoc.casefile.schema import Ledger

_COMMIT_ACTOR = Actor("adoc", "adoc@localhost")

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

    def commit(self, message: str, paths: list[str] | None = None) -> str:
        """Stage `paths` (default: everything) and commit. Returns the new
        commit's hexsha. Never touches a remote.
        """
        with self._lock:
            repo = Repo(self.root)
            if paths is None:
                repo.git.add(A=True)
            else:
                repo.index.add(paths)
            commit = repo.index.commit(message, author=_COMMIT_ACTOR, committer=_COMMIT_ACTOR)
            return commit.hexsha

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
