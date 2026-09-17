"""Convert the manual val.pdf review of 2026-09-16/17 into a review ledger.

That review produced the only human verdicts this project has: 88 number
warnings, 44 marker-order warnings and 33 retained-English findings, each with
a reason. They lived in gitignored scratch files, one power cut from being
lost, and could not be replayed because nothing consumed them.

This reads those files, joins each verdict to the unit's current result hash,
and writes a ledger the acceptance report consumes. Only harmless verdicts
become dismissals: a warning a human confirmed and fixed must keep firing if it
ever comes back. Retained-English verdicts become a term whitelist, because
that is the level the decision was actually made at - ``zonotope`` is kept
everywhere or nowhere.

One-off by design: the inputs are ad hoc files from one review session. Written
down so the ledger's provenance is auditable, not to be run again.
"""
from pathlib import Path
import argparse
import json

from pdf_translation.triage import ReviewLedger

#: Verdicts meaning "this warning is not a defect". Anything else - a confirmed
#: error, a fix - is deliberately absent: those must never dismiss a warning.
HARMLESS = {
    "number": {"artifact", "conversion"},
    "protected_marker_order": {"ok"},
}
REVIEWED_AT = "2026-09-17"


def load_rows(content_review: Path) -> dict[str, dict]:
    rows = {}
    for line in content_review.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            rows[row["unit_id"]] = row
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--content-review", type=Path, default=Path("output/pdf/val-usable-babeldoc-full-r1/content-review.jsonl"))
    parser.add_argument("--audit-root", type=Path, default=Path("tmp"), help="Directory holding the 2026-09 review scratch files.")
    parser.add_argument("--output", type=Path, default=Path("reports/val_review_ledger.json"))
    args = parser.parse_args()

    rows = load_rows(args.content_review)
    entries: list[dict] = []
    skipped: dict[str, int] = {}

    def note(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    for filename, category in (("number_audit_result.json", "number"), ("marker_order_audit.json", "protected_marker_order")):
        path = args.audit_root / filename
        if not path.is_file():
            note(f"missing:{filename}")
            continue
        for record in json.loads(path.read_text(encoding="utf-8")):
            row = rows.get(record["unit_id"])
            if row is None:
                note(f"{category}:unit_absent")
                continue
            if record["verdict"] not in HARMLESS[category]:
                note(f"{category}:not_harmless")
                continue
            entries.append({
                "unit_id": record["unit_id"],
                "result_hash": row.get("result_hash"),
                "category": category,
                "verdict": "dismiss",
                "reason": record.get("reason", ""),
                "reviewed_at": REVIEWED_AT,
            })

    retained: set[str] = set()
    leak_path = args.audit_root / "english_leak_audit.json"
    if leak_path.is_file():
        for record in json.loads(leak_path.read_text(encoding="utf-8")):
            if record["verdict"].startswith("keep"):
                retained.update(part.casefold() for part in record["leak"].split())
            else:
                note("retained_english:confirmed_omission")
    else:
        note("missing:english_leak_audit.json")

    ledger = ReviewLedger(entries, retained)
    ledger.dump(args.output, note=(
        "Seeded from the manual val.pdf review of 2026-09-16/17. Dismissals hold only while "
        "result_hash matches; retained_terms are English terms a reviewer kept on purpose."
    ))
    print(json.dumps({
        "output": str(args.output),
        "dismissals": len(entries),
        "retained_terms": sorted(retained),
        "skipped": skipped,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
