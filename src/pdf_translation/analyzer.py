from __future__ import annotations

import re
from collections import defaultdict

from .context_models import SemanticEntry, TermCandidate
from .schemas import TranslationUnit
from .term_matching import term_pattern

WORD = re.compile(r"\b[A-Za-z][A-Za-z-]{2,}\b")
URL = re.compile(r"https?://\S+")
STOPWORDS = frozenset({"a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in", "is", "it", "of", "on", "or", "see", "that", "the", "this", "to", "was", "were", "with"})


def extract_term_candidates(units: list[TranslationUnit], minimum_occurrences: int = 2) -> tuple[TermCandidate, ...]:
    occurrences: dict[str, list[str]] = defaultdict(list)
    original: dict[str, str] = {}
    for unit in units:
        text_without_urls = re.sub(r"⟦[PT]:[^⟧]+⟧", lambda m:" "*len(m.group()), URL.sub(lambda m:" "*len(m.group()), unit.source_text))
        for match in WORD.findall(text_without_urls):
            key = match.casefold()
            if key in STOPWORDS:
                continue
            original.setdefault(key, match)
            occurrences[key].append(unit.unit_id)
    # Candidate discovery must include one-off multiword concepts, not only
    # repeated individual tokens. These are evidence-backed candidates, not approvals.
    # Noun heads seed review candidates, including one-off concepts and plurals.
    # They are intentionally not evidence that the phrase has an approved sense.
    singular_heads={'matrix','function','rate','descent','decomposition','distribution',
        'initialization','variance','value','parameter','gradient','method','regression',
        'product','norm','boundary','rule','network','classifier','vector','penalty',
        'entropy','unit','layer','representation','embedding','regularization',
        'recognition','processing','deviation','decay','margin','machine','inference',
        'average','likelihood','probability','covariance'}
    heads=singular_heads | {word+'s' for word in singular_heads} | {'matrices','boundaries','penalties'}
    boundaries=STOPWORDS | {'have','has','had','can','will','would','should','essentially','given','simply','then'}
    phrases=set()
    for unit in units:
        matches=list(WORD.finditer(re.sub(r'⟦[PT]:[^⟧]+⟧',lambda m:' '*len(m.group()),URL.sub(lambda m:' '*len(m.group()),unit.source_text))))
        for end,word in enumerate(matches):
            if word.group().lower() not in heads:continue
            for size in range(2,5):
                group=matches[end-size+1:end+1] if end+1>=size else []
                if len(group)!=size or any(w.group().lower() in boundaries for w in group):continue
                if any(not re.fullmatch(r'[ \t\r\n]+',unit.source_text[a.end():b.start()]) for a,b in zip(group,group[1:])):continue
                phrase=' '.join(w.group() for w in group);key=phrase.casefold()
                original.setdefault(key,phrase);occurrences[key].append(unit.unit_id);phrases.add(key)
    candidates = [TermCandidate(source_term=original[key], occurrence_count=len(ids), source_ids=tuple(dict.fromkeys(ids))) for key, ids in occurrences.items() if len(ids) >= minimum_occurrences or key in phrases]
    return tuple(sorted(candidates, key=lambda item: (-item.occurrence_count, item.source_term.casefold())))


def validate_semantic_evidence(entries: list[SemanticEntry], known_source_ids: set[str]) -> None:
    for entry in entries:
        missing = set(entry.source_ids) - known_source_ids
        if missing:
            raise ValueError(f"semantic entry {entry.semantic_id} has unknown evidence: {sorted(missing)}")
        if entry.status == "approved" and not entry.evidence_text.strip():
            raise ValueError(f"approved semantic entry {entry.semantic_id} has no evidence")


def first_occurrences(units: list[TranslationUnit], terms: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for unit in units:
        folded = unit.source_text.casefold()
        for term in terms:
            if term not in result and term_pattern(term).search(folded):
                result[term] = unit.unit_id
    return result
