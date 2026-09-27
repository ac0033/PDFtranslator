"""Translation jobs: the one interface every way in (CLI, HTTP, MCP, skills) calls.

A job is a directory under the jobs root::

    <job_id>/source.<ext>        the uploaded document (PDF, LaTeX, HTML, Markdown, DOCX, EPUB)
    <job_id>/config.toml         run config generated from the job's translation model and options
    <job_id>/style.txt           optional per-document translation instruction
    <job_id>/job.json            request, resolved models, caller, runner pid, cancellation
    <job_id>/state.json          status, phase, delivery, AI OS record, cost (written by the runner)
    <job_id>/events.jsonl        numbered events (for long polling and callbacks)
    <job_id>/deliveries/<v>/     published versions: usable, optimized, degraded, revised
    <job_id>/work/               the workflow's working files
    <job_id>/aios/               the AI OS's log and the pi agent's transcript
    <job_id>/runner.log          the runner's console output

The runner is ``scripts/run_job.py`` in its own process, so a job keeps
running if the caller disconnects and its state is on disk if the server
restarts. It always ends in one of three deliveries - usable, optimized or
degraded (files plus an honest verdict) - unless the workflow itself fails,
which is reported with an error code. Model keys come from the server's
environment; a key sent with a request goes only into that job's runner
process and is never written to disk.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, model_validator

from . import jobstate
from .formats import FORMATS, FormatError, detect_format
from .jobspec import (
    KNOWN_PRICES,
    JobError,
    JobRequest,
    ModelChoice,
    ProfileStore,
    agent_key_variable,
    caller_key,
    check_translation_model,
    require_keys,
    validation_error,
)
from .llm import PRESETS, PRESET_ALIASES, resolve_endpoint

__all__ = ["JobError", "JobManager", "JobOptions", "JobRequest", "ModelChoice", "TERMINAL", "FILE_NAMES",
           "fetch_source", "fetch_pdf", "providers", "run_config", "wait_for", "KNOWN_PRICES"]

ROOT = Path(__file__).resolve().parents[2]
JOB_ID = re.compile(r"^job-\d{8}-\d{6}-[0-9a-f]{6}$")
MAX_SOURCE_BYTES = 200 * 1024 * 1024
MAX_PDF_BYTES = MAX_SOURCE_BYTES
TERMINAL = frozenset({"succeeded", "failed", "estimated", "cancelled"})
#: Files a caller may download, by name.
FILE_NAMES = ("mono", "dual", "source", "acceptance", "layout_review", "content_review", "log")
SOURCE_SUFFIX = {"pdf": ".pdf", "html": ".html", "markdown": ".md", "docx": ".docx", "epub": ".epub"}
CALLBACK_GIVE_UP = timedelta(hours=24)
WAIT_LIMIT = 60.0


class JobOptions(BaseModel):
    """The translation run's settings, as the workflow's run config needs them."""

    provider: str
    model_id: str
    base_url: str | None = None
    api_key_env: str | None = None
    api_protocol: Literal["openai-responses", "openai-chat", "anthropic", "gemini", "litellm"] | None = None
    structured_output: Literal["auto", "json_schema", "json_object", "prompt"] = "auto"
    #: Hard spend ceiling for the whole job; translation is priced first and not started above it.
    budget_usd: float = Field(default=2.0, gt=0, le=1000)
    input_usd_per_million: float | None = Field(default=None, ge=0)
    output_usd_per_million: float | None = Field(default=None, ge=0)
    reasoning_effort: str | None = None
    max_output_tokens: int = Field(default=2000, gt=0, le=64000)
    translation_concurrency: int = Field(default=8, ge=1, le=32)
    workers: int = Field(default=2, ge=1, le=8)
    layout_review: Literal["all", "flagged", "off"] = "all"
    vision: bool = True
    style_note: str | None = Field(default=None, max_length=4000)
    estimate_only: bool = False

    @model_validator(mode="after")
    def endpoint_and_prices_are_usable(self) -> "JobOptions":
        resolve_endpoint(self.provider, api_protocol=self.api_protocol, base_url=self.base_url,
                         api_key_env=self.api_key_env, structured_output=self.structured_output)
        if self.input_usd_per_million is None or self.output_usd_per_million is None:
            known = KNOWN_PRICES.get((PRESET_ALIASES.get(self.provider, self.provider), self.model_id))
            if known is None:
                raise ValueError("give input_usd_per_million and output_usd_per_million for this model; "
                                 "the budget cannot be enforced without prices")
            self.input_usd_per_million, self.output_usd_per_million = known
        return self

    @classmethod
    def build(cls, translation: ModelChoice, request: JobRequest) -> "JobOptions":
        prices = translation.known_prices() or (None, None)
        return cls(provider=translation.provider, model_id=translation.model, base_url=translation.base_url,
                   api_key_env=translation.api_key_env, api_protocol=translation.api_protocol,
                   structured_output=translation.structured_output, reasoning_effort=translation.reasoning_effort,
                   input_usd_per_million=prices[0], output_usd_per_million=prices[1], budget_usd=request.budget_usd,
                   max_output_tokens=request.max_output_tokens, translation_concurrency=request.translation_concurrency,
                   workers=request.workers, layout_review=request.layout_review, vision=request.vision,
                   style_note=request.style_note, estimate_only=request.estimate_only)

    def api_key_variable(self) -> str | None:
        return resolve_endpoint(self.provider, api_protocol=self.api_protocol, base_url=self.base_url,
                                api_key_env=self.api_key_env, structured_output=self.structured_output).api_key_env


def providers() -> list[dict]:
    """The provider presets a caller can name."""
    aliases: dict[str, list[str]] = {}
    for alias, name in PRESET_ALIASES.items():
        aliases.setdefault(name, []).append(alias)
    return [{"provider": name, "aliases": sorted(aliases.get(name, [])), "protocol": preset.protocol,
             "base_url": preset.base_url, "api_key_env": preset.api_key_env,
             "needs_base_url": preset.protocol == "openai-chat" and preset.base_url is None}
            for name, preset in PRESETS.items()]


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    return json.dumps(str(value), ensure_ascii=False)  # a JSON string is a valid TOML basic string


def run_config(job_id: str, options: JobOptions, job_dir: Path) -> str:
    values = {
        "run_id": job_id,
        "provider": options.provider,
        "model_id": options.model_id,
        "style_revision": "v0.7-default-style",
        "prompt_revision": "v0.7-complete-term-lock",
        "adapter_revision": "babeldoc-0.6.4-usable-v1",
        "pricing_revision": "job-supplied",
        "max_output_tokens": options.max_output_tokens,
        "budget_usd": options.budget_usd,
        "input_usd_per_million": options.input_usd_per_million,
        "output_usd_per_million": options.output_usd_per_million,
        "context_token_budget": 8000,
        "provider_max_attempts": 3,
        "output_token_policy": "adaptive-v1",
        "context_engine": "v1",
        "context_db": (job_dir / "context.sqlite").as_posix(),
        "translation_concurrency": options.translation_concurrency,
        "structured_output": options.structured_output,
    }
    for key in ("base_url", "api_key_env", "api_protocol", "reasoning_effort"):
        if getattr(options, key) is not None:
            values[key] = getattr(options, key)
    if options.reasoning_effort is None and PRESET_ALIASES.get(options.provider, options.provider) == "deepseek":
        values["reasoning_effort"] = "none"
    return "".join(f"{key} = {_toml_value(value)}\n" for key, value in values.items())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class JobManager:
    def __init__(self, root: Path | None = None, *, max_running: int = 2, runs_root: Path | None = None):
        self.root = Path(root or ROOT / "jobs").resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_running = max_running
        # Where the runner keeps each run's ledger.
        self.runs_root = Path(runs_root or ROOT / "runs").resolve()
        self.profiles = ProfileStore(self.root / "_profiles")
        # Keys and callback secrets sent with a request, held in memory only until the runner starts.
        self._pending_env: dict[str, dict[str, str]] = {}
        self._callback_secrets: dict[str, str] = {}
        self._callback_retry: dict[str, tuple[float, int]] = {}

    # --- creating and running -------------------------------------------------------

    def submit(self, data: bytes, filename: str, request: JobRequest, *, caller: str = "local",
               secrets_env: dict[str, str] | None = None, api_keys: dict[str, str] | None = None,
               callback_secret: str | None = None, idempotency_key: str | None = None) -> dict:
        """Create a job and start it (or queue it). Keys and secrets never touch disk.

        ``api_keys`` maps a role (``translation``, ``agent``, ``jev``) to a key
        sent with this request; ``secrets_env`` maps variable names to values.
        """
        if not data:
            raise JobError("the document is empty", code="source_unreadable")
        if len(data) > MAX_SOURCE_BYTES:
            raise JobError(f"the document is larger than {MAX_SOURCE_BYTES // (1024 * 1024)} MB",
                           code="source_too_large", status=413)
        try:
            fmt = detect_format(filename or "upload", data[:4096])
        except FormatError as problem:
            raise JobError(str(problem), code="unsupported_format", status=415,
                           hint=f"supported formats: {', '.join(FORMATS)}") from problem
        if idempotency_key:
            existing = self._idempotent(caller, idempotency_key)
            if existing:
                return self.get(existing)
        translation, agent, sources = self.profiles.resolve(caller, request)
        translation_var = check_translation_model(translation)
        agent_var = agent_key_variable(agent)
        env = dict(secrets_env or {})
        for role, key in (api_keys or {}).items():
            variable = {"translation": translation_var, "agent": agent_var, "jev": "TYPESAFE_API_KEY"}.get(role)
            if key and variable:
                env[variable] = key
        require_keys({"translation": translation_var, "agent": agent_var}, env)
        if request.callback_url and not (callback_secret or os.environ.get("PDFTRANS_CALLBACK_SECRET")):
            raise JobError("a callback_url needs a signing secret", code="callback_secret_missing", status=422,
                           hint="set PDFTRANS_CALLBACK_SECRET on the server, or send X-Callback-Secret")
        options = JobOptions.build(translation, request)
        job_id = f"job-{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"
        job_dir = self.root / job_id
        job_dir.mkdir()
        suffix = SOURCE_SUFFIX.get(fmt) or _latex_suffix(filename, data)
        source_file = f"source{suffix}"
        (job_dir / source_file).write_bytes(data)
        (job_dir / "config.toml").write_text(run_config(job_id, options, job_dir), encoding="utf-8")
        if request.style_note:
            (job_dir / "style.txt").write_text(request.style_note, encoding="utf-8")
        self._write_meta(job_id, {
            "job_id": job_id, "source_name": Path(filename or f"upload{suffix}").name, "source_file": source_file,
            "format": fmt, "created_at": _now(), "caller": caller,
            "request": request.model_dump(mode="json", exclude={"translation", "agent", "set_default"}),
            "translation": translation.model_dump(mode="json", exclude_none=True),
            "agent": agent.model_dump(mode="json", exclude_none=True), "model_sources": sources,
            "options": options.model_dump(mode="json"), "runs_root": str(self.runs_root),
            "callback_url": request.callback_url, "idempotency_key": idempotency_key,
            "pid": None, "cancelled": False})
        jobstate.write_state(job_dir, {"status": "queued", "phase": "queued", "delivery": "none"})
        jobstate.append_event(job_dir, "job.accepted", {"format": fmt, "translation": translation.label(),
                                                        "agent": agent.label(), "model_sources": sources})
        self._pending_env[job_id] = env
        if callback_secret:
            self._callback_secrets[job_id] = callback_secret
        self.dispatch()
        return self.get(job_id)

    def _idempotent(self, caller: str, key: str) -> str | None:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        for meta in self._metas():
            if meta.get("idempotency_key") == key and meta.get("caller") == caller:
                if datetime.fromisoformat(meta["created_at"]) > cutoff:
                    return meta["job_id"]
        return None

    def dispatch(self) -> None:
        """Start queued jobs while there is room, oldest first."""
        running = sum(1 for job in self._metas() if self._status(job)[0] in {"running", "starting"})
        for meta in sorted(self._metas(), key=lambda m: m["created_at"]):
            if running >= self.max_running:
                return
            if self._status(meta)[0] == "queued":
                self._start(meta)
                running += 1

    def _start(self, meta: dict) -> None:
        job_id = meta["job_id"]
        job_dir = self.root / job_id
        command = [sys.executable, "-m", "scripts.run_job", "--job-dir", str(job_dir)]
        if meta.get("mode") == "revise":
            command += ["--mode", "revise"]
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", **self._pending_env.pop(job_id, {})}
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        # stdin is closed: under MCP stdio the server's stdin carries JSON-RPC, and a runner that
        # inherited it could swallow the client's messages.
        with (job_dir / "runner.log").open("ab") as log:
            process = subprocess.Popen(command, cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=log,
                                       stderr=subprocess.STDOUT, creationflags=flags, start_new_session=os.name != "nt")
        meta["pid"] = process.pid
        meta["started_at"] = _now()
        self._write_meta(job_id, meta)

    # --- reading ----------------------------------------------------------------------

    def get(self, job_id: str) -> dict:
        meta = self._meta(job_id)
        job_dir = self.root / job_id
        status, error = self._status(meta)
        state = jobstate.read_state(job_dir) or self._legacy_state(job_id)
        entries = jobstate.deliveries(job_dir)
        recommended = jobstate.recommended(job_dir)
        report = self._report(job_id, recommended["version"]) if recommended else None
        view = {
            "job_id": job_id,
            "status": status,
            "phase": state.get("phase"),
            "stage": state.get("stage") if status not in TERMINAL else None,
            "format": meta.get("format", "pdf"),
            "route": state.get("route"),
            "source_name": meta["source_name"],
            "created_at": meta["created_at"],
            "started_at": meta.get("started_at"),
            "delivery": state.get("delivery", "none") if entries else "none",
            "recommended": recommended["version"] if recommended else None,
            "deliveries": [{"version": e["version"], "verdict": e.get("verdict"), "created_at": e["created_at"],
                            "recommended": recommended is not None and e["version"] == recommended["version"],
                            "note": e.get("note"), "files": e.get("files", {})} for e in entries],
            "problems": _problems(report),
            "translation_model": _label(meta.get("translation")) or _legacy_label(meta),
            "agent": {"model": _label(meta.get("agent")), "model_source": (meta.get("model_sources") or {}).get("agent"),
                      "jev": state.get("jev"), **_aios_summary(state.get("aios"))},
            "cost_usd": state.get("cost_usd"),
            "events_after": jobstate.last_seq(job_dir),
            "request": meta.get("request") or meta.get("options"),
            "files": sorted(self.files(job_id)),
        }
        if meta.get("budget_usd"):
            view["budget_usd"] = meta["budget_usd"]
        if meta.get("revisions"):
            view["revisions"] = meta["revisions"]
        if state.get("arxiv"):
            view["arxiv"] = state["arxiv"]
        if state.get("estimate"):
            view["estimate"] = state["estimate"]
        if report is not None:  # the v0.10 summary, kept for callers written against it
            view["result"] = {"verdict": report.get("verdict"),
                              "major_problems": [p.get("detail") for p in report.get("major_problems", [])],
                              "cost_usd": (state.get("cost_usd") or {}).get("total")}
        elif state.get("result"):
            view["result"] = state["result"]
        if error:
            view["error"] = error if isinstance(error, dict) else {"code": "runner_failed", "message": str(error)}
        if state.get("warning"):
            view["warning"] = state["warning"]
        view["for_agent"] = _for_agent(view)
        return view

    def _report(self, job_id: str, version: str) -> dict | None:
        path = self.root / job_id / jobstate.DELIVERIES / version / "acceptance-report.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _legacy_state(self, job_id: str) -> dict:
        """Jobs from before v0.11 kept their state in the PDF workflow's timings file."""
        timings = self._timings(job_id) or {}
        return {"status": timings.get("status"), "stage": timings.get("current_stage"), "phase": None,
                "result": timings.get("result"), "error": timings.get("error")}

    def list(self, limit: int = 50) -> list[dict]:
        self.dispatch()
        metas = sorted(self._metas(), key=lambda m: m["created_at"], reverse=True)[:limit]
        rows = []
        for meta in metas:
            state = jobstate.read_state(self.root / meta["job_id"]) or {}
            rows.append({"job_id": meta["job_id"], "status": self._status(meta)[0], "format": meta.get("format", "pdf"),
                         "delivery": state.get("delivery", "none"), "source_name": meta["source_name"],
                         "created_at": meta["created_at"]})
        return rows

    def files(self, job_id: str, version: str | None = None) -> dict[str, Path]:
        """A version's downloadable files by name (the recommended version when none is named)."""
        job_dir = self.root / self._checked(job_id)
        entry = None
        if version:
            entry = next((e for e in jobstate.deliveries(job_dir) if e["version"] == version), None)
            if entry is None:
                raise JobError(f"job {job_id} has no {version} version", code="version_missing", status=404)
        else:
            entry = jobstate.recommended(job_dir)
        found: dict[str, Path] = {}
        if entry is not None:
            base = job_dir / jobstate.DELIVERIES / entry["version"]
            found = {name: base / item["file"] for name, item in entry.get("files", {}).items() if (base / item["file"]).is_file()}
        elif (job_dir / "work").is_dir():  # a job from before v0.11, or one still running
            work = job_dir / "work"
            legacy = {"mono": work / "source.zh.zh.mono.pdf", "dual": work / "source.zh.zh.dual.pdf",
                      "acceptance": work / "acceptance-report.json", "layout_review": work / "layout-review.json",
                      "content_review": work / "content-review.jsonl"}
            found = {name: path for name, path in legacy.items() if path.is_file()}
        log = job_dir / "runner.log"
        if log.is_file():
            found["log"] = log
        return found

    def log_tail(self, job_id: str, lines: int = 40) -> str:
        job_dir = self.root / self._checked(job_id)
        text = ""
        for name in ("runner.log", "work.deliver.log"):
            path = job_dir / name
            if path.is_file():
                text += path.read_text(encoding="utf-8", errors="replace")
        return "\n".join(text.splitlines()[-lines:])

    def events(self, job_id: str, after: int = 0, wait: float = 0.0) -> dict:
        """Events numbered above ``after``; with ``wait``, hold up to that many seconds for the first new one."""
        job_dir = self.root / self._checked(job_id)
        deadline = time.monotonic() + max(0.0, min(wait, WAIT_LIMIT))
        while True:
            found = jobstate.read_events(job_dir, after)
            if found or time.monotonic() >= deadline:
                break
            if self._status(self._meta(job_id))[0] in TERMINAL:
                break
            self.dispatch()
            time.sleep(0.5)
        return {"job_id": job_id, "events": found, "last_seq": found[-1]["seq"] if found else after}

    def wait(self, job_id: str, after: int = 0, timeout: float = 50.0) -> dict:
        """For agents: wait server-side for news, then return the new events and the job as it is now."""
        answer = self.events(job_id, after, timeout)
        return {**answer, "job": self.get(job_id)}

    def cancel(self, job_id: str) -> dict:
        meta = self._meta(job_id)
        if self._status(meta)[0] in TERMINAL:
            raise JobError(f"job {job_id} has already finished", code="job_finished", status=409)
        pid = meta.get("pid")
        if pid and self._alive(meta):
            import psutil

            process = psutil.Process(pid)
            for child in process.children(recursive=True):
                child.kill()
            process.kill()
        meta["cancelled"] = True
        self._write_meta(job_id, meta)
        jobstate.append_event(self.root / job_id, "job.cancelled", {})
        return self.get(job_id)

    # --- caller profiles -------------------------------------------------------------------

    def profile(self, caller: str) -> dict:
        return {"caller": caller if caller.startswith(("client:", "local")) else "token", **self.profiles.get(caller)}

    def set_profile(self, caller: str, translation: dict | None, agent: dict | None) -> dict:
        try:
            choices = {role: ModelChoice.model_validate(value) for role, value in
                       (("translation", translation), ("agent", agent)) if value}
        except ValidationError as problem:
            raise validation_error(problem) from problem
        if not choices:
            raise JobError("give translation and/or agent", code="invalid_request", status=422)
        if "translation" in choices:
            check_translation_model(choices["translation"])
        self.profiles.put(caller, **choices)
        return self.profile(caller)

    # --- working on a finished job ------------------------------------------------------

    def units(self, job_id: str, *, pages: list[int] | None = None, unit_ids: list[str] | None = None,
              with_issues: bool = False, states: list[str] | None = None, limit: int = 200) -> dict:
        """Rows of the content review (source, translation, warnings per unit), filtered.

        ``issues`` also holds what acceptance judges over the whole text (a unit
        left in English, leaked markup), so every unit behind a verdict can be found here.
        """
        from .triage import leaked_markup, unit_is_untranslated

        path = self.root / self._checked(job_id) / "work" / "content-review.jsonl"
        if not path.is_file():
            raise JobError("this job has no translation yet", code="not_ready", status=409)
        wanted_pages, wanted_ids = set(pages or []), set(unit_ids or [])
        rows, matched = [], 0
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            source, target = row.get("source_text") or "", row.get("translated_text") or ""
            if target and unit_is_untranslated(source, target):
                row.setdefault("issues", []).append({"category": "untranslated_unit", "severity": "major",
                                                     "explanation": "the translation is still mostly English"})
            if leaked_markup(source, target):
                row.setdefault("issues", []).append({"category": "leaked_markup", "severity": "major",
                                                     "explanation": f"markup renders literally: {leaked_markup(source, target)}"})
            if wanted_pages and not wanted_pages & set(row.get("source_pages", [])):
                continue
            if wanted_ids and row["unit_id"] not in wanted_ids:
                continue
            if with_issues and not row.get("issues") and row.get("state") != "exhausted":
                continue
            if states and row.get("state") not in states:
                continue
            matched += 1
            if len(rows) < limit:
                view = {key: row.get(key) for key in (
                    "unit_id", "source_pages", "source_hash", "state", "source_text", "translated_text")}
                view["issues"] = [f"{i['category']} ({i['severity']}): {i['explanation']}" for i in row.get("issues", [])]
                rows.append(view)
        return {"job_id": job_id, "matched": matched, "returned": len(rows), "units": rows}

    def resume(self, job_id: str, *, secrets_env: dict[str, str] | None = None) -> dict:
        """Run a failed or cancelled job again from where it stopped.

        Every paid translation is read back from the run's cache, so only what
        was never done is done (and paid for) now.
        """
        meta = self._meta(job_id)
        status = self._status(meta)[0]
        if status not in {"failed", "cancelled"}:
            raise JobError(f"job {job_id} is {status}; only a failed or cancelled job can be resumed",
                           code="not_resumable", status=409)
        state = jobstate.read_state(self.root / job_id) or {}
        state.update(status="queued", error=None, stage=None)
        jobstate.write_state(self.root / job_id, state)
        meta.update(pid=None, cancelled=False, mode=None)
        meta.setdefault("resumed", []).append(_now())
        self._write_meta(job_id, meta)
        self._pending_env[job_id] = dict(secrets_env or {})
        self.dispatch()
        return self.get(job_id)

    def _finished(self, job_id: str) -> dict:
        meta = self._meta(job_id)
        status = self._status(meta)[0]
        if status != "succeeded":
            raise JobError(f"job {job_id} is {status}; this needs a job that finished translating",
                           code="not_ready", status=409)
        return meta

    def assist(self, job_id: str, task: str, *, api_key: str | None = None, budget_usd: float | None = None,
               **arguments) -> dict:
        """Run an :mod:`pdf_translation.assist` task with the job's model, within its budget."""
        from .assist import JobContext, run_task
        from .config import load_config
        from .llm import config_client

        meta = self._finished(job_id)
        job_dir = self.root / job_id
        if budget_usd is not None:
            if not 0 < budget_usd <= 1000:
                raise JobError("budget_usd must be between 0 and 1000")
            # Raising the ceiling is the caller's decision and is remembered for later calls.
            meta["budget_usd"] = budget_usd
            self._write_meta(job_id, meta)
        config = load_config(job_dir / "config.toml")
        source = job_dir / meta.get("source_file", "source.pdf")
        context = JobContext.load(config, job_dir / "work", source, self.runs_root, log=job_dir / "assist.jsonl")
        ceiling = meta.get("budget_usd") or config.budget_usd
        return run_task(context, config_client(config, api_key=api_key), task, budget_usd=ceiling, **arguments)

    def revise(self, job_id: str, fixes: list[dict], editor: str) -> dict:
        """Write fixes into a finished job, then write the outputs again and repeat acceptance.

        A fix is ``{unit_id, old_translated_text, new_translated_text, reason}``
        (``source_hash`` is filled in when missing). ``old_translated_text`` must be
        the translation the fix replaces (null for a unit never translated), so a
        fix written against an older text is refused. The whole batch is checked
        before anything is written. No model is called. The result is published as
        the ``revised`` version, recommended when it is not worse.
        """
        from .config import load_config
        from .local_fixes import apply_fixes

        meta = self._finished(job_id)
        if not fixes:
            raise JobError("no fixes given")
        if not editor or not editor.strip():
            raise JobError("say who made these fixes (editor), e.g. 'agent:claude-opus-5-5'")
        job_dir = self.root / job_id
        rows = {row["unit_id"]: row for row in self.units(job_id, limit=10 ** 9)["units"]}
        prepared = []
        for fix in fixes:
            if not isinstance(fix, dict) or not {"unit_id", "old_translated_text", "new_translated_text"} <= set(fix):
                raise JobError("each fix needs unit_id, old_translated_text and new_translated_text")
            if fix["unit_id"] not in rows:
                raise JobError(f"unknown unit: {fix['unit_id']}")
            prepared.append({"source_hash": rows[fix["unit_id"]]["source_hash"], "reason": "agent fix", **fix})
        try:
            pages = apply_fixes(prepared, editor.strip(), capture=job_dir / "work",
                                config=load_config(job_dir / "config.toml"), runs_root=self.runs_root)
        except (KeyError, ValueError) as problem:
            raise JobError(f"fixes refused, nothing written: {problem}", code="fixes_refused", status=422) from problem
        state = jobstate.read_state(job_dir) or {}
        state.update(status="queued", phase="revise", stage="render")
        jobstate.write_state(job_dir, state)
        meta.setdefault("revisions", []).append({"at": _now(), "editor": editor.strip(),
                                                 "units": [fix["unit_id"] for fix in prepared], "pages": pages})
        meta.update(pid=None, mode="revise", revise_pages=pages)
        self._write_meta(job_id, meta)
        self.dispatch()
        return self.get(job_id)

    # --- callbacks ------------------------------------------------------------------------

    def deliver_callbacks(self, *, transport=None) -> int:
        """POST new events to each job's callback_url, signed; retried with backoff for up to 24 hours."""
        import httpx

        sent = 0
        for meta in self._metas():
            url = meta.get("callback_url")
            if not url or meta.get("callback_gave_up"):
                continue
            job_id = meta["job_id"]
            next_at, attempts = self._callback_retry.get(job_id, (0.0, 0))
            if time.monotonic() < next_at:
                continue
            secret = self._callback_secrets.get(job_id) or os.environ.get("PDFTRANS_CALLBACK_SECRET")
            if not secret:
                continue
            for event in jobstate.read_events(self.root / job_id, meta.get("callback_seq", 0)):
                body = json.dumps(event, ensure_ascii=False).encode("utf-8")
                signature = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
                try:
                    # A callback to this machine or the local network must not go out through a proxy.
                    with httpx.Client(timeout=15, transport=transport, trust_env=not _is_local(url)) as client:
                        response = client.post(url, content=body, headers={
                            "Content-Type": "application/json", "X-Signature": f"sha256={signature}",
                            "X-Event-Seq": str(event["seq"]), "X-Job-Id": job_id, "User-Agent": "pdftrans-callback"})
                    ok = 200 <= response.status_code < 300
                except httpx.HTTPError:
                    ok = False
                if not ok:
                    attempts += 1
                    first_failure = meta.setdefault("callback_first_failure", _now())
                    if datetime.now(timezone.utc) - datetime.fromisoformat(first_failure) > CALLBACK_GIVE_UP:
                        meta["callback_gave_up"] = _now()
                        jobstate.append_event(self.root / job_id, "callback.abandoned", {"url": url})
                    self._callback_retry[job_id] = (time.monotonic() + min(3600, 5 * 2 ** attempts), attempts)
                    self._write_meta(job_id, meta)
                    break
                meta["callback_seq"] = event["seq"]
                meta.pop("callback_first_failure", None)
                self._callback_retry.pop(job_id, None)
                self._write_meta(job_id, meta)
                sent += 1
        return sent

    # --- internals ----------------------------------------------------------------------

    def _checked(self, job_id: str) -> str:
        if not JOB_ID.match(job_id or "") or not (self.root / job_id / "job.json").is_file():
            raise JobError(f"no such job: {job_id}", code="job_not_found", status=404)
        return job_id

    def _meta(self, job_id: str) -> dict:
        return json.loads((self.root / self._checked(job_id) / "job.json").read_text(encoding="utf-8"))

    def _metas(self) -> list[dict]:
        metas = []
        for path in self.root.glob("job-*/job.json"):
            try:
                metas.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return metas

    def _write_meta(self, job_id: str, meta: dict) -> None:
        jobstate.atomic_write(self.root / job_id / "job.json", json.dumps(meta, ensure_ascii=False, indent=2))

    def _timings(self, job_id: str) -> dict | None:
        path = self.root / job_id / "work.timings.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _alive(self, meta: dict) -> bool:
        pid = meta.get("pid")
        if not pid:
            return False
        import psutil

        try:
            process = psutil.Process(pid)
            # A reused pid belongs to another program; ours carries the job id.
            return process.is_running() and meta["job_id"] in " ".join(process.cmdline())
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False

    def _status(self, meta: dict) -> tuple[str, dict | str | None]:
        """(status, error) from what is on disk and whether the runner lives."""
        if meta.get("cancelled"):
            return "cancelled", None
        if not meta.get("pid"):
            return "queued", None
        state = jobstate.read_state(self.root / meta["job_id"]) or self._legacy_state(meta["job_id"])
        status = state.get("status")
        if status in TERMINAL:
            return status, state.get("error")
        if self._alive(meta):
            return ("running" if status == "running" else "starting"), None
        tail = self.log_tail(meta["job_id"], 8)
        return "failed", {"code": "runner_stopped",
                          "message": "the runner stopped without finishing" + (f": {tail}" if tail else "")}


