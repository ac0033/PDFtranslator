import runpy
from pathlib import Path
import pytest

canonical=runpy.run_path(str(Path(__file__).parents[1]/'scripts/audit_cross_page_records.py'))['canonical']


def test_marker_renumbering_does_not_change_source_identity():
    assert canonical('input ⟦P:old⟧',{'⟦P:old⟧':'x'})==canonical('input ⟦P:new⟧',{'⟦P:new⟧':'x'})


def test_same_ordinal_cannot_hide_different_math_or_source():
    assert canonical('input ⟦P:a⟧',{'⟦P:a⟧':'x'})!=canonical('input ⟦P:a⟧',{'⟦P:a⟧':'X'})
    assert canonical('code',{})!=canonical('explanation',{})


def test_unresolved_literal_cannot_pass_audit():
    with pytest.raises(ValueError,match='unresolved'):
        canonical('input ⟦P:missing⟧',{})
