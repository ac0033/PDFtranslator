from pathlib import Path

import pymupdf
import pytest
from pydantic import ValidationError

from pdf_translation.benchmark import build_samples
from pdf_translation.manifest import BenchmarkManifest, PageSample


def make_pdf(path: Path, pages: int = 50) -> None:
    document = pymupdf.open()
    for index in range(pages):
        page = document.new_page()
        font = "cour" if index == 7 else "helv"
        page.insert_text((72, 72), ("formula diagram code sample " * 8) + str(index), fontname=font)
    document.save(path)
    document.close()


def test_samples_are_deterministic_and_disjoint(tmp_path: Path):
    source = tmp_path / "source.pdf"
    make_pdf(source)
    first = build_samples(source, (1, 7, 12), development_count=5, frozen_count=8, seed=42)
    second = build_samples(source, (1, 7, 12), development_count=5, frozen_count=8, seed=42)
    assert first == second
    page_sets = [set(sample.page_indices) for sample in first]
    assert all(left.isdisjoint(right) for i, left in enumerate(page_sets) for right in page_sets[i + 1 :])
    assert [len(sample.page_indices) for sample in first] == [3, 5, 8]


def test_manifest_rejects_overlap_and_out_of_bounds():
    smoke = PageSample(name="smoke", page_indices=(1,), selection_method="manual")
    development = PageSample(name="development", page_indices=(1,), selection_method="seeded")
    with pytest.raises(ValidationError, match="overlaps"):
        BenchmarkManifest(document_uri="x.pdf", document_sha256="a" * 64, page_count=2, samples=(smoke, development))
    with pytest.raises(ValidationError, match="outside"):
        BenchmarkManifest(document_uri="x.pdf", document_sha256="a" * 64, page_count=1, samples=(smoke,))
