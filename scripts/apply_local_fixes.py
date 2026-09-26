"""Write reviewed translations back into a run and rerender only their pages.

Input is a JSON list of fixes, each ``{unit_id, source_hash, old_translated_text,
new_translated_text, reason}`` (see ``pdf_translation.local_fixes``). Never
calls a translation provider.

    uv run python -m scripts.apply_local_fixes fixes.json --editor "agent:..." \\
        --capture output/pdf/<run> --config configs/<run>.toml --source pdf/<file>.pdf --output-prefix zh
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys

from pdf_translation.config import load_config
from pdf_translation.local_fixes import apply_fixes, pages_of

ROOT = Path(__file__).resolve().parent.parent


def apply(fixes: list[dict], editor: str, *, capture: Path, config_path: Path, runs_root: Path = ROOT / "runs") -> list[int]:
    return apply_fixes(fixes, editor, capture=capture, config=load_config(config_path), runs_root=runs_root)


def rerender(pages: list[int], *, capture: Path, config_path: Path, source: Path, prefix: str, workers: int = 1) -> None:
    """Render the named pages again, ``workers`` at a time, then merge the books."""
    common = ["--capture", str(capture), "--config", str(config_path), "--source", str(source), "--output-prefix", prefix]
    for number in pages:
        for label in ("mono", "dual"):
            (capture / f"page-{number:04d}" / f"{source.stem}.zh.{label}.pdf").unlink(missing_ok=True)

    def render(number: int) -> int:
        return subprocess.run([sys.executable, "scripts/render_val_full.py", *common,
                               "--from-page", str(number), "--to-page", str(number)], cwd=ROOT).returncode

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        failed = [page for page, code in zip(pages, pool.map(render, pages)) if code]
    if failed:
        raise RuntimeError(f"pages failed to render: {failed}")
    subprocess.run([sys.executable, "scripts/render_val_full.py", *common, "--merge-only"], cwd=ROOT, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("fixes", type=Path)
    parser.add_argument("--editor", required=True, help="Who reviewed these fixes, e.g. 'agent:claude-opus-5-5'.")
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output-prefix", required=True, help="Infix of the merged book filenames.")
    parser.add_argument("--workers", type=int, default=1, help="Pages rendered at once.")
    parser.add_argument("--rerender-only", action="store_true", help="Fixes are already stored; only rerender their pages.")
    args = parser.parse_args()
    fixes = json.loads(args.fixes.read_text(encoding="utf-8"))
    pages = (pages_of(args.capture, [fix["unit_id"] for fix in fixes]) if args.rerender_only
             else apply(fixes, args.editor, capture=args.capture, config_path=args.config))
    print(json.dumps({"rerendering": pages}), flush=True)
    rerender(pages, capture=args.capture, config_path=args.config, source=args.source,
             prefix=args.output_prefix, workers=args.workers)


if __name__ == "__main__":
    main()
