"""Command-line access to translation jobs, on this machine or on a remote server.

Every command prints JSON on stdout (progress goes to stderr), so scripts and
agents can read the result. With ``--server`` the same commands call a remote
``pdftrans serve`` over HTTP instead of running jobs here.
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Annotated, Optional

import typer

from .service import TERMINAL, JobManager, fetch_source, providers

jobs_app = typer.Typer(no_args_is_help=True, help="Inspect and manage translation jobs.")

Server = Annotated[Optional[str], typer.Option("--server", envvar="PDFTRANS_SERVER", help="URL of a remote `pdftrans serve`; runs here when omitted.")]
Token = Annotated[Optional[str], typer.Option("--token", envvar="PDFTRANS_TOKEN", help="Bearer token for the server.")]
JobsRoot = Annotated[Optional[Path], typer.Option("--jobs-root", help="Where local jobs live. Default: jobs/ in the project.")]


def _print(payload) -> None:
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))


def _backend(server: str | None, token: str | None, jobs_root: Path | None):
    if server:
        from .client import RemoteJobs

        return RemoteJobs(server, token)
    return JobManager(jobs_root)


def _wait(backend, job_id: str, poll: float) -> dict:
    last = None
    while True:
        view = backend.get(job_id)
        marker = (view["status"], view.get("phase"), view.get("stage"), view.get("delivery"))
        if marker != last:
            typer.echo(f"[{time.strftime('%H:%M:%S')}] {view['status']} {view.get('phase') or ''} "
                       f"{view.get('stage') or ''} delivery={view.get('delivery')}".rstrip(), err=True)
            last = marker
        if view["status"] in TERMINAL:
            return view
        if isinstance(backend, JobManager):
            backend.dispatch()
        time.sleep(poll)


def _model(provider: str | None, model: str | None, base_url: str | None, key_env: str | None,
           input_price: float | None, output_price: float | None, role: str) -> dict | None:
    if not provider and not model:
        return None
    if not (provider and model):
        raise typer.BadParameter(f"give both the {role} provider and model")
    choice = {"provider": provider, "model": model, "base_url": base_url, "api_key_env": key_env,
              "input_usd_per_million": input_price, "output_usd_per_million": output_price}
    return {k: v for k, v in choice.items() if v is not None}


def translate(
    source: Annotated[str, typer.Argument(help="Document path (PDF, .tex/.zip LaTeX project, HTML, Markdown, DOCX, EPUB) or an http(s) URL.")],
    provider: Annotated[Optional[str], typer.Option(help="Translation model vendor; see `pdftrans providers`. Required the first time.")] = None,
    model_id: Annotated[Optional[str], typer.Option("--model-id", "--model", help="Translation model name. Required the first time.")] = None,
    base_url: Annotated[Optional[str], typer.Option("--base-url")] = None,
    api_key_env: Annotated[Optional[str], typer.Option("--api-key-env", help="Environment variable holding the translation model key.")] = None,
    input_price: Annotated[Optional[float], typer.Option("--input-usd-per-million")] = None,
    output_price: Annotated[Optional[float], typer.Option("--output-usd-per-million")] = None,
    agent_provider: Annotated[Optional[str], typer.Option("--agent-provider", help="AI OS model vendor. Required the first time.")] = None,
    agent_model: Annotated[Optional[str], typer.Option("--agent-model", help="AI OS model name. Required the first time.")] = None,
    agent_base_url: Annotated[Optional[str], typer.Option("--agent-base-url")] = None,
    agent_key_env: Annotated[Optional[str], typer.Option("--agent-api-key-env")] = None,
    set_default: Annotated[bool, typer.Option("--set-default", help="Make the models named here the new defaults.")] = False,
    client_id: Annotated[Optional[str], typer.Option("--client-id", help="Who is calling, for remembering default models.")] = None,
    budget_usd: Annotated[float, typer.Option("--budget-usd", help="Hard spend ceiling for the whole job.")] = 2.0,
    style_note: Annotated[Optional[str], typer.Option("--style-note", help="Subject and term choices for this document.")] = None,
    optimized: Annotated[bool, typer.Option("--optimized/--no-optimized", help="Also build the optimised version.")] = True,
    bilingual: Annotated[bool, typer.Option("--bilingual/--no-bilingual")] = True,
    network: Annotated[bool, typer.Option("--network/--no-network", help="Allow fetching an arXiv paper's LaTeX source.")] = True,
    main_file: Annotated[Optional[str], typer.Option("--main-file", help="LaTeX project: the main .tex file.")] = None,
    layout_review: Annotated[str, typer.Option("--layout-review", help="PDF: all | flagged | off")] = "all",
    no_vision: Annotated[bool, typer.Option("--no-vision")] = False,
    estimate_only: Annotated[bool, typer.Option("--estimate-only", help="Price only; no model call.")] = False,
    workers: Annotated[int, typer.Option(help="PDF: parallel capture/render processes.")] = 2,
    concurrency: Annotated[int, typer.Option("--concurrency", help="Model requests in flight.")] = 8,
    wait: Annotated[bool, typer.Option("--wait/--no-wait", help="Wait for the job to finish.")] = True,
    out_dir: Annotated[Optional[Path], typer.Option("--out-dir", help="Copy the recommended version's files here when done.")] = None,
    poll: Annotated[float, typer.Option(help="Seconds between status checks.")] = 10.0,
    server: Server = None, token: Token = None, jobs_root: JobsRoot = None,
) -> None:
    """Translate a document into Chinese: a usable version, then (by default) an optimised one."""
    request = {
        "translation": _model(provider, model_id, base_url, api_key_env, input_price, output_price, "translation"),
        "agent": _model(agent_provider, agent_model, agent_base_url, agent_key_env, None, None, "agent"),
        "set_default": set_default or None, "client_id": client_id, "budget_usd": budget_usd, "style_note": style_note,
        "deliver": {"optimized": optimized, "bilingual": bilingual}, "allow_network": network, "main_file": main_file,
        "layout_review": layout_review, "vision": not no_vision, "estimate_only": estimate_only, "workers": workers,
        "translation_concurrency": concurrency,
    }
    request = {k: v for k, v in request.items() if v is not None}
    backend = _backend(server, token, jobs_root)
    is_url = source.startswith(("http://", "https://"))
    try:
        if server:
            job = backend.submit(source, request)
        else:
            from .jobspec import JobRequest, caller_key

            data, name = fetch_source(source) if is_url else (Path(source).read_bytes(), Path(source).name)
            job = backend.submit(data, name, JobRequest.model_validate(request), caller=caller_key(client_id, None))
    except Exception as error:
        _print({"error": getattr(error, "payload", lambda: {"message": str(error)})()})
        raise typer.Exit(2)
    typer.echo(f"job {job['job_id']} {job['status']}", err=True)
    if not wait:
        return _print(job)
    job = _wait(backend, job["job_id"], poll)
    if out_dir and job.get("recommended"):
        out_dir.mkdir(parents=True, exist_ok=True)
        copied = {}
        entry = next(e for e in job["deliveries"] if e["version"] == job["recommended"])
        for name in ("mono", "dual", "source", "acceptance"):
            item = entry["files"].get(name)
            if not item:
                continue
            target = out_dir / item["file"]
            if server:
                backend.download(job["job_id"], name, target, job["recommended"])
            else:
                shutil.copy2(backend.files(job["job_id"], job["recommended"])[name], target)
            copied[name] = str(target)
        job["copied"] = copied
    _print(job)
    if job["status"] == "failed":
        raise typer.Exit(1)


def profile_command(
    provider: Annotated[Optional[str], typer.Option(help="Set the default translation model vendor.")] = None,
    model_id: Annotated[Optional[str], typer.Option("--model-id", "--model")] = None,
    agent_provider: Annotated[Optional[str], typer.Option("--agent-provider")] = None,
    agent_model: Annotated[Optional[str], typer.Option("--agent-model")] = None,
    client_id: Annotated[Optional[str], typer.Option("--client-id")] = None,
    server: Server = None, token: Token = None, jobs_root: JobsRoot = None,
) -> None:
    """Show (or, with model options, change) the default translation and AI OS models."""
    backend = _backend(server, token, jobs_root)
    translation = _model(provider, model_id, None, None, None, None, "translation")
    agent = _model(agent_provider, agent_model, None, None, None, None, "agent")
    try:
        if server:
            answer = backend.set_profile(translation, agent, client_id) if (translation or agent) else backend.profile()
        else:
            from .jobspec import caller_key

            caller = caller_key(client_id, None)
            answer = backend.set_profile(caller, translation, agent) if (translation or agent) else backend.profile(caller)
    except Exception as error:
        _print({"error": str(error)})
        raise typer.Exit(1)
    _print(answer)


def setup_command(
    check: Annotated[bool, typer.Option("--check", help="Only report what is installed; change nothing.")] = False,
) -> None:
    """Install the pi agent (the AI OS) at the pinned version and check LaTeX, Node.js and keys."""
    import os
    import subprocess

    from .aios.pi_agent import AGENT_DIR, MIN_NODE, availability, node_version

    report: dict = {}
    version = node_version()
    report["node"] = ".".join(map(str, version)) if version else None
    if not check and version and version[:2] >= MIN_NODE:
        npm = shutil.which("npm")
        if not npm:
            report["pi"] = "npm is not on PATH; install Node.js with npm"
        else:
            typer.echo(f"installing the pi agent into {AGENT_DIR / 'node_modules'} ...", err=True)
            command = [npm, "ci" if (AGENT_DIR / "package-lock.json").is_file() else "install",
                       "--ignore-scripts", "--no-audit", "--no-fund"]
            finished = subprocess.run(command, cwd=AGENT_DIR, capture_output=True, text=True, errors="replace",
                                      shell=os.name == "nt")
            report["npm"] = "ok" if finished.returncode == 0 else (finished.stdout + finished.stderr)[-1500:]
    ready, detail = availability()
    report["aios"] = {"available": ready, "detail": detail}
    report["latex"] = {engine: bool(shutil.which(engine)) for engine in ("xelatex", "pdflatex", "bibtex")}
    report["keys_in_environment"] = sorted(name for name in os.environ if name.endswith("_API_KEY"))
    report["jev"] = "configured" if os.environ.get("TYPESAFE_API_KEY") else "not configured (optional; set TYPESAFE_API_KEY)"
    _print(report)
    if not ready:
        raise typer.Exit(1)


@jobs_app.command("list")
def jobs_list(limit: int = 20, server: Server = None, token: Token = None, jobs_root: JobsRoot = None) -> None:
    """Recent jobs, newest first."""
    _print({"jobs": _backend(server, token, jobs_root).list(limit)})


@jobs_app.command("status")
def jobs_status(job_id: str, wait: Annotated[bool, typer.Option("--wait")] = False, poll: float = 10.0,
                server: Server = None, token: Token = None, jobs_root: JobsRoot = None) -> None:
    """A job's status; with --wait, block until it finishes."""
    backend = _backend(server, token, jobs_root)
    _print(_wait(backend, job_id, poll) if wait else backend.get(job_id))


