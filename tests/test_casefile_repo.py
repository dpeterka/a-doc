"""Tests for adoc.casefile.repo.DataRepo: layout creation, idempotency, no remotes."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from git import Repo

from adoc.casefile.ledger import load_ledger
from adoc.casefile.repo import HISTORY_RELPATH, LEDGER_RELPATH, DataRepo
from adoc.casefile.schema import AddHypothesis, Hypothesis, LedgerDiff, Provenance


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


# --- commit(): cross-process lock-contention retry (moved here from
# ingest/pipeline.py's `_commit_with_retry`, ledger-durability task, so
# every `DataRepo.commit()` caller benefits, not just ingest) -------------


def test_commit_retries_on_lock_contention_then_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient cross-process git index/ref-lock collision (`OSError`)
    is retried with backoff rather than raised immediately. Monkeypatches
    `DataRepo._commit_once` (the single, non-retrying attempt `commit()`
    wraps) to fail twice before delegating to the real implementation, so
    this exercises the actual retry loop in `commit()` against a real git
    repo, not a hand-rolled fake."""
    repo = DataRepo.init_at(tmp_path / "data")
    repo.write("case/questions-open.md", "# Open Questions\n\n- Ask about X\n")

    real_commit_once = repo._commit_once
    calls: list[str] = []
    sleeps: list[float] = []

    def _flaky(message: str, paths: list[str] | None) -> str:
        calls.append(message)
        if len(calls) < 3:
            raise OSError("Lock at '.git/index.lock' could not be obtained")
        return real_commit_once(message, paths)

    monkeypatch.setattr(repo, "_commit_once", _flaky)

    sha = repo.commit(
        "docs: update open questions", paths=["case/questions-open.md"], sleep=sleeps.append
    )

    assert sha
    assert len(calls) == 3
    # slept between attempt 1->2 and 2->3, never after the final success.
    assert len(sleeps) == 2
    assert Repo(repo.root).head.commit.hexsha == sha


