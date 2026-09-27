"""What a caller sends to start a job, and the rules that turn it into a runnable job.

A job request names two models:

* ``translation`` - the model that translates every unit (and repairs units);
* ``agent`` - the model the AI OS (the pi agent) reasons with when the
  automatic workflow cannot finish on its own, or when it builds the
  optimised version.

Neither has a built-in default. The first call from a caller must name both;
the service remembers them as that caller's defaults, so later calls may
leave either out. A caller that names a model uses it for that job only,
unless it also sets ``set_default``. A caller is identified by ``client_id``
when given, otherwise by a fingerprint of its access token (see
:func:`caller_key`). Keys are never remembered: they come from the server's
environment (the documented way) or, as a fallback, from a request header
for that one job.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .llm import PRESET_ALIASES, resolve_endpoint

#: USD per million tokens (input, output) for models whose price the service knows.
KNOWN_PRICES = {("deepseek", "deepseek-flash"): (0.30, 1.20), ("deepseek", "deepseek-chat"): (0.27, 1.10)}
#: pi's own name for a provider, and the environment variable it reads the key from.
PI_PROVIDERS = {
    "openai": ("openai", "OPENAI_API_KEY"),
    "anthropic": ("anthropic", "ANTHROPIC_API_KEY"),
    "gemini": ("google", "GEMINI_API_KEY"),
    "google": ("google", "GEMINI_API_KEY"),
    "deepseek": ("deepseek", "DEEPSEEK_API_KEY"),
    "openrouter": ("openrouter", "OPENROUTER_API_KEY"),
    "groq": ("groq", "GROQ_API_KEY"),
    "mistral": ("mistral", "MISTRAL_API_KEY"),
    "xai": ("xai", "XAI_API_KEY"),
}


class JobError(ValueError):
    """A request the job service refuses. ``code`` is stable; ``message`` says why; ``hint`` what to do."""

    def __init__(self, message: str, code: str = "bad_request", hint: str | None = None, status: int = 400):
        super().__init__(message)
        self.code = code
        self.hint = hint
        self.status = status

    def payload(self) -> dict:
        body = {"code": self.code, "message": str(self)}
        if self.hint:
            body["hint"] = self.hint
        return {"error": body}


class ModelChoice(BaseModel):
    """One model: which vendor, which model, how to reach it and what it costs."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    provider: str
    model: str = Field(alias="model_id")
    base_url: str | None = None
    api_key_env: str | None = None
    api_protocol: Literal["openai-responses", "openai-chat", "anthropic", "gemini", "litellm"] | None = None
    structured_output: Literal["auto", "json_schema", "json_object", "prompt"] = "auto"
    input_usd_per_million: float | None = Field(default=None, ge=0)
    output_usd_per_million: float | None = Field(default=None, ge=0)
    reasoning_effort: str | None = None

    def label(self) -> str:
        return f"{self.provider}/{self.model}"

    def known_prices(self) -> tuple[float, float] | None:
        if self.input_usd_per_million is not None and self.output_usd_per_million is not None:
            return self.input_usd_per_million, self.output_usd_per_million
        return KNOWN_PRICES.get((PRESET_ALIASES.get(self.provider, self.provider), self.model))


