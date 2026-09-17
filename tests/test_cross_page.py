from copy import deepcopy

import pytest
from pdf_translation.cross_page import join_cross_page_words, split_document_translation
from pdf_translation.hashing import stable_hash
from pdf_translation.schemas import SourceRef, TranslationUnit


def fixture():
    units, records = [], {}
    for index, text in enumerate(("We use the veloc-", "ity of updates. Next sentence:")):
        box = (72, 670, 370, 690) if index == 0 else (72, 75, 370, 100)
        uid = f"u{index}"
        units.append(TranslationUnit(unit_id=uid, document_id="doc", ir_revision="raw", unit_type="paragraph",
            source_lang="en", target_lang="zh-CN", source_text=text, source_hash=stable_hash(text),
            source_refs=(SourceRef(block_id=uid, page_index=index, bbox=box, char_start=0, char_end=len(text)),)))
        records[index] = [dict(unit_id=uid, fragment_id=f"f{index}", page_index=index, bbox=box,
            bbox_confidence=1.0, group_size=1, group_index=0, marker_map={}, babeldoc_text=text)]
    return units, records, {0: (600, 800), 1: (600, 800)}


def test_cross_page_word_is_complete_before_translation_and_raw_capture_unchanged():
    units, records, sizes = fixture()
    frozen = deepcopy(records)
    prepared, mapped, evidence = join_cross_page_words(units, records, sizes)
    assert len(prepared) == 1
    assert "velocity" in prepared[0].source_text
    assert "veloc-" not in prepared[0].source_text
    assert records == frozen
    assert evidence[0]["parent_unit_ids"] == ["u0", "u1"]
    assert [r.page_index for r in prepared[0].source_refs] == [0, 1]
    text = "⟦P:layout-f0⟧使用更新的速度。⟦P:layout-f1⟧下一句："
    fragments = split_document_translation({prepared[0].unit_id: text}, mapped)
    assert fragments == {"f0": "使用更新的速度。", "f1": "下一句："}
    with pytest.raises(ValueError, match="incomplete"):
        split_document_translation({prepared[0].unit_id: text}, {0: mapped[0]})


@pytest.mark.parametrize("fault", ["page-gap", "uncertain", "other-column", "finished", "not-bottom", "intervening"])
def test_cross_page_join_refuses_unsupported_or_ambiguous_source(fault):
    units, records, sizes = fixture()
    if fault == "page-gap":
        records[3] = records.pop(1)
        units[1] = units[1].model_copy(update={"source_refs": (units[1].source_refs[0].model_copy(update={"page_index": 3}),)})
    elif fault == "uncertain":
        records[1][0]["bbox_confidence"] = 0.4
    elif fault in {"other-column", "not-bottom"}:
        i = 1 if fault == "other-column" else 0
        box = (410, 75, 550, 100) if i else (72, 300, 370, 330)
        units[i] = units[i].model_copy(update={"source_refs": (units[i].source_refs[0].model_copy(update={"bbox": box}),)})
    elif fault == "finished":
        units[0] = units[0].model_copy(update={"source_text": "A complete sentence."})
    else:
        units.append(units[1].model_copy(update={"unit_id": "intervening", "source_text": "A new section heading", "source_refs": (units[1].source_refs[0].model_copy(update={"bbox": (72, 65, 370, 70)}),)}))
        records[1].append({**records[1][0], "unit_id": "intervening", "fragment_id": "extra"})
    prepared, _, evidence = join_cross_page_words(units, records, sizes)
    assert not evidence
    assert prepared == units


def test_cross_page_join_rejects_two_competing_top_fragments():
    units, records, sizes = fixture()
    units.append(units[1].model_copy(update={"unit_id": "duplicate"}))
    records[1].append({**records[1][0], "unit_id": "duplicate", "fragment_id": "extra"})
    prepared, _, evidence = join_cross_page_words(units, records, sizes)
    assert not evidence
    assert prepared == units
