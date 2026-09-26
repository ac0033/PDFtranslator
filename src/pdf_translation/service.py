"""Translation jobs: the one interface every way in (CLI, HTTP, MCP, skills) calls.

A job is a directory under the jobs root::

    <job_id>/source.pdf          the PDF to translate
    <job_id>/config.toml         run config generated from the job's options
    <job_id>/style.txt           optional per-document translation instruction
    <job_id>/job.json            options, creation time, runner pid, cancellation
    <job_id>/runner.log          the runner's console output
    <job_id>/work/               capture, per-page and merged output
    <job_id>/work.timings.json   stage timings and final status (written by the runner)
    <job_id>/assist.jsonl        model calls made for an agent after the run (task, cost)

After a job succeeds an agent may keep working on it: read its units, ask the
job's model for help (:mod:`pdf_translation.assist`), and write fixes back,
which renders the changed pages again and repeats acceptance (``revise``).

The runner is ``scripts/deliver_pdf.py`` in its own process, so a job keeps
running if the caller disconnects and its state is on disk if the server
restarts. An API key passed with a job goes only into the runner's environment;
it is never written to disk.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from .llm import PRESETS, PRESET_ALIASES, resolve_endpoint

ROOT = Path(__file__).resolve().parents[2]
JOB_ID = re.compile(r"^job-\d{8}-\d{6}-[0-9a-f]{6}$")
MAX_PDF_BYTES = 200 * 1024 * 1024
TERMINAL = frozenset({"succeeded", "failed", "estimated", "cancelled"})
#: USD per million tokens (input, output), used only when the caller gives none.
KNOWN_PRICES = {("deepseek", "deepseek-flash"): (0.30, 1.20)}
#: Files a caller may download, by name.
FILE_NAMES = ("mono", "dual", "acceptance", "layout_review", "content_review", "log")


class JobOptions(BaseModel):
    """What a caller may choose. Everything else is a fixed, tested default."""

    provider: str = "deepseek"
    model_id: str = "deepseek-flash"
    base_url: str | None = None
    api_key_env: str | None = None
    api_protocol: Literal["openai-responses", "openai-chat", "anthropic", "gemini", "litellm"] | None = None
    structured_output: Literal["auto", "json_schema", "json_object", "prompt"] = "auto"
    #: Hard spend ceiling for the job; translation is priced first and not started above it.
    budget_usd: float = Field(default=1.0, gt=0, le=1000)
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


class JobError(ValueError):
    """A request the job service refuses; the message says why."""


class JobManager:
    def __init__(self, root: Path | None = None, *, max_running: int = 2, runs_root: Path | None = None):
        self.root = Path(root or ROOT / "jobs").resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_running = max_running
        # Where the runner keeps each run's ledger (scripts/deliver_pdf.py uses runs/).
        self.runs_root = Path(runs_root or ROOT / "runs").resolve()
        # API keys given with a job, held in memory only until its runner starts.
        # A queued job whose server restarts starts with the server's own keys.
        self._pending_env: dict[str, dict[str, str]] = {}

    # --- creating and running -------------------------------------------------------

    def submit(self, pdf: bytes, filename: str, options: JobOptions, *, secrets_env: dict[str, str] | None = None) -> dict:
        """Create a job and start it (or queue it). ``secrets_env`` never touches disk."""
        if not pdf.startswith(b"%PDF"):
            raise JobError("the file is not a PDF")
        if len(pdf) > MAX_PDF_BYTES:
            raise JobError(f"the PDF is larger than {MAX_PDF_BYTES // (1024 * 1024)} MB")
        job_id = f"job-{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"
        job_dir = self.root / job_id
        job_dir.mkdir()
        (job_dir / "source.pdf").write_bytes(pdf)
        (job_dir / "config.toml").write_text(run_config(job_id, options, job_dir), encoding="utf-8")
        if options.style_note:
            (job_dir / "style.txt").write_text(options.style_note, encoding="utf-8")
        self._write_meta(job_id, {"job_id": job_id, "source_name": Path(filename or "source.pdf").name,
                                  "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                  "options": options.model_dump(mode="json"), "pid": None, "cancelled": False})
        self._pending_env[job_id] = dict(secrets_env or {})
        self.dispatch()
        return self.get(job_id)

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
        options = JobOptions.model_validate(meta["options"])
        command = [sys.executable, "-m", "scripts.deliver_pdf", "--source", str(job_dir / "source.pdf"),
                   "--config", str(job_dir / "config.toml"), "--output", str(job_dir / "work"),
                   "--output-prefix", "zh", "--workers", str(options.workers), "--layout-review", options.layout_review]
        if not options.vision:
            command.append("--no-vision")
        if options.estimate_only:
            command.append("--estimate-only")
        if (job_dir / "style.txt").is_file():
            command += ["--style-note-file", str(job_dir / "style.txt")]
        if meta.get("resume_from"):
            # A revision: translations are already fixed; render and accept again, no model call.
            command += ["--resume-from", meta["resume_from"], "--layout-review", "off"]
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", **self._pending_env.pop(job_id, {})}
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        with (job_dir / "runner.log").open("ab") as log:
            process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                                       creationflags=flags, start_new_session=os.name != "nt")
        meta["pid"] = process.pid
        meta["started_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._write_meta(job_id, meta)

    # --- reading ----------------------------------------------------------------------

    def get(self, job_id: str) -> dict:
        meta = self._meta(job_id)
        status, detail = self._status(meta)
        timings = self._timings(job_id) or {}
        view = {
            "job_id": job_id,
            "status": status,
            "stage": timings.get("current_stage"),
            "source_name": meta["source_name"],
            "pages": timings.get("pages"),
            "created_at": meta["created_at"],
            "started_at": meta.get("started_at"),
            "stages": timings.get("stages", {}),
            "elapsed_seconds": timings.get("total_seconds"),
            "options": meta["options"],
            "files": sorted(self.files(job_id)),
        }
        if timings.get("result"):
            view["result"] = timings["result"]
        if meta.get("revisions"):
            view["revisions"] = meta["revisions"]
        if meta.get("budget_usd"):
            view["budget_usd"] = meta["budget_usd"]
        if detail:
            view["error"] = detail
        return view

    def list(self, limit: int = 50) -> list[dict]:
        self.dispatch()
        metas = sorted(self._metas(), key=lambda m: m["created_at"], reverse=True)[:limit]
        return [{"job_id": m["job_id"], "status": self._status(m)[0], "source_name": m["source_name"],
                 "created_at": m["created_at"]} for m in metas]

    def files(self, job_id: str) -> dict[str, Path]:
        job_dir = self.root / self._checked(job_id)
        work = job_dir / "work"
        candidates = {
            "mono": work / "source.zh.zh.mono.pdf",
            "dual": work / "source.zh.zh.dual.pdf",
            "acceptance": work / "acceptance-report.json",
            "layout_review": work / "layout-review.json",
            "content_review": work / "content-review.jsonl",
            "log": job_dir / "work.deliver.log",
        }
        return {name: path for name, path in candidates.items() if path.is_file()}

    def log_tail(self, job_id: str, lines: int = 40) -> str:
        job_dir = self.root / self._checked(job_id)
        text = ""
        for name in ("runner.log", "work.deliver.log"):
            path = job_dir / name
            if path.is_file():
                text += path.read_text(encoding="utf-8", errors="replace")
        return "\n".join(text.splitlines()[-lines:])

    def cancel(self, job_id: str) -> dict:
        meta = self._meta(job_id)
        if self._status(meta)[0] in TERMINAL:
            raise JobError(f"job {job_id} has already finished")
        pid = meta.get("pid")
        if pid and self._alive(meta):
            import psutil

            process = psutil.Process(pid)
            for child in process.children(recursive=True):
                child.kill()
            process.kill()
        meta["cancelled"] = True
        self._write_meta(job_id, meta)
        return self.get(job_id)

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
            raise JobError("this job has no translation yet")
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

        Capture on disk is kept and every paid translation is read back from the
        run's cache, so only what was never done is done (and paid for) now.
        """
        meta = self._meta(job_id)
        status = self._status(meta)[0]
        if status not in {"failed", "cancelled"}:
            raise JobError(f"job {job_id} is {status}; only a failed or cancelled job can be resumed")
        timings = self._timings(job_id)
        if timings is not None:
            # The runner writes its own status once it starts; until then the job is queued or starting.
            for key in ("status", "error", "current_stage", "result"):
                timings.pop(key, None)
            (self.root / job_id / "work.timings.json").write_text(json.dumps(timings, ensure_ascii=False, indent=2),
                                                                encoding="utf-8")
        meta.update(pid=None, cancelled=False, resume_from=None)
        meta.setdefault("resumed", []).append(datetime.now(timezone.utc).isoformat(timespec="seconds"))
        self._write_meta(job_id, meta)
        self._pending_env[job_id] = dict(secrets_env or {})
        self.dispatch()
        return self.get(job_id)

    def _finished(self, job_id: str) -> dict:
        meta = self._meta(job_id)
        status = self._status(meta)[0]
        if status != "succeeded":
            raise JobError(f"job {job_id} is {status}; this needs a job that finished translating")
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
        context = JobContext.load(config, job_dir / "work", job_dir / "source.pdf", self.runs_root,
                                  log=job_dir / "assist.jsonl")
        ceiling = meta.get("budget_usd") or config.budget_usd
        return run_task(context, config_client(config, api_key=api_key), task, budget_usd=ceiling, **arguments)

    def revise(self, job_id: str, fixes: list[dict], editor: str) -> dict:
        """Write fixes into a finished job, then render the changed pages and run acceptance again.

        A fix is ``{unit_id, old_translated_text, new_translated_text, reason}``
        (``source_hash`` is filled in when missing). ``old_translated_text`` must be
        the translation the fix replaces (null for a unit never translated), so a
        fix written against an older text is refused. The whole batch is checked
        before anything is written. No model is called.
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
            raise JobError(f"fixes refused, nothing written: {problem}") from problem
        for page in pages:
            for label in ("mono", "dual"):
                (job_dir / "work" / f"page-{page:04d}" / f"source.zh.{label}.pdf").unlink(missing_ok=True)
        # Mark the job busy before its runner starts, so no caller takes the old verdict as current.
        timings = self._timings(job_id) or {}
        timings.update(status="running", current_stage="render")
        timings.pop("result", None)
        (job_dir / "work.timings.json").write_text(json.dumps(timings, ensure_ascii=False, indent=2), encoding="utf-8")
        meta.setdefault("revisions", []).append({
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "editor": editor.strip(),
            "units": [fix["unit_id"] for fix in prepared], "pages": pages})
        meta.update(pid=None, resume_from="render")
        self._write_meta(job_id, meta)
        self.dispatch()
        return self.get(job_id)

    # --- internals ----------------------------------------------------------------------

    def _checked(self, job_id: str) -> str:
        if not JOB_ID.match(job_id or "") or not (self.root / job_id / "job.json").is_file():
            raise JobError(f"no such job: {job_id}")
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
        path = self.root / job_id / "job.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)

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

    def _status(self, meta: dict) -> tuple[str, str | None]:
        """(status, error detail) from what is on disk and whether the runner lives."""
        if meta.get("cancelled"):
            return "cancelled", None
        if not meta.get("pid"):
            return "queued", None
        timings = self._timings(meta["job_id"]) or {}
        status = timings.get("status")
        if status in TERMINAL:
            return status, timings.get("error")
        if self._alive(meta):
            return ("running" if status else "starting"), None
        tail = self.log_tail(meta["job_id"], 8)
        return "failed", "the runner stopped without finishing" + (f": {tail}" if tail else "")


def fetch_pdf(url: str, *, timeout: float = 120.0) -> tuple[bytes, str]:
    """Download a PDF over http(s) for a job, within the size limit."""
    import httpx

    if not re.match(r"^https?://", url):
        raise JobError("only http(s) URLs can be fetched")
    with httpx.stream("GET", url, timeout=timeout, follow_redirects=True) as response:
        if response.status_code >= 400:
            raise JobError(f"fetching the PDF failed: HTTP {response.status_code}")
        chunks, size = [], 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > MAX_PDF_BYTES:
                raise JobError("the PDF is too large")
            chunks.append(chunk)
    name = url.rstrip("/").rsplit("/", 1)[-1] or "download.pdf"
    return b"".join(chunks), name


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
