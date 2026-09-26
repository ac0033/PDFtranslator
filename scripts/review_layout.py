"""Compare every rendered page with the original through a model, fix what does not line up.

Without --execute-paid this only detects candidates and prices the review; no
request is sent. With it, each page (original image, rendered image, and the
page's units) goes to the run's model; replacements that pass validation are
written back and only the changed pages are rendered again. The spend counts
against the config's budget_usd. Results: <capture>/layout-review.json.

    uv run python -m scripts.review_layout --capture output/pdf/<run> --config configs/<run>.toml \\
        --source pdf/<file>.pdf --output-prefix zh --execute-paid
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from dotenv import load_dotenv

from pdf_translation.config import load_config
from pdf_translation.layout_review import detect_page_issues, estimate_page_cost, page_request, review_pages
from pdf_translation.llm import config_client
from pdf_translation.local_fixes import apply_fixes, load_units
from pdf_translation.storage import StateStore
from scripts.apply_local_fixes import rerender

ROOT = Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output-prefix", required=True)
    parser.add_argument("--pages", choices=("all", "flagged"), default="all",
                        help="Review every page, or only pages with an automatic hint.")
    parser.add_argument("--no-vision", action="store_true", help="Send text only (for models that cannot read images).")
    parser.add_argument("--dpi", type=int, default=72, help="Resolution of the page images sent to the model.")
    parser.add_argument("--workers", type=int, default=1, help="Pages rendered again at once.")
    parser.add_argument("--execute-paid", action="store_true")
    parser.add_argument("--runs-root", type=Path, default=ROOT / "runs")
    args = parser.parse_args(argv)
    load_dotenv(ROOT / ".env", override=False)

    config = load_config(args.config)
    stem = args.source.stem
    rows = [json.loads(line) for line in (args.capture / "content-review.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    units = load_units(args.capture)
    pages = detect_page_issues(args.source, args.capture, stem, rows, units)
    if args.pages == "flagged":
        pages = {n: views for n, views in pages.items() if any(v.flags for v in views)}
    vision = not args.no_vision
    fake_images = (b"",) * (2 if vision else 0)
    estimate = sum(estimate_page_cost(config, page_request(config, n, views, fake_images)) or 0 for n, views in pages.items())
    summary = {"pages": len(pages), "flagged_units": sum(bool(v.flags) for views in pages.values() for v in views),
               "estimated_max_cost_usd": round(estimate, 4), "budget_usd": config.budget_usd}
    out = args.capture / "layout-review.json"
    if not args.execute_paid:
        out.write_text(json.dumps({"status": "estimate_only", **summary}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"status": "estimate_only", **summary}, ensure_ascii=False))
        return 0

    store = StateStore(args.runs_root / config.run_id / "state.sqlite")
    report = review_pages(pages, client=config_client(config), config=config, store=store, source_pdf=args.source,
                          capture=args.capture, stem=stem, units=units, vision=vision, dpi=args.dpi,
                          workers=config.translation_concurrency)
    fixes = report.accepted_fixes({row["unit_id"]: row for row in rows})
    changed: list[int] = []
    if fixes:
        editor = f"model:{config.provider}/{config.model_id} (layout review)"
        changed = apply_fixes(fixes, editor, capture=args.capture, config=config, runs_root=args.runs_root)
        rerender(changed, capture=args.capture, config_path=args.config, source=args.source,
                 prefix=args.output_prefix, workers=args.workers)
    result = {"status": "reviewed", **summary, **report.as_dict(), "pages_rerendered": changed}
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("status", "pages", "calls", "cost_usd", "vision", "accepted", "rejected", "pages_rerendered")},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
