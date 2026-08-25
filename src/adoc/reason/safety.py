"""Deterministic safety gates (PLAN.md "Safety"). NO LLM calls in this module.

`treatment_gate`: a deterministic scan of patient-facing output that blocks
dosing/prescriptive treatment instructions before they ever reach the
patient (CLAUDE.md rule 5). A bare number+unit alone is not sufficient to
flag as a dose — see `_CONTEXT_DOSAGE_RE` and ADR 0020 — because a dose is
part of an instruction and a measurement is part of a finding.

This module previously also carried a keyword/regex screen for emergency
presentations plus its warn-not-block wiring. It was removed (see
`docs/adr/0021*.md`): in live use it only ever produced false positives
(most memorably, "our home has a septic system and a well" flagged as a
medical emergency) and never caught a real one, because a patient having a
genuine medical emergency does not type it into this app. See that ADR for
the full reasoning and what is deliberately given up — there is now no
automated emergency detection anywhere in the system.

`treatment_gate` is deliberately conservative in what it DETECTS: a false
positive here (a rewrite request on benign text) costs nothing but a little
friction; a false negative on a real dosing instruction is the failure mode
this module exists to prevent.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------
# Treatment / dosing output gate
# --------------------------------------------------------------------------


class GateSpan(BaseModel):
    """One blocked span of patient-facing text, with why it was blocked."""

    start: int
    end: int
    text: str
    reason: str


class GateResult(BaseModel):
    """Result of `treatment_gate`. `rewrite_instruction` is populated
    whenever `passed` is `False` — a ready-to-use instruction for asking a
    composer stage to rewrite the offending text."""

    passed: bool
    spans: list[GateSpan] = Field(default_factory=list)
    rewrite_instruction: str | None = None


_REWRITE_INSTRUCTION = (
    "Rewrite this response to remove any specific drug name, dose, or instruction to "
    "start/stop/increase/decrease/taper a medication or supplement. Instead, name the "
    "relevant lab tests to request, the types of specialists to see, or general "
    "categories of options — framed as leads to discuss with your doctor, never as "
    "an instruction to follow on your own."
)

# A dose is part of an INSTRUCTION; a measurement is part of a FINDING. A
# bare number+unit is not enough on its own to tell the two apart — see
# ADR 0020. The `(?!...)` tail on both regexes below excludes the common
# *lab-result* concentration denominators (mg/dL, mg/L, mg/mL, mg/mcL) so a
# quantitative lab value ("CRP 8 mg/L") is never mistaken for a dosing
# instruction — those denominators never appear after a genuine dose.
#
# mg/mcg/iu fire unconditionally, with no context requirement: in this
# patient's record a bare (denominator-free) mg/mcg/iu amount is
# overwhelmingly a medication or supplement dose ("20 mg prednisone",
# "5000 IU vitamin D", "50 mcg levothyroxine") — real lab values that use
# these units almost always carry a denominator (mg/dL, ng/mL, mIU/mL...)
# already handled by the concentration carve-out or by simply not matching
# these unit tokens at all (see `_CONTEXT_DOSAGE_RE`'s docstring-comment for
# the ng/mL case). This is a deliberate judgment call, not an oversight: it
# accepts a small residual false-positive risk (a rare bare-mg measurement,
# e.g. a kidney-stone weight) to keep the far more common real dose caught
# without needing corroborating context.
_DENOMINATOR_EXCLUSION = r"(?!\s*/\s*(?:dl|l|ml|mcl)\b)"

_STRONG_DOSAGE_RE = re.compile(
    rf"\b\d+(?:\.\d+)?\s*(?:mg|mcg|iu)\b{_DENOMINATOR_EXCLUSION}",
    re.IGNORECASE,
)

# g/ml/units, by contrast, are common bare units for ordinary clinical
# MEASUREMENTS that have nothing to do with dosing: an ultrasound or urine
# volume in mL ("106.0 mL" — the real production incident that motivated
# this split, ADR 0020), a specimen or organ mass in g, a bone-density
# numerator in g (as in g/cm²), or a transfused-blood/lab-panel "units"
# count. So a bare number+unit in this group is only treated as a dosage
# span when the same clause also carries independent dosing context — see
# `_has_dosing_context`. Note "22 ng/mL" never reaches this regex at all:
# the unit token immediately after the number is "ng", not one this pattern
# matches, so it is unflagged regardless of context.
_CONTEXT_DOSAGE_RE = re.compile(
    rf"\b\d+(?:\.\d+)?\s*(?:g|units?|ml)\b{_DENOMINATOR_EXCLUSION}",
    re.IGNORECASE,
)

# Imperative/hortative treatment-construction verbs (base + inflected forms,
# including the gerund — "stop TAKING your prednisone", "I recommend
# TAPERING") that anchor an instruction to start/stop/change a medication.
# Deliberately word-level, not phrase-level: "you should take", "I recommend
# tapering", "consider taking" are all caught by the bare verb form itself
# ("take"/"tapering"/"taking") without needing to special-case the hortative
# prefix — the prefix never changes whether the sentence is an instruction.
_IMPERATIVE_VERB_FORMS: dict[str, str] = {
    form: base
    for base, forms in {
        "take": ("take", "takes", "taking", "took"),
        "start": ("start", "starts", "starting", "started"),
        "stop": ("stop", "stops", "stopping", "stopped"),
        "increase": ("increase", "increases", "increasing", "increased"),
        "decrease": ("decrease", "decreases", "decreasing", "decreased"),
        "taper": ("taper", "tapers", "tapering", "tapered"),
        "switch": ("switch", "switches", "switching", "switched"),
        "resume": ("resume", "resumes", "resuming", "resumed"),
        "discontinue": ("discontinue", "discontinues", "discontinuing", "discontinued"),
        "add": ("add", "adds", "adding", "added"),
    }.items()
    for form in forms
}

# How far past the anchor verb (in word tokens, within the same clause) a
# drug-like token or dosing pattern still counts as governed by that verb —
# "increase YOUR DOSE OF metformin" is 4 tokens; "take two tablets of
# ibuprofen" is 4 tokens. ~8 tokens per the treatment_gate spec, generous on
# purpose (a false positive here costs a little friction; a missed dosing
# instruction does not get a second chance).
_IMPERATIVE_WINDOW_TOKENS = 8

_WORD_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9']*")

# Clause boundaries for the imperative-verb window scan: a drug name several
# sentences away from an unrelated "stop"/"start" must never link up with it.
_CLAUSE_BOUNDARY_RE = re.compile(r"[.!?;\n]+")

# The documented allowlist carve-out: a clause that defers the actual
# decision to a clinician ("ask your doctor about tapering prednisone",
# "worth discussing whether to taper prednisone with your rheumatologist")
# describes a conversation to have, not an instruction to follow, so it is
# allowed to name a drug alongside an otherwise-gating verb.
_DEFER_TO_CLINICIAN_VERB_RE = re.compile(
    r"\b(?:ask(?:ing|ed)?|discuss(?:ing|ed)?|talk(?:ing|ed)?)\b"
)
_CLINICIAN_REFERENT_RE = re.compile(
    r"\b(?:doctor|doctors|physician|physicians|clinician|clinicians|specialist|specialists|"
    r"provider|providers|\w*ologist|\w*ologists|\w*iatrist|\w*iatrists)\b"
)

# Curated drug/supplement vocabulary + suffix shapes, so an imperative verb
# ("take", "stop", ...) only trips the gate when it is actually followed by
# something drug-like — "take your temperature" / "stop worrying" must not
# block, while "take 20 mg prednisone" / "stop your lisinopril" must.
_DRUG_KEYWORDS = frozenset(
    {
        "prednisone",
        "prednisolone",
        "ibuprofen",
        "acetaminophen",
        "tylenol",
        "aspirin",
        "warfarin",
        "coumadin",
        "eliquis",
        "apixaban",
        "xarelto",
        "metformin",
        "insulin",
        "hydroxychloroquine",
        "plaquenil",
        "methotrexate",
        "levothyroxine",
        "biotin",
        "vitamin",
        "iron",
        "calcium",
        "magnesium",
        "melatonin",
        "supplement",
        "supplements",
        "gabapentin",
        "lisinopril",
        "metoprolol",
        "steroid",
        "steroids",
        "antibiotic",
        "antibiotics",
    }
)
_DRUG_SUFFIXES = (
    "mab",
    "nib",
    "pril",
    "olol",
    "azole",
    "prazole",
    "sartan",
    "cillin",
    "statin",
    "done",
    "mycin",
    "oxetine",
    "dipine",
    "caine",
    "dronate",
)


def _is_drug_like(token: str) -> bool:
    lowered = token.lower().strip(".,;:'\"")
    if lowered in _DRUG_KEYWORDS:
        return True
    return any(lowered.endswith(suffix) for suffix in _DRUG_SUFFIXES)


def _is_deferred_to_clinician(clause: str) -> bool:
    """True when `clause` defers the actual decision to a clinician — see
    `_DEFER_TO_CLINICIAN_VERB_RE`'s docstring-comment above."""
    return bool(
        _DEFER_TO_CLINICIAN_VERB_RE.search(clause) and _CLINICIAN_REFERENT_RE.search(clause)
    )


