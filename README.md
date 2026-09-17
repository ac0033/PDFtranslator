# PDF Translation Runtime

这是按 `llm_pdf_translation_pipeline_plan.md` 实现的首个可运行版本。它优先保证可追溯、预算前置、严格契约、精确缓存和断点恢复，不会在缺少预算配置时调用收费模型。

当前工作候选版本为 `0.7.0rc13`。用户选定的 18 页 CS224n 讲义已完成真实 API 翻译、源文复核、专业术语约束和逐页视觉/保真验收，提供 18 页中文版与 36 页双语对照版。交付文件位于 `output/pdf/v0.7-rc13/delivery/`，完整证据见 `reports/v0.7_rc13_final_acceptance.json`。本轮累计核对 108 条批准术语、444 个原样资产与 1061 个代码字形。模型自动审校仍有漏报和误报，不能独立放行；本文档通过的是带逐页复核的验收流程。

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
- 每一次外部模型尝试都先写入独立尝试记录和总账；无效结构、严重保护标记或布局标记错误可有限重试，重试时把被拒译文和具体失败原因作为纠正反馈附加进请求（`retry_feedback_request`），重试上限跨恢复保持有效。
- V1 Context Engine：真实 PDF 单元带章节层级和局部邻接图；非连续抽样页之间禁止上下文泄漏。上下文还支持带作用域和审核状态的术语、风格规则、原文证据摘要、精确翻译记忆与依赖失效。
- `content-review.jsonl`：逐单元保存原文、译文、自动问题与人工语义/版面复核空位；即使 PDF 渲染失败也保留已付费结果和费用台账。页 42 的真实验证结果见 `reports/v0.7_rc1_validation.md`。
- `source_preserving`：实验性的源页副本编辑组件，只清空精确匹配的正文 `TJ` 操作后插入内嵌中文字体；落盘前后检查目标外的原字形/字体/字号/位置、图像、矢量路径、数学字形、数字/符号字形和 2× 像素。`source_mapping` 对保守匹配的捕获片段自动生成原文操作与单行译文位置，单独保留原页的章节编号。仍要求单一内容流及译文能放入原行，尚未接入 `pdf-translate` 的全页自动路径。零费用验证见 `reports/v0.7_rc3_mapping_probe.md`。
- `protected_literals`：从源 PDF 唯一锚定的文字中恢复 BabelDOC 占位符隐藏的短公式/脚注内容，把它们作为只读提示提供给模型，原保护标记和 PDF 图形保持不变。提示进入缓存键和费用上界；无法恢复的公式标记进入人工复核，并显示在 `content-review.jsonl`。既有捕获可在源文件哈希核验后内存补充，冻结文件不覆盖。零费用审计见 `reports/v0.7_rc4_protected_literals.md`。
- `plan_terminal_suffix_paragraph`：实验性地把已翻译的两行中文重新断行，只清空原正文文本操作，保留同一行末尾的原数字/冒号。写回器在可证明安全时把新增中文操作放回原文本流、置于原数字之前，使复制文本顺序与视觉阅读顺序一致；目标译文存在、文字顺序、目标外像素和整页原有符号都须通过检查。仅在第 42 页一个段落验证，未扩展为全页路径。证据见 `reports/v0.7_rc5_terminal_reflow.md`。
- `source_preserving` 的 rc.7 字形来源门：候选删除的 `TJ` 操作必须唯一对应到渲染出的字形起点；即使 MuPDF 将连续操作合并为一条字形序列，仍按下一操作的起点划开。所对应字形中只要有公式字体、数字或选定符号即拒绝；混排片段跨行连字符保留换行后再做精确比较。此门没有提升第 42 页的安全删除覆盖率，详见 `reports/v0.7_rc7_operation_provenance.md`。
- `map_single_line_mixed_native_prose`：仅当高置信单行片段恰有一个保护标记，且该标记与原 PDF 一个不可变字体 span 唯一对齐时，逐 span 分离正文与公式/变量。第 42 页 `P(x⁰)` 例中安全删掉三段正文操作，原位保留公式和 `X`；加入全页只删原文审计的失败关闭备选路径。尚未完成同段中文重排或整页接入，证据见 `reports/v0.7_rc8_mixed_line_mapping.md`。
- `interleaved_reading_order`：实验性地将每段中文插回对应已清空的原文操作后面，允许原公式和变量操作夹在中文之间。只在每个中文片段对应唯一源操作、下一原操作显式重置字体、生成流可唯一识别、嵌入前后整页 2× 渲染相同且中文/原锚点复制顺序正确时保存。一页诊断结果见 `reports/v0.7_rc9_interleaved_writeback.md`；未接入全页自动写回。
- `map_complete_semantic_unit_native_spans`：把完整语义单元中已经精确定位的正文进一步拆成原 PDF 的字体 span。2.2 开头三行拆为六条正文操作，脚注号 `5` 独立保留；写回器现在允许连续两条待清空操作之间无字体重置，但保护内容前仍需显式重置。三行中文诊断页及全页目标外审计见 `reports/v0.7_rc10_semantic_paragraph.md`；尚未自动适配其他段落。

## rc12 翻译质量修复

