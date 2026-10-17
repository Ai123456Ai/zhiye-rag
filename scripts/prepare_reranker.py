"""Download a pinned local cross encoder and verify real scoring on the chosen device."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.config import Settings  # Load the project's model cache settings.
from app.reranking import LocalReranker


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', default='BAAI/bge-reranker-base')
    parser.add_argument('--revision', default='main')
    parser.add_argument('--device', default='cuda')
    args = parser.parse_args()
    from huggingface_hub import HfApi, snapshot_download
    import torch
    if args.device.startswith('cuda') and not torch.cuda.is_available():
        raise RuntimeError('CUDA unavailable; preparation stopped.')
    info = HfApi().model_info(args.repo, revision=args.revision)
    if not any(file.rfilename.endswith('.safetensors') for file in info.siblings):
        raise ValueError('此准备脚本只下载 safetensors 权重，请选择提供此格式的模型。')
    revision = info.sha
    target = ROOT / 'data/models' / args.repo.split('/')[-1] / revision
    snapshot_download(args.repo, revision=revision, local_dir=target,
                      allow_patterns=['*.json', '*.txt', '*.safetensors', '*.model', 'README.md'], max_workers=2)
    reranker = LocalReranker(str(target), args.device)
    scores = reranker.score('北京出差酒店每晚最多报多少？', [
        '北京出差住宿上限为每人每晚500元。', '工作电脑故障需要通过IT工单系统报修。'])
    if scores[0] <= scores[1]:
        raise ValueError('模型未把相关资料排在无关资料前，请检查模型选择。')
    if args.device != 'auto' and not reranker.runtime['device'].startswith(args.device):
        raise ValueError('实际运行设备与请求设备不符。')
    metadata = {'repo': args.repo, 'revision': revision, 'path': target.as_posix(),
                'runtime': reranker.runtime, 'smoke_scores': scores,
                'gpu': torch.cuda.get_device_name(0) if args.device.startswith('cuda') else None}
    (target / 'preparation.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(metadata, ensure_ascii=False, indent=2))
    print(f'RERANKER_MODEL={target.as_posix()}')


if __name__ == '__main__':
    main()
