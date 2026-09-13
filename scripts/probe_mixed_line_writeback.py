"""One-page, non-production writeback probe around the source P(x^0) formula."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pymupdf

from pdf_translation.source_mapping import map_single_line_mixed_native_prose
from pdf_translation.source_preserving import TargetText, _located_segments, write_source_preserving_page


SOURCE = Path("pdf/dm.pdf")
CAPTURE = Path("output/pdf/v0.7-deepseek-validation/dm-297196f2cad5/445c49a3/capture-map.jsonl")
OUTPUT = Path("tmp/pdfs/source-preserving-page42-mixed-r10-probe.pdf")
SOURCE_SHA256 = "297196f2cad5ab23cf4855f815fd69e7b7b9ee9c31255f743b668416677ad24e"
FRAGMENT_ID = "p0042-f6948205fd388"
FONT = Path("C:/Windows/Fonts/simfang.ttf")
FONTSIZE = 9.96264


def main() -> None:
    if hashlib.sha256(SOURCE.read_bytes()).hexdigest() != SOURCE_SHA256:
        raise RuntimeError("registered source PDF hash changed")
    records = [json.loads(line) for line in CAPTURE.read_text(encoding="utf-8").splitlines() if line.strip()]
    record = next(item for item in records if item["fragment_id"] == FRAGMENT_ID)
    with pymupdf.open(SOURCE) as document:
        page = document[41]
        mapping = map_single_line_mixed_native_prose(page, record)
        rects = _located_segments(page, mapping.segments)
    font = pymupdf.Font(fontfile=str(FONT))
    # The source fragment says "we use P(x^0) to represent the probability that
    # X is false". This manually rephrased diagnostic sentence keeps that
    # meaning while fitting around the unmoved formula and X. It is not a
    # production translation-quality sign-off for the complete semantic unit.
    texts = ("我们用", "这个记号表示随机变量", "为假的概率。")
    origins_x = (rects[0].x0, 115.0, rects[2].x0)
    right_edges = (rects[0].x1, rects[1].x1, 310.0)
    insertions = []
    for text, rect, x, right in zip(texts, rects, origins_x, right_edges, strict=True):
        if x + font.text_length(text, fontsize=FONTSIZE) > right:
            raise RuntimeError("candidate Chinese exceeds its available source-line slot")
        baseline = rect.y1 - 2.0
        allowed = (
            rect.x0,
            min(rect.y0, baseline - font.ascender * FONTSIZE) - 0.5,
            right,
            max(rect.y1, baseline - font.descender * FONTSIZE) + 0.5,
        )
        insertions.append(TargetText(text, (x, baseline), allowed, FONT, "zh-fangsong", FONTSIZE))
    result = write_source_preserving_page(
        SOURCE, 41, OUTPUT, mapping.segments, tuple(insertions),
        interleaved_reading_order=(texts[0], "P(x0)", texts[1], "X", texts[2]),
    )
    print(json.dumps({"diagnostic_pdf": str(OUTPUT), "candidate_text": texts, "audit": result}, ensure_ascii=True))


if __name__ == "__main__":
    main()
