from collections import Counter
from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import ROOT, Settings
from app.evaluation import load_benchmark
from app.main import create_app
from app.query_planning import interleave_rankings, plan_queries
from app.service import RagService


QUESTION = '北京住宿报销上限是多少？另外，年假申请需要谁审批？'


def test_split_keeps_original_and_preserves_quoted_code_and_urls():
    question = '请解释 `a;b?c` 与“甲；乙？”的含义？另外，URL https://example.org/?q=test 如何解析？'
    plan = plan_queries(question, True, 3)
    assert plan['applied']
    assert plan['queries'] == [question, '请解释 `a;b?c` 与“甲；乙？”的含义',
                               'URL https://example.org/?q=test 如何解析']


@pytest.mark.parametrize('question,top_k,reason', [
    ('住宿报销上限是多少？', 3, 'no_safe_boundary'),
    ('请描述住宿报销流程，而不是随意编造报销金额', 3, 'dependent_clause'),
    ('住宿报销？另外，年假申请需要谁审批？', 3, 'short_or_repeated_clause'),
    ('住宿申请需要谁审批；住宿申请需要谁审批', 3, 'short_or_repeated_clause'),
    ('住宿申请需要谁审批；年假申请需要谁审批；交通报销如何申请；餐费报销如何申请', 5, 'too_many_parts'),
    (QUESTION, 1, 'insufficient_top_k'),
])
def test_unsafe_or_underbudget_splitting_falls_back(question, top_k, reason):
    plan = plan_queries(question, True, top_k)
    assert plan['queries'] == [question] and not plan['applied']
    assert plan['reason'] == reason


def test_round_robin_keeps_subquery_coverage_budget_and_chunk_provenance():
    rankings = [[('a', 100), ('b', 90), ('c', 80)],
                [('a', .9), ('b', .8)], [('a', .7), ('c', .6), ('d', .5)]]
    assert interleave_rankings(rankings, 3) == [('a', 1), ('c', 2), ('b', 0)]
    assert interleave_rankings(rankings, 10) == [('a', 1), ('c', 2), ('b', 0), ('d', 2)]
    assert interleave_rankings([[], [], []], 3) == []
    # Different chunk IDs retain their separate source identities, regardless of score ties.
    assert interleave_rankings([[], [('version-a', 0)], [('version-b', 0)]], 3) == [
        ('version-a', 1), ('version-b', 2)]


class Encoder:
    def encode(self, texts):
        return [[1., 0.] for _ in texts]

    def encode_query(self, text):
        return [1., 0.]


class Reranker:
    def __init__(self):
        self.calls = []

    def score(self, question, texts):
        self.calls.append((question, texts))
        return [0.] * len(texts)


@pytest.mark.parametrize('mode', ['bm25', 'vector', 'hybrid'])
@pytest.mark.parametrize('rerank', [False, True])
def test_each_query_uses_one_filtered_snapshot_and_keeps_citations(tmp_path, monkeypatch, mode, rerank):
    ranker = Reranker()
    service = RagService(Settings(db_path=tmp_path / 'test.db', embedding_model='fake'), Encoder(), ranker)
    for name, text, changes in [
        ('current.md', '北京住宿报销上限600元，年假申请需要主管审批。', {}),
        ('old.md', '北京住宿报销上限500元，年假申请需要经理审批。', {'effective_from': '2024-01-01'}),
        ('future.md', '北京住宿报销上限900元，年假申请需要总监审批。', {'effective_from': '2999-01-01'}),
        ('archive.md', '北京住宿报销上限800元，年假申请需要组长审批。', {'status': 'archived'}),
        ('scope.md', '上海住宿报销上限700元，年假申请需要老板审批。', {'scope': '上海'}),
        ('other.md', '其他住宿报销上限100元，年假申请需要领导审批。', {'document_key': '其他'}),
    ]:
        metadata = {'document_key': '手册', 'scope': '北京', 'effective_from': '2025-01-01', **changes}
        service.ingest(name, text.encode(), metadata)
    chunks = Mock(wraps=service.store.chunks)
    rank = Mock(wraps=service._rank_query)
    monkeypatch.setattr(service.store, 'chunks', chunks)
    monkeypatch.setattr(service, '_rank_query', rank)
    result = service.retrieve(QUESTION, 3, mode, rerank, '手册', '北京', decompose=True)
    assert result['query_plan']['applied']
    chunks.assert_called_once()
    assert rank.call_count == 3
    assert all({row['document_name'] for row in call.args[1]} == {'current.md'} for call in rank.call_args_list)
    assert {s['document_name'] for s in result['sources']} == {'current.md'}
    assert [s['citation'] for s in result['sources']] == [1]
    assert result['sources'][0]['query_index'] == 1
    assert len(ranker.calls) == (3 if rerank else 0)
    if rerank:
        assert all(all('600' in text for text in texts) for _, texts in ranker.calls)
        assert 'rerank_score' in result['sources'][0]


