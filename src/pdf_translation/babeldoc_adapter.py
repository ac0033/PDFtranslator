from __future__ import annotations

import asyncio
import ast
from collections import defaultdict, deque
from difflib import SequenceMatcher
import gc
import json
import re
import threading
from pathlib import Path

import pymupdf

from .budget import BudgetExceeded
from .config import RunConfig
from .cross_page import join_cross_page_words, split_document_translation
from .document_context import enrich_pdf_units_with_context
from .hashing import sha256_bytes, sha256_file, stable_hash
from .pipeline import ProviderAttemptsExhausted, estimate_units, run_units
from .protected_literals import enrich_units_with_protected_literals
from .schemas import ArtifactRef, ProtectedSpan, SourceRef, TranslationUnit
from .semantic_layout import group_semantic_fragments, layout_marker, plain_fragment_text, split_layout_translation
from .storage import StateStore

PLACEHOLDER = re.compile(r"</?b[^>]+>")


def _load_layout_model():
    """Load BabelDOC's ONNX model with bounded CPU-memory behavior."""
    # Warm asset/tokenizer caches before allocating the ONNX inference session.
    from babeldoc.assets.assets import warmup
    warmup()
    import numpy as np
    import onnx
    import onnxruntime
    from babeldoc.docvision.doclayout import OnnxModel, YoloResult, get_doclayout_onnx_model_path

    class LowMemoryOnnxModel(OnnxModel):
        _FIXED_IMGSZ = 640

        def predict(self, image, geometry=None, **kwargs):
            if isinstance(image, np.ndarray) and len(image.shape) == 3:
                image = [image]
            results = []
            for source_image in image:
                original_shape = source_image.shape[:2]
                pixels = self.resize_and_pad_image(source_image, new_shape=self._FIXED_IMGSZ)
                pixels = np.transpose(pixels, (2, 0, 1)).astype(np.float32) / 255.0
                prediction = self.model.run(None, {"images": np.stack([pixels], axis=0)})[0][0]
                prediction = prediction[prediction[..., 4] > 0.25]
                if len(prediction) > 0:
                    prediction[..., :4] = self.scale_boxes(
                        (self._FIXED_IMGSZ, self._FIXED_IMGSZ),
                        prediction[..., :4],
                        original_shape,
                    )
                    if geometry is not None:
                        prediction[..., [0, 2]] = geometry.px_len_to_pt(prediction[..., [0, 2]], "x")
                        prediction[..., [1, 3]] = geometry.px_len_to_pt(prediction[..., [1, 3]], "y")
                results.append(YoloResult(boxes_data=prediction, names=self._names))
            return results

    model_path = str(get_doclayout_onnx_model_path())
    definition = onnx.load(model_path)
    metadata = {item.key: item.value for item in definition.metadata_props}
    layout = LowMemoryOnnxModel.__new__(LowMemoryOnnxModel)
    layout.model_path = model_path
    layout._stride = ast.literal_eval(metadata["stride"])
    layout._names = ast.literal_eval(metadata["names"])
    del definition
    gc.collect()

    options = onnxruntime.SessionOptions()
    options.enable_cpu_mem_arena = False
    options.enable_mem_pattern = False
    options.execution_mode = onnxruntime.ExecutionMode.ORT_SEQUENTIAL
    options.intra_op_num_threads = 1
    layout.model = onnxruntime.InferenceSession(
        model_path,
        sess_options=options,
        providers=["CPUExecutionProvider"],
    )
    layout.lock = threading.Lock()
    return layout


def _normalized_text(text: str) -> str:
    return "".join(character.lower() for character in text if character.isalnum())


