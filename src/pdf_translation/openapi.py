"""OpenAPI 3.1 description of the HTTP API, served at ``GET /v1/openapi.json``.

Parameters and their descriptions come from the MCP tool schemas, so the two
ways in cannot describe the same call differently.
"""
from __future__ import annotations

from . import __version__
from .mcp_server import _MODEL_SCHEMA, _REQUEST_SCHEMA, TOOLS
from .service import FILE_NAMES

_TOOLS = {tool["name"]: tool for tool in TOOLS}
#: MCP tools that are another form of an HTTP operation.
ALIASES = {"translate_pdf": "translate_document"}
_ERROR = {"type": "object", "properties": {"error": {"type": "object", "properties": {
    "code": {"type": "string", "description": "Stable machine-readable reason, e.g. model_required, key_missing, "
                                              "unsupported_format, budget_too_low, job_not_found."},
    "message": {"type": "string"}, "hint": {"type": "string", "description": "What to do about it."}},
    "required": ["code", "message"]}}, "required": ["error"]}
_KEY_HEADERS = [
    {"name": "X-LLM-API-Key", "in": "header", "required": False, "schema": {"type": "string"},
     "description": "Fallback: translation model key for this job only, never stored. Prefer the server's environment."},
    {"name": "X-Agent-API-Key", "in": "header", "required": False, "schema": {"type": "string"},
     "description": "Fallback: AI OS model key for this job only, never stored."},
    {"name": "X-Jev-API-Key", "in": "header", "required": False, "schema": {"type": "string"},
     "description": "Fallback: TypeSafe Jev key for this job only, never stored."},
]
_JOB_ID = {"name": "job_id", "in": "path", "required": True, "schema": {"type": "string"}}
_VERSIONS = ["usable", "optimized", "degraded", "revised"]


def _body_of(tool: str, *drop: str) -> dict:
    schema = _TOOLS[tool]["inputSchema"]
    properties = {k: v for k, v in schema["properties"].items() if k not in drop}
    return {"type": "object", "properties": properties,
            "required": [k for k in schema["required"] if k not in drop]}


def _json(description: str, schema: dict | None = None) -> dict:
    return {"description": description, "content": {"application/json": {"schema": schema or {"type": "object"}}}}


def _errors(*codes: int) -> dict:
    meaning = {400: "Bad request", 401: "Missing or wrong bearer token",
               402: "The call would exceed the job's budget", 404: "No such job, version, file or route",
               409: "The job is not in a state that allows this", 413: "The document is too large",
               415: "Unsupported document format", 422: "The request cannot be run as given (see error.code)",
               502: "The model service failed"}
    return {str(code): _json(meaning[code], {"$ref": "#/components/schemas/Error"}) for code in codes}


def _op(tool: str | None, summary: str, responses: dict, **extra) -> dict:
    operation = {"summary": summary, "responses": responses, **extra}
    if tool:
        operation["operationId"] = tool
        operation["description"] = _TOOLS[tool]["description"]
    return operation


