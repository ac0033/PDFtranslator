"""Structured source formats: every adapter cuts prose into units, keeps markup, writes back, checks itself."""
import io
import zipfile
from pathlib import Path

import pytest

from pdf_translation.formats import FormatError, detect_format, docx_doc, epub_doc, html_doc, latex, markdown_doc
from pdf_translation.formats.common import Segment, build_units, restore
from pdf_translation.markers import SOURCE_MARKUP_TAG, pairs_are_nested
from pdf_translation.quality import MARKERS, check_quality
from pdf_translation.schemas import ModelSpec, TranslationDraft, TranslationResult, Usage
from pdf_translation.triage import unit_is_untranslated


def fake_translations(units, by_unit, *, word="译文"):
    """A 'translation' that keeps every marker once, in order, around Chinese text - and special characters."""
    return {id(by_unit[u.unit_id]): f"{word}50%&_#" + "甲".join(MARKERS.findall(u.source_text)) + "。" for u in units}


def result_for(unit, text):
    model = ModelSpec(provider="mock", model_id="m", max_output_tokens=100, prompt_revision="p", adapter_revision="a",
                      pricing_revision="p")
    return TranslationResult(request_id="r", unit_id=unit.unit_id, draft=TranslationDraft(unit_id=unit.unit_id, translated_text=text),
                             finish="complete", model=model, usage=Usage(usage_source="estimated"))


# --- shared pieces --------------------------------------------------------------------

def test_markers_restore_by_identity_and_a_broken_translation_falls_back_to_the_source():
    segment = Segment("s1")
    segment.text("See ")
    segment.open("\\emph{")
    segment.text("this")
    segment.close("}")
    segment.text(" and ", raw="and")  # raw differs to prove the fallback writes source bytes
    segment.keep("$x^2$", "formula")
    marked, markers = segment.marked()
    assert marked == "See ⟦P:a1⟧this⟦P:z1⟧ and ⟦P:k3⟧" and set(markers) == {"⟦P:a1⟧", "⟦P:z1⟧", "⟦P:k3⟧"}
    moved = {id(segment): "⟦P:k3⟧ 与 ⟦P:a1⟧这个⟦P:z1⟧"}
    assert restore(segment, moved, escape=str) == "$x^2$ 与 \\emph{这个}"
    lost = {id(segment): "⟦P:a1⟧这个⟦P:z1⟧"}
    assert restore(segment, lost, escape=str) == "See \\emph{this}and$x^2$"


def test_units_from_structured_sources_carry_the_markup_tag_and_readable_hints():
    segment = Segment("s1")
    segment.text("The value ")
    segment.keep("$\\alpha$", "formula")
    segment.text(" grows.")
    units, _ = build_units("doc", [segment, Segment("empty")], revision="t")
    assert len(units) == 1 and SOURCE_MARKUP_TAG in units[0].risk_tags
    assert units[0].protected_literal_hints == {"⟦P:k1⟧": "$\\alpha$"}


def test_the_gate_accepts_moved_markers_but_blocks_crossed_pairs():
    segment = Segment("s")
    segment.open("<b>")
    segment.text("bold words")
    segment.close("</b>")
    segment.text(" then ")
    segment.keep("<code>x</code>", "code")
    segment.text(" more text here.")
    units, _ = build_units("d", [segment], revision="t")
    unit = units[0]
    assert unit.source_text == "⟦P:a1⟧bold words⟦P:z1⟧ then ⟦P:k3⟧ more text here."
    moved = check_quality(unit, result_for(unit, "⟦P:k3⟧ 之后 ⟦P:a1⟧粗体⟦P:z1⟧ 更多。"))
    assert moved.status == "pass"
    crossed = check_quality(unit, result_for(unit, "⟦P:z1⟧粗体⟦P:a1⟧ ⟦P:k3⟧。"))
    assert crossed.status == "blocked" and any(i.category == "paired_marker_nesting" for i in crossed.issues)
    assert pairs_are_nested("⟦P:a1⟧⟦P:a2⟧x⟦P:z2⟧⟦P:z1⟧") and not pairs_are_nested("⟦P:a1⟧⟦P:a2⟧x⟦P:z1⟧⟦P:z2⟧")


