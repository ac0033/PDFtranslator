"""Rebuildable page evidence; automatic checks never imply human approval."""
from __future__ import annotations
from collections import Counter
import html
import json
from pathlib import Path
import pymupdf
from .hashing import sha256_file
from .pdf_fidelity import compare_visual_primitives


def _rows(path: Path) -> list[dict]:
    return [json.loads(s) for s in path.read_text(encoding='utf-8').splitlines() if s.strip()]


def build_page_review(source: Path, output_dir: Path) -> dict:
    report = json.loads((output_dir / 'run.report.json').read_text(encoding='utf-8'))
    if sha256_file(source) != report['source_sha256']:
        raise ValueError('page review source PDF hash mismatch')
    pages = report['physical_pages']
    if not pages or len(pages) != len(set(pages)):
        raise ValueError('page review requires unique physical pages')
    units_path = output_dir / report.get('translation_units_jsonl', 'units.jsonl')
    if units_path.resolve().parent != output_dir.resolve():
        raise ValueError('translation units must be inside the run directory')
    if report.get('translation_units_sha256') and sha256_file(units_path) != report['translation_units_sha256']:
        raise ValueError('translation units hash mismatch')
    units = _rows(units_path)
    review_path = output_dir / 'content-review.jsonl'
    rows = _rows(review_path) if review_path.exists() else []
    by_id = {r['unit_id']: r for r in rows}
    ids = {u['unit_id'] for u in units}
    if len(ids) != len(units) or len(by_id) != len(rows):
        raise ValueError('duplicate captured or review unit IDs')
    if set(by_id) - ids:
        raise ValueError('review contains unknown unit IDs')
    for u in units:
        r = by_id.get(u['unit_id'])
        if r and (r['source_hash'] != u['source_hash'] or r['source_text'] != u['source_text']):
            raise ValueError('review source differs from captured unit')
        refs = {ref['page_index'] + 1 for ref in u['source_refs']}
        if not refs or not refs.issubset(set(pages)):
            raise ValueError('captured unit pages do not match selection')
    hyphen_pages = set()
    for u in units:
        source_pages = {r['page_index'] + 1 for r in u['source_refs']}
        last_page = max(source_pages)
        if u['source_text'].rstrip().endswith('-') and last_page + 1 in pages:
            hyphen_pages.update((last_page, last_page + 1))
    target_path = output_dir / report['mono_pdf'] if report.get('mono_pdf') else None
    if target_path and target_path.resolve().parent != output_dir.resolve():
        raise ValueError('translated PDF must be inside the run directory')
    observations = {}
    observations_path = output_dir / 'agent-review.json'
    if observations_path.exists():
        saved = json.loads(observations_path.read_text(encoding='utf-8'))
        current_target_hash = sha256_file(target_path) if target_path and target_path.is_file() else None
        if saved.get('source_sha256') != report['source_sha256'] or saved.get('target_sha256') != current_target_hash:
            raise ValueError('agent observations refer to different PDF artifacts')
        observations = {row['source_page']: row for row in saved['pages']}
        if len(observations) != len(saved['pages']) or set(observations) != set(pages):
            raise ValueError('agent observations must cover each selected page exactly once')
    target = pymupdf.open(target_path) if target_path and target_path.is_file() else None
    assets = output_dir / 'page-review-assets'
    assets.mkdir(exist_ok=True)
    entries, sections = [], []
    try:
        with pymupdf.open(source) as original:
            if any(not isinstance(p, int) or p < 1 or p > len(original) for p in pages):
                raise ValueError('physical page outside source PDF')
            count_matches = target is not None and len(target) == len(pages)
            for i, p in enumerate(pages):
                page_units = [u for u in units if any(r['page_index'] + 1 == p for r in u['source_refs'])]
                pairs = [by_id.get(u['unit_id'], dict(unit_id=u['unit_id'], source_text=u['source_text'], translated_text=None, state='not_started')) for u in page_units]
                blockers = []
                if not page_units:
                    blockers.append('no_captured_units: coverage requires review')
                if any(not r.get('translated_text') or r.get('state') != 'accepted' for r in pairs):
                    blockers.append('missing_or_unaccepted_translation')
                if not count_matches:
                    blockers.append('missing_output_or_page_count_mismatch')
                source_image = f'page-review-assets/{i+1:04d}-source.png'
                original[p-1].get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5), alpha=False).save(output_dir / source_image)
                target_image, fidelity = None, None
                if count_matches:
                    target_image = f'page-review-assets/{i+1:04d}-target.png'
                    target[i].get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5), alpha=False).save(output_dir / target_image)
                    fidelity = compare_visual_primitives(original[p-1], target[i])
                    blockers.extend(k for k, passed in fidelity['checks'].items() if not passed)
                    if not target[i].get_text().strip() and page_units:
                        blockers.append('empty_target_text')
                warnings = sorted({issue.get('category', 'quality_issue') for r in pairs for issue in r.get('issues', [])})
                if any(len({r['page_index'] for r in u['source_refs']}) > 1 for u in page_units):
                    warnings.append('跨页段落：此处展示完整译文；PDF 按各页片段写回')
                if p in hyphen_pages:
                    warnings.append('cross_page_hyphenation_requires_review')
                if any(r.get('unresolved_formula_markers') for r in pairs):
                    warnings.append('unresolved_formula_literals')
                entry = dict(source_page=p, translated_page=i+1, unit_count=len(page_units), states=dict(Counter(r['state'] for r in pairs)), blockers=blockers, warnings=warnings, status='blocked' if blockers else 'pending_review', automated_checks_passed=not blockers, meaning_review=None, layout_review=None, formal_use_ready=False, fidelity=fidelity, source_image=source_image, target_image=target_image, units=pairs)
                entry['agent_review'] = observations.get(p)
                if entry['agent_review'] and any(entry['agent_review'].get(key) == 'fail' for key in ('meaning', 'layout')):
                    blockers.append('agent_review_failed')
                    entry['status'] = 'blocked'
                    entry['automated_checks_passed'] = not [b for b in blockers if b != 'agent_review_failed']
                entries.append(entry)
                figures = f'<figure><figcaption>原文</figcaption><img src="{source_image}"></figure>'
                figures += f'<figure><figcaption>译稿</figcaption><img src="{target_image}"></figure>' if target_image else '<p>译稿尚未生成</p>'
                table = ''.join('<tr><td><pre>'+html.escape(r['source_text'])+'</pre></td><td><pre>'+html.escape(r.get('translated_text') or '（无译文）')+'</pre></td></tr>' for r in pairs)
                notes = observations.get(p, {}).get('notes', '尚未记录 agent 逐页复核')
                details = html.escape(('; '.join(blockers + warnings) or '自动检查通过，语义和版面待复核') + ' | ' + notes)
                sections.append(f'<section id="page-{p}"><h2>第 {p} 页 · {entry["status"]}</h2><p>{details}</p><div class="pages">{figures}</div><details><summary>逐段原文与译文（{len(pairs)} 单元）</summary><table>{table}</table></details></section>')
    finally:
        if target is not None:
            target.close()
    result = dict(schema_version='1.0', source_sha256=report['source_sha256'], target_sha256=sha256_file(target_path) if target_path and target_path.is_file() else None, review_input_sha256=sha256_file(review_path) if review_path.exists() else None, run_report_sha256=sha256_file(output_dir / 'run.report.json'), page_count=len(pages), pages=entries, status='blocked' if any(e['blockers'] for e in entries) else 'pending_review', formal_use_ready=False, limitations=['Capture completeness requires source-page review', 'Automated checks do not establish semantic equivalence or human approval'])
    (output_dir / 'page-review.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    nav = ' · '.join(f'<a href="#page-{p}">第 {p} 页</a>' for p in pages)
    css = 'body{font:16px system-ui;margin:24px;color:#172b3a;background:#f5f7fa}section{background:white;padding:20px;margin:24px 0}nav{position:sticky;top:0;background:#e8eef4;padding:14px}.pages{display:flex;gap:16px}figure{margin:0;flex:1;min-width:0}img{width:100%;border:1px solid #ccd}table{width:100%;table-layout:fixed}td{vertical-align:top;border-bottom:1px solid #ddd;padding:12px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit}summary{cursor:pointer;padding:16px}'
    document = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>PDF 逐页验收</title><style>'+css+'</style><h1>PDF 逐页验收</h1><p>自动检查、语义复核与版面复核分别记录。生成译稿不代表验收通过。</p><nav>'+nav+'</nav>'+''.join(sections)+'</html>'
    (output_dir / 'page-review.html').write_text(document, encoding='utf-8')
    return dict(page_review_html='page-review.html', page_review_json='page-review.json', page_review_status=result['status'], review_page_count=len(pages))
