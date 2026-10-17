import json
from copy import deepcopy

import pytest

from app.answer_evaluation import inspect_answer, load_answer_cases, summarize_answers
from app.config import ROOT
from app.evaluation import load_benchmark


def response(answer, usage=None):
    return {'answer': answer, 'sources': [{'citation': 1}], 'usage': usage}


def test_citation_syntax_is_not_a_semantic_verdict():
    checked = inspect_answer(response('婚假99天 [1]，或者100天 [9]。'))
    assert checked['invalid_citation_ids'] == [9]
    assert checked['has_citation'] is True
    assert checked['semantic_verdict'] == 'pending_review'
    assert inspect_answer(response('错误结论 [1]'))['citation_ids_valid'] is True


def test_refusal_marker_does_not_prove_absence_of_fabrication():
    checked = inspect_answer(response('资料不足，无法确定。但应该能补贴每月500元。'))
    assert checked['refusal_marker_detected'] is True
    assert checked['semantic_verdict'] == 'pending_review'
    assert inspect_answer(response('没有检索到相关资料。'))['refusal_marker_detected'] is True
    empty = inspect_answer(response('没有资料里的引用编号'))
    assert empty['citation_ids_valid'] is True and empty['has_citation'] is False


def test_usage_and_negative_denominators_exclude_request_errors():
    negative_response = response('资料未提供。', {'prompt_tokens': 12, 'completion_tokens': 3, 'total_tokens': 15})
    positive_response = response('4小时内首次响应 [1]')
    rows = [{'case': {'category': 'unanswerable'}, 'response': negative_response,
             'checks': inspect_answer(negative_response)},
            {'case': {'category': 'scope'}, 'response': positive_response,
             'checks': inspect_answer(positive_response)},
            {'case': {'category': 'unanswerable'}, 'error': 'timeout'}]
    summary = summarize_answers(rows)
    assert summary['completed_count'] == 2 and summary['error_count'] == 1
    assert summary['unanswerable_count'] == 1
    assert summary['refusal_marker_rate_unanswerable'] == 1
    assert summary['citation_presence_rate_answerable'] == 1
    assert summary['usage']['total_tokens'] == 15
    assert summary['usage_reported_count'] == 1
    assert summary['semantic_accuracy'] is None and summary['faithfulness'] is None
    assert summarize_answers([])['refusal_marker_rate_unanswerable'] is None


def test_answer_dataset_references_known_cases_with_no_missing_labels():
    benchmark, _, _ = load_benchmark(ROOT / 'evals/benchmark.json', ROOT / 'evals/corpus')
    cases, version = load_answer_cases(ROOT / 'evals/answers.json', benchmark)
    assert version == 'answer-v1'
    assert len(cases) == 28
    assert sum(c['category'] == 'unanswerable' for c in cases) == 12
    assert sum(c['split'] == 'dev' for c in cases) == 10


@pytest.mark.parametrize('invalid', ['duplicate', 'unknown', 'missing_reference'])
def test_invalid_answer_annotations_fail_before_spending_tokens(tmp_path, invalid):
    annotation = {'id': 'H01', 'reference_answer': '参考答案'}
    rows = [annotation]
    if invalid == 'duplicate':
        rows.append(annotation)
    elif invalid == 'unknown':
        annotation['id'] = 'nonexistent'
    else:
        annotation['reference_answer'] = ''
    path = tmp_path / 'answers.json'
    path.write_text(json.dumps({'version': 'test', 'cases': rows}), encoding='utf-8')
    with pytest.raises(ValueError):
        load_answer_cases(path, [{'id': 'H01'}])


