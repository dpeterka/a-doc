"""a-doc — personal longitudinal medical diagnostic assistant."""

from importlib.metadata import PackageNotFoundError, version

try:
    # Single source of truth: the version declared in pyproject.toml, read
    # back from installed package metadata. A hand-maintained literal here
    # drifted silently from 0.10.0 through 0.16.0 — six releases stamping
    # `Provenance.app_version="0.10.0"` onto every artifact they produced,
    # which is exactly the record provenance exists to keep honest.
    __version__ = version("adoc")
except PackageNotFoundError:  # pragma: no cover - source tree without an install
    # Loudly wrong beats quietly stale: this must never look like a release.
    __version__ = "0.0.0+not-installed"
