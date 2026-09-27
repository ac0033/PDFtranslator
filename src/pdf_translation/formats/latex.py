"""LaTeX: translate a paper or book from its source, not its PDF.

The source already separates prose from formulas, so nothing has to be
guessed: every formula, citation, reference, label, command and environment
is carried through byte for byte, and only the prose between them is
translated. The translated project is compiled with XeLaTeX and the ``ctex``
package for Chinese; the original project is compiled too, so a failure can
be told apart from a project that never built here.

What is translated: paragraphs of the document body (including inside
``abstract``, theorem-like and list environments), section titles, the title,
figure and table captions, footnotes, and table cells holding prose. What is
kept: preamble (except ``\\title``), mathematics in every form, verbatim-like
environments (code, TikZ, algorithms), the bibliography, and every command
not known to wrap prose.

Accuracy check (:func:`check`): the multiset of formulas, citation and
reference commands and environments in each translated file must equal the
source's, braces must balance, and the translated project must compile.
"""
from __future__ import annotations

import gzip
import io
import os
import re
import shutil
import subprocess
import tarfile
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .common import Segment, restore

REVISION = "latex-source-v1"

SECTIONING = {"part", "chapter", "section", "subsection", "subsubsection", "paragraph", "subparagraph",
              "title", "caption", "subcaption"}
WRAP = {"emph", "textbf", "textit", "textsl", "textsc", "textup", "textmd", "textrm", "textsf", "textnormal",
        "underline", "uline", "text", "mbox", "hl", "enquote", "textcolor", "colorbox"}
CHILD = {"footnote", "thanks", "marginpar", "footnotetext"}
TEXT_ESCAPES = set("%&#_${}")
MATH_ENVS = {name + star for name in ("equation", "align", "alignat", "gather", "multline", "flalign", "eqnarray",
                                      "displaymath", "math", "dmath", "IEEEeqnarray") for star in ("", "*")}
VERBATIM_ENVS = {"verbatim", "verbatim*", "Verbatim", "BVerbatim", "lstlisting", "minted", "comment", "alltt",
                 "tikzpicture", "pgfpicture", "algorithmic", "algorithmic*", "filecontents", "filecontents*",
                 "picture", "forest", "tikzcd", "circuitikz", "axis", "thebibliography", "mdframed*", "listing"}
FLOAT_ENVS = {"figure", "figure*", "table", "table*", "algorithm", "algorithm*", "wrapfigure", "wraptable",
              "subfigure", "subtable", "SCfigure", "sidewaysfigure", "sidewaystable"}
TABULAR_ENVS = {"tabular", "tabular*", "tabularx", "tabulary", "longtable", "longtable*", "supertabular", "tabu"}
LIST_ENVS = {"itemize", "enumerate", "description", "compactitem", "compactenum", "asparaitem", "inparaenum",
             "list", "outline"}
#: Commands whose arguments are never prose: kept whole wherever they appear.
BLOCK_COMMANDS = {"maketitle", "tableofcontents", "listoffigures", "listoftables", "bibliography",
                  "bibliographystyle", "printbibliography", "appendix", "newpage", "clearpage", "cleardoublepage",
                  "centering", "raggedright", "raggedleft", "vspace", "vskip", "medskip", "bigskip", "smallskip",
                  "noindent", "indent", "pagebreak", "linebreak", "label", "hline", "toprule", "midrule",
                  "bottomrule", "frontmatter", "mainmatter", "backmatter", "newcommand", "renewcommand",
                  "setlength", "addtocounter", "setcounter", "includegraphics", "input", "include", "subfile"}
INCLUDES = {"input", "include", "subfile"}
REFERENCE_COMMANDS = ("cite", "citep", "citet", "citealp", "citealt", "citeauthor", "citeyear", "nocite", "ref",
                      "eqref", "autoref", "cref", "Cref", "pageref", "label", "url", "href", "includegraphics",
                      "input", "include")
ESCAPES = {"\\": r"\textbackslash{}", "{": r"\{", "}": r"\}", "$": r"\$", "&": r"\&", "#": r"\#",
           "^": r"\^{}", "_": r"\_", "%": r"\%", "~": r"\textasciitilde{}"}
