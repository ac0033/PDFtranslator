# 接入指南：给调用本服务的工具与 AI agent

读完本文，你应当能够：提交一份 PDF 并拿到中文版和双语版；看懂每个阶段产出的文件和字段；判断结果能否交给用户；在自动流程没处理好的地方自己动手修，或者在合适的时候委托本服务配置的模型去修，然后把修正写回、重新出版面。

本文对 MCP、HTTP、命令行三种接入方式同样适用，它们调用的是同一套任务服务（对照表见第 6 节）。仓库里的 `AGENTS.md` 是开发本项目的原则；其中关于“什么能交付”的标准同样约束你（第 5 节）。

## 1. 一句话流程

英文 PDF → 抽取段落与版面 → 翻译（模型）→ 按原版面回写中文 → 排版复核（模型，对照原页修正）→ 自动验收 → 中文版 `mono` + 双语版 `dual` + 验收单。

每个阶段的结果都落在磁盘上。重跑会从已有结果续跑，已付费的翻译不会重复付费。

## 2. 阶段、产物与费用

| 阶段（`stage`） | 做什么 | 主要产物 | 调用模型 |
|---|---|---|---|
| `capture` | 用 BabelDOC 解析版面，切成“单元”（段落大小的文本），公式、符号、版面分界替换成受保护标记 `⟦P:...⟧` | `units.jsonl`、`capture-map.jsonl` | 否 |
| `coverage` | 核对原文里有没有正文没进入任何单元 | `capture-coverage.json` | 否 |
| `estimate` / `translate` | 先按全文估算费用上限，超过 `budget_usd` 就一个请求也不发；然后并发翻译每个单元，每个请求都记账，通不过质量门的自动带反馈重试 | 运行库 `runs/<run_id>/state.sqlite`、`estimate.json` | 是 |
| `render` | 把译文按原版面逐页回写，合并成两本书 | `*.zh.mono.pdf`、`*.zh.dual.pdf`、`content-review.jsonl` | 否 |
| `layout-review` | 把每页原图、译图和该页各段交给模型，找出中英文对不上的地方并给出替换译文；替换必须通过校验才写回，只重渲染改动的页 | `layout-review.json` | 是 |
| `acceptance` | 按问题对阅读的影响分级，给出能否交付的结论 | `acceptance-report.json` | 否 |

时间参考：14 页讲义在 4 核笔记本上约 5 分钟、约 0.10 美元（deepseek-flash）；几百页的书要几个小时。

**受保护标记**：`⟦P:bd-p0003-u0012-o000⟧` 这类标记代表公式、符号或原样保留的片段，`⟦P:layout-...⟧` 代表版面分界。渲染时按标记把原图形放回去。任何译文都必须**原样、各一次、按原顺序**保留全部标记，否则质量门会拦下（`protected_marker`、`protected_marker_order`）。

## 3. 任务的状态

| `status` | 含义 | 你该做什么 |
|---|---|---|
| `queued` | 排队中（服务同时运行的任务数有上限） | 等 |
| `starting` / `running` | 运行中，`stage` 是当前阶段 | 每 30–60 秒查一次，不要更频繁 |
| `estimated` | 只估价的任务已完成，`result.estimated_max_cost_usd` 是费用上限 | 告诉用户价格，确认后再正式提交 |
| `succeeded` | 流程跑完了。**不等于能交付**，要看 `result.verdict` | 读验收结论（第 5 节） |
| `failed` | 某阶段出错，`error` 给出原因 | 读日志，按第 11 节处理；原因排除后 `resume_job` 续跑 |
| `cancelled` | 被取消 | — |

`result` 里的字段：`verdict`、`major_problems`（阻断交付的原因）、`cost_usd`（该任务至今的全部花费，含排版复核和你委托的调用）、`layout_review`（复核页数、采纳/拒绝的修正数、花费）、`books`。

## 4. 能下载的文件

