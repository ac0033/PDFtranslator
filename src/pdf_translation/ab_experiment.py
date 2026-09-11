from __future__ import annotations

from pathlib import Path

from .config import RunConfig
from .hashing import sha256_file, stable_hash
from .context_store import ContextStore
from .pipeline import DEFAULT_TRANSLATION_STYLE, estimate_units, output_token_limit
from .schemas import TranslationUnit


ALLOWED_CONFIG_DIFFERENCES = frozenset({"run_id", "context_engine", "context_db"})
MAX_COMBINED_AB_BUDGET_USD = 0.20


def _assert_controlled_pair(fixed: RunConfig, context: RunConfig) -> None:
    if fixed.context_engine != "v0-fixed":
        raise ValueError("fixed arm must use context_engine='v0-fixed'")
    if context.context_engine != "v1" or not context.context_db:
        raise ValueError("context arm must use context_engine='v1' with context_db")
    if fixed.output_token_policy != "adaptive-v1" or context.output_token_policy != "adaptive-v1":
        raise ValueError("both arms must use output_token_policy='adaptive-v1'")
    fixed_data = fixed.model_dump(mode="json")
    context_data = context.model_dump(mode="json")
    unexpected = {
        key: {"fixed": fixed_data[key], "context": context_data[key]}
        for key in fixed_data
        if key not in ALLOWED_CONFIG_DIFFERENCES and fixed_data[key] != context_data[key]
    }
    if unexpected:
        raise ValueError(f"A/B configs differ outside context controls: {unexpected}")


def prepare_ab_manifest(
    units_path: Path,
    units: list[TranslationUnit],
    fixed: RunConfig,
    context: RunConfig,
    *,
    cny_per_usd: float = 7.5,
    planned_budget_usd_per_arm: float = 0.10,
) -> dict:
    """Freeze a zero-call controlled experiment and its conservative ceilings."""
    _assert_controlled_pair(fixed, context)
    if fixed.budget_usd != 0 or context.budget_usd != 0:
        raise ValueError("preparation configs must keep budget_usd=0 until paid approval")
    if planned_budget_usd_per_arm <= 0:
        raise ValueError("planned budget per arm must be positive")
    if planned_budget_usd_per_arm * 2 > MAX_COMBINED_AB_BUDGET_USD:
        raise ValueError(f"planned combined budget exceeds {MAX_COMBINED_AB_BUDGET_USD} USD")
    context_store = ContextStore(Path(context.context_db))
    approved_styles = [rule for rule in context_store.styles() if rule.status == "approved"]
    document_ids = {unit.document_id for unit in units}
    if len(approved_styles) != 1 or approved_styles[0].content != DEFAULT_TRANSLATION_STYLE:
        raise ValueError("context arm must use exactly the same single style text as the fixed arm")
    if approved_styles[0].scope.kind != "document" or approved_styles[0].scope.value not in document_ids:
        raise ValueError("A/B common style must target the captured document")
    if any(entry.status == "approved" for entry in context_store.glossary()):
        raise ValueError("approved glossary entries would confound the neighbor-context A/B")
    if any(entry.status == "approved" for entry in context_store.semantics()):
        raise ValueError("approved semantic entries would confound the neighbor-context A/B")
    for unit in units:
        if any(entry.human_status == "confirmed" for entry in context_store.exact_tm(unit.source_hash)):
            raise ValueError("confirmed translation memory would confound the neighbor-context A/B")
    fixed_estimate = estimate_units(units, fixed)
    context_estimate = estimate_units(units, context)
    if fixed_estimate["billable_unit_count"] != context_estimate["billable_unit_count"]:
        raise ValueError("A/B billable unit counts differ")
    if fixed_estimate["reserved_output_tokens"] != context_estimate["reserved_output_tokens"]:
        raise ValueError("A/B output ceilings differ")
    combined = fixed_estimate["estimated_max_cost_usd"] + context_estimate["estimated_max_cost_usd"]
    billable = [unit for unit in units if unit.unit_type not in {"code", "protected"}]
    caps = [output_token_limit(unit, fixed) for unit in billable]
    fixed_execution = fixed.model_copy(update={"budget_usd": planned_budget_usd_per_arm})
    context_execution = context.model_copy(update={"budget_usd": planned_budget_usd_per_arm})
    return {
        "schema_version": "1.0",
        "experiment": "v0.6-context-ab",
        "status": "prepared_no_api_calls",
        "units_path": str(units_path),
        "units_file_sha256": sha256_file(units_path),
        "units_payload_hash": stable_hash(units),
        "unit_count": len(units),
        "billable_unit_count": len(billable),
        "protected_passthrough_count": len(units) - len(billable),
        "controlled_difference": {"fixed": "common style only", "context": "same common style plus neighboring source excerpts"},
        "common_config": {
            key: value
            for key, value in fixed.model_dump(mode="json").items()
            if key not in ALLOWED_CONFIG_DIFFERENCES
        },
        "fixed_arm": {"run_id": fixed.run_id, "preparation_config_hash": fixed.config_hash, "execution_config_hash": fixed_execution.config_hash, "estimate": fixed_estimate},
        "context_arm": {"run_id": context.run_id, "preparation_config_hash": context.config_hash, "execution_config_hash": context_execution.config_hash, "context_db": context.context_db, "estimate": context_estimate},
        "output_ceiling": {
            "policy": "adaptive-v1",
            "reserved_output_tokens": sum(caps),
            "minimum_per_unit": min(caps) if caps else 0,
            "maximum_per_unit": max(caps) if caps else 0,
        },
        "combined_one_pass_ceiling_usd": combined,
        "control_exchange_rate_cny_per_usd": cny_per_usd,
        "combined_one_pass_ceiling_cny": combined * cny_per_usd,
        "planned_budget_usd_per_arm": planned_budget_usd_per_arm,
        "planned_combined_hard_cap_usd": planned_budget_usd_per_arm * 2,
        "planned_combined_hard_cap_cny": planned_budget_usd_per_arm * 2 * cny_per_usd,
        "api_calls": 0,
        "paid_execution_authorized": False,
        "frozen_test_accessed": False,
    }