CHINESE_PACKAGE = (
    "\\usepackage[UTF8,scheme=plain]{ctex}% added by pdftrans for Chinese text\n"
    # scheme=plain keeps the document's own heading style; only the automatic labels become Chinese.
    "\\AtBeginDocument{\\def\\figurename{图}\\def\\tablename{表}\\def\\abstractname{摘要}"
    "\\def\\refname{参考文献}\\def\\bibname{参考文献}\\def\\contentsname{目录}\\def\\appendixname{附录}"
    "\\def\\listfigurename{插图}\\def\\listtablename{表格}}% added by pdftrans\n"
)
COMPILE_TIMEOUT = 600


def escape(text: str) -> str:
    """Plain translated text as LaTeX: every special character escaped."""
    return "".join(ESCAPES.get(char, char) for char in text)


# --- reading a project ----------------------------------------------------------------------

@dataclass
class Piece:
    #: raw (copied layout), skip (copied on purpose: preamble, code, bibliography), paragraph, heading, caption, cell
    kind: str
    raw: str = ""
    segment: Segment | None = None


@dataclass
class TexFile:
    path: str  # relative to the project root, forward slashes
    text: str
    pieces: list[Piece] = field(default_factory=list)
    is_main: bool = False


@dataclass
class LatexProject:
    root: Path
    main: str
    files: list[TexFile]

    def segments(self) -> list[Segment]:
        return [piece.segment for tex in self.files for piece in tex.pieces if piece.segment is not None]


def unpack(archive: Path, destination: Path) -> Path:
    """Unpack a .zip, .tar(.gz) or single gzipped .tex into ``destination``; returns it."""
    destination.mkdir(parents=True, exist_ok=True)
    data = archive.read_bytes()
    if data.startswith(b"PK"):
        with zipfile.ZipFile(io.BytesIO(data)) as bundle:
            _safe_extract_zip(bundle, destination)
        return destination
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    try:
        with tarfile.open(fileobj=io.BytesIO(data)) as bundle:
            for member in bundle.getmembers():
                target = (destination / member.name).resolve()
                if not str(target).startswith(str(destination.resolve())) or member.issym() or member.islnk():
                    continue
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(bundle.extractfile(member).read())
        return destination
    except tarfile.TarError:
        (destination / "main.tex").write_bytes(data)  # arXiv ships single-file papers as bare gzip
        return destination


def _safe_extract_zip(bundle: zipfile.ZipFile, destination: Path) -> None:
    root = destination.resolve()
    for info in bundle.infolist():
        target = (destination / info.filename).resolve()
        if not str(target).startswith(str(root)):
            continue
        if info.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(bundle.read(info))


def find_main(root: Path, hint: str | None = None) -> str:
    """The file holding ``\\documentclass`` and ``\\begin{document}``."""
    if hint:
        candidate = root / hint
        if not candidate.is_file():
            raise ValueError(f"main_file {hint!r} is not in the project")
        return candidate.relative_to(root).as_posix()
    candidates = []
    for path in sorted(root.rglob("*.tex")):
        text = read_text(path)
        if re.search(r"^[^%\n]*\\documentclass", text, re.M) and "\\begin{document}" in text:
            candidates.append(path)
    if not candidates:
        raise ValueError("no .tex file with \\documentclass and \\begin{document} was found")
    # Prefer the conventional names, then the largest file.
    candidates.sort(key=lambda p: (p.name not in {"main.tex", "paper.tex", "ms.tex"}, -p.stat().st_size))
    return candidates[0].relative_to(root).as_posix()


def read_text(path: Path) -> str:
    data = path.read_bytes()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def load_project(root: Path, main_hint: str | None = None) -> LatexProject:
    main = find_main(root, main_hint)
    seen: list[str] = []
    queue = [main]
    files: list[TexFile] = []
    while queue:
        relative = queue.pop(0)
        if relative in seen:
            continue
        seen.append(relative)
        path = root / relative
        tex = TexFile(relative, read_text(path), is_main=relative == main)
        parser = _Parser(tex.text, prefix=f"f{len(files)}")
        tex.pieces = parser.parse_main() if tex.is_main else parser.parse_included()
        files.append(tex)
        base = (root / relative).parent
        for name in parser.includes:
            for candidate in (base / name, root / name, base / f"{name}.tex", root / f"{name}.tex"):
                if candidate.is_file() and candidate.suffix == ".tex":
                    queue.append(candidate.resolve().relative_to(root.resolve()).as_posix())
                    break
    return LatexProject(root, main, files)


