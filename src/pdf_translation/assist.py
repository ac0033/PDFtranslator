"""Model calls an external agent asks for while working on a finished job.

An agent connected over MCP, HTTP or the CLI is usually a model itself, so most
of what it needs it can do alone: read the content review, judge a
translation, write a fix. It delegates to the job's own model when that is
better (see docs/agent_guide.md):

* many units at once - the job model is cheaper and does not fill the agent's context;
* retranslation that must read like the rest of the book - same model, style note, glossary;
* page images the agent cannot see (text-only agents, scripts);
* callers that are not models at all.

Every request is priced before anything is sent, counts against the job's
budget and is booked in the job's ledger (source ``agent_assist``). Nothing is
written to the translation: tasks return candidate fixes in the format
:func:`pdf_translation.local_fixes.apply_fixes` takes, each with the quality
gate's verdict, and the caller decides what to apply.
"""
from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .budget import BudgetExceeded, estimate_cost, reserve_budget
from .config import RunConfig
from .context_store import ContextStore
from .layout_review import ACCOUNT_REFUSALS, IMAGE_TOKENS, _page_image, detect_page_issues, review_pages
from .llm import LLMClient, LLMRequest, LLMResponse
from .local_fixes import load_units
from .pipeline import make_request
from .quality import check_quality
from .schemas import TranslationDraft, TranslationResult, TranslationUnit, Usage
from .storage import StateStore

TASKS = {
    "retranslate": "Translate the named units again with the book's style note and glossary, following an optional instruction.",
    "shorten": "Shorten the named units' translations so they fit their layout box, keeping the meaning.",
    "review_units": "Check the named units for wrong meaning, omissions, wrong terms and unreadable Chinese; propose fixes.",
    "review_pages": "Compare rendered pages with the original page (as images when the model can see) and propose fixes.",
    "ask": "Any other question, with optional units and page images attached as context.",
}
MAX_UNITS = 40
MAX_PAGES = 5
REVIEW_BATCH = 15
SOURCE = "agent_assist"

MARKER_RULES = (
    "Every ⟦P:...⟧ marker stands for a formula, symbol or layout boundary drawn separately: keep each one exactly "
    "as written, exactly once, in the original order, at the position of what it stands for. Leave code, "
    "identifiers, URLs and personal names as they are. Use the glossary's Chinese terms exactly."
)
TRANSLATE_SYSTEM = (
    "You translate one passage of an English document into Simplified Chinese for a published translation. "
    + MARKER_RULES + " Return one JSON object: translated_text is the complete Chinese passage; note says in one "
    "short sentence what you changed compared with the current translation."
)
TRANSLATE_SCHEMA = {
    "type": "object",
    "properties": {"translated_text": {"type": "string"}, "note": {"type": "string"}},
    "required": ["translated_text", "note"],
    "additionalProperties": False,
}
REVIEW_SYSTEM = (
    "You review Simplified Chinese translations of passages from an English document. Report only problems a "
    "reader would be hurt by: a wrong or lost meaning, text left untranslated, a wrong technical term, or Chinese "
    "that cannot be read. Wording you would merely prefer is not a problem; return an empty list when there is "
    "nothing. For each problem give fixed_translation, a complete corrected translation of that whole unit. "
    + MARKER_RULES
)
REVIEW_SCHEMA = {
    "type": "object",
    "properties": {"problems": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "unit_id": {"type": "string"},
            "severity": {"type": "string", "enum": ["wrong_meaning", "omission", "wrong_term", "unreadable", "minor"]},
            "explanation": {"type": "string"},
            "fixed_translation": {"type": "string"},
        },
        "required": ["unit_id", "severity", "explanation", "fixed_translation"],
        "additionalProperties": False,
    }}},
    "required": ["problems"],
    "additionalProperties": False,
}
ASK_SYSTEM = (
    "You help an agent finish the Simplified Chinese translation of an English PDF. Units are passages with a "
    "source and a translation; ⟦P:...⟧ markers stand for formulas, symbols or layout boundaries drawn separately. "
    "When page images are attached, each page comes as the original followed by the rendered translation. "
    "Answer precisely and briefly."
)


class AssistError(ValueError):
    """A request this module refuses before anything is sent; the message says why."""


