"""Jobs and every way in (MCP over stdio and HTTP, REST, CLI), with the runner faked."""
import base64
import hashlib
import hmac
import io
import json
import threading
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from pdf_translation import jobstate, service
from pdf_translation.cli import app
from pdf_translation.config import load_config
from pdf_translation.http_api import ApiServer
from pdf_translation.jobspec import JobRequest, ModelChoice
from pdf_translation.mcp_server import McpServer, serve_stdio
from pdf_translation.service import JobError, JobManager, JobOptions

PDF = b"%PDF-1.4\n% a tiny stand-in\n"
MODELS = {"translation": {"provider": "deepseek", "model": "deepseek-flash"},
          "agent": {"provider": "deepseek", "model": "deepseek-flash"}}


class FakeRunner:
    """Stands in for scripts/run_job.py: records how it was started."""

    started: list = []

    def __init__(self, command, cwd=None, env=None, stdin=None, stdout=None, stderr=None, creationflags=0, start_new_session=False):
        self.command, self.env, self.stdin = command, env, stdin
        self.pid = 4242 + len(FakeRunner.started)
        FakeRunner.started.append(self)


@pytest.fixture
def jobs(tmp_path: Path, monkeypatch):
    FakeRunner.started = []
    monkeypatch.setattr(service.subprocess, "Popen", FakeRunner)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-from-environment")
    alive: set[int] = set()
    monkeypatch.setattr(JobManager, "_alive", lambda self, meta: meta.get("pid") in alive)
    manager = JobManager(tmp_path / "jobs", max_running=1)
    manager.alive = alive
    return manager


def request(**extra) -> JobRequest:
    return JobRequest.model_validate({**MODELS, **extra})


def finish(manager: JobManager, job_id: str, *, version: str = "usable", verdict: str = "deliverable",
           status: str = "succeeded") -> None:
    """What the runner leaves behind: a published version and a final state."""
    job_dir = manager.root / job_id
    work = job_dir / "work"
    work.mkdir(exist_ok=True)
    (work / "doc.zh.pdf").write_bytes(PDF)
    report = {"verdict": verdict, "major_problems": [] if verdict != "not_deliverable" else
              [{"kind": "untranslated_text", "count": 2, "detail": "2 units still read as English"}],
              "minor_issues": [{"category": "number", "needs_human": 1, "sample_pages": [3]}]}
    jobstate.publish(job_dir, version, {"mono": work / "doc.zh.pdf"}, report)
    state = jobstate.read_state(job_dir) or {}
    state.update(status=status, phase="done", delivery=version, recommended=version,
                 cost_usd={"total": 0.01, "budget": 2.0})
    jobstate.write_state(job_dir, state)
    jobstate.append_event(job_dir, f"delivery.{version}", {"verdict": verdict})
    jobstate.append_event(job_dir, "job.finished", {})
    manager.alive.discard(manager._meta(job_id)["pid"])


# --- the job service ------------------------------------------------------------------

def test_options_need_prices_unless_the_model_has_known_ones():
    assert JobOptions(provider="deepseek", model_id="deepseek-flash").input_usd_per_million == 0.30
    with pytest.raises(ValueError, match="input_usd_per_million"):
        JobOptions(provider="qwen", model_id="qwen-max")
    assert JobOptions(provider="qwen", model_id="qwen-max", input_usd_per_million=1, output_usd_per_million=2)
    with pytest.raises(ValueError, match="needs base_url"):
        JobOptions(provider="openai-compatible", model_id="x", input_usd_per_million=0, output_usd_per_million=0)


def test_the_first_call_must_name_both_models_and_nothing_is_created_without_them(jobs):
    for missing in ({"translation": MODELS["translation"]}, {"agent": MODELS["agent"]}, {}):
        with pytest.raises(JobError) as refused:
            jobs.submit(PDF, "a.pdf", JobRequest.model_validate(missing), caller="client:new")
        assert refused.value.code == "model_required" and refused.value.status == 422
    assert list(jobs.root.glob("job-*")) == [] and FakeRunner.started == []


