# 架构

版本 0.11.0。早期（v0.1–v0.7）的逐版设计记录见 `archive/docs/architecture_v0.7.md`。

## 一个任务的流程

`scripts/run_job.py` 是任务的运行进程，由任务服务在独立进程中启动：

```
格式识别 → 路由 → 翻译工作流 → 验收 → 决策节点 ─┬─ 可用 ──→ 发布可用版 → 优化 → 发布优化版（不比可用版差才发布）
                                                   └─ 不可用 → AI OS 修复（有上限）─┬─ 修好 → 同上
                                                                                     └─ 修不好 → 发布降级版（文件 + 不可交付结论）
```

| 环节 | 模块 | 做什么 |
|---|---|---|
| 格式识别 | `formats/__init__.py` | 按内容和扩展名识别 PDF、LaTeX（`.tex` 或工程压缩包）、HTML、Markdown、DOCX、EPUB |
| 路由 | `formats/arxiv.py` | PDF 上印有带版本号的 arXiv 编号且允许联网时，取回源码并编译；页数相差不超过 1、上传 PDF 的词至少 85% 出现在编译结果中，才改走 LaTeX 路线 |
| PDF 工作流 | `scripts/deliver_pdf.py` | 抽取 → 覆盖度 → 翻译 → 渲染 → 排版复核 → 验收（见下节） |
| 源码格式工作流 | `formats/runner.py` 及各格式适配器 | 解析 → 翻译 → 原地回写 → 结构核对 → 验收（见“源码格式”一节） |
| 决策节点 | `aios/decision.py`、`aios/jev.py` | 是否可用由验收结论决定；每个阻断问题选一个修复动作 |
| AI OS 修复 | `aios/supervisor.py`、`aios/pi_agent.py`、`aios/tools.py` | 执行修复动作，规则处理不了的交给 pi agent；轮次、预算、时长、进展四道上限 |
| 优化 | `aios/optimize.py` | 只针对剩余告警，先复核后改，和可用版比较，不差才发布 |
| 状态与事件 | `jobstate.py` | `state.json`、带序号的 `events.jsonl`、`deliveries/<版本>/` 与清单（文件名、大小、SHA-256） |

各环节的进度写进 `state.json` 并记为事件；PDF 工作流各阶段耗时另写在 `work.timings.json`。

## PDF 工作流

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

## 源码格式

源码本身已经区分了正文、公式和标记，所以不需要版面模型，而是原地替换：

1. **解析**：适配器把正文切成段落（`formats/common.py` 的 `Segment`），段内必须保留的内容换成受保护标记（`markers.py`）：`⟦P:kN⟧` 是整块保留的内容（公式、引用、代码、单个符号），`⟦P:aN⟧…⟦P:zN⟧` 成对包住带格式的文字。段落外的内容逐字节复制。
2. **翻译**：段落变成普通翻译单元，带 `source_markup` 标记，走与 PDF 相同的流水线、质量门、缓存和记账。翻译模型能看到每个标记所代表的原文（例如公式的 LaTeX 源码），用来理解上下文。质量门对这类单元不把“标记换了位置”当作问题（还原按身份而非位置），但成对标记交叉或顺序颠倒是关键问题，会带着反馈重试。
3. **回写**：译文按格式转义后放回原位，标记还原为原始字节。标记缺失或多出的译文不写回，保留原文，验收照样把它记为未译。
4. **核对**：每种格式把输出与源文件逐项比较，任何差异都是大问题；另外统计留在复制部分里、没有进入翻译的正文词数（超过 1% 为大问题）。
5. **验收**：`acceptance.build_source_acceptance` 用与 PDF 相同的单元分级和告警阈值，加上格式核对结果和输出文件是否齐全。

| 格式 | 适配器 | 翻译的内容 | 原样保留 | 机器核对 |
|---|---|---|---|---|
| LaTeX | `formats/latex.py` | 正文段落、标题、`\title`、图表标题、脚注、表格单元格、列表项 | 导言区、所有数学、代码类环境、参考文献、引用与交叉引用命令 | 数学、环境、引用命令的多重集合不变；括号平衡；XeLaTeX + ctex 编译通过（原工程也会编译，用来区分原因） |
| HTML | `formats/html_doc.py` | 块级元素内的文字、`alt`、`title`、`placeholder`、`aria-label`、页面描述 | 代码、数学、脚本、样式 | 元素与属性（除翻译的属性和 `lang`）不变；代码类内容不变 |
| Markdown | `formats/markdown_doc.py` | 段落、标题、列表项、引用、表格单元格；链接文字 | 代码块与行内代码、数学、front matter、HTML 块 | 代码、数学、链接目标不变；标题数、列表项数不变 |
| DOCX | `formats/docx_doc.py` | 正文、表格、页眉页脚、脚注尾注中的段落 | 段落属性；非主格式的文字用成对标记保留格式；制表符、换行、图片、公式、域、书签 | 段落、表格、图片、公式、域、超链接、脚注引用数量不变 |
| EPUB | `formats/epub_doc.py` | 各章节（经 HTML 适配器）、目录标签、书名 | 图片、字体、样式；`mimetype` 仍是第一个且不压缩 | 各章节做 HTML 核对；文件不缺失 |

