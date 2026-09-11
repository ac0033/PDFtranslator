# PDF Translation Runtime

这是按 `llm_pdf_translation_pipeline_plan.md` 实现的首个可运行版本。它优先保证可追溯、预算前置、严格契约、精确缓存和断点恢复，不会在缺少预算配置时调用收费模型。

当前软件版本为 `0.3.0`。路线图中的 V0/V1/V2/V3 表示开发阶段，不等于 SemVer 主版本号；在真实 PDF 翻译闭环和公开接口稳定前，软件保持 `0.x`。版本规则见 `docs/versioning.md`。

## 当前能力

- `inspect`：登记 PDF 的 SHA-256、物理页数和基础页面信息。
- `select-samples`：以固定随机种子冻结互不重叠的 smoke、development 和 frozen-test 页集，并记录实际版面风险覆盖。
- `babeldoc-spike`：用本地确定性记录器验证单页文本截获、公式占位与 PDF 回写，不调用收费模型。
- `estimate`：离线估算待译文本 token 与最高费用；价格必须由用户显式填写。
- `run`：执行 JSONL Translation Unit；默认 `mock` 提供商，`openai` 提供商需预算和密钥。
- `resume`：仅继续输入哈希和配置哈希一致的未完成任务。
- `review`：运行保护标记、数字、URL 与空译文检查。
- `report`：输出状态、用量、缓存与问题汇总。
- SQLite 保存任务、结果、用量与事件；本地精确缓存避免热重跑再次调用模型。
- V1 Context Engine：带作用域和审核状态的术语、风格规则、原文证据摘要、邻文选择、精确翻译记忆与依赖失效。

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

登记真实 PDF 后，可先离线冻结测试页；命令中的页码按阅读习惯使用 1 起始，manifest 内统一保存为 0 起始物理页索引：

```powershell
uv run pdftrans inspect --input pdf/dm.pdf --output benchmark.dm.json
uv run pdftrans select-samples --manifest benchmark.dm.json --smoke-page 42 --smoke-page 51 --smoke-page 577
uv run pdftrans babeldoc-spike --manifest benchmark.dm.json --page 42 --output-dir reports/babeldoc-spike-page42
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

## V1 Context Engine

```powershell
uv run pdftrans context-init --output data/context-v1.sqlite --document-id demo-doc
uv run pdftrans terms --manifest demo/manifest.json --output reports/term-candidates.json
uv run pdftrans context-import --manifest demo/manifest.json --input configs/context.bundle.example.json --context-db data/context-v1.sqlite
uv run pdftrans context-inspect --manifest demo/manifest.json --config configs/v1.mock.toml
uv run pdftrans ablate --manifest demo/manifest.json --config configs/v1.mock.toml --output reports/v1-ablation.json
```

术语候选不会自动升级成 `approved`；语义摘要必须引用当前 manifest 中存在的原文单元。Context Router 始终先放强制项，预算不足时拒绝运行，不能静默裁掉依赖。

## V0 尚待外部输入

仓库本地已有三本测试 PDF，其中 `dm.pdf` 已登记并冻结分层测试页；文件本身被 Git 忽略。BabelDOC 0.6.4 已完成第 42 页的本地合成译文注入/回写实验，但认可样稿及其页码映射仍未提供，真实翻译质量、稳定 TU 坐标、完整双语 PDF 与 V0 主验收尚未完成。
