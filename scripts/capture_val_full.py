"""Capture a PDF page range in resumable chunks, then merge and enrich once.

Chunked capture keeps per-page unit and fragment identity (all page-local), so
merged units are identical to a single-pass capture; running the context
enrichment once over the merged unit list preserves document-wide section
tracking that per-chunk enrichment would lose.

The captured range is whatever --from-page/--to-page name, defaulting to the
whole document. Merging waits for every chunk in that range, and the report
records the pages actually captured, so a slice of a book is a first-class
input rather than a whole document with holes in it.

--auto gives every chunk its own process. BabelDOC fails random memory
allocations in a long Windows process, and capture is as exposed to that as
rendering is: a plain loop over chunks stays in one interpreter, so the
pressure accumulates until a chunk dies and takes the run with it. Chunk output
is its own checkpoint, so a retry is simply the same command again.
"""
from pathlib import Path
import argparse
import json
import sys
from pdf_translation.babeldoc_adapter import capture_pdf_sample
from pdf_translation.chunk_driver import DEFAULT_MAX_RETRIES, REQUIRED_COMMIT_BYTES, require_memory, run_subprocess_chunks, summarise
from pdf_translation.document_context import enrich_pdf_units_with_context
from pdf_translation.schemas import TranslationUnit

import pymupdf


def chunk_command(args, first: int, last: int) -> list[str]:
    """Rebuild this script's own command line for one chunk, in a new process."""
    return [
        sys.executable, str(Path(__file__).resolve()),
        '--source', str(args.source),
        '--total-pages', str(args.total_pages),
        '--chunk-size', str(args.chunk_size),
        '--capture-root', str(args.capture_root),
        '--output', str(args.output),
        '--from-page', str(first),
        '--to-page', str(last),
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--source', type=Path, default=Path('pdf/val.pdf'))
    parser.add_argument('--total-pages', type=int, default=None, help='Defaults to the source PDF page count.')
    parser.add_argument('--chunk-size', type=int, default=50)
    parser.add_argument('--from-page', type=int, default=1, help='1-based first page of the captured range')
    parser.add_argument('--to-page', type=int, default=None, help='1-based last page of the captured range (default: last page)')
    parser.add_argument('--capture-root', type=Path, default=Path('tmp/pdfs'), help='Scratch directory for BabelDOC capture work.')
    parser.add_argument('--auto', action='store_true', help='Give every chunk its own process and retry a chunk that crashed.')
    parser.add_argument('--min-free-mb', type=int, default=REQUIRED_COMMIT_BYTES // 1024 // 1024,
                        help='Commit headroom required before --auto starts. Lower it only knowing the workload.')
    parser.add_argument('--max-retries', type=int, default=DEFAULT_MAX_RETRIES, help='Attempts per chunk in --auto mode.')
    parser.add_argument('--merge-only', action='store_true', help='only merge existing chunk outputs')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.total_pages is None:
        with pymupdf.open(args.source) as document:
            args.total_pages = len(document)
    last_page = args.to_page or args.total_pages
    captured_pages = list(range(args.from_page, last_page + 1))
    chunks_root = args.output.parent / f'{args.output.name}-chunks'
    if args.auto and not args.merge_only:
        require_memory(args.min_free_mb * 1024 * 1024)
        starts = list(range(args.from_page, last_page + 1, args.chunk_size))
        chunks = [(start, min(start + args.chunk_size - 1, last_page)) for start in starts]
        outcomes = run_subprocess_chunks(chunks, lambda first, last: chunk_command(args, first, last), max_retries=args.max_retries)
        summary = summarise(outcomes)
        summary['status'] = 'chunks_complete' if not summary['failed_chunks'] else 'chunks_incomplete'
        print(json.dumps(summary, ensure_ascii=True), flush=True)
        if summary['failed_chunks']:
            return 1
    elif not args.merge_only:
        for start in range(args.from_page - 1, last_page, args.chunk_size):
            pages = tuple(range(start, min(start + args.chunk_size, last_page)))
            chunk_dir = chunks_root / f'chunk-{pages[0] + 1:04d}-{pages[-1] + 1:04d}'
            if (chunk_dir / 'units.jsonl').exists():
                print(f'skip existing {chunk_dir.name}', flush=True)
                continue
            report = capture_pdf_sample(args.source, pages, chunk_dir, args.capture_root / chunk_dir.name, enrich_context=False)
            print(f'captured {chunk_dir.name}: units={report["unit_count"]} empty={report["empty_pages"]}', flush=True)
    expected_chunks = len(range(args.from_page - 1, last_page, args.chunk_size))
    present = sorted(d for d in chunks_root.iterdir() if (d / 'units.jsonl').exists()) if chunks_root.exists() else []
    if len(present) < expected_chunks:
        print(json.dumps({'status': 'partial', 'chunks_present': len(present), 'chunks_expected': expected_chunks}, ensure_ascii=True), flush=True)
        return 0
    if args.output.exists():
        raise FileExistsError(args.output)
    all_units = []
    all_records = []
    empty_pages = []
    chunk_reports = []
    for chunk_dir in present:
        all_units.extend(TranslationUnit.model_validate_json(line) for line in (chunk_dir / 'units.jsonl').read_text(encoding='utf-8').splitlines() if line.strip())
        all_records.extend(line for line in (chunk_dir / 'capture-map.jsonl').read_text(encoding='utf-8').splitlines() if line.strip())
        chunk_report = json.loads((chunk_dir / 'capture.report.json').read_text(encoding='utf-8'))
        chunk_reports.append(chunk_report)
        empty_pages.extend(chunk_report['empty_pages'])
    all_units = enrich_pdf_units_with_context(all_units)
    args.output.mkdir(parents=True)
    (args.output / 'units.jsonl').write_text(''.join(unit.model_dump_json() + '\n' for unit in all_units), encoding='utf-8')
    (args.output / 'capture-map.jsonl').write_text(''.join(record + '\n' for record in all_records), encoding='utf-8')
    report = {
        'schema_version': '1.0',
        'source_file': args.source.name,
        'physical_pages': captured_pages,
        'source_page_count': args.total_pages,
        'empty_pages': sorted(empty_pages),
        'unit_count': len(all_units),
        'context_linked_units': sum(bool(unit.neighbor_ids) for unit in all_units),
        'section_scoped_units': sum(bool(unit.section_path) for unit in all_units),
        'api_calls': 0,
        'final_pdfs': 0,
        'chunks': chunk_reports,
    }
    (args.output / 'capture.report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: report[k] for k in ('unit_count', 'empty_pages', 'context_linked_units', 'section_scoped_units')}, ensure_ascii=True), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
