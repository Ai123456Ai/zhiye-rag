"""Compare all retrieval modes over the same index and questions."""
import argparse
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.service import RagService


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--top-k', type=int, choices=range(1, 11), default=1)
    parser.add_argument('--output', default='reports/comparison.json')
    args = parser.parse_args()
    settings = Settings.from_env()
    if not settings.embedding_model:
        raise ValueError('Configure EMBEDDING_MODEL first.')
    cases = []
    for filename, category in [('questions.json', 'literal'), ('paraphrases.json', 'paraphrase')]:
        cases.extend({**case, 'category': category} for case in json.loads((ROOT / 'evals' / filename).read_text(encoding='utf-8')))
    with tempfile.TemporaryDirectory(prefix='rag-compare-') as tmp:
        service = RagService(Settings(db_path=Path(tmp) / 'eval.db', embedding_model=settings.embedding_model,
                                      embedding_device=settings.embedding_device))
        path = ROOT / 'examples/星河科技员工手册.md'
        service.ingest(path.name, path.read_bytes())
        report = {
            'dataset': '20 synthetic questions (10 literal, 10 paraphrases), one fictional document; smoke benchmark only',
            'chunk_count': len(service.store.chunks()), 'top_k': args.top_k,
            'embedding_model': settings.embedding_model, 'runtime': service.embedder.runtime,
            'notes': 'Evidence substring matching; warm-up excluded; retrieval scores are not answer confidence.',
            'modes': {},
        }
        for mode in ('bm25', 'vector', 'hybrid'):
            service.search(cases[0]['question'], args.top_k, mode)
            results = []
            for case in cases:
                start = time.perf_counter()
                hits = service.search(case['question'], args.top_k, mode)
                elapsed = (time.perf_counter() - start) * 1000
                rank = next((i for i, h in enumerate(hits, 1) if case['evidence_contains'] in h['text']), None)
                results.append({**case, 'rank': rank, 'latency_ms': round(elapsed, 2)})
            metrics = {
                'evidence_hit_rate': sum(r['rank'] is not None for r in results) / len(results),
                'mrr_at_k': sum(1/r['rank'] if r['rank'] else 0 for r in results) / len(results),
                'median_latency_ms': round(statistics.median(r['latency_ms'] for r in results), 2),
                'hit_rate_by_category': {
                    category: sum(r['rank'] is not None for r in results if r['category'] == category) / 10
                    for category in ('literal', 'paraphrase')
                },
            }
            report['modes'][mode] = {**metrics, 'results': results}
            print(mode, json.dumps(metrics, ensure_ascii=False), flush=True)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'Report: {output.resolve()}')


if __name__ == '__main__':
    main()
