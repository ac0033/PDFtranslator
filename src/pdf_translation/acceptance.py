"""Decide, without a human, whether a rendered translation can be delivered.

Every input this needs already exists after a run: per-unit state and quality
issues in the run store, per-page output on disk, and the merged books. What
was missing is the aggregator that turns them into one machine readable
verdict, so an unattended run can stop instead of shipping a bad book.

Two things it insists on.

**It grades the run store, not the review queue.** ``content-review.jsonl`` is
a snapshot written when the run rendered. Later curated-TM fixes go into the
store and into the pages, and the snapshot is left behind: on val.pdf 25 units,
including all five confirmed number errors, still read as broken in the queue
while the books on disk were already correct. Grading that file would have
described a book nobody shipped. The queue is still used for what cannot
change - which unit covers which page - and its staleness is reported.

**It judges thresholds on warnings a human still has to read**, not on raw
warning counts. :mod:`pdf_translation.triage` dismisses warnings that are
provably artifacts of how a check tokenises text, and warnings a reviewer has
already judged harmless. On val that turns 136 number warnings into 6.

The grading follows AGENTS.md section 2. Major problems block delivery. Minor
problems do not, but section 2 also says many small defects add up to an
unreadable document, so each category carries its own share threshold: an
unrecoverable formula literal is a known capability boundary and is tolerated
broadly, while an untranslated paragraph is barely tolerated at all.

Per section 5 the report always states what was checked and what was not. It
never claims the prose is a faithful translation: that needs a human against
the source pages, and the report says how many pages that covers.
"""
from __future__ import annotations

import json
from pathlib import Path

import pymupdf

from .capture_coverage import reconcile_capture
from .triage import (
    DISMISSED_BY_LEDGER,
    DISMISSED_BY_MODEL,
    DISMISSED_BY_RULE,
    NEEDS_HUMAN,
    DocumentProfile,
    ReviewLedger,
    triage_rows,
)

#: Unit states that mean the translation never reached a deliverable form.
BLOCKING_STATES = ("blocked", "exhausted", "prepared", "not_started")

#: Share of all units a single minor category may reach, counting only the
#: warnings triage could not dismiss, before AGENTS.md section 2 escalation
#: applies. Categories absent here use the default.
DEFAULT_MINOR_THRESHOLDS = {
    # The formula image is preserved either way, so this marks the known
    # capability boundary of the capture layer, not a defect in the prose.
    "protected_source_literal_unavailable": 0.60,
    # A changed number inventory is the strongest available signal of a real
    # mistranslation once the tokenisation artifacts are gone.
    "number": 0.05,
    # Marker order usually shifts because Chinese word order differs. No rule
    # separates the rest, so these stay a human's problem.
    "protected_marker_order": 0.05,
    # A paragraph that came back in English is an AGENTS.md section 2 major
    # problem; a handful is tolerable, a document full of them is not.
    "untranslated_unit": 0.05,
    # Markup that renders literally: visible garbage, cheap to detect.
    "leaked_markup": 0.01,
    # Advisory. The detector has measured noise - on val it lists 231 terms,
    # most of them proper nouns - so it informs review rather than blocking.
    "retained_english": 0.30,
    "url": 0.02,
    "terminology": 0.02,
}
DEFAULT_CATEGORY_THRESHOLD = 0.05

#: Share of all units that may carry any unresolved minor issue at all.
DEFAULT_TOTAL_MINOR_THRESHOLD = 0.80

#: How many representative pages or terms to name per category.
SAMPLE_SIZE = 10

DELIVERABLE = "deliverable"
DELIVERABLE_WITH_KNOWN_ISSUES = "deliverable_with_known_issues"
NOT_DELIVERABLE = "not_deliverable"


def page_output_paths(render_dir: Path, stem: str, page_number: int) -> tuple[Path, Path]:
    """Locate the mono and dual PDFs one rendered page is expected to produce.

    This is the page-level checkpoint contract shared by the renderer, which
    writes it, and acceptance, which verifies it.
    """
    page_dir = render_dir / f"page-{page_number:04d}"
    return page_dir / f"{stem}.zh.mono.pdf", page_dir / f"{stem}.zh.dual.pdf"


def page_is_rendered(render_dir: Path, stem: str, page_number: int) -> bool:
    mono, dual = page_output_paths(render_dir, stem, page_number)
    return mono.is_file() and dual.is_file()


def missing_rendered_pages(render_dir: Path, stem: str, page_numbers: list[int]) -> list[int]:
    return [number for number in page_numbers if not page_is_rendered(render_dir, stem, number)]