def test_default_off_and_single_question_match_original_results(tmp_path):
    service = RagService(Settings(db_path=tmp_path / 'test.db'))
    service.ingest('travel.md', '北京住宿报销上限600元。'.encode())
    service.ingest('leave.md', '年假申请需要主管审批。'.encode())
    assert service.retrieve(QUESTION) == service.retrieve(QUESTION, decompose=False)
    question = '北京住宿报销上限是多少？'
    assert service.retrieve(question)['sources'] == service.retrieve(question, decompose=True)['sources']
    result = service.retrieve(QUESTION, 1, decompose=True)
    assert result['warnings'] and not result['query_plan']['applied']


def test_version_ambiguity_still_blocks_generation_after_decomposition(tmp_path, monkeypatch):
    service = RagService(Settings(db_path=tmp_path / 'test.db', llm_base_url='https://model.invalid', llm_model='fake'))
    for amount in [500, 600]:
        service.ingest(f'{amount}.md', f'北京住宿报销上限{amount}元，年假申请需要主管审批。'.encode(),
                       {'document_key': '手册', 'effective_from': '2025-01-01', 'version': str(amount)})
    post = Mock(side_effect=AssertionError('must not call API'))
    monkeypatch.setattr(httpx.Client, 'post', post)
    result = service.answer(QUESTION, 3, decompose=True)
    assert result['query_plan']['applied'] and result['answer_mode'] == 'document_conflict'
    assert len(result['conflicts'][0]['documents']) == 2
    post.assert_not_called()


def test_api_flag_and_generation_use_final_sources_without_real_api(tmp_path, monkeypatch):
    app = create_app(Settings(db_path=tmp_path / 'api.db', llm_base_url='https://model.invalid', llm_model='fake'))
    with TestClient(app) as client:
        app.state.rag.ingest('travel.md', '北京住宿报销上限600元。'.encode())
        app.state.rag.ingest('leave.md', '年假申请需要主管审批。'.encode())
        request = {'question': QUESTION, 'top_k': 3, 'decompose': True}
        result = client.post('/api/search', json=request).json()
        assert result['query_plan']['applied']
        assert {s['document_name'] for s in result['sources']} == {'travel.md', 'leave.md'}
        assert [s['citation'] for s in result['sources']] == [1, 2]

        original_post = httpx.Client.post

        def respond(self, url, **kwargs):
            # New Starlette versions inherit post() instead of overriding it.
            # Mock only the provider call, keeping requests to the test app real.
            if self is client:
                return original_post(self, url, **kwargs)
            messages = kwargs['json']['messages']
            assert '逐项作答' in messages[0]['content']
            for source in result['sources']:
                assert f"[{source['citation']}] 文件：{source['document_name']}" in messages[1]['content']
            return httpx.Response(200, request=httpx.Request('POST', url),
                                  json={'choices': [{'message': {'content': '600元 [1]，主管审批 [2]。'}}]})

        monkeypatch.setattr(httpx.Client, 'post', respond)
        answer = client.post('/api/chat', json=request)
        assert answer.status_code == 200 and answer.json()['answer_mode'] == 'llm'
        assert answer.json()['sources'] == result['sources']


def test_fixed_decomposition_dataset_has_valid_evidence_and_controls():
    cases, _, metadata = load_benchmark(ROOT / 'evals/public-fastapi/decomposition.json',
                                        ROOT / 'evals/public-fastapi/corpus')
    assert Counter(c['category'] for c in cases) == {'multi_evidence': 12, 'paraphrase': 6, 'unanswerable': 6}
    assert metadata['document_count'] == 6 and metadata['chunk_count'] == 50
    assert all(c['split'] == 'test' for c in cases)
