"""Drive the pi agent (https://pi.dev, MIT) over its RPC protocol as the job's AI OS.

pi is installed per project by ``pdftrans setup`` into ``agent/node_modules``
at the version pinned in ``agent/package.json``. It runs as a child process:

* isolated - its own agent directory inside the job, no user extensions,
  skills, prompt templates or context files, no session file;
* confined - only the tools of ``agent/pdftrans-tools.ts`` are enabled, so it
  cannot run shell commands or touch files outside what those tools do;
* bounded - this module, not the prompt, enforces the job's remaining budget
  (polling pi's own cost report), a wall-clock deadline and a turn limit, and
  aborts the agent when any is reached.

The model is the one the caller named (``agent`` in the job request). Its key
comes from the server's environment; when the caller's variable differs from
the one pi expects for that provider, the value is passed under pi's name in
the child's environment only.
"""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..jobspec import PI_PROVIDERS, ModelChoice, agent_key_variable
from ..llm import PRESET_ALIASES
from ..schemas import Usage

ROOT = Path(__file__).resolve().parents[3]
AGENT_DIR = ROOT / "agent"
EXTENSION = AGENT_DIR / "pdftrans-tools.ts"
SYSTEM_PROMPT = AGENT_DIR / "aios.md"
TOOLS = ("problems", "units", "retranslate", "fix_units", "review_pages", "render_and_check", "build_log", "finish")
MIN_NODE = (22, 19)
DEFAULT_DEADLINE = 30 * 60
DEFAULT_MAX_TURNS = 40


def pi_cli() -> Path | None:
    override = os.environ.get("PDFTRANS_PI_CLI")
    if override and Path(override).is_file():
        return Path(override)
    candidate = AGENT_DIR / "node_modules" / "@earendil-works" / "pi-coding-agent" / "dist" / "bundle" / "cli.js"
    return candidate if candidate.is_file() else None


def node_version() -> tuple[int, ...] | None:
    node = shutil.which("node")
    if not node:
        return None
    try:
        text = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception:  # an unusable node binary is the same as none
        return None
    match = re.match(r"v(\d+)\.(\d+)\.(\d+)", text)
    return tuple(int(part) for part in match.groups()) if match else None


def availability() -> tuple[bool, str]:
    """Whether the AI OS can run here, and if not, what to do about it."""
    version = node_version()
    if version is None:
        return False, "Node.js is not installed; install Node.js 22.19 or newer, then run `uv run pdftrans setup`"
    if version[:2] < MIN_NODE:
        return False, f"Node.js {'.'.join(map(str, version))} is too old; pi needs 22.19 or newer"
    if pi_cli() is None:
        return False, "the pi agent is not installed; run `uv run pdftrans setup`"
    return True, f"pi ready (Node.js {'.'.join(map(str, version))})"


@dataclass
class SessionResult:
    finished: bool
    stop_reason: str
    turns: int = 0
    tool_calls: int = 0
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    summary: str = ""
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def _pi_model(choice: ModelChoice) -> tuple[str, str, dict | None]:
    """(pi provider, model id, models.json content or None) for a caller's model."""
    name = PRESET_ALIASES.get(choice.provider, choice.provider)
    if choice.base_url:
        provider = re.sub(r"[^a-z0-9-]", "-", choice.provider.lower()) or "custom"
        api = "anthropic-messages" if choice.api_protocol == "anthropic" else "openai-completions"
        models = {"providers": {provider: {"baseUrl": choice.base_url, "api": api,
                                           "apiKey": f"${agent_key_variable(choice)}",
                                           "models": [{"id": choice.model}]}}}
        return provider, choice.model, models
    pi_name = PI_PROVIDERS.get(name, (name, ""))[0]
    return pi_name, choice.model, None


