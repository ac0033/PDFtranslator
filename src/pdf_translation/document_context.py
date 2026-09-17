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


def running_matter_ids(units: list[TranslationUnit]) -> set[str]:
    """Find repeated top-margin text using source geometry and other pages.

    Page numbers may vary; short fragments must be substrings of a longer
    repeated header. No document-specific header words are assumed.
    """
    def signature(unit):
        return re.sub(r"[\W\d_]+", "", _visible_text(unit).casefold())

    top = [u for u in units if u.source_refs and all(r.bbox[3] <= 60 for r in u.source_refs)]
    found = {u.unit_id for u in units if _is_running_matter(u)}
    for unit in top:
        key = signature(unit)
        if not key:
            found.add(unit.unit_id)
            continue
        matches = { _page_index(other) for other in top
            if len(signature(other)) >= 20 and key in signature(other)
            and _page_index(other) != _page_index(unit) }
        if len(matches) >= 2:
            found.add(unit.unit_id)
    return found


def _same_column(first: TranslationUnit, second: TranslationUnit) -> bool:
    if not first.source_refs or not second.source_refs:
        return True
    a, b = first.source_refs[0].bbox, second.source_refs[0].bbox
    overlap = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    return abs(a[0] - b[0]) <= 36 and overlap >= 0.5 * min(a[2] - a[0], b[2] - b[0])


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

    running = running_matter_ids(units)
    sections: dict[str, tuple[str, ...]] = {}
    current: tuple[str, ...] = ()
    previous_page: int | None = None
    for page in sorted(by_page):
        if previous_page is None or page != previous_page + 1:
            current = (f"Physical page {page + 1}",)
        for unit in by_page[page]:
            detected = None if unit.unit_id in running else _heading_path(_visible_text(unit), current)
            if detected:
                current = detected
            sections[unit.unit_id] = unit.section_path or current
        previous_page = page
    for unit in unpaged:
        sections[unit.unit_id] = unit.section_path

    eligible = [unit for unit in ordered if unit.unit_type not in {"code", "protected"} and unit.unit_id not in running]
    inferred_neighbors: dict[str, list[str]] = defaultdict(list)
    for index, unit in enumerate(eligible):
        unit_page = _page_index(unit)
        # Search within the reading column. Marginal captions must not displace
        # the preceding/following body paragraph simply because their y is near.
        for candidates in (reversed(eligible[:index]), iter(eligible[index + 1:])):
            for neighbor in candidates:
                neighbor_page = _page_index(neighbor)
                if unit_page is not None and neighbor_page is not None and abs(unit_page - neighbor_page) > 1:
                    break
                if not _same_column(unit, neighbor):
                    continue
                if sections[unit.unit_id] == sections[neighbor.unit_id]:
                    inferred_neighbors[unit.unit_id].append(neighbor.unit_id)
                break

    enriched: list[TranslationUnit] = []
    for unit in units:
        neighbors = tuple(dict.fromkeys(inferred_neighbors[unit.unit_id]))
        risk_tags = tuple(sorted(set(unit.risk_tags) | {"context_graph_v0.7", "context_graph_rc12"} | ({"running_matter"} if unit.unit_id in running else set())))
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