def _file_response() -> dict:
    return {"description": "The file", "content": {kind: {} for kind in (
        "application/pdf", "application/json", "application/x-ndjson", "text/plain", "text/html", "text/markdown",
        "application/zip", "application/epub+zip",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document")}}


def openapi_spec(server_url: str | None = None) -> dict:
    job_request = {"type": "object", "properties": _REQUEST_SCHEMA, "additionalProperties": False}
    submit_json = {"type": "object", "properties": {
        "source": {"type": "object", "properties": {
            "base64": {"type": "string", "description": "The document, base64-encoded."},
            "url": {"type": "string", "description": "http(s) URL of the document."},
            "filename": {"type": "string", "description": "Its file name; the extension helps tell the format."},
            "main_file": {"type": "string", "description": "LaTeX projects: the main .tex file."}}},
        **_REQUEST_SCHEMA}}
    paths = {
        "/health": {"get": _op(None, "Liveness, version, and whether the AI OS and Jev are available; needs no token",
                               {"200": _json("Server is up", {"type": "object", "properties": {
                                   "status": {"type": "string"}, "version": {"type": "string"},
                                   "aios": {"type": "object"}, "jev": {"type": "object"}}})},
                               security=[])},
        "/v1/providers": {"get": _op("list_providers", "Model vendors a job can use",
                                     {"200": _json("Providers"), **_errors(401)})},
        "/v1/profile": {
            "get": _op("get_profile", "This caller's default translation and agent models",
                       {"200": _json("The profile"), **_errors(401)},
                       parameters=[{"name": "X-Client-Id", "in": "header", "required": False, "schema": {"type": "string"}}]),
            "put": _op("set_profile", "Set this caller's default models",
                       {"200": _json("The profile"), **_errors(401, 422)},
                       requestBody={"required": True, "content": {"application/json": {
                           "schema": _body_of("set_profile")}}}),
        },
        "/v1/jobs": {
            "get": _op("list_jobs", "Recent jobs, newest first", {"200": _json("Jobs"), **_errors(401)},
                       parameters=[{"name": "limit", "in": "query", "schema": {"type": "integer", "default": 20}}]),
            "post": _op("translate_document", "Start translating a document; returns the job at once",
                        {"202": _json("The job, queued or starting", {"$ref": "#/components/schemas/Job"}),
                         **_errors(400, 401, 413, 415, 422)},
                        parameters=[*_KEY_HEADERS,
                                    {"name": "X-Callback-Secret", "in": "header", "required": False,
                                     "schema": {"type": "string"},
                                     "description": "HMAC-SHA256 key for callback signatures (else the server's PDFTRANS_CALLBACK_SECRET)."},
                                    {"name": "Idempotency-Key", "in": "header", "required": False,
                                     "schema": {"type": "string"},
                                     "description": "A retried submission with the same key (same caller, 24 h) returns the same job."},
                                    {"name": "X-Client-Id", "in": "header", "required": False, "schema": {"type": "string"}},
                                    {"name": "X-Job-Request", "in": "header", "required": False,
                                     "description": "The JobRequest as JSON, with the raw document as the body.",
                                     "schema": {"type": "string"}},
                                    {"name": "X-Job-Options", "in": "header", "required": False,
                                     "description": "v0.10 flat options as JSON, with an application/pdf body.",
                                     "schema": {"type": "string"}},
                                    {"name": "filename", "in": "query", "required": False,
                                     "description": "Name of the uploaded file, with a raw body.",
                                     "schema": {"type": "string"}}],
                        requestBody={"required": True, "content": {
                            "application/json": {"schema": submit_json},
                            "application/octet-stream": {"schema": {"type": "string", "format": "binary"}},
                            "application/pdf": {"schema": {"type": "string", "format": "binary"}}}}),
        },
        "/v1/jobs/{job_id}": {"get": _op("get_job", "Status, delivery, versions, problems, cost and for_agent summary",
                                         {"200": _json("The job", {"$ref": "#/components/schemas/Job"}),
                                          **_errors(401, 404)}, parameters=[_JOB_ID])},
        "/v1/jobs/{job_id}/events": {"get": _op(
            "wait_job", "Numbered events after `after`; with `wait`, held until the first new one (long poll)",
            {"200": _json("Events", {"type": "object", "properties": {
                "events": {"type": "array", "items": {"$ref": "#/components/schemas/Event"}},
                "last_seq": {"type": "integer"}}}), **_errors(401, 404)},
            parameters=[_JOB_ID, {"name": "after", "in": "query", "schema": {"type": "integer", "default": 0}},
                        {"name": "wait", "in": "query", "schema": {"type": "number", "default": 0, "maximum": 60}}])},
        "/v1/jobs/{job_id}/log": {"get": _op(None, "Trailing lines of the job's log",
                                             {"200": _json("Log lines"), **_errors(401, 404)},
                                             parameters=[_JOB_ID, {"name": "lines", "in": "query",
                                                                   "schema": {"type": "integer", "default": 40}}])},
        "/v1/jobs/{job_id}/files/{name}": {"get": _op(
            "get_job_file", "Download one file of the recommended version",
            {"200": _file_response(), **_errors(401, 404)},
            parameters=[_JOB_ID, {"name": "name", "in": "path", "required": True,
                                  "schema": {"type": "string", "enum": list(FILE_NAMES)}}])},
        "/v1/jobs/{job_id}/files/{version}/{name}": {"get": _op(
            None, "Download one file of a named version", {"200": _file_response(), **_errors(401, 404)},
            parameters=[_JOB_ID, {"name": "version", "in": "path", "required": True,
                                  "schema": {"type": "string", "enum": _VERSIONS}},
                        {"name": "name", "in": "path", "required": True,
                         "schema": {"type": "string", "enum": list(FILE_NAMES)}}])},
        "/v1/jobs/{job_id}/cancel": {"post": _op("cancel_job", "Stop a queued or running job",
                                                 {"200": _json("The job"), **_errors(401, 409)},
                                                 parameters=[_JOB_ID])},
        "/v1/jobs/{job_id}/resume": {"post": _op("resume_job", "Run a failed or cancelled job again",
                                                 {"202": _json("The job"), **_errors(401, 409)},
                                                 parameters=[_JOB_ID, _KEY_HEADERS[0]])},
        "/v1/jobs/{job_id}/units": {"get": _op(
            "get_units", "A finished job's units, filtered", {"200": _json("Units"), **_errors(401, 404, 409)},
            parameters=[_JOB_ID,
                        {"name": "page", "in": "query", "schema": {"type": "array", "items": {"type": "integer"}},
                         "explode": True, "description": "One-based page; repeat for several."},
                        {"name": "unit_id", "in": "query", "schema": {"type": "array", "items": {"type": "string"}},
                         "explode": True},
                        {"name": "with_issues", "in": "query", "schema": {"type": "boolean", "default": False}},
                        {"name": "state", "in": "query", "schema": {"type": "array", "items": {"type": "string"}},
                         "explode": True},
                        {"name": "limit", "in": "query", "schema": {"type": "integer", "default": 200}}])},
        "/v1/jobs/{job_id}/assist": {"post": _op(
            "ask_model", "The job's model does one task and returns candidate fixes",
            {"200": _json("Answer, cost and budget state"), **_errors(400, 401, 402, 409, 502)},
            parameters=[_JOB_ID, _KEY_HEADERS[0]],
            requestBody={"required": True, "content": {"application/json": {
                "schema": _body_of("ask_model", "job_id", "api_key")}}})},
        "/v1/jobs/{job_id}/revise": {"post": _op(
            "apply_fixes", "Write fixes, write the outputs again, repeat acceptance; published as 'revised'",
            {"202": _json("The job, running again"), **_errors(400, 401, 409, 422)},
            parameters=[_JOB_ID],
            requestBody={"required": True, "content": {"application/json": {
                "schema": _body_of("apply_fixes", "job_id")}}})},
        "/v1/guide": {"get": _op(None, "The integration guide for tools and agents",
                                 {"200": {"description": "Markdown", "content": {"text/markdown": {}}},
                                  **_errors(401)})},
        "/v1/openapi.json": {"get": _op(None, "This description", {"200": _json("OpenAPI 3.1"), **_errors(401)})},
        "/mcp": {"post": _op(None, "MCP over streamable HTTP (JSON-RPC 2.0, JSON responses)",
                             {"200": _json("JSON-RPC reply"), "202": {"description": "Notification accepted"},
                              **_errors(401)},
                             requestBody={"required": True, "content": {"application/json": {
                                 "schema": {"type": "object"}}}})},
    }
    schemas = {
        "Error": _ERROR,
        "ModelChoice": _MODEL_SCHEMA,
        "JobRequest": job_request,
        "Event": {"type": "object", "properties": {
            "seq": {"type": "integer"}, "at": {"type": "string"}, "job_id": {"type": "string"},
            "type": {"type": "string", "enum": ["job.accepted", "stage.changed", "repair.round", "delivery.usable",
                                                "delivery.optimized", "delivery.degraded", "delivery.revised",
                                                "job.failed", "job.finished", "job.cancelled", "callback.abandoned"]},
            "data": {"type": "object"}}},
        "Job": {"type": "object", "properties": {
            "job_id": {"type": "string"},
            "status": {"type": "string", "enum": ["queued", "starting", "running", "succeeded", "failed", "cancelled", "estimated"]},
            "phase": {"type": "string", "description": "translate | repair | optimize | revise | done"},
            "delivery": {"type": "string", "enum": ["none", *_VERSIONS],
                         "description": "What the caller can take now. degraded: files exist but did NOT pass acceptance."},
            "recommended": {"type": "string", "enum": _VERSIONS},
            "deliveries": {"type": "array", "items": {"type": "object"}},
            "problems": {"type": "array", "items": {"type": "object"}},
            "cost_usd": {"type": "object"},
            "events_after": {"type": "integer"},
            "error": {"type": "object"},
            "for_agent": {"type": "object", "description": "summary / summary_zh / next_step for AI agent hosts"}}},
    }
    spec = {
        "openapi": "3.1.0",
        "info": {"title": "Document translation service", "version": __version__,
                 "description": "Translate English documents (PDF, LaTeX, HTML, Markdown, DOCX, EPUB) into Chinese as "
                                "jobs: submit, wait (long poll or callback), fetch. Every job ends with files and an "
                                "honest verdict. See GET /v1/guide for the whole contract."},
        "components": {"securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}}, "schemas": schemas},
        "security": [{"bearer": []}],
        "paths": paths,
        "x-mcp-aliases": ALIASES,
        "x-callbacks": {"description": "With callback_url, every event is POSTed as JSON with "
                                       "X-Signature: sha256=<HMAC-SHA256 of the body>, X-Event-Seq and X-Job-Id; "
                                       "non-2xx answers are retried with backoff for up to 24 hours."},
    }
    if server_url:
        spec["servers"] = [{"url": server_url}]
    return spec
