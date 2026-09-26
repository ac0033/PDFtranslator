"""Reconciling what a page shows against what the capture layer delivered.

The gap this closes is specific: text that never became a unit is invisible to
every other check in the project, so a silent capture failure reads as a clean
book. The exclusions matter as much as the detection - without them val's Julia
listings alone would bury the real gaps under two thousand hits.
"""
import json

import pymupdf
import pytest

from pdf_translation.capture_coverage import (
    MATH_OPERATOR_WORDS,
    is_prose_font,
    page_prose_words,
    reconcile_capture,
    uncaptured_words,
)


def make_pdf(path, spans, pages=1):
    """spans: list of (text, fontname) laid out one line apart."""
    with pymupdf.open() as doc:
        for _ in range(pages):
            page = doc.new_page()
            y = 60
            for text, font in spans:
                page.insert_text((40, y), text, fontname=font, fontsize=11)
                y += 20
        doc.save(path)
    return path


def write_capture_map(path, per_page):
    """per_page: {page_index: [delivered text, ...]}"""
    lines = []
    for page_index, texts in per_page.items():
        for order, text in enumerate(texts):
            lines.append(json.dumps({"page_index": page_index, "sequence": order, "babeldoc_text": text}))
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def test_code_and_formula_fonts_are_not_prose():
    assert is_prose_font("TeXGyrePagella-Regular")
    assert is_prose_font("URWPalladioL-Roma")
    # Computer Modern text faces are not mathematical glyphs just because
    # their names begin with CM; otherwise LaTeX textbooks escape the audit.
    for font in ("CMR10", "CMBX12", "CMTI10", "CMSL10"):
        assert is_prose_font(font)
    assert not is_prose_font("CMMI10")
    assert not is_prose_font("CMEX10")
    assert not is_prose_font("JuliaMono-Medium")
    assert not is_prose_font("Courier")
    assert not is_prose_font("CMSY10")
    assert not is_prose_font("PazoMath-Bold")
    # An unknown font counts as prose: excluding what we cannot identify would
    # hide the very gaps this check exists to find.
    assert is_prose_font("")
    assert is_prose_font("SomeUnknownFace")


def test_operator_names_are_part_of_the_formula():
    for name in ("log", "exp", "arg", "max", "min", "otherwise"):
        assert name in MATH_OPERATOR_WORDS


def test_prose_words_skip_code_and_operators(tmp_path):
    source = make_pdf(tmp_path / 'p.pdf', [
        ('the quick brown fox jumps', 'helv'),
        ('struct Vector return function', 'cour'),
        ('log exp arg max', 'helv'),
    ])
    with pymupdf.open(source) as doc:
        words = page_prose_words(doc[0])
    assert set(words) == {'the', 'quick', 'brown', 'fox', 'jumps'}


def test_a_word_the_capture_merged_into_its_neighbour_counts_as_captured():
    source = {'true': 1, 'boolean': 1, 'table': 1}
    assert uncaptured_words(source, 'true boolean table') == {}
    # "of boolean" arriving as "ofboolean" still reached the translator.
    assert uncaptured_words(source, 'true ofboolean table') == {}
    assert uncaptured_words(source, 'boolean table') == {'true': 1}


def test_repeated_cells_are_counted_once_per_occurrence():
    # The val page-88 shape: 27 truth-table cells, none delivered.
    source = {'true': 27, 'value': 3}
    assert uncaptured_words(source, 'value value value') == {'true': 27}


def test_a_clean_capture_reports_nothing(tmp_path):
    source = make_pdf(tmp_path / 'clean.pdf', [('alpha beta gamma delta', 'helv')])
    capture = write_capture_map(tmp_path / 'capture-map.jsonl', {0: ['alpha beta gamma delta']})
    report = reconcile_capture(source, capture)
    assert report['uncaptured_words'] == 0
    assert report['significant_pages'] == []
    assert report['over_threshold'] is False


def test_an_uncaptured_cell_is_reported_with_its_page(tmp_path):
    source = make_pdf(tmp_path / 'gap.pdf', [
        ('alpha beta gamma delta epsilon zeta', 'helv'),
        ('true true true true', 'helv'),
    ])
    capture = write_capture_map(tmp_path / 'capture-map.jsonl', {0: ['alpha beta gamma delta epsilon zeta']})
    report = reconcile_capture(source, capture)
    assert report['uncaptured_words'] == 4
    assert report['significant_pages'][0]['page'] == 1
    assert report['significant_pages'][0]['words'] == {'true': 4}


def test_code_listings_are_not_reported_as_gaps(tmp_path):
    """A whole page of undelivered Julia is the pipeline working as designed."""
    source = make_pdf(tmp_path / 'code.pdf', [
        ('alpha beta gamma delta', 'helv'),
        ('struct Vector Float rand length return', 'cour'),
    ])
    capture = write_capture_map(tmp_path / 'capture-map.jsonl', {0: ['alpha beta gamma delta']})
    report = reconcile_capture(source, capture)
    assert report['uncaptured_words'] == 0


def test_one_stray_word_is_below_the_significance_floor(tmp_path):
    source = make_pdf(tmp_path / 'stray.pdf', [('alpha beta gamma delta epsilon', 'helv'), ('stray', 'helv')])
    capture = write_capture_map(tmp_path / 'capture-map.jsonl', {0: ['alpha beta gamma delta epsilon']})
    report = reconcile_capture(source, capture)
    assert report['uncaptured_words'] == 1
    assert report['pages_with_uncaptured'] == 1
    assert report['significant_pages'] == []


def test_a_page_the_capture_never_saw_is_wholly_uncaptured(tmp_path):
    source = make_pdf(tmp_path / 'two.pdf', [('alpha beta gamma delta', 'helv')], pages=2)
    capture = write_capture_map(tmp_path / 'capture-map.jsonl', {0: ['alpha beta gamma delta']})
    report = reconcile_capture(source, capture)
    assert [page['page'] for page in report['significant_pages']] == [2]
    assert report['significant_pages'][0]['share'] == 1.0


def test_losing_most_of_a_document_crosses_the_threshold(tmp_path):
    source = make_pdf(tmp_path / 'lost.pdf', [('alpha beta gamma delta epsilon zeta eta theta', 'helv')], pages=4)
    capture = write_capture_map(tmp_path / 'capture-map.jsonl', {0: ['alpha beta gamma delta epsilon zeta eta theta']})
    report = reconcile_capture(source, capture)
    assert report['over_threshold'] is True
    assert report['uncaptured_share'] > 0.5


def test_the_report_states_its_own_scope(tmp_path):
    source = make_pdf(tmp_path / 's.pdf', [('alpha beta gamma', 'helv')])
    capture = write_capture_map(tmp_path / 'capture-map.jsonl', {0: ['alpha beta gamma']})
    scope = reconcile_capture(source, capture)['scope']
    assert 'monospace' in scope
    assert 'Digits are excluded' in scope
    assert 'says nothing about whether the units that do exist were translated correctly' in scope
