from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest

from pdf_translation.source_preserving import (
    SourcePreservingUnsupported,
    ExactSourceSegment,
    TargetText,
    _changed_pixels_outside,
    _assert_inserted_text,
    _assert_interleaved_reading_order,
    _inline_before_preserved_suffix,
    _inline_each_removed_operation,
    _remove_whole_show_operations,
    write_source_preserving_page,
)


def test_text_operation_editor_removes_only_selected_tj_bytes():
    stream = (
        b"BT\n/F1 10 Tf\n"
        b"1 0 0 1 10 100 Tm [(Hello)]TJ\n"
        b"1 0 0 1 60 100 Tm [(KEEP)]TJ\nET"
    )
    page = SimpleNamespace(rotation=0, transformation_matrix=pymupdf.Identity)
    edited, removed = _remove_whole_show_operations(stream, page, [pymupdf.Rect(9, 99, 50, 110)])
    assert removed == 1
    assert b"1 0 0 1 10 100 Tm []TJ" in edited
    assert b"1 0 0 1 60 100 Tm [(KEEP)]TJ" in edited


def test_text_operation_editor_fails_closed_when_target_is_not_isolated():
    page = SimpleNamespace(rotation=0, transformation_matrix=pymupdf.Identity)
    with pytest.raises(SourcePreservingUnsupported, match="no isolated TJ"):
        _remove_whole_show_operations(b"1 0 0 1 60 100 Tm [(KEEP)]TJ", page, [pymupdf.Rect(0, 0, 20, 20)])


def test_text_operation_editor_rejects_overlapping_target_regions():
    page = SimpleNamespace(rotation=0, transformation_matrix=pymupdf.Identity)
    with pytest.raises(SourcePreservingUnsupported, match="multiple target segments"):
        _remove_whole_show_operations(
            b"1 0 0 1 10 100 Tm [(Hello)]TJ",
            page,
            [pymupdf.Rect(0, 90, 20, 110), pymupdf.Rect(5, 90, 30, 110)],
        )


def test_text_operation_editor_rejects_math_glyph_at_selected_operator_origin():
    stream = b"BT\n/F1 10 Tf\n1 0 0 1 10 100 Tm [(P)]TJ\nET"
    page = SimpleNamespace(
        rotation=0,
        transformation_matrix=pymupdf.Identity,
        get_texttrace=lambda: ({"font": "CMR10", "chars": ((80, 1, (10, 100), (10, 90, 16, 101)),)},),
    )
    with pytest.raises(SourcePreservingUnsupported, match="contains an immutable formula or symbol"):
        _remove_whole_show_operations(stream, page, [pymupdf.Rect(9, 99, 20, 110)])


def test_text_operation_editor_rejects_protected_glyph_after_plain_first_glyph():
    stream = b"BT\n/F1 10 Tf\n1 0 0 1 10 100 Tm [(Value 1)]TJ\nET"
    page = SimpleNamespace(
        rotation=0,
        transformation_matrix=pymupdf.Identity,
        get_texttrace=lambda: ({
            "font": "TeXGyrePagella-Regular",
            "chars": ((86, 1, (10, 100), (10, 90, 16, 101)), (49, 2, (48, 100), (48, 90, 54, 101))),
        },),
    )
    with pytest.raises(SourcePreservingUnsupported, match="contains an immutable formula or symbol"):
        _remove_whole_show_operations(stream, page, [pymupdf.Rect(9, 99, 60, 110)])


def test_text_operation_editor_rejects_ambiguous_glyph_provenance():
    stream = b"BT\n/F1 10 Tf\n1 0 0 1 10 100 Tm [(Hello)]TJ\nET"
    span = {"font": "Helvetica", "chars": ((72, 1, (10, 100), (10, 90, 16, 101)),)}
    page = SimpleNamespace(
        rotation=0,
        transformation_matrix=pymupdf.Identity,
        get_texttrace=lambda: (span, span),
    )
    with pytest.raises(SourcePreservingUnsupported, match="not uniquely linked"):
        _remove_whole_show_operations(stream, page, [pymupdf.Rect(9, 99, 20, 110)])


