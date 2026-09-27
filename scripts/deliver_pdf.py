"""Translate one PDF end to end with one command, and time every stage.

    capture -> coverage -> translate -> render -> layout review -> acceptance

Each stage is the existing resumable script run in its own process, so a rerun
of the same command resumes from what is already on disk rather than
re-capturing, re-paying or re-rendering. Stage wall-clock times are written to
<capture>.timings.json beside the capture directory; that file is how "how long does a document take" gets
answered with a measurement instead of an estimate.

The config's budget_usd guards the spend: translation first prices the whole
document and makes no API call if the ceiling is above it. Layout review (a
model compares each rendered page with the original and repairs mismatches,
see pdf_translation.layout_review) spends from the same budget.

--resume-from render starts at rendering: after fixes were written into a
finished run, the pages whose output was removed are rendered again, the books
merged, and acceptance repeated; nothing is captured or sent to a model.

Speed settings, none of which change the output:
    --workers              parallel capture/render processes (each needs ~2 GB)
    translation_concurrency in the config: provider requests in flight
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time

import pymupdf

from pdf_translation.config import load_config

ROOT = Path(__file__).resolve().parent.parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--style-note-file", type=Path, default=None, help="Book-specific translation instruction (UTF-8).")
    parser.add_argument("--output", type=Path, default=None, help="Capture/render directory. Defaults to output/pdf/<run_id>.")
    parser.add_argument("--output-prefix", default="zh", help="Infix of the merged book filenames.")
    parser.add_argument("--workers", type=int, default=1, help="Parallel capture/render processes.")
    parser.add_argument("--layout-review", choices=("all", "flagged", "off"), default="all",
                        help="Review every rendered page with the model, only pages with automatic hints, or none.")
    parser.add_argument("--no-vision", action="store_true", help="Layout review sends text only, no page images.")
    parser.add_argument("--estimate-only", action="store_true",
                        help="Capture and price the translation, then stop; no model is called.")
    parser.add_argument("--resume-from", choices=("start", "render"), default="start",
                        help="render: skip capture and translation (after fixes were written into a finished run).")
    parser.add_argument("--runs-root", type=Path, default=ROOT / "runs",
                        help="Root holding <run_id>/state.sqlite (the run's ledger and translations).")
    parser.add_argument("--chunk-size", type=int, default=None,
                        help="Pages per process. Defaults to spreading the document evenly over --workers, at most 10.")
    return parser


def default_chunk_size(pages: int, workers: int) -> int:
    """Even spread over the workers, capped at the proven 10-page memory bound."""
    return max(1, min(10, math.ceil(pages / max(1, workers))))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config(args.config)
    capture = args.output or ROOT / "output/pdf" / config.run_id
    # The capture step refuses to merge into an existing directory, so nothing
    # of ours may live inside it before it runs: log and timings sit beside it.
    capture.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open(args.source) as document:
        pages = len(document)
    chunk = args.chunk_size or default_chunk_size(pages, args.workers)
    scratch = Path("tmp/capture-scratch") / config.run_id
    log = capture.parent / f"{capture.name}.deliver.log"
    timings_path = capture.parent / f"{capture.name}.timings.json"
    timings: dict = {"source": str(args.source), "pages": pages, "workers": args.workers, "chunk_size": chunk,
                     "translation_concurrency": config.translation_concurrency, "stages": {}}
    if args.resume_from != "start":
        # Keep the earlier stages' record; the stages run now overwrite their own entries.
        earlier = _read(timings_path) or {}
        timings["stages"] = earlier.get("stages", {})
        timings["resumed_from"] = args.resume_from
        timings.pop("error", None)
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    parallel = ["--workers", str(args.workers)] if args.workers > 1 else []

    def save() -> None:
        timings["total_seconds"] = round(sum(s["seconds"] for s in timings["stages"].values()), 1)
        timings_path.write_text(json.dumps(timings, ensure_ascii=False, indent=2), encoding="utf-8")

    def stage(name: str, command: list[str], *, fatal: bool = True) -> int:
        print(f"[{name}] ...", flush=True)
        timings["current_stage"] = name
        save()
        started = time.perf_counter()
        with log.open("a", encoding="utf-8") as stream:
            stream.write(f"\n=== {name}: {' '.join(command)}\n")
            stream.flush()
            code = subprocess.run([sys.executable, *command], cwd=ROOT, env=env,
                                  stdout=stream, stderr=subprocess.STDOUT).returncode
        seconds = round(time.perf_counter() - started, 1)
        timings["stages"][name] = {"seconds": seconds, "exit_code": code}
        save()
        print(f"[{name}] {seconds}s exit={code}", flush=True)
        if code and fatal:
            raise StageFailed(f"{name} failed (exit {code}); see {log}")
        return code

    timings["status"] = "running"
    save()
    try:
        code = run_stages(args, config, capture, chunk, scratch, parallel, stage)
    except StageFailed as failure:
        timings.update(status="failed", error=str(failure), current_stage=None)
        save()
        print(json.dumps(timings, ensure_ascii=False, indent=2))
        return 1
    timings.update(current_stage=None, result=summarise(capture, config, args))
    timings["status"] = "estimated" if args.estimate_only else "succeeded"
    save()
    print(json.dumps(timings, ensure_ascii=False, indent=2))
    return code


class StageFailed(RuntimeError):
    pass


def run_stages(args, config, capture: Path, chunk: int, scratch: Path, parallel: list[str], stage) -> int:
    cli = ["-c", "from pdf_translation.cli import app; app()"]
    if args.resume_from == "render":
        return render_and_accept(args, config, capture, chunk, parallel, stage, cli)
    if (capture / "capture.report.json").is_file():
        # Captured by an earlier attempt (the merge writes this file last);
        # capturing again would refuse to overwrite it.
        print("[capture] already on disk", flush=True)
    else:
        stage("capture", ["scripts/capture_val_full.py", "--source", str(args.source), "--chunk-size", str(chunk), "--auto",
                          "--capture-root", str(scratch), "--output", str(capture), *parallel])
    stage("coverage", [*cli, "capture-coverage", "--source", str(args.source),
                       "--capture-map", str(capture / "capture-map.jsonl"),
                       "--output", str(capture / "capture-coverage.json")], fatal=False)
    translate = ["scripts/translate_val_usable_sample.py", "--capture", str(capture), "--source", str(args.source),
                 "--config", str(args.config), "--capture-root", str(scratch.parent), "--sample-name", config.run_id,
                 "--translate-only", "--runs-root", str(args.runs_root)]
    if not args.estimate_only:
        translate.append("--execute-paid")
    if args.style_note_file:
        translate += ["--style-note-file", str(args.style_note_file)]
    stage("estimate" if args.estimate_only else "translate", translate)
    if args.estimate_only:
        return 0
    return render_and_accept(args, config, capture, chunk, parallel, stage, cli)


def render_and_accept(args, config, capture: Path, chunk: int, parallel: list[str], stage, cli: list[str]) -> int:
    stage("render", ["scripts/render_val_full.py", "--auto", "--capture", str(capture), "--source", str(args.source),
                     "--config", str(args.config), "--output-prefix", args.output_prefix,
                     "--chunk-size", str(chunk), "--runs-root", str(args.runs_root), *parallel])
    if args.layout_review != "off":
        review = ["-m", "scripts.review_layout", "--capture", str(capture), "--config", str(args.config),
                  "--source", str(args.source), "--output-prefix", args.output_prefix, "--pages", args.layout_review,
                  "--workers", str(args.workers), "--execute-paid", "--runs-root", str(args.runs_root)]
        if args.no_vision:
            review.append("--no-vision")
        stage("layout-review", review, fatal=False)
    return stage("acceptance", [*cli, "acceptance-report", "--source", str(args.source), "--render-dir", str(capture),
                                "--run-id", config.run_id, "--output-prefix", args.output_prefix,
                                "--runs-root", str(args.runs_root)], fatal=False)


def _read(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def summarise(capture: Path, config, args) -> dict:
    """What a caller needs from a finished run: verdict, cost, where the books are."""
    stem = args.source.stem
    result: dict = {"output_dir": str(capture)}
    estimate = _read(capture / "estimate.json")
    if estimate:
        result["estimated_max_cost_usd"] = estimate.get("estimated_max_cost_usd")
        result["units"] = estimate.get("unit_count")
    if args.estimate_only:
        result["budget_usd"] = config.budget_usd
        return result
    books = {"mono": capture / f"{stem}.{args.output_prefix}.zh.mono.pdf",
             "dual": capture / f"{stem}.{args.output_prefix}.zh.dual.pdf"}
    result["books"] = {k: str(v) for k, v in books.items() if v.is_file()}
    acceptance = _read(capture / "acceptance-report.json")
    if acceptance:
        result["verdict"] = acceptance.get("verdict")
        result["major_problems"] = [p.get("detail") for p in acceptance.get("major_problems", [])]
    review = _read(capture / "layout-review.json")
    if review:
        result["layout_review"] = {k: review.get(k) for k in ("pages", "accepted", "rejected", "cost_usd", "stopped")}
    try:
        from pdf_translation.storage import StateStore
        result["cost_usd"] = round(StateStore(args.runs_root / config.run_id / "state.sqlite").spent(config.run_id), 6)
    except Exception:  # a failed run may have no store yet
        pass
    return result


if __name__ == "__main__":
    raise SystemExit(main())
