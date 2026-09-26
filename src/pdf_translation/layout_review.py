"""Have a model compare each rendered page with the original and repair what does not correspond.

Translation works paragraph by paragraph from extracted text. Some problems are
only visible on the page, where English and Chinese no longer line up:

* extracted text the reader never sees (a figure's hidden text layer) merged
  into a paragraph and translated into visible Chinese;
* pseudocode or code translated as prose;
* a formula fragment (an accent, a prime) placed away from its symbol;
* a translation too long for its box, so the renderer shrinks the type.

This module finds candidates without calling any model (:func:`detect_page_issues`),
then shows a model the original page, the rendered page and the page's units,
and asks for replacement translations (:func:`review_pages`). A replacement is
kept only if the quality gate accepts it and it does not add new problems;
too-long fixes must actually be shorter. Everything is booked against the run's
budget. The caller writes accepted fixes with :mod:`pdf_translation.local_fixes`
and rerenders the changed pages.
"""
from __future__ import annotations

import difflib
import json
import re
import statistics
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pymupdf

from .budget import BudgetExceeded, estimate_cost, reserve_budget
from .config import RunConfig
from .llm import LLMClient, LLMRequest
from .quality import check_quality
from .schemas import TranslationDraft, TranslationResult, TranslationUnit, Usage
from .storage import StateStore

CJK = re.compile(r"[一-鿿]")
LATIN_WORD = re.compile(r"[A-Za-z]{2}")
CODE_WORDS = re.compile(r"(?<![A-Za-z])(?:if|then|else|elif|end if|end for|end while|for|while|return|repeat|until|do|def|function|procedure)(?![A-Za-z])")
#: Headers and footers are set smaller on purpose; they are not squeezed prose.
MARGIN_SHARE = 0.08
#: Rendered Chinese this much smaller than the page's usual ratio counts as squeezed.
SQUEEZE_RATIO = 0.85
MIN_CJK_CHARS = 20
#: A unit whose words are at least this share off-page is off-page text only.
OFFPAGE_SHARE = 0.8
#: A marker move must leave the Chinese wording at least this similar.
MOVE_SIMILARITY = 0.9
#: A meaning fix may not drop more than this share of the Chinese text.
KEEP_SHARE = 0.7
#: Quality-gate categories that mean the text and the page may not line up.
LAYOUT_CATEGORIES = frozenset({"layout_boundary_unverifiable", "protected_marker_order", "protected_source_literal_unavailable"})
KINDS = ("extraction_garbage", "code_translated", "symbol_misplaced", "too_long", "mistranslation", "other")
SEVERITIES = ("wrong_meaning", "unreadable", "minor")
#: Only these are applied; a model's stylistic preference is not a defect.
APPLIED_SEVERITIES = frozenset({"wrong_meaning", "unreadable"})
#: Problems a fix of this kind may introduce: dropping hidden text can drop its numbers;
ALLOWED_NEW = {"extraction_garbage": {"number", "untranslated_unit"},
               # moving a marker is exactly what changes marker order
               "symbol_misplaced": {"protected_marker_order"}}
#: Formula notation belongs inside the markers, which render the original glyphs;
#: written out in the translation it prints twice, or as raw ^{...} syntax.
FORMULA_CHARS = set("=+^_{}\\≈≤≥≠∑∏∫σ∂∇−×·→←∈")
LATIN_WORDS = re.compile(r"[A-Za-z]{2,}")
#: HTTP statuses that refuse the account rather than one request.
ACCOUNT_REFUSALS = frozenset({401, 402, 403})
IMAGE_TOKENS = 1100  # budget reservation per page image; vendors bill images differently

