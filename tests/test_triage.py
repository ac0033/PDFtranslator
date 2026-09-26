"""Triage rules, measured against the only human verdicts this project has.

The corpus in tests/data is the manual val.pdf review of 2026-09-16/17. The
numbers asserted below are the claims pdf_translation.triage makes in its
docstring; if a change improves them, raise the floor. The assertion that must
never be relaxed is that no confirmed error is dismissed by a rule.
"""
import json
from pathlib import Path

from pdf_translation.triage import (
    DISMISSED_BY_LEDGER,
    DISMISSED_BY_RULE,
    NEEDS_HUMAN,
    DocumentProfile,
    ReviewLedger,
    explain_number_warning,
    leaked_markup,
    looks_like_reference,
    scan_retained_english,
    triage_rows,
    unit_is_untranslated,
)

DATA = Path(__file__).parent / 'data'


def load_corpus(name):
    return json.loads((DATA / name).read_text(encoding='utf-8'))['cases']


NUMBER_CASES = load_corpus('val_number_triage.json')
LEAK_CASES = load_corpus('val_retained_english_triage.json')


def unit(unit_id='u0', *, source='source', translated='译文', issues=(), pages=(1,), result_hash='h0'):
    return {
        'unit_id': unit_id, 'source_text': source, 'translated_text': translated,
        'source_pages': list(pages), 'result_hash': result_hash,
        'issues': [{'category': c, 'severity': s} for c, s in issues],
    }


# --- the number rule, against 88 human verdicts ----------------------------

def test_no_confirmed_number_error_is_ever_dismissed_by_a_rule():
    """The safety property. A rule that silences a real defect is worse than none."""
    silenced = [
        case for case in NUMBER_CASES
        if case['verdict'] == 'real' and explain_number_warning(case['source_text'], case['translated_text'])
    ]
    assert silenced == [], [(c['unit_id'], c['reason']) for c in silenced]


def test_equivalent_conversions_are_left_for_a_human():
    """'$370 million' -> '3.7亿' is equivalent, but no rule proves that."""
    conversions = [case for case in NUMBER_CASES if case['verdict'] == 'conversion']
    assert conversions, 'corpus lost its conversion cases'
    for case in conversions:
        assert explain_number_warning(case['source_text'], case['translated_text']) is None


def test_the_rule_dismisses_most_tokenisation_artifacts():
    artifacts = [case for case in NUMBER_CASES if case['verdict'] == 'artifact']
    dismissed = [c for c in artifacts if explain_number_warning(c['source_text'], c['translated_text'])]
    assert len(dismissed) / len(artifacts) >= 0.85, f'{len(dismissed)}/{len(artifacts)}'


def test_glued_digits_and_decade_expansion_are_named_separately():
    assert explain_number_warning('See Figure2.1 and chapter2', '见图2.1和第2章') == 'glued_digit_tokenisation'
    assert explain_number_warning('the 1940s', '20世纪40年代') == 'decade_expansion'
    assert explain_number_warning('page 163 938', '第163938页') == 'glued_digit_tokenisation'
    assert explain_number_warning('1 percent', '1%') == 'glued_digit_tokenisation'


def test_a_genuinely_changed_number_is_not_explained():
    assert explain_number_warning('see chapter 1 of the book', '参见该书') is None
    assert explain_number_warning('a value of 42', '值为 43') is None


# --- retained English -------------------------------------------------------

def test_the_scanner_finds_the_confirmed_omissions():
    """13 of the 14 confirmed omissions, term-level, on the pre-fix text."""
    rows = [
        {'unit_id': c['unit_id'], 'source_text': c['source_text'], 'translated_text': c['translated_text'],
         'source_pages': c['pages'], 'issues': []}
        for c in LEAK_CASES
    ]
    profile = DocumentProfile(rows)
    found = 0
    for case in LEAK_CASES:
        if case['verdict'] != 'fix':
            continue
        terms = scan_retained_english(case['source_text'], case['translated_text'], profile, set())
        if case['term'].split()[0].casefold() in terms:
            found += 1
    assert found >= 13, f'only {found} of 14 confirmed omissions were found'


def test_an_approved_term_is_not_reported_again():
    profile = DocumentProfile([])
    source = 'We apply the sigmoid function to the output of the rollout at every step of the simulation.'
    target = '在仿真的每一个时间步上，我们都对推演输出应用sigmoid函数，并记录所得到的结果数值。'
    assert 'sigmoid' in scan_retained_english(source, target, profile, set())
    assert 'sigmoid' not in scan_retained_english(source, target, profile, {'sigmoid'})


def test_acronyms_and_roman_numerals_are_not_leaks():
    profile = DocumentProfile([])
    terms = scan_retained_english('See MCTS in chapter xiii for details.', '详见第xiii章的MCTS方法说明。', profile, set())
    assert terms == []


def test_document_furniture_is_not_a_leak():
    """A running footer appears on every page; a dropped term does not."""
    rows = [unit(f'u{i}', source='Kochenderfer and Katz, page footer text here',
                 translated=f'Kochenderfer 与 Katz，本页页脚所载的文本内容第{i}页处') for i in range(300)]
    rows.append(unit('odd', source='We perform a rollout of the policy from the sampled initial state.',
                     translated='我们从采样得到的初始状态出发，对该策略执行一次rollout，并记录轨迹。'))
    profile = DocumentProfile(rows)
    assert scan_retained_english(rows[0]['source_text'], rows[0]['translated_text'], profile, set()) == []
    assert 'rollout' in scan_retained_english(rows[-1]['source_text'], rows[-1]['translated_text'], profile, set())


# --- untranslated units and leaked markup -----------------------------------

