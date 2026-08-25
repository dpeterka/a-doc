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

Session binding: `load_fingerprints`/`get_fingerprint` derive a short,
non-secret fingerprint (`sha256(salt || hash)[:16]`) per stored credential
record. `web.security` signs this fingerprint into every session cookie
alongside the username, and re-checks it against the current store on
every request — removing a user or resetting their password rewrites (or
deletes) their record, which changes (or removes) the fingerprint and
invalidates every outstanding session for that user immediately, rather
than waiting out the 30-day cookie lifetime.
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


def _new_yaml() -> YAML:
    """A fresh `YAML(typ="safe")` instance for one load/dump call.

    A single module-level `YAML()` used to be shared here - the only
    module-level `YAML` instance in the codebase (every other of the 13
    other construction sites builds one per call). ruamel's `YAML` objects
    are NOT thread-safe. This sits on the hot auth path
    (`web.security.SessionAuthMiddleware` -> `is_authenticated` ->
    `load_fingerprints` -> `_load` -> `_yaml.load(fh)`), which FastAPI
    serves from a sync-route thread pool, so concurrent requests really do
    call `.load()` on the same shared parser from two threads at once. That
    produced intermittent `ruamel.yaml.constructor.DuplicateKeyError`s on
    perfectly valid, non-duplicate YAML (confirmed by running 8 threads x
    40 loads against a static file - see
    `tests/test_web_users_concurrency.py`) - i.e. intermittent 500s on any
    authenticated page, worst right after container start or `adoc user
    add` (a fresh/rewritten file with no warm cache yet, so more
    concurrent callers land on an actual parse). Constructing a new,
    unshared `YAML()` per call removes the shared mutable state entirely
    instead of trying to lock around it - this module has no long-lived
    instance to hang a lock on. (`web.security._UserStoreCache`'s
    cache-fill has its own lock for the analogous race on ITS shared
    dict.)
    """
    yaml = YAML(typ="safe")
    yaml.default_flow_style = False
    return yaml


def _load(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as fh:
        data = _new_yaml().load(fh)
    if not data:
        return []
    users: list[dict[str, Any]] = data.get("users", [])
    return users


def _save(path: Path, users: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        _new_yaml().dump({"users": users}, fh)


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


def _fingerprint(salt_hex: str, hash_hex: str) -> str:
    """First 16 hex chars of sha256(salt || hash) - see module docstring
    'Session binding'. Deterministic and non-secret (it's a fingerprint of
    already-stored, salted material, never the password itself), but
    changes whenever the record does: a password reset generates a new
    salt and hash, so the fingerprint always rotates on password change."""
    return hashlib.sha256(bytes.fromhex(salt_hex) + bytes.fromhex(hash_hex)).hexdigest()[:16]


def load_fingerprints(path: Path) -> dict[str, str]:
    """username -> credential fingerprint for every stored user."""
    return {u["username"]: _fingerprint(u["salt"], u["hash"]) for u in _load(path)}


def get_fingerprint(path: Path, username: str) -> str | None:
    """Convenience single-user lookup over `load_fingerprints`; `None` if
    no such user (used by `web.security` to bind and re-verify sessions)."""
    return load_fingerprints(path).get(username)


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
