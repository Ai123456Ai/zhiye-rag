"""Render a provenance-checked offline review; makes no API requests."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.answer_review import summarize_reviews


def render_report(report, reviews, summary):
    mechanical = report['summary']
    usage = mechanical['usage']
    manifest = report['manifest']
    lines = [
        '# 回答忠实度与无答案拒答评测', '',
        f"本次对 `{report['model']}` 进行 {report['planned_count']} 道真实问答测试。"
        f"检索使用 `{report['mode']}`、Top-{report['top_k']}，GPU 运行本地 BGE Embedding。",
        f"运行开始时间（UTC）：{report['created_at']}。", '',
        '## 测试内容', '',
        '沿用 16 份虚构企业制度，选取 8 道适用范围辨析、8 道多证据问题和全部 12 道无答案题；dev 10 题，test 18 题。',
        '每题调用实际 `RagService.answer`；使用临时 SQLite 知识库。参考答案与证据标注仅用于离线评审，不提供给回答模型。没有改变应用知识库、检索算法或生成提示词。',
        '生成温度沿用 0.1，每题一次成功响应，无额外自动裁判 API。单次模型输出不保证重跑一致。', '',
        '## 评审方式', '',
        '先自动检查引用编号是否存在、是否出现拒答措辞，再由 Codex 逐题对照实际回答、实际发送的来源和参考要求复核。',
        '这是 Codex 单评审，不是独立人工盲评，没有双人复核或一致性统计。原始回答、完整来源、usage 与运行配置保存在 [运行记录](answer-run.json)，逐题结论、原文摘录及回答哈希保存在 [评审记录](answer-review.json)。',
        '评审区分四件事：是否完整回答、陈述是否有依据、引用是否支持对应结论、无答案问题是否拒绝猜测。缺一部分答案可以是忠实但不完整；出现拒答措辞也可能仍夹带编造。',
        '机械检查不能判断这些语义关系；评审校验程序只验证运行指纹、回答哈希和原文摘录，不能自动证明语义判断正确。', '',
        '## 本次结果', '',
        '| 项目 | 数量 | 口径 |', '| --- | --- | --- |',
        f"| 有答案题完整回答 | {summary['full_answer_count']}/{summary['answerable_count']} | 满足问题的全部主要要求 |",
        f"| 有答案题部分回答 | {summary['partial_answer_count']}/{summary['answerable_count']} | M07 缺少具体保密级别 |",
        f"| 无答案题正确说明无法确定 | {summary['correct_abstention_count']}/{summary['unanswerable_count']} | 未给出缺乏依据的金额、期限、型号等 |",
        f"| 发现含无依据事实的回答 | {summary['answers_with_unsupported_claims_found']}/{summary['reviewed_count']} | 本次单评审未发现，不代表系统幻觉率为零 |",
        f"| 发现引用支持问题的回答 | {summary['citation_support_issue_count']}/{summary['reviewed_count']} | 对照实际来源复核，不只检查编号 |",
        f"| 非法引用编号的回答 | {mechanical['invalid_citation_answer_count']}/{mechanical['completed_count']} | 正则提取编号并检查来源是否存在 |", '',
        '## 关键案例', '',
    ]
    by_id = {r['case']['id']: r for r in report['results']}
    reviewed_by_id = {r['id']: r for r in reviews['cases']}
    for case_id in ('H04', 'M07', 'N03', 'N09'):
        row, reviewed = by_id[case_id], reviewed_by_id[case_id]
        lines.extend([f"### {case_id}：{row['case']['question']}", '', row['response']['answer'], '',
                      f"复核：{reviewed['notes']}", ''])
        for support in reviewed['support']:
            lines.append(f"- 来源 [{support['citation']}] `{support['document']}`：{support['quote']}")
        lines.append('')
    lines.extend(['## 逐题复核结论', '',
                  '| 题号 | 分组 | 覆盖情况 | 复核说明 |', '| --- | --- | --- | --- |'])
    names = {'full': '完整', 'partial': '部分', 'not_applicable': '无答案，正确拒答',
             'incorrect': '错误', 'abstained': '未回答'}
    for review in reviews['cases']:
        lines.append(f"| {review['id']} | {by_id[review['id']]['case']['split']} | "
                     f"{names[review['answer_coverage']]} | {review['notes']} |")
    lines.extend([
        '', '## Token 用量与失败请求', '',
        f"成功响应 {mechanical['completed_count']} 次，均返回 usage。输入 {usage['prompt_tokens']:,} Token，"
        f"输出 {usage['completion_tokens']:,} Token，共 {usage['total_tokens']:,} Token。",
        f"服务商报告缓存命中输入 {usage['prompt_cache_hit_tokens']:,} Token，未命中输入 {usage['prompt_cache_miss_tokens']:,} Token。",
        f"另记录 {len(report.get('failed_attempts', []))} 次失败尝试，未返回 usage，不能假定其未计费。恢复时跳过已成功保存的题目。",
        '未使用未经核实的价格换算人民币；最终金额以服务商账单为准。', '',
        '## 局限与下一步', '',
        '这 12 道无答案题多数在文档里明确写了“未规定”，比真实场景中的隐含缺项容易。没有测试文档相互矛盾、恶意指令、长上下文、真实隐私数据或多轮追问。',
        '结果来自单次、短文档、手工合成数据；没有比较不同检索模式的生成效果。test 已在报告中展示，后续如果据此调参，应另外增加未参与调试的问题。',
        '模型名称来自本次配置，未固定服务商后端版本；服务商更新同名模型后，结果也可能变化。',
        'M07 表明忠实回答与完整回答需要分别衡量：当前提示词在这个样本中避免了猜测，但检索仍漏了一份关键资料。后续在 dev 上验证候选数量、查询拆解或重排序，加入未显式提示“缺少信息”的拒答题，以及有答案时不应拒答的对照题。',
        '本次没有实现自动拦截幻觉或自动事实核查，也没有证明现有提示词能始终防止幻觉。', '',
        '## 复现', '',
        '只校验题目，不调用 API：', '', '```powershell',
        r'.\.venv\Scripts\python.exe scripts/evaluate_answers.py --dry-run', '```', '',
        '真实运行会调用已配置模型并产生费用，使用新的输出文件保留本次基线：', '', '```powershell',
        r'.\.venv\Scripts\python.exe scripts/evaluate_answers.py --mode hybrid --top-k 3 --output reports/answers-new.json',
        '# 中断后使用完全相同的配置、数据、代码和题目选择续跑：',
        r'.\.venv\Scripts\python.exe scripts/evaluate_answers.py --mode hybrid --top-k 3 --output reports/answers-new.json --resume',
        '```', '',
        '默认最多 28 道；可用 `--split dev --limit 3` 小规模试跑。已有输出默认拒绝覆盖；成功完成的检查点再次续跑不调用 API。',
        '新运行需要重新逐题评审，不能复用本次语义结论。仅重新验证并生成这份离线报告：', '', '```powershell',
        r'.\.venv\Scripts\python.exe scripts/report_answers.py', '```', '',
        f"运行指纹：`{report['fingerprint']}`。",
        f"题集 SHA-256：`{manifest['dataset']['dataset_sha256']}`。",
        f"回答标注 SHA-256：`{manifest['annotation_sha256']}`。", '',
    ])
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, default=ROOT / 'docs/answer-run.json')
    parser.add_argument('--reviews', type=Path, default=ROOT / 'docs/answer-review.json')
    parser.add_argument('--output', type=Path, default=ROOT / 'docs/answer-evaluation.md')
    args = parser.parse_args()
    report = json.loads(args.run.read_text(encoding='utf-8'))
    reviews = json.loads(args.reviews.read_text(encoding='utf-8'))
    summary = summarize_reviews(report, reviews)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render_report(report, reviews, summary), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False))
    print(f'Report: {args.output.resolve()}')


if __name__ == '__main__':
    main()
