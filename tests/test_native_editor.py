from pathlib import Path
import pymupdf
from io import BytesIO
import pytest
from babeldoc.pdfminer.psparser import PSBaseParser, PSEOF
from pdf_translation.native_editor import NativeGlyph, NativePage, remove_native_glyphs


def test_removed_width_keeps_adjacent_kerning_number_separate():
    stream=b"[40(ab)20]TJ"
    glyphs=[NativeGlyph(i,c,"Test",10,(0,0),(0,0,1,1),(0,0,1,1),ord(c),500,2) for i,c in enumerate("ab")]
    native=NativePage(stream,[(0,None),(1,40),(3,b"ab"),(7,20),(9,None)],glyphs,{2:glyphs},1)
    edited=remove_native_glyphs(native,{0})
    parser=PSBaseParser(BytesIO(edited));values=[]
    try:
        while True:values.append(parser.nexttoken()[1])
    except PSEOF:pass
    assert values[1:5]==[40,-500,b"b",20]


def test_invalid_selection_is_rejected():
    native=NativePage(b"",[],[],{},1)
    with pytest.raises(ValueError):remove_native_glyphs(native,{123})


def test_native_number_relocation_preserves_pixels_and_geometry():
    from pathlib import Path
    from pdf_translation.native_assets import NativeAssetStore
    source=Path('pdf/cs224n-2019-notes03-neuralnets.pdf')
    if not source.exists():pytest.skip('public Stanford integration fixture not present')
    store=NativeAssetStore(source)
    try:
        asset=store.add(5,[606],275.68701171875)
        proof=store.verify_isolated_pixels(asset,-115,-14)
        assert proof['changed_pixels']==0
        assert proof['glyph_geometry_verified'] is True
        with pytest.raises(ValueError):
            store.verify_isolated_pixels(asset,-115.1,-14)
    finally:store.close()


def test_inline_error_relation_has_a_language_boundary():
    from pathlib import Path
    from pdf_translation.native_asset_plan import prepare_asset_document
    source=Path('pdf/cs224n-2019-notes03-neuralnets.pdf')
    if not source.exists():pytest.skip('public Stanford integration fixture not present')
    units,plans=prepare_asset_document(source)
    unit=next(u for u in units if 'This amount is the (error signal at' in u.source_text)
    literals=list(unit.protected_literal_hints.values())
    assert 'a^{(2)}_{1}' in literals
    assert not any(x.startswith('a^{(2)}_{1}=') for x in literals)
    assert any(x.startswith('=') for x in literals)


def test_native_asset_pixel_gate_rejects_shifted_output(monkeypatch):
    from pathlib import Path
    from pdf_translation.native_assets import NativeAssetStore
    from pdf_translation.source_preserving import SourcePreservingUnsupported
    source=Path('pdf/cs224n-2019-notes03-neuralnets.pdf')
    if not source.exists():pytest.skip('public Stanford integration fixture not present')
    store=NativeAssetStore(source)
    try:
        asset=store.add(5,[606],275.68701171875)
        real_place=store.place
        def corrupt(page,asset,dx,dy):
            evidence=real_place(page,asset,dx,dy)
            wrapper=next(x[0] for x in reversed(page.get_xobjects()) if x[2]==0)
            page.parent.xref_set_key(wrapper,'Matrix',f'[1 0 0 1 {dx+1} {-dy}]')
            return evidence
        monkeypatch.setattr(store,'place',corrupt)
        with pytest.raises(SourcePreservingUnsupported,match='raster differs'):
            store.verify_isolated_pixels(asset,-115,-14)
    finally:store.close()


def test_occluded_page_number_cannot_be_relocated():
    from pdf_translation.native_assets import NativeAssetStore
    source=Path('pdf/cs224n-2019-notes03-neuralnets.pdf')
    if not source.exists():pytest.skip('public Stanford fixture absent')
    store=NativeAssetStore(source)
    try:
        asset=store.add(13,[98,99],50)
        assert any(pymupdf.Rect(i['bbox']).intersects(asset.clip) for i in store.source[13].get_image_info())
        assert store.document[asset.page_index].get_image_info()==[]
        from pdf_translation.source_preserving import SourcePreservingUnsupported
        assert asset.source_visible is False
        with pytest.raises(SourcePreservingUnsupported,match='fully occluded'):
            store.verify_isolated_pixels(asset,-150,0)
    finally:store.close()


def test_glyph_clip_that_contains_actual_plot_ink_is_rejected(tmp_path):
    from pdf_translation.native_assets import NativeAssetStore
    from pdf_translation.native_editor import inspect_native_page
    from pdf_translation.source_preserving import SourcePreservingUnsupported
    import pymupdf
    source=Path('pdf/cs224n-2019-notes03-neuralnets.pdf')
    if not source.exists():pytest.skip('public Stanford fixture absent')
    fixture=tmp_path/'actual-overlap.pdf'
    with pymupdf.open(source) as original,pymupdf.open() as document:
        document.insert_pdf(original,from_page=13,to_page=13)
        glyph=inspect_native_page(document[0]).glyphs[98]
        box=pymupdf.Rect(glyph.bbox);x=(box.x0+box.x1)/2
        document[0].draw_line((x,box.y0),(x,box.y1),color=(0,0,0),width=.5)
        contents=document[0].get_contents();stream=b'\n'.join(document.xref_stream(ref) for ref in contents)
        document.update_stream(contents[0],stream);document[0].set_contents(contents[0]);document.save(fixture)
    with NativeAssetStore(fixture) as store:
        glyph=inspect_native_page(store.source[0]).glyphs[98]
        with pytest.raises(SourcePreservingUnsupported,match='nonuniform artwork'):
            store.add(0,[98],glyph.origin[1])

