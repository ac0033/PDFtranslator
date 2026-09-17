"""Preserve source fraction rules together with their encoded native glyphs."""
from .native_paint import operations
from .native_stream_edits import apply_stream_edits
from .source_preserving import SourcePreservingUnsupported


def fraction_paints(page,glyphs,artwork):
    selected=[]
    for drawing in page.get_drawings():
        items=drawing['items']
        if len(items)!=1 or items[0][0]!='l' or drawing['type']!='s':continue
        _,a,b=items[0]
        if abs(a.y-b.y)>.2:continue
        left,right=sorted((a.x,b.x));y=(a.y+b.y)/2
        if right-left<1 or (drawing.get('width',0) or 0)>1:continue
        above=[g for g in glyphs if g.bbox[0]<right and g.bbox[2]>left and 0<y-g.origin[1]<g.size*1.5]
        below=[g for g in glyphs if g.bbox[0]<right and g.bbox[2]>left and 0<g.origin[1]-y<g.size*1.5]
        if not above or not below:continue
        if left<min(g.bbox[0] for g in above+below)-2 or right>max(g.bbox[2] for g in above+below)+2:continue
        matches=[a for a in artwork if drawing['seqno'] in a['seqnos']]
        if len(matches)!=1:raise SourcePreservingUnsupported('fraction rule has no unique source paint operation')
        selected.append(matches[0])
    return selected


def suppress_paint_edits(native,ends):
    edits=[];seen=set()
    for op in operations(native):
        if op.end not in ends:continue
        if op.name in (b'S',b's',b'f',b'F',b'f*',b'B',b'B*',b'b',b'b*'):
            edits.append((op.start,op.end,b' n '));seen.add(op.end)
        elif op.name in (b'Do',b'sh'):
            edits.append((op.start,op.end,b' '));seen.add(op.end)
    if seen!=set(ends):raise SourcePreservingUnsupported('source paint removal did not match every operation')
    return edits


def isolate_composite(native,glyph_ids,artwork,keep_ends):
    edits=suppress_paint_edits(native,{a['end'] for a in artwork}-set(keep_ends))
    for index,glyphs in native.strings.items():
        if all(g.index in glyph_ids for g in glyphs):continue
        value=' '.join(f'<{g.code:0{g.code_bytes*2}x}>' if g.index in glyph_ids else format(-g.advance,'.12g') for g in glyphs)
        edits.append((native.tokens[index][0],native.tokens[index+1][0],b' '+value.encode('ascii')+b' '))
    return apply_stream_edits(native.stream,edits)
