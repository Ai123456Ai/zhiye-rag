"""Verify basic mode in a temporary source copy using the current isolated Python."""
import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def run(arguments, cwd, env, timeout=120):
    result = subprocess.run([sys.executable, *arguments], cwd=cwd, env=env,
                            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout)
    if result.returncode:
        raise RuntimeError(f'{arguments[0]} failed:\n{result.stdout}\n{result.stderr}')
    return result.stdout.strip()


def start_server(cwd, env, port, log):
    process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1',
                                '--port', str(port)], cwd=cwd, env=env, stdout=log, stderr=log)
    deadline = time.monotonic() + 20
    with httpx.Client(base_url=f'http://127.0.0.1:{port}', trust_env=False, timeout=1) as client:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError('Isolated server exited; inspect the verification error log.')
            try:
                response = client.get('/api/health')
                response.raise_for_status()
                return process, response.json()
            except httpx.HTTPError:
                time.sleep(.1)
    stop_server(process)
    raise RuntimeError('Isolated server did not become healthy within 20 seconds.')


def stop_server(process):
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def smoke(cwd, env):
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    process = None
    checks = []
    with (cwd / 'server.log').open('w', encoding='utf-8') as log:
        try:
            process, health = start_server(cwd, env, port, log)
            assert health['generation_enabled'] is False
            assert health['retrieval_modes'] == ['bm25']
            assert health['embedding_model'] is None and not health['reranker_available']
            checks.append('basic_health_without_models_or_generation')
            with httpx.Client(base_url=f'http://127.0.0.1:{port}', trust_env=False, timeout=10) as client:
                for url in ['/', '/static/app.js', '/static/metadata.css', '/docs', '/openapi.json']:
                    client.get(url).raise_for_status()
                checks.append('page_assets_and_openapi')
                assert client.get('/api/documents').json() == []
                no_evidence = client.post('/api/chat', json={'question': '尚未导入资料'}).json()
                assert no_evidence['answer_mode'] == 'no_evidence'
                body = '北京住宿每晚报销上限500元。年假申请需要主管审批。'.encode('utf-8')
                response = client.post('/api/documents', files={'file': ('manual.md', body, 'text/markdown')})
                assert response.status_code == 201, response.text
                document_id = response.json()['document_id']
                duplicate = client.post('/api/documents', files={'file': ('copy.md', body, 'text/markdown')})
                assert duplicate.status_code == 201 and duplicate.json()['created'] is False
                request = {'question': '北京住宿每晚报销上限是多少？另外，年假申请需要谁审批？',
                           'mode': 'bm25', 'top_k': 3, 'decompose': True}
                retrieval = client.post('/api/search', json=request)
                retrieval.raise_for_status()
                assert retrieval.json()['query_plan']['applied'] and retrieval.json()['sources']
                answer = client.post('/api/chat', json=request)
                answer.raise_for_status()
                data = answer.json()
                assert data['answer_mode'] == 'excerpts' and data['usage'] is None
                assert '500' in data['answer'] and '主管审批' in data['answer']
                assert data['sources'] == retrieval.json()['sources']
                checks.extend(['empty_library', 'upload_and_deduplicate', 'bm25_decomposition', 'excerpt_answer'])
                stop_server(process)
                process = None
                process, _ = start_server(cwd, env, port, log)
                documents = client.get('/api/documents').json()
                assert [item['id'] for item in documents] == [document_id]
                persisted = client.post('/api/search', json=request)
                persisted.raise_for_status()
                assert persisted.json()['sources'] == retrieval.json()['sources']
                checks.append('restart_preserves_documents_and_retrieval')
                deleted = client.delete(f'/api/documents/{document_id}')
                deleted.raise_for_status()
                assert client.get('/api/documents').json() == []
                assert client.post('/api/search', json=request).json()['sources'] == []
                checks.append('delete_removes_chunks')
        finally:
            stop_server(process)
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'reports/reproduction.json')
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Output exists; select a new --output to preserve the previous run.')
    config = Path(sys.prefix) / 'pyvenv.cfg'
    if sys.prefix == sys.base_prefix or not config.is_file():
        parser.error('Run with a fresh virtual environment, not the global interpreter.')
    if 'include-system-site-packages = false' not in config.read_text(encoding='utf-8').lower():
        parser.error('The verification environment must not share global site-packages.')
    optional = {name: importlib.util.find_spec(name) is not None
                for name in ['torch', 'numpy', 'sentence_transformers']}
    if any(optional.values()):
        parser.error('Use a basic/dev environment without optional local-model dependencies.')
    lock_path = ROOT / 'requirements-repro-win-py312.txt'
    lock = {}
    for line in lock_path.read_text(encoding='utf-8').splitlines():
        if line.strip() and not line.startswith('#'):
            name, version = line.split('==', 1)
            lock[name] = version
    mismatches = {name: version for name, version in lock.items()
                  if importlib.metadata.version(name) != version}
    if mismatches:
        parser.error('Dependency versions differ from the lock; install requirements-repro-win-py312.txt first.')
    env = os.environ.copy()
    for key in list(env):
        if key.upper().startswith(('LLM_', 'EMBEDDING_', 'RERANKER_', 'PYTHON', 'RAG_', 'HF_', 'HUGGING',
                                   'OPENAI_', 'DEEPSEEK_')):
            env.pop(key)
    env.update(PYTHONUTF8='1', PYTHONNOUSERSITE='1', PYTEST_DISABLE_PLUGIN_AUTOLOAD='1')
    env.update({key: '' for key in ['LLM_BASE_URL', 'LLM_API_KEY', 'LLM_MODEL', 'EMBEDDING_MODEL', 'RERANKER_MODEL']})
    report = {'created_at': datetime.now(timezone.utc).isoformat(), 'python': platform.python_version(),
              'platform': platform.system(), 'machine': platform.machine(), 'system_site_packages': False,
              'optional_packages_present': optional, 'source_copy_excluded': ['.env', 'data', '.venv', '.runtime', '.git'],
              'dependency_lock': lock_path.name, 'dependency_lock_matches': True,
              'packages': {d.metadata['Name']: d.version for d in importlib.metadata.distributions()},
              'generation_calls': 0, 'live_database_used': False, 'files_sha256': {}}
    with tempfile.TemporaryDirectory(prefix='rag-reproduction-') as temporary:
        snapshot = Path(temporary) / 'project'
        snapshot.mkdir()
        for name in ['app', 'static', 'examples', 'evals', 'tests', 'scripts', 'docs']:
            shutil.copytree(ROOT / name, snapshot / name,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc', 'reproduction-run.json'))
        for pattern in ['requirements*.txt', 'README.md', '.env.example']:
            for path in ROOT.glob(pattern):
                shutil.copy2(path, snapshot / path.name)
        for path in sorted(snapshot.rglob('*')):
            if path.is_file():
                report['files_sha256'][path.relative_to(snapshot).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        env.update(RAG_DB_PATH=str(snapshot / 'data' / 'verification.db'), HF_HOME=str(snapshot / 'data' / 'models'))
        report['pip_check'] = run(['-m', 'pip', 'check'], snapshot, env)
        print(report['pip_check'], flush=True)
        report['pytest'] = run(['-m', 'pytest', '-q', '-rs', 'tests'], snapshot, env)
        print(report['pytest'], flush=True)
        report['smoke_checks'] = smoke(snapshot, env)
        print('HTTP smoke checks: ' + ', '.join(report['smoke_checks']), flush=True)
        run(['scripts/prepare_public_corpus.py'], snapshot, env)
        run(['scripts/benchmark.py', '--dataset', 'evals/public-fastapi/benchmark.json',
             '--corpus', 'evals/public-fastapi/corpus', '--split', 'dev', '--modes', 'bm25',
             '--output', 'reports/repro-bm25.json', '--markdown', 'reports/repro-bm25.md'], snapshot, env)
        benchmark = json.loads((snapshot / 'reports/repro-bm25.json').read_text(encoding='utf-8'))
        report['benchmark'] = benchmark
    report['status'] = 'passed'
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'Passed. Report: {args.output.resolve()}')


if __name__ == '__main__':
    main()
