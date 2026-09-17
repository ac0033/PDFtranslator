"""Rebind already reviewed text to source-identical native marker revisions.

The caller must bind the review index to the same source PDF hash. No similarity
search is used: complete physical source character sequences must match exactly.
"""
import re
from collections import Counter
from .hashing import stable_hash

MARKER=re.compile(r'⟦P:[^⟧]+⟧')


def physical_literal(text):
    # These delimiters describe glyph positions in native math context, not new glyphs.
    return ''.join(c for c in re.sub(r'[_^](?=\{)','',text).replace('{','').replace('}','') if not c.isspace())


def physical_source(text,literals):
    parts=[];intervals={};cursor=0;last=0
    for match in MARKER.finditer(text):
        plain=''.join(text[last:match.start()].split());parts.append(plain);cursor+=len(plain)
        if match.group() not in literals:raise ValueError('review marker lacks source literal')
        value=physical_literal(literals[match.group()])
        if not value:raise ValueError('empty source literal cannot be rebound')
        intervals[match.group()]=(cursor,cursor+len(value));parts.append(value);cursor+=len(value);last=match.end()
    parts.append(''.join(text[last:].split()))
    return ''.join(parts),intervals


def rebind_review(unit,plan,record):
    old,old_intervals=physical_source(record['source_text'],record.get('source_literals',{}))
    new,new_intervals=physical_source(unit.source_text,unit.protected_literal_hints)
    if old!=new:raise ValueError('review source physical characters changed')
    if sorted(record.get('source_pages',plan['source_pages']))!=plan['source_pages']:
        raise ValueError('review source page interval changed')
    inverse={position:marker for marker,position in old_intervals.items()}
    mapping={};promoted=[]
    for marker,position in new_intervals.items():
        if position in inverse:mapping[inverse[position]]=marker
        else:
            literal=unit.protected_literal_hints[marker]
            if not re.fullmatch(r'[A-Za-z0-9]+',literal):raise ValueError('new complex source asset requires a reviewed revision')
            promoted.append((marker,literal))
    if set(mapping)!=set(old_intervals):raise ValueError('old source assets cannot be dropped or regrouped')
    text=MARKER.sub(lambda m:mapping[m.group()],record['translated_text'])
    counts=Counter(literal for _,literal in promoted)
    for literal,count in counts.items():
        # Never match inside another asset marker or a longer Latin identifier.
        plain=MARKER.sub(lambda m:' '*len(m.group()),text)
        matches=list(re.finditer(r'(?<![A-Za-z0-9_])'+re.escape(literal)+r'(?![A-Za-z0-9_])',plain))
        if len(matches)!=count:raise ValueError('new literal has ambiguous target occurrences')
        markers=[m for m,l in promoted if l==literal]
        for match,marker in reversed(list(zip(matches,markers))):text=text[:match.start()]+marker+text[match.end():]
    if sorted(MARKER.findall(text))!=sorted(new_intervals):raise ValueError('rebound target source asset coverage is not exact')
    before=physical_source(record['translated_text'],record.get('source_literals',{}))[0]
    after=physical_source(text,unit.protected_literal_hints)[0]
    if before!=after:raise ValueError('replay changed reviewed translation content')
    result={**record,'previous_unit_id':record['unit_id'],'unit_id':unit.unit_id,'source_text':unit.source_text,
            'source_literals':unit.protected_literal_hints,'source_pages':plan['source_pages'],'translated_text':text}
    proof={'unit_id':unit.unit_id,'review_record_hash':stable_hash(record),'source_physical_hash':stable_hash(new),
           'translation_physical_hash':stable_hash(after),'promoted_literals':promoted,'marker_rebinding':mapping,
           'physical_source_and_translation_unchanged':True}
    return result,proof
