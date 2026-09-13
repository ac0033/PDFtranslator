from __future__ import annotations

from collections import defaultdict
from numbers import Real
from pathlib import Path

import pymupdf


VISUAL_DRAWING_KEYS = (
    "type", "rect", "items", "color", "fill", "width", "dashes",
    "lineCap", "lineJoin", "closePath", "fill_opacity", "stroke_opacity",
)
COORDINATE_TOLERANCE_PT = 0.001
FORMULA_FONT_HINTS = ("cmr", "cmmi", "cmsy", "pazomath", "stix", "math", "symbol")
IMMUTABLE_SYMBOLS = frozenset("©=<>≤≥∑∏∫√∞≠±×÷∼≈→←↔∈∉∅∂∇∨∧¬⊆⊂⊇⊃∪∩∝∀∃")


def _stable_value(value):
    """Convert MuPDF geometry to comparable nested values."""
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, Real):
        return float(value)
    if isinstance(value, (pymupdf.Rect, pymupdf.Point, pymupdf.Quad)):
        return tuple(_stable_value(part) for part in value)
    if isinstance(value, (tuple, list)):
        return tuple(_stable_value(part) for part in value)
    return str(value)


def _close_value(source, target) -> bool:
    if isinstance(source, bool) or isinstance(target, bool):
        return source is target
    if isinstance(source, Real) and isinstance(target, Real):
        return abs(float(source) - float(target)) <= COORDINATE_TOLERANCE_PT
    if isinstance(source, tuple) and isinstance(target, tuple):
        return len(source) == len(target) and all(_close_value(a, b) for a, b in zip(source, target))
    return source == target


def _drawing_inventory(page) -> list[tuple]:
    return [tuple(_stable_value(path.get(key)) for key in VISUAL_DRAWING_KEYS) for path in page.get_drawings()]


def _image_inventory(page) -> dict[tuple, list[tuple]]:
    positions: dict[tuple, list[tuple]] = defaultdict(list)
    for image in page.get_image_info(hashes=True):
        key = (image["digest"].hex(), image["width"], image["height"])
        positions[key].append(tuple(float(value) for value in image["bbox"]))
    return {key: sorted(boxes) for key, boxes in positions.items()}


def _images_equal(source: dict[tuple, list[tuple]], target: dict[tuple, list[tuple]]) -> bool:
    return source.keys() == target.keys() and all(
        len(source[key]) == len(target[key])
        and all(_close_value(a, b) for a, b in zip(source[key], target[key]))
        for key in source
    )


def _formula_font_glyphs(page) -> dict[tuple[str, str], list[tuple]]:
    """Locate source glyphs set in recognizable math fonts at character precision."""
    grouped: dict[tuple[str, str], list[tuple]] = defaultdict(list)
    for block in page.get_text("rawdict")["blocks"]:
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                font = span.get("font", "")
                if not any(hint in font.casefold() for hint in FORMULA_FONT_HINTS):
                    continue
                for character in span.get("chars", []):
                    if character.get("c", "").isspace():
                        continue
                    grouped[(font, character["c"])].append(
                        (float(span["size"]), tuple(float(value) for value in character["bbox"]))
                    )
    return {key: sorted(values) for key, values in grouped.items()}


def _formula_font_glyphs_equal(source: dict[tuple, list[tuple]], target: dict[tuple, list[tuple]]) -> bool:
    return source.keys() == target.keys() and all(
        len(source[key]) == len(target[key])
        and all(_close_value(a, b) for a, b in zip(source[key], target[key]))
        for key in source
    )


def _numeric_symbol_glyphs(page) -> dict[tuple[str, str], list[tuple]]:
    """Track page numbers, footnotes, values and standalone math symbols in any font."""
    grouped: dict[tuple[str, str], list[tuple]] = defaultdict(list)
    for block in page.get_text("rawdict")["blocks"]:
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                font = span.get("font", "")
                for character in span.get("chars", []):
                    glyph = character.get("c", "")
                    if not glyph or not (
                        glyph.isdigit()
                        or glyph in IMMUTABLE_SYMBOLS
                        or any("\u0370" <= letter <= "\u03ff" for letter in glyph)
                    ):
                        continue
                    grouped[(font, glyph)].append(
                        (float(span["size"]), tuple(float(value) for value in character["bbox"]))
                    )
    return {key: sorted(values) for key, values in grouped.items()}