@dataclass
class JobContext:
    """What a task needs from one finished run."""

    config: RunConfig
    capture: Path
    source: Path
    store: StateStore
    rows: list[dict]
    units: dict[str, TranslationUnit]
    log: Path | None = None

    @classmethod
    def load(cls, config: RunConfig, capture: Path, source: Path, runs_root: Path, log: Path | None = None) -> "JobContext":
        review = capture / "content-review.jsonl"
        if not review.is_file():
            raise AssistError("this job has no translation yet (content-review.jsonl is missing)")
        rows = [json.loads(line) for line in review.read_text(encoding="utf-8").splitlines() if line.strip()]
        return cls(config, capture, source, StateStore(runs_root / config.run_id / "state.sqlite"), rows,
                   load_units(capture), log)

    @property
    def rows_by_id(self) -> dict[str, dict]:
        return {row["unit_id"]: row for row in self.rows}

    def page_count(self) -> int:
        return max((page for row in self.rows for page in row.get("source_pages", [])), default=0)


@dataclass
class Meter:
    """Prices, reserves and books every request of one task."""

    context: JobContext
    budget_usd: float
    calls: int = 0
    cost_usd: float = 0.0
    _in_flight: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def estimate(self, request: LLMRequest) -> float:
        tokens = (len(request.system) + len(request.user)) // 2 + IMAGE_TOKENS * len(request.images)
        config = self.context.config
        cost = estimate_cost(tokens, request.max_output_tokens, config.input_usd_per_million, config.output_usd_per_million)
        if cost is None:
            raise AssistError("the job's model has no price; the budget cannot be enforced")
        return cost

    def check_affordable(self, requests: list[LLMRequest]) -> float:
        """Refuse the whole task up front when its ceiling does not fit the budget."""
        total = sum(self.estimate(request) for request in requests)
        spent = self.context.store.spent(self.context.config.run_id)
        if spent + total > self.budget_usd:
            raise BudgetExceeded(f"this task may cost up to USD {total:.4f}; the job has spent {spent:.4f} of "
                                 f"{self.budget_usd:.4f}. Raise budget_usd to allow it.")
        return total

    def call(self, client: LLMClient, request: LLMRequest, subject: str) -> LLMResponse:
        estimate = self.estimate(request)
        config = self.context.config
        with self._lock:
            reserve_budget(self.context.store.spent(config.run_id), self._in_flight, estimate, self.budget_usd)
            self._in_flight += estimate
        try:
            response = client.complete(request)
        finally:
            with self._lock:
                self._in_flight -= estimate
        usage = response.usage
        if usage.cost_usd is None:
            usage = usage.model_copy(update={"cost_usd": estimate_cost(
                usage.input_tokens or 0, usage.output_tokens or 0, config.input_usd_per_million, config.output_usd_per_million)})
        with self._lock:
            self.context.store.record_usage(config.run_id, subject, usage, SOURCE)
            self.calls += 1
            self.cost_usd += usage.cost_usd or 0.0
        if response.truncated_reason:
            raise RuntimeError(f"reply truncated ({response.truncated_reason}); raise max_output_tokens")
        return response


# --- context the job model is given ----------------------------------------------------

def _styles(context: JobContext, unit: TranslationUnit) -> list[str]:
    path = context.config.context_db
    if not path or not Path(path).is_file():
        return []
    rules = [rule for rule in ContextStore(Path(path)).styles() if rule.status == "approved" and (
        rule.scope.kind not in {"document", "unit"} or rule.scope.value in {unit.document_id, unit.unit_id})]
    return [rule.content for rule in sorted(rules, key=lambda r: r.priority)]


def _glossary(context: JobContext, text: str) -> list[str]:
    path = context.config.context_db
    if not path or not Path(path).is_file():
        return []
    lowered = text.lower()
    return [f"{entry.source_term} → {entry.target_term}" for entry in ContextStore(Path(path)).glossary()
            if entry.status == "approved" and entry.source_term.lower() in lowered]


def _neighbours(context: JobContext, unit_id: str) -> tuple[dict | None, dict | None]:
    index = next(i for i, row in enumerate(context.rows) if row["unit_id"] == unit_id)
    before = context.rows[index - 1] if index > 0 else None
    after = context.rows[index + 1] if index + 1 < len(context.rows) else None
    return before, after


def _tag(name: str, body: str | None) -> str:
    return f"<{name}>\n{body}\n</{name}>\n" if body else ""


