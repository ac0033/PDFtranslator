from __future__ import annotations

import random
from collections import defaultdict
from pathlib import Path

import pymupdf

from .manifest import PageSample


def _page_tags(page: pymupdf.Page) -> set[str]:
    raw = page.get_text("dict")
    tags: set[str] = set()
    text_chars = mono_chars = math_chars = margin_chars = small_bottom_chars = 0
    for block in raw.get("blocks", []):
        if block.get("type") != 0:
            continue
        block_chars = 0
        sizes: list[float] = []
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = span.get("text", "")
                count = len(text.strip())
                block_chars += count
                text_chars += count
                font = span.get("font", "").lower()
                if "mono" in font or "courier" in font:
                    mono_chars += count
                if any(token in font for token in ("math", "symbol", "cmsy", "cmmi")):
                    math_chars += count
                sizes.append(float(span.get("size", 0)))
        x0, y0, x1, _ = block.get("bbox", (0, 0, 0, 0))
        if block_chars >= 20 and (x1 - x0) <= page.rect.width * 0.30 and (x0 <= page.rect.width * 0.12 or x1 >= page.rect.width * 0.88):
            margin_chars += block_chars
        if block_chars >= 10 and y0 >= page.rect.height * 0.82 and sizes and max(sizes) <= 9.5:
            small_bottom_chars += block_chars
    if text_chars >= 1200:
        tags.add("dense_text")
    if mono_chars >= 80:
        tags.add("code")
    if math_chars >= 10:
        tags.add("formula")
    if margin_chars >= 20:
        tags.add("margin_note")
    if small_bottom_chars >= 20:
        tags.add("footnote_or_footer")
    if page.get_images(full=True) or len(page.get_drawings()) >= 8:
        tags.add("figure_or_diagram")
    return tags


def profile_pages(path: Path) -> dict[int, tuple[str, ...]]:
    document = pymupdf.open(path)
    try:
        return {index: tuple(sorted(_page_tags(page))) for index, page in enumerate(document)}
    finally:
        document.close()


def _coverage(pages: list[int], profiles: dict[int, tuple[str, ...]]) -> tuple[str, ...]:
    return tuple(sorted({tag for page in pages for tag in profiles[page]}))


def _stratified_pick(candidates: list[int], profiles: dict[int, tuple[str, ...]], count: int, rng: random.Random) -> list[int]:
    if count > len(candidates):
        raise ValueError(f"cannot select {count} pages from {len(candidates)} candidates")
    shuffled = list(candidates)
    rng.shuffle(shuffled)
    by_tag: dict[str, list[int]] = defaultdict(list)
    for page in shuffled:
        for tag in profiles[page]:
            by_tag[tag].append(page)
    selected: list[int] = []
    for tag in ("code", "formula", "figure_or_diagram", "margin_note", "footnote_or_footer", "dense_text"):
        candidate = next((page for page in by_tag[tag] if page not in selected), None)
        if candidate is not None and len(selected) < count:
            selected.append(candidate)
    for page in shuffled:
        if len(selected) == count:
            break
        if page not in selected:
            selected.append(page)
    return sorted(selected)


def build_samples(
    path: Path,
    smoke_pages: tuple[int, ...],
    *,
    development_count: int = 12,
    frozen_count: int = 24,
    seed: int = 20260910,
) -> tuple[PageSample, ...]:
    profiles = profile_pages(path)
    missing = [page for page in smoke_pages if page not in profiles]
    if missing:
        raise ValueError(f"smoke pages outside document: {missing}")
    if len(set(smoke_pages)) != len(smoke_pages):
        raise ValueError("smoke pages contain duplicates")
    rng = random.Random(seed)
    remaining = [page for page in profiles if page not in smoke_pages]
    development = _stratified_pick(remaining, profiles, development_count, rng)
    frozen_candidates = [page for page in remaining if page not in development]
    frozen = _stratified_pick(frozen_candidates, profiles, frozen_count, rng)
    smoke = list(smoke_pages)
    return (
        PageSample(name="smoke", page_indices=tuple(smoke), selection_method="manually reviewed risk pages", coverage=_coverage(smoke, profiles)),
        PageSample(name="development", page_indices=tuple(development), selection_method=f"deterministic risk-stratified selection; seed={seed}", coverage=_coverage(development, profiles)),
        PageSample(name="frozen_test", page_indices=tuple(frozen), selection_method=f"deterministic risk-stratified holdout; seed={seed}", coverage=_coverage(frozen, profiles)),
    )
