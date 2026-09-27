"""Markdown: translate paragraphs, headings, list items, quotes and table cells in place.

Fenced and indented code, front matter, display math and HTML blocks are
copied unchanged. Inside prose, code spans, inline math, autolinks, images,
inline HTML and footnote references are kept whole; links and emphasis become
marker pairs so their wrapped words are translated but the syntax survives.

Accuracy check (:func:`check`): code blocks, code spans, math and link targets
in the output must equal the source's, and the output must have as many
headings, list items and table rows.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from .common import Segment, restore

REVISION = "markdown-source-v1"

FENCE = re.compile(r"^( {0,3})(`{3,}|~{3,})")
HEADING = re.compile(r"^( {0,3}#{1,6}[ \t]+)(.*?)([ \t]+#+[ \t]*)?$")
SETEXT = re.compile(r"^ {0,3}(=+|-+)[ \t]*$")
LIST_ITEM = re.compile(r"^([ \t]*(?:[-*+]|\d{1,9}[.)])[ \t]+(?:\[[ xX]\][ \t]+)?)(.*)$")
QUOTE = re.compile(r"^([ \t]{0,3}>[ \t]?)(.*)$")
TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
HTML_BLOCK = re.compile(r"^ {0,3}<(/?[A-Za-z][A-Za-z0-9-]*|!--)")
RULE = re.compile(r"^ {0,3}([-*_])([ \t]*\1){2,}[ \t]*$")
REFERENCE_DEFINITION = re.compile(r"^ {0,3}\[[^\]]+\]:\s+\S")

INLINE = re.compile(
    r"(?P<code>(`+)[^`]*?\2)"
    r"|(?P<math>\$\$[^$]+\$\$|\$(?=\S)[^$\n]*?\S\$)"
    r"|(?P<image>!\[[^\]]*\]\([^)]*\))"
    r"|(?P<autolink><https?://[^>]+>)"
    r"|(?P<footnote>\[\^[^\]]+\])"
    r"|(?P<link>\[(?P<text>[^\]]+)\](?P<target>\([^)]*\)|\[[^\]]*\]))"
    r"|(?P<html></?[A-Za-z][^>]*>)"
    r"|(?P<strong>(\*\*|__)(?=\S)(?P<strong_text>.+?)(?<=\S)\12)"
    r"|(?P<em>(?<![\w*])([*_])(?=\S)(?P<em_text>[^*_]+?)(?<=\S)\15(?![\w*]))"
    r"|(?P<url>https?://[^\s)>\]]+)"
)


@dataclass
class Piece:
    kind: str  # raw | skip | paragraph | heading | item | quote | cell
    raw: str = ""
    segment: Segment | None = None


@dataclass
class MarkdownDocument:
    text: str
    pieces: list[Piece]

    def segments(self) -> list[Segment]:
        return [piece.segment for piece in self.pieces if piece.segment is not None]


def inline(segment: Segment, text: str) -> None:
    cursor = 0
    for match in INLINE.finditer(text):
        segment.text(text[cursor:match.start()])
        kind = match.lastgroup if match.lastgroup not in {"text", "target", "strong_text", "em_text"} else None
        if match.group("link"):
            segment.open("[")
            inline(segment, match.group("text"))
            segment.close("]" + match.group("target"))
        elif match.group("strong"):
            delimiter = match.group(0)[:2]
            segment.open(delimiter)
            inline(segment, match.group("strong_text"))
            segment.close(delimiter)
        elif match.group("em"):
            delimiter = match.group(0)[0]
            segment.open(delimiter)
            inline(segment, match.group("em_text"))
            segment.close(delimiter)
        elif match.group("math"):
            segment.keep(match.group(0), "formula")
        elif match.group("code"):
            segment.keep(match.group(0), "code")
        elif match.group("url") or match.group("autolink"):
            segment.keep(match.group(0), "url")
        else:
            segment.keep(match.group(0), "other" if kind else "other")
        cursor = match.end()
    segment.text(text[cursor:])


def parse(text: str, prefix: str = "m") -> MarkdownDocument:
    lines = text.splitlines(keepends=True)
    pieces: list[Piece] = []
    counter = [0]
    section: list[str] = []

    def key() -> str:
        counter[0] += 1
        return f"{prefix}:{counter[0]}"

    def raw(value: str, kind: str = "raw") -> None:
        if pieces and pieces[-1].kind == kind:
            pieces[-1].raw += value
        else:
            pieces.append(Piece(kind, raw=value))

    def passage(kind: str, body: str, trailing: str = "") -> None:
        segment = Segment(key(), kind=kind, section=tuple(section[-2:]))
        leading = body[:len(body) - len(body.lstrip())]
        stripped = body.strip()
        end_space = body[len(body.rstrip()):]
        raw(leading)
        inline(segment, stripped)
        pieces.append(Piece(kind, segment=segment))
        raw(end_space + trailing)

    i = 0
    if lines and lines[0].strip() in {"---", "+++"}:
        fence = lines[0].strip()
        for j in range(1, len(lines)):
            if lines[j].strip() == fence:
                raw("".join(lines[:j + 1]), "skip")
                i = j + 1
                break
    previous_blank = True
    while i < len(lines):
        line = lines[i]
        body = line.rstrip("\r\n")
        newline = line[len(body):]
        if not body.strip():
            raw(line)
            previous_blank = True
            i += 1
            continue
        fence = FENCE.match(body)
        if fence:
            marker = fence.group(2)
            j = i + 1
            while j < len(lines) and not lines[j].lstrip().startswith(marker[0] * len(marker)):
                j += 1
            raw("".join(lines[i:j + 1]), "skip")
            i = j + 1
            previous_blank = False
            continue
        if body.strip().startswith("$$"):
            j = i
            if body.strip() == "$$" or not body.strip().endswith("$$") or len(body.strip()) < 4:
                j = i + 1
                while j < len(lines) and "$$" not in lines[j]:
                    j += 1
            raw("".join(lines[i:j + 1]), "skip")
            i = j + 1
            continue
        if previous_blank and (body.startswith("    ") or body.startswith("\t")) and not LIST_ITEM.match(body):
            j = i
            while j < len(lines) and (lines[j].startswith(("    ", "\t")) or not lines[j].strip()):
                j += 1
            raw("".join(lines[i:j]), "skip")
            i = j
            continue
        if HTML_BLOCK.match(body) or RULE.match(body) or REFERENCE_DEFINITION.match(body):
            raw(line, "skip" if HTML_BLOCK.match(body) else "raw")
            i += 1
            previous_blank = False
            continue
        heading = HEADING.match(body)
        if heading:
            raw(heading.group(1))
            passage("heading", heading.group(2), (heading.group(3) or "") + newline)
            section.append(heading.group(2).strip()[:80])
            i += 1
            previous_blank = False
            continue
        if "|" in body and i + 1 < len(lines) and TABLE_SEPARATOR.match(lines[i + 1]):
            j = i
            while j < len(lines) and "|" in lines[j] and lines[j].strip():
                row = lines[j].rstrip("\r\n")
                if TABLE_SEPARATOR.match(row):
                    raw(lines[j])
                else:
                    _table_row(row, raw, passage)
                    raw(lines[j][len(row):])
                j += 1
            i = j
            previous_blank = False
            continue
        item = LIST_ITEM.match(body)
        quote = QUOTE.match(body)
        if item or quote:
            prefix_text, content = (item.group(1), item.group(2)) if item else (quote.group(1), quote.group(2))
            # Continuation lines: indented text that is not a new item or block.
            j = i + 1
            continuation = []
            while j < len(lines):
                nxt = lines[j].rstrip("\r\n")
                if (not nxt.strip() or LIST_ITEM.match(nxt) or FENCE.match(nxt) or HEADING.match(nxt)
                        or (quote and not QUOTE.match(nxt)) or (item and not nxt.startswith((" ", "\t")))):
                    break
                continuation.append(lines[j])
                j += 1
            raw(prefix_text)
            if continuation:
                content_raw = content + newline + "".join(continuation)
                body_text = content_raw.rstrip("\r\n")
                passage("quote" if quote and not item else "item", body_text, content_raw[len(body_text):])
            else:
                passage("quote" if quote and not item else "item", content, newline)
            i = j
            previous_blank = False
            continue
        # Paragraph: consecutive lines up to a blank line or another block.
        j = i + 1
        while j < len(lines):
            nxt = lines[j].rstrip("\r\n")
            if (not nxt.strip() or FENCE.match(nxt) or HEADING.match(nxt) or LIST_ITEM.match(nxt)
                    or QUOTE.match(nxt) or HTML_BLOCK.match(nxt) or nxt.strip().startswith("$$")):
                break
            if SETEXT.match(nxt):
                break
            j += 1
        block = "".join(lines[i:j])
        body_text = block.rstrip("\r\n")
        if j < len(lines) and SETEXT.match(lines[j].rstrip("\r\n")) and j == i + 1:
            passage("heading", body_text, block[len(body_text):])
            raw(lines[j])
            section.append(body_text.strip()[:80])
            i = j + 1
        else:
            passage("paragraph", body_text, block[len(body_text):])
            i = j
        previous_blank = False
    return MarkdownDocument(text, pieces)


def _table_row(row: str, raw, passage) -> None:
    cells = re.split(r"(?<!\\)(\|)", row)
    for part in cells:
        if part == "|":
            raw("|")
        elif part.strip():
            passage("cell", part)
        else:
            raw(part)


def escape(text: str) -> str:
    # Translated prose is plain text; only a leading character that would start a block is neutralised.
    return text.replace("\n", " ")


def render(document: MarkdownDocument, translations: dict[int, str | None], *, bilingual: bool) -> str:
    out = []
    for piece in document.pieces:
        if piece.segment is None:
            out.append(piece.raw)
            continue
        segment = piece.segment
        translated = restore(segment, translations, escape=escape)
        if not bilingual or translations.get(id(segment)) is None:
            out.append(translated)
        elif piece.kind == "paragraph":
            out.append(f"{segment.original()}\n\n{translated}")
        elif piece.kind == "heading":
            out.append(f"{translated} / {segment.original()}")
        else:
            out.append(f"{segment.original()}<br>{translated}")
    return "".join(out)


# --- checking -------------------------------------------------------------------------------

def _inventory(text: str) -> dict[str, Counter]:
    fences = re.findall(r"^( {0,3})(`{3,}|~{3,})[^\n]*\n.*?^\1\2", text, re.M | re.S)
    code_blocks = Counter(m.group(0) for m in re.finditer(r"^ {0,3}(`{3,}|~{3,})[^\n]*\n.*?^ {0,3}\1", text, re.M | re.S))
    body = re.sub(r"^ {0,3}(`{3,}|~{3,})[^\n]*\n.*?^ {0,3}\1", "", text, flags=re.M | re.S)
    return {
        "code_blocks": code_blocks,
        "code_spans": Counter(m.group(0) for m in re.finditer(r"(`+)[^`]*?\1", body)),
        "math": Counter(m.group(0) for m in re.finditer(r"\$\$[^$]+\$\$|\$(?=\S)[^$\n]*?\S\$", body)),
        "links": Counter(m.group(1) for m in re.finditer(r"\]\(([^)]*)\)", body)),
        "headings": Counter({"count": len(re.findall(r"^ {0,3}#{1,6}[ \t]", body, re.M))}),
        "items": Counter({"count": len(re.findall(r"^[ \t]*(?:[-*+]|\d{1,9}[.)])[ \t]+", body, re.M))}),
        "fences": Counter({"count": len(fences)}),
    }


def check(source_text: str, translated_text: str, *, name: str = "document") -> list[dict]:
    before, after = _inventory(source_text), _inventory(translated_text)
    problems = []
    for aspect in before:
        if before[aspect] != after[aspect]:
            problems.append({"kind": f"markdown_{aspect}_changed", "severity": "major",
                             "count": sum((before[aspect] - after[aspect]).values()) + sum((after[aspect] - before[aspect]).values()),
                             "detail": f"{name}: {aspect} differ between the source and the translation"})
    return problems


WORD = re.compile(r"[A-Za-z]{3,}")


def leftover_prose(document: MarkdownDocument) -> dict:
    leftover, translated = [], 0
    for piece in document.pieces:
        if piece.kind == "raw":
            leftover.extend(WORD.findall(re.sub(r"\[[^\]]*\]:\s*\S+|https?://\S+", " ", piece.raw)))
        elif piece.segment is not None:
            translated += len(WORD.findall(piece.segment.plain_text()))
    total = translated + len(leftover)
    return {"leftover_words": len(leftover), "translated_words": translated,
            "share": round(len(leftover) / total, 4) if total else 0.0, "sample": leftover[:20]}