class Deliver(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Build the optimised version after the usable one. Default on; a caller turns it off explicitly.
    optimized: bool = True
    bilingual: bool = True


class JobRequest(BaseModel):
    """Everything a caller may choose for one job."""

    model_config = ConfigDict(extra="forbid")

    client_id: str | None = Field(default=None, max_length=128)
    translation: ModelChoice | None = None
    agent: ModelChoice | None = None
    set_default: bool = False
    deliver: Deliver = Field(default_factory=Deliver)
    budget_usd: float = Field(default=2.0, gt=0, le=1000)
    allow_network: bool = True
    callback_url: str | None = None
    main_file: str | None = None
    style_note: str | None = Field(default=None, max_length=4000)
    layout_review: Literal["all", "flagged", "off"] = "all"
    vision: bool = True
    workers: int = Field(default=2, ge=1, le=8)
    translation_concurrency: int = Field(default=8, ge=1, le=32)
    max_output_tokens: int = Field(default=2000, gt=0, le=64000)
    estimate_only: bool = False

    @model_validator(mode="after")
    def callback_is_http(self) -> "JobRequest":
        if self.callback_url and not re.match(r"^https?://", self.callback_url):
            raise ValueError("callback_url must be an http(s) URL")
        return self

    @classmethod
    def from_legacy(cls, options: dict) -> "JobRequest":
        """Accept the flat v0.10 ``options`` object: its model fields describe the translation model."""
        options = dict(options or {})
        model_keys = {"provider", "model_id", "base_url", "api_key_env", "api_protocol", "structured_output",
                      "input_usd_per_million", "output_usd_per_million", "reasoning_effort"}
        translation = {key: options.pop(key) for key in list(options) if key in model_keys}
        if translation:
            options["translation"] = translation
        return cls.model_validate(options)


def caller_key(client_id: str | None, token: str | None) -> str:
    """Who is calling, for remembering default models. Never stores the token itself."""
    if client_id:
        return "client:" + client_id
    if token:
        return "token:" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
    return "local"


class ProfileStore:
    """Each caller's default models, one small JSON file per caller."""

    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, caller: str) -> Path:
        return self.root / (hashlib.sha256(caller.encode("utf-8")).hexdigest()[:24] + ".json")

    def get(self, caller: str) -> dict:
        path = self._path(caller)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def put(self, caller: str, *, translation: ModelChoice | None = None, agent: ModelChoice | None = None) -> dict:
        profile = self.get(caller)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if translation is not None:
            profile["translation"] = translation.model_dump(mode="json", exclude_none=True)
        if agent is not None:
            profile["agent"] = agent.model_dump(mode="json", exclude_none=True)
        profile.setdefault("created_at", now)
        profile["updated_at"] = now
        from .jobstate import atomic_write

        atomic_write(self._path(caller), json.dumps(profile, ensure_ascii=False, indent=2))
        return profile

    def resolve(self, caller: str, request: JobRequest) -> tuple[ModelChoice, ModelChoice, dict[str, str]]:
        """The two models this job uses, and where each came from ("request" or "default").

        Raises ``model_required`` when a model is neither given nor remembered,
        before anything is created or spent.
        """
        profile = self.get(caller)
        chosen: dict[str, ModelChoice] = {}
        sources: dict[str, str] = {}
        missing = []
        for role in ("translation", "agent"):
            given = getattr(request, role)
            if given is not None:
                chosen[role], sources[role] = given, "request"
            elif profile.get(role):
                chosen[role], sources[role] = ModelChoice.model_validate(profile[role]), "default"
            else:
                missing.append(role)
        if missing:
            raise JobError(
                f"name the {' and '.join(missing)} model{'s' if len(missing) > 1 else ''}: this caller has no "
                "default yet", code="model_required",
                hint="send e.g. {\"translation\": {\"provider\": \"deepseek\", \"model\": \"deepseek-flash\"}, "
                     "\"agent\": {\"provider\": \"deepseek\", \"model\": \"deepseek-flash\"}}; the first models a "
                     "caller names become its defaults", status=422)
        first_time = not profile.get("translation") and not profile.get("agent")
        if first_time or request.set_default:
            self.put(caller, translation=request.translation if request.translation else None,
                     agent=request.agent if request.agent else None)
        return chosen["translation"], chosen["agent"], sources


def check_translation_model(choice: ModelChoice) -> str | None:
    """Validate the endpoint and prices; returns the API-key variable the model needs."""
    try:
        endpoint = resolve_endpoint(choice.provider, api_protocol=choice.api_protocol, base_url=choice.base_url,
                                    api_key_env=choice.api_key_env, structured_output=choice.structured_output)
    except ValueError as problem:
        raise JobError(f"translation model: {problem}", code="unknown_provider", status=422) from problem
    if choice.known_prices() is None:
        raise JobError(f"translation model {choice.label()} has no known price",
                       code="price_required", status=422,
                       hint="give input_usd_per_million and output_usd_per_million; the budget cannot be enforced without them")
    return endpoint.api_key_env


def agent_key_variable(choice: ModelChoice) -> str:
    """The environment variable holding the AI OS model's key."""
    if choice.api_key_env:
        return choice.api_key_env
    name = PRESET_ALIASES.get(choice.provider, choice.provider)
    if name in PI_PROVIDERS:
        return PI_PROVIDERS[name][1]
    if choice.base_url:
        return f"{re.sub(r'[^A-Z0-9]', '_', choice.provider.upper())}_API_KEY"
    raise JobError(f"agent model: the AI OS cannot reach provider {choice.provider!r} without a base_url",
                   code="unknown_provider", status=422,
                   hint=f"use one of {sorted(PI_PROVIDERS)} or give base_url (an OpenAI-compatible endpoint) and api_key_env")


def require_keys(variables: dict[str, str | None], supplied: dict[str, str]) -> None:
    """Refuse a job whose model keys are nowhere to be found, before anything is created."""
    for role, variable in variables.items():
        if variable and not (supplied.get(variable) or os.environ.get(variable)):
            raise JobError(f"the {role} model needs the environment variable {variable}, which the server does not have",
                           code="key_missing", status=422,
                           hint=f"set {variable} in the server's environment (or .env); a request header is the fallback")


def validation_error(problem: ValidationError) -> JobError:
    first = problem.errors()[0] if problem.errors() else {}
    where = ".".join(str(part) for part in first.get("loc", ()))
    return JobError(f"{where}: {first.get('msg', str(problem))}" if where else str(problem), code="invalid_request", status=422)
