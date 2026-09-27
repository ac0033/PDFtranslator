"""HTTP API: translation jobs for callers on any host, plus MCP at ``/mcp``.

Standard library only, so any machine that can install this project can serve
it. Every route except ``/health`` needs ``Authorization: Bearer <token>`` when a
token is set, and the server refuses to listen beyond loopback without one.

    GET  /health                           version, and whether the AI OS and Jev are available
    GET  /v1/providers
    GET  /v1/profile                       this caller's default models
    PUT  /v1/profile                       JSON {"translation", "agent"}: set them
    POST /v1/jobs                          JSON {"source": {"base64"|"url", "filename", "main_file"}, ...JobRequest},
                                           or the raw document as the body (JobRequest JSON in X-Job-Request),
                                           or the v0.10 forms (application/pdf + X-Job-Options, pdf_base64/pdf_url + options)
    GET  /v1/jobs?limit=20
    GET  /v1/jobs/{job_id}
    GET  /v1/jobs/{job_id}/events?after=17&wait=60     long poll: returns at the first new event
    GET  /v1/jobs/{job_id}/log?lines=40
    GET  /v1/jobs/{job_id}/files/{name}                 the recommended version's file
    GET  /v1/jobs/{job_id}/files/{version}/{name}       usable | optimized | degraded | revised
    POST /v1/jobs/{job_id}/cancel
    POST /v1/jobs/{job_id}/resume       run a failed or cancelled job again; paid work is reused
    GET  /v1/jobs/{job_id}/units?page=3&unit_id=...&with_issues=1&state=...&limit=200
    POST /v1/jobs/{job_id}/assist       JSON {"task", ...}: the job's model does one task
    POST /v1/jobs/{job_id}/revise       JSON {"fixes": [...], "editor"}: write fixes, render again
    GET  /v1/guide                      docs/agent_guide.md
    GET  /v1/openapi.json               OpenAPI 3.1 description of these routes
    POST /mcp                           MCP streamable HTTP (JSON responses)

Model keys belong in the server's environment. As a fallback a key may be sent
for one job in ``X-LLM-API-Key`` (translation model), ``X-Agent-API-Key`` (the
AI OS model) or ``X-Jev-API-Key``; it reaches only that job's runner and is never
stored. Errors are ``{"error": {"code", "message", "hint"}}``.
"""
from __future__ import annotations

import base64
import binascii
import hmac
import ipaddress
import json
import os
import re
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

from pydantic import ValidationError

from . import __version__
from .budget import BudgetExceeded
from .jobspec import JobError, JobRequest, caller_key, validation_error
from .mcp_server import GUIDE, McpServer
from .openapi import openapi_spec
from .service import FILE_NAMES, MAX_SOURCE_BYTES, JobManager, JobOptions, fetch_source, providers

JOB_ROUTE = re.compile(
    r"^/v1/jobs/([^/]+)(?:/(log|cancel|resume|units|assist|revise|events|files/(?:(usable|optimized|degraded|revised)/)?([a-z_]+)))?$")
CONTENT_TYPES = {".pdf": "application/pdf", ".json": "application/json", ".jsonl": "application/x-ndjson",
                 ".log": "text/plain; charset=utf-8", ".html": "text/html; charset=utf-8",
                 ".md": "text/markdown; charset=utf-8", ".zip": "application/zip",
                 ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                 ".epub": "application/epub+zip"}


