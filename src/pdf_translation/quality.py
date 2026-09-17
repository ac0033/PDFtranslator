from __future__ import annotations

from .term_matching import normalize_target_spacing

import re
from collections import Counter

from .hashing import stable_hash
from .context_models import GlossaryEntry
from .context_router import select_glossary
from .schemas import QualityIssue, QualityReport, TranslationResult, TranslationUnit
from .semantic_layout import LAYOUT_MARKER, layout_boundaries_are_natural, is_formula_only

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


def check_quality(unit: TranslationUnit, result: TranslationResult, glossary: list[GlossaryEntry] | None = None) -> QualityReport:
    issues: list[QualityIssue] = []
    translated = result.draft.translated_text if result.draft else ""

    def add(category: str, severity: str, explanation: str) -> None:
        issues.append(QualityIssue(issue_id=f"{unit.unit_id}:{category}:{len(issues)}", category=category, severity=severity, evidence_refs=(unit.unit_id,), explanation=explanation))

    if result.finish != "complete" or not translated.strip():
        add("incomplete", "critical", "translation is absent or provider did not complete")
    source_markers = MARKERS.findall(unit.source_text)
    translated_markers = MARKERS.findall(translated)
    if sorted(source_markers) != sorted(translated_markers):
        add("protected_marker", "critical", "protected marker multiset changed")
    unchanged_formula = translated == unit.source_text and is_formula_only(unit.source_text)
    source_layout_markers = LAYOUT_MARKER.findall(unit.source_text)
    translated_layout_markers = LAYOUT_MARKER.findall(translated)
    if sorted(source_markers) == sorted(translated_markers) and source_markers != translated_markers:
        severity = "critical" if source_layout_markers or "native_immutable_slots" in unit.risk_tags else "major"
        add("protected_marker_order", severity, "protected marker order changed")
    if source_layout_markers != translated_layout_markers:
        add("layout_marker_order", "critical", "layout marker order changed")
    elif source_layout_markers and not unchanged_formula and not layout_boundaries_are_natural(translated):
        add("layout_boundary", "critical", "translated layout boundary remains inside a sentence")
    if sorted(NUMBERS.findall(unit.source_text)) != sorted(NUMBERS.findall(translated)):
        add("number", "major", "number inventory changed")
    if sorted(url_inventory(unit.source_text)) != sorted(url_inventory(translated)):
        add("url", "major", "URL target inventory changed")
    missing_literal_markers = {
        span.marker for span in unit.protected
        if span.kind == "formula" and span.marker not in unit.protected_literal_hints
    }
    if missing_literal_markers and not unchanged_formula:
        add(
            "protected_source_literal_unavailable", "major",
            f"{len(missing_literal_markers)} protected source literals were unavailable to the translator; review nearby meaning against the source PDF",
        )
    for entry in select_glossary(unit, glossary or []):
        if not any(normalize_target_spacing(term) in normalize_target_spacing(translated) for term in (entry.target_term, *entry.accepted_target_variants)):
            add("terminology", "critical", f"Required discipline term missing: {entry.source_term} => {entry.target_term}")
        for forbidden in entry.forbidden_target_terms:
            if normalize_target_spacing(forbidden) in normalize_target_spacing(translated):
                add("terminology", "critical", f"Disallowed translation for {entry.source_term}: {forbidden}; use {entry.target_term}")
    status = "blocked" if any(i.severity == "critical" for i in issues) else ("needs_review" if issues else "pass")
    return QualityReport(unit_id=unit.unit_id, result_hash=stable_hash(result), policy_revision="v0.7-terminology", status=status, issues=tuple(issues), checks_run=("completion", "protected_marker_multiset", "protected_marker_order", "layout_marker_order", "layout_boundary", "number_inventory", "url_inventory", "protected_source_literal_availability", "approved_terminology"))
