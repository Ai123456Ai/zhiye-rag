"""Verify the vendored FastAPI corpus; optionally restore missing files from public sources."""
import argparse
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.evaluation import load_benchmark, verify_source_manifest

DATA = ROOT / 'evals/public-fastapi'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--download', action='store_true', help='Download missing files only; always enforce SHA-256.')
    args = parser.parse_args()
    manifest = json.loads((DATA / 'sources.json').read_text(encoding='utf-8'))
    pending = []
    for entry in manifest['files']:
        name = entry['file']
        if Path(name).name != name:
            parser.error(f'Invalid manifest filename: {name}')
        path = (DATA / 'corpus' if name.endswith('.md') else DATA) / name
        if path.exists():
            if hashlib.sha256(path.read_bytes()).hexdigest() != entry['sha256']:
                parser.error(f'Local file changed: {path}. Restore it explicitly before proceeding.')
            continue
        if not args.download:
            parser.error(f'Missing {name}. Run with --download to restore it.')
        request = urllib.request.Request(entry['download_url'], headers={'User-Agent': 'zhiye-rag-eval/1.0'})
        with urllib.request.urlopen(request, timeout=45) as response:
            body = response.read()
        if hashlib.sha256(body).hexdigest() != entry['sha256']:
            parser.error(f'Remote SHA-256 mismatch: {name}; no downloaded files were written.')
        pending.append((path, body))
    for path, body in pending:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    verify_source_manifest(DATA / 'sources.json', DATA / 'corpus')
    cases, _, metadata = load_benchmark(DATA / 'benchmark.json', DATA / 'corpus')
    print(f"Verified FastAPI {manifest['revision']}: {metadata['document_count']} documents, "
          f"{metadata['chunk_count']} chunks, {len(cases)} cases; license {manifest['license']}.")
    print('No generation API calls; no live database changes.')


if __name__ == '__main__':
    main()
