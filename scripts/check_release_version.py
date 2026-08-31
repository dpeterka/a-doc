#!/usr/bin/env python3
"""Fail if a release's declared version disagrees with the package.

`v0.20.0` was tagged and deployed from a branch whose `chore(release)` commit
was never made: the version bump and changelog edits were left unstaged, so
the tag said 0.20.0 while `pyproject.toml`, `adoc.__version__`, and every
provenance stamp the deploy produced all said 0.19.0.

`tests/test_version.py` cannot catch that. It pins `pyproject.toml` and
`adoc.__version__` to each other, and those two agreed perfectly — they were
merely stale with respect to the release they were being shipped as. The
missing check is against the name of the release itself.

Two call sites, both cheap:

  check_release_version.py --branch release/0.21.0   (PR gate, before merge)
  check_release_version.py --tag v0.21.0             (tag gate, after)

The branch form is the one that matters, because deploy fires on the push to
`main` — which happens BEFORE the tag exists. A tag-time check would only ever
tell you that something already shipped mislabelled.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+)")


def packaged_version(root: Path) -> str:
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def declared_version(label: str) -> str | None:
    """The version named by a branch (`release/1.2.3`) or tag (`v1.2.3`)."""
    match = _VERSION_RE.search(label)
    return match.group(1) if match else None


def check_backmerge(main_version: str, develop_version: str) -> int:
    """Fail when `develop` is behind `main`'s released version.

    Every release is merged to `main` and must be merged back to `develop`, or
    develop keeps building on a version that has already shipped. It has been
    missed three times in this project, and each time the symptom was the same
    and arrived late: the NEXT release bump found no version string to
    replace, produced an empty commit, and opened a pull request containing
    nothing.

    Comparing the two branches after a release turns that into an immediate,
    named failure instead of a puzzle one release later.
    """
    if main_version == develop_version:
        print(f"check-release-version: OK — develop and main agree at {main_version}")
        return 0
    print(
        f"check-release-version: develop is at {develop_version} but main has released "
        f"{main_version} — the back-merge after that release was missed. Merge main into "
        "develop before cutting the next release.",
        file=sys.stderr,
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--branch", default="", help="e.g. release/0.21.0")
    parser.add_argument("--tag", default="", help="e.g. v0.21.0")
    parser.add_argument("--root", default=".", help="repo root")
    parser.add_argument(
        "--backmerge",
        nargs=2,
        metavar=("MAIN_VERSION", "DEVELOP_VERSION"),
        help="fail when develop is behind main's released version",
    )
    args = parser.parse_args(argv)

    if args.backmerge:
        return check_backmerge(*args.backmerge)

    label = args.branch or args.tag
    if not label:
        print("check-release-version: nothing to check (no --branch/--tag)")
        return 0

    declared = declared_version(label)
    if declared is None:
        # Not a release ref. Ordinary feature branches must not be gated.
        print(f"check-release-version: {label!r} names no version; nothing to check")
        return 0

    packaged = packaged_version(Path(args.root))
    if declared != packaged:
        print(
            f"check-release-version: FAIL\n"
            f"  {label} declares {declared}\n"
            f"  pyproject.toml says {packaged}\n"
            f"\n"
            f"The version bump was probably never committed — check for unstaged\n"
            f"changes to pyproject.toml and CHANGELOG.md. Shipping this would tag\n"
            f"a release whose package, and every provenance stamp it writes,\n"
            f"reports a different version.",
            file=sys.stderr,
        )
        return 1

    print(f"check-release-version: OK — {label} matches pyproject {packaged}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
