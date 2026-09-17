from pathlib import Path

import pymupdf

from pdf_translation.hashing import stable_hash
from pdf_translation.protected_literals import _compact, enrich_units_with_protected_literals, recover_protected_literal_hints
from pdf_translation.schemas import ArtifactRef, ProtectedSpan, SourceRef, TranslationUnit


def test_recovers_unique_formula_literals_without_changing_marker():
    document = pymupdf.open()
    page = document.new_page(width=500, height=200)
    page.insert_text((40, 80), "For example P(X = 1), then P(X = n) is valid.", fontsize=12)
    record = {
        "bbox": (35, 60, 450, 95), "bbox_confidence": 0.99,
        "babeldoc_text": "For example P(X <b1>), then P(X <b2>n) is valid.",
        "marker_map": {"marker-one": "<b1>", "marker-two": "<b2>"},
    }
    assert recover_protected_literal_hints(page, record) == {
        "marker-one": "=1", "marker-two": "=",
    }
    document.close()


def test_ambiguous_or_unanchored_literals_are_not_guessed():
    document = pymupdf.open()
    page = document.new_page(width=500, height=200)
    page.insert_text((40, 70), "P(X = 1) is valid.", fontsize=12)
    page.insert_text((40, 90), "P(X = 2) is valid.", fontsize=12)
    record = {
        "bbox": (35, 55, 450, 105), "bbox_confidence": 0.99,
        "babeldoc_text": "P(X <b1>) is valid.", "marker_map": {"m": "<b1>"},
    }
    assert recover_protected_literal_hints(page, record) == {}
    assert recover_protected_literal_hints(page, {**record, "bbox_confidence": 0.4}) == {}
    assert recover_protected_literal_hints(page, {**record, "babeldoc_text": "<b1> is valid."}) == {}
    document.close()


def test_existing_capture_enrichment_keeps_file_and_adds_verified_hint(tmp_path: Path):
    source = tmp_path / "source.pdf"
    document = pymupdf.open()
    page = document.new_page(width=500, height=200)
    page.insert_text((40, 80), "For example P(X = 1) is valid.", fontsize=12)
    document.save(source)
    document.close()
    marker = "⟦P:formula-one⟧"
    source_text = f"For example P(X {marker}) is valid."
    source_ref = SourceRef(block_id="b1", page_index=0, bbox=(35, 60, 450, 95), char_start=0, char_end=len(source_text))
    span = ProtectedSpan(
        occurrence_id="one", marker=marker, kind="formula",
        original=ArtifactRef(uri="placeholder/one", sha256="0" * 64, media_type="text/plain"),
        source_ref=source_ref,
    )
    unit = TranslationUnit(
        unit_id="u1", document_id="d1", ir_revision="test", unit_type="paragraph",
        source_lang="en", target_lang="zh-CN", source_text=source_text,
        source_hash=stable_hash(source_text), protected=(span,), source_refs=(source_ref,),
    )
    record = {
        "unit_id": "u1", "bbox": (35, 60, 450, 95), "bbox_confidence": 0.99,
        "babeldoc_text": "For example P(X <b1>) is valid.",
        "marker_map": {marker: "<b1>"},
    }
    enriched = enrich_units_with_protected_literals(source, [unit], {0: [record]})
    assert unit.protected_literal_hints == {}
    assert enriched[0].protected_literal_hints == {marker: "=1"}
    assert record["protected_literal_hints"] == {marker: "=1"}


def test_full_sentence_recovers_inequality_around_formula():
    document = pymupdf.open()
    page = document.new_page(width=500, height=200)
    page.insert_text((40, 80), "If P(A) = 1 and P(A) = 0, then 0 <= P(A) <= 1.", fontsize=12)
    record = {
        "bbox": (35, 60, 450, 95), "bbox_confidence": 0.99,
        "babeldoc_text": "If P(A) <b1> and P(A) <b2>, then <b3>P(A) <b4>.",
        "marker_map": {f"m{i}": f"<b{i}>" for i in range(1, 5)},
    }
    assert recover_protected_literal_hints(page, record) == {
        "m1": "=1", "m2": "=0", "m3": "0<=", "m4": "<=1",
    }
    document.close()


def test_leading_footnote_number_requires_unique_native_line():
    document = pymupdf.open()
    page = document.new_page(width=500, height=200)
    page.insert_text((40, 80), "6 We will often use this notation.", fontsize=12)
    record = {
        "bbox": (35, 60, 450, 95), "bbox_confidence": 0.99,
        "babeldoc_text": "<b1>We will often use this notation.",
        "marker_map": {"footnote": "<b1>"},
    }
    assert recover_protected_literal_hints(page, record) == {"footnote": "6"}
    page.insert_text((40, 100), "7 We will often use this notation.", fontsize=12)
    assert recover_protected_literal_hints(page, {**record, "bbox": (35, 60, 450, 110)}) == {}
    document.close()