def test_later_calls_reuse_the_first_models_and_a_named_model_is_used_once(jobs):
    jobs.submit(PDF, "a.pdf", request(), caller="client:erp")
    second = jobs.submit(PDF, "b.pdf", JobRequest(), caller="client:erp")
    assert second["translation_model"] == "deepseek/deepseek-flash" and second["agent"]["model_source"] == "default"
    other = {"provider": "qwen", "model": "qwen-max", "input_usd_per_million": 1, "output_usd_per_million": 2}
    once = jobs.submit(PDF, "c.pdf", JobRequest.model_validate({"translation": other}), caller="client:erp",
                       secrets_env={"DASHSCOPE_API_KEY": "sk-q"})
    assert once["translation_model"] == "qwen/qwen-max"
    assert jobs.profile("client:erp")["translation"]["model"] == "deepseek-flash"  # the default did not move
    jobs.submit(PDF, "d.pdf", JobRequest.model_validate({"translation": other, "set_default": True}),
                caller="client:erp", secrets_env={"DASHSCOPE_API_KEY": "sk-q"})
    assert jobs.profile("client:erp")["translation"]["model"] == "qwen-max"
    with pytest.raises(JobError, match="model"):  # another caller does not inherit these defaults
        jobs.submit(PDF, "e.pdf", JobRequest(), caller="client:someone-else")


