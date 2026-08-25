# 0017. Scrubbing is the default, not an opt-in; `case/identifiers.yaml` is scaffolded and CLI-managed; the vision image path is an accepted gap

Status: Accepted (2026-08-25)

## Context

`LlmClient.from_settings` — the factory every real outbound call path
(`web/app.py`'s `create_app`, `cli.py`'s `onboard`) used — passed no
`scrubber` argument, and `LlmClient.__init__` defaulted a missing scrubber
to `Scrubber.noop()`. The result: the web app (every chat turn, every
intake turn, every visit-capture pass) and `adoc onboard` sent unscrubbed
text to Anthropic/OpenAI/Featherless. Only `cli.py`'s `_build_llm_client`
(used by `ingest`/`backfill`/`review`/`labs-dedupe-twins`) built a real
scrubber — and even that scrubber had nothing to scrub: `case/
identifiers.yaml`, the file `Scrubber.from_file` reads patient name/DOB/
address literals from, was never scaffolded, documented, or created, so in
practice only the shape-based SSN/phone/email/MRN regexes ever fired
anywhere. The patient's name, DOB, and address — the owner's explicit hard
line ("direct PII... first/last name, phone, address, DOB, SSN... do not
get transmitted to an external LLM") — were never scrubbed.

The owner's calibration for this fix: proportionate controls, not a HIPAA
program. Clinical content (lab values, analyte names, diagnoses, symptoms)
flowing to providers is expected and must never be touched by the scrubber
— over-scrubbing silently degrades every diagnosis, which is worse than
the bug being fixed.

## Decision

- **Invert the default.** `Scrubber.noop()` as a *fallback* is the footgun:
  a caller who forgets a scrubber silently gets none. `LlmClient.
  from_settings` now defaults `scrubber` to a real `Scrubber.from_file
  (settings.data_dir / "case" / "identifiers.yaml")` when the caller
  passes none; a no-op requires an explicit `scrubber=Scrubber.noop()`.
  The bare `LlmClient(bindings, providers)` constructor keeps its
  `Scrubber.noop()` default unchanged — it has no `Settings`/`data_dir` to
  build a real scrubber from, and every non-test caller of that
  constructor (`evals/suites/*.py`) only ever wires fake, non-network
  transports, so there is nothing for a scrubber to protect there.
- **Scaffold `case/identifiers.yaml`.** `adoc init` now writes a commented
  template (empty by default — no real values are ever known at `init`
  time) documenting every supported field (names, nicknames/maiden name,
  dob, address fragments, phone, mrn, email) and how to populate it, and
  commits it to the data repo like every other `case/` file.
- **`adoc identifiers show|add|remove`** reads/writes that file so the
  owner is never hand-editing YAML blind.
- **Fail loudly, never silently.** `Scrubber.coverage_warning` (and
  `LlmClient.privacy_warning`, which forwards it) is non-`None` whenever
  the identifiers file is missing or has no `names` entries. `cli.py`
  prints it to stderr from every real client-construction site
  (`_build_llm_client`, `onboard`); `web/app.py` prints it once at
  `create_app` time and also stores it on `app.state.privacy_warning`
  (reachable via `web.deps.get_privacy_warning`) as a seam for a
  route/template to surface as a banner, without building a notification
  system to do it.
- **Scope stays text-only, on purpose.** The scrubber matches whole-word/
  whole-phrase literals plus a few fixed shape regexes (SSN/phone/email/
  MRN) in `system`/message strings only. It is not touched by, and does
  not need to be extended to, structured-output schemas or tool
  definitions — those never carry patient-identifying literals, only
  field names/types.

### Rejected alternative: OCR-then-redact page images before vision upload

`ingest/vision.py`'s double-pass extraction sends whole PDF blocks and
rendered page PNGs of scanned lab reports directly to a vision-capable
model; the patient's name/DOB/address, as printed on the document, are
pixels a text scrubber cannot see. Redacting them first (OCR to locate the
identifier text, black out those regions, re-encode) was considered and
rejected: it is fragile (a redaction box that's off by a few pixels either
leaks the identifier or clips an adjacent lab value), and risks damaging
the very values the pipeline exists to extract — a worse failure mode than
the accepted exposure. This is a deliberate, owner-accepted limitation, not
an oversight: the image path sends the patient's name/DOB/address, as
printed, to the vision-capable model on every ingest. See `privacy.py`'s
module docstring for where this boundary is documented in code.

## Consequences

- Every real outbound text call (web chat/intake/visit-capture, `adoc
  onboard`, `adoc ingest`/`backfill`/`review`/`labs-dedupe-twins`) now
  scrubs by default, with no per-call-site code to remember.
- A fresh install is safe by construction for the *text* path: `adoc init`
  always creates the identifiers file, and the app warns loudly (not
  silently) until it's actually populated with the patient's name.
- The image/vision path remains an accepted, documented gap — no
  redaction is built, and none is planned; see PLAN.md's Privacy row.
