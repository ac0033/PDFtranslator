"""Conservatively recover visible source literals hidden by BabelDOC markers.

These strings are translation context only. The actual source PDF objects stay
protected and the translator must still return the original marker verbatim.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from pathlib import Path

import pymupdf

from .schemas import TranslationUnit


MIN_BBOX_CONFIDENCE = 0.85
MAX_LITERAL_CHARS = 16
_MATH_SIGNS = frozenset("=<>≤≥+-×÷∑∏∫√∞≠±∼≈→←↔∈∉∅∂∇∨∧¬⊆⊂⊇⊃∪∩∝∀∃")


def _compact(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"(?<=[^\W\d_]{3})-\s+(?=[^\W\d_]{2})", "", text)
    text = re.sub(r"\s+", "", text)
    # Some math fonts expose the not-equal slash as a separate combining glyph.
    return text.replace("\u0338=", "≠")


def _credible_literal(literal: str) -> bool:
    return bool(literal) and len(literal) <= MAX_LITERAL_CHARS and (
        any(char.isdigit() or char in _MATH_SIGNS for char in literal)
        or len(literal) == 1 and literal.isalpha()
    )


def recover_protected_literal_hints(page: pymupdf.Page, record: dict) -> dict[str, str]:
    """Return marker -> visible native literal only for a unique anchored match.

    Missing or ambiguous hints are omitted; no inferred content is sent to the
    model. A leading marker is accepted only when it is the sole numeric prefix
    on one native line. Other one-sided markers remain unknown.
    """
    marker_map: dict[str, str] = record.get("marker_map") or {}
    if not marker_map or float(record.get("bbox_confidence", 0.0)) < MIN_BBOX_CONFIDENCE:
        return {}
    source = record.get("babeldoc_text", "")
    tokens = tuple(marker_map.values())
    if len(set(tokens)) != len(tokens) or any(source.count(token) != 1 for token in tokens):
        return {}
    splitter = re.compile("(" + "|".join(re.escape(token) for token in tokens) + ")")
    parts = splitter.split(source)
    if len(parts) != len(tokens) * 2 + 1 or parts[1::2] != list(tokens):
        return {}
    anchors = [_compact(part) for part in parts[::2]]
    native = _compact(page.get_textbox(pymupdf.Rect(record["bbox"])))
    gap = f"(.{{1,{MAX_LITERAL_CHARS}}}?)"
    by_token: dict[str, str] = {}
    # A short final punctuation anchor is acceptable when the entire preceding
    # multi-placeholder sentence matches uniquely, not in an isolated search.
    if len(anchors[0]) >= 3 and len(anchors[-1]) >= 1 and all(len(anchor) >= 3 for anchor in anchors[1:-1]):
        pattern = gap.join(re.escape(anchor) for anchor in anchors)
        matches = list(re.finditer(pattern, native, flags=re.IGNORECASE))
        if len(matches) == 1 and all(_credible_literal(value) for value in matches[0].groups()):
            by_token.update(zip(tokens, matches[0].groups(), strict=True))

    # When one marker is not bounded, recover other markers only if their own
    # long adjacent text anchors uniquely determine a short native gap.
    for index, token in enumerate(tokens):
        if token in by_token or len(anchors[index]) < 8 or len(anchors[index + 1]) < 8:
            continue
        left = anchors[index][-32:]
        right = anchors[index + 1][:32]
        local = list(re.finditer(re.escape(left) + gap + re.escape(right), native, flags=re.IGNORECASE))
        if len(local) == 1 and _credible_literal(local[0].group(1)):
            by_token[token] = local[0].group(1)

    # A leading footnote marker may be recoverable as the only number before
    # the first prose words on one native PDF line. Other prefixes stay unknown.
    if tokens and tokens[0] not in by_token and not anchors[0] and len(anchors[1]) >= 12:
        prefix_anchor = anchors[1][:16]
        line_prefixes = []
        for block in page.get_text("dict", clip=pymupdf.Rect(record["bbox"]))["blocks"]:
            for line in block.get("lines", []):
                line_text = _compact("".join(span["text"] for span in line["spans"]))
                position = line_text.casefold().find(prefix_anchor.casefold())
                if position >= 0:
                    line_prefixes.append(line_text[:position])
        if len(line_prefixes) == 1 and line_prefixes[0].isdigit() and len(line_prefixes[0]) <= 3:
            by_token[tokens[0]] = line_prefixes[0]
    return {marker: by_token[token] for marker, token in marker_map.items() if token in by_token}


def enrich_units_with_protected_literals(
    source: Path,
    units: list[TranslationUnit],
    records_by_page: dict[int, list[dict]],
) -> list[TranslationUnit]:
    """Attach only verifiable source literals; preserve frozen capture files on reuse."""
    hints_by_unit: dict[str, dict[str, str]] = defaultdict(dict)
    with pymupdf.open(source) as document:
        for page_index, records in records_by_page.items():
            page = document[page_index]
            for record in records:
                hints = recover_protected_literal_hints(page, record)
                record["protected_literal_hints"] = hints
                unit_hints = hints_by_unit[record["unit_id"]]
                if any(marker in unit_hints and unit_hints[marker] != literal for marker, literal in hints.items()):
                    raise ValueError("conflicting native literals for one protected marker")
                unit_hints.update(hints)
    enriched = []
    for unit in units:
        hints = hints_by_unit.get(unit.unit_id, {})
        declared = {span.marker for span in unit.protected}
        if not set(hints) <= declared:
            raise ValueError("recovered hint has no protected marker in its translation unit")
        enriched.append(unit.model_copy(update={"protected_literal_hints": hints}))
    return enriched
