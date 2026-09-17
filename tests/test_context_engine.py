from pathlib import Path

import pytest

from pdf_translation.analyzer import extract_term_candidates, first_occurrences, validate_semantic_evidence
from pdf_translation.context_models import GlossaryEntry, Scope, SemanticEntry, StyleRule, TranslationMemoryEntry, make_invalidation_report
from pdf_translation.context_router import build_context_packet, select_exact_tm, select_glossary
from pdf_translation.context_store import ContextStore
from pdf_translation.config import RunConfig
from pdf_translation.hashing import stable_hash
from pdf_translation.pipeline import run_units
from pdf_translation.storage import StateStore
from pdf_translation.schemas import TranslationUnit


def unit(unit_id: str, text: str, section=("Chapter 1", "Section 1"), neighbors=(), dependencies=()) -> TranslationUnit:
    return TranslationUnit(unit_id=unit_id, document_id="doc", ir_revision="ir1", unit_type="paragraph", source_lang="en", target_lang="zh-CN", source_text=text, source_hash=stable_hash(text), section_path=section, neighbor_ids=neighbors, dependency_ids=dependencies)


def glossary(term_id: str, target: str, scope: Scope, status="approved") -> GlossaryEntry:
    return GlossaryEntry(term_id=term_id, source_term="policy", target_term=target, sense="decision rule", scope=scope, source_ids=("u0",), version="g1", status=status)


def test_scope_precedence_and_conflict_detection():
    value = unit("u1", "A policy maps states to actions.")
    entries = [glossary("g-doc", "方针", Scope(kind="document", value="doc")), glossary("g-ch", "策略", Scope(kind="chapter", value="Chapter 1"))]
    assert select_glossary(value, entries)[0].target_term == "策略"
    entries.append(glossary("g-ch-2", "政策", Scope(kind="chapter", value="Chapter 1")))
    with pytest.raises(ValueError, match="conflicting"):
        select_glossary(value, entries)


def test_router_keeps_required_and_trims_optional_context():
    neighbor = unit("u0", "This is optional neighboring prose " * 20)
    value = unit("u1", "A policy is selected.", neighbors=("u0",))
    style = StyleRule(rule_id="s1", content="Use natural textbook Chinese.", scope=Scope(kind="document", value="doc"), version="s1")
    packet, build = build_context_packet(value, [glossary("g1", "策略", Scope(kind="document", value="doc"))], [style], [], {"u0": neighbor}, token_budget=150, glossary_snapshot_id="g1", style_snapshot_id="s1")
    assert {item.category for item in packet.items} == {"style", "glossary"}
    assert any(not trace.selected and trace.item_id == "local:u0" for trace in build.traces)
    assert packet.token_count <= packet.token_budget


def test_required_dependency_never_silently_drops():
    value = unit("u1", "A policy is selected.", dependencies=("belief",))
    with pytest.raises(ValueError, match="missing semantic"):
        build_context_packet(value, [], [], [], {}, token_budget=100)


def test_semantic_summary_requires_known_evidence():
    entry = SemanticEntry(semantic_id="belief", concept="belief", scope=Scope(kind="chapter", value="Chapter 1"), evidence_text="A belief is a distribution.", summary="Belief means a distribution over states.", source_ids=("missing",), revision="m1", status="approved")
    with pytest.raises(ValueError, match="unknown evidence"):
        validate_semantic_evidence([entry], {"u1"})


def test_tm_reuse_requires_exact_confirmed_compatible_entry():
    value = unit("u1", "A policy is selected.")
    entry = TranslationMemoryEntry(tm_id="tm1", source_text=value.source_text, target_text="选择一个策略。", source_lang="en", target_lang="zh-CN", context_signature="c1", glossary_snapshot_id="g1", style_snapshot_id="s1", qa_status="passed", human_status="confirmed")
    assert select_exact_tm(value, [entry], "c1", "g1", "s1") == entry
    assert select_exact_tm(value, [entry], "changed", "g1", "s1") is None


def test_dependency_invalidation_is_transitive():
    report = make_invalidation_report({"glossary:g1"}, {"glossary:g1": {"semantic:s1"}, "semantic:s1": {"unit:u1", "unit:u2"}})
    assert report.affected_unit_ids == ("u1", "u2")


def test_analyzer_candidates_and_first_occurrence():
    units = [unit("u1", "Policy maps state to action."), unit("u2", "The policy is deterministic.")]
    candidates = extract_term_candidates(units)
    assert any(item.source_term.casefold() == "policy" and item.occurrence_count == 2 for item in candidates)
    assert first_occurrences(units, ["policy"]) == {"policy": "u1"}


def test_term_candidates_exclude_urls_and_common_words():
    candidates = extract_term_candidates([unit("u1", "See https://example.com for policy")], minimum_occurrences=1)
    assert [item.source_term.casefold() for item in candidates] == ["policy"]


def test_context_store_round_trip(tmp_path: Path):
    store = ContextStore(tmp_path / "context.sqlite")
    entry = glossary("g1", "策略", Scope(kind="document", value="doc"))
    store.put_glossary(entry)
    store.replace_dependencies("unit:u1", ["glossary:g1"])
    assert store.glossary() == [entry]
    assert store.dependency_edges() == {"glossary:g1": {"unit:u1"}}


