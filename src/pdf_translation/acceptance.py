"""Decide, without a human, whether a rendered translation can be delivered.

Every input this needs already exists after a run: per-unit state and quality
issues in the review queue, per-page render output on disk, and the merged
books. What was missing is the aggregator that turns them into one machine
readable verdict, so an unattended run can stop instead of shipping a bad book.

The grading follows AGENTS.md section 2. Major problems block delivery. Minor
problems do not, but section 2 also says many small defects can add up to an
unreadable document, so each issue category carries its own share threshold:
a formula placeholder that could not be recovered is a known capability
boundary and is tolerated broadly, while a changed number inventory indicates a
real translation error and is tolerated barely at all.

Per section 5 the report always states what was checked and what was not. It
never claims the placeholder text is semantically correct: that needs a human
against the source pages, and the report says how many pages that covers.
"""
from __future__ import annotations

import json
from pathlib import Path

import pymupdf

#: Unit states that mean the translation never reached a deliverable form.
BLOCKING_STATES = ("blocked", "exhausted", "prepared", "not_started")

#: Share of all units a single minor issue category may reach before AGENTS.md
#: section 2 escalation applies. Categories absent here use the default.
DEFAULT_MINOR_THRESHOLDS = {
    # A formula whose source literal could not be recovered: the formula image
    # is preserved either way, so this marks the known capability boundary of
    # the capture layer rather than a defect in the prose.
    "protected_source_literal_unavailable": 0.60,
    # A changed number inventory is the strongest available signal of a real
    # mistranslation, so very little of it is tolerated.
    "number": 0.05,
    # Marker order usually shifts because Chinese word order differs, but it
    # also catches genuine misplacement.
    "protected_marker_order": 0.05,
    "url": 0.02,
    "terminology": 0.02,
}
DEFAULT_CATEGORY_THRESHOLD = 0.05

#: Share of all units that may carry any minor issue at all.
DEFAULT_TOTAL_MINOR_THRESHOLD = 0.80

#: How many representative pages to name per issue category.
PAGE_SAMPLE_SIZE = 10

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


def _problem(kind: str, severity: str, count: int, detail: str, **extra) -> dict:
    return {"kind": kind, "severity": severity, "count": count, "detail": detail, **extra}


def _sample_pages(pages: set[int]) -> list[int]:
    return sorted(pages)[:PAGE_SAMPLE_SIZE]


def _grade_units(rows: list[dict]) -> tuple[list[dict], dict, dict]:
    """Split unit-level evidence into blocking problems and minor categories."""
    major: list[dict] = []
    states: dict[str, int] = {}
    blocking_pages: dict[str, set[int]] = {}
    critical_pages: set[int] = set()
    critical_units: list[str] = []
    untranslated: list[str] = []
    untranslated_pages: set[int] = set()
    minor: dict[str, dict] = {}

    for row in rows:
        state = row.get("state") or "not_started"
        states[state] = states.get(state, 0) + 1
        pages = set(row.get("source_pages") or [])
        if state in BLOCKING_STATES:
            blocking_pages.setdefault(state, set()).update(pages)
        if not (row.get("translated_text") or "").strip():
            untranslated.append(row["unit_id"])
            untranslated_pages.update(pages)
        for issue in row.get("issues") or []:
            category = issue.get("category", "unknown")
            if issue.get("severity") == "critical":
                critical_units.append(row["unit_id"])
                critical_pages.update(pages)
                continue
            bucket = minor.setdefault(category, {"count": 0, "units": 0, "pages": set()})
            bucket["count"] += 1
            bucket["pages"].update(pages)
        categories = {i.get("category", "unknown") for i in (row.get("issues") or []) if i.get("severity") != "critical"}
        for category in categories:
            minor[category]["units"] += 1

    for state, pages in sorted(blocking_pages.items()):
        major.append(_problem(
            f"unit_state_{state}",
            "major",
            states[state],
            f"{states[state]} units are in state '{state}' and never reached a deliverable translation",
            sample_pages=_sample_pages(pages),
        ))
    if critical_units:
        major.append(_problem(
            "critical_quality_issue",
            "major",
            len(critical_units),
            f"{len(critical_units)} units carry a critical quality issue",
            sample_units=critical_units[:PAGE_SAMPLE_SIZE],
            sample_pages=_sample_pages(critical_pages),
        ))
    if untranslated:
        major.append(_problem(
            "missing_translation",
            "major",
            len(untranslated),
            f"{len(untranslated)} units have no translated text",
            sample_units=untranslated[:PAGE_SAMPLE_SIZE],
            sample_pages=_sample_pages(untranslated_pages),
        ))
    return major, states, minor


