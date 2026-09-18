from pathlib import Path
import pymupdf
from pdf_translation.native_asset_plan import prepare_asset_document
from pdf_translation.native_asset_layout import render_asset_page


def test_mixed_diagram_row_keeps_both_native_neighbors(tmp_path):
    """A table row whose prose has native objects on either side of it.

    val.pdf page 94 lays out System | Property | Implementation. The Inverted
    Pendulum row puts a diagram's axis labels to the left of its quoted
    property and a block of code to the right, and the cell carries its own
    formula underneath. Leftover glyphs used to be chained by vertical overlap
    with no regard for where they sat across the page, so that chain hopped
    from column to column and never broke: all of it became one row reading
    '−π θ π ψB(=st)□: |Bθt| ≤ π/4 Bψ == @@ffoorrmmuullaa', two strings
    zippered together, and the row was left with one native neighbour where it
    has two.
    """
    source=tmp_path/'source.pdf'
    with pymupdf.open('pdf/val.pdf') as original,pymupdf.open() as single:
        single.insert_pdf(original,from_page=93,to_page=93);single.save(source)
    units,plans=prepare_asset_document(source)
    matches=[(u,p) for u,p in zip(units,plans) if 'Keep the pendulum balanced' in u.source_text]
    assert len(matches)==1
    unit,plan=matches[0]
    # The diagram's axis labels, the code column, and the cell's own formula.
    assert len(plan['assets'])==3
    assert all(a['placement']=='source_fixed' for a in plan['assets'].values())
    markers=list(plan['assets'])
    # The claim in the name: a native on each side of the prose, not only before
    # it. Counting assets alone would pass on three natives all stacked to one
    # side, which is exactly what the broken row produced.
    prose='‘‘Keep the pendulum balanced’’'
    quote=unit.source_text.index(prose)
    at=[unit.source_text.index(marker) for marker in markers]
    assert min(at)<quote<max(at)
    # Replacing the prose in place keeps every marker where the source put it:
    # the page has one prose interval, not one between every pair of natives.
    synthetic=unit.source_text.replace(prose,'测试文字')
    proof=render_asset_page(source,units,plans,{unit.unit_id:synthetic},0,tmp_path/'synthetic.pdf',Path('C:/Windows/Fonts/simfang.ttf'))
    assert proof['passed'] and proof['overlaid_source_pixels']==0
    assert proof['saved_source_glyph_inventory_verified']
    assert proof['asset_count']==0