| 名称 | 内容 |
|---|---|
| `mono` | 中文版 PDF |
| `dual` | 中英对照 PDF |
| `acceptance` | 验收单 JSON：`verdict`、`major_problems[]`（`kind`、`severity`、`count`、`detail`）、`minor_issues[]`（按类别计数、占比、示例页）、`retained_english_terms`（保留英文的术语）、`capture_coverage`、`coverage.checked`（这份验收实际检查了什么） |
| `content_review` | 每个单元一行 JSON：`unit_id`、`source_pages`、`source_hash`、`source_text`、`translated_text`、`state`、`issues[]` |
| `layout_review` | 排版复核的每条决定：页、单元、问题类型、严重程度、原译文、替换译文、是否采纳及原因 |
| `log` | 运行日志 |

单元的 `state`：`accepted`（通过质量门）、`needs_review`（有警告但可用）、`exhausted`（多次都没译出，**必然阻断交付**）。

常见的 `issues` 类别：

| 类别 | 意思 | 通常的严重程度 |
|---|---|---|
| `untranslated_unit` | 没有译文或整段仍是英文 | 大问题 |
| `incomplete` | 译文不完整 | 大问题 |
| `protected_marker` / `protected_marker_order` | 标记丢失、多出或顺序错 | 可能改变公式位置，需要看 |
| `number` | 原文数字在译文里找不到 | 需要看，数字错是大问题 |
| `terminology` | 没用术语表规定的译法 | 小问题，多了要处理 |
| `retained_english` | 译文里保留了英文词 | 通常可接受（术语、代码） |
| `protected_source_literal_unavailable` | 翻译时模型看不到某个标记代表的原文 | 提醒核对附近语义，多数无害 |
| `layout_boundary_unverifiable` | 版面分界处无法自动核对 | 提醒核对排版 |

`get_units` 返回的 `issues` 还包含验收对全文所做的判断（整段仍是英文、泄漏的标记），所以导致验收结论的每个单元都能用 `with_issues` 找到。只保留了人名、缩写等原文大写词的中文译文不算漏译。

不必逐条处理所有警告：验收单已按对阅读的影响分级，先看 `major_problems`，再看占比高的 `minor_issues`。

## 5. 什么能交给用户

- `deliverable`：可以交付。
- `deliverable_with_known_issues`：可以交付，同时告诉用户验收单里记录的已知问题。
- `not_deliverable`：**不能当作完成品交付**。`major_problems` 说明原因，例如某些单元从未译出。先修（第 7、8 节），修完验收通过再交付。

自动验收检查的是完整性、受保护的数字与公式、抽取覆盖度和页面齐全，**不证明每句话都译得忠实**。用户要依赖译文内容时，要说清这一点。不要把“文件已生成”说成“可用版本已通过”。

## 6. 接入方式对照

| 动作 | MCP 工具 | HTTP | 命令行（`uv run pdftrans ...`） |
|---|---|---|---|
| 可用的模型服务商 | `list_providers` | `GET /v1/providers` | `providers` |
| 提交翻译 | `translate_pdf` | `POST /v1/jobs` | `translate <文件或网址>` |
| 查状态 | `get_job` | `GET /v1/jobs/{id}` | `jobs status <id> [--wait]` |
| 列出任务 | `list_jobs` | `GET /v1/jobs` | `jobs list` |
| 取消 | `cancel_job` | `POST /v1/jobs/{id}/cancel` | `jobs cancel <id>` |
| 失败或取消后续跑 | `resume_job` | `POST /v1/jobs/{id}/resume` | `jobs resume <id>` |
| 取文件 | `get_job_file` | `GET /v1/jobs/{id}/files/{name}` | `jobs fetch <id> <name> <路径>` |
| 读单元 | `get_units` | `GET /v1/jobs/{id}/units` | `jobs units <id>` |
| 委托模型做一件事 | `ask_model` | `POST /v1/jobs/{id}/assist` | `jobs ask <id> <task>` |
| 写回修正并重出版面 | `apply_fixes` | `POST /v1/jobs/{id}/revise` | `jobs revise <id> <fixes.json>` |
| 本指南 | 资源 `pdftrans://docs/agent_guide` | `GET /v1/guide` | `guide` |
| 接口描述 | 工具的 `inputSchema` | `GET /v1/openapi.json`（OpenAPI 3.1） | 各子命令 `--help` |

