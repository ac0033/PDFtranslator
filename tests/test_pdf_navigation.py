import importlib.util
from pathlib import Path
import pymupdf
from pdf_translation.pdf_navigation import copy_links,mapped_toc,navigation_signature,resolved_destination


def fixture_pdf(path):
    with pymupdf.open() as doc:
        doc.new_page(width=300,height=400);doc.new_page(width=300,height=400)
        doc[0].insert_text((20,40),'Chapter link');doc[1].insert_text((30,150),'Destination')
        doc[0].insert_link({'kind':pymupdf.LINK_GOTO,'from':pymupdf.Rect(20,20,100,45),'page':1,'to':pymupdf.Point(30,150)})
        doc[1].insert_link({'kind':pymupdf.LINK_URI,'from':pymupdf.Rect(20,20,100,45),'uri':'https://example.org/book'})
        doc.set_toc([[1,'Chapter',2]])
        doc.set_metadata({'title':'Navigation fixture'})
        doc.xref_set_key(doc.pdf_catalog(),'Names',f'<< /Dests << /Names [(chapter) [{doc[1].xref} 0 R /XYZ 30 250 0]] >> >>')
        doc.save(path)
    with pymupdf.open(path) as doc:
        link=doc[0].get_links()[0]
        doc.xref_set_key(link['xref'],'A','<< /S /GoTo /D (chapter) >>')
        bookmark=doc.get_toc(simple=False)[0][3]
        doc.xref_set_key(bookmark['xref'],'A','<< /S /GoTo /D (chapter) >>')
        data=doc.tobytes()
    path.write_bytes(data)


def test_named_destination_is_converted_from_pdf_coordinates(tmp_path):
    path=tmp_path/'named.pdf';fixture_pdf(path)
    with pymupdf.open(path) as doc:
        link=doc[0].get_links()[0];assert link['kind']==pymupdf.LINK_NAMED
        dest=resolved_destination(doc,link,{0:0,1:2})
        assert dest['kind']==pymupdf.LINK_GOTO and dest['page']==2
        assert tuple(dest['to'])==(30.,150.)
        assert mapped_toc(doc,{0:0,1:2})[0][2]==3


def test_bilingual_pairs_keep_links_bookmarks_and_source_title(tmp_path):
    source=tmp_path/'source.pdf';fixture_pdf(source)
    spec=importlib.util.spec_from_file_location('build_bilingual_pdf',Path('scripts/build_bilingual_pdf.py'))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    output=tmp_path/'paired.pdf';report=module.build(source,source,output)
    assert report['saved_navigation_verified']
    with pymupdf.open(output) as doc:
        assert len(doc)==4 and doc.metadata['title'].startswith('Navigation fixture')
        assert doc[0].get_links()[0]['page']==2
        assert doc[1].get_links()[0]['page']==3
        assert tuple(doc[0].get_links()[0]['to'])==(30.,150.)
        assert doc[2].get_links()[0]['uri']=='https://example.org/book'
        toc=doc.get_toc()
        assert [2,'Chapter',3] in toc and [2,'Chapter',4] in toc
