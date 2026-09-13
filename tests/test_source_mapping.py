from pathlib import Path
import hashlib

import pymupdf
import pytest

from pdf_translation.source_mapping import (
    PlainFragmentMapping,
    editable_segments,
    terminal_immutable_suffix,
    map_placeholder_capture_fragment,
    map_single_line_mixed_native_prose,
    map_plain_capture_fragment,
    normalized_pdf_text,
    plan_single_line_fragment_insertions,
    plan_terminal_suffix_paragraph,
)
from pdf_translation.source_preserving import (
    SourcePreservingUnsupported,
    _changed_pixels_outside,
    _glyphs_outside,
    _located_segments,
    _remove_whole_show_operations,
)
from pdf_translation.pdf_fidelity import compare_visual_primitives


def test_normalization_keeps_number_and_operator_meaning():
    assert normalized_pdf_text("distri- butions") == normalized_pdf_text("distri-\nbutions")
    assert normalized_pdf_text("1.0") != normalized_pdf_text("10")
    assert normalized_pdf_text("P(X = 1)") != normalized_pdf_text("P(X = -1)")
    assert normalized_pdf_text("x- y") != normalized_pdf_text("xy")


def test_plain_capture_maps_exact_native_line_and_rejects_changed_number():
    document = pymupdf.open()
    page = document.new_page(width=400, height=200)
    page.insert_text((50, 80), "Value 1.0 is valid.", fontsize=12)
    bbox = tuple(page.search_for("Value 1.0 is valid.")[0])
    record = {
        "fragment_id": "f1", "bbox": bbox, "bbox_confidence": 1.0,
        "plain_text": "Value1.0 is valid.", "marker_map": {}, "protected_only": False,
    }
    mapping = map_plain_capture_fragment(page, record)
    assert mapping.line_count == 1
    assert mapping.segments[0].text == "Value 1.0 is valid."
    with pytest.raises(SourcePreservingUnsupported, match="captured text"):
        map_plain_capture_fragment(page, {**record, "plain_text": "Value 10 is valid."})
    document.close()


def test_placeholder_capture_maps_prose_around_unchanged_footnote():
    document = pymupdf.open()
    page = document.new_page(width=400, height=200)
    page.insert_text((50, 80), "A sentence.", fontsize=12)
    page.insert_text((130, 75), "5", fontsize=7)
    page.insert_text((140, 80), "There are", fontsize=12)
    record = {
        "fragment_id": "f2", "bbox": (45, 60, 230, 90), "bbox_confidence": 0.5,
        "babeldoc_text": "A sentence.<b1>There are", "marker_map": {"marker": "<b1>"},
        "protected_only": False,
    }
    mapping = map_placeholder_capture_fragment(page, record)
    assert [segment.text for segment in mapping.segments] == ["A sentence.", "There are"]
    with pytest.raises(SourcePreservingUnsupported, match="unsupported inline markup"):
        map_placeholder_capture_fragment(page, {**record, "babeldoc_text": "<i>A sentence.</i><b1>There are"})
    document.close()


def test_placeholder_capture_maps_real_pdf_line_break_hyphenation():
    document = pymupdf.open()
    page = document.new_page(width=400, height=200)
    page.insert_text((50, 75), "The nota-", fontsize=12)
    page.insert_text((50, 95), "tion is clear.", fontsize=12)
    page.insert_text((160, 95), "7", fontsize=7)
    record = {
        "fragment_id": "f3", "bbox": (45, 60, 240, 105), "bbox_confidence": 1.0,
        "babeldoc_text": "The nota- tion is clear.<b1>", "marker_map": {"marker": "<b1>"},
        "protected_only": False,
    }
    mapping = map_placeholder_capture_fragment(page, record)
    assert [segment.text for segment in mapping.segments] == ["The nota-", "tion is clear."]
    document.close()