def test_combining_not_equal_glyph_is_readable_to_translator():
    assert _compact("P\u0338 = NP") == "P≠NP"
    document = pymupdf.open()
    page = document.new_page(width=500, height=200)
    page.insert_text((40, 80), "The case P/=NP remains open.", fontsize=12)
    record = {
        "bbox": (35, 60, 450, 95), "bbox_confidence": 0.99,
        "babeldoc_text": "The case P<b1>NP remains open.",
        "marker_map": {"not-equal": "<b1>"},
    }
    assert recover_protected_literal_hints(page, record) == {"not-equal": "/="}
    document.close()


def test_untrusted_bbox_falls_back_to_a_unique_match_on_the_page():
    """A single match on the whole page is a stronger claim than a bad box."""
    document = pymupdf.open()
    page = document.new_page(width=500, height=200)
    page.insert_text((40, 80), "The value P(X = 7) governs the result.", fontsize=12)
    record = {
        "bbox": (0, 0, 10, 10), "bbox_confidence": 0.2,
        "babeldoc_text": "The value P(X <b1>) governs the result.",
        "marker_map": {"m": "<b1>"},
    }
    assert recover_protected_literal_hints(page, record) == {"m": "=7"}
    document.close()


def test_the_page_fallback_still_refuses_an_ambiguous_match():
    document = pymupdf.open()
    page = document.new_page(width=500, height=200)
    page.insert_text((40, 70), "The value P(X = 7) governs the result.", fontsize=12)
    page.insert_text((40, 95), "The value P(X = 9) governs the result.", fontsize=12)
    record = {
        "bbox": (0, 0, 10, 10), "bbox_confidence": 0.2,
        "babeldoc_text": "The value P(X <b1>) governs the result.",
        "marker_map": {"m": "<b1>"},
    }
    assert recover_protected_literal_hints(page, record) == {}
    document.close()


def test_a_code_identifier_is_credible_context():
    """"the sys function" cannot be translated from an opaque marker."""
    document = pymupdf.open()
    page = document.new_page(width=500, height=200)
    page.insert_text((40, 80), "It calls the sys function to advance.", fontsize=12)
    record = {
        "bbox": (35, 60, 450, 95), "bbox_confidence": 0.99,
        "babeldoc_text": "It calls the <b1> function to advance.",
        "marker_map": {"m": "<b1>"},
    }
    assert recover_protected_literal_hints(page, record) == {"m": "sys"}
    document.close()


def test_a_dotted_package_name_is_credible_context():
    document = pymupdf.open()
    page = document.new_page(width=500, height=200)
    page.insert_text((40, 80), "We depend on the Turing.jl package here.", fontsize=12)
    record = {
        "bbox": (35, 60, 450, 95), "bbox_confidence": 0.99,
        "babeldoc_text": "We depend on the <b1> package here.",
        "marker_map": {"m": "<b1>"},
    }
    assert recover_protected_literal_hints(page, record) == {"m": "Turing.jl"}
    document.close()


def test_a_run_of_prose_is_not_a_literal():
    """A prose gap means the anchors failed to bound the marker."""
    document = pymupdf.open()
    page = document.new_page(width=500, height=200)
    page.insert_text((40, 80), "It calls the very slow and careful routine to advance.", fontsize=12)
    record = {
        "bbox": (35, 60, 450, 95), "bbox_confidence": 0.99,
        "babeldoc_text": "It calls the <b1> to advance.",
        "marker_map": {"m": "<b1>"},
    }
    assert recover_protected_literal_hints(page, record) == {}
    document.close()


def test_the_tightest_bound_that_explains_the_markers_wins():
    """A wider gap admits a second alignment and would lose the reading.

    With a 16-character gap only the first occurrence can match, so the literal
    is unique; with 32 both match and the marker would go unresolved.
    """
    document = pymupdf.open()
    page = document.new_page(width=600, height=200)
    page.insert_text((40, 70), "startX1end", fontsize=12)
    page.insert_text((40, 95), "startYYYYYYYYYYYYYYYYYYYY1end", fontsize=12)
    record = {
        "bbox": (35, 55, 560, 110), "bbox_confidence": 0.99,
        "babeldoc_text": "start<b1>end",
        "marker_map": {"m": "<b1>"},
    }
    assert recover_protected_literal_hints(page, record) == {"m": "X1"}
    document.close()
