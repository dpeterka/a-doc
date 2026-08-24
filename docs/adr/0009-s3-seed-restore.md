# 0009. S3 seed/restore/bootstrap-data flow

Status: Accepted

## Context

The data repo (`ADOC_DATA_DIR`) has no git remote (PHI boundary, CLAUDE.md
rule 1) — it is a system-of-record git repo that only ever lives on one
machine at a time. That creates two problems this ADR resolves together:

1. **Seeding a fresh deployment.** The onboarding wizard (`adoc onboard`)
   is a guided, conversational, non-technical-operator experience — not
   something to run cold against a brand-new empty EFS filesystem attached
   to a headless ECS task. The approved workflow instead runs onboarding
   (and any initial `adoc backfill`/`adoc ingest`) **locally first**,
   where the patient can correct mistakes interactively, and only then
   hands the curated result to the deployed remote.
2. **Disaster recovery.** EFS is durable but not infallible, and a
   redeployed/replaced `ecs` stack, or a fresh environment stood up from
   scratch, starts with an empty filesystem. Something has to reconstruct
   a working data repo on that empty filesystem without a human doing a
   manual `git clone`/`scp` dance over an ECS Exec shell every time.

## Decision

Three commands, one direction each, forming a single reversible pipeline:

- **`adoc backup`** (`backup.py::run_backup`) — git-bundles the full data
  repo (`git bundle create --all`), plus `labs-export.jsonl` (the lossless
  JSONL rebuild of `labs.sqlite`) and a plain sync of the `sources/` tree,
  to fixed keys under `s3://$ADOC_BACKUP_BUCKET/latest/`. This is the same
  command whether run locally after curated onboarding, or by the nightly
  scheduled ECS task — one code path, not two.
- **`adoc restore [--bucket]`** (`backup.py::restore_from_bucket`) — the
  tested inverse: downloads the bundle, clones it into a fresh data repo,
  restores `sources/`, reconciles `labs-export.jsonl` (preferring the copy
  inside the git bundle, cross-checked against the S3 copy), and rebuilds
  `labs.sqlite` from it (`LabsDb.rebuild_from_jsonl`). It refuses outright
  to run over an already-initialized data repo (`DataRepo.is_initialized`)
  — **no `--force` escape hatch is offered**, so a mistaken invocation can
  never clobber live state; the only way to replace existing data is to
  move or remove it by hand first.
- **`adoc bootstrap-data`** (what `docker-entrypoint.sh` runs on every
  container start, web or jobs) — the orchestration a human doesn't have
  to remember: if `ADOC_DATA_DIR` is empty, try `adoc restore` when
  `ADOC_BACKUP_BUCKET` is set; if that raises `NoBackupError` (bucket
  reachable but genuinely nothing there yet — the very first boot of a
  brand-new environment), fall back to `adoc init` instead of failing the
  container. Any *other* exception from `restore_from_bucket` (bad
  credentials, a corrupt bundle, a real S3 error) is **not** swallowed —
  it fails the container loudly, because silently falling back to
  `adoc init` on a real error would quietly discard a patient's history
  behind an empty case file that looks superficially fine.

**Atomic staging.** `restore_from_bucket` never writes directly into
`data_dir`: it downloads and assembles everything (bundle clone, restored
`sources/`, rebuilt `labs.sqlite`, recreated `inbox/`/`work/`/`logs/`) in a
sibling `<data_dir>.restore-staging` directory first, and only `os.rename`s
it into place once *every* step has succeeded. A restore that fails
partway through (network blip mid-download, a malformed bundle) leaves
`data_dir` exactly as it was — empty — rather than a half-populated repo
that a later boot's "is `data_dir` empty?" check would mistake for already
seeded and skip restoring for good.

**`NoBackupError` vs. real-error semantics.** `NoBackupError` is a distinct
`RestoreError` subclass specifically so `bootstrap-data` can tell "nothing
to restore yet, this is fine, fall back to init" apart from every other
failure mode, which must propagate and fail the container. Collapsing
these into one exception type (or into a bare boolean) would force a
choice between "swallow all restore errors" (masks real data loss) or
"never fall back to init" (a brand-new environment could never bootstrap
itself at all).

**`work/users.yaml` is deliberately excluded, on both sides.** Web login
credentials (`web/users.py`) live at `<data_dir>/work/users.yaml`, inside
the gitignored `work/` directory. Backup's `_sync_sources` only walks
`sources/`, and `run_backup` never touches `work/`/`inbox/`/`logs/` at
all; restore recreates those three "operational" directories
(`_OPERATIONAL_DIRS`) empty, not populated from any backed-up content.
This is intentional, not an oversight: login credentials are
deployment-local state (who has access to *this* running service),
not part of the patient's medical case history the backup/restore
pipeline exists to protect — and re-provisioning them post-restore is a
30-second ECS Exec command (`adoc user add`), not worth the complexity of
backing up and restoring a secrets file. See README.md's restore-drill
section, which calls this out explicitly as a required post-restore step.

## Consequences

- Seeding a new deployment is a two-step, no-manual-file-copy flow:
  curate locally → `adoc backup` → the remote bootstraps itself on next
  boot. No `scp`/ECS-Exec-`git clone` runbook step exists any more.
- A restore drill (PLAN.md/CLAUDE.md/ADR 0004 release-gate requirement)
  must always end with re-provisioning logins — a restore that "succeeds"
  but leaves nobody able to log in is not a passing drill.
- `bootstrap-data` makes container startup slower on a genuine first-ever
  boot with a real backup present (git clone + `sources/` sync + SQLite
  rebuild) — `deploy/cfn/ecs.yaml`'s `HealthCheckGracePeriodSeconds: 900`
  is sized to cover that, not the steady-state case (see README.md's
  deploy-window note).
- The `NoBackupError` distinction must be preserved by any future refactor
  of `backup.py` — collapsing it into a generic exception would silently
  reintroduce the "mask real errors" or "can never cold-bootstrap" failure
  mode this ADR explicitly avoided.
