"""Mechanical checks are review aids, never semantic hallucination scores."""
import json
import re
from pathlib import Path

from app.document_policy import DocumentMetadata
from app.documents import parse_document


def load_answer_cases(path: Path, benchmark_cases: list[dict]):
    annotations = json.loads(path.read_text(encoding='utf-8'))
    by_id = {case['id']: case for case in benchmark_cases}
    seen = set()
    cases = []
    if not annotations.get('version') or not annotations.get('cases'):
        raise ValueError('回答评测版本与标注不能为空。')
    for annotation in annotations['cases']:
        case_id = annotation.get('id')
        if case_id in seen or case_id not in by_id:
            raise ValueError(f'未知或重复的回答题目：{case_id}')
        reference = annotation.get('reference_answer')
        if not isinstance(reference, str) or not reference.strip():
            raise ValueError(f'{case_id}: 缺少参考答案。')
        seen.add(case_id)
        cases.append({**by_id[case_id], 'reference_answer': reference})
    return cases, annotations['version']


def load_answer_suite(path: Path, benchmark_cases: list[dict], documents: dict):
    """Validate a mixed public/explicitly synthetic suite before any paid request."""
    suite = json.loads(path.read_text(encoding='utf-8'))
    if not suite.get('version') or not suite.get('cases'):
        raise ValueError('回答评测版本与题目不能为空。')
    by_id = {case['id']: case for case in benchmark_cases}
    cases, seen = [], set()
    groups = {'normal', 'missing', 'false_premise', 'content_conflict', 'version_conflict'}
    for item in suite['cases']:
        source = by_id.get(item.get('benchmark_id'), {})
        if item.get('benchmark_id') and not source:
            raise ValueError('未知的公开题目引用。')
        case = {**source, **item}
        case_id = case.get('id')
        if not isinstance(case_id, str) or not case_id or case_id in seen:
            raise ValueError('回答题号缺失或重复。')
        seen.add(case_id)
        if case.get('evaluation_group') not in groups or case.get('split') not in {'dev', 'test'}:
            raise ValueError(f'{case_id}: 无效分组。')
        for field in ('question', 'reference_answer'):
            if not isinstance(case.get(field), str) or not case[field].strip():
                raise ValueError(f'{case_id}: {field} 不能为空。')
        if case.get('category') not in {'literal', 'paraphrase', 'scope', 'multi_evidence', 'unanswerable'}:
            raise ValueError(f'{case_id}: 无效检索类别。')
        fixtures = case.get('fixture_documents', [])
        if not isinstance(fixtures, list):
            raise ValueError(f'{case_id}: fixture_documents 须为列表。')
        bodies = {}
        for fixture in fixtures:
            name, text = fixture.get('name'), fixture.get('text')
            if (not isinstance(name, str) or not name.endswith('.md') or '/' in name or '\\' in name
                    or name in bodies or not isinstance(text, str) or not text.strip()):
                raise ValueError(f'{case_id}: 无效或重复的合成资料。')
            DocumentMetadata.model_validate(fixture.get('metadata', {}))
            bodies[name] = text.encode('utf-8')
        if bool(fixtures) != (case['evaluation_group'] in {'content_conflict', 'version_conflict'}):
            raise ValueError(f'{case_id}: 只有冲突场景使用独立的合成资料。')
        parsed = {name: parse_document(name, body) for name, body in (bodies or documents).items()}
        evidence = case.get('evidence')
        if (not isinstance(evidence, list)
                or bool(evidence) == (case['category'] == 'unanswerable')):
            raise ValueError(f'{case_id}: 证据与有无答案类别不符。')
        if case['category'] == 'unanswerable' and not case.get('reason'):
            raise ValueError(f'{case_id}: 资料缺失题必须说明原因。')
        for unit in evidence:
            name, quote = unit.get('document'), unit.get('contains')
            if (name not in parsed or not isinstance(quote, str) or not quote.strip()
                    or not any(quote in chunk.text for chunk in parsed[name])):
                raise ValueError(f'{case_id}: 证据不在本场景实际资料中。')
        cases.append(case)
    return cases, suite['version']


