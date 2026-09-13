from pdf_translation.document_context import enrich_pdf_units_with_context
from pdf_translation.hashing import stable_hash
from pdf_translation.schemas import SourceRef, TranslationUnit


def unit(unit_id: str, text: str, page: int, y: float, unit_type: str = "paragraph") -> TranslationUnit:
    return TranslationUnit(
        unit_id=unit_id,
        document_id="doc",
        ir_revision="ir1",
        unit_type=unit_type,
        source_lang="en",
        target_lang="zh-CN",
        source_text=text,
        source_hash=stable_hash(text),
        source_refs=(SourceRef(block_id=unit_id, page_index=page, bbox=(40, y, 500, y + 10), char_start=0, char_end=len(text)),),
    )


def test_pdf_context_uses_reading_order_and_detected_section():
    values = [
        unit("body-2", "There are different representations.", 41, 120),
        unit("heading", "2.2 Probability Distributions", 41, 60),
        unit("body-1", "A distribution assigns probabilities.", 41, 90),
    ]
    enriched = {value.unit_id: value for value in enrich_pdf_units_with_context(values)}
    assert enriched["body-1"].section_path == ("2.2 Probability Distributions",)
    assert enriched["body-1"].neighbor_ids == ("heading", "body-2")
    assert enriched["body-2"].neighbor_ids == ("body-1",)


def test_pdf_context_preserves_chapter_and_nested_section_hierarchy():
    values = [
        unit("chapter", "20 chapter 2.representation", 41, 30),
        unit("section", "2.2 Probability Distributions", 41, 60),
        unit("subsection", "2.2.1 Discrete Probability Distributions", 41, 90),
        unit("body", "A distribution assigns probabilities.", 41, 120),
    ]
    enriched = {value.unit_id: value for value in enrich_pdf_units_with_context(values)}
    assert enriched["body"].section_path == (
        "chapter 2.representation",
        "2.2 Probability Distributions",
        "2.2.1 Discrete Probability Distributions",
    )


def test_pdf_context_does_not_leak_between_nonconsecutive_sample_pages():
    values = [unit("p42", "First sample.", 41, 90), unit("p51", "Second sample.", 50, 90)]
    enriched = {value.unit_id: value for value in enrich_pdf_units_with_context(values)}
    assert enriched["p42"].neighbor_ids == ()
    assert enriched["p51"].neighbor_ids == ()
    assert enriched["p42"].section_path == ("Physical page 42",)
    assert enriched["p51"].section_path == ("Physical page 51",)


def test_pdf_context_can_cross_consecutive_pages_but_skips_protected_units():
    values = [
        unit("body-a", "Paragraph continues.", 10, 700),
        unit("formula", "⟦P:eq⟧", 11, 40, unit_type="protected"),
        unit("body-b", "Continuation on next page.", 11, 70),
    ]
    enriched = {value.unit_id: value for value in enrich_pdf_units_with_context(values)}
    assert enriched["body-a"].neighbor_ids == ("body-b",)
    assert enriched["body-b"].neighbor_ids == ("body-a",)
    assert enriched["formula"].neighbor_ids == ()


def test_pdf_context_does_not_cross_section_boundary():
    values = [
        unit("old-body", "Previous section ends here.", 41, 90),
        unit("new-heading", "2.3 Bayesian Networks", 41, 120),
        unit("new-body", "A network represents dependencies.", 41, 150),
    ]
    values[0] = values[0].model_copy(update={"section_path": ("2.2 Probability Distributions",)})
    enriched = {value.unit_id: value for value in enrich_pdf_units_with_context(values)}
    assert enriched["old-body"].neighbor_ids == ()
    assert enriched["new-heading"].neighbor_ids == ("new-body",)
    assert enriched["new-body"].neighbor_ids == ("new-heading",)


def test_pdf_context_excludes_copyright_footer_and_recomputes_stale_links():
    values = [
        unit("body-a", "A distribution assigns probability.", 41, 90),
        unit("footer", "© 2022 MIT, shared under Creative Commons CC-BY-NC-ND.", 41, 600),
        unit("body-b", "The density integrates to one.", 42, 90),
    ]
    values[0] = values[0].model_copy(update={"neighbor_ids": ("footer",)})
    enriched = {value.unit_id: value for value in enrich_pdf_units_with_context(values)}
    assert enriched["body-a"].neighbor_ids == ("body-b",)
    assert enriched["body-b"].neighbor_ids == ("body-a",)
    assert enriched["footer"].neighbor_ids == ()
