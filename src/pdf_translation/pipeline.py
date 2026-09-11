from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

from .budget import estimate_cost, reserve_budget
from .config import RunConfig
from .context_router import build_context_packet, select_exact_tm
from .context_store import ContextStore
from .analyzer import validate_semantic_evidence
from .hashing import stable_hash
from .providers import DeepSeekProvider, MockProvider, OpenAIProvider, ProviderCallError
from .quality import check_quality, repair_boundary_markers
from .semantic_layout import restore_and_rebalance_layout_markers
from .schemas import ContextItem, ContextPacket, ModelSpec, TranslationDraft, TranslationRequest, TranslationResult, TranslationUnit, Usage
from .storage import StateStore
from .validation import validate_request, validate_result


class ProviderAttemptsExhausted(RuntimeError):
    pass


def _priced_usage(usage: Usage, config: RunConfig, estimated_input: int, estimated_output: int = 0) -> Usage:
    if usage.cost_usd is not None:
        return usage
    actual = estimate_cost(
        usage.input_tokens if usage.input_tokens is not None else estimated_input,
        usage.output_tokens if usage.output_tokens is not None else estimated_output,
        config.input_usd_per_million,
        config.output_usd_per_million,
    )
    return usage.model_copy(
        update={
            "cost_usd": actual,
            "usage_source": "estimated" if actual is not None else "unavailable",
        }
    )


def estimated_input_token_ceiling(request: TranslationRequest) -> int:
    """Conservative byte-based ceiling until the selected model counter is wired."""
    context_bytes = sum(len(item.content.encode("utf-8")) for item in request.context.items)
    return len(request.unit.source_text.encode("utf-8")) + context_bytes + 512


def estimate_units(units: list[TranslationUnit], config: RunConfig) -> dict:
    """Return a conservative, zero-call estimate for translatable units."""
    billable_units = [unit for unit in units if unit.unit_type not in {"code", "protected"}]
    if config.context_engine == "v1":
        if not config.context_db:
            raise ValueError("V1 context_engine requires context_db")
        context_store = ContextStore(Path(config.context_db))
        glossary = context_store.glossary()
        styles = context_store.styles()
        semantics = context_store.semantics()
        units_by_id = {unit.unit_id: unit for unit in units}
        validate_semantic_evidence(semantics, set(units_by_id))
        glossary_snapshot_id = stable_hash([entry for entry in glossary if entry.status == "approved"])
        style_snapshot_id = stable_hash([rule for rule in styles if rule.status == "approved"])
        requests = []
        for unit in billable_units:
            packet, _ = build_context_packet(
                unit,
                glossary,
                styles,
                semantics,
                units_by_id,
                token_budget=config.context_token_budget,
                glossary_snapshot_id=glossary_snapshot_id,
                style_snapshot_id=style_snapshot_id,
            )
            requests.append(make_request(unit, config, packet))
    else:
        requests = [make_request(unit, config) for unit in billable_units]
    input_tokens = sum(estimated_input_token_ceiling(request) for request in requests)
    output_tokens = len(requests) * config.max_output_tokens
    return {
        "unit_count": len(units),
        "billable_unit_count": len(billable_units),
        "protected_passthrough_count": len(units) - len(billable_units),
        "estimated_input_tokens_ceiling": input_tokens,
        "reserved_output_tokens": output_tokens,
        "estimated_max_cost_usd": estimate_cost(
            input_tokens,
            output_tokens,
            config.input_usd_per_million,
            config.output_usd_per_million,
        ),
        "provider": config.provider,
        "model_id": config.model_id,
        "is_estimate": True,
        "api_calls": 0,
    }


