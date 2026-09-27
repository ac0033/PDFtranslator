"""HTML (and the XHTML inside EPUB): translate text nodes in place.

The document is read as a stream of tokens whose concatenation is exactly the
source, so everything that is not translated is copied byte for byte. Prose is
the text between block boundaries (``p``, ``li``, ``td``, headings, ...);
inline elements inside it become marker pairs, and code, math, scripts and
styles are kept whole. ``alt``, ``title``, ``placeholder``, ``aria-label`` and
the page description are translated as their own units.

Accuracy check (:func:`check`): the output must hold the same elements with
the same attributes (other than the translated ones and ``lang``) and the same
code, math, script and style content as the source.
"""
from __future__ import annotations

import html
import re
from collections import Counter
from dataclasses import dataclass, field
from html.parser import HTMLParser

from .common import Segment, restore

REVISION = "html-source-v1"

BLOCK = {"html", "head", "body", "p", "div", "section", "article", "header", "footer", "nav", "aside", "main",
         "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "dl", "dt", "dd", "table", "thead", "tbody", "tfoot",
         "tr", "td", "th", "caption", "figure", "figcaption", "blockquote", "form", "fieldset", "legend", "details",
         "summary", "title", "option", "button", "hr", "address", "center", "menu", "hgroup", "search", "dialog",
         "colgroup", "col", "meta", "link", "base", "body", "frameset", "iframe", "video", "audio", "object",
         "select", "optgroup", "label", "br_block"}
#: Elements kept whole with everything inside: never prose.
OPAQUE = {"script", "style", "pre", "code", "kbd", "samp", "var", "math", "svg", "textarea", "template",
          "noscript", "canvas", "object", "iframe", "video", "audio", "tt"}
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr",
        "param", "keygen"}
HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6", "title"}
TEXT_ONLY = {"title", "option", "button", "summary", "legend", "label"}
TRANSLATED_ATTRIBUTES = ("alt", "title", "placeholder", "aria-label")
PROSE = re.compile(r"[A-Za-z]{2,}")


@dataclass
class Token:
    kind: str  # text | entity | start | end | startend | comment | decl | pi
    raw: str
    tag: str = ""
    attrs: list[tuple[str, str | None]] = field(default_factory=list)


class _Tokenizer(HTMLParser):
    def __init__(self, text: str):
        super().__init__(convert_charrefs=False)
        self.source = text
        self.starts: list[tuple[int, str, str, list]] = []
        lines = text.split("\n")
        self.line_offsets = [0]
        for line in lines[:-1]:
            self.line_offsets.append(self.line_offsets[-1] + len(line) + 1)

    def _offset(self) -> int:
        line, column = self.getpos()
        return self.line_offsets[line - 1] + column

    def _add(self, kind: str, tag: str = "", attrs=None) -> None:
        self.starts.append((self._offset(), kind, tag, attrs or []))

    def handle_starttag(self, tag, attrs):
        self._add("start", tag, attrs)

    def handle_startendtag(self, tag, attrs):
        self._add("startend", tag, attrs)

    def handle_endtag(self, tag):
        self._add("end", tag)

    def handle_data(self, data):
        self._add("text")

    def handle_entityref(self, name):
        self._add("entity")

    def handle_charref(self, name):
        self._add("entity")

    def handle_comment(self, data):
        self._add("comment")

    def handle_decl(self, decl):
        self._add("decl")

    def handle_pi(self, data):
        self._add("pi")

    def unknown_decl(self, data):
        self._add("decl")


def tokenize(text: str) -> list[Token]:
    """Tokens whose raw strings concatenate back to ``text`` exactly."""
    parser = _Tokenizer(text)
    parser.feed(text)
    parser.close()
    tokens: list[Token] = []
    starts = parser.starts
    covered = 0
    for index, (offset, kind, tag, attrs) in enumerate(starts):
        end = starts[index + 1][0] if index + 1 < len(starts) else len(text)
        if offset > covered:  # anything the parser skipped silently stays in the output
            tokens.append(Token("text", text[covered:offset]))
        if end <= offset:
            continue
        tokens.append(Token(kind, text[offset:end], tag, attrs))
        covered = end
    if covered < len(text):
        tokens.append(Token("text", text[covered:]))
    return tokens


