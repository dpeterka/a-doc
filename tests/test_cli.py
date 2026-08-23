"""Tests for adoc.cli: subcommand dispatch and exit codes.

Subcommands are invoked by calling `main()` directly (not via subprocess) so
that coverage.py can attribute execution to these tests. A separate
subprocess-based test exercises the real console entrypoint end-to-end for
`--help` / an unknown subcommand.

`ingest`/`backfill` are wired to the real `ingest.pipeline`; tests exercise
them by monkeypatching `adoc.cli._build_vision_client` (the seam the module
docstring calls out) so no real provider/network call ever happens.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from conftest import TINY_PDF_BYTES, fake_page_renderer

import adoc.cli as cli
from adoc.casefile.repo import DataRepo
from adoc.cli import main
from adoc.ingest.schema import ClassifyResult, DocumentExtraction

REPO_ROOT = Path(__file__).resolve().parents[1]

_STUB_COMMANDS = ["review", "eval"]


class _FakeVisionClient:
    """Routes every role to a fixed `clinical_note` classification - enough
    to exercise the CLI's ingest wiring without a real provider call.
    """

    def extract(self, role, *, system, parts, schema, binding_index=0, max_tokens=4096):  # type: ignore[no-untyped-def]
        if role == "classifier":
            return ClassifyResult(doc_type="clinical_note", doc_date=None)
        return DocumentExtraction(doc_type="clinical_note")  # pragma: no cover - not reached


def _patch_fake_vision(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_build_vision_client", lambda llm_client: _FakeVisionClient())
    monkeypatch.setattr(cli, "_build_renderer", lambda: fake_page_renderer(1))


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


def test_ingest_fails_without_an_initialized_data_repo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ADOC_DATA_DIR", str(tmp_path / "a-doc-data"))
    monkeypatch.setenv("ADOC_MODELS_FILE", str(REPO_ROOT / "models.yaml"))

    code = main(["ingest"])

    assert code == 1
    assert "not initialized" in capsys.readouterr().err


def test_ingest_scans_the_inbox_and_prints_a_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "a-doc-data"
    repo = DataRepo.init_at(data_dir)
    (repo.root / "inbox" / "visit.pdf").write_bytes(TINY_PDF_BYTES)
    monkeypatch.setenv("ADOC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ADOC_MODELS_FILE", str(REPO_ROOT / "models.yaml"))
    _patch_fake_vision(monkeypatch)

    code = main(["ingest"])

    assert code == 0
    out = capsys.readouterr().out
    assert "visit.pdf" in out
    assert "ingested" in out
    encounters = list((repo.root / "case" / "encounters").glob("*.md"))
    assert len(encounters) == 1


def test_backfill_ingests_a_given_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "a-doc-data"
    DataRepo.init_at(data_dir)
    backfill_dir = tmp_path / "historical"
    backfill_dir.mkdir()
    (backfill_dir / "old-visit.pdf").write_bytes(TINY_PDF_BYTES)
    monkeypatch.setenv("ADOC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ADOC_MODELS_FILE", str(REPO_ROOT / "models.yaml"))
    _patch_fake_vision(monkeypatch)

    code = main(["backfill", str(backfill_dir)])

    assert code == 0
    out = capsys.readouterr().out
    assert "old-visit.pdf" in out


def test_backfill_fails_for_a_missing_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "a-doc-data"
    DataRepo.init_at(data_dir)
    monkeypatch.setenv("ADOC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ADOC_MODELS_FILE", str(REPO_ROOT / "models.yaml"))

    code = main(["backfill", str(tmp_path / "does-not-exist")])

    assert code == 1
    assert "not a directory" in capsys.readouterr().err


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


def test_serve_fails_without_data_dir(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)  # hermetic: a developer .env must not leak into Settings
    monkeypatch.delenv("ADOC_DATA_DIR", raising=False)

    code = main(["serve"])

    assert code == 1
    assert "configuration error" in capsys.readouterr().err


def test_serve_builds_the_app_and_runs_uvicorn_with_host_and_port(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "a-doc-data"
    DataRepo.init_at(data_dir)
    monkeypatch.setenv("ADOC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ADOC_MODELS_FILE", str(REPO_ROOT / "models.yaml"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    calls: list[tuple[object, str, int]] = []
    monkeypatch.setattr(
        cli, "_run_uvicorn", lambda app, *, host, port: calls.append((app, host, port))
    )

    code = main(["serve", "--host", "0.0.0.0", "--port", "9000"])

    assert code == 0
    assert len(calls) == 1
    app, host, port = calls[0]
    assert host == "0.0.0.0"
    assert port == 9000
    from fastapi import FastAPI

    assert isinstance(app, FastAPI)
    assert "starting on http://0.0.0.0:9000" in capsys.readouterr().out


def test_serve_defaults_to_localhost_8080(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "a-doc-data"
    DataRepo.init_at(data_dir)
    monkeypatch.setenv("ADOC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ADOC_MODELS_FILE", str(REPO_ROOT / "models.yaml"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    calls: list[tuple[object, str, int]] = []
    monkeypatch.setattr(
        cli, "_run_uvicorn", lambda app, *, host, port: calls.append((app, host, port))
    )

    code = main(["serve"])

    assert code == 0
    _app, host, port = calls[0]
    assert host == "127.0.0.1"
    assert port == 8080


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
