# V0 interim acceptance

Status: **partially implemented; full V0 not yet accepted**.

## Passed locally

- Strict schema rejection, source hash verification and context budget enforcement.
- Protected marker inventory rejects missing and unknown markers.
- Quality checks detect changed numbers and preserve URL/marker inventories.
- SQLite run identity rejects changed inputs on resume.
- Deterministic offline pipeline completes with zero model cost.
- A generated one-page PDF was inspected as 1 page with stable SHA-256; its rendered PNG was visually legible with no clipping or overlap.
- DeepSeek-V4.1-Flash capability call passed on 2026-09-10 using API model ID `deepseek-flash`: 206 input tokens, 33 output tokens, accepted by all V0 text quality checks. Conservative peak-rate cost estimate was USD 0.0001014; the provider did not return a currency cost field.

## Not run and not passed

- `dm.pdf` manifest and accepted-sample mapping.
- Smoke/development/frozen benchmark selection.
- BabelDOC pinned dependency and one-page translation injection/write-back.
- Full benchmark translation quality comparison; the single capability call is connectivity evidence only.
- Bilingual PDF, aligned JSONL and page-by-page semantic/layout review.
- Response-loss recovery and live 429/timeout behavior.

## Inputs required to continue in document order

1. Place the exact source PDF inside this project (recommended `inputs/dm.pdf`).
2. Provide the accepted sample and its source page range.
3. Approve a V0 spend limit and verify the current model prices/account access.
4. Then execute steps 0.1 through 0.12 without starting full-book translation.
