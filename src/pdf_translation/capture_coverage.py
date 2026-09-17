"""Find source text that never became a translation unit.

Every other check in this project reasons about units. Text the capture layer
never handed over has no unit, so nothing downstream can see it: it is not
blocked, not flagged, not queued for review. It simply renders in the source
language and the report says the book is fine. On val.pdf four truth-table
cells on page 105 were lost that way, and a human found them by reading.

The reconciliation is a second opinion. pymupdf extracts the page independently
of BabelDOC, and whatever the first extractor saw but the second never
delivered is a capture gap. Applied to the delivered val.pdf this recovers the
known page-105 cells and two larger gaps nobody had noticed - 27 cells on page
88 and 8 on page 61.

Three classes of text are excluded, because the pipeline preserves them on
purpose and reporting them would bury the real gaps under thousands of hits:

* **Code**, identified by a monospace font. val's Julia listings alone account
  for most of the raw difference.
* **Formula glyphs**, identified by a mathematical font.
* **Operator names**, the standard LaTeX set (``\\log``, ``\\arg``, ``\\max`` …)
  plus the few words that appear in ``\\text{}`` inside a cases environment.
  These are set in the body font but belong to the formula.

What is left is prose: table cells, figure labels, stray sentences. On val that
is 139 words out of 92,076 (0.15%), on 50 pages, 13 of them with three or more.

Scope: alphabetic words of three or more characters. Digits are excluded - in a
technical book most of them belong to formulas, and the quality gate already
compares number inventories inside the units that do exist. A word the capture
merged into its neighbour (``of boolean`` -> ``ofboolean``) is present, just not
tokenised, and is not reported.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

import pymupdf

WORD = re.compile(r"[A-Za-z]{3,}")
CODE_FONT = re.compile(r"mono|courier|consol|typewriter|cmtt|inconsolata", re.I)
MATH_FONT = re.compile(r"^(?:cm(?:sy|mi|ex|r|b|bx|ti)|msam|msbm|eur|rsfs|stmary|wasy)|math|pazo", re.I)

#: Standard LaTeX operator names, plus the words that turn up in \text{} inside
#: a cases environment. Set in the body font, but part of the formula.
MATH_OPERATOR_WORDS = frozenset("""
arccos arcsin arctan arg cos cosh cot coth csc deg det dim exp gcd hom inf ker
lim liminf limsup log max min pmod sec sin sinh sup tan tanh
otherwise subject else then where such that minimize maximize
""".split())

#: A page needs at least this many uncaptured words before it is worth a look.
#: Below it the hits are almost always one stray glyph in a diagram.
DEFAULT_SIGNIFICANCE_FLOOR = 3

#: Share of a document's prose that may go uncaptured. Beyond this the book has
#: untranslated content in it, which AGENTS.md section 2 calls a major problem.
DEFAULT_UNCAPTURED_SHARE_THRESHOLD = 0.01

SAMPLE_WORDS = 8


def is_prose_font(font: str) -> bool:
    """Is this span body text, rather than code or a formula?

    An unrecognised or missing font name counts as prose. Excluding what we
    cannot identify would hide exactly the gaps this check exists to find.
    """
    return not (CODE_FONT.search(font or "") or MATH_FONT.search(font or ""))


def page_prose_words(page) -> Counter:
    """Count the body-text words on one page, excluding operator names."""
    words: Counter = Counter()
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                if not is_prose_font(span.get("font", "")):
                    continue
                for word in WORD.findall(span.get("text", "")):
                    key = word.casefold()
                    if key not in MATH_OPERATOR_WORDS:
                        words[key] += 1
    return words


def uncaptured_words(source_words, captured_text: str) -> dict[str, int]:
    """Words the page shows that the capture never delivered.

    A word the capture ran together with its neighbour still reached the
    translator, so it is matched against the whitespace-stripped text too.
    """
    source_words = Counter(source_words)
    captured = Counter(word.casefold() for word in WORD.findall(captured_text))
    glued = re.sub(r"[^a-z]", "", captured_text.casefold())
    return {word: count for word, count in (source_words - captured).items() if word not in glued}


def load_capture_map(capture_map: Path) -> dict[int, list[str]]:
    """Group the text BabelDOC handed over by source page."""
    by_page: dict[int, list[str]] = {}
    for line in Path(capture_map).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        by_page.setdefault(record["page_index"], []).append(record.get("babeldoc_text") or "")
    return by_page


def reconcile_capture(
    source: Path,
    capture_map: Path,
    *,
    significance_floor: int = DEFAULT_SIGNIFICANCE_FLOOR,
    threshold: float = DEFAULT_UNCAPTURED_SHARE_THRESHOLD,
    page_indices: tuple[int, ...] | None = None,
) -> dict:
    """Compare what the page shows against what the capture delivered.

    ``page_indices`` limits the reconciliation to pages the capture actually
    covered. Without it a sampled capture would report every unsampled page as
    wholly uncaptured, which is true but useless.
    """
    by_page = load_capture_map(capture_map)
    pages: list[dict] = []
    total_prose = 0
    total_uncaptured = 0
    with pymupdf.open(source) as document:
        page_count = len(document)
        scope_indices = tuple(range(page_count)) if page_indices is None else tuple(page_indices)
        for index in scope_indices:
            source_words = page_prose_words(document[index])
            captured_text = " ".join(by_page.get(index, []))
            missing = uncaptured_words(source_words, captured_text)
            prose_total = sum(source_words.values())
            missing_total = sum(missing.values())
            total_prose += prose_total
            total_uncaptured += missing_total
            if missing_total:
                pages.append({
                    "page": index + 1,
                    "prose_words": prose_total,
                    "uncaptured_words": missing_total,
                    "share": round(missing_total / prose_total, 4) if prose_total else 1.0,
                    "words": dict(Counter(missing).most_common(SAMPLE_WORDS)),
                })

    share = total_uncaptured / total_prose if total_prose else 0.0
    significant = [page for page in pages if page["uncaptured_words"] >= significance_floor]
    significant.sort(key=lambda page: -page["uncaptured_words"])
    return {
        "source": str(source),
        "page_count": page_count,
        "pages_reconciled": len(scope_indices),
        "prose_words": total_prose,
        "uncaptured_words": total_uncaptured,
        "uncaptured_share": round(share, 5),
        "threshold": threshold,
        "over_threshold": share > threshold,
        "pages_with_uncaptured": len(pages),
        "significance_floor": significance_floor,
        "significant_pages": significant,
        # When no single page clears the floor but the document as a whole is
        # losing text, a reader still needs somewhere to start looking.
        "sample_pages": [page["page"] for page in (significant or pages)][:SAMPLE_WORDS],
        "scope": (
            "Alphabetic words of three or more characters in body-text fonts. Code (monospace "
            "fonts), formula glyphs (mathematical fonts) and standard LaTeX operator names are "
            "excluded because the pipeline preserves them deliberately. Digits are excluded. "
            "A word the capture merged into its neighbour is treated as captured. This measures "
            "what never became a translation unit; it says nothing about whether the units that "
            "do exist were translated correctly."
        ),
    }
