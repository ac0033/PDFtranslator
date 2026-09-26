from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

import pymupdf
import typer
from dotenv import load_dotenv

from .babeldoc_adapter import _write_content_review_queue, capture_pdf_sample, run_one_page_spike, run_smoke_document, validate_capture_identity
from .ab_experiment import MAX_COMBINED_AB_BUDGET_USD, approved_execution_configs, build_blind_review, prepare_ab_manifest
from .acceptance import NOT_DELIVERABLE, build_acceptance_report, captured_page_span, format_summary, load_review_queue, refresh_from_store
from .triage import DocumentProfile, ReviewLedger, triage_rows
from .llm import config_client
from .warning_review import (
    UNREVIEWABLE_CATEGORIES,
    LLMWarningReviewProvider,
    MockWarningReviewProvider,
    build_warning_cases,
    ledger_entries,
    review_warnings,
)
from .benchmark import build_samples
from .capture_coverage import reconcile_capture
from .config import load_config
from .analyzer import extract_term_candidates, validate_semantic_evidence
from .context_models import ContextBundle, GlossaryEntry, Scope, StyleRule, make_invalidation_report
from .context_router import build_context_packet
from .context_store import ContextStore
from .document_context import enrich_pdf_units_with_context
from .hashing import sha256_bytes, sha256_file, stable_hash
from .manifest import BenchmarkManifest, load_manifest, load_units
from .pdf_adapter import inspect_pdf
from .pdf_fidelity import audit_pdf_primitives
from .page_review import build_page_review
from .pipeline import DEFAULT_TRANSLATION_STYLE, estimate_units, make_request, run_units
from .schemas import TranslationUnit
from .storage import StateStore

app = typer.Typer(no_args_is_help=True, help="Auditable V1 PDF translation runtime")
load_dotenv()


