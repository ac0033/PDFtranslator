from pathlib import Path
import pymupdf
import pytest
from pdf_translation.native_plan import native_page_plan


@pytest.mark.parametrize('page_index,expected',[(25,{'Agent','Environment','Policy','Transition Model','Observation Model','Sensor','action','state','observation'}),(62,{'K-S Statistic','model','data'}),(100,{'start'})])
def test_real_diagram_labels_have_independent_frames_and_do_not_take_math(page_index,expected):
    with pymupdf.open('pdf/val.pdf') as doc:
        native,rows=native_page_plan(doc[page_index])
        labels=[r for r in rows if r.get('figure_label')]
        assert {r['source_text'] for r in labels}==expected
        for row in labels:
            assert row['local_inline_frame']==row['bbox']
            assert row['bbox'][2]-row['bbox'][0]<100
            assert all(s['kind']=='prose' for s in row['slots'])
        ids=[gid for row in rows for s in row['slots'] for gid in s['glyph_ids']]
        assert len(ids)==len(set(ids))==len(native.glyphs)
        assert any(r['bbox'][2]-r['bbox'][0]>250 and any(s['kind']=='prose' for s in r['slots']) for r in rows if not r.get('figure_label'))


@pytest.mark.parametrize('page_index',[25,62,100,428])
def test_synthetic_label_layout_keeps_all_other_source_glyphs_and_pixels(page_index,tmp_path):
    from pdf_translation.native_asset_plan import prepare_asset_document
    from pdf_translation.native_asset_layout import render_asset_page
    source=tmp_path/'source.pdf'
    with pymupdf.open('pdf/val.pdf') as original,pymupdf.open() as single:
        single.insert_pdf(original,from_page=page_index,to_page=page_index);single.save(source)
    units,plans=prepare_asset_document(source)
    translations={u.unit_id:'测' for u,p in zip(units,plans) if p.get('figure_label')}
    # This is an intentionally synthetic layout test, never a translated deliverable.
    if page_index==428:
        assert not any('license' in u.source_text for u in units if u.unit_type!='protected')
        return
    assert translations
    proof=render_asset_page(source,units,plans,translations,0,tmp_path/'synthetic.pdf',Path('C:/Windows/Fonts/simfang.ttf'))
    assert proof['passed'] and proof['overlaid_source_pixels']==0
    assert proof['saved_source_glyph_inventory_verified']
    assert all(p['changed_pixels']==0 for p in proof['rigid_pixel_proof'])


def test_math_word_subscripts_stay_protected():
    with pymupdf.open('pdf/val.pdf') as doc:
        native,rows=native_page_plan(doc[62])
        scripts={g.index for g in native.glyphs if g.font=='TeXGyrePagella-Regular' and g.size<6.1 and g.origin[1]>210 and g.origin[1]<225}
        assert scripts
        prose={gid for r in rows for s in r['slots'] if s['kind']=='prose' for gid in s['glyph_ids']}
        assert not scripts&prose


def test_tightly_justified_cid_reference_keeps_spaces():
    with pymupdf.open('pdf/val.pdf') as doc:
        native,rows=native_page_plan(doc[428])
        reference=next(r['source_text'] for r in rows if r['source_text'].startswith('129.'))
        assert 'F. Liese and I. Vajda' in reference


def test_many_short_references_do_not_accumulate_font_ascent_offsets(tmp_path):
    import re
    from pdf_translation.native_asset_plan import prepare_asset_document
    from pdf_translation.native_asset_layout import render_asset_page
    source=tmp_path/'source.pdf'
    with pymupdf.open('pdf/val.pdf') as original,pymupdf.open() as single:
        single.insert_pdf(original,from_page=428,to_page=428);single.save(source)
    units,plans=prepare_asset_document(source);translations={}
    for unit in units:
        if unit.unit_type=='protected':continue
        parts=re.split(r'(⟦P:[^⟧]+⟧)',unit.source_text)
        translations[unit.unit_id]=''.join(p if p.startswith('⟦P:') else '测'*max(1,round(len(p.strip())*.35)) if p.strip() else p for p in parts)
    proof=render_asset_page(source,units,plans,translations,0,tmp_path/'synthetic.pdf',Path('C:/Windows/Fonts/simfang.ttf'),layout_only=True)
    assert proof['passed']
    # All reference entries keep their original line count in this fixture.
    # Font ascent differences must not create the former 17-point cumulative drift.
    groups=[g for g in proof['vertical_spacing'] if g['translated_units'] and not g['pinned']]
    assert len(groups)>=15 and max(g['dy'] for g in groups)<=1


