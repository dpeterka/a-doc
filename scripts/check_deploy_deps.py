#!/usr/bin/env python3
"""Verify the live deployment has everything it needs.

`docs/deployment-dependencies.md` is the prose; this is the check. It exists
because LIRICAL was containerised, pushed, given a task definition and IAM,
and never ran once — nobody had wired `ADOC_LIRICAL_CLUSTER` into a task
definition, and every layer looked complete in isolation.

Almost every dependency here fails SILENTLY by design: a review must not die
because a reference index is missing. The cost of that choice is that absence
looks exactly like working, so it has to be checked deliberately.

    python scripts/check_deploy_deps.py             # task definitions, from AWS
    python scripts/check_deploy_deps.py --in-task   # also the files, from inside a task

Exit 0 when everything required is present, 1 otherwise. Optional entries are
reported but never fail the run.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

TASK_FAMILIES = ("a-doc-web", "a-doc-jobs")

# (variable, required, what breaks without it)
REQUIRED_ENV: tuple[tuple[str, bool, str], ...] = (
    ("ADOC_DATA_DIR", True, "Settings() raises — the only loud failure here"),
    ("ADOC_SQLITE_JOURNAL_MODE", True, "WAL on EFS is unsafe (labs/db.py)"),
    ("ADOC_BACKUP_BUCKET", True, "backups silently no-op"),
    ("ADOC_TRUST_FORWARDED_FOR", False, "client IPs wrong in rate limiting"),
    ("ADOC_LIRICAL_CLUSTER", True, "LIRICAL never runs: 'not configured', 0.0s"),
    ("ADOC_LIRICAL_TASK_DEFINITION", True, "LIRICAL never runs"),
    ("ADOC_LIRICAL_SUBNETS", True, "run_task places nothing"),
    ("ADOC_LIRICAL_SECURITY_GROUPS", True, "run_task places nothing"),
)

REQUIRED_SECRETS = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "FEATHERLESS_API_KEY",
    "RCLONE_CONF",
)

# Reference indexes baked into the image. All degrade silently.
REFERENCE_FILES: tuple[tuple[str, str], ...] = (
    ("/opt/hpo-index.json", "phenotype matching off"),
    ("/opt/semsim-index.json", "similarity engine skips"),
    ("/opt/mondo-index.json", "vocabulary mismatch read as disagreement"),
    ("/opt/orphadata-index.json", "no definitions or prevalence"),
    ("/opt/statpearls.sqlite", "no clinical review text"),
)


def _describe(family: str) -> dict | None:
    try:
        out = subprocess.run(
            [
                "aws",
                "ecs",
                "describe-task-definition",
                "--task-definition",
                family,
                # Explicit: the caller's AWS config may set a different default
                # output format, and this parses the result as JSON.
                "--output",
                "json",
                "--no-cli-pager",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001 - report, do not raise
        print(f"  ! could not describe {family}: {type(exc).__name__}: {exc}")
        return None
    if out.returncode != 0:
        print(f"  ! could not describe {family}: {out.stderr.strip()[:160]}")
        return None
    try:
        return json.loads(out.stdout)["taskDefinition"]
    except (ValueError, KeyError) as exc:
        print(f"  ! {family}: unreadable describe output ({type(exc).__name__})")
        return None


def check_task_definitions() -> int:
    failures = 0
    for family in TASK_FAMILIES:
        print(f"\n{family}")
        described = _describe(family)
        if described is None:
            failures += 1
            continue
        container = described["containerDefinitions"][0]
        env = {e["name"] for e in container.get("environment", [])}
        secrets = {s["name"] for s in container.get("secrets", [])}

        for name, required, consequence in REQUIRED_ENV:
            if name in env:
                print(f"  ok       {name}")
            elif required:
                print(f"  MISSING  {name} — {consequence}")
                failures += 1
            else:
                print(f"  absent   {name} (optional) — {consequence}")

        for name in REQUIRED_SECRETS:
            if name in secrets:
                print(f"  ok       {name} (secret)")
            else:
                print(f"  MISSING  {name} (secret)")
                failures += 1
    return failures


def check_reference_files() -> int:
    """Only meaningful inside a task — these live in the image."""
    print("\nreference data (this container)")
    failures = 0
    for path, consequence in REFERENCE_FILES:
        p = Path(path)
        if p.exists():
            print(f"  ok       {path}  {p.stat().st_size // 1_000_000}MB")
        else:
            print(f"  MISSING  {path} — {consequence}")
            failures += 1
    return failures


def check_lirical_paths(root: Path) -> int:
    """The runner and the sidecar image must name the same data directory.

    They did not, and every launched task exited 1 on missing data files. The
    sidecar's own build-time smoke test could not catch it: that test uses the
    image's `$LIRICAL_DATA`, so it exercised the correct path while the only
    real caller passed a different one.
    """
    import re

    print("\nlirical data directory")
    dockerfile = root / "deploy" / "lirical" / "Dockerfile"
    runner = root / "src" / "adoc" / "knowledge" / "lirical_runner.py"
    if not dockerfile.is_file() or not runner.is_file():
        print("  skipped  (not run from a checkout)")
        return 0

    image = re.search(r"^ENV LIRICAL_DATA=(\S+)", dockerfile.read_text(), re.M)
    app = re.search(r'^LIRICAL_DATA_DIR = "([^"]+)"', runner.read_text(), re.M)
    if not image or not app:
        print("  MISSING  could not read one of the two declarations")
        return 1
    if image.group(1) != app.group(1):
        print(f"  MISMATCH image={image.group(1)}  runner={app.group(1)}")
        print("           every launched task will exit 1 on missing data files")
        return 1
    print(f"  ok       both name {app.group(1)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--in-task",
        action="store_true",
        help="also check the reference indexes on this container's filesystem",
    )
    parser.add_argument("--root", default=".", help="repo root")
    args = parser.parse_args(argv)

    print("check-deploy-deps: see docs/deployment-dependencies.md")
    failures = check_lirical_paths(Path(args.root))
    failures += check_reference_files() if args.in_task else check_task_definitions()

    print()
    if failures:
        print(f"check-deploy-deps: {failures} missing — the deployment is incomplete")
        return 1
    print("check-deploy-deps: OK")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
