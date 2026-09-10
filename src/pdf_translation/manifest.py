from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from .hashing import sha256_file
from .schemas import StrictModel, TranslationUnit


class PageSample(StrictModel):
    name: Literal["smoke", "development", "frozen_test"]
    page_indices: tuple[int, ...]
    selection_method: str
    coverage: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_pages(self) -> "PageSample":
        if not self.page_indices:
            raise ValueError(f"{self.name} sample is empty")
        if any(page < 0 for page in self.page_indices):
            raise ValueError("page indices must be zero-based non-negative integers")
        if len(set(self.page_indices)) != len(self.page_indices):
            raise ValueError(f"{self.name} sample contains duplicate pages")
        return self


class BenchmarkManifest(StrictModel):
    schema_version: str = "1.0"
    document_uri: str
    document_sha256: str
    page_count: int = Field(ge=0)
    sample_mapping: str | None = None
    units_uri: str | None = None
    sampling_seed: int | None = None
    samples: tuple[PageSample, ...] = ()
    notes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_samples(self) -> "BenchmarkManifest":
        names = [sample.name for sample in self.samples]
        if len(set(names)) != len(names):
            raise ValueError("sample names must be unique")
        assigned: dict[int, str] = {}
        for sample in self.samples:
            for page in sample.page_indices:
                if page >= self.page_count:
                    raise ValueError(f"page index {page} is outside a {self.page_count}-page document")
                if page in assigned:
                    raise ValueError(f"page index {page} overlaps {assigned[page]} and {sample.name}")
                assigned[page] = sample.name
        return self


def safe_resolve(root: Path, uri: str) -> Path:
    candidate = (root / uri).resolve()
    root = root.resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError(f"artifact escapes manifest root: {uri}")
    return candidate


def load_manifest(path: Path) -> BenchmarkManifest:
    manifest = BenchmarkManifest.model_validate_json(path.read_text(encoding="utf-8"))
    document = safe_resolve(path.parent, manifest.document_uri)
    if not document.is_file():
        raise FileNotFoundError(f"manifest document not found: {manifest.document_uri}")
    if sha256_file(document) != manifest.document_sha256:
        raise ValueError("document SHA-256 changed")
    return manifest


def load_units(path: Path, manifest: BenchmarkManifest) -> list[TranslationUnit]:
    if not manifest.units_uri:
        raise ValueError("manifest has no units_uri; run the parser adapter first")
    units_path = safe_resolve(path.parent, manifest.units_uri)
    return [TranslationUnit.model_validate_json(line) for line in units_path.read_text(encoding="utf-8").splitlines() if line.strip()]