def test_text_operation_editor_separates_coalesced_trace_at_next_pdf_operation():
    stream = (
        b"BT\n/F1 10 Tf\n1 0 0 1 10 100 Tm [(Hello)]TJ\n"
        b"1 0 0 1 10 80 Tm [(1)]TJ\nET"
    )
    page = SimpleNamespace(
        rotation=0,
        transformation_matrix=pymupdf.Identity,
        get_texttrace=lambda: ({
            "font": "Helvetica",
            "chars": ((72, 1, (10, 100), (10, 90, 16, 101)), (49, 2, (10, 80), (10, 70, 16, 81))),
        },),
    )
    edited, removed = _remove_whole_show_operations(stream, page, [pymupdf.Rect(9, 99, 20, 110)])
    assert removed == 1
    assert b"10 100 Tm []TJ" in edited
    assert b"10 80 Tm [(1)]TJ" in edited


def test_pixel_audit_rejects_change_outside_target_rectangle():
    white = bytes([255] * 12)
    changed = bytes([255, 255, 255, 0, 0, 0, 255, 255, 255, 255, 255, 255])
    source = SimpleNamespace(get_pixmap=lambda **_: SimpleNamespace(width=2, height=2, n=3, samples=white))
    target = SimpleNamespace(get_pixmap=lambda **_: SimpleNamespace(width=2, height=2, n=3, samples=changed))
    count, sample = _changed_pixels_outside(source, target, (pymupdf.Rect(0, 0, 0.5, 0.5),), scale=1)
    assert count == 1
    assert sample == [(1.0, 0.0)]


def test_inserted_text_audit_rejects_missing_text_and_suffix_before_translation():
    font = Path("C:/Windows/Fonts/simfang.ttf")
    target = TargetText("甲乙。", (10, 20), (9, 9, 60, 25), font, "zh-fangsong", 10)
    suffix = ExactSourceSegment("1:", (60, 9, 75, 25))
    missing = SimpleNamespace(get_textbox=lambda _rect: "1:")
    with pytest.raises(SourcePreservingUnsupported, match="missing or clipped"):
        _assert_inserted_text(missing, (target,), suffix, pymupdf.Rect(60, 9, 75, 25))
    wrong_order = SimpleNamespace(get_textbox=lambda _rect: "1:甲乙。")
    with pytest.raises(SourcePreservingUnsupported, match="precedes translated prose"):
        _assert_inserted_text(wrong_order, (target,), suffix, pymupdf.Rect(60, 9, 75, 25))


def test_inline_writer_rejects_protected_text_between_target_operations():
    stream = (
        b"BT\n1 0 0 1 10 100 Tm []TJ\n"
        b"1 0 0 1 15 100 Tm [(KEEP)]TJ\n"
        b"1 0 0 1 20 100 Tm []TJ\n"
        b"/F1 10 Tf\n1 0 0 1 30 100 Tm [(1:)]TJ\nET\n"
    )
    document = SimpleNamespace(xref_stream=lambda _xref: stream)
    page = SimpleNamespace(transformation_matrix=pymupdf.Identity)
    with pytest.raises(SourcePreservingUnsupported, match="protected text occurs between"):
        _inline_before_preserved_suffix(
            document, page, 1,
            [pymupdf.Rect(9, 99, 11, 101), pymupdf.Rect(19, 99, 21, 101)],
            pymupdf.Rect(29, 99, 35, 101), (), 2,
        )


def test_interleaved_writer_rejects_missing_original_font_reset():
    stream = (
        b"BT\n/F1 10 Tf\n1 0 0 1 10 100 Tm []TJ\n"
        b"1 0 0 1 20 100 Tm [(KEEP)]TJ\nET\n"
    )
    document = SimpleNamespace(xref_stream=lambda _xref: stream)
    page = SimpleNamespace(transformation_matrix=pymupdf.Identity)
    item = TargetText("甲", (10, 100), (9, 90, 20, 110), Path("C:/Windows/Fonts/simfang.ttf"), "zh-fangsong", 10)
    with pytest.raises(SourcePreservingUnsupported, match="explicit font reset"):
        _inline_each_removed_operation(
            document, page, 1, [pymupdf.Rect(9, 99, 12, 102)], (item,), 1
        )


