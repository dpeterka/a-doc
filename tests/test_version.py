"""The app version is stamped into `Provenance.app_version` on every
persisted LLM-derived artifact. A hand-maintained literal in
`adoc/__init__.py` drifted from the packaged version and went unnoticed
across six releases (0.10.0 -> 0.16.0), so every artifact produced in that
window carries a provenance stamp that names the wrong build. These tests
make that drift impossible to reintroduce quietly."""

from __future__ import annotations

import tomllib
from pathlib import Path

import adoc


def _pyproject_version() -> str:
    path = Path(__file__).resolve().parent.parent / "pyproject.toml"
    return str(tomllib.loads(path.read_text())["project"]["version"])


def test_runtime_version_matches_pyproject() -> None:
    assert adoc.__version__ == _pyproject_version()


def test_version_is_not_the_not_installed_sentinel() -> None:
    """The fallback is deliberately not a plausible release number. If it
    ever reaches provenance, the stamp should be obviously broken rather
    than quietly wrong."""
    assert adoc.__version__ != "0.0.0+not-installed"