def test_real_single_line_native_mapping_preserves_inline_formula_and_variable():
    source = Path("pdf/dm.pdf")
    expected_hash = "297196f2cad5ab23cf4855f815fd69e7b7b9ee9c31255f743b668416677ad24e"
    if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != expected_hash:
        pytest.skip("registered PDF source fixture is unavailable or has changed")
    record = {
        "fragment_id": "p0042-f6948205fd388",
        "bbox": (35.446998596191406, 567.2230224609375, 280.577392578125, 579.0684814453125),
        "bbox_confidence": 0.988764,
        "babeldoc_text": "we use P(x<b1>) to represent the probability that X is false.",
        "marker_map": {"protected-0": "<b1>"},
        "protected_only": False,
    }
    with pymupdf.open(source) as document:
        page = document[41]
        mapping = map_single_line_mixed_native_prose(page, record)
        assert [segment.text for segment in mapping.segments] == [
            "we use", "to represent the probability that", "is false.",
        ]
        rects = _located_segments(page, mapping.segments)
        edited, count = _remove_whole_show_operations(document.xref_stream(page.get_contents()[0]), page, rects)
        assert count == 3
        copied = pymupdf.open()
        try:
            copied.insert_pdf(document, from_page=41, to_page=41)
            target = copied[0]
            copied.update_stream(target.get_contents()[0], edited)
            assert all(not target.get_textbox(rect).strip() for rect in rects)
            assert _glyphs_outside(page, tuple(rects)) == _glyphs_outside(target, tuple(rects))
            assert compare_visual_primitives(page, target)["passed"]
            mask = tuple(pymupdf.Rect(r.x0 - 0.5, r.y0 - 0.5, r.x1 + 0.5, r.y1 + 0.5) for r in rects)
            assert _changed_pixels_outside(page, target, mask)[0] == 0
        finally:
            copied.close()
        with pytest.raises(SourcePreservingUnsupported, match="not uniquely aligned"):
            map_single_line_mixed_native_prose(
                document[41], {**record, "babeldoc_text": record["babeldoc_text"].replace("P(x", "P(y")},
            )
        with pytest.raises(SourcePreservingUnsupported, match="confidence is too low"):
            map_single_line_mixed_native_prose(document[41], {**record, "bbox_confidence": 0.2})
        with pytest.raises(SourcePreservingUnsupported, match="exactly one protected marker"):
            map_single_line_mixed_native_prose(
                document[41], {**record, "marker_map": {"first": "<b1>", "second": "<b2>"}},
            )


def test_single_line_planner_uses_saved_target_and_native_geometry():
    font = Path("C:/Windows/Fonts/simfang.ttf")
    if not font.is_file():
        pytest.skip("Chinese font fixture is unavailable")
    document = pymupdf.open()
    page = document.new_page(width=400, height=200)
    page.insert_text((50, 80), "Alpha beta.", fontsize=12)
    bbox = tuple(page.search_for("Alpha beta.")[0])
    record = {
        "fragment_id": "f1", "bbox": bbox, "bbox_confidence": 1.0,
        "plain_text": "Alpha beta.", "marker_map": {}, "protected_only": False,
        "unit_id": "u1", "group_index": 0, "group_size": 1,
    }
    segments, insertions = plan_single_line_fragment_insertions(
        page, (record,), {"f1": "甲乙。"}, font, fontsize=12
    )
    assert len(segments) == len(insertions) == 1
    assert insertions[0].text == "甲乙。"
    assert insertions[0].origin[0] == pytest.approx(bbox[0])
    with pytest.raises(SourcePreservingUnsupported, match="exceeds first source line"):
        plan_single_line_fragment_insertions(page, (record,), {"f1": "很长很长的中文译文超过原文行宽。"}, font, fontsize=12)
    with pytest.raises(SourcePreservingUnsupported, match="semantic group is incomplete"):
        plan_single_line_fragment_insertions(page, ({**record, "group_size": 2},), {"f1": "甲乙。"}, font, fontsize=12)
    document.close()


