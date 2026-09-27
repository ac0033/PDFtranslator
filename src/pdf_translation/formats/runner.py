"""Run one structured-source translation: parse, translate, write back, check, accept.

The three steps are separate so the AI OS can repeat the last one after it
fixes units (no parsing or model call is repeated) and so a job can resume:

* :func:`load` parses the source again (it is deterministic, so unit ids are
  stable across calls) and writes ``units.jsonl``;
* :func:`translate` prices the units, stops above the budget, and translates
  through the shared pipeline into the run store;
* :func:`render_and_accept` writes the outputs from whatever the run store
  holds now, runs the format's checks and writes ``acceptance-report.json``.

The work directory has the same files a PDF run has (``units.jsonl``,
``content-review.jsonl``, ``estimate.json``, ``acceptance-report.json``), so
everything that reads a run - assist, local fixes, acceptance, the job
service - works on it unchanged.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from ..acceptance import BLOCKING_STATES, build_source_acceptance
from ..babeldoc_adapter import _write_content_review_queue
from ..config import RunConfig
from ..context_models import Scope, StyleRule
from ..context_store import ContextStore
from ..pipeline import DEFAULT_TRANSLATION_STYLE, estimate_units, run_units
from ..schemas import TranslationUnit
from ..storage import StateStore
from ..triage import ReviewLedger
from . import FormatError, docx_doc, epub_doc, html_doc, latex, markdown_doc
from .common import Segment, build_units, document_id_for

EXTENSIONS = {"html": ".html", "markdown": ".md", "docx": ".docx", "epub": ".epub"}


class BudgetTooLow(RuntimeError):
    """The priced translation is above the job's budget; nothing was sent."""


@dataclass
class Loaded:
    fmt: str
    stem: str
    document: object
    units: list[TranslationUnit]
    by_unit: dict[str, Segment]
    source_bytes: bytes = b""
    source_text: str = ""
    extra: dict = field(default_factory=dict)


def _stem(name: str) -> str:
    stem = Path(name).name
    for suffix in (".tar.gz", ".tgz", ".zip", ".tar", ".gz", ".tex", ".html", ".htm", ".xhtml", ".md",
                   ".markdown", ".docx", ".epub", ".pdf"):
        if stem.lower().endswith(suffix):
            return stem[: -len(suffix)] or "document"
    return stem or "document"


def load(fmt: str, source: Path, work: Path, *, source_name: str | None = None, main_file: str | None = None) -> Loaded:
    """Parse the source and write ``units.jsonl``. ``source`` may be a directory for LaTeX."""
    work.mkdir(parents=True, exist_ok=True)
    stem = _stem(source_name or source.name)
    if fmt == "latex":
        project_root = work / "project"
        if not project_root.is_dir():
            if source.is_dir():
                shutil.copytree(source, project_root)
            elif source.suffix.lower() in {".tex", ".ltx"}:
                project_root.mkdir(parents=True)
                shutil.copy2(source, project_root / "main.tex")
            else:
                latex.unpack(source, project_root)
        try:
            project = latex.load_project(project_root, main_file)
        except ValueError as problem:
            raise FormatError(str(problem)) from problem
        data = b"".join((project_root / tex.path).read_bytes() for tex in project.files)
        segments = project.segments()
        loaded_document: object = project
        source_bytes, source_text = data, ""
    else:
        data = source.read_bytes()
        source_bytes, source_text = data, ""
        if fmt == "html":
            source_text = html_doc.decode(data)
            loaded_document = html_doc.parse(source_text)
        elif fmt == "markdown":
            source_text = data.decode("utf-8", errors="replace")
            loaded_document = markdown_doc.parse(source_text)
        elif fmt == "docx":
            try:
                loaded_document = docx_doc.parse(data)
            except Exception as problem:
                raise FormatError(f"this .docx cannot be read: {problem}") from problem
        elif fmt == "epub":
            try:
                loaded_document = epub_doc.parse(data)
            except Exception as problem:
                raise FormatError(f"this .epub cannot be read: {problem}") from problem
        else:
            raise FormatError(f"{fmt} is not a structured source format")
        segments = loaded_document.segments()
    revision = {"latex": latex.REVISION, "html": html_doc.REVISION, "markdown": markdown_doc.REVISION,
                "docx": docx_doc.REVISION, "epub": epub_doc.REVISION}[fmt]
    units, by_unit = build_units(document_id_for(data), segments, revision=revision)
    if not units:
        raise FormatError("the document holds no English prose to translate")
    (work / "units.jsonl").write_text("".join(unit.model_dump_json() + "\n" for unit in units), encoding="utf-8")
    return Loaded(fmt, stem, loaded_document, units, by_unit, source_bytes, source_text)


