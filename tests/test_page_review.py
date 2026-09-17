import json
from pathlib import Path
import pymupdf
import pytest
from pdf_translation.hashing import sha256_file
from pdf_translation.page_review import build_page_review
from pdf_translation.babeldoc_adapter import review_draft_renderable


def fixture_run(tmp_path):
    source = tmp_path / 'source.pdf'
    with pymupdf.open() as doc:
        doc.new_page().insert_text((30, 60), 'Value 42')
        doc.save(source)
    report = dict(source_sha256=sha256_file(source), physical_pages=[1])
    unit = dict(unit_id='u', source_hash='hash', source_text='Value 42', source_refs=[dict(page_index=0)])
    (tmp_path / 'run.report.json').write_text(json.dumps(report))
    (tmp_path / 'units.jsonl').write_text(json.dumps(unit))
    return source, report, unit


def test_missing_output_and_translation_remain_blocked(tmp_path):
    source, _, _ = fixture_run(tmp_path)
    build_page_review(source, tmp_path)
    review = json.loads((tmp_path / 'page-review.json').read_text())
    assert review['page_count'] == 1
    assert review['status'] == 'blocked'
    assert 'missing_or_unaccepted_translation' in review['pages'][0]['blockers']
    assert review['pages'][0]['meaning_review'] is None
    assert (tmp_path / review['pages'][0]['source_image']).is_file()


def test_unchanged_pdf_never_implies_semantic_approval(tmp_path):
    source, report, unit = fixture_run(tmp_path)
    report['mono_pdf'] = source.name
    (tmp_path / 'run.report.json').write_text(json.dumps(report))
    row = {**unit, 'translated_text': 'Value 42', 'state': 'accepted'}
    (tmp_path / 'content-review.jsonl').write_text(json.dumps(row))
    build_page_review(source, tmp_path)
    result = json.loads((tmp_path / 'page-review.json').read_text())
    assert result['pages'][0]['automated_checks_passed']
    assert result['status'] == 'pending_review'
    assert result['formal_use_ready'] is False


def test_changed_source_rejected(tmp_path):
    source, _, _ = fixture_run(tmp_path)
    source.write_bytes(source.read_bytes() + b'changed')
    with pytest.raises(ValueError, match='hash mismatch'):
        build_page_review(source, tmp_path)


def test_missing_numeric_glyph_blocks_page(tmp_path):
    source, report, unit = fixture_run(tmp_path)
    target = tmp_path / 'target.pdf'
    with pymupdf.open() as doc:
        doc.new_page().insert_text((30, 60), 'Value')
        doc.save(target)
    report['mono_pdf'] = target.name
    (tmp_path / 'run.report.json').write_text(json.dumps(report))
    (tmp_path / 'content-review.jsonl').write_text(json.dumps({**unit, 'translated_text': 'Value', 'state': 'accepted'}))
    build_page_review(source, tmp_path)
    result = json.loads((tmp_path / 'page-review.json').read_text())
    assert 'numeric_symbol_glyphs_text_font_size_bbox_equal' in result['pages'][0]['blockers']
    assert result['status'] == 'blocked'


def test_stale_review_source_rejected(tmp_path):
    source, _, unit = fixture_run(tmp_path)
    (tmp_path / 'content-review.jsonl').write_text(json.dumps({**unit, 'source_text': 'wrong'}))
    with pytest.raises(ValueError, match='differs'):
        build_page_review(source, tmp_path)


def test_draft_gate_rejects_critical_and_partial_results():
    warning = dict(state='needs_review', quality={'issues': [{'severity': 'major'}]})
    assert review_draft_renderable([warning], 1)
    assert not review_draft_renderable([warning], 2)
    assert not review_draft_renderable([], 0)
    assert not review_draft_renderable([dict(state='needs_review', quality={'issues': [{'severity': 'critical'}]})], 1)
    assert not review_draft_renderable([dict(state='prepared', quality=None)], 1)

def test_agent_observations_are_bound_to_pdf_hashes(tmp_path):
    source, _, _ = fixture_run(tmp_path)
    (tmp_path / 'agent-review.json').write_text(json.dumps({'source_sha256': 'wrong', 'target_sha256': None, 'pages': []}))
    with pytest.raises(ValueError, match='different PDF artifacts'):
        build_page_review(source, tmp_path)


def test_agent_review_must_cover_all_pages(tmp_path):
    source, report, _ = fixture_run(tmp_path)
    (tmp_path / 'agent-review.json').write_text(json.dumps({'source_sha256': report['source_sha256'], 'target_sha256': None, 'pages': []}))
    with pytest.raises(ValueError, match='each selected page'):
        build_page_review(source, tmp_path)

def test_symbolic_assignments_preserved_but_prose_not_exempted():
    from pdf_translation.semantic_layout import is_formula_only
    assert is_formula_only('⟦P:layout-x⟧z ⟦P:eq⟧Wx ⟦P:plus⟧b ⟦P:layout-y⟧a ⟦P:sigma⟧(z)')
    for source in ['a neuron', 'x ⟦P:equals⟧ in R', 'x ⟦P:p⟧ or b', '1.2 Neural Networks', 'This is ⟦P:p⟧', 'a ⟦P:p⟧ is a variable']:
        assert not is_formula_only(source)

def test_code_letter_change_is_detected_without_numeric_change(tmp_path):
    from pdf_translation.pdf_fidelity import compare_visual_primitives
    with pymupdf.open() as source, pymupdf.open() as target:
        a = source.new_page()
        b = target.new_page()
        a.insert_text((50, 80), 'x = y', fontname='cour')
        b.insert_text((50, 80), 'x = z', fontname='cour')
        audit = compare_visual_primitives(a, b)
        assert not audit['checks']['code_font_glyphs_text_font_size_bbox_equal']
        assert audit['source_code_font_glyph_count'] == 3
        assert not audit['passed']

def test_tolerant_glyph_matching_ignores_float_sort_order_without_relaxing_tolerance():
    from pdf_translation.pdf_fidelity import _formula_font_glyphs_equal
    a = {('Mono', 't'): [(8., (100., 500., 105., 508.)), (8., (100.00004, 100., 105., 108.))]}
    b = {('Mono', 't'): [(8., (100., 100., 105., 108.)), (8., (100.00004, 500., 105., 508.))]}
    assert _formula_font_glyphs_equal(a, b)
    b[('Mono', 't')][0] = (8., (100., 101., 105., 109.))
    assert not _formula_font_glyphs_equal(a, b)


def test_review_uses_hash_bound_prepared_semantic_units(tmp_path):
    source, report, unit = fixture_run(tmp_path)
    prepared = {**unit, 'unit_id': 'joined', 'source_text': 'Joined source'}
    path = tmp_path / 'prepared-units.jsonl'
    path.write_text(json.dumps(prepared))
    report.update(translation_units_jsonl=path.name, translation_units_sha256=sha256_file(path))
    (tmp_path / 'run.report.json').write_text(json.dumps(report))
    (tmp_path / 'content-review.jsonl').write_text(json.dumps({**prepared, 'translated_text': '完整译文', 'state': 'accepted'}), encoding='utf-8')
    build_page_review(source, tmp_path)
    result = json.loads((tmp_path / 'page-review.json').read_text(encoding='utf-8'))
    assert result['pages'][0]['units'][0]['unit_id'] == 'joined'
    path.write_text(json.dumps(unit))
    with pytest.raises(ValueError, match='units hash mismatch'):
        build_page_review(source, tmp_path)
