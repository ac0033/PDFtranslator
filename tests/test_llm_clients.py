"""Every supported protocol, against a fake of its own wire format."""
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from pdf_translation import llm
from pdf_translation.config import RunConfig
from pdf_translation.hashing import stable_hash
from pdf_translation.llm import LLMHTTPError, LLMRequest, make_llm_client, resolve_endpoint
from pdf_translation.pipeline import ProviderUnavailable, run_units, translation_provider
from pdf_translation.providers import DeepSeekProvider, LLMTranslationProvider, OpenAIProvider
from pdf_translation.schemas import TranslationUnit

SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}


def ask(**overrides) -> LLMRequest:
    return LLMRequest(system="You answer.", user="Question?", model_id="m", max_output_tokens=50, **overrides)


def config(provider: str, run_id: str = "r", **extra) -> RunConfig:
    return RunConfig(run_id=run_id, provider=provider, model_id="some-model", style_revision="s", prompt_revision="p",
                     adapter_revision="a", pricing_revision="p", max_output_tokens=500, budget_usd=1.0,
                     input_usd_per_million=1.0, output_usd_per_million=1.0, **extra)


# --- presets -----------------------------------------------------------------

def test_presets_cover_mainstream_vendors_and_local_servers():
    for name in ("openai", "deepseek", "anthropic", "gemini", "qwen", "kimi", "glm", "doubao", "siliconflow",
                 "openrouter", "groq", "mistral", "xai", "ollama", "vllm", "litellm"):
        assert resolve_endpoint(name, base_url="http://x/v1").protocol
    assert resolve_endpoint("kimi").base_url == "https://api.moonshot.cn/v1"
    assert resolve_endpoint("ollama").api_key_env is None


def test_overrides_win_over_the_preset():
    endpoint = resolve_endpoint("deepseek", api_protocol="openai-chat", structured_output="json_object",
                                base_url="https://proxy/v1", api_key_env="MY_KEY")
    assert (endpoint.protocol, endpoint.base_url, endpoint.api_key_env, endpoint.structured_output) == \
        ("openai-chat", "https://proxy/v1", "MY_KEY", "json_object")


def test_unknown_or_incomplete_endpoints_are_refused_with_a_way_forward():
    with pytest.raises(ValueError, match="openai-compatible"):
        resolve_endpoint("no-such-vendor")
    with pytest.raises(ValueError, match="needs base_url"):
        resolve_endpoint("openai-compatible")


def test_new_endpoint_fields_leave_existing_run_identities_alone():
    legacy = config("deepseek").model_dump(mode="json")
    for added_later in ("translation_concurrency", "api_protocol", "structured_output"):
        del legacy[added_later]
    assert config("deepseek").config_hash == stable_hash(legacy)
    assert config("deepseek", structured_output="json_object").config_hash != config("deepseek").config_hash


def test_default_deepseek_and_openai_keep_their_dedicated_providers():
    assert isinstance(translation_provider(config("deepseek")), DeepSeekProvider)
    assert type(translation_provider(config("openai"))) is OpenAIProvider
    generic = translation_provider(config("qwen"))
    assert type(generic) is LLMTranslationProvider and generic.client.protocol == "openai-chat"
    assert translation_provider(config("deepseek", api_protocol="openai-chat")).client.protocol == "openai-chat"


# --- OpenAI-compatible chat ---------------------------------------------------

class FakeChat:
    def __init__(self, content: str, finish: str = "stop"):
        self.content, self.finish, self.sent = content, finish, {}

    def __call__(self, **options):
        self.options = options
        return SimpleNamespace(chat=SimpleNamespace(completions=self))

    def create(self, **arguments):
        self.sent = arguments
        return SimpleNamespace(
            id="chat-1", choices=[SimpleNamespace(finish_reason=self.finish, message=SimpleNamespace(content=self.content))],
            usage=SimpleNamespace(prompt_tokens=11, completion_tokens=7, prompt_tokens_details=SimpleNamespace(cached_tokens=3),
                                  completion_tokens_details=None))


