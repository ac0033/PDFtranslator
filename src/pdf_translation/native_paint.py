"""Translate complete original PDF painting operations without rebuilding objects.

TJ strings retain their font codes and advances. Paths and images retain source
operators. Nonidentity graphics matrices are inverted for page-space movement.
"""
from dataclasses import dataclass
from .native_stream_edits import apply_stream_edits
from babeldoc.pdfminer.psparser import PSKeyword
from .source_preserving import SourcePreservingUnsupported


@dataclass
class Operation:
    start: int
    end: int
    name: bytes
    token_indices: tuple
    ctm: tuple


def operations(native):
    tokens=native.tokens;depth=0;begin=0;matrix=(1.,0.,0.,1.,0.,0.);stack=[];result=[]
    for i,(pos,token) in enumerate(tokens):
        if not isinstance(token,PSKeyword):continue
        name=token.name
        if name in (b'[',b'<<'):depth+=1;continue
        if name in (b']',b'>>'):depth-=1;continue
        if depth:continue
        end=tokens[i+1][0] if i+1<len(tokens) else len(native.stream)
        args=[t for _,t in tokens[begin:i]]
        result.append(Operation(tokens[begin][0],end,name,tuple(range(begin,i)),matrix))
        if name==b'q':stack.append(matrix)
        elif name==b'Q':
            if not stack:raise SourcePreservingUnsupported('unbalanced source graphics state')
            matrix=stack.pop()
        elif name==b'cm':
            if len(args)!=6:raise SourcePreservingUnsupported('invalid source matrix')
            a,b,c,d,e,f=matrix;u,v,w,x,y,z=map(float,args)
            matrix=(a*u+c*v,b*u+d*v,a*w+c*x,b*w+d*x,a*y+c*z+e,b*y+d*z+f)
        begin=i+1
    if depth or stack:raise SourcePreservingUnsupported('unbalanced source graphics state')
    return result


def nontext_operations(native):
    output=[];path_start=None
    for op in operations(native):
        if op.name in (b'm',b're') and path_start is None:path_start=op
        if op.name in (b'S',b's',b'f',b'F',b'f*',b'B',b'B*',b'b',b'b*'):
            if path_start is None:continue  # PDF permits painting an empty current path; it emits no object.
            if path_start.ctm!=op.ctm:raise SourcePreservingUnsupported('source changes matrix while constructing path')
            types=('fill-path','stroke-path') if op.name in (b'B',b'B*',b'b',b'b*') else ('stroke-path',) if op.name in (b'S',b's') else ('fill-path',)
            output.append((path_start.start,op.end,path_start.ctm,types));path_start=None
        elif op.name==b'n':path_start=None
        elif op.name in (b'Do',b'sh'):
            output.append((op.start,op.end,op.ctm,('xobject',) if op.name==b'Do' else ('fill-shade',)))
        elif op.name in (b'BI',b'ID',b'EI'):
            raise SourcePreservingUnsupported('inline image paint needs explicit parsing')
    return output


def bind_nontext(page,native):
    try:result=_bind_nontext_fast(page,native)
    except SourcePreservingUnsupported as exc:
        if str(exc) not in ('native nontext operations do not match rendered paint order','unaccounted source artwork, including nested forms'):raise
        result=_bind_marked_nontext(page,native)
    return _constrain_to_source_clips(page,native,result)