def test_display_formula_fail_subscript_is_not_translated_as_prose():
    with pymupdf.open('pdf/val.pdf') as doc:
        native,rows=native_page_plan(doc[180])
        prose={gid for r in rows for s in r['slots'] if s['kind']=='prose' for gid in s['glyph_ids']}
        subscript={1771,1772,1773,1774}
        assert ''.join(native.glyphs[i].text for i in sorted(subscript))=='fail'
        assert not prose&subscript


def test_aircraft_equations_labels_and_accents_render_together(tmp_path):
    import re
    from pdf_translation.native_asset_plan import prepare_asset_document
    from pdf_translation.native_asset_layout import render_asset_page
    source=tmp_path/'source.pdf'
    with pymupdf.open('pdf/val.pdf') as original,pymupdf.open() as single:
        single.insert_pdf(original,from_page=380,to_page=380);single.save(source)
    units,plans=prepare_asset_document(source)
    equation_labels=[u.source_text for u,p in zip(units,plans) if p.get('equation_label')]
    assert equation_labels.count('if')==2
    assert 'otherwise' in equation_labels and 'no advisory' in equation_labels
    labels={u.source_text for u,p in zip(units,plans) if p.get('figure_label')}
    assert {'our aircraft','intruder'}<=labels
    translations={}
    for unit in units:
        if unit.unit_type=='protected':continue
        parts=re.split(r'(⟦P:[^⟧]+⟧)',unit.source_text)
        translations[unit.unit_id]=''.join(p if p.startswith('⟦P:') else '测'*max(1,round(len(p.strip())*.35)) if p.strip() else p for p in parts)
    proof=render_asset_page(source,units,plans,translations,0,tmp_path/'synthetic.pdf',Path('C:/Windows/Fonts/simfang.ttf'))
    assert proof['passed'] and proof['saved_source_glyph_inventory_verified']
    assert proof['overlaid_source_pixels']==0


@pytest.mark.parametrize('page_index,top,bottom',[(44,170,355),(47,238,266)])
def test_likelihood_derivation_operators_do_not_become_prose(page_index,top,bottom):
    with pymupdf.open('pdf/val.pdf') as doc:
        native,rows=native_page_plan(doc[page_index])
        prose={gid for r in rows for slot in r['slots'] if slot['kind']=='prose' for gid in slot['glyph_ids']}
        formula={g.index for g in native.glyphs if g.bbox[1]>top and g.bbox[3]<bottom and g.origin[0]<375}
        assert len(formula)>30 and not formula&prose


def test_regular_word_value_beside_digit_is_translated_but_digit_stays_native():
    with pymupdf.open('pdf/val.pdf') as doc:
        native,rows=native_page_plan(doc[46])
        row=next(r for r in rows if 'Suppose we have a binary' in r['source_text'])
        prose=[s['text'] for s in row['slots'] if s['kind']=='prose']
        protected=[s for s in row['slots'] if s['kind']=='protected']
        assert any('value' in text for text in prose)
        assert not any('value' in s['text'] for s in protected)
        numeric={g.index for g in native.glyphs if g.text=='1' and row['bbox'][0]<=g.origin[0]<=row['bbox'][2] and abs(g.origin[1]-row['baseline'])<1}
        assert numeric and numeric<={gid for s in protected for gid in s['glyph_ids']}


@pytest.mark.parametrize('page_number,expected',[(136,{'average dispersion'}),(138,{'upper','lower'})])
def test_words_before_equation_equals_are_independent_labels(page_number,expected):
    with pymupdf.open('pdf/val.pdf') as doc:
        native,rows=native_page_plan(doc[page_number-1])
    labels=[r for r in rows if r.get('equation_label') and r['source_text'] in expected]
    assert {r['source_text'] for r in labels}==expected
    for row in labels:
        assert all(s['kind']=='prose' for s in row['slots'])
        assert all(native.glyphs[i].text!='=' for s in row['slots'] for i in s['glyph_ids'])
    ids=[i for r in rows for s in r['slots'] for i in s['glyph_ids']]
    assert len(ids)==len(set(ids))==len(native.glyphs)
