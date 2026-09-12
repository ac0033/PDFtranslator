from pathlib import Path
import json
from types import SimpleNamespace

import pytest

from pdf_translation.budget import BudgetExceeded, reserve_budget
from pdf_translation.config import RunConfig
from pdf_translation.hashing import sha256_bytes, stable_hash
from pdf_translation.pipeline import ProviderAttemptsExhausted, estimate_units, make_request, output_token_limit, run_units
from pdf_translation.providers import DeepSeekProvider, OpenAIProvider, ProviderCallError
from pdf_translation.quality import check_quality, repair_boundary_markers
from pdf_translation.schemas import ArtifactRef, ContextItem, ModelSpec, ProtectedSpan, SourceRef, TranslationDraft, TranslationResult, TranslationUnit, Usage
from pdf_translation.storage import StateStore


def config(run_id: str = "test-run") -> RunConfig:
    return RunConfig(run_id=run_id, provider="mock", model_id="offline", style_revision="s1", prompt_revision="p1", adapter_revision="a1", pricing_revision="none", max_output_tokens=100, budget_usd=0, input_usd_per_million=0, output_usd_per_million=0)


def unit(text: str = "Keep 42 and https://example.com") -> TranslationUnit:
    return TranslationUnit(unit_id="u1", document_id="d1", ir_revision="r1", unit_type="paragraph", source_lang="en", target_lang="zh-CN", source_text=text, source_hash=stable_hash(text))


def test_hot_rerun_does_not_add_provider_usage(tmp_path: Path):
    first = run_units([unit()], config(), tmp_path)
    second = run_units([unit()], config(), tmp_path)
    assert first["states"] == {"accepted": 1}
    assert second["input_tokens"] == first["input_tokens"]


def test_resume_refuses_changed_input(tmp_path: Path):
    run_units([unit()], config(), tmp_path)
    with pytest.raises(ValueError, match="input or config hash changed"):
        run_units([unit("changed")], config(), tmp_path)


def test_fault_injected_number_is_detected():
    source = unit()
    model = ModelSpec(provider="mock", model_id="m", max_output_tokens=10, prompt_revision="p", adapter_revision="a", pricing_revision="x")
    result = TranslationResult(request_id="r", unit_id="u1", draft=TranslationDraft(unit_id="u1", translated_text="Keep 43 and https://example.com"), finish="complete", model=model, usage=Usage(usage_source="unavailable"))
    report = check_quality(source, result)
    assert report.status == "needs_review"
    assert {issue.category for issue in report.issues} == {"number"}


def test_chinese_number_boundary_and_url_punctuation_are_equivalent():
    source = unit()
    model = ModelSpec(provider="mock", model_id="m", max_output_tokens=10, prompt_revision="p", adapter_revision="a", pricing_revision="x")
    result = TranslationResult(request_id="r", unit_id="u1", draft=TranslationDraft(unit_id="u1", translated_text="保留42并参见https://example.com。"), finish="complete", model=model, usage=Usage(usage_source="unavailable"))
    assert check_quality(source, result).status == "pass"


def test_formula_reordering_is_reviewable_without_layout_but_blocked_across_layout():
    model = ModelSpec(provider="mock", model_id="m", max_output_tokens=10, prompt_revision="p", adapter_revision="a", pricing_revision="x")
    source = unit("Before ⟦P:a⟧ and ⟦P:b⟧ after")
    result = TranslationResult(request_id="r", unit_id="u1", draft=TranslationDraft(unit_id="u1", translated_text="之后 ⟦P:b⟧ 与之前 ⟦P:a⟧"), finish="complete", model=model, usage=Usage(usage_source="unavailable"))
    report = check_quality(source, result)
    assert report.status == "needs_review"
    assert report.issues[0].category == "protected_marker_order"
    layout_source = unit("⟦P:layout-first⟧Before ⟦P:a⟧.⟦P:layout-second⟧After ⟦P:b⟧.")
    layout_result = result.model_copy(update={"draft": TranslationDraft(unit_id="u1", translated_text="⟦P:layout-first⟧之前 ⟦P:b⟧。⟦P:layout-second⟧之后 ⟦P:a⟧。")})
    assert check_quality(layout_source, layout_result).status == "blocked"


def test_paid_request_without_price_is_blocked():
    with pytest.raises(BudgetExceeded, match="price is unavailable"):
        reserve_budget(spent=0, reserved=0, requested=None, limit=10)


def test_paid_request_over_budget_is_blocked():
    with pytest.raises(BudgetExceeded, match="budget exceeded"):
        reserve_budget(spent=1, reserved=1, requested=1, limit=2.5)


