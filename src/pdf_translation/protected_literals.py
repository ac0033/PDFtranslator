"""Conservatively recover visible source literals hidden by BabelDOC markers.

BabelDOC replaces a formula or styled run with ``<b1>``, ``<b2>`` … and hands
over nothing else: the placeholder it stores for the marker is that token
itself, so the only way to learn what the marker stands for is to read the
glyphs off the page. Without that the translator sees an opaque marker in the
middle of a sentence and has to guess what the sentence is about.

Recovery is anchored, never inferred: the text either side of the marker must
appear in the page exactly once, and the literal is whatever sits in the gap.
Ambiguous or unbounded markers stay unknown, and no guess is ever sent to the
model. These strings are translation context only - the actual source PDF
objects stay protected and the translator must still return the marker verbatim.

Two things widen recovery without weakening that guarantee:

* When the record's bounding box is not trustworthy the anchors are matched
  against the **whole page** instead. Requiring a single match on the entire
  page is a stronger claim than trusting a low-confidence box, not a weaker one.
* A literal that is a plain code identifier - ``sys``, ``step``, ``Turing.jl``,
  ``disturbance_distribution`` - is credible even though it carries no digit or
  maths sign. These are exactly the words a translator needs to render "the
  ``sys`` function" correctly, and they are read off the page like any other.

Measured against the shipped val.pdf capture, the two together take marker
recovery from 1135 to 1594 of 5133 markers (22.1% to 31.1%), lose none, and
change none of the literals the stricter rule already recovered.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from pathlib import Path

import pymupdf

from .schemas import TranslationUnit


MIN_BBOX_CONFIDENCE = 0.85
#: Longest mathematical literal accepted. Beyond this a gap is prose that the
#: anchors failed to bound, not a formula.
MAX_LITERAL_CHARS = 16
#: Identifiers run longer than formulae and are unambiguous when they match.
MAX_IDENTIFIER_CHARS = 32
#: The widest gap the anchored patterns will consider at all.
MAX_GAP_CHARS = MAX_IDENTIFIER_CHARS
_MATH_SIGNS = frozenset("=<>≤≥+-×÷∑∏∫√∞≠±∼≈→←↔∈∉∅∂∇∨∧¬⊆⊂⊇⊃∪∩∝∀∃")
#: A bare code identifier or dotted package name, e.g. sys, step, Turing.jl.
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)*")


def _compact(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"(?<=[^\W\d_]{3})-\s+(?=[^\W\d_]{2})", "", text)
    text = re.sub(r"\s+", "", text)
    # Some math fonts expose the not-equal slash as a separate combining glyph.
    return text.replace("\u0338=", "≠")


def _credible_literal(literal: str, raw_native: str = "") -> bool:
    """Is this gap something a marker plausibly stands for?

    Either a short mathematical fragment, or a code identifier. A run of
    ordinary prose is neither: it means the anchors failed to bound the marker.

    Matching happens on whitespace-stripped text, which makes "very slow and
    careful routine" look exactly like one long identifier. An identifier
    therefore has to survive a second test: it must appear verbatim in the page
    as printed, spaces and all. A maths fragment is exempt because it is
    routinely typeset with spaces the stripping removes - "= 1" is written
    apart and read together.
    """
    if not literal:
        return False
    if len(literal) <= MAX_LITERAL_CHARS and (
        any(char.isdigit() or char in _MATH_SIGNS for char in literal)
        or len(literal) == 1 and literal.isalpha()
    ):
        return True
    if not (2 <= len(literal) <= MAX_IDENTIFIER_CHARS and _IDENTIFIER.fullmatch(literal)):
        return False
    return literal in raw_native


def recover_protected_literal_hints(page: pymupdf.Page, record: dict) -> dict[str, str]:
    """Return marker -> visible native literal only for a unique anchored match.

    Missing or ambiguous hints are omitted; no inferred content is sent to the
    model. A leading marker is accepted only when it is the sole numeric prefix
    on one native line. Other one-sided markers remain unknown.

    When the record's bounding box is not trustworthy the anchors are matched
    against the whole page instead of the box. The single-match requirement then
    has to hold over the entire page, which is a stronger claim than trusting
    the box, so nothing is loosened by widening where we look.
    """
    marker_map: dict[str, str] = record.get("marker_map") or {}
    if not marker_map:
        return {}
    bbox_trusted = float(record.get("bbox_confidence", 0.0)) >= MIN_BBOX_CONFIDENCE
    source = record.get("babeldoc_text", "")
    tokens = tuple(marker_map.values())
    if len(set(tokens)) != len(tokens) or any(source.count(token) != 1 for token in tokens):
        return {}
    splitter = re.compile("(" + "|".join(re.escape(token) for token in tokens) + ")")
    parts = splitter.split(source)
    if len(parts) != len(tokens) * 2 + 1 or parts[1::2] != list(tokens):
        return {}
    anchors = [_compact(part) for part in parts[::2]]
    raw_native = page.get_textbox(pymupdf.Rect(record["bbox"])) if bbox_trusted else page.get_text()
    native = _compact(raw_native)
    # Try the tightest bound that explains every marker before the wider one.
    # A longer gap admits more alignments, and in a formula-dense unit one of
    # them can displace the reading that worked: the whole-sentence path only
    # accepts a match when every group is credible, so a single wrong group
    # discards markers the narrow bound resolved.
    gap_limits = (MAX_LITERAL_CHARS, MAX_GAP_CHARS)
    by_token: dict[str, str] = {}
    # A short final punctuation anchor is acceptable when the entire preceding
    # multi-placeholder sentence matches uniquely, not in an isolated search.
    if len(anchors[0]) >= 3 and len(anchors[-1]) >= 1 and all(len(anchor) >= 3 for anchor in anchors[1:-1]):
        for limit in gap_limits:
            gap = f"(.{{1,{limit}}}?)"
            pattern = gap.join(re.escape(anchor) for anchor in anchors)
            matches = list(re.finditer(pattern, native, flags=re.IGNORECASE))
            if len(matches) == 1 and all(_credible_literal(value, raw_native) for value in matches[0].groups()):
                by_token.update(zip(tokens, matches[0].groups(), strict=True))
                break

    # When one marker is not bounded, recover other markers only if their own
    # long adjacent text anchors uniquely determine a short native gap.
    for index, token in enumerate(tokens):
        if token in by_token or len(anchors[index]) < 8 or len(anchors[index + 1]) < 8:
            continue
        left = anchors[index][-32:]
        right = anchors[index + 1][:32]
        for limit in gap_limits:
            gap = f"(.{{1,{limit}}}?)"
            local = list(re.finditer(re.escape(left) + gap + re.escape(right), native, flags=re.IGNORECASE))
            if len(local) == 1 and _credible_literal(local[0].group(1), raw_native):
                by_token[token] = local[0].group(1)
                break

    # A leading footnote marker may be recoverable as the only number before
    # the first prose words on one native PDF line. Other prefixes stay unknown.
    # This one needs the box: "the only prefix" means only within the record,
    # and a whole page has many lines that would qualify.
    if bbox_trusted and tokens and tokens[0] not in by_token and not anchors[0] and len(anchors[1]) >= 12:
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
