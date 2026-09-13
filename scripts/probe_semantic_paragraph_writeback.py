"""Non-production 2.2 opening-paragraph writeback around its unmoved footnote."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pymupdf

from pdf_translation.source_mapping import map_complete_semantic_unit_native_spans
from pdf_translation.source_preserving import TargetText, _located_segments, write_source_preserving_page


SOURCE = Path("pdf/dm.pdf")
CAPTURE = Path("output/pdf/v0.7-deepseek-validation/dm-297196f2cad5/445c49a3/capture-map.jsonl")
ALIGNED = Path("output/pdf/v0.7-deepseek-validation/dm-297196f2cad5/445c49a3/aligned.jsonl")
OUTPUT = Path("tmp/pdfs/source-preserving-page42-section22-r11-probe.pdf")
SOURCE_SHA256 = "297196f2cad5ab23cf4855f815fd69e7b7b9ee9c31255f743b668416677ad24e"
UNIT_ID = "bdg-ee80a27960959266"
FONT = Path("C:/Windows/Fonts/simfang.ttf")
FONTSIZE = 9.96264

# Layout-constrained diagnostic paraphrase of the accepted full-unit meaning.
# It is deliberately not promoted to a production translation or independent
# semantic-quality acceptance merely because its glyph geometry passes.
FIRST_SENTENCE = "概率分布为每一种可能出现的不同结果分别分配一个相应的概率。"
SECOND_SENTENCE = "概率分布有不同的表示方法；具体采用哪一种方法，需要看分布所涉及的结果究竟是离散的，还是连续的。"


def main() -> None:
    if hashlib.sha256(SOURCE.read_bytes()).hexdigest() != SOURCE_SHA256:
        raise RuntimeError("registered source PDF hash changed")
    records = [json.loads(line) for line in CAPTURE.read_text(encoding="utf-8").splitlines() if line.strip()]
    group = tuple(sorted((record for record in records if record["unit_id"] == UNIT_ID), key=lambda item: item["group_index"]))
    aligned = [json.loads(line) for line in ALIGNED.read_text(encoding="utf-8").splitlines() if line.strip()]
    accepted = next(item for item in aligned if item["unit"]["unit_id"] == UNIT_ID)
    if accepted["quality"]["state"] != "accepted" or accepted["quality"]["quality"]["status"] != "pass":
        raise RuntimeError("complete semantic unit is not accepted by the saved automatic checks")
    with pymupdf.open(SOURCE) as document:
        page = document[41]
        mapping = map_complete_semantic_unit_native_spans(page, group)
        if mapping.line_count != 3 or len(mapping.segments) != 6:
            raise RuntimeError("the registered paragraph no longer has three lines and six editable font spans")
        rects = _located_segments(page, mapping.segments)
        footnotes = page.search_for("5", clip=pymupdf.Rect(334, 257, 341, 270))
        if len(footnotes) != 1:
            raise RuntimeError("the original footnote 5 is not uniquely located")
        footnote = footnotes[0]
    if len(FIRST_SENTENCE) != 29 or len(SECOND_SENTENCE) != 47:
        raise RuntimeError("fitted diagnostic Chinese changed length")
    parts = (
        FIRST_SENTENCE[:4], FIRST_SENTENCE[4:13], FIRST_SENTENCE[13:],
        SECOND_SENTENCE[:4], SECOND_SENTENCE[4:34], SECOND_SENTENCE[34:],
    )
    font = pymupdf.Font(fontfile=str(FONT))
    widths = [font.text_length(part, fontsize=FONTSIZE) for part in parts]
    xs = (
        rects[0].x0,
        rects[0].x0 + widths[0],
        rects[0].x0 + widths[0] + widths[1],
        rects[3].x0,
        rects[4].x0,
        rects[5].x0,
    )
    if xs[2] + widths[2] >= footnote.x0 - 1 or xs[3] <= footnote.x1 + 1 or xs[3] + widths[3] > rects[3].x1:
        raise RuntimeError("Chinese first line does not fit around the unmoved footnote")
    insertions = []
    for part, rect, x, width in zip(parts, rects, xs, widths, strict=True):
        if not rect.contains(pymupdf.Point(x, (rect.y0 + rect.y1) / 2)):
            raise RuntimeError("Chinese phrase origin has left its corresponding source font span")
        baseline = rect.y1 - 2.0
        allowed = (
            min(x, rect.x0) - 0.1,
            min(rect.y0, baseline - font.ascender * FONTSIZE) - 0.5,
            x + width + 0.5,
            max(rect.y1, baseline - font.descender * FONTSIZE) + 0.5,
        )
        insertions.append(TargetText(part, (x, baseline), allowed, FONT, "zh-fangsong", FONTSIZE))
    order = (parts[0], parts[1], parts[2], "5", parts[3], parts[4], parts[5])
    result = write_source_preserving_page(
        SOURCE, 41, OUTPUT, mapping.segments, tuple(insertions), interleaved_reading_order=order
    )
    print(json.dumps({"diagnostic_pdf": str(OUTPUT), "source_unit_id": UNIT_ID, "parts": parts, "audit": result}, ensure_ascii=True))


if __name__ == "__main__":
    main()