def test_deepseek_provider_uses_env_and_official_endpoint():
    provider = DeepSeekProvider()
    assert provider.api_key_env == "DEEPSEEK_API_KEY"
    assert provider.base_url == "https://api.deepseek.com"
    assert provider.strict_schema is False


def test_provider_explicitly_separates_reference_context_from_source(monkeypatch: pytest.MonkeyPatch):
    captured = {}

    class FakeClient:
        def __init__(self, **kwargs):
            self.responses = self

        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                status="completed",
                usage=SimpleNamespace(input_tokens=10, output_tokens=10, input_tokens_details=None, output_tokens_details=None),
                output_text=json.dumps({"unit_id": "u1", "translated_text": "仅翻译源文", "uncertainties": []}),
                id="local-test-response",
            )

    monkeypatch.setenv("OPENAI_API_KEY", "local-test-only")
    monkeypatch.setattr("openai.OpenAI", FakeClient)
    request = make_request(unit("Source sentence"), config())
    neighbor = ContextItem(item_id="neighbor", category="local", content="Neighbor only", source_ids=(), revision="test", required=False, selection_reason="test")
    request = request.model_copy(update={"context": request.context.model_copy(update={"items": request.context.items + (neighbor,)})})
    result = OpenAIProvider().translate(request)
    assert result.draft.translated_text == "仅翻译源文"
    assert "<reference-context>" in captured["input"]
    assert "Neighbor only" in captured["input"]
    assert "<source-to-translate>\nSource sentence\n</source-to-translate>" in captured["input"]
    assert "never translate, summarize" in captured["instructions"]


def test_cached_commit_never_duplicates_provider_usage(tmp_path: Path):
    store = StateStore(tmp_path / "state.sqlite")
    source = unit()
    cfg = config()
    from pdf_translation.pipeline import make_request
    request = make_request(source, cfg)
    store.begin_run(cfg.run_id, stable_hash([source]), cfg.config_hash)
    store.prepare(cfg.run_id, source.unit_id, request.cache_key)
    result = TranslationResult(request_id=request.request_id, unit_id="u1", draft=TranslationDraft(unit_id="u1", translated_text=source.source_text), finish="complete", model=request.model, usage=Usage(input_tokens=100, output_tokens=50, cost_usd=1.0, usage_source="provider"))
    report = check_quality(source, result)
    store.commit(cfg.run_id, result, report, request.cache_key)
    store.commit(cfg.run_id, result, report, request.cache_key, cached=True)
    summary = store.summary(cfg.run_id)
    assert summary["cost_usd"] == 1.0
    assert summary["input_tokens"] == 100
    assert summary["output_tokens"] == 50


def test_protected_unit_bypasses_translation_provider(tmp_path: Path):
    source = unit("<b1>,").model_copy(update={"unit_type": "protected"})
    cfg = config("protected-pass")
    summary = run_units([source], cfg, tmp_path)
    stored = StateStore(tmp_path / cfg.run_id / "state.sqlite").result(cfg.run_id, source.unit_id)
    assert summary["states"] == {"accepted": 1}
    assert summary["cost_usd"] == 0.0
    assert stored is not None
    assert stored.draft is not None
    assert stored.draft.translated_text == source.source_text


def test_estimate_units_excludes_protected_passthrough():
    paragraph = unit()
    protected = unit("<b1>,").model_copy(update={"unit_id": "u2", "unit_type": "protected", "source_hash": stable_hash("<b1>,")})
    estimate = estimate_units([paragraph, protected], config())
    assert estimate["unit_count"] == 2
    assert estimate["billable_unit_count"] == 1
    assert estimate["protected_passthrough_count"] == 1
    assert estimate["reserved_output_tokens"] == 100
    assert estimate["estimated_max_cost_usd"] == 0.0
    assert estimate["api_calls"] == 0


def test_adaptive_output_ceiling_is_per_unit_and_bounded():
    cfg = config().model_copy(update={"max_output_tokens": 2000, "output_token_policy": "adaptive-v1"})
    assert output_token_limit(unit("short"), cfg) == 384
    assert output_token_limit(unit("x" * 960), cfg) == 848
    assert output_token_limit(unit("x" * 4000), cfg) == 2000
    estimate = estimate_units([unit("short")], cfg)
    assert estimate["reserved_output_tokens"] == 384


def test_only_missing_boundary_markers_are_repaired():
    source = "⟦P:lead⟧Translate ⟦P:middle⟧ this"
    repaired, changed = repair_boundary_markers(source, "翻译 ⟦P:middle⟧ 此内容")
    assert changed is True
    assert repaired == "⟦P:lead⟧翻译 ⟦P:middle⟧ 此内容"

    unchanged, changed = repair_boundary_markers(source, "⟦P:lead⟧翻译此内容")
    assert changed is False
    assert unchanged == "⟦P:lead⟧翻译此内容"


