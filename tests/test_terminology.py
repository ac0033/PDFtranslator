from pdf_translation.context_models import GlossaryEntry, Scope
from pdf_translation.context_router import build_context_packet
from pdf_translation.schemas import TranslationUnit, TranslationDraft
from pdf_translation.hashing import stable_hash
from pdf_translation.pipeline import make_request
from pdf_translation.config import RunConfig
from pdf_translation.providers import MockProvider
from pdf_translation.quality import check_quality


def fixture(text="Minimize the cost function."):
    unit=TranslationUnit(unit_id="u1",document_id="doc",ir_revision="r",unit_type="paragraph",source_lang="en",target_lang="zh-CN",source_text=text,source_hash=stable_hash(text))
    entry=GlossaryEntry(term_id="cost",source_term="cost function",target_term="成本函数",sense="machine learning",scope=Scope(kind="document",value="doc"),source_ids=("user",),version="1",status="approved",forbidden_target_terms=("代价函数",))
    request=make_request(unit,RunConfig(run_id="terms",provider="mock",model_id="offline",style_revision="s1",prompt_revision="p1",adapter_revision="a1",pricing_revision="none",max_output_tokens=100,budget_usd=0))
    result=MockProvider().translate(request)
    return unit,entry,result


def test_wrong_and_mixed_terminology_are_blocked():
    unit,entry,result=fixture()
    for text in ("最小化代价函数。","成本函数也称代价函数。"):
        candidate=result.model_copy(update={"draft":TranslationDraft(unit_id=unit.unit_id,translated_text=text)})
        assert check_quality(unit,candidate,[entry]).status=="blocked"
    correct=result.model_copy(update={"draft":TranslationDraft(unit_id=unit.unit_id,translated_text="最小化成本函数。")})
    assert check_quality(unit,correct,[entry]).status=="pass"


def test_term_is_required_context_and_does_not_match_other_senses():
    unit,entry,result=fixture()
    packet,_=build_context_packet(unit,[entry],[],[],{unit.unit_id:unit},token_budget=1000)
    assert any(i.required and "成本函数" in i.content and "代价函数" in i.content for i in packet.items)
    unrelated,_,result=fixture("A function has a computational cost.")
    candidate=result.model_copy(update={"draft":TranslationDraft(unit_id=unrelated.unit_id,translated_text="函数有计算开销。")})
    assert check_quality(unrelated,candidate,[entry]).status=="pass"


import pytest
from pdf_translation.term_lock import protect_terms,restore_terms
from pdf_translation.context_router import select_glossary
from pdf_translation.analyzer import first_occurrences


@pytest.mark.parametrize('source_term,target',[
    ('cost function','成本函数'),('identity covariance matrix','单位协方差矩阵'),
    ('Singular Value Decomposition','奇异值分解'),('weight matrix','权重矩阵'),
])
@pytest.mark.parametrize('separator',[' ','  ','\t','\n','\r\n','\u00a0'])
def test_layout_whitespace_has_consistent_term_selection_lock_and_quality(source_term,target,separator):
    phrase=separator.join(source_term.upper().split())
    unit,entry,result=fixture('Use '+phrase+'.')
    entry=entry.model_copy(update={'source_term':source_term,'target_term':target,'forbidden_target_terms':()})
    packet,_=build_context_packet(unit,[entry],[],[],{unit.unit_id:unit},token_budget=2000)
    masked,locks=protect_terms(unit.source_text,packet.items)
    assert len(locks)==1 and locks[0].source_term==phrase
    assert first_occurrences([unit],[source_term])=={source_term:unit.unit_id}
    restored,_=restore_terms('使用'+locks[0].marker+'。',locks)
    good=result.model_copy(update={'draft':TranslationDraft(unit_id=unit.unit_id,translated_text=restored)})
    bad=result.model_copy(update={'draft':TranslationDraft(unit_id=unit.unit_id,translated_text='使用该对象。')})
    assert check_quality(unit,good,[entry]).status=='pass'
    assert check_quality(unit,bad,[entry]).status=='blocked'


