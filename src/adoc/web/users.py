"""Username/password user store for the web login.

Replaces the single shared passphrase (`Settings.session_passphrase`,
still present for backward compatibility but no longer read by the login
route — see `web.routes.auth`) with per-user credentials, one entry per
patient/caregiver who needs access to the app now that it sits behind a
public ALB instead of a private tailnet (explicit user decision).

Storage: a flat YAML file at `<data_dir>/work/users.yaml`. `work/` is
already gitignored by `casefile.repo.DataRepo` (`_GITIGNORE`), so this
file — like the session-signing secret next to it — never reaches the
data repo's git history.

Hashing: stdlib `hashlib.scrypt` (n=2**14, r=8, p=1 — comfortably above
the historical minimum-recommended cost for an interactive login, cheap
enough not to matter for a single-digit number of users) with a random
16-byte salt per user. No extra dependency needed for a task-scoped
constraint of "scrypt, stdlib".

Timing: `verify_user` does the same amount of scrypt+compare work whether
or not `username` exists, so an attacker cannot use response timing to
enumerate valid usernames.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

USERS_RELPATH = Path("work") / "users.yaml"

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SALT_BYTES = 16

# Fixed, non-secret salt used only to give the "username not found" path in
# verify_user() the same scrypt cost as a real lookup. Never used to store
# or check a real password.
_DUMMY_SALT = bytes(_SALT_BYTES)
_DUMMY_HASH = hashlib.scrypt(b"", salt=_DUMMY_SALT, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)

_yaml = YAML(typ="safe")
_yaml.default_flow_style = False


def _load(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as fh:
        data = _yaml.load(fh)
    if not data:
        return []
    users: list[dict[str, Any]] = data.get("users", [])
    return users


def _save(path: Path, users: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        _yaml.dump({"users": users}, fh)


def _scrypt(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P
    )


def add_user(path: Path, username: str, password: str) -> None:
    """Add a user, replacing any existing entry for the same username
    (used both for first creation and password rotation)."""
    if not username:
        raise ValueError("username must not be empty")
    if not password:
        raise ValueError("password must not be empty")

    users = [u for u in _load(path) if u["username"] != username]
    salt = secrets.token_bytes(_SALT_BYTES)
    users.append(
        {
            "username": username,
            "salt": salt.hex(),
            "hash": _scrypt(password, salt).hex(),
            "created_at": datetime.now(UTC).isoformat(),
        }
    )
    _save(path, users)


def verify_user(path: Path, username: str, password: str) -> bool:
    """True iff `username`/`password` match a stored entry.

    Always performs one scrypt hash and one `hmac.compare_digest` call,
    whether or not `username` exists, and only ever compares digests with
    `hmac.compare_digest` (never `==`) — see module docstring "Timing".
    """
    users = _load(path)
    match = next((u for u in users if u["username"] == username), None)
    if match is None:
        dummy = _scrypt(password, _DUMMY_SALT)
        hmac.compare_digest(dummy, _DUMMY_HASH)
        return False

    salt = bytes.fromhex(match["salt"])
    expected = bytes.fromhex(match["hash"])
    submitted = _scrypt(password, salt)
    return hmac.compare_digest(submitted, expected)


def list_users(path: Path) -> list[str]:
    """Usernames in creation order."""
    return [u["username"] for u in _load(path)]


def remove_user(path: Path, username: str) -> bool:
    """Remove a user. Returns False if no such user existed."""
    users = _load(path)
    remaining = [u for u in users if u["username"] != username]
    if len(remaining) == len(users):
        return False
    _save(path, remaining)
    return True
