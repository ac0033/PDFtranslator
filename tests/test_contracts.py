from pathlib import Path

import pytest
from pydantic import ValidationError

from pdf_translation.hashing import stable_hash
from pdf_translation.schemas import ArtifactRef, ContextPacket, ProtectedSpan, SourceRef, TranslationUnit
from pdf_translation.validation import validate_packet, validate_unit


def unit(text: str = "Keep 42 and ⟦P:eq-1⟧") -> TranslationUnit:
    return TranslationUnit(unit_id="u1", document_id="d1", ir_revision="r1", unit_type="paragraph", source_lang="en", target_lang="zh-CN", source_text=text, source_hash=stable_hash(text))


def test_extra_fields_are_rejected():
    with pytest.raises(ValidationError):
        TranslationUnit.model_validate({**unit().model_dump(), "surprise": True})


def test_wrong_source_hash_is_rejected():
    value = unit().model_copy(update={"source_hash": "0" * 64})
    with pytest.raises(ValueError, match="source_hash"):
        validate_unit(value)


def test_context_budget_is_enforced():
    value = unit("plain")
    packet = ContextPacket(unit_id="u1", router_revision="r", memory_snapshot_id="m", token_count=11, token_counter_id="estimate", token_count_is_estimate=True, token_budget=10, packet_hash=stable_hash({"unit_id": "u1", "items": ()}))
    with pytest.raises(ValueError, match="budget"):
        validate_packet(value, packet)


def test_unknown_or_missing_marker_is_rejected():
    text = "Keep ⟦P:eq-1⟧"
    ref = SourceRef(block_id="b1", page_index=0, bbox=(0, 0, 1, 1), char_start=5, char_end=len(text))
    artifact = ArtifactRef(uri="assets/eq-1.bin", sha256="a" * 64, media_type="application/octet-stream")
    span = ProtectedSpan(occurrence_id="o1", marker="⟦P:eq-1⟧", kind="formula", original=artifact, source_ref=ref)
    value = unit(text).model_copy(update={"protected": (span,)})
    validate_unit(value)
    bad_text = text + " ⟦P:unknown⟧"
    with pytest.raises(ValueError, match="inventory"):
        validate_unit(value.model_copy(update={"source_text": bad_text, "source_hash": stable_hash(bad_text)}))
