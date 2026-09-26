"""One way to call any large language model.

Every place in this project that asks a model something - translating a
paragraph, fitting a translation to its layout box, judging a quality warning,
or a free-form question from an external agent - goes through
:func:`make_llm_client` and :meth:`LLMClient.complete`. What differs between
vendors is only the wire protocol, so that is all a client implements:

``openai-responses``  OpenAI Responses API (OpenAI; DeepSeek)
``openai-chat``       OpenAI-compatible Chat Completions: almost every other
                      vendor and every local server (Qwen/DashScope, Kimi,
                      GLM, Doubao, SiliconFlow, OpenRouter, Groq, Mistral,
                      xAI, Ollama, vLLM, LM Studio, ...)
``anthropic``         Anthropic Messages API (Claude)
``gemini``            Google Gemini generateContent
``litellm``           any model LiteLLM supports (``uv pip install litellm``)

A provider name in the run config selects a preset (protocol, default base
URL, API-key variable, structured-output mode); every field can be overridden,
so a vendor without a preset still works through ``openai-compatible``.

Structured output degrades gracefully. ``json_schema`` asks the server to
enforce the schema, ``json_object`` asks for any JSON object, ``prompt`` only
asks in words; in the last two the schema is appended to the system prompt.
Callers validate the returned JSON themselves in every mode, so a weaker mode
changes how often a reply is rejected, never what is accepted.
"""
from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass, field, replace
from typing import Literal, Protocol

from .schemas import Usage

Protocol_ = Literal["openai-responses", "openai-chat", "anthropic", "gemini", "litellm"]
StructuredMode = Literal["auto", "json_schema", "json_object", "prompt"]


@dataclass(frozen=True)
class Preset:
    protocol: str
    base_url: str | None
    api_key_env: str | None
    structured_output: str
    strict_schema: bool = False


