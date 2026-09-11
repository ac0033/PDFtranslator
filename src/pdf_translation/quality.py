from __future__ import annotations

import re
from collections import Counter

from .hashing import stable_hash
from .schemas import QualityIssue, QualityReport, TranslationResult, TranslationUnit

MARKERS = re.compile(r"⟦P:[A-Za-z0-9_.:-]+⟧")
# Chinese letters are Unicode "word" characters, so \w boundaries incorrectly
# reject a number touching normal Chinese prose (for example "值为42").
NUMBERS = re.compile(r"(?<![A-Za-z0-9_])[+-]?(?:\d+(?:\.\d+)?%?)(?![A-Za-z0-9_])")
URLS = re.compile(r"https?://[^\s)\]}]+")
URL_TRAILING_PUNCTUATION = ".,;:!?，。；：！？"


def url_inventory(text: str) -> list[str]:
    return [value.rstrip(URL_TRAILING_PUNCTUATION) for value in URLS.findall(text)]


def repair_boundary_markers(source_text: str, translated_text: str) -> tuple[str, bool]:
    """Restore only markers omitted from an exact source boundary.

    A model may treat a leading footnote marker as prompt decoration. We can
    safely restore it without guessing where an interior marker belongs. Any
    unknown, duplicated or interior omission remains blocked by quality QA.
    """
    source_inventory = Counter(MARKERS.findall(source_text))
    translated_inventory = Counter(MARKERS.findall(translated_text))
    if source_inventory == translated_inventory:
        return translated_text, False
    if translated_inventory - source_inventory:
        return translated_text, False

    missing = source_inventory - translated_inventory
    leading: list[str] = []
    cursor = 0
    while match := MARKERS.match(source_text, cursor):
        leading.append(match.group(0))
        cursor = match.end()

    trailing: list[str] = []
    cursor = len(source_text)
    while cursor:
        matches = list(MARKERS.finditer(source_text, 0, cursor))
        if not matches or matches[-1].end() != cursor:
            break
        trailing.insert(0, matches[-1].group(0))
        cursor = matches[-1].start()

    leading_missing: list[str] = []
    remaining = missing.copy()
    for marker in leading:
        if remaining[marker]:
            leading_missing.append(marker)
            remaining[marker] -= 1
    trailing_missing: list[str] = []
    for marker in trailing:
        if remaining[marker]:
            trailing_missing.append(marker)
            remaining[marker] -= 1
    if any(remaining.values()):
        return translated_text, False

    repaired = "".join(leading_missing) + translated_text + "".join(trailing_missing)
    if Counter(MARKERS.findall(repaired)) != source_inventory:
        return translated_text, False
    return repaired, True


def check_quality(unit: TranslationUnit, result: TranslationResult) -> QualityReport:
    issues: list[QualityIssue] = []
    translated = result.draft.translated_text if result.draft else ""

    def add(category: str, severity: str, explanation: str) -> None:
        issues.append(QualityIssue(issue_id=f"{unit.unit_id}:{category}:{len(issues)}", category=category, severity=severity, evidence_refs=(unit.unit_id,), explanation=explanation))

    if result.finish != "complete" or not translated.strip():
        add("incomplete", "critical", "translation is absent or provider did not complete")
    if sorted(MARKERS.findall(unit.source_text)) != sorted(MARKERS.findall(translated)):
        add("protected_marker", "critical", "protected marker multiset changed")
    if sorted(NUMBERS.findall(unit.source_text)) != sorted(NUMBERS.findall(translated)):
        add("number", "major", "number inventory changed")
    if sorted(url_inventory(unit.source_text)) != sorted(url_inventory(translated)):
        add("url", "major", "URL target inventory changed")
    status = "blocked" if any(i.severity == "critical" for i in issues) else ("needs_review" if issues else "pass")
    return QualityReport(unit_id=unit.unit_id, result_hash=stable_hash(result), policy_revision="v0.1", status=status, issues=tuple(issues), checks_run=("completion", "protected_marker_multiset", "number_inventory", "url_inventory"))
