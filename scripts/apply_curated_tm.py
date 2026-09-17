"""Apply curated TM entries to units whose committed result differs from the TM target.

Mirrors the run_units TM path (same request construction, validation and quality
check) but scoped to the given units, so already-accepted units are not swept.
No provider calls are made.
"""
from pathlib import Path
import argparse
import json
from pdf_translation.config import load_config
from pdf_translation.context_router import build_context_packet, select_exact_tm
from pdf_translation.context_store import ContextStore
from pdf_translation.hashing import stable_hash
from pdf_translation.pipeline import make_request
from pdf_translation.quality import check_quality
from pdf_translation.schemas import TranslationDraft, TranslationResult, TranslationUnit, Usage
from pdf_translation.storage import StateStore
from pdf_translation.validation import validate_request


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--capture', type=Path, required=True)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--context-db', type=Path, default=Path('runs/val-usable/context.sqlite'))
    parser.add_argument('--unit-id', nargs='+', required=True)
    args = parser.parse_args()
    cfg = load_config(args.config)
    units = [TranslationUnit.model_validate_json(line) for line in (args.capture / 'units.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
    by_id = {u.unit_id: u for u in units}
    cstore = ContextStore(args.context_db)
    glossary, styles, semantics = cstore.glossary(), cstore.styles(), cstore.semantics()
    gsnap = stable_hash([e for e in glossary if e.status == 'approved'])
    ssnap = stable_hash([r for r in styles if r.status == 'approved'])
    store = StateStore(Path('runs') / cfg.run_id / 'state.sqlite')
    applied, skipped = [], []
    for uid in args.unit_id:
        unit = by_id[uid]
        base_packet, _ = build_context_packet(unit, glossary, styles, semantics, by_id, token_budget=cfg.context_token_budget, glossary_snapshot_id=gsnap, style_snapshot_id=ssnap)
        tm_entry = select_exact_tm(unit, cstore.exact_tm(unit.source_hash), base_packet.packet_hash, gsnap, ssnap)
        if tm_entry is None:
            skipped.append((uid, 'no matching TM entry'))
            continue
        packet, _ = build_context_packet(unit, glossary, styles, semantics, by_id, token_budget=cfg.context_token_budget, glossary_snapshot_id=gsnap, style_snapshot_id=ssnap, tm_entry=tm_entry)
        request = make_request(unit, cfg, packet)
        validate_request(request)
        result = TranslationResult(request_id=request.request_id, unit_id=unit.unit_id, draft=TranslationDraft(unit_id=unit.unit_id, translated_text=tm_entry.target_text), finish='complete', provider_response_id=None, model=request.model, usage=Usage(input_tokens=0, output_tokens=0, cost_usd=0.0, usage_source='estimated'))
        report = check_quality(unit, result, glossary)
        if report.status == 'blocked':
            skipped.append((uid, f'quality blocked: {[i.category for i in report.issues if i.severity == "critical"]}'))
            continue
        store.prepare(cfg.run_id, unit.unit_id, request.cache_key)
        store.commit(cfg.run_id, result, report, request.cache_key, cached=True, source='curated_tm', record_usage=False)
        store.alias_cache_key(cfg.run_id, unit.unit_id, request.cache_key, result, report.status)
        applied.append((uid, report.status))
    print(json.dumps({'applied': applied, 'skipped': skipped}, ensure_ascii=False, indent=1), flush=True)


if __name__ == '__main__':
    main()
