"""Deterministic PHI scrubbing applied to all outbound TEXT.

Every string sent to a model provider (system prompt, chat messages) passes
through a `Scrubber` first (see `reason/client.py`). Scrubbing is plain,
deterministic code — never delegated to a model (CLAUDE.md "Code
conventions") — and is intentionally conservative: it only replaces direct
identifiers (name/DOB/MRN/address/phone/email) it can positively match,
never clinical values (lab names, values, units, symptoms, diagnoses).

Two identifier sources feed the same `Scrubber`:
- `PatientIdentifiers` — literal values specific to this patient, loaded
  from `case/identifiers.yaml` in the (gitignored, no-remote) data repo —
  see `IDENTIFIERS_RELPATH`/`scaffold_identifiers_file`/`add_identifier`/
  `remove_identifier` below, and `adoc identifiers show|add|remove`. These
  are matched as whole-word/whole-phrase, case-insensitive literals so a
  name can never match as a substring inside an unrelated word.
- A fixed set of regex classes (SSN, phone, email, MRN-like) that catch
  identifiers of a recognizable *shape* even when they aren't in the
  `PatientIdentifiers` list (e.g. a phone number appearing in a scanned
  report's letterhead).

`LlmClient.from_settings` (`reason/client.py`) builds a real `Scrubber` from
this file by default — a caller must explicitly opt into `Scrubber.noop()`
to skip scrubbing (tests/dev only), rather than getting it by omission.

**Scope boundary — text only, not images.** This module scrubs *text*
content (system prompts, chat messages). It has no effect on the binary
document/page-image path (`ingest/vision.py`'s `VisionClient`, which sends
PDF blocks and rendered page PNGs to a vision-capable model): a scanned lab
report's letterhead shows the patient's name/DOB/address as *pixels*, which
no text regex or literal match can touch. This is a deliberate, accepted
limitation (docs/adr/0017-default-scrubber-and-identifiers-file.md) — OCR-
then-redact was considered and rejected as fragile and liable to damage the
very values the pipeline exists to extract — not an oversight, and nothing
in this module should be assumed to cover that path.
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
        self,
        identifiers: PatientIdentifiers | None = None,
        *,
        enabled: bool = True,
        source_path: Path | None = None,
    ) -> None:
        self._enabled = enabled
        self._identifiers = identifiers if identifiers is not None else PatientIdentifiers.empty()
        self._literal_patterns = self._build_literal_patterns(self._identifiers)
        # Tracked only so `coverage_warning` can name the exact file to
        # populate; `None` for a `Scrubber` built directly from in-memory
        # `PatientIdentifiers` (no file involved, e.g. `noop()` or a caller
        # constructing identifiers programmatically) rather than via
        # `from_file`.
        self._source_path = source_path

    @classmethod
    def from_file(cls, path: Path | None) -> Scrubber:
        """Build a `Scrubber` from an optional data-repo identifiers file."""
        return cls(PatientIdentifiers.load(path), source_path=path)

    @classmethod
    def noop(cls) -> Scrubber:
        """A `Scrubber` that never modifies text (for tests/dev only) — an
        explicit, obvious opt-out. Never the default for a real outbound
        call path; see `reason/client.py`'s `LlmClient.from_settings`."""
        return cls(PatientIdentifiers.empty(), enabled=False)

    @property
    def coverage_warning(self) -> str | None:
        """`None` if this scrubber is either an explicit no-op (`noop()` —
        deliberate, tests/dev only) or was built `from_file` against a file
        that exists and has at least one `names` entry configured.
        Otherwise, a human-readable warning naming the exact file to
        create/populate — for a caller about to talk to an external
        provider to surface loudly rather than silently degrade (see
        `cli.py`/`web/app.py`)."""
        if not self._enabled or self._source_path is None:
            return None
        if not self._source_path.exists():
            return (
                f"the identifiers file {self._source_path} does not exist - the patient's "
                "name/DOB/address will NOT be scrubbed from outbound model calls. Create it "
                'with `adoc identifiers add name "Patient Name"` (`adoc init` scaffolds an '
                "empty template automatically)."
            )
        if not self._identifiers.names:
            return (
                f"{self._source_path} has no 'names' entries - the patient's name will NOT "
                "be scrubbed from outbound model calls. Run `adoc identifiers add name "
                '"Patient Name"` to add one (see `adoc identifiers show`).'
            )
        return None

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


# --------------------------------------------------------------------------
# `case/identifiers.yaml` scaffolding + CLI-facing read/write helpers.
#
# `adoc init` (cli.py) calls `scaffold_identifiers_file` so a fresh data repo
# always has this file (even though it starts empty — `Scrubber.
# coverage_warning` is what makes the empty-until-populated state loud
# rather than silent). `adoc identifiers show|add|remove` (cli.py) call the
# rest so the owner is never hand-editing YAML blind.
# --------------------------------------------------------------------------

