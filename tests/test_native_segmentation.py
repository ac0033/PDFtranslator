from copy import deepcopy
import pytest
from pdf_translation.native_segmentation import short_prose_continuation,paragraph_gap_boundary
from pdf_translation.native_translation import _heading


@pytest.mark.parametrize('tail',['two.','way.','too.','yet.','one!','out,'])
@pytest.mark.parametrize('size',[8,10,16])
def test_short_tail_uses_prose_context_at_multiple_scales(tail,size):
    assert short_prose_continuation(tail,'the explanation ends in this',size=size,previous_size=size,gap=size*1.3,previous_is_prose=True,math_words={'sin','max','log'})


@pytest.mark.parametrize('change',[{'text':'sin'},{'text':'max'},{'text':'new'},{'text':'old'},{'text':'x'},{'text':'AB'},
    {'previous_text':'A complete sentence.'},{'previous_is_prose':False},{'gap':30},{'size':6}])
def test_short_tail_rejects_math_and_unrelated_rows(change):
    args=dict(text='two.',previous_text='the comparison between the',size=10,previous_size=10,gap=13,previous_is_prose=True,math_words={'sin','max','log'})
    args.update(change)
    assert not short_prose_continuation(**args)


def row(y,text,size=12,page=5):
    return dict(baseline=y,size=size,page_index=page,column='body',source_text=text,slots=[{'kind':'prose','source_font_size':size}])


@pytest.mark.parametrize('label',['Winter 2019','Revision C','Issued September','Some unrelated title'])
@pytest.mark.parametrize('scale',[.75,1,1.5])
def test_spacing_boundary_is_independent_of_words_and_page(label,scale):
    rows=[row(100*scale,'Heading line one',12*scale),row(115.5*scale,'Heading line two',12*scale)]
    assert paragraph_gap_boundary(rows,row(135.5*scale,label,12*scale))
    assert not paragraph_gap_boundary(rows,row(131*scale,label,12*scale))


def test_spacing_does_not_infer_prose_leading_from_equation_rows():
    rows=[row(100,'Text'),row(115,'Equation')];rows[1]['slots'][0]['kind']='protected'
    assert not paragraph_gap_boundary(rows,row(145,'Next paragraph'))


def test_superscript_reference_is_not_numbered_heading():
    heading=row(100,'1 Contributors');heading['slots'].insert(0,{'kind':'protected','source_font_size':12})
    assert _heading(heading)
    footnote=deepcopy(heading);footnote['slots'][0]['source_font_size']=7
    assert not _heading(footnote)


@pytest.mark.parametrize('text',['are a , , b :','has x, y','for p, q','via u, v'])
@pytest.mark.parametrize('size',[8,10,16])
def test_short_lead_before_math_list_uses_context(text,size):
    from pdf_translation.native_segmentation import short_math_list_lead
    assert short_math_list_lead(text,'the preceding explanation continues',size=size,previous_size=size,gap=1.4*size,previous_is_prose=True,math_words={'sin','max','log'})


@pytest.mark.parametrize('text',['sin x, y','max a, b','new x, y','old x, y','are unknown words','are 1, 2','for x=y','has f(x)','via exp(x)','ARE x, y'])
def test_math_list_lead_rejects_functions_labels_and_ambiguous_rows(text):
    from pdf_translation.native_segmentation import short_math_list_lead
    assert not short_math_list_lead(text,'the preceding explanation continues',size=10,previous_size=10,gap=14,previous_is_prose=True,math_words={'sin','max','log'})


@pytest.mark.parametrize('word',['dimensional vector','word window','layer network','step algorithm'])
@pytest.mark.parametrize('size',[8,10,16])
def test_number_compound_hyphen_is_prose_across_wrapped_adjectives(word,size):
    from types import SimpleNamespace as G
    from pdf_translation.native_segmentation import number_compound_hyphen
    a=G(text='4',font='URWPalladioL-Roma',size=size,origin=(30,50),advance=500)
    b=G(text='-',font=a.font,size=size,origin=(30+size*.5,50))
    assert number_compound_hyphen(a,b,word,{'sin','log'})
    assert not number_compound_hyphen(a,b,'sin x',{'sin','log'})
    assert not number_compound_hyphen(a,b,'x vector',{'sin','log'})
    b.origin=(35+size,50)
    assert not number_compound_hyphen(a,b,word,{'sin','log'})


@pytest.mark.parametrize('change',[{'font':'CMR10'},{'text':'−'},{'origin':(35,45)},{'size':7}])
def test_compound_hyphen_rejects_math_fonts_minus_and_script_geometry(change):
    from types import SimpleNamespace as G
    from pdf_translation.native_segmentation import number_compound_hyphen
    a=G(text='4',font='URWPalladioL-Roma',size=10,origin=(30,50),advance=500)
    args=dict(text='-',font=a.font,size=10,origin=(35,50));args.update(change)
    assert not number_compound_hyphen(a,G(**args),'dimensional',set())
