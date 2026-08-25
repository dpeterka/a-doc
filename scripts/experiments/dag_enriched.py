"""Experiment profile: dag (full production-DAG diagnostic turn).

Runs ONE full diagnostic chat turn through the real production pipeline
(Ledger-Maintainer -> Challenger -> apply -> Composer) against the whole
case file, and writes the patient-facing reply to
`<data_dir>/case/experiments/dag.md` — comparable against the 'baseline'
profile's labs-only output (`case/experiments/baseline.md`).

**This mutates the ledger** (`case/differential-ledger.yaml`) in the
working data repo, exactly like a real diagnostic chat turn would. Never
run this against the safe store — refused below (`ADOC_SAFE_STORE`).

Invoked by `scripts/local-env.sh`'s `--experiment dag` via
`uv run python scripts/experiments/dag_enriched.py`. Prints ONLY metadata
(counts/durations/model ids) to stdout — never clinical content, since
this output lands in a terminal and a shell transcript (CLAUDE.md PHI
boundary rule 1).
"""

from __future__ import annotations

import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from adoc import __version__
from adoc.casefile.repo import LEDGER_RELPATH, DataRepo
from adoc.config import Settings
from adoc.labs.db import LabsDb
from adoc.privacy import Scrubber
from adoc.reason.client import LlmClient
from adoc.reason.dag import ContractViolation
from adoc.reason.safety import RedFlagResult
from adoc.reason.stages import run_diagnostic_turn

PROMPT = (
    "Based on my complete case file - my history, symptoms, and every lab "
    "result on record - what do you think is going on? Please give me your "
    "current differential."
)


def _refuse_if_safe_store(data_dir: Path) -> None:
    safe_store_env = os.environ.get("ADOC_SAFE_STORE")
    safe_store = (
        Path(safe_store_env).expanduser() if safe_store_env else Path.home() / "a-doc-data-local"
    )
    if data_dir.resolve() == safe_store.resolve():
        print(
            f"REFUSED: ADOC_DATA_DIR resolves to the safe store ({safe_store}); the 'dag' "
            "profile mutates the ledger and this tool never writes to the safe store. Point "
            "--dir at a working copy instead.",
            file=sys.stderr,
        )
        raise SystemExit(1)


def _build_llm_client(settings: Settings) -> LlmClient:
    """Mirrors `adoc.cli._build_llm_client`: real scrubbing + audit log,
    same as every other real LLM call this app makes."""
    scrubber = Scrubber.from_file(settings.data_dir / "case" / "identifiers.yaml")
    audit_log_path = settings.data_dir / "logs" / "api-audit.jsonl"
    return LlmClient.from_settings(settings, scrubber=scrubber, audit_log_path=audit_log_path)


def main() -> int:
    settings = Settings()
    _refuse_if_safe_store(settings.data_dir)
    print(
        "WARNING: the 'dag' experiment profile runs one real diagnostic turn through the "
        "production pipeline and WILL MUTATE case/differential-ledger.yaml in this working "
        "data repo, same as a real chat turn would.",
        file=sys.stderr,
    )

    repo = DataRepo(settings.data_dir)
    db = LabsDb(settings.data_dir / "labs.sqlite", journal_mode=settings.sqlite_journal_mode)
    client = _build_llm_client(settings)

    start = time.monotonic()
    try:
        outcome = run_diagnostic_turn(client, repo, db, repo.root / LEDGER_RELPATH, PROMPT)
    except ContractViolation as exc:
        duration = time.monotonic() - start
        # Only reason LABELS (the gate's own fixed vocabulary) leave this
        # process - never span text, which is generated clinical content.
        known_labels = ("dosage pattern", "imperative/hortative treatment instruction")
        histogram = {label: exc.message.count(f"({label})") for label in known_labels}
        print(
            f"METADATA: profile=dag CONTRACT-VIOLATION node={exc.node} "
            f"contract={exc.contract_name} duration={duration:.0f}s span_reasons={histogram} "
            f"message_chars={len(exc.message)}"
        )
        return 2
    duration = time.monotonic() - start

    if isinstance(outcome, RedFlagResult):
        print(f"METADATA: profile=dag red-flagged (no DAG run) duration={duration:.0f}s")
        return 0

    ledger_raw: dict[str, Any] = yaml.safe_load((repo.root / LEDGER_RELPATH).read_text()) or {}
    hyps = ledger_raw.get("hypotheses", [])
    tiers: dict[str, int] = {}
    origins: dict[str, int] = {}
    for h in hyps:
        tiers[h.get("tier", "?")] = tiers.get(h.get("tier", "?"), 0) + 1
        origins[h.get("origin", "?")] = origins.get(h.get("origin", "?"), 0) + 1

    out = repo.root / "case" / "experiments" / "dag.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        f"""# Experiment: dag (full production-DAG diagnostic turn)

- Generated: {datetime.now(UTC).isoformat()}
- App version: {__version__}
- Pipeline: Ledger-Maintainer -> Challenger -> apply -> Composer (production DAG)
- Prompt: "{PROMPT}"
- Duration: {duration:.0f}s
- Compare against: case/experiments/baseline.md (labs only, no case file, no ledger)

## Patient-facing reply (Composer output, post safety gate)

{outcome.tiers_rendered}

## Tests to request

{chr(10).join("- " + t for t in outcome.tests_to_request) or "(none)"}

## Framing acknowledgment

{outcome.framing_ack}
"""
    )
    repo.commit("experiment: full-DAG diagnostic run", paths=["case/experiments"])
    print(
        f"METADATA: profile=dag duration={duration:.0f}s "
        f"ledger_version={ledger_raw.get('version')} hypotheses={len(hyps)} tiers={tiers} "
        f"origins={origins} reply_chars={len(outcome.tiers_rendered)} "
        f"tests_to_request={len(outcome.tests_to_request)} written={out.relative_to(repo.root)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