def _split_clauses(text: str) -> list[tuple[int, str]]:
    """Split `text` into `(start_offset, clause_text)` pieces on sentence/
    clause boundaries, so the imperative-verb window scan (and the
    defer-to-clinician allowlist check) never reaches across an unrelated
    clause."""
    clauses: list[tuple[int, str]] = []
    start = 0
    for match in _CLAUSE_BOUNDARY_RE.finditer(text):
        clauses.append((start, text[start : match.start()]))
        start = match.end()
    clauses.append((start, text[start:]))
    return clauses


# Words that make a clause ADVICE regardless of who its subject is: "you
# should take X", "I recommend tapering X", "consider taking X".
_ADVICE_MARKERS = frozenset(
    {
        "should",
        "recommend",
        "recommended",
        "recommending",
        "suggest",
        "suggested",
        "suggesting",
        "advise",
        "advised",
        "consider",
        "must",
        "ought",
        "try",
    }
)

# A subject immediately before the verb makes the clause a description or a
# question about what the patient already takes ("ARE YOU still taking X",
# "YOU take X", "SHE was on X") rather than an instruction to take it.
_SUBJECT_TOKENS = frozenset(
    {
        "i",
        "you",
        "he",
        "she",
        "they",
        "we",
        "it",
        "are",
        "is",
        "was",
        "were",
        "been",
        "be",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "still",
        "currently",
        "your",
    }
)