@dataclass
class Piece:
    kind: str  # raw | skip | block | attribute
    raw: str = ""
    segment: Segment | None = None
    tag: str = ""


@dataclass
class HtmlDocument:
    text: str
    pieces: list[Piece]
    attributes: list[tuple[str, str, Segment]]  # (raw start tag, attribute name, segment)

    def segments(self) -> list[Segment]:
        blocks = [piece.segment for piece in self.pieces if piece.segment is not None]
        return blocks + [segment for _, _, segment in self.attributes]


def parse(text: str, prefix: str = "h") -> HtmlDocument:
    tokens = tokenize(text)
    pieces: list[Piece] = []
    attributes: list[tuple[str, str, Segment]] = []
    counter = [0]
    stack: list[str] = []
    current: list[Segment | None] = [None]
    block_tag = [""]
    section: list[str] = []

    def key() -> str:
        counter[0] += 1
        return f"{prefix}:{counter[0]}"

    def raw(value: str, kind: str = "raw") -> None:
        if pieces and pieces[-1].kind == kind:
            pieces[-1].raw += value
        else:
            pieces.append(Piece(kind, raw=value))

    def flush() -> None:
        segment = current[0]
        current[0] = None
        if segment is None:
            return
        # Whitespace around a passage is layout; keep it outside the translated span.
        pieces.append(Piece("block", segment=segment, tag=block_tag[0]))

    def ensure() -> Segment:
        if current[0] is None:
            kind = "heading" if block_tag[0] in HEADINGS else ("table_cell" if block_tag[0] in {"td", "th"} else "paragraph")
            current[0] = Segment(key(), kind=kind, section=tuple(section[-2:]))
        return current[0]

    def attribute_segments(token: Token) -> None:
        for name, value in token.attrs:
            if value and name in TRANSLATED_ATTRIBUTES and PROSE.search(value):
                segment = Segment(key(), kind="attribute")
                segment.text(value, raw=html.escape(value, quote=True))
                attributes.append((token.raw, name, segment))
        if token.tag == "meta":
            attrs = dict(token.attrs)
            if (attrs.get("name") or "").lower() in {"description", "og:description"} and attrs.get("content"):
                segment = Segment(key(), kind="attribute")
                segment.text(attrs["content"], raw=html.escape(attrs["content"], quote=True))
                attributes.append((token.raw, "content", segment))

    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token.kind in {"start", "startend"} and token.tag in OPAQUE and token.kind == "start":
            depth, j = 0, i
            while j < len(tokens):
                other = tokens[j]
                if other.tag == token.tag and other.kind == "start":
                    depth += 1
                elif other.tag == token.tag and other.kind == "end":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            whole = "".join(t.raw for t in tokens[i:j + 1])
            if current[0] is not None:
                current[0].keep(whole, "code" if token.tag != "math" else "formula")
            else:
                raw(whole, "skip")
            i = j + 1
            continue
        if token.kind in {"start", "startend", "end"} and (token.tag in BLOCK or token.tag.startswith("h") and token.tag[1:].isdigit()):
            flush()
            if token.kind in {"start", "startend"}:
                attribute_segments(token)
            raw(token.raw)
            if token.kind == "start" and token.tag not in VOID:
                stack.append(token.tag)
                block_tag[0] = token.tag
            elif token.kind == "end":
                if token.tag in stack:
                    while stack and stack.pop() != token.tag:
                        pass
                block_tag[0] = next((tag for tag in reversed(stack) if tag in BLOCK or tag in HEADINGS), "")
            i += 1
            continue
        if token.kind in {"start", "startend", "end"}:
            # Inline element inside prose.
            if token.kind in {"start", "startend"}:
                attribute_segments(token)
            segment = current[0]
            if token.kind == "start" and token.tag not in VOID:
                ensure().open(token.raw)
            elif token.kind == "end" and token.tag not in VOID:
                if segment is not None and _has_open(segment):
                    segment.close(token.raw)
                elif segment is not None:
                    segment.keep(token.raw)
                else:
                    raw(token.raw)
            else:  # void inline element: <br>, <img>, <wbr>
                if segment is not None:
                    segment.keep(token.raw)
                else:
                    raw(token.raw)
            i += 1
            continue
        if token.kind in {"comment", "decl", "pi"}:
            if current[0] is not None:
                current[0].keep(token.raw)
            else:
                raw(token.raw, "skip")
            i += 1
            continue
        # text or entity
        value = html.unescape(token.raw)
        if current[0] is None and not value.strip():
            raw(token.raw)
        elif "head" in stack and block_tag[0] != "title":
            raw(token.raw)  # text in <head> outside <title> is never shown
        else:
            ensure().text(value, raw=token.raw)
        i += 1
    flush()
    _balance(pieces)
    return HtmlDocument(text, pieces, attributes)


