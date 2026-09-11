from pathlib import Path

import pytest

from pdf_translation.ab_experiment import approved_execution_configs, build_blind_review, prepare_ab_manifest
from pdf_translation.config import RunConfig
from pdf_translation.context_models import Scope, StyleRule
from pdf_translation.context_store import ContextStore
from pdf_translation.hashing import stable_hash
from pdf_translation.pipeline import DEFAULT_TRANSLATION_STYLE
from pdf_translation.schemas import TranslationUnit


def config(run_id: str, context_engine: str, context_db: str | None = None) -> RunConfig:
    return RunConfig(
        run_id=run_id,
        provider="deepseek",
        model_id="deepseek-flash",
        style_revision="ab",
        prompt_revision="ab",
        adapter_revision="ab",
        pricing_revision="rates",
        max_output_tokens=2000,
        budget_usd=0,
        input_usd_per_million=0.3,
        output_usd_per_million=1.2,
        output_token_policy="adaptive-v1",
        context_engine=context_engine,
        context_db=context_db,
    )


def test_ab_manifest_allows_only_the_context_difference(tmp_path: Path):
    text = "A policy maps states to actions."
    value = TranslationUnit(unit_id="u1", document_id="doc", ir_revision="ir", unit_type="paragraph", source_lang="en", target_lang="zh-CN", source_text=text, source_hash=stable_hash(text))
    units_path = tmp_path / "units.jsonl"
    units_path.write_text(value.model_dump_json() + "\n", encoding="utf-8")
    context_path = tmp_path / "context.sqlite"
    store = ContextStore(context_path)
    store.put_style(StyleRule(rule_id="style", content=DEFAULT_TRANSLATION_STYLE, scope=Scope(kind="document", value="doc"), version="s1"))
    report = prepare_ab_manifest(
        units_path,
        [value],
        config("fixed", "v0-fixed"),
        config("context", "v1", str(context_path)),
    )
    assert report["status"] == "prepared_no_api_calls"
    assert report["api_calls"] == 0
    assert report["output_ceiling"]["reserved_output_tokens"] == 384
    assert report["fixed_arm"]["estimate"]["reserved_output_tokens"] == 384
    assert report["context_arm"]["estimate"]["reserved_output_tokens"] == 384


def test_ab_manifest_rejects_prompt_drift(tmp_path: Path):
    value = TranslationUnit(unit_id="u1", document_id="doc", ir_revision="ir", unit_type="paragraph", source_lang="en", target_lang="zh-CN", source_text="Text", source_hash=stable_hash("Text"))
    units_path = tmp_path / "units.jsonl"
    units_path.write_text(value.model_dump_json() + "\n", encoding="utf-8")
    context_path = tmp_path / "context.sqlite"
    fixed = config("fixed", "v0-fixed")
    context = config("context", "v1", str(context_path)).model_copy(update={"prompt_revision": "changed"})
    with pytest.raises(ValueError, match="outside context controls"):
        prepare_ab_manifest(units_path, [value], fixed, context)


def test_ab_manifest_rejects_extra_approved_style_as_a_confounder(tmp_path: Path):
    value = TranslationUnit(unit_id="u1", document_id="doc", ir_revision="ir", unit_type="paragraph", source_lang="en", target_lang="zh-CN", source_text="Text", source_hash=stable_hash("Text"))
    units_path = tmp_path / "units.jsonl"
    units_path.write_text(value.model_dump_json() + "\n", encoding="utf-8")
    context_path = tmp_path / "context.sqlite"
    store = ContextStore(context_path)
    store.put_style(StyleRule(rule_id="common", content=DEFAULT_TRANSLATION_STYLE, scope=Scope(kind="document", value="doc"), version="s1"))
    store.put_style(StyleRule(rule_id="extra", content="Use short sentences.", scope=Scope(kind="document", value="doc"), version="s1"))
    with pytest.raises(ValueError, match="exactly the same single style"):
        prepare_ab_manifest(
            units_path,
            [value],
            config("fixed", "v0-fixed"),
            config("context", "v1", str(context_path)),
        )


def test_paid_execution_budget_is_bounded_and_not_written_to_configs(tmp_path: Path):
    context_path = tmp_path / "context.sqlite"
    fixed = config("fixed", "v0-fixed")
    context = config("context", "v1", str(context_path))
    paid_fixed, paid_context = approved_execution_configs(fixed, context, 0.20)
    assert paid_fixed.budget_usd == 0.10
    assert paid_context.budget_usd == 0.10
    assert fixed.budget_usd == 0
    assert context.budget_usd == 0
    with pytest.raises(ValueError, match="within"):
        approved_execution_configs(fixed, context, 0.21)


def test_blind_review_hides_arm_identity_and_keeps_separate_key():
    values = [
        TranslationUnit(unit_id="u1", document_id="doc", ir_revision="ir", unit_type="paragraph", source_lang="en", target_lang="zh-CN", source_text="First", source_hash=stable_hash("First")),
        TranslationUnit(unit_id="u2", document_id="doc", ir_revision="ir", unit_type="protected", source_lang="en", target_lang="zh-CN", source_text="formula", source_hash=stable_hash("formula")),
    ]
    review, key = build_blind_review(values, {"u1": "固定译文"}, {"u1": "上下文译文"})
    assert review["pair_count"] == 1
    assert "arm" not in str(review)
    assert {review["rows"][0]["candidate_x"], review["rows"][0]["candidate_y"]} == {"固定译文", "上下文译文"}
    assert key["rows"][0]["unit_id"] == "u1"
    assert {key["rows"][0]["candidate_x_arm"], key["rows"][0]["candidate_y_arm"]} == {"fixed", "context"}
