"""Keep short figure labels in their own original text frames.

Only horizontal prose in short source text lines next to actual vector artwork
qualifies. Long paragraph blocks, margin captions, scripts and formula fonts
are excluded. Retained math never becomes part of the translated label.
"""
import re
import pymupdf


def figure_label_rows(page,native,geometry):
    if geometry['profile']!='measured_source_lines':return []
    body_width=geometry['body'][1]-geometry['body'][0]
    blocks=page.get_text('dict')['blocks']
    paragraph_lines=[pymupdf.Rect(l['bbox']) for b in blocks for l in b.get('lines',[]) if l['bbox'][2]-l['bbox'][0]>body_width*.6]
    drawings=[pymupdf.Rect(d['rect']) for d in page.get_drawings()
              if (d['rect'].width>8 or d['rect'].height>8) and not any(d['rect'].contains(box) for box in paragraph_lines)]
    rows=[];used=set()
    for block in blocks:
        lines=block.get('lines',[])
        if any(l['bbox'][2]-l['bbox'][0]>body_width*.6 for l in lines):continue
        for line in lines:
            box=pymupdf.Rect(line['bbox'])
            if tuple(line['dir'])!=(1.,0.) or box.width>body_width*.5:continue
            if box.y0<geometry['header_bottom'] or box.y1>geometry['footer_top']:continue
            col='margin' if geometry['margin'][1]>geometry['margin'][0] and box.x0>=(geometry['body'][1]+geometry['margin'][0])/2 else 'body'
            column_width=geometry[col][1]-geometry[col][0]
            if any(l['bbox'][2]-l['bbox'][0]>column_width*.6 for l in lines):continue
            # A line must touch or sit immediately beside a drawn figure.
            if not any((r+(-20,-20,20,20)).intersects(box) for r in drawings):continue
            gs=[g for g in native.glyphs if g.index not in used and g.direction==(1.,0.)
                and g.font in ('TeXGyrePagella-Regular','TeXGyrePagella-Bold','URWPalladioL-Roma','URWPalladioL-Bold')
                and g.size>=geometry['body_size']*.7 and box.x0-.1<=g.origin[0]<box.x1
                and box.y0<=g.origin[1]<=box.y1]
            if not gs:continue
            # Spatial line extraction is already ordered; normalize via native advances.
            from .native_plan import glyph_text,MATH_WORDS
            text=glyph_text(gs).strip();words=re.findall('[A-Za-z]+',text)
            if not any(len(w)>=4 and w.lower() not in MATH_WORDS for w in words):continue
            if any(g.text.isdigit() for g in gs):continue
            bounds=[min(g.origin[0] for g in gs),min(g.bbox[1] for g in gs),
                    max(g.origin[0]+g.advance*g.size/1000 for g in gs),max(g.bbox[3] for g in gs)]
            ids=[g.index for g in gs];used.update(ids)
            slot={'kind':'prose','glyph_ids':ids,'text':text,'bbox':bounds,'source_font_size':min(g.size for g in gs)}
            rows.append({'column':col,'baseline':gs[0].origin[1],'size':max(g.size for g in gs),
                'anchor_ids':ids,'anchor_text':text,'source_text':text,'bbox':bounds,
                'slots':[slot],'figure_label':True,'local_inline_frame':bounds})
    rows.extend(equation_label_rows(page,native,geometry,used))
    return rows


def equation_label_rows(page,native,geometry,used):
    from .native_plan import glyph_text,MATH_WORDS
    braces=[g for g in native.glyphs if g.font.startswith('CMEX') and g.text in {'\uf8f1','\uf8f2','\uf8f3','\uf8f4'}]
    result=[]
    for block in page.get_text('dict')['blocks']:
        for line in block.get('lines',[]):
            for span in line['spans']:
                text=span['text'].strip();size=span['size'];box=span['bbox']
                if span['font']!='TeXGyrePagella-Regular' or size<geometry['body_size']*.9:continue
                if not re.fullmatch(r'[A-Za-z][A-Za-z ,;:-]*',text):continue
                if text.lower() in MATH_WORDS-{'if','where','otherwise'}:continue
                beside_brace=any(g.origin[0]<box[0]<geometry['body'][1] and abs(g.origin[1]-span['origin'][1])<size*1.25 for g in braces)
                following=sorted((g for g in native.glyphs if not g.text.isspace() and abs(g.origin[1]-span['origin'][1])<.6 and g.origin[0]>=box[2]-.01),key=lambda g:g.origin[0])
                before_equals=bool(following and following[0].text=='=' and following[0].origin[0]-box[2]<size*1.5 and len(text)<=28 and len(text.split())<=3 and geometry['body'][0]<=box[0]<geometry['body'][1])
                if not beside_brace and not before_equals:continue
                gs=[g for g in native.glyphs if g.index not in used and g.font==span['font'] and abs(g.origin[1]-span['origin'][1])<.1 and box[0]-.1<=g.origin[0]<box[2]]
                if not gs:continue
                bounds=[min(g.origin[0] for g in gs),min(g.bbox[1] for g in gs),max(g.origin[0]+g.advance*g.size/1000 for g in gs),max(g.bbox[3] for g in gs)]
                ids=[g.index for g in gs];used.update(ids)
                slot={'kind':'prose','glyph_ids':ids,'text':glyph_text(gs),'bbox':bounds,'source_font_size':size}
                frame=[bounds[0]-size*.2,bounds[1],bounds[2]+size*.2,bounds[3]]
                result.append({'column':'body','baseline':span['origin'][1],'size':size,'anchor_ids':ids,'anchor_text':text,'source_text':text,'bbox':bounds,'slots':[slot],'equation_label':True,'local_inline_frame':frame})
    return result
