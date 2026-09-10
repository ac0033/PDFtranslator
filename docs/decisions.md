# V0 decisions

| Decision | Status | Evidence / reason |
|---|---|---|
| Python 3.12 and uv lock | implemented | `.python-version`, `pyproject.toml`, `uv.lock` |
| Pydantic v2 strict immutable contracts | implemented | extra fields and malformed ranges are rejected |
| SQLite state, exact cache and usage ledger | implemented | recovery and hot-rerun tests |
| Paid calls require an explicit calculable reservation within budget | implemented | missing price or excess budget raises before provider call |
| OpenAI Responses API, independent requests, structured output, `store=false` | implemented but not called | no API key, approved budget or benchmark supplied |
| DeepSeek-V4.1-Flash via `deepseek-flash` | configured and capability-tested | API key is loaded from ignored `.env`; USD 0.20 hard cap; single test result accepted |
| BabelDOC one-page injection/write-back | blocked | no source PDF/sample; integration point not verified |
| Bilingual PDF rendering | blocked | depends on successful BabelDOC spike |