def test_chat_json_object_mode_states_the_schema_and_strips_a_fence(monkeypatch):
    fake = FakeChat('```json\n{"answer": "42"}\n```')
    monkeypatch.setattr("openai.OpenAI", fake)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "k")
    reply = make_llm_client("qwen").complete(ask(json_schema=SCHEMA))
    assert json.loads(reply.text) == {"answer": "42"}
    assert fake.sent["response_format"] == {"type": "json_object"}
    assert '"required": ["answer"]' in fake.sent["messages"][0]["content"]
    assert fake.options["base_url"].startswith("https://dashscope")
    assert (reply.usage.input_tokens, reply.usage.output_tokens, reply.usage.cached_input_tokens) == (11, 7, 3)


def test_chat_json_schema_mode_lets_the_server_enforce_it(monkeypatch):
    fake = FakeChat('{"answer": "ok"}')
    monkeypatch.setattr("openai.OpenAI", fake)
    client = make_llm_client("openai-compatible", base_url="http://localhost:8000/v1", structured_output="json_schema")
    client.complete(ask(json_schema=SCHEMA))
    assert fake.sent["response_format"]["type"] == "json_schema"
    assert "JSON Schema" not in fake.sent["messages"][0]["content"]
    assert fake.options["api_key"] == "not-needed"


def test_chat_length_stop_is_reported_as_truncation(monkeypatch):
    monkeypatch.setattr("openai.OpenAI", FakeChat('{"ans', finish="length"))
    assert make_llm_client("ollama").complete(ask(json_schema=SCHEMA)).truncated_reason == "max_output_tokens"


def test_images_reach_a_vision_model(monkeypatch):
    fake = FakeChat("fine")
    monkeypatch.setattr("openai.OpenAI", fake)
    make_llm_client("ollama").complete(ask(images=(b"\x89PNG",)))
    parts = fake.sent["messages"][1]["content"]
    assert parts[0]["type"] == "text" and parts[1]["image_url"]["url"].startswith("data:image/png;base64,")


# --- Anthropic and Gemini (plain HTTP) ------------------------------------------

class FakeHTTP:
    def __init__(self, status: int, body: dict):
        self.status, self.body, self.calls = status, body, []

    def __call__(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "json": json})
        return httpx.Response(self.status, json=self.body, request=httpx.Request("POST", url))


def anthropic_reply(payload: dict, stop: str = "tool_use") -> dict:
    return {"id": "msg-1", "stop_reason": stop, "usage": {"input_tokens": 20, "output_tokens": 9, "cache_read_input_tokens": 4},
            "content": [{"type": "tool_use", "name": "response", "input": payload}]}


def test_anthropic_structured_output_is_a_forced_tool_call(monkeypatch):
    fake = FakeHTTP(200, anthropic_reply({"answer": "yes"}))
    monkeypatch.setattr(httpx, "post", fake)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    reply = make_llm_client("claude").complete(ask(json_schema=SCHEMA))
    sent = fake.calls[0]
    assert sent["url"] == "https://api.anthropic.com/v1/messages" and sent["headers"]["x-api-key"] == "k"
    assert sent["json"]["tool_choice"] == {"type": "tool", "name": "response"}
    assert json.loads(reply.text) == {"answer": "yes"}
    assert (reply.usage.input_tokens, reply.usage.cached_input_tokens) == (20, 4)