def test_invalid_billable_response_is_recorded_before_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    class RetryProvider:
        calls = 0

        def translate(self, request):
            self.calls += 1
            if self.calls == 1:
                raise ProviderCallError(
                    "bad schema",
                    category="invalid_structured_output",
                    usage=Usage(input_tokens=100, output_tokens=10, usage_source="provider"),
                    provider_response_id="failed-response",
                )
            return TranslationResult(
                request_id=request.request_id,
                unit_id=request.unit.unit_id,
                draft=TranslationDraft(unit_id=request.unit.unit_id, translated_text=request.unit.source_text),
                finish="complete",
                provider_response_id="successful-response",
                model=request.model,
                usage=Usage(input_tokens=120, output_tokens=20, usage_source="provider"),
            )

    provider = RetryProvider()
    monkeypatch.setattr("pdf_translation.pipeline.DeepSeekProvider", lambda **_: provider)
    cfg = config("retry-ledger").model_copy(
        update={
            "provider": "deepseek",
            "model_id": "deepseek-flash",
            "budget_usd": 1.0,
            "input_usd_per_million": 1.0,
            "output_usd_per_million": 1.0,
            "provider_max_attempts": 2,
        }
    )
    summary = run_units([unit()], cfg, tmp_path)
    assert summary["provider_attempts"] == 2
    assert summary["failed_provider_attempts"] == 1
    assert summary["input_tokens"] == 220
    assert summary["output_tokens"] == 30
    assert summary["cost_usd"] == pytest.approx(0.00025)
    attempts = StateStore(tmp_path / cfg.run_id / "state.sqlite").provider_attempts(cfg.run_id)
    assert [attempt["outcome"] for attempt in attempts] == ["invalid_response", "success"]
    assert attempts[0]["error_category"] == "invalid_structured_output"


def test_provider_attempt_limit_survives_resume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    class InvalidProvider:
        calls = 0

        def translate(self, request):
            self.calls += 1
            raise ProviderCallError(
                "bad schema",
                category="invalid_structured_output",
                usage=Usage(input_tokens=10, output_tokens=5, usage_source="provider"),
                provider_response_id=f"failed-{self.calls}",
            )

    provider = InvalidProvider()
    monkeypatch.setattr("pdf_translation.pipeline.DeepSeekProvider", lambda **_: provider)
    cfg = config("bounded-retry").model_copy(
        update={
            "provider": "deepseek",
            "model_id": "deepseek-flash",
            "budget_usd": 1.0,
            "input_usd_per_million": 1.0,
            "output_usd_per_million": 1.0,
            "provider_max_attempts": 2,
        }
    )
    with pytest.raises(ProviderAttemptsExhausted):
        run_units([unit()], cfg, tmp_path)
    with pytest.raises(ProviderAttemptsExhausted, match="already reached"):
        run_units([unit()], cfg, tmp_path)
    assert provider.calls == 2
    summary = StateStore(tmp_path / cfg.run_id / "state.sqlite").summary(cfg.run_id)
    assert summary["provider_attempts"] == 2
    assert summary["failed_provider_attempts"] == 2


def test_critical_marker_failure_is_recorded_and_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    class MarkerRetryProvider:
        calls = 0

        def translate(self, request):
            self.calls += 1
            translated = "missing marker" if self.calls == 1 else request.unit.source_text
            return TranslationResult(
                request_id=request.request_id,
                unit_id=request.unit.unit_id,
                draft=TranslationDraft(unit_id=request.unit.unit_id, translated_text=translated),
                finish="complete",
                provider_response_id=f"response-{self.calls}",
                model=request.model,
                usage=Usage(input_tokens=10, output_tokens=5, usage_source="provider"),
            )

    provider = MarkerRetryProvider()
    monkeypatch.setattr("pdf_translation.pipeline.DeepSeekProvider", lambda **_: provider)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only")
    text = "Before ⟦P:formula-1⟧ after"
    marker_start = text.index("⟦P:formula-1⟧")
    protected = ProtectedSpan(
        occurrence_id="formula-1",
        marker="⟦P:formula-1⟧",
        kind="formula",
        original=ArtifactRef(uri="fixture/formula.txt", sha256=sha256_bytes(b"formula"), media_type="text/plain"),
        source_ref=SourceRef(block_id="u1", page_index=0, bbox=(0, 0, 1, 1), char_start=marker_start, char_end=marker_start + len("⟦P:formula-1⟧")),
    )
    source = unit(text).model_copy(
        update={
            "protected": (protected,),
            "source_hash": stable_hash(text),
        }
    )
    cfg = config("marker-retry").model_copy(
        update={
            "provider": "deepseek",
            "model_id": "deepseek-flash",
            "budget_usd": 1.0,
            "input_usd_per_million": 1.0,
            "output_usd_per_million": 1.0,
            "provider_max_attempts": 2,
        }
    )
    summary = run_units([source], cfg, tmp_path)
    assert summary["states"] == {"accepted": 1}
    assert summary["provider_attempts"] == 2
    assert summary["failed_provider_attempts"] == 1
    attempts = StateStore(tmp_path / cfg.run_id / "state.sqlite").provider_attempts(cfg.run_id)
    assert attempts[0]["error_category"] == "quality:protected_marker"


