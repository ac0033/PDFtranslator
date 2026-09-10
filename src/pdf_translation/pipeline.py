from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from .budget import estimate_cost, reserve_budget
from .config import RunConfig
from .hashing import stable_hash
from .providers import DeepSeekProvider, MockProvider, OpenAIProvider
from .quality import check_quality
from .schemas import ContextItem, ContextPacket, ModelSpec, TranslationRequest, TranslationUnit, Usage
from .storage import StateStore
from .validation import validate_request, validate_result


def estimated_input_token_ceiling(request: TranslationRequest) -> int:
    """Conservative byte-based ceiling until the selected model counter is wired."""
    context_bytes = sum(len(item.content.encode("utf-8")) for item in request.context.items)
    return len(request.unit.source_text.encode("utf-8")) + context_bytes + 512


def make_request(unit: TranslationUnit, config: RunConfig) -> TranslationRequest:
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
    for unit in units:
        request = make_request(unit, config)
        validate_request(request)
        store.prepare(config.run_id, unit.unit_id, request.cache_key)
        if store.state(config.run_id, unit.unit_id) in {"accepted", "blocked", "needs_review"}:
            continue
        cached = store.cached(request.cache_key)
        if cached:
            result = cached.model_copy(update={"request_id": request.request_id})
            result = result.model_copy(update={"usage": Usage(input_tokens=0, output_tokens=0, cost_usd=0.0, usage_source="estimated")})
            from_cache = True
        else:
            estimated_input = estimated_input_token_ceiling(request)
            estimate = estimate_cost(estimated_input, config.max_output_tokens, config.input_usd_per_million, config.output_usd_per_million)
            if config.provider != "mock":
                reserve_budget(store.spent(config.run_id), 0.0, estimate, config.budget_usd)
            result = provider.translate(request)
            if result.usage.cost_usd is None:
                actual = estimate_cost(result.usage.input_tokens or estimated_input, result.usage.output_tokens or 0, config.input_usd_per_million, config.output_usd_per_million)
                result = result.model_copy(update={"usage": result.usage.model_copy(update={"cost_usd": actual, "usage_source": "estimated" if actual is not None else "unavailable"})})
            from_cache = False
        validate_result(request, result)
        report = check_quality(unit, result)
        store.commit(config.run_id, result, report, request.cache_key, cached=from_cache)
    return store.summary(config.run_id)
