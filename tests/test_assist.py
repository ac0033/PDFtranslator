"""An agent working on a finished job: reading units, delegating to the job's model, writing fixes back."""
import json
import threading
from pathlib import Path

import httpx
import pymupdf
import pytest

from pdf_translation import llm, service
from pdf_translation.config import load_config
from pdf_translation.context_models import GlossaryEntry, Scope, StyleRule
from pdf_translation.context_store import ContextStore
from pdf_translation.hashing import stable_hash
from pdf_translation.http_api import ApiServer
from pdf_translation.llm import LLMResponse, make_llm_client
from pdf_translation.mcp_server import McpServer
from pdf_translation.pipeline import make_request
from pdf_translation.quality import check_quality
from pdf_translation.schemas import SourceRef, TranslationDraft, TranslationResult, TranslationUnit, Usage
from pdf_translation.service import JobError, JobManager
from pdf_translation.storage import StateStore
from scripts import deliver_pdf

JOB = "job-20260926-120000-abcdef"
M0, M1 = "⟦P:bd-p0001-u0000-o000⟧", "⟦P:bd-p0001-u0000-o001⟧"
SOURCES = {"u1": f"The policy gradient uses {M0} as the step size.",
           "u2": "Value iteration converges when the discount factor is below one.",
           "u3": f"We update the weights {M1} after every episode."}
STORED = {"u1": f"策略梯度使用{M0}作为步长。", "u2": "当折扣因子小于一时，价值迭代收敛。", "u3": None}


def unit(uid: str) -> TranslationUnit:
    return TranslationUnit(unit_id=uid, document_id="doc", ir_revision="ir", unit_type="paragraph", source_lang="en",
                           target_lang="zh-CN", source_text=SOURCES[uid], source_hash=stable_hash(SOURCES[uid]),
                           source_refs=(SourceRef(block_id=uid, page_index=0, bbox=(72, 72, 300, 90), char_start=0,
                                                  char_end=len(SOURCES[uid])),))


class FakeRunner:
    started: list = []

    def __init__(self, command, **_):
        self.command = command
        self.pid = 5000 + len(FakeRunner.started)
        FakeRunner.started.append(self)


