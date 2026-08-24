"""Tests for adoc.casefile.schema/ledger: invariants, diff application, persistence.

Every ledger invariant (a-e) documented in `adoc.casefile.ledger` has at
least one test proving that violating it raises `LedgerInvariantError`.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from adoc.casefile.ledger import (
    LedgerInvariantError,
    append_history,
    apply_and_save,
    apply_diff,
    load_ledger,
    save_ledger,
    stale_hypotheses,
)
from adoc.casefile.schema import (
    AddEvidence,
    AddHypothesis,
    Evidence,
    Hypothesis,
    Ledger,
    LedgerDiff,
    Provenance,
    RecordChallenge,
    UpdateHypothesis,
    validate_source_ref,
)

# --- helpers --------------------------------------------------------------------------


def make_provenance(**overrides: object) -> Provenance:
    defaults: dict[str, object] = {
        "app_version": "0.1.0",
        "prompt_template_version": "ledger-maintainer-v1",
        "model_id": "claude-opus-5",
        "dag_node": "ledger_maintainer",
        "timestamp": datetime(2026, 8, 1, 12, 0, tzinfo=UTC),
    }
    defaults.update(overrides)
    return Provenance.model_validate(defaults)


def make_hypothesis(**overrides: object) -> Hypothesis:
    defaults: dict[str, object] = {
        "id": "sle-01",
        "name": "Systemic lupus erythematosus",
        "tier": "expanded",
        "probability": "moderate",
        "status": "active",
        "origin": "model",
        "first_proposed": date(2026, 8, 1),
    }
    defaults.update(overrides)
    return Hypothesis.model_validate(defaults)


def make_cant_miss(id_: str = "pe-01") -> Hypothesis:
    return make_hypothesis(id=id_, name="Pulmonary embolism", tier="cant-miss", origin="model")


def empty_ledger(version: int = 0) -> Ledger:
    return Ledger(version=version, updated=datetime(2026, 8, 1, tzinfo=UTC), hypotheses=[])


def diff_with(ops: list[object], rationale: str = "seed", **prov_overrides: object) -> LedgerDiff:
    return LedgerDiff.model_validate(
        {
            "provenance": make_provenance(**prov_overrides).model_dump(mode="json"),
            "rationale": rationale,
            "ops": ops,
        }
    )


# --- source-ref grammar ----------------------------------------------------------------


@pytest.mark.parametrize(
    "ref",
    [
        "labs:ana:2026-05-02",
        "labs:anti-dsdna:2026-07-10",
        "doc:smith-report.pdf#p1",
        "doc:smith-report.pdf#p12",
        "encounter:2026-08-20--rheum-visit.md",
        "pmid:38472910",
        "patient-report:2026-08-01",
    ],
)
def test_source_ref_grammar_accepts_valid_refs(ref: str) -> None:
    assert validate_source_ref(ref) == ref
    Evidence(claim="x", source=ref, strength="moderate")


@pytest.mark.parametrize(
    "ref",
    [
        "labs:ana ana:2026-05-02",  # whitespace in slug still invalid  # uppercase not allowed in slug
        "labs:ana:05-02-2026",  # wrong date format
        "labs:ana",  # missing date
        "doc:smith-report.pdf",  # missing #p<int>
        "doc:smith-report.pdf#page1",  # wrong page marker
        "pmid:abc123",  # non-digits
        "patient-report:2026/08/01",  # wrong date separator
        "just-some-text",
        "",
    ],
)
def test_source_ref_grammar_rejects_invalid_refs(ref: str) -> None:
    with pytest.raises(ValueError):
        validate_source_ref(ref)
    with pytest.raises(ValidationError):
        Evidence(claim="x", source=ref, strength="moderate")


def test_hypothesis_id_must_be_a_slug() -> None:
    with pytest.raises(ValidationError):
        make_hypothesis(id="SLE 01")


# --- S3 remediation: RecordChallenge.note must be substantive ---------------------------


@pytest.mark.parametrize("note", ["", ".", "reviewed", "ok", "   ", "short note"])
def test_record_challenge_note_below_min_length_is_rejected(note: str) -> None:
    with pytest.raises(ValidationError):
        RecordChallenge(id="sle-01", note=note)


def test_record_challenge_note_at_min_length_after_strip_is_accepted() -> None:
    # Exactly 20 characters after stripping surrounding whitespace.
    note = "  " + "x" * 20 + "  "
    challenge = RecordChallenge(id="sle-01", note=note)
    assert challenge.note.strip() == "x" * 20


def test_record_challenge_note_substantive_text_is_accepted() -> None:
    RecordChallenge(id="sle-01", note="Anti-dsDNA still pending; no contradicting evidence yet.")


# --- happy path -------------------------------------------------------------------------


def test_apply_diff_adds_hypothesis_and_bumps_version() -> None:
    ledger = empty_ledger()
    diff = diff_with([AddHypothesis(hypothesis=make_cant_miss())])

    new_ledger = apply_diff(ledger, diff)

    assert new_ledger.version == 1
    assert new_ledger.updated == diff.provenance.timestamp
    assert len(new_ledger.hypotheses) == 1
    assert new_ledger.hypotheses[0].id == "pe-01"
    assert new_ledger.hypotheses[0].last_challenged_version == 1
    # original untouched (pure function)
    assert ledger.version == 0
    assert ledger.hypotheses == []


def test_apply_diff_records_prior_probability_on_change() -> None:
    fresh = make_cant_miss()
    fresh.last_challenged_version = 1
    ledger = Ledger(
        version=1,
        updated=datetime(2026, 8, 1, tzinfo=UTC),
        hypotheses=[fresh],
    )
    diff = diff_with([UpdateHypothesis(id="pe-01", probability="high")])

    new_ledger = apply_diff(ledger, diff)
    hyp = new_ledger.hypotheses[0]

    assert hyp.probability == "high"
    assert hyp.prior_probability == "moderate"


def test_apply_diff_add_evidence_and_record_challenge() -> None:
    ledger = Ledger(
        version=1,
        updated=datetime(2026, 8, 1, tzinfo=UTC),
        hypotheses=[make_cant_miss()],
    )
    evidence = Evidence(
        claim="D-dimer elevated", source="labs:d-dimer:2026-08-01", strength="strong"
    )
    diff = diff_with(
        [
            AddEvidence(id="pe-01", for_or_against="for", evidence=evidence),
            RecordChallenge(id="pe-01", note="Considered and still plausible."),
        ]
    )

    new_ledger = apply_diff(ledger, diff)
    hyp = new_ledger.hypotheses[0]

    assert hyp.evidence_for == [evidence]
    assert hyp.last_challenged == diff.provenance.timestamp.date()
    assert hyp.last_challenged_version == new_ledger.version
    assert "Considered and still plausible." in hyp.challenger_notes


def test_apply_diff_unknown_hypothesis_id_raises() -> None:
    ledger = Ledger(
        version=1,
        updated=datetime(2026, 8, 1, tzinfo=UTC),
        hypotheses=[make_cant_miss()],
    )
    diff = diff_with([UpdateHypothesis(id="does-not-exist", probability="high")])

    with pytest.raises(LedgerInvariantError):
        apply_diff(ledger, diff)


def test_apply_diff_duplicate_add_raises() -> None:
    ledger = Ledger(
        version=1,
        updated=datetime(2026, 8, 1, tzinfo=UTC),
        hypotheses=[make_cant_miss()],
    )
    diff = diff_with([AddHypothesis(hypothesis=make_cant_miss())])

    with pytest.raises(LedgerInvariantError):
        apply_diff(ledger, diff)


# --- invariant (a): cant-miss non-empty while any hypothesis is active -----------------


def test_invariant_a_rejects_empty_cant_miss_while_active() -> None:
    ledger = empty_ledger()
    diff = diff_with([AddHypothesis(hypothesis=make_hypothesis(tier="expanded", status="active"))])

    with pytest.raises(LedgerInvariantError, match="cant-miss"):
        apply_diff(ledger, diff)


def test_invariant_a_allows_no_cant_miss_when_nothing_active() -> None:
    ledger = empty_ledger()
    diff = diff_with(
        [AddHypothesis(hypothesis=make_hypothesis(tier="expanded", status="ruled-out"))]
    )

    new_ledger = apply_diff(ledger, diff)
    assert new_ledger.hypotheses[0].status == "ruled-out"


def test_invariant_a_allows_active_alongside_active_cant_miss() -> None:
    ledger = empty_ledger()
    diff = diff_with(
        [
            AddHypothesis(hypothesis=make_cant_miss()),
            AddHypothesis(hypothesis=make_hypothesis(tier="expanded", status="active")),
        ]
    )

    new_ledger = apply_diff(ledger, diff)
    assert len(new_ledger.hypotheses) == 2


def test_invariant_a_rejects_ruling_out_the_only_cant_miss_while_others_active() -> None:
    fresh_cant_miss = make_cant_miss()
    fresh_cant_miss.last_challenged_version = 1
    fresh_other = make_hypothesis(id="sle-02", tier="expanded", status="active")
    fresh_other.last_challenged_version = 1
    ledger = Ledger(
        version=1,
        updated=datetime(2026, 8, 1, tzinfo=UTC),
        hypotheses=[fresh_cant_miss, fresh_other],
    )
    diff = diff_with([UpdateHypothesis(id="pe-01", status="ruled-out")])

    with pytest.raises(LedgerInvariantError, match="cant-miss"):
        apply_diff(ledger, diff)


# --- invariant (b): patient-origin promotion gating ------------------------------------


def test_invariant_b_rejects_promotion_in_same_diff_as_creation() -> None:
    fresh_cant_miss = make_cant_miss()
    fresh_cant_miss.last_challenged_version = 1
    ledger = Ledger(
        version=1,
        updated=datetime(2026, 8, 1, tzinfo=UTC),
        hypotheses=[fresh_cant_miss],
    )
    patient_hyp = make_hypothesis(
        id="lyme-01", tier="expanded", origin="patient", status="patient-proposed"
    )
    diff = diff_with(
        [
            AddHypothesis(hypothesis=patient_hyp),
            UpdateHypothesis(id="lyme-01", tier="most-likely"),
        ]
    )

    with pytest.raises(LedgerInvariantError, match="most-likely"):
        apply_diff(ledger, diff)


def test_invariant_b_rejects_promotion_without_prior_challenge() -> None:
    # last_challenged_version is set (the hypothesis's freshness clock, e.g. from
    # its own creation) but last_challenged (an actual RecordChallenge) never
    # happened - promotion must still be blocked.
    patient_hyp = make_hypothesis(
        id="lyme-01",
        tier="expanded",
        origin="patient",
        status="patient-proposed",
        last_challenged_version=2,
    )
    fresh_cant_miss = make_cant_miss()
    fresh_cant_miss.last_challenged_version = 2
    ledger = Ledger(
        version=2,
        updated=datetime(2026, 8, 3, tzinfo=UTC),
        hypotheses=[fresh_cant_miss, patient_hyp],
    )
    diff = diff_with([UpdateHypothesis(id="lyme-01", tier="most-likely")])

    with pytest.raises(LedgerInvariantError, match="challenged"):
        apply_diff(ledger, diff)


def test_invariant_b_allows_promotion_after_earlier_challenge() -> None:
    patient_hyp = make_hypothesis(
        id="lyme-01",
        tier="expanded",
        origin="patient",
        status="patient-proposed",
        last_challenged=date(2026, 8, 2),
        last_challenged_version=2,
    )
    fresh_cant_miss = make_cant_miss()
    fresh_cant_miss.last_challenged_version = 2
    ledger = Ledger(
        version=2,
        updated=datetime(2026, 8, 3, tzinfo=UTC),
        hypotheses=[fresh_cant_miss, patient_hyp],
    )
    diff = diff_with([UpdateHypothesis(id="lyme-01", tier="most-likely")])

    new_ledger = apply_diff(ledger, diff)
    assert new_ledger.hypotheses[1].tier == "most-likely"


def test_invariant_b_does_not_apply_to_model_origin_hypotheses() -> None:
    model_hyp = make_hypothesis(id="sle-02", tier="expanded", origin="model", status="active")
    model_hyp.last_challenged_version = 2
    fresh_cant_miss = make_cant_miss()
    fresh_cant_miss.last_challenged_version = 2
    ledger = Ledger(
        version=2,
        updated=datetime(2026, 8, 3, tzinfo=UTC),
        hypotheses=[fresh_cant_miss, model_hyp],
    )
    diff = diff_with([UpdateHypothesis(id="sle-02", tier="most-likely")])

    new_ledger = apply_diff(ledger, diff)
    assert new_ledger.hypotheses[1].tier == "most-likely"


def test_invariant_b_rejects_promotion_on_a_stale_pre_diff_challenge() -> None:
    """S3 remediation: invariant (b) previously required only that
    `last_challenged` be non-None (a challenge, EVER) before allowing a
    patient-origin promotion. That let an ancient PRE-diff challenge marker
    combine with a fresh same-diff RecordChallenge (which satisfies
    invariant (c)'s staleness check on its own) to look "challenged enough"
    for promotion. Invariant (b) must independently require that the
    challenge backing the promotion was itself RECENT — i.e. dated from a
    strictly earlier diff no more than STALENESS_HORIZON versions back —
    not merely present at some point in the hypothesis's history.
    """
    patient_hyp = make_hypothesis(
        id="lyme-01",
        tier="expanded",
        origin="patient",
        status="patient-proposed",
        last_challenged=date(2026, 1, 1),  # non-None, but ancient
        last_challenged_version=0,
    )
    fresh_cant_miss = make_cant_miss()
    fresh_cant_miss.last_challenged_version = 4
    ledger = Ledger(
        version=4,
        updated=datetime(2026, 8, 3, tzinfo=UTC),
        hypotheses=[fresh_cant_miss, patient_hyp],
    )
    # new_version = 5, threshold = 5 - STALENESS_HORIZON(2) = 3. This diff
    # challenges lyme-01 again in the SAME diff as the promotion attempt —
    # enough to satisfy invariant (c)'s staleness check on its own — but
    # invariant (b) must still reject the promotion because the PRE-diff
    # challenge marker (last_challenged_version=0) it would otherwise rely
    # on is stale.
    diff = diff_with(
        [
            RecordChallenge(id="lyme-01", note="Re-reviewed again; still active as of today."),
            UpdateHypothesis(id="lyme-01", tier="most-likely"),
        ]
    )

    with pytest.raises(LedgerInvariantError, match="stale"):
        apply_diff(ledger, diff)


def test_invariant_b_allows_promotion_on_a_recent_challenge_within_horizon() -> None:
    """Companion to the staleness-rejection test above: a challenge that is
    old but still within the staleness horizon must still allow promotion."""
    patient_hyp = make_hypothesis(
        id="lyme-01",
        tier="expanded",
        origin="patient",
        status="patient-proposed",
        last_challenged=date(2026, 8, 1),
        last_challenged_version=3,
    )
    fresh_cant_miss = make_cant_miss()
    fresh_cant_miss.last_challenged_version = 4
    ledger = Ledger(
        version=4,
        updated=datetime(2026, 8, 3, tzinfo=UTC),
        hypotheses=[fresh_cant_miss, patient_hyp],
    )
    # new_version = 5; threshold = 5 - 2 = 3; last_challenged_version (3) is
    # not below the threshold, so this is still recent enough.
    diff = diff_with([UpdateHypothesis(id="lyme-01", tier="most-likely")])

    new_ledger = apply_diff(ledger, diff)
    assert new_ledger.hypotheses[1].tier == "most-likely"


# --- invariant (c): staleness -----------------------------------------------------------


def test_stale_hypotheses_helper_flags_never_challenged_after_horizon() -> None:
    stale_hyp = make_cant_miss()  # last_challenged_version=None
    ledger = Ledger(version=3, updated=datetime(2026, 8, 3, tzinfo=UTC), hypotheses=[stale_hyp])

    assert stale_hypotheses(ledger) == [stale_hyp]


def test_stale_hypotheses_helper_ignores_fresh_hypotheses() -> None:
    fresh_hyp = make_cant_miss()
    fresh_hyp.last_challenged_version = 2
    ledger = Ledger(version=2, updated=datetime(2026, 8, 3, tzinfo=UTC), hypotheses=[fresh_hyp])

    assert stale_hypotheses(ledger) == []


def test_invariant_c_rejects_diff_when_unrelated_hypothesis_is_stale() -> None:
    stale_hyp = make_cant_miss(id_="pe-01")
    stale_hyp.last_challenged_version = 0
    other_hyp = make_hypothesis(id="sle-02", tier="expanded", status="active")
    other_hyp.last_challenged_version = 0
    ledger = Ledger(
        version=3,
        updated=datetime(2026, 8, 3, tzinfo=UTC),
        hypotheses=[stale_hyp, other_hyp],
    )
    # touches sle-02 only; pe-01 is stale (last_challenged_version=0, new_version=4,
    # threshold=4-2=2, 0 < 2) and this diff doesn't challenge it.
    diff = diff_with([UpdateHypothesis(id="sle-02", probability="high")])

    with pytest.raises(LedgerInvariantError, match="stale"):
        apply_diff(ledger, diff)


def test_invariant_c_allows_diff_that_challenges_the_stale_hypothesis() -> None:
    stale_hyp = make_cant_miss(id_="pe-01")
    stale_hyp.last_challenged_version = 0
    ledger = Ledger(version=3, updated=datetime(2026, 8, 3, tzinfo=UTC), hypotheses=[stale_hyp])
    diff = diff_with([RecordChallenge(id="pe-01", note="Re-reviewed; still on the board.")])

    new_ledger = apply_diff(ledger, diff)
    assert new_ledger.hypotheses[0].last_challenged_version == new_ledger.version


def test_invariant_c_does_not_flag_inactive_statuses() -> None:
    ruled_out_hyp = make_hypothesis(id="old-01", tier="expanded", status="ruled-out")
    ruled_out_hyp.last_challenged_version = 0
    cant_miss = make_cant_miss()
    cant_miss.last_challenged_version = 5
    ledger = Ledger(
        version=5,
        updated=datetime(2026, 8, 3, tzinfo=UTC),
        hypotheses=[ruled_out_hyp, cant_miss],
    )
    diff = diff_with([UpdateHypothesis(id="pe-01", discriminators=["CT angiogram"])])

    # must not raise: ruled-out hypothesis is exempt from staleness even though
    # its clock is old.
    new_ledger = apply_diff(ledger, diff)
    assert new_ledger.hypotheses[0].status == "ruled-out"


# --- invariant (d): confirmed-by-doctor raised bar --------------------------------------


def test_invariant_d_rejects_update_without_new_evidence_against() -> None:
    confirmed = make_hypothesis(id="ra-01", status="confirmed-by-doctor", tier="cant-miss")
    confirmed.last_challenged_version = 1
    ledger = Ledger(version=1, updated=datetime(2026, 8, 1, tzinfo=UTC), hypotheses=[confirmed])
    diff = diff_with([UpdateHypothesis(id="ra-01", probability="high")], rationale="routine bump")

    with pytest.raises(LedgerInvariantError, match="confirmed-by-doctor"):
        apply_diff(ledger, diff)


def test_invariant_d_rejects_update_with_empty_rationale_even_with_evidence() -> None:
    confirmed = make_hypothesis(id="ra-01", status="confirmed-by-doctor", tier="cant-miss")
    confirmed.last_challenged_version = 1
    ledger = Ledger(version=1, updated=datetime(2026, 8, 1, tzinfo=UTC), hypotheses=[confirmed])
    evidence = Evidence(claim="RF negative x2", source="labs:rf:2026-08-01", strength="strong")
    diff = diff_with(
        [AddEvidence(id="ra-01", for_or_against="against", evidence=evidence)], rationale=""
    )

    with pytest.raises(LedgerInvariantError, match="confirmed-by-doctor"):
        apply_diff(ledger, diff)


def test_invariant_d_allows_update_with_new_evidence_against_and_rationale() -> None:
    confirmed = make_hypothesis(id="ra-01", status="confirmed-by-doctor", tier="cant-miss")
    confirmed.last_challenged_version = 1
    ledger = Ledger(version=1, updated=datetime(2026, 8, 1, tzinfo=UTC), hypotheses=[confirmed])
    evidence = Evidence(claim="RF negative x2", source="labs:rf:2026-08-01", strength="strong")
    diff = diff_with(
        [
            AddEvidence(id="ra-01", for_or_against="against", evidence=evidence),
            UpdateHypothesis(id="ra-01", status="challenged"),
        ],
        rationale="New negative RF results contradict the confirmed RA diagnosis.",
    )

    new_ledger = apply_diff(ledger, diff)
    hyp = new_ledger.hypotheses[0]
    assert hyp.status == "challenged"
    assert hyp.evidence_against == [evidence]


def test_invariant_d_rejects_reinforcing_evidence_for_without_bar() -> None:
    confirmed = make_hypothesis(id="ra-01", status="confirmed-by-doctor", tier="cant-miss")
    confirmed.last_challenged_version = 1
    ledger = Ledger(version=1, updated=datetime(2026, 8, 1, tzinfo=UTC), hypotheses=[confirmed])
    evidence = Evidence(claim="CCP still positive", source="labs:ccp:2026-08-01", strength="strong")
    diff = diff_with(
        [AddEvidence(id="ra-01", for_or_against="for", evidence=evidence)],
        rationale="reinforcing existing diagnosis",
    )

    with pytest.raises(LedgerInvariantError, match="confirmed-by-doctor"):
        apply_diff(ledger, diff)


# --- invariant (e): version/updated/prior_probability/history --------------------------


def test_invariant_e_history_is_appended_alongside_yaml(tmp_path: Path) -> None:
    ledger_path = tmp_path / "differential-ledger.yaml"
    history_path = tmp_path / "ledger-history.jsonl"
    save_ledger(ledger_path, empty_ledger())

    diff = diff_with([AddHypothesis(hypothesis=make_cant_miss())])
    new_ledger = apply_and_save(ledger_path, history_path, diff)

    assert new_ledger.version == 1
    assert history_path.exists()
    lines = history_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1

    reloaded = load_ledger(ledger_path)
    assert reloaded == new_ledger


def test_append_history_never_rewrites_prior_lines(tmp_path: Path) -> None:
    history_path = tmp_path / "ledger-history.jsonl"
    ledger = empty_ledger()
    diff1 = diff_with([AddHypothesis(hypothesis=make_cant_miss())])
    ledger1 = apply_diff(ledger, diff1)
    append_history(history_path, diff1, ledger1)

    diff2 = diff_with([RecordChallenge(id="pe-01", note="Reviewed again: still relevant.")])
    ledger2 = apply_diff(ledger1, diff2)
    append_history(history_path, diff2, ledger2)

    lines = history_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2


# --- YAML persistence: round-trip stability ---------------------------------------------


def test_ledger_yaml_round_trip_is_stable(tmp_path: Path) -> None:
    ledger = Ledger(
        version=3,
        updated=datetime(2026, 8, 5, 9, 30, tzinfo=UTC),
        hypotheses=[
            make_cant_miss(),
            make_hypothesis(
                id="sle-02",
                mondo="MONDO:0007915",
                evidence_for=[
                    Evidence(claim="ANA 1:640", source="labs:ana:2026-05-02", strength="strong")
                ],
                evidence_against=[
                    Evidence(
                        claim="Anti-dsDNA negative",
                        source="labs:anti-dsdna:2026-07-10",
                        strength="moderate",
                    )
                ],
                discriminators=["Complement C3/C4"],
                challenger_notes="Still plausible.",
                last_challenged=date(2026, 8, 1),
                last_challenged_version=3,
            ),
        ],
    )
    path = tmp_path / "differential-ledger.yaml"

    save_ledger(path, ledger)
    first_text = path.read_text(encoding="utf-8")
    reloaded = load_ledger(path)

    assert reloaded == ledger

    save_ledger(path, reloaded)
    second_text = path.read_text(encoding="utf-8")
    assert first_text == second_text


def test_ledger_yaml_is_human_diffable_plain_text(tmp_path: Path) -> None:
    ledger = Ledger(
        version=1,
        updated=datetime(2026, 8, 1, tzinfo=UTC),
        hypotheses=[make_cant_miss()],
    )
    path = tmp_path / "differential-ledger.yaml"
    save_ledger(path, ledger)

    text = path.read_text(encoding="utf-8")
    assert "hypotheses:" in text
    assert "pe-01" in text
    assert not text.startswith("!!")  # no python-object tags, plain YAML


def test_patient_origin_cannot_be_added_directly_at_most_likely() -> None:
    """Invariant (b), add-path: origin=patient may never ENTER at most-likely."""
    ledger = empty_ledger()
    diff = diff_with(
        [
            AddHypothesis(hypothesis=make_cant_miss()),
            AddHypothesis(
                hypothesis=make_hypothesis(
                    id="pt-1", tier="most-likely", origin="patient", status="patient-proposed"
                )
            ),
        ]
    )
    with pytest.raises(LedgerInvariantError, match="challenged before promotion"):
        apply_diff(ledger, diff)


@pytest.mark.parametrize(
    "ref",
    [
        "labs:%-saturation:2025-05-06",  # live challenger failure
        "labs:b.-miyamotoi-ab-(igg):2024-08-09",
        "labs:a/g-ratio:2024-01-01",
        "labs:transferrin-saturation:2025-05-06",
    ],
)
def test_source_ref_slug_accepts_real_analyte_punctuation(ref: str) -> None:
    from adoc.casefile.schema import validate_source_ref

    assert validate_source_ref(ref) == ref
