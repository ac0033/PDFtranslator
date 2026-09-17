"""Offline RC14 regression from the exact accepted index, including overrides."""
import argparse,json
from pathlib import Path
from pdf_translation.hashing import sha256_file,stable_hash
from pdf_translation.review_replay import physical_source,rebind_review
from pdf_translation.native_asset_plan import prepare_asset_document
from pdf_translation.native_asset_layout import render_asset_page

parser=argparse.ArgumentParser()
parser.add_argument('--source',type=Path,required=True);parser.add_argument('--index',type=Path,required=True)
parser.add_argument('--review-patch',type=Path);parser.add_argument('--output',type=Path,required=True);parser.add_argument('--layout-only',action='store_true')
a=parser.parse_args();index=json.loads(a.index.read_text(encoding='utf-8'));root=a.index.parent
if index['source_pdf_sha256']!=sha256_file(a.source):raise ValueError('review belongs to another PDF')
if a.output.exists():raise FileExistsError('use a fresh output directory')
a.output.mkdir(parents=True)
patches={}
if a.review_patch:
    patch=json.loads(a.review_patch.read_text(encoding='utf-8'))
    if patch['source_pdf_sha256']!=index['source_pdf_sha256']:raise ValueError('review patch source changed')
    patches={e['review_record_hash']:e for e in patch['entries']}
lookup={};sources={}
for page in index['accepted_source_pages']:
    path=root/index.get('accepted_artifact_overrides',{}).get(str(page),{}).get('translation_review',f'page-{page:03}.translation-review.json')
    for record in json.loads(path.read_text(encoding='utf-8')):
        original_hash=stable_hash(record)
        if original_hash in patches:
            entry=patches[original_hash]
            if record['source_pages']!=entry['source_pages'] or record['source_literals'][entry['marker']]!=entry['old']:raise ValueError('review patch identity changed')
            record={**record,'source_literals':{**record['source_literals'],entry['marker']:entry['new']}}
        key=physical_source(record['source_text'],record.get('source_literals',{}))[0]
        if key in lookup and physical_source(lookup[key]['translated_text'],lookup[key].get('source_literals',{}))[0]!=physical_source(record['translated_text'],record.get('source_literals',{}))[0]:
            raise ValueError('accepted records disagree for the same complete source')
        lookup[key]=record;sources[key]={'path':str(path),'sha256':sha256_file(path),'original_record_hash':original_hash,'hint_revision':patches.get(original_hash)}
units,plans=prepare_asset_document(a.source);translations={};rebound=[];rebindings=[]
for unit,plan in zip(units,plans):
    if unit.unit_type=='protected':continue
    key=physical_source(unit.source_text,unit.protected_literal_hints)[0]
    record,proof=rebind_review(unit,plan,lookup[key]);proof['review_file']=sources[key]
    translations[unit.unit_id]=record['translated_text'];rebound.append(record);rebindings.append(proof)
(a.output/'translation-review.json').write_text(json.dumps(rebound,ensure_ascii=False,indent=2),encoding='utf-8')
(a.output/'rebindings.json').write_text(json.dumps(rebindings,ensure_ascii=False,indent=2),encoding='utf-8')
reports=[]
for page in index['accepted_source_pages']:
    try:
        proof=render_asset_page(a.source,units,plans,translations,page-1,a.output/f'page-{page:03}.zh.pdf',Path('C:/Windows/Fonts/simfang.ttf'),layout_only=a.layout_only)
    except Exception as exc:proof={'passed':False,'error':str(exc),'error_type':type(exc).__name__}
    if proof['passed'] and not a.layout_only:proof['output_sha256']=sha256_file(a.output/f'page-{page:03}.zh.pdf')
    (a.output/f'page-{page:03}.proof.json').write_text(json.dumps(proof,ensure_ascii=False,indent=2),encoding='utf-8')
    reports.append({'page':page,**proof})
    (a.output/'report.json').write_text(json.dumps({'source_sha256':sha256_file(a.source),'review_index_sha256':sha256_file(a.index),'api_calls':0,'pages':reports},ensure_ascii=False,indent=2),encoding='utf-8')
    print(page,proof['passed'],proof.get('error_type',''),flush=True)
