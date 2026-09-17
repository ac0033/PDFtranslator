from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import re

from .document_context import running_matter_ids
from .hashing import sha256_bytes, stable_hash
from .schemas import ArtifactRef, ProtectedSpan, SourceRef, TranslationUnit
from .semantic_layout import layout_marker


def join_cross_page_words(
    units: list[TranslationUnit], records_by_page: dict[int, list[dict]],
    page_sizes: dict[int, tuple[float, float]],
) -> tuple[list[TranslationUnit], dict[int, list[dict]], list[dict]]:
    """Prepare candidate plain-text word breaks across consecutive source pages.

    Keep raw callback text and fragment IDs unchanged for injection. Derived
    translation text gets a new identity and explicit provenance. Mixed formula
    fragments, uncertain locations, ambiguous columns and page gaps fail closed.
    Geometry is not lexical proof: genuine compound hyphens can be ambiguous,
    so every candidate retains its original parts for explicit meaning review.
    """
    records = deepcopy(records_by_page)
    by_unit = defaultdict(list)
    for rows in records.values():
        for row in rows:
            by_unit[row["unit_id"]].append(row)
    running = running_matter_ids(units)
    by_page = defaultdict(list)
    for unit in units:
        if len(unit.source_refs) == 1 and unit.unit_id not in running:
            by_page[unit.source_refs[0].page_index].append(unit)
    consumed, replacements, evidence = set(), {}, []
    for page in sorted(by_page):
        if page + 1 not in by_page or page not in page_sizes or page + 1 not in page_sizes:
            continue
        height, next_height = page_sizes[page][1], page_sizes[page + 1][1]
        for first in by_page[page]:
            if first.unit_id in consumed or first.protected or first.unit_type != "paragraph":
                continue
            rows = by_unit[first.unit_id]
            if len(rows) != 1 or rows[0]["bbox_confidence"] < 0.85:
                continue
            a = first.source_refs[0].bbox
            if a[3] < height * 0.80 or not re.search(r"[A-Za-z]{3,}-$", first.source_text):
                continue
            same_column = [u for u in by_page[page] if abs(u.source_refs[0].bbox[0] - a[0]) <= 24]
            if max(same_column, key=lambda u: u.source_refs[0].bbox[3]).unit_id != first.unit_id:
                continue
            next_column = sorted(
                (u for u in by_page[page + 1] if abs(u.source_refs[0].bbox[0] - a[0]) <= 24),
                key=lambda u: u.source_refs[0].bbox[1],
            )
            if not next_column:
                continue
            second = next_column[0]
            if len(next_column) > 1 and abs(next_column[1].source_refs[0].bbox[1] - second.source_refs[0].bbox[1]) <= 3:
                continue
            b = second.source_refs[0].bbox
            if (second.unit_id in consumed or second.protected or second.unit_type != "paragraph"
                or b[1] < 60 or b[1] > next_height * 0.15
                or len(by_unit[second.unit_id]) != 1 or by_unit[second.unit_id][0]["bbox_confidence"] < 0.85):
                continue
            suffix = re.match(r"[a-z]{2,}\b", second.source_text)
            if not suffix:
                continue
            left = first.source_text[:-1] + suffix.group()
            right = second.source_text[suffix.end():]
            pair = [rows[0], by_unit[second.unit_id][0]]
            pieces, refs, protected = [], [], []
            for row, body in zip(pair, (left, right), strict=True):
                marker = layout_marker(row["fragment_id"])
                start = sum(map(len, pieces))
                ref = SourceRef(block_id=f"bd-{row['fragment_id']}-block", page_index=row["page_index"],
                    bbox=tuple(row["bbox"]), char_start=start + len(marker), char_end=start + len(marker) + len(body))
                refs.append(ref)
                protected.append(ProtectedSpan(occurrence_id=f"layout-{row['fragment_id']}", marker=marker,
                    kind="other", original=ArtifactRef(uri=f"runtime-layout-boundary/{row['fragment_id']}.txt",
                    sha256=sha256_bytes(b""), media_type="text/plain"),
                    source_ref=ref.model_copy(update={"char_start": start, "char_end": start + len(marker)})))
                pieces.extend((marker, body))
            source_text = "".join(pieces)
            uid = "bdcross-" + stable_hash({"parents": [first.unit_id, second.unit_id], "text": source_text})[:16]
            merged = first.model_copy(update={"unit_id": uid, "source_text": source_text,
                "source_hash": stable_hash(source_text), "source_refs": tuple(refs), "protected": tuple(protected),
                "ir_revision": "babeldoc-cross-page-word-rc12", "neighbor_ids": (),
                "risk_tags": tuple(sorted(set(first.risk_tags + second.risk_tags) | {"semantic_group", "cross_page_word_join"}))})
            for index, row in enumerate(pair):
                row.update(unit_id=uid, group_index=index, group_size=2,
                    layout_marker=layout_marker(row["fragment_id"]), group_marker_map={})
            replacements[first.unit_id] = merged
            consumed.update((first.unit_id, second.unit_id))
            evidence.append({"unit_id": uid, "parent_unit_ids": [first.unit_id, second.unit_id],
                "physical_pages": [page + 1, page + 2], "operation": "join_cross_page_hyphenated_word",
                "source_parts": [first.source_text, second.source_text], "translation_source": source_text,
                "reconstructed_word": re.search(r"[A-Za-z]+$", left).group(),
                "raw_capture_preserved": True, "lexical_join_requires_review": True})
    result = [replacements.get(u.unit_id, u) for u in units if u.unit_id not in consumed or u.unit_id in replacements]
    return result, records, evidence


def split_document_translation(translated_by_unit: dict[str, str], records_by_page: dict[int, list[dict]]) -> dict[str, str]:
    """Split globally: one semantic unit may own slots on multiple pages."""
    from .semantic_layout import split_layout_translation

    groups = defaultdict(list)
    for rows in records_by_page.values():
        for row in rows:
            groups[row["unit_id"]].append(row)
    result = {}
    for uid, rows in groups.items():
        ordered = sorted(rows, key=lambda row: row["group_index"])
        if [r["group_index"] for r in ordered] != list(range(len(ordered))) or any(r["group_size"] != len(ordered) for r in ordered):
            raise ValueError(f"incomplete semantic group: {uid}")
        result.update(split_layout_translation(translated_by_unit[uid], ordered))
    return result
