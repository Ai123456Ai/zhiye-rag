"""Compare optional reranking on a shared temporary index; no generation requests."""
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

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.config import Settings
from app.evaluation import grouped_metrics, load_benchmark, score_case
from app.service import RagService


def pct(value):
    return '—' if value is None else f'{value:.1%}'


def markdown(report):
    dataset = report['dataset']
    lines = [
        '# 可选重排序实测', '',
        f"运行时间（UTC）：{report['created_at']}。共 {dataset['document_count']} 份评测资料、"
        f"{dataset['chunk_count']} 个片段，本次 {report['case_count']} 题，分组选择 `{report['split']}`。", '',
        '## 方法', '',
        '三种检索方式分别比较原始排名与重排序排名；每组共用同一个临时索引，未修改应用知识库，也未调用生成 API。',
        f"原始方式直接取前 {max(report['top_k'])} 条；重排序先召回最多 {report['candidates']} 条，"
        f"用 Cross Encoder 比较问题与片段，再取前 {max(report['top_k'])} 条；从结果计算 K={report['top_k']}。",
        '使用固定题目、参考文件名和原文证据。全部证据命中率要求每条证据都在前 K 条；MRR 仅表示第一条相关证据的排名，无答案题不计入这两个指标。',
        '另外统计候选池的完整证据覆盖，帮助区分“候选缺失”和“排序未排进前 K”。重排序只能调整候选池内的顺序。',
        '三组各预热一次，按每题先关闭、再开启的顺序串行测量；包含检索和适用时的重排序，排除模型加载；没有测并发性能。',
        '各分组表的耗时列统一显示全部所选题目的中位数，且对应实际取前 5 条的请求，不是单独测量 K=1 或 K=3 的请求。',
        '本次参数在评测前确定，未按结果调参。dev 用于诊断，test 用于固定基线；两组共享资料，题目由本项目编写，不是外部盲测。', '',
        '## 配置', '',
        f"- Embedding：`{report['embedding_model']}`，运行设备：`{report['embedding_runtime']['device']}`。",
        f"- Reranker：`{report['reranker_model']}`，运行设备：`{report['reranker_runtime']['device']}`。",
        '- 单对输入最多 512 Token，批大小 8，FP32，使用原始模型分数；长问题或长片段会被截断，可能损失末尾证据。',
        '- Reranker 分数不是置信度，未用阈值自动拒答。分数相同时保留候选原顺序；召回阶段并列仍按片段 ID 排序。',
        '- RRF 原始前 5 条使用每路最多 20 条，开启重排序后使用每路最多 40 条融合再取 10 条候选；当片段超过 20 条时，混合检索的对比同时包含候选扩大和重排序的效果，不能把全部差异归因于 Cross Encoder。候选池覆盖按每路 40 条、融合前 10 条计算。',
        f"- Python {report['python']}。数据 SHA-256：`{dataset['dataset_sha256']}`；资料 SHA-256：`{dataset['corpus_sha256']}`。", '',
    ]
    if provenance := dataset.get('provenance'):
        lines.extend(['## 公开资料来源', '',
                      f"- 仓库：{provenance['repository']}；版本：`{provenance['revision']}`；许可：{provenance['license']}。",
                      f"- 下载时间（UTC）：{provenance['retrieved_at']}；逐文件 SHA-256 与原始链接见题集旁的 `sources.json`。",
                      '- 原文来自公开文档，题目、参考答案与证据由本项目编写；真实文档不等于真实用户评测。',
                      '- 保留 Markdown 源文件原貌；外部代码包含指令、图片和链接未展开，本评测只使用文件中实际存在的文字。', ''])
    for split in ('dev', 'test', 'all'):
        if not any(split in variant['metrics'] for mode in report['modes'].values()
                   for variant in mode['variants'].values()):
            continue
        lines.extend([f'## {split} 结果', '',
                      '| 检索 | 重排序 | K | 有答案题数 | 全部证据命中率 | 平均证据召回率 | MRR | 总耗时中位数 ms |',
                      '| --- | --- | --- | --- | --- | --- | --- | --- |'])
        for mode, data in report['modes'].items():
            for name, variant in data['variants'].items():
                for k in report['top_k']:
                    m = variant['metrics'][split][str(k)]
                    mrr = '—' if m['mrr'] is None else f"{m['mrr']:.3f}"
                    lines.append(f"| {mode} | {'开' if name == 'on' else '关'} | {k} | {m['answerable_count']} | "
                                 f"{pct(m['full_evidence_hit_rate'])} | {pct(m['mean_evidence_recall'])} | "
                                 f"{mrr} | {variant['median_latency_ms']:.2f} |")
        lines.append('')
    lines.extend(['## 候选池覆盖（全部所选有答案题）', '',
                  '| 检索 | 候选上限 | 完整证据覆盖 |', '| --- | --- | --- |'])
    for mode, data in report['modes'].items():
        m = data['pool_metrics']['all']
        lines.append(f"| {mode} | {report['candidates']} | {pct(m['full_evidence_hit_rate'])} |")
    lines.extend(['', '## 发生变化的题目（K=3）', '',
                  '只记录完整命中状态或第一条相关证据排名发生变化的题目；完整来源及证据排名见 JSON。', ''])
    for mode, data in report['modes'].items():
        lines.extend([f'### {mode}', ''])
        off = {r['id']: r for r in data['variants']['off']['results']['3']}
        changes = []
        for row in data['variants']['on']['results']['3']:
            previous = off[row['id']]
            if row['full_hit'] is None:
                continue
            if (previous['full_hit'], previous['reciprocal_rank']) != (row['full_hit'], row['reciprocal_rank']):
                changes.append(f"- `{row['id']}`（{row['split']}）：完整命中 {previous['full_hit']} → {row['full_hit']}；"
                               f"倒数排名 {previous['reciprocal_rank']:.3f} → {row['reciprocal_rank']:.3f}。{row['question']}")
        lines.extend(changes or ['本次没有上述变化。'])
        lines.append('')
    lines.extend(['## 无答案题（全部所选问题，K=3）', '',
                  '| 检索 | 重排序 | 无答案题数 | 仍返回片段的比例 |', '| --- | --- | --- | --- |'])
    for mode, data in report['modes'].items():
        for name, variant in data['variants'].items():
            m = variant['metrics']['all']['3']
            lines.append(f"| {mode} | {'开' if name == 'on' else '关'} | {m['unanswerable_count']} | "
                         f"{pct(m['unanswerable_retrieval_rate'])} |")
    lines.append('')
    lines.extend([
        '## 边界', '',
        '这份报告只测检索，没有重新测量生成回答。不能把检索命中率当成问答准确率或幻觉率。',
        '无答案题仍可能召回片段；重排序未实现证据充分性判断，不能保证模型正确拒答。',
        '资料规模有限，标签可能漏标等价证据；单次时间受设备负载影响。是否开启应结合 dev 的证据覆盖、排序和额外耗时判断，保留关闭选项。',
        '', '## 复现', '', '```powershell',
        r'.\.venv\Scripts\python.exe scripts/compare_reranking.py --split all'
        + (f" --dataset {report['dataset_path']} --corpus {report['corpus_path']}" if 'dataset_path' in report else ''),
        '```', '',
        '默认写入 `reports/reranking.json` 和 `reports/reranking.md`。模型需先配置本地 `RERANKER_MODEL`。后续调试优先使用 `--split dev`。', '',
    ])
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--split', choices=('dev', 'test', 'all'), default='dev')
    parser.add_argument('--modes', nargs='+', choices=('bm25', 'vector', 'hybrid'), default=['bm25', 'vector', 'hybrid'])
    parser.add_argument('--dataset', type=Path, default=ROOT / 'evals/benchmark.json')
    parser.add_argument('--corpus', type=Path, default=ROOT / 'evals/corpus')
    parser.add_argument('--output', type=Path, default=ROOT / 'reports/reranking.json')
    parser.add_argument('--markdown', type=Path, default=ROOT / 'reports/reranking.md')
    args = parser.parse_args()
    cases, documents, metadata = load_benchmark(args.dataset, args.corpus)
    cases = [case for case in cases if args.split == 'all' or case['split'] == args.split]
    if not cases:
        parser.error('选定 split 没有问题。')
    configured = Settings.from_env()
    if not configured.reranker_model:
        parser.error('请先配置 RERANKER_MODEL。')
    if configured.reranker_candidates != 10:
        parser.error('比较脚本要求 RERANKER_CANDIDATES=10，以准确记录同样大小的候选池。')
    if any(mode != 'bm25' for mode in args.modes) and not configured.embedding_model:
        parser.error('向量与混合检索还需要 EMBEDDING_MODEL。')
    report = {
        'created_at': datetime.now(timezone.utc).isoformat(), 'dataset': metadata,
        'case_count': len(cases), 'split': args.split, 'top_k': [1, 3, 5],
        'dataset_path': args.dataset.as_posix(), 'corpus_path': args.corpus.as_posix(),
        'candidates': configured.reranker_candidates,
        'embedding_model': configured.embedding_model, 'reranker_model': configured.reranker_model,
        'python': platform.python_version(), 'generation_calls': 0, 'live_database_used': False,
        'code_sha256': {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                        for name in ('app/service.py', 'app/retrieval.py', 'app/reranking.py',
                                     'app/documents.py', 'app/evaluation.py', 'app/document_policy.py', 'app/store.py',
                                     'app/query_planning.py',
                                     'scripts/compare_reranking.py')}, 'modes': {},
    }
    with tempfile.TemporaryDirectory(prefix='rag-rerank-eval-') as tmp:
        service = RagService(Settings(
            db_path=Path(tmp) / 'eval.db', embedding_model=configured.embedding_model,
            embedding_device=configured.embedding_device, reranker_model=configured.reranker_model,
            reranker_device=configured.reranker_device, reranker_candidates=configured.reranker_candidates))
        for name, body in documents.items():
            service.ingest(name, body)
        for mode in dict.fromkeys(args.modes):
            for enabled in (False, True):
                service.search(cases[0]['question'], 5, mode, rerank=enabled)
            variants = {name: {'results': {str(k): [] for k in (1, 3, 5)}, 'latencies_ms': []}
                        for name in ('off', 'on')}
            pool_results = []
            for case in cases:
                pool = service.search(case['question'], 10, mode)
                pool_results.append(score_case(case, pool, 10))
                for enabled, name in ((False, 'off'), (True, 'on')):
                    start = time.perf_counter()
                    hits = service.search(case['question'], 5, mode, rerank=enabled)
                    elapsed = (time.perf_counter() - start) * 1000
                    variants[name]['latencies_ms'].append(elapsed)
                    for k in (1, 3, 5):
                        variants[name]['results'][str(k)].append({
                            **score_case(case, hits, k), 'search_latency_ms': round(elapsed, 3),
                            'rerank_scores': [h.get('rerank_score') for h in hits[:k]]})
            for variant in variants.values():
                variant['median_latency_ms'] = statistics.median(variant.pop('latencies_ms'))
                by_k = {k: grouped_metrics(rows) for k, rows in variant['results'].items()}
                variant['metrics'] = {split: {k: metrics[split] for k, metrics in by_k.items()}
                                      for split in by_k['3']}
            report['modes'][mode] = {'variants': variants, 'pool_metrics': grouped_metrics(pool_results),
                                     'pool_results': pool_results}
            print(mode, json.dumps({name: {'at_3': v['metrics']['all']['3'],
                                           'median_latency_ms': round(v['median_latency_ms'], 2)}
                                    for name, v in variants.items()}, ensure_ascii=False), flush=True)
        report['embedding_runtime'] = service.embedder.runtime if service.embedder else {'loaded': False, 'device': 'CPU'}
        report['reranker_runtime'] = service.reranker.runtime
    for path, body in ((args.output, json.dumps(report, ensure_ascii=False, indent=2)),
                       (args.markdown, markdown(report))):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding='utf-8')
        print(f'Report: {path.resolve()}')


if __name__ == '__main__':
    main()
