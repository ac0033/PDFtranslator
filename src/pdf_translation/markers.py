"""Protected markers used by the structured source formats (LaTeX, HTML, Markdown, DOCX, EPUB).

A unit cut from a structured source replaces every piece of markup it must not
lose with a marker the model copies through. Two kinds exist:

* ``⟦P:k<n>⟧`` - one opaque piece (a formula, a citation, a code span, a lone
  brace). The program restores it by identity, so the model may move it to
  wherever Chinese word order puts it.
* ``⟦P:a<n>⟧ ... ⟦P:z<n>⟧`` - the opening and closing half of markup that wraps
  words (``\\emph{...}``, ``<a href>...</a>``, a bold run). Moving these is fine
  too, but only as a properly nested pair: a closing half before its opening
  half would restore to broken markup.

Units carry the ``source_markup`` risk tag, which tells the gate that order
alone is not a defect here (restoration is by identity, not by position) and
tells the translator how to treat the two kinds.
"""
from __future__ import annotations

import re

SOURCE_MARKUP_TAG = "source_markup"
PAIR_MARKER = re.compile(r"⟦P:([az])(\d+)⟧")


def pairs_are_nested(text: str) -> bool:
    """True when every ``a<n>``/``z<n>`` pair opens before it closes and pairs do not cross."""
    stack: list[str] = []
    for match in PAIR_MARKER.finditer(text):
        kind, number = match.groups()
        if kind == "a":
            stack.append(number)
        elif not stack or stack.pop() != number:
            return False
    return not stack
