"""Render a translated PDF page by page, in fresh-process chunks, then merge.

BabelDOC/MuPDF memory pressure accumulates within a process, so pages are
rendered in bounded chunks and every chunk gets its own interpreter. Driver
mode (--auto) owns that loop: it re-invokes this script once per chunk, retries
a chunk whose subprocess crashed, and merges once every page is on disk.

Per-page output lands in <capture>/page-NNNN/ and is skipped when both PDFs
already exist, so a crash is resumed by rerunning the same command. No API
calls are made: translations are read from the run store.

Modes:
    --auto                     drive every chunk, retry crashes, then merge
    --from-page N --to-page M  render one chunk inside this process
    --merge-only               merge existing per-page output into the books
"""
from pathlib import Path
import argparse
import json
import sys

from pdf_translation.acceptance import captured_page_span, missing_rendered_pages, page_output_paths
from pdf_translation.chunk_driver import DEFAULT_MAX_RETRIES, REQUIRED_COMMIT_BYTES, chunk_ranges, require_memory, run_subprocess_chunks, summarise
from pdf_translation.babeldoc_adapter import (
    _injection_translator,
    _load_layout_model,
    _merge_pdfs,
    _page_is_verifiably_empty,
    _run_babeldoc_page,
)
from pdf_translation.config import load_config
from pdf_translation.cross_page import split_document_translation
from pdf_translation.storage import StateStore

import pymupdf

DEFAULT_CHUNK_SIZE = 10


# The page-level checkpoint contract lives in pdf_translation.acceptance, which
# verifies what this script writes; both sides must agree on it.
page_outputs = page_output_paths
missing_pages = missing_rendered_pages
# The captured range too: grading and merging must agree on which pages the
# run is responsible for, or a complete slice looks like a book with holes.
page_span = captured_page_span


def merge_rendered_pages(capture: Path, source: Path, output_prefix: str) -> dict:
    """Merge per-page output into the mono and dual books, carrying bookmarks."""
    stem = source.stem
    page_numbers = page_span(capture, source)
    missing = missing_pages(capture, stem, page_numbers)
    if missing:
        raise RuntimeError(f'pages missing rendered output: {missing}')
    mono_pages = [page_outputs(capture, stem, number)[0] for number in page_numbers]
    dual_pages = [page_outputs(capture, stem, number)[1] for number in page_numbers]
    mono_output = capture / f'{stem}.{output_prefix}.zh.mono.pdf'
    dual_output = capture / f'{stem}.{output_prefix}.zh.dual.pdf'
    _merge_pdfs(mono_pages, mono_output)
    _merge_pdfs(dual_pages, dual_output)
    with pymupdf.open(source) as source_doc:
        source_toc = source_doc.get_toc()
        whole_document = page_numbers == list(range(1, len(source_doc) + 1))
    # A slice's pages are renumbered from one, so the book's bookmarks would
    # point at pages the slice does not contain. Dropping them is honest; the
    # report says so rather than leaving a silently wrong outline behind.
    if source_toc and not whole_document:
        source_toc = []
    if source_toc:
        for path in (mono_output, dual_output):
            with pymupdf.open(path) as merged:
                merged.set_toc(source_toc)
                temp = path.with_suffix('.toc-tmp.pdf')
                merged.save(temp)
            temp.replace(path)
    counts = {}
    for path in (mono_output, dual_output):
        with pymupdf.open(path) as document:
            counts[path.name] = len(document)
    expected = len(page_numbers)
    status = 'completed' if all(count == expected for count in counts.values()) else 'page_count_mismatch'
    return {
        'status': status, 'expected_pages': expected, 'outputs': counts,
        'source_pages': [page_numbers[0], page_numbers[-1]],
        'bookmarks_carried': whole_document,
    }