def test_interleaved_reading_order_rejects_original_anchor_before_chinese():
    font = Path("C:/Windows/Fonts/simfang.ttf")
    item = TargetText("甲", (10, 100), (9, 90, 20, 110), font, "zh-fangsong", 10)
    page = SimpleNamespace(get_textbox=lambda _rect: "1甲")
    with pytest.raises(SourcePreservingUnsupported, match="wrong extraction order"):
        _assert_interleaved_reading_order(page, (item,), ("甲", "1"))


def test_interleaved_reading_order_accepts_repeated_chinese_phrase_occurrences():
    font = Path("C:/Windows/Fonts/simfang.ttf")
    first = TargetText("甲", (10, 100), (9, 90, 20, 110), font, "zh-fangsong", 10)
    second = TargetText("甲", (30, 100), (29, 90, 40, 110), font, "zh-fangsong", 10)
    page = SimpleNamespace(get_textbox=lambda _rect: "甲1甲")
    _assert_interleaved_reading_order(page, (first, second), ("甲", "1", "甲"))
    with pytest.raises(ValueError, match="each Chinese phrase occurrence"):
        _assert_interleaved_reading_order(page, (first, second), ("甲", "1"))


def test_source_page_copy_replaces_only_one_text_operation(tmp_path: Path):
    font = Path("C:/Windows/Fonts/simfang.ttf")
    if not font.is_file():
        pytest.skip("Chinese font fixture is unavailable")
    source = tmp_path / "source.pdf"
    target = tmp_path / "target.pdf"
    document = pymupdf.open()
    page = document.new_page(width=400, height=400)
    page.insert_font(fontname="helv")
    content_xref = document.get_new_xref()
    document.update_object(content_xref, "<<>>")
    document.update_stream(
        content_xref,
        b"BT /helv 12 Tf 1 0 0 1 50 320 Tm [(Alpha beta.)]TJ "
        b"1 0 0 1 50 290 Tm [(P(X)=1)]TJ ET "
        b"q 0 0 1 RG 50 240 50 20 re S Q",
    )
    page.set_contents(content_xref)
    document.save(source)
    document.close()
    result = write_source_preserving_page(
        source,
        0,
        target,
        (ExactSourceSegment("Alpha beta.", (40, 65, 200, 90)),),
        (TargetText("甲乙。", (50, 80), (49, 65, 200, 85), font, "zh-fangsong", 12),),
    )
    assert result["removed_text_operators"] == 1
    assert result["changed_pixels_outside_target_at_2x"] == 0
    with pymupdf.open(source) as before, pymupdf.open(target) as after:
        assert before[0].search_for("P(X)=1") == after[0].search_for("P(X)=1")
        assert "Alpha beta." not in after[0].get_text()
        assert "甲乙。" in after[0].get_text()


def test_source_preserving_writer_refuses_to_overwrite_existing_output(tmp_path: Path):
    existing = tmp_path / "important.pdf"
    existing.write_bytes(b"existing user artifact")
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        write_source_preserving_page(tmp_path / "missing-source.pdf", 0, existing, (), ())
    assert existing.read_bytes() == b"existing user artifact"


def test_inline_chinese_precedes_unmoved_numeric_suffix_in_pdf_text_order(tmp_path: Path):
    font = Path("C:/Windows/Fonts/simfang.ttf")
    if not font.is_file():
        pytest.skip("Chinese font fixture is unavailable")
    source = tmp_path / "source.pdf"
    target = tmp_path / "target.pdf"
    document = pymupdf.open()
    page = document.new_page(width=400, height=400)
    page.insert_font(fontname="helv")
    content_xref = document.get_new_xref()
    document.update_object(content_xref, "<<>>")
    document.update_stream(
        content_xref,
        b"BT\n/helv 12 Tf\n1 0 0 1 50 320 Tm [(Alpha beta.)]TJ\n"
        b"1 0 0 1 50 300 Tm [(Sum to)]TJ\n/helv 12 Tf\n"
        b"1 0 0 1 150 300 Tm [(1:)]TJ\nET\n",
    )
    page.set_contents(content_xref)
    document.save(source)
    document.close()
    result = write_source_preserving_page(
        source, 0, target,
        (ExactSourceSegment("Alpha beta.", (40, 65, 140, 90)), ExactSourceSegment("Sum to", (40, 85, 110, 110))),
        (
            TargetText("甲乙。", (50, 80), (49, 65, 140, 85), font, "zh-fangsong", 12),
            TargetText("合计为", (50, 100), (49, 85, 145, 105), font, "zh-fangsong", 12),
        ),
        ExactSourceSegment("1:", (145, 85, 170, 110)),
    )
    assert result["reading_order_in_source_stream"]
    with pymupdf.open(source) as before, pymupdf.open(target) as after:
        text = after[0].get_textbox(pymupdf.Rect(40, 65, 175, 110))
        assert text.index("甲乙。") < text.index("合计为") < text.index("1:")
        assert before[0].search_for("1:") == after[0].search_for("1:")


