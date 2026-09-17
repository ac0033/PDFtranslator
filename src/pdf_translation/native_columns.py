"""Measure prose columns from actual source lines, including mirrored book pages."""
from collections import Counter
from statistics import median
import re

PROSE_FONTS=('URWPalladioL-Roma','URWPalladioL-Bold','URWPalladioL-Ital','TeXPalladioL-SC',
             'TeXGyrePagella-Regular','TeXGyrePagella-Bold','TeXGyrePagella-Italic','TeXGyrePagella-BoldItalic')


def _mode(values,step=1.):
    bins=Counter(round(x/step)*step for x in values)
    center=bins.most_common(1)[0][0]
    return median(x for x in values if abs(x-center)<=step/2+.001)


def infer_columns(page):
    lines=[]
    for block in page.get_text('dict')['blocks']:
        for line in block.get('lines',[]):
            if tuple(line['dir'])!=(1.,0.):continue
            spans=[s for s in line['spans'] if s['font'] in PROSE_FONTS]
            text=''.join(s['text'] for s in spans)
            if len(re.findall('[A-Za-z]',text))<8:continue
            weight=sum(len(s['text']) for s in spans)
            size=sum(s['size']*len(s['text']) for s in spans)/weight
            lines.append({'bbox':line['bbox'],'size':size,'text':text,'weight':weight})
    width,height=page.rect.width,page.rect.height
    body_candidates=[l for l in lines if height*.08<l['bbox'][1]<height*.9 and l['bbox'][2]-l['bbox'][0]>width*.35 and l['bbox'][0]<width*.3 and 9<=l['size']<=14]
    body_size=_mode([l['size'] for l in body_candidates],.5) if body_candidates else 10.
    footer=[l['bbox'][1] for l in lines if l['bbox'][1]>height*.9 and l['bbox'][2]-l['bbox'][0]>width*.55 and l['size']<body_size*.95]
    footer_top=min(footer)-1 if footer else height
    header_bottom=height*.07
    # The existing simple-font margin-note profile has a declared, verified frame.
    # Detect it by source geometry and fonts rather than by path or page number.
    legacy=bool(lines and all(not any(s['font'].startswith('TeXGyre') for s in b.get('spans',[])) for block in page.get_text('dict')['blocks'] for b in block.get('lines',[]))
                and abs(width-612)<.1 and abs(height-792)<.1)
    if legacy:
        return {'body':[72.,382.8],'margin':[406.74,550.],'header':[72.,550.],
                'header_bottom':60.,'footer_top':height,'body_size':body_size,'profile':'verified_simple_font_margin_notes'}
    left=_mode([l['bbox'][0] for l in body_candidates],2.) if body_candidates else min((l['bbox'][0] for l in lines),default=width*.1)
    notes=[l for l in lines if header_bottom<l['bbox'][1]<footer_top and l['bbox'][0]>width*.6 and l['bbox'][2]-l['bbox'][0]>width*.1 and l['size']<body_size*.95]
    note_left=_mode([l['bbox'][0] for l in notes],2.) if notes else None
    eligible=[l for l in body_candidates if l['bbox'][0]<left+body_size*2 and (note_left is None or l['bbox'][2]<note_left-1)]
    right=max((l['bbox'][2] for l in eligible),default=width-left)+.5
    if note_left is not None:right=min(right,note_left-body_size*.5)
    right=min(right,width-5)
    note_right=min(width-5,max((l['bbox'][2] for l in notes),default=width-left)+.5)
    # Keep the inferred margin frame empty when there is no margin column.
    return {'body':[left,right],'margin':[note_left or width,note_right if note_left else width],
            'header':[min(left,*(l['bbox'][0] for l in lines)) if lines else left,width-min(left,35.)],
            'header_bottom':header_bottom,'footer_top':footer_top,'body_size':body_size,'profile':'measured_source_lines'}


def glyph_column(glyph,geometry):
    if glyph.origin[1]<geometry['header_bottom'] or glyph.origin[1]>geometry['footer_top']:return 'header'
    if geometry['profile']=='verified_simple_font_margin_notes':
        return 'body' if glyph.origin[0]<395 or glyph.font in PROSE_FONTS and glyph.size>=9.5 else 'margin'
    if geometry['margin'][1]<=geometry['margin'][0]:return 'body'
    boundary=(geometry['body'][1]+geometry['margin'][0])/2
    return 'margin' if glyph.origin[0]>=boundary else 'body'
