"""The tools the pi agent calls: ``python -m pdf_translation.aios.tools <name>`` with JSON on stdin.

Each call acts on the job in ``PDFTRANS_JOB_DIR`` through its
:class:`~.workspace.Workspace`. Paid tools refuse once the job's budget is
spent; every change passes the quality gate or is refused with the reason.
Pages touched by fixes are recorded in ``aios/changed-pages.json`` so the
supervisor re-renders exactly those after the agent stops.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .workspace import load_workspace

UNIT_FIELDS = ("unit_id", "source_pages", "state", "source_text", "translated_text")


def _workspace():
    job_dir = Path(os.environ["PDFTRANS_JOB_DIR"])
    return load_workspace(job_dir)


def _editor() -> str:
    return f"aios:pi:{os.environ.get('PDFTRANS_AGENT_MODEL', 'agent')}"


def _record_pages(ws, pages: list[int], changed: bool) -> None:
    path = ws.aios_dir / "changed-pages.json"
    try:
        known = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        known = {"pages": [], "changed": False}
    known["pages"] = sorted(set(known["pages"]) | set(pages))
    known["changed"] = known["changed"] or changed
    path.write_text(json.dumps(known), encoding="utf-8")


def _paid_guard(ws) -> None:
    if ws.remaining() <= 0:
        raise SystemExit(json.dumps({"error": "the job's budget is used up; no paid call can be made",
                                     "spent_usd": ws.spent(), "budget_usd": ws.budget()}))


def problems(ws, _: dict) -> dict:
    report = ws.acceptance()
    try:
        tried = json.loads((ws.aios_dir / "tried.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        tried = []
    return {
        "format": ws.fmt,
        "verdict": report.get("verdict"),
        "major_problems": [{k: p.get(k) for k in ("kind", "category", "count", "detail", "sample_pages", "sample_units", "sample")
                            if p.get(k) is not None} for p in report.get("major_problems", [])],
        "minor_issues": [{k: m.get(k) for k in ("category", "needs_human", "unit_share", "threshold")}
                         for m in report.get("minor_issues", [])],
        "already_tried": tried,
        "spent_usd": ws.spent(), "budget_usd": ws.budget(),
    }


def units(ws, args: dict) -> dict:
    wanted_ids = set(args.get("unit_ids") or [])
    pages = set(args.get("pages") or [])
    states = set(args.get("states") or [])
    category = args.get("category")
    limit = max(1, min(int(args.get("limit") or 20), 50))
    from ..triage import unit_is_untranslated

    residual: dict[str, list[str]] = {}
    for item in ws.acceptance().get("residual_warnings") or []:
        label = item["category"] + (f" ({', '.join(item['terms'][:8])})" if item.get("terms") else "")
        residual.setdefault(item["unit_id"], []).append(f"{label}: unresolved warning in the acceptance report")
    found, matched = [], 0
    for row in ws.rows():
        issues = [f"{i.get('category')} ({i.get('severity')}): {i.get('explanation')}" for i in row.get("issues") or []]
        issues += [text for text in residual.get(row["unit_id"], []) if not any(text.split(":")[0].split(" (")[0] == i.split(" (")[0] for i in issues)]
        if row.get("translated_text") and unit_is_untranslated(row.get("source_text") or "", row["translated_text"]):
            issues.append("untranslated_unit (major): the translation is still mostly English")
        if wanted_ids and row["unit_id"] not in wanted_ids:
            continue
        if pages and not pages & set(row.get("source_pages") or []):
            continue
        if states and (row.get("state") or "not_started") not in states:
            continue
        if args.get("with_issues") and not issues and (row.get("state") or "") not in {"exhausted", "blocked"}:
            continue
        if category and not any(issue.startswith(category) for issue in issues):
            continue
        matched += 1
        if len(found) < limit:
            found.append({**{k: row.get(k) for k in UNIT_FIELDS}, "issues": issues})
    return {"matched": matched, "returned": len(found), "units": found}


def retranslate(ws, args: dict) -> dict:
    _paid_guard(ws)
    unit_ids = list(dict.fromkeys(args.get("unit_ids") or []))[:40]
    if not unit_ids:
        return {"error": "name the units (unit_ids)"}
    result = ws.retranslate(unit_ids, args.get("instruction") or "Translate the source again, faithfully.", editor=_editor())
    _record_pages(ws, result["pages"], bool(result["applied"]))
    return result


def fix_units(ws, args: dict) -> dict:
    fixes = args.get("fixes") or []
    if not fixes:
        return {"error": "give fixes"}
    result = ws.write_fixes(fixes, _editor())
    _record_pages(ws, result["pages"], bool(result["applied"]))
    return result


def review_pages(ws, args: dict) -> dict:
    _paid_guard(ws)
    result = ws.review_pages(sorted(set(args.get("pages") or []))[:5], editor=_editor())
    _record_pages(ws, result["pages"], bool(result["applied"]))
    return result


def render_and_check(ws, _: dict) -> dict:
    changed = ws.aios_dir / "changed-pages.json"
    try:
        pages = json.loads(changed.read_text(encoding="utf-8")).get("pages", [])
    except (OSError, ValueError):
        pages = []
    report = ws.render_and_accept(pages)
    # The pages are rendered now; the supervisor still needs to know the agent changed something.
    try:
        was_changed = json.loads(changed.read_text(encoding="utf-8")).get("changed", False)
    except (OSError, ValueError):
        was_changed = False
    changed.write_text(json.dumps({"pages": [], "changed": was_changed}), encoding="utf-8")
    return problems(ws, {}) | {"verdict": report.get("verdict")}


def build_log(ws, _: dict) -> dict:
    if ws.fmt != "latex":
        return {"error": "only LaTeX jobs have a build log"}
    report = json.loads((ws.work / "format-check.json").read_text(encoding="utf-8")) if (ws.work / "format-check.json").is_file() else {}
    tails = [p.get("log_tail") for p in report.get("problems", []) if p.get("log_tail")]
    return {"log_tail": tails[0] if tails else "the translated project compiled; there is no failure to show",
            "notes": report.get("notes", [])}


def finish(ws, args: dict) -> dict:
    (ws.aios_dir / "finish.json").write_text(json.dumps({"summary": str(args.get("summary") or "")}, ensure_ascii=False),
                                             encoding="utf-8")
    return {"ok": True, "note": "the service now renders the outputs and runs acceptance itself"}


TOOLS = {"problems": problems, "units": units, "retranslate": retranslate, "fix_units": fix_units,
         "review_pages": review_pages, "render_and_check": render_and_check, "build_log": build_log, "finish": finish}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] not in TOOLS:
        print(json.dumps({"error": f"unknown tool; use one of {sorted(TOOLS)}"}))
        return 2
    raw = sys.stdin.read()
    args = json.loads(raw) if raw.strip() else {}
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[3] / ".env", override=False)
    ws = _workspace()
    try:
        answer = TOOLS[argv[0]](ws, args)
    except SystemExit as stop:
        print(stop.code if isinstance(stop.code, str) else json.dumps({"error": "stopped"}))
        return 0
    except Exception as problem:  # the agent reads the reason and adapts; the process must not crash silently
        answer = {"error": f"{type(problem).__name__}: {problem}"[:1000]}
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(answer, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
