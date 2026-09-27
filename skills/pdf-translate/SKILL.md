---
name: pdf-translate
description: Translate an English document - PDF (textbook, paper, lecture notes, manual), LaTeX source or project, HTML page, Markdown, Word (.docx) or EPUB - into Chinese, producing a Chinese version and a side-by-side bilingual version that keep layout, formulas and markup. Use when the user asks to translate a document into Chinese, wants a bilingual (中英对照) version, or asks about the status or quality of such a translation job.
---

# 文档翻译（英文 → 中文）

This skill drives the translation service in this repository. Every job ends with files and an honest verdict: a **usable** version as soon as it passes acceptance, then an **optimized** version, or - when the service's AI OS cannot repair it - a **degraded** version whose files exist but which did NOT pass acceptance.

## Choose how to call it

| Situation | Use |
|---|---|
| The repository is on this machine | CLI: `uv run pdftrans ...` in the repository root |
| An MCP client is connected to `pdf-translation` | MCP tools `translate_document`, `wait_job`, `get_job_file` |
| The service runs on another host | CLI with `--server <url> --token <token>`, or HTTP (see `docs/agent_guide.md`) |

## Steps

1. **Price first.** `--estimate-only` (MCP: `estimate_only: true`). Nothing is sent to a model. Report `estimate.estimated_max_cost_usd` to the user when it is more than a few cents, and confirm before spending.
2. **Translate.** The first call from a caller must name both models; later calls reuse them.
   ```bash
   uv run pdftrans translate <file|https://...> --budget-usd <ceiling> --out-dir <folder> \
     --provider deepseek --model deepseek-flash --agent-provider deepseek --agent-model deepseek-flash \
     [--style-note "subject; term choices"] [--no-optimized] [--main-file main.tex]
   ```
   Formats: `.pdf`, `.tex` or a zipped LaTeX project, `.html`, `.md`, `.docx`, `.epub`. `--budget-usd` is a hard ceiling for the whole job (translation, repair, optimisation). Keys live in the server's environment (`DEEPSEEK_API_KEY`, ...); a `key_missing` error means the operator must add one.
   With MCP: call `translate_document`, then loop on `wait_job` with the returned `events_after` until `delivery` is no longer `none` or `status` is `failed`. Do not poll faster.
3. **Read the result before handing anything over.**
   - `delivery: usable` / `optimized` / `revised` → deliverable. Give the user the `mono` and `dual` files (LaTeX also has `source`), and mention the known minor issues in `problems`. While `status` is still `running`, the optimised version is being built; you may deliver the usable one now.
   - `delivery: degraded` → **not deliverable**. Files exist; tell the user it is not a finished translation and list the major `problems`.
   - `status: failed` → no files; read `error.code`.
   `for_agent.summary_zh` is a ready-made, honest summary.
4. **Say what was checked.** Acceptance checks completeness, untranslated English, protected formulas/code/links/markup, and output integrity. It does not prove every sentence is a faithful translation; say so if the user relies on the text.

## Improving a finished job

The service's AI OS already repaired what it could. To go further:

1. `pdftrans jobs units <job_id> --with-issues` (MCP: `get_units`) lists flagged units.
2. Judge them yourself and write fixes, or delegate with `pdftrans jobs ask <job_id> <task>` (MCP: `ask_model`) for many units, glossary-consistent retranslation or page images you cannot see.
3. `pdftrans jobs revise <job_id> fixes.json --editor agent:<name> --wait` (MCP: `apply_fixes`) writes them; the result is published as the `revised` version. No model is called.

## When something goes wrong

- `model_required` → name `--provider/--model` and `--agent-provider/--agent-model` (first call of this caller).
- `failed` with HTTP 402 / "Insufficient Balance" → the model account needs funds; tell the user, do not retry in a loop. Then `pdftrans jobs resume <job_id>`; nothing already paid for is paid again.
- A LaTeX job with `latex_compile_failed` → the translated project is still in `source`; the acceptance report says whether the original project compiles on the server.

For the whole contract - fields, events, callbacks, error codes - read `docs/agent_guide.md` (also `pdftrans guide`, the MCP resource `pdftrans://docs/agent_guide`, or `GET /v1/guide`).
