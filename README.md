# PDF Translation Runtime

把英文文档翻译成中文，输出**中文版**和**中英双语对照版**。支持 PDF、LaTeX（单个 `.tex` 或整个工程压缩包）、HTML、Markdown、DOCX、EPUB。

- **PDF** 的版面解析与回写基于 [BabelDOC](https://github.com/funstory-ai/BabelDOC)，尽量保留原书版面、公式、图表和书签。带版本号的 arXiv 论文会先取回 LaTeX 源码，编译结果与原 PDF 对得上时改从源码翻译，公式原样保留。
- **LaTeX、HTML、Markdown、DOCX、EPUB** 直接在源文件里替换正文文字，公式、代码、链接、标签、引用和样式逐字保留，输出后逐项核对与原文一致；LaTeX 还会用 XeLaTeX 编译出 PDF。
- 翻译由大模型按段落完成，每次调用都记账，可断点续跑；付费前先估算整份文档的费用上限，超过 `budget_usd` 就不发任何请求。
- 翻译后自动验收，大问题阻断交付。验收不通过时，内置的 **AI OS**（开源 agent [pi](https://pi.dev)）在预算和轮次上限内修复；通过后先交付可用版，再生成**优化版**，优化版不比可用版差才发布。修不好时也会交出文件，并如实标明“不可交付”。

当前版本 `0.11.0`（0.x 阶段，接口仍可能调整）。

## 安装

需要 Python 3.12 与 [uv](https://docs.astral.sh/uv/)；AI OS 需要 Node.js 22.19 或更新版本；翻译 LaTeX 需要 TeX 发行版（MiKTeX 或 TeX Live，带 `xelatex` 和 `ctex` 宏包）。

```powershell
uv sync --extra dev
uv run pdftrans setup        # 把固定版本的 pi agent 装进 agent/node_modules，并检查 Node.js、LaTeX 和密钥
uv run pytest                # 可选：跑测试
```

`pdftrans setup` 装的是 `agent/package.json` 锁定的 pi 版本（`@earendil-works/pi-coding-agent`，MIT 许可），与系统里全局安装的 pi 互不影响。`uv run pdftrans setup --check` 只检查、不安装。没有 Node.js 时翻译照常进行，只是规则修不好的问题不会交给 agent。

模型 API Key 写进项目根目录的 `.env`（已被 Git 忽略），或服务端的环境变量：

```text
DEEPSEEK_API_KEY=<你的 key>
TYPESAFE_API_KEY=<可选：Jev 告警分诊与修复路由>
```

## 翻译一份文档（推荐方式）

```powershell
# 第一次调用要指定翻译模型和 AI OS 模型，之后可以省略，默认沿用第一次的
uv run pdftrans translate paper.pdf --provider deepseek --model deepseek-flash `
  --agent-provider deepseek --agent-model deepseek-flash --budget-usd 2 --out-dir out/

uv run pdftrans translate report.docx --out-dir out/            # 沿用默认模型
uv run pdftrans translate project.zip --main-file main.tex      # LaTeX 工程
uv run pdftrans translate page.html --no-optimized              # 只要可用版
uv run pdftrans translate book.epub --estimate-only             # 只估价
```

命令等任务结束后，把推荐版本的文件复制到 `--out-dir`：中文版、双语版、验收单，LaTeX 另有译后工程压缩包。任务先发布可用版，再生成优化版；进度与结果见 `pdftrans jobs status <job_id>`。

## 只跑 PDF 工作流（开发与调试用）

不经过任务服务，直接用配置文件驱动 PDF 工作流：

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

翻译以“任务”为单位对外提供：提交后立即返回任务编号，任务在独立进程中运行，状态、事件和各版本产物都在磁盘上，调用方断开或服务重启都不会丢失。命令行、HTTP、MCP 和 Skill 调用的是同一套任务服务。**完整的接口规范**（请求字段、模型规则、状态、事件与回调、错误码、两类宿主的标准流程）见 [`docs/agent_guide.md`](docs/agent_guide.md)。

- **命令行**：`pdftrans translate | jobs (list|status|events|log|fetch|cancel|resume|units|ask|revise) | profile | providers | setup | guide`，输出均为 JSON。加 `--server <地址> --token <令牌>` 改为调用远程服务。
- **HTTP 服务**：`pdftrans serve --host 0.0.0.0 --port 8766 --public-url https://<对外地址>`，监听本机以外的地址必须设置 `PDFTRANS_TOKEN`。主要接口：`POST /v1/jobs`（提交）、`GET /v1/jobs/{id}`（状态）、`GET /v1/jobs/{id}/events?after=&wait=`（长轮询）、`GET /v1/jobs/{id}/files/[{version}/]{name}`（下载）、`GET/PUT /v1/profile`（默认模型）；提交时给 `callback_url` 则每个事件都会带 HMAC 签名推送过去。OpenAPI 3.1 描述在 `GET /v1/openapi.json`。服务本身不提供 HTTPS，对公网开放时请放在反向代理后面。
- **MCP**（供 Claude、Cursor 等 AI 工具调用）：工具 `translate_document`、`wait_job`、`get_job`、`get_job_file`、`get_profile`、`set_profile` 等；任务结果里的 `for_agent` 给出可直接向用户汇报的中英文说明。
  - 本机（stdio）：Claude Code 执行 `claude mcp add pdf-translation -- uv --directory <项目目录> run pdftrans mcp`。
  - 远程（HTTP）：连接 `https://<对外地址>/mcp`，请求头带 `Authorization: Bearer <令牌>`。远程调用方只能上传文件或给网址，不能指定服务器上的文件路径（除非以 `--allow-local-paths` 启动）。
- **Skill**：把 `skills/pdf-translate/` 复制到 `~/.claude/skills/` 或某个项目的 `.claude/skills/` 下。

**模型与密钥**：第一次调用必须指定翻译模型和 AI OS 模型，之后不指定就沿用该调用方第一次指定的模型，指定了就按这次指定的用（`set_default` 才改默认）。不同调用方（按 `client_id` 或访问令牌区分）的默认互不影响。密钥放在服务端环境变量里；请求头 `X-LLM-API-Key`、`X-Agent-API-Key`、`X-Jev-API-Key` 只作为备用，只对一个任务有效，不写入磁盘。

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
| `src/pdf_translation/` | 核心库：捕获、上下文、翻译流水线、质量门、渲染、验收、任务服务 |
| `src/pdf_translation/formats/` | 源码格式适配器：LaTeX、HTML、Markdown、DOCX、EPUB，以及 arXiv 源码路线 |
| `src/pdf_translation/aios/` | AI OS：决策节点、Jev、修复循环、优化、pi 桥接与工具 |
| `agent/` | pi agent 的固定版本（`package.json` 与锁文件）、工具扩展 `pdftrans-tools.ts`、AI OS 的系统提示 `aios.md` |
| `scripts/` | 可重复运行的驱动脚本：`run_job.py`（任务运行进程）、`deliver_pdf.py`（PDF 一键全流程）、`capture_val_full.py` / `translate_val_usable_sample.py` / `render_val_full.py`（各阶段）、`apply_local_fixes.py`（写回人工修正） |
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
- 源码格式适配器在每种格式的若干真实文档上验证过，覆盖不了所有写法；没能翻译的正文会计入“遗留正文”，超过 1% 时阻断交付。
- Jev 分诊尚未用人工标注数据离线评估（需要 `TYPESAFE_API_KEY`）；没有密钥时使用规则表。
