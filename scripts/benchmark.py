"""Benchmark the actual retrieval service against a versioned evidence-labeled corpus."""
import argparse
import importlib.metadata
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


def percent(value):
    return '—' if value is None else f'{value:.1%}'


def markdown_report(report):
    data = report['dataset']
    lines = [
        '# 扩展检索评测基线', '',
        f"生成时间：{report['created_at']}。数据版本：`{data['version']}`。",
        f"{data['document_count']} 份评测资料，{data['chunk_count']} 个片段，"
        f"完整题集 {data['total_case_count']} 题，本次评测 {report['evaluated_case_count']} 题。",
        '', '## 方法与口径', '',
        '- 调用真实 `RagService.search`，三种模式共用临时索引；生成接口配置为空，不调用付费 API，不修改应用知识库。',
        '- 证据必须同时匹配文件名和标注原文；全部证据命中率要求该题的每条证据都召回。证据召回率是逐题覆盖比例的平均值。',
        '- MRR 取第一条相关证据的倒数排名，不能证明多证据问题已经获得完整依据。无答案题不进入上述三个指标的分母。',
        '- 无答案题召回率统计这类问题仍返回片段的比例；返回相关条款可能帮助模型拒答，因此此指标不等于回答错误率或拒答失败率。',
        '- dev 用于后续调试，test 作为当前基线；此次没有根据结果调整算法或阈值。题目由本项目手工编写，两组共享资料，均不是外部盲测。',
        '- 默认一次取前 5 条，再计算 K=1/3/5；这三个 K 的 RRF 候选数均为 20，与直接按对应 K 检索一致。',
        '', '## 运行条件', '',
        f"- 模型：`{report['embedding_model'] or '未启用'}`；设备：`{report['runtime']['device']}`。",
        f"- 环境：Python {report['environment']['python']}；依赖：`{json.dumps(report['environment']['packages'])}`。",
        '- 700 字符分块，100 字符重叠；BM25 保留正分，向量余弦阈值 >0.25，RRF 常数 60，未做阈值校准。',
        '- 排序分数不是答案可信度；并列时沿用服务的随机片段 ID 排序，重建索引后个别并列排名可能变化。',
        '- 预热与模型加载不计入逐题耗时。此次没有修改线上检索算法。',
        f"- 题集 SHA-256：`{data['dataset_sha256']}`。",
        f"- 资料 SHA-256：`{data['corpus_sha256']}`（按文件名排序，拼接文件名、NUL 与内容 SHA-256 后计算）。", '',
    ]
    if provenance := data.get('provenance'):
        lines.extend(['## 公开资料来源', '',
                      f"仓库：{provenance['repository']}；版本：`{provenance['revision']}`；许可：{provenance['license']}。",
                      '文档原文及逐文件哈希见题集旁的 sources.json。未展开外部代码包含指令或图片。', ''])
    splits = [split for split in ('dev', 'test') if any(
        split in result['metrics'] for mode in report['modes'].values() for result in mode['at_k'].values())]
    for split in splits:
        lines.extend([f'## {split} 结果', '',
                      '| 模式 | K | 有答案 / 无答案题数 | 全部证据命中率 | 平均证据召回率 | MRR@K | 无答案题召回率 |',
                      '| --- | --- | --- | --- | --- | --- | --- |'])
        for mode, result in report['modes'].items():
            for k, measured in result['at_k'].items():
                m = measured['metrics'][split]
                mrr = '—' if m['mrr'] is None else f"{m['mrr']:.3f}"
                lines.append(f"| {mode} | {k} | {m['answerable_count']} / {m['unanswerable_count']} | "
                             f"{percent(m['full_evidence_hit_rate'])} | {percent(m['mean_evidence_recall'])} | "
                             f"{mrr} | {percent(m['unanswerable_retrieval_rate'])} |")
        lines.append('')
    selected_k = '3' if '3' in next(iter(report['modes'].values()))['at_k'] else str(report['top_k'][-1])
    if 'test' in splits:
        lines.extend([f'## test 分组（K={selected_k}）', '',
                      '| 模式 | 类别 | 题数 | 全部证据命中率 |', '| --- | --- | --- | --- |'])
        for mode, result in report['modes'].items():
            for category, m in result['at_k'][selected_k]['metrics']['test']['by_category'].items():
                if category != 'unanswerable':
                    lines.append(f"| {mode} | {category} | {m['case_count']} | {percent(m['full_evidence_hit_rate'])} |")
        lines.append('')
    lines.extend(['## 耗时', '',
                  f"同机逐题串行检索，预热后取前 {max(report['top_k'])} 条的耗时；包括 SQLite 读取、BM25 计算，以及适用时的查询编码与向量计算。",
                  '每题每模式测量一次，未测并发和尾延迟，不能作为服务 SLA。', '',
                  '| 模式 | 中位数（ms） |', '| --- | --- |'])
    for mode, result in report['modes'].items():
        lines.append(f"| {mode} | {result['median_latency_ms']:.2f} |")
    lines.extend(['', f'## 有答案题漏召回明细（K={selected_k}）', '',
                  '以下列出未取齐全部证据的题目；完整命中与逐题来源见 JSON 报告。', ''])
    for mode, result in report['modes'].items():
        failures = [r for r in result['at_k'][selected_k]['results'] if r['full_hit'] is False]
        lines.append(f'### {mode}：{len(failures)} 题')
        lines.append('')
        if not failures:
            lines.extend(['本次没有有答案题漏召回。', ''])
        for r in failures:
            missing = '；'.join(f"{u['document']}：{u['contains']}" for u in r['evidence'] if u['rank'] is None)
            sources = '、'.join(s['document_name'] for s in r['sources']) or '无'
            lines.append(f"- `{r['id']}`（{r['split']}，{r['category']}）{r['question']} 缺少：{missing}。召回：{sources}。")
        lines.append('')
    lines.extend([
        '## 结论与后续验证', '',
        '这份报告衡量检索证据的覆盖情况，没有测量生成回答准确率、忠实度、拒答能力或 Token 费用。',
        '资料规模有限，标签可能漏标等价证据；不能把这里的百分比直接写成真实企业问答准确率。',
        '后续先在 dev 上分析漏召回：同义问题可比较查询改写与重排序，多证据问题可比较问题拆解和分路检索。重排序只能调整已召回候选的顺序，不能补回候选之外的证据。',
        '无答案题应另做“是否有充分依据作答”的回答评测，避免仅提高向量阈值就声称解决幻觉。测试结果已在本报告展示；后续反复依据 test 调参会污染基线，应另增未参与调试的新题。',
        '', '## 复现', '', '```powershell',
        r'.\.venv\Scripts\python.exe scripts/benchmark.py --split all --top-k 1 3 5'
        + (f" --dataset {report['dataset_path']} --corpus {report['corpus_path']}" if 'dataset_path' in report else ''),
        '```', '',
        '默认输出 `reports/benchmark.json` 和 `reports/benchmark.md`；本文件是一次实测结果的留档。可用 `--modes bm25` 单独运行无需语义模型的关键词评测。', '',
    ])
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--split', choices=('dev', 'test', 'all'), default='all')
    parser.add_argument('--top-k', type=int, nargs='+', choices=range(1, 6), default=[1, 3, 5])
    parser.add_argument('--modes', nargs='+', choices=('bm25', 'vector', 'hybrid'), default=['bm25', 'vector', 'hybrid'])
    parser.add_argument('--dataset', type=Path, default=ROOT / 'evals/benchmark.json')
    parser.add_argument('--corpus', type=Path, default=ROOT / 'evals/corpus')
    parser.add_argument('--output', type=Path, default=ROOT / 'reports/benchmark.json')
    parser.add_argument('--markdown', type=Path, default=ROOT / 'reports/benchmark.md')
    args = parser.parse_args()
    cases, documents, metadata = load_benchmark(args.dataset, args.corpus)
    cases = [case for case in cases if args.split == 'all' or case['split'] == args.split]
    if not cases:
        parser.error('选定 split 没有问题。')
    configured = Settings.from_env()
    semantic = any(mode != 'bm25' for mode in args.modes)
    if semantic and not configured.embedding_model:
        parser.error('向量与混合检索需要配置 EMBEDDING_MODEL，或仅使用 --modes bm25。')
    modes = list(dict.fromkeys(args.modes))
    top_ks = sorted(set(args.top_k))
    packages = {}
    for package in ('sentence-transformers', 'torch', 'transformers'):
        try:
            packages[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            packages[package] = 'not installed'
    report = {
        'created_at': datetime.now(timezone.utc).isoformat(), 'dataset': metadata,
        'evaluated_case_count': len(cases), 'split': args.split, 'top_k': top_ks,
        'dataset_path': args.dataset.as_posix(), 'corpus_path': args.corpus.as_posix(),
        'embedding_model': configured.embedding_model if semantic else '',
        'environment': {'python': platform.python_version(), 'packages': packages},
        'generation_calls': 0, 'live_database_used': False,
        'chunking': {'size_characters': 700, 'overlap_characters': 100},
        'filters': {'bm25': 'score > 0', 'vector': 'cosine > 0.25', 'rrf_constant': 60},
        'modes': {},
    }
    with tempfile.TemporaryDirectory(prefix='rag-benchmark-') as tmp:
        # Deliberately copy no LLM settings and never construct the live Store.
        settings = Settings(db_path=Path(tmp) / 'eval.db',
                            embedding_model=report['embedding_model'],
                            embedding_device=configured.embedding_device)
        service = RagService(settings)
        for name, body in documents.items():
            service.ingest(name, body)
        for mode in modes:
            service.search(cases[0]['question'], max(top_ks), mode)
            results = {str(k): [] for k in top_ks}
            latencies = []
            for case in cases:
                started = time.perf_counter()
                hits = service.search(case['question'], max(top_ks), mode)
                elapsed = (time.perf_counter() - started) * 1000
                latencies.append(elapsed)
                for k in top_ks:
                    results[str(k)].append({**score_case(case, hits, k), 'search_latency_ms': round(elapsed, 3)})
            report['modes'][mode] = {
                'median_latency_ms': round(statistics.median(latencies), 3),
                'at_k': {k: {'metrics': grouped_metrics(rows), 'results': rows} for k, rows in results.items()},
            }
            print(mode, json.dumps({k: {split: {key: value for key, value in metrics.items() if key != 'by_category'}
                                       for split, metrics in measured['metrics'].items()}
                                    for k, measured in report['modes'][mode]['at_k'].items()},
                                   ensure_ascii=False), flush=True)
        report['runtime'] = service.embedder.runtime if service.embedder else {'loaded': False, 'device': 'CPU (BM25 only)'}
    for output, content in ((args.output, json.dumps(report, ensure_ascii=False, indent=2)),
                            (args.markdown, markdown_report(report))):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(content, encoding='utf-8')
        print(f'Report: {output.resolve()}')


if __name__ == '__main__':
    main()
