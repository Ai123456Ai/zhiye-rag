import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app.config import ROOT, Settings
from app.document_policy import DocumentMetadata, DocumentMetadataPatch
from app.service import ModelError, RagService

logger = logging.getLogger(__name__)


class Query(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=10)
    mode: str = Field(default='bm25', pattern='^(bm25|vector|hybrid)$')
    rerank: bool = False
    decompose: bool = False
    document_key: str | None = Field(default=None, min_length=1, max_length=100)
    scope: str | None = Field(default=None, max_length=100)


def create_app(settings: Settings | None = None):
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app):
        app.state.rag = RagService(settings)
        yield

    app = FastAPI(title='知页 · RAG 知识库', version='0.1.0', lifespan=lifespan)
    app.mount('/static', StaticFiles(directory=ROOT / 'static'), name='static')

    @app.get('/', include_in_schema=False)
    def home():
        return FileResponse(ROOT / 'static' / 'index.html')

    @app.get('/api/health')
    def health():
        return {
            'status': 'ok', 'generation_enabled': settings.generation_enabled,
            'embedding_model': settings.embedding_model or None,
            'embedding_runtime': app.state.rag.embedder.runtime if app.state.rag.embedder else None,
            'retrieval_modes': ['bm25', 'vector', 'hybrid'] if settings.embedding_model else ['bm25'],
            'reranker_available': app.state.rag.reranker is not None,
            'reranker_runtime': app.state.rag.reranker.runtime if app.state.rag.reranker else None,
            'reranker_candidates': settings.reranker_candidates,
            'document_policy': 'effective_date_v1',
            'query_decomposition': 'rules_round_robin_v1',
        }

    @app.get('/api/documents')
    def documents():
        return app.state.rag.documents()

    @app.post('/api/documents', status_code=201)
    async def upload(file: UploadFile = File(...), document_key: str = Form(''), version: str = Form(''),
                     effective_from: str = Form(''), status: str = Form('active'), scope: str = Form('')):
        try:
            metadata = DocumentMetadata(document_key=document_key, version=version,
                                        effective_from=effective_from, status=status, scope=scope)
            content = await file.read(settings.max_upload_bytes + 1)
            if len(content) > settings.max_upload_bytes:
                raise HTTPException(413, '单个文档不能超过 10 MB。')
            return await run_in_threadpool(app.state.rag.ingest, file.filename or '', content, metadata)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        finally:
            await file.close()

    @app.post('/api/documents/sample')
    def sample():
        path = ROOT / 'examples' / '星河科技员工手册.md'
        try:
            return app.state.rag.ingest(path.name, path.read_bytes())
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.delete('/api/documents/{document_id}')
    def delete(document_id: str):
        if not app.state.rag.store.delete(document_id):
            raise HTTPException(404, '文档不存在。')
        return {'deleted': True}

    @app.patch('/api/documents/{document_id}')
    def update_document(document_id: str, changes: DocumentMetadataPatch):
        try:
            result = app.state.rag.store.update_metadata(document_id, changes.model_dump(mode='json', exclude_unset=True))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if result is None:
            raise HTTPException(404, '文档不存在。')
        return result

    @app.get('/api/documents/{document_id}')
    def document(document_id: str):
        result = app.state.rag.store.document(document_id)
        if result is None:
            raise HTTPException(404, '文档不存在。')
        return result

    def execute(query: Query, generation: bool):
        question = query.question.strip()
        if not question:
            raise HTTPException(422, '问题不能为空。')
        try:
            if generation:
                return app.state.rag.answer(question, query.top_k, query.mode, rerank=query.rerank,
                                            document_key=query.document_key, scope=query.scope, decompose=query.decompose)
            return app.state.rag.retrieve(question, query.top_k, query.mode, rerank=query.rerank,
                                           document_key=query.document_key, scope=query.scope, decompose=query.decompose)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except ModelError as exc:
            raise HTTPException(502, str(exc)) from exc
        except Exception as exc:
            logger.exception('Retrieval or model execution failed')
            raise HTTPException(503, '检索或模型执行失败，请查看服务日志；若使用本地模型，请确认模型已下载且显存充足。') from exc

    @app.post('/api/search')
    def search(query: Query):
        return execute(query, False)

    @app.post('/api/chat')
    def chat(query: Query):
        return execute(query, True)

    return app


app = create_app()