@jobs_app.command("log")
def jobs_log(job_id: str, lines: int = 40, server: Server = None, token: Token = None, jobs_root: JobsRoot = None) -> None:
    """The tail of a job's log."""
    _print({"job_id": job_id, "log": _backend(server, token, jobs_root).log_tail(job_id, lines)})


@jobs_app.command("cancel")
def jobs_cancel(job_id: str, server: Server = None, token: Token = None, jobs_root: JobsRoot = None) -> None:
    """Stop a queued or running job."""
    _print(_backend(server, token, jobs_root).cancel(job_id))


@jobs_app.command("resume")
def jobs_resume(job_id: str, wait: Annotated[bool, typer.Option("--wait")] = False, poll: float = 10.0,
                server: Server = None, token: Token = None, jobs_root: JobsRoot = None) -> None:
    """Run a failed or cancelled job again from where it stopped; paid work is reused."""
    backend = _backend(server, token, jobs_root)
    try:
        job = backend.resume(job_id)
    except Exception as error:
        _print({"error": str(error)})
        raise typer.Exit(1)
    _print(_wait(backend, job_id, poll) if wait else job)


@jobs_app.command("fetch")
def jobs_fetch(job_id: str, name: Annotated[str, typer.Argument(help="mono | dual | source | acceptance | layout_review | content_review | log")],
               destination: Path,
               version: Annotated[Optional[str], typer.Option(help="usable | optimized | degraded | revised; default: the recommended one")] = None,
               server: Server = None, token: Token = None, jobs_root: JobsRoot = None) -> None:
    """Save one of a job's files."""
    backend = _backend(server, token, jobs_root)
    if server:
        backend.download(job_id, name, destination, version)
    else:
        files = backend.files(job_id, version)
        if name not in files:
            _print({"error": f"{name} is not available; available: {sorted(files)}"})
            raise typer.Exit(1)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(files[name], destination)
    _print({"job_id": job_id, "name": name, "saved": str(destination)})


