import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import ROOT, Settings
from app.documents import parse_document, split_text
from app.main import create_app
from app.service import ModelError, RagService
from app.retrieval import LocalEmbedder


@pytest.fixture
def service(tmp_path):
    return RagService(Settings(db_path=tmp_path / 'test.db'))


def test_chunking_preserves_text_and_progress():
    text = '甲乙丙丁' * 900
    chunks = split_text(text, size=100, overlap=20)
    assert chunks[0] == text[:100]
    reconstructed = chunks[0] + ''.join(c[20:] for c in chunks[1:])
    assert reconstructed == text
    with pytest.raises(ValueError):
        split_text(text, 100, 100)


def test_chinese_retrieval_deduplication_and_delete(service):
    a = service.ingest('travel.md', '北京住宿报销上限为每晚500元。'.encode())
    service.ingest('leave.md', '年假申请需要主管审批。'.encode())
    assert service.ingest('copy.md', '北京住宿报销上限为每晚500元。'.encode())['created'] is False
    hits = service.search('北京住宿报销上限是多少？')
    assert hits[0]['document_name'] == 'travel.md'
    assert '500' in hits[0]['text']
    assert service.answer('北京住宿报销上限是多少？')['answer_mode'] == 'excerpts'
    assert service.store.delete(a['document_id'])
    assert not any(h['document_id'] == a['document_id'] for h in service.search('北京住宿报销'))


def test_no_evidence_and_no_fake_vector_mode(service):
    assert service.answer('不存在的问题')['answer_mode'] == 'no_evidence'
    with pytest.raises(ValueError, match='Embedding'):
        service.search('问题', mode='hybrid')


def test_parsing_errors_and_bom():
    assert parse_document('a.txt', '报销流程'.encode('utf-16'))[0].text == '报销流程'
    for name, body in [('a.exe', b'abc'), ('a.md', b'  '), ('a.pdf', b'broken')]:
        with pytest.raises(ValueError):
            parse_document(name, body)


class FakeEmbedder:
    def encode(self, texts):
        return [[1., 0.] if '住宿' in text else [0., 1.] for text in texts]


def test_vector_fusion_and_model_mismatch(tmp_path):
    settings = Settings(db_path=tmp_path / 'vector.db', embedding_model='fake')
    service = RagService(settings, FakeEmbedder())
    service.ingest('travel.md', '北京住宿每晚500元。'.encode())
    service.ingest('leave.md', '年假每年5天。'.encode())
    assert service.search('住宿标准', mode='vector')[0]['document_name'] == 'travel.md'
    assert service.search('住宿标准', mode='hybrid')[0]['document_name'] == 'travel.md'
    changed = RagService(Settings(db_path=settings.db_path, embedding_model='other'), FakeEmbedder())
    with pytest.raises(ValueError, match='重新导入'):
        changed.search('住宿', mode='hybrid')


def test_api_workflow(tmp_path):
    app = create_app(Settings(db_path=tmp_path / 'api.db', max_upload_bytes=1000))
    with TestClient(app) as client:
        assert client.get('/').status_code == 200
        assert client.get('/static/app.js').status_code == 200
        assert client.get('/api/health').json()['retrieval_modes'] == ['bm25']
        uploaded = client.post('/api/documents', files={'file': ('rules.md', '住宿报销上限500元。'.encode())})
        assert uploaded.status_code == 201
        document_id = uploaded.json()['document_id']
        assert len(client.get('/api/documents').json()) == 1
        result = client.post('/api/chat', json={'question': '住宿报销上限是多少？'})
        assert result.status_code == 200
        assert result.json()['sources'][0]['citation'] == 1
        assert result.json()['answer_mode'] == 'excerpts'
        assert client.post('/api/search', json={'question': '   '}).status_code == 422
        assert client.post('/api/search', json={'question': '住宿', 'top_k': 0}).status_code == 422
        assert client.post('/api/search', json={'question': '住宿', 'mode': 'hybrid'}).status_code == 400
        assert client.post('/api/documents', files={'file': ('big.txt', b'a' * 1001)}).status_code == 413
        assert client.post('/api/documents', files={'file': ('bad.zip', b'abc')}).status_code == 400
        assert client.delete(f'/api/documents/{document_id}').status_code == 200
        assert client.delete(f'/api/documents/{document_id}').status_code == 404
        assert client.post('/api/chat', json={'question': '住宿'}).json()['sources'] == []


