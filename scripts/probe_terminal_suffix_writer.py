"""Offline page-42 probe: reflow Chinese around original `1:` operations."""

import json
from pathlib import Path

import pymupdf

from pdf_translation.source_mapping import plan_terminal_suffix_paragraph
from pdf_translation.source_preserving import ExactSourceSegment, write_source_preserving_page


SOURCE = Path("pdf/dm.pdf")
OUTPUT = Path("tmp/pdfs/source-preserving-page42-terminal-r6.pdf")
WORK = Path("output/pdf/v0.7-deepseek-validation/dm-297196f2cad5/445c49a3")
FONT = Path("C:/Windows/Fonts/simfang.ttf")
FRAGMENT_ID = "p0042-f0d8ca41b9124"


def main() -> None:
    records = [json.loads(line) for line in (WORK / "capture-map.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    record = next(item for item in records if item["fragment_id"] == FRAGMENT_ID)
    aligned = [json.loads(line) for line in (WORK / "aligned.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    translated = next(item["translated_text"] for item in aligned if item["unit"]["unit_id"] == record["unit_id"])
    with pymupdf.open(SOURCE) as source_pdf:
        segments, insertions = plan_terminal_suffix_paragraph(source_pdf[41], record, translated, FONT)
    preserved_suffix = ExactSourceSegment("1:", segments[-1].search_clip)
    result = write_source_preserving_page(SOURCE, 41, OUTPUT, segments, insertions, preserved_suffix)
    print(json.dumps({"fragment_id": FRAGMENT_ID, "original_terminal_suffix": "1:", **result}, ensure_ascii=False))


if __name__ == "__main__":
    main()
