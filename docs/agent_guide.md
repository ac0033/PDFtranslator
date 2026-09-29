# 接入指南：给调用本服务的程序与 AI agent

读完本文，你应当能够：提交一份文档并拿到中文版和双语版；知道任务什么时候、以什么形式把结果交给你；看懂每个字段；判断结果能否交给用户；需要时自己继续改进。

本服务把两类宿主都当作一等调用方：

- **纯代码宿主**（业务系统、工作流、脚本）：按字段判断，用回调或长轮询等结果，按版本下载文件并校验 SHA-256。
- **AI agent 宿主**（Claude、Cursor 等，经 MCP）：用 `wait_job` 在服务端等待，用任务里的 `for_agent` 向用户如实汇报，必要时自己动手改进。

MCP、HTTP、命令行三种接入方式调用同一套任务服务，字段完全一致（对照表见第 9 节）。仓库里的 `AGENTS.md` 是开发本项目的原则，其中关于“什么能交付”的标准同样约束你（第 6 节）。

## 1. 一句话流程

英文文档 → 格式识别 → 翻译工作流 → 自动验收 → **决策节点** →（不可用时）**AI OS 修复** → 可用版 → **优化版** → 交付。

- **翻译工作流**是确定性的：PDF 走版面抽取、翻译、回写、排版复核；LaTeX、HTML、Markdown、DOCX、EPUB 走源码原地替换（只替换正文文字，公式、代码、标签、引用逐字保留）。带版本号的 arXiv 论文 PDF 会先尝试取回 LaTeX 源码，编译结果与上传的 PDF 对得上才改走源码路线。
- **决策节点**：能不能交付由代码按交付标准判定（漏译一处即阻断，不设阈值）；每个阻断问题由规则表（配置了 Jev 时由 Jev）选一个修复动作。
- **AI OS**：服务内置的 [pi](https://pi.dev) agent，用你指定的模型推理，只能调用本服务的工具。修复有硬性上限：最多 3 轮、不超过任务预算、总时长有限、连续两轮没有进展就停。
- **结果永远会交给你**：可用版（`usable`）、优化版（`optimized`），或者修不好时的**如实降级版**（`degraded`：文件照给，但结论是不可交付）。只有工作流本身失败（例如文件损坏）才没有文件，并附错误码。

## 2. 准备：服务端环境

模型密钥放在**服务端的环境变量**里（或项目根目录的 `.env`），一次配好，之后每次调用都不用再传：

| 变量 | 用途 | 缺失时 |
|---|---|---|
| 按服务商约定，如 `DEEPSEEK_API_KEY`、`OPENAI_API_KEY`、`ANTHROPIC_API_KEY`、`GEMINI_API_KEY` | 翻译模型和 AI OS 模型 | 提交时报 `key_missing`，不建任务、不花钱 |
| `TYPESAFE_API_KEY` | Jev 告警分诊与修复路由（可选） | 不调用 Jev，改用规则表，任务照常 |
| `PDFTRANS_TOKEN` | 访问本服务的令牌 | 只允许本机访问 |
| `PDFTRANS_CALLBACK_SECRET` | 回调签名密钥（可选） | 使用回调时须随请求给 `X-Callback-Secret` |

AI OS 需要 Node.js 22.19 或更新版本，并执行一次 `uv run pdftrans setup`（把固定版本的 pi 装进项目的 `agent/node_modules`）。`GET /health` 会报告 AI OS 与 Jev 是否可用。AI OS 不可用时任务仍会运行，只是规则修不好的问题不会再交给 agent，结果如实降级。

请求头 `X-LLM-API-Key`、`X-Agent-API-Key`、`X-Jev-API-Key` 是备用方案，给无法改动服务器环境的远程宿主使用：只对这一个任务有效，只进入该任务的进程，不写入磁盘。

## 3. 提交任务

HTTP：`POST /v1/jobs`，JSON 请求体：

```json
{
  "source": {"base64": "...", "filename": "paper.zip", "main_file": "main.tex"},
  "translation": {"provider": "deepseek", "model": "deepseek-flash"},
  "agent": {"provider": "deepseek", "model": "deepseek-flash"},
  "deliver": {"optimized": true, "bilingual": true},
  "budget_usd": 2.0,
  "allow_network": true,
  "style_note": "强化学习教材；policy 译为 策略",
  "callback_url": "https://host.example/hooks/translation"
}
```

- `source`：`base64` 或 `url` 二选一（MCP 另有 `path`，见第 9 节）；`filename` 帮助识别格式。支持 `.pdf`、`.tex`、LaTeX 工程压缩包（`.zip`、`.tar.gz`）、`.html`、`.md`、`.docx`、`.epub`。也可以直接把文件字节作为请求体，JSON 选项放在 `X-Job-Request` 头里。v0.10 的 PDF 上传方式（`application/pdf` + `X-Job-Options`）继续可用。
- `translation`：翻译模型；`agent`：AI OS 的模型。字段：`provider`、`model`，可选 `base_url`（兼容 OpenAI 接口的自建端点）、`api_key_env`（读哪个环境变量）、`input_usd_per_million` / `output_usd_per_million`（服务不认识价格的翻译模型必须给，否则无法执行预算）。
- `deliver.optimized`：默认 `true`。明确不要优化版时设为 `false`。
- `budget_usd`：整个任务的花费上限，包括翻译、排版复核、修复、AI OS 和优化。翻译前先估价，超过上限一个请求也不发。
- `allow_network`：是否允许服务自己去 arxiv.org 取论文源码。默认 `true`。
- `estimate_only`：只估价，不调用模型。

### 模型的指定规则

| 情况 | 结果 |
|---|---|
| 某个宿主第一次调用，没给 `translation` 或 `agent` | 422 `model_required`，说明缺哪一个；不建任务、不花钱 |
| 第一次调用，两个都给了 | 记为该宿主的默认模型 |
| 之后没给 | 用该宿主的默认；任务视图里 `model_source` 为 `default` |
| 之后给了 | 这次按给出的用；默认不变，除非同时设 `set_default: true` |

“宿主”按请求里的 `client_id`（或 `X-Client-Id` 头）区分；没有时按访问令牌区分。不同宿主的默认互不影响。查看或修改默认：`GET/PUT /v1/profile`（MCP：`get_profile`、`set_profile`；命令行：`pdftrans profile`）。服务只记模型，不记密钥。

重试提交时带上 `Idempotency-Key` 头：同一宿主 24 小时内用同一个键提交，返回的是同一个任务。

## 4. 等结果

### 任务视图（`GET /v1/jobs/{id}`）

```json
{
  "job_id": "job-20260927-101500-a1b2c3",
  "status": "running",
  "phase": "optimize",
  "delivery": "usable",
  "recommended": "usable",
  "deliveries": [{"version": "usable", "verdict": "deliverable_with_known_issues", "recommended": true,
                  "files": {"mono": {"file": "paper.zh.pdf", "bytes": 183011, "sha256": "..."}}}],
  "problems": [{"kind": "number", "severity": "minor", "count": 2, "pages": [3]}],
  "agent": {"model": "deepseek/deepseek-flash", "model_source": "default", "repair_rounds": 1, "repair_stop_reason": "usable"},
  "cost_usd": {"translation": 0.11, "repair": 0.01, "agent": 0.003, "total": 0.123, "budget": 2.0},
  "events_after": 17,
  "for_agent": {"summary": "...", "summary_zh": "...", "next_step": "..."}
}
```

| `status` + `delivery` | 意思 | 宿主该做什么 |
|---|---|---|
| `queued` / `starting` / `running` + `none` | 还没有可交付的文件 | 等待 |
| `running` + `usable` | 可用版已发布，优化版在做 | 可以先把可用版交给用户 |
| `succeeded` + `optimized` | 优化版已发布，是推荐版本 | 交付优化版；可用版仍可下载 |
| `succeeded` + `usable` | 没要优化版，或优化稿不比可用版好被丢弃（原因见 `agent.optimize_note`） | 交付可用版 |
| `succeeded` + `revised` | 你写回修正后重新生成的版本 | 看 `recommended`：不比原版差时它是推荐版本 |
| `succeeded` + `degraded` | **有文件，但未通过验收** | 如实告诉用户这不是完成品，列出 `problems`（第 6 节） |
| `estimated` | 只估价的任务完成 | 把 `estimate.estimated_max_cost_usd` 告诉用户，确认后再正式提交 |
| `failed` | 工作流本身失败，没有文件 | 看 `error.code` 和 `error.message`，排除原因后 `resume` |
| `cancelled` | 已取消 | — |

`phase`：`translate`、`repair`、`optimize`、`revise`、`done`。`stage` 是阶段内的当前步骤。

### 事件

每个任务的事件都有递增序号。断线后从最后一个序号接着读，不会丢。

`job.accepted` → `stage.changed`（多次）→（不可用时）`repair.round` → `delivery.usable` 或 `delivery.degraded` → `delivery.optimized` → `job.finished`。另有 `job.failed`、`job.cancelled`、`delivery.revised`、`callback.abandoned`。

- **纯代码宿主，二选一**：
  - **回调**：提交时给 `callback_url`。服务把每个事件按序 `POST` 过去，JSON 请求体就是事件本身，附带 `X-Signature: sha256=<HMAC-SHA256(密钥, 请求体)>`、`X-Event-Seq`、`X-Job-Id`。对方返回非 2xx 时按指数退避重试，最多 24 小时，之后记一条 `callback.abandoned`。先用同一个密钥校验签名，再处理事件。回调由 `pdftrans serve` 发出。
  - **长轮询**：`GET /v1/jobs/{id}/events?after=<上次的序号>&wait=60`。有新事件立即返回，没有就最多等 60 秒。
- **AI agent 宿主**：调用 `wait_job(job_id, events_after, timeout_s≤50)`。它在服务端等待，返回新事件和任务当前状态，不要自己高频轮询。

签名校验示例（Python）：

```python
import hmac, hashlib
expected = "sha256=" + hmac.new(secret.encode(), request_body, hashlib.sha256).hexdigest()
assert hmac.compare_digest(expected, request.headers["X-Signature"])
```

## 5. 取文件

`GET /v1/jobs/{id}/files/{name}` 取推荐版本的文件；`GET /v1/jobs/{id}/files/{version}/{name}` 取指定版本（`usable`、`optimized`、`degraded`、`revised`）。MCP：`get_job_file`，参数 `version` 可选。下载后按任务视图里的 `sha256` 校验。

| 名称 | 内容 |
|---|---|
| `mono` | 中文版：PDF（PDF 与 LaTeX 输入）、`.html`、`.md`、`.docx`、`.epub`，与输入格式一致 |
| `dual` | 双语对照版，格式同上 |
| `source` | LaTeX 输入独有：翻译后的 LaTeX 工程压缩包，可以自己改了再编译 |
| `acceptance` | 验收单 JSON（第 6 节） |
| `content_review` | 每个单元一行 JSON：原文、译文、状态、告警 |
| `layout_review` | PDF 排版复核的每条决定 |
| `log` | 运行日志 |

单元的 `state`：`accepted`（通过质量门）、`needs_review`（有警告但可用）、`blocked` / `exhausted`（没有可用译文，必然阻断交付）。

**受保护标记**：`⟦P:...⟧` 代表公式、代码、引用、标签等必须原样保留的内容。结构化格式中，`⟦P:aN⟧ … ⟦P:zN⟧` 成对包住带格式的文字（强调、链接、粗体），开头必须在结尾之前；其他标记可以随中文语序移动。你写译文时要原样保留每一个标记，而且每个只出现一次。

## 6. 什么能交给用户

- `deliverable`：可以交付。
- `deliverable_with_known_issues`：可以交付，同时告诉用户验收单里记录的已知问题。
- `not_deliverable`（也就是 `delivery: degraded`）：**不能当作完成品交付**。文件照样给你，便于用户对照、便于你继续修，但要明确告诉用户未通过验收，并说出 `problems` 里的阻断原因。

AI agent 宿主可以直接使用 `for_agent.summary`（英文）或 `for_agent.summary_zh`（中文）。这段话按以上规则生成，不会把不可交付说成可交付。

验收检查的是：单元是否都译出、有没有漏译的英文句子、关键质量问题、告警是否超过阈值、输出文件是否齐全。结构化格式另外逐项核对公式、代码、引用、链接和标签与原文一致，LaTeX 还要能编译通过；PDF 另外核对页面齐全和抽取覆盖度。验收**不证明每句话都译得忠实**；用户要依赖译文内容时要说清这一点。不要把“文件已生成”说成“已通过验收”。

## 7. 错误码

错误统一为 `{"error": {"code", "message", "hint"}}`。

| `code` | HTTP | 含义 |
|---|---|---|
| `model_required` | 422 | 第一次调用没给 `translation` 或 `agent` 模型 |
| `key_missing` | 422 | 服务端没有该模型需要的环境变量 |
| `price_required` | 422 | 翻译模型价格未知，无法执行预算 |
| `unknown_provider` | 422 | 服务商名不认识，或 AI OS 连不上该服务商（给 `base_url`） |
| `invalid_request` | 422 | 字段不合法，`message` 指出是哪个字段 |
| `callback_secret_missing` | 422 | 给了 `callback_url` 却没有签名密钥 |
| `unsupported_format` | 415 | 不是支持的格式 |
| `source_too_large` | 413 | 超过 200 MB |
| `source_unreadable` | 400 | 取不到或读不了（例如 LaTeX 工程里没有主文件） |
| `job_not_found` / `version_missing` / `file_not_ready` | 404 | 没有这个任务、版本或文件（文件可能还没生成） |
| `not_ready` / `not_resumable` / `job_finished` | 409 | 任务状态不允许这个操作 |
| `fixes_refused` | 422 | 写回的修正整批被拒，`message` 说明是哪一条 |
| `unauthorized` | 401 | 令牌缺失或不对 |
| `budget_exceeded` | 402 | 委托 `ask_model`（`assist`）时估价超过任务预算，未调用模型。由用户决定是否提高 `budget_usd` |
| `upstream_error` | 502 | 模型服务调用失败（`message` 含异常类型与原文）。余额不足见第 12 节，不要循环重试 |
| `not_found` | 404 | 路由或文件名不存在 |
| `method_not_allowed` | 405 | 该路径不支持此方法（如 `GET /mcp`，或对动作路径用了 GET） |
| `bad_request` | 400 | 参数无法解析（如非整数的 `after`、传给 `assist` 的多余参数） |

任务失败时 `error.code` 可能是：`workflow_failed`（工作流中途停止，看日志）、`budget_too_low`（估价超过预算，未花钱）、`source_unreadable`、`runner_stopped`（运行进程意外退出）、`internal_error`。

## 8. AI OS 做了什么

`agent` 字段和 `state.json` 里的 `aios` 记录了修复与优化：

- **修复**（验收不通过时）：每轮对每个阻断问题执行一个动作。重译有问题的单元并附上问题说明、复核单元、续译从未发出的单元、重新生成输出、对照原页复核（仅 PDF）、交给 pi agent。停止原因（`repair_stop_reason`）：`usable`、`max_rounds`、`budget`、`deadline`、`no_progress`、`nothing_to_do`、`agent_made_no_change`。
- **优化**（可用版发布之后）：只针对剩余告警。先让翻译模型复核，只改真错误；复核发现了真错误但改不好时，才交给 pi agent，且 agent 花费不超过任务至今花费（至少 0.05 美元）。优化稿要和可用版比较：没有阻断问题、评级不降、剩余告警不增加，才会发布；否则丢弃，可用版仍是推荐版本。
- pi agent 只能调用本服务的工具：读问题和单元、重译、写自己的译文、对照页面复核、重新生成并验收、结束。它改不了宣布“可交付”的结论，每次修改都要过质量门。

## 9. 接入方式对照

| 动作 | MCP 工具 | HTTP | 命令行（`uv run pdftrans ...`） |
|---|---|---|---|
| 可用的模型服务商 | `list_providers` | `GET /v1/providers` | `providers` |
| 查看或修改默认模型 | `get_profile` / `set_profile` | `GET` / `PUT /v1/profile` | `profile [--provider ... --model ... --agent-provider ... --agent-model ...]` |
| 提交翻译 | `translate_document`（PDF 旧工具 `translate_pdf` 仍可用） | `POST /v1/jobs` | `translate <文件或网址> [--provider --model --agent-provider --agent-model]` |
| 等待新进展 | `wait_job` | `GET /v1/jobs/{id}/events?after=&wait=` | `jobs events <id> --after N --wait 60` |
| 查状态 | `get_job` | `GET /v1/jobs/{id}` | `jobs status <id> [--wait]` |
| 列出任务 | `list_jobs` | `GET /v1/jobs` | `jobs list` |
| 取消 | `cancel_job` | `POST /v1/jobs/{id}/cancel` | `jobs cancel <id>` |
| 失败或取消后续跑 | `resume_job` | `POST /v1/jobs/{id}/resume` | `jobs resume <id>` |
| 取文件 | `get_job_file` | `GET /v1/jobs/{id}/files/[{version}/]{name}` | `jobs fetch <id> <name> <路径> [--version]` |
| 读单元 | `get_units` | `GET /v1/jobs/{id}/units` | `jobs units <id>` |
| 委托任务的模型做一件事 | `ask_model` | `POST /v1/jobs/{id}/assist` | `jobs ask <id> <task>` |
| 写回修正、重新生成 | `apply_fixes` | `POST /v1/jobs/{id}/revise` | `jobs revise <id> <fixes.json>` |
| 本指南 | 资源 `pdftrans://docs/agent_guide` | `GET /v1/guide` | `guide` |
| 接口描述 | 工具的 `inputSchema` | `GET /v1/openapi.json`（OpenAPI 3.1） | 各子命令 `--help` |

命令行加 `--server <地址> --token <令牌>` 就改为调用远程服务（也可用环境变量 `PDFTRANS_SERVER`、`PDFTRANS_TOKEN`）。HTTP 除 `/health` 外都要带 `Authorization: Bearer <令牌>`；服务没有设置令牌时不校验，此时 `serve` 只允许监听本机回环地址。

补充接口（同一套任务服务，表里没列的部分）：

| 动作 | MCP | HTTP | 命令行 |
|---|---|---|---|
| 运行日志尾部 | `get_job` 的 `log_lines` 参数 | `GET /v1/jobs/{id}/log?lines=40` | `jobs log <id> [--lines N]` |
| 按条件读单元 | `get_units` 的 `pages`、`unit_ids`、`with_issues`、`states`、`limit` | `GET /v1/jobs/{id}/units?page=&unit_id=&with_issues=1&state=&limit=200`（`page`、`unit_id`、`state` 可重复） | `jobs units <id>` |
| 服务健康与能力 | — | `GET /health`（无需令牌；含版本、AI OS 是否可用、Jev 是否已配置） | — |

### 三种启动方式

| 命令 | 用途 |
|---|---|
| `pdftrans setup` | 一次性安装 AI OS（固定版本的 pi）。 |
| `pdftrans mcp` | 在本机以 stdio 运行 MCP 服务，给本机的 AI 客户端用；调用方可用服务器本地路径（`path`）指定文档。 |
| `pdftrans serve [--host 127.0.0.1] [--port 8765] [--token ...] [--max-running 2] [--public-url ...] [--allow-local-paths]` | 同时提供 HTTP 接口和 MCP（`POST /mcp`，仅支持 POST，`GET /mcp` 返回 405），也负责发出回调。监听回环地址以外的地址必须设置令牌；`--public-url` 决定下载链接里的地址；`--allow-local-paths` 让 MCP 调用方可以用服务器上的路径指定文档，默认关闭。 |

MCP 的 `get_job_file` 不直接返回文件字节：它返回本地路径（本机客户端）或下载链接（远程客户端）、SHA-256，`inline: true` 时才附内容（文本，二进制为 base64）。

## 10. 标准工作流

**纯代码宿主**

1. 第一次调用时带上 `translation` 和 `agent`；之后可省略。
2. `POST /v1/jobs`，带 `callback_url`（或记下 `job_id` 做长轮询）和 `Idempotency-Key`。
3. 收到 `delivery.usable` 就可以把可用版交给业务方；收到 `delivery.optimized` 再换成优化版；收到 `delivery.degraded` 按“未通过验收”处理；`job.failed` 看 `error.code`。
4. 下载后校验 SHA-256。

**AI agent 宿主**

1. **先估价**：`estimate_only: true`，不调用模型、不花钱。价格超过几美分时告诉用户，确认后再提交。
2. **提交**：`translate_document`。第一次要给两个模型；有领域或术语要求时写进 `style_note`，这比事后修改便宜得多。
3. **等待**：循环调用 `wait_job`，传入上次的 `events_after`，直到 `delivery` 不再是 `none`，或 `status` 为 `failed`。
4. **汇报**：用 `for_agent.summary_zh`，先说能不能用、提供了哪些版本，再说已知问题。
5. **需要继续改进时**（第 11 节）：`get_units` → 判断 → 写出修正或 `ask_model` → `apply_fixes` → 用 `wait_job` 等 `delivery.revised`。

## 11. 自己改进一个已完成的任务

AI OS 已经处理过它能处理的问题。你仍然可以按用户的具体要求继续改。

**自己处理**：看懂验收单、判断少量单元是否误译、修正明显的错误（漏掉的否定、数字、术语）、任何需要结合用户意图的决定。

**委托 `ask_model`**：要处理的单元很多、重译要和全书保持一致、需要看页面图像而你看不到、译文太长被挤小、调用方不是模型。

| `task` | 必填 | 可选 | 返回 |
|---|---|---|---|
| `retranslate` | `unit_ids`（至多 40） | `instruction` | `candidates[]` |
| `shorten` | `unit_ids` | `shorten_to`、`instruction` | `candidates[]` |
| `review_units` | `unit_ids` | `instruction` | `problems[]`，带严重程度、说明和候选修正 |
| `review_pages` | `pages`（仅 PDF，至多 5） | `vision` | `problems[]`，通过校验的带 `fix` |
| `ask` | `prompt` | `unit_ids`、`pages` + `images`、`system`、`json_schema` | `text`（给了 `json_schema` 时另有 `json`） |

委托得到的只是候选。质量门拦下的候选不带 `fix`；带 `fix` 的也由你决定是否写回。每个请求先估价，计入任务预算；传 `budget_usd` 可以提高该任务的上限，但这是花用户的钱，要先征得同意。

**写回**（`apply_fixes`）：每条修正为 `{"unit_id", "old_translated_text", "new_translated_text", "reason"}`。`old_translated_text` 必须和当前译文完全一致（从 `get_units` 原样复制；未译出的单元写 `null`）；整批先全部校验，任何一条不通过则整批不写。写回后服务重新生成输出并验收，发布为 `revised` 版本，不调用模型、不花钱。

## 12. 出问题时

| 现象 | 处理 |
|---|---|
| `key_missing` | 请服务运维把对应的环境变量加进服务端，而不是每次随请求传密钥 |
| `failed`，日志里有 HTTP 402 或 `Insufficient Balance` | 模型账户余额不足。告诉用户去充值，**不要循环重试**。充值后对同一任务 `resume`，已付费的译文直接复用；不要重新提交 |
| `budget_too_low` | 把估价告诉用户，由用户决定是否提高 `budget_usd` |
| `delivery: degraded` | 把 `problems` 告诉用户；想继续修时按第 11 节操作 |
| LaTeX 输入报 `latex_compile_failed` | 看 `acceptance` 的说明：源工程在服务端也编译不过时，是服务端缺宏包或编译器，不是翻译的问题。`source` 里的译后工程仍可取回自行编译 |
| arXiv 论文没走源码路线 | 看任务的 `arxiv.reason`：没有版本号、不允许联网、arXiv 只有 PDF，或编译结果与上传的 PDF 对不上 |

## 13. 不要做的事

- 不要把 `not_deliverable` / `degraded` 的结果当作完成品交给用户，也不要把“文件已生成”说成“已通过验收”。
- 不要为了“再优化一版”重新提交整本翻译。优化版是服务默认生成的；你的修正写回只重新生成输出，几乎不花钱。
- 未经用户同意，不要提高预算。
- 不要把 API Key 写进文件、日志或提交记录；密钥放服务端环境变量。
- 有限的抽查通过不代表全文无误，汇报时要说明检查覆盖了哪些范围。
