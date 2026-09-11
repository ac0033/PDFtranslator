# PDF Translation Runtime

这是按 `llm_pdf_translation_pipeline_plan.md` 实现的首个可运行版本。它优先保证可追溯、预算前置、严格契约、精确缓存和断点恢复，不会在缺少预算配置时调用收费模型。

当前候选版本为 `0.6.0rc1`，计划对应 Git 快照 `v0.6.0-rc.1`。路线图中的 V0/V1/V2/V3 表示开发阶段，不等于 SemVer 主版本号；在完整文档接口稳定前，软件保持 `0.x`。版本规则见 `docs/versioning.md`。

## 当前能力

- `inspect`：登记 PDF 的 SHA-256、物理页数和基础页面信息。
- `select-samples`：以固定随机种子冻结互不重叠的 smoke、development 和 frozen-test 页集，并记录实际版面风险覆盖。
- `babeldoc-spike`：用本地确定性记录器验证单页文本截获、公式占位与 PDF 回写，不调用收费模型。
- `pdf-smoke`：按 manifest 中冻结的 smoke 页执行两遍式 BabelDOC 截获/注入；相邻碎片先按阅读顺序重组成语义段落，整段翻译后再按目标语言句界重排回原版面。输出单语 PDF、双语 PDF、对齐 JSONL 与运行报告。
- `pdf-smoke --reuse-capture`：复用已验证的 `units.jsonl` 和 `capture-map.jsonl`，跳过昂贵的版面捕获；复用页集必须与冻结的 smoke 页完全一致。
- `capture-development`：只捕获 manifest 中已经冻结的 development 页，生成真实 PDF 单元和上下文图；不会读取 frozen-test、调用模型或生成最终 PDF。
- `pdf-development`：只对 development 页执行翻译和回写；非 mock 提供商仍要求明确预算和 `--execute-paid`。
- `estimate-captured`：对已截获的 TranslationUnit JSONL 进行零调用费用估算，并排除代码/公式保护单元。
- `estimate`：离线估算待译文本 token 与最高费用；价格必须由用户显式填写。
- `run`：执行 JSONL Translation Unit；默认 `mock` 提供商，`openai` 提供商需预算和密钥。
- `resume`：仅继续输入哈希和配置哈希一致的未完成任务。
- `review`：运行保护标记、数字、URL 与空译文检查。
- `report`：输出状态、用量、缓存与问题汇总。
- SQLite 保存任务、结果、用量与事件；本地精确缓存避免热重跑再次调用模型。
- 每一次外部模型尝试都先写入独立尝试记录和总账；无效结构、严重保护标记或布局标记错误可有限重试，重试上限跨恢复保持有效。
- V1 Context Engine：真实 PDF 单元带章节层级和局部邻接图；非连续抽样页之间禁止上下文泄漏。上下文还支持带作用域和审核状态的术语、风格规则、原文证据摘要、精确翻译记忆与依赖失效。

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
uv run pdftrans pdf-smoke --manifest benchmark.dm.json --config configs/v0.5.mock.toml --output-dir output/pdf/v0.5-semantic-smoke-mock
uv run pdftrans estimate-captured --units output/pdf/v0.5-semantic-smoke-mock/units.jsonl --config configs/v0.5.deepseek.toml
uv run pdftrans context-init-captured --units output/pdf/v0.5-semantic-smoke-deepseek/units.jsonl --output data/context-v0.6.sqlite
uv run pdftrans ablate-captured --units output/pdf/v0.5-semantic-smoke-deepseek/units.jsonl --config configs/v0.6.mock.toml --output reports/v0.6_context_ablation_smoke.json
uv run pdftrans capture-development --manifest benchmark.dm.json --output-dir output/pdf/v0.6-development-capture
```

## DeepSeek V4.1 Flash

项目已提供 `configs/v0.6.deepseek.toml`。DeepSeek-V4.1-Flash 的 API 模型 ID 配置为 `deepseek-flash`。把 API Key 填入项目根目录的 `.env`：

```text
DEEPSEEK_API_KEY=在这里填写
```

随后先用候选页计算上限，再设置一个明确的 `budget_usd` 并显式开启付费执行：

```powershell
uv run pdftrans estimate-captured --units output/pdf/v0.6-development-capture/units.jsonl --config configs/v0.6.deepseek.toml
uv run pdftrans pdf-development --manifest benchmark.dm.json --config configs/v0.6.deepseek.toml --output-dir output/pdf/v0.6-development-capture --execute-paid --reuse-capture
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

术语候选不会自动升级成 `approved`；语义摘要必须引用当前输入中存在的原文单元。Context Router 始终先放强制项，预算不足时拒绝运行，不能静默裁掉依赖。`ablate-captured` 只证明上下文选择和 token 变化，不声称翻译质量已经改善。

## 当前 PDF 边界

仓库本地已有三本测试 PDF，其中 `dm.pdf` 已登记并冻结分层测试页；文件本身和运行输出均被 Git 忽略。`v0.5.0` 已用 DeepSeek V4.1 Flash 在三页 smoke 上完成真实验收。`v0.6.0-rc.1` 又在同一 74 单元上完成零费用上下文回归，并捕获 12 个 development 页形成 121 个真实单元：117 个可译单元实际选入局部上下文，跨不连续样本页泄漏为零。development 的 DeepSeek 保守上限为 USD 0.321915；候选版未获本轮付费预算，因此没有运行翻译质量 A/B，也没有读取 frozen-test。当前仍有三个版面边界：41 个 smoke 单元只有低置信度坐标；BabelDOC 的目录迁移会发出上游警告；公式内部英文及部分中英文间距仍需优化。