## 决策节点与 AI OS

- **可用与否由代码决定**：验收结论是 `not_deliverable` 就不可用。概率模型不参与这个判定，因为交付标准（例如漏译一处即阻断）不是概率。
- **修复动作**：`aios/decision.py` 的规则表为每类问题列出可用动作，按优先顺序排列。例如漏译对应带说明的重译，从未发出的单元对应续译，缺页对应重新渲染，其余交给 agent。某类问题有多个可选动作且配置了 Jev 时，由 Jev 的 Choice 选择；置信度低于 0.6 时交给 agent。告警类问题先用 Jev 的 Noul 判断哪些单元的告警是真问题，修复只针对这些单元；没有 Jev 时全部处理。
- **pi agent**（`aios/pi_agent.py`）：以 RPC 模式作为子进程运行，有独立的配置目录，关闭用户扩展、技能、上下文文件和会话文件，只开放 `agent/pdftrans-tools.ts` 注册的工具。这些工具调用 `python -m pdf_translation.aios.tools`，在任务目录内执行，付费工具先查预算，每次修改都要过质量门。预算、时长和轮数由监管进程计数，超出就中止，不靠提示词约束。agent 的花费从 pi 的会话统计读出，记进同一个账本。
- **上限**：修复最多 3 轮（`PDFTRANS_REPAIR_ROUNDS` 可改）、总时长 45 分钟、agent 单次 20 分钟或 40 轮、不超过任务预算；连续两轮阻断问题没有减少、或一轮里没有任何可执行的动作时立即停止。

## 翻译单元的路径

```
TranslationUnit → ContextPacket → TranslationRequest → LLMClient → TranslationResult → 质量门 → SQLite
```

- **上下文**（`context_router.py`、`context_store.py`）：作用域优先级 `unit > section > chapter > document > domain`；已核准的术语和风格是必带上下文，邻近段落和翻译记忆在超出预算时最先裁掉。上下文变化会改变缓存键。
- **模型调用**（`llm.py`、`providers.py`）：所有调用模型的地方（翻译、排版复核、告警复核、修复、接入方委托）都走 `make_llm_client`。配置里的 `provider` 选择预设，协议支持 `openai-responses`、`openai-chat`、`anthropic`、`gemini`、`litellm`；结构化输出方式不被支持时自动降一级，返回的 JSON 始终在本地校验。
- **状态与记账**（`storage.py`）：每次调用先写入 `provider_attempts` 和 `ledger`，再做结果校验；重试次数持久化，续跑不会重置。修复、agent 和优化的花费都记在同一个账本里（`source` 分别为 `agent_assist`、`aios_agent`、`aios_optimize`）。

## 对外接入

`service.py` 把一份文档的翻译封装成“任务”：每个任务是一个目录，里面有原文、配置、状态、事件、各版本产物和日志。调用方断开或服务重启都不会丢失任务。请求模型与“首次指定、之后沿用”的规则在 `jobspec.py`。

| 方式 | 模块 |
|---|---|
| 命令行 `pdftrans translate / jobs / profile / setup` | `service_cli.py`（加 `--server` 调用远程服务，客户端在 `client.py`） |
| HTTP `pdftrans serve` | `http_api.py`（除 `/health` 外都要令牌；每 5 秒调度排队任务并发送回调） |
| MCP（stdio 与 HTTP `/mcp`） | `mcp_server.py` |
| OpenAPI 描述 | `openapi.py`（参数说明与 MCP 工具共用同一份定义） |
| Claude Skill | `skills/pdf-translate/SKILL.md` |

完整的接入说明见 `docs/agent_guide.md`。

## 实验线

`native_*.py`、`source_preserving.py` 和 `pdftrans pdf-native` 是不经过 BabelDOC、直接编辑原页文字对象的实验写回方式，未进入默认流程。旧说明见 `archive/docs/native_workflow.md`。