@pytest.mark.parametrize('text',['cost.function','cost / function','cost ⟦P:1⟧ function','cost https://example.com function','cost functions','forecast function','cost_function'])
def test_term_matching_does_not_cross_nonwhitespace_or_word_boundaries(text):
    unit,entry,_=fixture(text)
    assert select_glossary(unit,[entry])==[]
    assert first_occurrences([unit],['cost function'])=={}


def test_equivalent_whitespace_glossary_conflict_is_rejected():
    unit,entry,_=fixture('cost function')
    conflicting=entry.model_copy(update={'term_id':'other','source_term':'cost\nfunction','target_term':'不同译名'})
    with pytest.raises(ValueError,match='conflicting approved glossary'):
        select_glossary(unit,[entry,conflicting])


@pytest.mark.parametrize('target',['Frobenius范数','Frobenius 范数','Frobenius\n范数'])
def test_script_boundary_spacing_does_not_change_approved_term(target):
    unit,entry,result=fixture('Frobenius norm')
    entry=entry.model_copy(update={'source_term':'Frobenius norm','target_term':'Frobenius 范数','forbidden_target_terms':()})
    result=result.model_copy(update={'draft':TranslationDraft(unit_id=unit.unit_id,translated_text=target)})
    assert check_quality(unit,result,[entry]).status=='pass'


@pytest.mark.parametrize('target',['Frobenius数','Frobenius 范围','Frobe nius范数'])
def test_spacing_tolerance_does_not_accept_changed_term(target):
    unit,entry,result=fixture('Frobenius norm')
    entry=entry.model_copy(update={'source_term':'Frobenius norm','target_term':'Frobenius 范数','forbidden_target_terms':()})
    result=result.model_copy(update={'draft':TranslationDraft(unit_id=unit.unit_id,translated_text=target)})
    assert check_quality(unit,result,[entry]).status=='blocked'


def test_forbidden_term_cannot_evade_check_with_script_boundary_space():
    unit,entry,result=fixture('Frobenius norm')
    entry=entry.model_copy(update={'source_term':'Frobenius norm','target_term':'Frobenius 范数','forbidden_target_terms':('Frobenius范围',)})
    result=result.model_copy(update={'draft':TranslationDraft(unit_id=unit.unit_id,translated_text='Frobenius范数和Frobenius 范围')})
    assert check_quality(unit,result,[entry]).status=='blocked'


@pytest.mark.parametrize('translated,passed',[('Sigmoid函数',True),('sigmoid函数',True),('普通函数',False)])
def test_only_explicit_target_spelling_variants_are_accepted(translated,passed):
    unit,entry,result=fixture('sigmoid function')
    entry=entry.model_copy(update={'source_term':'sigmoid','target_term':'Sigmoid','accepted_target_variants':('sigmoid',),'forbidden_target_terms':()})
    result=result.model_copy(update={'draft':TranslationDraft(unit_id=unit.unit_id,translated_text=translated)})
    assert (check_quality(unit,result,[entry]).status=='pass') is passed


@pytest.mark.parametrize('phrase', [
    'named entity recognition', 'speech recognition', 'natural language processing',
    'population standard deviation', 'exponential decay', 'geometric margin',
    'Support Vector Machines', 'Bayesian inference', 'moving average',
    'maximum likelihood', 'conditional probability', 'sample covariance',
])
def test_single_occurrence_concepts_across_domain_families_are_candidates(phrase):
    from pdf_translation.analyzer import extract_term_candidates
    unit, _, _ = fixture('Consider ' + phrase + '.')
    candidates = extract_term_candidates([unit])
    match = next(c for c in candidates if c.source_term.casefold() == phrase.casefold())
    assert match.occurrence_count == 1 and match.source_ids == (unit.unit_id,)

def test_candidate_phrase_cannot_cross_sentence_or_native_asset_boundary():
    from pdf_translation.analyzer import extract_term_candidates
    unit, _, _ = fixture('Speech. Recognition. Standard ⟦P:x⟧ deviation.')
    names = {c.source_term.casefold() for c in extract_term_candidates([unit])}
    assert 'speech recognition' not in names
    assert 'standard deviation' not in names
