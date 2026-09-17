import pytest
from pdf_translation.term_lock import protect_terms,restore_terms
from pdf_translation.schemas import ContextItem
from pdf_translation.analyzer import extract_term_candidates
from pdf_translation.schemas import TranslationUnit
from pdf_translation.hashing import stable_hash


def item(source,target):
    return ContextItem(item_id=source,category='glossary',content='',revision='1',required=True,selection_reason='approved',term_source=source,term_target=target)


def test_longest_phrase_and_repeated_occurrences_are_locked():
    source='An identity covariance matrix is a covariance matrix. Another identity covariance matrix.'
    masked,terms=protect_terms(source,[item('covariance matrix','协方差矩阵'),item('identity covariance matrix','单位协方差矩阵')])
    assert [t.source_term for t in terms]==['identity covariance matrix','covariance matrix','identity covariance matrix']
    restored,evidence=restore_terms('一个'+terms[0].marker+'是'+terms[1].marker+'。另一个'+terms[2].marker+'。',terms)
    assert restored=='一个单位协方差矩阵是协方差矩阵。另一个单位协方差矩阵。'
    assert evidence.provider_text.count('⟦T:')==3
    for broken in [terms[0].marker,masked+terms[0].marker,masked+'⟦T:999⟧']:
        with pytest.raises(ValueError):restore_terms(broken,terms)


def test_single_occurrence_multiword_concept_is_discovered():
    text='Whitening yields an identity covariance matrix.'
    u=TranslationUnit(unit_id='u',document_id='d',ir_revision='r',unit_type='paragraph',source_lang='en',target_lang='zh-CN',source_text=text,source_hash=stable_hash(text))
    candidates=extract_term_candidates([u])
    assert any(c.source_term=='identity covariance matrix' and c.source_ids==('u',) for c in candidates)


def test_candidate_discovery_ignores_internal_object_markers():
    text='An identity covariance matrix ⟦P:a-native-repeated⟧ ⟦P:a-native-repeated2⟧'
    u=TranslationUnit.model_construct(unit_id='u',source_text=text)
    terms=extract_term_candidates([u])
    assert any(t.source_term=='identity covariance matrix' for t in terms)
    assert not any('native' in t.source_term for t in terms)


def test_real_adapter_masks_terms_and_rejects_missing_marker_offline(monkeypatch):
    import json
    from types import SimpleNamespace
    from pdf_translation.providers import DeepSeekProvider,ProviderCallError
    from pdf_translation.schemas import ContextPacket
    from pdf_translation.pipeline import make_request
    from pdf_translation.config import RunConfig
    captured=[];reply=['一个⟦T:000⟧。']
    class FakeClient:
        def __init__(self,**kwargs):self.responses=self
        def create(self,**kwargs):
            captured.append(kwargs)
            return SimpleNamespace(id='offline-test-response',status='completed',output_text=json.dumps({'unit_id':'u','translated_text':reply[0],'uncertainties':[]}),usage=SimpleNamespace(input_tokens=10,output_tokens=10,input_tokens_details=None,output_tokens_details=None))
    monkeypatch.setattr('openai.OpenAI',FakeClient);monkeypatch.setenv('DEEPSEEK_API_KEY','offline-test-only')
    text='An identity covariance matrix.'
    unit=TranslationUnit(unit_id='u',document_id='d',ir_revision='r',unit_type='paragraph',source_lang='en',target_lang='zh-CN',source_text=text,source_hash=stable_hash(text))
    items=(item('identity covariance matrix','单位协方差矩阵'),)
    packet=ContextPacket(unit_id='u',router_revision='r',memory_snapshot_id='m',items=items,token_count=10,token_counter_id='test',token_count_is_estimate=True,token_budget=1000,packet_hash=stable_hash({'unit_id':'u','items':items}))
    cfg=RunConfig(run_id='terms',provider='deepseek',model_id='deepseek-flash',style_revision='s',prompt_revision='p',adapter_revision='a',pricing_revision='none',max_output_tokens=100,budget_usd=0)
    request=make_request(unit,cfg,packet);result=DeepSeekProvider().translate(request)
    assert '<source-to-translate>\nAn ⟦T:000⟧.\n</source-to-translate>' in captured[0]['input']
    assert result.draft.translated_text=='一个单位协方差矩阵。'
    assert result.terminology_evidence.provider_text=='一个⟦T:000⟧。'
    reply[0]='一个协方差矩阵变为单位矩阵。'
    with pytest.raises(ProviderCallError):DeepSeekProvider().translate(request)


@pytest.mark.parametrize('phrase',[
    'binary logistic regression','outer product','Frobenius norm',
    'recurrent neural networks','bias vectors','decision boundaries',
    'weight matrices','word embeddings','identity covariance\nmatrix',
    'binary logistic\r\nregression',
])
def test_single_occurrence_domain_noun_phrases_are_candidates(phrase):
    unit=TranslationUnit.model_construct(unit_id='u',source_text='Consider '+phrase+'.')
    candidates=extract_term_candidates([unit])
    expected=' '.join(phrase.split())
    assert any(c.source_term==expected and c.occurrence_count==1 and c.source_ids==('u',) for c in candidates)


@pytest.mark.parametrize('text',[
    'outer. product', 'outer; product', 'outer / product',
    'outer ⟦P:000⟧ product', 'outer https://example.com product',
    'outer and product',
])
def test_noun_phrase_discovery_does_not_cross_semantic_boundaries(text):
    unit=TranslationUnit.model_construct(unit_id='u',source_text=text)
    assert not any(c.source_term=='outer product' for c in extract_term_candidates([unit]))
