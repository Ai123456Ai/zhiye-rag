"""Run retrieval-only evaluation on a separate temporary database."""
import argparse
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.service import RagService  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=['bm25', 'vector', 'hybrid'], default='bm25')
    parser.add_argument('--top-k', type=int, default=5, choices=range(1, 11))
    parser.add_argument('--output', default='reports/retrieval.json')
    args = parser.parse_args()
    env = Settings.from_env()
    cases = json.loads((ROOT / 'evals/questions.json').read_text(encoding='utf-8'))
    with tempfile.TemporaryDirectory(prefix='rag-eval-') as tmp:
        service = RagService(Settings(db_path=Path(tmp) / 'eval.db', embedding_model=env.embedding_model, embedding_device=env.embedding_device))
        sample = ROOT / 'examples/星河科技员工手册.md'
        service.ingest(sample.name, sample.read_bytes())
        results = []
        for case in cases:
            start = time.perf_counter()
            hits = service.search(case['question'], args.top_k, args.mode)
            elapsed = (time.perf_counter() - start) * 1000
            rank = next((i for i, h in enumerate(hits, 1) if case['evidence_contains'] in h['text']), None)
            results.append({**case, 'rank': rank, 'latency_ms': round(elapsed, 2)})
    report = {
        'dataset': '10 synthetic questions on one fictional handbook; not a production benchmark',
        'mode': args.mode, 'top_k': args.top_k, 'questions': len(results),
        'evidence_hit_rate': sum(r['rank'] is not None for r in results) / len(results),
        'mrr_at_k': sum(1 / r['rank'] if r['rank'] else 0 for r in results) / len(results),
        'median_latency_ms': round(statistics.median(r['latency_ms'] for r in results), 2),
        'results': results,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in report.items() if k != 'results'}, ensure_ascii=False, indent=2))
    print(f'Report: {output.resolve()}')


if __name__ == '__main__':
    main()
