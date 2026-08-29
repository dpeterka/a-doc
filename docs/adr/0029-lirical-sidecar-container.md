# ADR 0029 — LIRICAL as a sidecar container, not a Python reimplementation

- Status: Accepted (2026-08-27)
- Implements the "independent *non-LLM* differential engines" line of
  PLAN.md's anti-anchoring row, and the Phase 3 acceptance criterion that
  reviews show LLM vs LIRICAL differentials with explicit divergence
  adjudication.

## Context

Phase 3 needs a differential engine that is mechanistically independent of
the LLM panel — a third check alongside the cross-family Challenger and the
ledger-blind panel. LIRICAL ranks candidate diseases by likelihood ratio
against each disease's phenotype profile in `phenotype.hpoa`. It has no
memory of the ledger, nothing to anchor on, and cannot be argued into a
conclusion.

It is a Java program. The app image is `python:3.12-slim`. So the question
was whether to reimplement phenotype-only scoring in Python or run the
reference implementation, and the deciding argument was estimated cost:

| | Python reimplementation | Java sidecar |
|---|---|---|
| Production code | ~600–700 lines | ~250 lines |
| Infra code | none | ~120 lines |
| Estimated build cost | 120–220k tokens | 60–110k tokens |

The estimate understates the gap. Validating a reimplementation of medical
likelihood-ratio arithmetic requires ground truth, which means standing up
the Java implementation **anyway** to generate reference output. The Python
path does not replace the Java work, it adds to it — unless we accept an
unvalidated reimplementation.

And the failure mode is asymmetric. A wrong LR implementation does not crash;
it emits confidently wrong disease rankings, which is the worst thing this
system can do. On the same day this decision was taken, a newly written
deterministic criteria scorer in this repo had two real bugs that only the
real corpus exposed, within minutes. Owning the arithmetic of a phenotype
ranker permanently is a liability with no upside.

## Decision

**Run upstream LIRICAL v2.4.1 in its own image** (`deploy/lirical/`), built
from `eclipse-temurin:21-jre-noble`, with input and output exchanged on EFS.
`knowledge/lirical.py` builds the invocation and parses the TSV; it never
computes a ranking. Both halves are pure and offline, so the whole Python
surface is unit-tested against a recorded fixture with no JVM and no network.

**A separate image, not a JRE in the app image.** A JRE would add ~200MB and
a second runtime to every always-on web task, for a capability only the deep
review uses, a few times a day, for about a second.

**Data baked in at build time**, per the knowledge-base decision: a deploy is
reproducible and a running container has no network dependency.

Two data fixes were required, both found by building and running it locally
before any of it reached AWS.

**1. LIRICAL 2.4.1 does not start against the current HGNC release.** It
loads `hgnc_complete_set.txt` into a map keyed by NCBI gene ID with no merge
function, and HGNC now ships two IDs under two symbols each:

```
IllegalStateException: Duplicate key NCBIGene:100874204
  (attempted merging values ALDH1L1-AS1 and SLC41A3-AS1)
```

It aborts at bootstrap, before any analysis. Measured: 2 collisions in 44,403
distinct IDs, both non-coding-RNA/pseudogene antisense entries annotated to no
disease. `dedupe_hgnc.py` keeps the first row per ID at build time. The rule
is generic rather than a hardcoded pair, because a later HGNC release will
collide somewhere else.

**2. The transcript databases are 255MB of the 347MB download and are unused
in phenotype-only mode — but the data resolver checks they exist.** Truncating
them to zero bytes satisfies the resolver and cuts the data to 95MB; the
ranking is byte-identical, so phenotype-only genuinely never deserializes
them.

Layering matters for the second fix. Downloading in one layer and truncating
in the next saves nothing — the full 347MB stays in the lower layer — and that
mistake was measured, not theorised: it produced a **1.18GB** image. Download,
dedupe and truncate are one `RUN`, which brings the image to **632MB** with the
smoke test still passing.

That second fix rests on an assumption about lazy loading, which is why the
image runs a **build-time smoke test**: a fixed synthetic phenotype
(arachnodactyly + aortic root aneurysm + bicuspid aortic valve) must still
rank Loeys-Dietz/Marfan disorders at the top. If a future LIRICAL starts
reading those files, or the HGNC fix stops holding, the *build* fails loudly
instead of a deep review failing quietly in production. The same assertion is
duplicated as a unit test over the recorded fixture.

## Consequences

- A second image to build and push in CI, and a LIRICAL version to track. The
  version is pinned by `ARG LIRICAL_VERSION`, so a bump is a reviewable diff.
- **Phenotype-only, always.** `--assembly` and `--vcf` are never passed.
  LIRICAL's genotype mode assumes rare-variant calls; this patient's genomic
  data is a genotyping array plus imputation carrying no per-variant quality
  metric, so a missing call means "not measured" rather than "not
  present" — which cannot support that reasoning. The deterministic genomics
  engine will get its own ADR when it is built.
- Negated phenotypes are passed (`-n`). Excluded findings are evidence, and a
  ranker that can only consume present findings discards them.
- The phenotype profile now exists (`case/phenotype.yaml`, built by
  `adoc phenotype-backfill` from `knowledge.hpo`'s deterministic label and
  synonym matching). Run against the real case file it produced 82 terms from
  30 encounters, and the full chain has been exercised end to end: profile →
  `lirical prioritize` → ranked diseases.

  **It is built only from OBSERVED sources.** The first version also scanned
  `case-summary.md` and `patient-theories.md`. That added one term and was a
  mistake of principle: those files discuss the ledger's own hypotheses, and
  an engine fed them is no longer independent of the ledger — which is the
  entire reason it is here.

- **The posttest probability is unusable with a large profile; the ranking is
  not.** Run against the real 82-term profile, every posttest probability
  reads 0.00%. An earlier version of this ADR called that "a diffuse-profile
  problem, not an engine problem", which asserted a cause without testing it
  and cleared the engine on no evidence. Measured properly:

  | profile | top-ranked disease | composite LR | posttest |
  |---|---|---|---|
  | all 82 terms | Autoinflammatory disease, systemic, with vasculitis | −25.97 | 0.00% |
  | best-attested 10 | *the same disease* | **+3.43** | 23.67% |
  | random 10 (control) | Alstrom syndrome | −2.13 | 0.00% |

  Two effects, not one. **Size**: adding terms no single disease explains
  drives the composite likelihood ratio from +3.4 to −26, and the probability
  collapses with it. **Selection**: a random ten does *not* recover it
  (−2.13), so this is not simply "fewer terms is better".

  And the ordering survives what the calibration does not. The 82-term run
  shares 8 of its top 20 with the curated subset — including the identical
  first place — against 2 of 20 for a random subset. So the full profile
  carries real signal that the posttest percentage hides.

  A caveat on "signal": consistency is not clinical correctness. That shared
  top-20 also contains entries like "Intellectual developmental disorder,
  autosomal dominant 77", which is not a serious candidate for this patient.
  The runs agree with each other; that does not make them right.

  Two consequences for how this engine gets used: feed it a **curated,
  current** subset rather than every term ever mentioned, and read the
  **ranking** rather than the posttest probability. Neither was obvious
  before measuring, and the first version of this section guessed wrong.

- **The profile it was given contains a known-bad term.** "Coma" is the
  single best-attested term in it, and it is a false positive from "myxedema
  coma" — a real entity with no HPO term for the compound, so only the
  second word matched. The best-attested ten produced a positive LR *despite*
  carrying it.

- Nothing in this ADR touches the ledger. The engine's output becomes a
  candidate differential to be adjudicated against the panel's, through the
  divergence path that already exists.
