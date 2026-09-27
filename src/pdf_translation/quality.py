from __future__ import annotations

from .term_matching import normalize_target_spacing

import re
from collections import Counter

from .hashing import stable_hash
from .context_models import GlossaryEntry
from .context_router import select_glossary
from .markers import SOURCE_MARKUP_TAG, pairs_are_nested
from .schemas import QualityIssue, QualityReport, TranslationResult, TranslationUnit
from .semantic_layout import LAYOUT_MARKER, layout_boundaries_are_natural, is_formula_only

MARKERS = re.compile(r"⟦P:[A-Za-z0-9_.:-]+⟧")
# Chinese letters are Unicode "word" characters, so \w boundaries incorrectly
# reject a number touching normal Chinese prose (for example "值为42").
NUMBERS = re.compile(r"(?<![A-Za-z0-9_])[+-]?(?:\d+(?:\.\d+)?%?)(?![A-Za-z0-9_])")
URLS = re.compile(r"https?://[^\s)\]}]+")
URL_TRAILING_PUNCTUATION = ".,;:!?，。；：！？"
#: Markup that renders literally when it reaches a page. Triage borrows this
#: to catch what got printed; the gate uses it while a retry can still fix it.
MARKUP_TAG = re.compile(r"</?[A-Za-z][A-Za-z0-9_-]*>")


def url_inventory(text: str) -> list[str]:
    return [value.rstrip(URL_TRAILING_PUNCTUATION) for value in URLS.findall(text)]


def marker_fragments(text: str) -> dict[str, int]:
    """Which layout fragment each protected marker sits in.

    Layout markers are the boundaries between the PDF text boxes a unit is
    written back into, so this is the thing that decides whether a reordered
    marker still lands in the right box.
    """
    fragment = 0
    placed: dict[str, int] = {}
    for match in MARKERS.finditer(text):
        token = match.group(0)
        if LAYOUT_MARKER.fullmatch(token):
            fragment += 1
            continue
        placed.setdefault(token, fragment)
    return placed


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
    if is_formula_only(unit.source_text) and not unchanged_formula:
        # The pipeline passes a formula-only unit through untouched, so text that
        # differs from the source did not come from translating it. This used to
        # be caught only as a side effect of the layout boundary check, which
        # made it invisible the moment that check was relaxed.
        add("formula_slot_changed", "critical",
            "a formula-only unit is passed through unchanged; this text differs from the source")
    source_layout_markers = LAYOUT_MARKER.findall(unit.source_text)
    translated_layout_markers = LAYOUT_MARKER.findall(translated)
    structured_source = SOURCE_MARKUP_TAG in unit.risk_tags
    if structured_source and sorted(source_markers) == sorted(translated_markers) and not pairs_are_nested(translated):
        # Markers are restored by identity, so a moved marker is harmless - except
        # the two halves of wrapping markup, which restore to broken LaTeX/HTML
        # when the closing half comes first or two pairs cross.
        add("paired_marker_nesting", "critical",
            "an opening ⟦P:a…⟧ marker and its closing ⟦P:z…⟧ marker are out of order or cross another pair")
    if (not structured_source and sorted(source_markers) == sorted(translated_markers)
            and source_markers != translated_markers):
        # Reordering only matters critically when a marker leaves the PDF text
        # box it belongs to, which is what the layout markers delimit. Chinese
        # puts the conditional clause first, so a footnote marker routinely
        # moves within its own fragment - correct Chinese that the old rule
        # rejected three times and then abandoned the paragraph over.
        source_fragments = marker_fragments(unit.source_text)
        translated_fragments = marker_fragments(translated)
        crossed = sorted(
            marker for marker, fragment in source_fragments.items()
            if translated_fragments.get(marker) != fragment
        )
        if crossed or "native_immutable_slots" in unit.risk_tags:
            add("protected_marker_order", "critical",
                f"a protected marker moved to a different layout fragment: {' '.join(crossed)}"
                if crossed else "protected marker order changed in immutable native slots")
        else:
            add("protected_marker_order", "major",
                "protected marker order changed within its layout fragment")
    if source_layout_markers != translated_layout_markers:
        add("layout_marker_order", "critical", "layout marker order changed")
    elif source_layout_markers and not unchanged_formula and not layout_boundaries_are_natural(translated):
        # Only when the source could satisfy this itself. A display equation can
        # split an English sentence with no sentence end to break at - "...yields
        # <eq> where..." - and rebalancing has nowhere to move the marker to. The
        # translation then cannot pass a check the source fails, so demanding it
        # burns every retry and loses the unit. Say it was unverifiable instead.
        if layout_boundaries_are_natural(unit.source_text):
            add("layout_boundary", "critical", "translated layout boundary remains inside a sentence")
        else:
            add(
                "layout_boundary_unverifiable", "major",
                "the source's own layout boundary falls inside a sentence, so the translated "
                "boundary cannot be checked against it; compare this fragment with the source PDF",
            )
    introduced_markup = sorted(set(MARKUP_TAG.findall(translated)) - set(MARKUP_TAG.findall(unit.source_text)))
    if introduced_markup:
        # Markup the translation invented renders literally: page 93 of the
        # delivered val book reads "区间STL规范的<term>定义</term>". The detector
        # lived only in triage, which runs after the book is printed, so the gate
        # accepted the unit with no issue at all and it was never retried. It is
        # a structural defect the model can fix on a second attempt, so it is
        # treated like a lost marker rather than left for a human to find.
        add("leaked_markup", "critical",
            f"the translation introduced markup that is not in the source: {' '.join(introduced_markup)}")
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
    return QualityReport(unit_id=unit.unit_id, result_hash=stable_hash(result), policy_revision="v0.7-terminology", status=status, issues=tuple(issues), checks_run=("completion", "protected_marker_multiset", "protected_marker_order", "layout_marker_order", "layout_boundary", "formula_slot_unchanged", "introduced_markup", "number_inventory", "url_inventory", "protected_source_literal_availability", "approved_terminology"))
