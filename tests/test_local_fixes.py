import json
from pathlib import Path

import pytest

from pdf_translation.config import load_config
from pdf_translation.hashing import stable_hash
from pdf_translation.pipeline import make_request
from pdf_translation.quality import check_quality
from pdf_translation.schemas import TranslationDraft, TranslationResult, TranslationUnit, Usage
from pdf_translation.storage import StateStore
from scripts import apply_local_fixes as fixes

RUN_ID = "local-fixes-test"
CONFIG_TOML = f"""
run_id = "{RUN_ID}"
provider = "deepseek"
model_id = "deepseek-flash"
style_revision = "s1"
prompt_revision = "p1"
adapter_revision = "a1"
pricing_revision = "p"
max_output_tokens = 2000
budget_usd = 1.0
"""
SOURCE = "Let ⟦P:bd-p0001-u0000-o000⟧ be the weight of ⟦P:bd-p0001-u0000-o001⟧."
STORED = "令⟦P:bd-p0001-u0000-o001⟧为⟦P:bd-p0001-u0000-o000⟧的权重。"
FIXED = "令⟦P:bd-p0001-u0000-o000⟧为⟦P:bd-p0001-u0000-o001⟧的权重。"


def _unit(uid: str) -> TranslationUnit:
    return TranslationUnit(unit_id=uid, document_id="doc", ir_revision="ir", unit_type="paragraph",
                           source_lang="en", target_lang="zh-CN", source_text=SOURCE, source_hash=stable_hash(SOURCE))


@pytest.fixture
def book(tmp_path: Path, monkeypatch):
    config = tmp_path / "run.toml"
    config.write_text(CONFIG_TOML, encoding="utf-8")
    capture = tmp_path / "capture"
    capture.mkdir()
    units = [_unit("u1"), _unit("u2"), _unit("u3")]
    (capture / "units.jsonl").write_text("\n".join(u.model_dump_json() for u in units), encoding="utf-8")
    store = StateStore(tmp_path / "runs" / RUN_ID / "state.sqlite")
    store.begin_run(RUN_ID, "input", "config")
    for unit in units:
        store.prepare(RUN_ID, unit.unit_id, f"key-{unit.unit_id}")
    for unit in units[:2]:
        request = make_request(unit, load_config(config))
        result = TranslationResult(request_id=request.request_id, unit_id=unit.unit_id, finish="complete", model=request.model,
                                   draft=TranslationDraft(unit_id=unit.unit_id, translated_text=STORED),
                                   usage=Usage(input_tokens=0, output_tokens=0, cost_usd=0, usage_source="estimated"))
        store.commit(RUN_ID, result, check_quality(unit, result), f"key-{unit.unit_id}")
    store.mark_exhausted(RUN_ID, "u3")
    monkeypatch.setattr(fixes, "_write_content_review_queue", lambda *args: None)
    monkeypatch.setattr(fixes, "pages_of", lambda capture, ids: [1])
    return store, capture, tmp_path / "runs", config


def _apply(book, batch):
    store, capture, runs, config = book
    return fixes.apply(batch, "agent:test", capture=capture, config_path=config, runs_root=runs)


def _fix(uid: str, new: str, old: str | None = STORED) -> dict:
    return {"unit_id": uid, "source_hash": stable_hash(SOURCE), "old_translated_text": old,
            "new_translated_text": new, "reason": "test"}


def test_fix_replaces_text_and_keeps_the_old_one_as_evidence(book):
    store = book[0]
    _apply(book, [_fix("u1", FIXED)])
    assert store.result(RUN_ID, "u1").draft.translated_text == FIXED
    with store.connect() as con:
        detail = json.loads(con.execute("SELECT detail FROM events WHERE unit_id='u1'").fetchone()[0])
    assert detail["old_translated_text"] == STORED and detail["editor"] == "agent:test"


def test_fix_written_against_an_older_translation_is_refused(book):
    with pytest.raises(ValueError, match="not the one this fix was written against"):
        _apply(book, [_fix("u1", "任何文本", old="一个过时的译文")])
    assert book[0].result(RUN_ID, "u1").draft.translated_text == STORED


def test_one_fix_dropping_a_formula_marker_aborts_the_whole_batch(book):
    good = _fix("u1", FIXED)
    dropped = _fix("u2", "令⟦P:bd-p0001-u0000-o000⟧为权重。")
    with pytest.raises(ValueError, match="quality gate blocks"):
        _apply(book, [good, dropped])
    # Nothing is written when any fix in the batch is rejected.
    assert book[0].result(RUN_ID, "u1").draft.translated_text == STORED


def test_an_untranslated_unit_can_be_filled(book):
    store = book[0]
    _apply(book, [_fix("u3", FIXED, old=None)])
    assert store.result(RUN_ID, "u3").draft.translated_text == FIXED
    assert store.state(RUN_ID, "u3") != "exhausted"


def test_filling_cannot_be_used_to_overwrite_a_translation_blindly(book):
    # old=None claims "there was nothing here"; for a translated unit that is false.
    with pytest.raises(ValueError, match="not an untranslated unit"):
        _apply(book, [_fix("u1", FIXED, old=None)])
    assert book[0].result(RUN_ID, "u1").draft.translated_text == STORED
