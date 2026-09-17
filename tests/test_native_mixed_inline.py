from pathlib import Path
import pymupdf
from pdf_translation.native_asset_plan import prepare_asset_document
from pdf_translation.native_asset_layout import render_asset_page


def test_mixed_diagram_row_keeps_both_native_neighbors(tmp_path):
    source=tmp_path/'source.pdf'
    with pymupdf.open('pdf/val.pdf') as original,pymupdf.open() as single:
        single.insert_pdf(original,from_page=93,to_page=93);single.save(source)
    units,plans=prepare_asset_document(source)
    matches=[(u,p) for u,p in zip(units,plans) if 'Keep the pendulum balanced' in u.source_text]
    assert len(matches)==1
    unit,plan=matches[0]
    assert len(plan['assets'])==2
    assert all(a['placement']=='source_fixed' for a in plan['assets'].values())
    markers=list(plan['assets'])
    synthetic=f'{markers[0]} 测试文字 {markers[1]}'
    proof=render_asset_page(source,units,plans,{unit.unit_id:synthetic},0,tmp_path/'synthetic.pdf',Path('C:/Windows/Fonts/simfang.ttf'))
    assert proof['passed'] and proof['overlaid_source_pixels']==0
    assert proof['saved_source_glyph_inventory_verified']
    assert proof['asset_count']==0
