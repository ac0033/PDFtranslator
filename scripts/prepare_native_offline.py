"""Prepare source units without contacting a translation provider."""
import argparse,json,time
from pathlib import Path
from pdf_translation.native_asset_plan import prepare_asset_document
from pdf_translation.hashing import sha256_file
p=argparse.ArgumentParser();p.add_argument('--source',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
if a.output.exists():raise FileExistsError(a.output)
a.output.mkdir(parents=True);start=time.monotonic();units,plans=prepare_asset_document(a.source)
(a.output/'units.jsonl').write_text(''.join(u.model_dump_json()+'\n' for u in units),encoding='utf-8')
(a.output/'plans.json').write_text(json.dumps(plans,ensure_ascii=False),encoding='utf-8')
report={'passed':True,'source_sha256':sha256_file(a.source),'units':len(units),'translatable_units':sum(u.unit_type!='protected' for u in units),'source_characters':sum(len(u.source_text) for u in units),'api_calls':0,'seconds':time.monotonic()-start}
(a.output/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8');print(json.dumps(report),flush=True)
