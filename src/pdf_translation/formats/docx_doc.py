"""DOCX: translate Word paragraphs in place, keeping styles, runs and objects.

Each ``w:p`` in the body, tables, headers, footers, footnotes and endnotes is
one passage. Its runs are grouped by formatting: text in the paragraph's
dominant formatting is plain text, text in any other formatting (bold words, a
different font) becomes a marker pair, and a hyperlink wraps its runs in a
pair too. Everything that is not text - tabs, breaks, images, equations,
fields, footnote references, bookmarks - is kept as a marker and put back as
the original XML. When the translation comes back the paragraph's runs are
rebuilt: each stretch of Chinese gets the formatting of the pair it sits in.

Accuracy check (:func:`check`): the output has as many paragraphs, tables,
images, equations, fields, hyperlinks and footnote references as the source.
"""
from __future__ import annotations

import copy
import io
import re
import zipfile
from collections import Counter
from dataclasses import dataclass, field

from lxml import etree

from .common import Segment, restore
from ..quality import MARKERS

REVISION = "docx-source-v1"
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
PARTS = re.compile(r"^word/(document|header\d*|footer\d*|footnotes|endnotes)\.xml$")


def w(tag: str) -> str:
    return f"{{{W}}}{tag}"


@dataclass
class Paragraph:
    part: str
    element: etree._Element
    segment: Segment
    #: marker -> ("keep", [elements]) | ("open", rPr or hyperlink) | ("close", None)
    slots: dict[str, tuple[str, object]] = field(default_factory=dict)
    base_rpr: etree._Element | None = None


@dataclass
class DocxDocument:
    data: bytes
    trees: dict[str, etree._ElementTree]
    paragraphs: list[Paragraph]

    def segments(self) -> list[Segment]:
        return [paragraph.segment for paragraph in self.paragraphs]


def _rpr_key(run: etree._Element) -> bytes:
    rpr = run.find(w("rPr"))
    if rpr is None:
        return b""
    clean = copy.deepcopy(rpr)
    for noise in (w("lang"), w("noProof"), w("rFonts")):
        for node in clean.findall(noise):
            clean.remove(node)
    return etree.tostring(clean)


def _is_text_run(run: etree._Element) -> bool:
    children = [child for child in run if child.tag != w("rPr")]
    return bool(children) and all(child.tag == w("t") for child in children)


def parse(data: bytes) -> DocxDocument:
    trees: dict[str, etree._ElementTree] = {}
    paragraphs: list[Paragraph] = []
    counter = 0
    with zipfile.ZipFile(io.BytesIO(data)) as bundle:
        for name in bundle.namelist():
            if not PARTS.match(name):
                continue
            tree = etree.parse(io.BytesIO(bundle.read(name)), etree.XMLParser(remove_blank_text=False, huge_tree=True))
            trees[name] = tree
            for element in tree.iter(w("p")):
                if element.getparent() is not None and element.getparent().tag == w("p"):
                    continue
                counter += 1
                paragraph = _paragraph(name, element, f"d:{counter}")
                if paragraph is not None:
                    paragraphs.append(paragraph)
    return DocxDocument(data, trees, paragraphs)


