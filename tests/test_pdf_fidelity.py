from pdf_translation.pdf_fidelity import compare_visual_primitives


class FakePage:
    rect = (0, 0, 576, 648)

    def __init__(self, *, image_x: float = 20, drawing_x: float = 4, formula_x: float = 30, digit_x: float = 80) -> None:
        self.image_x = image_x
        self.drawing_x = drawing_x
        self.formula_x = formula_x
        self.digit_x = digit_x

    def get_image_info(self, *, hashes: bool):
        assert hashes
        return [{"bbox": (self.image_x, 30, self.image_x + 10, 40), "digest": b"same image", "width": 100, "height": 100}]

    def get_drawings(self):
        return [{"type": "s", "rect": (self.drawing_x, 2, 10, 10), "items": [("l", (self.drawing_x, 2), (10, 10))], "color": (0, 0, 0), "width": 1}]

    def get_text(self, format: str):
        assert format == "rawdict"
        return {"blocks": [{"lines": [{"spans": [
            {"font": "CMR10", "size": 10, "chars": [{"c": "=", "bbox": (self.formula_x, 20, self.formula_x + 4, 30)}]},
            {"font": "TeXGyrePagella-Regular", "size": 8, "chars": [{"c": "5", "bbox": (self.digit_x, 20, self.digit_x + 4, 28)}]},
        ]}]}]}


def test_visual_primitive_audit_accepts_renderer_roundoff():
    result = compare_visual_primitives(FakePage(), FakePage(image_x=20.00002, drawing_x=4.00002))
    assert result["passed"]
    assert result["source_image_count"] == 1


def test_visual_primitive_audit_uses_numeric_tolerance_not_rounding_bins():
    result = compare_visual_primitives(FakePage(drawing_x=4.00049), FakePage(drawing_x=4.00051))
    assert result["passed"]


def test_visual_primitive_audit_detects_moved_image_or_drawing():
    moved_image = compare_visual_primitives(FakePage(), FakePage(image_x=21))
    moved_drawing = compare_visual_primitives(FakePage(), FakePage(drawing_x=5))
    assert not moved_image["checks"]["images_digest_and_bbox_equal"]
    assert not moved_drawing["checks"]["vector_paths_equal"]


def test_visual_primitive_audit_blocks_formula_glyph_movement():
    result = compare_visual_primitives(FakePage(), FakePage(formula_x=31))
    assert not result["passed"]
    assert not result["checks"]["formula_font_glyphs_text_font_size_bbox_equal"]
    assert result["source_formula_font_glyph_count"] == 1


def test_visual_primitive_audit_blocks_footnote_digit_movement_in_ordinary_font():
    result = compare_visual_primitives(FakePage(), FakePage(digit_x=81))
    assert not result["passed"]
    assert not result["checks"]["numeric_symbol_glyphs_text_font_size_bbox_equal"]
    assert result["source_numeric_symbol_glyph_count"] == 2