def _bind_nontext_fast(page,native):
    logs=[(i,kind,bbox) for i,(kind,bbox,*_) in enumerate(page.get_bboxlog()) if 'text' not in kind and bbox[0]<=bbox[2] and bbox[1]<=bbox[3]]
    paints=nontext_operations(native);cursor=0;result=[]
    drawings=_drawing_flow_boxes(page)
    for start,end,matrix,kinds in paints:
        if kinds==('xobject',):
            kinds=_isolated_xobject_kinds(page,native,start,end)
            if not kinds:continue
        records=logs[cursor:cursor+len(kinds)]
        if tuple(r[1] for r in records)!=kinds:
            raise SourcePreservingUnsupported('native nontext operations do not match rendered paint order')
        cursor+=len(kinds)
        boxes=[]
        for seq,kind,bbox in records:
            if seq in drawings:
                bbox=drawings[seq]
            if bbox[0]<bbox[2] and bbox[1]<bbox[3]:boxes.append(bbox)
        result.append({'start':start,'end':end,'ctm':matrix,'kinds':kinds,'seqnos':[r[0] for r in records],
                       'bbox':(min(b[0] for b in boxes),min(b[1] for b in boxes),max(b[2] for b in boxes),max(b[3] for b in boxes)) if boxes else (0,0,0,0),'visible':bool(boxes)})
    if cursor!=len(logs):raise SourcePreservingUnsupported('unaccounted source artwork, including nested forms')
    return result


def translation_commands(matrix,dy):
    a,b,c,d,_,_=matrix;det=a*d-b*c
    if abs(det)<1e-12:raise SourcePreservingUnsupported('singular source graphics matrix')
    # PDF user space is bottom-up; page coordinates are top-down.
    ux=c*dy/det;uy=-a*dy/det
    return f'1 0 0 1 {ux:.12g} {uy:.12g} cm '.encode(),f' 1 0 0 1 {-ux:.12g} {-uy:.12g} cm '.encode()


def rewrite_paint(native,selected,glyph_shifts,artwork):
    from .native_composite_assets import suppress_paint_edits
    edits=suppress_paint_edits(native,{a['end'] for a in artwork if a.get('remove')})
    for op in operations(native):
        if op.name!=b'TJ':continue
        strings=[i for i in op.token_indices if i in native.strings]
        shifts={glyph_shifts.get(g.index,0) for i in strings for g in native.strings[i] if g.index not in selected and not g.text.isspace()}
        if len(shifts)>1:raise SourcePreservingUnsupported('one source text operation crosses independently moved groups')
        dy=next(iter(shifts),0)
        value=native.stream[op.start:op.end]
        for i in reversed(strings):
            gs=native.strings[i]
            if not any(g.index in selected for g in gs):continue
            replacement=' '.join(format(-g.advance,'.12g') if g.index in selected else f'<{g.code:0{getattr(g,"code_bytes",1)*2}x}>' for g in gs)
            start=native.tokens[i][0]-op.start;end=native.tokens[i+1][0]-op.start
            value=value[:start]+b' '+replacement.encode()+b' '+value[end:]
        if dy:
            before,after=translation_commands(op.ctm,dy)
            value=b'q '+before+value+b' Q '
        if value!=native.stream[op.start:op.end]:edits.append((op.start,op.end,value))
    for item in artwork:
        if item.get('remove'):continue
        dy=item.get('dy',0)
        if not dy and not item.get('container_resize'):continue
        value=native.stream[item['start']:item['end']]
        if item.get('container_resize'):
            from .native_container_flow import resized_rectangle_bytes
            value=resized_rectangle_bytes(native,item)
        before,after=translation_commands(item['ctm'],dy)
        # Inverse cm preserves color/line settings made between path start and paint.
        edits.append((item['start'],item['end'],before+value+after))
    for clip in clipping_scopes(native):
        shifts={glyph_shifts.get(g.index,0) for g in native.glyphs if g.index not in selected and not g.text.isspace()
                and clip['end']<=native.tokens[g.token_index][0]<clip['scope_end']}
        shifts.update(a.get('dy',0) for a in artwork if not a.get('remove') and clip['end']<=a['start']<clip['scope_end'])
        if len(shifts)>1:raise SourcePreservingUnsupported('source clipping region crosses independently moved groups')
        dy=next(iter(shifts),0)
        if dy:
            before,after=translation_commands(clip['ctm'],dy)
            edits.append((clip['start'],clip['end'],before+native.stream[clip['start']:clip['end']]+after))
    edits.sort()
    if any(b[0]<a[1] for a,b in zip(edits,edits[1:])):
        raise SourcePreservingUnsupported('overlapping source paint rewrite ranges')
    return apply_stream_edits(native.stream,edits)


