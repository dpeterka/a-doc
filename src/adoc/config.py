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

    anthropic_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("ANTHROPIC_API_KEY")
    )
    openai_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("OPENAI_API_KEY")
    )
    featherless_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("FEATHERLESS_API_KEY")
    )

    session_passphrase: SecretStr | None = None


class ModelBinding(BaseModel):
    """One role -> provider/model binding from `models.yaml`."""

    provider: Literal["anthropic", "openai", "featherless"]
    model: str
    params: dict[str, Any] = Field(default_factory=dict)


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
