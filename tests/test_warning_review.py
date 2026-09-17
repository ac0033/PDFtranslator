"""Asking a model about a warning, without letting it decide on its own.

The contract has three parts and each is asserted here: evidence must be
quoted from the text, nothing is spent without being asked, and a model
verdict is recorded as a model's, not a person's.
"""
import pytest

from pdf_translation.config import RunConfig
from pdf_translation.budget import BudgetExceeded
from pdf_translation.schemas import ModelSpec
from pdf_translation.triage import DISMISSED_BY_LEDGER, DISMISSED_BY_MODEL, NEEDS_HUMAN, ReviewLedger, triage_rows
from pdf_translation.warning_review import (
    NOT_A_PROBLEM,
    REAL_PROBLEM,
    UNCERTAIN,
    MockWarningReviewProvider,
    WarningVerdict,
    build_warning_cases,
    estimate_review_cost,
    ledger_entries,
    review_warnings,
    validate_verdict,
    warning_id,
)


def config(budget=1.0, **overrides):
    return RunConfig(
        run_id='r', provider='mock', model_id='m', style_revision='s', prompt_revision='p',
        adapter_revision='a', pricing_revision='pr', max_output_tokens=512, budget_usd=budget,
        input_usd_per_million=0.3, output_usd_per_million=1.2, **overrides,
    )


def row(unit_id='u0', source='see chapter 1 of the work', translated='参见该著作', result_hash='h0'):
    return {
        'unit_id': unit_id, 'source_text': source, 'translated_text': translated,
        'source_pages': [7], 'result_hash': result_hash, 'issues': [],
    }


def residual_item(unit_id='u0', category='number', result_hash='h0'):
    return {'unit_id': unit_id, 'category': category, 'pages': [7], 'result_hash': result_hash}


def test_a_case_carries_everything_needed_to_answer_it():
    cases = build_warning_cases([row()], [residual_item()])
    assert len(cases) == 1
    case = cases[0]
    assert case['source'] == 'see chapter 1 of the work'
    assert case['target'] == '参见该著作'
    assert case['pages'] == [7]
    assert 'inventory of numbers' in case['why']
    assert case['warning_id'] == warning_id('u0', 'number', 'h0')


def test_a_warning_without_its_text_is_not_asked_about():
    """A question missing its evidence cannot be answered, so it is dropped."""
    assert build_warning_cases([], [residual_item()]) == []
    assert build_warning_cases([row(translated='')], [residual_item()]) == []


def test_the_warning_id_changes_when_the_translation_does():
    assert warning_id('u0', 'number', 'h1') != warning_id('u0', 'number', 'h2')
    assert warning_id('u0', 'number', 'h1') != warning_id('u0', 'url', 'h1')


def test_a_verdict_must_quote_text_that_is_really_there():
    case = build_warning_cases([row()], [residual_item()])[0]
    good = WarningVerdict(warning_id=case['warning_id'], verdict=REAL_PROBLEM, reason='章节号丢失',
                          evidence_source='chapter 1', evidence_target='参见')
    assert validate_verdict(good, case) is good
    for bad in (
        good.model_copy(update={'evidence_source': 'chapter 9'}),
        good.model_copy(update={'evidence_target': '没有这句话'}),
        good.model_copy(update={'warning_id': 'someone-elses'}),
    ):
        with pytest.raises(ValueError):
            validate_verdict(bad, case)


def test_a_decided_verdict_cannot_arrive_without_evidence():
    case = build_warning_cases([row()], [residual_item()])[0]
    with pytest.raises(ValueError):
        validate_verdict(WarningVerdict(warning_id=case['warning_id'], verdict=NOT_A_PROBLEM, reason='没问题'), case)
    # Admitting it cannot tell is allowed, and needs no quotes.
    undecided = WarningVerdict(warning_id=case['warning_id'], verdict=UNCERTAIN, reason='需对照原书版面')
    assert validate_verdict(undecided, case) is undecided


def test_the_estimate_is_offline_and_bounded():
    cases = build_warning_cases([row(f'u{i}') for i in range(10)],
                                [residual_item(f'u{i}') for i in range(10)])
    estimate = estimate_review_cost(cases, config())
    assert estimate['cases'] == 10
    assert estimate['estimated_input_tokens'] > 0
    assert estimate['estimated_max_cost_usd'] > 0
    assert estimate['within_budget'] is True
    assert estimate['is_estimate'] is True


def test_nothing_is_called_until_paid_execution_is_asked_for():
    calls = []

    class Counting(MockWarningReviewProvider):
        def review(self, case, model):
            calls.append(case['warning_id'])
            return super().review(case, model)

    cases = build_warning_cases([row()], [residual_item()])
    result = review_warnings(cases, Counting(), config())
    assert result['status'] == 'estimate_only'
    assert result['verdicts'] == []
    assert calls == []


def test_a_review_that_would_exceed_its_budget_does_not_start():
    cases = build_warning_cases([row(f'u{i}') for i in range(50)],
                                [residual_item(f'u{i}') for i in range(50)])
    with pytest.raises(BudgetExceeded):
        review_warnings(cases, MockWarningReviewProvider(), config(budget=0.0000001), execute_paid=True)


def test_a_paid_review_returns_validated_verdicts():
    cases = build_warning_cases([row()], [residual_item()])
    result = review_warnings(cases, MockWarningReviewProvider(), config(), execute_paid=True)
    assert result['status'] == 'completed'
    assert result['verdicts'][0]['verdict'] == REAL_PROBLEM
    assert result['failures'] == []


