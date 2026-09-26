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

from .service import TERMINAL, JobManager, JobOptions, fetch_pdf, providers

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
        marker = (view["status"], view.get("stage"))
        if marker != last:
            typer.echo(f"[{time.strftime('%H:%M:%S')}] {view['status']} {view.get('stage') or ''}".rstrip(), err=True)
            last = marker
        if view["status"] in TERMINAL:
            return view
        if isinstance(backend, JobManager):
            backend.dispatch()
        time.sleep(poll)


def translate(
    source: Annotated[str, typer.Argument(help="PDF path, or an http(s) URL.")],
    provider: Annotated[str, typer.Option(help="Model vendor preset; see `pdftrans providers`.")] = "deepseek",
    model_id: Annotated[str, typer.Option("--model-id")] = "deepseek-flash",
    base_url: Annotated[Optional[str], typer.Option("--base-url")] = None,
    api_key_env: Annotated[Optional[str], typer.Option("--api-key-env", help="Environment variable holding the model key.")] = None,
    budget_usd: Annotated[float, typer.Option("--budget-usd", help="Hard spend ceiling.")] = 1.0,
    input_price: Annotated[Optional[float], typer.Option("--input-usd-per-million")] = None,
    output_price: Annotated[Optional[float], typer.Option("--output-usd-per-million")] = None,
    style_note: Annotated[Optional[str], typer.Option("--style-note", help="Subject and term choices for this document.")] = None,
    layout_review: Annotated[str, typer.Option("--layout-review", help="all | flagged | off")] = "all",
    no_vision: Annotated[bool, typer.Option("--no-vision")] = False,
    estimate_only: Annotated[bool, typer.Option("--estimate-only", help="Capture and price only; no model call.")] = False,
    workers: Annotated[int, typer.Option(help="Parallel capture/render processes.")] = 2,
    concurrency: Annotated[int, typer.Option("--concurrency", help="Model requests in flight.")] = 8,
    wait: Annotated[bool, typer.Option("--wait/--no-wait", help="Wait for the job to finish.")] = True,
    out_dir: Annotated[Optional[Path], typer.Option("--out-dir", help="Copy the Chinese and bilingual PDFs here when done.")] = None,
    poll: Annotated[float, typer.Option(help="Seconds between status checks.")] = 10.0,
    server: Server = None, token: Token = None, jobs_root: JobsRoot = None,
) -> None:
    """Translate a PDF into Chinese (single-language and bilingual PDFs)."""
    options = {"provider": provider, "model_id": model_id, "base_url": base_url, "api_key_env": api_key_env,
               "budget_usd": budget_usd, "input_usd_per_million": input_price, "output_usd_per_million": output_price,
               "style_note": style_note, "layout_review": layout_review, "vision": not no_vision,
               "estimate_only": estimate_only, "workers": workers, "translation_concurrency": concurrency}
    options = {k: v for k, v in options.items() if v is not None}
    backend = _backend(server, token, jobs_root)
    is_url = source.startswith(("http://", "https://"))
    try:
        if server:
            job = backend.submit_url(source, options) if is_url else backend.submit_file(Path(source), options)
        else:
            validated = JobOptions.model_validate(options)
            pdf, name = fetch_pdf(source) if is_url else (Path(source).read_bytes(), Path(source).name)
            job = backend.submit(pdf, name, validated)
    except Exception as error:
        _print({"error": str(error)})
        raise typer.Exit(2)
    typer.echo(f"job {job['job_id']} {job['status']}", err=True)
    if not wait:
        return _print(job)
    job = _wait(backend, job["job_id"], poll)
    if out_dir and job["status"] == "succeeded":
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = Path(source).stem if not is_url else job["job_id"]
        copied = {}
        for name, label in (("mono", "zh"), ("dual", "zh-en")):
            target = out_dir / f"{stem}.{label}.pdf"
            if server:
                backend.download(job["job_id"], name, target)
            elif name in backend.files(job["job_id"]):
                shutil.copy2(backend.files(job["job_id"])[name], target)
            else:
                continue
            copied[name] = str(target)
        job["copied"] = copied
    _print(job)
    if job["status"] == "failed":
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


@jobs_app.command("fetch")
def jobs_fetch(job_id: str, name: Annotated[str, typer.Argument(help="mono | dual | acceptance | layout_review | content_review | log")],
               destination: Path, server: Server = None, token: Token = None, jobs_root: JobsRoot = None) -> None:
    """Save one of a job's files."""
    backend = _backend(server, token, jobs_root)
    if server:
        backend.download(job_id, name, destination)
    else:
        files = backend.files(job_id)
        if name not in files:
            _print({"error": f"{name} is not available; available: {sorted(files)}"})
            raise typer.Exit(1)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(files[name], destination)
    _print({"job_id": job_id, "name": name, "saved": str(destination)})


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
    app.command("serve")(serve_command)
    app.command("mcp")(mcp_command)
