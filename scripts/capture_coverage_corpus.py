"""Measure the capture layer against several documents, without calling an API.

Everything this project knows about its own generality comes from one book. The
capture layer is the cheapest place to widen that: capturing is local work, so a
sample of pages from each available PDF can be reconciled against an
independent extractor for nothing but time.

What this calibrates is narrow and worth stating plainly: whether the capture
layer hands over the body text of a document. It says nothing about translation
quality, layout, or whether the rest of the pipeline survives these documents -
those need paid runs, which this script deliberately does not start.

Pages are sampled on a deterministic spread across the middle of each document,
so front matter and back matter do not dominate and the result is reproducible.
All documents run in one process because loading the layout model costs far
more than capturing a page.
"""
from pathlib import Path
import argparse
import json
import time

from pdf_translation.babeldoc_adapter import capture_pdf_sample
from pdf_translation.capture_coverage import reconcile_capture

import pymupdf

DEFAULT_SOURCES = (
    "pdf/val.pdf",
    "pdf/dm.pdf",
    "pdf/optimization.pdf",
    "pdf/cs224n-2019-notes03-neuralnets.pdf",
)


def sample_pages(page_count: int, sample_size: int) -> tuple[int, ...]:
    """Evenly spaced pages across the middle 80% of the document."""
    if page_count <= sample_size:
        return tuple(range(page_count))
    first, last = int(page_count * 0.1), int(page_count * 0.9) - 1
    span = last - first
    return tuple(sorted({first + round(span * i / (sample_size - 1)) for i in range(sample_size)}))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", type=Path, action="append", help="Repeatable; defaults to every PDF in pdf/.")
    parser.add_argument("--sample", type=int, default=12, help="Pages to capture per document.")
    parser.add_argument("--work-root", type=Path, default=Path("tmp/capture-corpus"))
    parser.add_argument("--output", type=Path, default=Path("reports/capture_coverage_corpus.json"))
    args = parser.parse_args()

    sources = args.source or [Path(s) for s in DEFAULT_SOURCES]
    documents = []
    for source in sources:
        if not source.is_file():
            documents.append({"source": str(source), "error": "file not found"})
            continue
        with pymupdf.open(source) as document:
            page_count = len(document)
        pages = sample_pages(page_count, args.sample)
        capture_dir = args.work_root / source.stem
        started = time.time()
        try:
            capture_pdf_sample(source, pages, capture_dir, args.work_root / f"{source.stem}-scratch", enrich_context=False)
        except Exception as exc:  # a document the capture layer cannot handle is the finding
            documents.append({
                "source": str(source), "page_count": page_count, "sampled_pages": len(pages),
                "error": f"{type(exc).__name__}: {exc}", "seconds": round(time.time() - started, 1),
            })
            print(f"{source.name}: capture FAILED ({type(exc).__name__})", flush=True)
            continue
        report = reconcile_capture(source, capture_dir / "capture-map.jsonl", page_indices=pages)
        documents.append({
            "source": str(source),
            "page_count": page_count,
            "sampled_pages": len(pages),
            "seconds": round(time.time() - started, 1),
            "prose_words": report["prose_words"],
            "uncaptured_words": report["uncaptured_words"],
            "uncaptured_share": report["uncaptured_share"],
            "pages_with_uncaptured": report["pages_with_uncaptured"],
            "significant_pages": report["significant_pages"],
        })
        print(
            f"{source.name}: {report['uncaptured_words']}/{report['prose_words']} uncaptured "
            f"({report['uncaptured_share']:.3%}) over {len(pages)} sampled pages",
            flush=True,
        )

    payload = {
        "scope": (
            "Capture-layer reconciliation only, on a deterministic sample of pages per document. "
            "No translation, rendering or API call is performed, so this is not evidence that any "
            "of these documents can be translated end to end."
        ),
        "sample_size": args.sample,
        "documents": documents,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {args.output}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
