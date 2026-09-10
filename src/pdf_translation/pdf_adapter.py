from __future__ import annotations

from pathlib import Path

import pymupdf

from .hashing import sha256_file


def inspect_pdf(path: Path) -> dict:
    document = pymupdf.open(path)
    try:
        pages = [{"page_index": i, "width": page.rect.width, "height": page.rect.height, "rotation": page.rotation, "text_chars": len(page.get_text())} for i, page in enumerate(document)]
        return {"source": str(path.resolve()), "sha256": sha256_file(path), "page_count": len(document), "pages": pages, "parser": f"PyMuPDF/{pymupdf.VersionBind}", "babeldoc_status": "not_run"}
    finally:
        document.close()
