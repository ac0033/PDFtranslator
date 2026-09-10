from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from dotenv import load_dotenv

from .config import load_config
from .analyzer import extract_term_candidates, validate_semantic_evidence
from .context_models import ContextBundle, GlossaryEntry, Scope, StyleRule, make_invalidation_report
from .context_router import build_context_packet
from .context_store import ContextStore
from .hashing import sha256_bytes, stable_hash
from .manifest import BenchmarkManifest, load_manifest, load_units
from .pdf_adapter import inspect_pdf
from .pipeline import make_request, run_units
from .schemas import TranslationUnit
from .storage import StateStore

app = typer.Typer(no_args_is_help=True, help="Auditable V1 PDF translation runtime")
load_dotenv()


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