def test_a_bibliography_entry_is_not_an_untranslated_unit():
    reference = ('B. D. Ziebart, A. Maas, J. A. Bagnell, and A. K. Dey, "Maximum Entropy Inverse '
                 'Reinforcement Learning," in AAAI Conference on Artificial Intelligence, 2008.')
    assert looks_like_reference(reference)
    assert not unit_is_untranslated(reference, reference)


def test_a_paragraph_that_came_back_in_english_is_reported():
    source = ('The polynomial approximation of the interval remainder is computed for every '
              'reachable set in the decomposition, which bounds the propagated error.')
    target = '该 polynomial approximation of the interval remainder 对分解中每个可达集计算，bounds the propagated error。'
    assert not looks_like_reference(source)
    assert unit_is_untranslated(source, target)


def test_a_properly_translated_paragraph_is_not_reported():
    source = ('The polynomial approximation of the interval remainder is computed for every '
              'reachable set in the decomposition, which bounds the propagated error.')
    assert not unit_is_untranslated(source, '对分解中的每个可达集计算区间余项的多项式近似，从而界定传播误差。')


def test_names_and_acronyms_kept_in_latin_letters_are_not_untranslated_text():
    names = '⟦P:bd-p0001-u0004-o000⟧Authors: Milad Mohammadi, Rohit Mundra, Richard Socher, Lisa Wang, Amita Kamath'
    assert not unit_is_untranslated(names, '⟦P:bd-p0001-u0004-o000⟧作者：Milad Mohammadi、Rohit Mundra、Richard Socher、Lisa Wang、Amita Kamath')
    keys = 'Keyphrases: Language Models. RNN. Bi-directional RNN. Deep RNN. GRU. LSTM.'
    assert not unit_is_untranslated(keys, '关键词：语言模型。RNN。双向RNN。深度RNN。GRU。LSTM。')
    # The same names do not excuse English left untranslated around them, nor a unit with no Chinese at all.
    assert unit_is_untranslated(names, '作者：Milad Mohammadi, Rohit Mundra, Richard Socher, Lisa Wang, Amita Kamath and others wrote these notes')
    assert unit_is_untranslated(names, names)


def test_a_short_source_is_not_judged():
    assert not unit_is_untranslated('Figure 2.1', 'Figure 2.1')


def test_markup_the_translation_invented_is_reported():
    assert leaked_markup('Definition of an STL specification.', '区间STL规范的<term>定义</term>。') == ['</term>', '<term>']
    assert leaked_markup('Use <b>bold</b> here.', '此处使用<b>粗体</b>。') == []


# --- putting it together ----------------------------------------------------

def test_triage_splits_warnings_into_dismissed_and_outstanding():
    rows = [
        unit('artifact', source='See Figure2.1', translated='见图2.1', issues=[('number', 'major')]),
        unit('real', source='see chapter 1', translated='参见', issues=[('number', 'major')]),
        unit('order', source='a b', translated='b a', issues=[('protected_marker_order', 'major')]),
    ]
    result = triage_rows(rows)
    numbers = result['categories']['number']
    assert (numbers['total'], numbers[DISMISSED_BY_RULE], numbers[NEEDS_HUMAN]) == (2, 1, 1)
    assert result['categories']['protected_marker_order'][NEEDS_HUMAN] == 1
    assert {item['unit_id'] for item in result['residual']} == {'real', 'order'}


def test_a_critical_issue_is_never_triaged_away():
    rows = [unit('c', source='See Figure2.1', translated='见图2.1', issues=[('number', 'critical')])]
    assert 'number' not in triage_rows(rows)['categories']


def test_a_ledger_dismissal_holds_only_while_the_text_is_unchanged():
    row = unit('u', source='x 1 y', translated='译文', issues=[('number', 'major')], result_hash='h1')
    ledger = ReviewLedger([{'unit_id': 'u', 'result_hash': 'h1', 'category': 'number', 'verdict': 'dismiss'}])
    assert triage_rows([row], ledger=ledger)['categories']['number'][DISMISSED_BY_LEDGER] == 1

    retranslated = {**row, 'result_hash': 'h2'}
    result = triage_rows([retranslated], ledger=ledger)
    assert result['categories']['number'][NEEDS_HUMAN] == 1
    assert [entry['unit_id'] for entry in result['stale_ledger_entries']] == ['u']


def test_a_confirmed_verdict_never_dismisses_anything():
    row = unit('u', source='x 1 y', translated='译文', issues=[('number', 'major')], result_hash='h1')
    ledger = ReviewLedger([{'unit_id': 'u', 'result_hash': 'h1', 'category': 'number', 'verdict': 'confirm'}])
    assert triage_rows([row], ledger=ledger)['categories']['number'][NEEDS_HUMAN] == 1


def test_retained_english_is_grouped_by_term_for_review():
    rows = [unit(f'u{i}', source='We perform a rollout of the policy from the sampled initial state.',
                 translated='我们从采样得到的初始状态出发，对该策略执行一次rollout，并记录轨迹。',
                 pages=(i + 1,)) for i in range(3)]
    terms = triage_rows(rows)['retained_english_terms']
    assert terms[0]['term'] == 'rollout'
    assert terms[0]['units'] == 3
    assert terms[0]['sample_pages'] == [1, 2, 3]


def test_ledger_round_trips_through_a_file(tmp_path):
    path = tmp_path / 'ledger.json'
    ReviewLedger([{'unit_id': 'u', 'result_hash': 'h', 'category': 'number', 'verdict': 'dismiss'}], {'Sigmoid'}).dump(path)
    loaded = ReviewLedger.load(path)
    assert loaded.dismissal('u', 'h', 'number') is not None
    assert loaded.retained_terms == {'sigmoid'}
    assert ReviewLedger.load(tmp_path / 'absent.json').entries == []