def _store(config: RunConfig, runs_root: Path) -> StateStore:
    return StateStore(runs_root / config.run_id / "state.sqlite")


def translate(loaded: Loaded, work: Path, config: RunConfig, runs_root: Path, *, style_note: str | None = None,
              execute_paid: bool = True) -> dict:
    """Price, then (unless only estimating) translate every unit into the run store."""
    document_id = loaded.units[0].document_id
    store = ContextStore(Path(config.context_db)) if config.context_db else None
    if store is not None:
        content = DEFAULT_TRANSLATION_STYLE + ("\n" + style_note if style_note else "")
        store.put_style(StyleRule(rule_id=f"{config.run_id}-style-{document_id}",
                                  scope=Scope(kind="document", value=document_id), version="usable-v1", content=content))
    estimate = estimate_units(loaded.units, config)
    (work / "estimate.json").write_text(json.dumps(estimate, ensure_ascii=False, indent=2), encoding="utf-8")
    if not execute_paid:
        return {"estimate": estimate}
    if estimate["estimated_max_cost_usd"] > config.budget_usd:
        raise BudgetTooLow(f"the translation is priced at up to USD {estimate['estimated_max_cost_usd']:.4f}, "
                           f"above the budget of USD {config.budget_usd}; no model was called")
    summary = run_units(loaded.units, config, runs_root)
    _write_content_review_queue(loaded.units, _store(config, runs_root), config.run_id, work / "content-review.jsonl")
    return {"estimate": estimate, "summary": summary}


def stored_translations(loaded: Loaded, config: RunConfig, runs_root: Path) -> dict[int, str | None]:
    """``id(segment)`` -> translated text, for units whose translation may be written out."""
    store = _store(config, runs_root)
    translations: dict[int, str | None] = {}
    for unit in loaded.units:
        result = store.result(config.run_id, unit.unit_id)
        state = store.state(config.run_id, unit.unit_id)
        if result is not None and result.draft and state not in BLOCKING_STATES:
            translations[id(loaded.by_unit[unit.unit_id])] = result.draft.translated_text
    return translations


