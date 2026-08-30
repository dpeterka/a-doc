"""Application settings and model-role bindings.

Settings are loaded from the environment (and an optional `.env` file) via
pydantic-settings. Model role -> provider/model bindings live in a separate
`models.yaml` file (see PLAN.md "Model strategy & self-evaluation") so they
can be changed without a code release (CLAUDE.md rule 4).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from ruamel.yaml import YAML

_DEFAULT_MODELS_FILE = Path("models.yaml")


class Settings(BaseSettings):
    """Runtime configuration for the adoc application.

    Most fields are read under the `ADOC_` env-var prefix (e.g.
    `ADOC_DATA_DIR`). The three provider API keys are a deliberate exception:
    per the task spec and `.env.example`, they are supplied as bare
    (non-prefixed) env vars (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`,
    `FEATHERLESS_API_KEY`) because those are the conventional names the
    provider SDKs themselves look for. pydantic-settings only supports a
    single `env_prefix` per model, so each key field uses `AliasChoices` to
    accept the bare name explicitly rather than the prefixed one.
    """

    model_config = SettingsConfigDict(
        env_prefix="ADOC_",
        env_file=".env",
        extra="ignore",
    )

    data_dir: Path
    models_file: Path = _DEFAULT_MODELS_FILE
    dropbox_folder: str = "Dropbox/a-doc-inbox"

    # NCBI E-utilities, used by PubMed search and PMID verification. Both are
    # optional: anonymous access works at a lower rate limit, which is why the
    # shared limiter paces on whether a key is present rather than requiring
    # one. `eutils_email` is the address NCBI would contact about excessive
    # use — configuration rather than a constant, because a personal address
    # does not belong hard-coded in a public repo.
    eutils_email: str = ""
    eutils_api_key: str = ""

    # Launching the LIRICAL sidecar (ADR 0029: separate image, input and
    # output exchanged on EFS). All optional and all empty by default: a local
    # run has no cluster, and the review reports "the phenotype engine did not
    # run" rather than failing. The deployed task definitions supply them.
    #
    # Not derived from the running task's metadata, though that would work:
    # the review must be runnable from a laptop against a copy of the data,
    # and configuration that only resolves inside ECS would make that
    # impossible to test.
    lirical_cluster: str = ""
    lirical_task_definition: str = "a-doc-lirical"
    lirical_subnets: str = ""
    """Comma-separated. A string rather than a list because it arrives as one
    environment variable."""
    lirical_security_groups: str = ""

    def lirical_subnet_ids(self) -> list[str]:
        return [s.strip() for s in self.lirical_subnets.split(",") if s.strip()]

    def lirical_security_group_ids(self) -> list[str]:
        return [s.strip() for s in self.lirical_security_groups.split(",") if s.strip()]

    # SQLite journal mode for `labs.sqlite` (see `labs.db.LabsDb.__init__`'s
    # docstring for the full rationale): "WAL" is the fast local/dev/test
    # default; the deployed ECS/Fargate tasks set
    # `ADOC_SQLITE_JOURNAL_MODE=TRUNCATE` because the data directory is an
    # EFS mount and WAL is unsafe on NFS-family filesystems.
    sqlite_journal_mode: str = "WAL"

    # The compact HPO label/synonym index (`scripts/build_hpo_index.py`),
    # baked into the image at build time. A separate path rather than a file
    # in the data repo: it is a build artifact of a public ontology, not
    # patient data, and versioning it with the release is what makes a
    # phenotype profile reproducible. Absent locally, phenotype matching
    # switches itself off with a warning rather than failing.
    hpo_index_path: Path = Path("/opt/hpo-index.json")

    # Longest single chat message accepted. Enforced on the SERVER as well as
    # in the browser: `maxlength` is a convenience, not a control, and a
    # paste-heavy client or a stale page can exceed it.
    #
    # 2,000 characters is roughly 350 words — longer than any turn in the
    # patient's successful intake session, whose longest was 1,610. The turn
    # that failed in production was 6,775 characters and produced a 6,391-token
    # structured output; splitting that into a few messages is both easier for
    # her to review and far more likely to be recorded accurately.
    max_message_chars: int = 2000

    # Upper bound on a single `/upload` file, in MB (see `web.routes.upload`):
    # rejected before any pipeline call (ingest/vision), with a warm
    # in-page message rather than an unbounded read into memory/disk.
    max_upload_mb: int = 25

    # S3 bucket `adoc backup` uploads the git bundle + labs-export.jsonl +
    # sources/ to (see `adoc.backup`). Set by the ECS task definitions
    # (`deploy/cfn/ecs.yaml`) from the backup stack's bucket name; unset for
    # local dev, where `adoc backup` fails fast with a clear error instead
    # of silently no-op'ing.
    backup_bucket: str | None = None

    anthropic_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("ANTHROPIC_API_KEY")
    )
    openai_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("OPENAI_API_KEY")
    )
    featherless_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("FEATHERLESS_API_KEY")
    )

    # Legacy single-passphrase web login. Kept for backward compatibility
    # only — the login route (`web.routes.auth`) now checks per-user
    # credentials in `web.users` instead, and no longer reads this field.
    session_passphrase: SecretStr | None = None

    # True iff the process is only ever reachable through the ALB (see
    # `deploy/cfn/ecs.yaml`'s ServiceSecurityGroup, which admits inbound
    # 8080 from the ALB's security group only): when true, the login rate
    # limiter and secure-cookie logic trust the last hop of
    # `X-Forwarded-For`/`X-Forwarded-Proto`. Defaults to false so a local
    # `adoc serve` or a test run never trusts a client-supplied header;
    # the ECS task definitions set `ADOC_TRUST_FORWARDED_FOR=true` in the
    # deployed environment.
    trust_forwarded_for: bool = False


class ModelBinding(BaseModel):
    """One role -> provider/model binding from `models.yaml`."""

    provider: Literal["anthropic", "openai", "featherless"]
    model: str
    params: dict[str, Any] = Field(default_factory=dict)
    context_window: int | None = None
    """Total input+output tokens this binding accepts, DECLARED not guessed.

    Declared per binding for the same reason model ids are (CLAUDE.md rule
    4): the real limit depends on the model *and* the host — a model with a
    128k native window can be served with less by a hosted provider, and
    that is not discoverable from the model id.

    Matters because a multi-bound role sends the SAME payload to every
    binding: `blind_panel` renders one context pack and hands it to three
    families. The usable budget for such a role is therefore the SMALLEST
    window among its bindings — the weakest link — not the largest, and not
    a per-model calculation. See `LlmClient.context_budget`.

    `None` means undeclared, which disables the pre-flight check for that
    role rather than inventing a number.
    """


def load_model_bindings(path: Path | None = None) -> dict[str, list[ModelBinding]]:
    """Load `models.yaml` and return every role normalized to a list.

    Every role is returned as `list[ModelBinding]`, even roles bound to a
    single model (e.g. `primary_reasoner`), so callers have one uniform
    shape regardless of whether a role is single- or multi-bound (e.g.
    `blind_panel`, which has 2-3 bindings).
    """
    resolved = path if path is not None else _DEFAULT_MODELS_FILE
    yaml = YAML(typ="safe")
    with resolved.open("r", encoding="utf-8") as fh:
        data = yaml.load(fh) or {}

    roles = data.get("roles", {})
    bindings: dict[str, list[ModelBinding]] = {}
    for role, value in roles.items():
        if isinstance(value, list):
            bindings[role] = [ModelBinding.model_validate(item) for item in value]
        else:
            bindings[role] = [ModelBinding.model_validate(value)]
    return bindings
