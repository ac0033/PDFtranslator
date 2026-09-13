"""Fail-closed editing of simple PDF text-show operations on a source-page copy.

This supports only pages whose target text can be isolated as whole TJ operations
with explicit Tm positions in one content stream. Unsupported pages must use a
different editor; they must never silently fall back to whole-page replay.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import os
from pathlib import Path
import re
import tempfile

import pymupdf

from .pdf_fidelity import FORMULA_FONT_HINTS, IMMUTABLE_SYMBOLS, compare_visual_primitives


SHOW_OPERATION = re.compile(rb"([0-9.]+) 0 0 1 ([0-9.]+) ([0-9.]+) Tm \[(.*?)\]TJ", re.S)
# At 2x rendering, 0.5 pt is exactly one output pixel around source glyph bboxes.
# Glyph, image and vector checks remain unmasked and exact outside edit slots.
RASTER_ANTIALIAS_MARGIN_PT = 0.5


class SourcePreservingUnsupported(RuntimeError):
    """The source page cannot be edited without risking other page objects."""


@dataclass(frozen=True)
class ExactSourceSegment:
    text: str
    search_clip: tuple[float, float, float, float]


@dataclass(frozen=True)
class TargetText:
    text: str
    origin: tuple[float, float]
    allowed_rect: tuple[float, float, float, float]
    fontfile: Path
    fontname: str
    fontsize: float


def _located_segments(page: pymupdf.Page, segments: tuple[ExactSourceSegment, ...]) -> list[pymupdf.Rect]:
    if not segments:
        raise ValueError("at least one exact source segment is required")
    result = []
    for segment in segments:
        clip = pymupdf.Rect(segment.search_clip)
        found = [rect for rect in page.search_for(segment.text, clip=clip) if clip.contains(rect)]
        if len(found) != 1 or page.get_textbox(found[0]) != segment.text:
            raise SourcePreservingUnsupported(f"source text is not uniquely and exactly located: {segment.text!r}")
        result.append(found[0])
    return result


def _glyph_is_in_rects(character: tuple, rects: tuple[pymupdf.Rect, ...]) -> bool:
    bbox = pymupdf.Rect(character[3])
    origin = pymupdf.Point(character[2])
    return any(rect.intersects(bbox) or rect.contains(origin) for rect in rects)


def _glyphs_outside(page: pymupdf.Page, rects: tuple[pymupdf.Rect, ...]) -> Counter:
    return Counter(
        (character[0], span["font"], span["size"], tuple(character[2]), tuple(character[3]))
        for span in page.get_texttrace()
        for character in span["chars"]
        if not _glyph_is_in_rects(character, rects)
    )


def _remove_whole_show_operations(stream: bytes, page: pymupdf.Page, rects: list[pymupdf.Rect]) -> tuple[bytes, int]:
    if page.rotation != 0:
        raise SourcePreservingUnsupported("rotated pages are not supported by this text operator editor")
    traced_spans = tuple(span for span in page.get_texttrace() if span["chars"]) if hasattr(page, "get_texttrace") else None
    operations = tuple(SHOW_OPERATION.finditer(stream))
    operation_index = {match.start(): index for index, match in enumerate(operations)}
    operation_origins = tuple(
        pymupdf.Point(float(match.group(2)), float(match.group(3))) * page.transformation_matrix
        for match in operations
    )
    matches_per_rect = [0] * len(rects)
    selected = 0

    def replace(match: re.Match[bytes]) -> bytes:
        nonlocal selected
        index = operation_index[match.start()]
        origin = operation_origins[index]
        selected_rects = [index for index, rect in enumerate(rects) if rect.contains(origin)]
        if not selected_rects:
            return match.group(0)
        if len(selected_rects) != 1:
            raise SourcePreservingUnsupported("a text operation belongs to multiple target segments")
        if traced_spans is not None:
            matches = [
                (span, char_index) for span in traced_spans
                for char_index, character in enumerate(span["chars"])
                if pymupdf.Point(character[2]).distance_to(origin) < 0.05
            ]
            if len(matches) != 1:
                raise SourcePreservingUnsupported("target text operation is not uniquely linked to rendered glyphs")
            span, first_char = matches[0]
            last_char = len(span["chars"])
            if index + 1 < len(operations):
                next_origin = operation_origins[index + 1]
                next_matches = [
                    char_index for char_index in range(first_char + 1, last_char)
                    if pymupdf.Point(span["chars"][char_index][2]).distance_to(next_origin) < 0.05
                ]
                if len(next_matches) > 1:
                    raise SourcePreservingUnsupported("next text operation has ambiguous rendered glyph boundary")
                if next_matches:
                    last_char = next_matches[0]
            if any(hint in span["font"].casefold() for hint in FORMULA_FONT_HINTS) or any(
                (chr(character[0]) if isinstance(character[0], int) else character[0]).isdigit()
                or (chr(character[0]) if isinstance(character[0], int) else character[0]) in IMMUTABLE_SYMBOLS
                for character in span["chars"][first_char:last_char]
            ):
                raise SourcePreservingUnsupported("target text operation contains an immutable formula or symbol glyph")
        matches_per_rect[selected_rects[0]] += 1
        selected += 1
        return match.group(0).replace(b"[" + match.group(4) + b"]TJ", b"[]TJ", 1)

    edited = SHOW_OPERATION.sub(replace, stream)
    if not all(matches_per_rect):
        raise SourcePreservingUnsupported("some target text has no isolated TJ operation")
    return edited, selected


def _inline_before_preserved_suffix(
    output: pymupdf.Document,
    page: pymupdf.Page,
    source_stream_xref: int,
    target_rects: list[pymupdf.Rect],
    suffix_rect: pymupdf.Rect,
    insertions: tuple[TargetText, ...],
    removed_operators: int,
) -> pymupdf.Page:
    """Splice MuPDF-generated text into the original BT block before `1:`.

    This is permitted only when the next original show operation is the suffix
    and explicitly resets its own font. Unknown generated streams fail closed.
    """
    stream = output.xref_stream(source_stream_xref)
    empty_targets = [
        match for match in SHOW_OPERATION.finditer(stream)
        if match.group(4) == b"" and any(
            rect.contains(pymupdf.Point(float(match.group(2)), float(match.group(3))) * page.transformation_matrix)
            for rect in target_rects
        )
    ]
    if len(empty_targets) != removed_operators:
        raise SourcePreservingUnsupported("cannot identify all emptied source text operations for inline insertion")
    intervening = list(SHOW_OPERATION.finditer(stream, empty_targets[0].start(), empty_targets[-1].end()))
    if len(intervening) != len(empty_targets):
        raise SourcePreservingUnsupported("protected text occurs between translated source operations")
    cut = empty_targets[-1].end()
    text_boundaries = list(re.finditer(rb"(?<!\S)(BT|ET)(?!\S)", stream[:cut]))
    if not text_boundaries or text_boundaries[-1].group(1) != b"BT":
        raise SourcePreservingUnsupported("target operation is not inside a source text object")
    next_show = next(SHOW_OPERATION.finditer(stream, cut), None)
    if next_show is None:
        raise SourcePreservingUnsupported("original suffix text operation is missing")
    suffix_origin = pymupdf.Point(float(next_show.group(2)), float(next_show.group(3))) * page.transformation_matrix
    if not suffix_rect.contains(suffix_origin) or not re.search(rb"/[A-Za-z0-9_-]+\s+[0-9.]+\s+Tf", stream[cut:next_show.start()]):
        raise SourcePreservingUnsupported("preserved suffix is not next with an explicit original font reset")

    generated = []
    for item in insertions:
        font_token = re.escape(item.fontname.encode("ascii"))
        pattern = (
            rb"\nq\nBT\n(1 0 0 1 ([-0-9.]+) ([-0-9.]+) Tm\n/"
            + font_token
            + rb" [0-9.]+ Tf \[<[0-9A-Fa-f]+>\]TJ\n)ET\nQ\n"
        )
        matches = [
            (xref, match) for xref in page.get_contents()
            if xref != source_stream_xref
            for match in [re.fullmatch(pattern, output.xref_stream(xref))]
            if match is not None and (
                pymupdf.Point(float(match.group(2)), float(match.group(3))) * page.transformation_matrix
            ).distance_to(pymupdf.Point(item.origin)) < 0.02
        ]
        if len(matches) != 1 or any(xref == matches[0][0] for xref, _ in generated):
            raise SourcePreservingUnsupported("generated Chinese insertion stream is not uniquely recognizable")
        generated.append(matches[0])
    output.update_stream(source_stream_xref, stream[:cut] + b"\n" + b"".join(match.group(1) for _, match in generated) + stream[cut:])
    for xref, _ in generated:
        output.update_stream(xref, b"")
    return output.reload_page(page)


def _changed_pixels_outside(
    source: pymupdf.Page, target: pymupdf.Page, allowed: tuple[pymupdf.Rect, ...], scale: int = 2
) -> tuple[int, list[tuple[float, float]]]:
    matrix = pymupdf.Matrix(scale, scale)
    a = source.get_pixmap(matrix=matrix, alpha=False)
    b = target.get_pixmap(matrix=matrix, alpha=False)
    if (a.width, a.height, a.n) != (b.width, b.height, b.n):
        raise SourcePreservingUnsupported("source and target raster dimensions differ")
    first = memoryview(a.samples)
    second = memoryview(b.samples)
    changed = 0
    samples = []
    for y in range(a.height):
        row = y * a.width * a.n
        intervals = sorted(
            (max(0, int(rect.x0 * scale)), min(a.width, int(rect.x1 * scale) + 1))
            for rect in allowed
            if rect.y0 * scale <= y <= rect.y1 * scale
        )
        current = 0
        for left, right in intervals + [(a.width, a.width)]:
            end = max(current, left)
            start_byte = row + current * a.n
            end_byte = row + end * a.n
            if first[start_byte:end_byte] != second[start_byte:end_byte]:
                for x in range(current, end):
                    offset = row + x * a.n
                    if first[offset : offset + a.n] != second[offset : offset + a.n]:
                        changed += 1
                        if len(samples) < 10:
                            samples.append((x / scale, y / scale))
            current = max(current, right)
    return changed, samples


def _assert_inserted_text(
    page: pymupdf.Page,
    insertions: tuple[TargetText, ...],
    preserved_suffix: ExactSourceSegment | None,
    suffix_rect: pymupdf.Rect | None,
) -> None:
    insertion_rects = [pymupdf.Rect(item.allowed_rect) for item in insertions]
    for item, rect in zip(insertions, insertion_rects, strict=True):
        if item.text not in page.get_textbox(rect):
            raise SourcePreservingUnsupported("translated text is missing or clipped in its planned PDF slot")
    if preserved_suffix is None or suffix_rect is None:
        return
    region_rects = insertion_rects + [suffix_rect]
    region = pymupdf.Rect(
        min(rect.x0 for rect in region_rects) - 1,
        min(rect.y0 for rect in region_rects) - 1,
        max(rect.x1 for rect in region_rects) + 1,
        max(rect.y1 for rect in region_rects) + 1,
    )
    extracted = page.get_textbox(region)
    cursor = 0
    for item in insertions:
        position = extracted.find(item.text, cursor)
        if position < 0:
            raise SourcePreservingUnsupported("translated text has the wrong PDF extraction order")
        cursor = position + len(item.text)
    if extracted.find(preserved_suffix.text, cursor) < 0:
        raise SourcePreservingUnsupported("original terminal suffix is missing or precedes translated prose")


def write_source_preserving_page(
    source_path: Path,
    page_index: int,
    output_path: Path,
    segments: tuple[ExactSourceSegment, ...],
    insertions: tuple[TargetText, ...],
    preserved_suffix: ExactSourceSegment | None = None,
) -> dict:
    """Edit one source page and prove every glyph/pixel outside allowed slots survived."""
    if output_path.exists():
        raise FileExistsError(f"refusing to overwrite existing PDF: {output_path}")
    with pymupdf.open(source_path) as source:
        if page_index < 0 or page_index >= len(source):
            raise IndexError("source page index out of range")
        source_page = source[page_index]
        matched_rects = _located_segments(source_page, segments)
        suffix_rect = _located_segments(source_page, (preserved_suffix,))[0] if preserved_suffix else None
        if suffix_rect is not None and any(suffix_rect.intersects(rect) for rect in matched_rects):
            raise SourcePreservingUnsupported("preserved suffix overlaps editable source text")
        if not insertions:
            raise ValueError("at least one target text insertion is required")
        insertion_rects = tuple(pymupdf.Rect(item.allowed_rect) for item in insertions)
        for item, rect in zip(insertions, insertion_rects):
            if not item.fontfile.is_file() or item.fontsize <= 0 or not rect.contains(pymupdf.Point(item.origin)):
                raise ValueError("target font, size or insertion geometry is invalid")
            if any(
                _glyph_is_in_rects(char, (rect,)) and not _glyph_is_in_rects(char, tuple(matched_rects))
                for span in source_page.get_texttrace()
                for char in span["chars"]
            ):
                raise SourcePreservingUnsupported("target insertion slot intersects unchanged source glyphs")
            if any(rect.intersects(pymupdf.Rect(path["rect"])) for path in source_page.get_drawings()):
                raise SourcePreservingUnsupported("target insertion slot intersects source vector graphics")
            if any(rect.intersects(pymupdf.Rect(image["bbox"])) for image in source_page.get_image_info()):
                raise SourcePreservingUnsupported("target insertion slot intersects a source image")
        contents = source_page.get_contents()
        if len(contents) != 1:
            raise SourcePreservingUnsupported("expected exactly one source content stream")
        original_stream = source.xref_stream(contents[0])
        output = pymupdf.open()
        output.insert_pdf(source, from_page=page_index, to_page=page_index)
        candidate_path = None
        inline_visual_baseline = None
        try:
            page = output[0]
            copied_contents = page.get_contents()
            if len(copied_contents) != 1 or output.xref_stream(copied_contents[0]) != original_stream:
                raise SourcePreservingUnsupported("copy changed the source page content stream")
            edited_stream, removed_operators = _remove_whole_show_operations(original_stream, source_page, matched_rects)
            output.update_stream(copied_contents[0], edited_stream)
            if _glyphs_outside(source_page, tuple(matched_rects)) != _glyphs_outside(page, tuple(matched_rects)):
                raise SourcePreservingUnsupported("removing target operations changed other source glyphs")
            if any(page.get_textbox(rect).strip() for rect in matched_rects):
                raise SourcePreservingUnsupported("source text remains in a target segment after removal")
            for item in insertions:
                page.insert_font(fontname=item.fontname, fontfile=str(item.fontfile))
                page.insert_text(item.origin, item.text, fontname=item.fontname, fontsize=item.fontsize)
            if suffix_rect is not None:
                inline_visual_baseline = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False).samples
                page = _inline_before_preserved_suffix(
                    output, page, copied_contents[0], matched_rects, suffix_rect, insertions, removed_operators
                )
                if page.get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False).samples != inline_visual_baseline:
                    raise SourcePreservingUnsupported("inlining translated text changed its rendered appearance")
            _assert_inserted_text(page, insertions, preserved_suffix, suffix_rect)
            allowed = tuple(matched_rects) + insertion_rects
            if _glyphs_outside(source_page, allowed) != _glyphs_outside(page, allowed):
                raise SourcePreservingUnsupported("non-target glyph text/font/size/position changed")
            primitives = compare_visual_primitives(source_page, page)
            if not primitives["passed"]:
                raise SourcePreservingUnsupported("source page images, vectors or mathematical glyphs changed")
            pixel_allowed = tuple(
                pymupdf.Rect(
                    rect.x0 - RASTER_ANTIALIAS_MARGIN_PT,
                    rect.y0 - RASTER_ANTIALIAS_MARGIN_PT,
                    rect.x1 + RASTER_ANTIALIAS_MARGIN_PT,
                    rect.y1 + RASTER_ANTIALIAS_MARGIN_PT,
                )
                for rect in allowed
            )
            changed_outside, pixel_samples = _changed_pixels_outside(source_page, page, pixel_allowed)
            if changed_outside:
                raise SourcePreservingUnsupported(
                    f"{changed_outside} pixels changed outside target text regions; samples={pixel_samples}"
                )
            output_path.parent.mkdir(parents=True, exist_ok=True)
            handle, candidate_name = tempfile.mkstemp(prefix=".source-preserving-", suffix=".pdf", dir=output_path.parent)
            os.close(handle)
            candidate_path = Path(candidate_name)
            candidate_path.unlink()
            output.save(candidate_path, garbage=4, deflate=True)
        except BaseException:
            if candidate_path is not None:
                candidate_path.unlink(missing_ok=True)
            raise
        finally:
            output.close()
        try:
            with pymupdf.open(candidate_path) as persisted:
                if len(persisted) != 1:
                    raise SourcePreservingUnsupported("saved PDF page count changed")
                saved_page = persisted[0]
                if inline_visual_baseline is not None and saved_page.get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False).samples != inline_visual_baseline:
                    raise SourcePreservingUnsupported("saved inline PDF changed translated text appearance")
                _assert_inserted_text(saved_page, insertions, preserved_suffix, suffix_rect)
                if _glyphs_outside(source_page, allowed) != _glyphs_outside(saved_page, allowed):
                    raise SourcePreservingUnsupported("saved PDF changed non-target glyphs")
                if not compare_visual_primitives(source_page, saved_page)["passed"]:
                    raise SourcePreservingUnsupported("saved PDF changed original visual primitives")
                saved_pixel_diff, pixel_samples = _changed_pixels_outside(source_page, saved_page, pixel_allowed)
                if saved_pixel_diff:
                    raise SourcePreservingUnsupported(
                        f"saved PDF changed {saved_pixel_diff} pixels outside target regions; samples={pixel_samples}"
                    )
            if output_path.exists():
                raise FileExistsError(f"output PDF appeared during verification: {output_path}")
            # A same-directory hard link is atomic and refuses an existing name,
            # unlike os.replace(), which could overwrite a concurrent output.
            os.link(candidate_path, output_path)
        finally:
            candidate_path.unlink(missing_ok=True)
    return {
        "source_page": page_index + 1,
        "target_segment_count": len(segments),
        "removed_text_operators": removed_operators,
        "unchanged_glyphs_outside_target": True,
        "changed_pixels_outside_target_at_2x": 0,
        "pixel_mask_antialias_margin_pt": RASTER_ANTIALIAS_MARGIN_PT,
        "reading_order_in_source_stream": preserved_suffix is not None,
        "inline_appearance_identical_at_2x": True if preserved_suffix is not None else None,
        "visual_primitives": primitives,
        "full_translation_verified": False,
        "formal_use_ready": False,
    }
