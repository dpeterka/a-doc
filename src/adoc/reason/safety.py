"""Deterministic safety gates (PLAN.md "Safety"). NO LLM calls in this module.

Two independent, code-only screens:

- `red_flag_screen`: a keyword/regex screen for emergency presentations that
  MUST run before any API call is made for a chat turn. `guarded_turn` is the
  reusable wiring for that rule — later slices (the actual diagnostic/
  informational entry points in `reason/stages.py`) call it instead of
  re-implementing the "check first, call second" order themselves.
- `treatment_gate`: a deterministic scan of patient-facing output that blocks
  dosing/prescriptive treatment instructions before they ever reach the
  patient (CLAUDE.md rule 5).

Both screens are deliberately conservative: a false positive (an unnecessary
urgent-care banner, or a rewrite request on benign text) costs nothing but a
little friction; a false negative on a real emergency or a real dosing
instruction is the failure mode this module exists to prevent. Neither
screen attempts negation-detection ("no chest pain" still flags) — that is a
documented policy, not an oversight (see module docstring on `red_flag_screen`).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------
# Red-flag screen
# --------------------------------------------------------------------------

RedFlagCategory = Literal[
    "cardiac_chest_pain",
    "stroke_signs",
    "anaphylaxis",
    "suicidality_self_harm",
    "severe_bleeding",
    "sepsis_meningitis",
    "anticoagulant_emergency",
]

_CATEGORY_ORDER: tuple[RedFlagCategory, ...] = (
    "cardiac_chest_pain",
    "stroke_signs",
    "anaphylaxis",
    "suicidality_self_harm",
    "severe_bleeding",
    "sepsis_meningitis",
    "anticoagulant_emergency",
)


class RedFlagResult(BaseModel):
    """Result of `red_flag_screen`. `message` is the fixed urgent-care
    template, present if and only if `flagged` is `True`."""

    flagged: bool
    category: RedFlagCategory | None = None
    matched_terms: list[str] = Field(default_factory=list)
    message: str | None = None


def _phrase_pattern(term: str) -> re.Pattern[str]:
    """Compile `term` (one or more words) into a case-insensitive,
    word-boundary-anchored pattern tolerant of extra whitespace between
    words, so "chest  pain" and "chest\\npain" still match "chest pain".
    """
    words = term.split()
    body = r"\s+".join(re.escape(w) for w in words)
    return re.compile(rf"\b{body}\b", re.IGNORECASE)


# A `_Rule` fires when, for every group in `groups`, at least one term in
# that group is found in the text (AND-of-ORs). A single-condition rule is
# just one group with one term. A "combination" rule (e.g. "on an
# anticoagulant" AND "a bleeding/injury signal") is two groups.
_Group = tuple[tuple[re.Pattern[str], str], ...]


class _Rule:
    __slots__ = ("groups",)

    def __init__(self, *group_terms: str | tuple[str, ...]) -> None:
        groups: list[_Group] = []
        for g in group_terms:
            terms = (g,) if isinstance(g, str) else g
            groups.append(tuple((_phrase_pattern(t), t) for t in terms))
        self.groups: tuple[_Group, ...] = tuple(groups)

    def match(self, text: str) -> list[str] | None:
        matched: list[str] = []
        for group in self.groups:
            hit: str | None = None
            for pattern, term in group:
                if pattern.search(text):
                    hit = term
                    break
            if hit is None:
                return None
            matched.append(hit)
        return matched


_ANTICOAGULANTS = (
    "warfarin",
    "coumadin",
    "eliquis",
    "apixaban",
    "xarelto",
    "rivaroxaban",
    "pradaxa",
    "dabigatran",
    "savaysa",
    "edoxaban",
    "heparin",
    "blood thinner",
    "blood thinners",
    "anticoagulant",
)

_BLEED_OR_INJURY_SIGNALS = (
    "bleeding",
    "won't stop bleeding",
    "wont stop bleeding",
    "hit my head",
    "fell and hit my head",
    "black stool",
    "black tarry stool",
    "blood in my urine",
    "blood in my stool",
)

_CATEGORY_RULES: dict[RedFlagCategory, tuple[_Rule, ...]] = {
    # Combination (AND-of-groups) rules are used wherever the natural
    # phrasing commonly reorders or inserts words between the two concepts
    # ("my face is drooping", "my speech is slurred") — a fixed adjacent
    # phrase would miss those trivial rephrasings; requiring both concepts
    # present anywhere in the text does not.
    "cardiac_chest_pain": (
        _Rule("heart attack"),
        _Rule("chest", ("pain", "pressure", "tightness", "discomfort")),
        _Rule(("crushing", "elephant"), "chest"),
        _Rule(
            "pain",
            ("radiating to my arm", "radiating to my left arm", "radiating to my jaw"),
        ),
    ),
    "stroke_signs": (
        _Rule(("face", "facial"), ("droop", "drooping")),
        _Rule("arm", ("weak", "weakness")),
        _Rule("weakness on one side"),
        _Rule("speech", ("slurred", "garbled", "difficulty")),
        _Rule("can't speak"),
        _Rule("cant speak"),
        # Deliberately narrow to FAST (Face, Arm, Speech, Time) signs plus
        # sudden focal neuro deficits — "sudden confusion" alone overlaps
        # with sepsis/meningitis presentations and is handled there instead.
        _Rule("sudden", ("numbness", "vision loss")),
        _Rule("worst headache of my life"),
        _Rule("vision loss in one eye"),
    ),
    "anaphylaxis": (
        _Rule("anaphylaxis"),
        _Rule("anaphylactic"),
        _Rule(
            ("throat", "tongue", "lips", "face"),
            ("closing", "closing up", "swelling", "swollen"),
        ),
        _Rule("can't breathe"),
        _Rule("cant breathe"),
        _Rule("trouble breathing"),
        _Rule("difficulty breathing"),
        _Rule(("hives", "hive"), ("trouble breathing", "can't breathe", "difficulty breathing")),
    ),
    "suicidality_self_harm": (
        _Rule("kill myself"),
        _Rule("suicidal"),
        _Rule("suicide"),
        _Rule("want to die"),
        _Rule("end my life"),
        _Rule("hurt myself"),
        _Rule("harm myself"),
        _Rule("self-harm"),
        _Rule("no reason to live"),
        _Rule("better off dead"),
    ),
    "severe_bleeding": (
        _Rule("won't stop bleeding"),
        _Rule("wont stop bleeding"),
        _Rule("bleeding won't stop"),
        _Rule("uncontrolled bleeding"),
        _Rule("vomiting blood"),
        _Rule("coughing up blood"),
        _Rule("heavy bleeding"),
        _Rule("black tarry stool"),
    ),
    "sepsis_meningitis": (
        _Rule("sepsis"),
        _Rule("septic"),
        _Rule(("stiff neck", "neck stiffness"), ("fever", "high fever")),
        _Rule("confusion", "high fever"),
        _Rule("rash that won't fade", "fever"),
    ),
    "anticoagulant_emergency": (_Rule(_ANTICOAGULANTS, _BLEED_OR_INJURY_SIGNALS),),
}

_CRISIS_LINE_NOTE = (
    " In the US you can also call or text 988 (the Suicide & Crisis Lifeline) any time."
)

_GENERIC_URGENT_MESSAGE = (
    "This sounds like it could be a medical emergency. Please call 911 (or your local "
    "emergency number) or go to the nearest emergency room right now. If you can, have "
    "someone stay with you until help arrives.\n\n"
    "I'm a tool for organizing your longitudinal case file and preparing questions for "
    "your doctors — I'm not equipped to help with emergencies, and continuing this "
    "conversation could delay care you may need immediately. Please seek emergency care "
    "first; we can pick this back up once you're safe."
)

_MESSAGES: dict[RedFlagCategory, str] = {
    "cardiac_chest_pain": _GENERIC_URGENT_MESSAGE,
    "stroke_signs": _GENERIC_URGENT_MESSAGE,
    "anaphylaxis": _GENERIC_URGENT_MESSAGE,
    "suicidality_self_harm": _GENERIC_URGENT_MESSAGE + _CRISIS_LINE_NOTE,
    "severe_bleeding": _GENERIC_URGENT_MESSAGE,
    "sepsis_meningitis": _GENERIC_URGENT_MESSAGE,
    "anticoagulant_emergency": _GENERIC_URGENT_MESSAGE,
}


def red_flag_screen(text: str) -> RedFlagResult:
    """Deterministic emergency-presentation screen. No LLM call, ever.

    Conservative by design: this is a keyword/synonym screen, not a clinical
    negation parser, so a phrase like "no chest pain" or "chest pain went
    away years ago" still flags. That is intentional — a false positive here
    costs a little friction (an urgent-care banner shown when it wasn't
    strictly needed); a false negative could delay real emergency care. See
    PLAN.md "Safety" / "Key risks" #3.
    """
    for category in _CATEGORY_ORDER:
        for rule in _CATEGORY_RULES[category]:
            matched = rule.match(text)
            if matched is not None:
                return RedFlagResult(
                    flagged=True,
                    category=category,
                    matched_terms=matched,
                    message=_MESSAGES[category],
                )
    return RedFlagResult(flagged=False)


def guarded_turn[T](text: str, on_pass: Callable[[], T]) -> T | RedFlagResult:
    """Run `red_flag_screen(text)` and only call `on_pass()` if it passes.

    This is the reusable "check before any API call" wiring PLAN.md
    requires for every chat turn (loop (b)): callers in `reason/stages.py`
    (and any future entry point) should route every turn through this
    helper rather than calling a reasoner directly. `on_pass` is never
    invoked when the screen flags the turn — zero API calls on a red flag.
    """
    result = red_flag_screen(text)
    if result.flagged:
        return result
    return on_pass()


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

# Dosage units per the treatment_gate spec. The `(?!...)` tail excludes the
# common *lab-result* concentration denominators (mg/dL, mg/L, mg/mL, mg/mcL)
# so a quantitative lab value ("CRP 8 mg/L") is never mistaken for a dosing
# instruction — those denominators never appear after a genuine dose.
_DOSAGE_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:mg|mcg|g|units?|iu|ml)\b(?!\s*/\s*(?:dl|l|ml|mcl)\b)",
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


def _imperative_treatment_spans(text: str) -> list[GateSpan]:
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


def treatment_gate(text: str) -> GateResult:
    """Deterministic scan blocking dosing/prescriptive treatment instructions.

    Allowed (deliberately not flagged): naming a test to request, naming a
    specialist type, describing what doctors typically consider, or a
    clause that defers the actual decision to a clinician ("ask your doctor
    about tapering prednisone") — none of those trip either detector below.
    """
    spans: list[GateSpan] = []

    for match in _DOSAGE_RE.finditer(text):
        spans.append(
            GateSpan(
                start=match.start(),
                end=match.end(),
                text=match.group(0),
                reason="dosage pattern",
            )
        )

    spans.extend(_imperative_treatment_spans(text))

    if not spans:
        return GateResult(passed=True)

    spans.sort(key=lambda s: s.start)
    return GateResult(passed=False, spans=spans, rewrite_instruction=_REWRITE_INSTRUCTION)