def locate_source_bbox(source: Path, page_index: int, babeldoc_text: str) -> tuple[tuple[float, float, float, float], float]:
    """Best-effort match of BabelDOC text to a native PDF text block."""
    visible_text = PLACEHOLDER.sub("", babeldoc_text).strip()
    needle = _normalized_text(visible_text)
    if not needle:
        return (0.0, 0.0, 0.0, 0.0), 0.0
    document = pymupdf.open(source)
    try:
        page = document[page_index]
        exact_rects = page.search_for(" ".join(visible_text.split()))
        if exact_rects:
            union = exact_rects[0]
            for rect in exact_rects[1:]:
                union |= rect
            return (float(union.x0), float(union.y0), float(union.x1), float(union.y1)), 1.0
        candidates = page.get_text("blocks", sort=True)
    finally:
        document.close()
    best_bbox = (0.0, 0.0, 0.0, 0.0)
    best_score = 0.0
    for x0, y0, x1, y1, text, *_ in candidates:
        candidate = _normalized_text(text)
        if not candidate:
            continue
        score = SequenceMatcher(None, needle, candidate, autojunk=False).ratio()
        if needle in candidate or candidate in needle:
            score = max(score, min(len(needle), len(candidate)) / max(len(needle), len(candidate)))
        if score > best_score:
            best_score = score
            best_bbox = (float(x0), float(y0), float(x1), float(y1))
    return best_bbox, round(best_score, 6)


def canonicalize_babeldoc_text(
    text: str,
    *,
    page_index: int,
    sequence: int,
    bbox: tuple[float, float, float, float],
) -> tuple[str, tuple[ProtectedSpan, ...], dict[str, str]]:
    """Replace BabelDOC placeholders with runtime-owned protected markers."""
    prefix = f"bd-p{page_index + 1:04d}-u{sequence:04d}"
    block_id = f"{prefix}-block"
    pieces: list[str] = []
    protected: list[ProtectedSpan] = []
    marker_map: dict[str, str] = {}
    cursor = 0
    output_length = 0
    closing_tokens = {match.group(0)[2:-1] for match in PLACEHOLDER.finditer(text) if match.group(0).startswith("</")}
    for occurrence, match in enumerate(PLACEHOLDER.finditer(text)):
        before = text[cursor : match.start()]
        pieces.append(before)
        output_length += len(before)
        token = match.group(0)
        marker = f"⟦P:{prefix}-o{occurrence:03d}⟧"
        start = output_length
        pieces.append(marker)
        output_length += len(marker)
        marker_map[marker] = token
        tag_name = token.lstrip("</").rstrip(">")
        kind = "formula" if not token.startswith("</") and tag_name not in closing_tokens else "other"
        protected.append(
            ProtectedSpan(
                occurrence_id=f"{prefix}-o{occurrence:03d}",
                marker=marker,
                kind=kind,
                original=ArtifactRef(uri=f"babeldoc-placeholder/{prefix}-o{occurrence:03d}.txt", sha256=sha256_bytes(token.encode("utf-8")), media_type="text/plain"),
                source_ref=SourceRef(block_id=block_id, page_index=page_index, bbox=bbox, char_start=start, char_end=output_length),
            )
        )
        cursor = match.end()
    pieces.append(text[cursor:])
    return "".join(pieces), tuple(protected), marker_map


def restore_babeldoc_placeholders(text: str, marker_map: dict[str, str]) -> str:
    for marker, token in marker_map.items():
        text = text.replace(marker, token)
    return text


class ContentMatcher:
    """Match repeated captured strings without relying on callback order."""

    def __init__(self, expected: list[dict]) -> None:
        self._pending: dict[str, deque[dict]] = defaultdict(deque)
        for record in expected:
            self._pending[record["babeldoc_text"]].append(record)

    @property
    def remaining(self) -> int:
        return sum(len(records) for records in self._pending.values())

    def pop(self, text: str) -> dict | None:
        matches = self._pending.get(text)
        return matches.popleft() if matches else None


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


def _capture_translator(records: list[dict]):
    from babeldoc.translator.translator import BaseTranslator

    class CaptureTranslator(BaseTranslator):
        name = "pdftrans-capture"
        model = "deterministic-capture"

        def __init__(self) -> None:
            super().__init__(lang_in="en", lang_out="zh", ignore_cache=True)

        def do_translate(self, text, rate_limit_params=None) -> str:
            records.append({"sequence": len(records), "babeldoc_text": text})
            return text

        def do_llm_translate(self, text, rate_limit_params=None) -> str:
            raise NotImplementedError

    return CaptureTranslator()


