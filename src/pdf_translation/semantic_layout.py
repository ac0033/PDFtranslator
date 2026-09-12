from __future__ import annotations

import re
from collections import Counter


BABELDOC_PLACEHOLDER = re.compile(r"</?b[^>]+>")
LAYOUT_MARKER = re.compile(r"⟦P:layout-[A-Za-z0-9_.:-]+⟧")
ANY_PROTECTED_MARKER = re.compile(r"⟦P:[A-Za-z0-9_.:-]+⟧")
SENTENCE_END = frozenset(".!?。！？；;：:")


def plain_fragment_text(text: str) -> str:
    return BABELDOC_PLACEHOLDER.sub("", text).strip()


def _looks_like_heading(text: str, bbox: tuple[float, float, float, float]) -> bool:
    if bbox[1] < 60 and bbox[3] - bbox[1] < 20:
        return True
    return bool(len(text) < 80 and re.match(r"^\d+(?:\.\d+)*\s+[A-Z]", text))


def should_merge_fragments(first: dict, second: dict) -> bool:
    """Conservatively detect a layout split inside one semantic sentence."""
    if first.get("protected_only") or second.get("protected_only"):
        return False
    first_text = first.get("plain_text", "").strip()
    second_text = second.get("plain_text", "").strip()
    if len(first_text) < 5 or len(second_text) < 5:
        return False
    if _looks_like_heading(first_text, tuple(first["bbox"])):
        return False
    if first_text[-1] in SENTENCE_END or not second_text[0].islower():
        return False
    first_box = first["bbox"]
    second_box = second["bbox"]
    same_line = abs(second_box[1] - first_box[1]) <= 3 and -2 <= second_box[0] - first_box[2] <= 16
    if same_line:
        return True
    if second_box[1] <= first_box[1]:
        return False
    vertical_gap = second_box[1] - first_box[3]
    left_delta = abs(second_box[0] - first_box[0])
    return -32 <= vertical_gap <= 12 and left_delta <= 24


def group_semantic_fragments(records: list[dict]) -> list[list[dict]]:
    """Build deterministic semantic chains from spatially adjacent fragments."""
    spatial = sorted(records, key=lambda row: (row["bbox"][1], row["bbox"][0], row["fragment_id"]))
    predecessor: dict[str, str] = {}
    successor: dict[str, str] = {}
    by_id = {row["fragment_id"]: row for row in spatial}
    for second in spatial:
        candidates = [
            first
            for first in spatial
            if first["fragment_id"] not in successor and should_merge_fragments(first, second)
        ]
        if not candidates:
            continue
        first = min(
            candidates,
            key=lambda row: (
                abs(second["bbox"][1] - row["bbox"][3]),
                abs(second["bbox"][0] - row["bbox"][0]),
                row["fragment_id"],
            ),
        )
        predecessor[second["fragment_id"]] = first["fragment_id"]
        successor[first["fragment_id"]] = second["fragment_id"]

    groups: list[list[dict]] = []
    visited: set[str] = set()
    for record in spatial:
        fragment_id = record["fragment_id"]
        if fragment_id in predecessor or fragment_id in visited:
            continue
        chain: list[dict] = []
        while fragment_id not in visited:
            visited.add(fragment_id)
            chain.append(by_id[fragment_id])
            if fragment_id not in successor:
                break
            fragment_id = successor[fragment_id]
        groups.append(chain)
    return groups


def layout_marker(fragment_id: str) -> str:
    return f"⟦P:layout-{fragment_id}⟧"


def _sentence_boundaries(segment: str) -> list[int]:
    """Return raw-string offsets after punctuation and attached markers."""
    boundaries: list[int] = []
    index = 0
    while index < len(segment):
        protected = ANY_PROTECTED_MARKER.match(segment, index)
        if protected:
            index = protected.end()
            continue
        character = segment[index]
        if character not in SENTENCE_END:
            index += 1
            continue
        if character == ".":
            window = segment[max(0, index - 4) : index + 5]
            previous_character = segment[index - 1] if index else ""
            next_character = segment[index + 1] if index + 1 < len(segment) else ""
            if window.count(".") > 1 or (previous_character.isdigit() and next_character.isdigit()):
                index += 1
                continue
        end = index + 1
        while True:
            whitespace = re.match(r"\s*", segment[end:])
            end += whitespace.end() if whitespace else 0
            protected = ANY_PROTECTED_MARKER.match(segment, end)
            if not protected or LAYOUT_MARKER.fullmatch(protected.group(0)):
                break
            end = protected.end()
        boundaries.append(end)
        index = end
    return boundaries


