from __future__ import annotations

import re
import pymupdf
from .native_editor import inspect_native_page, remove_native_glyphs, glyph_inventory, trace_inventory, inventories_equal
from .native_translation import split_native_translation


class NativeLayoutOverflow(ValueError):
    pass


def fit_chunk(text, slots, natives, selected, column, font):
    remaining=text.strip()
    placements=[]
    for slot in slots:
        native=natives[slot['page_index']]
        glyphs=[native.glyphs[i] for i in slot['glyph_ids']]
        baseline=glyphs[0].origin[1]
        size=slot['source_font_size']
        x0,_,x1,_=slot['bbox']
        boundary=382 if column=='body' else 550
        left=x0-2
        right=boundary
        for g in native.glyphs:
            if g.index in selected[slot['page_index']] or abs(g.origin[1]-baseline)>size*.7:
                continue
            end=g.origin[0]+g.advance*g.size/1000
            if end<=x0+.1:left=max(left,end+.5)
            elif g.origin[0]>=x1-.1:right=min(right,g.origin[0]-.5)
        # Consecutive physical lines are filled together, keeping every immutable boundary.
        count=0
        for end in range(1,len(remaining)+1):
            if font.text_length(remaining[:end],fontsize=size)>right-left:break
            count=end
        if count<len(remaining) and count:
            while count and re.match(r'[A-Za-z]',remaining[count-1:count+1]) and len(remaining[count-1:count+1])==2 and remaining[count-1:count+1].isalpha():count-=1
            while count and remaining[count-1] in '（([【':count-=1
        if count:
            placements.append({'page_index':slot['page_index'],'origin':(left,baseline),'text':remaining[:count].rstrip(),'size':size,'width':right-left})
            remaining=remaining[count:].lstrip()
    if remaining:
        raise NativeLayoutOverflow(f'{text!r}: no source slot for {remaining!r}')
    return placements


def plan_native_layout(units, plans, translations, source, font_path):
    font=pymupdf.Font(fontfile=str(font_path))
    with pymupdf.open(source) as doc:natives={i:inspect_native_page(p) for i,p in enumerate(doc)}
    selected={i:set() for i in natives}
    for plan in plans:
        if plan['unit_id'] not in translations:continue
        for chunk in plan['chunks']:
            if chunk['kind']=='prose':
                for slot in chunk['slots']:selected[slot['page_index']].update(slot['glyph_ids'])
    placements=[];errors=[]
    for unit,plan in zip(units,plans):
        if unit.unit_id not in translations:continue
        try:
            for chunk,text in split_native_translation(unit,plan,translations[unit.unit_id]):
                placements.extend(fit_chunk(text,chunk['slots'],natives,selected,plan['column'],font))
        except ValueError as e:errors.append({'unit_id':unit.unit_id,'error':str(e)})
    return natives,selected,placements,errors
