import json,runpy
from pathlib import Path

audit=runpy.run_path(str(Path(__file__).parents[1]/'scripts/audit_retained_english.py'))['audit']


def check(tmp_path,source,target):
    (tmp_path/'page-001.translation-review.json').write_text(json.dumps([{'unit_id':'u','source_text':source,'translated_text':target}]),encoding='utf-8')
    p=tmp_path/'index.json';p.write_text(json.dumps({'source_pdf_sha256':'fixture','accepted_source_pages':[1]}),encoding='utf-8')
    return audit(p)


def test_source_bound_example_and_pending_term_are_separate(tmp_path):
    r=check(tmp_path,'Use "Paris" with Softmax.','使用“Paris”和Softmax。')
    assert [x['category'] for x in r['records']]==['source_language_example','terminology_choice_pending']
    assert r['unresolved_occurrences']==1 and not r['terminology_acceptance_passed']


def test_untranslated_prose_and_unsupported_example_are_not_exempted(tmp_path):
    r=check(tmp_path,'Other words.','Museums in Paris are amazing')
    assert r['unresolved_occurrences']==4 and not r['terminology_acceptance_passed']


def test_arbitrary_quoted_source_word_is_not_automatically_approved(tmp_path):
    r=check(tmp_path,'The word "normalization".','单词“normalization”。')
    assert r['records'][0]['category']=='unclassified'


def test_unknown_english_cannot_hide_inside_approved_hybrid_name(tmp_path):
    r=check(tmp_path,'Frobenius norm.','Frobenius 范数 means something')
    assert r['records'][0]['category']=='approved_hybrid_term'
    assert r['unresolved_occurrences']==2 and not r['terminology_acceptance_passed']
