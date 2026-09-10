# PDF Translation V0

这是按 `llm_pdf_translation_pipeline_plan.md` 实现的首个可运行版本。它优先保证可追溯、预算前置、严格契约、精确缓存和断点恢复，不会在缺少预算配置时调用收费模型。

## 当前能力

- `inspect`：登记 PDF 的 SHA-256、物理页数和基础页面信息。
- `estimate`：离线估算待译文本 token 与最高费用；价格必须由用户显式填写。
- `run`：执行 JSONL Translation Unit；默认 `mock` 提供商，`openai` 提供商需预算和密钥。
- `resume`：仅继续输入哈希和配置哈希一致的未完成任务。
- `review`：运行保护标记、数字、URL 与空译文检查。
- `report`：输出状态、用量、缓存与问题汇总。
- SQLite 保存任务、结果、用量与事件；本地精确缓存避免热重跑再次调用模型。

## 快速开始

```powershell
uv sync --extra dev
uv run pdftrans --help
uv run pytest
```

离线演示：

```powershell
uv run pdftrans demo --output-dir demo
uv run pdftrans run --manifest demo/manifest.json --config configs/v0.mock.toml
uv run pdftrans report --run-id demo-run
```

## DeepSeek V4.1 Flash

项目已提供 `configs/v0.deepseek.toml`。DeepSeek-V4.1-Flash 的官方 API 模型 ID 是 `deepseek-flash`。把 API Key 填入项目根目录的 `.env`：

```text
DEEPSEEK_API_KEY=在这里填写
```

随后先设置一个明确的 `budget_usd`，再运行：

```powershell
uv run pdftrans estimate --manifest demo/manifest.json --config configs/v0.deepseek.toml
uv run pdftrans run --manifest demo/manifest.json --config configs/v0.deepseek.toml
```

配置默认关闭思考模式，减少纯翻译任务的额外输出费用。请求使用独立的 Responses API 调用和严格 JSON Schema，不串联历史响应。`.env` 已被 Git 忽略；禁止把 Key 写入 TOML、日志或提交记录。

## V0 尚待外部输入

仓库目前没有 `dm.pdf`、认可样稿、样稿页码映射和费用上限。因此真实 BabelDOC 一页注入/回写、Astra 质量复现、双语 PDF 与完整 V0 主验收仍是阻塞项；代码不会把这些项目标记为通过。
