"""Model Context Protocol server: PDF translation as tools an AI agent can call.

The protocol is JSON-RPC 2.0. :class:`McpServer.handle` answers one message and
is shared by both transports:

* stdio (``pdftrans mcp``): newline-delimited messages on stdin/stdout, for a
  client on the same machine (Claude Desktop, Claude Code, Cursor, ...);
* streamable HTTP (``POST /mcp`` on ``pdftrans serve``): for clients on other
  hosts, behind the server's bearer token.

Translation takes minutes to hours, so the tools follow a job pattern: submit,
poll, fetch. Reading a PDF from a path on the server is allowed only to a local
client (or when the operator enables it): a remote caller who could name any
server path could have that file's contents translated and handed back.
"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from . import __version__
from .service import FILE_NAMES, JobError, JobManager, JobOptions, fetch_pdf, providers

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
MAX_INLINE_BYTES = 25 * 1024 * 1024
INSTRUCTIONS = (
    "Translate English PDFs (textbooks, papers, lecture notes) into Chinese, keeping layout, formulas "
    "and bookmarks. Call translate_pdf (it returns a job at once), then poll get_job every 30-60 s until "
    "status is succeeded, estimated, failed or cancelled, then get_job_file for 'mono' (Chinese) or "
    "'dual' (bilingual). Set estimate_only first to learn the price without spending. result.verdict "
    "'not_deliverable' lists major_problems; read the acceptance file before handing the PDF to a user."
)

_OPTION_SCHEMA = {
    "provider": {"type": "string", "description": "Model vendor preset (see list_providers). Default deepseek."},
    "model_id": {"type": "string", "description": "Model name at that vendor. Default deepseek-flash."},
    "base_url": {"type": "string", "description": "Override the vendor endpoint (proxy, private deployment)."},
    "api_key_env": {"type": "string", "description": "Name of the server environment variable holding the key."},
    "budget_usd": {"type": "number", "description": "Hard spend ceiling in USD. Default 1.0."},
    "input_usd_per_million": {"type": "number", "description": "Model input price; required for models without a built-in price."},
    "output_usd_per_million": {"type": "number", "description": "Model output price; required with the input price."},
    "style_note": {"type": "string", "description": "Per-document instruction: subject, term choices (e.g. 'policy 译为 策略')."},
    "layout_review": {"type": "string", "enum": ["all", "flagged", "off"], "description": "Page-by-page model review after rendering. Default all."},
    "vision": {"type": "boolean", "description": "Send page images to the reviewing model. Default true."},
    "estimate_only": {"type": "boolean", "description": "Only capture and price; no model call, no charge."},
    "workers": {"type": "integer", "description": "Parallel capture/render processes, 1-8. Default 2."},
    "translation_concurrency": {"type": "integer", "description": "Model requests in flight, 1-32. Default 8."},
}


def _schema(properties: dict, required: tuple[str, ...] = ()) -> dict:
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


TOOLS = [
    {"name": "list_providers",
     "description": "Model vendors a job can use, with the API-key variable each expects.",
     "inputSchema": _schema({})},
    {"name": "translate_pdf",
     "description": "Start translating a PDF into Chinese. Returns the job at once; poll get_job. "
                    "Give exactly one of pdf_url, pdf_base64 or (local clients) pdf_path.",
     "inputSchema": _schema({
         "pdf_url": {"type": "string", "description": "http(s) URL of the PDF."},
         "pdf_base64": {"type": "string", "description": "The PDF, base64-encoded."},
         "pdf_path": {"type": "string", "description": "Path to the PDF on the server (local clients only)."},
         "filename": {"type": "string"},
         "api_key": {"type": "string", "description": "Model API key for this job only; kept in memory, never stored."},
         **_OPTION_SCHEMA,
     })},
    {"name": "get_job",
     "description": "A job's status, current stage, timings and - when finished - verdict, cost and file names.",
     "inputSchema": _schema({"job_id": {"type": "string"},
                             "log_lines": {"type": "integer", "description": "Include this many trailing log lines."}},
                            ("job_id",))},
    {"name": "list_jobs", "description": "Recent jobs, newest first.",
     "inputSchema": _schema({"limit": {"type": "integer"}})},
    {"name": "cancel_job", "description": "Stop a queued or running job.",
     "inputSchema": _schema({"job_id": {"type": "string"}}, ("job_id",))},
    {"name": "get_job_file",
     "description": "Where to get a job's output: 'mono' Chinese PDF, 'dual' bilingual PDF, 'acceptance' report, "
                    "'layout_review', 'content_review' (per-paragraph source, translation, warnings), 'log'. "
                    "Returns a local path, a download URL for remote clients, and the content if inline is true.",
     "inputSchema": _schema({"job_id": {"type": "string"}, "name": {"type": "string", "enum": list(FILE_NAMES)},
                             "inline": {"type": "boolean", "description": "Return the file content (text, or base64 for PDFs)."}},
                            ("job_id", "name"))},
]


class McpServer:
    def __init__(self, manager: JobManager, *, local: bool, public_url: str | None = None,
                 allow_local_paths: bool | None = None):
        self.manager = manager
        self.local = local
        self.public_url = public_url.rstrip("/") if public_url else None
        self.allow_local_paths = local if allow_local_paths is None else allow_local_paths
        self.tools = {tool["name"]: tool for tool in TOOLS}
        self.extra_handlers: dict[str, callable] = {}

    def add_tool(self, definition: dict, handler) -> None:
        """Extend the server (used for the model-call and documentation tools)."""
        self.tools[definition["name"]] = definition
        self.extra_handlers[definition["name"]] = handler

    # --- JSON-RPC -------------------------------------------------------------------

    def handle(self, message: dict) -> dict | None:
        """Answer one JSON-RPC message; None for a notification."""
        method = message.get("method")
        msg_id = message.get("id")
        if msg_id is None:  # notifications (e.g. notifications/initialized) get no reply
            return None
        try:
            if method == "initialize":
                asked = (message.get("params") or {}).get("protocolVersion")
                result = {
                    "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "pdf-translation", "version": __version__},
                    "instructions": INSTRUCTIONS,
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": list(self.tools.values())}
            elif method == "tools/call":
                params = message.get("params") or {}
                result = self.call_tool(params.get("name"), params.get("arguments") or {})
            else:
                return _error(msg_id, -32601, f"method not found: {method}")
        except Exception as error:  # a broken request must not take the server down
            return _error(msg_id, -32603, f"{type(error).__name__}: {error}")
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def call_tool(self, name: str, arguments: dict) -> dict:
        if name not in self.tools:
            return _tool_result({"error": f"unknown tool: {name}"}, error=True)
        try:
            handler = self.extra_handlers.get(name) or getattr(self, f"_tool_{name}")
            return _tool_result(handler(arguments))
        except (JobError, ValidationError, ValueError) as problem:
            # Tool failures are results the model can read and act on, not protocol errors.
            return _tool_result({"error": str(problem)}, error=True)

    # --- tools --------------------------------------------------------------------------

    def _tool_list_providers(self, _: dict) -> dict:
        return {"providers": providers()}

    def _tool_translate_pdf(self, args: dict) -> dict:
        sources = [key for key in ("pdf_url", "pdf_base64", "pdf_path") if args.get(key)]
        if len(sources) != 1:
            raise JobError("give exactly one of pdf_url, pdf_base64, pdf_path")
        if sources[0] == "pdf_url":
            pdf, name = fetch_pdf(args["pdf_url"])
        elif sources[0] == "pdf_base64":
            pdf, name = base64.b64decode(args["pdf_base64"], validate=True), "upload.pdf"
        else:
            if not self.allow_local_paths:
                raise JobError("pdf_path is only available to local clients; send pdf_url or pdf_base64")
            path = Path(args["pdf_path"]).expanduser()
            pdf, name = path.read_bytes(), path.name
        options = JobOptions.model_validate({k: v for k, v in args.items() if k in _OPTION_SCHEMA})
        secrets_env = {}
        if args.get("api_key"):
            variable = options.api_key_variable()
            if not variable:
                raise JobError(f"provider {options.provider} takes no API key; set api_key_env to name one")
            secrets_env[variable] = args["api_key"]
        return self.manager.submit(pdf, args.get("filename") or name, options, secrets_env=secrets_env)

    def _tool_get_job(self, args: dict) -> dict:
        view = self.manager.get(args["job_id"])
        if args.get("log_lines"):
            view["log"] = self.manager.log_tail(args["job_id"], int(args["log_lines"]))
        return view

    def _tool_list_jobs(self, args: dict) -> dict:
        return {"jobs": self.manager.list(int(args.get("limit") or 20))}

    def _tool_cancel_job(self, args: dict) -> dict:
        return self.manager.cancel(args["job_id"])

    def _tool_get_job_file(self, args: dict) -> dict:
        files = self.manager.files(args["job_id"])
        name = args["name"]
        if name not in files:
            raise JobError(f"{name} is not available yet; available: {sorted(files)}")
        path = files[name]
        answer = {"name": name, "bytes": path.stat().st_size}
        if self.local:
            answer["path"] = str(path)
        if self.public_url:
            answer["url"] = f"{self.public_url}/v1/jobs/{args['job_id']}/files/{name}"
        if args.get("inline"):
            if answer["bytes"] > MAX_INLINE_BYTES:
                raise JobError("the file is too large to return inline; download it from the url")
            data = path.read_bytes()
            if path.suffix == ".pdf":
                answer["base64"] = base64.b64encode(data).decode("ascii")
            else:
                answer["text"] = data.decode("utf-8", errors="replace")
        return answer


def _tool_result(payload: dict, *, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
            "structuredContent": payload, "isError": error}


def _error(msg_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def serve_stdio(server: McpServer, stdin=None, stdout=None) -> None:
    """Newline-delimited JSON-RPC over stdio. Nothing else may be written to stdout."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            reply = _error(None, -32700, "parse error")
        else:
            reply = server.handle(message) if isinstance(message, dict) else _error(None, -32600, "invalid request")
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            stdout.flush()