SYSTEM = (
    "You check a Chinese translation of one PDF page against the original page. "
    "Image 1 is the original English page; image 2 is the rendered Chinese page. "
    "The JSON lists the page's text units: `source` is the text extracted from the PDF, "
    "`translation` is the current Chinese, `flags` are automatic hints.\n"
    "About markers: every ⟦P:...⟧ marker is replaced by the ORIGINAL glyphs of the PDF when the "
    "page is rendered (a formula, a symbol, an accent, or a layout slot). So never write formula "
    "notation, LaTeX, =, +, ^, _, braces or math symbols in the translation - the marker already "
    "prints them. `readable_source` shows what some markers contain.\n"
    "Report a unit only when a reader would be misled or cannot read it:\n"
    "- extraction_garbage: the unit contains text that is not visible on the original page. Units "
    "flagged `hidden_text` list such text in `hidden_segments` (it is drawn outside the page); "
    "remove only the translation of that text; keep every marker and the translation of what is visible.\n"
    "- code_translated: pseudocode or code was translated as prose; restore its keywords and "
    "structure exactly as in the original (e.g. if ... then ... end if), translating nothing but "
    "natural-language comments.\n"
    "- symbol_misplaced: a marker sits at the wrong position so the rendered formula or sentence "
    "reads wrongly; move markers, do not rewrite them.\n"
    "- too_long: flagged `squeezed`; shorten to about `fit.target_chars` Chinese characters "
    "without dropping meaning.\n"
    "- mistranslation: the Chinese states something the original does not.\n"
    "- other: any other mismatch that misleads the reader.\n"
    "Do NOT report wording preferences, synonyms, spacing, or English names that could be "
    "translated either way. Keep Chinese as Chinese: do not switch translated text back to English. "
    "`severity` is wrong_meaning, unreadable, or minor; anything minor will be ignored, so prefer "
    "not to report it. `fixed_translation` is the complete replacement Chinese for the unit - never "
    "empty - with "
    "every ⟦P:...⟧ marker exactly once and 'P:layout-' markers in their original order. "
    "If the page reads correctly, return an empty list."
)
SCHEMA = {
    "type": "object",
    "properties": {"problems": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "unit_id": {"type": "string"},
            "kind": {"type": "string", "enum": list(KINDS)},
            "severity": {"type": "string", "enum": list(SEVERITIES)},
            "explanation": {"type": "string"},
            "fixed_translation": {"type": "string"},
        },
        "required": ["unit_id", "kind", "severity", "explanation", "fixed_translation"],
        "additionalProperties": False,
    }}},
    "required": ["problems"],
    "additionalProperties": False,
}


@dataclass
class UnitView:
    unit_id: str
    source: str
    readable_source: str
    translation: str
    flags: list[str] = field(default_factory=list)
    fit: dict | None = None
    #: Stretches of the source drawn outside the visible page.
    hidden_segments: list[str] | None = None


@dataclass
class Decision:
    page: int
    unit_id: str
    kind: str
    explanation: str
    old_translation: str
    new_translation: str
    accepted: bool
    reason: str
    new_issues: list[str] = field(default_factory=list)
    severity: str = "wrong_meaning"


# --- detection, no model calls -------------------------------------------------

def _spans(page: pymupdf.Page) -> list[dict]:
    return [span for block in page.get_text("dict")["blocks"] for line in block.get("lines", []) for span in line["spans"]]


def _centre_in(span: dict, box: pymupdf.Rect) -> bool:
    x0, y0, x1, y1 = span["bbox"]
    return box.contains(pymupdf.Point((x0 + x1) / 2, (y0 + y1) / 2))


def measure_fit(source_page: pymupdf.Page, rendered_page: pymupdf.Page, records: list[dict]) -> dict[str, dict]:
    """Per unit: rendered Chinese size relative to the source, normalised by the page.

    A page often sets all Chinese a little smaller than the English; only a unit
    well below the page's own ratio was shrunk to fit its box.
    """
    source_spans, rendered_spans = _spans(source_page), _spans(rendered_page)
    height = source_page.rect.height
    per_unit: dict[str, dict] = {}
    for record in records:
        if not record.get("bbox"):
            continue
        box = pymupdf.Rect(record["bbox"])
        centre = (box.y0 + box.y1) / 2
        if box.width < 30 or centre < height * MARGIN_SHARE or centre > height * (1 - MARGIN_SHARE):
            continue
        entry = per_unit.setdefault(record["unit_id"], {"source": [], "rendered": [], "chars": 0})
        entry["source"] += [s["size"] for s in source_spans if _centre_in(s, box) and LATIN_WORD.search(s["text"])]
        inside = [s for s in rendered_spans if _centre_in(s, box) and CJK.search(s["text"])]
        entry["rendered"] += [s["size"] for s in inside]
        entry["chars"] += sum(len(CJK.findall(s["text"])) for s in inside)
    ratios = {uid: statistics.median(e["rendered"]) / statistics.median(e["source"])
              for uid, e in per_unit.items() if e["source"] and e["rendered"] and e["chars"] >= MIN_CJK_CHARS}
    if len(ratios) < 3:
        return {}
    base = statistics.median(ratios.values())
    fit = {}
    for uid, ratio in ratios.items():
        relative = ratio / base
        if relative < SQUEEZE_RATIO:
            chars = per_unit[uid]["chars"]
            # Text area scales with the square of the type size.
            fit[uid] = {"relative_size": round(relative, 2), "current_chars": chars,
                        "target_chars": int(chars * relative * relative)}
    return fit