def test_model_request_and_invalid_citations(tmp_path, monkeypatch):
    service = RagService(Settings(db_path=tmp_path / 'llm.db', llm_base_url='https://model.example/v1', llm_model='test'))
    service.ingest('a.md', '住宿报销上限500元。'.encode())

    def respond(self, url, **kwargs):
        assert url == 'https://model.example/v1/chat/completions'
        body = kwargs['json']
        assert '500' in body['messages'][1]['content']
        return httpx.Response(200, json={'choices': [{'message': {'content': '500元 [99]'}}]}, request=httpx.Request('POST', url))

    monkeypatch.setattr(httpx.Client, 'post', respond)
    result = service.answer('住宿上限？')
    assert result['answer_mode'] == 'llm'
    assert result['warnings']

    def fail(self, url, **kwargs):
        raise httpx.ConnectError('secret details')

    monkeypatch.setattr(httpx.Client, 'post', fail)
    with pytest.raises(ModelError) as exc:
        service.answer('住宿上限？')
    assert 'secret' not in str(exc.value)


def test_sample_evaluation_cases(service):
    path = ROOT / 'examples' / '星河科技员工手册.md'
    service.ingest(path.name, path.read_bytes())
    cases = json.loads((ROOT / 'evals' / 'questions.json').read_text(encoding='utf-8'))
    for case in cases:
        hits = service.search(case['question'], top_k=5)
        assert any(case['evidence_contains'] in hit['text'] for hit in hits), case['question']


def test_bge_query_instruction_and_document_encoding():
    from unittest.mock import Mock
    embedder = LocalEmbedder('D:/models/bge-small-zh-v1.5/revision', 'cuda')
    embedder.encode = Mock(return_value=[[1., 0.]])
    assert embedder.encode_query('住宿费用？') == [1., 0.]
    embedder.encode.assert_called_once_with(['为这个句子生成表示以用于检索相关文章：住宿费用？'])
    generic = LocalEmbedder('other-model')
    generic.encode = Mock(return_value=[[0., 1.]])
    generic.encode_query('住宿费用？')
    generic.encode.assert_called_once_with(['住宿费用？'])


def test_search_uses_query_specific_encoder(tmp_path):
    class QueryEmbedder(FakeEmbedder):
        def encode_query(self, text):
            return [1., 0.]
    service = RagService(Settings(db_path=tmp_path / 'query.db', embedding_model='fake'), QueryEmbedder())
    service.ingest('travel.md', '住宿每晚500元'.encode())
    service.ingest('other.md', '其他规定'.encode())
    assert service.search('住酒店花多少钱', mode='vector')[0]['document_name'] == 'travel.md'


def test_reindex_preserves_sources_and_failure_preserves_index(tmp_path):
    from scripts.reindex import reindex
    settings = Settings(db_path=tmp_path / 'reindex.db')
    service = RagService(settings)
    original = service.ingest('rules.md', '住宿每晚500元'.encode())
    chunk_id = service.store.chunks()[0]['id']
    new_settings = Settings(db_path=settings.db_path, embedding_model='fake')
    reindex(new_settings, FakeEmbedder())
    rows = service.store.chunks()
    assert rows[0]['id'] == chunk_id
    assert rows[0]['document_id'] == original['document_id']
    assert rows[0]['embedding'] == [1., 0.]
    backups = list(tmp_path.glob('*.before-reindex-*.db'))
    assert backups
    from app.store import Store
    assert Store(backups[0]).chunks()[0]['embedding'] is None

    class FailingEmbedder:
        def encode(self, texts):
            raise RuntimeError('GPU failed')
    with pytest.raises(RuntimeError, match='GPU failed'):
        reindex(new_settings, FailingEmbedder())
    assert service.store.chunks() == rows