class FakeModel:
    """Answers with the queued replies and remembers every request."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []
        self.protocol, self.api_key_env = "fake", None

    def complete(self, request):
        self.requests.append(request)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return LLMResponse(text=json.dumps(reply, ensure_ascii=False),
                           usage=Usage(input_tokens=1000, output_tokens=200, usage_source="provider"))


@pytest.fixture
def job(tmp_path: Path, monkeypatch):
    FakeRunner.started = []
    monkeypatch.setattr(service.subprocess, "Popen", FakeRunner)
    monkeypatch.setattr(JobManager, "_alive", lambda self, meta: False)
    manager = JobManager(tmp_path / "jobs", runs_root=tmp_path / "runs")
    job_dir = manager.root / JOB
    work = job_dir / "work"
    work.mkdir(parents=True)
    context_db = job_dir / "context.sqlite"
    (job_dir / "config.toml").write_text(
        f'run_id = "{JOB}"\nprovider = "deepseek"\nmodel_id = "deepseek-flash"\nstyle_revision = "s"\n'
        f'prompt_revision = "p"\nadapter_revision = "a"\npricing_revision = "p"\nmax_output_tokens = 2000\n'
        f'budget_usd = 0.01\ninput_usd_per_million = 0.30\noutput_usd_per_million = 1.20\n'
        f'context_db = "{context_db.as_posix()}"\n', encoding="utf-8")
    config = load_config(job_dir / "config.toml")
    document = pymupdf.open()
    document.new_page().insert_text((72, 72), "The policy gradient")
    document.save(job_dir / "source.pdf")
    units = [unit(uid) for uid in SOURCES]
    (work / "units.jsonl").write_text("\n".join(u.model_dump_json() for u in units), encoding="utf-8")
    store = StateStore(tmp_path / "runs" / JOB / "state.sqlite")
    store.begin_run(JOB, "input", "config")
    rows = []
    for u in units:
        store.prepare(JOB, u.unit_id, f"key-{u.unit_id}")
        text = STORED[u.unit_id]
        if text is None:
            store.mark_exhausted(JOB, u.unit_id)
        else:
            request = make_request(u, config)
            result = TranslationResult(request_id=request.request_id, unit_id=u.unit_id, finish="complete",
                                       model=request.model, draft=TranslationDraft(unit_id=u.unit_id, translated_text=text),
                                       usage=Usage(input_tokens=0, output_tokens=0, cost_usd=0, usage_source="estimated"))
            store.commit(JOB, result, check_quality(u, result), f"key-{u.unit_id}")
        rows.append({"unit_id": u.unit_id, "source_hash": u.source_hash, "source_pages": [1], "source_text": u.source_text,
                     "translated_text": text, "state": "exhausted" if text is None else "accepted",
                     "issues": [] if text else [{"category": "untranslated_unit", "severity": "critical", "explanation": "x"}]})
    (work / "content-review.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
    context = ContextStore(context_db)
    context.put_style(StyleRule(rule_id="style", scope=Scope(kind="document", value="doc"), version="1",
                                content="强化学习教材，术语从严。"))
    context.put_glossary(GlossaryEntry(term_id="t1", source_term="policy gradient", target_term="策略梯度", sense="rl",
                                       scope=Scope(kind="document", value="doc"), source_ids=("u1",), version="1",
                                       status="approved"))
    for page in (1,):
        page_dir = work / f"page-{page:04d}"
        page_dir.mkdir()
        for label in ("mono", "dual"):
            (page_dir / f"source.zh.{label}.pdf").write_bytes(b"%PDF-1.4\n")
    (job_dir / "job.json").write_text(json.dumps({
        "job_id": JOB, "source_name": "rl.pdf", "created_at": "2026-09-26T04:00:00+00:00",
        "options": {"budget_usd": 0.01}, "pid": 4321, "cancelled": False}), encoding="utf-8")
    (job_dir / "work.timings.json").write_text(json.dumps({
        "status": "succeeded", "stages": {"capture": {"seconds": 5.0, "exit_code": 0}},
        "result": {"verdict": "not_deliverable"}}), encoding="utf-8")
    return manager, store


def use_model(monkeypatch, model: FakeModel) -> list:
    keys = []

    def client(config, *, api_key=None):
        keys.append(api_key)
        return model

    monkeypatch.setattr(llm, "config_client", client)
    return keys


# --- delegating to the job's model ------------------------------------------------------

def test_retranslation_gets_the_books_style_and_glossary_and_is_booked(job, monkeypatch):
    manager, store = job
    fixed = f"我们在每个回合之后更新权重{M1}。"
    model = FakeModel({"translated_text": fixed, "note": "translated"})
    use_model(monkeypatch, model)
    answer = manager.assist(JOB, "retranslate", unit_ids=["u3"], instruction="保持简洁")
    candidate = answer["candidates"][0]
    assert candidate["fix"] == {"unit_id": "u3", "source_hash": stable_hash(SOURCES["u3"]), "old_translated_text": None,
                                "new_translated_text": fixed, "reason": "agent assist (retranslate): translated"}
    prompt = model.requests[0].user
    assert "强化学习教材" in prompt and "保持简洁" in prompt and SOURCES["u2"] in prompt  # style, instruction, context
    with store.connect() as con:
        booked = con.execute("SELECT unit_id, source, cost_usd FROM ledger WHERE source='agent_assist'").fetchall()
    assert [(r[0], r[1]) for r in booked] == [("assist:u3", "agent_assist")] and booked[0][2] > 0
    assert answer["cost_usd"] == pytest.approx(booked[0][2])
    log = [json.loads(line) for line in (manager.root / JOB / "assist.jsonl").read_text(encoding="utf-8").splitlines()]
    assert log[0]["task"] == "retranslate" and log[0]["fixes_proposed"] == 1


def test_glossary_terms_reach_the_model_only_where_they_occur(job, monkeypatch):
    manager, _ = job
    model = FakeModel({"translated_text": STORED["u1"] + "。", "note": ""}, {"translated_text": "价值迭代收敛。", "note": ""})
    use_model(monkeypatch, model)
    manager.assist(JOB, "retranslate", unit_ids=["u1", "u2"])
    by_source = {SOURCES[uid]: r.user for r in model.requests for uid in SOURCES if f"<source>\n{SOURCES[uid]}" in r.user}
    assert "policy gradient → 策略梯度" in by_source[SOURCES["u1"]]
    assert "<glossary>" not in by_source[SOURCES["u2"]]


def test_a_candidate_the_quality_gate_blocks_is_shown_but_not_applicable(job, monkeypatch):
    manager, _ = job
    use_model(monkeypatch, FakeModel({"translated_text": "策略梯度使用步长。", "note": "dropped the marker"}))
    candidate = manager.assist(JOB, "retranslate", unit_ids=["u1"])["candidates"][0]
    assert candidate["quality"] == "blocked" and "fix" not in candidate and candidate["issues"]


def test_a_shortened_text_that_is_not_shorter_is_not_offered(job, monkeypatch):
    manager, _ = job
    longer = f"策略梯度方法使用{M0}作为其更新的步长大小。"
    use_model(monkeypatch, FakeModel({"translated_text": longer, "note": ""}))
    candidate = manager.assist(JOB, "shorten", unit_ids=["u1"])["candidates"][0]
    assert "fix" not in candidate and "not shorter" in candidate["issues"][-1]


def test_a_task_beyond_the_budget_sends_nothing_until_the_caller_raises_it(job, monkeypatch):
    manager, store = job
    model = FakeModel({"translated_text": "x", "note": ""})
    use_model(monkeypatch, model)
    store.record_usage(JOB, "earlier", Usage(cost_usd=0.0099, usage_source="provider"), "translation")
    with pytest.raises(Exception, match="Raise budget_usd"):
        manager.assist(JOB, "retranslate", unit_ids=["u2"])
    assert model.requests == []
    manager.assist(JOB, "retranslate", unit_ids=["u2"], budget_usd=0.5)
    assert len(model.requests) == 1 and manager.get(JOB)["budget_usd"] == 0.5


def test_review_keeps_only_problems_about_units_it_was_shown(job, monkeypatch):
    manager, _ = job
    fixed = f"策略梯度以{M0}为步长。"
    use_model(monkeypatch, FakeModel({"problems": [
        {"unit_id": "u1", "severity": "wrong_meaning", "explanation": "e", "fixed_translation": fixed},
        {"unit_id": "u9", "severity": "wrong_meaning", "explanation": "invented", "fixed_translation": "x"}]}))
    answer = manager.assist(JOB, "review_units", unit_ids=["u1", "u2"])
    assert [p["unit_id"] for p in answer["problems"]] == ["u1"] and answer["problems"][0]["fix"]["new_translated_text"] == fixed


def test_ask_returns_parsed_json_and_needs_pages_for_images(job, monkeypatch):
    manager, _ = job
    model = FakeModel({"ok": True})
    keys = use_model(monkeypatch, model)
    answer = manager.assist(JOB, "ask", prompt="Is u1 fine?", unit_ids=["u1"], api_key="sk-call",
                            json_schema={"type": "object", "properties": {"ok": {"type": "boolean"}}})
    assert answer["json"] == {"ok": True} and SOURCES["u1"] in model.requests[0].user and keys == ["sk-call"]
    with pytest.raises(ValueError, match="images needs pages"):
        manager.assist(JOB, "ask", prompt="look", images=True)


def test_an_account_refusal_is_raised_not_hidden(job, monkeypatch):
    manager, _ = job
    use_model(monkeypatch, FakeModel(llm.LLMHTTPError(402, "Insufficient Balance")))
    with pytest.raises(llm.LLMHTTPError):
        manager.assist(JOB, "retranslate", unit_ids=["u2"])


def test_only_a_finished_job_can_be_worked_on(job):
    manager, _ = job
    timings = manager.root / JOB / "work.timings.json"
    timings.write_text(json.dumps({"status": "running"}), encoding="utf-8")
    with pytest.raises(JobError, match="finished translating"):
        manager.assist(JOB, "retranslate", unit_ids=["u1"])


def test_units_can_be_filtered_to_what_needs_attention(job):
    manager, _ = job
    flagged = manager.units(JOB, with_issues=True)
    assert [row["unit_id"] for row in flagged["units"]] == ["u3"] and flagged["units"][0]["issues"]
    assert manager.units(JOB, pages=[2])["matched"] == 0


# --- writing fixes back ---------------------------------------------------------------

def test_revision_writes_fixes_and_renders_only_the_changed_pages_again(job):
    manager, store = job
    fixed = f"我们在每个回合之后更新权重{M1}。"
    view = manager.revise(JOB, [{"unit_id": "u3", "old_translated_text": None, "new_translated_text": fixed}], "agent:test")
    assert store.result(JOB, "u3").draft.translated_text == fixed
    command = FakeRunner.started[-1].command
    assert command[command.index("--mode") + 1] == "revise"
    assert view["revisions"][0]["pages"] == [1]
    assert manager._meta(JOB)["revise_pages"] == [1]  # the runner renders just these pages again
    state = json.loads((manager.root / JOB / "state.json").read_text(encoding="utf-8"))
    assert state["phase"] == "revise"


def test_a_fix_against_an_outdated_translation_refuses_the_whole_batch(job):
    manager, store = job
    good = {"unit_id": "u3", "old_translated_text": None, "new_translated_text": f"更新权重{M1}。"}
    stale = {"unit_id": "u2", "old_translated_text": "旧译文", "new_translated_text": "新译文。"}
    with pytest.raises(JobError, match="nothing written"):
        manager.revise(JOB, [good, stale], "agent:test")
    assert store.result(JOB, "u3") is None and FakeRunner.started == []


def test_deliver_pdf_resumed_at_render_captures_and_translates_nothing(tmp_path):
    ran = []
    args = deliver_pdf.build_parser().parse_args(["--source", "s.pdf", "--config", "c.toml", "--resume-from", "render"])
    deliver_pdf.run_stages(args, load_config_stub(), tmp_path, 10, tmp_path, [], lambda name, command, fatal=True: ran.append(name) or 0)
    assert ran == ["render", "layout-review", "acceptance"]


def load_config_stub():
    class Config:
        run_id = "r"
    return Config()


# --- every way in ---------------------------------------------------------------------

def rpc(server, method, params=None):
    return server.handle({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})


def test_mcp_offers_the_guide_and_the_new_tools(job, monkeypatch):
    manager, _ = job
    server = McpServer(manager, local=False, public_url="https://t.example")
    names = {tool["name"] for tool in rpc(server, "tools/list")["result"]["tools"]}
    assert {"get_units", "ask_model", "apply_fixes"} <= names
    uri = rpc(server, "resources/list")["result"]["resources"][0]["uri"]
    assert "ask_model" in rpc(server, "resources/read", {"uri": uri})["result"]["contents"][0]["text"]
    use_model(monkeypatch, FakeModel({"translated_text": f"更新权重{M1}。", "note": ""}))
    reply = rpc(server, "tools/call", {"name": "ask_model", "arguments": {"job_id": JOB, "task": "retranslate", "unit_ids": ["u3"]}})
    fix = reply["result"]["structuredContent"]["candidates"][0]["fix"]
    applied = rpc(server, "tools/call", {"name": "apply_fixes", "arguments": {"job_id": JOB, "fixes": [fix], "editor": "agent:t"}})
    assert not applied["result"]["isError"] and applied["result"]["structuredContent"]["revisions"]
    broken = rpc(server, "tools/call", {"name": "ask_model", "arguments": {"job_id": JOB, "task": "retranslate", "unit_ids": ["u1"]}})
    assert broken["result"]["isError"]  # the job is rendering again


@pytest.fixture
def api(job):
    manager, _ = job
    server = ApiServer(("127.0.0.1", 0), manager, token="t0ken")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield httpx.Client(base_url=server.public_url, headers={"Authorization": "Bearer t0ken"})
    server.shutdown()
    server.server_close()


def test_http_units_assist_and_guide(api, monkeypatch):
    assert api.get(f"/v1/jobs/{JOB}/units", params={"with_issues": 1}).json()["units"][0]["unit_id"] == "u3"
    assert "ask_model" in api.get("/v1/guide").text
    model = FakeModel({"translated_text": "x", "note": ""})
    keys = use_model(monkeypatch, model)
    refused = api.post(f"/v1/jobs/{JOB}/assist", json={"task": "retranslate", "unit_ids": ["u2"] * 1 + ["u1"],
                                                       "max_output_tokens": 8000})
    assert refused.status_code == 402 and model.requests == []
    ok = api.post(f"/v1/jobs/{JOB}/assist", json={"task": "retranslate", "unit_ids": ["u2"], "budget_usd": 1},
                  headers={"X-LLM-API-Key": "sk-remote"})
    assert ok.status_code == 200 and keys[-1] == "sk-remote"
    assert api.post(f"/v1/jobs/{JOB}/assist", json={"task": "nope"}).status_code == 400


def test_a_key_given_for_one_client_wins_over_the_environment(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "from-env")
    assert llm._api_key(make_llm_client("deepseek", api_key="given").endpoint) == "given"
    assert llm._api_key(make_llm_client("deepseek").endpoint) == "from-env"
    assert "given" not in repr(make_llm_client("deepseek", api_key="given").endpoint)


# --- resuming ---------------------------------------------------------------------------

def test_a_failed_job_resumes_where_it_stopped(job, monkeypatch):
    manager, _ = job
    state = manager.root / JOB / "state.json"
    with pytest.raises(JobError, match="only a failed or cancelled"):
        manager.resume(JOB)  # it succeeded
    state.write_text(json.dumps({"status": "failed", "error": {"code": "workflow_failed", "message": "HTTP 402"}}),
                     encoding="utf-8")
    manager.resume(JOB, secrets_env={"DEEPSEEK_API_KEY": "sk-new"})
    runner = FakeRunner.started[-1]
    assert "--mode" not in runner.command
    monkeypatch.setattr(JobManager, "_alive", lambda self, meta: True)
    assert manager.get(JOB)["status"] == "starting"
    assert json.loads(state.read_text(encoding="utf-8")).get("error") is None


def test_a_rerun_keeps_a_finished_capture_instead_of_failing_on_it(tmp_path):
    (tmp_path / "capture.report.json").write_text("{}", encoding="utf-8")
    ran = []
    args = deliver_pdf.build_parser().parse_args(["--source", "s.pdf", "--config", "c.toml"])
    deliver_pdf.run_stages(args, load_config_stub(), tmp_path, 10, tmp_path, [], lambda name, command, fatal=True: ran.append(name) or 0)
    assert ran[0] == "coverage" and "capture" not in ran


def test_units_left_in_english_are_found_even_though_only_acceptance_judges_them(job):
    manager, _ = job
    path = manager.root / JOB / "work" / "content-review.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    rows[1]["translated_text"] = SOURCES["u2"]  # came back in English, with no warning of its own
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
    flagged = {row["unit_id"]: row["issues"] for row in manager.units(JOB, with_issues=True)["units"]}
    assert "u2" in flagged and flagged["u2"][0].startswith("untranslated_unit")