def approved_execution_configs(fixed: RunConfig, context: RunConfig, approved_combined_budget_usd: float) -> tuple[RunConfig, RunConfig]:
    """Create equal, bounded arm budgets without modifying the zero-budget files."""
    _assert_controlled_pair(fixed, context)
    if fixed.budget_usd != 0 or context.budget_usd != 0:
        raise ValueError("tracked A/B configs must remain at zero budget")
    if approved_combined_budget_usd <= 0 or approved_combined_budget_usd > MAX_COMBINED_AB_BUDGET_USD:
        raise ValueError(f"approved combined budget must be within (0, {MAX_COMBINED_AB_BUDGET_USD}] USD")
    per_arm = approved_combined_budget_usd / 2
    return fixed.model_copy(update={"budget_usd": per_arm}), context.model_copy(update={"budget_usd": per_arm})


def build_blind_review(
    units: list[TranslationUnit],
    fixed_targets: dict[str, str],
    context_targets: dict[str, str],
    *,
    seed: str = "20260911",
) -> tuple[dict, dict]:
    """Create deterministic anonymous X/Y pairs and a separate reveal key."""
    rows = []
    key_rows = []
    for unit in units:
        if unit.unit_type in {"code", "protected"}:
            continue
        if unit.unit_id not in fixed_targets or unit.unit_id not in context_targets:
            raise ValueError(f"missing accepted A/B target for {unit.unit_id}")
        pair_id = f"pair-{stable_hash({'seed': seed, 'unit_id': unit.unit_id})[:12]}"
        context_is_x = int(stable_hash({"pair_id": pair_id, "order": seed})[-1], 16) % 2 == 0
        x_arm, y_arm = ("context", "fixed") if context_is_x else ("fixed", "context")
        rows.append(
            {
                "pair_id": pair_id,
                "source_text": unit.source_text,
                "section_path": list(unit.section_path),
                "candidate_x": context_targets[unit.unit_id] if context_is_x else fixed_targets[unit.unit_id],
                "candidate_y": fixed_targets[unit.unit_id] if context_is_x else context_targets[unit.unit_id],
                "decision": None,
                "meaning_accuracy": None,
                "continuity": None,
                "terminology": None,
                "reference_clarity": None,
                "chinese_fluency": None,
                "notes": "",
            }
        )
        key_rows.append({"pair_id": pair_id, "unit_id": unit.unit_id, "candidate_x_arm": x_arm, "candidate_y_arm": y_arm})
    public = {"schema_version": "1.0", "status": "blind_review_pending", "pair_count": len(rows), "rows": rows}
    key = {"schema_version": "1.0", "seed": seed, "pair_count": len(key_rows), "rows": key_rows}
    return public, key