def load_review_queue(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def refresh_from_store(queue_rows: list[dict], store, run_id: str) -> tuple[list[dict], list[dict]]:
    """Replace each unit's translation and verdict with what the store holds.

    The queue keeps only what cannot change - the unit's source text and the
    pages it covers. Returns the refreshed rows and the units whose queue entry
    no longer matches, which is what "the review file is stale" means.
    """
    quality = {row["unit_id"]: row for row in store.quality_reports(run_id)}
    results = store.results(run_id)
    refreshed: list[dict] = []
    stale: list[dict] = []
    for row in queue_rows:
        unit_id = row["unit_id"]
        report = quality.get(unit_id)
        result = results.get(unit_id)
        if report is None:
            refreshed.append(row)
            continue
        translated = result.draft.translated_text if result and result.draft else None
        quality_payload = report.get("quality") or {}
        current = {
            **row,
            "translated_text": translated,
            "state": report.get("state") or "not_started",
            "issues": quality_payload.get("issues", []),
            "result_hash": quality_payload.get("result_hash"),
        }
        if (row.get("translated_text"), row.get("state")) != (translated, current["state"]):
            stale.append({"unit_id": unit_id, "pages": row.get("source_pages") or []})
        refreshed.append(current)
    return refreshed, stale


def _problem(kind: str, severity: str, count: int, detail: str, **extra) -> dict:
    return {"kind": kind, "severity": severity, "count": count, "detail": detail, **extra}


def _sample(values) -> list:
    return sorted(values)[:SAMPLE_SIZE]


def _grade_units(rows: list[dict]) -> tuple[list[dict], dict]:
    """Find the unit-level problems that block delivery outright."""
    major: list[dict] = []
    states: dict[str, int] = {}
    blocking_pages: dict[str, set[int]] = {}
    critical_pages: set[int] = set()
    critical_units: list[str] = []
    untranslated: list[str] = []
    untranslated_pages: set[int] = set()

    for row in rows:
        state = row.get("state") or "not_started"
        states[state] = states.get(state, 0) + 1
        pages = set(row.get("source_pages") or [])
        if state in BLOCKING_STATES:
            blocking_pages.setdefault(state, set()).update(pages)
        if not (row.get("translated_text") or "").strip():
            untranslated.append(row["unit_id"])
            untranslated_pages.update(pages)
        if any(issue.get("severity") == "critical" for issue in row.get("issues") or []):
            critical_units.append(row["unit_id"])
            critical_pages.update(pages)

    for state, pages in sorted(blocking_pages.items()):
        major.append(_problem(
            f"unit_state_{state}", "major", states[state],
            f"{states[state]} units are in state '{state}' and never reached a deliverable translation",
            sample_pages=_sample(pages),
        ))
    if critical_units:
        major.append(_problem(
            "critical_quality_issue", "major", len(critical_units),
            f"{len(critical_units)} units carry a critical quality issue",
            sample_units=critical_units[:SAMPLE_SIZE], sample_pages=_sample(critical_pages),
        ))
    if untranslated:
        major.append(_problem(
            "missing_translation", "major", len(untranslated),
            f"{len(untranslated)} units have no translated text",
            sample_units=untranslated[:SAMPLE_SIZE], sample_pages=_sample(untranslated_pages),
        ))
    return major, states


def _grade_render(render_dir: Path, source: Path, output_prefix: str) -> tuple[list[dict], dict]:
    """Check that every source page reached the books, and that they exist."""
    major: list[dict] = []
    stem = source.stem
    with pymupdf.open(source) as document:
        source_pages = len(document)
    missing = missing_rendered_pages(render_dir, stem, list(range(1, source_pages + 1)))
    if missing:
        major.append(_problem(
            "missing_rendered_page", "major", len(missing),
            "pages have no complete mono/dual render output", pages=missing[:SAMPLE_SIZE],
        ))
    books: dict[str, int | None] = {}
    for kind in ("mono", "dual"):
        path = render_dir / f"{stem}.{output_prefix}.zh.{kind}.pdf"
        if not path.is_file():
            books[path.name] = None
            major.append(_problem("missing_book", "major", 1, f"merged {kind} book is absent: {path.name}"))
            continue
        with pymupdf.open(path) as document:
            books[path.name] = len(document)
        if books[path.name] != source_pages:
            major.append(_problem(
                "page_count_mismatch", "major", abs(books[path.name] - source_pages),
                f"{path.name} has {books[path.name]} pages but the source has {source_pages}",
            ))
    return major, {"source_pages": source_pages, "rendered_pages": source_pages - len(missing), "books": books}


def _grade_minor(triage: dict, total_units: int, thresholds: dict[str, float], total_threshold: float) -> tuple[list[dict], list[dict]]:
    """Aggregate unresolved warnings and escalate any category that ran away."""
    escalated: list[dict] = []
    summary: list[dict] = []
    pages_by_category: dict[str, set[int]] = {}
    for item in triage["residual"]:
        pages_by_category.setdefault(item["category"], set()).update(item.get("pages") or [])

    for category, counts in sorted(triage["categories"].items(), key=lambda item: -item[1][NEEDS_HUMAN]):
        outstanding = counts[NEEDS_HUMAN]
        if not outstanding:
            continue
        limit = thresholds.get(category, DEFAULT_CATEGORY_THRESHOLD)
        share = outstanding / total_units if total_units else 0.0
        entry = {
            "category": category,
            "raised": counts["total"],
            "dismissed_by_rule": counts[DISMISSED_BY_RULE],
            "dismissed_by_ledger": counts[DISMISSED_BY_LEDGER],
            "model_says_not_a_problem": counts[DISMISSED_BY_MODEL],
            "needs_human": outstanding,
            "unit_share": round(share, 4),
            "threshold": limit,
            "sample_pages": _sample(pages_by_category.get(category, set())),
        }
        if counts["rules"]:
            entry["rules_applied"] = counts["rules"]
        summary.append(entry)
        if share > limit:
            escalated.append(_problem(
                "minor_issue_accumulation", "major", outstanding,
                f"'{category}' still needs human review on {share:.1%} of units, above the {limit:.0%} threshold; "
                "AGENTS.md section 2 escalates accumulated minor defects to a major problem",
                category=category, sample_pages=entry["sample_pages"],
            ))
    outstanding_total = sum(entry["needs_human"] for entry in summary)
    total_share = outstanding_total / total_units if total_units else 0.0
    if total_share > total_threshold:
        escalated.append(_problem(
            "minor_issue_accumulation", "major", outstanding_total,
            f"{total_share:.1%} of units still need human review, above the {total_threshold:.0%} threshold",
            category="__total__",
        ))
    return escalated, summary


def build_acceptance_report(
    source: Path,
    render_dir: Path,
    content_review: Path,
    *,
    output_prefix: str = "usable-deepseek",
    thresholds: dict[str, float] | None = None,
    total_threshold: float = DEFAULT_TOTAL_MINOR_THRESHOLD,
    store=None,
    run_id: str | None = None,
    ledger: ReviewLedger | None = None,
    capture_map: Path | None = None,
) -> dict:
    """Grade one rendered translation and return a machine-readable verdict."""
    queue_rows = load_review_queue(content_review)
    limits = {**DEFAULT_MINOR_THRESHOLDS, **(thresholds or {})}
    ledger = ledger or ReviewLedger([])

    if store is not None and run_id:
        rows, stale = refresh_from_store(queue_rows, store, run_id)
        graded_from = "run_store"
    else:
        rows, stale, graded_from = queue_rows, [], "review_queue"

    unit_major, states = _grade_units(rows)
    render_major, render = _grade_render(render_dir, source, output_prefix)
    triage = triage_rows(rows, ledger=ledger, profile=DocumentProfile(rows))
    escalated, minor_summary = _grade_minor(triage, len(rows), limits, total_threshold)

    coverage_report = None
    coverage_major: list[dict] = []
    if capture_map and Path(capture_map).is_file():
        coverage_report = reconcile_capture(source, Path(capture_map))
        if coverage_report["over_threshold"]:
            coverage_major.append(_problem(
                "uncaptured_prose", "major", coverage_report["uncaptured_words"],
                f"{coverage_report['uncaptured_share']:.2%} of body text never became a translation "
                f"unit, above the {coverage_report['threshold']:.0%} threshold; that text is in the "
                "book untranslated and no other check can see it",
                pages=coverage_report["sample_pages"],
            ))

    major = [*unit_major, *render_major, *escalated, *coverage_major]
    unresolved_formulas = sum(len(row.get("unresolved_formula_markers") or []) for row in queue_rows)
    formula_pages = {page for row in queue_rows if row.get("unresolved_formula_markers") for page in (row.get("source_pages") or [])}

    if major:
        verdict = NOT_DELIVERABLE
    elif minor_summary:
        verdict = DELIVERABLE_WITH_KNOWN_ISSUES
    else:
        verdict = DELIVERABLE

    return {
        "verdict": verdict,
        "source": str(source),
        "render_dir": str(render_dir),
        "graded_from": graded_from,
        "units": {"total": len(rows), "states": states},
        "render": render,
        "major_problems": major,
        "minor_issues": minor_summary,
        "retained_english_terms": triage["retained_english_terms"][:50],
        "capture_coverage": coverage_report,
        "thresholds": {"per_category": limits, "default_per_category": DEFAULT_CATEGORY_THRESHOLD, "total": total_threshold},
        "review_queue": {
            "path": str(content_review),
            "stale_units": len(stale),
            "sample_stale_pages": _sample({page for item in stale for page in item["pages"]}),
            "note": (
                "The queue is a snapshot from render time; later fixes live in the run store. "
                "Stale entries do not affect this verdict, which was graded from the store, but the "
                "queue should be regenerated before a human reads it."
            ) if graded_from == "run_store" else (
                "No run store was supplied, so this verdict rests on the queue file alone and its "
                "staleness could not be checked."
            ),
        },
        "ledger": {
            "dismissals_applied": sum(counts[DISMISSED_BY_LEDGER] for counts in triage["categories"].values()),
            "model_verdicts_applied": sum(counts[DISMISSED_BY_MODEL] for counts in triage["categories"].values()),
            "stale_entries": len(triage["stale_ledger_entries"]),
            "retained_terms": len(ledger.retained_terms),
            "note": (
                "A model verdict annotates a warning but does not clear it. AGENTS.md section 5: "
                "a model asserting there is no problem is not evidence that there is none."
            ),
        },
        "coverage": {
            "checked": [
                "unit state reached a deliverable translation",
                "no critical quality issue survived",
                "every unit has translated text",
                "every source page has complete mono/dual render output",
                "both merged books exist and match the source page count",
                "number warnings, after dismissing tokenisation artifacts and reviewed cases",
                "paragraphs that came back in the source language, excluding bibliography entries",
                "markup tags the translation introduced",
                "English terms left in Chinese prose (advisory; the detector has known noise)",
                "unresolved warning accumulation against per-category thresholds",
                "body text the capture layer never delivered, reconciled against an independent extractor",
            ],
            "not_checked": [
                "whether the Chinese text is a faithful translation of the source",
                "whether preserved formula images sit beside the right prose",
                "whether a retained English term should have been translated",
                "marker order correctness: 1 real case in 44 on val, too few to build a rule on",
                "visual layout quality beyond page presence and page count",
            ],
            "human_review_outstanding": {
                "warnings": len(triage["residual"]),
                "unresolved_formula_markers": unresolved_formulas,
                "pages_with_unresolved_formulas": len(formula_pages),
                "note": "formula placeholder meaning is not machine checkable here; those pages need a human against the source PDF",
            },
            "limitation": "this verdict covers the artifacts named above only; it is not evidence about any other document",
        },
    }


def format_summary(report: dict) -> str:
    """Render the verdict for a terminal, leading with what a reader must know."""
    units = report["units"]
    lines = [f"verdict: {report['verdict']} (graded from {report['graded_from']})"]
    lines.append(f"units: {units['total']} " + ", ".join(f"{state}={count}" for state, count in sorted(units["states"].items())))
    render = report["render"]
    lines.append(f"pages: {render['rendered_pages']}/{render['source_pages']} rendered; books " + ", ".join(
        f"{name}={pages if pages is not None else 'MISSING'}" for name, pages in render["books"].items()
    ))
    if report["major_problems"]:
        lines.append("major problems (block delivery):")
        for problem in report["major_problems"]:
            lines.append(f"  - [{problem['kind']}] {problem['detail']}")
    else:
        lines.append("major problems: none")
    if report["minor_issues"]:
        lines.append("unresolved warnings (recorded for later optimisation):")
        for entry in report["minor_issues"]:
            dismissed = entry["dismissed_by_rule"] + entry["dismissed_by_ledger"]
            annotated = f", {entry['model_says_not_a_problem']} annotated by a model" if entry["model_says_not_a_problem"] else ""
            origin = f" (from {entry['raised']} raised, {dismissed} dismissed)" if dismissed else ""
            lines.append(
                f"  - {entry['category']}: {entry['needs_human']} units{origin} "
                f"({entry['unit_share']:.1%}, threshold {entry['threshold']:.0%}{annotated}), pages e.g. {entry['sample_pages']}"
            )
    coverage = report.get("capture_coverage")
    if coverage:
        lines.append(
            f"capture coverage: {coverage['uncaptured_words']} of {coverage['prose_words']} body words "
            f"never became a unit ({coverage['uncaptured_share']:.2%}, threshold {coverage['threshold']:.0%}); "
            f"{len(coverage['significant_pages'])} pages worth a look"
            + (f", e.g. {[p['page'] for p in coverage['significant_pages'][:5]]}" if coverage['significant_pages'] else "")
        )
    queue = report["review_queue"]
    if queue["stale_units"]:
        lines.append(f"review queue is stale for {queue['stale_units']} units; regenerate it before a human reads it")
    outstanding = report["coverage"]["human_review_outstanding"]
    lines.append(
        f"not machine checkable: {outstanding['unresolved_formula_markers']} formula placeholders across "
        f"{outstanding['pages_with_unresolved_formulas']} pages need a human against the source"
    )
    return "\n".join(lines)
