"""Back up SQLite, then atomically replace stored vectors without deleting text."""
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.service import RagService


def reindex(settings: Settings, embedder=None):
    if not settings.embedding_model:
        raise ValueError('Set EMBEDDING_MODEL in .env first.')
    service = RagService(settings, embedder)
    rows = service.store.chunks()
    if not rows:
        print('No documents to reindex.')
        return
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup = settings.db_path.with_name(f'{settings.db_path.stem}.before-reindex-{stamp}.db')
    with service.store.connect() as source:
        target = sqlite3.connect(backup)
        try:
            source.backup(target)
        finally:
            target.close()
    print(f'Backup: {backup}', flush=True)
    vectors = service.embedder.encode([row['text'] for row in rows])
    if len(vectors) != len(rows):
        raise ValueError('Unexpected embedding count; database unchanged.')
    with service.store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        current = db.execute('SELECT id, text FROM chunks ORDER BY id').fetchall()
        if [(r['id'], r['text']) for r in current] != sorted((r['id'], r['text']) for r in rows):
            raise RuntimeError('Documents changed during indexing. Stop uploads and retry.')
        db.executemany('UPDATE chunks SET embedding=?, embedding_model=? WHERE id=?',
                       [(json.dumps(v), settings.embedding_model, row['id']) for row, v in zip(rows, vectors)])
    print(f'Reindexed {len(rows)} chunks; original document IDs and text preserved.')


def main():
    reindex(Settings.from_env())


if __name__ == '__main__':
    main()
