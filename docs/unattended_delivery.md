# 无人值守交付：现状与能力边界

目标（用户 2026-09-17 确认）：**对常规文本型 PDF 无人值守一次交付可用**。

指导原则：无人值守的本质不是零错误，是零人工兜底；而兜底自动化的前提是失败模式可检测。**出错时系统自己能发现，如实降级或报告，绝不静默交付坏结果。** 扫描件 OCR 明确排除在首版能力边界之外。

推进顺序：① A 稳定性 + D 自动验收单 → ② B 复核规则固化 → ③ C 多样性语料库与边界标定 → ④ 公式识别改进 + LLM 复审层。

**本文件记录的是第 ① 步的实际状态。第 ②③④ 步尚未开始。**

## 为什么需要这一步

val.pdf（442 页）的中文版与双语版已交付，但那份"可用"是人工兜底出来的：一次完整生成之后又做了约 6 轮人工复核修补（详见 `v0.7_rc14_current_status.md`）。去掉那 6 轮，一次跑完的成品会带着误译和漏译交付。第 ① 步要解决的是其中两件事——让渲染能自己跑完，让"能不能交付"这个判断不再依赖人。

## A：渲染驱动（`scripts/render_val_full.py`）

BabelDOC 0.6.4 在 Windows 长进程下随机内存分配失败，**根因仍未消除**。现在的做法是把它隔离在有界的子进程里，并让驱动自己处理崩溃。

```bash
# 无人值守跑完整本并合并
uv run python scripts/render_val_full.py --auto \
  --capture output/pdf/<capture-dir> \
  --config configs/<run>.toml \
  --source pdf/<book>.pdf \
  --chunk-size 10 --max-retries 3

# 仍可单块手工渲染 / 单独合并
uv run python scripts/render_val_full.py --from-page 11 --to-page 20 ...
uv run python scripts/render_val_full.py --merge-only ...
```

行为：
- 驱动进程按 `--chunk-size`（默认 10 页）逐块 `subprocess` 调用本脚本自身，每块独立解释器。
- 子进程非零退出或崩溃 → 重跑该块，每块最多 `--max-retries`（默认 3）次。
- **某块重试耗尽不中断整轮**：继续跑完其余块，尽量多抢救页面，最后统一汇报。
- 页级断点语义不变：`page-NNNN/` 下 mono 和 dual **两个**文件都存在才算完成；只写了一个算未完成。
- 全部页就绪后自动合并并写回原书书签；退出码 0；否则打印 `missing_pages` 与失败/重试块清单，退出码非零。

顺带修掉一个既有缺陷：页码范围原先取自最后一个有捕获记录的页，而空白页不产生捕获记录，因此以空白页结尾的 PDF 会少渲染最后一页、随后合并必然因缺页失败。现改为取自源 PDF 页数，空白页走既有的"验证为空则原样插入"路径。val.pdf 因第 442 页早已由手工运行写在磁盘上而未暴露此问题。

val 专属硬编码已参数化（`--runs-root`、`--output-prefix`），默认值保持不变以兼容现有产物路径。

## D：自动验收单（`src/pdf_translation/acceptance.py`）

```bash
uv run pdftrans acceptance-report \
  --source pdf/<book>.pdf \
  --render-dir output/pdf/<capture-dir> \
  --run-id <run_id>            # 可选：与运行库交叉核对复核队列是否过期
```

产出 `acceptance-report.json` + 终端摘要；结论为 `not_deliverable` 时**退出码非零**，供无人值守脚本判断。

判定按 AGENTS.md 第 2 节分级：

**大问题（阻断交付）**：单元处于 `blocked`/`exhausted`/`prepared`/`not_started`；存在 critical 质量问题；单元无译文；源 PDF 某页缺少完整的 mono+dual 渲染产物；合并成品缺失；成品页数与原文不符；复核队列相对运行库过期且隐藏了阻断状态。

**小问题（记录待优化，不阻断）**：needs_review 的 major 项，按 issue category 聚合计数并给代表页码。

