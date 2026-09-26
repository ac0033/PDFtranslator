from __future__ import annotations

import os
import math
import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from uuid import uuid4

from openai import APIStatusError

from .budget import BudgetExceeded, estimate_cost, reserve_budget
from .config import RunConfig
from .context_router import build_context_packet, select_exact_tm
from .context_store import ContextStore
from .analyzer import validate_semantic_evidence
from .hashing import stable_hash
from .providers import DeepSeekProvider, MockProvider, OpenAIProvider, ProviderCallError
from .quality import check_quality, repair_boundary_markers
from .semantic_layout import restore_and_rebalance_layout_markers, is_formula_only
from .schemas import ContextItem, ContextPacket, ModelSpec, TranslationDraft, TranslationRequest, TranslationResult, TranslationUnit, Usage
from .storage import StateStore
from .validation import validate_request, validate_result


class ProviderAttemptsExhausted(RuntimeError):
    def __init__(self, message: str, unit_id: str | None = None):
        super().__init__(message)
        self.unit_id = unit_id


class ProviderUnavailable(RuntimeError):
    """Account/service failure: stop the book, not the next hundred units."""


DEFAULT_TRANSLATION_STYLE = "忠实保留否定、量词、条件和概率程度；采用自然教材中文；不扩写。"
SOURCE_LITERAL_PROMPT_REVISION = "source-literals-v1+term-lock-v2"


def output_token_limit(unit: TranslationUnit, config: RunConfig) -> int:
    """Return the frozen per-unit response ceiling for the configured policy."""
    if config.output_token_policy == "fixed":
        return config.max_output_tokens
    adaptive = max(384, math.ceil(len(unit.source_text) * 0.75) + 128)
    return min(config.max_output_tokens, adaptive)


def expanded_output_token_limit(current: int, configured_maximum: int) -> int:
    """Double a truncated ceiling while respecting the configured hard maximum."""
    return min(configured_maximum, max(current + 128, current * 2))


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
    literal_bytes = len(json.dumps(request.unit.protected_literal_hints, ensure_ascii=False).encode("utf-8"))
    return len(request.unit.source_text.encode("utf-8")) + context_bytes + literal_bytes + 512


def estimate_units(units: list[TranslationUnit], config: RunConfig, *, context_units: list[TranslationUnit] | None = None) -> dict:
    """Return a conservative, zero-call estimate for translatable units."""
    billable_units = [unit for unit in units if unit.unit_type not in {"code", "protected"} and not is_formula_only(unit.source_text)]
    if config.context_engine == "v1":
        if not config.context_db:
            raise ValueError("V1 context_engine requires context_db")
        context_store = ContextStore(Path(config.context_db))
        glossary = context_store.glossary()
        styles = context_store.styles()
        semantics = context_store.semantics()
        units_by_id = {unit.unit_id: unit for unit in [*(context_units or []), *units]}
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
    output_tokens = sum(request.model.max_output_tokens for request in requests)
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


def make_request(unit: TranslationUnit, config: RunConfig, packet: ContextPacket | None = None, max_output_tokens_override: int | None = None) -> TranslationRequest:
    if packet is None:
        item = ContextItem(item_id="style:v0", category="style", content=DEFAULT_TRANSLATION_STYLE, source_ids=(), revision=config.style_revision, required=True, selection_reason="V0 fixed style")
        packet_data = {"unit_id": unit.unit_id, "items": (item,)}
        packet = ContextPacket(unit_id=unit.unit_id, router_revision="v0-fixed", memory_snapshot_id=config.style_revision, items=(item,), token_count=42, token_counter_id="fixed-estimate-v0", token_count_is_estimate=True, token_budget=config.context_token_budget, packet_hash=stable_hash(packet_data))
    max_output_tokens = max_output_tokens_override or output_token_limit(unit, config)
    if max_output_tokens > config.max_output_tokens:
        raise ValueError("per-unit max_output_tokens exceeds configured maximum")
    model = ModelSpec(provider=config.provider, model_id=config.model_id, reasoning_effort=config.reasoning_effort, max_output_tokens=max_output_tokens, prompt_revision=f"{config.prompt_revision}+{SOURCE_LITERAL_PROMPT_REVISION}", adapter_revision=config.adapter_revision, pricing_revision=config.pricing_revision)
    cache_key = stable_hash({"unit": unit, "context": packet, "model": model})
    return TranslationRequest(request_id=str(uuid4()), unit=unit, context=packet, model=model, cache_key=cache_key)


