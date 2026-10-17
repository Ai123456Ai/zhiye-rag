import math
from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.reranking import LocalReranker
from app.service import RagService


class FakeReranker:
    def __init__(self):
        self.calls = []

    @property
    def runtime(self):
        return {'loaded': True, 'device': 'fake'}

    def score(self, question, texts):
        self.calls.append((question, texts))
        return [10.0 if '正确证据' in text else -10.0 for text in texts]


def test_reranking_expands_pool_then_renumbers_citations_and_preserves_scores(tmp_path):
    reranker = FakeReranker()
    service = RagService(Settings(db_path=tmp_path / 'rerank.db'), reranker=reranker)
    service.ingest('distractor.md', '住宿住宿住宿住宿住宿。'.encode())
    service.ingest('target.md', '住宿正确证据500元。'.encode())
    original = service.search('住宿', 2)
    assert original[0]['document_name'] == 'distractor.md'
    # Merely configuring a reranker does not call it.
    assert not reranker.calls
    hits = service.search('住宿', 1, rerank=True)
    assert hits[0]['document_name'] == 'target.md' and hits[0]['citation'] == 1
    assert hits[0]['chunk_id'] == original[1]['chunk_id']
    assert hits[0]['score'] == original[1]['score']
    assert hits[0]['rerank_score'] == 10.0
    assert len(reranker.calls[0][1]) == 2
    empty = RagService(Settings(db_path=tmp_path / 'empty.db'), reranker=reranker)
    assert empty.search('住宿', rerank=True) == []
    assert len(reranker.calls) == 1


def test_candidate_limit_stable_ties_and_bad_score_failure(tmp_path):
    reranker = FakeReranker()
    service = RagService(Settings(db_path=tmp_path / 'limits.db'), reranker=reranker)
    for i in range(13):
        service.ingest(f'{i}.md', f'住宿第{i}份。'.encode())
    original = service.search('住宿', 10)
    reranker.score = Mock(return_value=[0.] * 10)
    hits = service.search('住宿', 3, rerank=True)
    assert [h['chunk_id'] for h in hits] == [h['chunk_id'] for h in original[:3]]
    assert len(reranker.score.call_args.args[1]) == 10
    for scores in ([1.], [math.nan] * 10):
        reranker.score.return_value = scores
        with pytest.raises(ValueError, match='重排序分数'):
            service.search('住宿', 3, rerank=True)


def test_local_reranker_uses_query_text_pairs_and_raw_scores_without_network():
    np = pytest.importorskip('numpy')
    pytest.importorskip('torch')
    reranker = LocalReranker('fake', 'cpu')
    reranker._model = Mock()
    reranker._model.predict.return_value = np.array([-3.5, 2.5])
    assert reranker.score('问题', ['甲', '乙']) == [-3.5, 2.5]
    assert reranker._model.predict.call_args.args[0] == [('问题', '甲'), ('问题', '乙')]
    assert reranker._model.predict.call_args.kwargs['activation_fn'](-3.5) == -3.5
    assert reranker.score('问题', []) == []
    reranker._model.predict.return_value = np.array([[0., 1.], [1., 0.]])
    with pytest.raises(ValueError, match='有限分数'):
        reranker.score('问题', ['甲', '乙'])


def test_rerank_api_is_opt_in_and_generation_uses_new_citation_order(tmp_path, monkeypatch):
    app = create_app(Settings(db_path=tmp_path / 'api.db', llm_base_url='https://model.invalid', llm_model='fake'))
    with TestClient(app) as client:
        assert client.get('/api/health').json()['reranker_available'] is False
        assert client.post('/api/search', json={'question': '住宿', 'rerank': True}).status_code == 400
        app.state.rag.reranker = FakeReranker()
        assert client.get('/api/health').json()['reranker_available'] is True
        app.state.rag.ingest('distractor.md', '住宿住宿住宿住宿住宿。'.encode())
        app.state.rag.ingest('target.md', '住宿正确证据500元。'.encode())

        original_post = httpx.Client.post

        def respond(self, url, **kwargs):
            if self is client:
                return original_post(self, url, **kwargs)
            context = kwargs['json']['messages'][1]['content']
            assert '[1] 文件：target.md' in context
            assert 'distractor.md' not in context
            return httpx.Response(200, json={'choices': [{'message': {'content': '500元 [1]'}}]},
                                  request=httpx.Request('POST', url))

        monkeypatch.setattr(httpx.Client, 'post', respond)
        response = client.post('/api/chat', json={'question': '住宿', 'rerank': True, 'top_k': 1})
        assert response.status_code == 200
        assert response.json()['rerank_enabled'] is True
        assert response.json()['sources'][0]['document_name'] == 'target.md'
        assert response.json()['warnings'] == []
        assert client.post('/api/search', json={'question': '住宿', 'top_k': 11}).status_code == 422
        # A reranker failure must not silently generate with the old ranking.
        app.state.rag.reranker.score = Mock(side_effect=RuntimeError('scoring failed'))
        post = Mock(side_effect=AssertionError('generation must not run'))
        monkeypatch.setattr(httpx.Client, 'post', post)
        # TestClient inherits httpx.Client, so call its request method directly.
        failed = client.request('POST', '/api/chat', json={'question': '住宿', 'rerank': True})
        assert failed.status_code == 503
        post.assert_not_called()


def test_reranker_candidates_bounds(tmp_path):
    for size in (0, 9, 101):
        with pytest.raises(ValueError, match='RERANKER_CANDIDATES'):
            Settings(db_path=tmp_path / 'test.db', reranker_candidates=size)
