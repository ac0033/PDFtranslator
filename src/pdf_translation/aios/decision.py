"""The decision node: is this version usable, and if not, what to do about each problem.

Usability is decided by code - the acceptance verdict built from the delivery
rules - never by a model: a probability is not a delivery rule, and
untranslated text blocks delivery at any count.

For each blocking problem the node picks one repair action. The rule table
lists the actions that can remove each kind of problem, best first. When a
kind has more than one and Jev is available, Jev picks among them; an answer
below :data:`~.jev.CHOICE_CONFIDENCE`, or a kind the table does not know,
goes to the pi agent. Units behind a warning category are filtered through
Jev's "is this real?" judgment when it is available, so repair effort goes to
defects rather than to false alarms of a check.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..acceptance import NOT_DELIVERABLE
from ..triage import unit_is_untranslated
from .jev import CHOICE_CONFIDENCE, REAL_PROBLEM, JevClient, JevError

ACTIONS = {
    "retranslate_units": "translate the affected passages again, telling the model what was wrong",
    "review_units": "have the translation model review the affected passages and correct real errors only",
    "resume_translation": "send the passages that were never translated",
    "rerender": "write the outputs again from the current translations, changing no text",
    "review_pages": "compare the rendered pages with the original pages and fix mismatches (PDF only)",
    "agent": "hand the problem to the AI agent to investigate and fix",
}

#: kind of problem -> actions that can remove it, best first.
RULES = {
    "unit_state_exhausted": ["retranslate_units"],
    "unit_state_blocked": ["retranslate_units"],
    "unit_state_prepared": ["resume_translation"],
    "unit_state_not_started": ["resume_translation"],
    "missing_translation": ["retranslate_units", "resume_translation"],
    "critical_quality_issue": ["retranslate_units", "agent"],
    "untranslated_text": ["retranslate_units", "agent"],
    "missing_rendered_page": ["rerender"],
    "missing_book": ["rerender"],
    "page_count_mismatch": ["rerender", "agent"],
    "missing_output": ["rerender", "agent"],
    "missing_acceptance": ["rerender"],
}
#: warning category (minor_issue_accumulation) -> actions, best first.
CATEGORY_RULES = {
    "number": ["review_units", "retranslate_units", "agent"],
    "url": ["retranslate_units", "review_units"],
    "terminology": ["retranslate_units", "review_units"],
    "protected_marker_order": ["review_units", "agent"],
    "leaked_markup": ["retranslate_units"],
    "retained_english": ["retranslate_units", "agent"],
    "layout_boundary_unverifiable": ["review_pages", "agent"],
    "protected_source_literal_unavailable": ["agent"],
}
INSTRUCTIONS = {
    "untranslated_text": "The previous translation left English sentences. Translate every sentence into Chinese; keep only names, acronyms, code and formulas in English.",
    "critical_quality_issue": "The previous translation failed an automatic check (a lost, extra or reordered marker, or invented markup). Keep every ⟦P:...⟧ marker exactly once and add no markup.",
    "missing_translation": "Translate the whole passage; the previous attempts returned nothing usable.",
    "unit_state_exhausted": "Translate the whole passage; the previous attempts were rejected by the quality checks. Keep every ⟦P:...⟧ marker exactly once.",
    "unit_state_blocked": "Translate the whole passage; the previous attempt was rejected by the quality checks. Keep every ⟦P:...⟧ marker exactly once.",
    "number": "Every number of the source must appear in the translation with the same value.",
    "url": "Copy every URL of the source unchanged.",
    "terminology": "Use the glossary's Chinese terms exactly.",
    "leaked_markup": "Add no tags or markup that are not in the source.",
    "retained_english": "Translate English words and phrases into Chinese unless they are names, acronyms, code or formulas.",
}
MAX_UNITS_PER_ACTION = 200


@dataclass
class Action:
    name: str
    problem: dict
    unit_ids: list[str] = field(default_factory=list)
    pages: list[int] = field(default_factory=list)
    instruction: str | None = None
    chosen_by: str = "rules"
    confidence: float | None = None

    def describe(self) -> dict:
        return {"action": self.name, "problem": self.problem.get("kind"), "category": self.problem.get("category"),
                "units": len(self.unit_ids), "pages": self.pages[:10], "chosen_by": self.chosen_by,
                "confidence": self.confidence}


@dataclass
class Decision:
    usable: bool
    verdict: str
    actions: list[Action]

    def for_agent(self) -> list[Action]:
        return [action for action in self.actions if action.name == "agent"]


def _units_for(problem: dict, rows: list[dict], report: dict | None = None) -> list[str]:
    kind = problem.get("kind", "")
    if kind.startswith("unit_state_"):
        state = kind.removeprefix("unit_state_")
        return [row["unit_id"] for row in rows if (row.get("state") or "not_started") == state]
    if kind == "missing_translation":
        return [row["unit_id"] for row in rows if not (row.get("translated_text") or "").strip()]
    if kind == "critical_quality_issue":
        return [row["unit_id"] for row in rows if any(i.get("severity") == "critical" for i in row.get("issues") or [])]
    if kind == "untranslated_text":
        return [row["unit_id"] for row in rows if row.get("translated_text")
                and unit_is_untranslated(row.get("source_text") or "", row["translated_text"])]
    category = problem.get("category")
    if category:
        return units_with_warning(category, rows, report)
    return []


def units_with_warning(category: str, rows: list[dict], report: dict | None) -> list[str]:
    """Units still carrying an unresolved warning of ``category`` (triage's list, else the quality issues)."""
    residual = (report or {}).get("residual_warnings")
    if residual is not None:
        return list(dict.fromkeys(item["unit_id"] for item in residual if item.get("category") == category))
    return [row["unit_id"] for row in rows if any(i.get("category") == category for i in row.get("issues") or [])]


def _real_units(unit_ids: list[str], category: str, rows_by_id: dict[str, dict], jev: JevClient | None) -> tuple[list[str], dict]:
    """Keep the units whose warning Jev judges real; all of them without Jev."""
    if jev is None or not unit_ids:
        return unit_ids, {}
    kept, judged = [], {}
    for uid in unit_ids[:MAX_UNITS_PER_ACTION]:
        row = rows_by_id[uid]
        issue = next((i for i in row.get("issues") or [] if i.get("category") == category), {})
        try:
            probability = jev.warning_is_real(category=category, explanation=issue.get("explanation", ""),
                                              source=row.get("source_text") or "", translation=row.get("translated_text") or "")
        except JevError:
            return unit_ids, judged  # Jev unavailable mid-way: do not guess, repair them all
        judged[uid] = round(probability, 3)
        if probability >= REAL_PROBLEM:
            kept.append(uid)
    return kept, judged


def decide(report: dict, rows: list[dict], *, fmt: str, jev: JevClient | None = None) -> Decision:
    verdict = report.get("verdict", NOT_DELIVERABLE)
    usable = verdict != NOT_DELIVERABLE
    actions: list[Action] = []
    rows_by_id = {row["unit_id"]: row for row in rows}
    for problem in report.get("major_problems", []):
        kind = problem.get("kind", "")
        category = problem.get("category")
        if kind == "minor_issue_accumulation" and category:
            candidates = list(CATEGORY_RULES.get(category, ["agent"]))
        else:
            candidates = list(RULES.get(kind, ["agent"]))
        if fmt != "pdf":
            candidates = [c for c in candidates if c != "review_pages"] or ["agent"]
        units = _units_for(problem, rows, report)
        judged = {}
        if kind == "minor_issue_accumulation" and category and category != "untranslated_unit":
            units, judged = _real_units(units, category, rows_by_id, jev)
        chosen, by, confidence = candidates[0], "rules", None
        if jev is not None and len(candidates) > 1:
            examples = [{"source": rows_by_id[uid].get("source_text", "")[:400],
                         "translation": (rows_by_id[uid].get("translated_text") or "")[:400]} for uid in units[:3]]
            try:
                choice, confidence = jev.repair_action(problem=problem, options={c: ACTIONS[c] for c in candidates},
                                                       examples=examples)
                chosen, by = (choice, "jev") if confidence >= CHOICE_CONFIDENCE and choice in candidates else ("agent", "jev-low-confidence")
            except JevError:
                pass
        instruction = INSTRUCTIONS.get(category or kind)
        pages = list(problem.get("pages") or problem.get("sample_pages") or [])
        action = Action(chosen, problem, units[:MAX_UNITS_PER_ACTION], pages, instruction, by, confidence)
        if judged:
            action.problem = {**problem, "jev_real_probability": judged}
        actions.append(action)
    return Decision(usable, verdict, actions)
