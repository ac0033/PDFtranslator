"""Run one translation job from start to delivery: the process the job service starts.

    route -> workflow -> decision -> [repair loop] -> usable (or degraded) -> [optimise] -> finished

* **route**: a PDF with a verifiable arXiv source takes the LaTeX route
  (:mod:`pdf_translation.formats.arxiv`); any other PDF the PDF workflow
  (``scripts/deliver_pdf.py``); LaTeX, HTML, Markdown, DOCX and EPUB their
  structured-source workflow (:mod:`pdf_translation.formats.runner`).
* **decision**: acceptance decides whether the version is usable; if not, the
  AI OS repair loop (:mod:`pdf_translation.aios.supervisor`) works on it
  within the job's rounds, budget and time.
* **delivery**: a usable version is published at once (``delivery.usable``),
  then - unless the caller turned it off - the optimiser builds the optimised
  version and publishes it only if it is not worse (``delivery.optimized``).
  A version the repair loop could not make usable is published as
  ``degraded``, with its verdict and problems: the caller always gets the
  files and an honest verdict.

``--mode revise`` renders again after a caller wrote fixes into the job and
publishes the result as the ``revised`` version when it is not worse.

State goes to ``<job>/state.json`` and numbered events to ``<job>/events.jsonl``.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

from pdf_translation import jobstate  # noqa: E402
from pdf_translation.acceptance import NOT_DELIVERABLE  # noqa: E402
from pdf_translation.aios.jev import JevClient  # noqa: E402
from pdf_translation.aios.optimize import compare, optimize  # noqa: E402
from pdf_translation.aios.supervisor import Limits, repair  # noqa: E402
from pdf_translation.aios.workspace import load_workspace  # noqa: E402
from pdf_translation.config import load_config  # noqa: E402
from pdf_translation.formats import FormatError, arxiv, runner  # noqa: E402
from pdf_translation.jobspec import ModelChoice  # noqa: E402


class JobFailed(RuntimeError):
    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


class Job:
    def __init__(self, job_dir: Path):
        self.dir = job_dir
        self.meta = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
        self.request = self.meta.get("request", {})
        self.state = jobstate.read_state(job_dir) or {}
        self.started = time.monotonic()

    # --- state and events -----------------------------------------------------------------

    def save(self, **changes) -> None:
        self.state.update(changes)
        jobstate.write_state(self.dir, self.state)

    def emit(self, kind: str, data: dict | None = None) -> None:
        jobstate.append_event(self.dir, kind, data)

    def stage(self, name: str) -> None:
        self.save(stage=name)
        self.emit("stage.changed", {"stage": name, "phase": self.state.get("phase")})
        print(f"[{time.strftime('%H:%M:%S')}] {self.state.get('phase')}: {name}", flush=True)

    def cost(self, ws) -> dict:
        try:
            with ws.store().connect() as con:
                rows = con.execute("SELECT source, SUM(COALESCE(cost_usd,0)) c FROM ledger WHERE run_id=? GROUP BY source",
                                   (ws.config.run_id,)).fetchall()
            by_source = {row["source"] or "translation": round(row["c"] or 0.0, 6) for row in rows}
        except Exception:
            by_source = {}
        groups = {"translation": 0.0, "layout_review": 0.0, "repair": 0.0, "agent": 0.0, "optimize": 0.0}
        for source, value in by_source.items():
            if source in {"aios_agent"}:
                groups["agent"] += value
            elif source in {"aios_optimize"}:
                groups["optimize"] += value
            elif source in {"agent_assist"}:
                groups["repair"] += value
            elif source and "layout" in source:
                groups["layout_review"] += value
            else:
                groups["translation"] += value
        return {**{k: round(v, 6) for k, v in groups.items()}, "total": ws.spent(), "budget": ws.budget()}

    # --- publishing ---------------------------------------------------------------------

    def publish(self, ws, version: str, report: dict, *, note: str | None = None, recommend: bool = True) -> dict:
        manifest = jobstate.publish(self.dir, version, ws.outputs(), report, note=note)
        changes = {"delivery": version}
        if recommend:
            changes["recommended"] = version
        self.save(**changes, cost_usd=self.cost(ws))
        self.emit(f"delivery.{version}", {"version": version, "verdict": report.get("verdict"),
                                          "files": sorted(manifest["files"]), "recommended": recommend, "note": note})
        return manifest

    # --- the run --------------------------------------------------------------------------

    def route(self) -> str:
        fmt = self.meta.get("format", "pdf")
        if fmt != "pdf" or self.request.get("estimate_only"):
            return fmt
        self.stage("arxiv-check")
        decision = arxiv.try_source(self.dir / self.meta["source_file"], self.dir / "work",
                                    allow_network=self.request.get("allow_network", True))
        self.save(arxiv=decision.as_dict())
        return "latex" if decision.use_source else "pdf"

    def run(self) -> int:
        self.save(status="running", phase="translate", stage="starting", error=None, pid=os.getpid())
        route = self.state.get("route") or self.route()
        self.save(route=route)
        (self.dir / "aios").mkdir(exist_ok=True)
        ws = load_workspace(self.dir)
        ws.log = self.dir / "aios" / "aios.jsonl"
        if route == "pdf":
            self.stage("workflow")
            code = ws.run_pdf(estimate_only=bool(self.request.get("estimate_only")))
            if self.request.get("estimate_only"):
                return self.finish_estimate(ws)
            if not (ws.work / "acceptance-report.json").is_file():
                raise JobFailed(f"the PDF workflow stopped before acceptance (exit {code}); see the log",
                                "workflow_failed")
        else:
            self.stage("parse")
            try:
                ws.loaded()
            except FormatError as problem:
                raise JobFailed(str(problem), "source_unreadable") from problem
            self.stage("translate")
            try:
                runner.translate(ws.loaded(), ws.work, ws.config, ws.runs_root, style_note=ws._style_note(),
                                 execute_paid=not self.request.get("estimate_only"))
            except runner.BudgetTooLow as problem:
                raise JobFailed(str(problem), "budget_too_low") from problem
            if self.request.get("estimate_only"):
                return self.finish_estimate(ws)
            self.stage("render-and-check")
            ws.render_and_accept()
        return self.decide_and_deliver(ws)

    def decide_and_deliver(self, ws) -> int:
        agent = ModelChoice.model_validate(self.meta["agent"])
        jev = JevClient.from_environment()
        report = ws.acceptance()
        self.save(first_verdict=report.get("verdict"), jev="on" if jev else "off (no TYPESAFE_API_KEY)")
        if report.get("verdict") == NOT_DELIVERABLE:
            self.save(phase="repair")
            self.stage("repair")
            outcome = repair(ws, agent_choice=agent, jev=jev, emit=self.emit, limits=self.limits())
            self.save(aios={"repair": outcome.as_dict(), "model": agent.label()})
            report = ws.acceptance()
        if report.get("verdict") == NOT_DELIVERABLE:
            self.publish(ws, "degraded", report,
                         note="the automatic repair could not make this version deliverable; read major_problems")
            self.save(status="succeeded", phase="done", stage=None, cost_usd=self.cost(ws))
            self.emit("job.finished", {"delivery": "degraded", "verdict": report.get("verdict")})
            return 0
        self.publish(ws, "usable", report)
        if (self.request.get("deliver") or {}).get("optimized", True):
            self.save(phase="optimize")
            self.stage("optimize")
            try:
                result = optimize(ws, agent_choice=agent, jev=jev, limits=self.limits())
            except Exception as problem:  # the usable version is already out; a failed optimisation must not undo that
                traceback.print_exc()
                result = {"changed": False, "keep": False, "reason": f"optimisation failed: {type(problem).__name__}: {problem}"[:500]}
                ws.render_and_accept()
            aios = self.state.get("aios") or {"model": agent.label()}
            aios["optimize"] = result
            self.save(aios=aios)
            final = ws.acceptance()
            note = result.get("reason")
            if result.get("keep", True):
                self.publish(ws, "optimized", final, note=note)
            else:
                self.save(recommended="usable", delivery="usable")
                self.emit("delivery.optimized", {"version": "optimized", "published": False, "note": note})
        self.save(status="succeeded", phase="done", stage=None, cost_usd=self.cost(ws))
        self.emit("job.finished", {"delivery": self.state.get("delivery"), "recommended": self.state.get("recommended")})
        return 0

    def finish_estimate(self, ws) -> int:
        estimate = json.loads((ws.work / "estimate.json").read_text(encoding="utf-8")) if (ws.work / "estimate.json").is_file() else {}
        self.save(status="estimated", phase="done", stage=None,
                  estimate={"estimated_max_cost_usd": estimate.get("estimated_max_cost_usd"),
                            "units": estimate.get("unit_count"), "budget_usd": ws.budget()})
        self.emit("job.finished", {"estimated_max_cost_usd": estimate.get("estimated_max_cost_usd")})
        return 0

    def revise(self) -> int:
        self.save(status="running", phase="revise", stage="render", error=None, pid=os.getpid())
        ws = load_workspace(self.dir)
        before = self.recommended_report()
        pages = self.meta.get("revise_pages") or []
        report = ws.render_and_accept(pages)
        verdict = compare(before, report) if before else None
        keep = verdict is None or verdict.keep or before.get("verdict") == NOT_DELIVERABLE
        self.publish(ws, "revised", report, note=verdict.reason if verdict else None, recommend=keep)
        self.save(status="succeeded", phase="done", stage=None, cost_usd=self.cost(ws))
        self.emit("job.finished", {"delivery": "revised", "recommended": self.state.get("recommended")})
        return 0

    def recommended_report(self) -> dict | None:
        entry = jobstate.recommended(self.dir)
        if not entry:
            return None
        path = self.dir / jobstate.DELIVERIES / entry["version"] / "acceptance-report.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None

    def limits(self) -> Limits:
        return Limits(max_rounds=int(os.environ.get("PDFTRANS_REPAIR_ROUNDS", 3)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--job-dir", type=Path, required=True)
    parser.add_argument("--mode", choices=("run", "revise"), default="run")
    args = parser.parse_args(argv)
    load_dotenv(ROOT / ".env", override=False)
    job = Job(args.job_dir.resolve())
    try:
        return job.revise() if args.mode == "revise" else job.run()
    except JobFailed as failure:
        job.save(status="failed", stage=None, error={"code": failure.code, "message": str(failure)})
        job.emit("job.failed", {"code": failure.code, "message": str(failure)})
        return 1
    except Exception as problem:
        detail = "".join(traceback.format_exception_only(type(problem), problem)).strip()
        traceback.print_exc()
        if jobstate.deliveries(job.dir):
            # A version is already published: the caller keeps it, and learns what went wrong afterwards.
            job.save(status="succeeded", phase="done", stage=None,
                     warning={"code": "internal_error", "message": detail[:1000]})
            job.emit("job.finished", {"delivery": job.state.get("delivery"), "warning": detail[:300]})
            return 0
        job.save(status="failed", stage=None, error={"code": "internal_error", "message": detail[:1000]})
        job.emit("job.failed", {"code": "internal_error", "message": detail[:1000]})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
