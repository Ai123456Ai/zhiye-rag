"""Compare fixed-budget retrieval with/without local clause decomposition. No generation API."""
import argparse
import hashlib
import json
import platform
import statistics
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.config import Settings
from app.evaluation import grouped_metrics, load_benchmark, score_case
from app.query_planning import POLICY
from app.service import RagService


def percentage(value):
    return '—' if value is None else f'{value:.1%}'


def markdown(report):
    lines = ['# 复合问题拆解检索对比', '',
             f"题集：`{report['dataset']['version']}`，split={report['split']}，共 {report['case_count']} 题。",
             f"每次请求独立取 Top-{report['top_k']}；重排序：{report['rerank']}；规则：`{POLICY}`。",
             '开关两组共用同一个临时索引，最终来源数上限相同；拆解组额外运行子问题检索，'
             '按子问题轮流取证据，再由原问题补充，按片段 ID 去重。启用重排序时对每个查询分别重排后合并。',
             '这是“分句检索加轮流合并”整体的比较，无法把收益单独归因于拆句或合并策略。'
             '原检索候选参数相同，但拆解组计算次数更多，不能声称计算预算相同。',
             '本次只测本地检索，不调用生成 API，也不测回答完整率、拒答或幻觉；应用知识库未用于评测。', '',
             '| 模式 | 拆解 | 完整证据命中 | 多证据题命中 | 单问题命中 | 已实际拆解 | 耗时中位数 ms |',
             '| --- | --- | --- | --- | --- | --- | --- |']
    for mode, data in report['modes'].items():
        for name, variant in data.items():
            metrics = variant['metrics']['all']
            groups = metrics['by_category']
            lines.append(f"| {mode} | {name} | {percentage(metrics['full_evidence_hit_rate'])} "
                         f"({sum(r['full_hit'] is True for r in variant['results'])}/{metrics['answerable_count']}) | "
                         f"{percentage(groups.get('multi_evidence', {}).get('full_evidence_hit_rate'))} | "
                         f"{percentage(groups.get('paraphrase', {}).get('full_evidence_hit_rate'))} | "
                         f"{sum(r['query_plan']['applied'] for r in variant['results'])}/{len(variant['results'])} | "
                         f"{variant['median_latency_ms']:.2f} |")
    lines.extend(['', '完整命中要求每条标注原句均出现在对应文件的最终来源中；标注可能漏掉等价证据。'
                  '无答案题不进入证据命中分母，仍召回资料不能直接认定为幻觉。', '', '## 逐题改善与退步', ''])
    for mode, data in report['modes'].items():
        off = {r['id']: r for r in data['off']['results']}
        changes = []
        lines.extend([f'### {mode}', ''])
        for row in data['on']['results']:
            previous = off[row['id']]
            if row['full_hit'] is not None and previous['full_hit'] != row['full_hit']:
                missing = [f"{e['document']}：{e['contains']}" for e in row['evidence'] if e['rank'] is None]
                changes.append(f"- {row['id']}：{'改善' if row['full_hit'] else '退步'} "
                               f"({previous['full_hit']} → {row['full_hit']})。{row['question']}"
                               + (f" 拆解后仍缺：{'；'.join(missing)}" if missing else ''))
        lines.extend(changes or ['没有完整命中状态变化。'])
        lines.append('')
    lines.extend(['## 无答案与成本', '',
                  '| 模式 | 拆解 | 无答案题数 | 仍召回比例 | 查询排名计算次数 |',
                  '| --- | --- | --- | --- | --- |'])
    for mode, data in report['modes'].items():
        for name, variant in data.items():
            metrics = variant['metrics']['all']
            lines.append(f"| {mode} | {name} | {metrics['unanswerable_count']} | "
                         f"{percentage(metrics['unanswerable_retrieval_rate'])} | "
                         f"{sum(len(row['query_plan']['queries']) for row in variant['results'])} |")
    lines.extend(['', '## 方法与限制', '',
        '规则只按明确标点和连接词处理最多三个子句；不做语义改写、不补齐省略主语，也不理解跨子句条件。'
        '短句、过多子句、来源数少于子问题数时回退原问题。反引号和引号中的分隔符不用于拆句。'
        '条件、比较或指代复杂时可能拆错，仍需人工核对展示的子问题。',
        '先用既有公开题的 dev 做诊断，再固定另一个题集的新问法运行；新问法共享六篇原文及部分既有概念，'
        '由同一项目编写，不是独立用户数据或外部盲测。没有按本次 test 结果再次调参。',
        '每题每组只测一次，轮流先开/先关，排除预热和模型加载；没有测并发或波动区间。'
        '并列排名仍受片段随机 ID 影响；同次开关对比共用索引，但重建索引后结果可能变化。',
        '候选顺序与检索/重排序分数分别保留；轮流合并后的总顺序不是按跨查询原始分数排序，'
        '这些分数都不是答案置信度。去重只按片段 ID，不把跨文档相同文本合并丢失来源。',
        '拆解默认关闭。尤其来源数较少时，分配给一个子问题的名额可能挤走另一条证据；'
        '既有版本筛选和歧义拦截仍执行，额外检索不等于证据充分性校验。', '',
        '## 复现与留档', '',
        f"运行时间（UTC）：{report['created_at']}；Python {report['python']}。",
        f"Embedding 运行状态：`{json.dumps(report['embedding_runtime'], ensure_ascii=False)}`。",
        f"题集 SHA-256：`{report['dataset']['dataset_sha256']}`。",
        f"资料 SHA-256：`{report['dataset']['corpus_sha256']}`。",
        'JSON 保存每题的实际子问题、来源、标注证据排名、耗时及代码哈希；老评测留档未被覆盖。', '',
        '```powershell',
        r'.\.venv\Scripts\python.exe scripts/compare_decomposition.py'
        f" --dataset {report['dataset_path']} --split {report['split']} --top-k {report['top_k']}"
        + (' --rerank' if report['rerank'] else ''),
        '```', '',
    ])
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, default=ROOT / 'evals/public-fastapi/decomposition.json')
    parser.add_argument('--corpus', type=Path, default=ROOT / 'evals/public-fastapi/corpus')
    parser.add_argument('--split', choices=['dev', 'test', 'all'], default='test')
    parser.add_argument('--modes', nargs='+', choices=['bm25', 'vector', 'hybrid'], default=['bm25', 'vector', 'hybrid'])
    parser.add_argument('--top-k', type=int, choices=range(1, 11), default=3)
    parser.add_argument('--rerank', action='store_true')
    parser.add_argument('--output', type=Path, default=ROOT / 'reports/decomposition.json')
    parser.add_argument('--markdown', type=Path, default=ROOT / 'reports/decomposition.md')
    args = parser.parse_args()
    if args.output.exists() or args.markdown.exists():
        parser.error('输出已存在，请用新的 --output 和 --markdown 保留原结果。')
    cases, documents, metadata = load_benchmark(args.dataset, args.corpus)
    cases = [case for case in cases if args.split == 'all' or case['split'] == args.split]
    if not cases:
        parser.error('没有选中的题目。')
    configured = Settings.from_env()
    if any(mode != 'bm25' for mode in args.modes) and not configured.embedding_model:
        parser.error('请配置本地 Embedding 模型，或仅选 --modes bm25。')
    if args.rerank and not configured.reranker_model:
        parser.error('请配置重排序模型，或省略 --rerank。')
    report = {'created_at': datetime.now(timezone.utc).isoformat(), 'dataset': metadata,
              'dataset_path': args.dataset.as_posix(), 'corpus_path': args.corpus.as_posix(),
              'split': args.split, 'case_count': len(cases), 'top_k': args.top_k, 'rerank': args.rerank,
              'policy': POLICY, 'python': platform.python_version(), 'generation_calls': 0,
              'live_database_used': False, 'embedding_model': configured.embedding_model,
              'embedding_device': configured.embedding_device,
              'reranker_model': configured.reranker_model if args.rerank else '',
              'reranker_candidates': configured.reranker_candidates,
              'code_sha256': {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in (
                  'app/service.py', 'app/query_planning.py', 'app/retrieval.py', 'app/reranking.py',
                  'app/document_policy.py', 'app/documents.py', 'app/store.py', 'app/evaluation.py',
                  'scripts/compare_decomposition.py')}, 'modes': {}}
    with tempfile.TemporaryDirectory(prefix='rag-decomposition-eval-') as tmp:
        service = RagService(Settings(db_path=Path(tmp) / 'eval.db',
                             embedding_model=configured.embedding_model,
                             embedding_device=configured.embedding_device,
                             reranker_model=configured.reranker_model if args.rerank else '',
                             reranker_device=configured.reranker_device,
                             reranker_candidates=configured.reranker_candidates))
        for name, body in documents.items():
            service.ingest(name, body)
        for mode in dict.fromkeys(args.modes):
            for enabled in [False, True]:
                service.retrieve(cases[0]['question'], args.top_k, mode, args.rerank, decompose=enabled)
            variants = {'off': {'results': []}, 'on': {'results': []}}
            for index, case in enumerate(cases):
                for enabled in ([False, True] if index % 2 == 0 else [True, False]):
                    started = time.perf_counter()
                    result = service.retrieve(case['question'], args.top_k, mode, args.rerank, decompose=enabled)
                    elapsed = round((time.perf_counter() - started) * 1000, 3)
                    row = {**score_case(case, result['sources'], args.top_k), 'query_plan': result['query_plan'],
                           'latency_ms': elapsed, 'warnings': result['warnings'],
                           'query_sources': [{'document': s['document_name'], 'ordinal': s['ordinal'],
                                             'retrieval_query': s.get('retrieval_query'),
                                             'rerank_score': s.get('rerank_score')} for s in result['sources']]}
                    variants['on' if enabled else 'off']['results'].append(row)
            for variant in variants.values():
                variant['metrics'] = grouped_metrics(variant['results'])
                variant['median_latency_ms'] = statistics.median(r['latency_ms'] for r in variant['results'])
            report['modes'][mode] = variants
            print(json.dumps({'mode': mode, **{name: {'full_hit_rate': v['metrics']['all']['full_evidence_hit_rate'],
                              'median_latency_ms': v['median_latency_ms']} for name, v in variants.items()}}), flush=True)
        report['embedding_runtime'] = service.embedder.runtime if service.embedder else {'loaded': False, 'device': 'CPU'}
        report['reranker_runtime'] = service.reranker.runtime if service.reranker else None
    for path, body in [(args.output, json.dumps(report, ensure_ascii=False, indent=2)), (args.markdown, markdown(report))]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body + '\n', encoding='utf-8')
        print(f'Report: {path.resolve()}')


if __name__ == '__main__':
    main()
