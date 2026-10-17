import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / '.env')


@dataclass(frozen=True)
class Settings:
    db_path: Path
    llm_base_url: str = ''
    llm_api_key: str = ''
    llm_model: str = ''
    embedding_model: str = ''
    embedding_device: str = 'auto'
    reranker_model: str = ''
    reranker_device: str = 'auto'
    reranker_candidates: int = 10
    max_upload_bytes: int = 10 * 1024 * 1024

    @classmethod
    def from_env(cls):
        db = Path(os.getenv('RAG_DB_PATH', 'data/knowledge.db'))
        return cls(
            db_path=db if db.is_absolute() else ROOT / db,
            llm_base_url=os.getenv('LLM_BASE_URL', '').rstrip('/'),
            llm_api_key=os.getenv('LLM_API_KEY', ''),
            llm_model=os.getenv('LLM_MODEL', ''),
            embedding_model=os.getenv('EMBEDDING_MODEL', ''),
            embedding_device=os.getenv('EMBEDDING_DEVICE', 'auto'),
            reranker_model=os.getenv('RERANKER_MODEL', ''),
            reranker_device=os.getenv('RERANKER_DEVICE', 'auto'),
            reranker_candidates=int(os.getenv('RERANKER_CANDIDATES', '10')),
        )

    def __post_init__(self):
        if not 10 <= self.reranker_candidates <= 100:
            raise ValueError('RERANKER_CANDIDATES 需要在 10 到 100 之间。')

    @property
    def generation_enabled(self):
        return bool(self.llm_base_url and self.llm_model)