def make_request(unit: TranslationUnit, config: RunConfig, packet: ContextPacket | None = None) -> TranslationRequest:
    if packet is None:
        item = ContextItem(item_id="style:v0", category="style", content="忠实保留否定、量词、条件和概率程度；采用自然教材中文；不扩写。", source_ids=(), revision=config.style_revision, required=True, selection_reason="V0 fixed style")
        packet_data = {"unit_id": unit.unit_id, "items": (item,)}
        packet = ContextPacket(unit_id=unit.unit_id, router_revision="v0-fixed", memory_snapshot_id=config.style_revision, items=(item,), token_count=42, token_counter_id="fixed-estimate-v0", token_count_is_estimate=True, token_budget=config.context_token_budget, packet_hash=stable_hash(packet_data))
    model = ModelSpec(provider=config.provider, model_id=config.model_id, reasoning_effort=config.reasoning_effort, max_output_tokens=config.max_output_tokens, prompt_revision=config.prompt_revision, adapter_revision=config.adapter_revision, pricing_revision=config.pricing_revision)
    cache_key = stable_hash({"unit": unit, "context": packet, "model": model})
    return TranslationRequest(request_id=str(uuid4()), unit=unit, context=packet, model=model, cache_key=cache_key)


def run_units(units: list[TranslationUnit], config: RunConfig, runs_root: Path) -> dict:
    run_dir = runs_root / config.run_id
    store = StateStore(run_dir / "state.sqlite")
    input_hash = stable_hash(units)
    store.begin_run(config.run_id, input_hash, config.config_hash)
    if config.provider == "mock":
        provider = MockProvider()
    elif config.provider == "openai":
        provider = OpenAIProvider(api_key_env=config.api_key_env or "OPENAI_API_KEY", base_url=config.base_url)
    elif config.provider == "deepseek":
        if config.model_id not in {"deepseek-flash", "deepseek-v4-flash"}:
            raise ValueError("DeepSeek V4.1 Flash API model_id must be deepseek-flash (recommended) or deepseek-v4-flash (legacy alias)")
        provider = DeepSeekProvider(api_key_env=config.api_key_env or "DEEPSEEK_API_KEY", base_url=config.base_url or "https://api.deepseek.com")
    else:
        provider = None
    if provider is None:
        raise ValueError(f"unsupported provider: {config.provider}")
    api_key_env = getattr(provider, "api_key_env", None)
    if config.provider != "mock" and api_key_env and not os.environ.get(api_key_env):
        raise RuntimeError(f"missing API key environment variable: {api_key_env}")
    context_store = None
    glossary = []
    styles = []
    semantics = []
    glossary_snapshot_id = "glossary-empty"
    style_snapshot_id = "style-empty"
    units_by_id = {unit.unit_id: unit for unit in units}
    if config.context_engine == "v1":
        if not config.context_db:
            raise ValueError("V1 context_engine requires context_db")
        context_store = ContextStore(Path(config.context_db))
        glossary = context_store.glossary()
        styles = context_store.styles()
        semantics = context_store.semantics()
        validate_semantic_evidence(semantics, set(units_by_id))
        glossary_snapshot_id = stable_hash([entry for entry in glossary if entry.status == "approved"])
        style_snapshot_id = stable_hash([rule for rule in styles if rule.status == "approved"])
    for unit in units:
        ledger_source = None
        provider_usage_recorded = False
        tm_entry = None
        if context_store:
            base_packet, _ = build_context_packet(unit, glossary, styles, semantics, units_by_id, token_budget=config.context_token_budget, glossary_snapshot_id=glossary_snapshot_id, style_snapshot_id=style_snapshot_id)
            tm_entry = select_exact_tm(unit, context_store.exact_tm(unit.source_hash), base_packet.packet_hash, glossary_snapshot_id, style_snapshot_id)
            packet, _ = build_context_packet(unit, glossary, styles, semantics, units_by_id, token_budget=config.context_token_budget, glossary_snapshot_id=glossary_snapshot_id, style_snapshot_id=style_snapshot_id, tm_entry=tm_entry)
            context_store.replace_dependencies(f"unit:{unit.unit_id}", [item.item_id for item in packet.items])
            request = make_request(unit, config, packet)
        else:
            request = make_request(unit, config)
        validate_request(request)
        store.prepare(config.run_id, unit.unit_id, request.cache_key)
        state = store.state(config.run_id, unit.unit_id)
        if state == "accepted":
            previous = store.result(config.run_id, unit.unit_id)
            if previous is not None and previous.draft is not None:
                repaired_text, layout_repaired = restore_and_rebalance_layout_markers(
                    unit.source_text, previous.draft.translated_text
                )
                if layout_repaired:
                    repaired_result = previous.model_copy(
                        update={
                            "request_id": request.request_id,
                            "draft": previous.draft.model_copy(update={"translated_text": repaired_text}),
                        }
                    )
                    validate_result(request, repaired_result)
                    repaired_report = check_quality(unit, repaired_result)
                    store.commit(
                        config.run_id,
                        repaired_result,
                        repaired_report,
                        request.cache_key,
                        cached=True,
                        source="deterministic_layout_repair",
                    )
            continue
        if state in {"blocked", "needs_review"}:
            previous = store.result(config.run_id, unit.unit_id)
            if previous is not None and previous.draft is not None:
                repaired_text, repaired = repair_boundary_markers(unit.source_text, previous.draft.translated_text)
                repaired_text, layout_repaired = restore_and_rebalance_layout_markers(unit.source_text, repaired_text)
                repaired = repaired or layout_repaired
                if repaired:
                    repaired_draft = previous.draft.model_copy(update={"translated_text": repaired_text})
                    repaired_result = previous.model_copy(update={"request_id": request.request_id, "draft": repaired_draft})
                    validate_result(request, repaired_result)
                    repaired_report = check_quality(unit, repaired_result)
                    store.commit(
                        config.run_id,
                        repaired_result,
                        repaired_report,
                        request.cache_key,
                        cached=True,
                        source="deterministic_layout_repair" if layout_repaired else "deterministic_boundary_repair",
                    )
                    if repaired_report.status == "pass":
                        continue
            continue
        cached = store.cached(request.cache_key)
        if cached:
            result = cached.model_copy(update={"request_id": request.request_id})
            result = result.model_copy(update={"usage": Usage(input_tokens=0, output_tokens=0, cost_usd=0.0, usage_source="estimated")})
            from_cache = True
        elif tm_entry:
            result = TranslationResult(request_id=request.request_id, unit_id=unit.unit_id, draft=TranslationDraft(unit_id=unit.unit_id, translated_text=tm_entry.target_text), finish="complete", provider_response_id=None, model=request.model, usage=Usage(input_tokens=0, output_tokens=0, cost_usd=0.0, usage_source="estimated"))
            from_cache = True
        elif unit.unit_type in {"code", "protected"}:
            result = TranslationResult(
                request_id=request.request_id,
                unit_id=unit.unit_id,
                draft=TranslationDraft(unit_id=unit.unit_id, translated_text=unit.source_text),
                finish="complete",
                provider_response_id=None,
                model=request.model,
                usage=Usage(input_tokens=0, output_tokens=0, cost_usd=0.0, usage_source="estimated"),
            )
            from_cache = False
            ledger_source = "protected_passthrough"
        else:
            estimated_input = estimated_input_token_ceiling(request)
            estimate = estimate_cost(estimated_input, config.max_output_tokens, config.input_usd_per_million, config.output_usd_per_million)
            if config.provider == "mock":
                result = provider.translate(request)
            else:
                prior_attempts = store.provider_attempt_count(config.run_id, unit.unit_id)
                if prior_attempts >= config.provider_max_attempts:
                    raise ProviderAttemptsExhausted(
                        f"provider attempt limit already reached for {unit.unit_id}: {prior_attempts}/{config.provider_max_attempts}"
                    )
                result = None
                for attempt_no in range(prior_attempts + 1, config.provider_max_attempts + 1):
                    reserve_budget(store.spent(config.run_id), 0.0, estimate, config.budget_usd)
                    try:
                        candidate = provider.translate(request)
                    except ProviderCallError as exc:
                        failed_usage = _priced_usage(exc.usage, config, estimated_input)
                        store.record_provider_attempt(
                            config.run_id,
                            unit.unit_id,
                            request.request_id,
                            attempt_no,
                            "invalid_response",
                            failed_usage,
                            error_category=exc.category,
                            provider_response_id=exc.provider_response_id,
                        )
                        if not exc.retryable or attempt_no >= config.provider_max_attempts:
                            raise ProviderAttemptsExhausted(
                                f"provider attempts exhausted for {unit.unit_id}: {exc.category}"
                            ) from exc
                        continue
                    except Exception as exc:
                        # The provider may have accepted a request without returning
                        # usage. Charge the reserved ceiling so retries remain bounded.
                        failed_usage = Usage(
                            input_tokens=estimated_input,
                            output_tokens=config.max_output_tokens,
                            cost_usd=estimate,
                            usage_source="estimated" if estimate is not None else "unavailable",
                        )
                        store.record_provider_attempt(
                            config.run_id,
                            unit.unit_id,
                            request.request_id,
                            attempt_no,
                            "error",
                            failed_usage,
                            error_category=type(exc).__name__,
                        )
                        raise ProviderAttemptsExhausted(
                            f"non-retryable provider error for {unit.unit_id}: {type(exc).__name__}"
                        ) from exc
                    candidate = candidate.model_copy(update={"usage": _priced_usage(candidate.usage, config, estimated_input)})
                    if candidate.draft is not None:
                        repaired_text, _ = repair_boundary_markers(unit.source_text, candidate.draft.translated_text)
                        repaired_text, _ = restore_and_rebalance_layout_markers(unit.source_text, repaired_text)
                        candidate = candidate.model_copy(update={"draft": candidate.draft.model_copy(update={"translated_text": repaired_text})})
                    try:
                        validate_result(request, candidate)
                        candidate_report = check_quality(unit, candidate)
                    except ValueError as exc:
                        error_category = "result_validation"
                    else:
                        critical_categories = sorted(issue.category for issue in candidate_report.issues if issue.severity == "critical")
                        error_category = "quality:" + ",".join(critical_categories) if critical_categories else None
                    if error_category:
                        store.record_provider_attempt(
                            config.run_id,
                            unit.unit_id,
                            request.request_id,
                            attempt_no,
                            "invalid_response",
                            candidate.usage,
                            error_category=error_category,
                            provider_response_id=candidate.provider_response_id,
                        )
                        if attempt_no >= config.provider_max_attempts:
                            raise ProviderAttemptsExhausted(
                                f"provider attempts exhausted for {unit.unit_id}: {error_category}"
                            )
                        continue
                    store.record_provider_attempt(
                        config.run_id,
                        unit.unit_id,
                        request.request_id,
                        attempt_no,
                        "success",
                        candidate.usage,
                        provider_response_id=candidate.provider_response_id,
                    )
                    result = candidate
                    provider_usage_recorded = True
                    break
                if result is None:
                    raise ProviderAttemptsExhausted(f"provider attempts exhausted for {unit.unit_id}")
            from_cache = False
        if result.draft is not None:
            repaired_text, repaired = repair_boundary_markers(unit.source_text, result.draft.translated_text)
            repaired_text, _ = restore_and_rebalance_layout_markers(unit.source_text, repaired_text)
            if repaired:
                result = result.model_copy(update={"draft": result.draft.model_copy(update={"translated_text": repaired_text})})
            elif repaired_text != result.draft.translated_text:
                result = result.model_copy(update={"draft": result.draft.model_copy(update={"translated_text": repaired_text})})
        validate_result(request, result)
        report = check_quality(unit, result)
        store.commit(
            config.run_id,
            result,
            report,
            request.cache_key,
            cached=from_cache,
            source=ledger_source,
            record_usage=not provider_usage_recorded,
        )
    return store.summary(config.run_id)
