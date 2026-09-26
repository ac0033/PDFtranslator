# 架构

版本 0.9.0。早期（v0.1–v0.7）的逐版设计记录见 `archive/docs/architecture_v0.7.md`。

## 一份 PDF 的流程

`scripts/deliver_pdf.py` 按顺序运行下列阶段，每个阶段是一个独立进程里的可续跑脚本；同一条命令重跑会从磁盘上已有的结果继续，不重复抽取、付费或渲染。

```
抽取 capture → 覆盖度 coverage → 翻译 translate → 渲染 render → 排版复核 layout review → 验收 acceptance
```

| 阶段 | 入口 | 做什么 | 产物 |
|---|---|---|---|
| 抽取 | `scripts/capture_val_full.py`、`babeldoc_adapter.py`、`semantic_layout.py` | BabelDOC 0.6.4 解析版面，重建语义段落，按块分进程执行 | `units.jsonl`、`capture-map.jsonl` |
| 覆盖度 | `pdftrans capture-coverage`（`capture_coverage.py`） | 用独立抽取器对账，找出从未进入翻译的正文 | 覆盖度报告 |
| 翻译 | `scripts/translate_val_usable_sample.py`、`pipeline.py` | 先按 `budget_usd` 估价，超出就不发请求；按段并发调用模型，结果过质量门（`quality.py`、`term_lock.py`） | 运行库 `state.sqlite` |
| 渲染 | `scripts/render_val_full.py`、`chunk_driver.py` | 把译文写回原页，按块分进程并自动重试；合并出中文版和双语版 | `*.zh.mono.pdf`、`*.zh.dual.pdf` |
| 排版复核 | `scripts/review_layout.py`、`layout_review.py` | 模型对照原页图和译页图找出对不上的地方，给出替换译文；通过质量门才写回，只重渲染改动的页 | `layout-review.json` |
| 验收 | `pdftrans acceptance-report`（`acceptance.py`、`triage.py`） | 按问题对阅读的影响分级，漏译等大问题判为 `not_deliverable` | `acceptance-report.json`、`content-review.jsonl` |

各阶段耗时和最终状态写在 `<run_id>.timings.json`。

## 翻译单元的路径

```
TranslationUnit → ContextPacket → TranslationRequest → LLMClient → TranslationResult → 质量门 → SQLite
```

- **上下文**（`context_router.py`、`context_store.py`）：作用域优先级 `unit > section > chapter > document > domain`；已核准的术语和风格是必带上下文，邻近段落和翻译记忆在超出预算时最先裁掉。上下文变化会改变缓存键。
- **模型调用**（`llm.py`、`providers.py`）：所有调用模型的地方（翻译、排版复核、告警复核、接入方委托）都走 `make_llm_client`。配置里的 `provider` 选择预设，协议支持 `openai-responses`、`openai-chat`、`anthropic`、`gemini`、`litellm`；结构化输出方式不被支持时自动降一级，返回的 JSON 始终在本地校验。
- **状态与记账**（`storage.py`）：每次调用先写入 `provider_attempts` 和 `ledger`，再做结果校验；重试次数持久化，续跑不会重置。同一运行加文件锁，防止两个进程同时写入。

## 对外接入

`service.py` 把一份 PDF 的翻译封装成“任务”：每个任务是一个目录，其中有原文、配置、日志和产物，由 `deliver_pdf.py` 在独立进程中运行。调用方断开或服务重启都不会丢失任务。

| 方式 | 模块 |
|---|---|
| 命令行 `pdftrans translate / jobs` | `service_cli.py`（加 `--server` 调用远程服务） |
| HTTP `pdftrans serve` | `http_api.py`（除 `/health` 外都要令牌） |
| MCP（stdio 与 HTTP `/mcp`） | `mcp_server.py` |
| Claude Skill | `skills/pdf-translate/SKILL.md` |

任务完成后，接入方可以读取单元、委托任务的模型做一件事（`assist.py`），再写回修正（`local_fixes.py`：只重渲染改动的页并重新验收）。完整说明见 `docs/agent_guide.md`。

## 实验线

`native_*.py`、`source_preserving.py` 和 `pdftrans pdf-native` 是不经过 BabelDOC、直接编辑原页文字对象的实验写回方式，未进入默认流程。旧说明见 `archive/docs/native_workflow.md`。
