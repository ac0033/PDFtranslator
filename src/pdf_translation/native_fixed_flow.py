"""Split translated prose around immovable display equations and code anchors.

All prose still comes from the whole-unit translation. This only allocates its
pieces to the original source intervals; no formula or code is reconstructed.
"""
from __future__ import annotations
import re
from .hashing import stable_hash
from .source_preserving import SourcePreservingUnsupported


def expand_fixed_flow(units,plans,translations):
    out_units=[];out_plans=[];out_text={};evidence=[]
    for unit,plan in zip(units,plans):
        if unit.unit_id not in translations:continue
        fixed={m:a for m,a in plan['assets'].items() if a.get('placement')=='source_fixed'}
        if not fixed:
            out_units.append(unit);out_plans.append(plan);out_text[unit.unit_id]=translations[unit.unit_id];continue
        source_markers=[m for m in re.findall(r'⟦P:[^⟧]+⟧',unit.source_text) if m in fixed]
        target_markers=[m for m in re.findall(r'⟦P:[^⟧]+⟧',translations[unit.unit_id]) if m in fixed]
        if target_markers!=source_markers:
            raise SourcePreservingUnsupported('fixed source anchors were reordered or omitted')
        pattern='|'.join(re.escape(m) for m in source_markers)
        source_parts=re.split(pattern,unit.source_text);target_parts=re.split(pattern,translations[unit.unit_id])
        if plan.get('inline_fixed_assets'):
            if len(plan['rows'])!=1 or len(fixed)!=len(plan['assets']):
                raise SourcePreservingUnsupported('mixed inline composites require explicit source intervals')
            row=plan['rows'][0];fixed_ids={gid for a in fixed.values() for gid in a['glyph_ids']}
            anchor_slots=[[s for s in row['slots'] if set(s['glyph_ids'])&set(fixed[m]['glyph_ids'])] for m in source_markers]
            edges=[min(s['bbox'][0] for s in slots) for slots in anchor_slots]
            allocated=set()
            for i,(source_text,target_text) in enumerate(zip(source_parts,target_parts)):
                slots=[s for s in row['slots'] if not set(s['glyph_ids'])&fixed_ids
                       and sum(s['bbox'][0]>edge for edge in edges)==i]
                if not slots:
                    if source_text.strip() or target_text.strip():raise SourcePreservingUnsupported('inline composite text has no original interval')
                    continue
                ids={gid for s in slots for gid in s['glyph_ids']};allocated.update(ids)
                frame=[min(s['bbox'][0] for s in slots),min(s['bbox'][1] for s in slots),max(s['bbox'][2] for s in slots),max(s['bbox'][3] for s in slots)]
                uid=f'{unit.unit_id}-inline{i:02d}'
                newrow={**row,'slots':slots,'bbox':frame,'source_text':source_text.strip()}
                out_units.append(unit.model_copy(update={'unit_id':uid,'source_text':source_text.strip(),'source_hash':stable_hash(source_text.strip()),'protected':(),'protected_literal_hints':{}}))
                out_plans.append({**plan,'unit_id':uid,'rows':[newrow],'chunks':[{'kind':'prose','slots':slots,'source_text':source_text.strip()}],'assets':{},'local_inline_frame':frame})
                out_text[uid]=target_text.strip()
            if allocated|fixed_ids!={gid for s in row['slots'] for gid in s['glyph_ids']}:
                raise SourcePreservingUnsupported('inline composite glyph partition incomplete')
            evidence.append({'unit_id':unit.unit_id,'fixed_markers':source_markers,'retained_glyph_ids':sorted(fixed_ids),'translated_intervals':len(source_parts),'original_composite_preserved':True})
            continue
        if len(plan['rows'])==1 and plan['rows'][0].get('inline_connector'):
            if len(source_markers)!=2 or any(p.strip() for p in (source_parts[0],source_parts[-1],target_parts[0],target_parts[-1])):
                raise SourcePreservingUnsupported('inline connector must remain between its two original formulas')
            row=plan['rows'][0];slots=[s for s in row['slots'] if s['kind']=='prose']
            if len(slots)!=1:raise SourcePreservingUnsupported('ambiguous inline connector interval')
            slot=slots[0];newrow={**row,'slots':slots,'bbox':slot['bbox'],'source_text':slot['text']}
            uid=f'{unit.unit_id}-connector';newunit=unit.model_copy(update={'unit_id':uid,'source_text':source_parts[1].strip(),'source_hash':stable_hash(source_parts[1].strip()),'protected':(),'protected_literal_hints':{}})
            out_units.append(newunit);out_plans.append({**plan,'unit_id':uid,'rows':[newrow],'chunks':[c for c in plan['chunks'] if c['kind']=='prose'],'assets':{},'local_inline_frame':slot['bbox']});out_text[uid]=target_parts[1].strip()
            evidence.append({'unit_id':unit.unit_id,'fixed_markers':source_markers,'retained_glyph_ids':sorted(gid for a in fixed.values() for gid in a['glyph_ids']),'translated_intervals':1,'inline_connector_original_frame':slot['bbox']})
            continue
        fixed_ids={gid for a in fixed.values() for gid in a['glyph_ids']}
        grouped=[[] for _ in source_parts]
        for row in plan['rows']:
            ids={gid for s in row['slots'] for gid in s['glyph_ids']}
            if ids<=fixed_ids:continue
            if ids&fixed_ids:raise SourcePreservingUnsupported('fixed anchor cuts through a prose row')
            key=(row['page_index'],row['baseline'])
            index=sum((a['page_index'],a['baseline'])<key for m in source_markers for a in [fixed[m]])
            grouped[index].append(row)
        allocated=set()
        for i,(source_text,target_text,rows) in enumerate(zip(source_parts,target_parts,grouped)):
            if not rows:
                if source_text.strip() or target_text.strip():
                    raise SourcePreservingUnsupported('translation has text outside a source prose interval')
                continue
            ids={gid for row in rows for slot in row['slots'] for gid in slot['glyph_ids']}
            allocated.update(ids)
            chunks=[]
            for chunk in plan['chunks']:
                slots=[s for s in chunk['slots'] if set(s['glyph_ids'])<=ids]
                if slots:chunks.append({**chunk,'slots':slots})
            assets={m:a for m,a in plan['assets'].items() if m not in fixed and set(a['glyph_ids'])<=ids}
            if set(re.findall(r'⟦P:[^⟧]+⟧',source_text))!=set(assets):
                raise SourcePreservingUnsupported('source interval and native object boundaries disagree')
            uid=f'{unit.unit_id}-flow{i:02d}'
            subset=tuple(s for s in unit.protected if s.marker in assets)
            newunit=unit.model_copy(update={'unit_id':uid,'source_text':source_text.strip(),'source_hash':stable_hash(source_text.strip()),
                'protected':subset,'protected_literal_hints':{m:unit.protected_literal_hints[m] for m in assets}})
            out_units.append(newunit);out_plans.append({**plan,'unit_id':uid,'parent_unit_id':unit.unit_id,'rows':rows,'chunks':chunks,
                'assets':assets,'code_context':any(s.kind=='code' and s.marker in fixed for s in unit.protected),'source_pages':sorted({r['page_index']+1 for r in rows})})
            out_text[uid]=target_text.strip()
        all_ids={gid for row in plan['rows'] for slot in row['slots'] for gid in slot['glyph_ids']}
        if allocated&fixed_ids or allocated|fixed_ids!=all_ids:
            raise SourcePreservingUnsupported('fixed-flow source glyph partition is incomplete')
        evidence.append({'unit_id':unit.unit_id,'fixed_markers':source_markers,'retained_glyph_ids':sorted(fixed_ids),'translated_intervals':sum(bool(r) for r in grouped)})
    return out_units,out_plans,out_text,evidence
