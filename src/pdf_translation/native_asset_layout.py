"""Render translated paragraphs around verified original glyph assets."""
from __future__ import annotations
from pathlib import Path
import re
import math
import os
import tempfile
import pymupdf
from .native_assets import NativeAssetStore
from .native_layout_geometry import horizontal_ink_frame,rightward_integer_shift
from .native_fixed_flow import expand_fixed_flow
from .native_page_flow import expand_prose_pages
from .native_rigid_flow import plan_rigid_flow
from .native_paint import rewrite_paint
from .native_editor import inspect_native_page,remove_native_glyphs,trace_inventory,inventories_equal,glyph_inventory
from .source_preserving import SourcePreservingUnsupported


def render_asset_page(source, units, plans, translations, page_index, output, font_path, *, layout_only=False):
    with NativeAssetStore(Path(source)) as store:
        return _render_asset_page(source,units,plans,translations,page_index,output,font_path,store=store,layout_only=layout_only)


def _render_asset_page(source, units, plans, translations, page_index, output, font_path, *, store, layout_only=False):
    if Path(output).exists():raise FileExistsError(output)
    units,plans,translations,fixed_flow=expand_fixed_flow(units,plans,translations)
    units,plans,translations,page_flow=expand_prose_pages(units,plans,translations,font_path,source=source)
    font=pymupdf.Font(fontfile=str(font_path))
    native=inspect_native_page(store.source[page_index])
    retained_occluded=[]
    spacing_evidence=[];spacing_issues=[]
    selected=set();texts=[];asset_positions=[];overflows=[];coverage=[];unit_bounds=[]
    for unit,plan in zip(units,plans):
        if unit.unit_id not in translations or page_index+1 not in plan['source_pages']:continue
        if any(a.get('placement')=='source_fixed' for a in plan['assets'].values()):raise SourcePreservingUnsupported('static display/code anchor flow is not yet implemented')
        if plan['source_pages']!=[page_index+1]:raise SourcePreservingUnsupported('cross-page asset paragraphs require page-spanning flow')
        for chunk in plan['chunks']:
            for slot in chunk['slots']:selected.update(slot['glyph_ids'])
        # Preserve source figures in place. Inline assets cannot duplicate existing vector ink.
        box=pymupdf.Rect(min(r['bbox'][0] for r in plan['rows']),min(r['bbox'][1] for r in plan['rows']),
                         max(r['bbox'][2] for r in plan['rows']),max(r['bbox'][3] for r in plan['rows']))
        # Original drawings remain in the background; asset creation and ink gates reject actual intersections.
        translated=translations[unit.unit_id]
        markers=re.findall(r'⟦P:[^⟧]+⟧',translated)
        if sorted(markers)!=sorted(plan['assets']):raise ValueError('translated asset coverage is not exact')
        xleft=84 if re.match(r'^⟦P:',unit.source_text) and re.match(r'^\d+\.',plan['rows'][0]['source_text']) else (72 if plan['column'] in ('body','header') else 406.74)
        if plan['rows'][0].get('page_geometry',{}).get('profile')=='measured_source_lines':
            xleft=plan['rows'][0]['column_bounds'][0]
        if plan.get('code_context'):xleft=box.x0
        text_start=len(texts);asset_start=len(asset_positions)
        x=xleft-12 if xleft==84 else xleft
        if plan['rows'][0].get('page_geometry',{}).get('profile')=='measured_source_lines' and re.match(r'^\d+\.',plan['rows'][0]['source_text']):
            x=min(x,box.x0)  # Preserve the number's hanging position before the text frame.
        baseline=plan['rows'][0]['baseline'];start=baseline
        limit=plan['rows'][0].get('column_bounds',[0,382.8 if plan['column']=='body' else 550])[1]
        if plan.get('local_inline_frame'):
            xleft=plan['local_inline_frame'][0];x=xleft;limit=plan['local_inline_frame'][2]
        size=min(s['source_font_size'] for c in plan['chunks'] if c['kind']=='prose' for s in c['slots'])
        if plan.get('figure_label') or plan.get('equation_label'):
            required=font.text_length(translated,fontsize=size)
            fitted=min(size,size*(limit-xleft)/required) if required else size
            if fitted<size*.8:raise SourcePreservingUnsupported(f'figure label exceeds original frame at readable font size: unit={unit.unit_id}')
            size=fitted
            x+=(limit-xleft-font.text_length(translated,fontsize=size))/2
        if plan.get('local_inline_frame') and font.text_length(translated,fontsize=size)>limit-xleft+.001:
            raise SourcePreservingUnsupported('inline connector translation does not fit its original frame')
        base_line_height=plan.get('flow_line_height',max(14,size*1.5));line_height=base_line_height;top=baseline-font.ascender*size;bottom=baseline-font.descender*size
        if plan['rows'][0].get('page_geometry',{}).get('profile')=='measured_source_lines':
            from statistics import median
            source_bases=sorted({r['baseline'] for r in plan['rows']})
            gaps=[b-a for a,b in zip(source_bases,source_bases[1:]) if size<=b-a<=size*1.8]
            base_line_height=median(gaps) if gaps else size*1.3
            line_height=base_line_height
        tokens=re.findall(r'⟦P:[^⟧]+⟧|——|……|[A-Za-z][A-Za-z0-9_-]*|.',translated)
        for ti,token in enumerate(tokens):
            reserve=0
            for following in tokens[ti+1:]:
                literal=unit.protected_literal_hints.get(following,following).strip()
                if not literal or not all(c in '，。；：！？、）”’》】.,;:!?)]' for c in literal):break
                if following in plan['assets']:
                    gs=[native.glyphs[i] for i in plan['assets'][following]['glyph_ids']]
                    reserve+=max(max(g.origin[0]+g.advance*g.size/1000,g.ink_bounds[2]) for g in gs)-min(min(g.origin[0],g.ink_bounds[0]) for g in gs)+2
                else:reserve+=font.text_length(following,fontsize=size)
            if token in '（([“‘《【' and ti+1<len(tokens):
                following=tokens[ti+1]
                if following in plan['assets']:
                    ids=plan['assets'][following]['glyph_ids'];gs=[native.glyphs[i] for i in ids]
                    reserve=max(max(g.origin[0]+g.advance*g.size/1000,g.ink_bounds[2]) for g in gs)-min(min(g.origin[0],g.ink_bounds[0]) for g in gs)+2
                else:reserve=font.text_length(following,fontsize=size)
            if token in plan['assets']:
                info=plan['assets'][token]
                asset=store.add(**{'source_page_index':info['page_index'],'glyph_ids':info['glyph_ids'],'baseline':info['baseline']})
                if not asset.source_visible:
                    selected.difference_update(info['glyph_ids'])
                    retained_occluded.append({'glyph_ids':info['glyph_ids'],'reason':'original composite has no visible glyph pixels; retain source position and occlusion'})
                    continue
                gs=[native.glyphs[i] for i in info['glyph_ids']]
                left,right=horizontal_ink_frame(gs)
                width=right-left+2
                height=max(g.bbox[3] for g in gs)-min(g.bbox[1] for g in gs)+2
                if width>limit-xleft+.001:raise SourcePreservingUnsupported(f'unbreakable token exceeds source column width: unit={unit.unit_id}, width={width}, available={limit-xleft}, size={size}')
                if x+width+reserve>limit and x>xleft:baseline+=line_height;x=xleft;line_height=base_line_height
                dx=rightward_integer_shift(x,left);dy=round(baseline-info['baseline'])
                asset_positions.append((asset,dx,dy))
                coverage.extend(info['glyph_ids'])
                top=min(top,min(g.bbox[1]+dy for g in gs));bottom=max(bottom,max(g.bbox[3]+dy for g in gs))
                x=left+dx+width;line_height=max(line_height,height)
            else:
                width=font.text_length(token,fontsize=size)
                if width>limit-xleft+.001:raise SourcePreservingUnsupported(f'unbreakable token exceeds source column width: unit={unit.unit_id}, width={width}, available={limit-xleft}, size={size}')
                if x+width+reserve>limit and x>xleft:baseline+=line_height;x=xleft;line_height=base_line_height
                if not token.isspace():texts.append({'text':token,'origin':(x,baseline),'size':size,'unit_id':unit.unit_id})
                x+=width
                top=min(top,baseline-font.ascender*size);bottom=max(bottom,baseline-font.descender*size)
        if len(texts)==text_start and len(asset_positions)==asset_start:top=bottom=baseline
        unit_bounds.append({'unit_id':unit.unit_id,'source_bbox':list(box),'flow_top':box.y0,'top':top,'bottom':bottom,'left':xleft,'right':limit,'column':plan['column'],'container_bbox':plan['rows'][0].get('container_bbox'),'last_baseline':max(r['baseline'] for r in plan['rows']),'fixed_inline':bool(plan.get('local_inline_frame')),'text_range':(text_start,len(texts)),'asset_range':(asset_start,len(asset_positions))})
    paint_ends=[p['end'] for asset,_,_ in asset_positions for p in asset.paint_records]
    if len(paint_ends)!=len(set(paint_ends)):raise SourcePreservingUnsupported('source fraction rule belongs to multiple assets')
    unit_shifts,glyph_shifts,artwork,spacing_evidence=plan_rigid_flow(store.source[page_index],native,selected,unit_bounds,paint_ends)
    for i,bounds in enumerate(unit_bounds):
        shift=unit_shifts[i];bounds['top']+=shift;bounds['bottom']+=shift
        for j in range(*bounds['text_range']):
            x,y=texts[j]['origin'];texts[j]['origin']=(x,y+shift)
        for j in range(*bounds['asset_range']):
            asset,dx,dy=asset_positions[j];asset_positions[j]=(asset,dx,dy+shift)
    ordered=unit_bounds
    for bounds in ordered:
        frame=pymupdf.Rect(bounds['left'],bounds['top'],bounds['right'],bounds['bottom'])
        if not store.source[page_index].rect.contains(frame):
            overflows.append({'unit_id':bounds['unit_id'],'outside_page':True})
        collisions=[g.index for g in native.glyphs if g.index not in selected and not g.text.isspace() and (pymupdf.Rect(g.bbox)+(0,glyph_shifts.get(g.index,0),0,glyph_shifts.get(g.index,0))).intersects(frame)]
        if collisions:overflows.append({'unit_id':bounds['unit_id'],'intersects_retained_glyphs':collisions})
    if overflows or spacing_issues:
        store.close();return {'passed':False,'overflows':overflows,'spacing_issues':spacing_issues,'vertical_spacing':spacing_evidence,'translation_complete':False,'formal_use_ready':False}
    if layout_only:
        store.close();return {'passed':True,'vertical_spacing':spacing_evidence,'layout_only':True,'translation_complete':False,'formal_use_ready':False}
    if len(coverage)!=len(set(coverage)):raise ValueError('source glyph asset used more than once')
    # Every original numeric glyph selected for translation must survive in an asset.
    missing=[g.index for g in native.glyphs if g.index in selected and g.text.isdigit() and g.index not in coverage]
    if missing:raise ValueError(f'original numeric glyphs missing: {missing}')
    with pymupdf.open() as target:
        target.insert_pdf(store.source,from_page=page_index,to_page=page_index)
        target.update_stream(target[0].get_contents()[0],rewrite_paint(native,selected,glyph_shifts,artwork))
        page=target.reload_page(target[0])
        retained_inventory=_shifted_inventory(native,selected,glyph_shifts)
        if not inventories_equal(retained_inventory,trace_inventory(page)):
            raise SourcePreservingUnsupported('paragraph removal changed retained source glyphs')
        from .native_container_flow import verify_container_drawings
        container_proof,rigid_target=verify_container_drawings(store.source[page_index],page,artwork)
        rigid_pixel_proof=_verify_rigid_pixels(store.source,page,page_index,native,selected,spacing_evidence,artwork,rigid_target)
        evidence=[]
        for asset,dx,dy in asset_positions:
            evidence.append(store.verify_isolated_pixels(asset,dx,dy))
            store.place(page,asset,dx,dy)
        with pymupdf.open(stream=target.tobytes(),filetype='pdf') as frozen:
            before=frozen[0].get_pixmap(matrix=pymupdf.Matrix(2,2),alpha=False)
        for item in texts:page.insert_text(item['origin'],item['text'],fontsize=item['size'],fontname='NativeZH',fontfile=str(font_path))
        with pymupdf.open(stream=target.tobytes(),filetype='pdf') as frozen:
            after=frozen[0].get_pixmap(matrix=pymupdf.Matrix(2,2),alpha=False)
        with pymupdf.open() as bg:
            bg.insert_pdf(store.source,from_page=page_index,to_page=page_index)
            bg.update_stream(bg[0].get_contents()[0],rewrite_paint(native,{g.index for g in native.glyphs},{},artwork))
            with pymupdf.open(stream=bg.tobytes(),filetype='pdf') as frozen:
                background=frozen[0].get_pixmap(matrix=pymupdf.Matrix(2,2)).samples
                for item in texts:
                    x,y=item['origin'];size=item['size']
                    rect=pymupdf.Rect(x,y-font.ascender*size,x+font.text_length(item['text'],fontsize=size),y-font.descender*size)
                    pixels=frozen[0].get_pixmap(matrix=pymupdf.Matrix(2,2),clip=rect).samples
                    if not pixels or min(pixels[:3])<128 or any(pixels[i:i+3]!=pixels[:3] for i in range(0,len(pixels),3)):
                        raise SourcePreservingUnsupported('Chinese text intersects nonuniform or dark source artwork')
        a=before.samples;b=after.samples
        changed=sum(a[i:i+3]!=b[i:i+3] for i in range(0,len(a),3) if a[i:i+3]!=background[i:i+3])
        if changed:
            locations=[((i//3)%before.width/2,(i//3)//before.width/2) for i in range(0,len(a),3) if a[i:i+3]!=b[i:i+3] and a[i:i+3]!=background[i:i+3]][:10]
            nearby=[item for item in texts if any(abs(item['origin'][0]-x)<30 and abs(item['origin'][1]-y)<20 for x,y in locations)]
            raise SourcePreservingUnsupported(f'Chinese overlaid {changed} original source ink pixels at {locations}; nearby text {nearby}')
        Path(output).parent.mkdir(parents=True,exist_ok=True)
        fd,staging_name=tempfile.mkstemp(prefix='.native-pending-',suffix='.pdf',dir=Path(output).parent)
        os.close(fd);staging=Path(staging_name)
        try:
            target.save(staging,deflate=True,garbage=4)
            expected=_shifted_inventory(native,selected,glyph_shifts)
            for asset,dx,dy in asset_positions:
                for key,values in asset.inventory.items():
                    expected.setdefault(key,[]).extend((size,(o[0]+dx,o[1]+dy),(b[0]+dx,b[1]+dy,b[2]+dx,b[3]+dy)) for size,o,b in values)
            with pymupdf.open(staging) as saved:
                actual=trace_inventory(saved[0]);source_fonts={g.font for g in native.glyphs}
                actual_source={k:v for k,v in actual.items() if k[0] in source_fonts}
                if not inventories_equal(expected,actual_source):
                    raise SourcePreservingUnsupported('saved PDF original glyph inventory differs from declared placements')
                saved[0].get_pixmap(matrix=pymupdf.Matrix(2,2)).save(str(Path(output).with_suffix('.png')))
            # A hard link publishes the already-verified bytes atomically and
            # refuses to overwrite a concurrent or previously accepted result.
            os.link(staging,output)
        finally:
            staging.unlink(missing_ok=True)
    store.close()
    return {'passed':True,'source_page':page_index+1,'asset_count':len(evidence),'assets':evidence,
            'vertical_spacing':spacing_evidence,'rigid_pixel_proof':rigid_pixel_proof,'container_proof':container_proof,'glyph_shifts':glyph_shifts,'artwork_shifts':artwork,'fixed_flow':fixed_flow,'page_flow':page_flow,'retained_occluded_assets':retained_occluded,'overlaid_source_pixels':0,'saved_source_glyph_inventory_verified':True,'copied_glyph_ids':sorted(coverage),'removed_glyph_ids':sorted(selected),'numeric_source_coverage':True,'translation_complete':False,'formal_use_ready':False}


def _shifted_inventory(native,selected,shifts):
    from dataclasses import replace
    moved=[]
    for g in native.glyphs:
        if g.index in selected:continue
        dy=shifts.get(g.index,0)
        moved.append(replace(g,origin=(g.origin[0],g.origin[1]+dy),bbox=(g.bbox[0],g.bbox[1]+dy,g.bbox[2],g.bbox[3]+dy)))
    return glyph_inventory(moved)


def _verify_rigid_pixels(source,target_page,page_index,native,selected,groups,artwork,rigid_target=None):
    proofs=[]
    with pymupdf.open() as original:
        original.insert_pdf(source,from_page=page_index,to_page=page_index)
        original.update_stream(original[0].get_contents()[0],rewrite_paint(native,selected,{},[{**a,'dy':0,'remove':a.get('remove',False) or bool(a.get('container_resize'))} for a in artwork]))
        with pymupdf.open(stream=original.tobytes(),filetype='pdf') as before,pymupdf.open(stream=rigid_target or target_page.parent.tobytes(),filetype='pdf') as after:
            for group in groups:
                if group['immutable_bbox'] is None:continue
                clip=pymupdf.Rect(group['immutable_bbox'])+(-.5,-.5,.5,.5)
                dy=group['dy'];dest=clip+(0,dy,0,dy)
                a=before[0].get_pixmap(matrix=pymupdf.Matrix(2,2),clip=clip)
                b=after[0].get_pixmap(matrix=pymupdf.Matrix(2,2),clip=dest)
                if (a.width,a.height,a.n)!=(b.width,b.height,b.n) or a.samples!=b.samples:
                    changed=sum(x!=y for x,y in zip(a.samples,b.samples))
                    raise SourcePreservingUnsupported(f'rigid source group {group["group"]} changed pixels: {changed}')
                proofs.append({'group':group['group'],'source_clip':list(clip),'target_clip':list(dest),'changed_pixels':0})
    return proofs