def _has_open(segment: Segment) -> bool:
    depth = 0
    for part in segment.parts:
        if part.kind == "open":
            depth += 1
        elif part.kind == "close":
            depth -= 1
    return depth > 0


def _balance(pieces: list[Piece]) -> None:
    """A passage cut by a block boundary with an inline element still open keeps it as markup."""
    for piece in pieces:
        segment = piece.segment
        if segment is None:
            continue
        depth = 0
        for part in segment.parts:
            if part.kind == "open":
                depth += 1
            elif part.kind == "close":
                depth -= 1
        if depth:
            # Unbalanced: turn every pair half into a plain kept marker.
            for part in segment.parts:
                if part.kind in {"open", "close"}:
                    part.kind = "keep"


def escape(text: str) -> str:
    return html.escape(text, quote=False)


def render(document: HtmlDocument, translations: dict[int, str | None], *, bilingual: bool, lang: str = "zh-CN") -> str:
    out = []
    for piece in document.pieces:
        if piece.segment is None:
            out.append(piece.raw)
            continue
        segment = piece.segment
        translated = restore(segment, translations, escape=escape)
        if not bilingual or translations.get(id(segment)) is None:
            out.append(_keep_edges(segment, translated))
        elif piece.tag in TEXT_ONLY:
            out.append(_keep_edges(segment, f"{translated} / {segment.original().strip()}"))
        else:
            original = segment.original().strip()
            out.append(_keep_edges(segment, f'{original}<br><span class="pdftrans-zh" lang="{lang}">{translated}</span>'))
    text = "".join(out)
    for tag_raw, name, segment in document.attributes:
        translated = translations.get(id(segment))
        if not translated:
            continue
        value = html.escape(translated, quote=True)
        if bilingual:
            value = html.escape(f"{translated} / {segment.plain_text()}", quote=True)
        new_tag = _set_attribute(tag_raw, name, value)
        text = text.replace(tag_raw, new_tag)
    return set_language(text, lang)


def _keep_edges(segment: Segment, body: str) -> str:
    """Put back the whitespace that surrounded the passage in the source."""
    original = segment.original()
    leading = original[:len(original) - len(original.lstrip())]
    trailing = original[len(original.rstrip()):]
    return leading + body + trailing


