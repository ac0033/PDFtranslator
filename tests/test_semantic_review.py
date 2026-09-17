import pytest
from pdf_translation.semantic_review import SemanticReview,SemanticFinding,validate_review


def test_review_is_bound_to_exact_source_target_and_case():
    finding=SemanticFinding(category='grammar',source_quote='source',target_quote='broken',explanation='Incomplete clause')
    review=SemanticReview(case_id='a',passed=False,findings=(finding,))
    assert validate_review(review,'a','source paragraph','broken translation')==review
    for case,source,target in [('b','source','broken'),('a','different','broken'),('a','source','different')]:
        with pytest.raises(ValueError):validate_review(review,case,source,target)


def test_green_verdict_cannot_hide_findings():
    f=SemanticFinding(category='title',source_quote='source',target_quote='title',explanation='Mixed title')
    with pytest.raises(ValueError,match='contradicts'):
        validate_review(SemanticReview(case_id='a',passed=True,findings=(f,)),'a','source','title')
    with pytest.raises(ValueError,match='contradicts'):
        validate_review(SemanticReview(case_id='a',passed=False,findings=()),'a','source','title')
    assert validate_review(SemanticReview(case_id='a',passed=True,findings=()),'a','source','title').passed


def test_readable_review_preserves_surrounding_text_and_requires_all_assets():
    from pdf_translation.semantic_review import readable_review_text
    text='before ⟦P:x⟧ after ⟦P:x⟧'
    assert readable_review_text(text, {'⟦P:x⟧':'X^{2}'})=='before X^{2} after X^{2}'
    with pytest.raises(ValueError, match='missing protected literal'):
        readable_review_text(text, {})
