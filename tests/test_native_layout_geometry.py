from types import SimpleNamespace
import pytest
from pdf_translation.native_layout_geometry import horizontal_ink_frame,rightward_integer_shift


@pytest.mark.parametrize('size',[7,10,16])
@pytest.mark.parametrize('overhang',[(-.2,.7),(0,.5),(-.1,.5)])
def test_ink_and_advance_both_fit_original_object_frame(size,overhang):
    g=SimpleNamespace(origin=(30,60),advance=500,size=size,ink_bounds=(30+overhang[0]*size,50,30+overhang[1]*size,61))
    left,right=horizontal_ink_frame([g])
    assert left<=g.ink_bounds[0] and left<=g.origin[0]
    assert right>=g.ink_bounds[2] and right>=30+size*.5
    for cursor in (12.1,50.7,100):
        dx=rightward_integer_shift(cursor,left)
        assert isinstance(dx,int) and cursor<=left+dx<cursor+1


def test_mixed_glyph_frame_includes_scripts_and_both_overhangs():
    a=SimpleNamespace(origin=(30,60),advance=500,size=10,ink_bounds=(28,50,36,62))
    b=SimpleNamespace(origin=(36,55),advance=400,size=7,ink_bounds=(35,49,40,56))
    assert horizontal_ink_frame([a,b])==(28,40)


@pytest.mark.parametrize('word',['Paris','Tokyo','Museums'])
def test_page_fit_keeps_short_quoted_example_whole(word):
    from pdf_translation.native_page_flow import _fit_prefix
    font=SimpleNamespace(text_length=lambda text,fontsize:len(text)*fontsize)
    text='123456“'+word+'” tail'
    end=_fit_prefix(text,10,1,font,1)
    assert end==6


def test_long_quote_can_wrap_at_word_boundaries():
    from pdf_translation.native_page_flow import _fit_prefix
    font=SimpleNamespace(text_length=lambda text,fontsize:len(text)*fontsize)
    text='“Alpha beta gamma delta”'
    end=_fit_prefix(text,10,2,font,1)
    assert text[:end]=='“Alpha beta gamma'


@pytest.mark.parametrize('symbol',['W','A','B'])
def test_nested_script_context_does_not_interleave_sub_and_superscripts(symbol):
    from pdf_translation.native_plan import _nested_script_text
    def g(index,text,x,y,size):return SimpleNamespace(index=index,text=text,origin=(x,y),size=size,advance=500)
    gs=[g(0,symbol,10,20,8),g(1,'(',18,16,6),g(2,'1',21,16,6),g(3,')',24,16,6),g(4,'i',17,23,6),g(5,'j',20,23,6)]
    assert _nested_script_text(gs)==symbol+'^{(1)}_{ij}'
