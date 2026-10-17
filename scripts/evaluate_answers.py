"""Evaluate actual generated answers with checkpoints; this spends model API tokens."""
import argparse
import hashlib
import json
import os
import sys
import tempfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.answer_evaluation import answer_markdown, inspect_answer, load_answer_cases, load_answer_suite, summarize_answers
from app.config import Settings
from app.evaluation import load_benchmark, score_case
from app.service import ModelError, RagService


def save(report, output):
    report['summary'] = summarize_answers(report['results'])
    output.parent.mkdir(parents=True, exist_ok=True)
    pending = output.with_suffix('.json.tmp')
    pending.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(pending, output)
    output.with_suffix('.md').write_text(answer_markdown(report), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('bm25', 'vector', 'hybrid'), default='hybrid')
    parser.add_argument('--top-k', type=int, choices=range(1, 11), default=3)
    parser.add_argument('--rerank', action='store_true', help='Opt in to the configured local reranker.')
    parser.add_argument('--decompose', action='store_true', help='Split explicit compound clauses locally and interleave evidence.')
    parser.add_argument('--split', choices=('dev', 'test', 'all'), default='all')
    parser.add_argument('--limit', type=int, default=28)
    parser.add_argument('--output', type=Path, default=ROOT / 'reports/answers.json')
    parser.add_argument('--resume', action='store_true', help='Reuse successful checkpoints; only retry failed/pending cases.')
    parser.add_argument('--dry-run', action='store_true', help='Validate cases and print counts; no model loading or API requests.')
    parser.add_argument('--dataset', type=Path, default=ROOT / 'evals/benchmark.json')
    parser.add_argument('--corpus', type=Path, default=ROOT / 'evals/corpus')
    parser.add_argument('--annotations', type=Path, default=ROOT / 'evals/answers.json')
    parser.add_argument('--suite', action='store_true', help='Use mixed public questions and isolated synthetic conflict fixtures.')
    args = parser.parse_args()
    if args.limit < 1:
        parser.error('--limit 必须大于零。')
    benchmark_cases, documents, metadata = load_benchmark(args.dataset, args.corpus)
    annotations_path = args.annotations
    cases, version = (load_answer_suite(annotations_path, benchmark_cases, documents) if args.suite
                      else load_answer_cases(annotations_path, benchmark_cases))
    cases = [case for case in cases if args.split == 'all' or case['split'] == args.split][:args.limit]
    if not cases:
        parser.error('没有选中的题目。')
    if args.dry_run:
        print(json.dumps({'case_count': len(cases), 'case_ids': [c['id'] for c in cases],
                          'unanswerable_count': sum(c['category'] == 'unanswerable' for c in cases),
                          'groups': {group: sum(c.get('evaluation_group', 'legacy') == group for c in cases)
                                     for group in sorted({c.get('evaluation_group', 'legacy') for c in cases})},
                          'api_calls': 0}, ensure_ascii=False))
        return
    configured = Settings.from_env()
    if not configured.generation_enabled:
        parser.error('请先配置 LLM_BASE_URL 和 LLM_MODEL。')
    if args.mode != 'bm25' and not configured.embedding_model:
        parser.error('向量与混合检索需要 EMBEDDING_MODEL。')
    if args.rerank and not configured.reranker_model:
        parser.error('--rerank 需要先配置 RERANKER_MODEL。')
    manifest = {
        'dataset': metadata, 'answer_version': version, 'suite': args.suite,
        'annotation_sha256': hashlib.sha256(annotations_path.read_bytes()).hexdigest(),
        'code_sha256': {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                        for name in ('app/service.py', 'app/retrieval.py', 'app/documents.py',
                                     'app/document_policy.py', 'app/store.py', 'app/query_planning.py',
                                     'app/answer_evaluation.py', 'app/evaluation.py', 'app/config.py',
                                     'app/reranking.py', 'scripts/evaluate_answers.py')},
        'model': configured.llm_model, 'mode': args.mode, 'top_k': args.top_k,
        'embedding_model': configured.embedding_model if args.mode != 'bm25' else '',
        'embedding_device': configured.embedding_device,
        'rerank': args.rerank, 'decompose': args.decompose,
        'reranker_model': configured.reranker_model if args.rerank else '',
        'reranker_device': configured.reranker_device, 'reranker_candidates': configured.reranker_candidates,
        'endpoint_sha256': hashlib.sha256(configured.llm_base_url.encode()).hexdigest(),
        'case_ids': [c['id'] for c in cases],
    }
    fingerprint = hashlib.sha256(json.dumps(manifest, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    if args.output.exists():
        if not args.resume:
            parser.error('输出文件已存在；用 --resume 继续，或指定新的 --output，避免重复计费。')
        report = json.loads(args.output.read_text(encoding='utf-8'))
        if report['fingerprint'] != fingerprint:
            parser.error('配置、代码或数据已变化，不能混用旧运行记录；请指定新输出文件。')
    else:
        report = {'created_at': datetime.now(timezone.utc).isoformat(), 'fingerprint': fingerprint,
                  'manifest': manifest, 'model': configured.llm_model, 'mode': args.mode,
                  'top_k': args.top_k, 'rerank': args.rerank, 'decompose': args.decompose,
                  'planned_count': len(cases), 'live_database_used': False,
                  'results': [], 'failed_attempts': []}
    completed = {row['case']['id'] for row in report['results'] if row.get('response')}
    if len(completed) == len(cases):
        print('All cases already complete; no API requests made.')
        return
    save(report, args.output)
    with tempfile.TemporaryDirectory(prefix='rag-answer-eval-') as tmp:
        settings = replace(configured, db_path=Path(tmp) / 'eval.db',
                           embedding_model=configured.embedding_model if args.mode != 'bm25' else '')
        service = RagService(settings)
        for name, body in documents.items():
            service.ingest(name, body)
        for index, case in enumerate(cases, 1):
            if case['id'] in completed:
                continue
            row = {'case': case}
            try:
                current = service
                if fixtures := case.get('fixture_documents'):
                    current = RagService(replace(settings, db_path=Path(tmp) / f'scenario-{index}.db'))
                    # Reuse loaded local models; each scenario still has an isolated database.
                    current.embedder, current.reranker = service.embedder, service.reranker
                    for fixture in fixtures:
                        current.ingest(fixture['name'], fixture['text'].encode('utf-8'), fixture.get('metadata'))
                options = {'rerank': args.rerank}
                if args.decompose:
                    options['decompose'] = True
                response = current.answer(case['question'], args.top_k, args.mode, **options)
                if response['answer_mode'] == 'excerpts':
                    raise ValueError('评测不能把原文摘录当成模型回答。')
                row.update(response=response, checks=inspect_answer(response),
                           retrieval=score_case(case, response['sources'], args.top_k))
            except ModelError:
                row['error'] = '模型请求失败；未保存底层异常或密钥。此请求可能已计费，usage未知。'
            report['results'] = [old for old in report['results'] if old['case']['id'] != case['id']]
            report['results'].append(row)
            if row.get('error'):
                report['failed_attempts'].append({'id': case['id'], 'time': datetime.now(timezone.utc).isoformat(),
                                                  'usage_unknown': True})
            report['runtime'] = service.embedder.runtime if service.embedder else {'loaded': False, 'device': 'CPU'}
            save(report, args.output)
            print(f"{index}/{len(cases)} {case['id']}: {'ERROR' if row.get('error') else 'saved'}", flush=True)
            if row.get('error'):
                raise SystemExit('Stopped at first API failure. Check connection/configuration, then use --resume.')
    print(json.dumps(report['summary'], ensure_ascii=False), flush=True)
    print(f'Report: {args.output.resolve()}')


if __name__ == '__main__':
    main()
