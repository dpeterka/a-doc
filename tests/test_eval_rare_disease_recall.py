"""The rare-disease differential-recall suite.

Built on a synthetic ten-term ontology rather than the real 8.2MB index: CI
has no index, and a test that silently depends on one is a test that stops
running the moment the build artifact moves.

The property pinned hardest is the one that made the first draft useless: a
recall MISS must not fail a case. `adoc eval` ANDs every case into its verdict
(`cli.py`), so a suite that marks misses as failures fails permanently at any
recall below 100% — which is every real engine, forever.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from adoc.evals.suites import rare_disease_recall as suite
from adoc.knowledge.semsim import SemSimIndex, SemSimResult

# A shallow ontology: ten leaf terms under one root. Enough structure for
# information content to be defined, small enough to reason about by hand.
_PARENTS = {f"HP:{n:07d}": ["HP:0000001"] for n in range(1, 41)}
_PARENTS["HP:0000001"] = []


def _index(disease_count: int = 12, terms_each: int = 10) -> SemSimIndex:
    """Diseases with DISTINCT term sets, so each is findable from its own
    annotations and recall reflects the engine rather than an unlucky corpus.

    The window stride is 3, not `terms_each`. Striding by the window width
    wraps the modulo back onto itself: 12 diseases collapsed into 4 distinct
    term sets, leaving three diseases with byte-identical annotations. Recall
    among indistinguishable diseases is arbitrary, so the high-recall test
    below was passing on a corpus that could not support the claim.
    """
    diseases: dict[str, dict] = {}
    for d in range(disease_count):
        start = (d * 3) % 40 + 1
        terms = [f"HP:{((start + i - 1) % 40) + 1:07d}" for i in range(terms_each)]
        diseases[f"OMIM:{600000 + d}"] = {"name": f"Disease {d}", "terms": terms}
    return SemSimIndex(parents=_PARENTS, diseases=diseases)


def test_the_synthetic_corpus_has_no_identical_diseases() -> None:
    """Guards the fixture itself. Every test that claims something about
    recall depends on the diseases being distinguishable at all."""
    index = _index()
    sets = {frozenset(index.disease_terms(d)) for d, _ in index.iter_diseases()}

    assert len(sets) == index.disease_count


class _StubIndex:
    """An index whose ranking is dictated by the test.

    Used for the two cases that are about the SUITE's arithmetic — how it
    scores hits and where it draws the gate — rather than about the engine.
    """

    def __init__(self, index: SemSimIndex, *, ranked: list[str] | None) -> None:
        self._index = index
        self._ranked = ranked

    def iter_diseases(self):  # noqa: ANN201 - test stub
        return self._index.iter_diseases()

    def disease_terms(self, disease_id: str):  # noqa: ANN201 - test stub
        return self._index.disease_terms(disease_id)

    def disease_name(self, disease_id: str):  # noqa: ANN201 - test stub
        return self._index.disease_name(disease_id)

    def rank(self, query, *, top_n=10):  # noqa: ANN001, ANN201, ARG002 - test stub
        if self._ranked is None:
            return SemSimResult(diseases=[])
        return self._index.rank(query, top_n=top_n)


def _gate(result) -> object:  # noqa: ANN001
    return next(c for c in result.cases if c.case_id.endswith("_above_threshold"))


# -- case construction ------------------------------------------------------


def test_cases_are_reproducible() -> None:
    """A regression must be attributable to a code change, not to a lucky
    sample. Two builds over the same index give identical cases."""
    index = _index()

    assert suite.build_cases(index, count=5) == suite.build_cases(index, count=5)


def test_a_query_is_a_subset_of_the_disease_plus_noise() -> None:
    """Handing the engine the complete annotation set asks whether it can look
    up an exact key. The subset is what makes this measure anything."""
    index = _index()

    for disease_id, query in suite.build_cases(index, count=4):
        own = set(index.disease_terms(disease_id))
        from_own = [t for t in query if t in own]

        assert len(from_own) == suite.TERMS_PER_CASE
        assert len(query) > suite.TERMS_PER_CASE, "no noise terms were added"
        assert len(own) > suite.TERMS_PER_CASE, "the query is the whole annotation set"


def test_thinly_annotated_diseases_are_excluded() -> None:
    """Below the floor the subset IS the whole annotation set — the lookup
    test this suite exists to avoid."""
    thin = SemSimIndex(
        parents=_PARENTS,
        diseases={
            "OMIM:600001": {"name": "Thin", "terms": ["HP:0000002", "HP:0000003"]},
            "OMIM:600002": {
                "name": "Thick",
                "terms": [f"HP:{n:07d}" for n in range(2, 2 + suite.MIN_ANNOTATIONS)],
            },
        },
    )

    assert [d for d, _ in suite.build_cases(thin, count=10)] == ["OMIM:600002"]


def test_cases_are_spread_across_the_corpus() -> None:
    """Taking the first N would sample one alphabetical neighbourhood of OMIM
    and call it a benchmark."""
    index = _index(disease_count=40)
    picked = [d for d, _ in suite.build_cases(index, count=4)]

    assert picked != sorted(d for d, _ in index.iter_diseases())[:4]
    assert len(set(picked)) == len(picked)


# -- scoring ----------------------------------------------------------------


def test_a_recall_MISS_does_not_fail_a_case(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bug that would have made this suite useless.

    `cli.py` ANDs every case into the overall verdict, so marking a miss as a
    failed case makes the suite fail at any recall below 100%. Here the engine
    returns nothing at all — total miss — and the per-case results must still
    report that the engine RAN.
    """
    monkeypatch.setattr(suite, "load_index", lambda _p: _StubIndex(_index(), ranked=None))

    result = suite.run(client_factory=None)  # type: ignore[arg-type]
    per_case = [c for c in result.cases if c.case_id.startswith("recall:")]

    assert per_case, "no per-case results were produced"
    assert all(c.passed for c in per_case), "a miss was reported as a failed case"
    assert not _gate(result).passed  # type: ignore[attr-defined]
    assert not result.passed, "zero recall must still fail the suite, via the gate"