def _injection_translator(expected: list[dict], translated_by_fragment: dict[str, str]):
    from babeldoc.translator.translator import BaseTranslator

    class InjectionTranslator(BaseTranslator):
        name = "pdftrans-inject"
        model = "runtime-results"

        def __init__(self) -> None:
            super().__init__(lang_in="en", lang_out="zh", ignore_cache=True)
            self.consumed = 0
            self.errors: list[str] = []
            self._matcher = ContentMatcher(expected)

        @property
        def remaining(self) -> int:
            return self._matcher.remaining

        def do_translate(self, text, rate_limit_params=None) -> str:
            record = self._matcher.pop(text)
            if record is None:
                self.errors.append(f"uncaptured BabelDOC text hash {stable_hash(text)}")
                return text
            self.consumed += 1
            translated = translated_by_fragment.get(record["fragment_id"])
            if translated is None:
                self.errors.append(f"missing accepted translation for {record['fragment_id']}")
                return text
            return restore_babeldoc_placeholders(translated, record["group_marker_map"])

        def do_llm_translate(self, text, rate_limit_params=None) -> str:
            raise NotImplementedError

    return InjectionTranslator()


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


def _run_babeldoc_page(source: Path, page_index: int, output_dir: Path, translator, layout=None):
    from babeldoc.format.pdf.translation_config import TranslationConfig

    output_dir.mkdir(parents=True, exist_ok=True)
    layout = layout or _load_layout_model()
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
        disable_same_text_fallback=True,
    )
    getattr(layout, "init_font_mapper", lambda _config: None)(config)
    return asyncio.run(_translate(config))