def capture_records(capture: Path) -> list[dict]:
    return [json.loads(line) for line in (capture / 'capture-map.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]


def captured_source_text(capture: Path, unit_ids: list[str]) -> dict[str, str]:
    """The source text of named units, for pages that must render without them."""
    wanted = set(unit_ids)
    found: dict[str, str] = {}
    for line in (capture / 'units.jsonl').read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        unit = json.loads(line)
        if unit['unit_id'] in wanted:
            found[unit['unit_id']] = unit['source_text']
    return found


def chunk_translations(store, run_id: str, capture: Path, unit_ids: set[str]) -> tuple[dict[str, str], list[str]]:
    """Text to inject for each unit, and which of them are still in the source language.

    A unit that never reached a deliverable translation falls back to its own
    source text, which carries the same markers by construction and so injects
    cleanly. Refusing instead cost the whole chunk's ten pages, and with them the
    merged book, over a single paragraph: on optimization.pdf two units out of
    1473 took fifty pages down with them. The page now renders with that one
    paragraph still in English, and acceptance reports the unit as untranslated
    from the run store - degraded in the open, never passed off as finished.
    """
    results = {unit_id: store.result(run_id, unit_id) for unit_id in unit_ids}
    untranslated = sorted(
        unit_id for unit_id, result in results.items()
        if result is None or result.draft is None
    )
    source_by_unit = captured_source_text(capture, untranslated) if untranslated else {}
    unresolved = sorted(set(untranslated) - set(source_by_unit))
    if unresolved:
        raise RuntimeError(f'units with neither a translation nor a captured source: {unresolved}')
    text = {
        unit_id: source_by_unit[unit_id] if unit_id in source_by_unit else results[unit_id].draft.translated_text
        for unit_id in unit_ids
    }
    return text, untranslated


def render_chunk(args, chunk_pages: list[int]) -> dict:
    """Render one bounded chunk of zero-based page indices inside this process."""
    cfg = load_config(args.config)
    store = StateStore(Path(args.runs_root) / cfg.run_id / 'state.sqlite')
    records = capture_records(args.capture)
    records_by_page: dict[int, list[dict]] = {}
    for record in records:
        records_by_page.setdefault(record['page_index'], []).append(record)
    chunk_unit_ids = {r['unit_id'] for p in chunk_pages for r in records_by_page.get(p, [])}
    translated_by_unit, untranslated = chunk_translations(store, cfg.run_id, args.capture, chunk_unit_ids)
    if untranslated:
        print(f'left in the source language: {untranslated}', flush=True)
    translated_by_fragment = split_document_translation(translated_by_unit, {p: records_by_page.get(p, []) for p in chunk_pages})

    layout = _load_layout_model()
    stem = args.source.stem
    rendered, reused, empty = [], [], []
    for page_index in chunk_pages:
        page_number = page_index + 1
        mono, dual = page_outputs(args.capture, stem, page_number)
        page_dir = mono.parent
        if mono.is_file() and dual.is_file():
            reused.append(page_number)
            continue
        page_records = records_by_page.get(page_index, [])
        if not page_records:
            if not _page_is_verifiably_empty(args.source, page_index):
                raise RuntimeError(f'page {page_number} has content but no captured records')
            page_dir.mkdir(parents=True, exist_ok=True)
            with pymupdf.open(args.source) as original, pymupdf.open() as single:
                single.insert_pdf(original, from_page=page_index, to_page=page_index)
                single.save(mono)
                single.save(dual)
            empty.append(page_number)
            continue
        page_dir.mkdir(parents=True, exist_ok=True)
        translator = _injection_translator(page_records, translated_by_fragment)
        _run_babeldoc_page(args.source, page_index, page_dir, translator, layout=layout)
        if translator.errors or translator.remaining:
            raise RuntimeError(f'injection mismatch on page {page_number}: errors={translator.errors}, remaining={translator.remaining}')
        rendered.append(page_number)
        print(f'page {page_number} rendered', flush=True)
    return {'rendered': rendered, 'reused': reused, 'empty_passthrough': empty,
            'untranslated_units': untranslated}


def chunk_command(args, first: int, last: int) -> list[str]:
    """Rebuild this script's own command line for one chunk."""
    return [
        sys.executable, str(Path(__file__).resolve()),
        '--capture', str(args.capture),
        '--config', str(args.config),
        '--source', str(args.source),
        '--runs-root', str(args.runs_root),
        '--output-prefix', args.output_prefix,
        '--from-page', str(first),
        '--to-page', str(last),
    ]


def run_auto(args, page_numbers: list[int]) -> int:
    """Drive every chunk in its own process, then merge. Returns an exit code."""
    require_memory(args.min_free_mb * 1024 * 1024)
    chunks = chunk_ranges(page_numbers[0], page_numbers[-1], args.chunk_size)
    outcomes = run_subprocess_chunks(
        chunks, lambda first, last: chunk_command(args, first, last), max_retries=args.max_retries,
    )
    still_missing = missing_pages(args.capture, args.source.stem, page_numbers)
    summary = summarise(outcomes, still_missing)
    if still_missing:
        summary['status'] = 'incomplete'
        print(json.dumps(summary, ensure_ascii=True), flush=True)
        return 1
    summary['merge'] = merge_rendered_pages(args.capture, args.source, args.output_prefix)
    summary['status'] = summary['merge']['status']
    print(json.dumps(summary, ensure_ascii=True), flush=True)
    return 0 if summary['status'] == 'completed' else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--capture', type=Path, default=Path('output/pdf/val-usable-babeldoc-full-r1'), help='Capture directory holding capture-map.jsonl and per-page output.')
    parser.add_argument('--config', type=Path, default=Path('configs/val.usable.deepseek.full.toml'))
    parser.add_argument('--source', type=Path, default=Path('pdf/val.pdf'))
    parser.add_argument('--runs-root', type=Path, default=Path('runs'), help='Root holding <run_id>/state.sqlite.')
    parser.add_argument('--output-prefix', default='usable-deepseek', help='Infix of the merged book filenames.')
    parser.add_argument('--from-page', type=int, default=None, help='Defaults to the first captured page.')
    parser.add_argument('--to-page', type=int, default=None, help='Defaults to the last captured page.')
    parser.add_argument('--auto', action='store_true', help='Drive every chunk in its own process, retry crashes, then merge.')
    parser.add_argument('--chunk-size', type=int, default=DEFAULT_CHUNK_SIZE, help='Pages per subprocess in --auto mode.')
    parser.add_argument('--min-free-mb', type=int, default=REQUIRED_COMMIT_BYTES // 1024 // 1024,
                        help='Commit headroom required before --auto starts. Lower it only knowing the workload.')
    parser.add_argument('--max-retries', type=int, default=DEFAULT_MAX_RETRIES, help='Attempts per chunk in --auto mode.')
    parser.add_argument('--merge-only', action='store_true')
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.merge_only:
        print(json.dumps(merge_rendered_pages(args.capture, args.source, args.output_prefix), ensure_ascii=True), flush=True)
        return 0

    # The span is the captured range, not the records inside it: a trailing
    # blank page has no records but still has to reach the book, and the
    # per-page path passes verifiably empty pages through unchanged.
    span = page_span(args.capture, args.source)
    page_numbers = list(range(args.from_page or span[0], (args.to_page or span[-1]) + 1))

    if args.auto:
        return run_auto(args, page_numbers)

    print(json.dumps(render_chunk(args, [number - 1 for number in page_numbers]), ensure_ascii=True), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
