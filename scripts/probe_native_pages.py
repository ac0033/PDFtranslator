"""Independent per-page layout stress checks. No provider calls or translations."""
import argparse,json,re,time,faulthandler
from pathlib import Path
import pymupdf
from pdf_translation.native_asset_plan import prepare_asset_document
from pdf_translation.native_asset_layout import render_asset_page
from pdf_translation.hashing import sha256_file
p=argparse.ArgumentParser();p.add_argument('--source',type=Path,required=True);p.add_argument('--pages',type=int,nargs='+');p.add_argument('--output',type=Path,required=True);a=p.parse_args()
if a.output.exists():raise FileExistsError(a.output)
a.output.mkdir(parents=True)
report={'source_sha256':sha256_file(a.source),'api_calls':0,'synthetic_placeholder_text':True,'scope':'independent physical pages; cross-page flow is a separate check','pages':[]}
report['implementation']={str(f):sha256_file(f) for f in sorted(Path('src/pdf_translation').glob('native_*.py'))}
with pymupdf.open(a.source) as original:
 for number in a.pages or range(1,len(original)+1):
  faulthandler.dump_traceback_later(45,repeat=True)
  started=time.monotonic();root=a.output/f'page-{number:03d}';root.mkdir();source=root/'source.pdf'
  with pymupdf.open() as single:
   single.insert_pdf(original,from_page=number-1,to_page=number-1);single.save(source)
  try:
   units,plans=prepare_asset_document(source);translations={}
   for unit in units:
    if unit.unit_type=='protected':continue
    parts=re.split(r'(⟦P:[^⟧]+⟧)',unit.source_text)
    translations[unit.unit_id]=''.join(s if s.startswith('⟦P:') else '测'*max(1,round(len(s.strip())*.35)) if s.strip() else s for s in parts)
   proof=render_asset_page(source,units,plans,translations,0,root/'synthetic.pdf',Path('C:/Windows/Fonts/simfang.ttf'),layout_only=True)
   (root/'proof.json').write_text(json.dumps(proof,ensure_ascii=False),encoding='utf-8')
   item={'page':number,'passed':proof['passed'],'overflows':proof.get('overflows',[])}
  except Exception as error:item={'page':number,'passed':False,'error_type':type(error).__name__,'error':str(error)}
  faulthandler.cancel_dump_traceback_later()
  item['seconds']=time.monotonic()-started;report['pages'].append(item)
  (a.output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
  print(json.dumps(item,ensure_ascii=True),flush=True)
report['passed']=all(x['passed'] for x in report['pages']);report['completed']=True
(a.output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
