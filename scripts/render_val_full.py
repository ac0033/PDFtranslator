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
import subprocess
import sys

from pdf_translation.acceptance import missing_rendered_pages, page_output_paths
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
DEFAULT_MAX_RETRIES = 3


# The page-level checkpoint contract lives in pdf_translation.acceptance, which
# verifies what this script writes; both sides must agree on it.
page_outputs = page_output_paths
missing_pages = missing_rendered_pages


def chunk_ranges(first_page: int, last_page: int, chunk_size: int) -> list[tuple[int, int]]:
    """Split an inclusive one-based page span into bounded chunks."""
    if chunk_size < 1:
        raise ValueError('chunk size must be at least 1')
    return [(start, min(start + chunk_size - 1, last_page)) for start in range(first_page, last_page + 1, chunk_size)]


def drive_chunks(chunks: list[tuple[int, int]], run_chunk, max_retries: int = DEFAULT_MAX_RETRIES) -> list[dict]:
    """Run each chunk in its own process, retrying a chunk that fails.

    run_chunk(first_page, last_page) returns a process exit code. A chunk that
    exhausts its retries does not stop the remaining chunks: an unattended run
    should salvage every page it can and report the rest.
    """
    if max_retries < 1:
        raise ValueError('max retries must be at least 1')
    outcomes: list[dict] = []
    for first, last in chunks:
        attempts = 0
        exit_code = None
        while attempts < max_retries:
            attempts += 1
            exit_code = run_chunk(first, last)
            if exit_code == 0:
                break
        outcomes.append({
            'from_page': first,
            'to_page': last,
            'attempts': attempts,
            'exit_code': exit_code,
            'ok': exit_code == 0,
        })
    return outcomes


def merge_rendered_pages(capture: Path, source: Path, output_prefix: str) -> dict:
    """Merge per-page output into the mono and dual books, carrying bookmarks."""
    stem = source.stem
    with pymupdf.open(source) as source_doc:
        total_pages = len(source_doc)
    page_numbers = list(range(1, total_pages + 1))
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
    status = 'completed' if all(count == total_pages for count in counts.values()) else 'page_count_mismatch'
    return {'status': status, 'expected_pages': total_pages, 'outputs': counts}


def capture_records(capture: Path) -> list[dict]:
    return [json.loads(line) for line in (capture / 'capture-map.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]


def render_chunk(args, chunk_pages: list[int]) -> dict:
    """Render one bounded chunk of zero-based page indices inside this process."""
    cfg = load_config(args.config)
    store = StateStore(Path(args.runs_root) / cfg.run_id / 'state.sqlite')
    records = capture_records(args.capture)
    records_by_page: dict[int, list[dict]] = {}
    for record in records:
        records_by_page.setdefault(record['page_index'], []).append(record)
    chunk_unit_ids = {r['unit_id'] for p in chunk_pages for r in records_by_page.get(p, [])}
    missing_results = [uid for uid in chunk_unit_ids if store.result(cfg.run_id, uid) is None or store.result(cfg.run_id, uid).draft is None]
    if missing_results:
        raise RuntimeError(f'units without translation results: {missing_results}')
    translated_by_unit = {uid: store.result(cfg.run_id, uid).draft.translated_text for uid in chunk_unit_ids}
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
    return {'rendered': rendered, 'reused': reused, 'empty_passthrough': empty}


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
    chunks = chunk_ranges(page_numbers[0], page_numbers[-1], args.chunk_size)

    def run_chunk(first: int, last: int) -> int:
        print(f'=== chunk {first}-{last} ===', flush=True)
        return subprocess.run(chunk_command(args, first, last)).returncode

    outcomes = drive_chunks(chunks, run_chunk, max_retries=args.max_retries)
    still_missing = missing_pages(args.capture, args.source.stem, page_numbers)
    summary = {
        'chunks': outcomes,
        'failed_chunks': [outcome for outcome in outcomes if not outcome['ok']],
        'retried_chunks': [outcome for outcome in outcomes if outcome['attempts'] > 1],
        'missing_pages': still_missing,
    }
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
    parser.add_argument('--from-page', type=int, default=1)
    parser.add_argument('--to-page', type=int, default=None)
    parser.add_argument('--auto', action='store_true', help='Drive every chunk in its own process, retry crashes, then merge.')
    parser.add_argument('--chunk-size', type=int, default=DEFAULT_CHUNK_SIZE, help='Pages per subprocess in --auto mode.')
    parser.add_argument('--max-retries', type=int, default=DEFAULT_MAX_RETRIES, help='Attempts per chunk in --auto mode.')
    parser.add_argument('--merge-only', action='store_true')
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.merge_only:
        print(json.dumps(merge_rendered_pages(args.capture, args.source, args.output_prefix), ensure_ascii=True), flush=True)
        return 0

    # The span comes from the source PDF, not from the captured records: a
    # trailing blank page has no records but still has to reach the book, and
    # the per-page path passes verifiably empty pages through unchanged.
    with pymupdf.open(args.source) as source_doc:
        last_page = args.to_page or len(source_doc)
    page_numbers = list(range(args.from_page, last_page + 1))

    if args.auto:
        return run_auto(args, page_numbers)

    print(json.dumps(render_chunk(args, [number - 1 for number in page_numbers]), ensure_ascii=True), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
