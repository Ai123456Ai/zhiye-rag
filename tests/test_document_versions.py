import hashlib
import sqlite3
from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.document_policy import DocumentMetadata, select_documents
from app.main import create_app
from app.service import RagService
from app.store import Store


def metadata(version='', effective_from=None, scope='', status='active', document_key='员工手册'):
    return dict(document_key=document_key, version=version, effective_from=effective_from, scope=scope, status=status)


def ingest(service, name, text, **kwargs):
    return service.ingest(name, text.encode('utf-8'), metadata(**kwargs))['document_id']


class Encoder:
    def encode(self, texts):
        return [[1., 0.] for _ in texts]

    def encode_query(self, text):
        return [1., 0.]


class Reranker:
    def __init__(self):
        self.seen = []

    def score(self, question, texts):
        self.seen.extend(texts)
        return [0.] * len(texts)


def test_legacy_database_migration_preserves_content_vectors_and_ids(tmp_path):
    path = tmp_path / 'legacy.db'
    body = '住宿上限500元。'.encode()
    with sqlite3.connect(path) as db:
        db.executescript('''
            CREATE TABLE documents (id TEXT PRIMARY KEY, name TEXT NOT NULL, digest TEXT UNIQUE NOT NULL,
                                    size INTEGER NOT NULL, created_at TEXT NOT NULL DEFAULT 'legacy-time');
            CREATE TABLE chunks (id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id)
                                 ON DELETE CASCADE, text TEXT NOT NULL, page INTEGER, ordinal INTEGER NOT NULL,
                                 embedding TEXT, embedding_model TEXT NOT NULL);
        ''')
        db.execute('INSERT INTO documents(id,name,digest,size) VALUES (?,?,?,?)',
                   ('old-doc', 'old.md', hashlib.sha256(body).hexdigest(), len(body)))
        db.execute('INSERT INTO chunks VALUES (?,?,?,?,?,?,?)', ('old-chunk', 'old-doc', body.decode(), 2, 1, '[1,0]', 'fake'))
    store = Store(path)
    doc = store.documents()[0]
    assert doc['id'] == 'old-doc' and doc['created_at'] == 'legacy-time'
    assert doc['status'] == 'active' and doc['effective_from'] is None and doc['document_key'] == ''
    row = store.chunks()[0]
    assert (row['id'], row['text'], row['page'], row['embedding']) == ('old-chunk', body.decode(), 2, [1, 0])
    assert Store(path).chunks() == store.chunks()  # Restart is idempotent.
    assert store.delete('old-doc') and store.chunks() == []


@pytest.mark.parametrize('mode', ['bm25', 'vector', 'hybrid'])
@pytest.mark.parametrize('rerank', [False, True])
def test_filtering_precedes_every_retriever_and_reranker(tmp_path, mode, rerank):
    reranker = Reranker()
    service = RagService(Settings(db_path=tmp_path / 'versions.db', embedding_model='fake'), Encoder(), reranker)
    new = ingest(service, 'new.md', '北京住宿报销上限600元。', version='v2', effective_from='2025-01-01')
    # Upload old last: arrival order is not authority.
    old = ingest(service, 'old.md', '北京住宿报销上限500元。', version='v99', effective_from='2024-01-01')
    future = ingest(service, 'future.md', '北京住宿报销上限900元。', effective_from='2999-01-01')
    archived = ingest(service, 'archive.md', '北京住宿报销上限800元。', effective_from='2025-02-01', status='archived')
    other = ingest(service, 'other.md', '外部手册住宿报销上限100元。', document_key='其他手册')
    hits = service.search('住宿报销', 5, mode, rerank, document_key='员工手册')
    assert {hit['document_id'] for hit in hits} == {new}
    assert hits[0]['version'] == 'v2' and hits[0]['effective_from'] == '2025-01-01'
    assert all('600' in text for text in reranker.seen)
    states = {doc['id']: doc['retrieval_state'] for doc in service.documents()}
    assert states[old] == 'superseded' and states[future] == 'future' and states[archived] == 'archived'
    assert service.store.document(old)['chunks'][0]['text'] == '北京住宿报销上限500元。'
    assert len(service.documents()) == 5 and states[other] == 'selected'


def test_same_date_alternatives_block_paid_generation_even_at_top_one(tmp_path, monkeypatch):
    service = RagService(Settings(db_path=tmp_path / 'conflict.db', llm_base_url='https://model.invalid', llm_model='fake'))
    a = ingest(service, 'a.md', '住宿上限500元。', version='v1', effective_from='2025-01-01')
    b = ingest(service, 'b.md', '住宿上限600元。', version='v2', effective_from='2025-01-01')
    post = Mock(side_effect=AssertionError('must not call generation'))
    monkeypatch.setattr(httpx.Client, 'post', post)
    result = service.answer('住宿上限多少？', top_k=1)
    assert result['answer_mode'] == 'document_conflict' and result['usage'] is None
    assert len(result['sources']) == 1 and result['sources'][0]['version_ambiguous']
    alternatives = result['conflicts'][0]['documents']
    assert {doc['id'] for doc in alternatives} == {a, b}
    assert all(doc['excerpt'] for doc in alternatives)
    assert '无法确定' in result['answer'] and result['warnings']
    post.assert_not_called()
    # Resolving metadata immediately restores the unambiguous path, without reindexing.
    before = service.store.chunks()
    service.store.update_metadata(a, {'status': 'archived'})
    assert {r['id']: r['embedding'] for r in before} == {r['id']: r['embedding'] for r in service.store.chunks()}
    assert not service.retrieve('住宿上限')['conflicts']
    assert service.search('住宿上限')[0]['document_id'] == b