def is_loopback(host: str) -> bool:
    if host in {"localhost", ""}:
        return host == "localhost"
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class ApiServer(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second server bind a port already in use and
    # take some of its requests; there it must stay off.
    allow_reuse_address = os.name != "nt"

    def __init__(self, address: tuple[str, int], manager: JobManager, *, token: str | None,
                 public_url: str | None = None, allow_local_paths: bool = False):
        if not token and not is_loopback(address[0]):
            raise ValueError("refusing to listen beyond loopback without a token; set --token or PDFTRANS_TOKEN")
        # Set before binding: a failed bind calls server_close() on the way out.
        self._stop = threading.Event()
        try:
            super().__init__(address, Handler)
        except OSError as error:
            raise ValueError(f"cannot listen on {address[0]}:{address[1]}: {error.strerror or error}") from error
        self.manager = manager
        self.token = token
        host, port = self.server_address[:2]
        self.public_url = (public_url or f"http://{host}:{port}").rstrip("/")
        self.mcp = McpServer(manager, local=False, public_url=self.public_url, allow_local_paths=allow_local_paths,
                             caller=caller_key(None, token))
        self.allow_local_paths = allow_local_paths
        self._ticker = threading.Thread(target=self._tick, daemon=True)
        self._ticker.start()

    def _tick(self) -> None:
        # Queued jobs start when a slot frees up, and callbacks go out, even if no caller is polling.
        while not self._stop.wait(5):
            try:
                self.manager.dispatch()
                self.manager.deliver_callbacks()
            except Exception:
                pass

    def server_close(self) -> None:
        self._stop.set()
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    server: ApiServer
    protocol_version = "HTTP/1.1"

    # --- plumbing -----------------------------------------------------------------------

    def log_message(self, format, *args):  # keep the console for the operator's own messages
        pass

    def _send(self, status: int, payload: dict | list) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _fail(self, status: int, message: str, code: str = "bad_request", hint: str | None = None) -> None:
        body = {"code": code, "message": message}
        if hint:
            body["hint"] = hint
        self._send(status, {"error": body})

    def _refuse(self, problem: JobError) -> None:
        self._send(problem.status, problem.payload())

    def _authorised(self) -> bool:
        if not self.server.token:
            return True
        supplied = self.headers.get("Authorization", "")
        expected = f"Bearer {self.server.token}"
        if hmac.compare_digest(supplied.encode(), expected.encode()):
            return True
        self._fail(HTTPStatus.UNAUTHORIZED, "missing or wrong bearer token", "unauthorized")
        return False

    def _caller(self, client_id: str | None = None) -> str:
        return caller_key(client_id or self.headers.get("X-Client-Id"), self.server.token)

    def _body(self) -> bytes | None:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_SOURCE_BYTES * 4 // 3 + 4096:  # base64 of the largest document, plus JSON
            self._fail(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request body too large", "source_too_large")
            return None
        return self.rfile.read(length) if length else b""

    def _keys(self) -> dict[str, str]:
        return {role: self.headers.get(header) for role, header in
                (("translation", "X-LLM-API-Key"), ("agent", "X-Agent-API-Key"), ("jev", "X-Jev-API-Key"))
                if self.headers.get(header)}

    # --- routes -------------------------------------------------------------------------

    def do_GET(self) -> None:
        url = urlparse(self.path)
        if url.path == "/health":
            from .aios.pi_agent import availability

            ready, detail = availability()
            return self._send(HTTPStatus.OK, {"status": "ok", "version": __version__,
                                              "aios": {"available": ready, "detail": detail},
                                              "jev": {"configured": bool(os.environ.get("TYPESAFE_API_KEY"))}})
        if not self._authorised():
            return
        if url.path == "/v1/guide":
            body = GUIDE.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/markdown; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if url.path == "/v1/openapi.json":
            return self._send(HTTPStatus.OK, openapi_spec(self.server.public_url))
        if url.path == "/mcp":  # no server-initiated stream is offered
            return self._fail(HTTPStatus.METHOD_NOT_ALLOWED, "this server answers MCP over POST only", "method_not_allowed")
        query = parse_qs(url.query)
        try:
            if url.path == "/v1/providers":
                return self._send(HTTPStatus.OK, {"providers": providers()})
            if url.path == "/v1/profile":
                return self._send(HTTPStatus.OK, self.server.manager.profile(self._caller()))
            if url.path == "/v1/jobs":
                return self._send(HTTPStatus.OK, {"jobs": self.server.manager.list(int(query.get("limit", ["20"])[0]))})
            match = JOB_ROUTE.match(url.path)
            if not match:
                return self._fail(HTTPStatus.NOT_FOUND, "no such route", "not_found")
            job_id, action, version, file_name = match.groups()
            if action is None:
                return self._send(HTTPStatus.OK, self.server.manager.get(job_id))
            if action == "events":
                return self._send(HTTPStatus.OK, self.server.manager.events(
                    job_id, int(query.get("after", ["0"])[0]), float(query.get("wait", ["0"])[0])))
            if action == "log":
                lines = int(query.get("lines", ["40"])[0])
                return self._send(HTTPStatus.OK, {"job_id": job_id, "log": self.server.manager.log_tail(job_id, lines)})
            if action == "units":
                return self._send(HTTPStatus.OK, self.server.manager.units(
                    job_id, pages=[int(p) for p in query.get("page", [])], unit_ids=query.get("unit_id"),
                    with_issues=query.get("with_issues", ["0"])[0] in {"1", "true"}, states=query.get("state"),
                    limit=int(query.get("limit", ["200"])[0])))
            if file_name:
                return self._file(job_id, file_name, version)
            return self._fail(HTTPStatus.METHOD_NOT_ALLOWED, "use POST", "method_not_allowed")
        except JobError as problem:
            return self._refuse(problem)
        except ValueError as problem:
            return self._fail(HTTPStatus.BAD_REQUEST, str(problem))

    def _file(self, job_id: str, name: str, version: str | None) -> None:
        if name not in FILE_NAMES:
            return self._fail(HTTPStatus.NOT_FOUND, f"unknown file; use one of {list(FILE_NAMES)}", "not_found")
        files = self.server.manager.files(job_id, version)
        if name not in files:
            return self._fail(HTTPStatus.NOT_FOUND, f"{name} is not available (yet)", "file_not_ready",
                              "wait for a delivery event; the job's files list names what exists")
        path = files[name]
        size = path.stat().st_size
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", CONTENT_TYPES.get(path.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(size))
        if name in {"mono", "dual", "source"}:
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(path.name)}")
        self.end_headers()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                self.wfile.write(chunk)

    def do_PUT(self) -> None:
        url = urlparse(self.path)
        if not self._authorised():
            return
        body = self._body()
        if body is None:
            return
        if url.path != "/v1/profile":
            return self._fail(HTTPStatus.NOT_FOUND, "no such route", "not_found")
        try:
            request = json.loads(body or b"{}")
            return self._send(HTTPStatus.OK, self.server.manager.set_profile(
                self._caller(request.get("client_id")), request.get("translation"), request.get("agent")))
        except JobError as problem:
            return self._refuse(problem)
        except ValueError as problem:
            return self._fail(HTTPStatus.BAD_REQUEST, str(problem))

    def do_POST(self) -> None:
        url = urlparse(self.path)
        if not self._authorised():
            return
        body = self._body()
        if body is None:
            return
        if url.path == "/mcp":
            return self._mcp(body)
        try:
            if url.path == "/v1/jobs":
                return self._submit(body)
            match = JOB_ROUTE.match(url.path)
            action = match.group(2) if match else None
            if action == "cancel":
                return self._send(HTTPStatus.OK, self.server.manager.cancel(match.group(1)))
            if action == "resume":
                secrets_env = {}
                key = self.headers.get("X-LLM-API-Key")
                if key:
                    options = JobOptions.model_validate(self.server.manager._meta(match.group(1))["options"])
                    variable = options.api_key_variable()
                    if variable:
                        secrets_env[variable] = key
                return self._send(HTTPStatus.ACCEPTED, self.server.manager.resume(match.group(1), secrets_env=secrets_env))
            if action == "assist":
                arguments = json.loads(body or b"{}")
                task = arguments.pop("task", None)
                arguments.pop("api_key", None)  # keys travel in the header only
                budget = arguments.pop("budget_usd", None)
                return self._send(HTTPStatus.OK, self.server.manager.assist(
                    match.group(1), task, api_key=self.headers.get("X-LLM-API-Key"), budget_usd=budget, **arguments))
            if action == "revise":
                request = json.loads(body or b"{}")
                return self._send(HTTPStatus.ACCEPTED, self.server.manager.revise(
                    match.group(1), request.get("fixes") or [], request.get("editor") or ""))
            return self._fail(HTTPStatus.NOT_FOUND, "no such route", "not_found")
        except JobError as problem:
            return self._refuse(problem)
        except BudgetExceeded as problem:
            return self._fail(HTTPStatus.PAYMENT_REQUIRED, str(problem), "budget_exceeded")
        except ValidationError as problem:
            return self._refuse(validation_error(problem))
        except TypeError as problem:  # an argument the task does not take
            return self._fail(HTTPStatus.BAD_REQUEST, str(problem))
        except ValueError as problem:
            return self._fail(HTTPStatus.BAD_REQUEST, str(problem))
        except Exception as problem:  # the model service failed
            return self._fail(HTTPStatus.BAD_GATEWAY, f"{type(problem).__name__}: {problem}"[:500], "upstream_error")

    def _submit(self, body: bytes) -> None:
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        query = parse_qs(urlparse(self.path).query)
        if content_type != "application/json":
            data = body
            filename = query.get("filename", ["upload.pdf" if content_type == "application/pdf" else "upload"])[0]
            if self.headers.get("X-Job-Request"):
                request = JobRequest.model_validate(json.loads(self.headers["X-Job-Request"]))
            else:
                request = JobRequest.from_legacy(json.loads(self.headers.get("X-Job-Options") or "{}"))
        else:
            payload = json.loads(body or b"{}")
            source = payload.pop("source", None) or {}
            legacy_options = payload.pop("options", None)
            for legacy, new in (("pdf_base64", "base64"), ("pdf_url", "url")):
                if payload.get(legacy):
                    source[new] = payload.pop(legacy)
            filename = source.get("filename") or payload.pop("filename", None)
            if source.get("main_file"):
                payload["main_file"] = source["main_file"]
            if source.get("base64"):
                try:
                    data = base64.b64decode(source["base64"], validate=True)
                except (binascii.Error, ValueError) as problem:
                    raise JobError("source.base64 is not valid base64", code="invalid_request") from problem
            elif source.get("url"):
                data, fetched = fetch_source(source["url"])
                filename = filename or fetched
            else:
                raise JobError("send the document as source.base64 or source.url (or as the raw request body)",
                               code="invalid_request")
            request = (JobRequest.from_legacy({**legacy_options, **payload}) if legacy_options is not None
                       else JobRequest.model_validate(payload))
        job = self.server.manager.submit(
            data, filename or "upload", request, caller=self._caller(request.client_id), api_keys=self._keys(),
            callback_secret=self.headers.get("X-Callback-Secret"), idempotency_key=self.headers.get("Idempotency-Key"))
        self._send(HTTPStatus.ACCEPTED, job)

    def _mcp(self, body: bytes) -> None:
        try:
            message = json.loads(body)
        except json.JSONDecodeError:
            return self._send(HTTPStatus.BAD_REQUEST, {"jsonrpc": "2.0", "id": None,
                                                       "error": {"code": -32700, "message": "parse error"}})
        reply = self.server.mcp.handle(message) if isinstance(message, dict) else None
        if reply is None:  # a notification
            self.send_response(HTTPStatus.ACCEPTED)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._send(HTTPStatus.OK, reply)


def serve(host: str, port: int, manager: JobManager, *, token: str | None, public_url: str | None = None,
          allow_local_paths: bool = False) -> ApiServer:
    return ApiServer((host, port), manager, token=token, public_url=public_url, allow_local_paths=allow_local_paths)
