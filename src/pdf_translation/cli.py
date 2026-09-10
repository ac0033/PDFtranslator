from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from dotenv import load_dotenv

from .config import load_config
from .hashing import sha256_bytes, stable_hash
from .manifest import BenchmarkManifest, load_manifest, load_units
from .pdf_adapter import inspect_pdf
from .pipeline import run_units
from .schemas import TranslationUnit
from .storage import StateStore

app = typer.Typer(no_args_is_help=True, help="Auditable V0 PDF translation runtime")
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


if __name__ == "__main__":
    app()
