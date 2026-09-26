# 原生 PDF 翻译流程（RC13，尚未完成真实运行验收）

`pdftrans pdf-native` 使用完整原文单元、批准术语上下文、现有模型适配器和预算质量门，再进入原始字形/矢量对象保护排版。它不读取逐页人工校订脚本作为模型结果。

## 无网络准备

```powershell
.venv/Scripts/pdftrans.exe pdf-native --input pdf/cs224n-2019-notes03-neuralnets.pdf --config configs/v0.7.native.deepseek.toml --output-dir output/pdf/v0.7-rc13/native-workflow-prepare-r2 --font C:/Windows/Fonts/simfang.ttf --source-page 15
```

默认只保存 `units.jsonl`、`native-plans.json`、源文/配置身份清单与预算估算，不调用模型。重复准备要求源文、配置、原生计划和单元身份一致；内容改变应使用另一个输出目录。省略 `--source-page` 可准备全文。页面选择保留完整跨页段落，并保留全篇原文单元用于邻近上下文检索。

## 真实执行与验收

实际执行需要显式 `--execute-paid`。此开关进入既有 `run_units`，使用配置的提供商、术语锁定和质量门。只有全部选定单元处于 accepted 状态才开始排版；模型原始响应、术语回填证据与使用量保留在运行库和结果文件中。排版后仅标记为待语义/视觉审阅，绝不自动视为正式验收通过。

当前准备配置指向 DeepSeek 官方 API 和文档批准术语库。新术语机制的实际发送此前被自动审批拒绝，要求用户明确确认文本及目的地；在该确认到达前，不执行真实发送。本次仅验证准备和阻断行为，不能写成实际 API 管线已验收。

原生编辑器仍受字体/PDF结构适配范围约束；不支持的对象组合或无法容纳的译文会报错。失败后保留文件供检查，已存在的渲染 PDF 不会自动覆盖。

现有 18 页合并审阅版基于此前真实 API 底稿和显式本地校订，已通过逐页与合并保真检查；它与这里尚待真实运行验证的新入口是不同的验收证据。


### Preparation identity and live verification boundary

The manifest binds source PDF, target font bytes, page selection, configuration,
extracted units/plans, and the logical glossary/style/semantic context snapshot.
Changing any of these requires a fresh output directory. The runtime ID also
includes the context snapshot hash, preventing an already accepted task from a
previous terminology revision being reused under the new native preparation.
This does not freeze concurrent edits to the context database during execution;
keep that database unchanged while a run is active.

The checked-in native configuration has a zero-dollar budget. Preparation makes
zero API calls. The page 15 preparation selects 8 complete units, including full
cross-page text and routed neighboring context. Its configured-rate estimate is
USD 0.0100095; this is not a verified current provider price or a completed API run.
A paid budget and the pending concrete outbound-content approval must be resolved
before executing the new native workflow. Earlier RC12/RC13 API responses remain
separate provenance and do not prove this revised workflow has run live.


Execution reports distinguish `translation_failed`, `render_failed`, and
`render_blocked`. Rendering failures retain `blocked_page` and `rendered_pages`;
exceptions remain failures at the CLI. Existing selected page PDFs stop an
executing rerun before the translation runtime is called. Inspect existing output
and choose a fresh directory for a new rendering attempt; no output is overwritten.


### Source-bound review feedback

A unit-scoped StyleRule can carry expected_source_hash for specific review feedback.
The router checks that hash against the actual unit.source_text (not a potentially
stale unit.source_hash field) before including the rule. A reused ordinal with
different source text raises an error. Other unit IDs do not inherit the feedback,
and a source-bound rule cannot have document-wide scope. The two recorded page15
revision rules now carry this binding; a per-document style file (not shipped with the repository) stores
the reusable snapshot. Generic document style rules remain independent of a single
source paragraph. This validates feedback identity; it does not discover semantic
errors automatically or replace human review of a model's revised translation.
