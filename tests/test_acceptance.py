import json
from pathlib import Path

import pymupdf
import pytest

from pdf_translation.triage import ReviewLedger
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


def unit(unit_id, *, state='accepted', pages=(1,), issues=(), translated='译文', markers=(),
         source='source paragraph', result_hash=None):
    return {
        'unit_id': unit_id,
        'source_pages': list(pages),
        'source_text': source,
        'translated_text': translated,
        'state': state,
        'result_hash': result_hash or f'hash-{unit_id}',
        'issues': [dict(issue) for issue in issues],
        'unresolved_formula_markers': list(markers),
    }


def flagged(unit_id, category, **kwargs):
    """A unit whose warning triage cannot explain away.

    Number warnings need digits that really differ: a rule that dismissed these
    would be dismissing a real defect.
    """
    kwargs.setdefault('source', 'see chapter 1 of the reference work')
    kwargs.setdefault('translated', '参见该参考著作。')
    return unit(unit_id, state='needs_review', issues=[issue(category)], **kwargs)


class FakeStore:
    """Stands in for StateStore: unit_id -> (state, translated, issues, hash)."""

    def __init__(self, units):
        self.units = units

    def quality_reports(self, run_id):
        return [
            {'unit_id': uid, 'state': state, 'quality': {'issues': issues, 'result_hash': result_hash}}
            for uid, (state, _translated, issues, result_hash) in self.units.items()
        ]

    def results(self, run_id):
        return {
            uid: type('R', (), {'draft': type('D', (), {'translated_text': translated})()})()
            for uid, (_state, translated, _issues, _hash) in self.units.items()
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
    rows += [flagged(f'f{i}', 'protected_source_literal_unavailable', pages=(2,), markers=['m']) for i in range(10)]
    source, render_dir, queue = fixture_delivery(tmp_path, rows)
    report = build_acceptance_report(source, render_dir, queue)
    assert report['verdict'] == DELIVERABLE_WITH_KNOWN_ISSUES
    assert report['major_problems'] == []
    recorded = {entry['category']: entry for entry in report['minor_issues']}
    assert recorded['protected_source_literal_unavailable']['needs_human'] == 10
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
    rows = [unit('u0'), unit('u1', state='needs_review', issues=[issue('protected_marker', 'critical')], translated='译文内容')]
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
    rows += [flagged(f'n{i}', 'number') for i in range(10)]
    source, render_dir, queue = fixture_delivery(tmp_path, rows)
    report = build_acceptance_report(source, render_dir, queue)
    assert report['verdict'] == NOT_DELIVERABLE
    escalated = [p for p in report['major_problems'] if p['kind'] == 'minor_issue_accumulation']
    assert escalated and escalated[0]['category'] == 'number'


def test_the_same_volume_of_formula_placeholders_stays_deliverable(tmp_path):
    # Same share, different category: a known capability boundary is tolerated
    # where a real-error signal is not.
    rows = [unit(f'u{i}') for i in range(90)]
    rows += [flagged(f'p{i}', 'protected_source_literal_unavailable') for i in range(10)]
    source, render_dir, queue = fixture_delivery(tmp_path, rows)
    report = build_acceptance_report(source, render_dir, queue)
    assert report['verdict'] == DELIVERABLE_WITH_KNOWN_ISSUES


def test_an_unknown_issue_category_uses_the_strict_default(tmp_path):
    rows = [unit(f'u{i}') for i in range(90)]
    rows += [flagged(f'x{i}', 'something_new') for i in range(10)]
    source, render_dir, queue = fixture_delivery(tmp_path, rows)
    report = build_acceptance_report(source, render_dir, queue)
    assert report['verdict'] == NOT_DELIVERABLE
    assert [p['category'] for p in report['major_problems'] if p['kind'] == 'minor_issue_accumulation'] == ['something_new']


def test_thresholds_are_overridable(tmp_path):
    rows = [unit(f'u{i}') for i in range(90)]
    rows += [flagged(f'n{i}', 'number') for i in range(10)]
    source, render_dir, queue = fixture_delivery(tmp_path, rows)
    report = build_acceptance_report(source, render_dir, queue, thresholds={'number': 0.5})
    assert report['verdict'] == DELIVERABLE_WITH_KNOWN_ISSUES
    assert report['thresholds']['per_category']['number'] == 0.5


def test_the_store_overrides_a_queue_that_was_written_before_the_fixes(tmp_path):
    """val.pdf shipped with 25 units whose queue entry predated their fix.

    Grading the file would have reported defects the delivered book no longer
    had, and in the other direction would hide ones it did.
    """
    rows = [unit('u0'), unit('u1', state='blocked', translated='', pages=(2,))]
    source, render_dir, queue = fixture_delivery(tmp_path, rows)
    store = FakeStore({
        'u0': ('accepted', '译文', [], 'hash-u0'),
        'u1': ('accepted', '修好的译文', [], 'fixed-hash'),
    })
    report = build_acceptance_report(source, render_dir, queue, store=store, run_id='r')
    assert report['verdict'] == DELIVERABLE
    assert report['graded_from'] == 'run_store'
    assert report['review_queue']['stale_units'] == 1
    assert report['review_queue']['sample_stale_pages'] == [2]


def test_the_store_also_surfaces_a_defect_the_queue_predates(tmp_path):
    rows = [unit('u0'), unit('u1')]
    source, render_dir, queue = fixture_delivery(tmp_path, rows)
    store = FakeStore({
        'u0': ('accepted', '译文', [], 'hash-u0'),
        'u1': ('blocked', '', [issue('protected_marker', 'critical')], 'later-hash'),
    })
    report = build_acceptance_report(source, render_dir, queue, store=store, run_id='r')
    assert report['verdict'] == NOT_DELIVERABLE
    assert {p['kind'] for p in report['major_problems']} >= {'unit_state_blocked', 'critical_quality_issue'}


def test_without_a_store_the_report_says_staleness_was_not_checked(tmp_path):
    rows = [unit('u0')]
    source, render_dir, queue = fixture_delivery(tmp_path, rows)
    report = build_acceptance_report(source, render_dir, queue)
    assert report['graded_from'] == 'review_queue'
    assert 'staleness could not be checked' in report['review_queue']['note']


def test_a_ledger_dismissal_removes_a_warning_from_the_delivery_maths(tmp_path):
    rows = [unit(f'u{i}') for i in range(90)]
    rows += [flagged(f'n{i}', 'number') for i in range(10)]
    source, render_dir, queue = fixture_delivery(tmp_path, rows)
    assert build_acceptance_report(source, render_dir, queue)['verdict'] == NOT_DELIVERABLE
    ledger = ReviewLedger([
        {'unit_id': f'n{i}', 'result_hash': f'hash-n{i}', 'category': 'number', 'verdict': 'dismiss'}
        for i in range(10)
    ])
    report = build_acceptance_report(source, render_dir, queue, ledger=ledger)
    assert report['verdict'] == DELIVERABLE
    assert report['ledger']['dismissals_applied'] == 10


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
    assert text.splitlines()[0].startswith(f'verdict: {NOT_DELIVERABLE}')
    assert 'unit_state_blocked' in text
    assert 'not machine checkable' in text


def write_capture_map(path, per_page):
    lines = [json.dumps({'page_index': page, 'sequence': i, 'babeldoc_text': text})
             for page, texts in per_page.items() for i, text in enumerate(texts)]
    path.write_text("\n".join(lines), encoding='utf-8')
    return path


def test_capture_coverage_is_reported_when_the_capture_map_is_there(tmp_path):
    rows = [unit('u0')]
    source, render_dir, queue = fixture_delivery(tmp_path, rows)
    # fixture_delivery writes "Page 1"/"Page 2"; deliver only page 1's text.
    capture_map = write_capture_map(render_dir / 'capture-map.jsonl', {0: ['Page 1']})
    report = build_acceptance_report(source, render_dir, queue, capture_map=capture_map)
    coverage = report['capture_coverage']
    assert coverage is not None
    assert coverage['pages_reconciled'] == 2
    assert any('capture layer never delivered' in item for item in report['coverage']['checked'])


def test_losing_most_of_the_body_text_stops_delivery(tmp_path):
    """Text that never became a unit is invisible to every other check."""
    rows = [unit('u0')]
    source, render_dir, queue = fixture_delivery(tmp_path, rows, pages=3)
    capture_map = write_capture_map(render_dir / 'capture-map.jsonl', {0: ['Page 1']})
    report = build_acceptance_report(source, render_dir, queue, capture_map=capture_map)
    assert report['verdict'] == NOT_DELIVERABLE
    uncaptured = [p for p in report['major_problems'] if p['kind'] == 'uncaptured_prose']
    assert uncaptured and uncaptured[0]['pages'] == [2, 3]


def test_a_complete_capture_does_not_block_delivery(tmp_path):
    rows = [unit('u0')]
    source, render_dir, queue = fixture_delivery(tmp_path, rows, pages=3)
    capture_map = write_capture_map(render_dir / 'capture-map.jsonl',
                                    {0: ['Page 1'], 1: ['Page 2'], 2: ['Page 3']})
    report = build_acceptance_report(source, render_dir, queue, capture_map=capture_map)
    assert report['verdict'] == DELIVERABLE
    assert report['capture_coverage']['uncaptured_words'] == 0


def test_without_a_capture_map_coverage_is_simply_absent(tmp_path):
    rows = [unit('u0')]
    source, render_dir, queue = fixture_delivery(tmp_path, rows)
    report = build_acceptance_report(source, render_dir, queue, capture_map=render_dir / 'absent.jsonl')
    assert report['capture_coverage'] is None


def slice_delivery(tmp_path, rows, *, pages, span):
    """A rendered slice: only the span's pages exist, and the books hold just those."""
    source, render_dir, queue = fixture_delivery(
        tmp_path, rows, pages=pages, render_pages=span, books=())
    (render_dir / 'capture.report.json').write_text(
        json.dumps({'physical_pages': list(span)}), encoding='utf-8')
    for kind in ('mono', 'dual'):
        with pymupdf.open(source) as original, pymupdf.open() as book:
            book.insert_pdf(original, from_page=span[0] - 1, to_page=span[-1] - 1)
            book.save(render_dir / f'source.usable-deepseek.zh.{kind}.pdf')
    return source, render_dir, queue


def test_a_complete_slice_of_a_book_is_deliverable(tmp_path):
    """Grading a 120-page slice against the source's 634 pages condemned it.

    Every page outside the slice read as missing and both books read as short,
    so a run that had done exactly what it was asked came back not_deliverable.
    """
    rows = [unit(f'u{i}', pages=(4,)) for i in range(10)]
    source, render_dir, queue = slice_delivery(tmp_path, rows, pages=10, span=range(4, 8))
    report = build_acceptance_report(source, render_dir, queue)
    assert report['verdict'] == DELIVERABLE
    assert report['major_problems'] == []
    assert report['render']['source_pages'] == 4
    assert report['render']['rendered_pages'] == 4
    assert report['render']['source_page_range'] == [4, 7]


def test_a_hole_inside_the_slice_still_stops_delivery(tmp_path):
    """Narrowing the span must not narrow what the check can see."""
    rows = [unit(f'u{i}', pages=(4,)) for i in range(10)]
    source, render_dir, queue = slice_delivery(tmp_path, rows, pages=10, span=range(4, 8))
    for path in page_output_paths(render_dir, source.stem, 6):
        path.unlink()
    report = build_acceptance_report(source, render_dir, queue)
    assert report['verdict'] == NOT_DELIVERABLE
    missing = [p for p in report['major_problems'] if p['kind'] == 'missing_rendered_page']
    assert missing and missing[0]['pages'] == [6]


def test_coverage_reconciles_the_slice_and_not_the_rest_of_the_book(tmp_path):
    """Pages outside the slice were never meant to be captured.

    Reconciling them would report the other 514 pages of the book as body text
    the capture lost, and block a delivery that is complete.
    """
    rows = [unit(f'u{i}', pages=(4,)) for i in range(10)]
    source, render_dir, queue = slice_delivery(tmp_path, rows, pages=10, span=range(4, 8))
    capture_map = write_capture_map(
        render_dir / 'capture-map.jsonl',
        {index: [f'Page {index + 1}'] for index in range(3, 7)})
    report = build_acceptance_report(source, render_dir, queue, capture_map=capture_map)
    assert report['verdict'] == DELIVERABLE
    assert report['capture_coverage']['pages_reconciled'] == 4
    assert report['capture_coverage']['uncaptured_words'] == 0


def test_a_page_the_slice_did_lose_is_still_caught(tmp_path):
    rows = [unit(f'u{i}', pages=(4,)) for i in range(10)]
    source, render_dir, queue = slice_delivery(tmp_path, rows, pages=10, span=range(4, 8))
    capture_map = write_capture_map(
        render_dir / 'capture-map.jsonl',
        {index: [f'Page {index + 1}'] for index in (3, 4, 5)})
    report = build_acceptance_report(source, render_dir, queue, capture_map=capture_map)
    assert report['verdict'] == NOT_DELIVERABLE
    uncaptured = [p for p in report['major_problems'] if p['kind'] == 'uncaptured_prose']
    assert uncaptured and uncaptured[0]['pages'] == [7]