_RETRY_FEEDBACK_GUIDANCE = {
    "layout_boundary": (
        "Each ⟦P:layout-...⟧ marker belongs to its own source fragment: place every marker immediately "
        "before the Chinese translation of its own fragment, keep the original marker order, and use each "
        "marker exactly once; never cluster all layout markers together. The text between two consecutive "
        "layout markers must be a complete chunk ending with sentence-final punctuation (one of . ! ? 。 ！ ？ ； ; ： :) "
        "or be empty; for a list of short items, end each item with sentence-final punctuation."
    ),
    "layout_marker_order": "Layout markers must appear in exactly the original order; do not reorder them.",
    "protected_marker": (
        "The translation must contain exactly the same ⟦P:...⟧ markers as the source, each the same number "
        "of times; do not drop, add, merge, or alter any marker."
    ),
    "protected_marker_order": "Keep every protected marker at its semantic position and in the original order.",
    "incomplete": "Return a complete translation of the whole source; never leave translated_text empty or partial.",
    "terminology": "Use the required approved target terms from the glossary exactly; never use forbidden variants.",
}


def retry_feedback_request(request: TranslationRequest, candidate: TranslationResult, error_category: str, attempt_no: int, config: RunConfig) -> TranslationRequest:
    """Rebuild a rejected request with explicit correction feedback for the next attempt."""
    rejected = candidate.draft.translated_text if candidate.draft else ""
    categories = error_category.removeprefix("quality:").split(",") if error_category.startswith("quality:") else [error_category]
    bullets = [f"- {_RETRY_FEEDBACK_GUIDANCE.get(category, 'Return one JSON object with unit_id, translated_text and uncertainties, translating the complete source.') }" for category in categories]
    content = (
        f"Correction feedback: a previous translation attempt for this exact unit was rejected by automated "
        f"checks ({error_category}). Rejected output: {rejected!r}\n"
        "Fix every listed problem and return a corrected, complete translation of the same source:\n"
        + "\n".join(bullets)
    )
    item = ContextItem(
        item_id=f"retry-feedback-{attempt_no}",
        category="style",
        content=content,
        source_ids=(),
        revision=config.prompt_revision,
        required=True,
        selection_reason=f"correction feedback after rejected attempt {attempt_no}",
    )
    items = (*request.context.items, item)
    packet = request.context.model_copy(update={
        "items": items,
        "token_count": request.context.token_count + max(1, len(content) // 4),
        "token_count_is_estimate": True,
        "packet_hash": stable_hash({"unit_id": request.context.unit_id, "items": items}),
    })
    return TranslationRequest(
        request_id=str(uuid4()),
        unit=request.unit,
        context=packet,
        model=request.model,
        cache_key=stable_hash({"unit": request.unit, "context": packet, "model": request.model}),
    )


class _RunStopped(RuntimeError):
    """A queued unit was not sent because another unit already stopped the run."""


class _SerializedStore:
    """One StateStore shared by provider threads, one call at a time.

    Each call opens its own SQLite connection, so serializing calls is enough
    to keep concurrent writers from tripping over the database lock.
    """

    def __init__(self, store: StateStore, lock: threading.RLock):
        self._store = store
        self._lock = lock

    def __getattr__(self, name: str):
        attribute = getattr(self._store, name)
        if not callable(attribute):
            return attribute

        def locked(*args, **kwargs):
            with self._lock:
                return attribute(*args, **kwargs)
        return locked


class _BudgetGuard:
    """Reserve a request's cost ceiling before sending it.

    The ledger only learns what a request cost after it returns, so with several
    requests in flight the check must also count the ceilings of those still
    outstanding, or N threads could each pass against the same headroom.
    """

    def __init__(self, store, config: RunConfig, lock: threading.RLock):
        self._store = store
        self._config = config
        self._lock = lock
        self._in_flight = 0.0

    def reserve(self, estimate: float | None) -> float:
        with self._lock:
            reserve_budget(self._store.spent(self._config.run_id), self._in_flight, estimate, self._config.budget_usd)
            self._in_flight += estimate
            return estimate

    def release(self, reservation: float) -> None:
        with self._lock:
            self._in_flight -= reservation


def _run_concurrently(jobs: list[tuple], work, workers: int, stop: threading.Event) -> None:
    """Run provider jobs in parallel; a run-wide failure stops jobs not yet sent.

    An account or budget failure stops everything, as in the serial loop. A unit
    that exhausts its own attempts does not: the others finish, and the first
    such unit is raised afterwards so the caller can mark it and resume.
    """
    failures: list[BaseException] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for future in as_completed([pool.submit(work, *job) for job in jobs]):
            error = future.exception()
            if error is None or isinstance(error, _RunStopped):
                continue
            if isinstance(error, (ProviderUnavailable, BudgetExceeded)):
                stop.set()
            failures.append(error)
    if failures:
        severity = (ProviderUnavailable, BudgetExceeded)
        raise next((e for kind in severity for e in failures if isinstance(e, kind)),
                   next((e for e in failures if not isinstance(e, ProviderAttemptsExhausted)), failures[0]))


def run_units(units: list[TranslationUnit], config: RunConfig, runs_root: Path, *, context_units: list[TranslationUnit] | None = None) -> dict:
    run_dir = runs_root / config.run_id
    store = StateStore(run_dir / "state.sqlite")
    input_hash = stable_hash(units) if context_units is None else stable_hash({"units": units, "context_units": context_units})
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
    units_by_id = {unit.unit_id: unit for unit in [*(context_units or []), *units]}
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
    serial = config.translation_concurrency <= 1 or config.provider == "mock"
    lock = threading.RLock()
    if not serial:
        store = _SerializedStore(store, lock)
    budget = _BudgetGuard(store, config, lock)
    stop = threading.Event()

    def call_provider(unit: TranslationUnit, request: TranslationRequest) -> tuple[TranslationResult, TranslationRequest, bool]:
        """Translate one unit through the provider; returns (result, final request, usage already recorded)."""
        usage_recorded = False
        estimated_input = estimated_input_token_ceiling(request)
        estimate = estimate_cost(estimated_input, request.model.max_output_tokens, config.input_usd_per_million, config.output_usd_per_million)
        if config.provider == "mock":
            result = provider.translate(request)
        else:
            prior_attempts = store.provider_attempt_count(config.run_id, unit.unit_id)
            if prior_attempts >= config.provider_max_attempts:
                raise ProviderAttemptsExhausted(
                    f"provider attempt limit already reached for {unit.unit_id}: {prior_attempts}/{config.provider_max_attempts}",
                    unit_id=unit.unit_id,
                )
            last_attempt = store.last_provider_attempt(config.run_id, unit.unit_id)
            if last_attempt and str(last_attempt.get("error_category") or "").startswith("truncated:max_output_tokens"):
                previous_limit = int(last_attempt.get("max_output_tokens") or request.model.max_output_tokens)
                expanded_limit = expanded_output_token_limit(previous_limit, config.max_output_tokens)
                if expanded_limit <= previous_limit:
                    raise ProviderAttemptsExhausted(f"configured output limit exhausted for {unit.unit_id}: {previous_limit}", unit_id=unit.unit_id)
                request = make_request(unit, config, request.context, max_output_tokens_override=expanded_limit)
                validate_request(request)
                store.prepare(config.run_id, unit.unit_id, request.cache_key)
                estimated_input = estimated_input_token_ceiling(request)
                estimate = estimate_cost(estimated_input, request.model.max_output_tokens, config.input_usd_per_million, config.output_usd_per_million)
            result = None
            for attempt_no in range(prior_attempts + 1, config.provider_max_attempts + 1):
                if stop.is_set():
                    raise _RunStopped()
                reservation = budget.reserve(estimate)
                try:
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
                            max_output_tokens=request.model.max_output_tokens,
                            error_category=exc.category,
                            provider_response_id=exc.provider_response_id,
                        )
                        if not exc.retryable or attempt_no >= config.provider_max_attempts:
                            raise ProviderAttemptsExhausted(
                                f"provider attempts exhausted for {unit.unit_id}: {exc.category}",
                                unit_id=unit.unit_id,
                            ) from exc
                        if exc.category == "truncated:max_output_tokens":
                            expanded_limit = expanded_output_token_limit(request.model.max_output_tokens, config.max_output_tokens)
                            if expanded_limit <= request.model.max_output_tokens:
                                raise ProviderAttemptsExhausted(f"configured output limit exhausted for {unit.unit_id}: {expanded_limit}", unit_id=unit.unit_id) from exc
                            request = make_request(unit, config, request.context, max_output_tokens_override=expanded_limit)
                            validate_request(request)
                            store.prepare(config.run_id, unit.unit_id, request.cache_key)
                            estimated_input = estimated_input_token_ceiling(request)
                            estimate = estimate_cost(estimated_input, request.model.max_output_tokens, config.input_usd_per_million, config.output_usd_per_million)
                        continue
                    except APIStatusError as exc:
                        # 402 means the account, not this paragraph, is blocked.
                        # It must not consume a unit's translation retry budget or
                        # charge a fictitious output bill, then fail every following
                        # paragraph in the book. Pause the whole run immediately.
                        denied = 400 <= exc.status_code < 500
                        failed_usage = Usage(
                            input_tokens=0 if denied else estimated_input,
                            output_tokens=0 if denied else request.model.max_output_tokens,
                            cost_usd=0.0 if denied else estimate,
                            usage_source="provider" if denied else "estimated",
                        )
                        store.record_provider_attempt(
                            config.run_id, unit.unit_id, request.request_id, attempt_no,
                            "error", failed_usage,
                            max_output_tokens=request.model.max_output_tokens,
                            error_category=f"http_{exc.status_code}",
                        )
                        raise ProviderUnavailable(
                            f"provider HTTP {exc.status_code}; translation paused for {unit.unit_id}"
                        ) from exc
                    except Exception as exc:
                        # The provider may have accepted a request without returning
                        # usage. Charge the reserved ceiling so retries remain bounded.
                        failed_usage = Usage(
                            input_tokens=estimated_input,
                            output_tokens=request.model.max_output_tokens,
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
                            max_output_tokens=request.model.max_output_tokens,
                            error_category=type(exc).__name__,
                        )
                        raise ProviderAttemptsExhausted(
                            f"non-retryable provider error for {unit.unit_id}: {type(exc).__name__}",
                            unit_id=unit.unit_id,
                        ) from exc
                    candidate = candidate.model_copy(update={"usage": _priced_usage(candidate.usage, config, estimated_input)})
                    if candidate.draft is not None:
                        repaired_text, _ = repair_boundary_markers(unit.source_text, candidate.draft.translated_text)
                        repaired_text, _ = restore_and_rebalance_layout_markers(unit.source_text, repaired_text)
                        candidate = candidate.model_copy(update={"draft": candidate.draft.model_copy(update={"translated_text": repaired_text})})
                    try:
                        validate_result(request, candidate)
                        candidate_report = check_quality(unit, candidate, glossary)
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
                            max_output_tokens=request.model.max_output_tokens,
                            attempted_result_json=candidate.model_dump_json(),
                            error_category=error_category,
                            provider_response_id=candidate.provider_response_id,
                        )
                        if attempt_no >= config.provider_max_attempts:
                            raise ProviderAttemptsExhausted(
                                f"provider attempts exhausted for {unit.unit_id}: {error_category}",
                                unit_id=unit.unit_id,
                            )
                        request = retry_feedback_request(request, candidate, error_category, attempt_no, config)
                        validate_request(request)
                        store.prepare(config.run_id, unit.unit_id, request.cache_key)
                        estimated_input = estimated_input_token_ceiling(request)
                        estimate = estimate_cost(estimated_input, request.model.max_output_tokens, config.input_usd_per_million, config.output_usd_per_million)
                        continue
                    store.record_provider_attempt(
                        config.run_id,
                        unit.unit_id,
                        request.request_id,
                        attempt_no,
                        "success",
                        candidate.usage,
                        max_output_tokens=request.model.max_output_tokens,
                        provider_response_id=candidate.provider_response_id,
                    )
                    result = candidate
                    usage_recorded = True
                    break
                finally:
                    budget.release(reservation)
            if result is None:
                raise ProviderAttemptsExhausted(f"provider attempts exhausted for {unit.unit_id}", unit_id=unit.unit_id)
        return result, request, usage_recorded

    def finalize(unit, request, original_cache_key, result, from_cache, ledger_source, provider_usage_recorded):
        if result.draft is not None:
            repaired_text, repaired = repair_boundary_markers(unit.source_text, result.draft.translated_text)
            repaired_text, _ = restore_and_rebalance_layout_markers(unit.source_text, repaired_text)
            if repaired:
                result = result.model_copy(update={"draft": result.draft.model_copy(update={"translated_text": repaired_text})})
            elif repaired_text != result.draft.translated_text:
                result = result.model_copy(update={"draft": result.draft.model_copy(update={"translated_text": repaired_text})})
        validate_result(request, result)
        report = check_quality(unit, result, glossary)
        store.commit(
            config.run_id,
            result,
            report,
            request.cache_key,
            cached=from_cache,
            source=ledger_source,
            record_usage=not provider_usage_recorded,
        )
        if request.cache_key != original_cache_key:
            store.alias_cache_key(config.run_id, unit.unit_id, original_cache_key, result, report.status)

    def translate_one(unit, request, original_cache_key):
        result, request, usage_recorded = call_provider(unit, request)
        finalize(unit, request, original_cache_key, result, False, None, usage_recorded)

    pending: list[tuple[TranslationUnit, TranslationRequest, str]] = []
    for unit in units:
        ledger_source = None
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
        if store.state(config.run_id, unit.unit_id) == "exhausted" and tm_entry is None:
            continue
        store.prepare(config.run_id, unit.unit_id, request.cache_key)
        original_cache_key = request.cache_key
        state = store.state(config.run_id, unit.unit_id)
        if state == "accepted":
            previous = store.result(config.run_id, unit.unit_id)
            if previous is not None and previous.draft is not None:
                repaired_text, layout_repaired = restore_and_rebalance_layout_markers(
                    unit.source_text, previous.draft.translated_text
                )
                if not layout_repaired:
                    store.commit(config.run_id, previous, check_quality(unit, previous, glossary), request.cache_key,
                                 cached=True, source="accepted_quality_recheck", record_usage=False)
                if layout_repaired:
                    repaired_result = previous.model_copy(
                        update={
                            "request_id": request.request_id,
                            "draft": previous.draft.model_copy(update={"translated_text": repaired_text}),
                        }
                    )
                    validate_result(request, repaired_result)
                    repaired_report = check_quality(unit, repaired_result, glossary)
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
                    repaired_report = check_quality(unit, repaired_result, glossary)
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
        elif (unit.unit_type in {"code", "protected"} or is_formula_only(unit.source_text)):
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
            if serial:
                translate_one(unit, request, original_cache_key)
            else:
                pending.append((unit, request, original_cache_key))
            continue
        finalize(unit, request, original_cache_key, result, from_cache, ledger_source, False)
    if pending:
        _run_concurrently(pending, translate_one, config.translation_concurrency, stop)
    return store.summary(config.run_id)
