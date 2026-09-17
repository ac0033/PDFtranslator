"""Offline layout stress probe with explicitly synthetic, nontranslation text."""
import json,re,argparse
from pathlib import Path
from pdf_translation.schemas import TranslationUnit
from pdf_translation.native_asset_layout import render_asset_page
from pdf_translation.hashing import sha256_file
parser=argparse.ArgumentParser();parser.add_argument('--pages',type=int,nargs='+',default=[17,26,42,51,63,101,161,301,429]);parser.add_argument('--report',type=Path,required=True)
parser.add_argument('--prepared',type=Path,default=Path('output/pdf/v0.7-rc14-val-preparation'))
a=parser.parse_args();root=a.prepared
units=[TranslationUnit.model_validate_json(line) for line in (root/'units.jsonl').read_text(encoding='utf-8').splitlines()]
plans=json.loads((root/'plans.json').read_text(encoding='utf-8'));report={'source_sha256':sha256_file(Path('pdf/val.pdf')),'synthetic_placeholder_text':True,'translation_started':False,'api_calls':0,'pages':[]}
for page in a.pages:
    translations={}
    for unit,plan in zip(units,plans):
        if unit.unit_type=='protected' or page not in plan['source_pages']:continue
        pieces=re.split(r'(⟦P:[^⟧]+⟧)',unit.source_text)
        text=''.join(part if part.startswith('⟦P:') else '测'*max(1,round(len(part.strip())*.35)) if part.strip() else part for part in pieces)
        translations[unit.unit_id]=text
    try:
        proof=render_asset_page(Path('pdf/val.pdf'),units,plans,translations,page-1,Path(f'tmp/val-layout-probe-{page}.pdf'),Path('C:/Windows/Fonts/simfang.ttf'),layout_only=True)
        result={'page':page,'passed':proof['passed'],'units':len(translations),'overflows':proof.get('overflows',[])}
    except Exception as exc:result={'page':page,'passed':False,'error_type':type(exc).__name__,'error':str(exc),'units':len(translations)}
    report['pages'].append(result);a.report.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(page,result['passed'],result.get('error','')[:150],flush=True)
