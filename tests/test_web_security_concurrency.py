"""Concurrency regression test for `LoginRateLimiter` (SHOULD-FIX, same
shape as the `web.users` shared-`YAML()` bug — code review).

`LoginRateLimiter`'s docstring used to claim "uvicorn's default
single-worker/async-event-loop model never calls this from two threads at
once" — that was false: `web.routes.auth`'s `login_submit` is a sync `def`
route, so Starlette runs it in its sync-route thread pool, and concurrent
login attempts (exactly the credential-stuffing scenario this class exists
to defend against) really do call `is_locked`/`record_failure` from
multiple threads at once.

The concrete bug: `is_locked` used to do a read-modify-write over
`self._username_failures[username]` (read the list, prune it, write the
pruned copy back) that was NOT atomic across the whole operation. If
`record_failure` appended a new failure to the *same* underlying list
between another thread's read and its write-back inside `is_locked`, that
append got silently overwritten by the stale pruned copy `is_locked` wrote
back — a genuine failed login attempt vanishes from the count,
undercounting the only brute-force control on a public login surface with
no WAF, no VPN, and no TOTP (ADR 0007).

A plain thread-count-based test (N threads x M calls via
`ThreadPoolExecutor`, no coordination) does NOT reliably reproduce this:
each individual dict/list operation here is fast enough that CPython's GIL
rarely preempts mid-operation within a practical iteration budget, so that
style of test passed even with the lock removed (confirmed while writing
this test) - a real case of "the race exists but a naive concurrency test
won't reliably catch it." This test instead deterministically forces the
interleaving that used to lose an update:

1. Seed one failure.
2. Start a thread calling `is_locked`, whose `_prune` call is wrapped to
   pause (via a `threading.Event`) right after computing its pruned copy
   from the pre-seeded list but before `is_locked` writes that copy back
   - the exact window the bug lived in.
3. Once `is_locked` is confirmed paused there, start a second thread
   calling `record_failure` on the SAME username/IP and give it a brief,
   generous window to actually run (`record_failure` is a couple of dict/
   list operations - fast enough that this is a setup-ordering wait, not a
   hope-for-a-lucky-interleaving one). Pre-fix, nothing blocks it, so it
   appends straight through. Post-fix, `self._lock` makes it block until
   `is_locked` releases the lock, so it cannot run early.
4. Release the pause; `is_locked` finishes and writes its pruned copy
   back.

Without the lock, step 4's write-back overwrites step 3's already-applied
append and the final count is short by exactly one. With the lock, step 3
cannot complete until step 4 has already released the lock, so nothing is
lost.
"""

from __future__ import annotations

import threading
import time
import types

from adoc.web.security import LoginRateLimiter

USERNAME = "alice"
IP = "9.9.9.9"


def test_is_locked_and_record_failure_do_not_race_a_lost_update() -> None:
    limiter = LoginRateLimiter(username_limit=1000, ip_limit=1000)
    limiter.record_failure(username=USERNAME, ip=IP)  # seed one failure

    entered_prune = threading.Event()
    release_prune = threading.Event()
    original_prune = LoginRateLimiter._prune

    def paused_prune(self: LoginRateLimiter, failures: list[float], now: float) -> list[float]:
        # Compute the pruned copy exactly as the real method would (a
        # snapshot of `failures` as it stood at call time), then pause
        # AFTER that snapshot but BEFORE `is_locked` writes it back.
        result = original_prune(self, failures, now)
        entered_prune.set()
        assert release_prune.wait(timeout=5), "record_failure thread never signaled release"
        return result

    limiter._prune = types.MethodType(paused_prune, limiter)  # type: ignore[method-assign]

    locker = threading.Thread(
        target=lambda: limiter.is_locked(username=USERNAME, ip=IP), daemon=True
    )
    recorder = threading.Thread(
        target=lambda: limiter.record_failure(username=USERNAME, ip=IP), daemon=True
    )

    locker.start()
    assert entered_prune.wait(timeout=5), "is_locked never reached the paused _prune call"

    recorder.start()
    # Generous, fixed setup window for `record_failure` to actually run its
    # (fast) dict/list operations pre-fix, or to reach and block on
    # `self._lock` post-fix - not a "hope it interleaves" sleep.
    time.sleep(0.2)

    release_prune.set()
    locker.join(timeout=5)
    recorder.join(timeout=5)
    assert not locker.is_alive()
    assert not recorder.is_alive()

    username_failures = limiter._username_failures[USERNAME]  # noqa: SLF001 - white-box check
    assert len(username_failures) == 2, (
        f"expected 2 recorded username failures (1 seeded + 1 concurrent), got "
        f"{len(username_failures)} - the concurrent record_failure() was lost to the "
        f"is_locked() read-modify-write race"
    )