def test_a_job_whose_key_is_nowhere_is_refused_before_anything_is_created(jobs, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    with pytest.raises(JobError) as refused:
        jobs.submit(PDF, "a.pdf", request())
    assert refused.value.code == "key_missing" and "DEEPSEEK_API_KEY" in str(refused.value)
    assert list(jobs.root.glob("job-*")) == []
    job = jobs.submit(PDF, "a.pdf", request(), api_keys={"translation": "sk-header"})  # the fallback
    assert FakeRunner.started[-1].env["DEEPSEEK_API_KEY"] == "sk-header"
    job_dir = jobs.root / job["job_id"]
    assert all("sk-header" not in p.read_text(encoding="utf-8", errors="ignore") for p in job_dir.rglob("*") if p.is_file())


def test_a_job_runs_with_a_valid_config(jobs):
    job = jobs.submit(PDF, "notes.pdf", request(style_note="policy 译为策略"))
    runner = FakeRunner.started[0]
    jobs.alive.add(runner.pid)
    assert runner.command[1:3] == ["-m", "scripts.run_job"] and "--job-dir" in runner.command
    job_dir = jobs.root / job["job_id"]
    assert (job_dir / "style.txt").read_text(encoding="utf-8") == "policy 译为策略"
    config = load_config(job_dir / "config.toml")
    assert (config.run_id, config.reasoning_effort, config.budget_usd) == (job["job_id"], "none", 2.0)
    assert jobs.get(job["job_id"])["status"] == "starting"
    finish(jobs, job["job_id"])
    view = jobs.get(job["job_id"])
    assert view["status"] == "succeeded" and view["delivery"] == "usable" and view["result"]["verdict"] == "deliverable"
    assert set(view["files"]) >= {"mono", "acceptance"}


@pytest.mark.parametrize("name,data,expected", [
    ("paper.pdf", PDF, "pdf"),
    ("notes.md", b"# Title\n\nSome text.", "markdown"),
    ("page.html", b"<!doctype html><html><body><p>Hi there</p></body></html>", "html"),
    ("paper.tex", b"\\documentclass{article}\\begin{document}Hello world\\end{document}", "latex"),
    ("project.zip", b"PK\x03\x04rest", "latex"),
])
def test_every_supported_format_is_recognised(jobs, name, data, expected):
    job = jobs.submit(data, name, request())
    assert job["format"] == expected


def test_an_unsupported_or_empty_document_is_refused(jobs):
    with pytest.raises(JobError) as refused:
        jobs.submit(b"\x00\x01binary", "x.bin", request())
    assert refused.value.code == "unsupported_format" and refused.value.status == 415
    with pytest.raises(JobError, match="empty"):
        jobs.submit(b"", "x.pdf", request())
    with pytest.raises(JobError, match="no such job"):
        jobs.get("../../etc")


def test_the_runner_cannot_read_the_callers_stdin(jobs):
    # Under MCP stdio the server's stdin is the JSON-RPC channel; a runner inheriting it could eat messages.
    jobs.submit(PDF, "a.pdf", request())
    assert FakeRunner.started[0].stdin is service.subprocess.DEVNULL


def test_jobs_beyond_the_limit_wait_their_turn(jobs):
    first = jobs.submit(PDF, "a.pdf", request())
    jobs.alive.add(FakeRunner.started[0].pid)
    second = jobs.submit(PDF, "b.pdf", request())
    assert jobs.get(second["job_id"])["status"] == "queued" and len(FakeRunner.started) == 1
    finish(jobs, first["job_id"])
    jobs.dispatch()
    assert len(FakeRunner.started) == 2


def test_a_runner_that_dies_without_finishing_is_reported_failed(jobs):
    job = jobs.submit(PDF, "a.pdf", request())
    view = jobs.get(job["job_id"])  # never marked alive
    assert view["status"] == "failed" and view["error"]["code"] == "runner_stopped"
    assert "no files" in view["for_agent"]["summary"].lower()


def test_versions_are_kept_separately_and_the_recommended_one_is_served(jobs):
    job = jobs.submit(PDF, "a.pdf", request())
    finish(jobs, job["job_id"], version="usable")
    finish(jobs, job["job_id"], version="optimized")
    view = jobs.get(job["job_id"])
    assert view["recommended"] == "optimized" and [d["version"] for d in view["deliveries"]] == ["usable", "optimized"]
    assert jobs.files(job["job_id"])["mono"].parent.name == "optimized"
    assert jobs.files(job["job_id"], "usable")["mono"].parent.name == "usable"
    entry = view["deliveries"][0]["files"]["mono"]
    assert entry["sha256"] == hashlib.sha256(PDF).hexdigest() and entry["bytes"] == len(PDF)
    with pytest.raises(JobError, match="no degraded"):
        jobs.files(job["job_id"], "degraded")


def test_a_degraded_delivery_has_files_and_says_it_is_not_deliverable(jobs):
    job = jobs.submit(PDF, "a.pdf", request())
    finish(jobs, job["job_id"], version="degraded", verdict="not_deliverable")
    view = jobs.get(job["job_id"])
    assert view["status"] == "succeeded" and view["delivery"] == "degraded" and "mono" in view["files"]
    assert [p["kind"] for p in view["problems"] if p["severity"] == "major"] == ["untranslated_text"]
    assert "NOT deliverable" in view["for_agent"]["summary"] and "不能当作完成品" in view["for_agent"]["summary_zh"]


def test_events_are_numbered_and_a_long_poll_returns_at_the_first_new_one(jobs):
    job = jobs.submit(PDF, "a.pdf", request())
    first = jobs.events(job["job_id"])
    assert [e["type"] for e in first["events"]] == ["job.accepted"]
    jobs.alive.add(FakeRunner.started[0].pid)
    timer = threading.Timer(0.3, finish, args=(jobs, job["job_id"]))
    timer.start()
    later = jobs.events(job["job_id"], after=first["last_seq"], wait=10)
    timer.join()
    assert later["events"][0]["type"] == "delivery.usable" and later["events"][0]["seq"] == 2
    assert jobs.wait(job["job_id"], after=99, timeout=1)["job"]["status"] == "succeeded"


def test_callbacks_are_signed_ordered_and_retried(jobs, monkeypatch):
    received, fail = [], [True]

    def handler(req: httpx.Request) -> httpx.Response:
        if fail[0]:
            fail[0] = False
            return httpx.Response(503)
        received.append(req)
        return httpx.Response(200)

    with pytest.raises(JobError, match="signing secret"):
        jobs.submit(PDF, "a.pdf", request(callback_url="https://host.example/hook"))
    job = jobs.submit(PDF, "a.pdf", request(callback_url="https://host.example/hook"), callback_secret="s3cret")
    transport = httpx.MockTransport(handler)
    assert jobs.deliver_callbacks(transport=transport) == 0  # the host was down: nothing lost, retried later
    jobs._callback_retry.clear()
    assert jobs.deliver_callbacks(transport=transport) == 1
    body = received[0].content
    expected = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert received[0].headers["X-Signature"] == f"sha256={expected}" and json.loads(body)["type"] == "job.accepted"
    assert jobs.deliver_callbacks(transport=transport) == 0  # already delivered


def test_a_retried_submission_with_the_same_idempotency_key_is_one_job(jobs):
    first = jobs.submit(PDF, "a.pdf", request(), idempotency_key="order-17")
    again = jobs.submit(PDF, "a.pdf", request(), idempotency_key="order-17")
    assert first["job_id"] == again["job_id"] and len(list(jobs.root.glob("job-*"))) == 1


# --- MCP ------------------------------------------------------------------------------

def rpc(server: McpServer, method: str, params: dict | None = None, msg_id: int = 1) -> dict:
    return server.handle({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}})


def call(server: McpServer, name: str, arguments: dict) -> dict:
    return rpc(server, "tools/call", {"name": name, "arguments": arguments})["result"]


