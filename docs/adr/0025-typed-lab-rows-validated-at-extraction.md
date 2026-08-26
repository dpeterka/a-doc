# ADR 0025 — A measurement is not a sentence: typed lab rows, validated at extraction

- Status: Accepted (2026-08-26)
- Supersedes the downstream-tolerance half of PR #151. Sets the direction
  that ADR 0024's closing note asked for: fix the data, not the readers.

## Context

Four separate guards have now been narrowed to tolerate rows that are not
really measurements (ADR 0016 ×3, ADR 0023, ADR 0024, PR #151). Every one of
those changes made a *reader* more forgiving. The owner's direction is to
stop doing that and fix the ingestion side instead.

Measured against the real store (2033 non-rejected rows), the problem is
not what the downstream failures implied:

| finding | rows | share |
|---|---|---|
| numeric result stored as TEXT (`<20`, `>150`) | **175** | 8.6% |
| qualitative result (`NON-REACTIVE`, `NO MUTATION DETECTED`) | 361 | 17.8% |
| prose fragment stored as an analyte name | **6** | 0.3% |
| diagnoses in the labs table | **0** | — |

Three things follow.

**The prose problem is tiny and already half-fixed.** All 6 rows come from
one document — a narrative DEXA/FRAX report where the value sits at the end
of a sentence rather than in a table. `reconcile.clean_result_name` already
strips a trailing `is`, so the *current* extractor would clean 3 of the 6;
those rows were ingested before it existed. The data is stale, not the code.

**But cleaning cannot fix the other half**, and this is the real design gap:

    'Left total hip: A statistically significant decrease of'   -> unchanged
    'The BMD measured is'                                       -> 'The BMD measured'

These are not badly-named measurements. They are sentences that happen to
contain a number. `Left total hip: A statistically significant decrease` is
not an analyte under any cleaning rule, because the problem is not the
name — it is that the row should never have been a lab row at all.
`DocumentExtraction` already has the correct destination for it
(`narrative_findings`), and nothing routed it there.

**The 175 are a bigger defect than the 6.** `<20` on an RNA Polymerase III
antibody is a negative result, and a move from `<20` to `45` is clinically
meaningful. Both are invisible today: the numeric content is trapped in
`value_text`, so those rows cannot be trended, compared to a reference
range, or seen by any numeric check.

**Diagnoses do not arrive here.** The owner's proposed three-way split
(lab result / diagnosis / RAG text) is right as a principle, but on the labs
path the third bucket is empty: interpretations like "low bone density but
no osteoporosis" live in the document-text corpus (ADR 0015) and the
ledger. The labs gate is therefore two-way — *measurement* or *narrative* —
and the diagnosis/other distinction belongs to the document classifier,
where it already exists (`DocType`).

## Decision

### 1. A row-kind gate at extraction (Phase A)

`ingest/rowkind.py::classify_extracted_row` — deterministic, no model call
— assigns every `ExtractedResult` one of:

- `quantitative` — a numeric value, optionally with a comparator.
- `qualitative` — a nominal/ordinal result (`NON-REACTIVE`, `NO MUTATION
  DETECTED`). A real result; simply not a number.
- `narrative` — a sentence. **Not stored as a lab row.** Diverted into
  `DocumentExtraction.narrative_findings`, where it remains available for
  retrieval, citation as `doc:<file>#p<n>`, and RAG.

Narrative is decided from the shape of the name AFTER `clean_result_name`:
a clause colon followed by prose, a leading article, a finite reporting verb
(`shows`, `measured`, `decrease`, `indicates`), or length beyond a word
budget with no recognized measure token. Conservative by construction — a
row is only diverted when it looks like a sentence, never merely because it
is long, since real analyte names get long (`FRAX ... 10-year probability of
major osteoporotic fracture` is a genuine measure).

### 2. `comparator` on `LabResult` (Phase A)

A new field holding `<`, `<=`, `>`, `>=` or `None`. `<20 Units` parses to
`value=20.0, comparator="<", ucum_unit="Units"` instead of
`value_text="<20"`. The 175 affected rows become trendable and
range-checkable, and every numeric consumer must treat a comparator-bearing
value as a bound rather than a point — which is exactly the distinction
ADR 0023 taught us the readers care about.

### 3. Revalidation of stored data (Phase A)

`adoc labs-revalidate` re-runs cleaning, the row-kind gate and comparator
parsing over rows already stored, because the fixes above are worthless
against a store populated before they existed. Dry-run by default, reports
every proposed change, and never deletes: a row reclassified as narrative is
marked `rejected` (the existing tombstone mechanism) with its text preserved
as a narrative finding, so nothing is lost and the change is reversible
through git history.

### 4. Taxonomy: panel, measure, derived-from (Phase B — designed, not built)

The owner's "MAIN VALUE MEASURE / SUB VALUE MEASURE" is confirmed by the
data, but the axis is not main-value/sub-value. It is **site-or-panel ×
measure**, with a third, separate *derivation* relation:

    left hip      -> Femoral Neck BMD, Femoral Neck T-Score, Femoral Neck
                     Z-Score, Total BMD, Total Z-Score
    lumbar spine  -> BMD, AP(L1-L4) T-Score
    CBC           -> 117 `... Absolute` rows and their `... %` counterparts
                     (48 `... Ratio`)

So three fields, not two:

- `panel` — the grouping the report itself presents (`Left Hip`, `CBC with
  differential`, `Iron panel`). Read off the report's own section headers,
  never inferred, mirroring how `specimen` is already handled.
- `measure` — the quantity within that panel (`Total BMD`, `T-Score`).
- `derived_from` — a T-Score is computed from a BMD; a differential `%` is
  computed from an `Absolute`. A derived value must never be compared
  against, or trended alongside, its parent as if it were an independent
  reading.

`name` stays exactly as it is, as the display and citation label, so no
existing source ref breaks.

Phase B is deliberately not implemented in the same change: `panel` must be
read from the document rather than guessed from name prefixes, which means
touching the extractor prompt and re-extracting, not a local migration.

## Consequences

- Prose never becomes a lab row again, and the 6 existing ones are retired
  to where they belong — without a further reader narrowing.
- 175 semi-quantitative results become usable data. That is the largest
  single improvement in this change, and it was invisible from the
  downstream failures that started the investigation.
- Every numeric consumer must now respect `comparator`. A `<20` is a bound;
  code that reads `value` alone would treat it as a point measurement and be
  wrong. This is a real obligation the field creates, not a free win.
- `adoc labs-revalidate` is a data migration on the patient's store. It
  takes a backup first and is dry-run by default.
- Phase B remains open. Until it lands, derived values (T-Score, Z-Score,
  differential percentages) are still stored as if they were independent
  readings, which is the known remaining modelling error.
