from __future__ import annotations

from pathlib import Path
import re
import unicodedata
import pymupdf

from .hashing import sha256_file, stable_hash
from .native_plan import native_page_plan
from .native_segmentation import paragraph_gap_boundary
from .schemas import ArtifactRef, ProtectedSpan, SourceRef, TranslationUnit


def _prose(row):
    return any(s['kind']=='prose' for s in row['slots'])


def _heading(row):
    if row['slots'] and row['slots'][0]['source_font_size']<row['size']*.9:return False  # Superscript footnote markers are not section numbers.
    text=row['source_text'].strip()
    return bool(re.match(r'^\d+(?:\.\d+)*\s+[A-Z]',text) and len(text)<120 and not re.match(r'^\d+\.\s',text))


def prepare_native_document(source: Path):
    document_hash=sha256_file(source)
    groups=[]
    with pymupdf.open(source) as document:
        pages=[]
        for pi,page in enumerate(document):
            _,rows=native_page_plan(page)
            for row in rows:
                row['page_index']=pi
                for slot in row['slots']:
                    slot['page_index']=pi
                    slot['baseline']=row['baseline']
            pages.append(rows)
        for col in ('header','body','margin'):
            current=[]
            for rows in pages:
                for row in [r for r in rows if r['column']==col]:
                    if current:
                        previous=current[-1]
                        gap=row['baseline']-previous['baseline']
                        adjacent=row['page_index']==previous['page_index']+1
                        end=bool(re.search(r'[.!?:;]["”\)]?$',previous['source_text'].strip()))
                        if previous['source_text'].rstrip().endswith(':') and row['page_index']==previous['page_index'] and re.match(r'^[a-z]',row['source_text']):
                            end=False  # Lowercase continuation belongs to the preceding colon clause.
                        numbered=bool(re.match(r'^\d+\.\s',row['source_text']))
                        named_section=adjacent and bool(re.match(r'^[A-Z][A-Za-z-]{1,25}:',row['source_text']))
                        stop=(row.get('inline_connector') or previous.get('inline_connector')
                            or row.get('local_inline_frame') or previous.get('local_inline_frame')
                            or row.get('container_bbox')!=previous.get('container_bbox')
                            or col=='body' and row['page_index']==previous['page_index'] and _prose(row) and _prose(previous) and max(row['size'],previous['size'])/min(row['size'],previous['size'])>1.15
                            or row['page_index']>previous['page_index']+1
                            or col!='body' and row['page_index']!=previous['page_index']
                            or not adjacent and gap>max(previous['size'],row['size'])*2.25
                            or numbered or named_section or _heading(row) or _heading(previous)
                            or paragraph_gap_boundary(current,row)
                            or end and _prose(row))
                        if stop:
                            groups.append(current);current=[]
                    current.append(row)
            if current:groups.append(current)
    units=[]
    plans=[]
    for gi,rows in enumerate(groups):
        uid=f'native-{document_hash[:12]}-g{gi:04d}'
        chunks=[]
        for row in rows:
            for slot in row['slots']:
                kind=slot['kind']
                if not chunks or chunks[-1]['kind']!=kind:
                    chunks.append({'kind':kind,'slots':[]})
                chunks[-1]['slots'].append(slot)
        pieces=[];spans=[];hints={}
        for ci,chunk in enumerate(chunks):
            if chunk['kind']=='prose':
                value=''
                for slot in chunk['slots']:
                    part=slot['text']
                    if value.endswith('-') and re.match(r'^[a-z]',part):value=value[:-1]+part
                    else:value+=(' ' if value else '')+part
                value=unicodedata.normalize('NFKC',value)
                chunk['source_text']=value
                pieces.append(value)
            else:
                marker=f'⟦P:n-g{gi:04d}-o{ci:03d}⟧'
                chunk['marker']=marker
                literal=' '.join(s.get('literal',s['text']) for s in chunk['slots'])
                hints[marker]=literal
                first=chunk['slots'][0]
                spans.append(ProtectedSpan(occurrence_id=f'n-g{gi:04d}-o{ci:03d}',marker=marker,kind='formula',
                    original=ArtifactRef(uri=f'native-glyphs/{uid}/{ci}',sha256=stable_hash(chunk),media_type='application/json'),
                    source_ref=SourceRef(block_id=uid,page_index=first['page_index'],bbox=tuple(first['bbox']),char_start=0,char_end=0)))
                pieces.append(marker)
        text=' '.join(pieces)
        refs=tuple(SourceRef(block_id=f'{uid}-r{ri}',page_index=row['page_index'],bbox=tuple(row['bbox']),char_start=0,char_end=len(text)) for ri,row in enumerate(rows))
        unit=TranslationUnit(unit_id=uid,document_id=f'{source.stem}-{document_hash[:16]}',ir_revision='native-immutable-slots-rc13',
            unit_type='paragraph' if any(c['kind']=='prose' for c in chunks) else 'protected',source_lang='en',target_lang='zh-CN',
            source_text=text,source_hash=stable_hash(text),source_refs=refs,protected=tuple(spans),protected_literal_hints=hints,
            risk_tags=('native_immutable_slots',))
        units.append(unit)
        plans.append({'unit_id':uid,'column':rows[0]['column'],'source_pages':sorted({r['page_index']+1 for r in rows}),
            'chunks':chunks,'rows':rows,**({'local_inline_frame':rows[0]['local_inline_frame'],'figure_label':bool(rows[0].get('figure_label')),'equation_label':bool(rows[0].get('equation_label'))} if len(rows)==1 and rows[0].get('local_inline_frame') else {})})
    # Real neighboring evidence stays in the reading column, including formulas.
    for i,unit in enumerate(units):
        neighbors=tuple(units[j].unit_id for j in (i-1,i+1) if 0<=j<len(units) and plans[j]['column']==plans[i]['column']
            and abs(plans[j]['source_pages'][0]-plans[i]['source_pages'][0])<=1)
        units[i]=unit.model_copy(update={'neighbor_ids':neighbors,'section_path':(plans[i]['column'],)})
    return units,plans


def split_native_translation(unit: TranslationUnit, plan: dict, translated: str):
    markers=[c['marker'] for c in plan['chunks'] if c['kind']=='protected']
    actual=re.findall(r'⟦P:[^⟧]+⟧',translated)
    if actual!=markers:
        raise ValueError(f'protected native object order changed for {unit.unit_id}')
    parts=re.split(r'⟦P:[^⟧]+⟧',translated)
    output=[];index=0
    for ci,chunk in enumerate(plan['chunks']):
        if chunk['kind']=='prose':
            output.append((chunk,parts[index].strip()))
        else:
            if (ci==0 or plan['chunks'][ci-1]['kind']=='protected') and parts[index].strip():
                raise ValueError('translation inserted text where no editable source slot exists')
            index+=1
    if plan['chunks'][-1]['kind']=='protected' and parts[-1].strip():
        raise ValueError('translation appended text beyond the source editable slots')
    return output