_ADVICE_LOOKBACK_TOKENS = 4


def _is_advice_construction(tokens: list[re.Match[str]], verb_index: int) -> bool:
    """Does the imperative verb at `verb_index` actually INSTRUCT?

    Used only by `treatment_gate(recording_only=True)`. An explicit advice
    marker anywhere before the verb makes it advice; otherwise a subject
    directly before it ("are you still TAKING", "you TAKE") makes it a
    question or a restatement, which a scribe is supposed to produce.
    A bare clause-initial verb ("TAKE 50 mcg daily") has neither and is
    treated as an instruction.
    """
    lookback = tokens[max(0, verb_index - _ADVICE_LOOKBACK_TOKENS) : verb_index]
    words = [t.group(0).lower() for t in lookback]
    if any(w in _ADVICE_MARKERS for w in words):
        return True
    return not any(w in _SUBJECT_TOKENS for w in words)


def _imperative_treatment_spans(text: str, *, recording_only: bool = False) -> list[GateSpan]:
    """Scan each clause of `text` for an imperative/hortative treatment
    construction (start/stop/take/increase/decrease/taper/switch/resume/
    discontinue/add — including "you should take", "I recommend tapering",
    "consider taking") followed, within `_IMPERATIVE_WINDOW_TOKENS` word
    tokens and the SAME clause, by a drug-like token. The window tolerates
    anything in between (determiners, pronouns, quantities, "dose of",
    "tablets of", ...) rather than requiring an exact shape, since a real
    sentence reorders and pads this construction in many ways and a false
    positive here is cheap. A clause that defers the decision to a
    clinician is exempted (see `_is_deferred_to_clinician`).
    """
    spans: list[GateSpan] = []
    for clause_start, clause_text in _split_clauses(text):
        if _is_deferred_to_clinician(clause_text):
            continue
        tokens = list(_WORD_TOKEN_RE.finditer(clause_text))
        for i, token in enumerate(tokens):
            if token.group(0).lower() not in _IMPERATIVE_VERB_FORMS:
                continue
            if recording_only and not _is_advice_construction(tokens, i):
                # Scribe mode: "are you still taking X", "you take X" are a
                # question and a restatement. Only an actual instruction
                # counts here — see `treatment_gate`'s `recording_only`.
                continue
            window = tokens[i + 1 : i + 1 + _IMPERATIVE_WINDOW_TOKENS]
            drug_token = next((t for t in window if _is_drug_like(t.group(0))), None)
            if drug_token is None:
                continue
            span_start = clause_start + token.start()
            span_end = clause_start + drug_token.end()
            spans.append(
                GateSpan(
                    start=span_start,
                    end=span_end,
                    text=text[span_start:span_end],
                    reason="imperative/hortative treatment instruction",
                )
            )
    return spans


