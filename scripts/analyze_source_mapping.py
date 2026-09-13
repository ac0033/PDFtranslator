"""Read-only audit of capture-to-source mapping coverage on page 42."""

import json
from pathlib import Path

import pymupdf

from pdf_translation.source_mapping import (
    editable_segments,
    map_plain_capture_fragment,
    map_placeholder_capture_fragment,
    map_single_line_mixed_native_prose,
)
from pdf_translation.source_preserving import (
    SourcePreservingUnsupported,
    RASTER_ANTIALIAS_MARGIN_PT,
    _changed_pixels_outside,
    _glyphs_outside,
    _located_segments,
    _remove_whole_show_operations,
)
from pdf_translation.pdf_fidelity import compare_visual_primitives


SOURCE = Path("pdf/dm.pdf")
CAPTURE = Path("output/pdf/v0.7-deepseek-validation/dm-297196f2cad5/445c49a3/capture-map.jsonl")


def audit_removal(document: pymupdf.Document, page: pymupdf.Page, stream: bytes, mapping) -> int:
    rects = _located_segments(page, editable_segments(mapping))
    edited, count = _remove_whole_show_operations(stream, page, rects)
    copied = pymupdf.open()
    try:
        copied.insert_pdf(document, from_page=41, to_page=41)
        copied_page = copied[0]
        copied.update_stream(copied_page.get_contents()[0], edited)
        if _glyphs_outside(page, tuple(rects)) != _glyphs_outside(copied_page, tuple(rects)):
            raise SourcePreservingUnsupported("candidate removal changed glyphs outside target chunks")
        if any(copied_page.get_textbox(rect).strip() for rect in rects):
            raise SourcePreservingUnsupported("target prose remains after candidate removal")
        if not compare_visual_primitives(page, copied_page)["passed"]:
            raise SourcePreservingUnsupported("candidate removal changed math glyphs, images or vectors")
        pixel_mask = tuple(
            pymupdf.Rect(
                rect.x0 - RASTER_ANTIALIAS_MARGIN_PT,
                rect.y0 - RASTER_ANTIALIAS_MARGIN_PT,
                rect.x1 + RASTER_ANTIALIAS_MARGIN_PT,
                rect.y1 + RASTER_ANTIALIAS_MARGIN_PT,
            ) for rect in rects
        )
        changed_outside, samples = _changed_pixels_outside(page, copied_page, pixel_mask)
        if changed_outside:
            raise SourcePreservingUnsupported(
                f"candidate removal changed {changed_outside} pixels outside source text; samples={samples}"
            )
    finally:
        copied.close()
    return count


def main() -> None:
    records = [json.loads(line) for line in CAPTURE.read_text(encoding="utf-8").splitlines() if line.strip()]
    rows = []
    with pymupdf.open(SOURCE) as document:
        page = document[41]
        contents = page.get_contents()
        if len(contents) != 1:
            raise RuntimeError("source mapping audit expects one page stream")
        stream = document.xref_stream(contents[0])
        for record in records:
            row = {"fragment_id": record["fragment_id"], "bbox_confidence": record["bbox_confidence"]}
            mappers = (
                (map_placeholder_capture_fragment, map_single_line_mixed_native_prose)
                if record.get("marker_map") else (map_plain_capture_fragment,)
            )
            first_failure = None
            for mapper in mappers:
                try:
                    mapping = mapper(page, record)
                    count = audit_removal(document, page, stream, mapping)
                    row.update(
                        status="safe_removal", line_count=mapping.line_count,
                        text_operator_count=count, mapping_strategy=mapper.__name__,
                    )
                    break
                except SourcePreservingUnsupported as exc:
                    if first_failure is None:
                        first_failure = str(exc)
                    if mapper is mappers[-1]:
                        row.update(status="blocked", reason=first_failure, alternative_reason=str(exc))
            rows.append(row)
    print(json.dumps({
        "fragment_count": len(rows),
        "safe_removal": sum(row["status"] == "safe_removal" for row in rows),
        "rows": rows,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
