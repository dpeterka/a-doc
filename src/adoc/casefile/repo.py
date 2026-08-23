"""Git plumbing for the data repo (PLAN.md "State": "Two git repos: code vs data").

`DataRepo` never performs a remote operation — the data repo has no remote
by design (CLAUDE.md PHI boundary rule 1). Every mutation to the data repo
is meant to be one commit; `commit()` is the only write-and-record primitive
other modules should use.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from git import Actor, Repo

from adoc.casefile.ledger import save_ledger
from adoc.casefile.schema import Ledger

_COMMIT_ACTOR = Actor("adoc", "adoc@localhost")

_CASE_SUBDIRS = ("encounters", "reviews")
_TOP_LEVEL_DIRS = ("sources", "inbox", "work", "logs")

_GITIGNORE = "labs.sqlite\ninbox/\nwork/\nlogs/\n"

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
        # sources/ is not gitignored (immutable originals) so it needs a
        # .gitkeep to be committed while still empty.
        (root / "sources" / ".gitkeep").touch()

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
        repo = Repo(self.root)
        if paths is None:
            repo.git.add(A=True)
        else:
            repo.index.add(paths)
        commit = repo.index.commit(message, author=_COMMIT_ACTOR, committer=_COMMIT_ACTOR)
        return commit.hexsha