# --- the parser -------------------------------------------------------------------------------

class _Parser:
    """Walks one .tex file into raw pieces and translatable segments."""

    COMMAND = re.compile(r"\\([A-Za-z@]+\*?|.)", re.S)
    BLANK_LINE = re.compile(r"\r?\n[ \t\r]*\n")  # Windows sources end lines with \r\n

    def __init__(self, text: str, prefix: str):
        self.text = text
        self.prefix = prefix
        self.pieces: list[Piece] = []
        self.includes: list[str] = []
        self.counter = 0
        self.section: tuple[str, ...] = ()

    # entry points

    def parse_main(self) -> list[Piece]:
        begin = self.text.find("\\begin{document}")
        end = self.text.rfind("\\end{document}")
        if begin < 0:
            return self.parse_included()
        begin += len("\\begin{document}")
        end = end if end > begin else len(self.text)
        self._preamble(0, begin)
        self._block(begin, end)
        self._raw(self.text[end:], "skip")
        return self.pieces

    def parse_included(self) -> list[Piece]:
        self._block(0, len(self.text))
        return self.pieces

    # helpers

    def _key(self) -> str:
        self.counter += 1
        return f"{self.prefix}:{self.counter}"

    def _raw(self, value: str, kind: str = "raw") -> None:
        if not value:
            return
        if self.pieces and self.pieces[-1].kind == kind:
            self.pieces[-1].raw += value
        else:
            self.pieces.append(Piece(kind, raw=value))

    def _comment_end(self, pos: int) -> int:
        newline = self.text.find("\n", pos)
        return len(self.text) if newline < 0 else newline + 1

    def _group_end(self, pos: int) -> int:
        """Index after the ``}`` matching the ``{`` at ``pos``."""
        depth, i, text = 0, pos, self.text
        while i < len(text):
            char = text[i]
            if char == "\\":
                i += 2
                continue
            if char == "%":
                i = self._comment_end(i)
                continue
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return i + 1
            i += 1
        return len(text)

    def _optional_end(self, pos: int) -> int:
        depth, i, text = 0, pos, self.text
        while i < len(text):
            char = text[i]
            if char == "\\":
                i += 2
                continue
            if char == "{":
                i = self._group_end(i)
                continue
            if char == "[":
                depth += 1
            elif char == "]":
                depth -= 1
                if depth == 0:
                    return i + 1
            i += 1
        return len(text)

    def _arguments_end(self, pos: int, *, limit: int | None = None) -> int:
        """Skip every ``[..]``/``{..}`` argument that directly follows a command."""
        text = self.text
        i = pos
        while True:
            j = i
            # Arguments may follow after spaces, but never on the next line: a paragraph
            # that opens with "{" must not be swallowed as the argument of the line above.
            while j < len(text) and text[j] in " \t" and (limit is None or j < limit):
                j += 1
            if j < len(text) and text[j] == "{":
                i = self._group_end(j)
            elif j < len(text) and text[j] == "[" and j == i:
                i = self._optional_end(j)
            else:
                return i

    def _env_name(self, pos: int) -> str | None:
        match = re.match(r"\\(begin|end)\s*\{([^}]+)\}", self.text[pos:pos + 80])
        return match.group(2) if match else None

    def _env_end(self, pos: int, name: str) -> tuple[int, int]:
        """(start of the matching ``\\end{name}``, index after it) for the ``\\begin`` at ``pos``."""
        pattern = re.compile(r"\\(begin|end)\s*\{" + re.escape(name) + r"\}")
        depth = 0
        verbatim = name in VERBATIM_ENVS
        for match in pattern.finditer(self.text, pos):
            if match.group(1) == "begin":
                depth += 1
                if verbatim and depth > 1:
                    depth -= 1
            else:
                depth -= 1
                if depth == 0:
                    return match.start(), match.end()
        return len(self.text), len(self.text)

    def _math_end(self, pos: int) -> int:
        text = self.text
        if text.startswith("$$", pos):
            end = text.find("$$", pos + 2)
            return len(text) if end < 0 else end + 2
        if text.startswith("\\(", pos):
            end = text.find("\\)", pos + 2)
            return len(text) if end < 0 else end + 2
        if text.startswith("\\[", pos):
            end = text.find("\\]", pos + 2)
            return len(text) if end < 0 else end + 2
        i = pos + 1
        while i < len(text):
            if text[i] == "\\":
                i += 2
                continue
            if text[i] == "$":
                return i + 1
            i += 1
        return len(text)

    # preamble: only \title is prose

    PREAMBLE_PROSE = re.compile(r"(?<!\\)\\(title|thanks)\s*(\[[^\]]*\])?\s*\{")

    def _preamble(self, start: int, end: int) -> None:
        """The preamble is copied, except the title and every ``\\thanks`` note (author footnotes)."""
        i = start
        for match in self.PREAMBLE_PROSE.finditer(self.text, start, end):
            if match.start() < i:
                continue  # a \\thanks inside the title was handled with the title
            if self.text.rfind("%", self.text.rfind("\n", 0, match.start()) + 1, match.start()) >= 0:
                continue  # commented out
            open_at = match.end() - 1
            close_at = self._group_end(open_at)
            self._raw(self.text[i:open_at + 1], "skip")
            kind = "heading" if match.group(1) == "title" else "footnote"
            segment = Segment(self._key(), kind=kind)
            self._inline(segment, open_at + 1, close_at - 1, stop="group")
            self.pieces.append(Piece(kind if kind == "heading" else "caption", segment=segment))
            self._raw("}")
            i = close_at
        self._raw(self.text[i:end], "skip")

    # block level

    def _block(self, start: int, end: int) -> None:
        text = self.text
        i = start
        while i < end:
            char = text[i]
            if char in " \t\n\r":
                j = i
                while j < end and text[j] in " \t\n\r":
                    j += 1
                self._raw(text[i:j])
                i = j
                continue
            if char == "%":
                j = min(self._comment_end(i), end)
                self._raw(text[i:j])
                i = j
                continue
            if char == "\\":
                match = self.COMMAND.match(text, i)
                name = match.group(1) if match else ""
                bare = name.rstrip("*")
                if bare == "begin":
                    i = self._environment(i, end)
                    continue
                if bare == "end":
                    j = self._arguments_end(match.end())
                    self._raw(text[i:j])
                    i = j
                    continue
                if bare in SECTIONING:
                    i = self._heading(i, match.end(), end, kind="caption" if "caption" in bare else "heading")
                    continue
                if bare == "item":
                    j = match.end()
                    if j < end and text[j] == "[":
                        j = self._optional_end(j)
                    self._raw(text[i:j])
                    i = j
                    continue
                if bare in BLOCK_COMMANDS or bare == "par":
                    j = min(self._arguments_end(match.end()), end)
                    if bare in INCLUDES:
                        argument = re.search(r"\{([^}]*)\}", text[match.end():j])
                        if argument:
                            self.includes.append(argument.group(1).strip())
                    self._raw(text[i:j])
                    i = j
                    continue
            segment = Segment(self._key(), kind="paragraph", section=self.section)
            j = self._inline(segment, i, end, stop="block")
            if j == i:  # nothing consumed: never loop forever on a character the inline parser refuses
                self._raw(text[i])
                i += 1
                continue
            self._piece(segment, "paragraph")
            i = j

    def _piece(self, segment: Segment, kind: str) -> None:
        # Leading/trailing whitespace belongs to the layout, not the passage.
        self.pieces.append(Piece(kind, segment=segment))

    def _heading(self, start: int, after_name: int, end: int, *, kind: str) -> int:
        text = self.text
        j = after_name
        while j < end and text[j] in " \t":
            j += 1
        if j < end and text[j] == "[":
            j = self._optional_end(j)
        while j < end and text[j] in " \t\n":
            j += 1
        if j >= end or text[j] != "{":
            self._raw(text[start:j])
            return j
        close = self._group_end(j)
        self._raw(text[start:j + 1])
        segment = Segment(self._key(), kind=kind, section=self.section)
        self._inline(segment, j + 1, close - 1, stop="group")
        self.pieces.append(Piece("heading" if kind == "heading" else "caption", segment=segment))
        if kind == "heading":
            title = " ".join(segment.plain_text().split())[:80]
            self.section = (*self.section[-2:], title) if title else self.section
        self._raw("}")
        return close

    def _environment(self, start: int, end: int) -> int:
        text = self.text
        name = self._env_name(start) or ""
        body_end, after = self._env_end(start, name)
        header_end = self._arguments_end(start + len("\\begin"), limit=None)
        if name in MATH_ENVS:
            segment = Segment(self._key(), kind="paragraph", section=self.section)
            j = self._inline(segment, start, end, stop="block")
            self._piece(segment, "paragraph")
            return j
        if name in VERBATIM_ENVS or name.startswith("lstlisting") or name.startswith("minted"):
            self._raw(text[start:after], "skip")
            return after
        if name in TABULAR_ENVS:
            self._raw(text[start:header_end])
            self._table(header_end, body_end)
            self._raw(text[body_end:after])
            return after
        self._raw(text[start:header_end])
        self._block(header_end, body_end)
        self._raw(text[body_end:after])
        return after

    def _table(self, start: int, end: int) -> None:
        i = start
        text = self.text
        while i < end:
            if text[i] in " \t\n\r":
                j = i
                while j < end and text[j] in " \t\n\r":
                    j += 1
                self._raw(text[i:j])
                i = j
                continue
            if text[i] == "&":
                self._raw("&")
                i += 1
                continue
            if text.startswith("\\\\", i):
                j = i + 2
                if j < end and text[j] == "[":
                    j = self._optional_end(j)
                self._raw(text[i:j])
                i = j
                continue
            if text[i] == "%":
                j = min(self._comment_end(i), end)
                self._raw(text[i:j])
                i = j
                continue
            segment = Segment(self._key(), kind="table_cell", section=self.section)
            j = self._inline(segment, i, end, stop="cell")
            if j == i:
                self._raw(text[i])
                i += 1
                continue
            self._piece(segment, "cell")
            i = j

    # inline level

    def _inline(self, segment: Segment, start: int, end: int, *, stop: str) -> int:
        """Fill ``segment`` from ``start``; returns where it stopped.

        ``stop`` is ``block`` (a paragraph: stop at a blank line or a block
        construct), ``group`` (stop at ``end``) or ``cell`` (stop at ``&`` or ``\\\\``).
        """
        text = self.text
        i = start
        opened: list[str] = []  # "pair" for a group we opened with a marker
        while i < end:
            char = text[i]
            if stop == "block" and self.BLANK_LINE.match(text, i):
                break
            if stop == "cell" and (char == "&" or text.startswith("\\\\", i)) and not opened:
                break
            if char == "%":
                j = min(self._comment_end(i), end)
                segment.keep(text[i:j])
                i = j
                continue
            if char == "$":
                j = min(self._math_end(i), end)
                segment.keep(text[i:j], "formula")
                i = j
                continue
            if char == "~":
                segment.keep("~", "symbol")
                i += 1
                continue
            if char == "{":
                segment.open("{")
                opened.append("pair")
                i += 1
                continue
            if char == "}":
                if not opened:
                    break  # the end of an enclosing group we do not own
                opened.pop()
                segment.close("}")
                i += 1
                continue
            if char == "\\":
                j = self._inline_command(segment, i, end, stop, opened)
                if j is None:
                    break
                i = j
                continue
            j = i
            while j < end and text[j] not in "\\%${}~&" and not (stop == "block" and self.BLANK_LINE.match(text, j)):
                j += 1
            if j < end and text[j] == "&" and stop != "cell":
                j += 1  # a stray & outside a table is text
            if j == i:
                segment.text(text[i])
                j = i + 1
            else:
                segment.text(text[i:j])
            i = j
        # Balance any group left open (malformed source or an argument cut at ``end``).
        while opened:
            opened.pop()
            segment.close("")
        return i

    def _inline_command(self, segment: Segment, start: int, end: int, stop: str, opened: list[str]) -> int | None:
        text = self.text
        match = self.COMMAND.match(text, start)
        name = match.group(1)
        bare = name.rstrip("*")
        after = match.end()
        if len(name) == 1 and name in TEXT_ESCAPES:
            segment.text(name, raw=text[start:after])
            return after
        if name in {"(", "["}:
            j = min(self._math_end(start), end)
            segment.keep(text[start:j], "formula")
            return j
        if name == "\\":
            j = after
            if j < end and text[j] == "[":
                j = self._optional_end(j)
            segment.keep(text[start:j], "symbol")
            return j
        if bare == "begin":
            env = self._env_name(start) or ""
            if env in MATH_ENVS:
                _, j = self._env_end(start, env)
                segment.keep(text[start:j], "formula")
                return j
            if stop == "block":
                return None  # any other environment ends the paragraph
            _, j = self._env_end(start, env)
            segment.keep(text[start:j], "other")
            return j
        if bare == "end":
            if stop == "block":
                return None
            j = self._arguments_end(after)
            segment.keep(text[start:j])
            return j
        if stop == "block" and (bare in SECTIONING or bare in {"item", "par"} or bare in INCLUDES
                                or bare in {"maketitle", "bibliography", "printbibliography", "appendix"}):
            return None
        if bare in WRAP:
            return self._wrap(segment, start, after, end, bare)
        if bare == "href":
            first = self._skip_spaces(after)
            if first < end and text[first] == "{":
                url_end = self._group_end(first)
                second = self._skip_spaces(url_end)
                if second < end and text[second] == "{":
                    close = self._group_end(second)
                    segment.open(text[start:second + 1])
                    self._inline(segment, second + 1, close - 1, stop="group")
                    segment.close("}")
                    return close
        if bare in {"multicolumn", "multirow"}:
            j = after
            groups = 0
            while groups < 2:
                j = self._skip_spaces(j)
                if j < end and text[j] == "[":
                    j = self._optional_end(j)
                    continue
                if j < end and text[j] == "{":
                    j = self._group_end(j)
                    groups += 1
                else:
                    break
            j = self._skip_spaces(j)
            if groups == 2 and j < end and text[j] == "{":
                close = self._group_end(j)
                segment.open(text[start:j + 1])
                self._inline(segment, j + 1, close - 1, stop="group")
                segment.close("}")
                return close
        if bare in CHILD:
            j = self._skip_spaces(after)
            if j < end and text[j] == "[":
                j = self._optional_end(j)
            if j < end and text[j] == "{":
                close = self._group_end(j)
                child = Segment(self._key(), kind="footnote", section=self.section)
                self._inline(child, j + 1, close - 1, stop="group")
                segment.nest(text[start:j + 1], child, "}")
                return close
        # Anything else - \cite, \ref, \label, \LaTeX, \ldots, accents, font switches,
        # user macros - is kept whole with its arguments.
        j = min(self._arguments_end(after), end)
        kind = "citation" if bare.startswith("cite") or bare in {"ref", "eqref", "autoref", "cref", "Cref"} else "other"
        segment.keep(text[start:j], kind)
        return j

    def _wrap(self, segment: Segment, start: int, after: int, end: int, name: str) -> int:
        text = self.text
        j = after
        # \textcolor{red}{words}, \colorbox{..}{..}: the first group is an argument, not prose.
        if name in {"textcolor", "colorbox"}:
            j = self._skip_spaces(j)
            if j < end and text[j] == "[":
                j = self._optional_end(j)
            j = self._skip_spaces(j)
            if j < end and text[j] == "{":
                j = self._group_end(j)
        j = self._skip_spaces(j)
        if j >= end or text[j] != "{":
            segment.keep(text[start:j])
            return j
        close = self._group_end(j)
        segment.open(text[start:j + 1])
        self._inline(segment, j + 1, close - 1, stop="group")
        segment.close("}")
        return close

    def _skip_spaces(self, pos: int) -> int:
        while pos < len(self.text) and self.text[pos] in " \t":
            pos += 1
        return pos