@jobs_app.command("events")
def jobs_events(job_id: str, after: int = 0, wait: float = 0.0,
                server: Server = None, token: Token = None, jobs_root: JobsRoot = None) -> None:
    """A job's numbered events after --after; --wait holds up to that many seconds for a new one."""
    _print(_backend(server, token, jobs_root).events(job_id, after, wait))


@jobs_app.command("units")
def jobs_units(job_id: str,
               page: Annotated[Optional[list[int]], typer.Option("--page", help="One-based page; repeatable.")] = None,
               unit: Annotated[Optional[list[str]], typer.Option("--unit", help="Unit id; repeatable.")] = None,
               with_issues: Annotated[bool, typer.Option("--with-issues", help="Only flagged or untranslated units.")] = False,
               state: Annotated[Optional[list[str]], typer.Option("--state", help="accepted | needs_review | exhausted")] = None,
               limit: int = 200, server: Server = None, token: Token = None, jobs_root: JobsRoot = None) -> None:
    """A finished job's units: source, translation, state and warnings."""
    _print(_backend(server, token, jobs_root).units(job_id, pages=page, unit_ids=unit, with_issues=with_issues,
                                                    states=state, limit=limit))


@jobs_app.command("ask")
def jobs_ask(
    job_id: str,
    task: Annotated[str, typer.Argument(help="retranslate | shorten | review_units | review_pages | ask")],
    unit: Annotated[Optional[list[str]], typer.Option("--unit", help="Unit id; repeatable.")] = None,
    page: Annotated[Optional[list[int]], typer.Option("--page", help="One-based page; repeatable.")] = None,
    instruction: Annotated[Optional[str], typer.Option(help="What to do differently.")] = None,
    prompt: Annotated[Optional[str], typer.Option(help="ask: the question.")] = None,
    system: Annotated[Optional[str], typer.Option(help="ask: replaces the default system prompt.")] = None,
    schema_file: Annotated[Optional[Path], typer.Option("--schema-file", help="ask: JSON Schema the reply must follow.")] = None,
    images: Annotated[bool, typer.Option("--images", help="ask: attach original and rendered page images.")] = False,
    no_vision: Annotated[bool, typer.Option("--no-vision", help="review_pages: text only.")] = False,
    shorten_to: Annotated[float, typer.Option("--shorten-to", help="shorten: target share of the current length.")] = 0.8,
    max_output_tokens: Annotated[Optional[int], typer.Option("--max-output-tokens")] = None,
    budget_usd: Annotated[Optional[float], typer.Option("--budget-usd", help="Raise the job's spend ceiling.")] = None,
    fixes_out: Annotated[Optional[Path], typer.Option("--fixes-out", help="Also save the applicable fixes, for `jobs revise`.")] = None,
    server: Server = None, token: Token = None, jobs_root: JobsRoot = None,
) -> None:
    """Have the job's own model do one task; returns candidate fixes, writes nothing."""
    arguments = {"unit_ids": unit, "pages": page, "instruction": instruction, "prompt": prompt, "system": system,
                 "json_schema": json.loads(schema_file.read_text(encoding="utf-8")) if schema_file else None,
                 "images": images or None, "vision": False if no_vision else None,
                 "shorten_to": shorten_to if task == "shorten" else None, "max_output_tokens": max_output_tokens}
    arguments = {k: v for k, v in arguments.items() if v is not None}
    try:
        answer = _backend(server, token, jobs_root).assist(job_id, task, budget_usd=budget_usd, **arguments)
    except Exception as error:
        _print({"error": f"{type(error).__name__}: {error}"})
        raise typer.Exit(1)
    if fixes_out:
        fixes = [item["fix"] for item in answer.get("candidates", answer.get("problems", [])) if "fix" in item]
        fixes_out.parent.mkdir(parents=True, exist_ok=True)
        fixes_out.write_text(json.dumps(fixes, ensure_ascii=False, indent=2), encoding="utf-8")
        answer["fixes_saved"] = {"path": str(fixes_out), "count": len(fixes)}
    _print(answer)