def test_section_number_remains_native_and_is_not_inserted_twice(monkeypatch):
    font = Path("C:/Windows/Fonts/simfang.ttf")
    if not font.is_file():
        pytest.skip("Chinese font fixture is unavailable")
    document = pymupdf.open()
    page = document.new_page(width=400, height=200)
    page.insert_text((50, 80), "2.2", fontsize=12)
    page.insert_text((85, 80), "Probability distributions", fontsize=12)
    from pdf_translation.source_preserving import ExactSourceSegment
    mapping = PlainFragmentMapping(
        "f1",
        (
            ExactSourceSegment("2.2", (45, 60, 75, 90)),
            ExactSourceSegment("Probability distributions", (80, 60, 250, 90)),
        ),
        2,
        1.0,
    )
    assert [segment.text for segment in editable_segments(mapping)] == ["Probability distributions"]
    monkeypatch.setattr("pdf_translation.source_mapping.map_plain_capture_fragment", lambda _page, _record: mapping)
    record = {
        "fragment_id": "f1", "unit_id": "u1", "group_index": 0, "group_size": 1,
        "bbox": (45, 60, 250, 90), "bbox_confidence": 1.0,
        "plain_text": "2.2 Probability distributions", "marker_map": {}, "protected_only": False,
    }
    segments, insertions = plan_single_line_fragment_insertions(
        page, (record,), {"f1": "2.2 概率分布"}, font, fontsize=12
    )
    assert [segment.text for segment in segments] == ["Probability distributions"]
    assert insertions[0].text == "概率分布"
    with pytest.raises(SourcePreservingUnsupported, match="changed an immutable"):
        plan_single_line_fragment_insertions(page, (record,), {"f1": "2.3 概率分布"}, font, fontsize=12)
    document.close()


def test_terminal_number_and_colon_are_excluded_from_editable_prose():
    from pdf_translation.source_preserving import ExactSourceSegment
    mapping = PlainFragmentMapping(
        "f1",
        (ExactSourceSegment("The masses must sum to 1:", (20, 20, 250, 45)),),
        1,
        1.0,
    )
    assert terminal_immutable_suffix(mapping) == "1:"
    assert [segment.text for segment in editable_segments(mapping)] == ["The masses must sum to"]


def test_two_line_paragraph_reflows_around_unchanged_terminal_number():
    font = Path("C:/Windows/Fonts/simfang.ttf")
    if not font.is_file():
        pytest.skip("Chinese font fixture is unavailable")
    document = pymupdf.open()
    page = document.new_page(width=500, height=200)
    page.insert_text((40, 70), "There are constraints on probability masses.", fontsize=12)
    page.insert_text((40, 90), "The masses must sum to", fontsize=12)
    page.insert_text((180, 90), "1:", fontsize=12)
    record = {
        "fragment_id": "f1", "unit_id": "u1", "group_index": 0, "group_size": 1,
        "bbox": (35, 55, 350, 100), "bbox_confidence": 1.0,
        "plain_text": "There are constraints on probability masses. The masses must sum to 1:",
        "marker_map": {}, "protected_only": False,
    }
    segments, insertions = plan_terminal_suffix_paragraph(
        page, record, "概率质量受到约束。质量之和为 1：", font, fontsize=10
    )
    assert "".join(item.text for item in insertions) == "概率质量受到约束。质量之和为"
    assert insertions[-1].text.endswith("为")
    assert segments[-1].text == "The masses must sum to"
    with pytest.raises(SourcePreservingUnsupported, match="changed the original terminal"):
        plan_terminal_suffix_paragraph(page, record, "概率质量受到约束。质量之和为 2：", font)
    with pytest.raises(SourcePreservingUnsupported, match="missing text or unresolved"):
        plan_terminal_suffix_paragraph(page, record, "P mass 受到约束。质量之和为 1：", font)
    document.close()
