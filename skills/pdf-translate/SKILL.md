---
name: pdf-translate
description: Translate an English PDF (textbook, paper, lecture notes, manual) into Chinese, producing a Chinese-only PDF and a side-by-side bilingual PDF that keep the original layout, formulas and bookmarks. Use when the user asks to translate a PDF or document into Chinese, wants a bilingual (中英对照) version, or asks about the status or quality of such a translation job.
---

# PDF 翻译（英文 → 中文）

This skill drives the PDF translation runtime in this repository. It produces two PDFs per document: `mono` (Chinese only) and `dual` (original and Chinese side by side), plus an acceptance report that says whether the result can be delivered.

## Choose how to call it

| Situation | Use |
|---|---|
| The repository is on this machine | CLI: `uv run pdftrans ...` in the repository root |
| An MCP client is connected to `pdf-translation` | MCP tools `translate_pdf`, `get_job`, `get_job_file` |
| The service runs on another host | CLI with `--server <url> --token <token>`, or HTTP (see `docs/agent_guide.md`) |

## Steps

1. **Price first.** Run with `--estimate-only` (MCP: `estimate_only: true`). Nothing is sent to a model and nothing is charged. Report `result.estimated_max_cost_usd` to the user when it is more than a few cents, and confirm before spending.
2. **Translate.**
   ```bash
   uv run pdftrans translate <file.pdf|https://...> --budget-usd <ceiling> --out-dir <folder> \
     [--provider deepseek --model-id deepseek-flash] [--style-note "subject; term choices"]
   ```
   `--budget-usd` is a hard ceiling. Models without a built-in price also need `--input-usd-per-million` and `--output-usd-per-million`. A per-document `--style-note` (for example "强化学习教材；policy 译为 策略") improves terminology.
   Without `--wait`, poll `pdftrans jobs status <job_id>` (MCP: `get_job`) every 30–60 s. A 15-page document takes about 5 minutes; hundreds of pages take hours.
3. **Read the verdict before handing anything over.** `result.verdict`:
   - `deliverable` / `deliverable_with_known_issues`: give the user the `mono` and `dual` PDFs, and summarise the known issues from the `acceptance` file.
   - `not_deliverable`: do not present it as finished. `result.major_problems` says why (for example a paragraph that was never translated). See "When something goes wrong".
4. **Say what was checked.** The acceptance report checks completeness, protected numbers and formulas, and capture coverage. It does not prove every sentence is a faithful translation; say so if the user relies on the text.

## When something goes wrong

- `status: failed` → read `pdftrans jobs log <job_id>`. HTTP 402 or "Insufficient Balance" means the model account needs funds; tell the user, do not retry in a loop.
- Missing or wrong paragraphs → the `content_review` file lists every paragraph with its source, translation and warnings. Fix translations with `scripts/apply_local_fixes.py` (no model call, only the changed pages are rendered again).
- Layout mismatches after rendering → `scripts/review_layout.py` has a model compare each page with the original and repair mismatches; it runs automatically unless `--layout-review off`.

For the full pipeline, every artefact, and when to call a model yourself, read `docs/agent_guide.md` in the repository.