def render_and_accept(loaded: Loaded, work: Path, config: RunConfig, runs_root: Path, *, bilingual: bool = True,
                      ledger: ReviewLedger | None = None) -> dict:
    """Write the outputs from the run store, check them, and grade the run."""
    store = _store(config, runs_root)
    _write_content_review_queue(loaded.units, store, config.run_id, work / "content-review.jsonl")
    translations = stored_translations(loaded, config, runs_root)
    out = work / "out"
    out.mkdir(parents=True, exist_ok=True)
    notes: list[str] = []
    problems: list[dict] = []
    if loaded.fmt == "latex":
        outputs, problems, notes = _render_latex(loaded, work, translations, bilingual=bilingual)
        leftover = latex.leftover_prose(loaded.document)
    else:
        ext = EXTENSIONS[loaded.fmt]
        mono_path, dual_path = out / f"{loaded.stem}.zh{ext}", out / f"{loaded.stem}.zh-en{ext}"
        if loaded.fmt == "html":
            mono = html_doc.to_utf8(html_doc.render(loaded.document, translations, bilingual=False))
            mono_path.write_text(mono, encoding="utf-8", newline="")
            problems = html_doc.check(loaded.source_text, mono)
            if bilingual:
                dual_path.write_text(html_doc.to_utf8(html_doc.render(loaded.document, translations, bilingual=True)),
                                     encoding="utf-8", newline="")
            leftover = html_doc.leftover_prose(loaded.document)
        elif loaded.fmt == "markdown":
            mono = markdown_doc.render(loaded.document, translations, bilingual=False)
            mono_path.write_text(mono, encoding="utf-8", newline="")
            problems = markdown_doc.check(loaded.source_text, mono)
            if bilingual:
                dual_path.write_text(markdown_doc.render(loaded.document, translations, bilingual=True), encoding="utf-8",
                                     newline="")
            leftover = markdown_doc.leftover_prose(loaded.document)
        elif loaded.fmt == "docx":
            mono = docx_doc.render(loaded.document, translations, bilingual=False)
            mono_path.write_bytes(mono)
            problems = docx_doc.check(loaded.source_bytes, mono)
            if bilingual:
                dual_path.write_bytes(docx_doc.render(loaded.document, translations, bilingual=True))
            leftover = docx_doc.leftover_prose(loaded.document)
        else:  # epub
            mono = epub_doc.render(loaded.document, translations, bilingual=False)
            mono_path.write_bytes(mono)
            problems = epub_doc.check(loaded.document, mono)
            if bilingual:
                dual_path.write_bytes(epub_doc.render(loaded.document, translations, bilingual=True))
            leftover = epub_doc.leftover_prose(loaded.document)
        outputs = {"mono": mono_path, **({"dual": dual_path} if bilingual else {})}
    (work / "format-check.json").write_text(json.dumps({"problems": problems, "leftover": leftover, "notes": notes},
                                                       ensure_ascii=False, indent=2), encoding="utf-8")
    report = build_source_acceptance(work / "content-review.jsonl", source_format=loaded.fmt, outputs=outputs,
                                     format_problems=problems, leftover=leftover, store=store, run_id=config.run_id,
                                     ledger=ledger, notes=notes)
    (work / "acceptance-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def _render_latex(loaded: Loaded, work: Path, translations: dict[int, str | None], *, bilingual: bool):
    project: latex.LatexProject = loaded.document
    out = work / "out"
    build = work / "build"
    notes: list[str] = []
    original_record = work / "original-build.json"
    if original_record.is_file():
        original = json.loads(original_record.read_text(encoding="utf-8"))
    else:
        original_tree = build / "original"
        if original_tree.exists():
            shutil.rmtree(original_tree)
        shutil.copytree(project.root, original_tree)
        result = latex.compile_project(original_tree, project.main, engine="pdflatex")
        if result.pdf is None:
            result = latex.compile_project(original_tree, project.main, engine="xelatex")
        original = {"compiled": result.pdf is not None, "engine": result.engine, "seconds": result.seconds,
                    "log_tail": result.log_tail[-1500:]}
        original_record.write_text(json.dumps(original, ensure_ascii=False, indent=2), encoding="utf-8")
    mono_tree = latex.write_project(project, build / "zh", translations, bilingual=False)
    problems = latex.check(project, mono_tree)
    source_zip = latex.zip_directory(mono_tree, out / f"{loaded.stem}.zh-source.zip")
    outputs: dict[str, Path | None] = {"source": source_zip}
    compiled = latex.compile_project(mono_tree, project.main, engine="xelatex")
    mono_pdf = out / f"{loaded.stem}.zh.pdf"
    if compiled.pdf is not None:
        shutil.copy2(compiled.pdf, mono_pdf)
        outputs["mono"] = mono_pdf
    else:
        outputs["mono"] = None
        cause = ("the source project compiles here, so the translation broke it" if original["compiled"]
                 else "the source project does not compile on this machine either")
        problems.append({"kind": "latex_compile_failed", "severity": "major", "count": 1,
                         "detail": f"the translated project did not compile with XeLaTeX; {cause}",
                         "log_tail": compiled.log_tail[-800:]})
    if bilingual:
        dual_tree = latex.write_project(project, build / "zh-en", translations, bilingual=True)
        dual = latex.compile_project(dual_tree, project.main, engine="xelatex")
        dual_pdf = out / f"{loaded.stem}.zh-en.pdf"
        if dual.pdf is not None:
            shutil.copy2(dual.pdf, dual_pdf)
            outputs["dual"] = dual_pdf
        else:
            outputs["dual"] = None
            notes.append("the bilingual project did not compile; the single-language output is unaffected")
    if not original["compiled"]:
        notes.append("the source project did not compile on this machine; the translation was checked structurally")
    return outputs, problems, notes
