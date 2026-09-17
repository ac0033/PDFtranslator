"""Audit complete cross-page review records by source content, not ordinal ID."""
import argparse,json,re,hashlib
from pathlib import Path


def canonical(text,literals):
    def replace(match):
        marker=match.group()
        if marker not in literals:raise ValueError('cross-page record has unresolved native literal')
        return '⟦literal:'+json.dumps(literals[marker],ensure_ascii=False)+'⟧'
    return re.sub(r'\s+',' ',re.sub(r'⟦P:[^⟧]+⟧',replace,text)).strip()


def audit(index_path):
    index_path=Path(index_path);root=index_path.parent;index=json.loads(index_path.read_text(encoding='utf-8'));groups={}
    for page in index['accepted_source_pages']:
        override=index.get('accepted_artifact_overrides',{}).get(str(page),{})
        path=root/override.get('translation_review',f'page-{page:03}.translation-review.json')
        for row in json.loads(path.read_text(encoding='utf-8')):
            pages=row.get('source_pages',[])
            if len(pages)<2:continue
            source=canonical(row['source_text'],row.get('source_literals',{}))
            identity=hashlib.sha256(json.dumps([pages,source],ensure_ascii=False).encode()).hexdigest()
            group=groups.setdefault(identity,{'source_identity':identity,'expected_pages':pages,'records':[]})
            group['records'].append({'page':page,'record_file':path.name,'legacy_or_current_unit_id':row['unit_id'],'translated_text':canonical(row['translated_text'],row.get('source_literals',{}))})
    checks=[]
    for group in groups.values():
        records=group.pop('records');actual=[r['page'] for r in records]
        group.update(recorded_pages=actual,full_records_present=sorted(actual)==sorted(group['expected_pages']),full_translation_identical=len({r['translated_text'] for r in records})==1,record_sources=[{'page':r['page'],'file':r['record_file']} for r in records])
        checks.append(group)
    return {'checks':checks,'passed':bool(checks) and all(c['full_records_present'] and c['full_translation_identical'] for c in checks),'scope':'Complete cross-page records and translations agree after native marker resolution. Assembly pixel proof and source/translation review are separate requirements.','entire_document_formal_use_ready':False}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('index');parser.add_argument('report');a=parser.parse_args();result=audit(a.index);Path(a.report).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps({'parents':len(result['checks']),'passed':result['passed']}))
    if not result['passed']:raise SystemExit(1)
