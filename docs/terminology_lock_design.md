# 完整术语识别与强制回填

用户要求 identity covariance matrix 固定译为“单位协方差矩阵”；此前将其拆成解释句的编辑已撤回。术语语义和指定表达须同时保留。

| 方案 | 主要问题 | 选择 |
| --- | --- | --- |
| 仅在提示词列出术语 | 模型或后续编辑仍可改写 | 只作语义参考 |
| 翻译后搜索替换 | 不知道术语被改写成哪种句式，可能误改普通语境 | 不作主机制 |
| 文档词组表 + 译前保护 + 固定回填 + 复查 | 需先可靠识别并核准术语 | 采用 |

流程：先扫描全文，收集完整技术词组及来源（单次出现也保留），对照学科含义核准译名，再按最长完整词组优先匹配。identity covariance matrix 必须优先于 covariance matrix。每次出现生成独立 ⟦T:...⟧ 标记。模型只能安排标记位置和翻译周边句子；程序验证标记恰好各出现一次，然后插入已核准的中文词条。缺失、重复、未知或损坏标记一律拒绝，保留响应ID和错误费用信息。

新 ContextItem 的 term_source/term_target 是结构化数据，避免解析提示词自然语言。实际 API 适配器已接入保护与回填，TranslationResult.terminology_evidence 保留模型原始标记文本和每个词组的源位置、译名及标记。提示版本改变使旧缓存失效。PDF 数学对象仍独立使用原始字形资产，不被术语机制重绘。

当前已实现：多词候选提取、最长匹配、真实适配器接入、严格标记检查、可审计回填、确定性术语质量门。已核准词表22项；全文575项候选尚未全部核准，不能当作完整覆盖证明。新词识别仍须逐段复查；不能声称只靠规则和现有词表就能100%识别所有学科术语。模型辅助识别复核属于后续真实验证范围，当前未完成。

验证：166项离线回归通过，包括最长词组优先、重复术语、单次出现词组、内部PDF标记过滤、真实API适配器的离线请求检查和漏标记拒绝。第15页使用既有真实RC12 API文本重新修订，完整术语、视觉、24个移动数学对象零像素差均复核。

新机制的真实调用尚未完成：自动审批拒绝把白化段落和术语表发送给DeepSeek，要求用户对具体内容和目的地明确授权。预估0.002724美元；请求为同一公开Stanford PDF第15页Whitening完整段落、公式占位符和相关核准词表，目的地https://api.deepseek.com，模型deepseek-flash。密钥只从本地.env读取，不写入报告。不得绕过拒绝，也不得把离线适配器测试当作真实调用成功。


第17页复核补充：现有25条已核定术语，新增RMS、root mean square、stochastic gradient descent。均方根译名依据 [MathWorks中文文档](https://ww2.mathworks.cn/help/matlab/ref/rms.html)，并与[英文定义](https://www.mathworks.com/help/matlab/ref/rms.html)核对。全文未知术语发现仍不能由当前词表匹配结果推定完成；新机制真实API验证仍待授权。


## Current candidate coverage revision

The approved glossary currently contains 53 entries. The latest candidate scan is
a per-document term-candidates report, bound to the source PDF and current
extracted-unit file hashes. It contains 727 review candidates; this count includes
ordinary words and nested phrases and is not a count of confirmed technical terms.
Historical 22/25-entry and 575-candidate counts above describe earlier snapshots.

Candidate discovery now includes noun heads for regression, product, norm,
network, classifier, vector, layer, representation, embedding and related plurals,
and permits a phrase to span a line break within a translation unit. It still
cannot cross punctuation, a protected object, a URL, or a stop-word boundary.
This recovers single-occurrence `binary logistic regression`, `outer product`,
`weight matrices`, and `word vectors` in the actual document, with source evidence.
It does not auto-approve candidates or establish exhaustive terminology coverage.
Unapproved examples needing sense/translation review include `hidden layer`,
`Frobenius norm`, `word vectors`, and `transfer matrix`.

34 targeted tests passed, including ten single-occurrence/line-break/plural
examples and six semantic-boundary counterexamples. These are offline checks.
New API terminology validation and remaining candidate adjudication are open.
