# Local-dev scripts

Repeatable tooling for standing up a local a-doc environment from the real
data store, instead of hand-rolled `cp -r` / `adoc init` / ad-hoc resets.

**Safe store.** `ADOC_SAFE_STORE` (default `$HOME/a-doc-data-local`) is
treated as **read-only** by everything here — only ever read from or
`git clone`d, never written to, deleted, or committed into. Your actual
working copy lives at `--dir` (default `$HOME/a-doc-data-test`) and is the
only thing these scripts ever mutate. Populating a working dir uses
`git clone` (committed content only), so gitignored material in the safe
store — `labs.sqlite`, `work/users.yaml` (real login credentials!),
`logs/`, `inbox/` — is never copied into your test dir.

Requires `uv` (resolved from `PATH`, falling back to `~/.local/bin/uv`),
`git`, and `curl` (for the `/healthz` wait). `uv sync --all-extras` should
already have been run once in the repo.

## Bring up a local copy

```
scripts/start-local
```

Clones `ADOC_SAFE_STORE` into `$HOME/a-doc-data-test` (first run only —
reused as-is on later runs), starts `adoc serve` in the background, waits
for `/healthz`, and prints the URL.

```
scripts/start-local --dir ~/scratch/a-doc-2 --port 9001
scripts/start-local --force        # wipe and re-clone from the safe store
scripts/stop-local                 # stop it
scripts/restart-local --port 9001  # stop + start, same options
```

`start-local` refuses to start a second time over a dir that's already
running (use `restart-local`), and refuses `--force` against anything that
resolves to the safe store, `$HOME`, or a top-level system directory.

## Log in

```
scripts/user-create-local          # adoc user add <name> — prompts for a password
scripts/user-list-local
```

`user-create-local` needs an interactive terminal (it prompts for a
password); run from a real shell, not a script or CI job.

## Test intake from scratch

```
scripts/start-local --intake
```

Clears intake facts/coverage/transcript, restores the 5 onboarding-derived
case files (`case/case-summary.md`, `questions-open.md`,
`family-history.md`, `medications.md`, `care-team.md`) to their `adoc init`
stubs, and clears `logs/chat` — so the next `/chat` turn is a brand-new
initial visit. Commits the reset. Never touches `sources/`, `doc-text/`,
labs data, encounters, the differential ledger, or `work/users.yaml` (so
existing logins survive). Combine with `--no-start` to reset without also
launching the server.

## Rebuild derived state after a fresh clone

```
scripts/start-local --re-index
```

Rebuilds `labs.sqlite` from the committed `labs-export.jsonl` and the
document-text search index from the committed `doc-text/*.txt` files,
without re-running ingestion. Reports row/document counts.

## Run the baseline-vs-DAG comparison

```
scripts/start-local --experiment baseline --no-start   # labs-only control
scripts/start-local --experiment dag --no-start        # full production DAG
scripts/start-local --experiment all --no-start        # both
```

`baseline` (alias `study` — the owner's "DAG, Study, All" list didn't
define "Study"; this interprets it as the labs-only baseline control, so
flag it if that's wrong) is one completion against the lab data alone.
`dag` runs one real diagnostic turn through the production pipeline
(Ledger-Maintainer → Challenger → apply → Composer) and **mutates the
working repo's ledger**, same as a real chat turn. Both refuse to run
against the safe store. Output goes to `<dir>/case/experiments/<name>.md`;
**stdout is metadata only** (counts, durations, token usage, model ids) —
never clinical content, since it lands in a terminal and a shell
transcript.

## All options

See `scripts/local-env.sh <verb> --help` (or any wrapper's `--help`) for
the full, current option list and defaults.