def _translate_request(context: JobContext, unit_id: str, instruction: str, max_output_tokens: int) -> LLMRequest:
    unit = context.units[unit_id]
    row = context.rows_by_id[unit_id]
    before, after = _neighbours(context, unit_id)
    glossary = _glossary(context, unit.source_text)
    user = (_tag("style", "\n".join(_styles(context, unit)))
            + _tag("glossary", "\n".join(glossary))
            + _tag("previous_passage", before and f"{before['source_text']}\n译文：{before.get('translated_text') or ''}")
            + _tag("source", unit.source_text)
            + _tag("current_translation", row.get("translated_text"))
            + _tag("next_passage", after and after["source_text"])
            + _tag("instruction", instruction))
    budget = min(8000, max(max_output_tokens, 300 + 3 * len(unit.source_text)))
    return LLMRequest(system=TRANSLATE_SYSTEM, user=user, model_id=context.config.model_id, max_output_tokens=budget,
                      json_schema=TRANSLATE_SCHEMA, schema_name="translation",
                      reasoning_effort=context.config.reasoning_effort)


def candidate(context: JobContext, unit_id: str, new_text: str, reason: str) -> dict:
    """A proposed replacement with the quality gate's verdict; ``fix`` is set only when it may be applied."""
    unit = context.units[unit_id]
    row = context.rows_by_id[unit_id]
    old = row.get("translated_text")
    # Judged exactly as apply_fixes will judge it, so ``fix`` is set only when it can be applied.
    result = TranslationResult(
        request_id="agent-assist", unit_id=unit_id, draft=TranslationDraft(unit_id=unit_id, translated_text=new_text),
        finish="complete", provider_response_id=None, model=make_request(unit, context.config).model,
        usage=Usage(input_tokens=0, output_tokens=0, cost_usd=0, usage_source="estimated"))
    quality = check_quality(unit, result)
    answer = {"unit_id": unit_id, "pages": row.get("source_pages", []), "old_translated_text": old,
              "new_translated_text": new_text, "quality": quality.status,
              "issues": [f"{issue.category}: {issue.explanation}" for issue in quality.issues]}
    if new_text.strip() and new_text != old and quality.status != "blocked":
        answer["fix"] = {"unit_id": unit_id, "source_hash": unit.source_hash, "old_translated_text": old,
                         "new_translated_text": new_text, "reason": reason}
    return answer


# --- tasks ------------------------------------------------------------------------------

def _checked_units(context: JobContext, unit_ids: list[str] | None, *, needed: bool = True) -> list[str]:
    unit_ids = list(dict.fromkeys(unit_ids or []))
    if needed and not unit_ids:
        raise AssistError("name the units to work on (unit_ids); list them with get_units")
    if len(unit_ids) > MAX_UNITS:
        raise AssistError(f"at most {MAX_UNITS} units per request; split the work")
    unknown = [uid for uid in unit_ids if uid not in context.units or uid not in context.rows_by_id]
    if unknown:
        raise AssistError(f"unknown units: {unknown[:5]}")
    return unit_ids


def _checked_pages(context: JobContext, pages: list[int] | None, *, needed: bool = True) -> list[int]:
    pages = sorted(set(pages or []))
    if needed and not pages:
        raise AssistError("name the pages (one-based)")
    if len(pages) > MAX_PAGES:
        raise AssistError(f"at most {MAX_PAGES} pages per request; split the work")
    last = context.page_count()
    if any(page < 1 or page > last for page in pages):
        raise AssistError(f"pages run from 1 to {last}")
    return pages


