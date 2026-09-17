"""Estimate / resume a budgeted usable-version sample from a preserved capture."""
import argparse
import json
from pathlib import Path
from dotenv import load_dotenv
from pdf_translation.babeldoc_adapter import run_smoke_document
from pdf_translation.config import load_config
from pdf_translation.context_models import Scope,StyleRule
from pdf_translation.context_store import ContextStore
from pdf_translation.pipeline import DEFAULT_TRANSLATION_STYLE
from pdf_translation.schemas import TranslationUnit


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--capture',type=Path,required=True)
    parser.add_argument('--config',type=Path,default=Path('configs/val.usable.deepseek.toml'))
    parser.add_argument('--execute-paid',action='store_true')
    args=parser.parse_args();load_dotenv(Path('.env'),override=False)
    cfg=load_config(args.config)
    units=[TranslationUnit.model_validate_json(line) for line in (args.capture/'units.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
    documents={u.document_id for u in units}
    if len(documents)!=1:raise ValueError('capture must belong to one source document')
    store=ContextStore(Path(cfg.context_db));document_id=next(iter(documents))
    store.put_style(StyleRule(rule_id=f'val-usable-style-{document_id}',scope=Scope(kind='document',value=document_id),version='val-usable-v1',content=DEFAULT_TRANSLATION_STYLE+'\n本书为 Algorithms for Validation。逐段完整翻译，不总结或删减；保留公式标记、数字、否定和条件关系；前后术语一致。'))
    records=[json.loads(line) for line in (args.capture/'capture-map.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
    pages=tuple(sorted({r['page_index'] for r in records}))
    report=run_smoke_document(Path('pdf/val.pdf'),pages,cfg,args.capture,Path('runs'),Path('tmp/pdfs')/args.capture.name,execute_paid=False,reuse_capture=True,sample_name='usable-sample')
    (args.capture/'estimate.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'estimated_max_cost_usd':report['estimated_max_cost_usd'],'units':report['unit_count'],'pages':report['physical_pages'],'budget_usd':cfg.budget_usd},ensure_ascii=True),flush=True)
    if args.execute_paid:
        if report['estimated_max_cost_usd']>cfg.budget_usd:raise ValueError('estimated sample cost exceeds configured ceiling; no API call made')
        report=run_smoke_document(Path('pdf/val.pdf'),pages,cfg,args.capture,Path('runs'),Path('tmp/pdfs')/args.capture.name,execute_paid=True,reuse_capture=True,sample_name='usable-sample',render_review_draft=True)
        print(json.dumps(report,ensure_ascii=True),flush=True)


if __name__=='__main__':main()
