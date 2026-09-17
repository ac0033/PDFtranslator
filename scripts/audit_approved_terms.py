"""Audit all approved term/unit pairs in the selected reviewed-page index."""
import argparse,json
from pathlib import Path
from pdf_translation.context_models import GlossaryEntry
from pdf_translation.context_router import select_glossary
from pdf_translation.term_matching import normalize_target_spacing
from pdf_translation.schemas import TranslationUnit
from pdf_translation.hashing import sha256_file


def audit(index_path,glossary_path,document_id):
    index_path=Path(index_path);glossary_path=Path(glossary_path);root=index_path.parent
    index=json.loads(index_path.read_text(encoding='utf-8'))
    glossary=[GlossaryEntry.model_validate(e) for e in json.loads(glossary_path.read_text(encoding='utf-8'))['glossary']]
    checks=[];reviewed=[]
    for page in index['accepted_source_pages']:
        p=root/index.get('accepted_artifact_overrides',{}).get(str(page),{}).get('translation_review',f'page-{page:03}.translation-review.json')
        reviewed.append({'page':page,'review_file':p.name,'sha256':sha256_file(p)})
        for row in json.loads(p.read_text(encoding='utf-8')):
            unit=TranslationUnit.model_construct(unit_id=row['unit_id'],document_id=document_id,source_text=row['source_text'],section_path=())
            target=normalize_target_spacing(row['translated_text'])
            for entry in select_glossary(unit,glossary):
                found=any(normalize_target_spacing(s) in target for s in (entry.target_term,*entry.accepted_target_variants))
                forbidden=[s for s in entry.forbidden_target_terms if normalize_target_spacing(s) in target]
                checks.append({'page':page,'unit_id':row['unit_id'],'source_term':entry.source_term,'target_term':entry.target_term,'required_present':found,'forbidden_found':forbidden,'passed':found and not forbidden})
    return {'glossary_sha256':sha256_file(glossary_path),'source_pdf_sha256':index['source_pdf_sha256'],
        'document_id':document_id,'approved_entries':sum(e.status=='approved' for e in glossary),'reviews':reviewed,
        'checks':checks,'passed':bool(checks) and all(c['passed'] for c in checks),
        'scope':'All approved glossary term/unit pairs in reviewed records; target occurrence checking does not establish occurrence alignment or discover unknown terms.',
        'entire_document_formal_use_ready':False}


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('index');p.add_argument('glossary');p.add_argument('document_id');p.add_argument('report');a=p.parse_args()
    result=audit(a.index,a.glossary,a.document_id);Path(a.report).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'checks':len(result['checks']),'passed':result['passed']}))
    if not result['passed']:raise SystemExit(1)
