# FastAPI 公开文档评测集 v1

资料来自 [FastAPI](https://github.com/fastapi/fastapi) 发布标签 `0.115.0` 的六篇中文教程，按 MIT 许可分发；原始版权和许可全文保留在 [LICENSE](LICENSE)。本次通过 jsDelivr 下载，原始仓库路径、实际下载地址、UTC 下载时间和逐文件 SHA-256 记录在 [sources.json](sources.json)。使用的是发布标签加内容校验锁定，没有声称已解析到 Git commit。

`corpus/` 保存逐字节未改写的 Markdown 原文，不含此 README、许可或来源清单。这六篇原文共 50 个片段，使用项目现有 700 字符分块、100 字符重叠。外部代码包含指令 `{!...!}`、图片和链接未展开；只有文件中实际存在的文字属于知识库，其他章节的内容不属于本评测范围。评测衡量对这个版本资料的检索，不能代替对最新版 FastAPI 行为的核实。

[benchmark.json](benchmark.json) 包含在首次检索运行前编写并固定的 30 道问题：原词、口语改写、适用范围、多证据、无答案各 6 道；dev 10 道（8 道有答案、2 道无答案），test 20 道（16 道有答案、4 道无答案）。所有多证据题涉及两个不同章节。无答案题来自文档中自然缺失的信息，例如连接池大小、外部请求超时和具体性能数值，没有在原文中额外加入“未规定”的拒答提示。

问题、证据和参考答案由项目编写；文档是真实公开文档，问题并非真实用户日志或独立外部盲测。两组共享资料和部分概念；标签可能漏标其他等价证据。题集的 `reference_answer` 用于后续复核，不参与检索，不发送给模型。无答案只表示当前六篇资料无法充分作答，不表示互联网上没有答案。

```powershell
# 校验原文、许可、证据和题集，不联网、不调用生成 API：
.\.venv\Scripts\python.exe scripts/prepare_public_corpus.py
# 克隆时已包含资料；若误删了文件，可按校验值重新下载缺失项：
.\.venv\Scripts\python.exe scripts/prepare_public_corpus.py --download
# 只测基础检索，无 reranker 也可运行：
.\.venv\Scripts\python.exe scripts/benchmark.py --dataset evals/public-fastapi/benchmark.json --corpus evals/public-fastapi/corpus --split all --output reports/public-base.json --markdown reports/public-base.md
# 在同一个临时索引比较三种检索方式开启/关闭重排序：
.\.venv\Scripts\python.exe scripts/compare_reranking.py --dataset evals/public-fastapi/benchmark.json --corpus evals/public-fastapi/corpus --split all --output reports/public-fastapi.json --markdown reports/public-fastapi.md
```

后续调试使用 `--split dev`，本版题目不因实测结果改写。首次结果和失败分析见 [公开文档评测报告](../../docs/public-evaluation.md)。上述脚本使用独立临时数据库，不修改应用知识库，也不调用 DeepSeek。

## 独立的回答评测题集

[answers.json](answers.json) 是 `public-answer-v1`，保留原来的 benchmark.json、原始语料和检索标签不变。它引用原题中的 8 道正常问题和 6 道自然缺项问题，并另附 3 道错误前提题、3 个独立虚构冲突场景，共 20 道。

普通题使用六篇公开原文；两个正文冲突场景与一个同日版本歧义场景各使用 JSON 内显式标注的 `fixture_documents`，独立建临时库，不把虚构运维规则混入官方文档。错误前提题仍只用原始公开资料。所有题的证据和元信息在调用前校验。

`benchmark_id` 将题目与原检索标注对应，`evaluation_group` 区分正常、缺项、错误前提、正文冲突和版本歧义；`reference_answer` 只用于离线复核。合成冲突场景的参考要求也不会发送给模型。`split` 沿用既有题目的划分，新挑战题标为 test，但这些均不是独立外部盲测；后续不可围绕本次展示的题目反复调参后继续称作未见测试。

```powershell
# 离线校验，不加载模型、不产生 API 费用：
.\.venv\Scripts\python.exe scripts/evaluate_answers.py --dataset evals/public-fastapi/benchmark.json --corpus evals/public-fastapi/corpus --annotations evals/public-fastapi/answers.json --suite --dry-run
```

实际模型回答、单评审、失败案例和用量见 [回答评测报告](../../docs/public-answer-evaluation.md)。真实运行会产生 API 费用；检查点会跳过成功记录，配置、代码或数据变更后必须新建输出文件。

## 复合问题拆解对照

[decomposition.json](decomposition.json) 是另存的 `decomposition-fastapi-v1`：12 道跨章节多证据题、6 道单问题控制、6 道自然缺项，共 24 题，均标为 test。题集在拆解规则实现后、首次运行这些新问法之前固定，未按本次结果改题或调参；仍共享原六篇资料与部分已有知识点，由同一项目编写，不是独立外部盲测。

此前先在既有 benchmark 的 dev 上诊断规则；完整旧题另作已知案例回归，不能当作未见数据。新旧题共用原文与来源清单，各次对照在独立临时数据库内比较拆解关闭/开启，最终 Top-3 来源预算相同，拆解组检索计算更多。

```powershell
.\.venv\Scripts\python.exe scripts/compare_decomposition.py
```

脚本不调用生成 API，已有输出拒绝覆盖。新题 [检索报告](../../docs/query-decomposition.md)、[原始结果](../../docs/decomposition-run.json) 与 [分析](../../docs/decomposition-analysis.md) 保留实际分句、逐题来源和代码哈希。检索改善不等于回答质量改善。
