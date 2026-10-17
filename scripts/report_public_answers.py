"""Validate saved single-reviewer annotations and render the public answer report."""
import argparse
import json
from collections import Counter
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.answer_review import summarize_reviews


def render_report(report, reviews):
    summary = summarize_reviews(report, reviews)
    summary['limitation'] = '公开文档题与独立虚构场景、单次运行、Codex 单评审，非独立人工盲评。'
    by_id = {row['case']['id']: row for row in report['results']}
    reviewed = {row['id']: row for row in reviews['cases']}
    usage = report['summary']['usage']
    modes = Counter(row['response']['answer_mode'] for row in report['results'])
    names = {'normal': '公开文档正常问答', 'missing': '公开文档自然缺项',
             'false_premise': '公开文档错误前提', 'content_conflict': '虚构正文冲突',
             'version_conflict': '虚构版本歧义'}
    coverage = {'full': '完整', 'partial': '部分', 'incorrect': '错误',
                'abstained': '未回答', 'not_applicable': '不适用'}
    lines = [
        '# 公开文档回答、拒答与冲突评测', '',
        '使用已留档的 FastAPI 0.115.0 六篇中文教程，选取 8 道正常问题、6 道自然缺项问题，'
        '另编写 3 道错误前提问题；3 个独立虚构场景验证正文冲突与版本歧义。'
        '虚构运维规则不属于 FastAPI 官方文档，也不用于宣称框架行为。', '',
        f"运行时间（UTC）：{report['created_at']}。模型：`{report['model']}`，"
        f"检索：`{report['mode']}`，Top-K={report['top_k']}，重排序：{report['rerank']}。",
        '公开问答共用六篇文档的临时索引；三个冲突场景各用独立临时库。'
        '没有修改网页知识库。参考答案与评审要求不发送给模型。', '',
        f"完成 {len(by_id)} 题；实际模型回答 {modes['llm']} 次，"
        f"本地版本拦截 {modes['document_conflict']} 次。无证据本地返回 {modes['no_evidence']} 次。",
        f"评审：{reviews['reviewer']}。逐题对照问题、参考要求和实际召回原文；"
        '单个 Codex 评审，不是独立人工盲评，也未验证多评审一致性。', '',
        '## 分组结果', '',
        '| 分组 | 题数 | 满足参考要求 | 部分回答 | 发现无依据事实 | 引用支持问题 |',
        '| --- | --- | --- | --- | --- | --- |',
    ]
    for group, label in names.items():
        rows = [reviewed[key] for key, row in by_id.items() if row['case']['evaluation_group'] == group]
        good = sum((r['correct_abstention'] if group == 'missing' else r['answer_coverage'] == 'full')
                   and not r['unsupported_claims_found'] and r['citation_support'] != 'issue' for r in rows)
        lines.append(f"| {label} | {len(rows)} | {good}/{len(rows)} | "
                     f"{sum(r['answer_coverage'] == 'partial' for r in rows)} | "
                     f"{sum(r['unsupported_claims_found'] for r in rows)} | "
                     f"{sum(r['citation_support'] == 'issue' for r in rows)} |")
    lines.extend(['', '“满足参考要求”在缺项组表示正确拒答，在正文冲突组表示披露矛盾并引用双方，'
                  '在版本组表示本地拦截并列出候选；还须未发现无依据事实与引用支持问题。'
                  '“完整覆盖”与“来源支持充分”分别判断，不把这些行为合并成一个问答准确率。', '',
                  '## 逐题结论', '', '| 题号 | 分组 | 回答路径 | 覆盖 | 复核说明 |',
                  '| --- | --- | --- | --- | --- |'])
    for row in report['results']:
        case, response = row['case'], row['response']
        review = reviewed[case['id']]
        lines.append(f"| {case['id']} | {names[case['evaluation_group']]} | {response['answer_mode']} | "
                     f"{coverage[review['answer_coverage']]} | {review['notes'].replace('|', '/')} |")
    lines.extend(['', '## 主要发现', '',
        '- **跨章节问题漏证据。** FM02 的前三条全是异常处理章节，未找到正常响应过滤的 response_model；'
        'FM05 的前三条全是响应模型章节，未找到 HTTPException.detail 字典条款。两题均部分回答，未猜测缺失的一半。',
        '- **错误前提会改变召回。** FL04 正常问法召回了 dict/list 的明确规则；WP01 加入“只能传 str”前提后，'
        '相同配置没有召回该规则。模型没有顺从错误前提，但未能完整纠正，并引用了参数校验错误的 detail 列表示例，'
        '该示例不能直接证明 HTTPException.detail 的完整约束。',
        '- **知识正确不代表引用充分。** WP03 核心纠正方向正确，但额外解释 exclude_defaults=True 的语义，'
        '引用片段只列出参数名和一个未展开的外链，没有该语义说明。按“仅依据实际来源”标准记为一处无依据扩展和引用支持问题，'
        '不将它表述为该参数知识本身错误。',
        '- **自然缺项拒答。** 6 道缺项问题未补造连接池、超时、哈希成本、月费用、凭据或 P99 数值。'
        '这些样本比正文明确写“未规定”的合成题更接近自然资料缺失，但样本仍很少。',
        '- **两种冲突路径分别验证。** CF01/CF02 实际调用模型并披露两份文件/同文件内的正文矛盾；'
        'CF03 由元信息规则在本地列出候选。FM03 能区分两个教程示例的不同默认值，没有误当作同一规则冲突。',
    ])
    lines.extend(['', '## 用量与机械检查', '',
        f"有 usage 的成功响应 {report['summary']['usage_reported_count']} 次；"
        f"输入 {usage['prompt_tokens']:,}、输出 {usage['completion_tokens']:,}，"
        f"共 {usage['total_tokens']:,} Token。缓存命中输入 {usage['prompt_cache_hit_tokens']:,}，"
        f"未命中输入 {usage['prompt_cache_miss_tokens']:,} Token。",
        f"另有 {len(report.get('failed_attempts', []))} 次失败尝试，usage 未知，不能假定未计费。"
        '没有调用额外裁判 API；费用以服务商账单为准。',
        f"机械检查发现不存在的引用编号的回答数：{report['summary']['invalid_citation_answer_count']}。"
        '引用编号存在、出现拒答词均不能直接证明结论正确。', '',
        '## 结论的边界', '',
        '公开资料及其中 14 道题此前已经用于检索评测，结果不属于未见测试或真实用户盲测。'
        '错误前提和冲突题也是项目自行编写的少量样本；公开资料与虚构冲突场景分别统计。',
        '每题仅运行一次，没有比较生成模型、不同检索配置或多次采样。模型名称取自配置，'
        '未固定服务商实际后端版本；单题耗时不代表并发性能。',
        '正文冲突依赖实际检索到双方且模型遵循提示词；版本拦截依赖显式元信息。'
        '即使本次全部冲突案例通过，也不能声称具备通用矛盾检测或幻觉自动拦截能力。',
        '本轮没有测文档指令注入、多轮对话、长上下文、OCR、表格或真实生产负载。'
        '改动提示词或检索策略后，应使用新题验证，并保留本次基线。', '',
        '## 留档与复现', '',
        '- [题集与虚构场景](../evals/public-fastapi/answers.json)',
        '- [实际回答与来源 JSON](public-answer-run.json)',
        '- [逐题完整原始记录](public-answer-run.md)',
        '- [带回答/来源哈希的单评审记录](public-answer-review.json)', '',
        '只校验题目，不联网、不调用模型：', '', '```powershell',
        r'.\.venv\Scripts\python.exe scripts/evaluate_answers.py --dataset evals/public-fastapi/benchmark.json --corpus evals/public-fastapi/corpus --annotations evals/public-fastapi/answers.json --suite --dry-run',
        '```', '', '真实运行会产生 API 费用；使用新文件保留本次基线：', '', '```powershell',
        r'.\.venv\Scripts\python.exe scripts/evaluate_answers.py --dataset evals/public-fastapi/benchmark.json --corpus evals/public-fastapi/corpus --annotations evals/public-fastapi/answers.json --suite --mode hybrid --top-k 3 --output reports/public-answers-new.json',
        '```', '', '中断后保持配置、代码和数据不变，原命令追加 `--resume`；'
        '已成功保存的题目不会再次调用 API。新运行必须重新逐题复核，不能沿用本次评审。', '',
        '仅校验本次评审与回答、来源哈希，并重新生成报告：', '', '```powershell',
        r'.\.venv\Scripts\python.exe scripts/report_public_answers.py', '```', '',
        f"运行指纹：`{report['fingerprint']}`。",
        f"回答题集 SHA-256：`{report['manifest']['annotation_sha256']}`。", '',
    ])
    return '\n'.join(lines), summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, default=ROOT / 'docs/public-answer-run.json')
    parser.add_argument('--reviews', type=Path, default=ROOT / 'docs/public-answer-review.json')
    parser.add_argument('--output', type=Path, default=ROOT / 'docs/public-answer-evaluation.md')
    args = parser.parse_args()
    report = json.loads(args.run.read_text(encoding='utf-8'))
    reviews = json.loads(args.reviews.read_text(encoding='utf-8'))
    rendered, summary = render_report(report, reviews)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == '__main__':
    main()
