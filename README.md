# PDF Translation Runtime

这是按 `llm_pdf_translation_pipeline_plan.md` 实现的首个可运行版本。它优先保证可追溯、预算前置、严格契约、精确缓存和断点恢复，不会在缺少预算配置时调用收费模型。

当前候选版本为 `0.7.0rc8`，Git 快照命名为 `v0.7.0-rc.8`。本版对单行混排片段新增严格的原生字形映射：仅删除可精确定位的英文正文操作，保留同一行的原公式和变量。第 42 页真实只删原文审计从 5/21 增至 6/21；这是安全删除覆盖，不是整页翻译完成率。整页和整本 PDF 仍不能正式使用。路线图中的 V0/V1/V2/V3 表示开发阶段，不等于 SemVer 主版本号；在完整文档接口稳定前，软件保持 `0.x`。版本规则见 `docs/versioning.md`。

## 当前能力

- `inspect`：登记 PDF 的 SHA-256、物理页数和基础页面信息。
- `select-samples`：以固定随机种子冻结互不重叠的 smoke、development 和 frozen-test 页集，并记录实际版面风险覆盖。
- `babeldoc-spike`：用本地确定性记录器验证单页文本截获、公式占位与 PDF 回写，不调用收费模型。
- `pdf-smoke`：按 manifest 中冻结的 smoke 页执行两遍式 BabelDOC 截获/注入；相邻碎片先按阅读顺序重组成语义段落，整段翻译后再按目标语言句界重排回原版面。输出单语 PDF、双语 PDF、对齐 JSONL 与运行报告。
- `pdf-smoke --reuse-capture`：复用已验证的 `units.jsonl` 和 `capture-map.jsonl`，跳过昂贵的版面捕获；复用页集必须与冻结的 smoke 页完全一致。
- `capture-development`：只捕获 manifest 中已经冻结的 development 页，生成真实 PDF 单元和上下文图；不会读取 frozen-test、调用模型或生成最终 PDF。
- `pdf-development`：只对 development 页执行翻译和回写；非 mock 提供商仍要求明确预算和 `--execute-paid`。
- `pdf-translate`：面向任意输入 PDF 的上下文优先入口；默认处理全书，也可用 `--source-page` 先做页级验证。配置必须使用 V1 上下文；默认零预算，付费仍需显式开关。复用捕获会核验源 PDF 哈希和页集，输出后自动审计图像、矢量图形与可识别的数学字体字形。所有路径均明确标注尚未达到正式使用门槛。
- `audit-nontext`：比较源页与译稿的页面尺寸、图像摘要/位置、矢量图形指令及可识别数学字体字形；其他未翻译字形、字体和全文位置尚未得到完整证明。
- `estimate-captured`：对已截获的 TranslationUnit JSONL 进行零调用费用估算，并排除代码/公式保护单元。
- `ab-context-init` / `ab-prepare-development`：建立无历史污染的实验上下文库，冻结受控 A/B 输入、配置哈希和费用上界；全程零模型调用。
- `ab-run-development`：默认只报告需要付费授权；只有同时给出显式执行开关和不超过人民币 2 元（按控制汇率 7.5 折算）的总预算才会运行，并保持两组预算相等。本轮完整 A/B 已结束；请勿重复付费运行。
- `ab-export-blind-review`：把两组已接受译文导出为匿名 X/Y 评审表，并把揭盲键单独保存。
- `estimate`：离线估算待译文本 token 与最高费用；价格必须由用户显式填写。
- `run`：执行 JSONL Translation Unit；默认 `mock` 提供商，`openai` 提供商需预算和密钥。
- `resume`：仅继续输入哈希和配置哈希一致的未完成任务。
- `review`：运行保护标记、数字、URL 与空译文检查。
- `report`：输出状态、用量、缓存与问题汇总。
- SQLite 保存任务、结果、用量与事件；本地精确缓存避免热重跑再次调用模型。
- 每一次外部模型尝试都先写入独立尝试记录和总账；无效结构、严重保护标记或布局标记错误可有限重试，重试上限跨恢复保持有效。
- V1 Context Engine：真实 PDF 单元带章节层级和局部邻接图；非连续抽样页之间禁止上下文泄漏。上下文还支持带作用域和审核状态的术语、风格规则、原文证据摘要、精确翻译记忆与依赖失效。
- `content-review.jsonl`：逐单元保存原文、译文、自动问题与人工语义/版面复核空位；即使 PDF 渲染失败也保留已付费结果和费用台账。页 42 的真实验证结果见 `reports/v0.7_rc1_validation.md`。
- `source_preserving`：实验性的源页副本编辑组件，只清空精确匹配的正文 `TJ` 操作后插入内嵌中文字体；落盘前后检查目标外的原字形/字体/字号/位置、图像、矢量路径、数学字形、数字/符号字形和 2× 像素。`source_mapping` 对保守匹配的捕获片段自动生成原文操作与单行译文位置，单独保留原页的章节编号。仍要求单一内容流及译文能放入原行，尚未接入 `pdf-translate` 的全页自动路径。零费用验证见 `reports/v0.7_rc3_mapping_probe.md`。
- `protected_literals`：从源 PDF 唯一锚定的文字中恢复 BabelDOC 占位符隐藏的短公式/脚注内容，把它们作为只读提示提供给模型，原保护标记和 PDF 图形保持不变。提示进入缓存键和费用上界；无法恢复的公式标记进入人工复核，并显示在 `content-review.jsonl`。既有捕获可在源文件哈希核验后内存补充，冻结文件不覆盖。零费用审计见 `reports/v0.7_rc4_protected_literals.md`。
- `plan_terminal_suffix_paragraph`：实验性地把已翻译的两行中文重新断行，只清空原正文文本操作，保留同一行末尾的原数字/冒号。写回器在可证明安全时把新增中文操作放回原文本流、置于原数字之前，使复制文本顺序与视觉阅读顺序一致；目标译文存在、文字顺序、目标外像素和整页原有符号都须通过检查。仅在第 42 页一个段落验证，未扩展为全页路径。证据见 `reports/v0.7_rc5_terminal_reflow.md`。
- `source_preserving` 的 rc.7 字形来源门：候选删除的 `TJ` 操作必须唯一对应到渲染出的字形起点；即使 MuPDF 将连续操作合并为一条字形序列，仍按下一操作的起点划开。所对应字形中只要有公式字体、数字或选定符号即拒绝；混排片段跨行连字符保留换行后再做精确比较。此门没有提升第 42 页的安全删除覆盖率，详见 `reports/v0.7_rc7_operation_provenance.md`。
- `map_single_line_mixed_native_prose`：仅当高置信单行片段恰有一个保护标记，且该标记与原 PDF 一个不可变字体 span 唯一对齐时，逐 span 分离正文与公式/变量。第 42 页 `P(x⁰)` 例中安全删掉三段正文操作，原位保留公式和 `X`；加入全页只删原文审计的失败关闭备选路径。尚未完成同段中文重排或整页接入，证据见 `reports/v0.7_rc8_mixed_line_mapping.md`。

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
uv run pdftrans ab-context-init --units output/pdf/v0.6-development-capture/units.jsonl --output data/context-v0.6-ab.sqlite
uv run pdftrans ab-prepare-development
uv run pdftrans ab-run-development
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

仓库本地已有三本测试 PDF，其中 `dm.pdf` 已登记并冻结分层测试页；文件本身和运行输出均被 Git 忽略。`v0.5.0` 已用 DeepSeek V4.1 Flash 在三页 smoke 上完成真实验收。`v0.6.0-rc.1` 在同一 74 单元上完成零费用上下文回归，并捕获 12 个 development 页形成 121 个真实单元。`v0.6.0-rc.5` 已对这 121 个单元完成受控 DeepSeek A/B：固定组自动通过 111、需复核 10；上下文组自动通过 114、需复核 7。本轮所有诊断及最终运行按配置单价估算合计 USD 0.1073217，按控制汇率 7.5 为人民币约 0.805 元；这不是服务商最终账单。117 对匿名译文已导出，尚未完成全量人工盲评，因此不能仅凭自动门宣称上下文改善翻译质量。frozen-test 未访问。详情见 `reports/v0.6_rc5_ab_result.md`。当前仍有三个版面边界：41 个 smoke 单元只有低置信度坐标；BabelDOC 的目录迁移会发出上游警告；公式内部英文及部分中英文间距仍需优化。
