"""Versioned prompt-template loader.

Every file in `reason/prompts/*.md` is a system prompt for one DAG stage,
starting with a mandatory `<!-- version: N -->` header. `load_prompt`
parses that header and hashes the full file so callers (`reason/stages.py`)
can stamp `Provenance.prompt_template_version` on every LLM-derived
artifact (PLAN.md "Provenance & re-evaluation policy"). Prompt edits are
code — CLAUDE.md rule 2 requires the safety suite to pass before any change
to a template here can merge.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

_PROMPTS_DIR = Path(__file__).parent / "prompts"

_VERSION_HEADER_RE = re.compile(r"^<!--\s*version:\s*(\S+)\s*-->\s*\n")


@dataclass(frozen=True)
class Prompt:
    """A loaded prompt template: raw text, declared version, and content hash."""

    name: str
    text: str
    version: str
    sha256: str


class PromptError(Exception):
    """Raised when a prompt template file is missing or malformed."""


def load_prompt(name: str) -> Prompt:
    """Load `reason/prompts/<name>.md`, parsing its `<!-- version: N -->` header.

    Raises `PromptError` if the file does not exist or does not start with
    a well-formed version header.
    """
    path = _PROMPTS_DIR / f"{name}.md"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise PromptError(f"no such prompt template: {name!r} (looked for {path})") from exc

    match = _VERSION_HEADER_RE.match(text)
    if match is None:
        raise PromptError(
            f"prompt template {name!r} must start with a '<!-- version: N -->' header"
        )

    return Prompt(
        name=name,
        text=text,
        version=match.group(1),
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )
