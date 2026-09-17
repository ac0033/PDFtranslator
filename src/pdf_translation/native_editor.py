"""Edit simple-font TJ strings while retaining every other source byte.

The editor never decides what is translatable. Callers supply exact glyph IDs;
its job is to keep the advance of each removed glyph and verify the remaining
source glyphs. Unsupported encodings and text-state variants fail closed.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
import re

import pymupdf
from babeldoc.pdfminer.psparser import PSBaseParser, PSEOF, PSKeyword

from .pdf_fidelity import _unordered_close_equal, compare_visual_primitives
from .source_preserving import SourcePreservingUnsupported, _changed_pixels_outside


@dataclass(frozen=True)
class NativeGlyph:
    index: int
    text: str
    font: str
    size: float
    origin: tuple[float, float]
    bbox: tuple[float, float, float, float]
    ink_bounds: tuple[float, float, float, float]
    code: int
    advance: float
    token_index: int
    glyph_name: str | None = None
    code_bytes: int = 1
    direction: tuple[float, float] = (1.0, 0.0)


@dataclass
class NativePage:
    stream: bytes
    tokens: list
    glyphs: list[NativeGlyph]
    strings: dict[int, list[NativeGlyph]]
    contents_xref: int


class CIDWidths(dict):
    def __init__(self, default):super().__init__();self.default=default
    def __missing__(self, key):return self.default
    def __contains__(self,key):return isinstance(key,int) and 0<=key<=65535


def _number_array(value):
    tokens=re.findall(r'\[|\]|[-+]?\d+(?:\.\d+)?',value);position=0
    def parse():
        nonlocal position
        if tokens[position]!='[':raise SourcePreservingUnsupported('invalid font width array')
        position+=1;result=[]
        while position<len(tokens) and tokens[position]!=']':
            if tokens[position]=='[':result.append(parse())
            else:result.append(float(tokens[position]));position+=1
        if position>=len(tokens):raise SourcePreservingUnsupported('unterminated font width array')
        position+=1;return result
    return parse()


def _font_widths(document, xref):
    subtype=document.xref_get_key(xref,'Subtype')[1]
    if subtype=='/Type0':
        if document.xref_get_key(xref,'Encoding')[1]!='/Identity-H':
            raise SourcePreservingUnsupported('native CID editor requires Identity-H encoding')
        descendants=document.xref_get_key(xref,'DescendantFonts')[1]
        matches=re.findall(r'(\d+)\s+0\s+R',descendants)
        if len(matches)!=1:raise SourcePreservingUnsupported('ambiguous CID descendant font')
        child=int(matches[0]);kind,default=document.xref_get_key(child,'DW')
        widths=CIDWidths(float(default) if kind!='null' else 1000.)
        kind,array=document.xref_get_key(child,'W')
        if kind=='null':return widths
        if kind=='xref':array=document.xref_object(int(array.split()[0]))
        entries=_number_array(array);i=0
        while i<len(entries):
            first=int(entries[i]);second=entries[i+1]
            if isinstance(second,list):
                for offset,width in enumerate(second):widths[first+offset]=width
                i+=2
            else:
                last=int(second);width=entries[i+2]
                if not 0<=first<=last<=65535:raise SourcePreservingUnsupported('invalid CID width interval')
                for code in range(first,last+1):widths[code]=width
                i+=3
        return widths
    if subtype not in ('/Type1','/TrueType'):
        raise SourcePreservingUnsupported('native editor requires a supported outline font')
    first = int(document.xref_get_key(xref, 'FirstChar')[1])
    kind, widths = document.xref_get_key(xref, 'Widths')
    if kind == 'xref':widths = document.xref_object(int(widths.split()[0]))
    values = [float(v) for v in re.findall(r'[-+]?\d+(?:\.\d+)?', widths)]
    return {first + i: value for i, value in enumerate(values)}


def traced_glyphs(span):
    """Collapse MuPDF's zero-width Unicode continuation entries for one ligature."""
    result=[]
    for code,gid,origin,bbox in span['chars']:
        if gid<0:
            if not result or tuple(origin)!=tuple(result[-1][2]):
                raise SourcePreservingUnsupported('unanchored Unicode glyph continuation')
            text,previous_gid,previous_origin,previous_box=result[-1]
            result[-1]=(text+chr(code),previous_gid,previous_origin,
                        (min(previous_box[0],bbox[0]),min(previous_box[1],bbox[1]),max(previous_box[2],bbox[2]),max(previous_box[3],bbox[3])))
        else:result.append((chr(code),gid,origin,bbox))
    return result


def inspect_native_page(page: pymupdf.Page) -> NativePage:
    if len(page.get_contents()) != 1:
        raise SourcePreservingUnsupported('native editor requires one source content stream')
    xref = page.get_contents()[0]
    stream = page.parent.xref_stream(xref)
    parser = PSBaseParser(BytesIO(stream))
    tokens = []
    try:
        while True:
            tokens.append(parser.nexttoken())
    except PSEOF:
        pass
    font_bounds = {}
    font_names = {}
    cache=getattr(page.parent,"_native_font_cache",{})
    page.parent._native_font_cache=cache
    for f in page.get_fonts():
        if f[0] in cache:
            cached=cache[f[0]];font_bounds[f[3].split('+')[-1]]=cached['bounds']
            font_names[f[3].split('+')[-1]]=cached['names'];continue
        font_data=page.parent.extract_font(f[0])[3]
        native_font = pymupdf.Font(fontbuffer=font_data)
        # With no PDF Encoding override, the embedded Type1 encoding is authoritative.
        if page.parent.xref_get_key(f[0], 'Encoding')[0]=='null':
            header=font_data.split(b'currentfile eexec')[0]
            font_names[f[3].split('+')[-1]]={int(code):name.decode('ascii') for code,name in
                re.findall(rb'dup\s+(\d+)\s+/([A-Za-z0-9_.]+)\s+put',header)}
        font_bounds[f[3].split('+')[-1]] = tuple(getattr(native_font.bbox, key) for key in ("x0", "y0", "x1", "y1"))
        cache[f[0]]={'bounds':font_bounds[f[3].split('+')[-1]],'names':font_names.get(f[3].split('+')[-1],{}),'widths':_font_widths(page.parent,f[0])}
    fonts = {f[4]: (f[3].split('+')[-1], cache[f[0]]['widths'], 2 if f[2]=='Type0' else 1, f[0]) for f in page.get_fonts()}
    traced = defaultdict(deque)
    for span in page.get_texttrace():
        if span['wmode'] != 0 or span['type'] != 0:
            raise SourcePreservingUnsupported('native editor requires horizontal filled source text')
        for char in traced_glyphs(span):
            traced[span['font']].append((span, char))
    glyphs, strings, mapping = [], {}, {}
    font = None
    font_stack=[]
    array = False
    for index, (_, token) in enumerate(tokens):
        if isinstance(token, PSKeyword):
            op = token.name
            if op==b'q':font_stack.append(font)
            elif op==b'Q':
                if not font_stack:raise SourcePreservingUnsupported('unbalanced source font state')
                font=font_stack.pop()
            if op in (b'Tj', b"'", b'"'):
                raise SourcePreservingUnsupported('source text show operator is not TJ')
            if op in (b'Tc', b'Tw', b'Tz'):
                expected = 100 if op == b'Tz' else 0
                if tokens[index - 1][1] != expected:
                    raise SourcePreservingUnsupported('source has nondefault character spacing or scale')
            if op == b'Tf':
                font = fonts[tokens[index - 2][1].name]
            if op == b'[':
                array = True
            elif op == b']':
                array = False
        elif isinstance(token, bytes) and array:
            if font is None:
                raise SourcePreservingUnsupported('text has no active font')
            name, widths, code_bytes, font_xref = font
            items = []
            if len(token)%code_bytes:raise SourcePreservingUnsupported('truncated native CID code')
            for offset in range(0,len(token),code_bytes):
                code=int.from_bytes(token[offset:offset+code_bytes],'big')
                if not traced[name] or code not in widths:
                    raise SourcePreservingUnsupported('encoded source does not match traced glyph counts')
                span, char = traced[name].popleft()
                pair = (font_xref, code)
                if pair in mapping and mapping[pair] != char[0]:
                    raise SourcePreservingUnsupported(f'encoded character has inconsistent Unicode mapping: font={font_xref}:{name} code={code} expected={mapping[pair]} actual={char[0]} token={index}')
                mapping[pair] = char[0]
                x, y = char[2]
                bx0, by0, bx1, by1 = font_bounds[name]
                size = span['size']
                ink = (x + bx0 * size, y - by1 * size, x + bx1 * size, y - by0 * size)
                glyph = NativeGlyph(len(glyphs), char[0], name, size, tuple(char[2]),
                    tuple(char[3]), ink, code, widths[code], index, font_names.get(name,{}).get(code),code_bytes,tuple(span['dir']))
                items.append(glyph)
                glyphs.append(glyph)
            strings[index] = items
    if any(traced.values()):
        raise SourcePreservingUnsupported('source contains unaccounted rendered glyphs')
    return NativePage(stream, tokens, glyphs, strings, xref)