def test_commit_raises_after_exhausting_retry_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once every retry attempt is exhausted, the original exception
    propagates (so a caller like `ingest_inbox`'s per-file try/except can
    route the file to `work/failed/`, rather than this being silently
    swallowed)."""
    repo = DataRepo.init_at(tmp_path / "data")
    repo.write("case/questions-open.md", "# Open Questions\n\n- Ask about X\n")

    def _always_locked(_message: str, _paths: list[str] | None) -> str:
        raise OSError("Lock at '.git/index.lock' could not be obtained")

    monkeypatch.setattr(repo, "_commit_once", _always_locked)

    with pytest.raises(OSError, match="Lock at"):
        repo.commit(
            "docs: update open questions",
            paths=["case/questions-open.md"],
            sleep=lambda _seconds: None,
        )


# --- apply_ledger_diff(): the durability fix ------------------------------


def _cant_miss_diff(hyp_id: str, *, dag_node: str = "apply") -> LedgerDiff:
    provenance = Provenance(
        app_version="test",
        prompt_template_version="ledger_maintainer@v1",
        model_id="test-model",
        dag_node=dag_node,
        timestamp=datetime(2026, 8, 1, tzinfo=UTC),
    )
    hypothesis = Hypothesis(
        id=hyp_id,
        name=f"Hypothesis {hyp_id}",
        tier="cant-miss",
        probability="moderate",
        status="active",
        origin="model",
        first_proposed=date(2026, 8, 1),
    )
    return LedgerDiff(
        provenance=provenance,
        rationale=f"seed {hyp_id}",
        ops=[AddHypothesis(hypothesis=hypothesis)],
    )


def test_apply_ledger_diff_applies_saves_and_commits(tmp_path: Path) -> None:
    repo = DataRepo.init_at(tmp_path / "data")
    ledger_path = repo.root / LEDGER_RELPATH
    history_path = repo.root / HISTORY_RELPATH
    diff = _cant_miss_diff("pe-01")

    new_ledger = repo.apply_ledger_diff(ledger_path, history_path, diff)

    assert new_ledger.version == 1
    assert [h.id for h in new_ledger.hypotheses] == ["pe-01"]
    assert load_ledger(ledger_path) == new_ledger


def test_apply_ledger_diff_commits_ledger_and_history_leaves_nothing_dirty(
    tmp_path: Path,
) -> None:
    """Durability: after a diagnostic turn's apply, `git log` in the data
    repo shows a commit containing the ledger, and nothing is left dirty
    (CONFIRMED durability defect: previously nothing in `reason/stages.py`
    or `web/routes/chat.py` ever committed a diagnostic-turn ledger update
    — only the weekly review's `paths=["case"]` sweep did, so an update
    could sit uncommitted, and therefore un-backed-up by `adoc backup`
    (bundles committed git refs only), for up to a week)."""
    repo = DataRepo.init_at(tmp_path / "data")
    ledger_path = repo.root / LEDGER_RELPATH
    history_path = repo.root / HISTORY_RELPATH
    diff = _cant_miss_diff("pe-01")

    new_ledger = repo.apply_ledger_diff(ledger_path, history_path, diff)

    git_repo = Repo(repo.root)
    assert not git_repo.is_dirty(untracked_files=True)

    head_commit = git_repo.head.commit
    assert f"v{new_ledger.version}" in head_commit.message
    changed = {d.a_path for d in head_commit.diff(head_commit.parents[0])}
    assert LEDGER_RELPATH in changed
    assert HISTORY_RELPATH in changed


def test_apply_ledger_diff_change_is_captured_by_backup_bundle(tmp_path: Path) -> None:
    """A diagnostic turn's ledger commit is now reachable from HEAD, so
    `adoc backup`'s `git bundle create --all` (`backup._bundle_data_repo`)
    captures it — a redeploy or S3 restore in the window between two
    weekly reviews no longer silently loses it. Proven with a real
    bundle-and-clone round trip, not just a HEAD-sha assertion."""
    from adoc.backup import _bundle_data_repo

    repo = DataRepo.init_at(tmp_path / "data")
    ledger_path = repo.root / LEDGER_RELPATH
    history_path = repo.root / HISTORY_RELPATH
    diff = _cant_miss_diff("pe-01")
    repo.apply_ledger_diff(ledger_path, history_path, diff)

    head_sha = Repo(repo.root).head.commit.hexsha

    bundle_path = tmp_path / "backup.bundle"
    _bundle_data_repo(repo.root, bundle_path)

    clone_path = tmp_path / "clone"
    cloned = Repo.clone_from(str(bundle_path), str(clone_path))
    try:
        assert cloned.head.commit.hexsha == head_sha
        assert (clone_path / LEDGER_RELPATH).exists()
        assert (clone_path / HISTORY_RELPATH).exists()
        restored_ledger = load_ledger(clone_path / LEDGER_RELPATH)
        assert [h.id for h in restored_ledger.hypotheses] == ["pe-01"]
    finally:
        cloned.close()


def test_apply_ledger_diff_concurrent_turns_both_survive(tmp_path: Path) -> None:
    """CONFIRMED durability defect (the core bug): two overlapping
    diagnostic turns are genuine concurrent callers of `casefile.ledger.
    apply_and_save` — `web/routes/chat.py`'s `chat_send` is a sync route,
    so Starlette thread-pools it. Unlocked, both threads `load_ledger` the
    SAME version, both validate their diff against that identical stale
    snapshot, and both `save_ledger` — the second save silently clobbers
    the first turn's applied diff (last writer wins), while
    `append_history` (append-mode) records BOTH diffs regardless, so the
    committed history permanently disagrees with the ledger it describes.

    `DataRepo.apply_ledger_diff` holds `self._lock` across the whole
    load->apply->save->append->commit sequence, so a concurrent second
    caller is applied to the FIRST caller's result, not the stale snapshot
    it started from: both hypotheses must survive, the version must have
    advanced by exactly 2 (never 1, never a lost update), and the history
    file must agree with the ledger.

    Each worker's diff adds its OWN cant-miss hypothesis (distinct ids) so
    the ledger invariants hold regardless of which order the two diffs are
    actually applied in — this test is about the durability/locking fix,
    not about invariant ordering.
    """
    from concurrent.futures import ThreadPoolExecutor

    repo = DataRepo.init_at(tmp_path / "data")
    ledger_path = repo.root / LEDGER_RELPATH
    history_path = repo.root / HISTORY_RELPATH
    diffs = [_cant_miss_diff("hyp-0"), _cant_miss_diff("hyp-1")]

    def _apply(diff: LedgerDiff) -> None:
        repo.apply_ledger_diff(ledger_path, history_path, diff)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(_apply, diffs))

    final = load_ledger(ledger_path)
    assert final.version == 2
    assert {h.id for h in final.hypotheses} == {"hyp-0", "hyp-1"}

    history_lines = history_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(history_lines) == 2
    resulting_versions = sorted(json.loads(line)["resulting_version"] for line in history_lines)
    assert resulting_versions == [1, 2]

    git_repo = Repo(repo.root)
    assert not git_repo.is_dirty(untracked_files=True)
    # every diagnostic-turn ledger write is its own commit — two applied
    # diffs plus the repo's own init commit.
    assert sum(1 for _ in git_repo.iter_commits()) == 3