def _paragraph(part: str, element: etree._Element, key: str) -> Paragraph | None:
    style = element.find(f"{w('pPr')}/{w('pStyle')}")
    style_name = (style.get(w("val")) or "") if style is not None else ""
    kind = "heading" if style_name.lower().startswith(("heading", "title")) else "paragraph"
    # Dominant formatting: the run properties covering most text characters.
    weights: Counter = Counter()
    rprs: dict[bytes, etree._Element | None] = {}
    for run in element.iter(w("r")):
        text = "".join(t.text or "" for t in run.findall(w("t")))
        if text:
            k = _rpr_key(run)
            weights[k] += len(text)
            rprs.setdefault(k, run.find(w("rPr")))
    if not weights:
        return None
    base_key = weights.most_common(1)[0][0]
    segment = Segment(key, kind=kind, fold_whitespace=False)
    paragraph = Paragraph(part, element, segment, base_rpr=rprs.get(base_key))
    slots: list[tuple[str, object]] = []  # in order, used to assign markers after segment.marked()

    def walk(container: etree._Element) -> None:
        children = [child for child in container if child.tag != w("pPr")]
        index = 0
        current_rpr: bytes | None = None
        while index < len(children):
            child = children[index]
            tag = child.tag
            is_field = tag == w("r") and (child.find(w("fldChar")) is not None or child.find(w("instrText")) is not None)
            if tag == w("r") and not is_field and child.find(w("t")) is not None:
                k = _rpr_key(child)
                if k != base_key and k != current_rpr:
                    if current_rpr is not None:
                        segment.close("[formatting ends]")
                        slots.append(("close", None))
                    segment.open("[formatted text starts]")
                    slots.append(("open", child.find(w("rPr"))))
                    current_rpr = k
                elif k == base_key and current_rpr is not None:
                    segment.close("[formatting ends]")
                    slots.append(("close", None))
                    current_rpr = None
                # A run may mix text with tabs and breaks: text is translated, the rest kept in place.
                for item in child:
                    if item.tag == w("rPr"):
                        continue
                    if item.tag == w("t"):
                        segment.text(item.text or "")
                    else:
                        segment.keep(f"[{etree.QName(item).localname}]")
                        slots.append(("keep-in-run", (child.find(w("rPr")), item)))
                index += 1
                continue
            if current_rpr is not None:
                segment.close("[formatting ends]")
                slots.append(("close", None))
                current_rpr = None
            if tag == w("hyperlink"):
                segment.open("[link starts]")
                slots.append(("open-link", child))
                walk(child)
                segment.close("[link ends]")
                slots.append(("close", None))
                index += 1
                continue
            if tag == w("r") and child.find(w("fldChar")) is not None and child.find(w("fldChar")).get(w("fldCharType")) == "begin":
                # A complex field runs from its begin run to its end run: keep all of it.
                group = [child]
                depth = 1
                index += 1
                while index < len(children) and depth:
                    group.append(children[index])
                    char = children[index].find(w("fldChar"))
                    if char is not None:
                        depth += {"begin": 1, "end": -1}.get(char.get(w("fldCharType")), 0)
                    index += 1
                segment.keep("[field]")
                slots.append(("keep", group))
                continue
            name = etree.QName(child).localname
            segment.keep(f"[{name}]", "formula" if name.startswith("oMath") else "other")
            slots.append(("keep", [child]))
            index += 1
        if current_rpr is not None:
            segment.close("[formatting ends]")
            slots.append(("close", None))

    walk(element)
    if not segment.has_prose():
        return None
    # Pair each marker with its slot, in order of appearance.
    ordered = MARKERS.findall(segment.marked()[0])
    if len(ordered) != len(slots):
        return None
    paragraph.slots = dict(zip(ordered, slots))
    return paragraph


def _new_run(text: str, rpr: etree._Element | None) -> etree._Element:
    run = etree.Element(w("r"))
    if rpr is not None:
        run.append(copy.deepcopy(rpr))
    t = etree.SubElement(run, w("t"))
    t.text = text
    t.set(XML_SPACE, "preserve")
    return run


def _rebuild(paragraph: Paragraph, marked_text: str, *, append_after: etree._Element | None = None) -> etree._Element:
    """A new paragraph element for ``marked_text`` (translated text with markers)."""
    source = paragraph.element
    target = etree.Element(source.tag, nsmap=source.nsmap)
    for key, value in source.attrib.items():
        target.set(key, value)
    ppr = source.find(w("pPr"))
    if ppr is not None:
        target.append(copy.deepcopy(ppr))
    containers: list[etree._Element] = [target]
    rprs: list[etree._Element | None] = [paragraph.base_rpr]
    cursor = 0
    for match in MARKERS.finditer(marked_text):
        text = marked_text[cursor:match.start()]
        if text:
            containers[-1].append(_new_run(text, rprs[-1]))
        kind, payload = paragraph.slots[match.group(0)]
        if kind == "keep":
            for element in payload:
                containers[-1].append(copy.deepcopy(element))
        elif kind == "keep-in-run":
            rpr, element = payload
            run = etree.SubElement(containers[-1], w("r"))
            if rpr is not None:
                run.append(copy.deepcopy(rpr))
            run.append(copy.deepcopy(element))
        elif kind == "open":
            rprs.append(payload)
            containers.append(containers[-1])
        elif kind == "open-link":
            link = etree.SubElement(containers[-1], payload.tag, nsmap=payload.nsmap)
            for key, value in payload.attrib.items():
                link.set(key, value)
            first_run = payload.find(w("r"))
            rprs.append(first_run.find(w("rPr")) if first_run is not None else rprs[-1])
            containers.append(link)
        elif kind == "close":
            if len(containers) > 1:
                containers.pop()
                rprs.pop()
        cursor = match.end()
    tail = marked_text[cursor:]
    if tail:
        containers[-1].append(_new_run(tail, rprs[-1]))
    return target


