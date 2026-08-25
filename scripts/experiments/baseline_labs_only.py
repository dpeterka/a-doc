"""Experiment profile: baseline (labs-only single-shot control).

One single-shot completion against ONLY the extracted lab data: no case
summary, no onboarding narrative, no patient theories, no ledger — the
"what would a plain LLM conclude from the documents alone" condition,
comparable against the 'dag' profile's full-pipeline output
(case/experiments/dag.md).

Invoked by `scripts/local-env.sh`'s `--experiment baseline` (alias
`study` — see `start-local --help` for that interpretation) via
`uv run python scripts/experiments/baseline_labs_only.py`, against
whatever `ADOC_DATA_DIR` the caller has already exported. Writes the full
model output to `<data_dir>/case/experiments/baseline.md` and commits it;
stdout carries ONLY metadata (counts/durations/model ids/token usage),
never clinical content — this output lands in a terminal and a shell
transcript (CLAUDE.md PHI boundary rule 1).

Refuses to run against the safe store (`ADOC_SAFE_STORE`) — this tool
never writes there.
"""

from __future__ import annotations

import os
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from adoc import __version__
from adoc.casefile.repo import DataRepo
from adoc.config import Settings
from adoc.labs.db import LabsDb
from adoc.labs.models import LabResult
from adoc.privacy import Scrubber
from adoc.reason.client import LlmClient, Message

PROMPT = (
    "Below is the complete longitudinal laboratory record extracted from one "
    "patient's medical documents - every analyte, every date, values, units, "
    "reference ranges, and abnormal flags. You have NO other information "
    "about this patient (no history, no symptoms, no demographics beyond "
    "what the labs imply).\n\n"
    "Based purely on this lab data: what diagnoses would you consider, and "
    "why? Give a tiered differential (most likely / worth expanding on / "
    "can't-miss), citing the specific lab findings and trends that drive "
    "each hypothesis, and note what additional testing would best "
    "discriminate between them."
)

SYSTEM = (
    "You are a careful diagnostician reviewing raw laboratory data. Reason "
    "from the data alone; say so explicitly when the labs are insufficient "
    "to support or exclude a hypothesis. Do not recommend treatments or "
    "dosing - testing suggestions only."
)


def _refuse_if_safe_store(data_dir: Path) -> None:
    safe_store_env = os.environ.get("ADOC_SAFE_STORE")
    safe_store = (
        Path(safe_store_env).expanduser() if safe_store_env else Path.home() / "a-doc-data-local"
    )
    if data_dir.resolve() == safe_store.resolve():
        print(
            f"REFUSED: ADOC_DATA_DIR resolves to the safe store ({safe_store}); this tool "
            "never writes there. Point --dir at a working copy instead.",
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

    repo = DataRepo(settings.data_dir)
    db = LabsDb(settings.data_dir / "labs.sqlite", journal_mode=settings.sqlite_journal_mode)
    client = _build_llm_client(settings)

    rows = db.all_non_rejected_rows()
    series: dict[tuple[str, str], list[LabResult]] = defaultdict(list)
    for r in rows:
        series[(r.name, r.specimen)].append(r)

    lines: list[str] = []
    for (name, specimen), items in sorted(series.items()):
        label = name if specimen == "unknown" else f"{name} [{specimen}]"
        lines.append(f"{label}:")
        for r in sorted(items, key=lambda x: x.date):
            value = r.value if r.value is not None else (r.value_text or "?")
            unit = f" {r.ucum_unit}" if r.ucum_unit else ""
            ref = f" (ref {r.ref_text})" if r.ref_text else ""
            flag = f" [{r.flag}]" if r.flag else ""
            lines.append(f"  {r.date.isoformat()}: {value}{unit}{ref}{flag}")
    labs_block = "\n".join(lines)

    start = time.monotonic()
    result = client.complete(
        "primary_reasoner",
        system=SYSTEM,
        messages=[Message(role="user", content=f"{PROMPT}\n\n---\n\n{labs_block}")],
    )
    duration = time.monotonic() - start

    out = repo.root / "case" / "experiments" / "baseline.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        f"""# Experiment: baseline (labs-only single-shot control)

- Generated: {datetime.now(UTC).isoformat()}
- App version: {__version__}
- Condition: ONE completion, lab data only - no case file, no onboarding
  narrative, no ledger, no Challenger, no safety DAG.
- Model: {result.model_id}
- Input: {len(rows)} lab rows across {len(series)} analyte series
- Duration: {duration:.0f}s
- Tokens: {result.usage.input_tokens} in / {result.usage.output_tokens} out
- Compare against: case/experiments/dag.md (production DAG on the full case file)

## Model output (verbatim, ungated - experimental artifact, not medical advice)

{result.text}
"""
    )
    repo.commit("experiment: labs-only baseline run", paths=["case/experiments"])

    print(
        f"METADATA: profile=baseline rows={len(rows)} series={len(series)} "
        f"duration={duration:.0f}s model={result.model_id} "
        f"tokens_in={result.usage.input_tokens} tokens_out={result.usage.output_tokens} "
        f"cost_est={result.cost_estimate} reply_chars={len(result.text)} "
        f"written={out.relative_to(repo.root)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