def extract_page_units(source: Path, page_index: int, capture_dir: Path, layout=None) -> tuple[list[TranslationUnit], list[dict]]:
    """Capture layout fragments, then rebuild conservative semantic groups."""
    raw_records: list[dict] = []
    _run_babeldoc_page(source, page_index, capture_dir, _capture_translator(raw_records), layout=layout)
    document_sha256 = sha256_file(source)
    document_id = f"{source.stem}-{document_sha256[:16]}"
    records: list[dict] = []
    ordered_records = sorted(raw_records, key=lambda item: (stable_hash(item["babeldoc_text"]), item["babeldoc_text"]))
    duplicate_counts: dict[str, int] = defaultdict(int)
    for sequence, original_record in enumerate(ordered_records):
        record = {**original_record, "callback_sequence": original_record["sequence"], "sequence": sequence}
        duplicate_index = duplicate_counts[record["babeldoc_text"]]
        duplicate_counts[record["babeldoc_text"]] += 1
        bbox, bbox_confidence = locate_source_bbox(source, page_index, record["babeldoc_text"])
        canonical, protected, marker_map = canonicalize_babeldoc_text(
            record["babeldoc_text"], page_index=page_index, sequence=sequence, bbox=bbox
        )
        source_hash = stable_hash(canonical)
        singleton_unit_id = f"bd-{stable_hash({'document_sha256': document_sha256, 'page_index': page_index, 'source_hash': source_hash, 'duplicate_index': duplicate_index})[:16]}"
        fragment_id = f"p{page_index + 1:04d}-f{stable_hash({'source_hash': source_hash, 'duplicate_index': duplicate_index})[:12]}"
        plain_text = plain_fragment_text(record["babeldoc_text"])
        protected_only = not any(character.isalpha() for character in plain_text)
        risk_tags = {"babeldoc_plain_hook"}
        if protected_only:
            risk_tags.add("protected_only")
        if bbox_confidence < 0.45:
            risk_tags.add("bbox_match_low")
        if any(item.kind == "formula" for item in protected):
            risk_tags.add("formula")
        records.append(
            {
                **record,
                "page_index": page_index,
                "fragment_id": fragment_id,
                "singleton_unit_id": singleton_unit_id,
                "canonical_text": canonical,
                "source_hash": source_hash,
                "plain_text": plain_text,
                "protected_only": protected_only,
                "protected": [item.model_dump(mode="json") for item in protected],
                "risk_tags": sorted(risk_tags),
                "marker_map": marker_map,
                "bbox": bbox,
                "bbox_confidence": bbox_confidence,
                "duplicate_index": duplicate_index,
            }
        )

    units: list[TranslationUnit] = []
    for group in group_semantic_fragments(records):
        group_marker_map = {
            marker: token
            for record in group
            for marker, token in record["marker_map"].items()
        }
        if len(group) == 1:
            record = group[0]
            unit_id = record["singleton_unit_id"]
            source_text = record["canonical_text"]
            source_refs = (
                SourceRef(
                    block_id=f"bd-{record['fragment_id']}-block",
                    page_index=page_index,
                    bbox=tuple(record["bbox"]),
                    char_start=0,
                    char_end=len(source_text),
                ),
            )
            protected = tuple(ProtectedSpan.model_validate(item) for item in record["protected"])
            unit_type = "protected" if record["protected_only"] else "paragraph"
            risk_tags = set(record["risk_tags"])
            record["layout_marker"] = None
        else:
            pieces: list[str] = []
            source_refs_list: list[SourceRef] = []
            protected_list: list[ProtectedSpan] = []
            risk_tags = {tag for record in group for tag in record["risk_tags"]}
            risk_tags.update({"semantic_group", "layout_resegmentation"})
            for group_index, record in enumerate(group):
                if pieces:
                    pieces.append(" ")
                marker = layout_marker(record["fragment_id"])
                marker_start = sum(len(piece) for piece in pieces)
                pieces.append(marker)
                marker_end = marker_start + len(marker)
                record["layout_marker"] = marker
                protected_list.append(
                    ProtectedSpan(
                        occurrence_id=f"layout-{record['fragment_id']}",
                        marker=marker,
                        kind="other",
                        original=ArtifactRef(
                            uri=f"runtime-layout-boundary/{record['fragment_id']}.txt",
                            sha256=sha256_bytes(b""),
                            media_type="text/plain",
                        ),
                        source_ref=SourceRef(
                            block_id=f"bd-{record['fragment_id']}-block",
                            page_index=page_index,
                            bbox=tuple(record["bbox"]),
                            char_start=marker_start,
                            char_end=marker_end,
                        ),
                    )
                )
                fragment_start = marker_end
                pieces.append(record["canonical_text"])
                fragment_end = fragment_start + len(record["canonical_text"])
                source_refs_list.append(
                    SourceRef(
                        block_id=f"bd-{record['fragment_id']}-block",
                        page_index=page_index,
                        bbox=tuple(record["bbox"]),
                        char_start=fragment_start,
                        char_end=fragment_end,
                    )
                )
                for item_data in record["protected"]:
                    item = ProtectedSpan.model_validate(item_data)
                    shifted_ref = item.source_ref.model_copy(
                        update={
                            "char_start": fragment_start + item.source_ref.char_start,
                            "char_end": fragment_start + item.source_ref.char_end,
                        }
                    )
                    protected_list.append(item.model_copy(update={"source_ref": shifted_ref}))
                record["group_index"] = group_index
            source_text = "".join(pieces)
            source_refs = tuple(source_refs_list)
            protected = tuple(protected_list)
            unit_type = "paragraph"
            unit_id = f"bdg-{stable_hash({'document_sha256': document_sha256, 'page_index': page_index, 'fragments': [record['fragment_id'] for record in group], 'source_text': source_text})[:16]}"

        unit = TranslationUnit(
            unit_id=unit_id,
            document_id=document_id,
            ir_revision="babeldoc-0.6.4-semantic-groups-v0.5",
            unit_type=unit_type,
            source_lang="en",
            target_lang="zh-CN",
            source_text=source_text,
            source_hash=stable_hash(source_text),
            source_refs=source_refs,
            protected=protected,
            risk_tags=tuple(sorted(risk_tags)),
        )
        units.append(unit)
        for group_index, record in enumerate(group):
            record["unit_id"] = unit_id
            record["group_index"] = group_index
            record["group_size"] = len(group)
            record["group_marker_map"] = group_marker_map
    units = enrich_units_with_protected_literals(source, units, {page_index: records})
    return units, records


