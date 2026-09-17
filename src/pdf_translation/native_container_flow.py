"""Reflow recognized prose containers without stretching their contents.

Only an opaque, light, axis-aligned, single filled rectangle is resizable.
All other drawings keep the ordinary rigid-object rules.
"""
from .native_paint import operations
from .source_preserving import SourcePreservingUnsupported


def contains(a,b,tolerance=.01):
    return a[0]-tolerance<=b[0] and a[1]-tolerance<=b[1] and a[2]+tolerance>=b[2] and a[3]+tolerance>=b[3]


def container_groups(page,native,items,unit_bounds,artwork,layout):
    boxes={tuple(b['container_bbox']) for b in unit_bounds if b.get('container_bbox')}
    if not boxes:return items,[]
    if page.rotation or page.cropbox_position!=(0,0):
        raise SourcePreservingUnsupported('container resizing requires an unrotated, uncropped page')
    draws={d['seqno']:d for d in page.get_drawings()};ops=operations(native)
    used=set();containers=[];parents=[]
    for box in sorted(boxes,key=lambda b:(b[1],b[0])):
        matches=[(i,a) for i,a in enumerate(artwork) if not a.get('remove') and len(a['seqnos'])==1 and a['seqnos'][0] in draws and max(abs(x-y) for x,y in zip(draws[a['seqnos'][0]]['rect'],box))<.01]
        if len(matches)!=1:raise SourcePreservingUnsupported('prose container does not bind to one source rectangle')
        art_index,art=matches[0];draw=draws[art['seqnos'][0]]
        path=[o for o in ops if art['start']<=o.start and o.end<=art['end']]
        a,b,c,d,_,_=art['ctm']
        if (draw['type']!='f' or len(draw['items'])!=1 or draw['items'][0][0]!='re' or not draw.get('fill') or min(draw['fill'])<.8 or draw.get('fill_opacity',1)!=1 or abs(b)>1e-9 or abs(c)>1e-9 or abs(a*d)<1e-12 or [o.name for o in path]!=[b're',b'f']):
            raise SourcePreservingUnsupported('prose container requires a plain opaque rectangle')
        background=[i for i,item in enumerate(items) if item['kind']=='artwork' and item['index']==art_index]
        if len(background)!=1:raise SourcePreservingUnsupported('prose container background ownership is ambiguous')
        # Horizontal frame rules sit just outside the fill. Bind those by
        # their actual line geometry, not by widening every child's bounds.
        border_indices=set()
        for i,item in enumerate(items):
            if item['kind']!='artwork':continue
            candidate=artwork[item['index']]
            if len(candidate['seqnos'])!=1:continue
            line=draws.get(candidate['seqnos'][0])
            if not line or line['type']!='s' or len(line['items'])!=1 or line['items'][0][0]!='l':continue
            r=line['rect']
            if abs(r.x0-box[0])<.01 and abs(r.x1-box[2])<.01 and r.height<.01 and min(abs(r.y0-box[1]),abs(r.y0-box[3]))<.5:
                border_indices.add(i)
        children=[i for i,item in enumerate(items) if i!=background[0] and (i in border_indices or contains(box,item['source'],.6))]
        if not children or any(i in used for i in children+background):
            raise SourcePreservingUnsupported('nested or overlapping prose containers require explicit ownership')
        for i,item in enumerate(items):
            if i in children or i in background:continue
            r=item['source']
            if min(box[2],r[2])-max(box[0],r[0])>.6 and min(box[3],r[3])-max(box[1],r[1])>.6:
                raise SourcePreservingUnsupported('source object crosses a prose container boundary')
        # The measured prose container is one body column. Its frame rules
        # extend half a point beyond text bounds; that sliver is not a second
        # independent column with the top border as the bottom's predecessor.
        local=layout([{**items[i],'flow':list(items[background[0]]['flow'])} for i in children])
        delta=round(max(g['output'][3] for g in local)-max(g['source'][3] for g in local))
        # Preserve the source top and bottom padding, including rounding to a
        # whole PDF point so retained glyph pixels keep the same raster phase.
        if box[3]+delta<=box[1]:raise SourcePreservingUnsupported('prose container has no remaining height')
        resize={'source_rect':list(box),'height_delta':delta,'source_seqno':draw['seqno']}
        containers.append({'groups':local,'artwork_index':art_index,'resize':resize})
        bg=items[background[0]];target=list(bg['source']);target[3]+=delta
        parents.append({**bg,'kind':'container','index':len(containers)-1,'target':target})
        used.update(children+background)
    return [item for i,item in enumerate(items) if i not in used]+parents,containers


