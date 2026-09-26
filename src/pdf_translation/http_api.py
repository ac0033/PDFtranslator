"""HTTP API: translation jobs for callers on any host, plus MCP at ``/mcp``.

Standard library only, so any machine that can install this project can serve
it. Every route except ``/health`` needs ``Authorization: Bearer <token>`` when a
token is set, and the server refuses to listen beyond loopback without one.

    GET  /health
    GET  /v1/providers
    POST /v1/jobs                       application/pdf body (options in the
                                        X-Job-Options header as JSON), or JSON
                                        {"pdf_base64"|"pdf_url", "filename", "options"}
    GET  /v1/jobs?limit=20
    GET  /v1/jobs/{job_id}
    GET  /v1/jobs/{job_id}/log?lines=40
    GET  /v1/jobs/{job_id}/files/{name}  mono | dual | acceptance | layout_review | content_review | log
    POST /v1/jobs/{job_id}/cancel
    POST /mcp                           MCP streamable HTTP (JSON responses)

A model API key for one job may be sent in ``X-LLM-API-Key``; it reaches only
that job's runner process and is never stored.
"""
from __future__ import annotations

import base64
import hmac
import ipaddress
import os
import json
import re
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from pydantic import ValidationError

from . import __version__
from .mcp_server import McpServer
from .service import FILE_NAMES, MAX_PDF_BYTES, JobError, JobManager, JobOptions, fetch_pdf, providers

JOB_ROUTE = re.compile(r"^/v1/jobs/([^/]+)(?:/(log|cancel|files/([a-z_]+)))?$")
CONTENT_TYPES = {".pdf": "application/pdf", ".json": "application/json", ".jsonl": "application/x-ndjson",
                 ".log": "text/plain; charset=utf-8"}


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
        self.mcp = McpServer(manager, local=False, public_url=self.public_url, allow_local_paths=allow_local_paths)
        self.allow_local_paths = allow_local_paths
        self._ticker = threading.Thread(target=self._tick, daemon=True)
        self._ticker.start()

    def _tick(self) -> None:
        # Queued jobs start when a slot frees up, even if no caller is polling.
        while not self._stop.wait(10):
            try:
                self.manager.dispatch()
            except Exception:
                pass

    def server_close(self) -> None:
        self._stop.set()
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    server: ApiServer
    protocol_version = "HTTP/1.1"

    # --- plumbing -----------------------------------------------------------------------

    def _send(self, status: int, payload: dict | list) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _fail(self, status: int, message: str) -> None:
        self._send(status, {"error": message})

    def _authorised(self) -> bool:
        if not self.server.token:
            return True
        supplied = self.headers.get("Authorization", "")
        expected = f"Bearer {self.server.token}"
        if hmac.compare_digest(supplied.encode(), expected.encode()):
            return True
        self._fail(HTTPStatus.UNAUTHORIZED, "missing or wrong bearer token")
        return False

    def _body(self) -> bytes | None:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_PDF_BYTES * 4 // 3 + 4096:  # base64 of the largest PDF, plus JSON
            self._fail(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request body too large")
            return None
        return self.rfile.read(length) if length else b""

    # --- routes -------------------------------------------------------------------------

    def do_GET(self) -> None:
        url = urlparse(self.path)
        if url.path == "/health":
            return self._send(HTTPStatus.OK, {"status": "ok", "version": __version__})
        if not self._authorised():
            return
        if url.path == "/mcp":  # no server-initiated stream is offered
            return self._fail(HTTPStatus.METHOD_NOT_ALLOWED, "this server answers MCP over POST only")
        query = parse_qs(url.query)
        try:
            if url.path == "/v1/providers":
                return self._send(HTTPStatus.OK, {"providers": providers()})
            if url.path == "/v1/jobs":
                return self._send(HTTPStatus.OK, {"jobs": self.server.manager.list(int(query.get("limit", ["20"])[0]))})
            match = JOB_ROUTE.match(url.path)
            if not match:
                return self._fail(HTTPStatus.NOT_FOUND, "no such route")
            job_id, action, file_name = match.groups()
            if action is None:
                return self._send(HTTPStatus.OK, self.server.manager.get(job_id))
            if action == "log":
                lines = int(query.get("lines", ["40"])[0])
                return self._send(HTTPStatus.OK, {"job_id": job_id, "log": self.server.manager.log_tail(job_id, lines)})
            if file_name:
                return self._file(job_id, file_name)
            return self._fail(HTTPStatus.METHOD_NOT_ALLOWED, "use POST")
        except JobError as problem:
            return self._fail(HTTPStatus.NOT_FOUND, str(problem))

    def _file(self, job_id: str, name: str) -> None:
        if name not in FILE_NAMES:
            return self._fail(HTTPStatus.NOT_FOUND, f"unknown file; use one of {list(FILE_NAMES)}")
        files = self.server.manager.files(job_id)
        if name not in files:
            return self._fail(HTTPStatus.NOT_FOUND, f"{name} is not available yet")
        path = files[name]
        size = path.stat().st_size
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", CONTENT_TYPES.get(path.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(size))
        if path.suffix == ".pdf":
            self.send_header("Content-Disposition", f'attachment; filename="{job_id}.{name}.pdf"')
        self.end_headers()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                self.wfile.write(chunk)

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
            if match and match.group(2) == "cancel":
                return self._send(HTTPStatus.OK, self.server.manager.cancel(match.group(1)))
            return self._fail(HTTPStatus.NOT_FOUND, "no such route")
        except (JobError, ValidationError, ValueError) as problem:
            return self._fail(HTTPStatus.BAD_REQUEST, str(problem))

    def _submit(self, body: bytes) -> None:
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        query = parse_qs(urlparse(self.path).query)
        if content_type == "application/pdf":
            pdf = body
            filename = query.get("filename", ["upload.pdf"])[0]
            options_raw = json.loads(self.headers.get("X-Job-Options") or "{}")
        else:
            request = json.loads(body or b"{}")
            options_raw = request.get("options") or {}
            filename = request.get("filename")
            if request.get("pdf_base64"):
                pdf = base64.b64decode(request["pdf_base64"], validate=True)
            elif request.get("pdf_url"):
                pdf, fetched_name = fetch_pdf(request["pdf_url"])
                filename = filename or fetched_name
            else:
                raise JobError("send the PDF as an application/pdf body, pdf_base64 or pdf_url")
        options = JobOptions.model_validate(options_raw)
        secrets_env = {}
        key = self.headers.get("X-LLM-API-Key")
        if key:
            variable = options.api_key_variable()
            if not variable:
                raise JobError(f"provider {options.provider} takes no API key; set api_key_env to name one")
            secrets_env[variable] = key
        self._send(HTTPStatus.ACCEPTED, self.server.manager.submit(pdf, filename or "upload.pdf", options, secrets_env=secrets_env))

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