IDENTIFIERS_RELPATH = Path("case") / "identifiers.yaml"
"""Location of the per-patient identifiers file inside the data repo,
relative to `Settings.data_dir`."""

IDENTIFIERS_TEMPLATE = """\
# case/identifiers.yaml -- direct identifiers for THIS patient.
#
# Every value listed here is stripped from text sent to an external LLM
# provider (Anthropic/OpenAI/Featherless) before the request leaves the
# process -- see privacy.py and reason/client.py. Matching is whole-word/
# whole-phrase and case-insensitive, so list every form an identifier might
# appear in: full legal name, nicknames, a maiden name, "Last, First", etc.
#
# NOTE: this covers TEXT only. Scanned document images/PDFs sent for vision
# extraction (ingest/vision.py) are NOT covered -- the identifiers printed
# on the page are pixels, not text. See privacy.py's module docstring.
#
# This file lives only in the data repo (ADOC_DATA_DIR, no git remote) --
# never copy real values into the a-doc source repo or its test fixtures.
#
# Edit by hand, or use:
#   adoc identifiers show
#   adoc identifiers add    <name|dob|mrn|address|phone|email> <value>
#   adoc identifiers remove <name|dob|mrn|address|phone|email> <value>
#
# This file starts EMPTY. Until at least one name is added, the patient's
# name/DOB/address are NOT scrubbed from outbound model calls -- `adoc`
# warns loudly about this on every run until it's populated.

names: []               # e.g. ["Jane Q. Public", "Jane Public", "Janie"]
dob: null                # e.g. "1985-03-14"
mrn: []                  # e.g. ["123456", "MRN 123456"]
address_fragments: []    # e.g. ["123 Main St", "Springfield, IL 62704"]
phone: []                # e.g. ["217-555-0134"]
email: []                # e.g. ["jane.public@example.com"]
"""


def scaffold_identifiers_file(path: Path) -> bool:
    """Write the commented `IDENTIFIERS_TEMPLATE` at `path` if nothing is
    there yet. Returns `True` if it created the file, `False` if one
    already existed there (left untouched either way — this never
    overwrites real values)."""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(IDENTIFIERS_TEMPLATE, encoding="utf-8")
    return True


_LIST_FIELDS: dict[str, str] = {
    "name": "names",
    "mrn": "mrn",
    "address": "address_fragments",
    "phone": "phone",
    "email": "email",
}
IDENTIFIER_FIELDS: tuple[str, ...] = (*sorted(_LIST_FIELDS), "dob")
"""Every field name `adoc identifiers add|remove` accepts."""


def _save_identifiers_file(path: Path, identifiers: PatientIdentifiers) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    yaml = YAML(typ="safe")
    yaml.default_flow_style = False
    with path.open("w", encoding="utf-8") as fh:
        yaml.dump(identifiers.model_dump(mode="json"), fh)


def _unknown_field_error(field: str) -> ValueError:
    return ValueError(f"unknown identifiers field {field!r} (expected one of {IDENTIFIER_FIELDS})")


def add_identifier(path: Path, field: str, value: str) -> PatientIdentifiers:
    """Add `value` to `field` (one of `IDENTIFIER_FIELDS`) in the
    identifiers file at `path`, creating the file first if needed. `dob` is
    a scalar (this replaces any existing value); every other field is a
    list (adding a value already present is a no-op). Raises `ValueError`
    for an unknown field."""
    identifiers = PatientIdentifiers.load(path)
    if field == "dob":
        identifiers.dob = value
    elif field in _LIST_FIELDS:
        values = getattr(identifiers, _LIST_FIELDS[field])
        if value not in values:
            values.append(value)
    else:
        raise _unknown_field_error(field)
    _save_identifiers_file(path, identifiers)
    return identifiers


def remove_identifier(path: Path, field: str, value: str | None) -> tuple[PatientIdentifiers, bool]:
    """Remove `value` from `field` in the identifiers file at `path`.
    Returns `(identifiers, removed)`. `dob` ignores `value` and clears the
    field if it was set. Raises `ValueError` for an unknown field."""
    identifiers = PatientIdentifiers.load(path)
    removed = False
    if field == "dob":
        removed = identifiers.dob is not None
        identifiers.dob = None
    elif field in _LIST_FIELDS:
        values = getattr(identifiers, _LIST_FIELDS[field])
        if value is not None and value in values:
            values.remove(value)
            removed = True
    else:
        raise _unknown_field_error(field)
    if removed:
        _save_identifiers_file(path, identifiers)
    return identifiers, removed