def test_checkpoint_resume_skips_successful_paid_calls(tmp_path, monkeypatch):
    from scripts import evaluate_answers
    from app.config import Settings
    from app.service import ModelError

    output = tmp_path / 'run.json'
    questions = []

    class FakeService:
        embedder = None
        fail_second = True

        def __init__(self, settings):
            assert settings.db_path != ROOT / 'data/knowledge.db'

        def ingest(self, name, content):
            pass

        def answer(self, question, top_k, mode, rerank=False):
            questions.append(question)
            if len(questions) == 2 and self.fail_second:
                raise ModelError('do not leak underlying details')
            return {'answer': '资料不足，无法确定。', 'answer_mode': 'llm',
                    'sources': [], 'latency_ms': 1, 'warnings': [],
                    'usage': {'prompt_tokens': 2, 'completion_tokens': 3, 'total_tokens': 5}}

    monkeypatch.setattr(evaluate_answers, 'RagService', FakeService)
    monkeypatch.setattr(Settings, 'from_env', classmethod(lambda cls: Settings(
        db_path=ROOT / 'data/knowledge.db', llm_base_url='https://fake.invalid', llm_model='fake')))
    argv = ['evaluate_answers.py', '--mode', 'bm25', '--limit', '2', '--output', str(output)]
    monkeypatch.setattr('sys.argv', argv)
    with pytest.raises(SystemExit, match='first API failure'):
        evaluate_answers.main()
    checkpoint = json.loads(output.read_text(encoding='utf-8'))
    assert checkpoint['summary']['completed_count'] == 1
    assert checkpoint['summary']['error_count'] == 1
    FakeService.fail_second = False
    monkeypatch.setattr('sys.argv', argv + ['--resume'])
    evaluate_answers.main()
    assert len(questions) == 3 and questions[1] == questions[2]
    completed = json.loads(output.read_text(encoding='utf-8'))
    assert completed['summary']['completed_count'] == 2
    assert completed['summary']['usage']['total_tokens'] == 10
    assert len(completed['failed_attempts']) == 1
    evaluate_answers.main()
    assert len(questions) == 3  # Re-running a completed checkpoint spends no tokens.


def reviewed_fixture():
    from app.answer_review import response_hash
    actual = response('响应4小时 [1]')
    actual['sources'][0]['text'] = '4个工作小时内首次响应，不承诺修复。'
    report = {'planned_count': 1, 'fingerprint': 'run', 'results': [
        {'case': {'id': 'H', 'category': 'scope'}, 'response': actual}]}
    reviews = {'run_fingerprint': 'run', 'reviewer': 'test', 'cases': [
        {'id': 'H', 'response_sha256': response_hash(actual), 'answer_coverage': 'full',
         'unsupported_claims_found': False, 'citation_support': 'supported', 'correct_abstention': None,
         'notes': '对照首次响应与修复区别。', 'support': [{'citation': 1, 'quote': '不承诺修复'}]}]}
    return report, reviews


def test_review_hash_binds_answer_and_actual_context():
    from app.answer_review import summarize_reviews, validate_reviews
    report, reviews = reviewed_fixture()
    assert summarize_reviews(report, reviews)['full_answer_count'] == 1
    changed_answer = deepcopy(report)
    changed_answer['results'][0]['response']['answer'] = '保证4小时修好 [1]'
    with pytest.raises(ValueError, match='已变化'):
        validate_reviews(changed_answer, reviews)
    changed_context = deepcopy(report)
    changed_context['results'][0]['response']['sources'][0]['text'] = '更换后的文档'
    with pytest.raises(ValueError, match='已变化'):
        validate_reviews(changed_context, reviews)


@pytest.mark.parametrize('invalid', ['wrong_run', 'unknown_quote', 'wrong_citation', 'duplicate', 'missing'])
def test_invalid_review_provenance_is_rejected(invalid):
    from app.answer_review import validate_reviews
    report, reviews = reviewed_fixture()
    if invalid == 'wrong_run':
        reviews['run_fingerprint'] = 'other'
    elif invalid == 'unknown_quote':
        reviews['cases'][0]['support'][0]['quote'] = '保证4小时修好'
    elif invalid == 'wrong_citation':
        reviews['cases'][0]['support'][0]['citation'] = 99
    elif invalid == 'duplicate':
        reviews['cases'].append(deepcopy(reviews['cases'][0]))
    else:
        reviews['cases'] = []
    with pytest.raises(ValueError):
        validate_reviews(report, reviews)


def test_saved_semantic_reviews_match_the_saved_actual_answers():
    from app.answer_review import summarize_reviews
    report = json.loads((ROOT / 'docs/answer-run.json').read_text(encoding='utf-8'))
    reviews = json.loads((ROOT / 'docs/answer-review.json').read_text(encoding='utf-8'))
    summary = summarize_reviews(report, reviews)
    assert summary['reviewed_count'] == 28
    assert summary['full_answer_count'] == 15 and summary['partial_answer_count'] == 1
    assert summary['correct_abstention_count'] == 12
