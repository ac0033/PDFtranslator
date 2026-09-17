"""Capture the full val.pdf in resumable page chunks, then merge and enrich once.

Chunked capture keeps per-page unit and fragment identity (all page-local), so
merged units are identical to a single-pass capture; running the context
enrichment once over the merged unit list preserves document-wide section
tracking that per-chunk enrichment would lose.
"""
from pathlib import Path
import argparse
import json
from pdf_translation.babeldoc_adapter import capture_pdf_sample
from pdf_translation.document_context import enrich_pdf_units_with_context
from pdf_translation.schemas import TranslationUnit


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, default=Path('pdf/val.pdf'))
    parser.add_argument('--total-pages', type=int, default=442)
    parser.add_argument('--chunk-size', type=int, default=50)
    parser.add_argument('--from-page', type=int, default=1, help='1-based first page of this worker range')
    parser.add_argument('--to-page', type=int, default=None, help='1-based last page of this worker range (default: total-pages)')
    parser.add_argument('--merge-only', action='store_true', help='only merge existing chunk outputs')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    last_page = args.to_page or args.total_pages
    chunks_root = args.output.parent / f'{args.output.name}-chunks'
    if not args.merge_only:
        chunk_reports = []
        for start in range(args.from_page - 1, last_page, args.chunk_size):
            pages = tuple(range(start, min(start + args.chunk_size, last_page)))
            chunk_dir = chunks_root / f'chunk-{pages[0] + 1:04d}-{pages[-1] + 1:04d}'
            if (chunk_dir / 'units.jsonl').exists():
                chunk_reports.append(json.loads((chunk_dir / 'capture.report.json').read_text(encoding='utf-8')))
                print(f'skip existing {chunk_dir.name}', flush=True)
                continue
            report = capture_pdf_sample(args.source, pages, chunk_dir, Path('tmp/pdfs') / chunk_dir.name, enrich_context=False)
            chunk_reports.append(report)
            print(f'captured {chunk_dir.name}: units={report["unit_count"]} empty={report["empty_pages"]}', flush=True)
    expected_chunks = len(range(0, args.total_pages, args.chunk_size))
    present = sorted(d for d in chunks_root.iterdir() if (d / 'units.jsonl').exists()) if chunks_root.exists() else []
    if len(present) < expected_chunks:
        print(json.dumps({'status': 'partial', 'chunks_present': len(present), 'chunks_expected': expected_chunks}, ensure_ascii=True), flush=True)
        return
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
        'physical_pages': list(range(1, args.total_pages + 1)),
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


if __name__ == '__main__':
    main()
