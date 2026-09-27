"""arXiv papers: translate the LaTeX source instead of the PDF when it provably matches.

Most papers on arXiv ship their LaTeX source. Translating the source keeps
every formula exact - the PDF route's weakest point - so a PDF whose first
page carries an arXiv identifier with a version (``arXiv:2401.01234v2``) is
checked here before anything is translated:

1. the identifier and version are read from the PDF's text (never guessed);
2. the source of exactly that version is downloaded from arxiv.org
   (one request, a descriptive User-Agent, as arXiv asks of automated clients);
3. the source is compiled here and the result compared with the uploaded PDF:
   page counts within one page, and at least 85% of the PDF's words found in
   the compiled text.

Only when all three hold does the job take the LaTeX route; otherwise it
takes the PDF route and the report says why. ``allow_network=false`` in the
job request turns the download off.
"""
from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import httpx
import pymupdf

from .. import __version__
from . import latex

IDENTIFIER = re.compile(r"arXiv:\s?(\d{4}\.\d{4,5})(v\d+)", re.I)
OLD_IDENTIFIER = re.compile(r"arXiv:\s?([a-z\-]+(?:\.[A-Z]{2})?/\d{7})(v\d+)", re.I)
EPRINT_URL = "https://arxiv.org/e-print/{identifier}"
USER_AGENT = f"pdftrans/{__version__} (document translation; single on-demand source fetch)"
MAX_BYTES = 200 * 1024 * 1024
MIN_WORD_RECALL = 0.85
WORD = re.compile(r"[a-z]{4,}")


@dataclass
class ArxivDecision:
    use_source: bool
    reason: str
    identifier: str | None = None
    project: Path | None = None
    page_counts: tuple[int, int] | None = None
    word_recall: float | None = None

    def as_dict(self) -> dict:
        return {"use_source": self.use_source, "reason": self.reason, "identifier": self.identifier,
                "page_counts": list(self.page_counts) if self.page_counts else None, "word_recall": self.word_recall}


def find_identifier(pdf: Path) -> str | None:
    """The versioned arXiv identifier printed on the first pages or in the metadata, if any."""
    with pymupdf.open(pdf) as document:
        texts = [document.metadata.get("subject") or "", document.metadata.get("title") or "",
                 document.metadata.get("keywords") or ""]
        texts += [document[index].get_text() for index in range(min(2, len(document)))]
    for text in texts:
        for pattern in (IDENTIFIER, OLD_IDENTIFIER):
            match = pattern.search(text)
            if match:
                return match.group(1) + match.group(2)
    return None


def _words(pdf: Path) -> tuple[int, set[str]]:
    with pymupdf.open(pdf) as document:
        text = " ".join(page.get_text() for page in document)
        return len(document), set(WORD.findall(text.lower()))


def download(identifier: str, destination: Path, *, transport=None, timeout: float = 120.0) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with httpx.Client(transport=transport, timeout=timeout, follow_redirects=True,
                      headers={"User-Agent": USER_AGENT}) as client:
        with client.stream("GET", EPRINT_URL.format(identifier=identifier)) as response:
            if response.status_code >= 400:
                raise RuntimeError(f"arXiv answered HTTP {response.status_code}")
            size = 0
            with destination.open("wb") as out:
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise RuntimeError("the source archive is larger than 200 MB")
                    out.write(chunk)
    return destination


def try_source(pdf: Path, work: Path, *, allow_network: bool, transport=None) -> ArxivDecision:
    """Decide whether this PDF can be translated from its arXiv source; fetch and verify it if so."""
    identifier = find_identifier(pdf)
    if identifier is None:
        return ArxivDecision(False, "no versioned arXiv identifier on the PDF")
    if not allow_network:
        return ArxivDecision(False, "the job does not allow network access (allow_network=false)", identifier)
    archive = work / "arxiv-source.download"
    project = work / "arxiv-source"
    try:
        download(identifier, archive, transport=transport)
        if archive.read_bytes()[:4] == b"%PDF":
            return ArxivDecision(False, "arXiv has no LaTeX source for this paper (PDF only)", identifier)
        if project.exists():
            shutil.rmtree(project)
        latex.unpack(archive, project)
        main = latex.find_main(project)
    except Exception as problem:
        return ArxivDecision(False, f"the source could not be fetched or read: {problem}"[:300], identifier)
    build = work / "arxiv-verify"
    if build.exists():
        shutil.rmtree(build)
    shutil.copytree(project, build)
    compiled = latex.compile_project(build, main, engine="pdflatex")
    if compiled.pdf is None:
        compiled = latex.compile_project(build, main, engine="xelatex")
    if compiled.pdf is None:
        shutil.rmtree(project, ignore_errors=True)
        return ArxivDecision(False, "the arXiv source does not compile on this machine", identifier)
    uploaded_pages, uploaded_words = _words(pdf)
    built_pages, built_words = _words(compiled.pdf)
    recall = len(uploaded_words & built_words) / len(uploaded_words) if uploaded_words else 0.0
    shutil.rmtree(build, ignore_errors=True)
    archive.unlink(missing_ok=True)
    counts = (uploaded_pages, built_pages)
    if abs(uploaded_pages - built_pages) > 1 or recall < MIN_WORD_RECALL:
        shutil.rmtree(project, ignore_errors=True)
        return ArxivDecision(False, f"the compiled source does not match the uploaded PDF (pages {uploaded_pages} vs "
                                    f"{built_pages}, word recall {recall:.0%})", identifier, None, counts, round(recall, 3))
    return ArxivDecision(True, "the arXiv source compiles and matches the uploaded PDF", identifier, project,
                         counts, round(recall, 3))
