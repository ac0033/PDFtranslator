"""Model Context Protocol server: document translation as tools an AI agent can call.

The protocol is JSON-RPC 2.0. :class:`McpServer.handle` answers one message and
is shared by both transports:

* stdio (``pdftrans mcp``): newline-delimited messages on stdin/stdout, for a
  client on the same machine (Claude Desktop, Claude Code, Cursor, ...);
* streamable HTTP (``POST /mcp`` on ``pdftrans serve``): for clients on other
  hosts, behind the server's bearer token.

Translation takes minutes to hours, so the tools follow a job pattern: submit
(``translate_document``), wait server-side (``wait_job``, which holds the call
until something happens so the agent does not poll), fetch
(``get_job_file``). Every job ends with files and an honest verdict: the
service's own AI OS repairs a version that fails acceptance, publishes the
usable version, then the optimised one. An agent may still work on a finished
job itself: read units (``get_units``), have the job's model do a task
(``ask_model``) and write fixes back (``apply_fixes``). Reading a document from
a path on the server is allowed only to a local client (or when the operator
enables it): a remote caller who could name any server path could have that
file's contents translated and handed back.
"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from . import __version__
from .assist import TASKS
from .jobspec import JobRequest, validation_error
from .service import FILE_NAMES, ROOT, JobError, JobManager, JobOptions, fetch_source, providers

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
MAX_INLINE_BYTES = 25 * 1024 * 1024
INSTRUCTIONS = (
    "Translate English documents - PDF, LaTeX (a .tex file or a zipped project), HTML, Markdown, DOCX, EPUB - into "
    "Chinese, keeping layout, formulas and markup. Call translate_document; it returns a job at once. The first call "
    "must name two models, translation and agent (the service's AI OS); later calls may omit them to reuse the "
    "first ones. Then call wait_job with the job's events_after until delivery is usable, optimized or degraded, or "
    "status is failed. usable/optimized versions are deliverable; degraded means files exist but the translation did "
    "NOT pass acceptance - tell the user so. Fetch files with get_job_file ('mono' Chinese, 'dual' bilingual, "
    "'source' translated LaTeX project). Each job result has for_agent.summary: use it to report honestly. "
    "Set estimate_only first to learn the price without spending. Read the resource pdftrans://docs/agent_guide "
    "for the whole contract."
)
GUIDE = ROOT / "docs" / "agent_guide.md"
GUIDE_URI = "pdftrans://docs/agent_guide"

_MODEL_SCHEMA = {
    "type": "object",
    "properties": {
        "provider": {"type": "string", "description": "Vendor preset (see list_providers), e.g. deepseek, openai, anthropic."},
        "model": {"type": "string", "description": "Model name at that vendor."},
        "base_url": {"type": "string", "description": "An OpenAI-compatible endpoint, for private or proxy deployments."},
        "api_key_env": {"type": "string", "description": "Server environment variable holding the key."},
        "input_usd_per_million": {"type": "number"},
        "output_usd_per_million": {"type": "number"},
    },
    "required": ["provider", "model"],
}
_REQUEST_SCHEMA = {
    "translation": {**_MODEL_SCHEMA, "description": "The translation model. Required on this caller's first call; then remembered."},
    "agent": {**_MODEL_SCHEMA, "description": "The AI OS model (the pi agent that repairs and optimises). Required on the first call; then remembered."},
    "set_default": {"type": "boolean", "description": "Make the models named here this caller's new defaults."},
    "client_id": {"type": "string", "description": "Who is calling, for remembering default models."},
    "deliver": {"type": "object", "properties": {"optimized": {"type": "boolean"}, "bilingual": {"type": "boolean"}},
                "description": "optimized (default true): also build the optimised version. bilingual (default true)."},
    "budget_usd": {"type": "number", "description": "Hard spend ceiling for the whole job (translation, repair, optimisation). Default 2.0."},
    "allow_network": {"type": "boolean", "description": "Let the service fetch an arXiv paper's LaTeX source. Default true."},
    "callback_url": {"type": "string", "description": "POST every event here, signed with HMAC-SHA256 (X-Signature)."},
    "max_output_tokens": {"type": "integer", "description": "Per-request output ceiling for the translation model. Default 2000."},
    "main_file": {"type": "string", "description": "LaTeX projects: the main .tex file, when it cannot be found automatically."},
    "style_note": {"type": "string", "description": "Per-document instruction: subject, term choices (e.g. 'policy 译为 策略')."},
    "layout_review": {"type": "string", "enum": ["all", "flagged", "off"], "description": "PDF: page-by-page model review. Default all."},
    "vision": {"type": "boolean", "description": "PDF: send page images to the reviewing model. Default true."},
    "estimate_only": {"type": "boolean", "description": "Only price; no model call, no charge."},
    "workers": {"type": "integer", "description": "PDF: parallel capture/render processes, 1-8. Default 2."},
    "translation_concurrency": {"type": "integer", "description": "Model requests in flight, 1-32. Default 8."},
}
#: The v0.10 flat options, still accepted by translate_pdf.
_OPTION_SCHEMA = {
    "provider": {"type": "string"}, "model_id": {"type": "string"}, "base_url": {"type": "string"},
    "api_key_env": {"type": "string"}, "budget_usd": {"type": "number"}, "input_usd_per_million": {"type": "number"},
    "output_usd_per_million": {"type": "number"}, "style_note": {"type": "string"},
    "layout_review": {"type": "string", "enum": ["all", "flagged", "off"]}, "vision": {"type": "boolean"},
    "estimate_only": {"type": "boolean"}, "workers": {"type": "integer"}, "translation_concurrency": {"type": "integer"},
    "agent": _MODEL_SCHEMA,
}
_KEYS = {
    "api_key": {"type": "string", "description": "Fallback: translation model key for this job only; never stored. Prefer the server's environment."},
    "agent_api_key": {"type": "string", "description": "Fallback: AI OS model key for this job only; never stored."},
    "jev_api_key": {"type": "string", "description": "Fallback: TypeSafe Jev key for this job only; never stored."},
}


def _schema(properties: dict, required: tuple[str, ...] = ()) -> dict:
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


_SOURCE = {
    "url": {"type": "string", "description": "http(s) URL of the document."},
    "base64": {"type": "string", "description": "The document, base64-encoded."},
    "path": {"type": "string", "description": "Path to the document on the server (local clients only)."},
    "filename": {"type": "string", "description": "The document's file name; its extension helps tell the format."},
}

TOOLS = [
    {"name": "list_providers",
     "description": "Model vendors a job can use, with the API-key variable each expects.",
     "inputSchema": _schema({})},
    {"name": "translate_document",
     "description": "Start translating a document (PDF, LaTeX .tex/.zip/.tar.gz, HTML, Markdown, DOCX, EPUB) into "
                    "Chinese. Returns the job at once; then call wait_job. Give exactly one of url, base64 or (local "
                    "clients) path. The first call must name translation and agent models.",
     "inputSchema": _schema({**_SOURCE, **_REQUEST_SCHEMA, **_KEYS})},
    {"name": "translate_pdf",
     "description": "v0.10 form of translate_document for PDFs (pdf_url, pdf_base64 or pdf_path, flat options).",
     "inputSchema": _schema({"pdf_url": {"type": "string"}, "pdf_base64": {"type": "string"},
                             "pdf_path": {"type": "string"}, "filename": {"type": "string"}, **_OPTION_SCHEMA, **_KEYS})},
    {"name": "wait_job",
     "description": "Wait on the server (up to timeout_s, at most 50) for a job's next events, then return them with "
                    "the job's current state. Pass the last events_after you saw. Use this instead of polling.",
     "inputSchema": _schema({"job_id": {"type": "string"},
                             "events_after": {"type": "integer", "description": "The last event number you have seen (0 at first)."},
                             "timeout_s": {"type": "integer"}}, ("job_id",))},
    {"name": "get_job",
     "description": "A job's status, delivery, versions, problems, cost, AI OS record and for_agent summary.",
     "inputSchema": _schema({"job_id": {"type": "string"},
                             "log_lines": {"type": "integer", "description": "Include this many trailing log lines."}},
                            ("job_id",))},
    {"name": "list_jobs", "description": "Recent jobs, newest first.",
     "inputSchema": _schema({"limit": {"type": "integer"}})},
    {"name": "cancel_job", "description": "Stop a queued or running job.",
     "inputSchema": _schema({"job_id": {"type": "string"}}, ("job_id",))},
    {"name": "resume_job",
     "description": "Run a failed or cancelled job again from where it stopped (e.g. after the model account was "
                    "topped up). Work already done and paid for is reused, not paid again.",
     "inputSchema": _schema({"job_id": {"type": "string"}, "api_key": _KEYS["api_key"]}, ("job_id",))},
    {"name": "get_profile", "description": "This caller's default translation and agent models.",
     "inputSchema": _schema({"client_id": {"type": "string"}})},
    {"name": "set_profile", "description": "Change this caller's default translation and/or agent model.",
     "inputSchema": _schema({"client_id": {"type": "string"}, "translation": _MODEL_SCHEMA, "agent": _MODEL_SCHEMA})},
    {"name": "get_units",
     "description": "A finished job's units (paragraph-sized passages): source, translation, state and the automatic "
                    "checks' warnings. Filter by pages, unit_ids, with_issues (only flagged or untranslated units) "
                    "or states (accepted, needs_review, exhausted).",
     "inputSchema": _schema({"job_id": {"type": "string"},
                             "pages": {"type": "array", "items": {"type": "integer"}, "description": "One-based pages (PDF)."},
                             "unit_ids": {"type": "array", "items": {"type": "string"}},
                             "with_issues": {"type": "boolean"},
                             "states": {"type": "array", "items": {"type": "string"}},
                             "limit": {"type": "integer", "description": "Most units returned. Default 200."}},
                            ("job_id",))},
    {"name": "ask_model",
     "description": "Have the job's own translation model do one task on a finished job; it is priced first, counts "
                    "against the job's budget and is logged. Nothing is written: the answer holds candidate fixes "
                    "(each with the quality gate's verdict; 'fix' is present only when it may be applied) to pass "
                    "to apply_fixes. Tasks: " + "; ".join(f"{name}: {text}" for name, text in TASKS.items()),
     "inputSchema": _schema({
         "job_id": {"type": "string"},
         "task": {"type": "string", "enum": list(TASKS)},
         "unit_ids": {"type": "array", "items": {"type": "string"},
                      "description": "retranslate, shorten, review_units; optional context for ask. At most 40."},
         "pages": {"type": "array", "items": {"type": "integer"},
                   "description": "review_pages; optional for ask. One-based, at most 5."},
         "instruction": {"type": "string", "description": "What to do differently, e.g. 'policy 译为 策略'."},
         "prompt": {"type": "string", "description": "ask: the question."},
         "system": {"type": "string", "description": "ask: replaces the default system prompt."},
         "json_schema": {"type": "object", "description": "ask: the reply must be a JSON object of this schema."},
         "images": {"type": "boolean", "description": "ask: attach the original and rendered image of each page."},
         "vision": {"type": "boolean", "description": "review_pages: send page images. Default true."},
         "shorten_to": {"type": "number", "description": "shorten: target share of the current length. Default 0.8."},
         "max_output_tokens": {"type": "integer"},
         "budget_usd": {"type": "number", "description": "Raise the job's spend ceiling for this and later calls."},
         "api_key": _KEYS["api_key"],
     }, ("job_id", "task"))},
    {"name": "apply_fixes",
     "description": "Write corrected translations into a finished job, then write the outputs again and repeat "
                    "acceptance; the result is published as the 'revised' version (recommended when not worse). "
                    "Each fix is {unit_id, old_translated_text (the translation it replaces, null for an untranslated "
                    "unit), new_translated_text, reason}. The whole batch is checked first; a fix against an outdated "
                    "text, or one the quality gate blocks, refuses the batch. No model is called.",
     "inputSchema": _schema({
         "job_id": {"type": "string"},
         "fixes": {"type": "array", "items": {"type": "object"}},
         "editor": {"type": "string", "description": "Who made the fixes, e.g. 'agent:claude-opus-5-5'."},
     }, ("job_id", "fixes", "editor"))},
    {"name": "get_job_file",
     "description": "Where to get a job's output: 'mono' (Chinese), 'dual' (bilingual), 'source' (translated LaTeX "
                    "project), 'acceptance' report, 'layout_review', 'content_review', 'log'. version picks usable, "
                    "optimized, degraded or revised (default: the recommended one). Returns a local path, a download "
                    "URL for remote clients, the SHA-256, and the content if inline is true.",
     "inputSchema": _schema({"job_id": {"type": "string"}, "name": {"type": "string", "enum": list(FILE_NAMES)},
                             "version": {"type": "string", "enum": ["usable", "optimized", "degraded", "revised"]},
                             "inline": {"type": "boolean", "description": "Return the file content (text, or base64 for binary files)."}},
                            ("job_id", "name"))},
]


class McpServer:
    def __init__(self, manager: JobManager, *, local: bool, public_url: str | None = None,
                 allow_local_paths: bool | None = None, caller: str = "local"):
        self.manager = manager
        #: Whose default models a job without models uses (see pdf_translation.jobspec).
        self.caller = caller
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
                    "capabilities": {"tools": {"listChanged": False}, "resources": {"listChanged": False}},
                    "serverInfo": {"name": "pdf-translation", "version": __version__},
                    "instructions": INSTRUCTIONS,
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": list(self.tools.values())}
            elif method == "resources/list":
                result = {"resources": [{"uri": GUIDE_URI, "name": "agent_guide", "mimeType": "text/markdown",
                                         "description": "The whole pipeline, every artefact, and when to delegate to a model."}]}
            elif method == "resources/read":
                uri = (message.get("params") or {}).get("uri")
                if uri != GUIDE_URI:
                    return _error(msg_id, -32002, f"resource not found: {uri}")
                result = {"contents": [{"uri": GUIDE_URI, "mimeType": "text/markdown",
                                        "text": GUIDE.read_text(encoding="utf-8")}]}
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
        except JobError as problem:
            # Tool failures are results the model can read and act on, not protocol errors.
            return _tool_result(problem.payload(), error=True)
        except ValidationError as problem:
            return _tool_result(validation_error(problem).payload(), error=True)
        except ValueError as problem:
            return _tool_result({"error": {"code": "bad_request", "message": str(problem)}}, error=True)
        except Exception as problem:  # the model service failing, the budget reached, ...
            return _tool_result({"error": {"code": "internal_error", "message": f"{type(problem).__name__}: {problem}"}},
                                error=True)

    # --- tools --------------------------------------------------------------------------

    def _caller(self, args: dict) -> str:
        from .jobspec import caller_key

        return caller_key(args.get("client_id"), None) if args.get("client_id") else self.caller

    def _read_source(self, url: str | None, data64: str | None, path: str | None, filename: str | None) -> tuple[bytes, str]:
        given = [value for value in (url, data64, path) if value]
        if len(given) != 1:
            raise JobError("give exactly one of url, base64, path", code="invalid_request")
        if url:
            data, name = fetch_source(url)
        elif data64:
            data, name = base64.b64decode(data64, validate=True), filename or "upload"
        else:
            if not self.allow_local_paths:
                raise JobError("path is only available to local clients; send url or base64", code="forbidden", status=403)
            source = Path(path).expanduser()
            if not source.is_file():
                raise JobError(f"no such file: {source}", code="source_unreadable")
            data, name = source.read_bytes(), source.name
        return data, filename or name

    def _keys(self, args: dict) -> dict:
        return {role: args[key] for role, key in (("translation", "api_key"), ("agent", "agent_api_key"),
                                                  ("jev", "jev_api_key")) if args.get(key)}

    def _tool_list_providers(self, _: dict) -> dict:
        return {"providers": providers()}

    def _tool_translate_document(self, args: dict) -> dict:
        data, name = self._read_source(args.get("url"), args.get("base64"), args.get("path"), args.get("filename"))
        try:
            request = JobRequest.model_validate({k: v for k, v in args.items() if k in _REQUEST_SCHEMA})
        except ValidationError as problem:
            raise validation_error(problem) from problem
        return self.manager.submit(data, name, request, caller=self._caller(args), api_keys=self._keys(args))

    def _tool_translate_pdf(self, args: dict) -> dict:
        data, name = self._read_source(args.get("pdf_url"), args.get("pdf_base64"), args.get("pdf_path"), args.get("filename"))
        try:
            request = JobRequest.from_legacy({k: v for k, v in args.items() if k in _OPTION_SCHEMA})
        except ValidationError as problem:
            raise validation_error(problem) from problem
        return self.manager.submit(data, name, request, caller=self._caller(args), api_keys=self._keys(args))

    def _tool_wait_job(self, args: dict) -> dict:
        timeout = max(1, min(int(args.get("timeout_s") or 50), 50))
        return self.manager.wait(args["job_id"], int(args.get("events_after") or 0), timeout)

    def _tool_get_job(self, args: dict) -> dict:
        view = self.manager.get(args["job_id"])
        if args.get("log_lines"):
            view["log"] = self.manager.log_tail(args["job_id"], int(args["log_lines"]))
        return view

    def _tool_list_jobs(self, args: dict) -> dict:
        return {"jobs": self.manager.list(int(args.get("limit") or 20))}

    def _tool_cancel_job(self, args: dict) -> dict:
        return self.manager.cancel(args["job_id"])

    def _tool_resume_job(self, args: dict) -> dict:
        secrets_env = {}
        if args.get("api_key"):
            options = JobOptions.model_validate(self.manager._meta(args["job_id"])["options"])
            variable = options.api_key_variable()
            if variable:
                secrets_env[variable] = args["api_key"]
        return self.manager.resume(args["job_id"], secrets_env=secrets_env)

    def _tool_get_profile(self, args: dict) -> dict:
        return self.manager.profile(self._caller(args))

    def _tool_set_profile(self, args: dict) -> dict:
        return self.manager.set_profile(self._caller(args), args.get("translation"), args.get("agent"))

    def _tool_get_units(self, args: dict) -> dict:
        return self.manager.units(args["job_id"], pages=args.get("pages"), unit_ids=args.get("unit_ids"),
                                  with_issues=bool(args.get("with_issues")), states=args.get("states"),
                                  limit=int(args.get("limit") or 200))

    def _tool_ask_model(self, args: dict) -> dict:
        arguments = {k: v for k, v in args.items() if k not in {"job_id", "task", "api_key", "budget_usd"} and v is not None}
        return self.manager.assist(args["job_id"], args["task"], api_key=args.get("api_key"),
                                   budget_usd=args.get("budget_usd"), **arguments)

    def _tool_apply_fixes(self, args: dict) -> dict:
        return self.manager.revise(args["job_id"], args["fixes"], args["editor"])

    def _tool_get_job_file(self, args: dict) -> dict:
        from .hashing import sha256_file

        files = self.manager.files(args["job_id"], args.get("version"))
        name = args["name"]
        if name not in files:
            raise JobError(f"{name} is not available yet; available: {sorted(files)}", code="file_not_ready", status=404)
        path = files[name]
        answer = {"name": name, "file": path.name, "bytes": path.stat().st_size, "sha256": sha256_file(path)}
        if self.local:
            answer["path"] = str(path)
        if self.public_url:
            version = f"{args['version']}/" if args.get("version") else ""
            answer["url"] = f"{self.public_url}/v1/jobs/{args['job_id']}/files/{version}{name}"
        if args.get("inline"):
            if answer["bytes"] > MAX_INLINE_BYTES:
                raise JobError("the file is too large to return inline; download it from the url", code="too_large")
            data = path.read_bytes()
            if path.suffix in {".json", ".jsonl", ".log", ".md", ".html", ".tex", ".txt"}:
                answer["text"] = data.decode("utf-8", errors="replace")
            else:
                answer["base64"] = base64.b64encode(data).decode("ascii")
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
