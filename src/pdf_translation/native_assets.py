"""Copy native PDF glyph assets with independently verified translation-only geometry.

This is an alternative layout representation, not a waiver of source fidelity.
Each asset retains encoded source glyphs; its only allowed transform is translation.
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import pymupdf
from .native_editor import inspect_native_page, remove_native_glyphs, glyph_inventory, trace_inventory, inventories_equal, strip_nontext_paint
from .source_preserving import SourcePreservingUnsupported


@dataclass
class NativeAsset:
    page_index: int
    source_page_index: int
    glyph_ids: tuple[int,...]
    clip: pymupdf.Rect
    baseline: float
    inventory: dict
    background_rgb: tuple[int,int,int]
    reference_pixels: tuple
    source_visible: bool = True
    paint_records: tuple = ()


class NativeAssetStore:
    def __init__(self, source: Path):
        self.source=pymupdf.open(source)
        self.document=pymupdf.open()
        self.natives={}
        self.assets={}
        self.backgrounds={}
        self.artwork={}

    def __enter__(self):return self

    def __exit__(self,*args):self.close()

    def close(self):
        if not self.document.is_closed:self.document.close()
        if not self.source.is_closed:self.source.close()

    def add(self, source_page_index, glyph_ids, baseline):
        key=(source_page_index,tuple(sorted(glyph_ids)))
        if key in self.assets:return self.assets[key]
        if source_page_index not in self.natives:
            self.natives[source_page_index]=inspect_native_page(self.source[source_page_index])
        native=self.natives[source_page_index]
        selected=set(glyph_ids)
        glyphs=[g for g in native.glyphs if g.index in selected]
        if len(glyphs)!=len(selected) or not glyphs:raise ValueError('asset glyph IDs must be a source subset')
        from .native_paint import bind_nontext,rewrite_paint
        from .native_composite_assets import fraction_paints,isolate_composite
        if source_page_index not in self.artwork:self.artwork[source_page_index]=bind_nontext(self.source[source_page_index],native)
        artwork=self.artwork[source_page_index]
        paints=fraction_paints(self.source[source_page_index],glyphs,artwork)
        paint_ends={a['end'] for a in paints}
        index=len(self.document)
        self.document.insert_pdf(self.source,from_page=source_page_index,to_page=source_page_index)
        group_type,group_value=self.source.xref_get_key(self.source[source_page_index].xref,'Group')
        if group_type=='xref':group_value=self.source.xref_object(int(group_value.split()[0]))
        if group_type!='null':self.document.xref_set_key(self.document[index].xref,'Group',group_value)
        self.document.update_stream(self.document[index].get_contents()[0],
            remove_native_glyphs(native,{g.index for g in native.glyphs}-selected))
        inventory=glyph_inventory(glyphs)
        if not inventories_equal(inventory,trace_inventory(self.document[index])):
            raise SourcePreservingUnsupported('asset changed source glyph content or geometry')
        # Renderer text bounds describe these actual glyphs, rather than the
        # font's maximum envelope (which can catch unrelated neighboring arrows).
        ink=[bbox for kind,bbox,*_ in self.document[index].get_bboxlog()
             if kind in ('fill-text','stroke-text') and bbox[0]<bbox[2] and bbox[1]<bbox[3]]
        if ink:
            clip=pymupdf.Rect(min(b[0] for b in ink)-.5,min(b[1] for b in ink)-.5,
                              max(b[2] for b in ink)+.5,max(b[3] for b in ink)+.5)
        else:
            clip=pymupdf.Rect(min(g.ink_bounds[0] for g in glyphs)-.5,min(g.ink_bounds[1] for g in glyphs)-.5,
                              max(g.ink_bounds[2] for g in glyphs)+.5,max(g.ink_bounds[3] for g in glyphs)+.5)
        for paint in paints:clip|=pymupdf.Rect(paint['bbox'])+(-.5,-.5,.5,.5)
        background_key=(source_page_index,tuple(sorted(paint_ends)))
        # Test actual artwork ink, not a path bounding box that may enclose whitespace.
        if background_key not in self.backgrounds:
            with pymupdf.open() as background:
                background.insert_pdf(self.source,from_page=source_page_index,to_page=source_page_index)
                background.update_stream(background[0].get_contents()[0],rewrite_paint(native,{g.index for g in native.glyphs},{},[{**a,'remove':a['end'] in paint_ends} for a in artwork]))
                self.backgrounds[background_key]=background.tobytes()
        with pymupdf.open(stream=self.backgrounds[background_key],filetype='pdf') as background:
            pixels=background[0].get_pixmap(matrix=pymupdf.Matrix(2,2),clip=clip).samples
            background_rgb=tuple(pixels[:3])
            if not pixels or any(pixels[i:i+3]!=pixels[:3] for i in range(0,len(pixels),3)):
                raise SourcePreservingUnsupported(f'inline glyph asset intersects nonuniform artwork: page={source_page_index+1}, glyphs={sorted(selected)}, clip={list(clip)}')
        # Image bounding boxes may include large blank margins. Uniform-background
        # verification above rejects actual picture ink, and strip_nontext_paint
        # below removes every image invocation from the isolated glyph asset.
        with pymupdf.open(stream=self.document.tobytes(),filetype='pdf') as frozen:
            reference=frozen[index].get_pixmap(matrix=pymupdf.Matrix(2,2),clip=clip)
            reference_pixels=(reference.width,reference.height,reference.n,reference.samples)
        xref=self.document[index].get_contents()[0]
        self.document.update_stream(xref,isolate_composite(native,selected,artwork,paint_ends))
        if not inventories_equal(inventory,trace_inventory(self.document[index])):
            raise SourcePreservingUnsupported('glyph-only extraction changed native glyphs')
        asset=NativeAsset(index,source_page_index,tuple(sorted(selected)),clip,float(baseline),inventory,background_rgb,reference_pixels,reference.samples!=pixels,tuple(paints))
        self.assets[key]=asset
        return asset

    def place(self, page, asset, dx, dy):
        if dx!=round(dx) or dy!=round(dy):raise ValueError('asset translation must use integer points for exact raster comparison')
        dest=asset.clip+(dx,dy,dx,dy)
        if not page.rect.contains(dest):raise SourcePreservingUnsupported('asset would be clipped by the output page')
        before=trace_inventory(page)
        backdrop=page.get_pixmap(matrix=pymupdf.Matrix(2,2),clip=dest)
        sprite=self.document[asset.page_index].get_pixmap(matrix=pymupdf.Matrix(2,2),clip=asset.clip)
        if (backdrop.width,backdrop.height,backdrop.n)!=(sprite.width,sprite.height,sprite.n):
            raise SourcePreservingUnsupported('asset collision masks have different pixel grids')
        a=backdrop.samples;b=sprite.samples
        collision=sum(a[i:i+3]!=bytes(asset.background_rgb) and b[i:i+3]!=b'\xff\xff\xff' for i in range(0,len(a),3))
        if collision:raise SourcePreservingUnsupported(f'asset overlaps {collision} existing ink pixels')
        page.show_pdf_page(dest,self.document,asset.page_index,clip=asset.clip,keep_proportion=True)
        # show_pdf_page recomputes a float scale from tiny clip rectangles. Force exact translation.
        wrapper=next(x[0] for x in reversed(page.get_xobjects()) if x[2]==0)
        pdf_dy=page.rect.height-self.document[asset.page_index].rect.height-dy
        page.parent.xref_set_key(wrapper,'Matrix',f'[1 0 0 1 {dx} {pdf_dy}]')
        expected={k:list(v) for k,v in before.items()}
        for key,values in asset.inventory.items():
            expected.setdefault(key,[]).extend((size,(o[0]+dx,o[1]+dy),(b[0]+dx,b[1]+dy,b[2]+dx,b[3]+dy)) for size,o,b in values)
        if not inventories_equal(expected,trace_inventory(page)):
            actual=trace_inventory(page)
            bad=[(k,expected.get(k),actual.get(k)) for k in expected.keys()|actual.keys() if not inventories_equal({k:expected[k]} if k in expected else {},{k:actual[k]} if k in actual else {})]
            raise SourcePreservingUnsupported(f'placed asset changed source glyph geometry: ids={asset.glyph_ids}, offset={(dx,dy)}, differences={bad[:3]}')
        return {'source_page':asset.source_page_index+1,'glyph_ids':list(asset.glyph_ids),'dx':dx,'dy':dy,
                'source_clip':list(asset.clip),'target_clip':list(dest),'glyph_geometry_verified':True,'source_paint_ends':[a['end'] for a in asset.paint_records]}

    def verify_isolated_pixels(self, asset, dx, dy):
        if not asset.source_visible:raise SourcePreservingUnsupported("fully occluded source glyphs must remain at their original position")
        # Isolated probe rules out raster changes hidden by other page content.
        with pymupdf.open() as probe:
            page=probe.new_page(width=max(612,asset.clip.x1+dx+10),height=max(792,asset.clip.y1+dy+10))
            group_type,group_value=self.source.xref_get_key(self.source[asset.source_page_index].xref,'Group')
            if group_type=='xref':group_value=self.source.xref_object(int(group_value.split()[0]))
            if group_type!='null':probe.xref_set_key(page.xref,'Group',group_value)
            if asset.background_rgb!=(255,255,255):page.draw_rect(page.rect,color=None,fill=tuple(v/255 for v in asset.background_rgb),overlay=False)
            evidence=self.place(page,asset,dx,dy)
            # Verify the serialized PDFs that readers actually receive, avoiding stale live-page rendering caches.
            with pymupdf.open(stream=self.document.tobytes(),filetype='pdf') as saved_source, pymupdf.open(stream=probe.tobytes(),filetype='pdf') as saved_target:
                a=saved_source[asset.page_index].get_pixmap(matrix=pymupdf.Matrix(2,2),clip=asset.clip)
                b=saved_target[0].get_pixmap(matrix=pymupdf.Matrix(2,2),clip=pymupdf.Rect(evidence['target_clip']))
            rw,rh,rn,reference=asset.reference_pixels
            if (rw,rh,rn)!=(b.width,b.height,b.n) or reference!=b.samples:
                raise SourcePreservingUnsupported(f'relocated source asset raster differs: glyphs={asset.glyph_ids}, clip={asset.clip}, offset={(dx,dy)}, sizes={(a.width,a.height,b.width,b.height)}, bytes={sum(x!=y for x,y in zip(a.samples,b.samples))}')
            return {**evidence,'raster_scale':2,'changed_pixels':0}