# --- writing ----------------------------------------------------------------------------------

def render_file(tex: TexFile, translations: dict[int, str | None], *, bilingual: bool) -> str:
    out = []
    for piece in tex.pieces:
        if piece.kind in {"raw", "skip"}:
            out.append(piece.raw)
            continue
        segment = piece.segment
        translated = restore(segment, translations, escape=escape)
        original = segment.original()
        if not bilingual or translations.get(id(segment)) is None:
            out.append(translated)
        elif piece.kind == "paragraph":
            out.append(f"{original}\n\n{translated}")
        elif piece.kind in {"heading", "caption"}:
            out.append(f"{translated} / {original}")
        else:
            out.append(translated)
    text = "".join(out)
    if tex.is_main:
        text = add_chinese_support(text)
    return text


def add_chinese_support(text: str) -> str:
    """Load ctex right after \\documentclass unless the file already sets up Chinese."""
    if re.search(r"\\usepackage(\[[^\]]*\])?\{(ctex|xeCJK|CJKutf8|CJK)\}", text) or "\\documentclass" not in text:
        return text
    match = re.search(r"^[^%\n]*\\documentclass(\[[^\]]*\])?\{[^}]*\}[^\n]*\n", text, re.M)
    if not match:
        return text
    return text[:match.end()] + CHINESE_PACKAGE + text[match.end():]


