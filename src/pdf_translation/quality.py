from __future__ import annotations

import re

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