@pytest.mark.parametrize("source,translation,expected", [
    ("Build it with ⟦P:k1⟧, which takes about 2 seconds", "Build it with ⟦P:k1⟧, which takes about 2 seconds", True),
    ("See the table below.", "See the table below.", True),
    ("Getting Started with Cargo", "Getting Started with Cargo", False),
    ("Neural Networks", "Neural Networks", False),
    ("Christopher Manning", "Christopher Manning", False),
    ("Build it with ⟦P:k1⟧, which takes about 2 seconds", "用⟦P:k1⟧构建，大约需要 2 秒", False),
])
def test_a_short_unit_left_as_an_english_sentence_counts_as_untranslated(source, translation, expected):
    assert unit_is_untranslated(source, translation) is expected


@pytest.mark.parametrize("name,head,expected", [
    ("a.pdf", b"%PDF-1.7", "pdf"), ("a.tex", b"\\documentclass{article}", "latex"),
    ("proj.zip", b"PK\x03\x04", "latex"), ("proj.tar.gz", b"\x1f\x8b\x08", "latex"),
    ("book.epub", b"PK\x03\x04\x00\x00mimetypeapplication/epub+zip", "epub"), ("r.docx", b"PK\x03\x04", "docx"),
    ("a.md", b"# T", "markdown"), ("a.html", b"<p>", "html"), ("noext", b"<!DOCTYPE html><html>", "html"),
])
def test_formats_are_told_apart_by_content_and_name(name, head, expected):
    assert detect_format(name, head) == expected


def test_an_unknown_format_is_refused_with_the_supported_list():
    with pytest.raises(FormatError, match="send a .pdf"):
        detect_format("data.bin", b"\x00\x01\x02")


# --- LaTeX ------------------------------------------------------------------------------

PAPER = r"""\documentclass{article}
\usepackage{amsmath}
\title{Learning to \emph{Translate} Fast}
\begin{document}
\maketitle
\begin{abstract}
We study the problem of translating papers. It costs 50\% less than before~\cite{smith2020}.
\end{abstract}
\section{Introduction}\label{sec:intro}
The model maps $x \in \mathbb{R}^n$ to a label.\footnote{See the appendix for the proof.} We write
\begin{equation}
  f(x) = \sum_i w_i x_i
\end{equation}
where the weights are learned, as shown in Figure~\ref{fig:a}.

% a comment line that stays
\begin{figure}[t]
\centering
\includegraphics[width=0.5\linewidth]{fig.png}
\caption{The \textbf{overall} architecture of the system.}
\label{fig:a}
\end{figure}

\begin{itemize}
\item The first point is important.
\item The second point matters too.
\end{itemize}

\begin{tabular}{ll}
Method & Accuracy \\
Our approach & 91.2 \\
\end{tabular}

\begin{verbatim}
print("not translated")
\end{verbatim}
\end{document}
"""


@pytest.fixture
def paper(tmp_path: Path) -> latex.LatexProject:
    root = tmp_path / "paper"
    root.mkdir()
    (root / "main.tex").write_text(PAPER, encoding="utf-8")
    return latex.load_project(root)


def test_latex_prose_becomes_units_and_everything_else_is_kept(paper):
    units, by_unit = build_units("d", paper.segments(), revision="t")
    texts = [u.source_text for u in units]
    assert "Learning to ⟦P:a1⟧Translate⟦P:z1⟧ Fast" in texts
    assert any(t.startswith("We study the problem") and "50% less" in t and "⟦P:k" in t for t in texts)
    assert "Introduction" in texts and "See the appendix for the proof." in texts
    assert any("⟦P:" in t and t.startswith("The model maps") and "where the weights are learned" in t for t in texts)
    assert "The first point is important." in texts and "Our approach" in texts
    assert not any("not translated" in t or "architecture" in t and "includegraphics" in t for t in texts)
    assert latex.leftover_prose(paper)["leftover_words"] == 0


