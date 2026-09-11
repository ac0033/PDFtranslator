from pathlib import Path

import pymupdf
import pytest

from pdf_translation import babeldoc_adapter
from pdf_translation.babeldoc_adapter import ContentMatcher, canonicalize_babeldoc_text, locate_source_bbox, restore_babeldoc_placeholders
from pdf_translation.config import load_config


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
