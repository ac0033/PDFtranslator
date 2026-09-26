"""Jobs and every way in (MCP over stdio and HTTP, REST, CLI), with the runner faked."""
import base64
import io
import json
import threading
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from pdf_translation import service
from pdf_translation.cli import app
from pdf_translation.config import load_config
from pdf_translation.http_api import ApiServer
from pdf_translation.mcp_server import McpServer, serve_stdio
from pdf_translation.service import JobError, JobManager, JobOptions

PDF = b"%PDF-1.4\n% a tiny stand-in\n"


class FakeRunner:
    """Stands in for scripts/deliver_pdf.py: records how it was started."""

    started: list = []

    def __init__(self, command, cwd=None, env=None, stdin=None, stdout=None, stderr=None, creationflags=0, start_new_session=False):
        self.command, self.env, self.stdin = command, env, stdin
        self.pid = 4242 + len(FakeRunner.started)
        FakeRunner.started.append(self)


@pytest.fixture
def jobs(tmp_path: Path, monkeypatch):
    FakeRunner.started = []
    monkeypatch.setattr(service.subprocess, "Popen", FakeRunner)
    alive: set[int] = set()
    monkeypatch.setattr(JobManager, "_alive", lambda self, meta: meta.get("pid") in alive)
    manager = JobManager(tmp_path / "jobs", max_running=1)
    manager.alive = alive
    return manager


def finish(manager: JobManager, job_id: str, status: str = "succeeded") -> None:
    job_dir = manager.root / job_id
    work = job_dir / "work"
    work.mkdir(exist_ok=True)
    (work / "source.zh.zh.mono.pdf").write_bytes(PDF)
    (work / "acceptance-report.json").write_text('{"verdict": "deliverable"}', encoding="utf-8")
    timings = {"status": status, "pages": 3, "stages": {"capture": {"seconds": 1.0, "exit_code": 0}},
               "result": {"verdict": "deliverable", "cost_usd": 0.01}}
    (job_dir / "work.timings.json").write_text(json.dumps(timings), encoding="utf-8")
    manager.alive.discard(manager._meta(job_id)["pid"])


# --- the job service ------------------------------------------------------------------

def test_options_need_prices_unless_the_model_has_known_ones():
    assert JobOptions().input_usd_per_million == 0.30
    with pytest.raises(ValueError, match="input_usd_per_million"):
        JobOptions(provider="qwen", model_id="qwen-max")
    assert JobOptions(provider="qwen", model_id="qwen-max", input_usd_per_million=1, output_usd_per_million=2)
    with pytest.raises(ValueError, match="needs base_url"):
        JobOptions(provider="openai-compatible", model_id="x", input_usd_per_million=0, output_usd_per_million=0)


def test_a_job_runs_with_a_valid_config_and_its_key_never_touches_disk(jobs):
    job = jobs.submit(PDF, "notes.pdf", JobOptions(style_note="policy 译为策略"), secrets_env={"DEEPSEEK_API_KEY": "sk-secret"})
    runner = FakeRunner.started[0]
    jobs.alive.add(runner.pid)
    assert runner.env["DEEPSEEK_API_KEY"] == "sk-secret"
    assert "--style-note-file" in runner.command
    job_dir = jobs.root / job["job_id"]
    assert all("sk-secret" not in p.read_text(encoding="utf-8", errors="ignore") for p in job_dir.rglob("*") if p.is_file())
    config = load_config(job_dir / "config.toml")
    assert (config.run_id, config.reasoning_effort) == (job["job_id"], "none")
    assert jobs.get(job["job_id"])["status"] == "starting"
    finish(jobs, job["job_id"])
    view = jobs.get(job["job_id"])
    assert view["status"] == "succeeded" and view["result"]["verdict"] == "deliverable"
    assert set(view["files"]) >= {"mono", "acceptance"}


def test_the_runner_cannot_read_the_callers_stdin(jobs):
    # Under MCP stdio the server's stdin is the JSON-RPC channel; a runner inheriting it could eat messages.
    jobs.submit(PDF, "a.pdf", JobOptions())
    assert FakeRunner.started[0].stdin is service.subprocess.DEVNULL


def test_jobs_beyond_the_limit_wait_their_turn(jobs):
    first = jobs.submit(PDF, "a.pdf", JobOptions())
    jobs.alive.add(FakeRunner.started[0].pid)
    second = jobs.submit(PDF, "b.pdf", JobOptions())
    assert jobs.get(second["job_id"])["status"] == "queued" and len(FakeRunner.started) == 1
    finish(jobs, first["job_id"])
    jobs.dispatch()
    assert len(FakeRunner.started) == 2


def test_a_runner_that_dies_without_finishing_is_reported_failed(jobs):
    job = jobs.submit(PDF, "a.pdf", JobOptions())
    assert jobs.get(job["job_id"])["status"] == "failed"  # never marked alive


def test_bad_input_and_bad_ids_are_refused(jobs):
    with pytest.raises(JobError, match="not a PDF"):
        jobs.submit(b"hello", "x.pdf", JobOptions())
    with pytest.raises(JobError, match="no such job"):
        jobs.get("../../etc")


# --- MCP ------------------------------------------------------------------------------

