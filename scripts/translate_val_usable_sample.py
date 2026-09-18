"""Estimate / resume a budgeted usable-version translation from a preserved capture.

The capture directory is the only thing this needs: units.jsonl names the pages
and the document, so the pages translated are exactly the pages captured. That
makes a slice of a book a first-class input - there is no separate page list to
disagree with what is on disk.

Costing runs first and always. --execute-paid re-runs the same work for real
only after the estimate is compared against the config's ceiling, so a surprise
in the estimate stops the run rather than quietly spending money.

--style-note carries a per-book instruction into the stored style rule. It is a
per-document rule, so two books translated through the same context database do
not inherit each other's wording decisions.
"""
import argparse
import json
from pathlib import Path
from dotenv import load_dotenv
from pdf_translation.babeldoc_adapter import run_smoke_document
from pdf_translation.config import load_config
from pdf_translation.context_models import Scope, StyleRule
from pdf_translation.context_store import ContextStore
from pdf_translation.pipeline import DEFAULT_TRANSLATION_STYLE
from pdf_translation.schemas import TranslationUnit


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--capture', type=Path, required=True, help='Capture directory holding units.jsonl and capture-map.jsonl.')
    parser.add_argument('--source', type=Path, default=Path('pdf/val.pdf'), help='The PDF the capture came from.')
    parser.add_argument('--config', type=Path, default=Path('configs/val.usable.deepseek.toml'))
    parser.add_argument('--capture-root', type=Path, default=Path('tmp/pdfs'), help='Parent of the BabelDOC scratch directory.')
    parser.add_argument('--sample-name', default='usable-sample')
    parser.add_argument('--style-note', default=None, help='Book-specific instruction appended to the default style.')
    parser.add_argument('--style-version', default='usable-v1', help='Version stamped on the stored style rule.')
    parser.add_argument('--execute-paid', action='store_true')
    return parser


def load_units(capture: Path) -> list[TranslationUnit]:
    lines = (capture / 'units.jsonl').read_text(encoding='utf-8').splitlines()
    return [TranslationUnit.model_validate_json(line) for line in lines if line.strip()]


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_dotenv(Path('.env'), override=False)
    config = load_config(args.config)
    units = load_units(args.capture)
    documents = {unit.document_id for unit in units}
    if len(documents) != 1:
        raise ValueError('capture must belong to one source document')
    document_id = next(iter(documents))

    store = ContextStore(Path(config.context_db))
    content = DEFAULT_TRANSLATION_STYLE + ('\n' + args.style_note if args.style_note else '')
    store.put_style(StyleRule(
        rule_id=f'{args.sample_name}-style-{document_id}',
        scope=Scope(kind='document', value=document_id),
        version=args.style_version,
        content=content,
    ))

    records = [json.loads(line) for line in (args.capture / 'capture-map.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
    pages = tuple(sorted({record['page_index'] for record in records}))
    scratch = args.capture_root / args.capture.name

    def run(execute_paid: bool, render_review_draft: bool = False) -> dict:
        return run_smoke_document(
            args.source, pages, config, args.capture, Path('runs'), scratch,
            execute_paid=execute_paid, reuse_capture=True,
            sample_name=args.sample_name, render_review_draft=render_review_draft,
        )

    report = run(False)
    (args.capture / 'estimate.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({
        'estimated_max_cost_usd': report['estimated_max_cost_usd'],
        'units': report['unit_count'],
        'pages': len(report['physical_pages']),
        'budget_usd': config.budget_usd,
    }, ensure_ascii=True), flush=True)

    if args.execute_paid:
        if report['estimated_max_cost_usd'] > config.budget_usd:
            raise ValueError('estimated cost exceeds the configured ceiling; no API call made')
        report = run(True, render_review_draft=True)
        print(json.dumps(report, ensure_ascii=True), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