def write_project(project: LatexProject, destination: Path, translations: dict[int, str | None], *,
                  bilingual: bool) -> Path:
    """Copy the whole project to ``destination`` with every parsed .tex file translated."""
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(project.root, destination)
    for tex in project.files:
        # newline="" writes the line endings the source had, instead of translating them again.
        (destination / tex.path).write_text(render_file(tex, translations, bilingual=bilingual), encoding="utf-8",
                                            newline="")
    return destination


def zip_directory(directory: Path, archive: Path) -> Path:
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(directory.rglob("*")):
            if path.is_file() and path.suffix not in {".aux", ".log", ".out", ".toc", ".fls", ".fdb_latexmk", ".xdv"}:
                bundle.write(path, path.relative_to(directory).as_posix())
    return archive


# --- compiling --------------------------------------------------------------------------------

@dataclass
class CompileResult:
    pdf: Path | None
    engine: str
    log_tail: str
    seconds: float = 0.0


def compile_project(root: Path, main: str, *, engine: str = "xelatex", timeout: int = COMPILE_TIMEOUT) -> CompileResult:
    """Build ``main`` in place: engine, bibtex when needed and possible, engine twice more."""
    import time

    started = time.monotonic()
    executable = shutil.which(engine)
    if not executable:
        return CompileResult(None, engine, f"{engine} is not installed on this machine")
    main_path = root / main
    workdir = main_path.parent
    stem = main_path.stem
    base = [executable, "-interaction=nonstopmode", "-file-line-error"]
    version = subprocess.run([executable, "--version"], capture_output=True, text=True, errors="replace").stdout
    if "MiKTeX" in version:
        base.append("-enable-installer")  # fetch missing packages instead of stopping at a dialog
    log_tail = ""
    env = {**os.environ, "max_print_line": "1000"}

    def run(command: list[str]) -> int:
        nonlocal log_tail
        remaining = max(30, timeout - (time.monotonic() - started))
        try:
            finished = subprocess.run(command, cwd=workdir, capture_output=True, text=True, errors="replace",
                                      timeout=remaining, stdin=subprocess.DEVNULL, env=env)
        except subprocess.TimeoutExpired:
            log_tail = f"{command[0]} timed out"
            return -1
        log_tail = (finished.stdout + finished.stderr)[-3000:]
        return finished.returncode

    run([*base, main_path.name])
    aux = workdir / f"{stem}.aux"
    bbl = workdir / f"{stem}.bbl"
    if aux.is_file() and "\\bibdata" in aux.read_text(encoding="utf-8", errors="replace") and not bbl.is_file():
        if shutil.which("bibtex"):
            run([shutil.which("bibtex"), stem])
    run([*base, main_path.name])
    code = run([*base, main_path.name])
    pdf = workdir / f"{stem}.pdf"
    ok = pdf.is_file() and pdf.stat().st_size > 0 and code != -1
    return CompileResult(pdf if ok else None, engine, log_tail, round(time.monotonic() - started, 1))