def compare_visual_primitives(source_page, translated_page) -> dict:
    """Compare page geometry, raster images and vector paths without text claims."""
    source_size = tuple(_stable_value(value) for value in source_page.rect)
    target_size = tuple(_stable_value(value) for value in translated_page.rect)
    source_images = _image_inventory(source_page)
    target_images = _image_inventory(translated_page)
    source_paths = _drawing_inventory(source_page)
    target_paths = _drawing_inventory(translated_page)
    source_formula_glyphs = _formula_font_glyphs(source_page)
    target_formula_glyphs = _formula_font_glyphs(translated_page)
    source_numeric_symbols = _numeric_symbol_glyphs(source_page)
    target_numeric_symbols = _numeric_symbol_glyphs(translated_page)
    checks = {
        "page_geometry_equal": _close_value(source_size, target_size),
        "images_digest_and_bbox_equal": _images_equal(source_images, target_images),
        "vector_paths_equal": len(source_paths) == len(target_paths) and all(
            _close_value(a, b) for a, b in zip(source_paths, target_paths)
        ),
        "formula_font_glyphs_text_font_size_bbox_equal": _formula_font_glyphs_equal(source_formula_glyphs, target_formula_glyphs),
        "numeric_symbol_glyphs_text_font_size_bbox_equal": _formula_font_glyphs_equal(source_numeric_symbols, target_numeric_symbols),
    }
    return {
        "checks": checks,
        "source_image_count": sum(len(boxes) for boxes in source_images.values()),
        "target_image_count": sum(len(boxes) for boxes in target_images.values()),
        "source_vector_path_count": len(source_paths),
        "target_vector_path_count": len(target_paths),
        "source_formula_font_glyph_count": sum(len(values) for values in source_formula_glyphs.values()),
        "target_formula_font_glyph_count": sum(len(values) for values in target_formula_glyphs.values()),
        "source_numeric_symbol_glyph_count": sum(len(values) for values in source_numeric_symbols.values()),
        "target_numeric_symbol_glyph_count": sum(len(values) for values in target_numeric_symbols.values()),
        "passed": all(checks.values()),
    }


def audit_pdf_primitives(source: Path, translated: Path, source_page_indices: tuple[int, ...]) -> dict:
    """Audit a mono translated sample against corresponding physical source pages."""
    with pymupdf.open(source) as source_pdf, pymupdf.open(translated) as target_pdf:
        if len(target_pdf) != len(source_page_indices):
            raise ValueError("translated PDF page count does not match the selected source pages")
        if len(set(source_page_indices)) != len(source_page_indices):
            raise ValueError("source page indices must be unique")
        if any(index < 0 or index >= len(source_pdf) for index in source_page_indices):
            raise ValueError("source page index is out of range")
        pages = []
        for target_index, source_index in enumerate(source_page_indices):
            comparison = compare_visual_primitives(source_pdf[source_index], target_pdf[target_index])
            pages.append({"source_page": source_index + 1, "translated_page": target_index + 1, **comparison})
    return {
        "schema_version": "1.0",
        "scope": "page geometry, raster images, vector paths, recognizable math-font glyphs, and numeric/symbol glyph text/font/size/position",
        "coordinate_tolerance_pt": COORDINATE_TOLERANCE_PT,
        "pages": pages,
        "visual_primitives_passed": all(page["passed"] for page in pages),
        "full_non_text_fidelity_verified": False,
        "unverified": [
            "Latin formula variables and symbols outside the recognizable math-font/numeric-symbol lists",
            "original font, size and position of other unchanged text objects",
            "pixel-level rendering outside translated text regions",
        ],
    }