def test_a_provider_that_misquotes_is_recorded_as_a_failure_not_a_verdict():
    class Fabricating:
        def review(self, case, model):
            return WarningVerdict(warning_id=case['warning_id'], verdict=NOT_A_PROBLEM,
                                  reason='编造的证据', evidence_source='文本里没有这句')

    cases = build_warning_cases([row()], [residual_item()])
    result = review_warnings(cases, Fabricating(), config(), execute_paid=True)
    assert result['status'] == 'completed_with_failures'
    assert result['verdicts'] == []
    assert 'evidence not found' in result['failures'][0]['error']


def test_only_a_clearance_becomes_a_dismissal_and_it_names_the_model():
    cases = build_warning_cases([row('u0'), row('u1', source='plain text', translated='纯文本')],
                                [residual_item('u0'), residual_item('u1')])
    result = review_warnings(cases, MockWarningReviewProvider(), config(), execute_paid=True)
    entries = ledger_entries(result, cases, reviewed_at='2026-09-17')
    by_unit = {entry['unit_id']: entry for entry in entries}
    assert by_unit['u0']['verdict'] == 'confirm'     # the mock found a missing digit
    assert by_unit['u1']['verdict'] == 'dismiss'
    assert all(entry['reviewed_by'].startswith('model:') for entry in entries)
    assert all(entry['reason'] for entry in entries)


def test_an_uncertain_verdict_writes_nothing_to_the_ledger():
    case = build_warning_cases([row()], [residual_item()])[0]
    result = {'model_id': 'm', 'verdicts': [
        WarningVerdict(warning_id=case['warning_id'], verdict=UNCERTAIN, reason='说不准').model_dump()
    ]}
    assert ledger_entries(result, [case], reviewed_at='2026-09-17') == []


def test_a_model_dismissal_annotates_the_warning_but_leaves_it_on_the_list():
    """AGENTS.md section 5: a model saying there is no problem is not evidence."""
    rows = [{**row(), 'issues': [{'category': 'number', 'severity': 'major'}]}]
    ledger = ReviewLedger([{
        'unit_id': 'u0', 'result_hash': 'h0', 'category': 'number', 'verdict': 'dismiss',
        'reason': '等值换算', 'evidence_source': 'chapter 1', 'evidence_target': '参见',
        'reviewed_by': 'model:deepseek',
    }])
    result = triage_rows(rows, ledger=ledger)
    numbers = result['categories']['number']
    assert numbers[DISMISSED_BY_MODEL] == 1
    assert numbers[DISMISSED_BY_LEDGER] == 0
    assert numbers[NEEDS_HUMAN] == 1
    assert result['residual'][0]['model_verdict']['reason'] == '等值换算'
    assert result['residual'][0]['model_verdict']['reviewed_by'] == 'model:deepseek'


def test_model_verdicts_can_be_trusted_when_asked_for_explicitly():
    rows = [{**row(), 'issues': [{'category': 'number', 'severity': 'major'}]}]
    ledger = ReviewLedger([{
        'unit_id': 'u0', 'result_hash': 'h0', 'category': 'number', 'verdict': 'dismiss',
        'reviewed_by': 'model:deepseek', 'reason': '等值换算',
    }])
    result = triage_rows(rows, ledger=ledger, trust_model_verdicts=True)
    assert result['categories']['number'][NEEDS_HUMAN] == 0
    assert result['residual'] == []


def test_a_human_dismissal_still_outranks_everything():
    rows = [{**row(), 'issues': [{'category': 'number', 'severity': 'major'}]}]
    ledger = ReviewLedger([{'unit_id': 'u0', 'result_hash': 'h0', 'category': 'number', 'verdict': 'dismiss'}])
    result = triage_rows(rows, ledger=ledger)
    assert result['categories']['number'][DISMISSED_BY_LEDGER] == 1
    assert result['categories']['number'][NEEDS_HUMAN] == 0


def test_the_model_spec_comes_from_the_run_config():
    cases = build_warning_cases([row()], [residual_item()])
    seen = []

    class Recording(MockWarningReviewProvider):
        def review(self, case, model):
            seen.append(model)
            return super().review(case, model)

    review_warnings(cases, Recording(), config(), execute_paid=True)
    assert isinstance(seen[0], ModelSpec)
    assert seen[0].model_id == 'm'


def test_a_marker_nobody_can_read_is_not_put_to_the_model():
    """The model has no more information than the pipeline; it would be guessing."""
    from pdf_translation.warning_review import REVIEWABLE_CATEGORIES, UNREVIEWABLE_CATEGORIES

    assert 'protected_source_literal_unavailable' in UNREVIEWABLE_CATEGORIES
    assert 'retained_english' in UNREVIEWABLE_CATEGORIES
    assert not (REVIEWABLE_CATEGORIES & set(UNREVIEWABLE_CATEGORIES))
    cases = build_warning_cases(
        [row('u0'), row('u1'), row('u2')],
        [residual_item('u0', 'protected_source_literal_unavailable'),
         residual_item('u1', 'retained_english'),
         residual_item('u2', 'untranslated_unit')],
    )
    assert [case['category'] for case in cases] == ['untranslated_unit']


def test_the_caller_can_widen_the_scope_deliberately():
    cases = build_warning_cases(
        [row('u0')], [residual_item('u0', 'retained_english')],
        categories={'retained_english'},
    )
    assert [case['category'] for case in cases] == ['retained_english']