def test_the_gate_is_the_only_case_that_can_fail_on_rate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With disjoint annotation sets every disease is findable from its own
    terms, so recall is high and the gate passes."""
    monkeypatch.setattr(suite, "load_index", lambda _p: _index())

    result = suite.run(client_factory=None)  # type: ignore[arg-type]
    recall_at_10 = next(m.value for m in result.metrics if m.name == "recall_at_10")

    assert recall_at_10 >= suite.MIN_RECALL_AT_10
    assert _gate(result).passed  # type: ignore[attr-defined]
    assert result.passed


def test_the_floor_sits_below_the_measured_baseline() -> None:
    """A gate set above what the engine does is a suite that fails on the day
    it ships. The first committed run measured recall@10 0.525."""
    assert suite.MIN_RECALL_AT_10 < 0.525


def test_recall_is_monotonic_across_cutoffs(monkeypatch: pytest.MonkeyPatch) -> None:
    """recall@1 <= recall@3 <= recall@10 by construction. If this ever breaks,
    the hit accounting is wrong and every reported rate is suspect."""
    monkeypatch.setattr(suite, "load_index", lambda _p: _index())

    result = suite.run(client_factory=None)  # type: ignore[arg-type]
    by_name = {m.name: m.value for m in result.metrics}

    assert by_name["recall_at_1"] <= by_name["recall_at_3"] <= by_name["recall_at_10"]


# -- absence ----------------------------------------------------------------


def test_no_index_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    """The index is a build artifact. A local checkout has none, and failing
    there would train everyone to ignore this suite."""
    monkeypatch.setattr(suite, "load_index", lambda _p: None)

    result = suite.run(client_factory=None)  # type: ignore[arg-type]

    assert any(m.name == "skipped" for m in result.metrics)
    assert result.cases == []
    assert result.passed


def test_an_unreadable_index_skips_rather_than_raising(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An eval run must not die because a reference artifact is corrupt."""

    def _boom(_path: Path) -> None:
        raise OSError("index is a directory")

    monkeypatch.setattr(suite, "load_index", _boom)

    result = suite.run(client_factory=None)  # type: ignore[arg-type]

    assert any(m.name == "skipped" for m in result.metrics)


def test_it_reports_that_no_model_was_used(monkeypatch: pytest.MonkeyPatch) -> None:
    """It accepts a binding to satisfy the protocol and does not use one.
    Reporting a binding label it ignored would misattribute every result."""
    monkeypatch.setattr(suite, "load_index", lambda _p: _index())

    result = suite.run(client_factory="unused", candidate="claude-opus-5")  # type: ignore[arg-type]

    assert "no model binding" in result.binding_label