命令行加 `--server <地址> --token <令牌>` 就改为调用远程服务。HTTP 除 `/health` 外都要带 `Authorization: Bearer <令牌>`。模型 Key 可以随任务或随单次委托调用传入（MCP 参数 `api_key`，HTTP 头 `X-LLM-API-Key`）；只在内存中使用，不写入磁盘。

## 7. 标准工作流

1. **先估价**：`estimate_only: true`，不调用模型、不花钱。价格超过几美分时告诉用户并确认。
2. **提交**：`budget_usd` 是硬上限。deepseek-flash 以外的模型要给 `input_usd_per_million` 和 `output_usd_per_million`，否则无法控制预算，服务会拒绝。有领域或术语要求时写进 `style_note`（例如“强化学习教材；policy 译为 策略”），比事后修便宜得多。
3. **轮询**直到 `status` 为 `succeeded`、`failed`、`estimated` 或 `cancelled`。
4. **读结论**（第 5 节）。
5. **需要改进时**，循环：
   1. `get_units`，加 `with_issues: true`，看被标记的单元；或按页读（`pages`）。
   2. 判断哪些是真问题（第 8 节决定自己改还是委托）。
   3. 得到修正：自己写，或 `ask_model` 返回的候选里带 `fix` 的那些。
   4. `apply_fixes` 写回。任务会重新进入 `running`（只重渲染改动的页并重新验收，通常一两分钟，不调用模型），轮询到 `succeeded` 后再看 `verdict`。
6. **交付**：给出 `mono` 和 `dual`，先说能不能正常使用、提供了哪些版本，再说已知问题。

## 8. 自己处理，还是委托模型

你自己就是模型，大多数判断你自己做更好：你知道用户要什么，也看得到上下文。本服务的 `ask_model` 并不比你聪明。它的价值在于**用本任务的翻译模型、在本任务的预算和账本内**做事。按下面的规则选：

**自己处理**（不必委托）：

- 看懂验收单，决定能否交付，向用户解释。
- 判断少量单元（几条到十几条）是否误译，写出修正。
- 修正显而易见的错误：漏掉的否定、数字、术语，个别标记放错了位置。
- 任何需要结合用户意图的决定：取舍、要不要继续花钱。

**委托 `ask_model`**：

| 情况 | 原因 | 任务 |
|---|---|---|
| 要处理的单元很多（几十条以上） | 本任务的模型通常便宜得多；你的上下文留给判断 | `review_units`、`retranslate` |
| 重译要和全书一致 | 它用同一个模型、同一份风格说明和术语表，并带上前后文 | `retranslate`（可加 `instruction`） |
| 需要看页面图像而你看不到（纯文本 agent、脚本） | 它把原页和译页的图像一起发给能看图的模型 | `review_pages`，或 `ask` 加 `images` |
| 译文太长被挤小 | 按目标长度压缩并保持含义 | `shorten`（`shorten_to` 默认 0.8） |
| 调用方不是模型（脚本、工作流） | 没有别的智能可用 | 任何任务 |

委托得到的也只是**候选**：质量门拦下的候选不带 `fix`；带 `fix` 的也要由你决定是否写回。模型的结论不是证据：说“没有问题”不等于没有问题，说“有问题”也要核实。实测中，模型看图后断言某页漏了正文，核对文字后发现该页完整。能用文字核对的（例如比对原页与译页抽出的文本），先核对再下结论。

费用：每个委托请求先估价，全部请求的上限加上已花费超过任务预算时，一个请求也不发，并告诉你需要多少；传 `budget_usd` 可以提高该任务的上限（这是花用户的钱，先征得同意）。花费计入 `result.cost_usd`，每次调用记在任务目录的 `assist.jsonl` 里。

## 9. `ask_model` 的任务