class PiSession:
    def __init__(self, job_dir: Path, choice: ModelChoice, *, budget_left: float, deadline_seconds: float = DEFAULT_DEADLINE,
                 max_turns: int = DEFAULT_MAX_TURNS, extra_env: dict[str, str] | None = None):
        self.job_dir = job_dir
        self.choice = choice
        self.budget_left = budget_left
        self.deadline_seconds = deadline_seconds
        self.max_turns = max_turns
        self.extra_env = extra_env or {}
        self.transcript = job_dir / "aios" / "pi-events.jsonl"

    def _command(self) -> tuple[list[str], dict[str, str]]:
        cli = pi_cli()
        if cli is None:
            raise RuntimeError(availability()[1])
        agent_home = self.job_dir / "aios" / "pi-home"
        agent_home.mkdir(parents=True, exist_ok=True)
        provider, model, models = _pi_model(self.choice)
        if models is not None:
            (agent_home / "models.json").write_text(json.dumps(models, indent=2), encoding="utf-8")
        command = [shutil.which("node") or "node", str(cli), "--mode", "rpc", "--no-session",
                   "--no-extensions", "--no-skills", "--no-prompt-templates", "--no-themes", "--no-context-files",
                   "--no-approve", "--offline", "-e", str(EXTENSION), "--tools", ",".join(TOOLS),
                   "--append-system-prompt", str(SYSTEM_PROMPT), "--provider", provider, "--model", model]
        if self.choice.reasoning_effort:
            command += ["--thinking", self.choice.reasoning_effort]
        env = {**os.environ, **self.extra_env, "PI_CODING_AGENT_DIR": str(agent_home), "PI_OFFLINE": "1",
               "PDFTRANS_JOB_DIR": str(self.job_dir), "PDFTRANS_PYTHON": sys.executable, "PDFTRANS_ROOT": str(ROOT),
               "PDFTRANS_AGENT_MODEL": self.choice.label()}
        variable = agent_key_variable(self.choice)
        expected = PI_PROVIDERS.get(PRESET_ALIASES.get(self.choice.provider, self.choice.provider), (None, None))[1]
        if expected and variable != expected and env.get(variable):
            env[expected] = env[variable]
        return command, env

    def run(self, task: str) -> SessionResult:
        command, env = self._command()
        process = subprocess.Popen(command, cwd=ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", bufsize=1)
        lines: queue.Queue = queue.Queue()

        def pump(stream, tag):
            for line in stream:
                lines.put((tag, line))
            lines.put((tag, None))

        threading.Thread(target=pump, args=(process.stdout, "out"), daemon=True).start()
        threading.Thread(target=pump, args=(process.stderr, "err"), daemon=True).start()
        result = SessionResult(False, "running")
        started = time.monotonic()
        stats_pending = False
        next_id = 0

        def send(payload: dict) -> None:
            nonlocal next_id
            next_id += 1
            payload.setdefault("id", f"c{next_id}")
            try:
                process.stdin.write(json.dumps(payload) + "\n")
                process.stdin.flush()
            except (OSError, ValueError):
                pass

        def stop(reason: str) -> None:
            if result.stop_reason == "running":
                result.stop_reason = reason
            send({"type": "abort"})

        send({"type": "prompt", "message": task})
        closed = 0
        settled = False
        with self.transcript.open("a", encoding="utf-8") as transcript:
            while closed < 2:
                if time.monotonic() - started > self.deadline_seconds and result.stop_reason == "running":
                    stop("deadline")
                    break
                try:
                    tag, line = lines.get(timeout=1.0)
                except queue.Empty:
                    continue
                if line is None:
                    closed += 1
                    continue
                if tag == "err":
                    if line.strip():
                        result.errors.append(line.strip()[:300])
                    continue
                transcript.write(line if line.endswith("\n") else line + "\n")
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                kind = record.get("type")
                if kind == "response":
                    if record.get("command") == "prompt" and not record.get("success"):
                        result.stop_reason = "prompt_refused"
                        result.errors.append(str(record.get("error"))[:300])
                        break
                    if record.get("command") == "get_session_stats" and record.get("success"):
                        stats_pending = False
                        self._take_stats(record.get("data") or {}, result)
                        if result.cost_usd > self.budget_left:
                            stop("budget")
                    continue
                if kind in {"tool_execution_end", "tool_execution_start"} and kind == "tool_execution_end":
                    result.tool_calls += 1
                    if (record.get("toolName") or record.get("tool_name") or record.get("name")) == "finish":
                        result.finished = True
                if kind == "turn_end":
                    result.turns += 1
                    if result.turns >= self.max_turns:
                        stop("turn_limit")
                if kind in {"turn_end", "message_end", "tool_execution_end"} and not stats_pending:
                    stats_pending = True
                    send({"type": "get_session_stats"})
                if kind == "agent_settled":
                    settled = True
                    break
        # Final accounting, then an orderly shutdown (closing stdin asks pi to exit).
        if process.poll() is None:
            send({"type": "get_session_stats", "id": "final"})
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    tag, line = lines.get(timeout=0.5)
                except queue.Empty:
                    continue
                if line is None:
                    break
                if tag == "out":
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if record.get("type") == "response" and record.get("command") == "get_session_stats":
                        self._take_stats(record.get("data") or {}, result)
                        break
            try:
                process.stdin.close()
            except OSError:
                pass
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
        if result.stop_reason == "running":
            result.stop_reason = "finished" if result.finished else ("settled" if settled else "exited")
        finish = self.job_dir / "aios" / "finish.json"
        if finish.is_file():
            try:
                result.summary = json.loads(finish.read_text(encoding="utf-8")).get("summary", "")
            except ValueError:
                pass
        return result

    def _take_stats(self, data: dict, result: SessionResult) -> None:
        tokens = data.get("tokens") or {}
        result.input_tokens = int(tokens.get("input") or 0) + int(tokens.get("cacheRead") or 0)
        result.output_tokens = int(tokens.get("output") or 0)
        cost = float(data.get("cost") or 0.0)
        if not cost:
            prices = self.choice.known_prices()
            if prices:
                cost = (result.input_tokens * prices[0] + result.output_tokens * prices[1]) / 1_000_000
        result.cost_usd = round(cost, 6)


def book(store, run_id: str, result: SessionResult) -> None:
    """Count the agent's own model calls against the job's budget, in the job's ledger."""
    if not (result.input_tokens or result.output_tokens or result.cost_usd):
        return
    store.record_usage(run_id, "aios-agent", Usage(input_tokens=result.input_tokens, output_tokens=result.output_tokens,
                                                   cost_usd=result.cost_usd, usage_source="provider"), "aios_agent")
