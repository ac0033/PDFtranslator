from __future__ import annotations

import re

from .context_models import ContextBuildResult, GlossaryEntry, Scope, SelectionTrace, SemanticEntry, StyleRule, TranslationMemoryEntry
from .hashing import stable_hash
from .term_matching import normalized_term, term_pattern
from .schemas import ContextItem, ContextPacket, TranslationUnit

CATEGORY_ORDER = {"style": 0, "glossary": 1, "definition": 2, "local": 3, "summary": 4, "tm": 5}
LOCAL_CONTEXT_CHARACTER_LIMIT = 320


def estimate_tokens(text: str) -> int:
    return max(1, len(text.encode("utf-8")))


def applicable_scopes(unit: TranslationUnit) -> tuple[Scope, ...]:
    scopes = [Scope(kind="unit", value=unit.unit_id)]
    if unit.section_path:
        scopes.append(Scope(kind="section", value="/".join(unit.section_path)))
        scopes.append(Scope(kind="chapter", value=unit.section_path[0]))
    scopes.extend((Scope(kind="document", value=unit.document_id), Scope(kind="domain", value="general")))
    return tuple(scopes)


def _scope_rank(scope: Scope, ordered: tuple[Scope, ...]) -> int | None:
    try:
        return ordered.index(scope)
    except ValueError:
        return None


def select_glossary(unit: TranslationUnit, entries: list[GlossaryEntry]) -> list[GlossaryEntry]:
    scopes = applicable_scopes(unit)
    matches: dict[str, tuple[int, GlossaryEntry]] = {}
    for entry in entries:
        if entry.status != "approved" or entry.source_term in entry.exceptions:
            continue
        if not term_pattern(entry.source_term).search(unit.source_text):
            continue
        rank = _scope_rank(entry.scope, scopes)
        if rank is None:
            continue
        key = normalized_term(entry.source_term)
        previous = matches.get(key)
        if previous and previous[0] == rank and previous[1].target_term != entry.target_term:
            raise ValueError(f"conflicting approved glossary entries for {entry.source_term} at {entry.scope.kind}:{entry.scope.value}")
        if previous is None or rank < previous[0]:
            matches[key] = (rank, entry)
    return [value[1] for value in sorted(matches.values(), key=lambda value: (value[0], value[1].source_term.casefold()))]


def select_exact_tm(unit: TranslationUnit, entries: list[TranslationMemoryEntry], context_signature: str, glossary_snapshot_id: str, style_snapshot_id: str) -> TranslationMemoryEntry | None:
    eligible = [entry for entry in entries if entry.source_hash == unit.source_hash and entry.source_text == unit.source_text and entry.source_lang == unit.source_lang and entry.target_lang == unit.target_lang and entry.context_signature == context_signature and entry.glossary_snapshot_id == glossary_snapshot_id and entry.style_snapshot_id == style_snapshot_id and entry.qa_status == "passed" and entry.human_status == "confirmed"]
    if len(eligible) > 1 and len({entry.target_text for entry in eligible}) > 1:
        raise ValueError(f"conflicting confirmed TM entries for {unit.unit_id}")
    return eligible[0] if eligible else None