def remove_native_glyphs(native: NativePage, selected: set[int]) -> bytes:
    if not selected or not selected <= {g.index for g in native.glyphs}:
        raise ValueError('selected glyph IDs must be a nonempty source subset')
    edits = []
    for index, glyphs in native.strings.items():
        if not any(g.index in selected for g in glyphs):
            continue
        start = native.tokens[index][0]
        end = native.tokens[index + 1][0]
        # TJ numeric adjustments are in thousandths of the font size. Replace
        # deleted glyphs with their exact negative width to retain text advance.
        replacement = ' '.join(format(-g.advance, '.12g') if g.index in selected else f'<{g.code:0{g.code_bytes*2}x}>' for g in glyphs)
        edits.append((start, end, b' ' + replacement.encode('ascii') + b' '))
    from .native_stream_edits import apply_stream_edits
    return apply_stream_edits(native.stream,edits)


def glyph_inventory(glyphs):
    result = defaultdict(list)
    for glyph in glyphs:
        if not glyph.text.isspace():
            result[(glyph.font, glyph.text)].append((glyph.size, glyph.origin, glyph.bbox))
    return dict(result)


def trace_inventory(page):
    result = defaultdict(list)
    for span in page.get_texttrace():
        for char in traced_glyphs(span):
            if not char[0].isspace():
                result[(span['font'], char[0])].append((span['size'], tuple(char[2]), tuple(char[3])))
    return dict(result)


def inventories_equal(a, b):
    return a.keys() == b.keys() and all(_unordered_close_equal(a[key], b[key]) for key in a)


def write_removed_prose_probe(source: Path, page_index: int, output: Path, selected: set[int]) -> dict:
    if output.exists():
        raise FileExistsError(output)
    with pymupdf.open(source) as document:
        page = document[page_index]
        native = inspect_native_page(page)
        retained = glyph_inventory([g for g in native.glyphs if g.index not in selected])
        with pymupdf.open() as target:
            target.insert_pdf(document, from_page=page_index, to_page=page_index)
            target.update_stream(target[0].get_contents()[0], remove_native_glyphs(native, selected))
            target_page = target.reload_page(target[0])
            if not inventories_equal(retained, trace_inventory(target_page)):
                raise SourcePreservingUnsupported('removal changed retained source glyphs')
            primitives = compare_visual_primitives(page, target_page)
            if not primitives['passed']:
                raise SourcePreservingUnsupported('removal changed an immutable object')
            masks = [pymupdf.Rect(g.ink_bounds) + (-0.5, -0.5, 0.5, 0.5) for g in native.glyphs if g.index in selected]
            changed, _ = _changed_pixels_outside(page, target_page, masks)
            if changed:
                raise SourcePreservingUnsupported(f'removal changed {changed} outside pixels')
            output.parent.mkdir(parents=True, exist_ok=True)
            target.save(output, deflate=True)
        with pymupdf.open(output) as saved:
            if not inventories_equal(retained, trace_inventory(saved[0])):
                raise SourcePreservingUnsupported('saved removal changed retained glyphs')
        return {'source_page': page_index + 1, 'removed_prose_glyphs': len(selected),
            'retained_source_glyphs_verified': True, 'changed_pixels_outside_prose': changed,
            'primitives': primitives, 'translation_complete': False, 'formal_use_ready': False}



def strip_nontext_paint(stream: bytes) -> bytes:
    """Isolated glyph assets only: discard painting while preserving text state.

    Never apply this to an output source page. Paths, colors, clipping and text
    advances remain; only non-text paint operations are disabled.
    """
    parser=PSBaseParser(BytesIO(stream));tokens=[]
    try:
        while True:tokens.append(parser.nexttoken())
    except PSEOF:pass
    edits=[]
    for i,(start,token) in enumerate(tokens):
        if not isinstance(token,PSKeyword):continue
        op=token.name
        end=tokens[i+1][0] if i+1<len(tokens) else len(stream)
        if op in (b'BI',b'ID',b'EI'):
            raise SourcePreservingUnsupported('inline images in glyph-only asset are unsupported')
        if op in (b'S',b's',b'f',b'F',b'f*',b'B',b'B*',b'b',b'b*'):
            edits.append((start,end,b' n '))
        elif op in (b'Do',b'sh'):
            if i==0:raise SourcePreservingUnsupported('paint operator lacks its resource name')
            edits.append((tokens[i-1][0],end,b' '))
    from .native_stream_edits import apply_stream_edits
    return apply_stream_edits(stream,edits)
