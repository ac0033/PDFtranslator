import json,runpy
from pathlib import Path
import pytest

audit=runpy.run_path(str(Path(__file__).parents[1]/'scripts/audit_approved_terms.py'))['audit']


@pytest.mark.parametrize('target,passed',[('成本函数',True),('函数',False),('成本函数又称代价函数',False)])
def test_missing_or_forbidden_term_fails_saved_record_audit(tmp_path,target,passed):
    index=tmp_path/'index.json';index.write_text(json.dumps({'source_pdf_sha256':'source-fixture','accepted_source_pages':[1]}),encoding='utf-8')
    (tmp_path/'page-001.translation-review.json').write_text(json.dumps([{'unit_id':'u','source_text':'cost function','translated_text':target}]),encoding='utf-8')
    glossary=tmp_path/'terms.json';glossary.write_text(json.dumps({'glossary':[{'term_id':'cost','source_term':'cost function','target_term':'成本函数','sense':'optimization','scope':{'kind':'document','value':'doc'},'source_ids':['fixture'],'version':'1','status':'approved','forbidden_target_terms':['代价函数']}]}),encoding='utf-8')
    result=audit(index,glossary,'doc')
    assert result['passed'] is passed
    assert result['reviews'][0]['sha256']
    assert result['entire_document_formal_use_ready'] is False