def _is_local(url: str) -> bool:
    import ipaddress
    from urllib.parse import urlparse

    host = urlparse(url).hostname or ""
    if host == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_private


def _label(choice: dict | None) -> str | None:
    return f"{choice['provider']}/{choice['model']}" if choice else None


def _legacy_label(meta: dict) -> str | None:
    options = meta.get("options") or {}
    return f"{options['provider']}/{options['model_id']}" if options.get("provider") else None


def _latex_suffix(filename: str, data: bytes) -> str:
    name = (filename or "").lower()
    if data.startswith(b"PK"):
        return ".zip"
    if data[:2] == b"\x1f\x8b":
        return ".tar.gz"
    if name.endswith(".tar"):
        return ".tar"
    return ".tex"


def _problems(report: dict | None) -> list[dict]:
    if not report:
        return []
    found = [{"kind": p.get("kind"), "severity": "major", "count": p.get("count"), "detail": p.get("detail"),
              "pages": p.get("pages") or p.get("sample_pages") or []} for p in report.get("major_problems", [])]
    found += [{"kind": m.get("category"), "severity": "minor", "count": m.get("needs_human"),
               "pages": m.get("sample_pages") or []} for m in report.get("minor_issues", []) if m.get("needs_human")]
    return found


def _aios_summary(aios: dict | None) -> dict:
    if not aios:
        return {}
    repair = aios.get("repair") or {}
    optimize = aios.get("optimize") or {}
    summary = {}
    if repair:
        summary.update(repair_rounds=repair.get("rounds"), repair_stop_reason=repair.get("stop_reason"))
    if optimize:
        summary.update(optimize_kept=optimize.get("keep"), optimize_note=optimize.get("reason"))
    return summary