def _merge_pdfs(inputs: list[Path], output: Path) -> None:
    temporary = output.with_name(f"{output.stem}.tmp{output.suffix}")
    temporary.unlink(missing_ok=True)
    merged = pymupdf.open()
    try:
        for path in inputs:
            source = pymupdf.open(path)
            try:
                merged.insert_pdf(source)
            finally:
                source.close()
        merged.save(temporary, garbage=4, deflate=True)
    finally:
        merged.close()
    temporary.replace(output)


def _page_is_verifiably_empty(source: Path, page_index: int) -> bool:
    """Confirm a page BabelDOC rejected as paragraph-less really carries no content."""
    import pymupdf

    with pymupdf.open(source) as document:
        page = document[page_index]
        return not page.get_text().strip() and not page.get_images() and not page.get_drawings()


def capture_pdf_sample(
    source: Path,
    page_indices: tuple[int, ...],
    output_dir: Path,
    capture_root: Path,
    *,
    enrich_context: bool = True,
) -> dict:
    """Capture a frozen sample into runtime units without model calls or final rendering."""
    output_dir.mkdir(parents=True, exist_ok=True)
    layout = _load_layout_model()
    all_units: list[TranslationUnit] = []
    records_by_page: dict[int, list[dict]] = {}
    empty_pages: list[int] = []
    for page_index in page_indices:
        try:
            units, records = extract_page_units(source, page_index, capture_root / f"page-{page_index + 1:04d}", layout=layout)
        except RuntimeError as exc:
            if "no paragraphs" in str(exc) and _page_is_verifiably_empty(source, page_index):
                empty_pages.append(page_index + 1)
                records_by_page[page_index] = []
                continue
            raise
        all_units.extend(units)
        records_by_page[page_index] = records
    if enrich_context:
        all_units = enrich_pdf_units_with_context(all_units)
    units_path = output_dir / "units.jsonl"
    capture_map_path = output_dir / "capture-map.jsonl"
    units_path.write_text("".join(unit.model_dump_json() + "\n" for unit in all_units), encoding="utf-8")
    with capture_map_path.open("w", encoding="utf-8") as stream:
        for page_index in page_indices:
            for record in records_by_page[page_index]:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    report = {
        "schema_version": "1.0",
        "source_file": source.name,
        "source_sha256": sha256_file(source),
        "physical_pages": [page + 1 for page in page_indices],
        "empty_pages": empty_pages,
        "unit_count": len(all_units),
        "context_linked_units": sum(bool(unit.neighbor_ids) for unit in all_units),
        "section_scoped_units": sum(bool(unit.section_path) for unit in all_units),
        "api_calls": 0,
        "final_pdfs": 0,
    }
    (output_dir / "capture.report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def validate_capture_identity(
    source: Path,
    page_indices: tuple[int, ...],
    units: list[TranslationUnit],
    records_by_page: dict[int, list[dict]],
) -> None:
    """Reject a captured workload from another PDF or page selection."""
    expected_document_id = f"{source.stem}-{sha256_file(source)[:16]}"
    if not units or {unit.document_id for unit in units} != {expected_document_id}:
        raise ValueError("captured units do not belong to the current source PDF hash")
    if set(records_by_page) != set(page_indices):
        raise ValueError("captured page set does not match the requested source pages")
    unit_ids = {unit.unit_id for unit in units}
    if any(record.get("unit_id") not in unit_ids for records in records_by_page.values() for record in records):
        raise ValueError("capture map refers to a unit absent from captured units")
    if any(
        source_ref.page_index not in page_indices
        for unit in units
        for source_ref in unit.source_refs
    ):
        raise ValueError("captured unit refers to a source page outside the requested selection")


