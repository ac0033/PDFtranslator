import json
from pathlib import Path

import pymupdf
import pytest

from pdf_translation.acceptance import (
    DELIVERABLE,
    DELIVERABLE_WITH_KNOWN_ISSUES,
    NOT_DELIVERABLE,
    build_acceptance_report,
    format_summary,
    page_output_paths,
)


def fixture_delivery(tmp_path, rows, *, pages=2, render_pages=None, books=('mono', 'dual'), book_pages=None):
    """Build a rendered delivery: a source PDF, per-page output and the books."""
    source = tmp_path / 'source.pdf'
    with pymupdf.open() as doc:
        for number in range(pages):
            doc.new_page().insert_text((30, 60), f'Page {number + 1}')
        doc.save(source)
    render_dir = tmp_path / 'render'
    render_dir.mkdir()
    for number in (range(1, pages + 1) if render_pages is None else render_pages):
        mono, dual = page_output_paths(render_dir, source.stem, number)
        mono.parent.mkdir(parents=True, exist_ok=True)
        with pymupdf.open(source) as original, pymupdf.open() as single:
            single.insert_pdf(original, from_page=number - 1, to_page=number - 1)
            single.save(mono)
            single.save(dual)
    for kind in books:
        with pymupdf.open(source) as original, pymupdf.open() as book:
            book.insert_pdf(original, from_page=0, to_page=(book_pages or pages) - 1)
            book.save(render_dir / f'source.usable-deepseek.zh.{kind}.pdf')
    queue = render_dir / 'content-review.jsonl'
    queue.write_text('\n'.join(json.dumps(row) for row in rows), encoding='utf-8')
    return source, render_dir, queue


def unit(unit_id, *, state='accepted', pages=(1,), issues=(), translated='译文', markers=()):
    return {
        'unit_id': unit_id,
        'source_pages': list(pages),
        'source_text': 'source',
        'translated_text': translated,
        'state': state,
        'issues': [dict(issue) for issue in issues],
        'unresolved_formula_markers': list(markers),
    }


def issue(category, severity='major'):
    return {'issue_id': f'i:{category}', 'category': category, 'severity': severity, 'explanation': category}


def test_a_clean_run_is_deliverable(tmp_path):
    rows = [unit(f'u{i}') for i in range(10)]
    source, render_dir, queue = fixture_delivery(tmp_path, rows)
    report = build_acceptance_report(source, render_dir, queue)
    assert report['verdict'] == DELIVERABLE
    assert report['major_problems'] == []
    assert report['minor_issues'] == []
    assert report['render']['rendered_pages'] == 2
    assert report['units']['states'] == {'accepted': 10}


def test_minor_issues_are_recorded_without_blocking_delivery(tmp_path):
    rows = [unit(f'u{i}') for i in range(90)]
    rows += [unit(f'f{i}', state='needs_review', pages=(2,), issues=[issue('protected_source_literal_unavailable')], markers=['m']) for i in range(10)]
    source, render_dir, queue = fixture_delivery(tmp_path, rows)
    report = build_acceptance_report(source, render_dir, queue)
    assert report['verdict'] == DELIVERABLE_WITH_KNOWN_ISSUES
    assert report['major_problems'] == []
    recorded = {entry['category']: entry for entry in report['minor_issues']}
    assert recorded['protected_source_literal_unavailable']['units'] == 10
    assert recorded['protected_source_literal_unavailable']['sample_pages'] == [2]
    outstanding = report['coverage']['human_review_outstanding']
    assert outstanding['unresolved_formula_markers'] == 10
    assert outstanding['pages_with_unresolved_formulas'] == 1


def test_a_blocked_unit_stops_delivery(tmp_path):
    rows = [unit('u0'), unit('u1', state='blocked', pages=(2,))]
    source, render_dir, queue = fixture_delivery(tmp_path, rows)
    report = build_acceptance_report(source, render_dir, queue)
    assert report['verdict'] == NOT_DELIVERABLE
    blocked = [p for p in report['major_problems'] if p['kind'] == 'unit_state_blocked']
    assert blocked and blocked[0]['sample_pages'] == [2]


def test_an_exhausted_unit_stops_delivery(tmp_path):
    rows = [unit('u0'), unit('u1', state='exhausted', translated='')]
    source, render_dir, queue = fixture_delivery(tmp_path, rows)
    report = build_acceptance_report(source, render_dir, queue)
    assert report['verdict'] == NOT_DELIVERABLE
    kinds = {p['kind'] for p in report['major_problems']}
    assert {'unit_state_exhausted', 'missing_translation'} <= kinds


def test_a_critical_quality_issue_stops_delivery(tmp_path):
    rows = [unit('u0'), unit('u1', state='needs_review', issues=[issue('protected_marker', 'critical')])]
    source, render_dir, queue = fixture_delivery(tmp_path, rows)
    report = build_acceptance_report(source, render_dir, queue)
    assert report['verdict'] == NOT_DELIVERABLE
    critical = [p for p in report['major_problems'] if p['kind'] == 'critical_quality_issue']
    assert critical and critical[0]['sample_units'] == ['u1']
    # A critical issue must not be double counted as a tolerable minor one.
    assert report['minor_issues'] == []


def test_a_missing_rendered_page_stops_delivery(tmp_path):
    rows = [unit('u0')]
    source, render_dir, queue = fixture_delivery(tmp_path, rows, pages=3, render_pages=[1, 3])
    report = build_acceptance_report(source, render_dir, queue)
    assert report['verdict'] == NOT_DELIVERABLE
    missing = [p for p in report['major_problems'] if p['kind'] == 'missing_rendered_page']
    assert missing and missing[0]['pages'] == [2]


