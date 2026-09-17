"""Lock approved multiword terms independently of immutable PDF glyph objects."""
from collections import Counter
import re
from .schemas import TermReplacement, TermLockEvidence
from .term_matching import term_pattern


def protect_terms(source, items):
    if '⟦T:' in source:raise ValueError('source collides with reserved terminology markers')
    matches=[]
    for item in items:
        if item.category!='glossary' or not item.required or not item.term_source or not item.term_target:continue
        for match in term_pattern(item.term_source).finditer(source):matches.append((match.start(),match.end(),item.term_target))
    chosen=[];cursor=0
    for start,end,target in sorted(set(matches),key=lambda x:(x[0],-(x[1]-x[0]),x[2])):
        if start<cursor:continue
        rivals={t for a,b,t in matches if a==start and b==end}
        if len(rivals)>1:raise ValueError('conflicting approved translations for a source term span')
        chosen.append(TermReplacement(marker=f'⟦T:{len(chosen):03d}⟧',source_term=source[start:end],target_term=target,start=start,end=end));cursor=end
    masked=source
    for term in reversed(chosen):masked=masked[:term.start]+term.marker+masked[term.end:]
    return masked,tuple(chosen)


def restore_terms(provider_text, terms):
    actual=Counter(re.findall(r'⟦T:[^⟧]*⟧',provider_text));expected=Counter(t.marker for t in terms)
    if actual!=expected:raise ValueError('terminology marker missing, duplicated, or unknown')
    restored=provider_text
    for term in terms:restored=restored.replace(term.marker,term.target_term)
    if '⟦T:' in restored:raise ValueError('malformed terminology marker remains')
    return restored,TermLockEvidence(provider_text=provider_text,replacements=terms)
