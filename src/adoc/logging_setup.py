"""One place that decides where log output goes.

Without this, the root logger has no handler, so `logging.lastResort` prints
WARNING and above to stderr and silently drops every INFO line — including
the per-DAG-node progress (`reason.dag`) and per-model-call timings
(`reason.client`) that explain why a turn is taking minutes. A diagnostic
turn that logs nothing until it fails is indistinguishable from a hung one.

Every entrypoint calls this: the CLI (so `serve`, `review`, `ingest`, and
the rest all behave the same) and the standalone scripts under `scripts/`,
which run out-of-process and would otherwise be silent.
"""

from __future__ import annotations

import logging

_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# httpx logs one INFO line per request. Useful for a single call, pure noise
# across a multi-node DAG run where every stage already logs its own timing.
_NOISY_LOGGERS = ("httpx", "httpx2", "httpcore")


def configure_logging(*, level: int = logging.INFO) -> None:
    """Attach a stderr handler to the root logger, once.

    Safe to call more than once: `basicConfig` is a no-op when the root
    logger already has handlers, which is also what keeps this from
    fighting uvicorn's own `dictConfig` (it sets
    `disable_existing_loggers: False` and touches only the `uvicorn.*`
    loggers, so the root configuration here survives `uvicorn.run`).
    """
    logging.basicConfig(level=level, format=_LOG_FORMAT, datefmt=_DATE_FORMAT)
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
