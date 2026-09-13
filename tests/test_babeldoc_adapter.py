from pathlib import Path
import json
from types import SimpleNamespace

import pymupdf
import pytest

from pdf_translation import babeldoc_adapter
from pdf_translation.babeldoc_adapter import ContentMatcher, _write_content_review_queue, canonicalize_babeldoc_text, locate_source_bbox, restore_babeldoc_placeholders, validate_capture_identity
from pdf_translation.config import load_config
from pdf_translation.hashing import sha256_file, stable_hash
from pdf_translation.schemas import TranslationUnit


def test_babeldoc_placeholders_round_trip_and_have_distinct_occurrences():
    source = "Value <b1>x</b1> plus <b2>."
    canonical, protected, marker_map = canonicalize_babeldoc_text(
        source, page_index=4, sequence=2, bbox=(1.0, 2.0, 3.0, 4.0)
    )
    assert len(protected) == 3
    assert len({item.occurrence_id for item in protected}) == 3
    assert all(item.marker in canonical for item in protected)
    assert [item.kind for item in protected] == ["other", "other", "formula"]
    assert restore_babeldoc_placeholders(canonical, marker_map) == source
    for item in protected:
        start = item.source_ref.char_start
        end = item.source_ref.char_end
        assert canonical[start:end] == item.marker


def test_source_bbox_matches_native_text_block(tmp_path: Path):
    source = tmp_path / "source.pdf"
    document = pymupdf.open()
    page = document.new_page(width=400, height=400)
    page.insert_textbox((50, 60, 350, 160), "A probability distribution assigns probabilities to outcomes.", fontsize=12)
    document.save(source)
    document.close()
    bbox, confidence = locate_source_bbox(source, 0, "A probability distribution assigns <b1>probabilities</b1> to outcomes.")
    assert bbox != (0.0, 0.0, 0.0, 0.0)
    assert confidence >= 0.75


def test_short_source_bbox_prefers_exact_text_location(tmp_path: Path):
    source = tmp_path / "short-source.pdf"
    document = pymupdf.open()
    page = document.new_page(width=400, height=400)
    page.insert_text((50, 80), "we can write the density function")
    page.insert_text((250, 250), "we cannot")
    document.save(source)
    document.close()
    bbox, confidence = locate_source_bbox(source, 0, "we can")
    assert bbox[0] < 100
    assert bbox[1] < 100
    assert confidence == 1.0


def test_content_matcher_handles_changed_order_and_repeated_text():
    expected = [
        {"sequence": 0, "babeldoc_text": "first", "unit_id": "u1", "marker_map": {}},
        {"sequence": 1, "babeldoc_text": "second", "unit_id": "u2", "marker_map": {}},
        {"sequence": 2, "babeldoc_text": "first", "unit_id": "u3", "marker_map": {}},
    ]
    matcher = ContentMatcher(expected)
    assert matcher.pop("second")["unit_id"] == "u2"
    assert matcher.pop("first")["unit_id"] == "u1"
    assert matcher.pop("first")["unit_id"] == "u3"
    assert matcher.pop("missing") is None
    assert matcher.remaining == 0


def test_reuse_capture_checks_files_before_loading_layout_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def fail_if_loaded():
        raise AssertionError("layout model must not load before capture validation")

    monkeypatch.setattr(babeldoc_adapter, "_load_layout_model", fail_if_loaded)
    with pytest.raises(FileNotFoundError, match="--reuse-capture"):
        babeldoc_adapter.run_smoke_document(
            tmp_path / "source.pdf",
            (41, 50, 576),
            load_config(Path("configs/v0.5.mock.toml")),
            tmp_path / "missing-capture",
            tmp_path / "runs",
            tmp_path / "capture",
            reuse_capture=True,
        )


def test_reused_capture_rejects_wrong_pdf_identity(tmp_path: Path):
    source = tmp_path / "source.pdf"
    source.write_bytes(b"pdf identity A")
    text = "A sentence."
    wrong = TranslationUnit(unit_id="u1", document_id="other-document", ir_revision="test", unit_type="paragraph", source_lang="en", target_lang="zh-CN", source_text=text, source_hash=stable_hash(text))
    with pytest.raises(ValueError, match="source PDF hash"):
        validate_capture_identity(source, (0,), [wrong], {0: [{"unit_id": "u1"}]})


def test_reused_capture_rejects_wrong_page_selection(tmp_path: Path):
    source = tmp_path / "source.pdf"
    source.write_bytes(b"pdf identity B")
    text = "A sentence."
    document_id = f"{source.stem}-{sha256_file(source)[:16]}"
    value = TranslationUnit(unit_id="u1", document_id=document_id, ir_revision="test", unit_type="paragraph", source_lang="en", target_lang="zh-CN", source_text=text, source_hash=stable_hash(text))
    with pytest.raises(ValueError, match="page set"):
        validate_capture_identity(source, (0,), [value], {1: [{"unit_id": "u1"}]})


def test_content_review_queue_keeps_a_blocked_translation_available(tmp_path: Path):
    text = "A sentence about probability."
    value = TranslationUnit(unit_id="u1", document_id="doc", ir_revision="test", unit_type="paragraph", source_lang="en", target_lang="zh-CN", source_text=text, source_hash=stable_hash(text))

    class FakeStore:
        def quality_reports(self, run_id):
            return [{"unit_id": "u1", "state": "needs_review", "quality": {"result_hash": "a" * 64, "issues": [{"category": "number"}]}}]

        def result(self, run_id, unit_id):
            return SimpleNamespace(draft=SimpleNamespace(translated_text="关于概率的一句话。"))

    output = tmp_path / "content-review.jsonl"
    _write_content_review_queue([value], FakeStore(), "run", output)
    row = json.loads(output.read_text(encoding="utf-8"))
    assert row["source_text"] == text
    assert row["translated_text"] == "关于概率的一句话。"
    assert row["meaning_equivalent_human_review"] is None
    assert row["state"] == "needs_review"


def test_content_review_queue_exposes_known_and_unknown_formula_literals(tmp_path: Path):
    source_text, protected, _ = canonicalize_babeldoc_text(
        "P(X <b1>) and P(Y <b2>)", page_index=0, sequence=0, bbox=(0, 0, 100, 20)
    )
    value = TranslationUnit(
        unit_id="u1", document_id="doc", ir_revision="test", unit_type="paragraph",
        source_lang="en", target_lang="zh-CN", source_text=source_text,
        source_hash=stable_hash(source_text), protected=protected,
        protected_literal_hints={protected[0].marker: "=1"},
    )

    class FakeStore:
        def quality_reports(self, run_id):
            return []

        def result(self, run_id, unit_id):
            return None

    output = tmp_path / "content-review.jsonl"
    _write_content_review_queue([value], FakeStore(), "run", output)
    row = json.loads(output.read_text(encoding="utf-8"))
    assert row["protected_literal_hints"] == {protected[0].marker: "=1"}
    assert row["unresolved_formula_markers"] == [protected[1].marker]