def render(document: DocxDocument, translations: dict[int, str | None], *, bilingual: bool) -> bytes:
    trees = {name: copy.deepcopy(tree) for name, tree in document.trees.items()}
    # Map each source paragraph to the same element in the copied tree by position.
    originals = {name: {id(element): index for index, element in enumerate(tree.iter(w("p")))}
                 for name, tree in document.trees.items()}
    copies = {name: list(tree.iter(w("p"))) for name, tree in trees.items()}
    for paragraph in document.paragraphs:
        translated = translations.get(id(paragraph.segment))
        if not translated:
            continue
        position = originals[paragraph.part][id(paragraph.element)]
        element = copies[paragraph.part][position]
        _, markers = paragraph.segment.marked()
        if sorted(MARKERS.findall(translated)) != sorted(markers):
            continue
        rebuilt = _rebuild(paragraph, translated.strip())
        parent = element.getparent()
        if bilingual:
            parent.insert(parent.index(element) + 1, rebuilt)
        else:
            parent.replace(element, rebuilt)
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(document.data)) as source, zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target:
        for info in source.infolist():
            if info.filename in trees:
                body = etree.tostring(trees[info.filename], xml_declaration=True, encoding="UTF-8", standalone=True)
                target.writestr(info, body)
            else:
                target.writestr(info, source.read(info.filename))
    return output.getvalue()


def _inventory(data: bytes, *, bilingual: bool = False) -> Counter:
    counts: Counter = Counter()
    with zipfile.ZipFile(io.BytesIO(data)) as bundle:
        for name in bundle.namelist():
            if not PARTS.match(name):
                continue
            root = etree.fromstring(bundle.read(name))
            counts["tables"] += len(root.findall(f".//{w('tbl')}"))
            counts["images"] += len(root.findall(".//{*}drawing")) + len(root.findall(".//{*}pict"))
            counts["equations"] += len(root.findall(f".//{{{M}}}oMath"))
            counts["fields"] += len(root.findall(f".//{w('fldChar')}")) + len(root.findall(f".//{w('fldSimple')}"))
            counts["hyperlinks"] += len(root.findall(f".//{w('hyperlink')}"))
            counts["footnote_references"] += len(root.findall(f".//{w('footnoteReference')}"))
            counts["paragraphs"] += len(root.findall(f".//{w('p')}"))
    return counts


def check(source: bytes, translated: bytes) -> list[dict]:
    """Compare the single-language output with the source object by object."""
    before, after = _inventory(source), _inventory(translated)
    problems = []
    for aspect in before:
        if after[aspect] != before[aspect]:
            problems.append({"kind": f"docx_{aspect}_changed", "severity": "major",
                             "count": abs(after[aspect] - before[aspect]),
                             "detail": f"the document has {after[aspect]} {aspect} after translation, {before[aspect]} before"})
    return problems


WORD = re.compile(r"[A-Za-z]{3,}")


def leftover_prose(document: DocxDocument) -> dict:
    """Prose in paragraphs this adapter could not take apart (and so left in English)."""
    covered = {id(p.element): p for p in document.paragraphs}
    leftover, translated = [], 0
    for tree in document.trees.values():
        for element in tree.iter(w("p")):
            text = "".join(t.text or "" for t in element.iter(w("t")))
            if id(element) in covered:
                # Text the paragraph holds beyond what became the unit sits in a kept object
                # (a content control, a tracked insertion, a field result) and stays English.
                segment_words = WORD.findall(covered[id(element)].segment.plain_text())
                translated += len(segment_words)
                remaining = list(WORD.findall(text))
                for word in segment_words:
                    if word in remaining:
                        remaining.remove(word)
                leftover.extend(remaining)
            elif element.getparent() is None or element.getparent().tag != w("p"):
                leftover.extend(WORD.findall(text))
    total = translated + len(leftover)
    return {"leftover_words": len(leftover), "translated_words": translated,
            "share": round(len(leftover) / total, 4) if total else 0.0, "sample": leftover[:20]}
