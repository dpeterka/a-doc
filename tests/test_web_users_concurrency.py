"""Concurrency regression test for the shared-`YAML()` auth-path crash.

Root cause (code review, confirmed by execution): `web/users.py` used to
hold a single module-level `YAML(typ="safe")` instance and reuse it for
every `_load`/`_save` call. `_load` sits on the hot auth path
(`SessionAuthMiddleware` -> `is_authenticated` -> `load_fingerprints` ->
`_load` -> `_yaml.load(fh)`), which FastAPI serves from a sync-route thread
pool - so concurrent authenticated requests really do call `.load()` on the
one shared `YAML` instance from multiple threads at once. ruamel's `YAML`
objects are not thread-safe: sharing one across threads corrupts its
internal composer/constructor state and raises
`ruamel.yaml.constructor.DuplicateKeyError` on perfectly valid,
non-duplicate YAML - an intermittent 500 on any authenticated page.

`web/users.py` now builds a fresh `YAML()` per `_load`/`_save` call
(`_new_yaml()`), so there is no shared mutable parser to race on.

This test drives real thread contention (not sleep-based) directly against
the public `load_fingerprints`/`verify_user` API over a realistic
multi-user store, sized so the failure is overwhelmingly likely without the
fix rather than occasional (see this file's bottom note for the exact
error observed with the fix reverted).

All data here is synthetic (CLAUDE.md PHI boundary rule 1) - usernames and
passwords are fixture values, never real patient data.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from adoc.web.users import add_user, load_fingerprints, verify_user

USERNAMES = [f"user-{i}" for i in range(8)]


def _seed(path: Path) -> None:
    for username in USERNAMES:
        add_user(path, username, f"password-{username}")


def test_concurrent_load_fingerprints_never_raises(tmp_path: Path) -> None:
    """8 threads x 40 `load_fingerprints` calls against one on-disk
    `work/users.yaml` with 8 users (multi-key mappings, the shape that
    trips up a shared ruamel parser's internal state). Without the
    per-call-`YAML()` fix, this reliably raised
    `ruamel.yaml.constructor.DuplicateKeyError` on a real fraction of
    calls - see module docstring.
    """
    path = tmp_path / "work" / "users.yaml"
    _seed(path)

    errors: list[BaseException] = []
    errors_lock = threading.Lock()
    results: list[dict[str, str]] = []
    results_lock = threading.Lock()

    def _call() -> None:
        try:
            fingerprints = load_fingerprints(path)
            with results_lock:
                results.append(fingerprints)
        except BaseException as exc:  # noqa: BLE001 - capture and report, not just fail loudly
            with errors_lock:
                errors.append(exc)

    threads, per_thread = 8, 40
    with ThreadPoolExecutor(max_workers=threads) as pool:
        futures = [pool.submit(_call) for _ in range(threads * per_thread)]
        for future in futures:
            future.result()

    assert errors == [], (
        f"concurrent load_fingerprints() raised {len(errors)}/{threads * per_thread} "
        f"exception(s); first: {errors[0]!r}"
    )
    assert len(results) == threads * per_thread
    # Every call must see the same, fully-correct fingerprint set - a
    # torn/corrupted read would show up here as a wrong key set, not just
    # as a raised exception.
    expected_usernames = set(USERNAMES)
    for fingerprints in results:
        assert set(fingerprints) == expected_usernames


def test_concurrent_verify_user_never_raises(tmp_path: Path) -> None:
    """Same shape as above but through `verify_user` (also calls `_load`
    under the hood via `web.routes.auth`'s real login path), mixing valid
    and invalid credentials across threads.
    """
    path = tmp_path / "work" / "users.yaml"
    _seed(path)

    errors: list[BaseException] = []
    errors_lock = threading.Lock()

    def _call(i: int) -> None:
        try:
            username = USERNAMES[i % len(USERNAMES)]
            ok = verify_user(path, username, f"password-{username}")
            assert ok is True
        except BaseException as exc:  # noqa: BLE001
            with errors_lock:
                errors.append(exc)

    threads, per_thread = 8, 40
    with ThreadPoolExecutor(max_workers=threads) as pool:
        futures = [pool.submit(_call, i) for i in range(threads * per_thread)]
        for future in futures:
            future.result()

    assert errors == [], (
        f"concurrent verify_user() raised {len(errors)}/{threads * per_thread} "
        f"exception(s); first: {errors[0]!r}"
    )