@jobs_app.command("revise")
def jobs_revise(job_id: str, fixes: Annotated[Path, typer.Argument(help="JSON list of fixes.")],
                editor: Annotated[str, typer.Option(help="Who made the fixes, e.g. 'agent:claude-opus-5-5'.")],
                wait: Annotated[bool, typer.Option("--wait", help="Wait for the pages to be rendered again.")] = False,
                poll: float = 10.0, server: Server = None, token: Token = None, jobs_root: JobsRoot = None) -> None:
    """Write fixes into a finished job, render the changed pages again, repeat acceptance."""
    backend = _backend(server, token, jobs_root)
    try:
        job = backend.revise(job_id, json.loads(fixes.read_text(encoding="utf-8")), editor)
    except Exception as error:
        _print({"error": str(error)})
        raise typer.Exit(1)
    _print(_wait(backend, job_id, poll) if wait else job)


def guide_command() -> None:
    """Print the guide for agents: the pipeline, its files, and when to delegate to a model."""
    from .mcp_server import GUIDE

    typer.echo(GUIDE.read_text(encoding="utf-8"))


def providers_command() -> None:
    """Model vendors a job can use, with the API-key variable each expects."""
    _print({"providers": providers()})


def serve_command(
    host: Annotated[str, typer.Option(help="Interface to listen on; anything beyond loopback needs a token.")] = "127.0.0.1",
    port: int = 8765,
    token: Token = None,
    jobs_root: JobsRoot = None,
    max_running: Annotated[int, typer.Option("--max-running", help="Jobs run at once; more wait in a queue.")] = 2,
    public_url: Annotated[Optional[str], typer.Option("--public-url", help="URL clients reach this server at (for download links).")] = None,
    allow_local_paths: Annotated[bool, typer.Option("--allow-local-paths", help="Let MCP callers name PDFs by server path.")] = False,
) -> None:
    """Serve the HTTP API and MCP (POST /mcp) for callers on any host."""
    from dotenv import load_dotenv

    from .http_api import serve

    load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)
    try:
        server = serve(host, port, JobManager(jobs_root, max_running=max_running), token=token,
                       public_url=public_url, allow_local_paths=allow_local_paths)
    except ValueError as problem:
        typer.echo(str(problem), err=True)
        raise typer.Exit(2)
    typer.echo(f"serving on {server.public_url} (MCP at {server.public_url}/mcp)", err=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def mcp_command(jobs_root: JobsRoot = None,
                max_running: Annotated[int, typer.Option("--max-running")] = 2) -> None:
    """Run the MCP server on stdio, for an AI client on this machine."""
    from dotenv import load_dotenv

    from .mcp_server import McpServer, serve_stdio

    load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)
    # stdout carries the protocol; anything else printed there would corrupt it.
    serve_stdio(McpServer(JobManager(jobs_root, max_running=max_running), local=True))


def register(app: typer.Typer) -> None:
    app.command("translate")(translate)
    app.add_typer(jobs_app, name="jobs")
    app.command("providers")(providers_command)
    app.command("profile")(profile_command)
    app.command("setup")(setup_command)
    app.command("guide")(guide_command)
    app.command("serve")(serve_command)
    app.command("mcp")(mcp_command)