def test_v1_pipeline_recomputes_when_context_snapshot_changes(tmp_path: Path):
    context_path = tmp_path / "context.sqlite"
    store = ContextStore(context_path)
    scope = Scope(kind="document", value="doc")
    store.put_style(StyleRule(rule_id="s1", content="Natural Chinese", scope=scope, version="s1"))
    value = unit("u1", "A policy is selected.")
    cfg = RunConfig(run_id="v1", provider="mock", model_id="offline", style_revision="s1", prompt_revision="p1", adapter_revision="a1", pricing_revision="none", max_output_tokens=100, budget_usd=0, input_usd_per_million=0, output_usd_per_million=0, context_engine="v1", context_db=str(context_path))
    run_units([value], cfg, tmp_path / "runs")
    state = StateStore(tmp_path / "runs/v1/state.sqlite")
    first_key = state.task_cache_key("v1", "u1")
    store.put_glossary(glossary("g1", "策略", scope))
    run_units([value], cfg, tmp_path / "runs")
    second_key = state.task_cache_key("v1", "u1")
    assert first_key != second_key
    # The mock does not obey the newly approved Chinese term; the gate must block it.
    assert state.summary("v1")["states"] == {"blocked": 1}
    assert any(issue["category"] == "terminology" for report in state.quality_reports("v1") for issue in report["quality"]["issues"])


def test_v1_exact_confirmed_tm_is_reused_without_provider_text(tmp_path: Path):
    context_path = tmp_path / "context.sqlite"
    store = ContextStore(context_path)
    scope = Scope(kind="document", value="doc")
    style = StyleRule(rule_id="s1", content="Natural Chinese", scope=scope, version="s1")
    store.put_style(style)
    value = unit("u1", "A policy is selected.")
    glossary_snapshot = stable_hash([])
    style_snapshot = stable_hash([style])
    base_packet, _ = build_context_packet(value, [], [style], [], {"u1": value}, glossary_snapshot_id=glossary_snapshot, style_snapshot_id=style_snapshot)
    tm = TranslationMemoryEntry(tm_id="tm1", source_text=value.source_text, target_text="选择一个策略。", source_lang="en", target_lang="zh-CN", context_signature=base_packet.packet_hash, glossary_snapshot_id=glossary_snapshot, style_snapshot_id=style_snapshot, qa_status="passed", human_status="confirmed")
    store.put_tm(tm)
    cfg = RunConfig(run_id="v1-tm", provider="mock", model_id="offline", style_revision="s1", prompt_revision="p1", adapter_revision="a1", pricing_revision="none", max_output_tokens=100, budget_usd=0, input_usd_per_million=0, output_usd_per_million=0, context_engine="v1", context_db=str(context_path))
    run_units([value], cfg, tmp_path / "runs")
    result = StateStore(tmp_path / "runs/v1-tm/state.sqlite").result("v1-tm", "u1")
    assert result is not None
    assert result.draft is not None
    assert result.draft.translated_text == "选择一个策略。"
    assert result.provider_response_id is None


def test_neighbor_excerpts_use_preceding_tail_and_following_head():
    from pdf_translation.schemas import SourceRef
    values = [unit("before", "OLD " * 150 + "Relevant tail."), unit("current", "Current paragraph."), unit("after", "Relevant head. " + "LATER " * 150)]
    for index, value in enumerate(values):
        values[index] = value.model_copy(update={"source_refs": (SourceRef(block_id=value.unit_id, page_index=0, bbox=(50, 100 * index, 350, 100 * index + 20), char_start=0, char_end=len(value.source_text)),)})
    current = values[1].model_copy(update={"neighbor_ids": ("after", "before")})
    packet, _ = build_context_packet(current, [], [], [], {u.unit_id: u for u in values}, token_budget=1000)
    assert packet.items[0].content.endswith("Relevant tail.")
    assert packet.items[0].selection_reason == "preceding tail"
    assert packet.items[1].content.startswith("Following source excerpt: Relevant head.")


def test_review_feedback_binds_source_content_not_only_unit_id():
    original=unit('u1','Original complete paragraph.')
    feedback=StyleRule(rule_id='review-u1',content='Review grammar.',scope=Scope(kind='unit',value='u1'),version='r1',expected_source_hash=stable_hash(original.source_text))
    packet,_=build_context_packet(original,[],[feedback],[],{'u1':original},token_budget=2000)
    assert any(i.item_id=='style:review-u1' for i in packet.items)
    # Deliberately retain the stale source_hash field: inspect actual text anyway.
    changed=original.model_copy(update={'source_text':'A different paragraph reuses this ordinal.'})
    with pytest.raises(ValueError,match='no longer matches source'):
        build_context_packet(changed,[],[feedback],[],{'u1':changed},token_budget=2000)
    another=unit('u2','Another paragraph.')
    packet,_=build_context_packet(another,[],[feedback],[],{'u2':another},token_budget=2000)
    assert not any(i.item_id=='style:review-u1' for i in packet.items)


def test_source_bound_feedback_cannot_be_document_wide():
    with pytest.raises(ValueError,match='requires unit scope'):
        StyleRule(rule_id='bad',content='Specific feedback.',scope=Scope(kind='document',value='doc'),version='r1',expected_source_hash=stable_hash('one paragraph'))