def _for_agent(view: dict) -> dict:
    """What an AI agent host should tell its user and do next, following the delivery rules."""
    status, delivery = view["status"], view["delivery"]
    majors = [p for p in view["problems"] if p["severity"] == "major"]
    minors = [p for p in view["problems"] if p["severity"] == "minor"]
    if status in {"queued", "starting", "running"} and delivery == "none":
        return {"summary": f"Translation in progress ({view.get('phase') or 'queued'}: {view.get('stage') or '...'}).",
                "summary_zh": f"翻译进行中（{view.get('phase') or '排队'}：{view.get('stage') or '…'}）。",
                "next_step": "call wait_job with events_after; do not poll more often than every 30 s"}
    if status == "failed" and delivery == "none":
        error = view.get("error") or {}
        return {"summary": f"The job failed ({error.get('code')}): {error.get('message', '')[:300]}. There are no files.",
                "summary_zh": f"任务失败（{error.get('code')}），没有生成文件。",
                "next_step": "fix the cause named in error, then resume_job"}
    if status == "cancelled":
        return {"summary": "The job was cancelled.", "summary_zh": "任务已取消。", "next_step": "none"}
    if status == "estimated":
        estimate = view.get("estimate") or {}
        return {"summary": f"Estimated cost up to USD {estimate.get('estimated_max_cost_usd')}.",
                "summary_zh": f"预估费用上限 {estimate.get('estimated_max_cost_usd')} 美元。",
                "next_step": "ask the user to confirm the price, then submit the job without estimate_only"}
    if delivery == "degraded":
        kinds = ", ".join(sorted({p["kind"] for p in majors})) or "see problems"
        return {"summary": f"Files were produced but the translation is NOT deliverable: {kinds}. "
                           "Tell the user it is not a finished translation.",
                "summary_zh": f"已生成文件，但译稿未通过验收（{kinds}），不能当作完成品交付。",
                "next_step": "report the problems to the user; optionally fix units and apply_fixes"}
    pending = status in {"running", "starting"}
    known = f" Known minor issues: {', '.join(p['kind'] for p in minors)}." if minors else ""
    known_zh = f"已知小问题：{'、'.join(p['kind'] for p in minors)}。" if minors else ""
    version = view.get("recommended") or delivery
    return {"summary": f"The {version} version is deliverable.{known}"
                       + (" The optimised version is still being built." if pending else ""),
            "summary_zh": f"{ {'usable': '可用版', 'optimized': '优化版', 'revised': '修订版'}.get(version, version) }已通过验收，可以交付。{known_zh}"
                          + ("优化版仍在生成。" if pending else ""),
            "next_step": ("give the user the files of the recommended version now; wait_job for delivery.optimized"
                          if pending else "give the user the files of the recommended version")}


def fetch_source(url: str, *, timeout: float = 120.0) -> tuple[bytes, str]:
    """Download a document over http(s) for a job, within the size limit."""
    import httpx

    if not re.match(r"^https?://", url):
        raise JobError("only http(s) URLs can be fetched", code="invalid_request")
    with httpx.stream("GET", url, timeout=timeout, follow_redirects=True) as response:
        if response.status_code >= 400:
            raise JobError(f"fetching the document failed: HTTP {response.status_code}", code="source_unreadable")
        chunks, size = [], 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > MAX_SOURCE_BYTES:
                raise JobError("the document is too large", code="source_too_large", status=413)
            chunks.append(chunk)
    name = url.split("?")[0].rstrip("/").rsplit("/", 1)[-1] or "download"
    return b"".join(chunks), name


fetch_pdf = fetch_source


def wait_for(manager: JobManager, job_id: str, *, poll: float = 5.0, timeout: float | None = None) -> dict:
    """Block until a job finishes (for synchronous callers such as the CLI)."""
    started = time.monotonic()
    while True:
        view = manager.get(job_id)
        if view["status"] in TERMINAL:
            return view
        if timeout is not None and time.monotonic() - started > timeout:
            return view
        manager.dispatch()
        time.sleep(poll)
