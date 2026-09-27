"""EPUB: translate every content document in the spine, the navigation and the title.

An EPUB is a ZIP of XHTML chapters plus a package file. Each chapter (and the
navigation document) goes through the HTML adapter; the NCX table of contents
has its labels translated; the package's ``dc:title`` is translated and its
``dc:language`` set to Chinese. Everything else - images, fonts, CSS - is
copied, and ``mimetype`` stays the first, uncompressed entry as the format
requires.

Accuracy check: the HTML check on every chapter, plus the same set of files
in the output as in the source.
"""
from __future__ import annotations

import html
import io
import posixpath
import re
import zipfile
from dataclasses import dataclass

from . import html_doc
from .common import Segment, restore

REVISION = "epub-source-v1"


@dataclass
class Chapter:
    path: str
    text: str
    document: html_doc.HtmlDocument


@dataclass
class EpubDocument:
    data: bytes
    opf_path: str
    opf: str
    chapters: list[Chapter]
    ncx_path: str | None
    ncx: str | None
    ncx_labels: list[tuple[tuple[int, int], Segment]]
    title: tuple[tuple[int, int], Segment] | None

    def segments(self) -> list[Segment]:
        found = []
        if self.title:
            found.append(self.title[1])
        for chapter in self.chapters:
            found.extend(chapter.document.segments())
        found.extend(segment for _, segment in self.ncx_labels)
        return found


def parse(data: bytes) -> EpubDocument:
    with zipfile.ZipFile(io.BytesIO(data)) as bundle:
        container = bundle.read("META-INF/container.xml").decode("utf-8", errors="replace")
        match = re.search(r'full-path="([^"]+)"', container)
        if not match:
            raise ValueError("META-INF/container.xml names no package file")
        opf_path = match.group(1)
        opf = bundle.read(opf_path).decode("utf-8", errors="replace")
        base = posixpath.dirname(opf_path)
        manifest = {}
        for item in re.finditer(r"<item\b[^>]*>", opf):
            tag = item.group(0)
            ident = re.search(r'\bid="([^"]+)"', tag)
            href = re.search(r'\bhref="([^"]+)"', tag)
            media = re.search(r'\bmedia-type="([^"]+)"', tag)
            props = re.search(r'\bproperties="([^"]+)"', tag)
            if ident and href:
                manifest[ident.group(1)] = (posixpath.normpath(posixpath.join(base, html.unescape(href.group(1)))),
                                            media.group(1) if media else "", props.group(1) if props else "")
        order = [ref.group(1) for ref in re.finditer(r'<itemref\b[^>]*idref="([^"]+)"', opf)]
        documents = [manifest[ident][0] for ident in order if ident in manifest]
        for ident, (path, media, props) in manifest.items():
            if "nav" in props.split() and path not in documents:
                documents.append(path)
        chapters = []
        for index, path in enumerate(documents):
            if path not in bundle.namelist():
                continue
            text = html_doc.decode(bundle.read(path))
            chapters.append(Chapter(path, text, html_doc.parse(text, prefix=f"e{index}")))
        ncx_path = next((path for path, media, _ in manifest.values() if media == "application/x-dtbncx+xml"), None)
        ncx = bundle.read(ncx_path).decode("utf-8", errors="replace") if ncx_path and ncx_path in bundle.namelist() else None
    labels = []
    if ncx:
        for number, label in enumerate(re.finditer(r"<text>([^<]*)</text>", ncx)):
            segment = Segment(f"ncx:{number}", kind="heading")
            segment.text(html.unescape(label.group(1)), raw=label.group(1))
            labels.append(((label.start(1), label.end(1)), segment))
    title = None
    title_match = re.search(r"<dc:title[^>]*>([^<]*)</dc:title>", opf)
    if title_match:
        segment = Segment("opf:title", kind="heading")
        segment.text(html.unescape(title_match.group(1)), raw=title_match.group(1))
        title = ((title_match.start(1), title_match.end(1)), segment)
    return EpubDocument(data, opf_path, opf, chapters, ncx_path, ncx, labels, title)


def _splice(text: str, replacements: list[tuple[tuple[int, int], str]]) -> str:
    for (start, end), value in sorted(replacements, key=lambda item: -item[0][0]):
        text = text[:start] + value + text[end:]
    return text


def render(document: EpubDocument, translations: dict[int, str | None], *, bilingual: bool) -> bytes:
    replaced: dict[str, bytes] = {}
    for chapter in document.chapters:
        out = html_doc.render(chapter.document, translations, bilingual=bilingual)
        replaced[chapter.path] = html_doc.to_utf8(out).encode("utf-8")
    opf = document.opf
    if document.title:
        span, segment = document.title
        value = restore(segment, translations, escape=html_doc.escape)
        if bilingual and translations.get(id(segment)):
            value = f"{value} / {segment.original()}"
        opf = _splice(opf, [(span, value)])
    opf = re.sub(r"(<dc:language[^>]*>)[^<]*(</dc:language>)", r"\1zh-CN\2", opf, count=1)
    replaced[document.opf_path] = opf.encode("utf-8")
    if document.ncx is not None and document.ncx_path:
        values = []
        for span, segment in document.ncx_labels:
            value = restore(segment, translations, escape=html_doc.escape)
            if bilingual and translations.get(id(segment)):
                value = f"{value} / {segment.original()}"
            values.append((span, value))
        replaced[document.ncx_path] = _splice(document.ncx, values).encode("utf-8")
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(document.data)) as source, zipfile.ZipFile(output, "w") as target:
        names = source.namelist()
        if "mimetype" in names:
            target.writestr(zipfile.ZipInfo("mimetype"), source.read("mimetype"), compress_type=zipfile.ZIP_STORED)
        for info in source.infolist():
            if info.filename == "mimetype":
                continue
            body = replaced.get(info.filename, source.read(info.filename))
            target.writestr(info, body, compress_type=zipfile.ZIP_DEFLATED)
    return output.getvalue()


def check(document: EpubDocument, translated: bytes) -> list[dict]:
    problems = []
    with zipfile.ZipFile(io.BytesIO(translated)) as bundle:
        names = bundle.namelist()
        if names[:1] != ["mimetype"]:
            problems.append({"kind": "epub_mimetype_not_first", "severity": "major", "count": 1,
                             "detail": "the mimetype entry is not the first file, so readers may reject the book"})
        with zipfile.ZipFile(io.BytesIO(document.data)) as source:
            missing = set(source.namelist()) - set(names)
        if missing:
            problems.append({"kind": "epub_files_missing", "severity": "major", "count": len(missing),
                             "detail": f"{len(missing)} files of the source book are missing", "sample": sorted(missing)[:5]})
        for chapter in document.chapters:
            translated_text = bundle.read(chapter.path).decode("utf-8", errors="replace")
            problems.extend(html_doc.check(chapter.text, translated_text, name=chapter.path))
    return problems


def leftover_prose(document: EpubDocument) -> dict:
    leftover = translated = 0
    sample: list[str] = []
    for chapter in document.chapters:
        report = html_doc.leftover_prose(chapter.document)
        leftover += report["leftover_words"]
        translated += report["translated_words"]
        sample.extend(report["sample"])
    total = leftover + translated
    return {"leftover_words": leftover, "translated_words": translated,
            "share": round(leftover / total, 4) if total else 0.0, "sample": sample[:20]}
