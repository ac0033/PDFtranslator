"""Measure Jev's warning triage against the human-labelled warnings before trusting it.

The decision node sends repair effort only to the units whose warning Jev
judges real (``aios.jev.JevClient.warning_is_real``). A real defect Jev calls a
false alarm would go unrepaired, so the bar is the one the rule-based triage
already meets (``tests/test_triage.py``): **no confirmed error may be
dismissed**. On top of that, Jev is only useful if it removes a fair share of
the false alarms.

Corpus: ``tests/data/val_number_triage.json`` (88 number warnings; 5 real) and
``tests/data/val_retained_english_triage.json`` (33 retained-English
warnings; 14 needed a fix), from the manual val.pdf review.

    uv run python -m scripts.evaluate_jev [--threshold 0.5] [--output jev-eval.json]

Needs ``TYPESAFE_API_KEY``. Exit code 0 means Jev met the bar at this
threshold (safe to keep enabled); 1 means it did not (unset the key and the
decision node falls back to its rules); 2 means it could not run.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

from pdf_translation.aios.jev import KEY_ENV, JevClient, JevError  # noqa: E402

DATA = ROOT / "tests" / "data"
MIN_FALSE_ALARMS_FILTERED = 0.5
EXPLANATIONS = {"number": "number inventory changed between source and translation",
                "retained_english": "English words left in the Chinese translation"}


def cases() -> list[dict]:
    found = []
    for name, category, real in (("val_number_triage.json", "number", {"real"}),
                                 ("val_retained_english_triage.json", "retained_english", {"fix"})):
        for case in json.loads((DATA / name).read_text(encoding="utf-8"))["cases"]:
            found.append({"category": category, "unit_id": case["unit_id"], "real": case["verdict"] in real,
                          "verdict": case["verdict"], "source": case["source_text"], "translation": case["translated_text"]})
    return found


def evaluate(client: JevClient, threshold: float) -> dict:
    results = []
    for case in cases():
        probability = client.warning_is_real(category=case["category"], explanation=EXPLANATIONS[case["category"]],
                                             source=case["source"], translation=case["translation"])
        results.append({**{k: case[k] for k in ("category", "unit_id", "verdict", "real")}, "p_real": round(probability, 4)})
    real = [r for r in results if r["real"]]
    false_alarms = [r for r in results if not r["real"]]
    missed = [r for r in real if r["p_real"] < threshold]
    filtered = [r for r in false_alarms if r["p_real"] < threshold]
    share = len(filtered) / len(false_alarms) if false_alarms else 0.0
    passed = not missed and share >= MIN_FALSE_ALARMS_FILTERED
    return {
        "threshold": threshold, "cases": len(results), "real_defects": len(real),
        "real_defects_dismissed": [f"{r['category']}:{r['unit_id']} p={r['p_real']}" for r in missed],
        "false_alarms": len(false_alarms), "false_alarms_filtered": len(filtered),
        "false_alarm_share_filtered": round(share, 3),
        "passed": passed,
        "verdict": ("Jev meets the bar: no real defect dismissed and at least half the false alarms filtered"
                    if passed else "Jev does not meet the bar at this threshold: leave TYPESAFE_API_KEY unset "
                                   "so the decision node uses its rules"),
        "usage": {"calls": client.usage.calls, "input_tokens": client.usage.input_tokens,
                  "output_tokens": client.usage.output_tokens},
        "results": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--threshold", type=float, default=0.5)
    parser.add_argument("--output", type=Path, default=None, help="Write the full result, per case, as JSON.")
    args = parser.parse_args(argv)
    load_dotenv(ROOT / ".env", override=False)
    if not os.environ.get(KEY_ENV):
        print(json.dumps({"error": f"{KEY_ENV} is not set; nothing was evaluated"}))
        return 2
    try:
        report = evaluate(JevClient(os.environ[KEY_ENV]), args.threshold)
    except JevError as problem:
        print(json.dumps({"error": str(problem)}))
        return 2
    if args.output:
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {k: v for k, v in report.items() if k != "results"}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