def test_unknown_dates_are_not_assumed_older_and_unrelated_conflicts_do_not_block(tmp_path):
    service = RagService(Settings(db_path=tmp_path / 'unknown.db'))
    ingest(service, 'unknown.md', '住宿上限500元。', version='unknown')
    ingest(service, 'dated.md', '住宿上限600元。', version='v2', effective_from='2025-01-01')
    result = service.answer('住宿上限', 1)
    assert result['answer_mode'] == 'document_conflict'
    assert result['conflicts'][0]['reasons'] == ['ambiguous_versions']
    service.ingest('coffee.md', '咖啡供应时间为上午九点。'.encode())
    assert service.answer('咖啡供应')['answer_mode'] == 'excerpts'
    assert not service.retrieve('咖啡供应')['conflicts']


def test_scopes_are_not_compared_by_date_and_exact_filter_resolves_choice(tmp_path):
    service = RagService(Settings(db_path=tmp_path / 'scopes.db'))
    beijing = ingest(service, 'bj.md', '住宿上限500元。', effective_from='2024-01-01', scope='北京')
    shanghai = ingest(service, 'sh.md', '住宿上限700元。', effective_from='2025-01-01', scope='上海')
    assert service.answer('住宿上限', 1)['answer_mode'] == 'document_conflict'
    assert service.retrieve('住宿上限')['conflicts'][0]['reasons'] == ['multiple_scopes']
    result = service.answer('住宿上限', scope='北京')
    assert result['answer_mode'] == 'excerpts' and result['conflicts'] == []
    assert {source['document_id'] for source in result['sources']} == {beijing}
    assert service.search('住宿上限', scope='上海')[0]['document_id'] == shanghai
    assert service.search('住宿上限', scope='') == []  # No implicit fallback to another scope.


def test_effective_date_boundary_and_same_version_label_does_not_override_date():
    docs = [dict(id='old', name='old', **metadata('v99', '2025-01-01')),
            dict(id='new', name='new', **metadata('v1', '2025-02-01'))]
    from datetime import date
    assert select_documents(docs, as_of=date(2025, 1, 31))[0] == {'old'}
    assert select_documents(docs, as_of=date(2025, 2, 1))[0] == {'new'}


def test_metadata_validation_duplicate_does_not_mutate_and_old_docs_remain_unclassified(tmp_path):
    service = RagService(Settings(db_path=tmp_path / 'meta.db'))
    body = '住宿上限500元。'.encode()
    original = service.ingest('old.md', body)
    with pytest.raises(ValueError, match='编辑版本信息'):
        service.ingest('copy.md', body, metadata('v1', '2025-01-01'))
    assert service.documents()[0]['document_key'] == ''
    service.store.update_metadata(original['document_id'], metadata('v1', '2025-01-01'))
    assert service.ingest('copy.md', body)['created'] is False
    for values in ({'version': 'v1'}, metadata(effective_from='2025-02-30'), metadata(effective_from='20250101'),
                   metadata(status='invalid'), {'document_key': ' '}, {'unexpected': True}):
        if values == {'document_key': ' '}:
            assert DocumentMetadata.model_validate(values).document_key == ''
        else:
            with pytest.raises(ValueError):
                DocumentMetadata.model_validate(values)


def test_api_upload_patch_search_conflict_and_view_archived_source(tmp_path):
    app = create_app(Settings(db_path=tmp_path / 'api-versions.db'))
    with TestClient(app) as client:
        first = client.post('/api/documents', data=metadata('v1', '2024-01-01'),
                            files={'file': ('old.md', '住宿上限500元。'.encode())})
        second = client.post('/api/documents', data=metadata('v2', '2025-01-01'),
                             files={'file': ('new.md', '住宿上限600元。'.encode())})
        assert first.status_code == second.status_code == 201
        old, new = first.json()['document_id'], second.json()['document_id']
        response = client.post('/api/chat', json={'question': '住宿上限', 'document_key': '员工手册'})
        assert response.json()['sources'][0]['document_id'] == new
        assert client.patch(f'/api/documents/{old}', json={'effective_from': '2025-01-01'}).status_code == 200
        result = client.post('/api/search', json={'question': '住宿上限', 'top_k': 1}).json()
        assert len(result['sources']) == 1 and len(result['conflicts'][0]['documents']) == 2
        assert client.post('/api/chat', json={'question': '住宿上限'}).json()['answer_mode'] == 'document_conflict'
        assert client.patch(f'/api/documents/{old}', json={'status': 'archived'}).status_code == 200
        view = client.get(f'/api/documents/{old}').json()
        assert view['document']['status'] == 'archived' and '500' in view['chunks'][0]['text']
        assert 'embedding' not in view['chunks'][0]
        assert client.get('/api/documents/missing').status_code == 404
        assert client.patch('/api/documents/missing', json={'status': 'active'}).status_code == 404
        assert client.patch(f'/api/documents/{old}', json={'effective_from': 'nonsense'}).status_code == 422
        assert client.patch(f'/api/documents/{new}', json={'document_key': ''}).status_code == 400
        assert client.patch(f'/api/documents/{old}', json={'status': 'invalid'}).status_code == 422
        assert client.post('/api/documents', data={'version': 'v1'}, files={'file': ('bad.md', b'text')}).status_code == 400
        assert client.post('/api/search', json={'question': '住宿', 'scope': '不存在的范围'}).json()['sources'] == []
        assert client.post('/api/search', json={'question': '住宿', 'document_key': ''}).status_code == 422
        assert client.patch(f'/api/documents/{old}', json={'effective_from': None}).json()['effective_from'] is None
