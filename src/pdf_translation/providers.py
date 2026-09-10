from __future__ import annotations

import json
import os
from typing import Protocol

from .schemas import TranslationDraft, TranslationRequest, TranslationResult, Usage


class Provider(Protocol):
    def translate(self, request: TranslationRequest) -> TranslationResult: ...


class MockProvider:
    def translate(self, request: TranslationRequest) -> TranslationResult:
        # Deliberately preserves every source token for deterministic offline contract tests.
        draft = TranslationDraft(unit_id=request.unit.unit_id, translated_text=f"[离线演示] {request.unit.source_text}")
        return TranslationResult(request_id=request.request_id, unit_id=request.unit.unit_id, draft=draft, finish="complete", provider_response_id=None, model=request.model, usage=Usage(input_tokens=0, output_tokens=0, cost_usd=0.0, usage_source="estimated"))


class OpenAIProvider:
    def __init__(self, api_key_env: str = "OPENAI_API_KEY", base_url: str | None = None, strict_schema: bool = True):
        self.api_key_env = api_key_env
        self.base_url = base_url
        self.strict_schema = strict_schema

    def translate(self, request: TranslationRequest) -> TranslationResult:
        from openai import OpenAI

        api_key = os.environ.get(self.api_key_env)
        if not api_key:
            raise RuntimeError(f"missing API key environment variable: {self.api_key_env}")
        schema = TranslationDraft.model_json_schema()
        context = "\n\n".join(f"[{item.category}] {item.content}" for item in request.context.items)
        prompt = f"Unit ID: {request.unit.unit_id}\nContext:\n{context}\n\nSource:\n{request.unit.source_text}"
        client_options = {"api_key": api_key}
        if self.base_url:
            client_options["base_url"] = self.base_url
        client = OpenAI(**client_options)
        response_format = {"type": "json_schema", "name": "translation_draft", "schema": schema}
        if self.strict_schema:
            response_format["strict"] = True
        arguments = {
            "model": request.model.model_id,
            "instructions": "Translate the source into faithful, natural Simplified Chinese. Preserve every ⟦P:...⟧ marker, number, and URL exactly. Return only the requested JSON schema.",
            "input": prompt,
            "max_output_tokens": request.model.max_output_tokens,
            "store": False,
            "text": {"format": response_format},
        }
        if request.model.reasoning_effort:
            arguments["reasoning"] = {"effort": request.model.reasoning_effort}
        response = client.responses.create(**arguments)
        draft = TranslationDraft.model_validate(json.loads(response.output_text))
        usage_obj = response.usage
        details = getattr(usage_obj, "output_tokens_details", None)
        usage = Usage(input_tokens=getattr(usage_obj, "input_tokens", None), cached_input_tokens=getattr(getattr(usage_obj, "input_tokens_details", None), "cached_tokens", None), output_tokens=getattr(usage_obj, "output_tokens", None), reasoning_tokens=getattr(details, "reasoning_tokens", None), cost_usd=None, usage_source="provider")
        return TranslationResult(request_id=request.request_id, unit_id=request.unit.unit_id, draft=draft, finish="complete", provider_response_id=response.id, model=request.model, usage=usage)


class DeepSeekProvider(OpenAIProvider):
    def __init__(self, api_key_env: str = "DEEPSEEK_API_KEY", base_url: str = "https://api.deepseek.com"):
        # DeepSeek documents type/name/schema for Responses structured output,
        # but does not document OpenAI's additional `strict` flag.
        super().__init__(api_key_env=api_key_env, base_url=base_url, strict_schema=False)
