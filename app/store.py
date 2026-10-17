import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

from app.documents import Chunk
from app.document_policy import DocumentMetadata


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    digest TEXT UNIQUE NOT NULL, size INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                    text TEXT NOT NULL, page INTEGER, ordinal INTEGER NOT NULL,
                    embedding TEXT, embedding_model TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS chunks_document_idx ON chunks(document_id);
            ''')
            # Serialize additive migrations, preserving all IDs, text and vectors.
            db.execute('BEGIN IMMEDIATE')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(documents)')}
            additions = {
                'document_key': "TEXT NOT NULL DEFAULT ''", 'version': "TEXT NOT NULL DEFAULT ''",
                'effective_from': 'TEXT', 'status': "TEXT NOT NULL DEFAULT 'active'",
                'scope': "TEXT NOT NULL DEFAULT ''",
            }
            for name, definition in additions.items():
                if name not in columns:
                    db.execute(f'ALTER TABLE documents ADD COLUMN {name} {definition}')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def find_digest(self, content: bytes):
        with self.connect() as db:
            row = db.execute('SELECT id FROM documents WHERE digest=?', (hashlib.sha256(content).hexdigest(),)).fetchone()
            return row['id'] if row else None

    def add(self, name: str, content: bytes, chunks: list[Chunk], vectors=None, model='', metadata=None):
        metadata = DocumentMetadata.model_validate(metadata or {}).model_dump(mode='json')
        if vectors is not None and len(vectors) != len(chunks):
            raise ValueError('向量数量与分块数量不一致。')
        document_id = uuid.uuid4().hex
        try:
            with self.connect() as db:
                db.execute('''INSERT INTO documents
                    (id,name,digest,size,document_key,version,effective_from,status,scope)
                    VALUES (?,?,?,?,?,?,?,?,?)''',
                           (document_id, name, hashlib.sha256(content).hexdigest(), len(content),
                            metadata['document_key'], metadata['version'], metadata['effective_from'],
                            metadata['status'], metadata['scope']))
                for i, chunk in enumerate(chunks):
                    db.execute('INSERT INTO chunks VALUES (?,?,?,?,?,?,?)',
                               (uuid.uuid4().hex, document_id, chunk.text, chunk.page, chunk.ordinal,
                                json.dumps(vectors[i]) if vectors is not None else None, model))
        except sqlite3.IntegrityError:
            existing = self.find_digest(content)
            if existing:
                return existing, False
            raise
        return document_id, True

    def documents(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute('''SELECT d.*, count(c.id) AS chunk_count
                FROM documents d LEFT JOIN chunks c ON d.id=c.document_id
                GROUP BY d.id ORDER BY d.created_at DESC, d.id''')]

    def document(self, document_id):
        with self.connect() as db:
            document = db.execute('SELECT * FROM documents WHERE id=?', (document_id,)).fetchone()
            if document is None:
                return None
            chunks = [dict(row) for row in db.execute(
                'SELECT text,page,ordinal FROM chunks WHERE document_id=? ORDER BY ordinal', (document_id,))]
            return {'document': dict(document), 'chunks': chunks}

    def chunks(self):
        with self.connect() as db:
            rows = [dict(row) for row in db.execute('''SELECT c.*, d.name AS document_name,
                d.document_key, d.version, d.effective_from, d.status, d.scope
                FROM chunks c JOIN documents d ON c.document_id=d.id ORDER BY d.id, c.ordinal''')]
        for row in rows:
            row['embedding'] = json.loads(row['embedding']) if row['embedding'] else None
        return rows

    def update_metadata(self, document_id: str, changes: dict):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT * FROM documents WHERE id=?', (document_id,)).fetchone()
            if current is None:
                return None
            previous = {key: current[key] for key in DocumentMetadata.model_fields}
            metadata = DocumentMetadata.model_validate({**previous, **changes}).model_dump(mode='json')
            db.execute('''UPDATE documents SET document_key=?,version=?,effective_from=?,status=?,scope=? WHERE id=?''',
                       (metadata['document_key'], metadata['version'], metadata['effective_from'],
                        metadata['status'], metadata['scope'], document_id))
            return {'document_id': document_id, **metadata}

    def delete(self, document_id: str) -> bool:
        with self.connect() as db:
            return db.execute('DELETE FROM documents WHERE id=?', (document_id,)).rowcount > 0
