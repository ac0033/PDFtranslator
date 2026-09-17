from __future__ import annotations

import json
import os
import re
from typing import Protocol

from pydantic import ValidationError

from .schemas import TranslationDraft, TranslationRequest, TranslationResult, Usage
from .semantic_layout import rebalance_layout_markers
from .term_lock import protect_terms, restore_terms


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
        source_text,term_locks=protect_terms(request.unit.source_text,request.context.items)
        schema = TranslationDraft.model_json_schema()
        context = "\n\n".join(f"<context-item category=\"{item.category}\">{item.content}</context-item>" for item in request.context.items)
        protected_literals = json.dumps(request.unit.protected_literal_hints, ensure_ascii=False, sort_keys=True)
        prompt = (
            f"<unit-id>{request.unit.unit_id}</unit-id>\n"
            f"<reference-context>\n{context}\n</reference-context>\n"
            f"<protected-source-literals>\n{protected_literals}\n</protected-source-literals>\n"
            f"<source-to-translate>\n{source_text}\n</source-to-translate>"
        )
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
                "Translate only the text inside <source-to-translate>. The content inside "
                "<reference-context> is reference material only: never translate, summarize, "
                "quote, continue, or include it in translated_text. "
                "<protected-source-literals> maps exact protected markers to readable formula "
                "or footnote text from the source PDF when uniquely recoverable. Use it only "
                "to understand the source; preserve each marker exactly and do not copy its "
                "literal outside the marker. An empty map means no literal was verified. "
                "Even when the source is a "
                "short heading, footer, fragment, or metadata line, return only its translation. "
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
        if "native_immutable_slots" in request.unit.risk_tags:
            readable = source_text
            for marker, literal in request.unit.protected_literal_hints.items():
                readable = readable.replace(marker, literal)
            arguments["input"] += "\n<complete-readable-source>\n" + readable + "\n</complete-readable-source>"
            arguments["instructions"] += (
                " First read complete-readable-source: markers can contain a sentence-ending period "
                "and the NEXT sentence's subject together. Do not mistake that boundary for broken grammar. "
                "The readable source is an exact expansion of the same source, not additional material. "
                " This source uses immutable native PDF objects. Every non-layout marker is an "
                "immovable original object, including numbers, punctuation and complete formulas. "
                "Use the supplied literal map to understand the exact relationships and sentence boundaries. "
                "Keep ALL markers in exactly the same order. Translate the prose between them with its "
                "original semantic scope, rephrasing naturally without moving any claim across a marker. "
                "Do not duplicate numbers, punctuation, words or formulas already inside a marker. "
                "Do not insert prose before a leading marker or after a trailing marker. "
                "Match original ASCII parentheses if their partners occur inside markers. "
                "Translate whole sentences coherently, including across physical page or line breaks. "
                "Do not omit content for brevity. Return only the requested JSON."
            )
        if "native_source_assets" in request.unit.risk_tags:
            readable = source_text
            for marker, literal in request.unit.protected_literal_hints.items():
                readable = readable.replace(marker, literal)
            arguments["input"] += "\n<complete-readable-source>" + readable + "</complete-readable-source>"
            arguments["instructions"] += (
                " Native asset markers preserve source formulas or numbers exactly. Their literal map and "
                "complete-readable-source expose the full sentence meaning. Translate coherently as one paragraph; "
                "you may place Chinese before, between or after markers to express correct Chinese grammar. "
                "Never repeat a protected formula or number as prose. Preserve markers exactly once. "
                "Retain Latin variable names in the English prose such as i in i-th. "
                "For 'the error at a = W', express the ERROR AT a equals W, not that a itself equals W. "
                "Match original ASCII parentheses if a closing parenthesis is inside a protected formula."
            )
        native_parts = None
        if "native_immutable_slots" in request.unit.risk_tags:
            native_parts = re.split(r'(⟦P:[^⟧]+⟧)', source_text)
            editable = {str(i): part.strip() for i, part in enumerate(native_parts) if i % 2 == 0 and part.strip()}
            segment_schema = {"type": "object", "properties": {key: {"type": "string"} for key in editable},
                              "required": list(editable), "additionalProperties": False}
            arguments["text"]["format"]["schema"] = segment_schema
            arguments["input"] += "\n<editable-segments>" + json.dumps(editable, ensure_ascii=False) + "</editable-segments>"
            sequence = [{"editable_id": str(i), "English": part.strip()} if i % 2 == 0 else
                        {"fixed_original": request.unit.protected_literal_hints.get(part, part)}
                        for i, part in enumerate(native_parts) if part.strip()]
            arguments["input"] += "\n<ordered-render-sequence>" + json.dumps(sequence, ensure_ascii=False) + "</ordered-render-sequence>"
            arguments["instructions"] = (
                "Translate faithful complete natural Simplified Chinese, preserving the author's meaning. "
                "complete-readable-source is the whole original paragraph with formulas expanded. "
                "reference-context is for understanding only, not additional text to translate. "
                "Some protected markers contain a full stop and the next sentence's subject. "
                " RESPONSE FORMAT: return a JSON object mapping EACH editable-segments ID to its Chinese translation. "
                "Do not return unit_id, translated_text, uncertainties, or any protected markers. "
                "Read the whole paragraph and neighbors together before translating these segments. "
                "The program interleaves translated segments with the unchanged original objects. "
                "Preserve each segment's meaning in its assigned position; use natural Chinese phrasing suited to that order. "
                "The ordered-render-sequence explicitly shows the fixed original objects between editable IDs. "
                "Do NOT reproduce any fixed_original number, formula, variable, or punctuation inside your translations. "
                "Translate ONLY the English attached to each editable_id, using the rest to resolve meaning. "
                "Do not move the meaning of one editable_id into another ID. No omission or summaries. All listed IDs are required."
            )
        arguments["instructions"] += (
            " Translate cited paper and book titles as complete titles, including any terminology markers "
            "inside them; never leave an English title with only a technical phrase replaced by Chinese. "
            "Preserve author names, acronyms and citation markers. Do not invent or omit title content. "
            "Before returning, silently read the entire Chinese translation with every terminology marker "
            "expanded to its target_term. Ensure each sentence has a complete predicate and object, and "
            "complete any Chinese grammatical construction around a noun-phrase marker without changing "
            "the claim. A marker is a noun phrase, not an entire clause. Do not insert artificial spaces "
            "between Chinese words and Chinese terminology markers. Keep heading/body boundaries clear."
        )
        if term_locks:
            arguments["input"] += "\n<locked-terminology>"+json.dumps([t.model_dump() for t in term_locks],ensure_ascii=False)+"</locked-terminology>"
            arguments["instructions"] += " Preserve every ⟦T:...⟧ terminology marker exactly once at its semantic position. It represents a complete approved technical term. Do not translate, expand, paraphrase or duplicate it. The program inserts its exact target_term after translation. Use source_term and target_term only to construct grammatical surrounding Chinese."
        if request.model.reasoning_effort:
            arguments["reasoning"] = {"effort": request.model.reasoning_effort}
        response = client.responses.create(**arguments)
        usage = _response_usage(response)
        if getattr(response, "status", None) == "incomplete":
            details = getattr(response, "incomplete_details", None)
            reason = getattr(details, "reason", None) or "unknown"
            raise ProviderCallError(
                f"provider response was incomplete: {reason}",
                category=f"truncated:{reason}",
                usage=usage,
                provider_response_id=getattr(response, "id", None),
                retryable=reason == "max_output_tokens",
            )
        term_evidence=None
        try:
            parsed = json.loads(response.output_text)
            if native_parts is not None:
                if not isinstance(parsed, dict) or set(parsed) != set(editable) or any(not isinstance(v, str) or not v.strip() for v in parsed.values()):
                    raise ValueError("native segment IDs or translations missing")
                if any("⟦P:" in v for v in parsed.values()):
                    raise ValueError("native prose response inserted a protected marker")
                assembled = "".join(parsed[str(i)] if str(i) in editable else part for i, part in enumerate(native_parts))
                draft = TranslationDraft(unit_id=request.unit.unit_id, translated_text=assembled)
            else:
                draft = TranslationDraft.model_validate(parsed)
            if term_locks:
                restored,term_evidence=restore_terms(draft.translated_text,term_locks)
                draft=draft.model_copy(update={"translated_text":restored})
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            raise ProviderCallError(
                "provider response did not match TranslationDraft",
                category="invalid_structured_output",
                usage=usage,
                provider_response_id=getattr(response, "id", None),
            ) from exc
        return TranslationResult(request_id=request.request_id, unit_id=request.unit.unit_id, draft=draft, finish="complete", provider_response_id=response.id, model=request.model, usage=usage, terminology_evidence=term_evidence)


class DeepSeekProvider(OpenAIProvider):
    def __init__(self, api_key_env: str = "DEEPSEEK_API_KEY", base_url: str = "https://api.deepseek.com"):
        # DeepSeek documents type/name/schema for Responses structured output,
        # but does not document OpenAI's additional `strict` flag.
        super().__init__(api_key_env=api_key_env, base_url=base_url, strict_schema=False)
