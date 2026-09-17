import json
from pathlib import Path
import pymupdf
from pdf_translation.native_editor import inspect_native_page,remove_native_glyphs
from pdf_translation.pdf_fidelity import compare_visual_primitives
from pdf_translation.source_preserving import _changed_pixels_outside
from pdf_translation.hashing import sha256_file
import argparse
parser=argparse.ArgumentParser()
parser.add_argument('--source',required=True,type=Path)
parser.add_argument('--output',required=True,type=Path)
parser.add_argument('--proof',required=True,type=Path)
parser.add_argument('--source-page',required=True,type=int)
parser.add_argument('--font',required=True,type=Path)
parser.add_argument('--report',required=True,type=Path)
args=parser.parse_args()
source=args.source;output=args.output;page_index=args.source_page-1
proof=json.loads(args.proof.read_text(encoding='utf-8'))
assert proof['source_page']==args.source_page
selected=set(proof['removed_glyph_ids'])
with pymupdf.open(source) as original,pymupdf.open(output) as final:
 native=inspect_native_page(original[page_index]);source_fonts={g.font for g in native.glyphs}
 with pymupdf.open() as retained,pymupdf.open() as graphics:
  for doc,ids in [(retained,selected),(graphics,{g.index for g in native.glyphs})]:
   doc.insert_pdf(original,from_page=page_index,to_page=page_index);doc.update_stream(doc[0].get_contents()[0],remove_native_glyphs(native,ids))
  with pymupdf.open(stream=retained.tobytes(),filetype='pdf') as a_doc,pymupdf.open(stream=graphics.tobytes(),filetype='pdf') as bg_doc:
   a=a_doc[0].get_pixmap(matrix=pymupdf.Matrix(2,2)).samples;bg=bg_doc[0].get_pixmap(matrix=pymupdf.Matrix(2,2)).samples;b=final[0].get_pixmap(matrix=pymupdf.Matrix(2,2)).samples
   changed_ink=sum(a[i:i+3]!=b[i:i+3] for i in range(0,len(a),3) if a[i:i+3]!=bg[i:i+3]);assert changed_ink==0,changed_ink
 masks=[pymupdf.Rect(g.ink_bounds)+(-.5,-.5,.5,.5) for g in native.glyphs if g.index in selected]
 for asset in proof['assets']:
  assert set(asset['glyph_ids'])<=selected and asset['glyph_geometry_verified'] and asset['changed_pixels']==0
  masks.extend([pymupdf.Rect(asset['source_clip']),pymupdf.Rect(asset['target_clip'])])
 font=pymupdf.Font(fontfile=str(args.font));box=font.bbox
 for span in final[0].get_texttrace():
  if span['font'] in source_fonts:continue
  for char in span['chars']:
   x,y=char[2];size=span['size'];masks.append(pymupdf.Rect(x+box.x0*size-.5,y-box.y1*size-.5,x+box.x1*size+.5,y-box.y0*size+.5))
 outside,_=_changed_pixels_outside(original[page_index],final[0],masks);assert outside==0,(outside,_)
 primitive=compare_visual_primitives(original[page_index],final[0])
 for name in ('page_geometry_equal','code_font_glyphs_text_font_size_bbox_equal','images_digest_and_bbox_equal','vector_paths_equal'):
  assert primitive['checks'][name],name
 assert proof['saved_source_glyph_inventory_verified'] and all(a['changed_pixels']==0 for a in proof['assets'])
 report={'source_page':args.source_page,'source_pdf_sha256':sha256_file(source),'output_sha256':sha256_file(output),
  'unchanged_source_foreground_changed_pixels':changed_ink,'changed_pixels_outside_declared_text_edits':outside,
  'code_glyph_count':primitive['source_code_font_glyph_count'],'code_glyph_geometry_equal':True,'vector_path_count':primitive['source_vector_path_count'],'vector_paths_equal':True,
  'native_asset_count':len(proof['assets']),'all_native_asset_pixel_differences_zero':True,'saved_source_glyph_inventory_verified':True,
  'cross_page_flow':proof['page_flow'],'entire_document_formal_use_ready':False}
args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print({k:v for k,v in report.items() if k!='cross_page_flow'})
