"""Write reviewed translations back into a run, checked against what they replace.

A fix is ``{unit_id, source_hash, old_translated_text, new_translated_text,
reason}``. ``old_translated_text`` is null for a unit that never got a
translation (state ``exhausted``). Every fix is checked against the current
source and the current stored text before anything is written, so a fix
written for an older translation cannot silently overwrite a newer one. The
replaced text is kept verbatim in the ``events`` table. No model is called.

A fix whose new text the quality gate blocks (e.g. a dropped or invented
protected marker) aborts the whole batch.
"""
from __future__ import annotations

import json
from pathlib import Path

from .babeldoc_adapter import _write_content_review_queue
from .config import RunConfig
from .hashing import stable_hash
from .pipeline import make_request
from .quality import check_quality
from .schemas import TranslationDraft, TranslationResult, TranslationUnit, Usage
from .storage import StateStore


def load_units(capture: Path) -> dict[str, TranslationUnit]:
    return {
        unit.unit_id: unit for line in (capture / "units.jsonl").read_text(encoding="utf-8").splitlines()
        if line for unit in [TranslationUnit.model_validate_json(line)]
    }


def apply_fixes(fixes: list[dict], editor: str, *, capture: Path, config: RunConfig, runs_root: Path) -> list[int]:
    """Validate the whole batch, then write it. Returns the pages to rerender."""
    if len({fix["unit_id"] for fix in fixes}) != len(fixes):
        raise ValueError("one fix per unit")
    run_id = config.run_id
    units = load_units(capture)
    store = StateStore(runs_root / run_id / "state.sqlite")
    prepared = []
    for fix in fixes:
        uid = fix["unit_id"]
        unit = units[uid]
        if fix["source_hash"] != unit.source_hash:
            raise ValueError(f"source has changed for {uid}")
        current = store.result(run_id, uid)
        if fix["old_translated_text"] is None:
            if current is not None or store.state(run_id, uid) != "exhausted":
                raise ValueError(f"{uid} is not an untranslated unit; name the translation this fix replaces")
        elif current is None or current.draft.translated_text != fix["old_translated_text"]:
            raise ValueError(f"stored translation for {uid} is not the one this fix was written against")
        request = make_request(unit, config)
        result = TranslationResult(
            request_id=request.request_id, unit_id=uid,
            draft=TranslationDraft(unit_id=uid, translated_text=fix["new_translated_text"]),
            finish="complete", provider_response_id=None, model=request.model,
            usage=Usage(input_tokens=0, output_tokens=0, cost_usd=0, usage_source="estimated"),
        )
        quality = check_quality(unit, result)
        if quality.status == "blocked":
            raise ValueError(f"quality gate blocks the fix for {uid}: {[i.explanation for i in quality.issues]}")
        cache_key = store.task_cache_key(run_id, uid)
        if not cache_key:
            raise RuntimeError(f"no existing run identity for {uid}")
        prepared.append((unit, result, quality, cache_key, fix))
    for unit, result, quality, cache_key, fix in prepared:
        store.commit(run_id, result, quality, cache_key, cached=True, source="agent_curated", record_usage=False)
        with store.connect() as con:
            con.execute("INSERT INTO events(run_id,unit_id,event,detail) VALUES(?,?,?,?)", (
                run_id, unit.unit_id, "local_translation_fix",
                json.dumps({"editor": editor, "reason": fix["reason"], "source_hash": unit.source_hash,
                            "old_translated_text": fix["old_translated_text"],
                            "new_target_hash": stable_hash(fix["new_translated_text"])}, ensure_ascii=False),
            ))
    _write_content_review_queue(list(units.values()), store, run_id, capture / "content-review.jsonl")
    return pages_of(capture, [unit.unit_id for unit, *_ in prepared])


def pages_of(capture: Path, unit_ids: list[str]) -> list[int]:
    """One-based pages the named units appear on, from the review queue."""
    wanted = set(unit_ids)
    pages = set()
    for line in (capture / "content-review.jsonl").read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row["unit_id"] in wanted:
            pages.update(row["source_pages"])
    return sorted(pages)