def _load_captured_units(path: Path, *, enrich_context: bool = False) -> list[TranslationUnit]:
    units = [
        TranslationUnit.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return enrich_pdf_units_with_context(units) if enrich_context else units


@app.command()
def inspect(input: Annotated[Path, typer.Option("--input")], output: Annotated[Path, typer.Option("--output")] = Path("benchmark.json")) -> None:
    """Register an input PDF without sending its contents anywhere."""
    data = inspect_pdf(input)
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        document_uri = str(input.resolve().relative_to(output.parent.resolve()))
    except ValueError:
        raise typer.BadParameter("output manifest must be in the input PDF directory or one of its parents")
    manifest = BenchmarkManifest(document_uri=document_uri, document_sha256=data["sha256"], page_count=data["page_count"], notes=("sample mapping not supplied", "BabelDOC spike not run"))
    output.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    detail = output.with_suffix(".inspection.json")
    detail.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    typer.echo(f"wrote {output} and {detail}")


@app.command("select-samples")
def select_samples(
    manifest: Annotated[Path, typer.Option("--manifest")],
    smoke_page: Annotated[list[int], typer.Option("--smoke-page", help="One-based physical PDF page; repeat this option.")],
    seed: Annotated[int, typer.Option("--seed")] = 20260910,
    development_count: Annotated[int, typer.Option("--development-count")] = 12,
    frozen_count: Annotated[int, typer.Option("--frozen-count")] = 24,
) -> None:
    """Freeze disjoint smoke, development and frozen-test page sets."""
    current = load_manifest(manifest)
    if not smoke_page:
        raise typer.BadParameter("provide at least one --smoke-page")
    document = (manifest.parent / current.document_uri).resolve()
    zero_based = tuple(page - 1 for page in smoke_page)
    if any(page < 0 for page in zero_based):
        raise typer.BadParameter("--smoke-page values are one-based and must be positive")
    samples = build_samples(document, zero_based, development_count=development_count, frozen_count=frozen_count, seed=seed)
    sampling_note = "sample page_indices are zero-based physical PDF indices"
    updated = current.model_copy(
        update={
            "sampling_seed": seed,
            "samples": samples,
            "notes": tuple(note for note in current.notes if note not in {"sample mapping not supplied", sampling_note})
            + (sampling_note,),
        }
    )
    # Re-validate cross-set overlap and page bounds after model_copy.
    updated = BenchmarkManifest.model_validate(updated.model_dump(mode="json"))
    manifest.write_text(updated.model_dump_json(indent=2), encoding="utf-8")
    typer.echo(updated.model_dump_json(indent=2))


@app.command("babeldoc-spike")
def babeldoc_spike(
    manifest: Annotated[Path, typer.Option("--manifest")],
    page: Annotated[int, typer.Option("--page", help="One-based physical PDF page.")],
    output_dir: Annotated[Path, typer.Option("--output-dir")] = Path("reports/babeldoc-spike"),
) -> None:
    """Validate one-page interception and PDF write-back without a paid API."""
    current = load_manifest(manifest)
    if page < 1 or page > current.page_count:
        raise typer.BadParameter(f"--page must be between 1 and {current.page_count}")
    document = (manifest.parent / current.document_uri).resolve()
    typer.echo(json.dumps(run_one_page_spike(document, page - 1, output_dir), ensure_ascii=False, indent=2))


@app.command("pdf-smoke")
def pdf_smoke(
    manifest: Annotated[Path, typer.Option("--manifest")],
    config: Annotated[Path, typer.Option("--config")],
    output_dir: Annotated[Path, typer.Option("--output-dir")] = Path("output/pdf/v0.4-smoke"),
    runs_root: Annotated[Path, typer.Option("--runs-root")] = Path("runs"),
    capture_root: Annotated[Path, typer.Option("--capture-root")] = Path("tmp/pdfs/v0.4-capture"),
    execute_paid: Annotated[bool, typer.Option("--execute-paid", help="Required for non-mock providers after explicit budget approval.")] = False,
    reuse_capture: Annotated[bool, typer.Option("--reuse-capture", help="Reuse validated units.jsonl and capture-map.jsonl from output-dir.")] = False,
) -> None:
    """Run the frozen smoke pages through capture, runtime QA, and PDF write-back."""
    current = load_manifest(manifest)
    smoke = next((sample for sample in current.samples if sample.name == "smoke"), None)
    if smoke is None:
        raise typer.BadParameter("manifest has no frozen smoke sample")
    document = (manifest.parent / current.document_uri).resolve()
    report = run_smoke_document(
        document,
        smoke.page_indices,
        load_config(config),
        output_dir,
        runs_root,
        capture_root,
        execute_paid=execute_paid,
        reuse_capture=reuse_capture,
    )
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("pdf-development")
def pdf_development(
    manifest: Annotated[Path, typer.Option("--manifest")],
    config: Annotated[Path, typer.Option("--config")],
    output_dir: Annotated[Path, typer.Option("--output-dir")] = Path("output/pdf/v0.6-development"),
    runs_root: Annotated[Path, typer.Option("--runs-root")] = Path("runs"),
    capture_root: Annotated[Path, typer.Option("--capture-root")] = Path("tmp/pdfs/v0.6-development-capture"),
    execute_paid: Annotated[bool, typer.Option("--execute-paid", help="Required for non-mock providers after explicit budget approval.")] = False,
    reuse_capture: Annotated[bool, typer.Option("--reuse-capture")] = False,
) -> None:
    """Run only the frozen development pages; frozen-test pages remain inaccessible."""
    current = load_manifest(manifest)
    sample = next((item for item in current.samples if item.name == "development"), None)
    if sample is None:
        raise typer.BadParameter("manifest has no frozen development sample")
    document = (manifest.parent / current.document_uri).resolve()
    report = run_smoke_document(
        document,
        sample.page_indices,
        load_config(config),
        output_dir,
        runs_root,
        capture_root,
        execute_paid=execute_paid,
        reuse_capture=reuse_capture,
        sample_name="development",
    )
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("capture-development")
def capture_development(
    manifest: Annotated[Path, typer.Option("--manifest")],
    output_dir: Annotated[Path, typer.Option("--output-dir")] = Path("output/pdf/v0.6-development-capture"),
    capture_root: Annotated[Path, typer.Option("--capture-root")] = Path("tmp/pdfs/v0.6-development-capture"),
) -> None:
    """Capture development pages for offline context analysis; never opens frozen-test pages."""
    current = load_manifest(manifest)
    sample = next((item for item in current.samples if item.name == "development"), None)
    if sample is None:
        raise typer.BadParameter("manifest has no frozen development sample")
    document = (manifest.parent / current.document_uri).resolve()
    typer.echo(json.dumps(capture_pdf_sample(document, sample.page_indices, output_dir, capture_root), ensure_ascii=False, indent=2))


@app.command("pdf-translate")
def pdf_translate(
    input: Annotated[Path, typer.Option("--input")],
    config: Annotated[Path, typer.Option("--config")],
    output_root: Annotated[Path, typer.Option("--output-root")] = Path("output/pdf/production"),
    runs_root: Annotated[Path, typer.Option("--runs-root")] = Path("runs"),
    capture_root: Annotated[Path, typer.Option("--capture-root")] = Path("tmp/pdfs/production"),
    source_page: Annotated[list[int] | None, typer.Option("--source-page", help="One-based physical page; repeat to test a selection. Omit for the full PDF.")] = None,
    execute_paid: Annotated[bool, typer.Option("--execute-paid")] = False,
    reuse_capture: Annotated[bool, typer.Option("--reuse-capture")] = False,
    render_review_draft: Annotated[bool, typer.Option("--render-review-draft", help="Render complete, structurally safe translations for review even when semantic warnings remain. Never marks pages accepted.")] = False,
) -> None:
    """Run a context-first PDF translation without a benchmark A/B dependency."""
    if not input.is_file():
        raise typer.BadParameter("--input must be an existing PDF")
    with pymupdf.open(input) as source_pdf:
        page_count = len(source_pdf)
    pages = tuple(page - 1 for page in source_page) if source_page else tuple(range(page_count))
    if not pages or len(set(pages)) != len(pages) or any(page < 0 or page >= page_count for page in pages):
        raise typer.BadParameter("source pages must be unique and within the physical PDF page range")
    base_config = load_config(config)
    if base_config.context_engine != "v1" or not base_config.context_db:
        raise typer.BadParameter("pdf-translate requires V1 neighboring context and a context_db")
    source_hash = sha256_file(input)
    selection_hash = stable_hash(pages)[:8]
    run_config = base_config.model_copy(update={"run_id": f"{base_config.run_id}-{source_hash[:12]}-{selection_hash}"})
    work_dir = output_root / f"{input.stem}-{source_hash[:12]}" / selection_hash
    units_path = work_dir / "units.jsonl"
    if not reuse_capture:
        if units_path.exists():
            raise typer.BadParameter("capture already exists; use --reuse-capture or a different output root")
        capture_pdf_sample(input, pages, work_dir, capture_root / f"{input.stem}-{source_hash[:12]}" / selection_hash)
    units = _load_captured_units(units_path)
    records_by_page: dict[int, list[dict]] = {}
    for line in (work_dir / "capture-map.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            record = json.loads(line)
            records_by_page.setdefault(record["page_index"], []).append(record)
    validate_capture_identity(input, pages, units, records_by_page)
    context_store = ContextStore(Path(run_config.context_db))
    document_id = units[0].document_id
    if not any(rule.status == "approved" and rule.scope.kind == "document" and rule.scope.value == document_id for rule in context_store.styles()):
        context_store.put_style(
            StyleRule(
                rule_id=f"default-style-{document_id}",
                content=DEFAULT_TRANSLATION_STYLE,
                scope=Scope(kind="document", value=document_id),
                version="v0.7-default-style",
            )
        )
    report = run_smoke_document(
        input, pages, run_config, work_dir, runs_root,
        capture_root / f"{input.stem}-{source_hash[:12]}" / selection_hash,
        execute_paid=execute_paid,
        reuse_capture=True,
        sample_name="full" if len(pages) == page_count else "selection",
        render_review_draft=render_review_draft,
    )
    report["formal_use_ready"] = False
    report.setdefault("formal_use_blockers", ["translation or rendering has not passed all formal-use gates"])
    if report.get("mono_pdf"):
        fidelity_path = work_dir / "nontext.audit.json"
        fidelity = audit_pdf_primitives(input, work_dir / report["mono_pdf"], pages)
        fidelity_path.write_text(json.dumps(fidelity, ensure_ascii=False, indent=2), encoding="utf-8")
        report["nontext_audit"] = fidelity_path.name
        report["visual_primitives_passed"] = fidelity["visual_primitives_passed"]
        report["full_non_text_fidelity_verified"] = False
        report["formal_use_ready"] = False
        blockers = []
        if not fidelity["visual_primitives_passed"]:
            blockers.append("visual primitives, math-font glyphs or numeric/symbol glyphs differ from the source")
        if report["low_bbox_matches"]:
            blockers.append("low-confidence source text locations require review")
        blockers.extend((
            "unchanged glyphs outside recognized math fonts are not yet verified",
            "source/target meaning equivalence is not yet human-verified",
        ))
        report["formal_use_blockers"] = blockers
        if not fidelity["visual_primitives_passed"]:
            report["status"] = "visual_primitive_gate_blocked"
        (work_dir / "run.report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    page_review = build_page_review(input, work_dir)
    typer.echo(json.dumps({"output_dir": str(work_dir), **report, **page_review}, ensure_ascii=False, indent=2))


@app.command("review-pages")
def review_pages(
    input: Annotated[Path, typer.Option("--input")],
    output_dir: Annotated[Path, typer.Option("--output-dir")],
) -> None:
    """Rebuild saved page evidence without API calls."""
    typer.echo(json.dumps(build_page_review(input, output_dir), ensure_ascii=False, indent=2))


@app.command("review-warnings")
def review_warnings_command(
    source: Annotated[Path, typer.Option("--source", help="Original PDF (for the report header only).")],
    render_dir: Annotated[Path, typer.Option("--render-dir", help="Directory holding content-review.jsonl.")],
    config: Annotated[Path, typer.Option("--config", help="Run config supplying the model, prices and budget.")],
    run_id: Annotated[str, typer.Option("--run-id", help="Grade from this run's live store rather than the queue snapshot.")] = None,
    runs_root: Annotated[Path, typer.Option("--runs-root")] = Path("runs"),
    review_ledger: Annotated[Path, typer.Option("--review-ledger", help="Existing verdicts; new ones are merged in with --update-ledger.")] = None,
    output: Annotated[Path, typer.Option("--output", help="Where to write the review result.")] = None,
    execute_paid: Annotated[bool, typer.Option("--execute-paid", help="Actually call the provider. Without this only an estimate is produced.")] = False,
    update_ledger: Annotated[bool, typer.Option("--update-ledger", help="Append the model's verdicts to the ledger.")] = False,
    provider_name: Annotated[str, typer.Option("--provider", help="deepseek, openai or mock.")] = None,
    categories: Annotated[list[str], typer.Option("--category", help="Restrict to these warning categories; repeat. Defaults to the ones a model can answer.")] = None,
) -> None:
    """Ask a model whether each surviving warning is a real problem.

    Estimates by default and calls nothing. A model verdict is recorded as a
    model's: acceptance counts it separately and, unless explicitly trusted,
    leaves the warning on the human list.
    """
    queue = render_dir / "content-review.jsonl"
    if not queue.is_file():
        raise typer.BadParameter(f"content review queue not found: {queue}")
    cfg = load_config(config)
    rows = load_review_queue(queue)
    if run_id:
        rows, _stale = refresh_from_store(rows, StateStore(runs_root / run_id / "state.sqlite"), run_id)
    ledger = ReviewLedger.load(review_ledger)
    triage = triage_rows(rows, ledger=ledger, profile=DocumentProfile(rows))
    cases = build_warning_cases(rows, triage["residual"], categories=set(categories) if categories else None)

    chosen = (provider_name or cfg.provider).lower()
    if chosen == "mock":
        reviewer = MockWarningReviewProvider()
    else:
        try:
            # Another provider than the config's must not inherit its endpoint or key.
            override = {} if chosen == cfg.provider else {"provider": chosen, "base_url": None, "api_key_env": None, "api_protocol": None}
            reviewer = LLMWarningReviewProvider(config_client(cfg.model_copy(update=override)))
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc

    result = review_warnings(cases, reviewer, cfg, execute_paid=execute_paid)
    result["source"] = str(source)
    result["not_asked"] = {
        category: sum(1 for item in triage["residual"] if item["category"] == category)
        for category in sorted(UNREVIEWABLE_CATEGORIES)
        if any(item["category"] == category for item in triage["residual"])
    }
    result["why_not_asked"] = {k: v for k, v in UNREVIEWABLE_CATEGORIES.items() if k in result["not_asked"]}
    result["cases_by_category"] = {
        category: sum(1 for case in cases if case["category"] == category)
        for category in sorted({case["category"] for case in cases})
    }
    if execute_paid and update_ledger:
        if review_ledger is None:
            raise typer.BadParameter("--update-ledger needs --review-ledger")
        entries = ledger_entries(result, cases, reviewed_at=datetime.now(timezone.utc).date().isoformat())
        ReviewLedger(ledger.entries + entries, ledger.retained_terms).dump(review_ledger)
        result["ledger_entries_added"] = len(entries)

    destination = output or render_dir / "warning-review.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    typer.echo(json.dumps({k: v for k, v in result.items() if k not in ("verdicts", "failures")}, ensure_ascii=False, indent=2))
    if not execute_paid:
        typer.echo("estimate only: nothing was sent to a provider. Re-run with --execute-paid to spend it.")
    typer.echo(f"wrote {destination}")


@app.command("capture-coverage")
def capture_coverage(
    source: Annotated[Path, typer.Option("--source", help="Original PDF.")],
    capture_map: Annotated[Path, typer.Option("--capture-map", help="capture-map.jsonl produced by the capture step.")],
    output: Annotated[Path, typer.Option("--output")] = None,
) -> None:
    """Find body text the capture layer never delivered. No API calls.

    Text that never became a unit is invisible to every other check: it is not
    blocked, not flagged, not queued, and it renders in the source language.
    """
    # A slice of a book is reconciled over its own pages; without this every
    # page outside it reports as wholly uncaptured, which is true but useless.
    span = captured_page_span(capture_map.parent, source)
    report = reconcile_capture(source, capture_map, page_indices=tuple(page - 1 for page in span))
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    typer.echo(json.dumps({k: v for k, v in report.items() if k != "significant_pages"}, ensure_ascii=False, indent=2))
    for page in report["significant_pages"]:
        typer.echo(f"  page {page['page']}: {page['uncaptured_words']} words ({page['share']:.1%}) {page['words']}")


@app.command("refresh-review-queue")
def refresh_review_queue(
    capture: Annotated[Path, typer.Option("--capture", help="Capture directory holding units.jsonl and content-review.jsonl.")],
    run_id: Annotated[str, typer.Option("--run-id")],
    runs_root: Annotated[Path, typer.Option("--runs-root")] = Path("runs"),
) -> None:
    """Rewrite content-review.jsonl from the run store. No API calls.

    The queue is written once, when the run renders. Every later fix lands in
    the store, so the file on disk drifts away from the book that shipped. Run
    this after applying curated TM entries, or whenever acceptance reports the
    queue as stale.
    """
    units_path = capture / "units.jsonl"
    if not units_path.is_file():
        raise typer.BadParameter(f"captured units not found: {units_path}")
    units = _load_captured_units(units_path)
    store = StateStore(runs_root / run_id / "state.sqlite")
    destination = capture / "content-review.jsonl"
    before = destination.read_text(encoding="utf-8").splitlines() if destination.is_file() else []
    _write_content_review_queue(units, store, run_id, destination)
    after = destination.read_text(encoding="utf-8").splitlines()
    changed = sum(1 for old, new in zip(before, after) if old != new) + abs(len(after) - len(before))
    typer.echo(json.dumps({"path": str(destination), "units": len(after), "lines_changed": changed}, ensure_ascii=False))


@app.command("acceptance-report")
def acceptance_report(
    source: Annotated[Path, typer.Option("--source", help="Original PDF the translation was rendered from.")],
    render_dir: Annotated[Path, typer.Option("--render-dir", help="Directory holding page-NNNN output and the merged books.")],
    content_review: Annotated[Path, typer.Option("--content-review", help="content-review.jsonl produced by the run.")] = None,
    output_prefix: Annotated[str, typer.Option("--output-prefix", help="Infix of the merged book filenames.")] = "usable-deepseek",
    run_id: Annotated[str, typer.Option("--run-id", help="Grade from this run's live store instead of the review queue snapshot.")] = None,
    runs_root: Annotated[Path, typer.Option("--runs-root")] = Path("runs"),
    review_ledger: Annotated[Path, typer.Option("--review-ledger", help="Human verdicts that dismiss warnings while the text is unchanged.")] = None,
    capture_map: Annotated[Path, typer.Option("--capture-map", help="capture-map.jsonl, for reconciling what the capture layer delivered.")] = None,
    output: Annotated[Path, typer.Option("--output", help="Where to write acceptance-report.json.")] = None,
) -> None:
    """Decide whether a rendered translation can be delivered, without a human."""
    queue = content_review or render_dir / "content-review.jsonl"
    if not queue.is_file():
        raise typer.BadParameter(f"content review queue not found: {queue}")
    store = StateStore(runs_root / run_id / "state.sqlite") if run_id else None
    report = build_acceptance_report(
        source, render_dir, queue, output_prefix=output_prefix,
        store=store, run_id=run_id, ledger=ReviewLedger.load(review_ledger),
        capture_map=capture_map or render_dir / "capture-map.jsonl",
    )
    destination = output or render_dir / "acceptance-report.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    typer.echo(format_summary(report))
    typer.echo(f"wrote {destination}")
    if report["verdict"] == NOT_DELIVERABLE:
        raise typer.Exit(code=1)


@app.command("audit-nontext")
def audit_nontext(
    source: Annotated[Path, typer.Option("--source")],
    translated: Annotated[Path, typer.Option("--translated")],
    source_page: Annotated[list[int], typer.Option("--source-page", help="One-based source PDF page; repeat in translated PDF order.")],
    output: Annotated[Path, typer.Option("--output")],
) -> None:
    """Check unchanged visual primitives; report text-glyph fidelity as unverified."""
    if not source_page:
        raise typer.BadParameter("provide at least one --source-page")
    report = audit_pdf_primitives(source, translated, tuple(page - 1 for page in source_page))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command()
def estimate(manifest: Annotated[Path, typer.Option("--manifest")], config: Annotated[Path, typer.Option("--config")]) -> None:
    """Estimate tokens and maximum cost without calling a model."""
    m = load_manifest(manifest)
    units = load_units(manifest, m)
    c = load_config(config)
    from .pipeline import estimated_input_token_ceiling, make_request
    input_tokens = sum(estimated_input_token_ceiling(make_request(u, c)) for u in units)
    output_tokens = len(units) * c.max_output_tokens
    from .budget import estimate_cost
    typer.echo(json.dumps({"units": len(units), "estimated_input_tokens": input_tokens, "reserved_output_tokens": output_tokens, "estimated_max_cost_usd": estimate_cost(input_tokens, output_tokens, c.input_usd_per_million, c.output_usd_per_million), "is_estimate": True}, indent=2))


@app.command("estimate-captured")
def estimate_captured(
    units: Annotated[Path, typer.Option("--units", help="Captured TranslationUnit JSONL file.")],
    config: Annotated[Path, typer.Option("--config")],
) -> None:
    """Estimate a captured PDF workload without calling a model."""
    cfg = load_config(config)
    captured = _load_captured_units(units, enrich_context=cfg.context_engine == "v1")
    typer.echo(json.dumps(estimate_units(captured, cfg), ensure_ascii=False, indent=2))


@app.command()
def run(manifest: Annotated[Path, typer.Option("--manifest")], config: Annotated[Path, typer.Option("--config")], runs_root: Annotated[Path, typer.Option("--runs-root")] = Path("runs")) -> None:
    """Run or safely resume a manifest."""
    m = load_manifest(manifest)
    units = load_units(manifest, m)
    typer.echo(json.dumps(run_units(units, load_config(config), runs_root), ensure_ascii=False, indent=2))


@app.command()
def resume(manifest: Annotated[Path, typer.Option("--manifest")], config: Annotated[Path, typer.Option("--config")], runs_root: Annotated[Path, typer.Option("--runs-root")] = Path("runs")) -> None:
    """Alias of run; hash checks prevent resuming changed inputs."""
    run(manifest, config, runs_root)


@app.command()
def report(run_id: Annotated[str, typer.Option("--run-id")], runs_root: Annotated[Path, typer.Option("--runs-root")] = Path("runs")) -> None:
    typer.echo(json.dumps(StateStore(runs_root / run_id / "state.sqlite").summary(run_id), ensure_ascii=False, indent=2))


@app.command("retry-exhausted")
def retry_exhausted(
    run_id: Annotated[str, typer.Option("--run-id")],
    runs_root: Annotated[Path, typer.Option("--runs-root")] = Path("runs"),
) -> None:
    """Let units that exhausted their attempts be translated again. No API calls.

    A unit is pinned as exhausted so a rerun does not pay for the same rejection
    twice, and reruns skip it for good. That is right while the rules stay the
    same and wrong the moment one is corrected: the verdict was about a rule
    that no longer exists, but the unit stays untranslated and its pages stay
    unrendered. Run this after fixing a check, then rerun the translation.
    """
    units = StateStore(runs_root / run_id / "state.sqlite").clear_exhausted(run_id)
    typer.echo(json.dumps({"run_id": run_id, "cleared": len(units), "units": units}, ensure_ascii=False))


@app.command()
def review(run_id: Annotated[str, typer.Option("--run-id")], runs_root: Annotated[Path, typer.Option("--runs-root")] = Path("runs")) -> None:
    """Show every unit-level quality decision and its evidence."""
    typer.echo(json.dumps(StateStore(runs_root / run_id / "state.sqlite").quality_reports(run_id), ensure_ascii=False, indent=2))


@app.command()
def render(run_id: Annotated[str, typer.Option("--run-id")]) -> None:
    """Guard the still-unverified BabelDOC write-back boundary."""
    raise typer.BadParameter(f"run {run_id} cannot render: BabelDOC one-page injection/write-back spike has not been completed")


@app.command()
def demo(output_dir: Path = Path("demo")) -> None:
    """Create a deterministic offline manifest with protected content."""
    output_dir.mkdir(parents=True, exist_ok=True)
    document = output_dir / "demo.txt"
    document.write_text("offline fixture", encoding="utf-8")
    text = "The value is 42. See https://example.com."
    unit = TranslationUnit(unit_id="demo-u1", document_id="demo-doc", ir_revision="demo-v1", unit_type="paragraph", source_lang="en", target_lang="zh-CN", source_text=text, source_hash=stable_hash(text))
    units = output_dir / "units.jsonl"
    units.write_text(unit.model_dump_json() + "\n", encoding="utf-8")
    manifest = BenchmarkManifest(document_uri="demo.txt", document_sha256=sha256_bytes(document.read_bytes()), page_count=0, units_uri="units.jsonl", notes=("offline contract fixture",))
    (output_dir / "manifest.json").write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    typer.echo(f"wrote {output_dir / 'manifest.json'}")


@app.command("context-init")
def context_init(
    output: Annotated[Path, typer.Option("--output")] = Path("data/context-v1.sqlite"),
    document_id: Annotated[str, typer.Option("--document-id")] = "demo-doc",
) -> None:
    """Create a V1 context store with reviewed starter style rules."""
    store = ContextStore(output)
    scope = Scope(kind="document", value=document_id)
    rules = (
        StyleRule(rule_id="faithfulness", content="忠实保留否定、量词、条件和概率程度；不扩写作者未给出的解释。", scope=scope, priority=10, version="style-v1"),
        StyleRule(rule_id="textbook-zh", content="使用自然、准确、克制的简体中文教材表达。", scope=scope, priority=20, version="style-v1"),
    )
    for rule in rules:
        store.put_style(rule)
    typer.echo(json.dumps({"context_db": str(output), "document_id": document_id, "approved_style_rules": len(rules), "glossary_entries": 0, "note": "glossary remains empty until source evidence and human approval are available"}, ensure_ascii=False, indent=2))


@app.command("context-init-captured")
def context_init_captured(
    units: Annotated[Path, typer.Option("--units")],
    output: Annotated[Path, typer.Option("--output")] = Path("data/context-v0.6.sqlite"),
) -> None:
    """Initialize reviewed document-level style rules from captured PDF identity."""
    captured = _load_captured_units(units)
    document_ids = {unit.document_id for unit in captured}
    if len(document_ids) != 1:
        raise typer.BadParameter("captured units must belong to exactly one document")
    context_init(output=output, document_id=next(iter(document_ids)))


@app.command("ab-context-init")
def ab_context_init(
    units: Annotated[Path, typer.Option("--units")] = Path("output/pdf/v0.6-development-capture/units.jsonl"),
    output: Annotated[Path, typer.Option("--output")] = Path("data/context-v0.6-ab.sqlite"),
) -> None:
    """Create a clean A/B context store with the exact fixed-arm style text."""
    if output.exists():
        raise typer.BadParameter("A/B context store already exists; use a new path to avoid stale context")
    captured = _load_captured_units(units)
    document_ids = {unit.document_id for unit in captured}
    if len(document_ids) != 1:
        raise typer.BadParameter("captured units must belong to exactly one document")
    store = ContextStore(output)
    rule = StyleRule(
        rule_id="ab-common-style",
        content=DEFAULT_TRANSLATION_STYLE,
        scope=Scope(kind="document", value=next(iter(document_ids))),
        priority=10,
        version="v0.6-ab-style",
    )
    store.put_style(rule)
    typer.echo(json.dumps({"context_db": str(output), "document_id": rule.scope.value, "approved_style_rules": 1, "glossary_entries": 0, "semantic_entries": 0, "translation_memory_entries": 0}, ensure_ascii=False, indent=2))


@app.command("terms")
def terms_command(
    manifest: Annotated[Path, typer.Option("--manifest")],
    output: Annotated[Path, typer.Option("--output")] = Path("reports/term-candidates.json"),
    minimum_occurrences: Annotated[int, typer.Option("--minimum-occurrences")] = 2,
) -> None:
    """Extract review candidates; never approves terminology automatically."""
    m = load_manifest(manifest)
    units = load_units(manifest, m)
    candidates = extract_term_candidates(units, minimum_occurrences)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps([item.model_dump(mode="json") for item in candidates], ensure_ascii=False, indent=2), encoding="utf-8")
    typer.echo(f"wrote {len(candidates)} candidates to {output}; approval status was not changed")


@app.command("context-import")
def context_import(
    manifest: Annotated[Path, typer.Option("--manifest")],
    input: Annotated[Path, typer.Option("--input")],
    context_db: Annotated[Path, typer.Option("--context-db")],
) -> None:
    """Import a reviewed context bundle after validating source evidence."""
    m = load_manifest(manifest)
    units = load_units(manifest, m)
    bundle = ContextBundle.model_validate_json(input.read_text(encoding="utf-8"))
    validate_semantic_evidence(list(bundle.semantics), {unit.unit_id for unit in units})
    store = ContextStore(context_db)
    for entry in bundle.glossary:
        store.put_glossary(entry)
    for rule in bundle.styles:
        store.put_style(rule)
    for entry in bundle.semantics:
        store.put_semantic(entry)
    for entry in bundle.translation_memory:
        store.put_tm(entry)
    typer.echo(json.dumps({"glossary": len(bundle.glossary), "styles": len(bundle.styles), "semantics": len(bundle.semantics), "translation_memory": len(bundle.translation_memory)}, indent=2))


@app.command("context-inspect")
def context_inspect(
    manifest: Annotated[Path, typer.Option("--manifest")],
    config: Annotated[Path, typer.Option("--config")],
) -> None:
    """Build V1 packets and print selection traces without calling a model."""
    m = load_manifest(manifest)
    units = load_units(manifest, m)
    cfg = load_config(config)
    if cfg.context_engine != "v1" or not cfg.context_db:
        raise typer.BadParameter("config must enable context_engine='v1' and set context_db")
    store = ContextStore(Path(cfg.context_db))
    glossary, styles, semantics = store.glossary(), store.styles(), store.semantics()
    units_by_id = {unit.unit_id: unit for unit in units}
    glossary_snapshot = stable_hash([entry for entry in glossary if entry.status == "approved"])
    style_snapshot = stable_hash([rule for rule in styles if rule.status == "approved"])
    output = []
    for unit in units:
        packet, build = build_context_packet(unit, glossary, styles, semantics, units_by_id, cfg.context_token_budget, glossary_snapshot, style_snapshot)
        output.append({"unit_id": unit.unit_id, "token_count": packet.token_count, "token_budget": packet.token_budget, "memory_snapshot_id": packet.memory_snapshot_id, "items": [item.model_dump(mode="json") for item in packet.items], "traces": [trace.model_dump(mode="json") for trace in build.traces]})
    typer.echo(json.dumps(output, ensure_ascii=False, indent=2))


@app.command()
def ablate(
    manifest: Annotated[Path, typer.Option("--manifest")],
    config: Annotated[Path, typer.Option("--config")],
    output: Annotated[Path | None, typer.Option("--output")] = None,
) -> None:
    """Compare V0 fixed context and V1 selected context without quality claims."""
    m = load_manifest(manifest)
    units = load_units(manifest, m)
    cfg = load_config(config)
    if not cfg.context_db:
        raise typer.BadParameter("context_db is required")
    store = ContextStore(Path(cfg.context_db))
    units_by_id = {unit.unit_id: unit for unit in units}
    glossary, styles, semantics = store.glossary(), store.styles(), store.semantics()
    rows = []
    for unit in units:
        base = make_request(unit, cfg.model_copy(update={"context_engine": "v0-fixed", "context_db": None}))
        packet, build = build_context_packet(unit, glossary, styles, semantics, units_by_id, cfg.context_token_budget, stable_hash(glossary), stable_hash(styles))
        rows.append({"unit_id": unit.unit_id, "v0_context_tokens": base.context.token_count, "v1_context_tokens": packet.token_count, "v1_selected_items": len(packet.items), "v1_trimmed_items": sum(not trace.selected for trace in build.traces), "quality_comparison": "not_run"})
    report = {"schema_version": "1.0", "rows": rows, "warning": "Token counts are conservative estimates; this report does not claim a quality or cost improvement."}
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
        typer.echo(f"wrote {output}")
    else:
        typer.echo(rendered)


@app.command("ablate-captured")
def ablate_captured(
    units: Annotated[Path, typer.Option("--units")],
    config: Annotated[Path, typer.Option("--config")],
    output: Annotated[Path, typer.Option("--output")] = Path("reports/v0.6-context-ablation.json"),
) -> None:
    """Compare fixed and routed context for captured PDF units without model calls."""
    captured = _load_captured_units(units, enrich_context=True)
    cfg = load_config(config)
    if cfg.context_engine != "v1" or not cfg.context_db:
        raise typer.BadParameter("config must enable context_engine='v1' and set context_db")
    store = ContextStore(Path(cfg.context_db))
    glossary, styles, semantics = store.glossary(), store.styles(), store.semantics()
    validate_semantic_evidence(semantics, {unit.unit_id for unit in captured})
    units_by_id = {unit.unit_id: unit for unit in captured}
    glossary_snapshot = stable_hash([entry for entry in glossary if entry.status == "approved"])
    style_snapshot = stable_hash([rule for rule in styles if rule.status == "approved"])
    rows = []
    for unit in captured:
        base = make_request(unit, cfg.model_copy(update={"context_engine": "v0-fixed", "context_db": None}))
        packet, build = build_context_packet(
            unit,
            glossary,
            styles,
            semantics,
            units_by_id,
            cfg.context_token_budget,
            glossary_snapshot,
            style_snapshot,
        )
        rows.append(
            {
                "unit_id": unit.unit_id,
                "section_path": list(unit.section_path),
                "neighbor_ids": list(unit.neighbor_ids),
                "v0_context_tokens": base.context.token_count,
                "v1_context_tokens": packet.token_count,
                "selected_item_ids": [item.item_id for item in packet.items],
                "trimmed_item_ids": [trace.item_id for trace in build.traces if not trace.selected],
                "quality_comparison": "not_run",
            }
        )
    report = {
        "schema_version": "1.0",
        "source": str(units),
        "unit_count": len(captured),
        "context_linked_units": sum(bool(unit.neighbor_ids) for unit in captured),
        "section_scoped_units": sum(bool(unit.section_path) for unit in captured),
        "v0_context_tokens": sum(row["v0_context_tokens"] for row in rows),
        "v1_context_tokens": sum(row["v1_context_tokens"] for row in rows),
        "rows": rows,
        "warning": "Offline structural ablation only; it does not claim a translation-quality improvement.",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    typer.echo(f"wrote {output}")


@app.command("ab-prepare-development")
def ab_prepare_development(
    units: Annotated[Path, typer.Option("--units")] = Path("output/pdf/v0.6-development-capture/units.jsonl"),
    fixed_config: Annotated[Path, typer.Option("--fixed-config")] = Path("configs/v0.6.ab.fixed.deepseek.toml"),
    context_config: Annotated[Path, typer.Option("--context-config")] = Path("configs/v0.6.ab.context.deepseek.toml"),
    output: Annotated[Path, typer.Option("--output")] = Path("reports/v0.6_ab_manifest.json"),
    cny_per_usd: Annotated[float, typer.Option("--cny-per-usd")] = 7.5,
    planned_budget_usd_per_arm: Annotated[float, typer.Option("--planned-budget-usd-per-arm")] = 0.10,
) -> None:
    """Freeze and price the development A/B experiment without model calls."""
    captured = _load_captured_units(units, enrich_context=True)
    report = prepare_ab_manifest(
        units,
        captured,
        load_config(fixed_config),
        load_config(context_config),
        cny_per_usd=cny_per_usd,
        planned_budget_usd_per_arm=planned_budget_usd_per_arm,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("ab-run-development")
def ab_run_development(
    units: Annotated[Path, typer.Option("--units")] = Path("output/pdf/v0.6-development-capture/units.jsonl"),
    fixed_config: Annotated[Path, typer.Option("--fixed-config")] = Path("configs/v0.6.ab.fixed.deepseek.toml"),
    context_config: Annotated[Path, typer.Option("--context-config")] = Path("configs/v0.6.ab.context.deepseek.toml"),
    runs_root: Annotated[Path, typer.Option("--runs-root")] = Path("runs"),
    execute_paid: Annotated[bool, typer.Option("--execute-paid", help="Required after a separate explicit spend approval.")] = False,
    approved_combined_budget_usd: Annotated[float, typer.Option("--approved-combined-budget-usd")] = 0.0,
) -> None:
    """Run the frozen A/B pair under one explicit combined hard budget."""
    captured = _load_captured_units(units, enrich_context=True)
    fixed_base = load_config(fixed_config)
    context_base = load_config(context_config)
    preparation = prepare_ab_manifest(units, captured, fixed_base, context_base)
    if not execute_paid:
        typer.echo(json.dumps({"status": "paid_approval_required", "api_calls": 0, "preparation": preparation}, ensure_ascii=False, indent=2))
        return
    try:
        fixed, context = approved_execution_configs(fixed_base, context_base, approved_combined_budget_usd)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    per_arm = approved_combined_budget_usd / 2
    fixed_estimate = estimate_units(captured, fixed)
    context_estimate = estimate_units(captured, context)
    if fixed_estimate["estimated_max_cost_usd"] > per_arm or context_estimate["estimated_max_cost_usd"] > per_arm:
        raise typer.BadParameter("one-pass estimate exceeds the equal per-arm budget")
    fixed_summary = run_units(captured, fixed, runs_root)
    fixed_done = fixed_summary.get("states", {}).get("accepted", 0) + fixed_summary.get("states", {}).get("needs_review", 0)
    if fixed_done != len(captured):
        raise RuntimeError("fixed arm did not fully pass; context arm was not started")
    context_summary = run_units(captured, context, runs_root)
    combined_cost = fixed_summary["cost_usd"] + context_summary["cost_usd"]
    if combined_cost > approved_combined_budget_usd:
        raise RuntimeError("combined A/B cost exceeded the approved hard budget")
    typer.echo(json.dumps({"status": "completed", "approved_combined_budget_usd": approved_combined_budget_usd, "maximum_supported_budget_usd": MAX_COMBINED_AB_BUDGET_USD, "fixed": fixed_summary, "context": context_summary, "combined_cost_usd": combined_cost}, ensure_ascii=False, indent=2))


@app.command("ab-export-blind-review")
def ab_export_blind_review(
    units: Annotated[Path, typer.Option("--units")] = Path("output/pdf/v0.6-development-capture/units.jsonl"),
    fixed_run_id: Annotated[str, typer.Option("--fixed-run-id")] = "v0.6-ab-fixed-deepseek-r5",
    context_run_id: Annotated[str, typer.Option("--context-run-id")] = "v0.6-ab-context-deepseek-r5",
    runs_root: Annotated[Path, typer.Option("--runs-root")] = Path("runs"),
    review_output: Annotated[Path, typer.Option("--review-output")] = Path("reports/v0.6_ab_blind_review.json"),
    key_output: Annotated[Path, typer.Option("--key-output")] = Path("reports/v0.6_ab_blind_key.json"),
    seed: Annotated[str, typer.Option("--seed")] = "20260911",
) -> None:
    """Export anonymous X/Y translations and a separate reveal key."""
    captured = _load_captured_units(units, enrich_context=True)
    fixed_store = StateStore(runs_root / fixed_run_id / "state.sqlite")
    context_store = StateStore(runs_root / context_run_id / "state.sqlite")
    fixed_targets = {}
    context_targets = {}
    for unit in captured:
        if unit.unit_type in {"code", "protected"}:
            continue
        fixed_result = fixed_store.result(fixed_run_id, unit.unit_id)
        context_result = context_store.result(context_run_id, unit.unit_id)
        if fixed_result is None or fixed_result.draft is None or context_result is None or context_result.draft is None:
            raise typer.BadParameter(f"both reviewable translations are required for {unit.unit_id}")
        fixed_targets[unit.unit_id] = fixed_result.draft.translated_text
        context_targets[unit.unit_id] = context_result.draft.translated_text
    review, key = build_blind_review(captured, fixed_targets, context_targets, seed=seed)
    review_output.parent.mkdir(parents=True, exist_ok=True)
    key_output.parent.mkdir(parents=True, exist_ok=True)
    review_output.write_text(json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8")
    key_output.write_text(json.dumps(key, ensure_ascii=False, indent=2), encoding="utf-8")
    typer.echo(json.dumps({"review_output": str(review_output), "key_output": str(key_output), "pair_count": review["pair_count"]}, ensure_ascii=False, indent=2))


@app.command()
def invalidate(
    context_db: Annotated[Path, typer.Option("--context-db")],
    changed_id: Annotated[list[str], typer.Option("--changed-id")],
) -> None:
    """Report units transitively affected by changed context objects."""
    store = ContextStore(context_db)
    report = make_invalidation_report(set(changed_id), store.dependency_edges())
    typer.echo(report.model_dump_json(indent=2))


if __name__ == "__main__":
    app()


@app.command("pdf-native")
def pdf_native(
    input: Annotated[Path,typer.Option("--input")],
    config: Annotated[Path,typer.Option("--config")],
    output_dir: Annotated[Path,typer.Option("--output-dir")],
    font: Annotated[Path,typer.Option("--font")],
    source_page: Annotated[list[int] | None,typer.Option("--source-page")] = None,
    runs_root: Annotated[Path,typer.Option("--runs-root")] = Path("runs"),
    execute_paid: Annotated[bool,typer.Option("--execute-paid")] = False,
) -> None:
    """Prepare native objects and estimate; execute the runtime only with --execute-paid."""
    from .native_pipeline import run_native_pdf
    report=run_native_pdf(input,load_config(config),output_dir,runs_root,font,pages=source_page,execute_paid=execute_paid)
    typer.echo(json.dumps(report,ensure_ascii=False,indent=2))


from .service_cli import register as _register_service_commands  # noqa: E402

_register_service_commands(app)
