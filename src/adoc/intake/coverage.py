"""Coverage-map state for the conversational intake engine
(`docs/adr/0012-initial-visit-conversation.md`).

Replaces the cursor/per-section status machine (`intake.wizard.IntakeState`/
`SectionState`, still used verbatim by the `--legacy-wizard` escape hatch)
with a flat per-topic coverage map: `covered: bool` + `covered_at`. There is
no "current topic" and no stepping — the conversational engine
(`intake.agent`) may capture facts for any topic in any turn, and a topic
becomes `covered` only when the deterministic gate
(`intake.facts.section_completion_blockers`) finds nothing blocking it.
`intake_complete` is a separate, monotonic flag: once every topic is
covered and no blocker remains anywhere, `intake.agent.run_intake_turn`
sets it and never clears it again — this is what unlocks the diagnostic
chat pipeline (`web.routes.chat`).

Both this module's `CoverageState` and the legacy wizard's `IntakeState`
persist to the same path (`case/intake-state.yaml`) — the two onboarding
paths are not intended to be interleaved against the same data repo, but
`load_coverage_state` still migrates an old-style (sections/cursor) file on
read so a repo that was onboarded under `--legacy-wizard` for a while and
then switches to the default conversational engine keeps its progress
(`complete` -> `covered`) rather than silently resetting.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from ruamel.yaml import YAML

from adoc.intake.wizard import INTAKE_STATE_RELPATH

__all__ = [
    "INTAKE_STATE_RELPATH",
    "CoverageState",
    "TopicCoverage",
    "load_coverage_state",
    "save_coverage_state",
]


class TopicCoverage(BaseModel):
    """Per-topic coverage: whether the deterministic gate has ever accepted
    this topic as covered, and when."""

    covered: bool = False
    covered_at: datetime | None = None


class CoverageState(BaseModel):
    """The full `case/intake-state.yaml` document under the new engine."""

    topics: dict[str, TopicCoverage] = Field(default_factory=dict)
    intake_complete: bool = False


def _migrate_legacy(raw: dict[str, Any]) -> CoverageState:
    """Migrate an old-style `{"sections": {key: {"status": ..., "completed_at":
    ...}}, "cursor": ...}` document (written by `intake.wizard`/the pre-0012
    conversational engine) into a `CoverageState`: a section whose `status`
    was `"complete"` migrates to `covered=True` (carrying `completed_at`
    forward as `covered_at`); `cursor is None` with at least one section on
    file means every section had already been completed, so the whole
    intake counts as complete too.
    """
    old_sections: dict[str, Any] = raw.get("sections") or {}
    topics: dict[str, Any] = {}
    for key, value in old_sections.items():
        value = value or {}
        covered = value.get("status") == "complete"
        topics[key] = {
            "covered": covered,
            "covered_at": value.get("completed_at") if covered else None,
        }
    intake_complete = bool(old_sections) and raw.get("cursor") is None
    return CoverageState.model_validate({"topics": topics, "intake_complete": intake_complete})


def load_coverage_state(path: Path) -> CoverageState:
    """Load `case/intake-state.yaml` as a `CoverageState`. A missing file
    yields a fresh, empty `CoverageState`. An old-style (sections/cursor)
    file is transparently migrated (see `_migrate_legacy`)."""
    if not path.exists():
        return CoverageState()
    yaml = YAML(typ="safe")
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.load(fh) or {}
    if "topics" in raw:
        return CoverageState.model_validate(raw)
    return _migrate_legacy(raw)


def save_coverage_state(path: Path, state: CoverageState) -> None:
    """Write `state` to `path` as stable, human-diffable YAML."""
    data = state.model_dump(mode="json")
    yaml = YAML()
    yaml.default_flow_style = False
    yaml.indent(mapping=2, sequence=4, offset=2)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        yaml.dump(data, fh)
