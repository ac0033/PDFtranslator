from __future__ import annotations

import re
from collections import Counter

from .hashing import stable_hash
from .schemas import ContextPacket, DocumentIR, TranslationRequest, TranslationResult, TranslationUnit

MARKERS = re.compile(r"⟦P:[A-Za-z0-9_.:-]+⟧")


def validate_document_ir(document: DocumentIR) -> None:
    blocks = {block.block_id: block for block in document.blocks}
    if len(blocks) != len(document.blocks):
        raise ValueError("duplicate block_id")
    for block in document.blocks:
        for ref in block.source_refs:
            if ref.block_id != block.block_id:
                raise ValueError(f"source ref points to wrong block: {block.block_id}")
            if ref.page_index >= document.page_count:
                raise ValueError(f"page index outside document: {ref.page_index}")
            if ref.char_end > len(block.source_text):
                raise ValueError(f"character range outside block: {block.block_id}")


def validate_unit(unit: TranslationUnit) -> None:
    if stable_hash(unit.source_text) != unit.source_hash:
        raise ValueError("source_hash does not match source_text")
    expected = Counter(item.marker for item in unit.protected)
    if len({item.occurrence_id for item in unit.protected}) != len(unit.protected):
        raise ValueError("duplicate protected occurrence_id")
    if Counter(MARKERS.findall(unit.source_text)) != expected:
        raise ValueError("protected marker inventory does not match source_text")


def validate_packet(unit: TranslationUnit, packet: ContextPacket) -> None:
    if packet.unit_id != unit.unit_id:
        raise ValueError("context unit_id mismatch")
    if packet.token_count > packet.token_budget:
        raise ValueError("context token budget exceeded")
    if packet.missing_dependencies:
        raise ValueError("context has missing dependencies")
    if stable_hash({"unit_id": packet.unit_id, "items": packet.items}) != packet.packet_hash:
        raise ValueError("packet_hash mismatch")


def validate_request(request: TranslationRequest) -> None:
    validate_unit(request.unit)
    validate_packet(request.unit, request.context)
    expected = stable_hash({"unit": request.unit, "context": request.context, "model": request.model})
    if expected != request.cache_key:
        raise ValueError("cache_key mismatch")


def validate_result(request: TranslationRequest, result: TranslationResult) -> None:
    if result.request_id != request.request_id or result.unit_id != request.unit.unit_id:
        raise ValueError("result identity mismatch")
    if result.model != request.model:
        raise ValueError("result model mismatch")
    if result.finish == "complete" and result.draft is None:
        raise ValueError("complete result requires draft")
    if result.draft and result.draft.unit_id != request.unit.unit_id:
        raise ValueError("draft unit_id mismatch")