所有任务都只对 `succeeded` 的任务可用，并且不写入任何东西。

| `task` | 必填 | 可选 | 返回 |
|---|---|---|---|
| `retranslate` | `unit_ids`（至多 40） | `instruction` | `candidates[]` |
| `shorten` | `unit_ids` | `shorten_to`、`instruction` | `candidates[]`（不比原文短的候选不带 `fix`） |
| `review_units` | `unit_ids` | `instruction`（例如“只看术语”） | `problems[]`：`severity`（`wrong_meaning` / `omission` / `wrong_term` / `unreadable` / `minor`）、`explanation`，以及候选字段 |
| `review_pages` | `pages`（至多 5，从 1 开始） | `vision`（默认 true） | `problems[]`：`kind`、`severity`、`validation`（校验结论），通过校验的带 `fix` |
| `ask` | `prompt` | `unit_ids`、`pages` + `images`、`system`、`json_schema`、`max_output_tokens` | `text`；给了 `json_schema` 时另有解析后的 `json` |

每个候选：`unit_id`、`pages`、`old_translated_text`、`new_translated_text`、`quality`（`pass` / `needs_review` / `blocked`）、`issues`，可写回时还有 `fix`。每次返回都带 `calls`、`cost_usd`、`spent_usd`、`budget_usd`。

`review_pages` 用的就是流程里排版复核的同一套校验：它只采纳会损害阅读的问题（`wrong_meaning`、`unreadable`），并拒绝改动公式、编造英文、借“修正”改写原意的替换。流程已经对每页复核过一次，只在你有具体怀疑时对个别页再用。

## 10. 写回修正（`apply_fixes`）

每条修正：

```json
{"unit_id": "bd-...", "old_translated_text": "当前译文（未译出的单元写 null）",
 "new_translated_text": "新译文", "reason": "为什么改"}
```

- `old_translated_text` 必须和当前存储的译文完全一致，防止你基于旧版本的修改覆盖别人较新的修改；从 `get_units` 原样复制。
- 整批先全部校验：任何一条对不上或被质量门拦下（例如丢了标记），整批都不写入，错误信息说明是哪一条。
- `editor` 写明是谁改的，例如 `agent:claude-opus-5-5`。被替换的旧译文和修改人都会留痕。
- 写回后只重渲染涉及的页，并重新验收；不调用模型，不花钱。

## 11. 出问题时

| 现象 | 处理 |
|---|---|
| `failed`，日志里有 HTTP 402 或 `Insufficient Balance` | 模型账户余额不足。告诉用户充值，**不要循环重试**。充值后对同一任务 `resume_job`：已抽取的内容和已付费的译文直接复用，只做剩下的部分。不要重新提交新任务，那样会从头付费 |
| 401 / 403 | Key 无效或无权限。请用户检查 Key 或服务商 |
| 估价超过预算 | 告诉用户价格，由用户决定是否提高 `budget_usd` |
| `not_deliverable`，原因是单元从未译出（`exhausted`） | `get_units` 加 `states: ["exhausted"]` 找出来，自己译或 `retranslate`，然后 `apply_fixes` |
| 某段误译 | 自己改；量大时 `review_units` |
| 排版对不上：混入原页看不见的文字、伪代码被译成散文、符号错位、译文太挤 | 找到单元修正译文；看不到页面时用 `review_pages` |
| 公式图形本身显示异常 | 译文改不了公式图形。如实记录为已知问题 |
| 其他阶段失败 | `get_job` 加 `log_lines` 看日志尾部；抽取和渲染会自动重试崩溃的分块 |

## 12. 不要做的事

- 不要把 `not_deliverable` 的结果当作完成品交给用户，也不要把“文件已生成”说成“已通过验收”。
- 不要为了“再优化一版”重新提交整本翻译。修正写回只重排改动的页，几乎不花钱。
- 未经用户同意，不要提高预算。
- 不要把 API Key 写进文件、日志或提交记录。
- 有限的抽查通过不代表全书无误，汇报时说明检查覆盖了哪些范围。
