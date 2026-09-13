"""Offline probe: auto-plan section 2.2 from capture and accepted DeepSeek text."""

import json
from pathlib import Path

import pymupdf

from pdf_translation.semantic_layout import split_layout_translation
from pdf_translation.source_mapping import plan_single_line_fragment_insertions
from pdf_translation.source_preserving import write_source_preserving_page


SOURCE = Path("pdf/dm.pdf")
OUTPUT = Path("tmp/pdfs/source-preserving-page42-auto-r3.pdf")
FONT = Path("C:/Windows/Fonts/simfang.ttf")
WORK = Path("output/pdf/v0.7-deepseek-validation/dm-297196f2cad5/445c49a3")
SOURCE_ANCHOR = "A probability distribution assigns probabilities to different outcomes."


def main() -> None:
    records = [json.loads(line) for line in (WORK / "capture-map.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    anchor = next(record for record in records if SOURCE_ANCHOR in record["babeldoc_text"])
    group = tuple(sorted((record for record in records if record["unit_id"] == anchor["unit_id"]), key=lambda item: item["group_index"]))
    aligned = [json.loads(line) for line in (WORK / "aligned.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    translated = next(row["translated_text"] for row in aligned if row["unit"]["unit_id"] == anchor["unit_id"])
    translated_by_fragment = split_layout_translation(translated, list(group))
    with pymupdf.open(SOURCE) as source_pdf:
        segments, insertions = plan_single_line_fragment_insertions(
            source_pdf[41], group, translated_by_fragment, FONT
        )
    result = write_source_preserving_page(
        SOURCE,
        41,
        OUTPUT,
        segments=segments,
        insertions=insertions,
    )
    print(json.dumps({"unit_id": anchor["unit_id"], "planned_fragments": len(group), **result}, ensure_ascii=False))


if __name__ == "__main__":
    main()