#: Vendor presets. ``base_url`` and ``api_key_env`` are defaults the run config
#: may override; ``openai-compatible`` has no default endpoint on purpose.
PRESETS: dict[str, Preset] = {
    "openai": Preset("openai-responses", None, "OPENAI_API_KEY", "json_schema", strict_schema=True),
    # DeepSeek documents type/name/schema for structured output but not the
    # additional `strict` flag.
    "deepseek": Preset("openai-responses", "https://api.deepseek.com", "DEEPSEEK_API_KEY", "json_schema"),
    "anthropic": Preset("anthropic", "https://api.anthropic.com", "ANTHROPIC_API_KEY", "json_schema"),
    "gemini": Preset("gemini", "https://generativelanguage.googleapis.com/v1beta", "GEMINI_API_KEY", "json_object"),
    "qwen": Preset("openai-chat", "https://dashscope.aliyuncs.com/compatible-mode/v1", "DASHSCOPE_API_KEY", "json_object"),
    "moonshot": Preset("openai-chat", "https://api.moonshot.cn/v1", "MOONSHOT_API_KEY", "json_object"),
    "zhipu": Preset("openai-chat", "https://open.bigmodel.cn/api/paas/v4", "ZHIPUAI_API_KEY", "json_object"),
    "doubao": Preset("openai-chat", "https://ark.cn-beijing.volces.com/api/v3", "ARK_API_KEY", "json_object"),
    "siliconflow": Preset("openai-chat", "https://api.siliconflow.cn/v1", "SILICONFLOW_API_KEY", "json_object"),
    "openrouter": Preset("openai-chat", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", "json_object"),
    "groq": Preset("openai-chat", "https://api.groq.com/openai/v1", "GROQ_API_KEY", "json_object"),
    "mistral": Preset("openai-chat", "https://api.mistral.ai/v1", "MISTRAL_API_KEY", "json_object"),
    "xai": Preset("openai-chat", "https://api.x.ai/v1", "XAI_API_KEY", "json_object"),
    "ollama": Preset("openai-chat", "http://localhost:11434/v1", None, "json_object"),
    "openai-compatible": Preset("openai-chat", None, None, "json_object"),
    "litellm": Preset("litellm", None, None, "json_object"),
}
PRESET_ALIASES = {"kimi": "moonshot", "glm": "zhipu", "dashscope": "qwen", "volcengine": "doubao",
                  "claude": "anthropic", "google": "gemini", "vllm": "openai-compatible", "lmstudio": "openai-compatible"}


class LLMHTTPError(RuntimeError):
    """The model service refused the request (HTTP status >= 400)."""

    def __init__(self, status_code: int, message: str):
        super().__init__(f"HTTP {status_code}: {message[:300]}")
        self.status_code = status_code


@dataclass(frozen=True)
class LLMRequest:
    system: str
    user: str
    model_id: str
    max_output_tokens: int
    #: When set, the reply must be one JSON object; the client asks for it as
    #: strongly as the endpoint allows. Callers still validate the result.
    json_schema: dict | None = None
    schema_name: str = "response"
    reasoning_effort: str | None = None
    #: PNG images shown to the model after the text (vision-capable models).
    images: tuple[bytes, ...] = ()
    temperature: float | None = None


@dataclass(frozen=True)
class LLMResponse:
    text: str
    usage: Usage
    response_id: str | None = None
    #: Why generation stopped early, e.g. ``max_output_tokens``; None when complete.
    truncated_reason: str | None = None
    raw: dict = field(default_factory=dict, repr=False, compare=False)


class LLMClient(Protocol):
    protocol: str
    api_key_env: str | None

    def complete(self, request: LLMRequest) -> LLMResponse: ...


@dataclass(frozen=True)
class Endpoint:
    provider: str
    protocol: str
    base_url: str | None
    api_key_env: str | None
    structured_output: str
    strict_schema: bool
    #: A key given for this client only (e.g. by a remote caller); never stored.
    #: When unset the key is read from ``api_key_env``.
    api_key: str | None = field(default=None, repr=False, compare=False)


def known_providers() -> list[str]:
    return sorted([*PRESETS, *PRESET_ALIASES])


def resolve_endpoint(provider: str, *, api_protocol: str | None = None, base_url: str | None = None,
                     api_key_env: str | None = None, structured_output: str = "auto") -> Endpoint:
    """Combine a preset with explicit overrides from the run config."""
    name = PRESET_ALIASES.get(provider, provider)
    if name not in PRESETS:
        raise ValueError(f"unknown provider {provider!r}; use one of {known_providers()} "
                         "or 'openai-compatible' with base_url for any other OpenAI-compatible service")
    preset = PRESETS[name]
    protocol = api_protocol or preset.protocol
    url = base_url or preset.base_url
    if protocol == "openai-chat" and not url:
        raise ValueError(f"provider {provider!r} needs base_url, e.g. http://localhost:8000/v1")
    mode = preset.structured_output if structured_output == "auto" else structured_output
    return Endpoint(name, protocol, url, api_key_env or preset.api_key_env, mode, preset.strict_schema and mode == "json_schema")


def make_llm_client(provider: str, *, api_protocol: str | None = None, base_url: str | None = None,
                    api_key_env: str | None = None, structured_output: str = "auto", api_key: str | None = None) -> LLMClient:
    endpoint = resolve_endpoint(provider, api_protocol=api_protocol, base_url=base_url,
                                api_key_env=api_key_env, structured_output=structured_output)
    if api_key:
        endpoint = replace(endpoint, api_key=api_key)
    return client_for(endpoint)


def client_for(endpoint: Endpoint) -> LLMClient:
    kinds = {
        "openai-responses": OpenAIResponsesClient,
        "openai-chat": OpenAIChatClient,
        "anthropic": AnthropicClient,
        "gemini": GeminiClient,
        "litellm": LiteLLMClient,
    }
    if endpoint.protocol not in kinds:
        raise ValueError(f"unknown api_protocol {endpoint.protocol!r}; use one of {sorted(kinds)}")
    return kinds[endpoint.protocol](endpoint)


def config_client(config, *, api_key: str | None = None) -> LLMClient:
    """The client a run config asks for; ``api_key`` overrides the key variable."""
    return make_llm_client(config.provider, api_protocol=config.api_protocol, base_url=config.base_url,
                           api_key_env=config.api_key_env, structured_output=config.structured_output, api_key=api_key)


def schema_instruction(schema: dict) -> str:
    return ("\nReturn exactly one JSON object and nothing else - no prose, no Markdown fence. "
            "It must validate against this JSON Schema:\n" + json.dumps(schema, ensure_ascii=False))


def strip_json_fence(text: str) -> str:
    """Models asked for JSON in words sometimes wrap it in a Markdown fence."""
    body = text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[1] if "\n" in body else ""
        body = body.rsplit("```", 1)[0]
    return body.strip()


def _api_key(endpoint: Endpoint) -> str:
    if endpoint.api_key:
        return endpoint.api_key
    if endpoint.api_key_env is None:
        return "not-needed"  # local servers such as Ollama accept any key
    key = os.environ.get(endpoint.api_key_env)
    if not key:
        raise RuntimeError(f"missing API key environment variable: {endpoint.api_key_env}")
    return key


def _b64(image: bytes) -> str:
    return base64.b64encode(image).decode("ascii")


class _Base:
    def __init__(self, endpoint: Endpoint):
        self.endpoint = endpoint
        self.protocol = endpoint.protocol
        self.api_key_env = endpoint.api_key_env
        self.base_url = endpoint.base_url

    def _system(self, request: LLMRequest, *, schema_enforced: bool) -> str:
        if request.json_schema is not None and not schema_enforced:
            return request.system + schema_instruction(request.json_schema)
        return request.system


class OpenAIResponsesClient(_Base):
    """OpenAI Responses API. Also what DeepSeek's endpoint speaks."""

    def complete(self, request: LLMRequest) -> LLMResponse:
        from openai import OpenAI

        options = {"api_key": _api_key(self.endpoint)}
        if self.base_url:
            options["base_url"] = self.base_url
        schema_enforced = request.json_schema is not None and self.endpoint.structured_output == "json_schema"
        content: str | list = request.user
        if request.images:
            content = [{"role": "user", "content": [{"type": "input_text", "text": request.user}, *[
                {"type": "input_image", "image_url": f"data:image/png;base64,{_b64(image)}"} for image in request.images]]}]
        arguments = {
            "model": request.model_id,
            "instructions": self._system(request, schema_enforced=schema_enforced),
            "input": content,
            "max_output_tokens": request.max_output_tokens,
            "store": False,
        }
        if request.json_schema is not None:
            if schema_enforced:
                response_format = {"type": "json_schema", "name": request.schema_name, "schema": request.json_schema}
                if self.endpoint.strict_schema:
                    response_format["strict"] = True
            else:
                response_format = {"type": "json_object"}
            arguments["text"] = {"format": response_format}
        if request.reasoning_effort:
            arguments["reasoning"] = {"effort": request.reasoning_effort}
        if request.temperature is not None:
            arguments["temperature"] = request.temperature
        response = OpenAI(**options).responses.create(**arguments)
        usage_obj = getattr(response, "usage", None)
        details = getattr(usage_obj, "output_tokens_details", None)
        usage = Usage(
            input_tokens=getattr(usage_obj, "input_tokens", None),
            cached_input_tokens=getattr(getattr(usage_obj, "input_tokens_details", None), "cached_tokens", None),
            output_tokens=getattr(usage_obj, "output_tokens", None),
            reasoning_tokens=getattr(details, "reasoning_tokens", None),
            usage_source="provider",
        )
        truncated = None
        if getattr(response, "status", None) == "incomplete":
            truncated = getattr(getattr(response, "incomplete_details", None), "reason", None) or "unknown"
        return LLMResponse(text=getattr(response, "output_text", "") or "", usage=usage,
                           response_id=getattr(response, "id", None), truncated_reason=truncated)


#: Weaker structured-output modes to fall back to, in order.
DOWNGRADE = {"json_schema": "json_object", "json_object": "prompt"}


def _rejects_response_format(error: Exception) -> bool:
    """A 400 whose message blames response_format: the endpoint lacks that mode."""
    return getattr(error, "status_code", None) == 400 and "response_format" in str(error)


class OpenAIChatClient(_Base):
    """OpenAI-compatible Chat Completions: the lingua franca of model vendors.

    Support for ``response_format`` varies by vendor and even by model, so a
    400 that names it steps down one mode (json_schema -> json_object ->
    prompt) and the client remembers the mode that worked.
    """

    def __init__(self, endpoint: Endpoint):
        super().__init__(endpoint)
        self.mode = endpoint.structured_output

    def complete(self, request: LLMRequest) -> LLMResponse:
        while True:
            try:
                return self._complete(request, self.mode)
            except Exception as error:
                if request.json_schema is None or self.mode not in DOWNGRADE or not _rejects_response_format(error):
                    raise
                self.mode = DOWNGRADE[self.mode]

    def _complete(self, request: LLMRequest, mode: str) -> LLMResponse:
        from openai import OpenAI

        client = OpenAI(api_key=_api_key(self.endpoint), base_url=self.base_url)
        schema_enforced = request.json_schema is not None and mode == "json_schema"
        user: str | list = request.user
        if request.images:
            user = [{"type": "text", "text": request.user}, *[
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{_b64(image)}"}} for image in request.images]]
        arguments = {
            "model": request.model_id,
            "messages": [{"role": "system", "content": self._system(request, schema_enforced=schema_enforced)},
                         {"role": "user", "content": user}],
            "max_tokens": request.max_output_tokens,
        }
        if request.json_schema is not None and mode == "json_schema":
            arguments["response_format"] = {"type": "json_schema", "json_schema": {
                "name": request.schema_name, "schema": request.json_schema, "strict": self.endpoint.strict_schema}}
        elif request.json_schema is not None and mode == "json_object":
            arguments["response_format"] = {"type": "json_object"}
        if request.reasoning_effort and request.reasoning_effort != "none":
            arguments["reasoning_effort"] = request.reasoning_effort
        if request.temperature is not None:
            arguments["temperature"] = request.temperature
        response = client.chat.completions.create(**arguments)
        choice = response.choices[0]
        usage_obj = getattr(response, "usage", None)
        prompt_details = getattr(usage_obj, "prompt_tokens_details", None)
        completion_details = getattr(usage_obj, "completion_tokens_details", None)
        usage = Usage(
            input_tokens=getattr(usage_obj, "prompt_tokens", None),
            cached_input_tokens=getattr(prompt_details, "cached_tokens", None) or getattr(usage_obj, "prompt_cache_hit_tokens", None),
            output_tokens=getattr(usage_obj, "completion_tokens", None),
            reasoning_tokens=getattr(completion_details, "reasoning_tokens", None),
            usage_source="provider" if usage_obj is not None else "unavailable",
        )
        text = choice.message.content or ""
        if request.json_schema is not None and not schema_enforced:
            text = strip_json_fence(text)
        truncated = "max_output_tokens" if choice.finish_reason == "length" else None
        return LLMResponse(text=text, usage=usage, response_id=getattr(response, "id", None), truncated_reason=truncated)


def _post_json(url: str, headers: dict, body: dict, timeout: float = 600.0) -> dict:
    import httpx

    response = httpx.post(url, headers=headers, json=body, timeout=timeout)
    if response.status_code >= 400:
        raise LLMHTTPError(response.status_code, response.text)
    return response.json()


class AnthropicClient(_Base):
    """Anthropic Messages API. Structured output is a forced tool call."""

    VERSION = "2023-06-01"

    def complete(self, request: LLMRequest) -> LLMResponse:
        content: list = [{"type": "text", "text": request.user}]
        content += [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": _b64(image)}}
                    for image in request.images]
        body = {
            "model": request.model_id,
            "max_tokens": request.max_output_tokens,
            "system": request.system,
            "messages": [{"role": "user", "content": content}],
        }
        forced_tool = request.json_schema is not None and self.endpoint.structured_output == "json_schema"
        if forced_tool:
            body["tools"] = [{"name": request.schema_name, "description": "Return the answer as this object.",
                              "input_schema": request.json_schema}]
            body["tool_choice"] = {"type": "tool", "name": request.schema_name}
        elif request.json_schema is not None:
            body["system"] = self._system(request, schema_enforced=False)
        if request.temperature is not None:
            body["temperature"] = request.temperature
        data = _post_json(f"{self.base_url.rstrip('/')}/v1/messages", {
            "x-api-key": _api_key(self.endpoint), "anthropic-version": self.VERSION, "content-type": "application/json",
        }, body)
        blocks = data.get("content", [])
        if forced_tool:
            tool = next((b for b in blocks if b.get("type") == "tool_use"), None)
            text = json.dumps(tool["input"], ensure_ascii=False) if tool else ""
        else:
            text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
            if request.json_schema is not None:
                text = strip_json_fence(text)
        usage_obj = data.get("usage", {})
        usage = Usage(input_tokens=usage_obj.get("input_tokens"), output_tokens=usage_obj.get("output_tokens"),
                      cached_input_tokens=usage_obj.get("cache_read_input_tokens"), usage_source="provider")
        truncated = "max_output_tokens" if data.get("stop_reason") == "max_tokens" else None
        return LLMResponse(text=text, usage=usage, response_id=data.get("id"), truncated_reason=truncated, raw=data)


class GeminiClient(_Base):
    """Google Gemini generateContent (REST)."""

    def complete(self, request: LLMRequest) -> LLMResponse:
        parts: list = [{"text": request.user}]
        parts += [{"inline_data": {"mime_type": "image/png", "data": _b64(image)}} for image in request.images]
        config: dict = {"maxOutputTokens": request.max_output_tokens}
        if request.json_schema is not None:
            config["responseMimeType"] = "application/json"
            if self.endpoint.structured_output == "json_schema":
                config["responseJsonSchema"] = request.json_schema
        if request.reasoning_effort == "none":
            config["thinkingConfig"] = {"thinkingBudget": 0}
        if request.temperature is not None:
            config["temperature"] = request.temperature
        schema_enforced = request.json_schema is not None and self.endpoint.structured_output == "json_schema"
        body = {
            "systemInstruction": {"parts": [{"text": self._system(request, schema_enforced=schema_enforced)}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": config,
        }
        data = _post_json(f"{self.base_url.rstrip('/')}/models/{request.model_id}:generateContent",
                          {"x-goog-api-key": _api_key(self.endpoint), "content-type": "application/json"}, body)
        candidate = (data.get("candidates") or [{}])[0]
        text = "".join(p.get("text", "") for p in candidate.get("content", {}).get("parts", []) if not p.get("thought"))
        if request.json_schema is not None:
            text = strip_json_fence(text)
        meta = data.get("usageMetadata", {})
        usage = Usage(input_tokens=meta.get("promptTokenCount"), output_tokens=meta.get("candidatesTokenCount"),
                      cached_input_tokens=meta.get("cachedContentTokenCount"), reasoning_tokens=meta.get("thoughtsTokenCount"),
                      usage_source="provider" if meta else "unavailable")
        truncated = "max_output_tokens" if candidate.get("finishReason") == "MAX_TOKENS" else None
        return LLMResponse(text=text, usage=usage, response_id=data.get("responseId"), truncated_reason=truncated, raw=data)


class LiteLLMClient(_Base):
    """Any model LiteLLM routes to (``model_id`` like ``bedrock/...``, ``azure/...``)."""

    def complete(self, request: LLMRequest) -> LLMResponse:
        try:
            import litellm
        except ImportError as exc:
            raise RuntimeError(("provider 'litellm' needs the optional package: uv pip install litellm "
                               "(LiteLLM currently requires openai<3, so that environment moves to openai 2.x)")) from exc
        user: str | list = request.user
        if request.images:
            user = [{"type": "text", "text": request.user}, *[
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{_b64(image)}"}} for image in request.images]]
        arguments = {
            "model": request.model_id,
            "messages": [{"role": "system", "content": self._system(request, schema_enforced=False)},
                         {"role": "user", "content": user}],
            "max_tokens": request.max_output_tokens,
        }
        if self.api_key_env or self.endpoint.api_key:
            arguments["api_key"] = _api_key(self.endpoint)
        if self.base_url:
            arguments["api_base"] = self.base_url
        if request.json_schema is not None and self.endpoint.structured_output != "prompt":
            arguments["response_format"] = {"type": "json_object"}
        if request.temperature is not None:
            arguments["temperature"] = request.temperature
        response = litellm.completion(**arguments)
        choice = response.choices[0]
        usage_obj = getattr(response, "usage", None)
        usage = Usage(input_tokens=getattr(usage_obj, "prompt_tokens", None), output_tokens=getattr(usage_obj, "completion_tokens", None),
                      usage_source="provider" if usage_obj is not None else "unavailable")
        text = choice.message.content or ""
        if request.json_schema is not None:
            text = strip_json_fence(text)
        truncated = "max_output_tokens" if choice.finish_reason == "length" else None
        return LLMResponse(text=text, usage=usage, response_id=getattr(response, "id", None), truncated_reason=truncated)