def test_mcp_handshake_and_tools(jobs):
    server = McpServer(jobs, local=True)
    init = rpc(server, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t"}})
    assert init["result"]["protocolVersion"] == "2025-03-26" and "tools" in init["result"]["capabilities"]
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    names = {tool["name"] for tool in rpc(server, "tools/list")["result"]["tools"]}
    assert {"translate_document", "translate_pdf", "wait_job", "get_job", "get_job_file", "list_providers",
            "get_profile", "set_profile"} <= names
    assert rpc(server, "no/such")["error"]["code"] == -32601


def test_mcp_translates_waits_and_returns_files(jobs, tmp_path):
    server = McpServer(jobs, local=True)
    doc = tmp_path / "notes.md"
    doc.write_text("# Notes\n\nThe model learns quickly.\n", encoding="utf-8")
    refused = call(server, "translate_document", {"path": str(doc)})
    assert refused["isError"] and refused["structuredContent"]["error"]["code"] == "model_required"
    started = call(server, "translate_document", {"path": str(doc), **MODELS, "budget_usd": 0.5})
    assert not started["isError"] and started["structuredContent"]["format"] == "markdown"
    job_id = started["structuredContent"]["job_id"]
    finish(jobs, job_id)
    waited = call(server, "wait_job", {"job_id": job_id, "events_after": 1, "timeout_s": 2})["structuredContent"]
    assert waited["job"]["delivery"] == "usable" and "deliverable" in waited["job"]["for_agent"]["summary"]
    got = call(server, "get_job_file", {"job_id": job_id, "name": "mono", "inline": True})["structuredContent"]
    assert base64.b64decode(got["base64"]) == PDF and got["sha256"] == hashlib.sha256(PDF).hexdigest()


def test_mcp_legacy_translate_pdf_still_works(jobs, tmp_path):
    server = McpServer(jobs, local=True)
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(PDF)
    started = call(server, "translate_pdf", {"pdf_path": str(pdf_path), "provider": "deepseek", "model_id": "deepseek-flash",
                                             "agent": MODELS["agent"], "budget_usd": 0.5})
    assert not started["isError"], started
    assert started["structuredContent"]["request"]["budget_usd"] == 0.5


def test_remote_mcp_callers_cannot_name_server_files(jobs, tmp_path):
    server = McpServer(jobs, local=False, public_url="https://t.example")
    reply = call(server, "translate_document", {"path": str(tmp_path / "x.pdf"), **MODELS})
    assert reply["isError"] and "local clients" in reply["structuredContent"]["error"]["message"]


def test_mcp_over_stdio_answers_line_by_line(jobs):
    stdin = io.StringIO('{"jsonrpc":"2.0","id":7,"method":"ping"}\nnot json\n{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
    stdout = io.StringIO()
    serve_stdio(McpServer(jobs, local=True), stdin, stdout)
    replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert replies[0] == {"jsonrpc": "2.0", "id": 7, "result": {}} and replies[1]["error"]["code"] == -32700
    assert len(replies) == 2


# --- HTTP -----------------------------------------------------------------------------

@pytest.fixture
def api(jobs):
    server = ApiServer(("127.0.0.1", 0), jobs, token="t0ken")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, httpx.Client(base_url=server.public_url, headers={"Authorization": "Bearer t0ken"})
    server.shutdown()
    server.server_close()


def test_http_needs_the_token_except_for_health(api):
    server, client = api
    health = httpx.get(f"{server.public_url}/health").json()
    assert health["status"] == "ok" and "available" in health["aios"] and "configured" in health["jev"]
    denied = httpx.get(f"{server.public_url}/v1/jobs")
    assert denied.status_code == 401 and denied.json()["error"]["code"] == "unauthorized"
    assert client.get("/v1/providers").status_code == 200


def test_http_submit_wait_and_download_by_version(api):
    server, client = api
    refused = client.post("/v1/jobs", json={"source": {"base64": base64.b64encode(PDF).decode(), "filename": "a.pdf"}})
    assert refused.status_code == 422 and refused.json()["error"]["code"] == "model_required"
    created = client.post("/v1/jobs", json={"source": {"base64": base64.b64encode(PDF).decode(), "filename": "a.pdf"},
                                            **MODELS, "deliver": {"optimized": False}},
                          headers={"X-Agent-API-Key": "sk-agent"})
    assert created.status_code == 202
    job_id = created.json()["job_id"]
    assert FakeRunner.started[-1].env["DEEPSEEK_API_KEY"] == "sk-agent"  # both models are DeepSeek here
    assert client.get(f"/v1/jobs/{job_id}/files/mono").status_code == 404
    finish(server.manager, job_id)
    events = client.get(f"/v1/jobs/{job_id}/events", params={"after": 1, "wait": 1}).json()
    assert [e["type"] for e in events["events"]] == ["delivery.usable", "job.finished"]
    assert client.get(f"/v1/jobs/{job_id}").json()["status"] == "succeeded"
    download = client.get(f"/v1/jobs/{job_id}/files/usable/mono")
    assert download.content == PDF and download.headers["content-type"] == "application/pdf"
    assert client.get(f"/v1/jobs/{job_id}/files/optimized/mono").status_code == 404
    assert client.get("/v1/jobs/..%2F..%2Fsecret").status_code == 404
    assert client.post("/v1/jobs", json={**MODELS}).json()["error"]["code"] == "invalid_request"


def test_http_accepts_the_v0_10_pdf_upload_once_a_default_exists(api):
    server, client = api
    client.put("/v1/profile", json=MODELS)
    created = client.post("/v1/jobs", params={"filename": "a.pdf"}, content=PDF,
                          headers={"Content-Type": "application/pdf", "X-Job-Options": '{"budget_usd": 0.2}',
                                   "X-LLM-API-Key": "sk-remote"})
    assert created.status_code == 202, created.text
    assert FakeRunner.started[-1].env["DEEPSEEK_API_KEY"] == "sk-remote"
    assert client.get("/v1/profile").json()["agent"]["model"] == "deepseek-flash"


def test_http_carries_mcp(api):
    _, client = api
    reply = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()
    assert any(tool["name"] == "translate_document" for tool in reply["result"]["tools"])
    assert client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}).status_code == 202


