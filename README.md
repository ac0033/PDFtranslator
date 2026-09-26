# PDF Translation Runtime

把英文 PDF（教材、论文、讲义）翻译成中文，输出**中文单语版**和**中英双语对照版**，尽量保留原书版面、公式、图表和书签。

- 版面解析与回写基于 [BabelDOC](https://github.com/funstory-ai/BabelDOC)；翻译由大模型完成，按段落调用，每次调用都记账、可断点续跑。
- 付费前先估算整份文档的费用上限，超过配置里的 `budget_usd` 就不发任何请求。
- 翻译后自动验收：漏译、公式与数字保护、捕获覆盖度等问题分级报告，大问题阻断交付。

当前版本 `0.10.0`（0.x 阶段，接口仍可能调整）。

## 安装

需要 Python 3.12 与 [uv](https://docs.astral.sh/uv/)。

```powershell
uv sync --extra dev
uv run pytest            # 可选：跑测试
```

把模型 API Key 写进项目根目录的 `.env`（已被 Git 忽略，不要写进配置文件或提交记录）：

```text
DEEPSEEK_API_KEY=<你的 key>
```

## 翻译一份 PDF

1. 复制一份配置，至少改 `run_id`、`budget_usd`、`context_db`：

   ```toml
   run_id = "my-book-r1"
   provider = "deepseek"
   model_id = "deepseek-flash"
   style_revision = "v0.7-default-style"
   prompt_revision = "v0.7-complete-term-lock"
   adapter_revision = "babeldoc-0.6.4-usable-v1"
   pricing_revision = "deepseek-official-2026-09-15-peak-ceiling"
   max_output_tokens = 2000
   budget_usd = 1.00                  # 费用上限，不是账单
   input_usd_per_million = 0.30
   output_usd_per_million = 1.20
   context_token_budget = 8000
   api_key_env = "DEEPSEEK_API_KEY"
   base_url = "https://api.deepseek.com"
   reasoning_effort = "none"
   provider_max_attempts = 3
   output_token_policy = "adaptive-v1"
   context_engine = "v1"
   context_db = "runs/my-book/context.sqlite"
   translation_concurrency = 8        # 同时发出的翻译请求数
   ```

2. （可选）写一个 UTF-8 文本文件，说明这本书的领域和术语约定，例如"value function 译为价值函数"。

3. 一条命令跑完抽取 → 翻译 → 渲染 → 验收：

   ```powershell
   uv run python -m scripts.deliver_pdf --source pdf/<文件>.pdf --config configs/<配置>.toml `
     --style-note-file configs/<风格说明>.txt --workers 3
   ```

   结果在 `output/pdf/<run_id>/`：`*.zh.mono.pdf`（中文）、`*.zh.dual.pdf`（双语）、`acceptance-report.json`（验收单）、`content-review.jsonl`（逐段原文、译文与告警）。各阶段耗时写在 `output/pdf/<run_id>.timings.json`。

   同一条命令重跑会从磁盘上已有的结果继续，不会重复付费。14 页讲义在 4 核笔记本上约 5 分钟、费用约 USD 0.10。

   渲染之后、验收之前，流程会自动做一轮**排版复核**：把每页的原文图、译文图和该页各段原文与译文交给模型，找出中英文对不上的地方并直接给出替换译文。例如混入了原页看不见的隐藏文字、伪代码被翻成了散文、公式符号放错了位置、译文太长被挤小。替换译文必须通过质量门且不带来新问题才会写回，只重渲染被修改的页；结果写在 `layout-review.json`，费用计入同一个 `budget_usd`。`--layout-review flagged` 只复核有自动线索的页，`off` 关闭；模型不能看图时自动改为只发文字，也可用 `--no-vision` 指定。也可以对已有译稿单独运行：`uv run python -m scripts.review_layout --capture ... --config ... --source ... --output-prefix ...`（不加 `--execute-paid` 时只检测并估算费用）。

4. 看验收单：`deliverable` / `deliverable_with_known_issues` 可以交付；`not_deliverable` 会列出原因（例如某段三次都没译出）。人工核对后用 `scripts/apply_local_fixes.py` 写回修正并只重渲染受影响的页，不调用模型。

## 接入方式

翻译能力以“任务”为单位对外提供：提交后立即返回任务编号，任务在独立进程中运行，状态和产物都在磁盘上，调用方断开或服务重启都不会丢失。四种方式调用的是同一套任务服务。

**命令行**（输出均为 JSON，进度写到标准错误）：

```powershell
uv run pdftrans translate <文件.pdf 或 https://...> --budget-usd 1 --out-dir <目录>   # 等待完成并复制成品
uv run pdftrans translate <文件.pdf> --estimate-only                                   # 只估价，不调用模型
uv run pdftrans jobs list | status <job_id> [--wait] | log <job_id> | fetch <job_id> mono <保存路径> | cancel <job_id>
uv run pdftrans jobs resume <job_id>                                                   # 失败（如欠费）或取消后续跑，已付费部分不重复付费
uv run pdftrans providers                                                              # 可用的模型服务商
uv run pdftrans guide                                                                  # 给接入方的完整说明
```

加上 `--server <服务地址> --token <令牌>`（或环境变量 `PDFTRANS_SERVER`、`PDFTRANS_TOKEN`），同样的命令改为调用另一台主机上的服务。

**HTTP 服务**（供任意主机调用）：

```powershell
$env:PDFTRANS_TOKEN = "<一个足够长的随机串>"
uv run pdftrans serve --host 0.0.0.0 --port 8766 --public-url https://<对外地址>
```

监听本机以外的地址必须设置令牌，否则拒绝启动。接口：`GET /health`、`GET /v1/providers`、`POST /v1/jobs`（请求体为 PDF 字节，选项放在 `X-Job-Options` 头里；或 JSON `{"pdf_base64"|"pdf_url", "options"}`）、`GET /v1/jobs/{id}`、`GET /v1/jobs/{id}/log`、`GET /v1/jobs/{id}/files/{mono|dual|acceptance|layout_review|content_review|log}`、`POST /v1/jobs/{id}/cancel`、`POST /v1/jobs/{id}/resume`，以及下面“继续改进”用到的 `GET /v1/jobs/{id}/units`、`POST /v1/jobs/{id}/assist`、`POST /v1/jobs/{id}/revise` 和 `GET /v1/guide`。完整的接口描述（OpenAPI 3.1，可用来生成客户端）在 `GET /v1/openapi.json`。除 `/health` 外都要带 `Authorization: Bearer <令牌>`。某个任务要用调用方自己的模型 Key 时放在 `X-LLM-API-Key` 头里：它只进入该任务的进程环境，不写入磁盘。服务本身不提供 HTTPS，对公网开放时请放在反向代理后面。

**MCP**（供 Claude、Cursor 等 AI 工具调用；工具：`translate_pdf`、`get_job`、`get_job_file`、`list_jobs`、`cancel_job`、`resume_job`、`list_providers`、`get_units`、`ask_model`、`apply_fixes`；接入指南以资源 `pdftrans://docs/agent_guide` 提供）：

- 本机（stdio）：Claude Code 执行 `claude mcp add pdf-translation -- uv --directory <项目目录> run pdftrans mcp`；Claude Desktop 在配置文件的 `mcpServers` 里加入 `{"pdf-translation": {"command": "uv", "args": ["--directory", "<项目目录>", "run", "pdftrans", "mcp"]}}`。
- 远程（HTTP）：连接 `https://<对外地址>/mcp`，请求头带 `Authorization: Bearer <令牌>`。远程调用方只能上传 PDF 或给网址，不能指定服务器上的文件路径（除非以 `--allow-local-paths` 启动）。

**继续改进已完成的任务**：接入方读取被检查标记的单元（`jobs units <id> --with-issues` / `get_units`），自己判断并写出修正，或在合适时委托本任务的模型做一件事（`jobs ask <id> <task>` / `ask_model`：重译、压缩过长译文、复核一批单元、对照页面图像复核、自由提问；先估价、计入任务预算、只返回候选修正），然后写回（`jobs revise <id> fixes.json` / `apply_fixes`），服务只重渲染改动的页并重新验收，不调用模型。什么时候自己处理、什么时候委托模型，见 [`docs/agent_guide.md`](docs/agent_guide.md)。

**Skill**：把 `skills/pdf-translate/` 复制到 `~/.claude/skills/`（所有项目可用）或某个项目的 `.claude/skills/` 下，Claude Code 在用户要求翻译 PDF 时会按其中的步骤调用上述能力。

## 换用其他模型

配置里的 `provider` 选一个预设，填 `model_id`，把对应的 API Key 写进 `.env`：

| `provider` | 接口 | 默认 Key 变量 |
|---|---|---|
| `deepseek` | DeepSeek | `DEEPSEEK_API_KEY` |
| `openai` | OpenAI | `OPENAI_API_KEY` |
| `anthropic`（别名 `claude`） | Anthropic Claude | `ANTHROPIC_API_KEY` |
| `gemini`（别名 `google`） | Google Gemini | `GEMINI_API_KEY` |
| `qwen`（别名 `dashscope`） | 阿里云通义千问 | `DASHSCOPE_API_KEY` |
| `moonshot`（别名 `kimi`） | 月之暗面 Kimi | `MOONSHOT_API_KEY` |
| `zhipu`（别名 `glm`） | 智谱 GLM | `ZHIPUAI_API_KEY` |
| `doubao`（别名 `volcengine`） | 火山引擎豆包 | `ARK_API_KEY` |
| `siliconflow` / `openrouter` / `groq` / `mistral` / `xai` | 对应服务 | `SILICONFLOW_API_KEY` 等 |
| `ollama` | 本机 Ollama，无需 Key | — |
| `openai-compatible`（别名 `vllm`、`lmstudio`） | 任何兼容 OpenAI 接口的服务，必须填 `base_url` | 用 `api_key_env` 指定，本地服务可不填 |
| `litellm` | LiteLLM 支持的任意模型（需先 `uv pip install litellm`；LiteLLM 目前要求 openai<3，该环境的 openai 会降到 2.x，本项目两者都兼容），`model_id` 写成 `bedrock/...`、`azure/...` 等 | 按 LiteLLM 约定 |

可选覆盖项：`base_url`（换代理或私有部署地址）、`api_key_env`（换 Key 变量名）、`api_protocol`（`openai-responses` / `openai-chat` / `anthropic` / `gemini` / `litellm`）、`structured_output`（`json_schema` / `json_object` / `prompt`）。某个服务不支持指定的结构化输出方式时会自动降一级，返回的 JSON 始终在本地校验。费用上限按配置里的 `input_usd_per_million` / `output_usd_per_million` 计算，换模型时要一并改成该模型的价格。

## 目录

| 目录 | 内容 |
|---|---|
| `src/pdf_translation/` | 核心库：捕获、上下文、翻译流水线、质量门、渲染、验收 |
| `scripts/` | 可重复运行的驱动脚本：`deliver_pdf.py`（一键全流程）、`capture_val_full.py` / `translate_val_usable_sample.py` / `render_val_full.py`（各阶段）、`apply_local_fixes.py`（写回人工修正） |
| `configs/` | 配置示例与测试用配置 |
| `tests/` | 测试 |
| `docs/` | 设计与使用文档：`agent_guide.md`（给接入的工具和 agent）、`architecture.md`（流程与模块）、`unattended_delivery.md`（无人值守交付与验收规则）、`terminology_lock_design.md`、`versioning.md`、`CHANGELOG.md` |
| `archive/` | 已被取代的历史文档，仅供参考 |
| `pdf/`、`output/`、`runs/`、`tmp/` | 输入、产物、运行库与临时文件，均被 Git 忽略 |

## 命令行

`uv run pdftrans --help` 列出全部子命令。常用的：

| 命令 | 作用 |
|---|---|
| `acceptance-report` | 判断一份已渲染的译稿能否交付，并说明原因 |
| `capture-coverage` | 找出从未进入翻译的正文 |
| `review-warnings` | 让模型逐条判断剩余告警是否真问题（默认只估算费用） |
| `estimate-captured` | 离线估算已捕获单元的翻译费用上限 |

## 已知边界

- 公式按原书图形保留，程序无法核对公式与相邻文字的对应，需要抽查。
- 版面由 BabelDOC 在原文框内重排中文；个别公式字形会被拆开单独排版，出现孤立的符号。
- 在 Windows 上长时间运行 BabelDOC 会随机出现内存分配失败，因此抽取与渲染按小块分进程执行并自动重试。
