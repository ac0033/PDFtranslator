"""The repair loop: turn a version that failed acceptance into a usable one, or stop honestly.

Each round asks the decision node for one action per blocking problem,
carries the actions out through the workflow's nodes, hands whatever the rule
table routes to ``agent`` to the pi agent, then renders and runs acceptance
again. The loop stops when:

* the version is usable (acceptance, not the agent, says so);
* ``max_rounds`` rounds have run;
* the job's budget is spent;
* the wall-clock deadline passed;
* two rounds in a row did not reduce the blocking problems ("no progress");
* a round had nothing it could do (no applicable action and no agent).

Stopping without a usable version is not a failure of the job: the runner
publishes the files as a degraded delivery with the verdict and problems, so
the caller always gets a result and never a silently bad one.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from ..budget import BudgetExceeded
from ..jobspec import ModelChoice
from . import pi_agent
from .decision import Decision, decide
from .jev import JevClient
from .workspace import Workspace


@dataclass
class Limits:
    max_rounds: int = 3
    deadline_seconds: float = 45 * 60
    agent_deadline_seconds: float = 20 * 60
    agent_max_turns: int = 40


@dataclass
class RepairOutcome:
    usable: bool
    rounds: int
    stop_reason: str
    history: list[dict] = field(default_factory=list)
    agent_runs: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"usable": self.usable, "rounds": self.rounds, "stop_reason": self.stop_reason,
                "history": self.history, "agent_runs": self.agent_runs}


def weight(report: dict) -> int:
    """How much blocks delivery: problems, weighted by how many units or pages each covers."""
    return sum(max(1, int(problem.get("count") or 1)) for problem in report.get("major_problems", []))


def run_actions(ws: Workspace, decision: Decision) -> dict:
    """Carry out every non-agent action; returns what changed."""
    pages: set[int] = set()
    changed = False
    done = []
    for action in decision.actions:
        entry = action.describe()
        try:
            if action.name == "retranslate_units" and action.unit_ids:
                result = ws.retranslate(action.unit_ids, action.instruction or "Translate the source again, faithfully.")
                pages.update(result["pages"])
                changed |= bool(result["applied"])
                entry.update(applied=len(result["applied"]), refused=len(result["refused"]), cost_usd=result["cost_usd"])
            elif action.name == "review_units" and action.unit_ids:
                result = ws.review_units(action.unit_ids, action.instruction)
                pages.update(result["pages"])
                changed |= bool(result["applied"])
                entry.update(applied=len(result["applied"]), refused=len(result["refused"]), cost_usd=result["cost_usd"])
            elif action.name == "review_pages" and action.pages:
                result = ws.review_pages(action.pages[:10])
                pages.update(result["pages"])
                changed |= bool(result["applied"])
                entry.update(applied=len(result["applied"]), cost_usd=result["cost_usd"])
            elif action.name == "resume_translation":
                ws.resume_translation()
                changed = True
                entry.update(result="resumed")
            elif action.name == "rerender":
                pages.update(action.pages)
                changed = True
                entry.update(result="scheduled")
            elif action.name == "agent":
                entry.update(result="handed to the agent")
            else:
                entry.update(result="nothing to act on")
        except BudgetExceeded as problem:
            entry.update(result=f"stopped: {problem}")
            done.append(entry)
            return {"pages": sorted(pages), "changed": changed, "actions": done, "budget_exhausted": True}
        except Exception as problem:  # one failing action must not end the others
            entry.update(result=f"error: {type(problem).__name__}: {problem}"[:300])
        done.append(entry)
    return {"pages": sorted(pages), "changed": changed, "actions": done, "budget_exhausted": False}


def agent_task(ws: Workspace, decision: Decision, *, purpose: str) -> str:
    problems = [action.describe() | {"detail": action.problem.get("detail")} for action in decision.actions]
    if purpose == "optimize":
        opening = ("This job's translation already PASSED acceptance and has been delivered as the usable version. "
                   "You are building the optimised version: fix real remaining defects (warnings that are true "
                   "problems), never make a passing translation worse, and stop early if nothing is worth changing.")
    else:
        opening = ("This job's translation FAILED acceptance. Remove the problems that block delivery; the automatic "
                   "actions listed below have already been tried in this round.")
    return (f"{opening}\n\nJob format: {ws.fmt}. Budget left: USD {ws.remaining():.4f}.\n"
            f"Problems and what the decision node did:\n{json.dumps(problems, ensure_ascii=False, indent=1)}\n\n"
            "Start with the `problems` tool, work through the units, check with `render_and_check`, and end with `finish`.")


def run_agent(ws: Workspace, choice: ModelChoice, decision: Decision, limits: Limits, *, purpose: str,
              max_turns: int | None = None, budget: float | None = None) -> dict:
    ok, reason = pi_agent.availability()
    if not ok:
        return {"ran": False, "reason": reason}
    (ws.aios_dir / "finish.json").unlink(missing_ok=True)
    (ws.aios_dir / "tried.json").write_text(json.dumps([a.describe() for a in decision.actions], ensure_ascii=False),
                                             encoding="utf-8")
    session = pi_agent.PiSession(ws.job_dir, choice, budget_left=min(ws.remaining(), budget if budget is not None else ws.remaining()),
                                 deadline_seconds=limits.agent_deadline_seconds,
                                 max_turns=max_turns or limits.agent_max_turns)
    result = session.run(agent_task(ws, decision, purpose=purpose))
    pi_agent.book(ws.store(), ws.config.run_id, result)
    try:
        changed = json.loads((ws.aios_dir / "changed-pages.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        changed = {"pages": [], "changed": False}
    (ws.aios_dir / "changed-pages.json").unlink(missing_ok=True)
    ws.note("agent", purpose=purpose, **{k: v for k, v in result.as_dict().items() if k != "errors"})
    return {"ran": True, **result.as_dict(), "changed": changed.get("changed", False), "pages": changed.get("pages", [])}


def repair(ws: Workspace, *, agent_choice: ModelChoice, jev: JevClient | None, emit, limits: Limits | None = None) -> RepairOutcome:
    limits = limits or Limits()
    started = time.monotonic()
    report = ws.acceptance()
    outcome = RepairOutcome(False, 0, "running")
    previous = weight(report)
    stalls = 0
    for round_no in range(1, limits.max_rounds + 1):
        decision = decide(report, ws.rows(), fmt=ws.fmt, jev=jev)
        if decision.usable:
            outcome.usable, outcome.stop_reason = True, "usable"
            break
        if ws.remaining() <= 0:
            outcome.stop_reason = "budget"
            break
        if time.monotonic() - started > limits.deadline_seconds:
            outcome.stop_reason = "deadline"
            break
        outcome.rounds = round_no
        emit("repair.round", {"round": round_no, "blocking_weight": previous,
                              "actions": [action.describe() for action in decision.actions]})
        done = run_actions(ws, decision)
        pages = set(done["pages"])
        changed = done["changed"]
        agent_result = None
        if decision.for_agent() or not changed:
            if not done["budget_exhausted"] and ws.remaining() > 0:
                agent_result = run_agent(ws, agent_choice, decision, limits, purpose="repair")
                outcome.agent_runs.append(agent_result)
                if agent_result.get("ran"):
                    pages.update(agent_result.get("pages", []))
                    changed = changed or agent_result.get("changed", False)
        if changed:
            report = ws.render_and_accept(sorted(pages))
        current = weight(report)
        outcome.history.append({"round": round_no, "actions": done["actions"], "agent": agent_result,
                                "blocking_weight_before": previous, "blocking_weight_after": current,
                                "verdict": report.get("verdict")})
        if report.get("verdict") != "not_deliverable":
            outcome.usable, outcome.stop_reason = True, "usable"
            break
        if done["budget_exhausted"]:
            outcome.stop_reason = "budget"
            break
        if not changed:
            outcome.stop_reason = "nothing_to_do" if not (agent_result and agent_result.get("ran")) else "agent_made_no_change"
            break
        stalls = stalls + 1 if current >= previous else 0
        previous = current
        if stalls >= 2:
            outcome.stop_reason = "no_progress"
            break
    else:
        outcome.stop_reason = "max_rounds"
    if outcome.stop_reason == "running":
        outcome.stop_reason = "usable" if outcome.usable else "stopped"
    return outcome