def test_latex_write_back_keeps_structure_and_escapes_translated_text(paper, tmp_path):
    units, by_unit = build_units("d", paper.segments(), revision="t")
    out = latex.write_project(paper, tmp_path / "zh", fake_translations(units, by_unit), bilingual=False)
    raw = (out / "main.tex").read_bytes().decode("utf-8")
    assert "\r\r" not in raw  # line endings are written as the source had them, never doubled
    text = raw.replace("\r\n", "\n")
    assert "\\usepackage[UTF8,scheme=plain]{ctex}" in text
    assert "译文50\\%\\&\\_\\#" in text and "f(x) = \\sum_i w_i x_i" in text
    assert '\\begin{verbatim}\nprint("not translated")' in text and "\\cite{smith2020}" in text
    assert latex.check(paper, out) == []


def test_latex_check_catches_a_lost_formula(paper, tmp_path):
    units, by_unit = build_units("d", paper.segments(), revision="t")
    out = latex.write_project(paper, tmp_path / "zh", fake_translations(units, by_unit), bilingual=False)
    main = out / "main.tex"
    main.write_text(main.read_text(encoding="utf-8").replace("$x \\in \\mathbb{R}^n$", ""), encoding="utf-8")
    kinds = {problem["kind"] for problem in latex.check(paper, out)}
    assert "latex_math_changed" in kinds


def test_windows_line_endings_still_separate_paragraphs(tmp_path):
    root = tmp_path / "w"
    root.mkdir()
    (root / "main.tex").write_bytes(b"\\documentclass{article}\r\n\\begin{document}\r\nFirst paragraph here.\r\n\r\n"
                                    b"Second paragraph here.\r\n\\end{document}\r\n")
    project = latex.load_project(root)
    assert [s.plain_text().strip() for s in project.segments()] == ["First paragraph here.", "Second paragraph here."]


def test_latex_projects_follow_input_files(tmp_path):
    root = tmp_path / "p"
    (root / "sections").mkdir(parents=True)
    (root / "main.tex").write_text("\\documentclass{article}\\begin{document}\n\\input{sections/intro}\n\\end{document}\n",
                                   encoding="utf-8")
    (root / "sections" / "intro.tex").write_text("This section introduces the method.\n", encoding="utf-8")
    project = latex.load_project(root)
    assert [f.path for f in project.files] == ["main.tex", "sections/intro.tex"]
    assert [s.plain_text().strip() for s in project.segments()] == ["This section introduces the method."]


def test_latex_archives_unpack_safely(tmp_path):
    archive = tmp_path / "p.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("main.tex", "\\documentclass{article}\\begin{document}Hi\\end{document}")
        bundle.writestr("../escape.tex", "outside")
    root = latex.unpack(archive, tmp_path / "unpacked")
    assert (root / "main.tex").is_file() and not (tmp_path / "escape.tex").exists()
    assert latex.find_main(root) == "main.tex"


# --- HTML -------------------------------------------------------------------------------

PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>The Rust Book &amp; More</title>
<meta name="description" content="Learn systems programming"><style>p{color:red}</style></head>
<body><h1 id="x">Getting <em>Started</em></h1>
<p>Let&#39;s start your <a href="https://r.io">Rust journey</a>! See <code>cargo new</code>.<br>
Second line &amp; more.</p>
<ul><li>Install Rust on <b>Linux</b></li></ul>
<img src="a.png" alt="A crab mascot"><pre>fn main() {}</pre>
<script>var s = "do not translate";</script>
</body></html>"""


def test_html_tokens_rebuild_the_source_exactly():
    assert "".join(token.raw for token in html_doc.tokenize(PAGE)) == PAGE


def test_html_translates_text_and_attributes_and_keeps_code(tmp_path):
    document = html_doc.parse(PAGE)
    units, by_unit = build_units("d", document.segments(), revision="t")
    texts = [u.source_text for u in units]
    assert "Getting ⟦P:a1⟧Started⟦P:z1⟧" in texts and "A crab mascot" in texts
    assert not any("do not translate" in t or "fn main" in t for t in texts)
    out = html_doc.render(document, fake_translations(units, by_unit), bilingual=False)
    assert '<html lang="zh-CN">' in out and "<pre>fn main() {}</pre>" in out and 'href="https://r.io"' in out
    assert "译文50%&amp;_#" in out and 'alt="译文50%&amp;_#。"' in out
    assert html_doc.check(PAGE, out) == []
    assert html_doc.leftover_prose(document)["leftover_words"] == 0
    dual = html_doc.render(document, fake_translations(units, by_unit), bilingual=True)
    assert "Install Rust on <b>Linux</b><br><span class=\"pdftrans-zh\"" in dual


def test_html_check_catches_a_changed_link():
    changed = PAGE.replace('href="https://r.io"', 'href="https://elsewhere.example"')
    assert html_doc.check(PAGE, changed)[0]["kind"] == "html_elements_changed"


# --- Markdown -----------------------------------------------------------------------------

NOTES = """---
title: Demo
---
# Getting Started