**累积升级**：AGENTS.md 第 2 节要求"单处轻微缺陷大量出现应提升为大问题"。实现为**按类别分别设阈值**（占全部单元的比例），而非单一总占比——因为不同类别指示的严重程度差异极大：

| 类别 | 阈值 | 理由 |
|---|---|---|
| `protected_source_literal_unavailable` | 60% | 公式图形原样保留，这标记的是捕获层的**已知能力边界**，不是译文缺陷；数学教材中天然高频 |
| `number` | 5% | 数字清单变化是**真实误译**的最强可用信号（val 全书 136 处中查出 5 处真错） |
| `protected_marker_order` | 5% | 多为中文语序重排（val 44 处中 43 处），但也确实抓到过真实错位 |
| `url` / `terminology` | 2% | 直接改变含义 |
| 未列出的新类别 | 5%（默认） | 未知即从严 |
| 任一小问题的总占比 | 80% | 兜底 |

阈值可通过 `build_acceptance_report(thresholds=...)` 覆盖，且原样写进报告。

**报告明确声明未检查什么**（AGENTS.md 第 5 节）：译文是否忠实于原文、保留的公式图形是否贴在正确的文字旁、页面呈现与页数之外的版面质量，均**不在自动检查范围**。报告给出仍需人工对照原书的公式占位符数量及涉及页数。

## 在 val.pdf 上的实际结论（2026-09-17）

```
verdict: deliverable_with_known_issues
units: 4963  accepted=3379, needs_review=1584
pages: 442/442 rendered; mono=442, dual=442
major problems: none
minor issues:
  protected_source_literal_unavailable  1466 units (29.5%, 阈值 60%)
  number                                 136 units ( 2.7%, 阈值  5%)
  protected_marker_order                  44 units ( 0.9%, 阈值  5%)
not machine checkable: 3998 个公式占位符，分布在 361 页，需人工对照原书
```

报告存档：`reports/v0.7_rc14_val_acceptance.json`。

这条结论只覆盖上述产物，**不构成对任何其他文档的能力证明**。

## 本步骤明确未解决的问题

1. **BabelDOC 内存崩溃根因未消除**，仅做了进程隔离 + 自动重试。换机器、换文档仍可能复发，只是不再需要人盯着。
2. **验收单不判断译文对错**。val 那 6 轮人工复核里查出的 5 处数字真错、14 处漏译、1 处脚注错位，当前验收单**抓不出来**——它只会把这些单元归入 `number`/`protected_marker_order` 小问题计数。把那些分诊结论固化成自动规则是第 ② 步 B 的工作，标注数据在 `tmp/` 下的 `number_audit_result.json`、`english_leak_audit.json`、`marker_order_audit.json` 等文件中。
3. **捕获层覆盖度无度量**。第 105 页 4 个 "true" 单元格未被捕获、以原文显示——这类漏捕获不会进入 content-review.jsonl，因此当前验收单**看不见**。逐页"提取文字量 vs 渲染文字量"对账是第 ③ 步的内容。
4. **通用性未验证**。A、D 的单元测试用合成 PDF，真实数据验证只有 val.pdf 一本。第 ③ 步才会用 3–5 本不同风格的 PDF 标定边界。
5. `--auto` 的完整链路**未在真实 442 页上端到端跑过**：val 的页级产物早已齐备，重跑会覆盖唯一可回退成品，因此只做了非破坏性核对（442/442 页断点齐全、分块计划覆盖全书）。驱动逻辑本身由 `tests/test_render_driver.py` 用假子进程覆盖成功/崩溃重试/重试耗尽三条路径。

## 据实区分

- **文件已生成**：`acceptance-report.json` 可对任意已渲染产物生成。
- **可用版本已通过**：val.pdf 的两个成品经自动验收判定为 `deliverable_with_known_issues`。
- **无人值守能力已具备**：**否**。渲染能自己跑完、交付判断能自动做出，但"一次跑完的译文质量足够"尚未成立（见上文第 2、3 点）。
- **版本已发布**：否。
