"""What every structured-format adapter shares: segments, markers, units, restoration.

An adapter reads its source into :class:`Segment` objects. A segment is one
translatable passage (a paragraph, a heading, a caption, a table cell, an
``alt`` text) written as a sequence of parts:

* plain text, which is translated;
* ``keep`` parts - markup the translation must not change, each becoming one
  ``⟦P:k<n>⟧`` marker;
* ``open``/``close`` parts - the two halves of markup that wraps words, which
  become an ``⟦P:a<n>⟧``/``⟦P:z<n>⟧`` pair;
* ``child`` parts - a nested passage translated as its own unit (a footnote
  inside a paragraph) and spliced back in between fixed opening and closing
  markup.

:func:`build_units` turns segments into ordinary translation units, and
:func:`restore` turns a translation back into source markup, escaping the
translated text for the target format and putting every marker's original
bytes back.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from ..hashing import sha256_bytes, stable_hash
from ..markers import SOURCE_MARKUP_TAG
from ..quality import MARKERS
from ..schemas import ArtifactRef, ProtectedSpan, SourceRef, TranslationUnit

#: A passage needs translating only if it holds a word of two or more Latin letters.
PROSE = re.compile(r"[A-Za-z]{2,}")
#: Hint text the translator sees for a marker is cut at this length.
HINT_LIMIT = 300
WHITESPACE = re.compile(r"\s+")


@dataclass
class Part:
    kind: str  # text | keep | open | close | child
    value: str = ""
    marker_kind: str = "other"  # formula | code | symbol | url | citation | other
    child: "Segment | None" = None
    closing: str = ""  # for child parts: markup after the nested passage
    #: For text parts: the exact source bytes (with escapes, entities, line breaks) the value came from.
    raw: str | None = None


@dataclass
class Segment:
    """One translatable passage and where it came from."""

    key: str
    kind: str = "paragraph"
    parts: list[Part] = field(default_factory=list)
    section: tuple[str, ...] = ()
    #: Collapse runs of whitespace in the text parts (LaTeX and HTML source line breaks).
    fold_whitespace: bool = True

    # --- building -------------------------------------------------------------------

    def text(self, value: str, raw: str | None = None) -> None:
        """Add translatable text; ``raw`` is its exact source form when that differs (escapes, entities)."""
        if not value and not raw:
            return
        raw = value if raw is None else raw
        if self.parts and self.parts[-1].kind == "text":
            self.parts[-1].value += value
            self.parts[-1].raw += raw
        else:
            self.parts.append(Part("text", value, raw=raw))

    def keep(self, value: str, marker_kind: str = "other") -> None:
        if value:
            self.parts.append(Part("keep", value, marker_kind))

    def open(self, value: str) -> None:
        self.parts.append(Part("open", value))

    def close(self, value: str) -> None:
        self.parts.append(Part("close", value))

    def nest(self, opening: str, child: "Segment", closing: str) -> None:
        self.parts.append(Part("child", opening, child=child, closing=closing))

    # --- reading --------------------------------------------------------------------

    def plain_text(self) -> str:
        return "".join(part.value for part in self.parts if part.kind == "text")

    def has_prose(self) -> bool:
        return bool(PROSE.search(self.plain_text()))

    def children(self) -> list["Segment"]:
        found = []
        for part in self.parts:
            if part.kind == "child" and part.child is not None:
                found.append(part.child)
                found.extend(part.child.children())
        return found

    def original(self) -> str:
        """The passage's exact source bytes, rebuilt from its parts."""
        out = []
        for part in self.parts:
            if part.kind == "text":
                out.append(part.raw)
            elif part.kind == "child":
                out.append(part.value + part.child.original() + part.closing)
            else:
                out.append(part.value)
        return "".join(out)

    def marked(self) -> tuple[str, dict[str, Part]]:
        """The text the translator sees, and each marker's part."""
        text, markers = [], {}
        pairs: list[str] = []
        counter = 0
        for part in self.parts:
            if part.kind == "text":
                value = part.value
                if self.fold_whitespace:
                    value = WHITESPACE.sub(" ", value)
                text.append(value)
                continue
            counter += 1
            if part.kind == "open":
                marker = f"⟦P:a{counter}⟧"
                pairs.append(str(counter))
            elif part.kind == "close":
                number = pairs.pop() if pairs else str(counter)
                marker = f"⟦P:z{number}⟧"
            else:
                marker = f"⟦P:k{counter}⟧"
            markers[marker] = part
            text.append(marker)
        # An unmatched opening half (malformed source) is still restored by identity.
        return "".join(text).strip(), markers


