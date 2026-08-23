"""Deterministic PHI scrubbing applied to all outbound text.

Every string sent to a model provider (system prompt, chat messages) passes
through a `Scrubber` first (see `reason/client.py`). Scrubbing is plain,
deterministic code — never delegated to a model (CLAUDE.md "Code
conventions") — and is intentionally conservative: it only replaces direct
identifiers (name/DOB/MRN/address/phone/email) it can positively match,
never clinical values (lab names, values, units, symptoms, diagnoses).

Two identifier sources feed the same `Scrubber`:
- `PatientIdentifiers` — literal values specific to this patient, optionally
  loaded from a file in the (gitignored, no-remote) data repo. These are
  matched as whole-word/whole-phrase, case-insensitive literals so a name
  can never match as a substring inside an unrelated word.
- A fixed set of regex classes (SSN, phone, email, MRN-like) that catch
  identifiers of a recognizable *shape* even when they aren't in the
  `PatientIdentifiers` list (e.g. a phone number appearing in a scanned
  report's letterhead).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple

from pydantic import BaseModel, Field
from ruamel.yaml import YAML

# Replacement tokens are stable strings so scrubbed text stays reasonably
# readable to the model (and to a human auditing logs) without ever leaking
# the underlying value.
NAME_TOKEN = "[NAME]"
DOB_TOKEN = "[DOB]"
MRN_TOKEN = "[MRN]"
ADDRESS_TOKEN = "[ADDRESS]"
PHONE_TOKEN = "[PHONE]"
EMAIL_TOKEN = "[EMAIL]"
SSN_TOKEN = "[SSN]"


class PatientIdentifiers(BaseModel):
    """Direct identifiers for the (single) patient this instance serves.

    All fields are optional/empty by default so a fresh install with no
    identifiers file configured still gets the regex-class scrubbing below
    (SSN/phone/email/MRN-shaped strings), just not patient-specific literal
    matching.
    """

    names: list[str] = Field(default_factory=list)
    dob: str | None = None
    mrn: list[str] = Field(default_factory=list)
    address_fragments: list[str] = Field(default_factory=list)
    phone: list[str] = Field(default_factory=list)
    email: list[str] = Field(default_factory=list)

    @classmethod
    def empty(cls) -> PatientIdentifiers:
        return cls()

    @classmethod
    def load(cls, path: Path | None) -> PatientIdentifiers:
        """Load identifiers from an optional data-repo YAML file.

        Returns an empty (no-op-for-literals) instance if `path` is `None`
        or does not exist — the caller is never required to have identifiers
        configured for `Scrubber` to be safe to use.
        """
        if path is None or not path.exists():
            return cls.empty()
        yaml = YAML(typ="safe")
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.load(fh) or {}
        return cls.model_validate(data)


class _LiteralPattern(NamedTuple):
    token: str
    regex: re.Pattern[str]


class _RegexClass(NamedTuple):
    token: str
    regex: re.Pattern[str]


def _literal_pattern(token: str, value: str) -> _LiteralPattern | None:
    stripped = value.strip()
    if not stripped:
        return None
    # \b...\b (word-boundary) anchoring is what makes "name inside a word
    # must not match" hold: re.escape(stripped) followed by \b only matches
    # when the character after the literal is a non-word character (or end
    # of string), so "Ann" never matches inside "Anniversary".
    regex = re.compile(rf"\b{re.escape(stripped)}\b", re.IGNORECASE)
    return _LiteralPattern(token=token, regex=regex)


# Fixed regex classes, always active regardless of PatientIdentifiers.
# Deliberately narrow shapes so ordinary clinical values (lab results,
# reference ranges, vitals like "120/80") are never caught:
#   - SSN requires the exact 3-2-4 digit-dash grouping.
#   - Phone requires a full 10-digit US-shaped number.
#   - MRN requires an explicit "MRN" label immediately before the digits.
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
# `(?<!\w)`/`(?!\w)` rather than `\b` on the outer edges: the leading `(`
# of a parenthesized area code is itself non-word, so a `\b` anchored right
# before it would never be satisfied (non-word -> non-word is not a
# boundary) and the match would start one character late, dropping the
# `(` from the replacement.
_PHONE_RE = re.compile(r"(?<!\w)(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}(?!\w)")
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_MRN_RE = re.compile(r"\bMRN[:#]?\s*\d{4,10}\b", re.IGNORECASE)

_REGEX_CLASSES: tuple[_RegexClass, ...] = (
    _RegexClass(token=SSN_TOKEN, regex=_SSN_RE),
    _RegexClass(token=PHONE_TOKEN, regex=_PHONE_RE),
    _RegexClass(token=EMAIL_TOKEN, regex=_EMAIL_RE),
    _RegexClass(token=MRN_TOKEN, regex=_MRN_RE),
)


class Scrubber:
    """Deterministic PHI scrubber built from `PatientIdentifiers`.

    `scrub(text)` returns `(scrubbed_text, replacement_count)`. Construction
    is cheap and side-effect free; a `Scrubber` is safe to share/reuse across
    many calls (it holds only compiled regexes).
    """

    def __init__(
        self, identifiers: PatientIdentifiers | None = None, *, enabled: bool = True
    ) -> None:
        self._enabled = enabled
        self._identifiers = identifiers if identifiers is not None else PatientIdentifiers.empty()
        self._literal_patterns = self._build_literal_patterns(self._identifiers)

    @classmethod
    def from_file(cls, path: Path | None) -> Scrubber:
        """Build a `Scrubber` from an optional data-repo identifiers file."""
        return cls(PatientIdentifiers.load(path))

    @classmethod
    def noop(cls) -> Scrubber:
        """A `Scrubber` that never modifies text (for tests/dev only)."""
        return cls(PatientIdentifiers.empty(), enabled=False)

    @staticmethod
    def _build_literal_patterns(identifiers: PatientIdentifiers) -> tuple[_LiteralPattern, ...]:
        candidates: list[tuple[str, str]] = [(NAME_TOKEN, name) for name in identifiers.names]
        if identifiers.dob:
            candidates.append((DOB_TOKEN, identifiers.dob))
        candidates += [(MRN_TOKEN, mrn) for mrn in identifiers.mrn]
        candidates += [(ADDRESS_TOKEN, frag) for frag in identifiers.address_fragments]
        candidates += [(PHONE_TOKEN, phone) for phone in identifiers.phone]
        candidates += [(EMAIL_TOKEN, email) for email in identifiers.email]

        patterns = [
            pattern for token, value in candidates if (pattern := _literal_pattern(token, value))
        ]
        # Longest literal first so a longer identifier (e.g. a full name) is
        # consumed before a shorter one that happens to be a substring of it
        # (e.g. a first name alone) could partially match instead.
        patterns.sort(key=lambda p: len(p.regex.pattern), reverse=True)
        return tuple(patterns)

    def scrub(self, text: str) -> tuple[str, int]:
        """Replace direct identifiers in `text` with stable tokens.

        Never touches clinical content: only patient-specific literals
        (names/DOB/MRN/address/phone/email, matched whole-word,
        case-insensitively) and the fixed SSN/phone/email/MRN-shaped regex
        classes are replaced.
        """
        if not self._enabled:
            return text, 0

        result = text
        total = 0
        for token, regex in self._literal_patterns:
            result, count = regex.subn(token, result)
            total += count
        for token, regex in _REGEX_CLASSES:
            result, count = regex.subn(token, result)
            total += count
        return result, total

    def __call__(self, text: str) -> tuple[str, int]:
        return self.scrub(text)
