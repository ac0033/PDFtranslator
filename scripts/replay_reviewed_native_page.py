"""Replay source-matched reviewed translations without trusting ordinal unit IDs."""
import argparse,json,re,copy
from pathlib import Path
from audit_cross_page_records import canonical
from pdf_translation.native_asset_plan import prepare_asset_document
from pdf_translation.native_asset_layout import render_asset_page

parser=argparse.ArgumentParser()
parser.add_argument('--source',type=Path,required=True)
parser.add_argument('--review',type=Path,required=True)
parser.add_argument('--page',type=int,required=True)
parser.add_argument('--font',type=Path,required=True)
parser.add_argument('--output-dir',type=Path,required=True)
a=parser.parse_args()
if a.output_dir.exists():raise FileExistsError('use a fresh output directory')
records=json.loads(a.review.read_text(encoding='utf-8'))
lookup={}
for row in records:
 key=canonical(row['source_text'],row.get('source_literals',{}))
 if key in lookup:raise ValueError('ambiguous reviewed source')
 lookup[key]=row
units,plans=prepare_asset_document(a.source)
translations={};rebound=[]
for u,p in zip(units,plans):
 if a.page not in p['source_pages'] or u.unit_type=='protected':continue
 key=canonical(u.source_text,u.protected_literal_hints)
 if key not in lookup:raise ValueError('source changed or missing review: '+u.source_text)
 row=copy.deepcopy(lookup[key])
 old=re.findall(r'⟦P:[^⟧]+⟧',row['source_text']);new=re.findall(r'⟦P:[^⟧]+⟧',u.source_text)
 if len(old)!=len(new):raise ValueError('marker count changed')
 mapping=dict(zip(old,new))
 text=re.sub(r'⟦P:[^⟧]+⟧',lambda m:mapping[m.group()],row['translated_text'])
 assert canonical(text,u.protected_literal_hints)==canonical(row['translated_text'],row.get('source_literals',{}))
 row.update(previous_unit_id=row['unit_id'],unit_id=u.unit_id,source_text=u.source_text,source_literals=u.protected_literal_hints,source_pages=p['source_pages'],translated_text=text)
 translations[u.unit_id]=text;rebound.append(row)
a.output_dir.mkdir(parents=True)
(a.output_dir/'translation-review.json').write_text(json.dumps(rebound,ensure_ascii=False,indent=2),encoding='utf-8')
proof=render_asset_page(a.source,units,plans,translations,a.page-1,a.output_dir/f'page-{a.page:03}.zh.pdf',a.font)
(a.output_dir/f'page-{a.page:03}.render-proof.json').write_text(json.dumps(proof,ensure_ascii=False,indent=2),encoding='utf-8')
print({'passed':proof['passed'],'reviewed_units':len(rebound)})
if not proof['passed']:raise SystemExit(1)
