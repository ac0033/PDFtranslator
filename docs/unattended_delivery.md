# 无人值守交付与验收规则

目标：对常规文本型 PDF 无人值守一次交付可用（扫描件 OCR 不在范围内）。无人值守的目标不是零错误，而是零人工兜底：出错时系统自己能发现，如实降级或报告，绝不静默交付坏结果。

本文件只写现行的用法和规则。各项能力的来由、每本书的实测数据和踩过的坑见 `archive/docs/unattended_delivery_history.md`。

## 1. 一条命令跑完一份 PDF

```powershell
uv run python -m scripts.deliver_pdf --source pdf/<文件>.pdf --config configs/<配置>.toml `
  --style-note-file configs/<风格说明>.txt --workers 3
```

依次执行抽取 → 覆盖度核对 → 翻译 → 渲染 → 排版复核 → 验收，各阶段耗时写入 `<输出目录>.timings.json`。每个阶段都能续跑，同一条命令重跑会从磁盘上已有的结果继续。`--resume-from render` 只重渲染缺失的页并重新验收，不抽取、不调用模型。

| 设置 | 作用 |
|---|---|
| `--workers N` | 抽取和渲染同时运行的进程数。每个进程按 2 GB 可提交内存计算，不够时自动下调 |
| 配置里的 `translation_concurrency` | 同时发出的翻译请求数。只影响速度，不影响运行身份，已有运行可以调高后续跑 |
| `--layout-review all / flagged / off` | 排版复核的范围，详见 README |
| `--estimate-only` | 只估价，不调用模型 |

实测：14 页讲义（285 个翻译单元）在 4 核笔记本上插电运行，`--workers 3`、并发 8，约 5 分钟，费用约 USD 0.10。用电池供电时抽取会慢 4 倍以上。

## 2. 分块进程与崩溃重试

BabelDOC 0.6.4 在 Windows 长进程里会随机出现内存分配失败，根因还没有消除。抽取（`capture_val_full.py`）和渲染（`render_val_full.py`）都通过 `chunk_driver.py` 按块在独立子进程里运行：

- 子进程崩溃就重跑这一块，最多重试 `--max-retries` 次；某一块重试用完不会中断整轮，其余块照常跑完，最后统一报告缺失的页。
- 页级断点：`page-NNNN/` 下的中文版和双语版两个文件都存在，这一页才算完成。
- 启动前预检可提交内存，低于 `--min-free-mb`（默认 2048）时立即报出具体数字，不会跑十分钟后死在 OpenBLAS 里。
- 页码范围以捕获报告里的 `physical_pages` 为准，所以只截取书中一段页码时，也能正常合并和验收。
- 没有译文的单元回退为原文渲染，这一页不会因此缺失；验收时仍按未翻译单元处理。

## 3. 自动验收单（`pdftrans acceptance-report`）

```powershell
uv run pdftrans acceptance-report --source pdf/<文件>.pdf --render-dir output/pdf/<run_id> `
  --run-id <run_id> --review-ledger <复核台账>.json
```

输出 `acceptance-report.json`，结论为 `deliverable`、`deliverable_with_known_issues` 或 `not_deliverable`。结论为 `not_deliverable` 时退出码非零。带上 `--run-id` 时以运行库为准；不带时报告会注明结论只基于 `content-review.jsonl` 快照，没有检查快照是否过期。

**大问题（阻断交付）**
- 有单元处于 `blocked`、`exhausted`、`prepared` 或 `not_started` 状态，或者没有译文。不设数量阈值，1 个也阻断。
- 存在 critical 质量问题。
- 有页面缺少完整的渲染结果，合并成品缺失，或页数与原文不符。
- 捕获覆盖度超过阈值（见第 5 节）。

**小问题**：分诊后仍需人工处理的告警按类别计数，给出代表页码。某一类的数量超过下表比例（占全部单元）时升级为大问题：

| 类别 | 阈值 | 说明 |
|---|---|---|
| `protected_source_literal_unavailable` | 60% | 公式图形原样保留，这一类反映捕获层的能力边界，不是译文缺陷 |
| `layout_boundary_unverifiable` | 30% | 原文自己的版面边界落在句子中间，无从核对 |
| `retained_english` | 30% | 仅作参考，检测器有已知噪声 |
| `number` | 5% | 数字清单变化是误译最强的信号 |
| `protected_marker_order` | 5% | 多数是中文语序调整，其余无法用规则区分 |
| `untranslated_unit` | 5% | 译文仍以拉丁字母为主的疑似漏译 |
| `url`、`terminology` | 2% | 直接改变含义 |
| `leaked_markup` | 1% | 标签原样显示在页面上 |
| 未列出的类别 | 5% | 没见过的类别从严处理 |
| 残留告警单元合计 | 80% | 兜底 |

阈值可以通过 `build_acceptance_report(thresholds=...)` 覆盖，实际使用的值会写进报告。

报告同时写明**没有检查**的内容：译文是否忠实于原文、公式图形是否挨着正确的文字、页数之外的版面质量；还会给出需要人工对照原书的公式占位符数量和涉及页数。

## 4. 告警分诊与复核台账

告警只有两种情况可以消除：

1. 规则能证明告警是检查本身造成的。例如 `Figure2.1` 与 `图2.1` 分词方式不同，导致数字清单对不上。
2. 人已经判定无害并记进复核台账。台账按译文哈希记录，重译后自动失效。

`tests/test_triage.py` 用人工标注过的真实告警做断言：被规则消除的真实错误必须为 0。

## 5. 捕获覆盖度（`pdftrans capture-coverage`）

用 PyMuPDF 独立解析同一页，找出看得见、但从未进入翻译的正文。代码（等宽字体）、公式字形（数学字体）和 LaTeX 算子名会被排除；认不出的字体按正文处理。未捕获的词超过全书正文的 1% 时判为大问题。只要 `capture-map.jsonl` 存在，验收单就会默认运行这项检查。

```powershell
uv run pdftrans capture-coverage --source pdf/<文件>.pdf --capture-map output/pdf/<run_id>/capture-map.jsonl
```

局限：只核对由字母组成的词；图注、示意图里的文字和表格单元格无法区分，都会报出来等人判断。

## 6. 模型复核告警（`pdftrans review-warnings`）

每条告警发一次简短调用，只问“这是不是真问题”。默认只估价，加 `--execute-paid` 才真正调用，加 `--update-ledger` 才把结论写进台账。

- 模型必须逐字引用原文和译文作为证据，并在本地校验；引用不出原文的结论记为失败。
- 模型的结论记为 `reviewed_by: model:<id>`，默认仍留在人工清单上，只附上理由；显式设置 `trust_model_verdicts=True` 后才采信。
- 不问模型答不了的问题：`protected_source_literal_unavailable`（模型同样看不到公式）和 `retained_english`（属于全书的术语决定）不送复核。

## 7. 其他工具

- `pdftrans retry-exhausted --run-id <id>`：修改质量规则后，让之前被判为 `exhausted` 的单元重新翻译。
- `scripts/apply_local_fixes.py`：写回人工核对过的译文，只重渲染受影响的页，不调用模型。

## 8. 已知限制

- BabelDOC 内存崩溃的根因没有消除，只能靠进程隔离和自动重试规避。
- 临时目录位置不能配置，也不预检磁盘空间。BabelDOC 用系统 TEMP，磁盘满时报出的错误看不出原因；目前只能在外部设置 `TMP` / `TEMP` 环境变量绕开。
- 分诊参数和各类阈值只在少数几本书上量化过，不能作为适用于任意 PDF 的保证。
