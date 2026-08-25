"""Tests for adoc.casefile.repo.DataRepo: layout creation, idempotency, no remotes."""

from __future__ import annotations

from pathlib import Path

from git import Repo

from adoc.casefile.ledger import load_ledger
from adoc.casefile.repo import HISTORY_RELPATH, LEDGER_RELPATH, DataRepo


def test_init_at_creates_expected_layout(tmp_path: Path) -> None:
    root = tmp_path / "a-doc-data"

    repo = DataRepo.init_at(root)

    assert repo.is_initialized
    assert (root / ".git").is_dir()
    assert (root / "case" / "case-summary.md").exists()
    assert (root / "case" / "questions-open.md").exists()
    assert (root / "case" / "family-history.md").exists()
    assert (root / "case" / "medications.md").exists()
    assert (root / "case" / "care-team.md").exists()
    assert (root / "case" / "differential-ledger.yaml").exists()
    assert (root / "case" / "encounters").is_dir()
    assert (root / "case" / "reviews").is_dir()
    assert (root / "sources").is_dir()
    assert (root / "doc-text").is_dir()
    assert (root / "inbox").is_dir()
    assert (root / "work").is_dir()
    assert (root / "logs").is_dir()
    assert (root / ".gitignore").exists()


def test_init_at_commits_doc_text_dir_not_gitignored(tmp_path: Path) -> None:
    """docs/adr/0015-document-text-corpus.md: `doc-text/` is committed at
    init (a `.gitkeep`, like `sources/`) and is NOT listed in `.gitignore`
    - unlike `sources/genomics/`, it must be tracked."""
    root = tmp_path / "a-doc-data"
    DataRepo.init_at(root)

    gitignore = (root / ".gitignore").read_text(encoding="utf-8")
    assert "doc-text" not in gitignore

    git_repo = Repo(root)
    tracked = {entry.path for entry in git_repo.head.commit.tree.traverse() if entry.type == "blob"}
    assert "doc-text/.gitkeep" in tracked


def test_init_at_gitignore_covers_generated_and_working_dirs(tmp_path: Path) -> None:
    root = tmp_path / "a-doc-data"
    DataRepo.init_at(root)

    gitignore = (root / ".gitignore").read_text(encoding="utf-8")
    for entry in ("labs.sqlite", "inbox/", "work/", "logs/"):
        assert entry in gitignore


def test_init_at_writes_empty_v0_ledger(tmp_path: Path) -> None:
    root = tmp_path / "a-doc-data"
    DataRepo.init_at(root)

    ledger = load_ledger(root / LEDGER_RELPATH)
    assert ledger.version == 0
    assert ledger.schema_version == 1
    assert ledger.hypotheses == []


def test_init_at_makes_a_root_commit(tmp_path: Path) -> None:
    root = tmp_path / "a-doc-data"
    DataRepo.init_at(root)

    git_repo = Repo(root)
    commits = list(git_repo.iter_commits())
    assert len(commits) == 1
    assert "initialize" in commits[0].message.lower()
    # tracked working-dir files, not the gitignored ones
    tracked = set(git_repo.git.ls_files().splitlines())
    assert "case/differential-ledger.yaml" in tracked
    assert ".gitignore" in tracked
    assert not any(p.startswith(("inbox/", "work/", "logs/")) for p in tracked)


def test_init_at_is_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "a-doc-data"

    first = DataRepo.init_at(root)
    first_commit = Repo(root).head.commit.hexsha

    second = DataRepo.init_at(root)
    second_commit = Repo(root).head.commit.hexsha

    assert first.is_initialized
    assert second.is_initialized
    assert first_commit == second_commit
    assert len(list(Repo(root).iter_commits())) == 1


def test_init_at_idempotent_even_with_extra_uncommitted_changes(tmp_path: Path) -> None:
    root = tmp_path / "a-doc-data"
    DataRepo.init_at(root)

    # simulate an in-progress edit the second init_at call should not disturb
    (root / "case" / "case-summary.md").write_text("edited\n", encoding="utf-8")

    DataRepo.init_at(root)

    # init_at detected the repo was already initialized and made no new commit
    assert len(list(Repo(root).iter_commits())) == 1
    assert (root / "case" / "case-summary.md").read_text(encoding="utf-8") == "edited\n"


