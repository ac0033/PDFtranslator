# PDF Translation Runtime

把英文 PDF（教材、论文、讲义）翻译成中文，输出**中文单语版**和**中英双语对照版**，尽量保留原书版面、公式、图表和书签。

- 版面解析与回写基于 [BabelDOC](https://github.com/funstory-ai/BabelDOC)；翻译由大模型完成，按段落调用，每次调用都记账、可断点续跑。
- 付费前先估算整份文档的费用上限，超过配置里的 `budget_usd` 就不发任何请求。
- 翻译后自动验收：漏译、公式与数字保护、捕获覆盖度等问题分级报告，大问题阻断交付。

当前版本 `0.8.0`（0.x 阶段，接口仍可能调整）。

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

4. 看验收单：`deliverable` / `deliverable_with_known_issues` 可以交付；`not_deliverable` 会列出原因（例如某段三次都没译出）。人工核对后用 `scripts/apply_local_fixes.py` 写回修正并只重渲染受影响的页，不调用模型。

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
| `litellm` | LiteLLM 支持的任意模型（需先 `uv pip install litellm`），`model_id` 写成 `bedrock/...`、`azure/...` 等 | 按 LiteLLM 约定 |

可选覆盖项：`base_url`（换代理或私有部署地址）、`api_key_env`（换 Key 变量名）、`api_protocol`（`openai-responses` / `openai-chat` / `anthropic` / `gemini` / `litellm`）、`structured_output`（`json_schema` / `json_object` / `prompt`）。某个服务不支持指定的结构化输出方式时会自动降一级，返回的 JSON 始终在本地校验。费用上限按配置里的 `input_usd_per_million` / `output_usd_per_million` 计算，换模型时要一并改成该模型的价格。

## 目录

| 目录 | 内容 |
|---|---|
| `src/pdf_translation/` | 核心库：捕获、上下文、翻译流水线、质量门、渲染、验收 |
| `scripts/` | 可重复运行的驱动脚本：`deliver_pdf.py`（一键全流程）、`capture_val_full.py` / `translate_val_usable_sample.py` / `render_val_full.py`（各阶段）、`apply_local_fixes.py`（写回人工修正） |
| `configs/` | 配置示例与测试用配置 |
| `tests/` | 测试 |
| `docs/` | 设计与使用文档：`architecture.md`、`unattended_delivery.md`（无人值守交付与验收规则）、`quality_fix_policy.md`、`versioning.md`、`CHANGELOG.md` |
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
