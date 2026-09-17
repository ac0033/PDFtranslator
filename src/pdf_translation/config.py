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

    @property
    def config_hash(self) -> str:
        return stable_hash(self)


def load_config(path: Path) -> RunConfig:
    with path.open("rb") as stream:
        return RunConfig.model_validate(tomllib.load(stream))