def _isolated_xobject_kinds(page,native,start,end):
    """Use the renderer to bind one original image or nested vector form."""
    import pymupdf
    edits=[]
    for op in operations(native):
        if op.start==start and op.end==end:continue
        if op.name in (b'TJ',b'Tj',b"'",b'"'):
            edits.append((op.start,op.end,b' [] TJ '))
        elif op.name in (b'S',b's',b'f',b'F',b'f*',b'B',b'B*',b'b',b'b*'):
            terminal=native.tokens[op.token_indices[-1]+1][0] if op.token_indices else op.start
            edits.append((terminal,op.end,b' n '))
        elif op.name in (b'Do',b'sh'):
            edits.append((op.start,op.end,b' '))
    stream=apply_stream_edits(native.stream,edits)
    with pymupdf.open() as isolated:
        isolated.insert_pdf(page.parent,from_page=page.number,to_page=page.number)
        isolated.update_stream(isolated[0].get_contents()[0],stream)
        kinds=tuple(row[0] for row in isolated[0].get_bboxlog() if row[1][0]<=row[1][2] and row[1][1]<=row[1][3])
    if any('text' in kind for kind in kinds):raise SourcePreservingUnsupported('nested source form contains unaccounted text')
    return kinds


def clipping_scopes(native):
    """Locate clipping path construction and its enclosing graphics-state scope."""
    result=[];active=[];depth=0;path_start=None;clipping=False
    for op in operations(native):
        if op.name==b'q':depth+=1
        elif op.name==b'Q':
            for clip in active[:]:
                if clip['depth']==depth:clip['scope_end']=op.start;active.remove(clip)
            depth-=1
        if op.name in (b'm',b're') and path_start is None:path_start=op
        if op.name in (b'W',b'W*'):clipping=True
        if op.name in (b'n',b'S',b's',b'f',b'F',b'f*',b'B',b'B*',b'b',b'b*'):
            if clipping:
                if path_start is None or op.name!=b'n':raise SourcePreservingUnsupported('painting and clipping in the same path needs a composite operation')
                clip={'start':path_start.start,'end':op.end,'ctm':path_start.ctm,'depth':depth,'scope_end':len(native.stream)}
                result.append(clip);active.append(clip)
            path_start=None;clipping=False
    return result


def _bind_marked_nontext(page,native):
    """Use invisible probe markers to delimit actual renderer events per operation.

    Some clipped strokes emit no visible bbox record. A source B operator can
    therefore produce only its fill. Probe markers identify these cases without
    guessing, and the complete original nontext log must remain identical.
    """
    import pymupdf
    paints=nontext_operations(native);edits=[]
    sentinel=b' q BT /helv 1 Tf 3 Tr 1 0 0 1 0 0 Tm (M) Tj ET Q '
    for start,end,matrix,kinds in paints:edits.append((end,end,sentinel))
    for op in operations(native):
        if op.name==b'TJ':edits.append((op.start,op.end,b' [] TJ '))
    stream=apply_stream_edits(native.stream,edits)
    with pymupdf.open() as probe:
        probe.insert_pdf(page.parent,from_page=page.number,to_page=page.number)
        probe[0].insert_font(fontname='helv')
        probe.update_stream(probe[0].get_contents()[0],stream)
        groups=[];pending=[]
        for kind,bbox,*_ in probe[0].get_bboxlog():
            if kind=='ignore-text':groups.append(pending);pending=[]
            elif 'text' not in kind and bbox[0]<=bbox[2] and bbox[1]<=bbox[3]:pending.append((kind,bbox))
        if pending or len(groups)!=len(paints):raise SourcePreservingUnsupported('paint probe markers do not cover every operation')
    logs=[(i,k,b) for i,(k,b,*_) in enumerate(page.get_bboxlog()) if 'text' not in k and b[0]<=b[2] and b[1]<=b[3]]
    flat=[x for g in groups for x in g]
    if len(flat)!=len(logs) or any(a[0]!=b[1] or max(abs(x-y) for x,y in zip(a[1],b[2]))>.001 for a,b in zip(flat,logs)):
        raise SourcePreservingUnsupported('paint probe changed source graphics')
    drawings=_drawing_flow_boxes(page);cursor=0;result=[]
    for paint,events in zip(paints,groups):
        start,end,matrix,_=paint;records=logs[cursor:cursor+len(events)];cursor+=len(events);boxes=[]
        for seq,kind,bbox in records:
            if seq in drawings:
                bbox=drawings[seq]
            if bbox[0]<bbox[2] and bbox[1]<bbox[3]:boxes.append(bbox)
        box=(min(b[0] for b in boxes),min(b[1] for b in boxes),max(b[2] for b in boxes),max(b[3] for b in boxes)) if boxes else None
        result.append({'start':start,'end':end,'ctm':matrix,'kinds':tuple(r[1] for r in records),'seqnos':[r[0] for r in records],
                       'bbox':box,'visible':bool(boxes),'binding':'renderer_probe'})
    return result



