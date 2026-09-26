"""OpenAPI 3.1 description of the HTTP API, served at ``GET /v1/openapi.json``.

Parameters and their descriptions come from the MCP tool schemas, so the two
ways in cannot describe the same call differently.
"""
from __future__ import annotations

from . import __version__
from .mcp_server import _OPTION_SCHEMA, TOOLS
from .service import FILE_NAMES

_TOOLS = {tool["name"]: tool for tool in TOOLS}
_ERROR = {"type": "object", "properties": {"error": {"type": "string"}}, "required": ["error"]}
_KEY_HEADER = {"name": "X-LLM-API-Key", "in": "header", "required": False, "schema": {"type": "string"},
               "description": "Model API key for this job or call only; kept in memory, never stored."}
_JOB_ID = {"name": "job_id", "in": "path", "required": True, "schema": {"type": "string"}}


def _body_of(tool: str, *drop: str) -> dict:
    schema = _TOOLS[tool]["inputSchema"]
    properties = {k: v for k, v in schema["properties"].items() if k not in drop}
    return {"type": "object", "properties": properties,
            "required": [k for k in schema["required"] if k not in drop]}


def _json(description: str, schema: dict | None = None) -> dict:
    return {"description": description, "content": {"application/json": {"schema": schema or {"type": "object"}}}}


def _errors(*codes: int) -> dict:
    meaning = {400: "Bad request or refused input", 401: "Missing or wrong bearer token",
               402: "The call would exceed the job's budget", 404: "No such job, file or route",
               502: "The model service failed"}
    return {str(code): _json(meaning[code], _ERROR) for code in codes}


def _op(tool: str | None, summary: str, responses: dict, **extra) -> dict:
    operation = {"summary": summary, "responses": responses, **extra}
    if tool:
        operation["operationId"] = tool
        operation["description"] = _TOOLS[tool]["description"]
    return operation


def openapi_spec(server_url: str | None = None) -> dict:
    options = {"type": "object", "properties": _OPTION_SCHEMA, "additionalProperties": False}
    submit_json = {"type": "object", "properties": {
        "pdf_base64": {"type": "string", "description": "The PDF, base64-encoded."},
        "pdf_url": {"type": "string", "description": "http(s) URL of the PDF."},
        "filename": {"type": "string"},
        "options": options}}
    paths = {
        "/health": {"get": _op(None, "Liveness and version; needs no token",
                               {"200": _json("Server is up", {"type": "object", "properties": {
                                   "status": {"type": "string"}, "version": {"type": "string"}}})},
                               security=[])},
        "/v1/providers": {"get": _op("list_providers", "Model vendors a job can use",
                                     {"200": _json("Providers"), **_errors(401)})},
        "/v1/jobs": {
            "get": _op("list_jobs", "Recent jobs, newest first", {"200": _json("Jobs"), **_errors(401)},
                       parameters=[{"name": "limit", "in": "query", "schema": {"type": "integer", "default": 20}}]),
            "post": _op("translate_pdf", "Start translating a PDF; returns the job at once",
                        {"202": _json("The job, queued or starting"), **_errors(400, 401)},
                        parameters=[_KEY_HEADER,
                                    {"name": "X-Job-Options", "in": "header", "required": False,
                                     "description": "Job options as JSON, with an application/pdf body.",
                                     "schema": {"type": "string"}},
                                    {"name": "filename", "in": "query", "required": False,
                                     "description": "Name of the uploaded file, with an application/pdf body.",
                                     "schema": {"type": "string"}}],
                        requestBody={"required": True, "content": {
                            "application/pdf": {"schema": {"type": "string", "format": "binary"}},
                            "application/json": {"schema": submit_json}}}),
        },
        "/v1/jobs/{job_id}": {"get": _op("get_job", "Status, stage, timings and, when finished, verdict and files",
                                         {"200": _json("The job"), **_errors(401, 404)}, parameters=[_JOB_ID])},
        "/v1/jobs/{job_id}/log": {"get": _op(None, "Trailing lines of the job's log",
                                             {"200": _json("Log lines"), **_errors(401, 404)},
                                             parameters=[_JOB_ID, {"name": "lines", "in": "query",
                                                                   "schema": {"type": "integer", "default": 40}}])},
        "/v1/jobs/{job_id}/files/{name}": {"get": _op(
            "get_job_file", "Download one of the job's files",
            {"200": {"description": "The file: PDF, JSON, JSON lines or text",
                     "content": {"application/pdf": {}, "application/json": {}, "application/x-ndjson": {},
                                 "text/plain": {}}}, **_errors(401, 404)},
            parameters=[_JOB_ID, {"name": "name", "in": "path", "required": True,
                                  "schema": {"type": "string", "enum": list(FILE_NAMES)}}])},
        "/v1/jobs/{job_id}/cancel": {"post": _op("cancel_job", "Stop a queued or running job",
                                                 {"200": _json("The job"), **_errors(400, 401)},
                                                 parameters=[_JOB_ID])},
        "/v1/jobs/{job_id}/resume": {"post": _op("resume_job", "Run a failed or cancelled job again",
                                                 {"202": _json("The job"), **_errors(400, 401)},
                                                 parameters=[_JOB_ID, _KEY_HEADER])},
        "/v1/jobs/{job_id}/units": {"get": _op(
            "get_units", "A finished job's units, filtered", {"200": _json("Units"), **_errors(401, 404)},
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
            {"200": _json("Answer, cost and budget state"), **_errors(400, 401, 402, 502)},
            parameters=[_JOB_ID, _KEY_HEADER],
            requestBody={"required": True, "content": {"application/json": {
                "schema": _body_of("ask_model", "job_id", "api_key")}}})},
        "/v1/jobs/{job_id}/revise": {"post": _op(
            "apply_fixes", "Write fixes, render the changed pages again and repeat acceptance",
            {"202": _json("The job, running again"), **_errors(400, 401)},
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
    spec = {
        "openapi": "3.1.0",
        "info": {"title": "PDF translation service", "version": __version__,
                 "description": "Translate English PDFs into Chinese as jobs: submit, poll, fetch. "
                                "See GET /v1/guide for the whole workflow and how to read the verdict."},
        "components": {"securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}}},
        "security": [{"bearer": []}],
        "paths": paths,
    }
    if server_url:
        spec["servers"] = [{"url": server_url}]
    return spec
