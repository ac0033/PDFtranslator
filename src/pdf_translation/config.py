from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import Field

from .hashing import stable_hash
from .schemas import StrictModel


class RunConfig(StrictModel):
    schema_version: str = "1.0"
    run_id: str
    provider: str
    model_id: str
    source_lang: str = "en"
    target_lang: str = "zh-CN"
    style_revision: str
    prompt_revision: str
    adapter_revision: str
    pricing_revision: str
    max_output_tokens: int = Field(gt=0)
    budget_usd: float = Field(ge=0)
    input_usd_per_million: float | None = Field(default=None, ge=0)
    output_usd_per_million: float | None = Field(default=None, ge=0)
    context_token_budget: int = Field(default=600, gt=0)
    api_key_env: str | None = None
    base_url: str | None = None
    reasoning_effort: str | None = None
    provider_max_attempts: int = Field(default=2, ge=1, le=3)
    output_token_policy: Literal["fixed", "adaptive-v1"] = "fixed"
    context_engine: Literal["v0-fixed", "v1"] = "v0-fixed"
    context_db: str | None = None
    join_cross_page_words: bool = False
    # How many provider requests may be in flight at once. It changes how fast
    # a run finishes, not what it produces, so it is left out of config_hash:
    # raising it must not refuse to resume a run started with a lower value.
    translation_concurrency: int = Field(default=1, ge=1, le=32)
    # Which wire protocol reaches the model; None takes the provider preset's
    # (see pdf_translation.llm.PRESETS).
    api_protocol: Literal["openai-responses", "openai-chat", "anthropic", "gemini", "litellm"] | None = None
    # How strongly to ask for JSON: "auto" takes the preset's mode.
    structured_output: Literal["auto", "json_schema", "json_object", "prompt"] = "auto"

    @property
    def config_hash(self) -> str:
        identity = self.model_dump(mode="json", exclude=EXECUTION_ONLY_FIELDS)
        for name, default in IDENTITY_FIELDS_OMITTED_AT_DEFAULT.items():
            if identity.get(name) == default:
                identity.pop(name)
        return stable_hash(identity)


#: Settings that affect speed only; excluded from the run identity.
EXECUTION_ONLY_FIELDS = frozenset({"translation_concurrency"})
#: Settings added after runs already existed. They change the output when set,
#: so they belong to the identity then; at their default they are left out, so
#: a run started before the field existed keeps its hash and can resume.
IDENTITY_FIELDS_OMITTED_AT_DEFAULT = {"api_protocol": None, "structured_output": "auto"}


def load_config(path: Path) -> RunConfig:
    with path.open("rb") as stream:
        return RunConfig.model_validate(tomllib.load(stream))
