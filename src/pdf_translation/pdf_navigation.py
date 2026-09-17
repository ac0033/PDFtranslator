"""Copy resolved PDF navigation with explicit source-to-output page maps."""
import pymupdf
from .source_preserving import SourcePreservingUnsupported


def resolved_destination(document,entry,page_map):
    kind=entry['kind']
    if kind==pymupdf.LINK_NAMED:
        if entry.get('page',-1)<0 or 'to' not in entry:
            raise SourcePreservingUnsupported('unresolved named PDF destination')
        original_page=entry['page']
        # MuPDF exposes named destinations in PDF bottom-up coordinates.
        point=pymupdf.Point(entry['to'])*document[original_page].transformation_matrix
        kind=pymupdf.LINK_GOTO
    elif kind==pymupdf.LINK_GOTO:
        original_page=entry['page'];point=pymupdf.Point(entry.get('to',(0,0)))
    else:
        if kind not in (pymupdf.LINK_URI,pymupdf.LINK_GOTOR,pymupdf.LINK_LAUNCH):
            raise SourcePreservingUnsupported(f'unsupported PDF navigation kind: {kind}')
        return {k:v for k,v in entry.items() if k not in ('xref','id','from','nameddest')}
    if original_page not in page_map:raise SourcePreservingUnsupported('PDF destination page missing from output')
    return {'kind':kind,'page':page_map[original_page],'to':point,'zoom':entry.get('zoom',0)}


def copy_links(source,target,page_map):
    count=0
    for pi,output_page in page_map.items():
        page=target[output_page]
        for old in page.get_links():page.delete_link(old)
        for link in source[pi].get_links():
            destination=resolved_destination(source,link,page_map)
            page.insert_link({**destination,'from':link['from']});count+=1
    return count


def mapped_toc(source,page_map):
    result=[]
    for level,title,page,detail in source.get_toc(simple=False):
        destination=resolved_destination(source,detail,page_map)
        for key in ('color','bold','italic','collapse'):
            if key in detail:destination[key]=detail[key]
        result.append([level,title,destination.get('page',-1)+1,destination])
    return result


def navigation_signature(document):
    identity={i:i for i in range(len(document))};links=[]
    def values(entry):
        normalized=resolved_destination(document,entry,identity)
        if 'to' in normalized and not isinstance(normalized['to'],str):normalized['to']=tuple(round(v,3) for v in normalized['to'])
        return normalized
    for pi,page in enumerate(document):
        for link in page.get_links():links.append({'source_page':pi,'from':tuple(round(v,3) for v in link['from']),**values(link)})
    toc=[{'level':l,'title':t,**values(d)} for l,t,_,d in document.get_toc(simple=False)]
    return {'links':links,'toc':toc}
