from __future__ import annotations

import re
from collections import defaultdict

from .schemas import TranslationUnit


MARKER = re.compile(r"⟦P:[^⟧]+⟧")
CHAPTER_HEADING = re.compile(r"^\s*(?:\d+\s+)?(chapter\s+\d+\b[^!?]{0,120})\s*$", re.IGNORECASE)
SECTION_HEADING = re.compile(r"^\s*((?:\d+\.)+\d*)\s+([^.!?]{1,120})\s*$")
NUMBERED_HEADING = re.compile(r"^((?:\d+\.)+\d*)\b")
RUNNING_MATTER = re.compile(r"(?:©|\bcopyright\b|creative\s+commons|\bcc-by(?:-[a-z]+)*\b|\bcomments\s+to\s+\S+@)", re.IGNORECASE)


def _visible_text(unit: TranslationUnit) -> str:
    return " ".join(MARKER.sub("", unit.source_text).split())


def _is_running_matter(unit: TranslationUnit) -> bool:
    return bool(RUNNING_MATTER.search(_visible_text(unit)))


def _order_key(unit: TranslationUnit) -> tuple[int, float, float, str]:
    if not unit.source_refs:
        return (10**9, 10**9, 10**9, unit.unit_id)
    first = min(unit.source_refs, key=lambda ref: (ref.page_index, ref.bbox[1], ref.bbox[0], ref.block_id))
    return (first.page_index, first.bbox[1], first.bbox[0], unit.unit_id)


def _page_index(unit: TranslationUnit) -> int | None:
    return min((ref.page_index for ref in unit.source_refs), default=None)


def _heading_path(text: str, current: tuple[str, ...]) -> tuple[str, ...] | None:
    chapter = CHAPTER_HEADING.match(text)
    if chapter:
        return (chapter.group(1).strip(),)
    section = SECTION_HEADING.match(text)
    if section:
        number = section.group(1).rstrip(".")
        label = f"{number} {section.group(2).strip()}"
        for index in range(len(current) - 1, -1, -1):
            ancestor = NUMBERED_HEADING.match(current[index])
            if ancestor and number.startswith(ancestor.group(1).rstrip(".") + "."):
                return (*current[: index + 1], label)
        if current and current[0].casefold().startswith("chapter "):
            return (current[0], label)
        return (label,)
    return None


def enrich_pdf_units_with_context(units: list[TranslationUnit]) -> list[TranslationUnit]:
    """Attach deterministic section and local-neighbor evidence to captured PDF units.

    Neighbor links only cross a page boundary when the selected physical pages are
    consecutive. This prevents unrelated benchmark samples from leaking context.
    """
    ordered = sorted(units, key=_order_key)
    by_page: dict[int, list[TranslationUnit]] = defaultdict(list)
    unpaged: list[TranslationUnit] = []
    for unit in ordered:
        page = _page_index(unit)
        if page is None:
            unpaged.append(unit)
        else:
            by_page[page].append(unit)

    sections: dict[str, tuple[str, ...]] = {}
    current: tuple[str, ...] = ()
    previous_page: int | None = None
    for page in sorted(by_page):
        if previous_page is None or page != previous_page + 1:
            current = (f"Physical page {page + 1}",)
        for unit in by_page[page]:
            detected = _heading_path(_visible_text(unit), current)
            if detected:
                current = detected
            sections[unit.unit_id] = unit.section_path or current
        previous_page = page
    for unit in unpaged:
        sections[unit.unit_id] = unit.section_path

    eligible = [unit for unit in ordered if unit.unit_type not in {"code", "protected"} and not _is_running_matter(unit)]
    inferred_neighbors: dict[str, list[str]] = defaultdict(list)
    for index, unit in enumerate(eligible):
        unit_page = _page_index(unit)
        for neighbor_index in (index - 1, index + 1):
            if neighbor_index < 0 or neighbor_index >= len(eligible):
                continue
            neighbor = eligible[neighbor_index]
            neighbor_page = _page_index(neighbor)
            same_section = sections[unit.unit_id] == sections[neighbor.unit_id]
            if same_section and (unit_page is None or neighbor_page is None or abs(unit_page - neighbor_page) <= 1):
                inferred_neighbors[unit.unit_id].append(neighbor.unit_id)

    enriched: list[TranslationUnit] = []
    for unit in units:
        neighbors = tuple(dict.fromkeys(inferred_neighbors[unit.unit_id]))
        risk_tags = tuple(sorted(set(unit.risk_tags) | {"context_graph_v0.7"}))
        enriched.append(
            unit.model_copy(
                update={
                    "section_path": sections[unit.unit_id],
                    "neighbor_ids": neighbors,
                    "risk_tags": risk_tags,
                }
            )
        )
    return enriched
