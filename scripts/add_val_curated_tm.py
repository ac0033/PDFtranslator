"""Add a curated translation-memory entry for a unit whose provider attempts are exhausted.

The entry is bound to the unit's exact source hash plus the live context,
glossary and style snapshots, mirroring how run_units selects exact TM. It is
recorded with qa_status=passed and human_status=confirmed so the pipeline can
use it without another paid call; the curation is agent-written and listed in
the delivery notes for human review.
"""
from pathlib import Path
import argparse
import json
from pdf_translation.context_models import TranslationMemoryEntry
from pdf_translation.context_router import build_context_packet
from pdf_translation.context_store import ContextStore
from pdf_translation.hashing import stable_hash
from pdf_translation.schemas import TranslationUnit


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--capture', type=Path, required=True)
    parser.add_argument('--context-db', type=Path, default=Path('runs/val-usable/context.sqlite'))
    parser.add_argument('--config', type=Path, default=Path('configs/val.usable.deepseek.full.toml'))
    parser.add_argument('--unit-id', required=True)
    parser.add_argument('--target', required=True)
    args = parser.parse_args()
    from pdf_translation.config import load_config
    token_budget = load_config(args.config).context_token_budget
    units = [TranslationUnit.model_validate_json(line) for line in (args.capture / 'units.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
    unit = next((u for u in units if u.unit_id == args.unit_id), None)
    if unit is None:
        raise ValueError(f'unit not found in capture: {args.unit_id}')
    store = ContextStore(args.context_db)
    glossary = store.glossary()
    styles = store.styles()
    semantics = store.semantics()
    glossary_snapshot_id = stable_hash([entry for entry in glossary if entry.status == 'approved'])
    style_snapshot_id = stable_hash([rule for rule in styles if rule.status == 'approved'])
    base_packet, _ = build_context_packet(unit, glossary, styles, semantics, {u.unit_id: u for u in units}, token_budget=token_budget, glossary_snapshot_id=glossary_snapshot_id, style_snapshot_id=style_snapshot_id)
    entry = TranslationMemoryEntry(
        tm_id=f'curated-{args.unit_id}',
        source_text=unit.source_text,
        target_text=args.target,
        source_lang=unit.source_lang,
        target_lang=unit.target_lang,
        context_signature=base_packet.packet_hash,
        glossary_snapshot_id=glossary_snapshot_id,
        style_snapshot_id=style_snapshot_id,
        qa_status='passed',
        human_status='confirmed',
    )
    store.put_tm(entry)
    print(json.dumps({'tm_id': entry.tm_id, 'source_hash': entry.source_hash, 'context_signature': entry.context_signature}, ensure_ascii=True), flush=True)


if __name__ == '__main__':
    main()