def test_openapi_describes_every_route_and_every_tool(api):
    import re
    from pdf_translation import http_api
    from pdf_translation.mcp_server import TOOLS

    _, client = api
    spec = client.get("/v1/openapi.json").json()
    assert spec["openapi"].startswith("3.1") and spec["servers"][0]["url"] == client.base_url.__str__().rstrip("/")
    documented = {(method.lower(), path.split("?")[0])
                  for method, path in re.findall(r"^\s+(GET|POST|PUT)\s+(/\S+)", http_api.__doc__, re.M)}
    described = {(method, path) for path, item in spec["paths"].items() for method in item}
    assert documented == described
    operation_ids = {op.get("operationId") for item in spec["paths"].values() for op in item.values()}
    assert {tool["name"] for tool in TOOLS} <= operation_ids | set(spec["x-mcp-aliases"])
    assert set(spec["x-mcp-aliases"].values()) <= operation_ids
    assert set(JobRequest.model_fields) - {"translation", "agent"} <= set(
        spec["components"]["schemas"]["JobRequest"]["properties"])


def test_listening_beyond_loopback_requires_a_token(jobs):
    with pytest.raises(ValueError, match="without a token"):
        ApiServer(("0.0.0.0", 0), jobs, token=None)


def test_a_port_in_use_is_reported_as_such(api, jobs):
    server, _ = api
    with pytest.raises(ValueError, match="cannot listen"):
        ApiServer(("127.0.0.1", server.server_address[1]), jobs, token="x")


# --- CLI ------------------------------------------------------------------------------

def test_cli_submits_without_waiting_and_prints_json(jobs, tmp_path):
    doc = tmp_path / "doc.md"
    doc.write_text("# A\n\nThe text of the page.\n", encoding="utf-8")
    first = CliRunner().invoke(app, ["translate", str(doc), "--no-wait", "--jobs-root", str(jobs.root)])
    assert first.exit_code == 2 and "model_required" in first.stdout
    result = CliRunner().invoke(app, ["translate", str(doc), "--no-wait", "--jobs-root", str(jobs.root),
                                      "--budget-usd", "0.3", "--provider", "deepseek", "--model", "deepseek-flash",
                                      "--agent-provider", "deepseek", "--agent-model", "deepseek-flash", "--no-optimized"])
    assert result.exit_code == 0, result.output
    job = json.loads(result.stdout[result.stdout.index("{"):])
    assert job["request"]["budget_usd"] == 0.3 and job["request"]["deliver"]["optimized"] is False
    assert job["format"] == "markdown"
    listed = CliRunner().invoke(app, ["jobs", "list", "--jobs-root", str(jobs.root)])
    assert job["job_id"] in listed.stdout
    profile = CliRunner().invoke(app, ["profile", "--jobs-root", str(jobs.root)])
    assert "deepseek-flash" in profile.stdout
