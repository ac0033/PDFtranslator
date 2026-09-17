"""Native source rows and explicit editable slots for immutable-object layout."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, replace
import re
import unicodedata

from .native_editor import inspect_native_page
from .native_columns import PROSE_FONTS,infer_columns,glyph_column
from .native_segmentation import short_prose_continuation, short_math_list_lead

PRIMARY = PROSE_FONTS
MATH_WORDS = {'exp','log','ln','sin','cos','tan','tanh','hardtanh','softsign','sgn','argmax','argmin','max','min','rect','leaky','minimize','where','otherwise'}


def word_gap(glyph):
    # CID TeX text uses justified spaces as narrow as 0.17 em.
    return max(.5,(.15 if getattr(glyph,'code_bytes',1)==2 else .18)*glyph.size)


def glyph_text(glyphs):
    text = ''
    previous = None
    for glyph in sorted(glyphs, key=lambda g: (g.origin[0], g.index)):
        if previous and glyph.origin[0] - (previous.origin[0] + previous.advance * previous.size / 1000) > word_gap(glyph):
            text += ' '
        text += glyph.text
        previous = glyph
    return unicodedata.normalize('NFKC', text)


def column(glyph):
    if glyph.origin[1] < 60:
        return 'header'
    # A long body line can cross the nominal margin boundary; keep its words together.
    return 'body' if glyph.origin[0] < 395 or glyph.font in PRIMARY and glyph.size>=9.5 else 'margin'


def native_page_plan(page):
    native = inspect_native_page(page)
    geometry=infer_columns(page)
    def page_column(glyph):return glyph_column(glyph,geometry)
    from .native_figure_labels import figure_label_rows
    labels=figure_label_rows(page,native,geometry)
    label_ids={gid for r in labels for slot in r['slots'] for gid in slot['glyph_ids']}
    script_ids=set();math_attachments={};operator_ids=set()
    for block in page.get_text('dict')['blocks']:
        for line in block.get('lines',[]):
            pieces=line['spans']
            for i,span in enumerate(pieces[:-1]):
                if span['text'].strip()=='sign' and pieces[i+1]['text'].lstrip().startswith('('):
                    operator_ids.update(g.index for g in native.glyphs if g.font==span['font'] and abs(g.origin[1]-span['origin'][1])<.1 and span['bbox'][0]-.1<=g.origin[0]<span['bbox'][2])
    spans=[span for block in page.get_text('dict')['blocks'] for line in block.get('lines',[]) for span in line['spans']]
    math_bases=[span for span in spans if span['font'] in ('URWPalladioL-Ital','PazoMath-Italic') and len(span['text'].strip())==1 and span['text'].strip().isalpha()]
    for span in spans:
        if span['font'] not in PRIMARY:continue
        parents=[parent for parent in math_bases if span['size']<parent['size']*.85
                 and abs(span['origin'][0]-parent['bbox'][2])<=parent['size']*.6
                 and .1<abs(span['origin'][1]-parent['origin'][1])<=parent['size']*.6]
        if parents:
            script_ids.update(g.index for g in native.glyphs if g.font==span['font'] and abs(g.size-span['size'])<.01 and abs(g.origin[1]-span['origin'][1])<.1 and span['bbox'][0]-.1<=g.origin[0]<span['bbox'][2])
    for mark in native.glyphs:
        if mark.text not in {'\u0307','\u0308','\u02d9','\u00a8','\u02c6'}:continue
        candidates=[g for g in native.glyphs if g.font in ('URWPalladioL-Ital','PazoMath-Italic') and g.text.isalpha()
                    and g.origin[0]-.5<=mark.origin[0]<=g.origin[0]+g.advance*g.size/1000+.5
                    and 0<=g.origin[1]-mark.origin[1]<=g.size*.5 and abs(g.size-mark.size)<.5]
        if candidates:math_attachments[mark.index]=min(candidates,key=lambda g:abs(g.origin[1]-mark.origin[1])).index
    # Baselines come from native prose fonts, never superscript/math-font boxes.
    anchors = defaultdict(list)
    for glyph in native.glyphs:
        if glyph.index not in label_ids|script_ids|operator_ids and glyph.font in PRIMARY and glyph.direction==(1.,0.) and glyph.origin[1]<geometry['footer_top']:
            anchors[(page_column(glyph), round(glyph.origin[1] * 2) / 2)].append(glyph)
    rows = []
    previous_anchor = {}
    for (col, baseline), glyphs in sorted(anchors.items(), key=lambda item: (item[0][0], item[0][1])):
        text = glyph_text(glyphs)
        previous = previous_anchor.get(col)
        continuation = bool(previous and max(g.size for g in glyphs)>=previous[2]*.9 and 0 < baseline-previous[0] <= 20 and previous[1].rstrip().endswith('-') and re.match(r'^[a-z]{1,3}(?:\b|[0-9])',text))
        lexical_text=glyph_text([g for g in glyphs if g.font!='URWPalladioL-Ital']) if geometry['profile']=='measured_source_lines' else text
        words = re.findall(r'[A-Za-z]{2,}', lexical_text)
        prose_words = [w for w in words if w.lower() not in MATH_WORDS]
        prose = len(prose_words) >= 2 and any(len(w) >= 3 for w in prose_words)
        prose |= len(prose_words) == 1 and len(prose_words[0]) >= 4 and prose_words[0].lower() not in MATH_WORDS
        prose |= len(prose_words)==1 and prose_words[0].lower() in {'be','of','to','in','is','by','as','at','or','and'}
        prose |= bool(previous and short_prose_continuation(text,previous[1],size=max(g.size for g in glyphs),previous_size=previous[2],gap=baseline-previous[0],previous_is_prose=previous[3],math_words=MATH_WORDS))
        prose |= bool(previous and short_math_list_lead(text,previous[1],size=max(g.size for g in glyphs),previous_size=previous[2],gap=baseline-previous[0],previous_is_prose=previous[3],math_words=MATH_WORDS))
        prose |= continuation
        prose |= bool(re.match(r'^where\b',text))
        prose |= bool(previous and baseline-previous[0]<=20 and re.search(r'\bet$',previous[1].strip()) and text.strip() in {'al.','al'})
        if geometry['profile']=='measured_source_lines' and not prose_words:prose=False
        if prose_words and {w.lower() for w in prose_words} <= {'new','old'}:
            prose=False  # Literal old/new labels inside update equations.
        # Nearby small script anchors do not replace the surrounding text row.
        subordinate=bool(previous and max(g.size for g in glyphs)<previous[2]*.9 and 0<baseline-previous[0]<=previous[2]*.8)
        if not subordinate:
            previous_anchor[col]=(baseline,text,max(g.size for g in glyphs),bool(prose))
        if not prose:
            continue
        ordered = sorted(glyphs, key=lambda g: (g.origin[0], g.index))
        if text.strip().startswith('"') and text.strip().endswith('"'):
            # A standalone quoted sentence is source example data (NER here).
            continue
        rows.append({'column':col,'baseline':baseline,'anchor_ids':[g.index for g in ordered],
            'anchor_text':text,'size':max(g.size for g in glyphs),'glyphs':[]})
    # Math-only continuation lines need their own baseline candidates. Otherwise
    # a following line's superscript can be attached to letters in the previous caption.
    for glyph in native.glyphs:
        col=page_column(glyph);minimum=7.5 if col=='margin' else 9.5
        if glyph.font in PRIMARY or glyph.size<minimum or col!='margin':continue
        if not any(r['column']==col and abs(r['baseline']-glyph.origin[1])<=1 for r in rows):
            rows.append({'column':col,'baseline':glyph.origin[1],'anchor_ids':[],
                         'anchor_text':'','size':glyph.size,'glyphs':[]})
    assigned_rows={}
    for glyph in native.glyphs:
        if glyph.index in label_ids:continue
        parent_row=None
        for parent in reversed(native.glyphs[max(0,glyph.index-20):glyph.index]):
            if parent.size>=glyph.size*1.2 and parent.text.isalpha() and abs(parent.origin[1]-glyph.origin[1])<=10 and -2<=glyph.origin[0]-parent.origin[0]<=25:
                parent_row=assigned_rows.get(parent.index)
                if parent_row is not None:break
        nested_math_script=bool(parent_row is not None and glyph.size<parent_row['size']*.85
            and parent.font=='URWPalladioL-Ital' and glyph.font in ('URWPalladioL-Ital','TeXGyrePagella-Italic'))
        if parent_row is not None and (parent_row['column']=='margin' and glyph.font not in PRIMARY or nested_math_script):
            parent_row['glyphs'].append(glyph);assigned_rows[glyph.index]=parent_row;continue
        # TeX builds tall delimiters from vertically stacked extension glyphs.
        # Their origins are construction offsets, not separate formula baselines.
        if glyph.font.startswith('CMEX'):
            stack=[g for g in native.glyphs if g.font==glyph.font and g.code==glyph.code
                   and abs(g.size-glyph.size)<.01 and abs(g.origin[0]-glyph.origin[0])<.01
                   and abs(g.origin[1]-glyph.origin[1])<=glyph.size*1.5]
            stack_rows=[r for r in rows if r['column']==page_column(glyph)
                        and any(abs(r['baseline']-g.origin[1])<=.6 for g in stack)]
            if len(stack)>=2 and len(stack_rows)==1:
                row=stack_rows[0];row['glyphs'].append(glyph);assigned_rows[glyph.index]=row;continue
        candidates = [r for r in rows if r['column'] == page_column(glyph)
            and abs(r['baseline']-glyph.origin[1]) <= (0.6 if glyph.font in PRIMARY and glyph.size >= r['size']*0.95 else 8)]
        if glyph.font not in PRIMARY:
            # A high delimiter in a centered display can share the previous
            # short prose line's construction y coordinate. It belongs there
            # only if it is horizontally adjacent to that line's anchors.
            candidates=[r for r in candidates if not r['anchor_ids'] or
                min(native.glyphs[i].origin[0] for i in r['anchor_ids'])-r['size']*1.5<=glyph.origin[0]<=
                max(native.glyphs[i].origin[0]+native.glyphs[i].advance*native.glyphs[i].size/1000 for i in r['anchor_ids'])+r['size']*1.5]
        if candidates:
            row = min(candidates,key=lambda r:abs(r['baseline']-glyph.origin[1]))
            row['glyphs'].append(glyph)
            assigned_rows[glyph.index]=row
    for gid,base in math_attachments.items():
        destination=assigned_rows.get(base);previous=assigned_rows.get(gid)
        if destination is not None and destination is not previous:
            if previous is not None:previous['glyphs']=[g for g in previous['glyphs'] if g.index!=gid]
            destination['glyphs'].append(native.glyphs[gid]);assigned_rows[gid]=destination
    result = []
    for row in rows:
        glyphs = sorted(row.pop('glyphs'),key=lambda g:(g.origin[0],g.index))
        anchors = [g for g in glyphs if g.index in row['anchor_ids']]
        # Form words with the same spatial gap rule used for readable evidence.
        groups=[]
        for glyph in (g for g in glyphs if abs(g.origin[1]-row['baseline'])<=0.6):
            if (not groups or glyph.origin[0]-(groups[-1][-1].origin[0]+groups[-1][-1].advance*groups[-1][-1].size/1000)>word_gap(glyph)):
                groups.append([])
            groups[-1].append(glyph)
        editable=set()
        italic_runs=[]
        for g in glyphs:
            if not italic_runs or g.font not in ('URWPalladioL-Ital','TeXGyrePagella-Italic') or italic_runs[-1][-1].font not in ('URWPalladioL-Ital','TeXGyrePagella-Italic') or abs(g.origin[1]-italic_runs[-1][-1].origin[1])>.6 or g.origin[0]-(italic_runs[-1][-1].origin[0]+italic_runs[-1][-1].advance*italic_runs[-1][-1].size/1000)>g.size*.8:
                italic_runs.append([])
            italic_runs[-1].append(g)
        italic_prose_ids={g.index for run in italic_runs if all(g.font in ('URWPalladioL-Ital','TeXGyrePagella-Italic') for g in run) and any(len(w)>=3 and w.lower() not in MATH_WORDS for w in re.findall(r'[A-Za-z]+',glyph_text(run))) for g in run}
        italic_heading=sum(g.font in ('URWPalladioL-Ital','TeXGyrePagella-Italic') for g in anchors)>len(anchors)*.7
        for group in groups:
            word=''.join(g.text for g in group)
            if re.fullmatch(r'[ivxlcdm]+',word.lower().strip('.,:;')) and 'part '+word.lower().strip('.,:;') in row['anchor_text'].lower():
                continue
            # Keep alphanumeric identifiers and source function names intact.
            if re.fullmatch(r'(?:[A-Z],){2,}[A-Z][.,;:]?',word):continue
            call=re.fullmatch(r'([A-Za-z]+)\([^()]*\)[.,;:]?',word)
            if call and call[1].lower() in MATH_WORDS:continue
            prose_number=re.fullmatch(r'([A-Za-z]+)\d+(?:[.,]\d+)*[.,;:!?)]*',word)
            ordinary_number_prefix=bool(prose_number and prose_number[1].lower() in {'value','values','figure','example','algorithm','chapter','section','table','equation','step','steps','page','pages'} and all(not g.text.isalpha() or g.font in ('TeXGyrePagella-Regular','TeXGyrePagella-Bold') for g in group))
            if (any(g.text.isdigit() for g in group) and '-' not in word and not ordinary_number_prefix) or word.strip('.,:;()[]').lower() in MATH_WORDS - {'where','otherwise','minimize','leaky'}:
                continue
            for glyph in group:
                if glyph.index in math_attachments or glyph.index in script_ids or glyph.index in operator_ids:continue
                if glyph.font in ('URWPalladioL-Ital','TeXGyrePagella-Italic') and not italic_heading:
                    # Math-font delimiters separate italic variable runs. Do not
                    # concatenate f, W and x across delimiters into a prose word.
                    if glyph.index not in italic_prose_ids:
                        continue
                if glyph.font in PRIMARY and not glyph.text.isdigit() and not re.match(r'[=<>+*/\\^|∈∑∂∇α-ωΑ-Ω]',glyph.text):
                    editable.add(glyph.index)
        # Punctuation-only slots do not need translation and retain their source
        # appearance. Non-prose glyphs, including every sub/superscript, separate slots.
        slots=[]
        for glyph in glyphs:
            if glyph.index in editable:
                if not slots or slots[-1]['kind']!='prose':slots.append({'kind':'prose','glyphs':[]})
            else:
                if not slots or slots[-1]['kind']!='protected':slots.append({'kind':'protected','glyphs':[]})
            slots[-1]['glyphs'].append(glyph)
        for slot in slots:
            gs=slot.pop('glyphs')
            if slot['kind']=='prose' and not any(g.text.isalpha() for g in gs):slot['kind']='protected'
            slot.update(glyph_ids=[g.index for g in gs], text=glyph_text(gs),
                bbox=[min(g.origin[0] for g in gs),min(g.bbox[1] for g in gs),max(g.origin[0]+g.advance*g.size/1000 for g in gs),max(g.bbox[3] for g in gs)],
                source_font_size=min(g.size for g in gs))
        if not any(s['kind']=='prose' for s in slots):continue
        row['slots']=slots
        row['source_text']=' '.join(s['text'] for s in slots)
        row['bbox']=[min(s['bbox'][0] for s in slots),min(s['bbox'][1] for s in slots),max(s['bbox'][2] for s in slots),max(s['bbox'][3] for s in slots)]
        result.append(row)
    result.extend(labels)
    assigned = {gid for row in result for slot in row['slots'] for gid in slot['glyph_ids']}
    for col in ('header','body','margin'):
        rest = sorted((g for g in native.glyphs if g.index not in assigned and page_column(g)==col),key=lambda g:(g.bbox[1],g.origin[0]))
        clusters=[]
        for g in rest:
            if not clusters or g.bbox[1]>max(x.bbox[3] for x in clusters[-1])+0.1:
                clusters.append([])
            clusters[-1].append(g)
        for gs in clusters:
            bounds=[min(g.origin[0] for g in gs),min(g.bbox[1] for g in gs),max(g.origin[0]+g.advance*g.size/1000 for g in gs),max(g.bbox[3] for g in gs)]
            base=max(gs,key=lambda g:g.size).origin[1]
            result.append({'column':col,'baseline':base,'size':max(g.size for g in gs),'anchor_ids':[],
                'anchor_text':'','source_text':glyph_text(gs),'bbox':bounds,
                'slots':[{'kind':'protected','glyph_ids':[g.index for g in gs],'text':glyph_text(gs),'bbox':bounds,'source_font_size':min(g.size for g in gs)}]})
    for row in result:
        _expose_inline_connector(native,row)
    all_ids=[gid for row in result for slot in row['slots'] for gid in slot['glyph_ids']]
    if len(all_ids)!=len(set(all_ids)) or set(all_ids)!={g.index for g in native.glyphs}:
        raise ValueError('native plan does not cover source glyphs exactly once')
    for row in result:
        for slot in row['slots']:
            if slot['kind']=='protected':
                gs=[native.glyphs[i] for i in slot['glyph_ids']]
                slot['literal']=glyph_text(gs) if any('Mono' in g.font for g in gs) else math_literal(gs,row['baseline'])
    containers=[]
    if geometry['profile']=='measured_source_lines':
        for drawing in page.get_drawings():
            fill=drawing.get('fill');rect=drawing['rect']
            if fill and min(fill)>=.8 and rect.width>(geometry['body'][1]-geometry['body'][0])*.6 and rect.height>geometry['body_size']*3:
                contents=[r for r in result if r['column']=='body' and any(s['kind']=='prose' for s in r['slots']) and r['bbox'][2]-r['bbox'][0]>rect.width*.55 and rect.contains(r['bbox'])]
                if contents:
                    left=min(r['bbox'][0] for r in contents);right=max(r['bbox'][2] for r in contents)+.5
                    if left>rect.x0+1 and right<rect.x1-1:containers.append((rect,[left,right]))
    for row in result:
        row['column_bounds']=geometry[row['column']]
        row['page_geometry']=geometry
        candidates=[c for c in containers if c[0].contains(row['bbox'])]
        if candidates and row['column']=='body':
            rect,frame=min(candidates,key=lambda c:c[0].get_area())
            row['column_bounds']=frame;row['container_bbox']=list(rect)
    return native,sorted(result,key=lambda r:(r['column'],r['baseline']))


def math_literal(glyphs, baseline):
    """Readable evidence with script roles inferred from original baselines.

    This is context only. Rendering always keeps the actual source glyphs.
    """
    # This decoding changes context evidence only; NativeGlyph.text and its
    # source bytes remain untouched for all rendering and fidelity inventories.
    names={'vextenddouble':'‖','radicalBigg':'√','radicalBig':'√',
           'parenleftbigg':'(','parenrightbigg':')','braceleftBigg':'{',
           'bracketleftbigg':'[','bracketrightbigg':']'}
    decoded=[];seen_stacks=set()
    for g in sorted(glyphs,key=lambda g:g.index):
        if g.text=='�' and g.glyph_name in names:
            if g.glyph_name=='vextenddouble':
                key=(g.font,round(g.origin[0],2),round(g.size,2))
                if key in seen_stacks:continue
                seen_stacks.add(key)
            g=replace(g,text=names[g.glyph_name],origin=(g.origin[0],baseline))
        decoded.append(g)
    parts=[]
    current=None
    for g in decoded:
        role='super' if g.origin[1]<baseline-2 else ('sub' if g.origin[1]>baseline+1.5 else 'base')
        if role!=current:
            parts.append([role,[]]);current=role
        parts[-1][1].append(g)
    return ''.join(('^{'+_nested_script_text(gs)+'}' if role=='super' else '_{'+_nested_script_text(gs)+'}' if role=='sub' else glyph_text(gs)) for role,gs in parts)


def _nested_script_text(glyphs):
    size=max(g.size for g in glyphs)
    parents=[g for g in glyphs if g.size>=size*.95]
    if size/min(g.size for g in glyphs)<1.15 or max(g.origin[1] for g in parents)-min(g.origin[1] for g in parents)>.6:
        return glyph_text(glyphs)
    baseline=parents[0].origin[1];parts=[]
    for g in sorted(glyphs,key=lambda g:g.index):
        role='super' if g.origin[1]<baseline-2 else ('sub' if g.origin[1]>baseline+1.5 else 'base')
        if not parts or parts[-1][0]!=role:parts.append((role,[]))
        parts[-1][1].append(g)
    return ''.join('^{'+glyph_text(gs)+'}' if role=='super' else '_{'+glyph_text(gs)+'}' if role=='sub' else glyph_text(gs) for role,gs in parts)


def _expose_inline_connector(native, row):
    """Expose a prose connector inside a display equation without splitting its math vertically."""
    if any(s['kind']=='prose' for s in row['slots']):return
    gs=[native.glyphs[i] for s in row['slots'] for i in s['glyph_ids']]
    words=[]
    for g in sorted((g for g in gs if g.font=='URWPalladioL-Roma' and g.text.isalpha()),key=lambda g:(g.origin[1],g.origin[0])):
        if not words or abs(g.origin[1]-words[-1][-1].origin[1])>.6 or g.origin[0]-(words[-1][-1].origin[0]+words[-1][-1].advance*words[-1][-1].size/1000)>max(.5,g.size*.18):words.append([])
        words[-1].append(g)
    matches=[w for w in words if ''.join(g.text for g in w)=='where']
    if len(matches)!=1:return
    word=matches[0];ids={g.index for g in word};left=min(g.bbox[0] for g in word);right=max(g.bbox[2] for g in word)
    before=[g for g in gs if g.index not in ids and g.bbox[2]<=left]
    after=[g for g in gs if g.index not in ids and g.bbox[0]>=right]
    if not before or not after or len(before)+len(word)+len(after)!=len(gs):return
    slots=[]
    for kind,part in [('protected',before),('prose',word),('protected',after)]:
        bounds=[min(g.bbox[0] for g in part),min(g.bbox[1] for g in part),max(g.bbox[2] for g in part),max(g.bbox[3] for g in part)]
        slots.append({'kind':kind,'glyph_ids':[g.index for g in sorted(part,key=lambda g:g.index)],'text':glyph_text(part),'bbox':bounds,'source_font_size':min(g.size for g in part)})
    row.update(slots=slots,inline_connector=True,baseline=word[0].origin[1],size=word[0].size,source_text=' '.join(s['text'] for s in slots))
