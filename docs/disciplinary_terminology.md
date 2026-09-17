# 专业术语验收

用户于 2026-09-14 要求使用学科约定译名，不按普通语言随意意译。当前文档明确固定 cost function → 成本函数，objective function → 目标函数。用户的 object function 按本上下文理解为 objective function；不将 object function 作为自动全局替换规则。

术语配置：config/cs224n-approved-terminology.json。导入现有 context-import 命令或 ContextStore；本轮已导入 runs/v0.7-rc13-terminology/context.sqlite。下一次真实翻译必须启用 context_engine=v1 并使用该词表（保留既有样式和上下文规则）。已核准术语作为 required 上下文进入请求；质量门阻断缺少指定译名或含禁用译名的结果，包括缓存复验。词表变化改变上下文快照和缓存键。

不同语境要区分：loss function 为损失函数；objective function 为目标函数；cost function 按本项目要求为成本函数。不能把独立出现的 cost、objective 或 object 一律按这些词组替换。其余术语随完整段落逐项核对并加入有来源的学科词表，不将这两项通过外推为整份术语已通过。

第 11 页已另存 terms 修订版，原始 API 输出和旧版保留。accepted_artifact_overrides 指向当前版本。已验收 5 页回查结果在 terminology-review.json。全篇仍需逐页完成术语、语义及视觉验收。
