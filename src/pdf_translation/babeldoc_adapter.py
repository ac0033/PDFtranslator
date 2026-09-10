from __future__ import annotations

import asyncio
import json
import re
import threading
from pathlib import Path

import pymupdf

from .hashing import sha256_file, stable_hash

PLACEHOLDER = re.compile(r"</?b[^>]+>")


def _recording_translator(records: list[dict]):
    from babeldoc.translator.translator import BaseTranslator

    lock = threading.Lock()

    class RecordingTranslator(BaseTranslator):
        name = "pdftrans-spike"
        model = "deterministic-no-api"

        def __init__(self) -> None:
            super().__init__(lang_in="en", lang_out="zh", ignore_cache=True)

        def _translate(self, text: str, channel: str) -> str:
            translated = f"译：{text}"
            with lock:
                records.append(
                    {
                        "sequence": len(records),
                        "channel": channel,
                        "source_hash": stable_hash(text),
                        "source_text": text,
                        "translated_text": translated,
                        "placeholders": PLACEHOLDER.findall(text),
                    }
                )
            return translated

        def do_translate(self, text, rate_limit_params=None) -> str:
            return self._translate(text, "translate")

        def do_llm_translate(self, text, rate_limit_params=None) -> str:
            # The spike exercises the stable plain-text hook. BabelDOC probes
            # this method with None; raising opts out of its prompt/JSON path.
            raise NotImplementedError

    return RecordingTranslator()


async def _translate(config):
    from babeldoc.format.pdf.high_level import async_translate

    async for event in async_translate(config):
        if event["type"] == "finish":
            # The upstream event iterator does not terminate after `finish` on
            # all Windows builds, so consume the terminal event explicitly.
            return event["translate_result"]
        if event["type"] == "error":
            raise RuntimeError(str(event["error"]))
    raise RuntimeError("BabelDOC ended without a finish event")


def run_one_page_spike(source: Path, page_index: int, output_dir: Path) -> dict:
    """Run BabelDOC with a deterministic local translator; never calls an API."""
    import babeldoc
    from babeldoc.docvision.base_doclayout import DocLayoutModel
    from babeldoc.format.pdf.translation_config import TranslationConfig

    output_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict] = []
    translator = _recording_translator(records)
    layout = DocLayoutModel.load_onnx()
    config = TranslationConfig(
        translator=translator,
        input_file=source,
        lang_in="en",
        lang_out="zh",
        doc_layout_model=layout,
        pages=str(page_index + 1),
        output_dir=output_dir,
        debug=False,
        qps=1,
        pool_max_workers=1,
        term_pool_max_workers=1,
        auto_extract_glossary=False,
        save_auto_extracted_glossary=False,
        only_include_translated_page=True,
        skip_scanned_detection=True,
        disable_rich_text_translate=True,
    )
    getattr(layout, "init_font_mapper", lambda _config: None)(config)
    result = asyncio.run(_translate(config))
    document_sha256 = sha256_file(source)
    for row in records:
        row["page_index"] = page_index
        row["unit_id"] = f"bd-{stable_hash({'document_sha256': document_sha256, 'page_index': page_index, 'sequence': row['sequence'], 'source_hash': row['source_hash']})[:16]}"
    aligned = output_dir / "aligned.spike.jsonl"
    aligned.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in records), encoding="utf-8")
    output_paths = [path for path in (result.mono_pdf_path, result.dual_pdf_path) if path]
    if len(output_paths) != 2 or any(not path.is_file() for path in output_paths):
        raise RuntimeError("BabelDOC did not create both mono and dual PDFs")
    page_counts = {}
    for path in output_paths:
        document = pymupdf.open(path)
        try:
            page_counts[path.name] = len(document)
        finally:
            document.close()
    if set(page_counts.values()) != {1}:
        raise RuntimeError(f"one-page spike produced unexpected page counts: {page_counts}")
    if len({row["unit_id"] for row in records}) != len(records):
        raise RuntimeError("generated BabelDOC unit IDs are not unique")
    if any(PLACEHOLDER.findall(row["translated_text"]) != row["placeholders"] for row in records):
        raise RuntimeError("synthetic translator changed BabelDOC placeholders")
    report = {
        "schema_version": "1.0",
        "babeldoc_version": babeldoc.__version__,
        "source_file": source.name,
        "source_sha256": document_sha256,
        "page_index": page_index,
        "physical_page": page_index + 1,
        "translator": "deterministic-no-api",
        "translation_calls": len(records),
        "placeholder_occurrences": sum(len(row["placeholders"]) for row in records),
        "output_page_counts": page_counts,
        "aligned_jsonl": aligned.name,
        "mono_pdf": result.mono_pdf_path.name if result.mono_pdf_path else None,
        "dual_pdf": result.dual_pdf_path.name if result.dual_pdf_path else None,
        "status": "completed",
        "warning": "Synthetic prefix output validates interception and write-back only; it is not a translation quality sample.",
    }
    (output_dir / "spike.report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
