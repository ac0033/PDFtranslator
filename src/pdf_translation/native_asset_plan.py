"""Semantic boundaries for native source assets, independent of original prose slots."""
from __future__ import annotations
from pathlib import Path
import re
from .hashing import stable_hash
from .native_translation import prepare_native_document
from .native_plan import math_literal, glyph_text, MATH_WORDS
from .native_segmentation import number_compound_hyphen
from .native_assets import NativeAssetStore
from .native_editor import inspect_native_page
from .schemas import ProtectedSpan,ArtifactRef


def prepare_asset_document(source: Path):
    originals,plans=prepare_native_document(source)
    store=NativeAssetStore(source)
    units=[];newplans=[]
    for original,plan in zip(originals,plans):
        if original.unit_type=='protected':
            uid=original.unit_id.replace('native-','asset-',1)
            units.append(original.model_copy(update={'unit_id':uid,'neighbor_ids':tuple(n.replace('native-','asset-',1) for n in original.neighbor_ids)}))
            newplans.append({**plan,'unit_id':uid});continue
        pieces=[];spans=[];hints={};assets={}
        fixed_ids=set()
        for ri,row in enumerate(plan['rows']):
            if row.get('inline_connector'):
                fixed_ids.update(gid for s in row['slots'] if s['kind']=='protected' for gid in s['glyph_ids'])
                continue
            if not all(s['kind']=='protected' for s in row['slots']):continue
            pi=row['page_index']
            if pi not in store.natives:store.natives[pi]=inspect_native_page(store.source[pi])
            gs=[store.natives[pi].glyphs[gid] for s in row['slots'] for gid in s['glyph_ids']]
            size=max(g.size for g in gs)
            base=[g for g in gs if g.size>=size*.9 and not g.text.isspace() and g.text not in '.,;:']
            previous=plan['rows'][ri-1] if ri else None
            left=row.get('column_bounds',[406.74 if row['column']=='margin' else 72])[0]
            short_function=bool(re.match(r"^[A-Za-z]+\s*[′']?\s*\(",glyph_text(gs)) and row['bbox'][2]-row['bbox'][0]<80)
            continuation=(previous is not None and previous['page_index']==pi and previous['column']==row['column']
                and any(s['kind']=='prose' for s in previous['slots']) and not previous['source_text'].rstrip().endswith((':','.','!','?'))
                and 0<row['baseline']-previous['baseline']<=22 and (row['bbox'][2]-row['bbox'][0]<40 or short_function) and row['bbox'][0]<=left+15
                and (short_function or sum(g.text.isalpha() for g in base)==1 and all(g.text.isalpha() or g.text in '()' for g in base))
                and not any('Mono' in g.font for g in gs))
            if not continuation:fixed_ids.update(g.index for g in gs)

        def add_plain(text):
            if text:pieces.append(text)
        def add_asset(slot,gs):
            if not gs:return
            oid=f'a-{original.unit_id}-o{len(spans):03d}'
            marker=f'⟦P:{oid}⟧'
            is_code=any('Mono' in g.font for g in gs)
            literal=glyph_text(gs) if is_code else math_literal(gs,slot['baseline'])
            ref=original.source_refs[0].model_copy(update={'page_index':slot['page_index'],'bbox':tuple(slot['bbox'])})
            spans.append(ProtectedSpan(occurrence_id=oid,marker=marker,kind='code' if is_code else 'formula',
                original=ArtifactRef(uri=f'native-assets/{oid}',sha256=stable_hash([g.index for g in gs]),media_type='application/json'),source_ref=ref))
            hints[marker]=literal
            assets[marker]={'page_index':slot['page_index'],'glyph_ids':[g.index for g in gs],'baseline':slot['baseline'],
                'placement':'source_fixed' if all(g.index in fixed_ids for g in gs) else 'inline_reflow'}
            pieces.append(marker)
        for chunk in plan['chunks']:
            if chunk['kind']=='prose':
                text=chunk['source_text']
                if text.startswith(('•','◦','▪','●')):
                    slot=chunk['slots'][0];pi=slot['page_index']
                    if pi not in store.natives:store.natives[pi]=inspect_native_page(store.source[pi])
                    glyphs=[store.natives[pi].glyphs[i] for i in slot['glyph_ids']]
                    bullet=next((g for g in glyphs if g.text==text[0]),None)
                    if bullet is None:raise ValueError('list marker has no native glyph')
                    add_asset(slot,[bullet]);text=text[1:].lstrip()
                add_plain(text);continue
            for slot in chunk['slots']:
                if slot['page_index'] not in store.natives:
                    store.natives[slot['page_index']]=inspect_native_page(store.source[slot['page_index']])
                native=store.natives[slot['page_index']]
                gs=sorted((native.glyphs[i] for i in slot['glyph_ids']),key=lambda g:g.index)
                pending=[]; relation_split=False
                # In prose, punctuation is language; periods cannot absorb the next formula subject.
                for index,g in enumerate(gs):
                    adjacent_digits=index>0 and index+1<len(gs) and gs[index-1].text.isdigit() and gs[index+1].text.isdigit()
                    punctuation=g.index not in fixed_ids and g.text in '.,:;' and (g.text=='.' or index==len(gs)-1) and g.font in ('URWPalladioL-Roma','URWPalladioL-Bold','TeXGyrePagella-Regular','TeXGyrePagella-Bold') and not adjacent_digits
                    ci=next(i for i,c in enumerate(plan['chunks']) if c is chunk)
                    next_prose=plan['chunks'][ci+1].get('source_text','') if ci+1<len(plan['chunks']) else ''
                    prose_slash=g.text=='/' and len(gs)==1 and bool(re.search(r'[A-Za-z]{2,}$',chunk_before(plan,chunk))) and bool(re.match(r'^[A-Za-z]{2,}',next_prose))
                    previous_glyph=native.glyphs[g.index-1] if g.index else None
                    prose_hyphen=g.index not in fixed_ids and number_compound_hyphen(previous_glyph,g,next_prose,MATH_WORDS)
                    if punctuation or prose_slash or prose_hyphen:
                        add_asset(slot,pending);pending=[];add_plain(g.text);continue
                    # 'error at a = ...' describes an error at a; it is not an equation a=...
                    if g.text=='=' and pending and not relation_split and re.search(r'error(?: signal)? at\s*$', chunk_before(plan,chunk)):
                        add_asset(slot,pending);pending=[];relation_split=True
                    pending.append(g)
                add_asset(slot,pending)
        # A vector fraction bar belongs to the native formula. Preserve the
        # composite in place rather than extracting its glyphs without the bar.
        if len(plan['rows'])==1:
            for marker,asset in assets.items():
                if asset['placement']!='inline_reflow':continue
                gs=[store.natives[asset['page_index']].glyphs[i] for i in asset['glyph_ids']]
                left=min(g.ink_bounds[0] for g in gs);right=max(g.ink_bounds[2] for g in gs)
                top=min(g.ink_bounds[1] for g in gs);bottom=max(g.ink_bounds[3] for g in gs)
                bars=[item for path in store.source[asset['page_index']].get_drawings() for item in path['items']
                    if item[0]=='l' and abs(item[1].y-item[2].y)<.2
                    and left-1<=min(item[1].x,item[2].x)<max(item[1].x,item[2].x)<=right+1
                    and top<item[1].y<bottom]
                if bars:
                    asset['placement']='source_fixed';plan={**plan,'inline_fixed_assets':True}
        if plan.get('inline_fixed_assets'):
            # Prose on a row containing a source composite stays between its
            # original neighbors. Moving other objects from that same row
            # would invalidate those explicitly bounded prose intervals.
            for asset in assets.values():asset['placement']='source_fixed'
        text=' '.join(pieces)
        uid=original.unit_id.replace('native-','asset-',1)
        unit=original.model_copy(update={'unit_id':uid,'source_text':text,'source_hash':stable_hash(text),
            'ir_revision':'native-source-assets-rc13','protected':tuple(spans),'protected_literal_hints':hints,
            'risk_tags':('native_source_assets',), 'neighbor_ids':tuple(n.replace('native-','asset-',1) for n in original.neighbor_ids)})
        units.append(unit);newplans.append({**plan,'unit_id':uid,'assets':assets})
    store.close()
    return units,newplans


def chunk_before(plan,chunk):
    index=next(i for i,c in enumerate(plan['chunks']) if c is chunk)
    return plan['chunks'][index-1].get('source_text','') if index else ''
