from pathlib import Path
import json
import pytest
from pdf_translation.config import load_config
from pdf_translation import native_pipeline as pipeline

SOURCE=Path('pdf/cs224n-2019-notes03-neuralnets.pdf')
FONT=Path('C:/Windows/Fonts/simfang.ttf')


def test_preparation_has_full_context_but_no_runtime_calls(tmp_path,monkeypatch):
    seen={}
    def estimate(units,config,*,context_units):
        seen.update(selected=units,context=context_units)
        return {'api_calls':0}
    def forbidden(*args,**kwargs):raise AssertionError('prepare must not call runtime')
    monkeypatch.setattr(pipeline,'estimate_units',estimate)
    monkeypatch.setattr(pipeline,'run_units',forbidden)
    cfg=load_config(Path('configs/v0.7.native.deepseek.toml'))
    out=tmp_path/'prepare'
    report=pipeline.run_native_pdf(SOURCE,cfg,out,tmp_path/'runs',FONT,pages=[15])
    assert report['status']=='prepared' and report['estimate']['api_calls']==0
    assert len(seen['context'])>len(seen['selected'])>0
    assert any('Whitening' in u.source_text for u in seen['selected'])
    assert not list(out.glob('*.pdf'))
    assert json.loads((out/'native.manifest.json').read_text())['pages']==[15]
    with pytest.raises(ValueError,match='identity changed'):
        pipeline.run_native_pdf(SOURCE,cfg,out,tmp_path/'runs',FONT,pages=[16])
    other_font=tmp_path/'other-font.ttf'
    other_font.write_bytes(FONT.read_bytes()+b'changed-font-identity')
    with pytest.raises(ValueError,match='identity changed'):
        pipeline.run_native_pdf(SOURCE,cfg,out,tmp_path/'runs',other_font,pages=[15])


def test_failed_translation_gate_blocks_all_rendering(tmp_path,monkeypatch):
    monkeypatch.setattr(pipeline,'estimate_units',lambda *a,**k:{'api_calls':0})
    monkeypatch.setattr(pipeline,'run_units',lambda *a,**k:{'states':{'review_required':8}})
    class State:
        def __init__(self,*args):pass
        def state(self,*args):return 'review_required'
    monkeypatch.setattr(pipeline,'StateStore',State)
    def forbidden(*args,**kwargs):raise AssertionError('blocked units must not render')
    monkeypatch.setattr(pipeline,'render_asset_page',forbidden)
    report=pipeline.run_native_pdf(SOURCE,load_config(Path('configs/v0.7.native.deepseek.toml')),tmp_path/'blocked',tmp_path/'runs',FONT,pages=[15],execute_paid=True)
    assert report['status']=='translation_quality_blocked' and report['blocked_units']
    assert report['formal_use_ready'] is False
    assert not list((tmp_path/'blocked').glob('*.pdf'))


@pytest.mark.parametrize("changed", ["glossary", "styles", "semantics"])
def test_context_revision_invalidates_preparation_and_runtime_identity(tmp_path,monkeypatch,changed):
    snapshot={"glossary":[],"styles":[],"semantics":[]}
    class Context:
        def __init__(self,*args):pass
        def glossary(self):return snapshot["glossary"]
        def styles(self):return snapshot["styles"]
        def semantics(self):return snapshot["semantics"]
    monkeypatch.setattr(pipeline,'ContextStore',Context)
    monkeypatch.setattr(pipeline,'estimate_units',lambda *a,**k:{'api_calls':0})
    # Extraction is independent of this identity regression and already tested above.
    from types import SimpleNamespace
    units=[SimpleNamespace(unit_id='u',unit_type='paragraph',model_dump_json=lambda:'{}')]
    monkeypatch.setattr(pipeline,'prepare_asset_document',lambda _: (units,[{'source_pages':[15]}]))
    monkeypatch.setattr(pipeline,'enrich_pdf_units_with_context',lambda u:u)
    original_hash=pipeline.stable_hash
    monkeypatch.setattr(pipeline,'stable_hash',lambda value:original_hash(['u']) if value is units else original_hash(value))
    cfg=load_config(Path('configs/v0.7.native.deepseek.toml'))
    out=tmp_path/'prepare'
    first=pipeline.run_native_pdf(SOURCE,cfg,out,tmp_path/'runs',FONT,pages=[15])
    repeated=pipeline.run_native_pdf(SOURCE,cfg,out,tmp_path/'runs',FONT,pages=[15])
    assert repeated['run_id']==first['run_id']
    snapshot[changed].append({'revision':'changed'})
    with pytest.raises(ValueError,match='identity changed'):
        pipeline.run_native_pdf(SOURCE,cfg,out,tmp_path/'runs',FONT,pages=[15])
    next_run=pipeline.run_native_pdf(SOURCE,cfg,tmp_path/'revised',tmp_path/'runs',FONT,pages=[15])
    assert next_run['run_id']!=first['run_id']