# Dosing frequency/schedule vocabulary — the other independent signal (along
# with an imperative treatment verb, see `_has_dosing_context`) that
# corroborates a bare g/ml/units number as an actual dose rather than a
# measurement. Includes common clinical shorthand (BID/TID/QID/QHS/PRN)
# since patient-facing dosing text sometimes echoes it verbatim.
_DOSING_FREQUENCY_RE = re.compile(
    r"\b(?:"
    r"daily|"
    r"once\s+a\s+day|twice\s+a\s+day|three\s+times\s+a\s+day|four\s+times\s+a\s+day|"
    r"once\s+daily|twice\s+daily|three\s+times\s+daily|four\s+times\s+daily|"
    r"b\.?i\.?d\.?|t\.?i\.?d\.?|q\.?i\.?d\.?|q\.?h\.?s\.?|"
    r"every\s+\d+\s*(?:hours?|hrs?|days?|weeks?)|"
    r"at\s+bedtime|"
    r"with\s+meals?|with\s+food|"
    r"prn|as\s+needed"
    r")\b",
    re.IGNORECASE,
)


def _clause_has_imperative_verb(clause_text: str) -> bool:
    """True when `clause_text` contains any imperative/hortative treatment
    verb form from `_IMPERATIVE_VERB_FORMS` — the SAME vocabulary
    `_imperative_treatment_spans` uses, not duplicated. Unlike that scan,
    this does not require a drug-like token nearby: it is one of two
    independent corroborating signals for `_has_dosing_context`, the other
    being a dosing frequency/schedule term."""
    return any(
        token.group(0).lower() in _IMPERATIVE_VERB_FORMS
        for token in _WORD_TOKEN_RE.finditer(clause_text)
    )


def _has_dosing_context(clause_text: str) -> bool:
    """True when `clause_text` (one clause, from `_split_clauses`) carries a
    signal that a bare g/ml/units number+unit is an actual DOSE rather than
    a measurement: an imperative/hortative treatment verb anywhere in the
    clause ("take 5 mL twice daily"), or a dosing frequency/schedule term
    anywhere in the clause ("the dose is 5 mL twice daily", no verb needed).
    Deliberately clause-scoped, like the imperative-verb window scan, so a
    dosing cue in one sentence never corroborates a measurement in another.
    """
    return _clause_has_imperative_verb(clause_text) or bool(
        _DOSING_FREQUENCY_RE.search(clause_text)
    )


def _clause_containing(clauses: list[tuple[int, str]], pos: int) -> str:
    """Return the clause text (from `_split_clauses`) that contains offset
    `pos` in the original text."""
    current = clauses[0][1] if clauses else ""
    for clause_start, clause_text in clauses:
        if clause_start > pos:
            break
        current = clause_text
    return current


def treatment_gate(text: str, *, recording_only: bool = False) -> GateResult:
    """Deterministic scan blocking dosing/prescriptive treatment instructions.

    Allowed (deliberately not flagged): naming a test to request, naming a
    specialist type, describing what doctors typically consider, a clause
    that defers the actual decision to a clinician ("ask your doctor about
    tapering prednisone"), or a bare clinical MEASUREMENT — an ultrasound or
    urine volume in mL, a specimen mass in g, a lab value in ng/mL or
    mg/dL, a BMD in g/cm² — none of those trip any detector below (see
    ADR 0020, `_CONTEXT_DOSAGE_RE`).

    `recording_only=True` drops the bare-dosage rule and keeps ONLY the
    imperative-instruction rule. Use it where the assistant is acting as a
    SCRIBE rather than an advisor — the intake conversation, whose job
    explicitly includes asking "which medication, and what dose?" and
    reading a medication list back for confirmation. Naming a drug and its
    dose there is *recording what the patient takes*, which is the opposite
    of prescribing it; blocking it made intake withhold its own reply to a
    patient who had just said she could not remember her medication.
    "Start taking 50 mcg daily" still trips the imperative rule, which is
    the thing rule 5 actually exists to stop.
    """
    spans: list[GateSpan] = []

    if not recording_only:
        for match in _STRONG_DOSAGE_RE.finditer(text):
            spans.append(
                GateSpan(
                    start=match.start(),
                    end=match.end(),
                    text=match.group(0),
                    reason="dosage pattern",
                )
            )

    if not recording_only:
        clauses = _split_clauses(text)
        for match in _CONTEXT_DOSAGE_RE.finditer(text):
            clause_text = _clause_containing(clauses, match.start())
            if not _has_dosing_context(clause_text):
                continue
            spans.append(
                GateSpan(
                    start=match.start(),
                    end=match.end(),
                    text=match.group(0),
                    reason="dosage pattern",
                )
            )

    spans.extend(_imperative_treatment_spans(text, recording_only=recording_only))

    if not spans:
        return GateResult(passed=True)

    spans.sort(key=lambda s: s.start)
    return GateResult(passed=False, spans=spans, rewrite_instruction=_REWRITE_INSTRUCTION)