def _set_attribute(tag_raw: str, name: str, value: str) -> str:
    pattern = re.compile(r"(\s" + re.escape(name) + r"\s*=\s*)(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.I)
    return pattern.sub(lambda m: f'{m.group(1)}"{value}"', tag_raw, count=1)


def set_language(text: str, lang: str) -> str:
    def replace(match: re.Match) -> str:
        tag = match.group(0)
        if re.search(r"\slang\s*=", tag, re.I):
            tag = re.sub(r"(\slang\s*=\s*)(\"[^\"]*\"|'[^']*'|[^\s>]+)", rf'\1"{lang}"', tag, count=1, flags=re.I)
        else:
            tag = tag[:5] + f' lang="{lang}"' + tag[5:]
        if re.search(r"\sxml:lang\s*=", tag, re.I):
            tag = re.sub(r"(\sxml:lang\s*=\s*)(\"[^\"]*\"|'[^']*')", rf'\1"{lang}"', tag, count=1, flags=re.I)
        return tag
    return re.sub(r"<html\b[^>]*>", replace, text, count=1, flags=re.I)


def decode(data: bytes) -> str:
    head = data[:2048].decode("ascii", errors="ignore")
    match = re.search(r"charset=[\"']?([A-Za-z0-9_-]+)", head) or re.search(r"encoding=[\"']([A-Za-z0-9_-]+)", head)
    for encoding in (match.group(1) if match else None, "utf-8", "cp1252"):
        if not encoding:
            continue
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", errors="replace")


def to_utf8(text: str) -> str:
    """Declare UTF-8, since the output is written as UTF-8 whatever the source used."""
    text = re.sub(r"(<meta[^>]*charset=[\"']?)([A-Za-z0-9_-]+)", r"\1utf-8", text, count=1, flags=re.I)
    return re.sub(r"(<\?xml[^>]*encoding=[\"'])([A-Za-z0-9_-]+)", r"\1utf-8", text, count=1)


# --- checking -------------------------------------------------------------------------------

def _structure(text: str) -> tuple[Counter, Counter]:
    tags, opaque = Counter(), Counter()
    tokens = tokenize(text)
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token.kind in {"start", "startend", "end"}:
            attrs = tuple(sorted((name, value) for name, value in token.attrs
                                 if name not in TRANSLATED_ATTRIBUTES and name not in {"lang", "xml:lang", "content", "charset"}))
            tags[(token.kind, token.tag, attrs)] += 1
            if token.kind == "start" and token.tag in OPAQUE:
                depth, j = 0, i
                while j < len(tokens):
                    if tokens[j].tag == token.tag and tokens[j].kind == "start":
                        depth += 1
                    elif tokens[j].tag == token.tag and tokens[j].kind == "end":
                        depth -= 1
                        if depth == 0:
                            break
                    j += 1
                opaque["".join(t.raw for t in tokens[i:j + 1])] += 1
        i += 1
    return tags, opaque


def check(source_text: str, translated_text: str, *, name: str = "document") -> list[dict]:
    """Major problems where the translated document's markup differs from the source's."""
    problems = []
    before_tags, before_opaque = _structure(source_text)
    after_tags, after_opaque = _structure(translated_text)
    if before_tags != after_tags:
        lost = sum((before_tags - after_tags).values())
        added = sum((after_tags - before_tags).values())
        problems.append({"kind": "html_elements_changed", "severity": "major", "count": lost + added,
                         "detail": f"{name}: {lost} elements lost and {added} added by the translation",
                         "sample": [str(item) for item in list((before_tags - after_tags))[:3]]})
    if before_opaque != after_opaque:
        problems.append({"kind": "html_code_changed", "severity": "major",
                         "count": sum((before_opaque - after_opaque).values()),
                         "detail": f"{name}: code, math, script or style content differs from the source"})
    return problems


WORD = re.compile(r"[A-Za-z]{3,}")


def leftover_prose(document: HtmlDocument) -> dict:
    leftover, translated = [], 0
    for piece in document.pieces:
        if piece.kind == "raw":
            visible = re.sub(r"<[^>]*>", " ", piece.raw)
            leftover.extend(WORD.findall(html.unescape(visible)))
        elif piece.segment is not None:
            translated += len(WORD.findall(piece.segment.plain_text()))
    total = translated + len(leftover)
    return {"leftover_words": len(leftover), "translated_words": translated,
            "share": round(len(leftover) / total, 4) if total else 0.0, "sample": leftover[:20]}