This is **really important**: run `pip install x` and see the [docs](https://x.io/docs) for $E=mc^2$.

- First item with a [link][ref]
- Second item
  continued here

| Name | Value |
|------|-------|
| Speed | fast |

```python
print("hello world")
```

[ref]: https://example.com
"""


def test_markdown_keeps_code_math_and_links_and_translates_the_rest():
    document = markdown_doc.parse(NOTES)
    units, by_unit = build_units("d", document.segments(), revision="t")
    texts = [u.source_text for u in units]
    assert "Getting Started" in texts and "Second item continued here" in texts and "Speed" in texts
    assert any("⟦P:a" in t and "really important" in t for t in texts)
    out = markdown_doc.render(document, fake_translations(units, by_unit), bilingual=False)
    assert 'print("hello world")' in out and "](https://x.io/docs)" in out and "$E=mc^2$" in out
    assert out.startswith("---\ntitle: Demo\n---\n# 译文") and "| 译文" in out and "|------|-------|" in out
    assert markdown_doc.check(NOTES, out) == []
    assert markdown_doc.leftover_prose(document)["leftover_words"] == 0


# --- DOCX ---------------------------------------------------------------------------------

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def make_docx() -> bytes:
    body = (f'<w:document xmlns:w="{W}"><w:body>'
            '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Quarterly Report</w:t></w:r></w:p>'
            '<w:p><w:r><w:t xml:space="preserve">Revenue grew by </w:t></w:r>'
            '<w:r><w:rPr><w:b/></w:rPr><w:t>twelve percent</w:t></w:r>'
            '<w:r><w:t xml:space="preserve"> in the third quarter.</w:t><w:tab/><w:t>See the table.</w:t></w:r></w:p>'
            '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>North America</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
            '</w:body></w:document>')
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("[Content_Types].xml", "<Types/>")
        bundle.writestr("word/document.xml", body)
    return buffer.getvalue()


def test_docx_keeps_formatting_runs_tabs_and_tables():
    data = make_docx()
    document = docx_doc.parse(data)
    units, by_unit = build_units("d", document.segments(), revision="t")
    texts = [u.source_text for u in units]
    assert texts == ["Quarterly Report", "Revenue grew by ⟦P:a1⟧twelve percent⟦P:z1⟧ in the third quarter.⟦P:k3⟧See the table.",
                     "North America"]
    out = docx_doc.render(document, fake_translations(units, by_unit), bilingual=False)
    assert docx_doc.check(data, out) == []
    xml = zipfile.ZipFile(io.BytesIO(out)).read("word/document.xml").decode("utf-8")
    assert "<w:b/>" in xml and "<w:tab/>" in xml and "Heading1" in xml and "译文" in xml and "Revenue" not in xml
    dual = docx_doc.render(document, fake_translations(units, by_unit), bilingual=True)
    dual_xml = zipfile.ZipFile(io.BytesIO(dual)).read("word/document.xml").decode("utf-8")
    assert "Revenue grew by" in dual_xml and "译文" in dual_xml


# --- EPUB ---------------------------------------------------------------------------------

def make_epub() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        bundle.writestr("META-INF/container.xml", '<container><rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>')
        bundle.writestr("OEBPS/content.opf",
                        '<package><metadata><dc:title>A Short Guide</dc:title><dc:language>en</dc:language></metadata>'
                        '<manifest><item id="c1" href="ch1.xhtml" media-type="application/xhtml+xml"/>'
                        '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/></manifest>'
                        '<spine><itemref idref="c1"/></spine></package>')
        bundle.writestr("OEBPS/ch1.xhtml", '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml">'
                                           "<head><title>Chapter One</title></head><body><h1>Chapter One</h1>"
                                           "<p>The journey begins here.</p><img src=\"a.png\"/></body></html>")
        bundle.writestr("OEBPS/toc.ncx", "<ncx><navMap><navPoint><navLabel><text>Chapter One</text></navLabel></navPoint></navMap></ncx>")
        bundle.writestr("OEBPS/a.png", b"\x89PNG")
    return buffer.getvalue()


def test_epub_translates_chapters_toc_and_title_and_stays_a_valid_book():
    data = make_epub()
    document = epub_doc.parse(data)
    units, by_unit = build_units("d", document.segments(), revision="t")
    assert {u.source_text for u in units} >= {"A Short Guide", "Chapter One", "The journey begins here."}
    out = epub_doc.render(document, fake_translations(units, by_unit), bilingual=False)
    assert epub_doc.check(document, out) == []
    bundle = zipfile.ZipFile(io.BytesIO(out))
    assert bundle.namelist()[0] == "mimetype" and bundle.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
    assert "<dc:language>zh-CN</dc:language>" in bundle.read("OEBPS/content.opf").decode("utf-8")
    assert "译文" in bundle.read("OEBPS/toc.ncx").decode("utf-8") and bundle.read("OEBPS/a.png") == b"\x89PNG"


def test_prose_inside_an_unknown_macro_counts_as_left_in_english(tmp_path):
    (tmp_path / "main.tex").write_text(
        r"\documentclass{article}\begin{document}We did this. \todo{check these results again} "
        r"Then more \cite{x} text.\end{document}", encoding="utf-8")
    report = latex.leftover_prose(latex.load_project(tmp_path))
    assert report["sample"] == ["check", "these", "results", "again"]  # \cite{x} is not prose


def test_author_notes_in_the_preamble_are_translated_but_names_are_not(tmp_path):
    (tmp_path / "main.tex").write_text(
        r"\documentclass{article}" "\n"
        r"\title{Fast Models\thanks{Supported by a grant from the foundation.}}" "\n"
        r"\author{Ada Lovelace\thanks{Equal contribution. Listing order is random.}}" "\n"
        r"% \thanks{a commented-out note stays out}" "\n"
        r"\begin{document}\maketitle Body text here.\end{document}", encoding="utf-8")
    project = latex.load_project(tmp_path)
    texts = [s.plain_text().strip() for s in project.segments()]
    assert texts[:2] == ["Fast Models", "Equal contribution. Listing order is random."]
    assert "Supported by a grant from the foundation." in [c.plain_text() for s in project.segments() for c in s.children()]
    assert not any("Ada Lovelace" in t or "commented-out" in t for t in texts)
    assert r"\def\figurename{图}" in latex.add_chinese_support((tmp_path / "main.tex").read_text(encoding="utf-8"))
