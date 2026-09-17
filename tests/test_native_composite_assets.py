from pathlib import Path
import re
import json
import pytest
import pymupdf
from pdf_translation.native_asset_plan import prepare_asset_document
from pdf_translation.native_asset_layout import render_asset_page


@pytest.mark.parametrize('ratio',[.35,.12])
def test_original_inline_fraction_rule_moves_once_with_its_glyphs(tmp_path,ratio):
    source=tmp_path/'source.pdf'
    with pymupdf.open('pdf/val.pdf') as original,pymupdf.open() as single:
        single.insert_pdf(original,from_page=46,to_page=46);single.save(source)
    units,plans=prepare_asset_document(source);translations={}
    for unit in units:
        if unit.unit_type=='protected':continue
        parts=re.split(r'(⟦P:[^⟧]+⟧)',unit.source_text)
        translations[unit.unit_id]=''.join(p if p.startswith('⟦P:') else '测'*max(1,round(len(p.strip())*ratio)) if p.strip() else p for p in parts)
    output=tmp_path/'synthetic.pdf'
    proof=render_asset_page(source,units,plans,translations,0,output,Path('C:/Windows/Fonts/simfang.ttf'))
    (tmp_path/'proof.json').write_text(json.dumps(proof,indent=2),encoding='utf-8')
    if ratio==.12:
        assert len(proof['container_proof'])==1
        assert proof['container_proof'][0]['height_delta'] < -20
        inner=[g for g in proof['vertical_spacing'] if 'container' in g]
        assert max(g['dy'] for g in inner)-min(g['dy'] for g in inner)>20
        assert all(abs(gap['delta'])<=.5 for g in inner for gap in g['gaps'])
    assert proof['passed'] and proof['saved_source_glyph_inventory_verified']
    copied=[end for asset in proof['assets'] for end in asset['source_paint_ends']]
    removed=[a['end'] for a in proof['artwork_shifts'] if a.get('remove')]
    assert copied and len(copied)==len(set(copied)) and set(copied)==set(removed)
    assert all(asset['changed_pixels']==0 for asset in proof['assets'])
    with pymupdf.open(source) as a,pymupdf.open(output) as b:
        assert len(a[0].get_drawings())==len(b[0].get_drawings())


def test_saved_inventory_failure_never_publishes_a_pdf(tmp_path,monkeypatch):
    import pdf_translation.native_asset_layout as layout
    source=tmp_path/'source.pdf'
    with pymupdf.open('pdf/val.pdf') as original,pymupdf.open() as single:
        single.insert_pdf(original,from_page=25,to_page=25);single.save(source)
    units,plans=prepare_asset_document(source)
    translations={unit.unit_id:''.join(part if part.startswith('⟦P:') else '测'*max(1,round(len(part.strip())*.35)) if part.strip() else part for part in re.split(r'(⟦P:[^⟧]+⟧)',unit.source_text)) for unit in units if unit.unit_type!='protected'}
    compare=layout.inventories_equal;calls=0
    def fail_saved_inventory(expected,actual):
        nonlocal calls
        calls+=1
        return False if calls==2 else compare(expected,actual)
    monkeypatch.setattr(layout,'inventories_equal',fail_saved_inventory)
    output=tmp_path/'rejected.pdf'
    from pdf_translation.source_preserving import SourcePreservingUnsupported
    with pytest.raises(SourcePreservingUnsupported,match='saved PDF original glyph inventory'):
        render_asset_page(source,units,plans,translations,0,output,Path('C:/Windows/Fonts/simfang.ttf'))
    assert calls==2
    assert not output.exists()
    assert not list(tmp_path.glob('.native-pending-*'))


@pytest.mark.parametrize('page_number',[44,55,62,144])
def test_short_prose_and_tall_math_delimiters_preserve_saved_source(page_number,tmp_path):
    source=tmp_path/'source.pdf'
    with pymupdf.open('pdf/val.pdf') as original,pymupdf.open() as single:
        single.insert_pdf(original,from_page=page_number-1,to_page=page_number-1);single.save(source)
    units,plans=prepare_asset_document(source)
    translations={u.unit_id:''.join(s if s.startswith('⟦P:') else '测'*max(1,round(len(s.strip())*.35)) if s.strip() else s for s in re.split(r'(⟦P:[^⟧]+⟧)',u.source_text)) for u in units if u.unit_type!='protected'}
    proof=render_asset_page(source,units,plans,translations,0,tmp_path/'synthetic.pdf',Path('C:/Windows/Fonts/simfang.ttf'))
    assert proof['passed'] and proof['saved_source_glyph_inventory_verified']
    assert all(item['changed_pixels']==0 for item in proof['rigid_pixel_proof'])