def rebalance_layout_markers(text: str) -> str:
    """Move layout boundaries away from the middle of a source sentence.

    This deterministic helper makes the offline provider exercise the same
    contract expected from a coherent translator. It does not translate text.
    """
    markers = LAYOUT_MARKER.findall(text)
    if len(markers) < 2:
        return text
    first_position = text.find(markers[0])
    prefix = text[:first_position]
    body = text[first_position + len(markers[0]) :]
    fragments: list[str] = []
    for marker in markers[1:]:
        position = body.find(marker)
        fragments.append(body[:position])
        body = body[position + len(marker) :]
    fragments.append(body)

    body_without_layout = "".join(fragments)
    original_boundaries: list[int] = []
    offset = 0
    for fragment in fragments[:-1]:
        offset += len(fragment)
        original_boundaries.append(offset)
    sentence_boundaries = _sentence_boundaries(body_without_layout)

    placements: list[int] = []
    previous = 0
    for original in original_boundaries:
        candidates = [boundary for boundary in sentence_boundaries if boundary >= previous]
        placement = min(candidates, key=lambda boundary: (abs(boundary - original), boundary), default=len(body_without_layout))
        placements.append(placement)
        previous = placement

    output = prefix + markers[0]
    cursor = 0
    for marker, placement in zip(markers[1:], placements, strict=True):
        output += body_without_layout[cursor:placement] + marker
        cursor = placement
    return output + body_without_layout[cursor:]