def test_a_half_written_page_counts_as_missing(tmp_path):
    rows = [unit('u0')]
    source, render_dir, queue = fixture_delivery(tmp_path, rows, pages=2, render_pages=[1, 2])
    page_output_paths(render_dir, source.stem, 2)[1].unlink()
    report = build_acceptance_report(source, render_dir, queue)
    assert report['verdict'] == NOT_DELIVERABLE
    assert [p['pages'] for p in report['major_problems'] if p['kind'] == 'missing_rendered_page'] == [[2]]


def test_a_missing_book_stops_delivery(tmp_path):
    rows = [unit('u0')]
    source, render_dir, queue = fixture_delivery(tmp_path, rows, books=('mono',))
    report = build_acceptance_report(source, render_dir, queue)
    assert report['verdict'] == NOT_DELIVERABLE
    assert any(p['kind'] == 'missing_book' for p in report['major_problems'])


def test_a_short_book_stops_delivery(tmp_path):
    rows = [unit('u0')]
    source, render_dir, queue = fixture_delivery(tmp_path, rows, pages=3, book_pages=2)
    report = build_acceptance_report(source, render_dir, queue)
    assert report['verdict'] == NOT_DELIVERABLE
    mismatch = [p for p in report['major_problems'] if p['kind'] == 'page_count_mismatch']
    assert len(mismatch) == 2


def test_accumulated_minor_issues_escalate_per_category(tmp_path):
    # Ten percent of units with a changed number inventory is far past the
    # strict threshold for that category, even though nothing is critical.
    rows = [unit(f'u{i}') for i in range(90)]
    rows += [unit(f'n{i}', state='needs_review', issues=[issue('number')]) for i in range(10)]
    source, render_dir, queue = fixture_delivery(tmp_path, rows)
    report = build_acceptance_report(source, render_dir, queue)
    assert report['verdict'] == NOT_DELIVERABLE
    escalated = [p for p in report['major_problems'] if p['kind'] == 'minor_issue_accumulation']
    assert escalated and escalated[0]['category'] == 'number'


def test_the_same_volume_of_formula_placeholders_stays_deliverable(tmp_path):
    # Same share, different category: a known capability boundary is tolerated
    # where a real-error signal is not.
    rows = [unit(f'u{i}') for i in range(90)]
    rows += [unit(f'p{i}', state='needs_review', issues=[issue('protected_source_literal_unavailable')]) for i in range(10)]
    source, render_dir, queue = fixture_delivery(tmp_path, rows)
    report = build_acceptance_report(source, render_dir, queue)
    assert report['verdict'] == DELIVERABLE_WITH_KNOWN_ISSUES


def test_an_unknown_issue_category_uses_the_strict_default(tmp_path):
    rows = [unit(f'u{i}') for i in range(90)]
    rows += [unit(f'x{i}', state='needs_review', issues=[issue('something_new')]) for i in range(10)]
    source, render_dir, queue = fixture_delivery(tmp_path, rows)
    report = build_acceptance_report(source, render_dir, queue)
    assert report['verdict'] == NOT_DELIVERABLE
    assert [p['category'] for p in report['major_problems'] if p['kind'] == 'minor_issue_accumulation'] == ['something_new']


def test_thresholds_are_overridable(tmp_path):
    rows = [unit(f'u{i}') for i in range(90)]
    rows += [unit(f'n{i}', state='needs_review', issues=[issue('number')]) for i in range(10)]
    source, render_dir, queue = fixture_delivery(tmp_path, rows)
    report = build_acceptance_report(source, render_dir, queue, thresholds={'number': 0.5})
    assert report['verdict'] == DELIVERABLE_WITH_KNOWN_ISSUES
    assert report['thresholds']['per_category']['number'] == 0.5


def test_a_stale_queue_that_hides_blocking_work_stops_delivery(tmp_path):
    rows = [unit(f'u{i}') for i in range(10)]
    source, render_dir, queue = fixture_delivery(tmp_path, rows)
    report = build_acceptance_report(source, render_dir, queue, store_states={'accepted': 9, 'blocked': 1})
    assert report['verdict'] == NOT_DELIVERABLE
    assert any(p['kind'] == 'stale_review_queue' for p in report['major_problems'])


def test_a_store_that_merely_differs_in_counts_does_not_stop_delivery(tmp_path):
    rows = [unit(f'u{i}') for i in range(10)]
    source, render_dir, queue = fixture_delivery(tmp_path, rows)
    report = build_acceptance_report(source, render_dir, queue, store_states={'accepted': 12})
    assert report['verdict'] == DELIVERABLE


def test_the_report_states_what_it_did_not_check(tmp_path):
    rows = [unit('u0', markers=['m1', 'm2'])]
    source, render_dir, queue = fixture_delivery(tmp_path, rows)
    report = build_acceptance_report(source, render_dir, queue)
    coverage = report['coverage']
    assert any('faithful translation' in item for item in coverage['not_checked'])
    assert coverage['human_review_outstanding']['unresolved_formula_markers'] == 2
    assert 'not evidence about any other document' in coverage['limitation']


def test_summary_leads_with_the_verdict_and_names_blocking_problems(tmp_path):
    rows = [unit('u0'), unit('u1', state='blocked', pages=(2,))]
    source, render_dir, queue = fixture_delivery(tmp_path, rows)
    text = format_summary(build_acceptance_report(source, render_dir, queue))
    assert text.splitlines()[0] == f'verdict: {NOT_DELIVERABLE}'
    assert 'unit_state_blocked' in text
    assert 'not machine checkable' in text
