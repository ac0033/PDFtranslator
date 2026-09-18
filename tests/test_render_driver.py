import importlib.util
import json
from pathlib import Path

import pymupdf
import pytest

from pdf_translation import chunk_driver


def _load_driver():
    """Load the render script as a module so its globals can be substituted."""
    path = Path(__file__).parents[1] / 'scripts/render_val_full.py'
    spec = importlib.util.spec_from_file_location('render_val_full_under_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


driver = _load_driver()


def write_page_pair(capture: Path, stem: str, page_number: int, source: Path, page_index: int) -> None:
    """Stand in for a rendered page: the checkpoint is both PDFs existing."""
    mono, dual = driver.page_outputs(capture, stem, page_number)
    mono.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open(source) as original, pymupdf.open() as single:
        single.insert_pdf(original, from_page=page_index, to_page=page_index)
        single.save(mono)
        single.save(dual)


def fixture_capture(tmp_path, pages=3, captured=None, span=None):
    source = tmp_path / 'source.pdf'
    with pymupdf.open() as doc:
        for number in range(pages):
            doc.new_page().insert_text((30, 60), f'Page {number + 1}')
        doc.save(source)
    capture = tmp_path / 'capture'
    capture.mkdir()
    indices = range(pages) if captured is None else captured
    records = [{'page_index': index, 'unit_id': f'u{index}'} for index in indices]
    (capture / 'capture-map.jsonl').write_text('\n'.join(json.dumps(r) for r in records), encoding='utf-8')
    if span is not None:
        (capture / 'capture.report.json').write_text(
            json.dumps({'physical_pages': list(span)}), encoding='utf-8')
    return source, capture


def test_chunk_ranges_cover_every_page_with_a_ragged_tail():
    assert driver.chunk_ranges(1, 25, 10) == [(1, 10), (11, 20), (21, 25)]
    assert driver.chunk_ranges(1, 10, 10) == [(1, 10)]
    assert driver.chunk_ranges(7, 9, 10) == [(7, 9)]
    with pytest.raises(ValueError):
        driver.chunk_ranges(1, 10, 0)


def test_page_checkpoint_requires_both_mono_and_dual(tmp_path):
    source, capture = fixture_capture(tmp_path)
    stem = source.stem
    assert driver.missing_pages(capture, stem, [1, 2, 3]) == [1, 2, 3]
    write_page_pair(capture, stem, 1, source, 0)
    mono, dual = driver.page_outputs(capture, stem, 2)
    mono.parent.mkdir(parents=True, exist_ok=True)
    mono.write_bytes(b'partial')
    assert driver.missing_pages(capture, stem, [1, 2, 3]) == [2, 3]


def test_chunk_command_round_trips_through_the_parser(tmp_path):
    args = driver.build_parser().parse_args([
        '--capture', str(tmp_path / 'cap'),
        '--source', str(tmp_path / 'book.pdf'),
        '--runs-root', str(tmp_path / 'runs'),
        '--output-prefix', 'custom',
    ])
    command = driver.chunk_command(args, 11, 20)
    child = driver.build_parser().parse_args(command[2:])
    assert (child.from_page, child.to_page) == (11, 20)
    assert child.output_prefix == 'custom'
    assert child.capture == args.capture and child.source == args.source
    assert child.runs_root == args.runs_root
    assert not child.auto and not child.merge_only


def test_merge_writes_both_books_with_source_page_count(tmp_path):
    source, capture = fixture_capture(tmp_path)
    for index in range(3):
        write_page_pair(capture, source.stem, index + 1, source, index)
    report = driver.merge_rendered_pages(capture, source, 'usable-deepseek')
    assert report['status'] == 'completed'
    assert report['expected_pages'] == 3
    assert set(report['outputs'].values()) == {3}
    assert (capture / 'source.usable-deepseek.zh.mono.pdf').is_file()
    assert (capture / 'source.usable-deepseek.zh.dual.pdf').is_file()


def test_merge_refuses_to_publish_a_book_with_a_missing_page(tmp_path):
    source, capture = fixture_capture(tmp_path)
    for index in (0, 2):
        write_page_pair(capture, source.stem, index + 1, source, index)
    with pytest.raises(RuntimeError, match=r'\[2\]'):
        driver.merge_rendered_pages(capture, source, 'usable-deepseek')


def fake_subprocess(monkeypatch, source, capture, *, crash_pages=(), crash_times=0):
    """Replace the chunk subprocess with one that writes the page checkpoints."""
    # The driver preflights commit headroom before starting; these tests are
    # about the loop, not about how loaded the machine running them is.
    monkeypatch.setattr(chunk_driver, 'available_commit_bytes', lambda: 8 * 1024 * 1024 * 1024)
    attempts = {}

    class Completed:
        def __init__(self, returncode):
            self.returncode = returncode

    def fake_run(command, *args, **kwargs):
        first = int(command[command.index('--from-page') + 1])
        last = int(command[command.index('--to-page') + 1])
        attempts[first] = attempts.get(first, 0) + 1
        if first in crash_pages and attempts[first] <= crash_times:
            return Completed(1)
        for number in range(first, last + 1):
            write_page_pair(capture, source.stem, number, source, number - 1)
        return Completed(0)

    monkeypatch.setattr(chunk_driver.subprocess, 'run', fake_run)
    return attempts


def test_auto_mode_renders_every_chunk_then_merges(tmp_path, monkeypatch):
    source, capture = fixture_capture(tmp_path)
    fake_subprocess(monkeypatch, source, capture)
    exit_code = driver.main(['--auto', '--chunk-size', '2', '--capture', str(capture), '--source', str(source)])
    assert exit_code == 0
    assert driver.missing_pages(capture, source.stem, [1, 2, 3]) == []
    with pymupdf.open(capture / 'source.usable-deepseek.zh.mono.pdf') as merged:
        assert len(merged) == 3


def test_auto_mode_resumes_after_a_crashed_chunk(tmp_path, monkeypatch):
    source, capture = fixture_capture(tmp_path)
    attempts = fake_subprocess(monkeypatch, source, capture, crash_pages=(3,), crash_times=1)
    exit_code = driver.main(['--auto', '--chunk-size', '2', '--capture', str(capture), '--source', str(source)])
    assert exit_code == 0
    assert attempts[3] == 2
    assert driver.missing_pages(capture, source.stem, [1, 2, 3]) == []


def test_auto_mode_reports_missing_pages_and_fails_when_retries_run_out(tmp_path, monkeypatch, capsys):
    source, capture = fixture_capture(tmp_path)
    fake_subprocess(monkeypatch, source, capture, crash_pages=(3,), crash_times=99)
    exit_code = driver.main(['--auto', '--chunk-size', '2', '--capture', str(capture), '--source', str(source)])
    assert exit_code == 1
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert summary['status'] == 'incomplete'
    assert summary['missing_pages'] == [3]
    assert [c['from_page'] for c in summary['failed_chunks']] == [3]
    assert summary['chunks'][1]['attempts'] == 3
    # The book must not be published from an incomplete page set.
    assert not (capture / 'source.usable-deepseek.zh.mono.pdf').exists()


def test_auto_mode_covers_a_trailing_page_that_captured_nothing(tmp_path, monkeypatch):
    # A blank final page produces no capture records. Deriving the span from
    # the last captured record would silently stop one page short and leave the
    # merge to fail on a missing page.
    source, capture = fixture_capture(tmp_path, pages=3, captured=[0, 1])
    fake_subprocess(monkeypatch, source, capture)
    assert driver.main(['--auto', '--chunk-size', '2', '--capture', str(capture), '--source', str(source)]) == 0
    assert driver.missing_pages(capture, source.stem, [1, 2, 3]) == []
    with pymupdf.open(capture / 'source.usable-deepseek.zh.mono.pdf') as merged:
        assert len(merged) == 3


def test_merge_only_mode_does_not_render(tmp_path, monkeypatch):
    source, capture = fixture_capture(tmp_path)
    for index in range(3):
        write_page_pair(capture, source.stem, index + 1, source, index)

    monkeypatch.setattr(chunk_driver, 'available_commit_bytes', lambda: 8 * 1024 * 1024 * 1024)

    def refuse(*args, **kwargs):
        raise AssertionError('--merge-only must not spawn a render subprocess')

    monkeypatch.setattr(chunk_driver.subprocess, 'run', refuse)
    assert driver.main(['--merge-only', '--capture', str(capture), '--source', str(source)]) == 0
    assert (capture / 'source.usable-deepseek.zh.mono.pdf').is_file()


def test_a_slice_renders_and_merges_only_the_pages_it_captured(tmp_path, monkeypatch):
    """A 120-page slice of a 634-page book is a deliverable, not a book with holes.

    The span used to come from the source PDF, so a slice asked for every page
    of the whole book, rendered pages nobody captured and then refused to merge
    on the ones it could not produce.
    """
    source, capture = fixture_capture(tmp_path, pages=10, captured=range(3, 7), span=range(4, 8))
    fake_subprocess(monkeypatch, source, capture)
    assert driver.main(['--auto', '--chunk-size', '2', '--capture', str(capture), '--source', str(source)]) == 0
    assert driver.missing_pages(capture, source.stem, [4, 5, 6, 7]) == []
    # Pages outside the slice must not have been rendered at all.
    assert driver.missing_pages(capture, source.stem, [1, 2, 3, 8, 9, 10]) == [1, 2, 3, 8, 9, 10]
    with pymupdf.open(capture / 'source.usable-deepseek.zh.mono.pdf') as merged:
        assert len(merged) == 4


def test_a_slice_merge_reports_its_span_and_drops_the_books_bookmarks(tmp_path):
    source, capture = fixture_capture(tmp_path, pages=10, captured=range(3, 7), span=range(4, 8))
    with pymupdf.open(source) as document:
        document.set_toc([[1, 'Chapter 9', 9]])
        document.saveIncr()
    for number in (4, 5, 6, 7):
        write_page_pair(capture, source.stem, number, source, number - 1)
    report = driver.merge_rendered_pages(capture, source, 'usable-deepseek')
    assert report['status'] == 'completed'
    assert report['expected_pages'] == 4
    assert report['source_pages'] == [4, 7]
    # The book's outline points at page 9, which this four-page slice does not have.
    assert report['bookmarks_carried'] is False
    with pymupdf.open(capture / 'source.usable-deepseek.zh.mono.pdf') as merged:
        assert merged.get_toc() == []


def test_a_whole_book_still_carries_its_bookmarks(tmp_path):
    source, capture = fixture_capture(tmp_path, pages=3)
    with pymupdf.open(source) as document:
        document.set_toc([[1, 'Chapter 1', 2]])
        document.saveIncr()
    for index in range(3):
        write_page_pair(capture, source.stem, index + 1, source, index)
    report = driver.merge_rendered_pages(capture, source, 'usable-deepseek')
    assert report['bookmarks_carried'] is True
    with pymupdf.open(capture / 'source.usable-deepseek.zh.mono.pdf') as merged:
        assert merged.get_toc() == [[1, 'Chapter 1', 2]]