def looks_like_code(source: str) -> bool:
    return "←" in source or len(CODE_WORDS.findall(source)) >= 2


def readable(unit: TranslationUnit) -> str:
    text = unit.source_text
    for marker, literal in unit.protected_literal_hints.items():
        text = text.replace(marker, literal)
    return text


def _letters(text: str) -> str:
    return "".join(ch for ch in text.lower() if ch.isalpha())


def page_text_visibility(page: pymupdf.Page) -> tuple[str, str]:
    """Letters drawn inside the visible page and letters drawn outside it.

    Some PDFs carry text positioned beyond the crop box - typically a figure
    imported with its own captions. The reader never sees it, but extraction
    can merge it into a neighbouring paragraph.
    """
    visible, hidden = [], []
    for trace in page.get_texttrace():
        letters = _letters("".join(chr(c[0]) for c in trace["chars"]))
        (visible if pymupdf.Rect(trace["bbox"]).intersects(page.rect) else hidden).append(letters)
    return "|".join(visible), "|".join(hidden)


GRAM = 8


def hidden_share(text: str, visible: str, hidden: str) -> float:
    """Share of the text's letter 8-grams drawn only outside the page.

    Whole-word matching is too coarse - common words occur on both sides - and
    extracted text loses its spaces, so runs of letters are compared instead.
    """
    letters = _letters(text)
    grams = {letters[i:i + GRAM] for i in range(len(letters) - GRAM + 1)}
    if not grams:
        return 0.0
    return sum(1 for g in grams if g in hidden and g not in visible) / len(grams)


def hidden_segments(source: str, visible: str, hidden: str) -> list[str]:
    """Stretches of the source between markers that are off-page text."""
    return [part.strip() for part in re.split(r"⟦[^⟧]*⟧", source)
            if len(_letters(part)) >= GRAM and hidden_share(part, visible, hidden) >= OFFPAGE_SHARE]


