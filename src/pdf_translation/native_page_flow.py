"""Allocate a whole translated prose unit across its original physical pages."""
from __future__ import annotations
import math,re
import pymupdf
from .hashing import stable_hash
from .source_preserving import SourcePreservingUnsupported


def _fit_prefix(text,width,lines,font,size,asset_widths=None):
    used=0;line=1;end=0
    matches=[]
    pattern=r'⟦P:[^⟧]+⟧|——|……|[A-Za-z][A-Za-z0-9_-]*|.'
    for match in re.finditer(r'⟦P:[^⟧]+⟧|“[^”\n]*”|——|……|[A-Za-z][A-Za-z0-9_-]*|.',text):
        if match.group().startswith('“') and font.text_length(match.group(),fontsize=size)>width:
            matches.extend((part.group(),match.start()+part.end()) for part in re.finditer(pattern,match.group()))
        else:matches.append((match.group(),match.end()))
    for token,token_end in matches:
        length=(asset_widths or {}).get(token,font.text_length(token,fontsize=size))
        if length>width:raise SourcePreservingUnsupported('unbreakable prose token exceeds column width')
        if used+length>width:line+=1;used=0
        if line>lines:break
        used+=length;end=token_end
    return end


def expand_prose_pages(units,plans,translations,font_path,source=None):
    font=pymupdf.Font(fontfile=str(font_path));out_units=[];out_plans=[];out_text={};proof=[]
    for unit,plan in zip(units,plans):
        if unit.unit_id not in translations:continue
        if len(plan['source_pages'])==1:
            out_units.append(unit);out_plans.append(plan);out_text[unit.unit_id]=translations[unit.unit_id];continue
        widths={}
        if plan['assets']:
            if source is None:raise SourcePreservingUnsupported('cross-page inline source assets require original glyph measurements')
            from .native_editor import inspect_native_page
            with pymupdf.open(source) as original:
                pages={i:inspect_native_page(original[i]) for i in {a['page_index'] for a in plan['assets'].values()}}
                for marker,a in plan['assets'].items():
                    if a['placement']!='inline_reflow':raise SourcePreservingUnsupported('cross-page fixed asset was not expanded')
                    gs=[pages[a['page_index']].glyphs[i] for i in a['glyph_ids']]
                    widths[marker]=max(max(g.origin[0]+g.advance*g.size/1000,g.ink_bounds[2]) for g in gs)-min(min(g.origin[0],g.ink_bounds[0]) for g in gs)+3
            markers=re.findall(r'⟦P:[^⟧]+⟧',translations[unit.unit_id])
            if sorted(markers)!=sorted(plan['assets']):raise SourcePreservingUnsupported('cross-page asset coverage is not exact')
            sequence=[plan['assets'][m]['page_index'] for m in markers]
            if sequence!=sorted(sequence):raise SourcePreservingUnsupported('cross-page asset order would move an original object between pages')
        remaining=translations[unit.unit_id].strip();parts=[]
        for page_number in plan['source_pages']:
            rows=[r for r in plan['rows'] if r['page_index']==page_number-1]
            chunks=[{**c,'slots':[s for s in c['slots'] if s['page_index']==page_number-1]} for c in plan['chunks']]
            chunks=[c for c in chunks if c['slots']]
            size=min(s['source_font_size'] for c in chunks if c['kind']=='prose' for s in c['slots'])
            height=max(r['baseline'] for r in rows)-min(r['baseline'] for r in rows)
            baselines=sorted({r['baseline'] for r in rows})
            gaps=[b-a for a,b in zip(baselines,baselines[1:]) if b>a]
            line_height=min(max(14,size*1.5),max(size*1.25,min(gaps))) if gaps else max(14,size*1.5)
            lines=max(1,math.floor((height+.001)/line_height)+1)
            width=rows[0]['column_bounds'][1]-rows[0]['column_bounds'][0] if rows[0].get('column_bounds') else 310.8 if plan['column']=='body' else 143.26
            page_assets={m:a for m,a in plan['assets'].items() if a['page_index']==page_number-1}
            minimum=max((remaining.index(m)+len(m) for m in page_assets),default=0)
            maximum=min((remaining.index(m) for m,a in plan['assets'].items() if a['page_index']>=page_number and m in remaining),default=len(remaining))
            end=min(maximum,_fit_prefix(remaining,width,lines,font,size,widths))
            if end<minimum:raise SourcePreservingUnsupported('original-page asset does not fit its translated page interval')
            if end<len(remaining):
                punctuation=[m.end() for m in re.finditer(r'[。！？；]',remaining[:end])]
                if punctuation and punctuation[-1]>=minimum:end=punctuation[-1]
                while end and remaining[end-1] in '（([“‘':end-=1
                while end and end<len(remaining) and remaining[end] in '，。；：！？、）”’.,;:!?)]':
                    previous=list(re.finditer(r'⟦P:[^⟧]+⟧|“[^”\n]*”|[A-Za-z][A-Za-z0-9_-]*|.',remaining[:end]))
                    end=previous[-1].start() if previous else 0
            if end<minimum:raise SourcePreservingUnsupported('punctuation boundary would move an original-page asset')
            text=remaining[:end].strip();remaining=remaining[end:].lstrip()
            source_text=' '.join(s['text'] for c in chunks for s in c['slots'])
            uid=f'{unit.unit_id}-p{page_number:03d}'
            out_units.append(unit.model_copy(update={'unit_id':uid,'source_text':source_text,'source_hash':stable_hash(source_text),'protected_literal_hints':{m:unit.protected_literal_hints[m] for m in page_assets}}))
            out_plans.append({**plan,'unit_id':uid,'parent_unit_id':unit.unit_id,'source_pages':[page_number],'chunks':chunks,'rows':rows,'flow_line_height':line_height,'assets':page_assets})
            out_text[uid]=text;parts.append({'page':page_number,'unit_id':uid,'translated_text':text})
        if remaining:raise SourcePreservingUnsupported('whole cross-page translation exceeds original page intervals')
        if ''.join(p['translated_text'] for p in parts).replace(' ','')!=translations[unit.unit_id].strip().replace(' ',''):
            raise SourcePreservingUnsupported('cross-page allocation changed translated content')
        proof.append({'unit_id':unit.unit_id,'parts':parts,'content_preserved':True})
    return out_units,out_plans,out_text,proof
