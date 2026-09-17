"""Native source-object PDF workflow using the standard translation runtime."""
import json
from pathlib import Path
import pymupdf
from .native_asset_plan import prepare_asset_document
from .native_asset_layout import render_asset_page
from .document_context import enrich_pdf_units_with_context
from .pipeline import estimate_units,run_units
from .storage import StateStore
from .context_store import ContextStore
from .hashing import sha256_file,stable_hash


def run_native_pdf(source,config,output_dir,runs_root,font_path,*,pages=None,execute_paid=False):
    source=Path(source);output_dir=Path(output_dir);font_path=Path(font_path)
    if config.context_engine!='v1' or not config.context_db:raise ValueError('native workflow requires V1 context and context_db')
    if not font_path.is_file():raise ValueError('target font does not exist')
    with pymupdf.open(source) as doc:count=len(doc)
    pages=list(range(1,count+1)) if pages is None else list(pages)
    if not pages or len(set(pages))!=len(pages) or any(p<1 or p>count for p in pages):raise ValueError('invalid source page selection')
    units,plans=prepare_asset_document(source)
    units=enrich_pdf_units_with_context(units)
    selected=[u for u,p in zip(units,plans) if u.unit_type!='protected' and set(p['source_pages'])&set(pages)]
    if not selected:raise ValueError('selection has no translatable text')
    context_store=ContextStore(Path(config.context_db))
    context_snapshot={'glossary':context_store.glossary(),'styles':context_store.styles(),'semantics':context_store.semantics()}
    context_hash=stable_hash(context_snapshot)
    config=config.model_copy(update={'run_id':config.run_id+'-'+sha256_file(source)[:12]+'-'+stable_hash(pages)[:8]+'-'+context_hash[:12], 'adapter_revision':'native-assets-rc14-spacing'})
    identity={'context_snapshot_hash':context_hash,'source_pdf_sha256':sha256_file(source),'target_font_sha256':sha256_file(font_path),'pages':pages,'config_hash':config.config_hash,'units_hash':stable_hash(units),'plans_hash':stable_hash(plans)}
    output_dir.mkdir(parents=True,exist_ok=True);manifest=output_dir/'native.manifest.json'
    if manifest.exists():
        if json.loads(manifest.read_text(encoding='utf-8'))!=identity:raise ValueError('prepared native identity changed; use another output directory')
    else:
        manifest.write_text(json.dumps(identity,indent=2),encoding='utf-8')
        (output_dir/'units.jsonl').write_text('\n'.join(u.model_dump_json() for u in units)+'\n',encoding='utf-8')
        (output_dir/'native-plans.json').write_text(json.dumps(plans,ensure_ascii=False,indent=2),encoding='utf-8')
        (output_dir/'run.config.json').write_text(config.model_dump_json(indent=2),encoding='utf-8')
    estimate=estimate_units(selected,config,context_units=units)
    report={'status':'prepared','source_pages':pages,'selected_units':len(selected),'context_units':len(units),'estimate':estimate,'formal_use_ready':False,'run_id':config.run_id}
    def save():
        (output_dir/'native.report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        return report
    if not execute_paid:return save()
    # Refuse existing outputs before a provider can incur any new charges.
    if any((output_dir/f'page-{page:03}.zh.pdf').exists() for page in pages):
        raise FileExistsError('native render exists; inspect it before rerunning')
    report.update(status='translating')
    save()
    try:
        report['runtime']=run_units(selected,config,Path(runs_root),context_units=units)
    except Exception as exc:
        report.update(status='translation_failed',error_type=type(exc).__name__)
        save()
        raise
    state=StateStore(Path(runs_root)/config.run_id/'state.sqlite')
    blocked=[u.unit_id for u in selected if state.state(config.run_id,u.unit_id)!='accepted']
    if blocked:report.update(status='translation_quality_blocked',blocked_units=blocked);return save()
    translations={u.unit_id:state.result(config.run_id,u.unit_id).draft.translated_text for u in selected}
    results=[state.result(config.run_id,u.unit_id).model_dump(mode='json') for u in selected]
    (output_dir/'translation-results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
    rendered=[]
    for page in pages:
        report.update(status='rendering',current_page=page,rendered_pages=list(rendered))
        save()
        output=output_dir/f'page-{page:03}.zh.pdf'
        try:
            if output.exists():raise FileExistsError('native render exists; inspect it before rerunning')
            proof=render_asset_page(source,units,plans,translations,page-1,output,font_path)
            (output_dir/f'page-{page:03}.render-proof.json').write_text(json.dumps(proof,ensure_ascii=False,indent=2),encoding='utf-8')
            if not proof['passed']:report.update(status='render_blocked',blocked_page=page,rendered_pages=list(rendered));return save()
            with pymupdf.open(source) as doc:doc[page-1].get_pixmap(matrix=pymupdf.Matrix(2,2)).save(output_dir/f'page-{page:03}.source.png')
            rendered.append(page)
        except Exception as exc:
            # Exception messages may contain provider payloads; retain only type here.
            report.update(status='render_failed',blocked_page=page,rendered_pages=list(rendered),error_type=type(exc).__name__)
            save()
            raise
    report.update(status='rendered_pending_semantic_visual_review',rendered_pages=rendered,formal_use_ready=False)
    return save()
