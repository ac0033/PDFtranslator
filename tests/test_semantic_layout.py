import pytest

from pdf_translation.semantic_layout import (
    group_semantic_fragments,
    layout_boundaries_are_natural,
    layout_marker,
    rebalance_layout_markers,
    restore_and_rebalance_layout_markers,
    should_merge_fragments,
    split_layout_translation,
)


def fragment(fragment_id: str, text: str, bbox: tuple[float, float, float, float], *, protected_only: bool = False) -> dict:
    return {
        "fragment_id": fragment_id,
        "plain_text": text,
        "bbox": bbox,
        "protected_only": protected_only,
    }


def test_page_42_sentence_fragments_are_grouped_by_geometry_not_callback_order():
    first = fragment(
        "first",
        "A probability distribution assigns probabilities to different outcomes. There are",
        (35.47, 258.09, 517.62, 301.07),
    )
    continuation = fragment(
        "continuation",
        "different ways to represent probability distributions depending on the outcomes.",
        (35.71, 275.66, 385.11, 299.64),
    )
    # Deliberately reverse capture order: spatial/semantic evidence must win.
    groups = group_semantic_fragments([continuation, first])
    assert [[item["fragment_id"] for item in group] for group in groups] == [["first", "continuation"]]


def test_header_footnote_and_protected_content_are_not_merged():
    header = fragment("header", "20 Chapter 2 Representation", (35, 38, 193, 48))
    body = fragment("body", "and transitivity assumptions continue here", (35, 80, 500, 110))
    protected = fragment("formula", "x", (35, 120, 80, 130), protected_only=True)
    assert should_merge_fragments(header, body) is False
    assert should_merge_fragments(body, protected) is False


def test_same_line_horizontal_fragments_are_merged_in_reading_order():
    first = fragment("first", "we can", (59.36, 239.65, 89.73, 249.68))
    second = fragment(
        "second",
        "write the density function as a product of univariate Gaussian densities",
        (92.22, 239.65, 406.01, 249.68),
    )
    groups = group_semantic_fragments([second, first])
    assert [[item["fragment_id"] for item in group] for group in groups] == [["first", "second"]]


def test_layout_boundary_moves_to_sentence_boundary_and_splits_cleanly():
    first_marker = layout_marker("first")
    second_marker = layout_marker("continuation")
    footnote = "⟦P:footnote-5⟧"
    source = (
        f"{first_marker}A probability distribution assigns probabilities to outcomes."
        f"{footnote}There are {second_marker}different ways to represent it."
    )
    balanced = rebalance_layout_markers(source)
    assert balanced == (
        f"{first_marker}A probability distribution assigns probabilities to outcomes."
        f"{footnote}{second_marker}There are different ways to represent it."
    )
    assert layout_boundaries_are_natural(balanced) is True
    pieces = split_layout_translation(
        balanced,
        [
            {"fragment_id": "first", "layout_marker": first_marker},
            {"fragment_id": "continuation", "layout_marker": second_marker},
        ],
    )
    assert pieces["first"].endswith(footnote)
    assert pieces["continuation"] == "There are different ways to represent it."


def test_layout_boundary_waits_for_sentence_end_in_later_fragment():
    first_marker = layout_marker("first")
    second_marker = layout_marker("continuation")
    source = f"{first_marker}we can {second_marker}write the density function:"
    balanced = rebalance_layout_markers(source)
    assert balanced == f"{first_marker}we can write the density function:{second_marker}"
    assert layout_boundaries_are_natural(balanced) is True