def test_anthropic_max_tokens_and_http_errors(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(httpx, "post", FakeHTTP(200, anthropic_reply({}, stop="max_tokens")))
    assert make_llm_client("anthropic").complete(ask(json_schema=SCHEMA)).truncated_reason == "max_output_tokens"
    monkeypatch.setattr(httpx, "post", FakeHTTP(402, {"error": "credit balance too low"}))
    with pytest.raises(LLMHTTPError) as raised:
        make_llm_client("anthropic").complete(ask())
    assert raised.value.status_code == 402


def test_gemini_asks_for_json_and_can_turn_thinking_off(monkeypatch):
    body = {"responseId": "g-1", "candidates": [{"finishReason": "STOP", "content": {"parts": [
        {"text": "thinking...", "thought": True}, {"text": '{"answer": "g"}'}]}}],
        "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 3, "thoughtsTokenCount": 2}}
    fake = FakeHTTP(200, body)
    monkeypatch.setattr(httpx, "post", fake)
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    reply = make_llm_client("gemini").complete(ask(json_schema=SCHEMA, reasoning_effort="none"))
    generation = fake.calls[0]["json"]["generationConfig"]
    assert generation["responseMimeType"] == "application/json" and generation["thinkingConfig"] == {"thinkingBudget": 0}
    assert fake.calls[0]["url"].endswith("/models/m:generateContent")
    assert json.loads(reply.text) == {"answer": "g"} and reply.usage.reasoning_tokens == 2


def test_litellm_without_the_package_says_how_to_install_it(monkeypatch):
    import builtins
    real_import = builtins.__import__
    monkeypatch.setattr(builtins, "__import__", lambda name, *a, **k: (_ for _ in ()).throw(ImportError()) if name == "litellm" else real_import(name, *a, **k))
    with pytest.raises(RuntimeError, match="uv pip install litellm"):
        make_llm_client("litellm").complete(ask())


# --- a whole run through a non-OpenAI vendor ----------------------------------

def units() -> list[TranslationUnit]:
    texts = ["Keep 42 exactly.", "A second paragraph."]
    return [TranslationUnit(unit_id=f"u{n}", document_id="d", ir_revision="r", unit_type="paragraph", source_lang="en",
                            target_lang="zh-CN", source_text=t, source_hash=stable_hash(t)) for n, t in enumerate(texts)]


def test_a_run_translates_and_bills_through_claude(tmp_path: Path, monkeypatch):
    def reply(url, headers=None, json=None, timeout=None):
        unit_id = json["messages"][0]["content"][0]["text"].split("<unit-id>")[1].split("</unit-id>")[0]
        text = {"u0": "准确保留 42。", "u1": "第二段。"}[unit_id]
        body = anthropic_reply({"unit_id": unit_id, "translated_text": text, "uncertainties": []})
        return httpx.Response(200, json=body, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", reply)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    summary = run_units(units(), config("anthropic", run_id="claude-run"), tmp_path)
    assert summary["states"] == {"accepted": 2}
    assert summary["input_tokens"] == 40 and summary["cost_usd"] > 0


def test_an_account_refusal_from_any_vendor_pauses_the_run(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(httpx, "post", FakeHTTP(402, {"error": "billing"}))
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    with pytest.raises(ProviderUnavailable, match="HTTP 402"):
        run_units(units(), config("gemini", run_id="gemini-run"), tmp_path)


def test_an_endpoint_that_rejects_response_format_steps_down_and_remembers(monkeypatch):
    class Rejected(Exception):
        status_code = 400

    class Picky(FakeChat):
        def create(self, **arguments):
            self.sent = arguments
            self.attempts = getattr(self, "attempts", 0) + 1
            if "response_format" in arguments:
                raise Rejected("This response_format type is unavailable now")
            return super().create(**arguments)

    fake = Picky('{"answer": "fine"}')
    monkeypatch.setattr("openai.OpenAI", fake)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    client = make_llm_client("deepseek", api_protocol="openai-chat")
    assert json.loads(client.complete(ask(json_schema=SCHEMA)).text) == {"answer": "fine"}
    assert client.mode == "prompt" and "JSON Schema" in fake.sent["messages"][0]["content"]
    fake.attempts = 0
    client.complete(ask(json_schema=SCHEMA))
    assert fake.attempts == 1  # the working mode is remembered


def test_an_unrelated_bad_request_is_not_mistaken_for_a_format_problem(monkeypatch):
    class Rejected(Exception):
        status_code = 400

    class Broken(FakeChat):
        def create(self, **arguments):
            raise Rejected("model does not exist")

    monkeypatch.setattr("openai.OpenAI", Broken(""))
    with pytest.raises(Rejected):
        make_llm_client("ollama").complete(ask(json_schema=SCHEMA))
