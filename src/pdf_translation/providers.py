from __future__ import annotations

import json
import os
from typing import Protocol

from pydantic import ValidationError

from .schemas import TranslationDraft, TranslationRequest, TranslationResult, Usage
from .semantic_layout import rebalance_layout_markers


class Provider(Protocol):
    def translate(self, request: TranslationRequest) -> TranslationResult: ...


class ProviderCallError(RuntimeError):
    """A provider returned a billable response that could not become a result."""

    def __init__(
        self,
        message: str,
        *,
        category: str,
        usage: Usage,
        provider_response_id: str | None,
        retryable: bool = True,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.usage = usage
        self.provider_response_id = provider_response_id
        self.retryable = retryable


def _response_usage(response) -> Usage:
    usage_obj = response.usage
    details = getattr(usage_obj, "output_tokens_details", None)
    return Usage(
        input_tokens=getattr(usage_obj, "input_tokens", None),
        cached_input_tokens=getattr(getattr(usage_obj, "input_tokens_details", None), "cached_tokens", None),
        output_tokens=getattr(usage_obj, "output_tokens", None),
        reasoning_tokens=getattr(details, "reasoning_tokens", None),
        cost_usd=None,
        usage_source="provider",
    )


class MockProvider:
    def translate(self, request: TranslationRequest) -> TranslationResult:
        # Deliberately preserves every source token for deterministic offline contract tests.
        coherent_source = rebalance_layout_markers(request.unit.source_text)
        draft = TranslationDraft(unit_id=request.unit.unit_id, translated_text=f"[离线演示] {coherent_source}")
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
            "instructions": (
                "Translate the complete source into faithful, coherent, natural Simplified Chinese. "
                "Preserve every ⟦P:...⟧ marker, number, and URL exactly. Markers containing "
                "'P:layout-' identify original layout slots: keep them exactly once and in their "
                "original order, but move them to natural Chinese sentence boundaries when the "
                "source layout split occurs inside a sentence; never leave a dangling word or "
                "clause in the preceding slot. Keep all other protected markers at their semantic "
                "positions. Return one JSON object instance with exactly this shape: "
                "{\"unit_id\": \"the exact Unit ID\", \"translated_text\": \"the translation\", "
                "\"uncertainties\": []}. Do not return or describe the JSON Schema definition."
            ),
            "input": prompt,
            "max_output_tokens": request.model.max_output_tokens,
            "store": False,
            "text": {"format": response_format},
        }
        if request.model.reasoning_effort:
            arguments["reasoning"] = {"effort": request.model.reasoning_effort}
        response = client.responses.create(**arguments)
        usage = _response_usage(response)
        try:
            draft = TranslationDraft.model_validate(json.loads(response.output_text))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise ProviderCallError(
                "provider response did not match TranslationDraft",
                category="invalid_structured_output",
                usage=usage,
                provider_response_id=getattr(response, "id", None),
            ) from exc
        return TranslationResult(request_id=request.request_id, unit_id=request.unit.unit_id, draft=draft, finish="complete", provider_response_id=response.id, model=request.model, usage=usage)


class DeepSeekProvider(OpenAIProvider):
    def __init__(self, api_key_env: str = "DEEPSEEK_API_KEY", base_url: str = "https://api.deepseek.com"):
        # DeepSeek documents type/name/schema for Responses structured output,
        # but does not document OpenAI's additional `strict` flag.
        super().__init__(api_key_env=api_key_env, base_url=base_url, strict_schema=False)