@pytest.fixture
def small_native_run(tmp_path,monkeypatch):
    from types import SimpleNamespace
    units=[SimpleNamespace(unit_id='u',unit_type='paragraph',model_dump_json=lambda:'{}')]
    monkeypatch.setattr(pipeline,'prepare_asset_document',lambda _: (units,[{'source_pages':[1,2]}]))
    monkeypatch.setattr(pipeline,'enrich_pdf_units_with_context',lambda u:u)
    original_hash=pipeline.stable_hash
    monkeypatch.setattr(pipeline,'stable_hash',lambda value:original_hash(['u']) if value is units else original_hash(value))
    monkeypatch.setattr(pipeline,'estimate_units',lambda *a,**k:{'api_calls':0})
    monkeypatch.setattr(pipeline,'run_units',lambda *a,**k:{'states':{'accepted':1}})
    result=SimpleNamespace(draft=SimpleNamespace(translated_text='test'),model_dump=lambda **k:{'draft':'test'})
    class State:
        def __init__(self,*args):pass
        def state(self,*args):return 'accepted'
        def result(self,*args):return result
    monkeypatch.setattr(pipeline,'StateStore',State)
    out=tmp_path/'out'
    cfg=load_config(Path('configs/v0.7.native.deepseek.toml'))
    def run(execute=True):
        return pipeline.run_native_pdf(SOURCE,cfg,out,tmp_path/'runs',FONT,pages=[1,2],execute_paid=execute)
    return out,run


def test_render_exception_persists_completed_pages_and_preserves_output(small_native_run,monkeypatch):
    out,run=small_native_run
    def render(source,units,plans,translations,page,output,font):
        if page==1:raise ValueError('injected layout failure')
        output.write_bytes(b'first page fixture')
        return {'passed':True}
    monkeypatch.setattr(pipeline,'render_asset_page',render)
    with pytest.raises(ValueError,match='injected'):run()
    report=json.loads((out/'native.report.json').read_text())
    assert report['status']=='render_failed' and report['blocked_page']==2
    assert report['rendered_pages']==[1] and report['formal_use_ready'] is False
    assert (out/'page-001.zh.pdf').read_bytes()==b'first page fixture'
    def forbidden(*a,**k):raise AssertionError('existing PDF must block before API')
    monkeypatch.setattr(pipeline,'run_units',forbidden)
    with pytest.raises(FileExistsError):run()
    assert json.loads((out/'native.report.json').read_text())==report


def test_provider_exception_is_recorded_without_payload(small_native_run,monkeypatch):
    out,run=small_native_run
    def fail(*a,**k):raise RuntimeError('sensitive payload fixture')
    monkeypatch.setattr(pipeline,'run_units',fail)
    with pytest.raises(RuntimeError):run()
    text=(out/'native.report.json').read_text()
    report=json.loads(text)
    assert report['status']=='translation_failed' and report['error_type']=='RuntimeError'
    assert 'sensitive payload' not in text and report['formal_use_ready'] is False


def test_false_render_proof_stops_before_next_page(small_native_run,monkeypatch):
    out,run=small_native_run
    calls=[]
    def render(*args):
        calls.append(args[4])
        return {'passed':False}
    monkeypatch.setattr(pipeline,'render_asset_page',render)
    report=run()
    assert calls==[0] and report['status']=='render_blocked'
    assert report['blocked_page']==1 and report['rendered_pages']==[]
    assert report['formal_use_ready'] is False
