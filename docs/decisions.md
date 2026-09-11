# V0 decisions

| Decision | Status | Evidence / reason |
|---|---|---|
| Python 3.12 and uv lock | implemented | `.python-version`, `pyproject.toml`, `uv.lock` |
| Pydantic v2 strict immutable contracts | implemented | extra fields and malformed ranges are rejected |
| SQLite state, exact cache and usage ledger | implemented | recovery and hot-rerun tests |
| Paid calls require an explicit calculable reservation within budget | implemented | missing price or excess budget raises before provider call |
| OpenAI Responses API, independent requests, structured output, `store=false` | implemented but not called | no API key, approved budget or benchmark supplied |
| DeepSeek-V4.1-Flash via `deepseek-flash` | configured and capability-tested | API key is loaded from ignored `.env`; USD 0.20 hard cap; single test result accepted |
| Benchmark input and page sets | implemented | three local PDFs registered; `dm.pdf` smoke/development/frozen-test sets are disjoint and reproducible |
| BabelDOC multi-page capture/injection | candidate pass | two-pass exact-content matching completed pages 42, 51 and 577; 78 stable units were injected without depending on callback order |
| Formula/code protection | implemented | 39 placeholder-only units bypassed the model and were restored byte-for-byte at the runtime boundary |
| Boundary-marker repair | implemented | four model outputs omitted a leading footnote marker; only exact source-boundary omissions are restored deterministically, while interior/unknown marker changes remain blocked |
| Bilingual PDF rendering | accepted for three-page smoke | real DeepSeek mono/dual PDFs are readable; 42 low-confidence region matches, TOC migration and mixed-script spacing remain open |
| Captured-workload budget gate | implemented and exercised | 39 billable units estimated to USD 0.1024467; actual token-based estimate was USD 0.0064098, within the user-approved CNY 0.80 ceiling |
| Semantic paragraph reconstruction | v0.5 release candidate | incomplete adjacent fragments are grouped by page geometry and textual continuation; callback order is not treated as reading order |
| Target-language layout resegmentation | v0.5 release candidate | explicit layout markers may move only to natural sentence boundaries, must remain exactly once and in order, and are split back to the original write-back slots |
| Short-fragment location | v0.5 release candidate | exact page-text lookup precedes fuzzy block matching, preventing `we can` from inheriting an unrelated large block |
| Reusable capture | implemented | `--reuse-capture` requires existing capture files and an exact frozen page-set match; non-executing budget checks do not load ONNX |
| DeepSeek structured-output recovery | implemented | the prompt requests a JSON object instance rather than the schema definition; invalid schema echoes fail strict validation before storage |
| Cross-slot marker safety | accepted for v0.5 smoke | deterministic repair preserves the full BabelDOC marker order, keeps footnote markers in their original write-back slot, and ignores decimals and spaced ellipses as sentence boundaries |
| v0.5 real translation | accepted for three-page smoke | 74/74 units accepted; 35 DeepSeek translations used 11,586 input and 2,717 output tokens in the recorded ledger; six rendered pages passed visual review |
| Complete provider-attempt accounting | v0.6 release candidate | billable responses are ledgered before schema/result/quality validation; tests inject invalid schema and critical marker failures and verify both attempts and cost |
| Bounded selective retry | v0.6 release candidate | invalid structured output and critical protected/layout-marker failures retry at most `provider_max_attempts`; persisted counts survive resume |
| Real-PDF context graph | v0.6 release candidate | headings create nested scope paths; adjacent translatable units provide capped local context; nonconsecutive sample pages cannot link |
| Development structural ablation | accepted offline | 12 frozen development pages yielded 121 units; all 117 translatable units selected local context; no cross-sample links; no quality claim |
| Adaptive output ceiling | v0.6 rc.2 accepted offline | each request reserves `min(configured max, max(384, ceil(source chars * 0.75) + 128))`; truncation is ledgered and may retry once with a bounded expanded ceiling |
| Controlled v0.6 quality A/B | prepared, not run | the two arms share model, prompt, style, adapter and adaptive output cap; only neighboring source context differs. The 117-unit combined one-pass ceiling is USD 0.1837797 / CNY 1.378348 at 7.5, under a separately approved CNY 1.5 hard cap |
