from __future__ import annotations

import re
from collections import defaultdict

from .context_models import SemanticEntry, TermCandidate
from .schemas import TranslationUnit

WORD = re.compile(r"\b[A-Za-z][A-Za-z-]{2,}\b")
URL = re.compile(r"https?://\S+")
STOPWORDS = frozenset({"a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in", "is", "it", "of", "on", "or", "see", "that", "the", "this", "to", "was", "were", "with"})


def extract_term_candidates(units: list[TranslationUnit], minimum_occurrences: int = 2) -> tuple[TermCandidate, ...]:
    occurrences: dict[str, list[str]] = defaultdict(list)
    original: dict[str, str] = {}
    for unit in units:
        text_without_urls = URL.sub(" ", unit.source_text)
        for match in WORD.findall(text_without_urls):
            key = match.casefold()
            if key in STOPWORDS:
                continue
            original.setdefault(key, match)
            occurrences[key].append(unit.unit_id)
    candidates = [TermCandidate(source_term=original[key], occurrence_count=len(ids), source_ids=tuple(dict.fromkeys(ids))) for key, ids in occurrences.items() if len(ids) >= minimum_occurrences]
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
            if term not in result and re.search(rf"(?<![A-Za-z0-9_]){re.escape(term.casefold())}(?![A-Za-z0-9_])", folded):
                result[term] = unit.unit_id
    return result