def test_omitted_layout_markers_are_restored_at_target_sentence_boundaries():
    first_marker = layout_marker("first")
    second_marker = layout_marker("continuation")
    protected = "⟦P:footnote-5⟧"
    source = f"{first_marker}A distribution assigns probabilities.{protected}There are {second_marker}different forms."
    translated = f"概率分布为结果分配概率。{protected}概率分布有不同的表示形式。"
    repaired, changed = restore_and_rebalance_layout_markers(source, translated)
    assert changed is True
    assert repaired == f"{first_marker}概率分布为结果分配概率。{protected}{second_marker}概率分布有不同的表示形式。"
    assert ANY_MARKERS(repaired) == ANY_MARKERS(source)
    assert layout_boundaries_are_natural(repaired) is True


def test_layout_markers_are_not_restored_when_semantic_marker_is_missing():
    first_marker = layout_marker("first")
    second_marker = layout_marker("continuation")
    source = f"{first_marker}First.⟦P:formula-1⟧Then {second_marker}continue."
    translated = "第一句。然后继续。"
    repaired, changed = restore_and_rebalance_layout_markers(source, translated)
    assert changed is False
    assert repaired == translated


def test_boundary_marker_relocation_never_crosses_other_formula_markers():
    first = layout_marker("first")
    second = layout_marker("second")
    formula_a = "⟦P:formula-a⟧"
    formula_b = "⟦P:formula-b⟧"
    boundary_formula = "⟦P:boundary-formula⟧"
    next_formula = "⟦P:next-formula⟧"
    source = f"{first}First sentence. Use {formula_a}x {formula_b}y here.{boundary_formula}The support {second}of a distribution is nonzero near {next_formula}z."
    translated = f"{first}第一句。这里使用 {formula_a}x {formula_b}y。{boundary_formula}分布的支撑{second}是在 {next_formula}z 附近非零的集合。"
    repaired, changed = restore_and_rebalance_layout_markers(source, translated)
    assert changed is True
    assert ANY_MARKERS(repaired) == ANY_MARKERS(source)
    assert layout_boundaries_are_natural(repaired) is True


def test_layout_boundary_does_not_split_decimal_or_spaced_ellipsis():
    first_marker = layout_marker("first")
    second_marker = layout_marker("continuation")
    source = f"{first_marker}Values 1, . . . , n are used before {second_marker}the next sentence. Figure 2.1 follows."
    translated = f"取值为 1, . . . , n。图 2.1 如下。"
    repaired, changed = restore_and_rebalance_layout_markers(source, translated)
    assert changed is True
    assert ". . ." in repaired
    assert ". {second_marker}.".format(second_marker=second_marker) not in repaired
    assert "2.1" in repaired
    assert layout_boundaries_are_natural(repaired) is True


def ANY_MARKERS(text: str) -> list[str]:
    import re

    return re.findall(r"⟦P:[^⟧]+⟧", text)


def test_dangling_layout_boundary_and_reordered_markers_are_rejected():
    first_marker = layout_marker("first")
    second_marker = layout_marker("continuation")
    dangling = f"{first_marker}一个概率分布为结果分配概率。有{second_marker}不同的表示方式。"
    assert layout_boundaries_are_natural(dangling) is False
    with pytest.raises(ValueError, match="layout marker order changed"):
        split_layout_translation(
            f"{second_marker}后半段。{first_marker}前半段。",
            [
                {"fragment_id": "first", "layout_marker": first_marker},
                {"fragment_id": "continuation", "layout_marker": second_marker},
            ],
        )


def test_layout_boundary_accepts_toc_entries_ending_in_page_numbers():
    first_marker = layout_marker("toc-body")
    second_marker = layout_marker("toc-appendix")
    toc = f"{first_marker}12 运行时监控 323\n12.1 运行设计域监控 323\n12.5 练习 351{second_marker}附录"
    assert layout_boundaries_are_natural(toc) is True
    mid_sentence_digit = f"{first_marker}取值为 3{second_marker}乘以 2 得到结果。"
    assert layout_boundaries_are_natural(mid_sentence_digit) is True
    dangling = f"{first_marker}概率分布为结果分配概率{second_marker}的表示方式。"
    assert layout_boundaries_are_natural(dangling) is False
