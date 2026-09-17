"""Run the explicitly approved four-case review; retain raw evidence and budget."""
import argparse, json, os, sqlite3
from pathlib import Path
from dotenv import load_dotenv
from openai import OpenAI
from pdf_translation.semantic_review import SemanticReview, validate_review, readable_review_text, REVIEW_CONTEXT_INSTRUCTIONS
from pdf_translation.term_matching import term_pattern

ROOT = Path(__file__).resolve().parents[1]
def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--context-revision', action='store_true')
    parser.add_argument('--reasoning-check', action='store_true')
    args = parser.parse_args()
    if args.reasoning_check: args.context_revision = True
    output_limit = 6000 if args.reasoning_check else 1600
    prepared = json.loads((ROOT/'reports/v0.7_rc13_semantic_review_prepared.json').read_text(encoding='utf-8'))
    prior = 0.0
    ledger_files = []
    for suffix in ('', '-r2', '-r3', '-r4'):
        report = json.loads((ROOT/f'output/pdf/v0.7-rc13/native-live-page15{suffix}/native.report.json').read_text(encoding='utf-8'))
        db = ROOT/'runs'/report['run_id']/'state.sqlite'
        with sqlite3.connect(db.as_uri()+'?mode=ro', uri=True) as con:
            prior += con.execute('SELECT COALESCE(SUM(cost_usd),0) FROM provider_attempts WHERE run_id=?', (report['run_id'],)).fetchone()[0]
        ledger_files.append(str(db.relative_to(ROOT)))
    load_dotenv(ROOT/'.env')
    client = OpenAI(api_key=os.environ['DEEPSEEK_API_KEY'], base_url=prepared['destination'], max_retries=0, timeout=90)
    base = ROOT/'output/pdf/v0.7-rc13/page15-semantic-review-live'
    if args.context_revision:
        prior += json.loads((base/'summary.json').read_text(encoding='utf-8'))['review_estimated_cost_usd']
        prepared['instructions'] += REVIEW_CONTEXT_INSTRUCTIONS
    if args.reasoning_check:
        prior += json.loads((base.with_name(base.name+'-r2')/'summary.json').read_text(encoding='utf-8'))['review_estimated_cost_usd']
    out = base.with_name(base.name+('-r3' if args.reasoning_check else '-r2')) if args.context_revision else base
    out.mkdir(exist_ok=False)
    summary = dict(status='running', prior_estimated_cost_usd=prior, review_estimated_cost_usd=0.0,
                   budget_cap_usd=0.1, prior_ledgers=ledger_files, cases=[], formal_use_ready=False,
                   authorization='User explicitly agreed to four source/translation review cases in this thread.')
    save(out/'summary.json', summary)
    save(out/'review-contract.json', {'instructions':prepared['instructions'], 'schema':prepared['output_schema'], 'context_revision':args.context_revision, 'reasoning_effort':'high' if args.reasoning_check else 'none', 'output_limit':output_limit})
    for case in prepared['cases']:
        payload = {key:case[key] for key in ('case_id','source','target','literal_hints')}
        if args.context_revision:
            payload['source'] = readable_review_text(case['source'], case['literal_hints'])
            payload['target'] = readable_review_text(case['target'], case['literal_hints'])
            glossary = json.loads((ROOT/'config/cs224n-approved-terminology.json').read_text(encoding='utf-8'))['glossary']
            payload['approved_terminology'] = [{k:g.get(k, []) for k in ('source_term','target_term','accepted_target_variants','forbidden_target_terms')} for g in glossary if g['status']=='approved' and g['scope']=={'kind':'document','value':'cs224n-2019-notes03-neuralnets-1c29d0561e0051cb'} and term_pattern(g['source_term']).search(case['source'])]
        body = json.dumps(payload, ensure_ascii=False)
        reserve = (len((body+prepared['instructions']+json.dumps(prepared['output_schema'])).encode('utf-8'))*.3+output_limit*1.2)/1e6
        if prior+summary['review_estimated_cost_usd']+reserve > .1:
            raise RuntimeError('aggregate budget would be exceeded')
        save(out/(case['case_id']+'.request.json'), payload)
        row = dict(case_id=case['case_id'], expected_pass=case['expected_pass_for_local_evaluation_only'])
        try:
            response = client.responses.create(model=prepared['model'], instructions=prepared['instructions'], input=body,
                max_output_tokens=output_limit, store=False, reasoning={'effort':'high' if args.reasoning_check else 'none'},
                text={'format':{'type':'json_schema','name':'semantic_review','schema':prepared['output_schema']}})
            save(out/(case['case_id']+'.response.json'), response.model_dump(mode='json'))
            usage = response.usage
            cost = (usage.input_tokens*.3+usage.output_tokens*1.2)/1e6 if usage else reserve
            summary['review_estimated_cost_usd'] += cost
            row.update(provider_response_id=response.id, estimated_cost_usd=cost)
            review = validate_review(SemanticReview.model_validate_json(response.output_text), case['case_id'], payload['source'], payload['target'])
            row.update(review=review.model_dump(mode='json'), contract_valid=True, matches_expected=review.passed==row['expected_pass'])
        except Exception as exc:
            row.update(error_type=type(exc).__name__, contract_valid=False, matches_expected=False)
            if 'estimated_cost_usd' not in row:
                summary['review_estimated_cost_usd'] += reserve
                row['reserved_uncertain_cost_usd'] = reserve
        summary['cases'].append(row)
        summary['aggregate_estimated_cost_usd'] = prior+summary['review_estimated_cost_usd']
        save(out/'summary.json', summary)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    summary['status'] = 'passed_four_case_live_validation' if all(x['matches_expected'] for x in summary['cases']) else 'needs_review'
    save(out/'summary.json', summary)
if __name__ == '__main__':
    main()
