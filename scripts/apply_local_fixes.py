"""Replace reviewed translations in a run and rerender only their pages.

Input is a JSON list of fixes, each ``{unit_id, source_hash, old_translated_text,
new_translated_text, reason}``. ``old_translated_text`` is null for a unit that
never got a translation (state ``exhausted``). Every fix is checked against the
current source and the current stored text before anything is written, so a
fix written for an older translation cannot silently overwrite a newer one. The
replaced text is kept verbatim in the ``events`` table. Never calls a
translation provider.

A fix whose new text the quality gate blocks (e.g. a dropped or invented
protected marker) aborts the whole batch.

    uv run python -m scripts.apply_local_fixes fixes.json --editor "agent:..." \\
        --capture output/pdf/<run> --config configs/<run>.toml --source pdf/<file>.pdf --output-prefix zh
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys

from pdf_translation.babeldoc_adapter import _write_content_review_queue
from pdf_translation.config import load_config
from pdf_translation.hashing import stable_hash
from pdf_translation.pipeline import make_request
from pdf_translation.quality import check_quality
from pdf_translation.schemas import TranslationDraft, TranslationResult, TranslationUnit, Usage
from pdf_translation.storage import StateStore

ROOT = Path(__file__).resolve().parent.parent


def apply(fixes: list[dict], editor: str, *, capture: Path, config_path: Path, runs_root: Path = ROOT / "runs") -> list[int]:
    if len({fix["unit_id"] for fix in fixes}) != len(fixes):
        raise ValueError("one fix per unit")
    cfg = load_config(config_path)
    run_id = cfg.run_id
    units = {
        unit.unit_id: unit for line in (capture / "units.jsonl").read_text(encoding="utf-8").splitlines()
        if line for unit in [TranslationUnit.model_validate_json(line)]
    }
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
        request = make_request(unit, cfg)
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
    # Validate the whole batch before writing any row.
    for unit, result, quality, cache_key, fix in prepared:
        store.commit(run_id, result, quality, cache_key, cached=True, source="agent_curated", record_usage=False)
        with store.connect() as con:
            con.execute("INSERT INTO events(run_id,unit_id,event,detail) VALUES(?,?,?,?)", (
                run_id, unit.unit_id, "local_translation_fix",
                json.dumps({"editor": editor, "reason": fix["reason"], "source_hash": unit.source_hash,
                            "old_translated_text": fix["old_translated_text"],
                            "new_target_hash": stable_hash(fix["new_translated_text"])}, ensure_ascii=False),
            ))
        print(json.dumps({"unit": unit.unit_id, "quality": quality.status,
                          "issues": [i.category for i in quality.issues]}, ensure_ascii=False), flush=True)
    _write_content_review_queue(list(units.values()), store, run_id, capture / "content-review.jsonl")
    return pages_of(capture, [unit.unit_id for unit, *_ in prepared])


def pages_of(capture: Path, unit_ids: list[str]) -> list[int]:
    wanted = set(unit_ids)
    pages = set()
    for line in (capture / "content-review.jsonl").read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row["unit_id"] in wanted:
            pages.update(row["source_pages"])
    return sorted(pages)


def rerender(pages: list[int], *, capture: Path, config_path: Path, source: Path, prefix: str) -> None:
    common = ["--capture", str(capture), "--config", str(config_path), "--source", str(source), "--output-prefix", prefix]
    for number in pages:
        for label in ("mono", "dual"):
            (capture / f"page-{number:04d}" / f"{source.stem}.zh.{label}.pdf").unlink(missing_ok=True)
        subprocess.run([sys.executable, "scripts/render_val_full.py", *common,
                        "--from-page", str(number), "--to-page", str(number)], cwd=ROOT, check=True)
    subprocess.run([sys.executable, "scripts/render_val_full.py", *common, "--merge-only"], cwd=ROOT, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("fixes", type=Path)
    parser.add_argument("--editor", required=True, help="Who reviewed these fixes, e.g. 'agent:claude-opus-5-5'.")
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output-prefix", required=True, help="Infix of the merged book filenames.")
    parser.add_argument("--rerender-only", action="store_true", help="Fixes are already stored; only rerender their pages.")
    args = parser.parse_args()
    fixes = json.loads(args.fixes.read_text(encoding="utf-8"))
    pages = (pages_of(args.capture, [fix["unit_id"] for fix in fixes]) if args.rerender_only
             else apply(fixes, args.editor, capture=args.capture, config_path=args.config))
    print(json.dumps({"rerendering": pages}), flush=True)
    rerender(pages, capture=args.capture, config_path=args.config, source=args.source, prefix=args.output_prefix)


if __name__ == "__main__":
    main()
