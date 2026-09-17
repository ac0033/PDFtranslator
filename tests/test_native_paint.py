from pathlib import Path
import pymupdf
import pytest
from pdf_translation.native_editor import inspect_native_page,glyph_inventory,trace_inventory,inventories_equal
from pdf_translation.native_paint import bind_nontext,rewrite_paint,operations

SOURCE=Path('pdf/cs224n-2019-notes03-neuralnets.pdf')


@pytest.mark.parametrize('page_index',[2,5,10,14])
def test_whole_original_page_translation_preserves_pixels_and_glyphs(page_index):
    if not SOURCE.exists():pytest.skip('integration PDF absent')
    with pymupdf.open(SOURCE) as source,pymupdf.open() as target:
        page=source[page_index];native=inspect_native_page(page)
        art=bind_nontext(page,native)
        target.insert_pdf(source,from_page=page_index,to_page=page_index)
        shifts={g.index:-10 for g in native.glyphs}
        target.update_stream(target[0].get_contents()[0],rewrite_paint(native,set(),shifts,[{**a,'dy':-10} for a in art]))
        with pymupdf.open(stream=target.tobytes(),filetype='pdf') as saved:
            expected={k:[(size,(x,y-10),(a,b-10,c,d-10)) for size,(x,y),(a,b,c,d) in values] for k,values in glyph_inventory(native.glyphs).items()}
            assert inventories_equal(expected,trace_inventory(saved[0]))
            a=page.get_pixmap(matrix=pymupdf.Matrix(2,2));b=saved[0].get_pixmap(matrix=pymupdf.Matrix(2,2))
            assert a.samples[a.stride*20:]==b.samples[:-b.stride*20]


def test_removal_and_movement_retain_advances_and_unselected_text():
    with pymupdf.open(SOURCE) as source,pymupdf.open() as target:
        n=inspect_native_page(source[2]);selected={g.index for g in n.glyphs if g.text.isalpha()}
        target.insert_pdf(source,from_page=2,to_page=2)
        target.update_stream(target[0].get_contents()[0],rewrite_paint(n,selected,{g.index:5 for g in n.glyphs},[]))
        with pymupdf.open(stream=target.tobytes(),filetype='pdf') as saved:
            expected={k:[(size,(x,y+5),(a,b+5,c,d+5)) for size,(x,y),(a,b,c,d) in v] for k,v in glyph_inventory([g for g in n.glyphs if g.index not in selected]).items()}
            assert inventories_equal(expected,trace_inventory(saved[0]))


def test_visible_paint_envelope_respects_nested_source_clips():
    import pymupdf
    from pdf_translation.native_editor import inspect_native_page
    from pdf_translation.native_paint import bind_nontext
    with pymupdf.open() as doc:
        page=doc.new_page(width=200,height=200)
        page.draw_line((0,0),(1,1))
        # Outer PDF clip [20,40,100,140]; inner clip [40,60,80,100].
        # The huge original path remains untouched in the content stream.
        stream=b'q 20 40 80 100 re W n q 40 60 40 40 re W n -1000 -1000 m 1000 1000 l S Q Q'
        doc.update_stream(page.get_contents()[0],stream)
        native=inspect_native_page(page)
        paints=bind_nontext(page,native)
        assert len(paints)==1 and paints[0]['visible']
        assert paints[0]['bbox']==(40,100,80,140)
        assert doc.xref_stream(page.get_contents()[0])==stream
