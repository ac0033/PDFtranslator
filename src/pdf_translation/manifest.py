from __future__ import annotations

import json
from pathlib import Path

from pydantic import Field

from .hashing import sha256_file
from .schemas import StrictModel, TranslationUnit


class BenchmarkManifest(StrictModel):
    schema_version: str = "1.0"
    document_uri: str
    document_sha256: str
    page_count: int = Field(ge=0)
    sample_mapping: str | None = None
    units_uri: str | None = None
    notes: tuple[str, ...] = ()


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
