"""Tests for adoc.cli: subcommand dispatch and exit codes.

Subcommands are invoked by calling `main()` directly (not via subprocess) so
that coverage.py can attribute execution to these tests. A separate
subprocess-based test exercises the real console entrypoint end-to-end for
`--help` / an unknown subcommand.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from adoc.cli import main

REPO_ROOT = Path(__file__).resolve().parents[1]

_STUB_COMMANDS = ["ingest", "review", "serve", "backfill", "eval"]


@pytest.mark.parametrize("command", _STUB_COMMANDS)
def test_stub_subcommands_exit_zero(command: str, capsys: pytest.CaptureFixture[str]) -> None:
    code = main([command])
    assert code == 0
    out = capsys.readouterr().out
    assert "not implemented (phase 1)" in out


def test_init_succeeds_with_valid_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "a-doc-data"
    monkeypatch.setenv("ADOC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ADOC_MODELS_FILE", str(REPO_ROOT / "models.yaml"))

    code = main(["init"])

    assert code == 0
    out = capsys.readouterr().out
    assert "data_dir=" in out
    assert "loaded 6 model role bindings" in out


def test_init_creates_data_repo_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "a-doc-data"
    monkeypatch.setenv("ADOC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ADOC_MODELS_FILE", str(REPO_ROOT / "models.yaml"))

    first_code = main(["init"])
    first_out = capsys.readouterr().out
    assert first_code == 0
    assert f"initialized data repo at {data_dir}" in first_out
    assert (data_dir / "case" / "differential-ledger.yaml").exists()

    second_code = main(["init"])
    second_out = capsys.readouterr().out
    assert second_code == 0
    assert f"already initialized at {data_dir}" in second_out


def test_init_fails_without_data_dir(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)  # hermetic: a developer .env must not leak into Settings
    monkeypatch.delenv("ADOC_DATA_DIR", raising=False)

    code = main(["init"])

    assert code == 1
    err = capsys.readouterr().err
    assert "configuration error" in err


def test_onboard_fails_without_data_dir(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)  # hermetic: a developer .env must not leak into Settings
    monkeypatch.delenv("ADOC_DATA_DIR", raising=False)

    code = main(["onboard"])

    assert code == 1
    err = capsys.readouterr().err
    assert "configuration error" in err


def test_onboard_fails_if_data_repo_not_initialized(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "a-doc-data"
    monkeypatch.setenv("ADOC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ADOC_MODELS_FILE", str(REPO_ROOT / "models.yaml"))

    code = main(["onboard"])

    assert code == 1
    err = capsys.readouterr().err
    assert "run `adoc init` first" in err


def test_onboard_runs_the_wizard_loop_against_an_initialized_repo(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data_dir = tmp_path / "a-doc-data"
    monkeypatch.setenv("ADOC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ADOC_MODELS_FILE", str(REPO_ROOT / "models.yaml"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    assert main(["init"]) == 0
    capsys.readouterr()

    def _eof_input(_prompt: str = "") -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", _eof_input)

    # Immediate EOF: no LLM call is ever made, so no network / API key is
    # needed to exercise the wiring end-to-end.
    code = main(["onboard"])

    assert code == 0
    out = capsys.readouterr().out
    assert "[1/10] Basics" in out
    assert "resume anytime with `adoc onboard`" in out


def test_unknown_subcommand_exits_nonzero() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["not-a-real-command"])
    assert excinfo.value.code != 0


def test_console_entrypoint_help_subprocess() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "adoc.cli", "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "adoc" in result.stdout
