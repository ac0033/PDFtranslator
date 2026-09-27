"""Structured source formats: LaTeX, HTML, Markdown, DOCX and EPUB.

A PDF has to be taken apart by a layout model before anything can be
translated, and put back together page by page. A structured source already
says what is prose, what is a formula and what is markup, so each adapter here
translates in place: it cuts the prose into units, replaces every piece of
markup inside them with a protected marker (:mod:`pdf_translation.markers`),
sends the units through the same translation pipeline, quality gate and run
store as a PDF, and writes each translation back where its source was. Markup
outside the prose is copied byte for byte.

That makes the result checkable in a way a PDF is not: every formula, citation,
link and tag in the output can be compared with the source, and the adapter
does exactly that (``check``) before acceptance grades the run.
"""
from __future__ import annotations

from pathlib import Path

#: File suffix -> format name. A ``.zip`` or ``.tar.gz`` is a LaTeX project.
SUFFIXES = {
    ".pdf": "pdf",
    ".tex": "latex",
    ".ltx": "latex",
    ".zip": "latex",
    ".gz": "latex",
    ".tgz": "latex",
    ".tar": "latex",
    ".html": "html",
    ".htm": "html",
    ".xhtml": "html",
    ".md": "markdown",
    ".markdown": "markdown",
    ".docx": "docx",
    ".epub": "epub",
}
FORMATS = ("pdf", "latex", "html", "markdown", "docx", "epub")


class FormatError(ValueError):
    """The file is not something this format can read; the message says why."""


def detect_format(filename: str, head: bytes) -> str:
    """Name the format of an uploaded file from its name and first bytes.

    Content wins over the name where they can disagree: a ZIP whose first entry
    is ``mimetype`` with ``application/epub+zip`` is an EPUB and a ZIP holding
    ``word/document.xml`` is a DOCX whatever it is called.
    """
    if head.startswith(b"%PDF"):
        return "pdf"
    name = filename.lower()
    if head.startswith(b"PK"):
        if b"mimetypeapplication/epub+zip" in head[:100]:
            return "epub"
        if name.endswith(".docx") or b"word/" in head[:2000]:
            return "docx"
        if name.endswith(".epub"):
            return "epub"
        return "latex"
    if head[:2] == b"\x1f\x8b" or name.endswith((".tar.gz", ".tgz", ".tar")):
        return "latex"
    suffix = Path(name).suffix
    if suffix in SUFFIXES and SUFFIXES[suffix] not in {"pdf", "docx", "epub"}:
        return SUFFIXES[suffix]
    text = head[:4000].decode("utf-8", errors="ignore").lstrip().lower()
    if text.startswith(("<!doctype html", "<html")) or "<body" in text:
        return "html"
    if "\\documentclass" in text or "\\begin{document}" in text:
        return "latex"
    raise FormatError(
        f"cannot tell the format of {filename!r}; send a .pdf, .tex (or a zipped LaTeX project), "
        ".html, .md, .docx or .epub file")
