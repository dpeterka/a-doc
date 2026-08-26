"""Tests for adoc.config: Settings env loading and models.yaml binding loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from adoc.config import Settings, load_model_bindings

REPO_ROOT = Path(__file__).resolve().parents[1]
MODELS_FILE = REPO_ROOT / "models.yaml"


def test_settings_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    data_dir = tmp_path / "a-doc-data"
    monkeypatch.setenv("ADOC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ADOC_SESSION_PASSPHRASE", "correct-horse-battery-staple")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-oai-test")
    monkeypatch.setenv("FEATHERLESS_API_KEY", "sk-feather-test")

    settings = Settings()

    assert settings.data_dir == data_dir
    assert settings.session_passphrase is not None
    assert settings.session_passphrase.get_secret_value() == "correct-horse-battery-staple"
    assert settings.anthropic_api_key is not None
    assert settings.anthropic_api_key.get_secret_value() == "sk-ant-test"
    assert settings.openai_api_key is not None
    assert settings.openai_api_key.get_secret_value() == "sk-oai-test"
    assert settings.featherless_api_key is not None
    assert settings.featherless_api_key.get_secret_value() == "sk-feather-test"


def test_settings_optional_keys_default_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)  # hermetic: a developer .env must not leak into Settings
    monkeypatch.setenv("ADOC_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("FEATHERLESS_API_KEY", raising=False)
    monkeypatch.delenv("ADOC_SESSION_PASSPHRASE", raising=False)

    settings = Settings()

    assert settings.anthropic_api_key is None
    assert settings.openai_api_key is None
    assert settings.featherless_api_key is None
    assert settings.session_passphrase is None


def test_settings_deploy_only_fields_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)  # hermetic: a developer .env must not leak into Settings
    monkeypatch.setenv("ADOC_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ADOC_SQLITE_JOURNAL_MODE", raising=False)
    monkeypatch.delenv("ADOC_BACKUP_BUCKET", raising=False)

    settings = Settings()

    assert settings.sqlite_journal_mode == "WAL"
    assert settings.backup_bucket is None


def test_settings_deploy_only_fields_from_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ADOC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ADOC_SQLITE_JOURNAL_MODE", "TRUNCATE")
    monkeypatch.setenv("ADOC_BACKUP_BUCKET", "a-doc-backup-bucket")

    settings = Settings()

    assert settings.sqlite_journal_mode == "TRUNCATE"
    assert settings.backup_bucket == "a-doc-backup-bucket"


def test_load_model_bindings_has_all_roles() -> None:
    bindings = load_model_bindings(MODELS_FILE)

    expected_roles = {
        "primary_reasoner",
        "challenger",
        "blind_panel",
        "extractor_pass_a",
        "extractor_pass_b",
        "classifier",
        "test_chooser",
        "intake_agent",
        "entailment_verifier",
        # Test-harness only (`scripts/intake-replay --persona`); no DAG stage
        # resolves it. Listed here rather than exempted so the set stays an
        # exact equality check — a role appearing or vanishing unnoticed is
        # what this test exists to catch.
        "patient_simulator",
    }
    assert set(bindings.keys()) == expected_roles

    # every role is normalized to a list, even single-model roles
    assert len(bindings["primary_reasoner"]) == 1
    assert bindings["primary_reasoner"][0].provider == "anthropic"
    assert bindings["primary_reasoner"][0].model == "claude-opus-5"

    # blind_panel is the multi-binding role
    assert len(bindings["blind_panel"]) == 3
    providers = {b.provider for b in bindings["blind_panel"]}
    assert providers == {"anthropic", "openai", "featherless"}
