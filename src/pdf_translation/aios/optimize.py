"""The optimised version: improve a version that already passed, and keep it only if it is not worse.

The usable version is published first and never touched again. Optimisation
then works on the job's run store:

1. The remaining warnings are gathered per category. With Jev, only the
   units whose warning it judges real are worked on.
2. The job's model reviews those units and corrects real errors only (it is
   told a passing translation must not get worse); fixes pass the quality gate.
3. If warnings remain and the AI OS is available, the pi agent gets the job
   with the explicit note that the version already passed.
4. The outputs are written again and graded, and compared with the usable
   version: no blocking problem, and no more unresolved warnings than before.

A version that fails the comparison is discarded: the run store is restored
from the copy taken before optimising (the money spent is booked again, so
the ledger stays true) and the outputs are written again from it.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field

from ..acceptance import DELIVERABLE, DELIVERABLE_WITH_KNOWN_ISSUES, NOT_DELIVERABLE
from ..jobspec import ModelChoice
from ..schemas import Usage
from .decision import Action, Decision, _real_units, units_with_warning
from .jev import JevClient
from .supervisor import Limits, run_agent
from .workspace import Workspace

VERDICT_RANK = {NOT_DELIVERABLE: 0, DELIVERABLE_WITH_KNOWN_ISSUES: 1, DELIVERABLE: 2}
#: Warning categories where a second look can find a real error in a passing translation.
IMPROVABLE = ("number", "url", "terminology", "protected_marker_order", "leaked_markup", "retained_english",
              "untranslated_unit")
MAX_UNITS = 120
#: What the optimisation agent may spend: this share of the job's spend so far, but at least the floor.
AGENT_BUDGET_SHARE = 1.0
AGENT_BUDGET_FLOOR = 0.05
OPTIMIZE_INSTRUCTION = (
    "This translation already passed acceptance. Report only real errors a reader would be hurt by "
    "(a wrong or missing number, lost meaning, a wrong term, English left untranslated); an empty list "
    "is the right answer when there are none. Never make a correct translation worse.")


@dataclass
class Comparison:
    keep: bool
    reason: str
    before: dict = field(default_factory=dict)
    after: dict = field(default_factory=dict)


def residual(report: dict) -> int:
    return sum(item.get("needs_human", 0) for item in report.get("minor_issues", []))


def compare(before: dict, after: dict) -> Comparison:
    summary = lambda r: {"verdict": r.get("verdict"), "major": len(r.get("major_problems", [])), "residual": residual(r)}
    b, a = summary(before), summary(after)
    if a["major"]:
        return Comparison(False, "the optimised version has blocking problems", b, a)
    if VERDICT_RANK.get(a["verdict"], 0) < VERDICT_RANK.get(b["verdict"], 0):
        return Comparison(False, "the optimised version grades lower", b, a)
    if a["residual"] > b["residual"]:
        return Comparison(False, "the optimised version has more unresolved warnings", b, a)
    return Comparison(True, "not worse than the usable version" if a == b else "better than the usable version", b, a)


def targets(ws: Workspace, report: dict, jev: JevClient | None) -> dict[str, list[str]]:
    rows = ws.rows()
    rows_by_id = {row["unit_id"]: row for row in rows}
    found: dict[str, list[str]] = {}
    categories = [item["category"] for item in report.get("minor_issues", []) if item.get("needs_human")]
    for category in categories:
        if category not in IMPROVABLE:
            continue
        units = [uid for uid in units_with_warning(category, rows, report) if uid in rows_by_id]
        units, _ = _real_units(units, category, rows_by_id, jev)
        if units:
            found[category] = units
    return found


def optimize(ws: Workspace, *, agent_choice: ModelChoice, jev: JevClient | None, limits: Limits | None = None) -> dict:
    limits = limits or Limits()
    before = ws.acceptance()
    work = targets(ws, before, jev)
    if not work:
        return {"changed": False, "keep": True, "reason": "nothing left to improve", "comparison": None}
    store_path = ws.runs_root / ws.config.run_id / "state.sqlite"
    backup = ws.aios_dir / "state-before-optimize.sqlite"
    shutil.copy2(store_path, backup)
    spent_before = ws.spent()
    pages: set[int] = set()
    applied = 0
    actions = []
    unresolved: dict[str, list[str]] = {}
    for category, unit_ids in work.items():
        if ws.remaining() <= 0:
            break
        result = ws.review_units(unit_ids[:MAX_UNITS], OPTIMIZE_INSTRUCTION, editor="aios:optimize")
        pages.update(result["pages"])
        applied += len(result["applied"])
        refused = [item["unit_id"] for item in result["refused"] if item.get("unit_id")]
        if refused:
            unresolved[category] = refused
        actions.append({"category": category, "units": len(unit_ids), "real_problems_found": result["problems_found"],
                        "applied": len(result["applied"]), "cost_usd": result["cost_usd"]})
    agent = None
    # The agent is called only for real defects the review found but could not fix: a warning the
    # reviewing model judged harmless is not worth an agent's money, and the version already passed.
    agent_budget = min(ws.remaining(), max(AGENT_BUDGET_FLOOR, AGENT_BUDGET_SHARE * spent_before))
    if unresolved and agent_budget > 0:
        decision = Decision(True, before.get("verdict", ""), [
            Action("agent", {"kind": "unfixed_real_problem", "category": category,
                             "detail": f"the review found real problems in {len(ids)} units but its fixes were refused"},
                   ids) for category, ids in unresolved.items()])
        agent = run_agent(ws, agent_choice, decision, limits, purpose="optimize",
                          max_turns=min(15, limits.agent_max_turns), budget=agent_budget)
        if agent.get("ran"):
            pages.update(agent.get("pages", []))
            applied += 1 if agent.get("changed") else 0
    if not applied:
        backup.unlink(missing_ok=True)
        return {"changed": False, "keep": True, "reason": "the review found nothing worth changing",
                "actions": actions, "agent": agent, "comparison": None}
    after = ws.render_and_accept(sorted(pages))
    comparison = compare(before, after)
    if not comparison.keep:
        spent = ws.spent() - spent_before
        shutil.copy2(backup, store_path)
        if spent > 0:
            ws.store().record_usage(ws.config.run_id, "optimize-discarded",
                                    Usage(cost_usd=round(spent, 6), usage_source="provider"), "aios_optimize")
        ws.render_and_accept(sorted(pages))
    backup.unlink(missing_ok=True)
    return {"changed": True, "keep": comparison.keep, "reason": comparison.reason, "actions": actions, "agent": agent,
            "comparison": {"before": comparison.before, "after": comparison.after}}
