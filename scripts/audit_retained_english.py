"""Inventory retained English in reviewed prose, with explicit review categories."""
import argparse,json,re
from pathlib import Path
from pdf_translation.hashing import sha256_file

TOKEN=re.compile(r"(?<![A-Za-z])[A-Za-z][A-Za-z-]{2,}(?![A-Za-z])")
PHRASE=re.compile(r"[A-Za-z][A-Za-z-]*(?:[ \t]+[A-Za-z][A-Za-z-]*){2,}")
NAMES=set("Amani Andrej Christopher Collobert Karpathy Manning Mundra Peddada Qiaojing Richard Rohit Ronan Socher Srivastava Xavier Yan".casefold().split())
SOFTWARE=set("MATLAB NumPy Python SciPy".casefold().split())
ALGORITHMS=set("Adagrad Adam RMSProp".casefold().split())
ACRONYMS=set("NER NLP ReLU RMS SGD SVD SVM".casefold().split())
PENDING=set("Dropout Sigmoid Softmax".casefold().split())
EXAMPLES=("Museums in Paris are amazing", "Not all museums in Paris")
TITLE="Dropout: A Simple Way to Prevent Neural Networks from Overfitting"


def audit(index_path, glossary_path=None):
    index_path=Path(index_path);root=index_path.parent
    index=json.loads(index_path.read_text(encoding='utf-8'));records=[]
    glossary=json.loads(Path(glossary_path).read_text(encoding='utf-8'))['glossary'] if glossary_path else []
    for page in index['accepted_source_pages']:
        override=index.get('accepted_artifact_overrides',{}).get(str(page),{})
        path=root/override.get('translation_review',f'page-{page:03}.translation-review.json')
        for row in json.loads(path.read_text(encoding='utf-8')):
            source=row['source_text'];target=re.sub(r'⟦P:[^⟧]+⟧',' ',row['translated_text'])
            spans=[]
            for example in EXAMPLES:
                if example in source:
                    for m in re.finditer(re.escape(example),target):spans.append((m.start(),m.end(),'source_language_example'))
            # These two quoted tokens are explicitly discussed as word-order examples
            # in this document. Do not exempt arbitrary quoted English words.
            for token in ('Paris','Museums'):
                if re.search(r'["“]'+token+r'["”]',source):
                    for m in re.finditer(r'["“]'+token+r'["”]',target):
                        spans.append((m.start(),m.end(),'source_language_example'))
            if TITLE in source:
                for m in re.finditer(re.escape(TITLE),target):spans.append((m.start(),m.end(),'retained_bibliographic_title'))
            for m in TOKEN.finditer(target):
                token=m.group();key=token.casefold()
                role=next((role for start,end,role in spans if start<=m.start() and m.end()<=end),None)
                if role is None:
                    role=('author_name' if key in NAMES else 'software_name' if key in SOFTWARE else
                          'algorithm_name' if key in ALGORITHMS else 'disciplinary_acronym' if key in ACRONYMS else
                          'approved_hybrid_term' if key=='frobenius' and re.match(r'\s*范数',target[m.end():]) else
                          'terminology_choice_pending' if key in PENDING else 'unclassified')
                if role=='terminology_choice_pending':
                    for entry in glossary:
                        if entry.get('status')!='approved':continue
                        scope=entry.get('scope',{})
                        if scope.get('kind')!='document' or not scope.get('value','').endswith(index['source_pdf_sha256'][:16]):continue
                        names=[entry['target_term'],*entry.get('accepted_target_variants',[])]
                        if token in names and re.search(r'(?<![A-Za-z])'+re.escape(entry['source_term'])+r'(?![A-Za-z])',source,re.I):
                            role='approved_disciplinary_name';break
                records.append({'source_page':page,'unit_id':row['unit_id'],'token':token,'target_span':[m.start(),m.end()],
                    'category':role,'review_file':path.name,'review_sha256':sha256_file(path),
                    'excerpt':target[max(0,m.start()-20):min(len(target),m.end()+35)]})
    unresolved=[r for r in records if r['category'] in ('unclassified','terminology_choice_pending')]
    return {'source_pdf_sha256':index['source_pdf_sha256'],'reviewed_pages':index['accepted_source_pages'],
        'retained_token_occurrences':len(records),'unresolved_occurrences':len(unresolved),
        'records':records,'unresolved':unresolved,'terminology_acceptance_passed':not unresolved,
        'entire_document_formal_use_ready':False,
        'scope':'Retained English alphabetic tokens of at least three characters only. Categories support review; they do not prove all Chinese terminology correct or permit untranslated prose. No translation or API call is performed.'}


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('index');p.add_argument('report');p.add_argument('--glossary');a=p.parse_args()
    r=audit(a.index,a.glossary);Path(a.report).write_text(json.dumps(r,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:r[k] for k in ('retained_token_occurrences','unresolved_occurrences','terminology_acceptance_passed')}))
