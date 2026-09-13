"""Offline page-42 acceptance probe for the fail-closed source-preserving writer."""

from pathlib import Path

from pdf_translation.source_preserving import ExactSourceSegment, TargetText, write_source_preserving_page


SOURCE = Path("pdf/dm.pdf")
OUTPUT = Path("tmp/pdfs/source-preserving-page42-generic-v2.pdf")
FONT = Path("C:/Windows/Fonts/simfang.ttf")


def main() -> None:
    result = write_source_preserving_page(
        SOURCE,
        41,
        OUTPUT,
        segments=(
            ExactSourceSegment("A probability distribution assigns probabilities to different outcomes.", (30, 250, 340, 273)),
            ExactSourceSegment("There are", (340, 250, 390, 273)),
            ExactSourceSegment("different ways to represent probability distributions depending on whether they", (30, 273, 390, 288)),
            ExactSourceSegment("involve discrete or continuous outcomes.", (30, 287, 390, 303)),
        ),
        insertions=(
            TargetText("概率分布为不同结果分配概率。", (35.477, 269.746), (35.4, 258, 330, 272), FONT, "zh-fangsong", 10),
            TargetText("根据涉及的是离散结果还是连续结果，概率分布有不同的表示方式。", (35.716, 283.693), (35.5, 275, 385.5, 287), FONT, "zh-fangsong", 10),
        ),
    )
    print(result)


if __name__ == "__main__":
    main()
