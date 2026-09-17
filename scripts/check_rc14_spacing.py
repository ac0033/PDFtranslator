"""Offline spacing regression using exact, source-bound reviewed translations."""
import json,re
from pathlib import Path
from audit_cross_page_records import canonical
from pdf_translation.native_asset_plan import prepare_asset_document
from pdf_translation.native_asset_layout import render_asset_page
from pdf_translation.hashing import sha256_file
source=Path('pdf/cs224n-2019-notes03-neuralnets.pdf')
units,plans=prepare_asset_document(source)
reports=[]
for page in range(1,19):
    try:
        records=json.loads(Path(f'output/pdf/v0.7-rc13/native-assets-sample/page-{page:03}.translation-review.json').read_text(encoding='utf-8'))
        lookup={canonical(r['source_text'],r.get('source_literals',{})):r for r in records if 'source_text' in r}
        translations={};missing=[]
        for unit,plan in zip(units,plans):
            if page not in plan['source_pages'] or unit.unit_type=='protected':continue
            key=canonical(unit.source_text,unit.protected_literal_hints)
            if key not in lookup:
                missing.append(unit.unit_id);continue
            row=lookup[key]
            old=re.findall(r'⟦P:[^⟧]+⟧',row['source_text']);new=re.findall(r'⟦P:[^⟧]+⟧',unit.source_text)
            assert len(old)==len(new)
            mapping=dict(zip(old,new))
            text=re.sub(r'⟦P:[^⟧]+⟧',lambda m:mapping[m.group()],row['translated_text'])
            assert canonical(text,unit.protected_literal_hints)==canonical(row['translated_text'],row.get('source_literals',{}))
            translations[unit.unit_id]=text
        try:
            if missing:raise ValueError('review source identities missing: '+str(missing))
            proof=render_asset_page(source,units,plans,translations,page-1,Path(f'output/pdf/v0.7-rc14-spacing/offline-{page:03}.pdf'),Path('C:/Windows/Fonts/simfang.ttf'),layout_only=True)
        except Exception as exc:
            proof={'passed':False,'error':str(exc),'error_type':type(exc).__name__}
    except Exception as exc:
        proof={'passed':False,'error':str(exc),'error_type':type(exc).__name__}
    reports.append({'page':page,**proof})
    Path('reports/v0.7_rc14_spacing_preflight.json').write_text(json.dumps({'source_sha256':sha256_file(source),'api_calls':0,'pages':reports},ensure_ascii=False,indent=2),encoding='utf-8')
    print(page,proof['passed'],'adjusted',sum(r['adjusted'] for r in proof.get('vertical_spacing',[])),'unresolved',len(proof.get('spacing_issues',[])),flush=True)
