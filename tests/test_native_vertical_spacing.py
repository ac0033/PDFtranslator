import pytest
from pdf_translation.native_vertical_spacing import plan_vertical_spacing, baseline_shift


@pytest.mark.parametrize('size',[7,10,16])
def test_one_line_shorter_retains_first_and_last_source_baselines(size):
    source=[100+i*size*1.2 for i in range(5)]
    target=[100+i*size*1.2 for i in range(4)]
    result=plan_vertical_spacing(source,target,size)
    assert result['adjusted'] and not result['unresolved']
    moved=[b+shift for b,shift in result['shifts']]
    assert moved[0]==100 and abs(moved[-1]-source[-1])<=.5
    assert all(b-a<=size*1.8+.01 for a,b in zip(moved,moved[1:]))


def test_single_line_cannot_hide_a_missing_source_line():
    result=plan_vertical_spacing([20,32],[20],10)
    assert result['unresolved'] and not result['adjusted']
    assert result['residual_points']==12


def test_extreme_contraction_is_blocked_instead_of_giant_leading():
    result=plan_vertical_spacing([20,32,44,56,68],[20,35],10)
    assert result['unresolved'] and result['reason']=='required_leading_exceeds_limit'


@pytest.mark.parametrize('source,target', [([20],[20]),([20,32],[20,35]),([20,32,44],[20,35,50])])
def test_normal_and_expanded_paragraphs_are_unchanged(source,target):
    result=plan_vertical_spacing(source,target,10)
    assert not result['adjusted'] and not result['unresolved']
    assert all(shift==0 for _,shift in result['shifts'])


def test_fixed_inline_frame_is_never_moved():
    result=plan_vertical_spacing([20,32],[20],10,fixed_inline=True)
    assert not result['adjusted'] and not result['unresolved']


def test_source_asset_rounding_does_not_create_phantom_line():
    result=plan_vertical_spacing([20,32,44,56,68],[20,20.4,35,35.6,50,49.7,65],10)
    assert len(result['shifts'])==4
    assert baseline_shift(35.6,result['shifts'])==baseline_shift(35,result['shifts'])
    with pytest.raises(ValueError):baseline_shift(500,result['shifts'])


def test_empty_cross_page_part_remains_unresolved():
    assert plan_vertical_spacing([20,32],[],10)['unresolved']
