"""Render translated val.pdf pages in fresh-process chunks, then merge.

Each chunk runs in its own invocation so BabelDOC/MuPDF memory pressure from
earlier pages cannot fail later ones. Per-page outputs land directly in the
main output directory and are skipped when both PDFs already exist, so any
crash can be resumed by simply rerunning the same command. No API calls are
made: translations are read from the run store.
"""
from pathlib import Path
import argparse
import json
from pdf_translation.babeldoc_adapter import (
    _injection_translator,
    _load_layout_model,
    _merge_pdfs,
    _page_is_verifiably_empty,
    _run_babeldoc_page,
)
from pdf_translation.config import load_config
from pdf_translation.cross_page import split_document_translation
from pdf_translation.schemas import TranslationUnit
from pdf_translation.storage import StateStore

import pymupdf


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--capture', type=Path, default=Path('output/pdf/val-usable-babeldoc-full-r1'))
    parser.add_argument('--config', type=Path, default=Path('configs/val.usable.deepseek.full.toml'))
    parser.add_argument('--source', type=Path, default=Path('pdf/val.pdf'))
    parser.add_argument('--from-page', type=int, default=1)
    parser.add_argument('--to-page', type=int, default=None)
    parser.add_argument('--merge-only', action='store_true')
    args = parser.parse_args()
    cfg = load_config(args.config)
    store = StateStore(Path('runs') / cfg.run_id / 'state.sqlite')
    records = [json.loads(line) for line in (args.capture / 'capture-map.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
    pages = sorted({r['page_index'] for r in records})
    first, last = args.from_page - 1, (args.to_page or (pages[-1] + 1)) - 1
    chunk_pages = [p for p in range(first, last + 1)]

    if args.merge_only:
        with pymupdf.open(args.source) as source_doc:
            total_pages = len(source_doc)
        mono_pages, dual_pages, missing = [], [], []
        for page_index in range(total_pages):
            page_dir = args.capture / f'page-{page_index + 1:04d}'
            mono, dual = page_dir / f'{args.source.stem}.zh.mono.pdf', page_dir / f'{args.source.stem}.zh.dual.pdf'
            if not (mono.is_file() and dual.is_file()):
                missing.append(page_index + 1)
                continue
            mono_pages.append(mono)
            dual_pages.append(dual)
        if missing:
            raise RuntimeError(f'pages missing rendered output: {missing}')
        mono_output = args.capture / f'{args.source.stem}.usable-deepseek.zh.mono.pdf'
        dual_output = args.capture / f'{args.source.stem}.usable-deepseek.zh.dual.pdf'
        _merge_pdfs(mono_pages, mono_output)
        _merge_pdfs(dual_pages, dual_output)
        with pymupdf.open(args.source) as source_doc:
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
        expected = total_pages
        status = 'completed' if all(count == expected for count in counts.values()) else 'page_count_mismatch'
        print(json.dumps({'status': status, 'expected_pages': expected, 'outputs': counts}, ensure_ascii=True), flush=True)
        return

    units = [TranslationUnit.model_validate_json(line) for line in (args.capture / 'units.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
    by_id = {u.unit_id: u for u in units}
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
    rendered, reused, empty = [], [], []
    for page_index in chunk_pages:
        page_dir = args.capture / f'page-{page_index + 1:04d}'
        mono = page_dir / f'{args.source.stem}.zh.mono.pdf'
        dual = page_dir / f'{args.source.stem}.zh.dual.pdf'
        if mono.is_file() and dual.is_file():
            reused.append(page_index + 1)
            continue
        page_records = records_by_page.get(page_index, [])
        if not page_records:
            if not _page_is_verifiably_empty(args.source, page_index):
                raise RuntimeError(f'page {page_index + 1} has content but no captured records')
            page_dir.mkdir(parents=True, exist_ok=True)
            with pymupdf.open(args.source) as original, pymupdf.open() as single:
                single.insert_pdf(original, from_page=page_index, to_page=page_index)
                single.save(mono)
                single.save(dual)
            empty.append(page_index + 1)
            continue
        page_dir.mkdir(parents=True, exist_ok=True)
        translator = _injection_translator(page_records, translated_by_fragment)
        result = _run_babeldoc_page(args.source, page_index, page_dir, translator, layout=layout)
        if translator.errors or translator.remaining:
            raise RuntimeError(f'injection mismatch on page {page_index + 1}: errors={translator.errors}, remaining={translator.remaining}')
        rendered.append(page_index + 1)
        print(f'page {page_index + 1} rendered', flush=True)
    print(json.dumps({'rendered': rendered, 'reused': reused, 'empty_passthrough': empty}, ensure_ascii=True), flush=True)


if __name__ == '__main__':
    main()