def _write_content_review_queue(units: list[TranslationUnit], store: StateStore, run_id: str, path: Path) -> None:
    """Keep source/target pairs inspectable even when rendering is blocked."""
    quality = {row["unit_id"]: row for row in store.quality_reports(run_id)}
    with path.open("w", encoding="utf-8") as stream:
        for unit in units:
            row = quality.get(unit.unit_id, {})
            result = store.result(run_id, unit.unit_id)
            report = row.get("quality")
            stream.write(json.dumps({
                "unit_id": unit.unit_id,
                "source_hash": unit.source_hash,
                "source_pages": sorted({ref.page_index + 1 for ref in unit.source_refs}),
                "section_path": list(unit.section_path),
                "source_text": unit.source_text,
                "protected_literal_hints": unit.protected_literal_hints,
                "unresolved_formula_markers": [
                    span.marker for span in unit.protected
                    if span.kind == "formula" and span.marker not in unit.protected_literal_hints
                ],
                "translated_text": result.draft.translated_text if result and result.draft else None,
                "state": row.get("state", "not_started"),
                "result_hash": report.get("result_hash") if report else None,
                "issues": report.get("issues", []) if report else [],
                "meaning_equivalent_human_review": None,
                "unchanged_elements_human_review": None,
                "review_notes": "",
            }, ensure_ascii=False) + "\n")