def inspect_answer(response: dict):
    """Only syntax/refusal markers; a plausible unsupported answer may pass."""
    answer = response['answer']
    sources = response['sources']
    citations = [int(n) for n in re.findall(r'\[(\d+)\]', answer)]
    valid = {source['citation'] for source in sources}
    invalid = sorted(set(citations) - valid)
    refusal_marker = bool(re.search(
        r'资料不足|无法确定|无法确认|无法回答|未(?:明确)?(?:提供|规定|说明|包含)|没有(?:提供|规定|明确)|未检索到|没有检索到', answer))
    return {
        'citation_ids': sorted(set(citations)), 'invalid_citation_ids': invalid,
        'has_citation': bool(citations), 'citation_ids_valid': not invalid,
        'refusal_marker_detected': refusal_marker,
        'semantic_verdict': 'pending_review',
    }


def summarize_answers(rows: list[dict]):
    successful = [r for r in rows if r.get('response')]
    negatives = [r for r in successful if r['case']['category'] == 'unanswerable']
    answerable = [r for r in successful if r['case']['category'] != 'unanswerable']
    def rate(rows, check):
        return sum(bool(check(r)) for r in rows) / len(rows) if rows else None
    usage_fields = ('prompt_tokens', 'completion_tokens', 'total_tokens',
                    'prompt_cache_hit_tokens', 'prompt_cache_miss_tokens')
    usage_rows = [(r['response'].get('usage') or {}) for r in successful]
    return {
        'completed_count': len(successful), 'error_count': len(rows) - len(successful),
        'answerable_count': len(answerable), 'unanswerable_count': len(negatives),
        'invalid_citation_answer_count': sum(bool(r['checks']['invalid_citation_ids']) for r in successful),
        'citation_presence_rate_answerable': rate(answerable, lambda r: r['checks']['has_citation']),
        'refusal_marker_rate_unanswerable': rate(negatives, lambda r: r['checks']['refusal_marker_detected']),
        'refusal_marker_rate_answerable': rate(answerable, lambda r: r['checks']['refusal_marker_detected']),
        'usage_reported_count': sum(bool(usage) for usage in usage_rows),
        'answer_modes': {mode: sum(r['response'].get('answer_mode') == mode for r in successful)
                         for mode in sorted({r['response'].get('answer_mode', 'unknown') for r in successful})},
        'usage': {field: sum(u.get(field, 0) or 0 for u in usage_rows) for field in usage_fields},
        'semantic_accuracy': None, 'faithfulness': None,
        'note': '引用存在和拒答词仅为机械检查；语义正确性、忠实度和实际拒答须逐题复核。缺失usage不代表没有计费。',
    }


def answer_markdown(report: dict):
    summary = report['summary']
    lines = [
        '# 回答评测原始记录', '',
        f"模型：`{report['model']}`；检索：`{report['mode']}`；Top-K：{report['top_k']}。",
        f"问题拆解：{report.get('decompose', False)}；重排序：{report.get('rerank', False)}。",
        f"计划 {report['planned_count']} 题，完成 {summary['completed_count']} 题，错误 {summary['error_count']} 题。",
        f"实际接口 usage 汇总：`{json.dumps(summary['usage'])}`。未按未经核实的单价估算费用，金额以服务商账单为准。", '',
        '**以下只是机械检查与完整原文记录，不是自动裁判判定的正确率。**',
        '检查范围：引用编号存在性、是否出现拒答措辞。含拒答措辞仍可能夹带编造事实；有引用也不代表引用支持结论。',
        '参考答案和原文标注仅用于离线评审，未发送给生成模型。所有记录使用临时知识库，未修改应用知识库。', '',
    ]
    for row in report['results']:
        case = row['case']
        lines.extend([f"## {case['id']}（{case['split']} / {case['category']}）", '',
                      f"问题：{case['question']}", '', f"参考要求：{case['reference_answer']}", ''])
        if not row.get('response'):
            lines.extend([f"请求失败：{row.get('error', 'unknown')}", ''])
            continue
        response = row['response']
        lines.extend(['### 实际回答', '', response['answer'], '',
                      f"机械检查：`{json.dumps(row['checks'], ensure_ascii=False)}`。", '',
                      f"证据完整召回：{row['retrieval']['full_hit']}；耗时：{response['latency_ms']} ms。", '',
                      f"回答路径：`{response['answer_mode']}`；"
                      f"模型调用：{'是' if response['answer_mode'] == 'llm' else '否'}。", '',
                      '### 实际来源（仅 llm 路径发送给模型）', ''])
        for source in response['sources']:
            lines.extend([f"#### [{source['citation']}] {source['document_name']} / 片段 {source['ordinal']}", '',
                          source['text'], ''])
    return '\n'.join(lines)