def _parallel(meter: Meter, work, items: list) -> tuple[dict, dict]:
    """Run ``work(item)`` concurrently; per-item errors are reported, account refusals stop everything."""
    results: dict = {}
    errors: dict = {}

    def guarded(item):
        try:
            results[item] = work(item)
        except BudgetExceeded as error:
            errors[item] = f"not sent: {error}"
        except Exception as error:
            if getattr(error, "status_code", None) in ACCOUNT_REFUSALS:
                raise
            errors[item] = f"{type(error).__name__}: {error}"[:300]

    workers = max(1, min(meter.context.config.translation_concurrency, len(items)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(guarded, items))
    return results, errors


def retranslate(context: JobContext, meter: Meter, client: LLMClient, unit_ids: list[str], *,
                instruction: str | None = None, shorten_to: float | None = None, max_output_tokens: int = 2000) -> dict:
    unit_ids = _checked_units(context, unit_ids)
    requests = {}
    for uid in unit_ids:
        current = context.rows_by_id[uid].get("translated_text") or ""
        if shorten_to is not None:
            if not current:
                raise AssistError(f"{uid} has no translation to shorten; use retranslate")
            limit = max(1, int(len(current) * shorten_to))
            ask = (f"The current translation does not fit its box. Rewrite it in at most {limit} characters "
                   f"(it has {len(current)}), keeping the full meaning; drop filler, not content.")
            ask += f"\n{instruction}" if instruction else ""
        else:
            ask = instruction or "Translate the source again, faithfully and fluently."
        requests[uid] = _translate_request(context, uid, ask, max_output_tokens)
    meter.check_affordable(list(requests.values()))

    def one(uid: str) -> dict:
        reply = json.loads(meter.call(client, requests[uid], f"assist:{uid}").text)
        text = str(reply.get("translated_text", "")).strip()
        reason = f"agent assist ({'shorten' if shorten_to else 'retranslate'}): {reply.get('note', '')}".strip()
        answer = candidate(context, uid, text, reason)
        answer["note"] = reply.get("note", "")
        if shorten_to is not None and len(text) >= len(context.rows_by_id[uid].get("translated_text") or ""):
            answer.pop("fix", None)
            answer["issues"].append("not shorter than the current translation")
        return answer

    results, errors = _parallel(meter, one, unit_ids)
    return {"candidates": [results[uid] for uid in unit_ids if uid in results], "errors": errors}


def review_units(context: JobContext, meter: Meter, client: LLMClient, unit_ids: list[str], *,
                 instruction: str | None = None, max_output_tokens: int = 4000) -> dict:
    unit_ids = _checked_units(context, unit_ids)
    batches = [unit_ids[i:i + REVIEW_BATCH] for i in range(0, len(unit_ids), REVIEW_BATCH)]
    requests = {}
    for number, batch in enumerate(batches):
        payload = [{"unit_id": uid, "source": context.units[uid].source_text,
                    "translation": context.rows_by_id[uid].get("translated_text") or "",
                    "automatic_warnings": sorted({i["category"] for i in context.rows_by_id[uid].get("issues", [])})}
                   for uid in batch]
        glossary = sorted({line for uid in batch for line in _glossary(context, context.units[uid].source_text)})
        user = (_tag("glossary", "\n".join(glossary)) + _tag("instruction", instruction)
                + _tag("units", json.dumps(payload, ensure_ascii=False)))
        budget = min(8000, max(max_output_tokens, 400 + 3 * sum(len(p["translation"]) for p in payload)))
        requests[number] = LLMRequest(system=REVIEW_SYSTEM, user=user, model_id=context.config.model_id,
                                      max_output_tokens=budget, json_schema=REVIEW_SCHEMA, schema_name="unit_review",
                                      reasoning_effort=context.config.reasoning_effort)
    meter.check_affordable(list(requests.values()))

    def one(number: int) -> list[dict]:
        reply = json.loads(meter.call(client, requests[number], f"assist:review-{number:03d}").text)
        found = []
        for problem in reply.get("problems", []):
            uid = problem.get("unit_id")
            if uid not in batches[number]:
                continue  # the model named a unit it was not shown
            answer = candidate(context, uid, str(problem.get("fixed_translation", "")).strip(),
                               f"agent assist review ({problem.get('severity')}): {problem.get('explanation', '')}")
            answer.update(severity=problem.get("severity"), explanation=problem.get("explanation", ""))
            found.append(answer)
        return found

    results, errors = _parallel(meter, one, list(requests))
    return {"units_reviewed": len(unit_ids), "problems": [p for n in sorted(results) for p in results[n]],
            "errors": {f"batch-{n}": e for n, e in errors.items()}}


def review_page_layout(context: JobContext, meter: Meter, client: LLMClient, pages: list[int], *,
                       vision: bool = True, dpi: int = 72) -> dict:
    pages = _checked_pages(context, pages)
    views = detect_page_issues(context.source, context.capture, context.source.stem, context.rows, context.units,
                               only=set(pages))
    if not views:
        return {"pages_reviewed": [], "problems": [], "note": "no translated units on these pages"}
    config = context.config.model_copy(update={"budget_usd": meter.budget_usd})
    spent_before = context.store.spent(config.run_id)
    report = review_pages(views, client=client, config=config, store=_Rebooked(context.store), source_pdf=context.source,
                          capture=context.capture, stem=context.source.stem, units=context.units, vision=vision,
                          dpi=dpi, workers=config.translation_concurrency)
    meter.calls += report.calls
    meter.cost_usd += context.store.spent(config.run_id) - spent_before
    fixes = {fix["unit_id"]: fix for fix in report.accepted_fixes(context.rows_by_id)}
    problems = []
    for decision in report.decisions:
        entry = {"page": decision.page, "unit_id": decision.unit_id, "kind": decision.kind,
                 "severity": decision.severity, "explanation": decision.explanation,
                 "old_translated_text": decision.old_translation, "new_translated_text": decision.new_translation,
                 "validation": decision.reason}
        if decision.unit_id in fixes and decision.accepted:
            entry["fix"] = fixes[decision.unit_id]
        problems.append(entry)
    return {"pages_reviewed": report.pages_reviewed, "vision": report.vision, "problems": problems,
            "errors": {str(k): v for k, v in report.errors.items()}, "stopped": report.stopped}


class _Rebooked:
    """The store as layout review sees it, with its calls booked as agent requests."""

    def __init__(self, store: StateStore):
        self._store = store

    def record_usage(self, run_id: str, subject: str, usage: Usage, source: str) -> None:
        self._store.record_usage(run_id, f"assist:{subject}", usage, SOURCE)

    def __getattr__(self, name: str):
        return getattr(self._store, name)


def ask(context: JobContext, meter: Meter, client: LLMClient, prompt: str, *, unit_ids: list[str] | None = None,
        pages: list[int] | None = None, images: bool = False, system: str | None = None,
        json_schema: dict | None = None, max_output_tokens: int = 2000, dpi: int = 72) -> dict:
    if not prompt or not prompt.strip():
        raise AssistError("ask needs a prompt")
    unit_ids = _checked_units(context, unit_ids, needed=False)
    pages = _checked_pages(context, pages, needed=False)
    if images and not pages:
        raise AssistError("images needs pages")
    attached = [{"unit_id": uid, "pages": context.rows_by_id[uid].get("source_pages", []),
                 "source": context.units[uid].source_text, "translation": context.rows_by_id[uid].get("translated_text")}
                for uid in unit_ids]
    user = prompt.strip() + "\n" + _tag("units", attached and json.dumps(attached, ensure_ascii=False))
    pictures: tuple[bytes, ...] = ()
    if images:
        stem = context.source.stem
        for page in pages:
            rendered = context.capture / f"page-{page:04d}" / f"{stem}.zh.mono.pdf"
            if not rendered.is_file():
                raise AssistError(f"page {page} has no rendered translation")
            pictures += (_page_image(context.source, page - 1, dpi), _page_image(rendered, 0, dpi))
    request = LLMRequest(system=system or ASK_SYSTEM, user=user, model_id=context.config.model_id,
                         max_output_tokens=min(max_output_tokens, 16000), json_schema=json_schema,
                         schema_name="answer", images=pictures, reasoning_effort=context.config.reasoning_effort)
    meter.check_affordable([request])
    response = meter.call(client, request, "assist:ask")
    answer: dict = {"text": response.text}
    if json_schema is not None:
        try:
            answer["json"] = json.loads(response.text)
        except ValueError:
            answer["json_error"] = "the reply is not valid JSON"
    return answer


def run_task(context: JobContext, client: LLMClient, task: str, *, budget_usd: float | None = None,
             unit_ids: list[str] | None = None, pages: list[int] | None = None, instruction: str | None = None,
             prompt: str | None = None, system: str | None = None, json_schema: dict | None = None,
             images: bool = False, vision: bool = True, shorten_to: float = 0.8,
             max_output_tokens: int | None = None) -> dict:
    """Run one task and return its answer, cost and budget state."""
    if task not in TASKS:
        raise AssistError(f"unknown task {task!r}; use one of {sorted(TASKS)}")
    if not 0.2 <= shorten_to < 1:
        raise AssistError("shorten_to is the share of the current length to aim for, between 0.2 and 1")
    meter = Meter(context, budget_usd if budget_usd is not None else context.config.budget_usd)
    limit = {"max_output_tokens": max_output_tokens} if max_output_tokens else {}
    if task == "retranslate":
        body = retranslate(context, meter, client, unit_ids, instruction=instruction, **limit)
    elif task == "shorten":
        body = retranslate(context, meter, client, unit_ids, instruction=instruction, shorten_to=shorten_to, **limit)
    elif task == "review_units":
        body = review_units(context, meter, client, unit_ids, instruction=instruction, **limit)
    elif task == "review_pages":
        body = review_page_layout(context, meter, client, pages, vision=vision)
    else:
        body = ask(context, meter, client, prompt or "", unit_ids=unit_ids, pages=pages, images=images,
                   system=system, json_schema=json_schema, **limit)
    config = context.config
    answer = {"task": task, "model": f"{config.provider}/{config.model_id}", **body, "calls": meter.calls,
              "cost_usd": round(meter.cost_usd, 6), "spent_usd": round(context.store.spent(config.run_id), 6),
              "budget_usd": meter.budget_usd}
    if context.log is not None:
        entry = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "task": task,
                 "units": unit_ids or [], "pages": pages or [], "calls": meter.calls, "cost_usd": answer["cost_usd"],
                 "fixes_proposed": sum("fix" in c for c in body.get("candidates", body.get("problems", [])))}
        with context.log.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return answer
