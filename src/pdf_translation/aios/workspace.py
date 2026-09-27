"""One job's working state, seen the same way whether it is a PDF or a structured source.

The supervisor, the optimiser and the pi agent's tools all act on a job
through this class, so each workflow node is one method here: render and
accept, retranslate units, review units or pages, resume translation. PDF
jobs run the existing stage scripts in their own processes (BabelDOC needs
the isolation); structured sources call :mod:`pdf_translation.formats.runner`.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ..acceptance import BLOCKING_STATES, refresh_from_store, load_review_queue
from ..config import RunConfig, load_config
from ..storage import StateStore

ROOT = Path(__file__).resolve().parents[3]
UNIT_BATCH = 40


@dataclass
class Workspace:
    job_dir: Path
    fmt: str  # the route actually taken: "pdf" or a structured format
    source: Path  # the PDF, the uploaded source file, or a LaTeX project directory
    config: RunConfig
    runs_root: Path
    source_name: str = "source"
    bilingual: bool = True
    layout_review: str = "all"
    vision: bool = True
    workers: int = 2
    main_file: str | None = None
    log: Path | None = None
    _loaded: object | None = field(default=None, repr=False)

    # --- basics -----------------------------------------------------------------------

    @property
    def work(self) -> Path:
        return self.job_dir / "work"

    @property
    def aios_dir(self) -> Path:
        path = self.job_dir / "aios"
        path.mkdir(exist_ok=True)
        return path

    def store(self) -> StateStore:
        return StateStore(self.runs_root / self.config.run_id / "state.sqlite")

    def spent(self) -> float:
        try:
            return round(self.store().spent(self.config.run_id), 6)
        except Exception:
            return 0.0

    def budget(self) -> float:
        return self.config.budget_usd

    def remaining(self) -> float:
        return max(0.0, self.budget() - self.spent())

    def note(self, event: str, **data) -> None:
        if self.log is None:
            return
        from ..jobstate import now

        with self.log.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"at": now(), "event": event, **data}, ensure_ascii=False) + "\n")

    def acceptance(self) -> dict:
        try:
            return json.loads((self.work / "acceptance-report.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"verdict": "not_deliverable", "major_problems": [
                {"kind": "missing_acceptance", "severity": "major", "count": 1,
                 "detail": "no acceptance report was produced"}], "minor_issues": []}

    def rows(self) -> list[dict]:
        """Every unit's source, current translation, state and warnings (from the run store)."""
        path = self.work / "content-review.jsonl"
        if not path.is_file():
            return []
        rows, _ = refresh_from_store(load_review_queue(path), self.store(), self.config.run_id)
        return rows

    # --- the workflow's nodes -------------------------------------------------------------

    def loaded(self):
        if self._loaded is None:
            from ..formats import runner

            self._loaded = runner.load(self.fmt, self.source, self.work, source_name=self.source_name,
                                       main_file=self.main_file)
        return self._loaded

    def render_and_accept(self, pages: list[int] | None = None) -> dict:
        """Write the outputs from the run store and run acceptance again. Free."""
        if self.fmt == "pdf":
            for page in pages or []:
                for label in ("mono", "dual"):
                    (self.work / f"page-{page:04d}" / f"source.zh.{label}.pdf").unlink(missing_ok=True)
            self.run_pdf(resume_from="render", layout_review="off")
            return self.acceptance()
        from ..formats import runner
        from ..triage import ReviewLedger

        return runner.render_and_accept(self.loaded(), self.work, self.config, self.runs_root, bilingual=self.bilingual,
                                        ledger=ReviewLedger([]))

    def run_pdf(self, *, resume_from: str | None = None, layout_review: str | None = None,
                estimate_only: bool = False) -> int:
        """The PDF workflow (``scripts/deliver_pdf.py``) in its own process."""
        command = [sys.executable, "-m", "scripts.deliver_pdf", "--source", str(self.source),
                   "--config", str(self.job_dir / "config.toml"), "--output", str(self.work),
                   "--output-prefix", "zh", "--workers", str(self.workers),
                   "--layout-review", layout_review or self.layout_review]
        if not self.vision:
            command.append("--no-vision")
        if estimate_only:
            command.append("--estimate-only")
        if (self.job_dir / "style.txt").is_file():
            command += ["--style-note-file", str(self.job_dir / "style.txt")]
        if resume_from:
            command += ["--resume-from", resume_from]
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        with (self.job_dir / "runner.log").open("ab") as log:
            return subprocess.run(command, cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=log,
                                  stderr=subprocess.STDOUT).returncode

    def resume_translation(self) -> dict:
        """Send the units that were never translated (prepared/not started), reusing every paid result."""
        if self.fmt == "pdf":
            code = self.run_pdf(layout_review="off")
            return {"exit_code": code}
        from ..formats import runner

        self.store().requeue_provider_failures(self.config.run_id)
        return runner.translate(self.loaded(), self.work, self.config, self.runs_root,
                                style_note=self._style_note(), execute_paid=True)

    def _style_note(self) -> str | None:
        path = self.job_dir / "style.txt"
        return path.read_text(encoding="utf-8") if path.is_file() else None

    def _assist_context(self):
        from ..assist import JobContext

        source = self.source if self.fmt == "pdf" else self.job_dir / "source.bin"
        return JobContext.load(self.config, self.work, source, self.runs_root, log=self.job_dir / "assist.jsonl")

    def _client(self):
        from ..llm import config_client

        return config_client(self.config)

    def apply(self, candidates: list[dict], editor: str) -> dict:
        """Apply the candidates that carry an applicable fix, one at a time so one refusal spares the rest."""
        from ..local_fixes import apply_fixes

        applied, refused, pages = [], [], set()
        for candidate in candidates:
            fix = candidate.get("fix")
            if not fix:
                refused.append({"unit_id": candidate.get("unit_id"), "why": candidate.get("issues") or "no applicable fix"})
                continue
            try:
                pages.update(apply_fixes([fix], editor, capture=self.work, config=self.config, runs_root=self.runs_root))
                applied.append(fix["unit_id"])
            except (KeyError, ValueError, RuntimeError) as problem:
                refused.append({"unit_id": fix["unit_id"], "why": str(problem)[:300]})
        return {"applied": applied, "refused": refused, "pages": sorted(pages)}

    def retranslate(self, unit_ids: list[str], instruction: str, *, editor: str = "aios") -> dict:
        """The job's model translates the units again; results that pass the gate are written. Paid."""
        from ..assist import Meter, retranslate

        results = {"applied": [], "refused": [], "pages": [], "cost_usd": 0.0}
        for start in range(0, len(unit_ids), UNIT_BATCH):
            batch = unit_ids[start:start + UNIT_BATCH]
            context = self._assist_context()
            meter = Meter(context, self.budget())
            answer = retranslate(context, meter, self._client(), batch, instruction=instruction)
            results["cost_usd"] += meter.cost_usd
            outcome = self.apply(answer["candidates"], editor)
            for key in ("applied", "refused"):
                results[key].extend(outcome[key])
            results["pages"] = sorted(set(results["pages"]) | set(outcome["pages"]))
            results["refused"].extend({"unit_id": uid, "why": why} for uid, why in answer.get("errors", {}).items())
        results["cost_usd"] = round(results["cost_usd"], 6)
        self.note("retranslate", units=len(unit_ids), applied=len(results["applied"]), cost_usd=results["cost_usd"])
        return results

    def review_units(self, unit_ids: list[str], instruction: str | None = None, *, editor: str = "aios") -> dict:
        """The job's model checks the units for real errors and proposes fixes; applicable fixes are written. Paid."""
        from ..assist import Meter, review_units

        results = {"applied": [], "refused": [], "pages": [], "cost_usd": 0.0, "problems_found": 0}
        for start in range(0, len(unit_ids), UNIT_BATCH):
            batch = unit_ids[start:start + UNIT_BATCH]
            context = self._assist_context()
            meter = Meter(context, self.budget())
            answer = review_units(context, meter, self._client(), batch, instruction=instruction)
            results["cost_usd"] += meter.cost_usd
            problems = answer.get("problems", [])
            results["problems_found"] += len(problems)
            outcome = self.apply([p for p in problems if p.get("severity") != "minor"], editor)
            for key in ("applied", "refused"):
                results[key].extend(outcome[key])
            results["pages"] = sorted(set(results["pages"]) | set(outcome["pages"]))
        results["cost_usd"] = round(results["cost_usd"], 6)
        self.note("review_units", units=len(unit_ids), applied=len(results["applied"]), cost_usd=results["cost_usd"])
        return results

    def review_pages(self, pages: list[int], *, editor: str = "aios") -> dict:
        """PDF only: compare rendered pages with the source images and apply the fixes that pass. Paid."""
        if self.fmt != "pdf":
            raise ValueError("page review is only available for PDF jobs")
        from ..assist import Meter, review_page_layout

        results = {"applied": [], "refused": [], "pages": [], "cost_usd": 0.0}
        for start in range(0, len(pages), 5):
            context = self._assist_context()
            meter = Meter(context, self.budget())
            answer = review_page_layout(context, meter, self._client(), pages[start:start + 5], vision=self.vision)
            results["cost_usd"] += meter.cost_usd
            outcome = self.apply(answer.get("problems", answer.get("candidates", [])), editor)
            for key in ("applied", "refused"):
                results[key].extend(outcome[key])
            results["pages"] = sorted(set(results["pages"]) | set(outcome["pages"]))
        results["cost_usd"] = round(results["cost_usd"], 6)
        return results

    def write_fixes(self, fixes: list[dict], editor: str) -> dict:
        """Translations written by an agent itself: old text and hash are filled in from the store."""
        rows = {row["unit_id"]: row for row in self.rows()}
        candidates = []
        from ..assist import candidate as make_candidate

        context = self._assist_context()
        for fix in fixes:
            uid = fix.get("unit_id")
            if uid not in rows:
                candidates.append({"unit_id": uid, "issues": ["unknown unit"]})
                continue
            candidates.append(make_candidate(context, uid, str(fix.get("new_translated_text", "")),
                                             str(fix.get("reason") or "agent fix")))
        return self.apply(candidates, editor)

    # --- outputs --------------------------------------------------------------------------

    def outputs(self) -> dict[str, Path | None]:
        """The files a version consists of, as the workflow left them in ``work``."""
        if self.fmt == "pdf":
            books = {"mono": self.work / "source.zh.zh.mono.pdf", "dual": self.work / "source.zh.zh.dual.pdf"}
            extra = {"layout_review": self.work / "layout-review.json"}
        else:
            report = self.acceptance()
            books = {name: Path(path) if path else None for name, path in (report.get("outputs") or {}).items()}
            extra = {}
        return {**books, "content_review": self.work / "content-review.jsonl", **extra}

    def unit_ids_where(self, predicate) -> list[str]:
        return [row["unit_id"] for row in self.rows() if predicate(row)]

    def blocking_units(self) -> list[str]:
        return self.unit_ids_where(lambda row: (row.get("state") or "not_started") in BLOCKING_STATES
                                   or not (row.get("translated_text") or "").strip())


def load_workspace(job_dir: Path, runs_root: Path | None = None) -> Workspace:
    """The workspace of an existing job, from its ``job.json`` and ``state.json``."""
    from ..jobstate import read_state

    meta = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
    state = read_state(job_dir) or {}
    request = meta.get("request", {})
    route = state.get("route") or meta.get("format", "pdf")
    if route == "latex" and (job_dir / "work" / "arxiv-source").is_dir():
        source = job_dir / "work" / "arxiv-source"
    else:
        source = job_dir / meta.get("source_file", "source.pdf")
    return Workspace(
        job_dir=job_dir, fmt=route, source=source, config=load_config(job_dir / "config.toml"),
        runs_root=runs_root or Path(meta.get("runs_root") or ROOT / "runs"), source_name=meta.get("source_name", "source"),
        bilingual=(request.get("deliver") or {}).get("bilingual", True), layout_review=request.get("layout_review", "all"),
        vision=request.get("vision", True), workers=request.get("workers", 2), main_file=request.get("main_file"),
        log=job_dir / "aios" / "aios.jsonl" if (job_dir / "aios").is_dir() else None,
    )