def test_interleaved_chinese_preserves_number_between_source_operations(tmp_path: Path):
    font = Path("C:/Windows/Fonts/simfang.ttf")
    if not font.is_file():
        pytest.skip("Chinese font fixture is unavailable")
    source = tmp_path / "source.pdf"
    target = tmp_path / "target.pdf"
    document = pymupdf.open()
    page = document.new_page(width=400, height=400)
    page.insert_font(fontname="helv")
    content_xref = document.get_new_xref()
    document.update_object(content_xref, "<<>>")
    document.update_stream(
        content_xref,
        b"BT\n/helv 12 Tf\n1 0 0 1 50 320 Tm [(Hello)]TJ\n"
        b"/helv 12 Tf\n1 0 0 1 110 320 Tm [(1)]TJ\n"
        b"/helv 12 Tf\n1 0 0 1 125 320 Tm [(world)]TJ\n"
        b"/helv 12 Tf\n1 0 0 1 250 320 Tm [(END)]TJ\nET\n",
    )
    page.set_contents(content_xref)
    document.save(source)
    document.close()
    result = write_source_preserving_page(
        source, 0, target,
        (ExactSourceSegment("Hello", (45, 65, 85, 90)), ExactSourceSegment("world", (120, 65, 170, 90))),
        (
            TargetText("甲", (50, 80), (49, 65, 75, 85), font, "zh-fangsong", 12),
            TargetText("乙", (125, 80), (124, 65, 150, 85), font, "zh-fangsong", 12),
        ),
        interleaved_reading_order=("甲", "1", "乙"),
    )
    assert result["reading_order_in_source_stream"]
    assert result["inline_appearance_identical_at_2x"]
    with pymupdf.open(source) as before, pymupdf.open(target) as after:
        text = after[0].get_textbox(pymupdf.Rect(45, 65, 155, 90))
        assert text.index("甲") < text.index("1") < text.index("乙")
        assert before[0].search_for("1") == after[0].search_for("1")


def test_interleaved_chinese_allows_consecutive_emptied_operations_without_font_reset(tmp_path: Path):
    font = Path("C:/Windows/Fonts/simfang.ttf")
    if not font.is_file():
        pytest.skip("Chinese font fixture is unavailable")
    source = tmp_path / "source.pdf"
    target = tmp_path / "target.pdf"
    document = pymupdf.open()
    page = document.new_page(width=400, height=400)
    page.insert_font(fontname="helv")
    content_xref = document.get_new_xref()
    document.update_object(content_xref, "<<>>")
    document.update_stream(
        content_xref,
        b"BT\n/helv 12 Tf\n1 0 0 1 50 320 Tm [(Hello)]TJ\n"
        b"1 0 0 1 100 320 Tm [(world)]TJ\n"
        b"/helv 12 Tf\n1 0 0 1 180 320 Tm [(1)]TJ\nET\n",
    )
    page.set_contents(content_xref)
    document.save(source)
    document.close()
    result = write_source_preserving_page(
        source, 0, target,
        (ExactSourceSegment("Hello", (45, 65, 85, 90)), ExactSourceSegment("world", (95, 65, 145, 90))),
        (
            TargetText("甲", (50, 80), (49, 65, 75, 85), font, "zh-fangsong", 12),
            TargetText("乙", (100, 80), (99, 65, 125, 85), font, "zh-fangsong", 12),
        ),
        interleaved_reading_order=("甲", "乙"),
    )
    assert result["reading_order_in_source_stream"]
    with pymupdf.open(source) as before, pymupdf.open(target) as after:
        text = after[0].get_textbox(pymupdf.Rect(45, 65, 195, 90))
        assert text.index("甲") < text.index("乙") < text.index("1")
        assert before[0].search_for("1") == after[0].search_for("1")
