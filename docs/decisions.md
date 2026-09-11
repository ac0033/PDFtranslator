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
| Bilingual PDF rendering | offline candidate | three-page mono/dual PDFs are readable; real Chinese translation, 42 low-confidence region matches and TOC migration remain open |
| Captured-workload budget gate | implemented | 39 billable units estimate to USD 0.1024467 at the configured peak rates; dry-run reports zero API calls and paid execution requires `--execute-paid` plus a non-zero config budget |
