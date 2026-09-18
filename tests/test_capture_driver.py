"""The chunked capture driver, and the one thing a chunk process must not do.

A chunk sees the other chunks' output on disk but not how many the run is meant
to have. The first version let it judge completeness from that, so chunk 150-159
of a 120-page slice published its own 129 units as the finished capture and
every later chunk then died on the output directory already existing - three
times each, because the driver read those deaths as crashes worth retrying.
"""
import importlib.util
import json
from pathlib import Path

import pymupdf
import pytest

from pdf_translation import chunk_driver


def _load_driver():
    path = Path(__file__).parents[1] / 'scripts/capture_val_full.py'
    spec = importlib.util.spec_from_file_location('capture_val_full_under_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


driver = _load_driver()


@pytest.fixture
def book(tmp_path):
    source = tmp_path / 'book.pdf'
    with pymupdf.open() as document:
        for number in range(30):
            document.new_page().insert_text((30, 60), f'Page {number + 1}')
        document.save(source)
    return source


@pytest.fixture
def captured(monkeypatch):
    """Write a chunk's three files without running BabelDOC."""
    monkeypatch.setattr(chunk_driver, 'available_commit_bytes', lambda: 8 * 1024 * 1024 * 1024)
    seen = []

    def capture_pdf_sample(source, pages, chunk_dir, scratch, enrich_context=False):
        seen.append((pages[0] + 1, pages[-1] + 1))
        chunk_dir.mkdir(parents=True, exist_ok=True)
        units = [
            {'unit_id': f'u{page}', 'source_text': f'Page {page + 1}', 'page_index': page}
            for page in pages
        ]
        (chunk_dir / 'units.jsonl').write_text(
            ''.join(json.dumps(unit) + '\n' for unit in units), encoding='utf-8')
        (chunk_dir / 'capture-map.jsonl').write_text(
            ''.join(json.dumps(unit) + '\n' for unit in units), encoding='utf-8')
        report = {'unit_count': len(units), 'empty_pages': []}
        (chunk_dir / 'capture.report.json').write_text(json.dumps(report), encoding='utf-8')
        return report

    monkeypatch.setattr(driver, 'capture_pdf_sample', capture_pdf_sample)
    # Merging enriches context over the merged list; that behaviour has its own
    # tests, and standing it up here would mean building real TranslationUnits.
    monkeypatch.setattr(driver, 'enrich_pdf_units_with_context', lambda units: units)
    monkeypatch.setattr(driver.TranslationUnit, 'model_validate_json',
                        staticmethod(lambda line: _Unit(json.loads(line))))
    return seen


class _Unit:
    def __init__(self, data):
        self.data = data
        self.neighbor_ids = []
        self.section_path = []

    def model_dump_json(self):
        return json.dumps(self.data)


def base_args(book, tmp_path, output):
    return ['--source', str(book), '--chunk-size', '10',
            '--capture-root', str(tmp_path / 'scratch'), '--output', str(output)]


def test_a_chunk_process_captures_its_range_and_stops(tmp_path, book, captured):
    output = tmp_path / 'out'
    assert driver.main(base_args(book, tmp_path, output)
                       + ['--from-page', '11', '--to-page', '20', '--capture-only']) == 0
    assert captured == [(11, 20)]
    assert (tmp_path / 'out-chunks/chunk-0011-0020/units.jsonl').is_file()
    # The whole point: ten pages of a thirty-page job are not a merged capture.
    assert not output.exists()


def test_a_chunk_stops_even_when_every_other_chunk_is_already_on_disk(tmp_path, book, captured):
    """The regression: 'every chunk I can see is present' is not completeness."""
    output = tmp_path / 'out'
    for first, last in ((1, 10), (11, 20), (21, 30)):
        assert driver.main(base_args(book, tmp_path, output)
                           + ['--from-page', str(first), '--to-page', str(last), '--capture-only']) == 0
    assert not output.exists()


def test_the_driver_merges_once_it_has_driven_every_chunk(tmp_path, book, captured, monkeypatch):
    output = tmp_path / 'out'
    monkeypatch.setattr(chunk_driver.subprocess, 'run',
                        lambda command, *a, **k: _Completed(driver.main(command[2:])))
    assert driver.main(base_args(book, tmp_path, output) + ['--auto']) == 0
    assert captured == [(1, 10), (11, 20), (21, 30)]
    report = json.loads((output / 'capture.report.json').read_text(encoding='utf-8'))
    assert report['unit_count'] == 30
    assert len(report['chunks']) == 3
    assert report['physical_pages'] == list(range(1, 31))
    assert len((output / 'units.jsonl').read_text(encoding='utf-8').splitlines()) == 30


class _Completed:
    def __init__(self, returncode):
        self.returncode = returncode


def test_a_missing_chunk_is_named_rather_than_merged_around(tmp_path, book, captured):
    output = tmp_path / 'out'
    args = base_args(book, tmp_path, output)
    driver.main(args + ['--from-page', '1', '--to-page', '10', '--capture-only'])
    driver.main(args + ['--from-page', '21', '--to-page', '30', '--capture-only'])
    assert driver.main(args + ['--merge-only']) == 0
    assert not output.exists()


def test_a_second_merge_refuses_rather_than_half_overwriting(tmp_path, book, captured):
    output = tmp_path / 'out'
    args = base_args(book, tmp_path, output)
    for first, last in ((1, 10), (11, 20), (21, 30)):
        driver.main(args + ['--from-page', str(first), '--to-page', str(last), '--capture-only'])
    assert driver.main(args + ['--merge-only']) == 0
    with pytest.raises(FileExistsError, match='Delete it to merge again'):
        driver.main(args + ['--merge-only'])


def test_chunk_command_round_trips_and_carries_capture_only(tmp_path):
    args = driver.build_parser().parse_args([
        '--source', 'pdf/book.pdf', '--output', str(tmp_path / 'out'),
        '--chunk-size', '10', '--capture-root', 'scratch',
        '--from-page', '150', '--to-page', '269', '--total-pages', '634',
    ])
    child = driver.build_parser().parse_args(driver.chunk_command(args, 150, 159)[2:])
    assert (child.from_page, child.to_page) == (150, 159)
    assert child.capture_only and not child.auto and not child.merge_only
    assert child.output == args.output and child.capture_root == args.capture_root