# --- checking -------------------------------------------------------------------------------

MATH_PATTERN = re.compile(r"\$\$.+?\$\$|\$[^$]+?\$|\\\(.+?\\\)|\\\[.+?\\\]", re.S)
ENV_PATTERN = re.compile(r"\\(begin|end)\s*\{([^}]+)\}")
REFERENCE_PATTERN = re.compile(r"\\(" + "|".join(REFERENCE_COMMANDS) + r")\*?((?:\[[^\]]*\])*\{[^}]*\})")


def skeleton(text: str) -> dict[str, Counter]:
    """What a translation must not change: formulas, references, environments, brace balance."""
    body = re.sub(r"(?<!\\)%[^\n]*", "", text)  # comments are copied, but may hold anything
    body = body.replace("\\$", "").replace("\\{", "").replace("\\}", "")
    return {
        "math": Counter(" ".join(m.split()) for m in MATH_PATTERN.findall(body)),
        "environments": Counter(f"{kind}:{name}" for kind, name in ENV_PATTERN.findall(body)),
        "references": Counter(f"{name}{args}" for name, args in REFERENCE_PATTERN.findall(body)),
        "braces": Counter({"open": body.count("{"), "close": body.count("}")}),
    }


def check(project: LatexProject, translated_root: Path) -> list[dict]:
    """Major problems where a translated file's structure differs from its source."""
    problems = []
    for tex in project.files:
        before = skeleton(add_chinese_support(tex.text) if tex.is_main else tex.text)
        after = skeleton(read_text(translated_root / tex.path))
        for aspect in ("math", "environments", "references"):
            if before[aspect] != after[aspect]:
                lost = sum((before[aspect] - after[aspect]).values())
                added = sum((after[aspect] - before[aspect]).values())
                problems.append({"kind": f"latex_{aspect}_changed", "severity": "major", "count": lost + added,
                                 "detail": f"{tex.path}: {lost} {aspect} items lost and {added} added by the translation",
                                 "sample": [*list((before[aspect] - after[aspect]).keys())[:3],
                                            *list((after[aspect] - before[aspect]).keys())[:3]]})
        if after["braces"]["open"] != after["braces"]["close"] and before["braces"]["open"] == before["braces"]["close"]:
            problems.append({"kind": "latex_braces_unbalanced", "severity": "major", "count": 1,
                             "detail": f"{tex.path}: braces no longer balance after translation"})
    return problems