def build_units(document_id: str, segments: list[Segment], *, revision: str) -> tuple[list[TranslationUnit], dict[str, Segment]]:
    """Units for every segment (and nested segment) that holds prose, in reading order."""
    ordered: list[Segment] = []
    for segment in segments:
        ordered.append(segment)
        ordered.extend(segment.children())
    translatable = [segment for segment in ordered if segment.has_prose()]
    units, by_unit = [], {}
    ids = [f"u{index:05d}" for index in range(1, len(translatable) + 1)]
    for index, (unit_id, segment) in enumerate(zip(ids, translatable)):
        source_text, markers = segment.marked()
        protected, hints = [], {}
        for marker, part in markers.items():
            literal = part.value + ("…" + part.closing if part.kind == "child" else "")
            protected.append(ProtectedSpan(
                occurrence_id=f"{unit_id}:{marker}", marker=marker,
                kind=part.marker_kind if part.kind == "keep" else "other",
                original=ArtifactRef(uri=f"source:{segment.key}", sha256=sha256_bytes(literal.encode("utf-8")),
                                     media_type="text/x-source-markup"),
                source_ref=SourceRef(block_id=segment.key, page_index=0, bbox=(0.0, 0.0, 0.0, 0.0),
                                     char_start=0, char_end=0)))
            hints[marker] = literal if len(literal) <= HINT_LIMIT else literal[:HINT_LIMIT] + "…"
        neighbours = tuple(ids[position] for position in (index - 1, index + 1) if 0 <= position < len(ids))
        units.append(TranslationUnit(
            unit_id=unit_id, document_id=document_id, ir_revision=revision, unit_type=segment.kind,
            source_lang="en", target_lang="zh-CN", source_text=source_text, source_hash=stable_hash(source_text),
            section_path=segment.section, protected=tuple(protected), protected_literal_hints=hints,
            neighbor_ids=neighbours, risk_tags=(SOURCE_MARKUP_TAG,)))
        by_unit[unit_id] = segment
    return units, by_unit


def restore(segment: Segment, translations: dict[int, str | None], *, escape: Callable[[str], str]) -> str:
    """Source markup for ``segment`` with its translation (or its original when there is none).

    ``translations`` maps ``id(segment)`` to the translated text with markers.
    A translation that lost or invented a marker cannot be restored faithfully,
    so the original passage is written instead; acceptance still sees the unit
    as untranslated through the run store.
    """
    translated = translations.get(id(segment))
    if not translated:
        return _original_with_children(segment, translations, escape)
    _, markers = segment.marked()
    found = MARKERS.findall(translated)
    if sorted(found) != sorted(markers):
        return _original_with_children(segment, translations, escape)
    out, cursor = [], 0
    for match in MARKERS.finditer(translated):
        out.append(escape(translated[cursor:match.start()]))
        part = markers[match.group(0)]
        if part.kind == "child":
            out.append(part.value + restore(part.child, translations, escape=escape) + part.closing)
        else:
            out.append(part.value)
        cursor = match.end()
    out.append(escape(translated[cursor:]))
    return "".join(out)


def _original_with_children(segment: Segment, translations: dict[int, str | None], escape) -> str:
    """The passage's own source, with any nested passage still translated where it can be."""
    out = []
    for part in segment.parts:
        if part.kind == "text":
            out.append(part.raw)
        elif part.kind == "child":
            out.append(part.value + restore(part.child, translations, escape=escape) + part.closing)
        else:
            out.append(part.value)
    return "".join(out)


def document_id_for(data: bytes) -> str:
    return "doc-" + sha256_bytes(data)[:16]
