"""Create unscaled source/translation pairs and verify both saved page halves."""
import argparse,json
from pathlib import Path
import pymupdf
from pdf_translation.hashing import sha256_file
from pdf_translation.pdf_navigation import copy_links,mapped_toc,navigation_signature

def build(source,translated,output):
    if output.exists():raise FileExistsError(output)
    output.parent.mkdir(parents=True,exist_ok=True)
    with pymupdf.open(source) as a,pymupdf.open(translated) as b,pymupdf.open() as out:
        if len(a)!=len(b):raise ValueError('source/translation page counts differ')
        for i in range(len(a)):
            if a[i].rect!=b[i].rect or a[i].rect.x0!=0 or a[i].rect.y0!=0:
                raise ValueError('pair geometry mismatch')
            out.insert_pdf(a,from_page=i,to_page=i,links=False)
            out.insert_pdf(b,from_page=i,to_page=i,links=False)
        out.xref_set_key(out.pdf_catalog(),'PageLayout','/TwoPageLeft')
        left={i:2*i for i in range(len(a))};right={i:2*i+1 for i in range(len(b))}
        navigation_counts={'source_links':copy_links(a,out,left),'translated_links':copy_links(b,out,right)}
        original_toc=mapped_toc(a,left);translated_toc=mapped_toc(b,right)
        if original_toc or translated_toc:
            toc=[]
            for label,first,entries in [('English',1,original_toc),('中文',2,translated_toc)]:
                toc.append([1,label,first]);toc.extend([[level+1,title,page,detail] for level,title,page,detail in entries])
            out.set_toc(toc)
        else:out.set_toc([[1,f'Page {i+1} / 第 {i+1} 页',2*i+1] for i in range(len(a))])
        expected_navigation=navigation_signature(out)
        out.set_metadata({'title':(a.metadata.get('title') or source.stem)+' — English / 中文','subject':'Original English followed by reviewed Chinese; paired-page reading'})
        out.save(output,garbage=4,deflate=True)
    checks=[]
    with pymupdf.open(source) as a,pymupdf.open(translated) as b,pymupdf.open(output) as paired:
        assert len(a)*2==len(paired)
        if navigation_signature(paired)!=expected_navigation:raise ValueError('saved bilingual navigation differs')
        for i in range(len(a)):
            for offset,(name,doc) in enumerate([('source',a),('translation',b)]):
                actual_page=paired[2*i+offset]
                expected=doc[i].get_pixmap(matrix=pymupdf.Matrix(2,2),alpha=False)
                actual=actual_page.get_pixmap(matrix=pymupdf.Matrix(2,2),alpha=False)
                if (expected.width,expected.height,expected.n)!=(actual.width,actual.height,actual.n) or expected.samples!=actual.samples:
                    raise ValueError(f'saved bilingual page differs: {i+1} {name}')
                if doc[i].get_text()!=actual_page.get_text():
                    raise ValueError(f'saved bilingual text differs: {i+1} {name}')
                checks.append({'source_page':i+1,'pdf_page':2*i+offset+1,'side':name,'raster_scale':2,'saved_pixels_equal':True,'extracted_text_equal':True})
    report={'source_sha256':sha256_file(source),'translated_sha256':sha256_file(translated),'output_sha256':sha256_file(output),'page_count':len(checks),'paired_source_pages':len(checks)//2,'layout':'alternating original/Chinese pages; TwoPageLeft; unscaled','checks':checks,'navigation':navigation_counts,'saved_navigation_verified':True,'passed':True}
    output.with_suffix('.verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return report
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('source',type=Path);p.add_argument('translated',type=Path);p.add_argument('output',type=Path);a=p.parse_args();r=build(a.source,a.translated,a.output);print({'pages':r['page_count'],'verified_pages':len(r['checks']),'sha256':r['output_sha256']})