def restore_and_rebalance_layout_markers(source_text: str, translated_text: str) -> tuple[str, bool]:
    """Restore layout-only markers and place them at target sentence boundaries.

    Layout markers carry no source semantics. They may therefore be restored
    deterministically when every non-layout protected marker is still exact.
    Unknown or duplicated layout markers remain a hard failure.
    """
    expected = LAYOUT_MARKER.findall(source_text)
    if not expected:
        return translated_text, False
    actual = LAYOUT_MARKER.findall(translated_text)
    if Counter(actual) - Counter(expected):
        return translated_text, False

    source_markers = ANY_PROTECTED_MARKER.findall(source_text)
    source_non_layout = [marker for marker in source_markers if not LAYOUT_MARKER.fullmatch(marker)]
    target_non_layout = [marker for marker in ANY_PROTECTED_MARKER.findall(translated_text) if not LAYOUT_MARKER.fullmatch(marker)]
    if source_non_layout != target_non_layout:
        return translated_text, False

    source_visible = ANY_PROTECTED_MARKER.sub("", source_text)
    target_body = LAYOUT_MARKER.sub("", translated_text)

    # Footnote-like markers attached to source sentence punctuation must stay
    # with their original BabelDOC fragment. Move only this unambiguous class.
    boundary_markers: list[tuple[str, float]] = []
    for match in ANY_PROTECTED_MARKER.finditer(source_text):
        marker = match.group(0)
        if LAYOUT_MARKER.fullmatch(marker):
            continue
        visible_prefix = ANY_PROTECTED_MARKER.sub("", source_text[: match.start()]).rstrip()
        if visible_prefix and visible_prefix[-1] in SENTENCE_END:
            boundary_markers.append((marker, len(visible_prefix) / max(len(source_visible), 1)))

    base = target_body
    for marker, _ in boundary_markers:
        base = base.replace(marker, "", 1)
    boundary_positions = _sentence_boundaries(base)
    footnote_placements: list[tuple[int, int, str]] = []
    boundary_marker_names = {marker for marker, _ in boundary_markers}
    remaining_positions = {
        marker: base.find(marker)
        for marker in source_non_layout
        if marker not in boundary_marker_names
    }
    previous = 0
    for order, (marker, ratio) in enumerate(boundary_markers):
        sequence_index = source_non_layout.index(marker)
        preceding = [item for item in source_non_layout[:sequence_index] if item not in boundary_marker_names]
        following = [item for item in source_non_layout[sequence_index + 1 :] if item not in boundary_marker_names]
        lower = max((remaining_positions[item] + len(item) for item in preceding), default=0)
        upper = min((remaining_positions[item] for item in following), default=len(base))
        approximate = round(ratio * len(base))
        candidates = [boundary for boundary in boundary_positions if boundary >= max(previous, lower) and boundary <= upper]
        if not candidates:
            return translated_text, False
        placement = min(candidates, key=lambda boundary: (abs(boundary - approximate), boundary), default=len(base))
        footnote_placements.append((placement, order, marker))
        previous = placement

    decorated = ""
    cursor = 0
    for placement, _, marker in sorted(footnote_placements):
        decorated += base[cursor:placement] + marker
        cursor = placement
    decorated += base[cursor:]

    non_layout_positions = {marker: decorated.find(marker) for marker in source_non_layout}
    natural_boundaries = [0, *_sentence_boundaries(decorated), len(decorated)]
    layout_placements: list[tuple[int, int, str]] = []
    previous = 0
    for order, marker in enumerate(expected):
        sequence_index = source_markers.index(marker)
        preceding = [item for item in source_markers[:sequence_index] if not LAYOUT_MARKER.fullmatch(item)]
        following = [item for item in source_markers[sequence_index + 1 :] if not LAYOUT_MARKER.fullmatch(item)]
        lower = max((non_layout_positions[item] + len(item) for item in preceding), default=0)
        upper = min((non_layout_positions[item] for item in following), default=len(decorated))
        visible_prefix = ANY_PROTECTED_MARKER.sub("", source_text[: source_text.find(marker)])
        approximate = round(len(visible_prefix) / max(len(source_visible), 1) * len(decorated))
        candidates = [boundary for boundary in natural_boundaries if boundary >= max(previous, lower) and boundary <= upper]
        if not candidates:
            return translated_text, False
        placement = min(candidates, key=lambda boundary: (abs(boundary - approximate), boundary))
        layout_placements.append((placement, order, marker))
        previous = placement

    rebuilt = ""
    cursor = 0
    for placement, _, marker in sorted(layout_placements):
        rebuilt += decorated[cursor:placement] + marker
        cursor = placement
    rebuilt += decorated[cursor:]
    if ANY_PROTECTED_MARKER.findall(rebuilt) != source_markers:
        return translated_text, False
    changed = rebuilt != translated_text
    return rebuilt, changed


def split_layout_translation(text: str, records: list[dict]) -> dict[str, str]:
    """Split one coherent translation into the original layout slots."""
    if len(records) == 1:
        return {records[0]["fragment_id"]: text}
    expected = [record["layout_marker"] for record in records]
    actual = LAYOUT_MARKER.findall(text)
    if actual != expected:
        raise ValueError(f"layout marker order changed: expected={expected}, actual={actual}")
    positions = [(text.index(marker), marker) for marker in expected]
    prefix = text[: positions[0][0]]
    result: dict[str, str] = {}
    for index, (position, marker) in enumerate(positions):
        start = position + len(marker)
        end = positions[index + 1][0] if index + 1 < len(positions) else len(text)
        segment = text[start:end]
        if index == 0:
            segment = prefix + segment
        result[records[index]["fragment_id"]] = segment.strip()
    return result


def layout_boundaries_are_natural(text: str) -> bool:
    """Require every later layout marker to follow a target sentence boundary."""
    matches = list(LAYOUT_MARKER.finditer(text))
    if len(matches) < 2:
        return True
    for index, match in enumerate(matches[1:], start=1):
        previous = text[matches[index - 1].end() : match.start()]
        visible = ANY_PROTECTED_MARKER.sub("", previous).strip()
        if visible and visible[-1] not in SENTENCE_END:
            return False
    return True
