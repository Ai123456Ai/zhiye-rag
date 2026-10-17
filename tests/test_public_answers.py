import json
from copy import deepcopy

import pytest

from app.answer_evaluation import load_answer_suite
from app.config import ROOT, Settings
from app.evaluation import load_benchmark


def public_suite():
    cases, documents, _ = load_benchmark(ROOT / 'evals/public-fastapi/benchmark.json',
                                        ROOT / 'evals/public-fastapi/corpus')
    return load_answer_suite(ROOT / 'evals/public-fastapi/answers.json', cases, documents)


def test_public_answer_suite_checks_all_public_and_synthetic_evidence():
    cases, version = public_suite()
    assert version == 'public-answer-v1' and len(cases) == 20
    assert sum(c['evaluation_group'] == 'missing' for c in cases) == 6
    assert sum(bool(c.get('fixture_documents')) for c in cases) == 3
    assert next(c for c in cases if c['id'] == 'WP01')['category'] == 'scope'


@pytest.mark.parametrize('invalid', ['unknown_reference', 'duplicate', 'bad_evidence', 'bad_metadata'])
def test_invalid_public_suite_fails_before_model_calls(tmp_path, invalid):
    cases, documents, _ = load_benchmark(ROOT / 'evals/public-fastapi/benchmark.json',
                                        ROOT / 'evals/public-fastapi/corpus')
    suite = json.loads((ROOT / 'evals/public-fastapi/answers.json').read_text(encoding='utf-8'))
    if invalid == 'unknown_reference':
        suite['cases'][0]['benchmark_id'] = 'nonexistent'
    elif invalid == 'duplicate':
        suite['cases'].append(deepcopy(suite['cases'][0]))
    elif invalid == 'bad_evidence':
        suite['cases'][-1]['evidence'][0]['contains'] = 'not in fixture'
    else:
        suite['cases'][-1]['fixture_documents'][0]['metadata']['effective_from'] = 'not a date'
    path = tmp_path / 'suite.json'
    path.write_text(json.dumps(suite), encoding='utf-8')
    with pytest.raises(ValueError):
        load_answer_suite(path, cases, documents)


def test_public_runner_isolates_fixtures_skips_local_conflict_and_resumes(tmp_path, monkeypatch):
    from scripts import evaluate_answers

    calls = []

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {'choices': [{'message': {'content': '资料不足，无法确定。'}}],
                    'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}}

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, url, *, headers, json):
            calls.append(json['messages'])
            return FakeResponse()

    monkeypatch.setattr('app.service.httpx.Client', FakeClient)
    monkeypatch.setattr(Settings, 'from_env', classmethod(lambda cls: Settings(
        db_path=tmp_path / 'DO-NOT-CREATE.db', llm_base_url='https://fake.invalid', llm_model='fake')))
    output = tmp_path / 'run.json'
    argv = ['evaluate_answers.py', '--mode', 'bm25', '--suite',
            '--dataset', str(ROOT / 'evals/public-fastapi/benchmark.json'),
            '--corpus', str(ROOT / 'evals/public-fastapi/corpus'),
            '--annotations', str(ROOT / 'evals/public-fastapi/answers.json'), '--output', str(output)]
    monkeypatch.setattr('sys.argv', argv)
    evaluate_answers.main()
    report = json.loads(output.read_text(encoding='utf-8'))
    assert len(calls) == 19
    assert report['summary']['answer_modes'] == {'document_conflict': 1, 'llm': 19}
    by_id = {r['case']['id']: r for r in report['results']}
    for name in ['CF01', 'CF02', 'CF03']:
        assert all(s['document_name'].startswith('虚构') for s in by_id[name]['response']['sources'])
    assert by_id['CF03']['response']['usage'] is None
    assert len(by_id['CF03']['response']['conflicts'][0]['documents']) == 2
    assert not (tmp_path / 'DO-NOT-CREATE.db').exists()
    assert all('应否定错误前提' not in messages[1]['content'] for messages in calls)
    monkeypatch.setattr('sys.argv', argv + ['--resume'])
    evaluate_answers.main()
    assert len(calls) == 19


def test_saved_public_reviews_bind_actual_answers_and_render_grouped_findings():
    from scripts.report_public_answers import render_report

    run = json.loads((ROOT / 'docs/public-answer-run.json').read_text(encoding='utf-8'))
    reviews = json.loads((ROOT / 'docs/public-answer-review.json').read_text(encoding='utf-8'))
    text, summary = render_report(run, reviews)
    assert summary['reviewed_count'] == 20
    assert summary['correct_abstention_count'] == 6
    assert summary['partial_answer_count'] == 3
    assert summary['answers_with_unsupported_claims_found'] == 1
    assert summary['citation_support_issue_count'] == 1
    assert '| 公开文档错误前提 | 3 | 1/3 |' in text
    assert '| 虚构版本歧义 | 1 | 1/1 |' in text
