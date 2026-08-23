"""Smoke tests: package import, version, and subpackage importability.

The subpackage imports below are not just a sanity check — they are what
makes the 70% coverage gate honest for Phase 0. The subpackages
(casefile/labs/ingest/intake/reason/evals/knowledge/web) are currently empty
placeholders with only a docstring; importing them here means coverage.py
counts them as executed rather than silently reporting 0% for files no test
ever touches.
"""

from __future__ import annotations

import importlib

import adoc

_SUBPACKAGES = [
    "adoc.casefile",
    "adoc.labs",
    "adoc.ingest",
    "adoc.intake",
    "adoc.reason",
    "adoc.evals",
    "adoc.knowledge",
    "adoc.web",
]


def test_version() -> None:
    assert adoc.__version__ == "0.1.0"


def test_subpackages_import() -> None:
    for name in _SUBPACKAGES:
        module = importlib.import_module(name)
        assert module.__doc__ is not None