def build_context_packet(
    unit: TranslationUnit,
    glossary: list[GlossaryEntry],
    styles: list[StyleRule],
    semantics: list[SemanticEntry],
    units_by_id: dict[str, TranslationUnit],
    token_budget: int = 600,
    glossary_snapshot_id: str = "glossary-empty",
    style_snapshot_id: str = "style-empty",
    tm_entry: TranslationMemoryEntry | None = None,
) -> tuple[ContextPacket, ContextBuildResult]:
    scopes = applicable_scopes(unit)
    candidates: list[tuple[ContextItem, int]] = []
    for rule in styles:
        if rule.status == "approved" and _scope_rank(rule.scope, scopes) is not None:
            if rule.expected_source_hash is not None and rule.expected_source_hash != stable_hash(unit.source_text):
                raise ValueError(f"source-bound feedback no longer matches source: {rule.rule_id}")
            item = ContextItem(item_id=f"style:{rule.rule_id}", category="style", content=rule.content, source_ids=(), revision=rule.version, required=True, selection_reason=f"approved {rule.scope.kind} style")
            candidates.append((item, rule.priority))
    for entry in select_glossary(unit, glossary):
        content = f"{entry.source_term} => {entry.target_term}; sense: {entry.sense}"
        if entry.forbidden_target_terms:
            content += "; 禁用译名: " + "、".join(entry.forbidden_target_terms)
        item = ContextItem(item_id=f"glossary:{entry.term_id}", category="glossary", content=content, source_ids=entry.source_ids, revision=entry.version, required=True, selection_reason=f"approved match at {entry.scope.kind} scope", term_source=entry.source_term, term_target=entry.target_term)
        candidates.append((item, 0))
    for semantic in semantics:
        if semantic.status != "approved" or semantic.semantic_id not in unit.dependency_ids:
            continue
        item = ContextItem(item_id=f"semantic:{semantic.semantic_id}", category="definition", content=f"{semantic.summary}\nEvidence: {semantic.evidence_text}", source_ids=semantic.source_ids, revision=semantic.revision, required=True, selection_reason="explicit semantic dependency")
        candidates.append((item, 0))
    def order_key(value):
        return min(((r.page_index, r.bbox[1], r.bbox[0]) for r in value.source_refs), default=(0, 0, 0))

    for neighbor_id in unit.neighbor_ids:
        neighbor = units_by_id.get(neighbor_id)
        if neighbor:
            preceding = order_key(neighbor) < order_key(unit)
            content = neighbor.source_text
            if "native_immutable_slots" in neighbor.risk_tags:
                for marker, literal in neighbor.protected_literal_hints.items():
                    content = content.replace(marker, literal)
            if len(content) > LOCAL_CONTEXT_CHARACTER_LIMIT:
                content = ("… " + content[-LOCAL_CONTEXT_CHARACTER_LIMIT:].lstrip()) if preceding else (content[:LOCAL_CONTEXT_CHARACTER_LIMIT].rstrip() + " …")
            content = ("Preceding source excerpt: " if preceding else "Following source excerpt: ") + content
            item = ContextItem(item_id=f"local:{neighbor_id}", category="local", content=content, source_ids=(neighbor_id,), revision=neighbor.ir_revision, required=False, selection_reason="preceding tail" if preceding else "following head")
            candidates.append((item, 0 if preceding else 1))
    if tm_entry:
        item = ContextItem(item_id=f"tm:{tm_entry.tm_id}", category="tm", content=tm_entry.target_text, source_ids=(tm_entry.tm_id,), revision=tm_entry.glossary_snapshot_id + ":" + tm_entry.style_snapshot_id, required=False, selection_reason="exact confirmed TM")
        candidates.append((item, 0))

    candidates.sort(key=lambda pair: (not pair[0].required, CATEGORY_ORDER[pair[0].category], pair[1], pair[0].item_id))
    selected: list[ContextItem] = []
    traces: list[SelectionTrace] = []
    used = 0
    for item, _ in candidates:
        cost = estimate_tokens(item.content)
        if used + cost <= token_budget:
            selected.append(item)
            used += cost
            traces.append(SelectionTrace(item_id=item.item_id, selected=True, reason=item.selection_reason, estimated_tokens=cost))
        elif item.required:
            raise ValueError(f"required context exceeds token budget at {item.item_id}: {used + cost} > {token_budget}")
        else:
            traces.append(SelectionTrace(item_id=item.item_id, selected=False, reason="optional item trimmed by token budget", estimated_tokens=cost))
    snapshot = stable_hash({"glossary": glossary_snapshot_id, "style": style_snapshot_id, "semantic": [(item.item_id, item.revision) for item in selected if item.category in {"definition", "summary"}]})
    payload = {"unit_id": unit.unit_id, "items": tuple(selected)}
    packet = ContextPacket(unit_id=unit.unit_id, router_revision="v1-directional-neighbors-rc12", memory_snapshot_id=snapshot, items=tuple(selected), token_count=used, token_counter_id="utf8-byte-ceiling-v1", token_count_is_estimate=True, token_budget=token_budget, missing_dependencies=tuple(sorted(set(unit.dependency_ids) - {item.item_id.removeprefix("semantic:") for item in selected if item.item_id.startswith("semantic:")})), packet_hash=stable_hash(payload))
    if packet.missing_dependencies:
        raise ValueError(f"missing semantic dependencies: {packet.missing_dependencies}")
    return packet, ContextBuildResult(packet_hash=packet.packet_hash, traces=tuple(traces))
