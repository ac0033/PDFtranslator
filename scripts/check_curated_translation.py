"""Validate a curated translation for one captured unit against the quality gate."""
from pathlib import Path
import argparse
import json
from pdf_translation.context_store import ContextStore
from pdf_translation.quality import check_quality
from pdf_translation.schemas import ModelSpec, TranslationDraft, TranslationResult, TranslationUnit


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--capture', type=Path, required=True)
    parser.add_argument('--context-db', type=Path, default=Path('runs/val-usable/context.sqlite'))
    parser.add_argument('--unit-id', required=True)
    parser.add_argument('--target', required=True)
    args = parser.parse_args()
    units = [TranslationUnit.model_validate_json(line) for line in (args.capture / 'units.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
    unit = next((u for u in units if u.unit_id == args.unit_id), None)
    if unit is None:
        raise ValueError(f'unit not found in capture: {args.unit_id}')
    glossary = ContextStore(args.context_db).glossary()
    model = ModelSpec(provider='deepseek', model_id='deepseek-flash', max_output_tokens=2000, prompt_revision='curation', adapter_revision='curation', pricing_revision='none')
    result = TranslationResult(request_id='curation', unit_id=unit.unit_id, draft=TranslationDraft(unit_id=unit.unit_id, translated_text=args.target), finish='complete', provider_response_id=None, model=model, usage={'usage_source': 'estimated'})
    report = check_quality(unit, result, glossary)
    print(json.dumps({'status': report.status, 'issues': [{'category': i.category, 'severity': i.severity, 'explanation': i.explanation} for i in report.issues]}, ensure_ascii=False, indent=1), flush=True)


if __name__ == '__main__':
    main()