def resized_rectangle_bytes(native,item):
    resize=item['container_resize'];path=[o for o in operations(native) if item['start']<=o.start and o.end<=item['end']]
    if [o.name for o in path]!=[b're',b'f']:
        raise SourcePreservingUnsupported('resizable rectangle source operation changed')
    op=path[0];x,y,w,h=[float(native.tokens[i][1]) for i in op.token_indices]
    d=item['ctm'][3]
    anchor=max(y,y+h) if d>0 else min(y,y+h)
    height=resize['source_rect'][3]-resize['source_rect'][1]
    scale=(height+resize['height_delta'])/height
    value=f'{x:.12g} {anchor+(y-anchor)*scale:.12g} {w:.12g} {h*scale:.12g} re\n'.encode()
    return value+native.stream[op.end:item['end']]


def verify_container_drawings(source_page,target_page,artwork):
    """Check the actual rendered rectangle independently, then remove only those
    backgrounds from the separate rigid-content pixel comparison copies.
    """
    import pymupdf
    from .native_editor import inspect_native_page
    from .native_paint import bind_nontext
    from .native_composite_assets import suppress_paint_edits
    from .native_stream_edits import apply_stream_edits
    resized=[a for a in artwork if a.get('container_resize')]
    if not resized:return [],None
    original={d['seqno']:d for d in source_page.get_drawings()}
    actual=target_page.get_drawings();proofs=[];seqnos=set()
    for art in resized:
        spec=art['container_resize'];before=original[spec['source_seqno']]
        rect=list(spec['source_rect']);dy=art.get('dy',0)
        expected=[rect[0],rect[1]+dy,rect[2],rect[3]+spec['height_delta']+dy]
        matches=[d for d in actual if max(abs(x-y) for x,y in zip(d['rect'],expected))<.002]
        if len(matches)!=1:raise SourcePreservingUnsupported('saved container rectangle geometry differs from planned height')
        after=matches[0]
        if after['seqno'] in seqnos:raise SourcePreservingUnsupported('duplicate resized container')
        keys=('type','fill','color','fill_opacity','stroke_opacity','width','lineCap','lineJoin','dashes','even_odd','closePath')
        if any(before.get(k)!=after.get(k) for k in keys) or len(after['items'])!=1 or after['items'][0][0]!='re':
            raise SourcePreservingUnsupported('resized container changed source fill or path style')
        seqnos.add(after['seqno'])
        proofs.append({'source_rect':rect,'target_rect':list(after['rect']),'height_delta':spec['height_delta'],'dy':dy,'source_style_verified':True})
    # Use the actual target byte stream, not another re-render of the plan.
    native=inspect_native_page(target_page);bound=bind_nontext(target_page,native)
    ends={a['end'] for a in bound if set(a['seqnos'])&seqnos}
    if len(ends)!=len(resized):raise SourcePreservingUnsupported('actual container removal binding is ambiguous')
    stream=apply_stream_edits(native.stream,suppress_paint_edits(native,ends))
    with pymupdf.open() as clean:
        clean.insert_pdf(target_page.parent,from_page=target_page.number,to_page=target_page.number)
        clean.update_stream(clean[0].get_contents()[0],stream)
        return proofs,clean.tobytes()