def test_truncation_is_charged_then_retried_with_expanded_ceiling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    class TruncationProvider:
        limits = []

        def translate(self, request):
            self.limits.append(request.model.max_output_tokens)
            if len(self.limits) == 1:
                raise ProviderCallError(
                    "truncated",
                    category="truncated:max_output_tokens",
                    usage=Usage(input_tokens=10, output_tokens=request.model.max_output_tokens, usage_source="provider"),
                    provider_response_id="truncated-response",
                )
            return TranslationResult(
                request_id=request.request_id,
                unit_id=request.unit.unit_id,
                draft=TranslationDraft(unit_id=request.unit.unit_id, translated_text=request.unit.source_text),
                finish="complete",
                provider_response_id="complete-response",
                model=request.model,
                usage=Usage(input_tokens=10, output_tokens=10, usage_source="provider"),
            )

    provider = TruncationProvider()
    monkeypatch.setattr("pdf_translation.pipeline.DeepSeekProvider", lambda **_: provider)
    cfg = config("truncation-retry").model_copy(
        update={
            "provider": "deepseek",
            "model_id": "deepseek-flash",
            "max_output_tokens": 2000,
            "output_token_policy": "adaptive-v1",
            "budget_usd": 1.0,
            "input_usd_per_million": 1.0,
            "output_usd_per_million": 1.0,
            "provider_max_attempts": 2,
        }
    )
    summary = run_units([unit("short")], cfg, tmp_path)
    assert provider.limits == [384, 768]
    assert summary["states"] == {"accepted": 1}
    assert summary["provider_attempts"] == 2
    attempts = StateStore(tmp_path / cfg.run_id / "state.sqlite").provider_attempts(cfg.run_id)
    assert [attempt["max_output_tokens"] for attempt in attempts] == [384, 768]
    assert attempts[0]["error_category"] == "truncated:max_output_tokens"


def test_resume_expands_the_last_persisted_truncated_ceiling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    class CompleteProvider:
        limits = []

        def translate(self, request):
            self.limits.append(request.model.max_output_tokens)
            return TranslationResult(
                request_id=request.request_id,
                unit_id=request.unit.unit_id,
                draft=TranslationDraft(unit_id=request.unit.unit_id, translated_text=request.unit.source_text),
                finish="complete",
                provider_response_id="resumed-response",
                model=request.model,
                usage=Usage(input_tokens=10, output_tokens=10, usage_source="provider"),
            )

    provider = CompleteProvider()
    monkeypatch.setattr("pdf_translation.pipeline.DeepSeekProvider", lambda **_: provider)
    cfg = config("truncation-resume").model_copy(
        update={
            "provider": "deepseek",
            "model_id": "deepseek-flash",
            "max_output_tokens": 2000,
            "output_token_policy": "adaptive-v1",
            "budget_usd": 1.0,
            "input_usd_per_million": 1.0,
            "output_usd_per_million": 1.0,
            "provider_max_attempts": 2,
        }
    )
    source = unit("short")
    request = make_request(source, cfg)
    store = StateStore(tmp_path / cfg.run_id / "state.sqlite")
    store.begin_run(cfg.run_id, stable_hash([source]), cfg.config_hash)
    store.prepare(cfg.run_id, source.unit_id, request.cache_key)
    store.record_provider_attempt(
        cfg.run_id,
        source.unit_id,
        request.request_id,
        1,
        "invalid_response",
        Usage(input_tokens=10, output_tokens=384, cost_usd=0.000394, usage_source="provider"),
        max_output_tokens=384,
        error_category="truncated:max_output_tokens",
        provider_response_id="interrupted-response",
    )
    summary = run_units([source], cfg, tmp_path)
    assert provider.limits == [768]
    assert summary["states"] == {"accepted": 1}
    attempts = store.provider_attempts(cfg.run_id)
    assert [attempt["max_output_tokens"] for attempt in attempts] == [384, 768]