`configs/v0.7.quality.deepseek.toml` 启用 `join_cross_page_words`。原始 `units.jsonl` 和 `capture-map.jsonl` 保留，派生语义单元写入 `prepared-units.jsonl`，合并证据写入 `semantic-preparation.json`。跨页单元只翻译一次，写回前按全局片段分配；逐页复核读取报告绑定哈希的派生单元。几何启发式不能证明每个连字符都属于断词，每个合并仍需语义复核。

下面保留 rc11 的标准 CLI 示例。已生成的 rc12 结果见验证报告；对现有产物使用 `review-pages` 无须重新调用 API。

## 完整 PDF 与逐页验收

```powershell
# 捕获全份 PDF 并估算，不调用翻译 API
uv run pdftrans pdf-translate --input pdf/cs224n-2019-notes03-neuralnets.pdf --config configs/v0.7.page-review.deepseek.toml --output-root output/pdf/v0.7-rc11 --capture-root tmp/pdfs/v0.7-rc11
# 确认配置中的费用上限后真实调用 API；只复用当前输入哈希对应的捕获
uv run pdftrans pdf-translate --input pdf/cs224n-2019-notes03-neuralnets.pdf --config configs/v0.7.page-review.deepseek.toml --output-root output/pdf/v0.7-rc11 --capture-root tmp/pdfs/v0.7-rc11 --reuse-capture --execute-paid --render-review-draft
# 对已有运行重建图片和页面审计，不调用 API
uv run pdftrans review-pages --input pdf/cs224n-2019-notes03-neuralnets.pdf --output-dir <命令返回的output_dir>
```

入口输出 `page-review.html` 和 `page-review.json`，逐页列出原文、译稿、单元状态、公式/数字/图像审计及复核备注。缺失译文、未接受单元、PDF 缺失或页面数错误都会阻断对应页。即使所有自动检查通过，仍保持 `pending_review`，不会自动认定语义正确。

`--render-review-draft` 只允许全部单元均已有结果且不存在 critical 问题时生成待审稿；major 警告仍记录在逐页验收中，不会被清除。严格识别的短符号赋值公式保持原样；该修复允许已误送模型的公式从原台账恢复，不增加重试。跨页连字符片段会标为需要复核。预算耗尽和重试耗尽都会保留费用台账及逐段复核文件。可选 `agent-review.json` 必须绑定原文和译稿 SHA-256，并恰好覆盖全部选定页；它不等同于人工批准。

## 整本渲染与自动验收（无人值守）

```powershell
# 渲染整本：每 10 页一个子进程，崩溃自动重试、页级续跑，齐全后自动合并并写回书签
uv run python scripts/render_val_full.py --auto --capture output/pdf/<capture-dir> --config configs/<run>.toml --source pdf/<book>.pdf
# 判断能否交付：产出 acceptance-report.json；not_deliverable 时退出码非零
uv run pdftrans acceptance-report --source pdf/<book>.pdf --render-dir output/pdf/<capture-dir> --run-id <run_id>
# 单独查捕获层漏了什么（验收单默认已含此项）
uv run pdftrans capture-coverage --source pdf/<book>.pdf --capture-map output/pdf/<capture-dir>/capture-map.jsonl
```

渲染驱动把 BabelDOC 在 Windows 长进程下的随机内存分配失败隔离在有界子进程里（根因未消除），每块最多重试 3 次，某块耗尽不中断其余块，最后按 `missing_pages` 决定退出码。

验收单按 AGENTS.md 第 2 节分级：未完成翻译、critical 问题、缺页、成品页数不符等为大问题并阻断交付；其余告警先经分诊（`src/pdf_translation/triage.py`）消除可证明的检查自身产物和已由人裁定无害的条目，再按类别的累积阈值判定。传 `--run-id` 时以运行库为准，而不是渲染时写下的 `content-review.jsonl` 快照——后者不包含此后的 curated-TM 修复。

分诊只在两种情况下消除告警：规则能证明它是检查的分词产物，或人已在台账里判定无害（按 result_hash 键控，重译即失效）。对 val.pdf 的 165 条人工裁定实测：0 处真实错误被消除，数字告警 136→1，标记顺序 44→0。另新增三类此前没有的信号：`untranslated_unit`（整段未译，排除文献条目）、`leaked_markup`（译文里冒出的标记标签）、`retained_english`（中文行文里的英文，按术语分组，咨询性）。

报告明确声明**不检查**译文是否忠实、公式是否贴对位置、残留英文该不该译，并给出仍需人工对照原书的页数。

验收单还会把源 PDF 与捕获层交出的文本逐页对账（`src/pdf_translation/capture_coverage.py`）。**从未成为翻译单元的文本对其他所有检查都是隐形的**——不阻断、不告警、不进队列，只会以原文印在成品上。对账用 pymupdf 作第二意见，排除代码（等宽字体）、公式字形（数学字体）和 LaTeX 标准算子名。val.pdf 实测 92,076 个正文词中 139 个未捕获（0.151%），并查出第 88 页整列 27 个真值表单元格漏捕——此前无人知道。

目标、能力边界与明确未解决的问题见 `docs/unattended_delivery.md`。

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