def test_data_repo_has_no_remote_configuration(tmp_path: Path) -> None:
    root = tmp_path / "a-doc-data"
    DataRepo.init_at(root)

    git_repo = Repo(root)
    assert list(git_repo.remotes) == []


def test_read_write_round_trip(tmp_path: Path) -> None:
    root = tmp_path / "a-doc-data"
    repo = DataRepo.init_at(root)

    repo.write("case/questions-open.md", "# Open Questions\n\n- Ask about X\n")
    assert repo.read("case/questions-open.md") == "# Open Questions\n\n- Ask about X\n"


def test_commit_stages_and_commits_specific_paths(tmp_path: Path) -> None:
    root = tmp_path / "a-doc-data"
    repo = DataRepo.init_at(root)

    repo.write("case/questions-open.md", "# Open Questions\n\n- Ask about X\n")
    sha = repo.commit("docs: update open questions", paths=["case/questions-open.md"])

    git_repo = Repo(root)
    assert git_repo.head.commit.hexsha == sha
    assert len(list(git_repo.iter_commits())) == 2
    assert git_repo.head.commit.message.strip() == "docs: update open questions"


def test_commit_default_stages_everything(tmp_path: Path) -> None:
    root = tmp_path / "a-doc-data"
    repo = DataRepo.init_at(root)

    repo.write("case/family-history.md", "# Family History\n\nMother: Hashimoto's.\n")
    repo.write("case/medications.md", "# Medications\n\nLevothyroxine.\n")
    sha = repo.commit("docs: onboarding section update")

    git_repo = Repo(root)
    assert git_repo.head.commit.hexsha == sha
    diffs = git_repo.commit(sha).diff(git_repo.commit(sha).parents[0])
    changed = {d.a_path for d in diffs}
    assert "case/family-history.md" in changed
    assert "case/medications.md" in changed


def test_history_relpath_constant_matches_ledger_convention() -> None:
    assert HISTORY_RELPATH == "case/ledger-history.jsonl"
    assert LEDGER_RELPATH == "case/differential-ledger.yaml"


def test_concurrent_commits_do_not_race_or_lose_a_commit(tmp_path: Path) -> None:
    """`DataRepo` is a process-wide singleton (`app.state.repo`) driven from
    FastAPI's sync-route thread pool, so two requests can call `commit()` at
    the same moment. `commit()` is a read-modify-write over `.git/index` and
    `HEAD`: unserialized, concurrent callers either collide on git's own
    index.lock (`GitCommandError`) or silently lose one thread's commit by
    racing the ref update. Every intake turn commits, so a lost commit is
    lost patient-reported facts.

    Sized so that failure without the lock is overwhelmingly likely rather
    than occasional: 8 threads x 6 commits each, all contending on one repo.
    """
    from concurrent.futures import ThreadPoolExecutor

    repo = DataRepo.init_at(tmp_path / "data")
    baseline = sum(1 for _ in Repo(repo.root).iter_commits())

    threads, per_thread = 8, 6

    def _write_and_commit(worker: int) -> list[str]:
        shas = []
        for i in range(per_thread):
            relpath = f"case/notes/worker-{worker}-{i}.md"
            repo.write(relpath, f"worker {worker} note {i}\n")
            shas.append(repo.commit(f"test: worker {worker} note {i}"))
        return shas

    with ThreadPoolExecutor(max_workers=threads) as pool:
        results = list(pool.map(_write_and_commit, range(threads)))

    all_shas = [sha for batch in results for sha in batch]
    assert len(all_shas) == threads * per_thread
    assert all(isinstance(sha, str) and sha for sha in all_shas)

    # Every commit must actually be in history: a lost update would show up
    # as a shortfall here even though commit() returned a sha to its caller.
    final = sum(1 for _ in Repo(repo.root).iter_commits())
    assert final == baseline + threads * per_thread

    # And every file written is present in the final tree.
    for worker in range(threads):
        for i in range(per_thread):
            assert (repo.root / f"case/notes/worker-{worker}-{i}.md").exists()
