"""Geometry and context rules shared by native PDF text segmentation.

These predicates do not depend on a page number, document title, or a particular
short word. Formula-name exclusions are supplied by the caller.
"""
from __future__ import annotations
import re
from statistics import median


def short_prose_continuation(text,previous_text,*,size,previous_size,gap,previous_is_prose,math_words):
    word=text.strip()
    return bool(previous_is_prose and re.fullmatch(r"[a-z]{2,3}[.,!?;:]?",word)
        and word.rstrip('.,!?;:').lower() not in set(math_words)|{'new','old'}
        and previous_text.strip() and not re.search(r'[.!?:;]["”)]?$',previous_text.strip())
        and .9<=size/previous_size<=1.1 and 0<gap<=previous_size*1.8)


def paragraph_gap_boundary(previous_rows,row):
    """Infer an extra inter-paragraph gap from nearby prose leading."""
    if len(previous_rows)<2 or not any(s['kind']=='prose' for s in row['slots']):return False
    previous=previous_rows[-1]
    if row['page_index']!=previous['page_index'] or row['column']!=previous['column']:return False
    gaps=[]
    for a,b in zip(previous_rows,previous_rows[1:]):
        if a['page_index']!=row['page_index'] or b['page_index']!=row['page_index']:continue
        if any(all(s['kind']=='protected' for s in r['slots']) for r in (a,b)):continue
        if max(a['size'],b['size'])/min(a['size'],b['size'])>1.1:continue
        gap=b['baseline']-a['baseline']
        if a['size']<=gap<=a['size']*1.6:gaps.append(gap)
    if not gaps:return False
    gap=row['baseline']-previous['baseline']
    return gap>median(gaps)*1.25 and gap>=max(previous['size'],row['size'])*1.55


def short_math_list_lead(text,previous_text,**context):
    """Recognize wrapped prose before a list, excluding operators and calls."""
    match=re.fullmatch(r"([a-z]{2,3})\s+([A-Za-z0-9\s,;:{}\[\].]+)",text.strip())
    if not match or re.search(r"[A-Za-z]{2,}",match[2]):return False
    if not re.search(r"[A-Za-z]",match[2]):return False
    return short_prose_continuation(match[1],previous_text,**context)


def number_compound_hyphen(previous,hyphen,next_prose,math_words):
    """A prose-font hyphen attached to a number before a wrapped adjective."""
    word=re.match(r'([a-z]{3,})\b',next_prose)
    if not word or word[1] in math_words:return False
    return bool(previous and previous.text.isdigit() and hyphen.text=='-'
        and hyphen.font in {'URWPalladioL-Roma','URWPalladioL-Bold'} and previous.font==hyphen.font
        and .95<=hyphen.size/previous.size<=1.05
        and abs(hyphen.origin[1]-previous.origin[1])<.6
        and abs(hyphen.origin[0]-(previous.origin[0]+previous.advance*previous.size/1000))<hyphen.size*.18)