def detect_page_issues(source_pdf: Path, capture: Path, stem: str, rows: list[dict],
                       units: dict[str, TranslationUnit]) -> dict[int, list[UnitView]]:
    """Every page with its units and automatic flags. One-based page numbers."""
    records = [json.loads(line) for line in (capture / "capture-map.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    by_page: dict[int, list[dict]] = {}
    for record in records:
        by_page.setdefault(record["page_index"] + 1, []).append(record)
    rows_by_id = {row["unit_id"]: row for row in rows}
    pages: dict[int, list[UnitView]] = {}
    with pymupdf.open(source_pdf) as source:
        for number, page_records in sorted(by_page.items()):
            rendered_path = capture / f"page-{number:04d}" / f"{stem}.zh.mono.pdf"
            visible, hidden = page_text_visibility(source[number - 1])
            fit: dict[str, dict] = {}
            if rendered_path.is_file():
                with pymupdf.open(rendered_path) as rendered:
                    fit = measure_fit(source[number - 1], rendered[0], page_records)
            views = []
            for unit_id in dict.fromkeys(r["unit_id"] for r in page_records):
                row = rows_by_id.get(unit_id)
                if not row or not row.get("translated_text") or unit_id not in units:
                    continue
                unit = units[unit_id]
                flags = sorted({i["category"] for i in row.get("issues", []) if i["category"] in LAYOUT_CATEGORIES})
                if looks_like_code(unit.source_text):
                    flags.append("code_like")
                if unit_id in fit:
                    flags.append("squeezed")
                invisible: list[str] = []
                if hidden:
                    if hidden_share(unit.source_text, visible, hidden) >= OFFPAGE_SHARE:
                        # Entirely off the page: rendered off the page too, so the
                        # reader never sees it and there is nothing to repair.
                        continue
                    invisible = hidden_segments(unit.source_text, visible, hidden)
                if invisible:
                    flags.append("hidden_text")
                views.append(UnitView(unit_id, unit.source_text, readable(unit), row["translated_text"], flags,
                                      fit.get(unit_id), invisible or None))
            if views:
                pages[number] = views
    return pages


# --- review and validation --------------------------------------------------------

def validate_fix(unit: TranslationUnit, current: TranslationResult, new_text: str, kind: str) -> tuple[bool, str, list[str]]:
    """Accept a replacement only if it is no worse than what it replaces."""
    new_text = new_text.strip()
    if not new_text or new_text == current.draft.translated_text:
        return False, "empty or unchanged", []
    candidate = current.model_copy(update={"draft": current.draft.model_copy(update={"translated_text": new_text})})
    before = check_quality(unit, current)
    after = check_quality(unit, candidate)
    if after.status == "blocked":
        return False, "quality gate blocks it: " + "; ".join(i.explanation for i in after.issues if i.severity == "critical"), []
    added = sorted({i.category for i in after.issues} - {i.category for i in before.issues} - ALLOWED_NEW.get(kind, set()))
    if added:
        return False, "adds problems: " + ", ".join(added), added
    old_text = current.draft.translated_text
    old_cjk, new_cjk = "".join(CJK.findall(old_text)), "".join(CJK.findall(new_text))
    if kind == "too_long" and len(new_cjk) >= len(old_cjk):
        return False, "not shorter", []
    # The gate checks form, not meaning, so bound how much a fix may rewrite.
    if kind == "symbol_misplaced" and old_cjk and difflib.SequenceMatcher(None, old_cjk, new_cjk).ratio() < MOVE_SIMILARITY:
        return False, "rewrites the wording; moving a marker keeps the words", []
    if kind in ("mistranslation", "code_translated", "other") and len(new_cjk) < KEEP_SHARE * len(old_cjk):
        return False, "drops too much of the text", []
    if kind != "code_translated":
        strip = lambda text: re.sub(r"⟦[^⟧]*⟧", "", text)
        added = sorted(set(strip(new_text)) & FORMULA_CHARS - set(strip(old_text)))
        if added:
            return False, "writes formula notation outside the markers: " + "".join(added), []
        if len(LATIN_WORDS.findall(strip(new_text))) > len(LATIN_WORDS.findall(strip(old_text))):
            return False, "puts English back into the Chinese", []
    allowed = sorted({i.category for i in after.issues} - {i.category for i in before.issues})
    return True, "accepted", allowed


def _page_image(pdf: Path, index: int, dpi: int) -> bytes:
    with pymupdf.open(pdf) as document:
        return document[index].get_pixmap(dpi=dpi).tobytes("png")


def _rejects_images(error: Exception) -> bool:
    text = str(error).lower()
    return getattr(error, "status_code", None) == 400 and any(word in text for word in ("image", "vision", "multimodal", "content type"))


@dataclass
class ReviewReport:
    pages_reviewed: list[int] = field(default_factory=list)
    calls: int = 0
    cost_usd: float = 0.0
    vision: bool = True
    decisions: list[Decision] = field(default_factory=list)
    errors: dict[int, str] = field(default_factory=dict)
    stopped: str | None = None

    def accepted_fixes(self, rows_by_id: dict[str, dict]) -> list[dict]:
        return [{"unit_id": d.unit_id, "source_hash": rows_by_id[d.unit_id]["source_hash"],
                 "old_translated_text": d.old_translation, "new_translated_text": d.new_translation,
                 "reason": f"layout review ({d.kind}): {d.explanation}"}
                for d in self.decisions if d.accepted]

    def as_dict(self) -> dict:
        return {"pages_reviewed": self.pages_reviewed, "calls": self.calls, "cost_usd": round(self.cost_usd, 6),
                "vision": self.vision, "errors": self.errors, "stopped": self.stopped,
                "accepted": sum(d.accepted for d in self.decisions), "rejected": sum(not d.accepted for d in self.decisions),
                "decisions": [asdict(d) for d in self.decisions]}


def page_request(config: RunConfig, number: int, views: list[UnitView], images: tuple[bytes, ...]) -> LLMRequest:
    payload = [{k: v for k, v in asdict(view).items() if v not in (None, [])} for view in views]
    user = f"<page>{number}</page>\n<units>\n{json.dumps(payload, ensure_ascii=False)}\n</units>"
    budget = min(8000, max(config.max_output_tokens, 400 + 3 * sum(len(v.translation) for v in views)))
    return LLMRequest(system=SYSTEM, user=user, model_id=config.model_id, max_output_tokens=budget,
                      json_schema=SCHEMA, schema_name="layout_review", images=images,
                      reasoning_effort=config.reasoning_effort)


def estimate_page_cost(config: RunConfig, request: LLMRequest) -> float | None:
    input_tokens = (len(request.system) + len(request.user)) // 2 + IMAGE_TOKENS * len(request.images)
    return estimate_cost(input_tokens, request.max_output_tokens, config.input_usd_per_million, config.output_usd_per_million)


def review_pages(pages: dict[int, list[UnitView]], *, client: LLMClient, config: RunConfig, store: StateStore,
                 source_pdf: Path, capture: Path, stem: str, units: dict[str, TranslationUnit],
                 vision: bool = True, dpi: int = 72, workers: int = 1) -> ReviewReport:
    """Ask the model about each page and keep the fixes that pass validation."""
    report = ReviewReport(vision=vision)
    lock = threading.Lock()
    in_flight = [0.0]

    def one(number: int) -> None:
        views = pages[number]
        images: tuple[bytes, ...] = ()
        if report.vision:
            images = (_page_image(source_pdf, number - 1, dpi),
                      _page_image(capture / f"page-{number:04d}" / f"{stem}.zh.mono.pdf", 0, dpi))
        request = page_request(config, number, views, images)
        estimate = estimate_page_cost(config, request)
        with lock:
            reserve_budget(store.spent(config.run_id), in_flight[0], estimate, config.budget_usd)
            in_flight[0] += estimate
        try:
            try:
                response = client.complete(request)
            except Exception as error:
                if not images or not _rejects_images(error):
                    raise
                with lock:
                    report.vision = False
                response = client.complete(page_request(config, number, views, ()))
            usage = response.usage
            if usage.cost_usd is None:
                usage = usage.model_copy(update={"cost_usd": estimate_cost(
                    usage.input_tokens or 0, usage.output_tokens or 0, config.input_usd_per_million, config.output_usd_per_million)})
            with lock:
                store.record_usage(config.run_id, f"page-{number:04d}", usage, "layout_review")
                report.calls += 1
                report.cost_usd += usage.cost_usd or 0.0
        finally:
            with lock:
                in_flight[0] -= estimate
        if response.truncated_reason:
            raise RuntimeError(f"reply truncated: {response.truncated_reason}")
        problems = json.loads(response.text).get("problems", [])
        views_by_id = {v.unit_id: v for v in views}
        decisions = []
        for problem in problems:
            uid = problem.get("unit_id")
            kind = problem.get("kind") if problem.get("kind") in KINDS else "other"
            severity = problem.get("severity", "minor")
            view = views_by_id.get(uid)
            if view is None:
                continue  # the model named a unit that is not on this page
            current = store.result(config.run_id, uid)
            if current is None or current.draft is None or current.draft.translated_text != view.translation:
                continue
            if severity not in APPLIED_SEVERITIES:
                accepted, reason, new_issues = False, f"severity {severity}: not applied", []
            else:
                accepted, reason, new_issues = validate_fix(units[uid], current, problem.get("fixed_translation", ""), kind)
            decisions.append(Decision(number, uid, kind, problem.get("explanation", ""), view.translation,
                                      problem.get("fixed_translation", "").strip(), accepted, reason, new_issues, severity))
        with lock:
            report.pages_reviewed.append(number)
            report.decisions += decisions

    stop = threading.Event()

    def guarded(number: int) -> None:
        if stop.is_set():
            with lock:
                report.errors[number] = f"not reviewed: {report.stopped}"
            return
        try:
            one(number)
        except BudgetExceeded as error:
            # Keep what earlier pages found; send nothing more.
            with lock:
                report.stopped = report.stopped or f"budget reached: {error}"
                report.errors[number] = f"not reviewed: {error}"
            stop.set()
        except Exception as error:
            # One page's failure must not cost the others - unless the account
            # itself is refused, when asking for every remaining page would only
            # repeat the refusal.
            status = getattr(error, "status_code", None)
            with lock:
                report.errors[number] = f"{type(error).__name__}: {error}"[:300]
                if status in ACCOUNT_REFUSALS:
                    report.stopped = report.stopped or f"account refused (HTTP {status})"
            if status in ACCOUNT_REFUSALS:
                stop.set()

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        list(pool.map(guarded, sorted(pages)))
    report.pages_reviewed.sort()
    # One unit fixed twice (it spans two pages) keeps only its first accepted fix.
    seen: set[str] = set()
    for decision in sorted(report.decisions, key=lambda d: (d.page, d.unit_id)):
        if decision.accepted and decision.unit_id in seen:
            decision.accepted, decision.reason = False, "already fixed on an earlier page"
        elif decision.accepted:
            seen.add(decision.unit_id)
    return report
