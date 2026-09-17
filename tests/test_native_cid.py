from pathlib import Path
import pymupdf
import pytest
from pdf_translation.native_editor import inspect_native_page,remove_native_glyphs,trace_inventory,glyph_inventory,inventories_equal
from pdf_translation.native_paint import rewrite_paint,bind_nontext
from pdf_translation.native_columns import infer_columns

SOURCE=Path('pdf/val.pdf')


@pytest.mark.parametrize('page_index',[0,3,16,49,100,300])
def test_real_cid_removal_preserves_every_unselected_glyph(page_index):
    if not SOURCE.exists():pytest.skip('val fixture absent')
    with pymupdf.open(SOURCE) as source,pymupdf.open() as target:
        native=inspect_native_page(source[page_index]);selected={g.index for g in native.glyphs if g.direction==(1.,0.) and g.text.isalpha() and g.index%3==0}
        assert selected and any(g.code_bytes==2 for g in native.glyphs)
        target.insert_pdf(source,from_page=page_index,to_page=page_index)
        target.update_stream(target[0].get_contents()[0],remove_native_glyphs(native,selected))
        with pymupdf.open(stream=target.tobytes(),filetype='pdf') as saved:
            assert inventories_equal(glyph_inventory([g for g in native.glyphs if g.index not in selected]),trace_inventory(saved[0]))


def test_ligature_is_one_encoded_glyph_with_complete_unicode_text():
    with pymupdf.open(SOURCE) as doc:
        native=inspect_native_page(doc[3]);ligatures=[g for g in native.glyphs if g.text=='fi']
        assert ligatures and all(g.code_bytes==2 for g in ligatures)
        assert inventories_equal(glyph_inventory(native.glyphs),trace_inventory(doc[3]))


def test_mirrored_columns_and_footer_are_measured_from_source():
    with pymupdf.open(SOURCE) as doc:
        odd=infer_columns(doc[20]);even=infer_columns(doc[21])
        assert 58<odd['body'][0]<61 and 34<even['body'][0]<37
        assert 419<odd['margin'][0]<422 and 395<even['margin'][0]<398
        assert 590<odd['footer_top']<600 and odd['body'][1]<odd['margin'][0]


@pytest.mark.parametrize('page_index',[3,41,49,62,63,89,91,100,105,311])
def test_cid_page_and_clipped_vectors_survive_integer_translation(page_index):
    with pymupdf.open(SOURCE) as source,pymupdf.open() as target:
        page=source[page_index];native=inspect_native_page(page);art=bind_nontext(page,native)
        target.insert_pdf(source,from_page=page_index,to_page=page_index)
        target.update_stream(target[0].get_contents()[0],rewrite_paint(native,set(),{g.index:-5 for g in native.glyphs},[{**a,'dy':-5} for a in art]))
        with pymupdf.open(stream=target.tobytes(),filetype='pdf') as saved:
            a=page.get_pixmap(matrix=pymupdf.Matrix(2,2));b=saved[0].get_pixmap(matrix=pymupdf.Matrix(2,2))
            assert a.samples[a.stride*10:]==b.samples[:-b.stride*10]


def test_small_body_font_does_not_turn_footer_into_zero_width_margin():
    from pdf_translation.native_columns import glyph_column
    with pymupdf.open(SOURCE) as doc:
        page=doc[428];geometry=infer_columns(page);native=inspect_native_page(page)
        assert 590<geometry['footer_top']<600
        footer=[g for g in native.glyphs if 596<g.origin[1]<607]
        assert footer and all(glyph_column(g,geometry)=='header' for g in footer)
        assert geometry['margin'][0]==geometry['margin'][1]
        from dataclasses import replace
        right=replace(footer[0],origin=(550.,300.))
        assert glyph_column(right,geometry)=='body'
