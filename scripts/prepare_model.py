"""Download a pinned snapshot and verify GPU encoding before enabling it."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.retrieval import LocalEmbedder


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--repo', default='BAAI/bge-small-zh-v1.5')
    parser.add_argument('--revision', default='main')
    parser.add_argument('--device', default='cuda')
    args = parser.parse_args()
    from huggingface_hub import HfApi, snapshot_download
    import torch

    if args.device == 'cuda' and not torch.cuda.is_available():
        raise RuntimeError('CUDA unavailable; GPU preparation stopped.')
    revision = HfApi().model_info(args.repo, revision=args.revision).sha
    target = ROOT / 'data/models' / args.repo.split('/')[-1] / revision
    snapshot_download(
        args.repo, revision=revision, local_dir=target,
        allow_patterns=['*.json', '*.txt', '*.safetensors', '1_Pooling/*', 'README.md'],
        max_workers=2,
    )
    embedder = LocalEmbedder(str(target), args.device)
    vectors = embedder.encode(['北京出差住宿上限为500元。', '工作电脑故障需提交IT工单。'])
    assert len(vectors) == 2 and len(vectors[0]) > 0
    assert all(abs(sum(v*v for v in row) - 1) < 0.001 for row in vectors)
    embedder.encode_query('住酒店能报多少钱？')
    assert embedder.runtime['device'].startswith(args.device)
    metadata = {'repo': args.repo, 'revision': revision, 'path': target.as_posix(),
                'dimensions': len(vectors[0]), 'runtime': embedder.runtime,
                'gpu': torch.cuda.get_device_name(0) if args.device == 'cuda' else None}
    (target / 'preparation.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(metadata, ensure_ascii=False, indent=2))
    print(f'EMBEDDING_MODEL={target.as_posix()}')


if __name__ == '__main__':
    main()