def _drawing_flow_boxes(page):
    """Visible layout envelopes honor the source graphics clipping stack.

    A curve can extend thousands of points beyond a plot while its source
    clip exposes only a small box. Its hidden continuation must not move text.
    Native path and clipping bytes remain unchanged by this measurement.
    """
    import pymupdf
    clips=[];boxes={}
    for draw in page.get_drawings(extended=True):
        level=draw['level']
        clips=[(depth,rect) for depth,rect in clips if depth<level]
        if draw['type']=='clip':
            rect=pymupdf.Rect(draw['scissor'])
            for _,parent in clips:rect=rect & parent
            clips.append((level,rect));continue
        if 'seqno' not in draw or 'rect' not in draw:continue
        pad=max(.5,float(draw.get('width',0) or 0)/2)
        rect=pymupdf.Rect(draw['rect'])+(-pad,-pad,pad,pad)
        for _,clip in clips:rect=rect & clip
        rect=rect & page.rect
        boxes[draw['seqno']]=tuple(rect)
        if draw['type']=='fs':boxes[draw['seqno']+1]=tuple(rect)
    return boxes



def _constrain_to_source_clips(page,native,artwork):
    import pymupdf
    scopes=clipping_scopes(native);ops=operations(native);bounds=[]
    for clip in scopes:
        path=[op for op in ops if clip['start']<=op.start and op.end<=clip['end']]
        if any(op.name not in (b're',b'm',b'l',b'h',b'W',b'W*',b'n') for op in path):continue
        points=[]
        for op in path:
            values=[float(native.tokens[i][1]) for i in op.token_indices]
            if op.name==b're':
                x,y,w,h=values;points.extend([(x,y),(x+w,y),(x,y+h),(x+w,y+h)])
            elif op.name in (b'm',b'l'):points.append(tuple(values))
        if not points:continue
        matrix=pymupdf.Matrix(*clip['ctm'])*page.transformation_matrix
        mapped=[pymupdf.Point(*point)*matrix for point in points]
        bounds.append((clip,pymupdf.Rect(min(p.x for p in mapped),min(p.y for p in mapped),max(p.x for p in mapped),max(p.y for p in mapped))))
    for art in artwork:
        if art.get('visible') is False or art['bbox'] is None:continue
        rect=pymupdf.Rect(art['bbox']) & page.rect
        for clip,box in bounds:
            if clip['end']<=art['start']<clip['scope_end']:rect=rect & box
        art['bbox']=tuple(rect) if not rect.is_empty else (0,0,0,0)
        art['visible']=not rect.is_empty
    return artwork
