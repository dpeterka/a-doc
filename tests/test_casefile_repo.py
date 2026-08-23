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
    assert (root / "inbox").is_dir()
    assert (root / "work").is_dir()
    assert (root / "logs").is_dir()
    assert (root / ".gitignore").exists()


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