def rpc(server: McpServer, method: str, params: dict | None = None, msg_id: int = 1) -> dict:
    return server.handle({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}})


def test_mcp_handshake_and_tools(jobs):
    server = McpServer(jobs, local=True)
    init = rpc(server, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t"}})
    assert init["result"]["protocolVersion"] == "2025-03-26" and "tools" in init["result"]["capabilities"]
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    names = {tool["name"] for tool in rpc(server, "tools/list")["result"]["tools"]}
    assert {"translate_pdf", "get_job", "get_job_file", "list_providers"} <= names
    assert rpc(server, "no/such")["error"]["code"] == -32601


def test_mcp_translates_polls_and_returns_files(jobs, tmp_path):
    server = McpServer(jobs, local=True)
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(PDF)
    started = rpc(server, "tools/call", {"name": "translate_pdf", "arguments": {"pdf_path": str(pdf_path), "budget_usd": 0.5}})["result"]
    assert not started["isError"]
    job_id = started["structuredContent"]["job_id"]
    finish(jobs, job_id)
    status = rpc(server, "tools/call", {"name": "get_job", "arguments": {"job_id": job_id}})["result"]["structuredContent"]
    assert status["status"] == "succeeded"
    got = rpc(server, "tools/call", {"name": "get_job_file", "arguments": {"job_id": job_id, "name": "mono", "inline": True}})
    assert base64.b64decode(got["result"]["structuredContent"]["base64"]) == PDF


def test_remote_mcp_callers_cannot_name_server_files(jobs, tmp_path):
    server = McpServer(jobs, local=False, public_url="https://t.example")
    reply = rpc(server, "tools/call", {"name": "translate_pdf", "arguments": {"pdf_path": str(tmp_path / "x.pdf")}})
    assert reply["result"]["isError"] and "local clients" in reply["result"]["structuredContent"]["error"]


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
    assert httpx.get(f"{server.public_url}/health").json()["status"] == "ok"
    assert httpx.get(f"{server.public_url}/v1/jobs").status_code == 401
    assert client.get("/v1/providers").status_code == 200


def test_http_upload_poll_and_download(api):
    server, client = api
    created = client.post("/v1/jobs", params={"filename": "a.pdf"}, content=PDF,
                          headers={"Content-Type": "application/pdf", "X-Job-Options": '{"budget_usd": 0.2}',
                                   "X-LLM-API-Key": "sk-remote"})
    assert created.status_code == 202
    job_id = created.json()["job_id"]
    assert FakeRunner.started[-1].env["DEEPSEEK_API_KEY"] == "sk-remote"
    assert client.get(f"/v1/jobs/{job_id}/files/mono").status_code == 404
    finish(server.manager, job_id)
    assert client.get(f"/v1/jobs/{job_id}").json()["status"] == "succeeded"
    download = client.get(f"/v1/jobs/{job_id}/files/mono")
    assert download.content == PDF and download.headers["content-type"] == "application/pdf"
    assert client.get("/v1/jobs/..%2F..%2Fsecret").status_code == 404
    assert client.post("/v1/jobs", json={"options": {}}).status_code == 400


def test_http_carries_mcp(api):
    _, client = api
    reply = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()
    assert any(tool["name"] == "translate_pdf" for tool in reply["result"]["tools"])
    assert client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}).status_code == 202


def test_openapi_describes_every_route_and_every_tool(api):
    import re
    from pdf_translation import http_api
    from pdf_translation.mcp_server import TOOLS, _OPTION_SCHEMA

    _, client = api
    spec = client.get("/v1/openapi.json").json()
    assert spec["openapi"].startswith("3.1") and spec["servers"][0]["url"] == client.base_url.__str__().rstrip("/")
    documented = {(method.lower(), path.split("?")[0])
                  for method, path in re.findall(r"^\s+(GET|POST)\s+(/\S+)", http_api.__doc__, re.M)}
    described = {(method, path) for path, item in spec["paths"].items() for method in item}
    assert documented == described
    operation_ids = {op.get("operationId") for item in spec["paths"].values() for op in item.values()}
    assert {tool["name"] for tool in TOOLS} <= operation_ids
    assert set(_OPTION_SCHEMA) <= set(JobOptions.model_fields)


def test_listening_beyond_loopback_requires_a_token(jobs):
    with pytest.raises(ValueError, match="without a token"):
        ApiServer(("0.0.0.0", 0), jobs, token=None)


# --- CLI ------------------------------------------------------------------------------

def test_cli_submits_without_waiting_and_prints_json(jobs, tmp_path):
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(PDF)
    result = CliRunner().invoke(app, ["translate", str(pdf), "--no-wait", "--jobs-root", str(jobs.root), "--budget-usd", "0.3"])
    assert result.exit_code == 0, result.output
    job = json.loads(result.stdout[result.stdout.index("{"):])
    assert job["options"]["budget_usd"] == 0.3
    listed = CliRunner().invoke(app, ["jobs", "list", "--jobs-root", str(jobs.root)])
    assert job["job_id"] in listed.stdout


def test_a_port_in_use_is_reported_as_such(api, jobs):
    server, _ = api
    with pytest.raises(ValueError, match="cannot listen"):
        ApiServer(("127.0.0.1", server.server_address[1]), jobs, token="x")