LEFTOVER_STRIP = re.compile(r"(?<!\\)%[^\n]*|\$\$.+?\$\$|\$[^$]*\$|\\[A-Za-z@]+\*?(\[[^\]]*\])*(\{[^{}]*\})*|\\.", re.S)
WORD = re.compile(r"[A-Za-z]{3,}")


def leftover_prose(project: LatexProject) -> dict:
    """Words of prose the parser left in copied layout, against all prose words it translated.

    The PDF route reconciles its capture against an independent extractor; this
    is the same idea for a source: anything that reads as prose but was copied
    rather than translated is English left in the output.
    """
    leftover: list[str] = []
    translated = 0
    for tex in project.files:
        for piece in tex.pieces:
            if piece.kind == "raw":
                leftover.extend(WORD.findall(LEFTOVER_STRIP.sub(" ", piece.raw)))
            elif piece.segment is not None:
                for segment in (piece.segment, *piece.segment.children()):
                    translated += len(WORD.findall(segment.plain_text()))
                    leftover.extend(_prose_in_kept_macros(segment))
    total = translated + len(leftover)
    return {"leftover_words": len(leftover), "translated_words": translated,
            "share": round(len(leftover) / total, 4) if total else 0.0, "sample": leftover[:20]}


KEPT_MACRO = re.compile(r"\\([A-Za-z@]+)\*?")
MIN_MACRO_PROSE_WORDS = 3


def _prose_in_kept_macros(segment) -> list[str]:
    """Words inside a user macro the parser kept whole, e.g. ``\\todo{check these results}``.

    A macro not known to wrap prose is copied, arguments and all; when an
    argument reads as a phrase, that phrase stays English in the output.
    """
    found: list[str] = []
    for part in segment.parts:
        if part.kind != "keep" or part.marker_kind != "other" or not part.value.startswith("\\"):
            continue
        name = KEPT_MACRO.match(part.value)
        if not name or name.group(1) in REFERENCE_COMMANDS or name.group(1) in BLOCK_COMMANDS \
                or name.group(1) in {"begin", "end", "includegraphics", "label", "url", "href"}:
            continue
        for group in re.findall(r"\{([^{}]*)\}", part.value):
            words = WORD.findall(LEFTOVER_STRIP.sub(" ", group))
            if len(words) >= MIN_MACRO_PROSE_WORDS:
                found.extend(words)
    return found
