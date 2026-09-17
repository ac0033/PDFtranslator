"""Build native page review from the exact assembled PDF and accepted index."""
import argparse, html, json, shutil
from pathlib import Path
import pymupdf
from pdf_translation.hashing import sha256_file
from pdf_translation.semantic_review import readable_review_text

def build(source, index_path, pdf, output, label="当前版"):
    index=json.loads(index_path.read_text(encoding='utf-8'))
    assembly=json.loads(pdf.with_suffix('.assembly.json').read_text(encoding='utf-8'))
    if sha256_file(source)!=index['source_pdf_sha256'] or assembly['source_pdf_sha256']!=index['source_pdf_sha256']:
        raise ValueError('source identity mismatch')
    if not assembly['assembly_fidelity_passed'] or sha256_file(pdf)!=assembly['output_sha256']:
        raise ValueError('assembled PDF differs from verified artifact')
    final_accepted=False
    if index.get('entire_document_formal_use_ready'):
        final_report=json.loads((index_path.parent/index['document_acceptance']).read_text(encoding='utf-8'))
        if final_report['status']!='passed_agent_document_acceptance' or final_report['chinese_pdf_sha256']!=sha256_file(pdf):
            raise ValueError('final acceptance refers to another PDF')
        final_accepted=True
    status_note='18 页已通过逐页语义、视觉和保真验收。' if final_accepted else '18 页已逐页复核，尚未标记为正式验收完成。'
    assets=output.parent/(output.stem+'-assets')
    if output.exists(): raise FileExistsError(output)
    assets.mkdir(exist_ok=True)
    sections=[];records=[]
    with pymupdf.open(source) as a,pymupdf.open(pdf) as b:
        if len(a)!=len(b) or index['accepted_source_pages']!=list(range(1,len(a)+1)):
            raise ValueError('page coverage mismatch')
        for i in range(len(a)):
            page=i+1;override=index.get('accepted_artifact_overrides',{}).get(str(page),{})
            accepted=index_path.parent/override.get('pdf',f'page-{page:03}.zh.pdf')
            rec=assembly['inputs'][i]
            if rec['source_page']!=page or rec['sha256']!=sha256_file(accepted):
                raise ValueError('index changed since assembly')
            acceptance=index_path.parent/override.get('acceptance',f'page-{page:03}.acceptance.json')
            if json.loads(acceptance.read_text(encoding='utf-8'))['status']!='passed_agent_semantic_and_visual_review':
                raise ValueError('missing semantic/visual review')
            review=index_path.parent/override.get('translation_review',f'page-{page:03}.translation-review.json')
            rows=json.loads(review.read_text(encoding='utf-8'))
            images=[]
            for label,doc in [('source',a),('target',b)]:
                name=f'{page:03}-{label}.png';doc[i].get_pixmap(matrix=pymupdf.Matrix(1.5,1.5),alpha=False).save(assets/name)
                images.append(f'<figure><figcaption>{"原文" if label=="source" else "译文"}</figcaption><img loading="lazy" src="{assets.name}/{name}" alt="第 {page} 页"></figure>')
            pairs=[]
            for row in rows:
                hints=row.get('source_literals',{})
                s=html.escape(readable_review_text(row['source_text'],hints));t=html.escape(readable_review_text(row['translated_text'],hints))
                pairs.append(f'<tr><td>{s}</td><td>{t}</td></tr>')
            sections.append(f'<section id="page-{page}"><h2>第 {page} 页</h2><div class="pages">{"".join(images)}</div><details><summary>查看完整段落对照</summary><p>跨页段落显示完整原文和译文，实际分页以页面图像为准。</p><table><thead><tr><th>原文</th><th>译文</th></tr></thead><tbody>{"".join(pairs)}</tbody></table></details></section>')
            records.append({'source_page':page,'pdf_sha256':rec['sha256'],'translation_review_sha256':sha256_file(review),'acceptance_sha256':sha256_file(acceptance)})
    options=''.join(f'<option value="page-{p}">第 {p} 页</option>' for p in index['accepted_source_pages'])
    import os
    link=Path(os.path.relpath(pdf,output.parent)).as_posix()
    document='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>PDF 逐页审阅 · {html.escape(label)}</title><style>body{font:16px system-ui;background:#f4f5f7;color:#182433;margin:0}header{padding:24px;max-width:1500px;margin:auto}nav{position:sticky;top:0;background:white;padding:12px 24px;z-index:1;border-bottom:1px solid #ddd}select,a{font:inherit;margin-right:20px}section{max-width:1500px;background:white;margin:24px auto;padding:20px;scroll-margin-top:60px}.pages{display:flex;gap:16px}figure{flex:1;min-width:0;margin:0}img{width:100%;border:1px solid #ddd}figcaption{padding:8px}details{margin-top:20px}summary{cursor:pointer}table{width:100%;table-layout:fixed;border-collapse:collapse}td,th{width:50%;vertical-align:top;text-align:left;white-space:pre-wrap;overflow-wrap:anywhere;padding:12px;border:1px solid #ddd}@media(max-width:800px){.pages{display:block}}</style>'''
    document+=f'<header><h1>PDF 逐页审阅 · {html.escape(label)}</h1><p>{status_note}</p></header><nav><label>页码 <select onchange="location.hash=this.value">{options}</select></label><a href="{html.escape(link,quote=True)}">打开整本 PDF</a></nav>'+''.join(sections)+'</html>'
    document=document.replace('{html.escape(label)}',html.escape(label))
    output.write_text(document,encoding='utf-8')
    manifest={'source_sha256':sha256_file(source),'target_sha256':sha256_file(pdf),'index_sha256':sha256_file(index_path),'pages':records,'formal_use_ready':final_accepted}
    output.with_suffix('.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    return manifest

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('source',type=Path);p.add_argument('index',type=Path);p.add_argument('pdf',type=Path);p.add_argument('output',type=Path);p.add_argument('--label',default='当前版');a=p.parse_args()
    result=build(a.source,a.index,a.pdf,a.output,a.label);print({'pages':len(result['pages']),'target_sha256':result['target_sha256']})
