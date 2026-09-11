from pathlib import Path

import pymupdf

from pdf_translation.babeldoc_adapter import ContentMatcher, canonicalize_babeldoc_text, locate_source_bbox, restore_babeldoc_placeholders


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
