"""Smoke tests for `scripts/*-local` (local-dev tooling, not part of the
`adoc` package or its runtime). Deliberately shallow: each wrapper exists,
is executable, and `--help` exits 0 without creating or touching any data
directory. Never runs `start-local` for real — no server, no clone of a
safe store, no network/API calls. See `scripts/README.md` for the full
end-to-end exercise, which is a manual verification step, not a CI test.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"

WRAPPERS = [
    "start-local",
    "stop-local",
    "restart-local",
    "user-create-local",
    "user-list-local",
]


def _isolated_env(tmp_path: Path) -> tuple[dict[str, str], Path]:
    """A copy of the environment pointed at a throwaway $HOME, with
    ADOC_SAFE_STORE unset — so nothing a subprocess does here can ever
    reach a real home directory or a real safe store."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    env = dict(os.environ)
    env["HOME"] = str(fake_home)
    env.pop("ADOC_SAFE_STORE", None)
    env.pop("ADOC_LOCAL_DEV_STATE_DIR", None)
    return env, fake_home


@pytest.mark.parametrize("name", WRAPPERS)
def test_wrapper_exists_and_is_executable(name: str) -> None:
    path = SCRIPTS_DIR / name
    assert path.is_file(), f"missing wrapper script: {path}"
    assert path.stat().st_mode & stat.S_IXUSR, f"{path} is not executable"


def test_local_env_impl_exists_and_is_executable() -> None:
    path = SCRIPTS_DIR / "local-env.sh"
    assert path.is_file(), f"missing implementation script: {path}"
    assert path.stat().st_mode & stat.S_IXUSR, f"{path} is not executable"


@pytest.mark.parametrize("name", WRAPPERS)
def test_wrapper_help_exits_zero_without_touching_home(name: str, tmp_path: Path) -> None:
    env, fake_home = _isolated_env(tmp_path)
    before = set(fake_home.iterdir())

    result = subprocess.run(
        [str(SCRIPTS_DIR / name), "--help"],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )

    assert result.returncode == 0, f"{name} --help: rc={result.returncode} stderr={result.stderr!r}"
    assert result.stdout.strip(), f"{name} --help printed no usage text"

    after = set(fake_home.iterdir())
    assert after == before, f"{name} --help touched {fake_home}: {after - before}"


def test_local_env_sh_help_exits_zero_without_touching_home(tmp_path: Path) -> None:
    env, fake_home = _isolated_env(tmp_path)
    before = set(fake_home.iterdir())

    result = subprocess.run(
        [str(SCRIPTS_DIR / "local-env.sh"), "--help"],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr
    after = set(fake_home.iterdir())
    assert after == before


def test_user_create_local_missing_username_fails_clearly(tmp_path: Path) -> None:
    env, fake_home = _isolated_env(tmp_path)
    before = set(fake_home.iterdir())

    result = subprocess.run(
        [str(SCRIPTS_DIR / "user-create-local")],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )

    assert result.returncode != 0
    assert result.stderr.strip() != ""
    after = set(fake_home.iterdir())
    assert after == before


@pytest.mark.parametrize(
    "name",
    [
        "local-env.sh",
        *WRAPPERS,
        "local_dev_ops.py",
        "experiments/baseline_labs_only.py",
        "experiments/dag_enriched.py",
    ],
)
def test_script_passes_bash_or_python_syntax_check(name: str) -> None:
    path = SCRIPTS_DIR / name
    assert path.is_file(), f"missing script: {path}"
    if path.suffix == ".py":
        result = subprocess.run(
            ["python3", "-m", "py_compile", str(path)], capture_output=True, text=True
        )
    else:
        result = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, f"{path}: {result.stderr}"
