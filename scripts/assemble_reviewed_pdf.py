"""Assemble individually reviewed pages and verify saved page equivalence.

This gate proves assembly fidelity, not translation quality or live API use.
"""
import argparse
import json
from pathlib import Path
import pymupdf
from pdf_translation.hashing import sha256_file
from pdf_translation.native_editor import trace_inventory,inventories_equal


def assemble(source,index_path,output):
    source=Path(source);index_path=Path(index_path);output=Path(output)
    report_path=output.with_suffix('.assembly.json')
    if output.exists() or report_path.exists():raise FileExistsError(output)
    index=json.loads(index_path.read_text(encoding='utf-8'));root=index_path.parent
    with pymupdf.open(source) as doc:count=len(doc)
    if index['source_pdf_sha256']!=sha256_file(source):raise ValueError('source PDF hash mismatch')
    if index['accepted_source_pages']!=list(range(1,count+1)) or index['remaining_source_pages']:raise ValueError('not every source page is reviewed')
    inputs=[]
    for page in range(1,count+1):
        override=index.get('accepted_artifact_overrides',{}).get(str(page),{})
        pdf=root/override.get('pdf',f'page-{page:03}.zh.pdf')
        acceptance=root/override.get('acceptance',f'page-{page:03}.acceptance.json')
        review=json.loads(acceptance.read_text(encoding='utf-8'))
        if review['source_page']!=page or review['status']!='passed_agent_semantic_and_visual_review':raise ValueError('invalid page review')
        if review.get('output_pdf',pdf.name)!=pdf.name:raise ValueError('review refers to another PDF')
        digest=sha256_file(pdf)
        evidence=[]
        for path in root.glob(f'page-{page:03}*.fidelity.json'):
            data=json.loads(path.read_text(encoding='utf-8'))
            if data.get('output_sha256')==digest:evidence.append(path.name)
        if not evidence:raise ValueError(f'no hash-linked fidelity evidence for {pdf}')
        with pymupdf.open(pdf) as doc:
            if len(doc)!=1:raise ValueError('review input must have exactly one page')
        inputs.append({'source_page':page,'pdf':pdf.name,'sha256':digest,'acceptance':acceptance.name,'fidelity_evidence':evidence})
    output.parent.mkdir(parents=True,exist_ok=True)
    with pymupdf.open() as merged:
        for record in inputs:
            with pymupdf.open(root/record['pdf']) as doc:merged.insert_pdf(doc)
        merged.save(output,garbage=4,deflate=True)
    with pymupdf.open(output) as merged:
        if len(merged)!=count:raise ValueError('merged page count changed')
        for i,record in enumerate(inputs):
            with pymupdf.open(root/record['pdf']) as original:
                a=original[0];b=merged[i]
                if a.rect!=b.rect or not inventories_equal(trace_inventory(a),trace_inventory(b)):raise ValueError(f'page {i+1} glyphs changed')
                x=a.get_pixmap(matrix=pymupdf.Matrix(2,2),alpha=False);y=b.get_pixmap(matrix=pymupdf.Matrix(2,2),alpha=False)
                if (x.width,x.height,x.n)!=(y.width,y.height,y.n) or x.samples!=y.samples:raise ValueError(f'page {i+1} rendered pixels changed')
                record.update(saved_glyph_inventory_equal=True,saved_pixels_equal=True,raster_scale=2)
    report={'source_pdf_sha256':sha256_file(source),'output_sha256':sha256_file(output),'source_page_count':count,'inputs':inputs,'assembly_fidelity_passed':True,'entire_document_formal_use_ready':False,'scope':'Exact saved-page equivalence to individually reviewed inputs. Does not prove new live API pipeline, terminology discovery coverage, or source-boundary continuity.'}
    report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('source');parser.add_argument('index');parser.add_argument('output');args=parser.parse_args()
    result=assemble(args.source,args.index,args.output)
    print(json.dumps({'pages':result['source_page_count'],'sha256':result['output_sha256'],'assembly_fidelity_passed':True}))
