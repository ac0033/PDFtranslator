from pathlib import Path

import pytest

from pdf_translation.budget import BudgetExceeded, reserve_budget
from pdf_translation.config import RunConfig
from pdf_translation.hashing import stable_hash
from pdf_translation.pipeline import estimate_units, run_units
from pdf_translation.providers import DeepSeekProvider
from pdf_translation.quality import check_quality, repair_boundary_markers
from pdf_translation.schemas import ModelSpec, TranslationDraft, TranslationResult, TranslationUnit, Usage
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


def test_only_missing_boundary_markers_are_repaired():
    source = "⟦P:lead⟧Translate ⟦P:middle⟧ this"
    repaired, changed = repair_boundary_markers(source, "翻译 ⟦P:middle⟧ 此内容")
    assert changed is True
    assert repaired == "⟦P:lead⟧翻译 ⟦P:middle⟧ 此内容"

    unchanged, changed = repair_boundary_markers(source, "⟦P:lead⟧翻译此内容")
    assert changed is False
    assert unchanged == "⟦P:lead⟧翻译此内容"
