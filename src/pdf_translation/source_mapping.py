"""Conservative mapping from BabelDOC plain fragments to native PDF lines.

Plain fragments use native line geometry and exact text equality. Mixed
fragments may map prose around protected placeholders only when each chunk is
uniquely located. Ambiguity blocks source-operation removal.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import unicodedata

import pymupdf

from .source_preserving import ExactSourceSegment, SourcePreservingUnsupported, TargetText, _located_segments


MIN_CAPTURE_BBOX_CONFIDENCE = 0.85
LINE_CLIP_MARGIN_PT = 1.0


@dataclass(frozen=True)
class PlainFragmentMapping:
    fragment_id: str
    segments: tuple[ExactSourceSegment, ...]
    line_count: int
    bbox_confidence: float


def normalized_pdf_text(value: str) -> str:
    """Ignore only layout spacing and alphabetic line-break hyphenation.

    Punctuation, mathematical operators and number formatting remain evidence:
    treating ``1.0`` as ``10`` would be unsafe for source-to-target alignment.
    """
    text = unicodedata.normalize("NFKC", value).casefold()
    # Only collapse plausible word hyphenation; a short expression such as
    # ``x- y`` may be subtraction and must remain distinct from ``xy``.
    text = re.sub(r"(?<=[^\W\d_]{3})-\s+(?=[^\W\d_]{2})", "", text)
    return re.sub(r"\s+", "", text)


def _is_numeric_label(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    return bool(compact) and any(character.isdigit() for character in compact) and all(
        character.isdigit() or character in ".:-" for character in compact
    )


def terminal_immutable_suffix(mapping: PlainFragmentMapping) -> str | None:
    """A terminal numeric value plus colon/semicolon stays in the source PDF."""
    if not mapping.segments:
        return None
    match = re.fullmatch(r".+?\S\s+([0-9]+(?:\.[0-9]+)?[:;])", mapping.segments[-1].text)
    return match.group(1) if match else None


def editable_segments(mapping: PlainFragmentMapping) -> tuple[ExactSourceSegment, ...]:
    """Keep stand-alone labels and a terminal numeric suffix in original PDF operations."""
    result = []
    for index, segment in enumerate(mapping.segments):
        if _is_numeric_label(segment.text):
            continue
        suffix = terminal_immutable_suffix(mapping) if index == len(mapping.segments) - 1 else None
        if suffix:
            prose = segment.text[: -len(suffix)].rstrip()
            if not prose:
                raise SourcePreservingUnsupported("terminal immutable suffix has no prose before it")
            segment = ExactSourceSegment(prose, segment.search_clip)
        result.append(segment)
    if not result:
        raise SourcePreservingUnsupported("fragment has no prose after preserving numeric labels")
    return tuple(result)


def map_plain_capture_fragment(page: pymupdf.Page, record: dict) -> PlainFragmentMapping:
    """Find exact native lines for a capture fragment, or fail closed."""
    if record.get("protected_only") or record.get("marker_map"):
        raise SourcePreservingUnsupported("fragment contains protected content or is protected-only")
    confidence = float(record.get("bbox_confidence", 0.0))
    if confidence < MIN_CAPTURE_BBOX_CONFIDENCE:
        raise SourcePreservingUnsupported("fragment source location confidence is too low")
    capture_bbox = pymupdf.Rect(record["bbox"])
    native_lines: list[tuple[pymupdf.Rect, str]] = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            line_bbox = pymupdf.Rect(line["bbox"])
            if capture_bbox.contains((line_bbox.tl + line_bbox.br) / 2):
                line_text = "".join(span["text"] for span in line["spans"])
                if line_text.strip():
                    native_lines.append((line_bbox, line_text))
    captured = normalized_pdf_text(record.get("plain_text", ""))
    native = normalized_pdf_text("\n".join(text for _, text in native_lines))
    if not native_lines or not captured or captured != native:
        raise SourcePreservingUnsupported("captured text does not uniquely cover its native PDF lines")
    segments = tuple(
        ExactSourceSegment(
            text,
            (
                bbox.x0 - LINE_CLIP_MARGIN_PT,
                bbox.y0 - LINE_CLIP_MARGIN_PT,
                bbox.x1 + LINE_CLIP_MARGIN_PT,
                bbox.y1 + LINE_CLIP_MARGIN_PT,
            ),
        )
        for bbox, text in native_lines
    )
    _located_segments(page, segments)
    return PlainFragmentMapping(
        fragment_id=record["fragment_id"],
        segments=segments,
        line_count=len(segments),
        bbox_confidence=confidence,
    )


def map_placeholder_capture_fragment(page: pymupdf.Page, record: dict) -> PlainFragmentMapping:
    """Map prose chunks around preserved formula/footnote placeholders."""
    marker_map = record.get("marker_map") or {}
    if record.get("protected_only") or not marker_map:
        raise SourcePreservingUnsupported("fragment has no mixed prose/protected content")
    source = record.get("babeldoc_text", "")
    placeholders = tuple(marker_map.values())
    if not placeholders or any(source.count(marker) != 1 for marker in placeholders):
        raise SourcePreservingUnsupported("protected placeholder occurrence count is ambiguous")
    if re.search(r"<[^>]+>", source) and any(
        token not in placeholders for token in re.findall(r"<[^>]+>", source)
    ):
        raise SourcePreservingUnsupported("fragment contains unsupported inline markup")
    splitter = re.compile("|".join(re.escape(marker) for marker in placeholders))
    chunks = [chunk.strip() for chunk in splitter.split(source) if chunk.strip()]
    if not chunks:
        raise SourcePreservingUnsupported("fragment has no prose outside protected placeholders")
    clip = pymupdf.Rect(record["bbox"])
    segments: list[ExactSourceSegment] = []
    for chunk in chunks:
        hits = page.search_for(chunk, clip=clip)
        # A search hit can end at a PDF line break (for example, ``nota-`` /
        # ``tion``). Preserve that boundary so the existing alphabetic
        # line-hyphen rule can compare the original prose without loosening
        # punctuation, numbers or mathematical operators.
        if not hits or normalized_pdf_text("\n".join(page.get_textbox(hit) for hit in hits)) != normalized_pdf_text(chunk):
            raise SourcePreservingUnsupported(f"protected-fragment prose is not exactly located: {chunk!r}")
        for hit in hits:
            if not clip.contains(hit):
                raise SourcePreservingUnsupported("protected-fragment prose falls outside captured location")
            extracted = page.get_textbox(hit).strip()
            segments.append(
                ExactSourceSegment(
                    extracted,
                    (
                        hit.x0 - LINE_CLIP_MARGIN_PT,
                        hit.y0 - LINE_CLIP_MARGIN_PT,
                        hit.x1 + LINE_CLIP_MARGIN_PT,
                        hit.y1 + LINE_CLIP_MARGIN_PT,
                    ),
                )
            )
    _located_segments(page, tuple(segments))
    return PlainFragmentMapping(
        fragment_id=record["fragment_id"],
        segments=tuple(segments),
        line_count=len(segments),
        bbox_confidence=float(record.get("bbox_confidence", 0.0)),
    )


def plan_single_line_fragment_insertions(
    page: pymupdf.Page,
    records: tuple[dict, ...],
    translated_by_fragment: dict[str, str],
    fontfile: Path,
    fontsize: float = 10.0,
) -> tuple[tuple[ExactSourceSegment, ...], tuple[TargetText, ...]]:
    """Plan fragments whose entire Chinese text fits their first native line.

    This is intentionally narrow: no silent font shrinking, slot expansion,
    protected-object relocation or replacement of untranslated fragments.
    """
    if not records or not fontfile.is_file() or fontsize <= 0:
        raise ValueError("records, local Chinese font and positive font size are required")
    unit_ids = {record.get("unit_id") for record in records}
    if len(unit_ids) != 1 or None in unit_ids:
        raise SourcePreservingUnsupported("fragment plan must contain exactly one complete semantic unit")
    if sorted(record.get("group_index") for record in records) != list(range(len(records))) or any(
        record.get("group_size") != len(records) for record in records
    ):
        raise SourcePreservingUnsupported("semantic group is incomplete or out of order")
    if set(translated_by_fragment) != {record["fragment_id"] for record in records}:
        raise SourcePreservingUnsupported("translated fragments do not exactly match the semantic group")
    font = pymupdf.Font(fontfile=str(fontfile))
    segments: list[ExactSourceSegment] = []
    insertions: list[TargetText] = []
    for record in records:
        mapping = (
            map_placeholder_capture_fragment(page, record)
            if record.get("marker_map") else map_plain_capture_fragment(page, record)
        )
        if terminal_immutable_suffix(mapping):
            raise SourcePreservingUnsupported("terminal immutable suffix requires paragraph reflow")
        source_segments = editable_segments(mapping)
        source_rects = _located_segments(page, source_segments)
        translated = translated_by_fragment.get(record["fragment_id"])
        if translated is None:
            raise SourcePreservingUnsupported("accepted translation is missing for a source fragment")
        for marker in record.get("marker_map", {}):
            if translated.count(marker) != 1:
                raise SourcePreservingUnsupported("translated protected marker count changed")
            translated = translated.replace(marker, "")
        for segment in mapping.segments:
            if _is_numeric_label(segment.text):
                translated = translated.lstrip()
                if not translated.startswith(segment.text):
                    raise SourcePreservingUnsupported("target changed an immutable page/section label")
                translated = translated[len(segment.text):]
        translated = translated.strip()
        if not translated or "⟦P:" in translated or "<b" in translated:
            raise SourcePreservingUnsupported("target fragment is empty or contains unresolved markers")
        first = source_rects[0]
        width = font.text_length(translated, fontsize=fontsize)
        if width > first.width:
            raise SourcePreservingUnsupported(
                f"Chinese fragment exceeds first source line: {width:.2f} > {first.width:.2f} pt"
            )
        baseline = first.y1 - 2.0
        allowed = (
            first.x0,
            min(first.y0, baseline - font.ascender * fontsize) - 0.5,
            first.x1,
            max(first.y1, baseline - font.descender * fontsize) + 0.5,
        )
        segments.extend(source_segments)
        insertions.append(
            TargetText(translated, (first.x0, baseline), allowed, fontfile, "zh-fangsong", fontsize)
        )
    return tuple(segments), tuple(insertions)


def plan_terminal_suffix_paragraph(
    page: pymupdf.Page,
    record: dict,
    translated: str,
    fontfile: Path,
    fontsize: float = 10.0,
) -> tuple[tuple[ExactSourceSegment, ...], tuple[TargetText, ...]]:
    """Reflow a two-line paragraph while retaining its original terminal `1:`.

    The target must contain exactly the same number and corresponding terminal
    punctuation. Text is wrapped only after complete translation; the second
    line must visually approach the unmoved number. No font shrinking, invented
    line or protected-object move is allowed.
    """
    if record.get("group_index") != 0 or record.get("group_size") != 1 or not record.get("unit_id"):
        raise SourcePreservingUnsupported("terminal-suffix reflow requires one complete semantic unit")
    if record.get("marker_map") or not fontfile.is_file() or fontsize <= 0:
        raise SourcePreservingUnsupported("terminal-suffix reflow requires plain text and a local Chinese font")
    mapping = map_plain_capture_fragment(page, record)
    source_suffix = terminal_immutable_suffix(mapping)
    if not source_suffix or len(mapping.segments) != 2:
        raise SourcePreservingUnsupported("terminal-suffix reflow requires exactly two source prose lines")
    target_match = re.search(r"([0-9]+(?:\.[0-9]+)?)([:：;；])$", translated.strip())
    if not target_match or target_match.group(1) != source_suffix[:-1] or (
        source_suffix[-1] == ":" and target_match.group(2) not in ":："
        or source_suffix[-1] == ";" and target_match.group(2) not in ";；"
    ):
        raise SourcePreservingUnsupported("target changed the original terminal number or punctuation")
    body = translated[: target_match.start()].strip()
    if not body or "⟦P:" in body or "<b" in body or any(
        character.isascii() and character.isalnum() for character in body
    ):
        raise SourcePreservingUnsupported("target paragraph has missing text or unresolved markers")
    source_segments = editable_segments(mapping)
    source_rects = _located_segments(page, source_segments)
    font = pymupdf.Font(fontfile=str(fontfile))
    choices = []
    for index in range(1, len(body)):
        before, after = body[index - 1], body[index]
        if after in "，。！？；：、）》」】" or before in "（《「【":
            continue
        if before.isascii() and after.isascii() and before.isalnum() and after.isalnum():
            continue
        first_text = body[:index].strip()
        second_text = body[index:].strip()
        if not second_text:
            continue
        widths = tuple(font.text_length(text, fontsize=fontsize) for text in (first_text, second_text))
        if all(width <= rect.width for width, rect in zip(widths, source_rects, strict=True)):
            terminal_gap = source_rects[-1].width - widths[-1]
            natural_break_penalty = 0 if before in "。！？；，" else 1
            choices.append((terminal_gap, natural_break_penalty, first_text, second_text))
    if not choices:
        raise SourcePreservingUnsupported("Chinese paragraph cannot fit its two native prose lines")
    terminal_gap, _, first_text, second_text = min(choices)
    if terminal_gap > max(12.0, source_rects[-1].width * 0.15):
        raise SourcePreservingUnsupported("Chinese line ends too far from the original terminal number")
    insertions = []
    for text, rect in zip((first_text, second_text), source_rects, strict=True):
        baseline = rect.y1 - 2.0
        allowed = (
            rect.x0,
            min(rect.y0, baseline - font.ascender * fontsize) - 0.5,
            rect.x1,
            max(rect.y1, baseline - font.descender * fontsize) + 0.5,
        )
        insertions.append(TargetText(text, (rect.x0, baseline), allowed, fontfile, "zh-fangsong", fontsize))
    return source_segments, tuple(insertions)