def run_smoke_document(
    source: Path,
    page_indices: tuple[int, ...],
    config: RunConfig,
    output_dir: Path,
    runs_root: Path,
    capture_root: Path,
    *,
    execute_paid: bool = False,
    reuse_capture: bool = False,
    sample_name: str = "smoke",
    render_review_draft: bool = False,
) -> dict:
    """Capture, translate, quality-check and inject a frozen PDF sample."""
    output_dir.mkdir(parents=True, exist_ok=True)
    units_path = output_dir / "units.jsonl"
    capture_map_path = output_dir / "capture-map.jsonl"
    layout = None
    if reuse_capture:
        if not units_path.is_file() or not capture_map_path.is_file():
            raise FileNotFoundError("--reuse-capture requires existing units.jsonl and capture-map.jsonl")
        all_units = [
            TranslationUnit.model_validate_json(line)
            for line in units_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        records_by_page: dict[int, list[dict]] = defaultdict(list)
        for line in capture_map_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                record = json.loads(line)
                records_by_page[record["page_index"]].append(record)
        validate_capture_identity(source, page_indices, all_units, records_by_page)
        all_units = enrich_units_with_protected_literals(source, all_units, records_by_page)
    else:
        layout = _load_layout_model()
        all_units = []
        records_by_page = {}
        for page_index in page_indices:
            units, records = extract_page_units(source, page_index, capture_root / f"page-{page_index + 1:04d}", layout=layout)
            all_units.extend(units)
            records_by_page[page_index] = records
    if not reuse_capture:
        units_path.write_text("".join(unit.model_dump_json() + "\n" for unit in all_units), encoding="utf-8")
        with capture_map_path.open("w", encoding="utf-8") as stream:
            for page_index in page_indices:
                for record in records_by_page[page_index]:
                    stream.write(json.dumps(record, ensure_ascii=False) + "\n")

    cross_page_evidence = []
    if config.join_cross_page_words:
        with pymupdf.open(source) as document:
            sizes = {i: (document[i].rect.width, document[i].rect.height) for i in page_indices}
        all_units, records_by_page, cross_page_evidence = join_cross_page_words(all_units, records_by_page, sizes)
        (output_dir / "semantic-preparation.json").write_text(
            json.dumps(cross_page_evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    if config.context_engine == "v1":
        all_units = enrich_pdf_units_with_context(all_units)
    if config.join_cross_page_words:
        (output_dir / "prepared-units.jsonl").write_text(
            "".join(u.model_dump_json() + "\n" for u in all_units), encoding="utf-8")


    estimate = estimate_units(all_units, config)
    base_report = {
        "schema_version": "1.0",
        "source_file": source.name,
        "source_sha256": sha256_file(source),
        "physical_pages": [page + 1 for page in page_indices],
        "sample_name": sample_name,
        "unit_count": len(all_units),
        "billable_unit_count": estimate["billable_unit_count"],
        "protected_passthrough_count": estimate["protected_passthrough_count"],
        "semantic_group_count": sum("semantic_group" in unit.risk_tags for unit in all_units),
        "grouped_fragment_count": sum(record["group_size"] > 1 for records in records_by_page.values() for record in records),
        "layout_boundary_markers": sum(record["layout_marker"] is not None for records in records_by_page.values() for record in records),
        "protected_occurrences": sum(len(record["marker_map"]) for records in records_by_page.values() for record in records),
        "verified_protected_literal_hints": sum(len(unit.protected_literal_hints) for unit in all_units),
        "unresolved_formula_markers": sum(
            span.kind == "formula" and span.marker not in unit.protected_literal_hints
            for unit in all_units for span in unit.protected
        ),
        "low_bbox_matches": sum(record["bbox_confidence"] < 0.45 for records in records_by_page.values() for record in records),
        "estimated_input_tokens_ceiling": estimate["estimated_input_tokens_ceiling"],
        "reserved_output_tokens": estimate["reserved_output_tokens"],
        "estimated_max_cost_usd": estimate["estimated_max_cost_usd"],
        "provider": config.provider,
        "model_id": config.model_id,
        "capture_reused": reuse_capture,
        "cross_page_word_joins": len(cross_page_evidence),
        "translation_units_jsonl": "prepared-units.jsonl" if config.join_cross_page_words else "units.jsonl",
        "translation_units_sha256": sha256_file(output_dir / ("prepared-units.jsonl" if config.join_cross_page_words else "units.jsonl")),
        "context_engine": config.context_engine,
        "context_linked_units": sum(bool(unit.neighbor_ids) for unit in all_units),
        "section_scoped_units": sum(bool(unit.section_path) for unit in all_units),
    }
    report_path = output_dir / "run.report.json"
    if config.provider != "mock" and not execute_paid:
        report = {**base_report, "status": "budget_approval_required", "api_calls": 0}
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report

    store = StateStore(runs_root / config.run_id / "state.sqlite")
    review_queue_path = output_dir / "content-review.jsonl"
    exhausted_units: list[str] = []
    try:
        while True:
            try:
                summary = run_units(all_units, config, runs_root)
                break
            except ProviderAttemptsExhausted as exc:
                if exc.unit_id is None or exc.unit_id in exhausted_units:
                    raise
                store.mark_exhausted(config.run_id, exc.unit_id)
                exhausted_units.append(exc.unit_id)
    except (ProviderAttemptsExhausted, BudgetExceeded) as exc:
        _write_content_review_queue(all_units, store, config.run_id, review_queue_path)
        report = {**base_report, "status": "budget_exhausted" if isinstance(exc, BudgetExceeded) else "provider_attempts_exhausted", "error": str(exc), "exhausted_units": exhausted_units, "run_summary": store.summary(config.run_id), "content_review_jsonl": review_queue_path.name}
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report
    if exhausted_units:
        base_report = {**base_report, "exhausted_units": exhausted_units}
    _write_content_review_queue(all_units, store, config.run_id, review_queue_path)
    draft_allowed = render_review_draft and review_draft_renderable(store.quality_reports(config.run_id), len(all_units))
    if summary.get("states") != {"accepted": len(all_units)} and not draft_allowed:
        report = {**base_report, "status": "quality_gate_blocked", "run_summary": summary, "content_review_jsonl": review_queue_path.name}
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report

    translated_by_unit = {}
    for unit in all_units:
        result = store.result(config.run_id, unit.unit_id)
        if result is None or result.draft is None:
            raise RuntimeError(f"accepted unit has no translation result: {unit.unit_id}")
        translated_by_unit[unit.unit_id] = result.draft.translated_text

    translated_by_fragment = split_document_translation(translated_by_unit, records_by_page)

    mono_pages: list[Path] = []
    dual_pages: list[Path] = []
    injection_reused_pages: list[int] = []
    try:
        if layout is None:
            layout = _load_layout_model()
        for page_index in page_indices:
            page_dir = output_dir / f"page-{page_index + 1:04d}"
            if not records_by_page[page_index]:
                if not _page_is_verifiably_empty(source, page_index):
                    raise RuntimeError(f"page {page_index + 1} has content but no captured records")
                page_dir.mkdir(parents=True, exist_ok=True)
                passthrough = page_dir / f"{source.stem}.zh.mono.pdf"
                with pymupdf.open(source) as original, pymupdf.open() as single:
                    single.insert_pdf(original, from_page=page_index, to_page=page_index)
                    single.save(passthrough)
                mono_pages.append(passthrough)
                dual_pages.append(passthrough)
                continue
            existing_mono = page_dir / f"{source.stem}.zh.mono.pdf"
            existing_dual = page_dir / f"{source.stem}.zh.dual.pdf"
            if reuse_capture and existing_mono.is_file() and existing_dual.is_file():
                mono_pages.append(existing_mono)
                dual_pages.append(existing_dual)
                injection_reused_pages.append(page_index + 1)
                continue
            translator = _injection_translator(records_by_page[page_index], translated_by_fragment)
            result = _run_babeldoc_page(source, page_index, page_dir, translator, layout=layout)
            if translator.errors or translator.remaining:
                raise RuntimeError(
                    f"BabelDOC injection mismatch on page {page_index + 1}: "
                    f"errors={translator.errors}, remaining={translator.remaining}"
                )
            if not result.mono_pdf_path or not result.dual_pdf_path:
                raise RuntimeError(f"BabelDOC did not render both outputs for page {page_index + 1}")
            mono_pages.append(result.mono_pdf_path)
            dual_pages.append(result.dual_pdf_path)
    except (RuntimeError, MemoryError, OSError) as exc:
        report = {
            **base_report,
            "status": "render_failed",
            "render_error": str(exc),
            "injection_reused_pages": injection_reused_pages,
            "run_summary": summary,
            "content_review_jsonl": review_queue_path.name,
            "api_calls": summary.get("provider_attempts", 0),
        }
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report

    mono_output = output_dir / f"{source.stem}.{sample_name}.{config.provider}.zh.mono.pdf"
    dual_output = output_dir / f"{source.stem}.{sample_name}.{config.provider}.zh.dual.pdf"
    _merge_pdfs(mono_pages, mono_output)
    _merge_pdfs(dual_pages, dual_output)
    for path in (mono_output, dual_output):
        document = pymupdf.open(path)
        try:
            if len(document) != len(page_indices):
                raise RuntimeError(f"merged output {path.name} has {len(document)} pages, expected {len(page_indices)}")
        finally:
            document.close()
    quality_by_unit = {row["unit_id"]: row for row in store.quality_reports(config.run_id)}
    aligned_path = output_dir / "aligned.jsonl"
    with aligned_path.open("w", encoding="utf-8") as stream:
        for unit in all_units:
            stream.write(
                json.dumps(
                    {
                        "unit": unit.model_dump(mode="json"),
                        "translated_text": translated_by_unit[unit.unit_id],
                        "quality": quality_by_unit[unit.unit_id],
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    report = {
        **base_report,
        "status": "review_draft" if draft_allowed and summary.get("states") != {"accepted": len(all_units)} else ("completed_with_location_review" if base_report["low_bbox_matches"] else "completed"),
        "review_draft": draft_allowed and summary.get("states") != {"accepted": len(all_units)},
        "injection_reused_pages": injection_reused_pages,
        "run_summary": summary,
        "mono_pdf": mono_output.name,
        "dual_pdf": dual_output.name,
        "aligned_jsonl": aligned_path.name,
        "capture_map_jsonl": capture_map_path.name,
        "content_review_jsonl": review_queue_path.name,
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


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


def review_draft_renderable(rows: list[dict], expected_count: int) -> bool:
    """Only completed, structurally safe results may enter a review draft."""
    return bool(rows) and len(rows) == expected_count and all(
        row.get("state") in {"accepted", "needs_review"}
        and row.get("quality") is not None
        and not any(issue["severity"] == "critical" for issue in row["quality"].get("issues", []))
        for row in rows
    )