def _grade_render(render_dir: Path, source: Path, output_prefix: str) -> tuple[list[dict], dict]:
    """Check that every source page reached the books, and that they exist."""
    major: list[dict] = []
    stem = source.stem
    with pymupdf.open(source) as document:
        source_pages = len(document)
    page_numbers = list(range(1, source_pages + 1))
    missing = missing_rendered_pages(render_dir, stem, page_numbers)
    if missing:
        major.append(_problem(
            "missing_rendered_page",
            "major",
            len(missing),
            "pages have no complete mono/dual render output",
            pages=missing[:PAGE_SAMPLE_SIZE],
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
                "page_count_mismatch",
                "major",
                abs(books[path.name] - source_pages),
                f"{path.name} has {books[path.name]} pages but the source has {source_pages}",
            ))
    return major, {"source_pages": source_pages, "rendered_pages": source_pages - len(missing), "books": books}


def _grade_minor(minor: dict, total_units: int, thresholds: dict[str, float], total_threshold: float) -> tuple[list[dict], list[dict]]:
    """Aggregate minor issues and escalate any category that accumulated too far."""
    escalated: list[dict] = []
    summary: list[dict] = []
    for category, bucket in sorted(minor.items(), key=lambda item: -item[1]["count"]):
        limit = thresholds.get(category, DEFAULT_CATEGORY_THRESHOLD)
        share = bucket["units"] / total_units if total_units else 0.0
        entry = {
            "category": category,
            "issues": bucket["count"],
            "units": bucket["units"],
            "unit_share": round(share, 4),
            "threshold": limit,
            "sample_pages": _sample_pages(bucket["pages"]),
        }
        summary.append(entry)
        if share > limit:
            escalated.append(_problem(
                "minor_issue_accumulation",
                "major",
                bucket["units"],
                f"'{category}' affects {share:.1%} of units, above the {limit:.0%} threshold; "
                "AGENTS.md section 2 escalates accumulated minor defects to a major problem",
                category=category,
                sample_pages=entry["sample_pages"],
            ))
    affected = sum(entry["units"] for entry in summary)
    total_share = affected / total_units if total_units else 0.0
    if total_share > total_threshold:
        escalated.append(_problem(
            "minor_issue_accumulation",
            "major",
            affected,
            f"{total_share:.1%} of units carry at least one minor issue, above the {total_threshold:.0%} threshold",
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
    store_states: dict[str, int] | None = None,
) -> dict:
    """Grade one rendered translation and return a machine-readable verdict."""
    rows = load_review_queue(content_review)
    limits = {**DEFAULT_MINOR_THRESHOLDS, **(thresholds or {})}

    unit_major, states, minor = _grade_units(rows)
    render_major, render = _grade_render(render_dir, source, output_prefix)
    escalated, minor_summary = _grade_minor(minor, len(rows), limits, total_threshold)

    major = [*unit_major, *render_major, *escalated]
    if store_states and store_states != states:
        # The queue is a snapshot. If the live store still holds blocking work
        # the queue does not show, the queue is stale and cannot be graded.
        hidden = {state: store_states.get(state, 0) for state in BLOCKING_STATES if store_states.get(state)}
        if hidden and not any(states.get(state) for state in hidden):
            major.append(_problem(
                "stale_review_queue",
                "major",
                sum(hidden.values()),
                f"the run store reports blocking states {hidden} that the review queue does not; regenerate the queue",
            ))

    unresolved_formulas = sum(len(row.get("unresolved_formula_markers") or []) for row in rows)
    formula_pages = {page for row in rows if row.get("unresolved_formula_markers") for page in (row.get("source_pages") or [])}

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
        "units": {"total": len(rows), "states": states},
        "render": render,
        "major_problems": major,
        "minor_issues": minor_summary,
        "thresholds": {"per_category": limits, "default_per_category": DEFAULT_CATEGORY_THRESHOLD, "total": total_threshold},
        "coverage": {
            "checked": [
                "unit state reached a deliverable translation",
                "no critical quality issue survived",
                "every unit has translated text",
                "every source page has complete mono/dual render output",
                "both merged books exist and match the source page count",
                "minor issue accumulation against per-category thresholds",
            ],
            "not_checked": [
                "whether the Chinese text is a faithful translation of the source",
                "whether preserved formula images sit beside the right prose",
                "visual layout quality beyond page presence and page count",
            ],
            "human_review_outstanding": {
                "unresolved_formula_markers": unresolved_formulas,
                "pages_with_unresolved_formulas": len(formula_pages),
                "note": "formula placeholder meaning is not machine checkable here; these pages need a human against the source PDF",
            },
            "limitation": "this verdict covers the artifacts named above only; it is not evidence about any other document",
        },
    }


def format_summary(report: dict) -> str:
    """Render the verdict for a terminal, leading with what a reader must know."""
    lines = [f"verdict: {report['verdict']}"]
    units = report["units"]
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
        lines.append("minor issues (recorded for later optimisation):")
        for entry in report["minor_issues"]:
            lines.append(
                f"  - {entry['category']}: {entry['units']} units ({entry['unit_share']:.1%} of {units['total']}, "
                f"threshold {entry['threshold']:.0%}), pages e.g. {entry['sample_pages']}"
            )
    outstanding = report["coverage"]["human_review_outstanding"]
    lines.append(
        f"not machine checkable: {outstanding['unresolved_formula_markers']} formula placeholders across "
        f"{outstanding['pages_with_unresolved_formulas']} pages need a human against the source"
    )
    return "\n".join(lines)
