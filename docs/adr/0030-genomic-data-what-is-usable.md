# ADR 0030 — What this patient's genomic data can and cannot support

- Status: Accepted (2026-08-27)
- Scopes the deterministic genomics engine that ADR 0010 anticipated
  ("this ADR's 'never LLM-processed' rule constrains the *intake* path, not a
  future deterministic genomics engine").

## Context

The ask was to turn the archived genomic data into a knowledge document a
model can use to weigh hypotheses. ADR 0010 already permits this: the raw
bytes never reach a model, and a derived artifact is what the reasoner sees.

Before building anything, the files were examined. **Structure and locus
coverage only** — which loci a chip interrogates is a property of the chip;
what they say is the patient's data and was deliberately kept out of the
work log.

28 files, 431 MB, in three distinct kinds.

**1. The 23andMe v5 raw export.** 643,161 rows, GRCh37, plus-strand,
`rsid / chromosome / position / genotype`. Directly measured genotypes.
Coverage of a curated autoimmune-relevant panel: **12 of 14**, including
HFE C282Y and H63D, the coeliac HLA-DQ2.5 and DQ8 tags, both HLA-B27 tags,
STAT4, TNFAIP3, SH2B3 and TNPO3/IRF5. Missing: PTPN22 R620W (rs2476601) and
IRF5 rs2004640, neither of which is on the v5 chip.

**2. Two "phased" exports.** 23andMe's own header: *"This file contains
calculated genotypes ... Phasing is a statistical process and may contain
errors. As such, this data is suitable only for research, educational, and
informational use and not for medical or other use."* They also cover only
6 of the 14 curated loci.

**3. 25 per-chromosome imputed BCFs.** Valid BCF2. Their header carries
`##DISCLAIMER=This file contains imputed genotype data ...`, GRCh38 contigs,
and exactly one FORMAT field: `HDS` — haploid dosage. **No `GT`, and no
imputation quality metric: no `R2`, no `INFO`, no `INFO` definitions at
all.**

## Decision

**Build the knowledge document from the raw array export only.**

**Exclude the phased exports.** The vendor states they are not for medical
use, and they cover fewer of the relevant loci than the file they derive
from. There is no case for preferring them.

**Exclude the imputed BCFs.** Without a per-variant quality score there is
no way to distinguish a confidently imputed common variant from a coin
flip, and imputation's whole purpose is to infer sites the array did not
measure. Thresholding `HDS` into best-guess genotypes would recover
reliably only the sites already present on the array — an information gain
of approximately zero, bought with a genome-build mismatch (GRCh38 against
the array's GRCh37) and a false impression of coverage. 431 MB stays
archived and unread.

**The artifact's job is confirmatory-test leads, not findings.** 23andMe
states that only a subset of markers on the raw file are individually
validated. That fits this system's posture exactly: the document should say
"the array suggests X; the clinical test that settles it is Y", which is
input to the Test-Chooser rather than a conclusion. HFE, HLA-B27 and coeliac
HLA typing all have proper clinical tests.

**Three properties the artifact must have:**

1. **A bounded curated panel, never a dump.** The blind panel's context
   budget is 31,232 tokens. A variant dump is useless to a model and is also
   the only genuinely re-identifying form this data takes; an interpreted
   panel is neither.
2. **A fixed header stating that absence is not exclusion.** A model reading
   a missing pathogenic variant as an exclusion is the most dangerous
   misreading available here, and it must be closed in the artifact rather
   than in a prompt.
3. **Citable claims.** A `genomic:<gene>:<variant>` source-ref type, checked
   by the same machinery as every other claim (ADR 0028), so a model cannot
   assert a variant the patient does not have.

## Consequences

- No `bcftools` and no `pysam`: parsing one tab-separated text file needs
  neither, so the app image and its dependency set are untouched.
- **A hypothesis already on the ledger is unreachable by this data.** The
  panel raised FMR1 premutation / FXPOI; that is a CGG repeat expansion,
  invisible to both a genotyping array and imputation. The artifact should
  say so explicitly, because "we have genome data on file" otherwise reads
  as though the question has been covered.
- Should real sequencing ever happen (PLAN.md Phase 4), the exclusion of the
  BCFs is not a precedent — a VCF with quality-scored rare-variant calls is
  a different input, and LIRICAL's genotype mode becomes available with it.
- The engine itself is not built by this ADR. What is decided here is which
  of the 431 MB is admissible, and on what terms.
