import math
import re
from collections import Counter, defaultdict
from threading import Lock


STOP = {'什么', '怎么', '如何', '多少', '是否', '可以', '这个', '那个', '我们', '的是', '是多', '少钱', '请问', '一下', '哪些', 'the', 'is', 'a', 'an', 'of', 'to', 'and'}


def tokenize(text: str) -> list[str]:
    terms = []
    for part in re.findall(r'[\u4e00-\u9fff]+|[a-z0-9_]+', text.lower()):
        if '\u4e00' <= part[0] <= '\u9fff':
            terms.extend([part] if len(part) == 1 else [part[i:i + 2] for i in range(len(part) - 1)])
        else:
            terms.append(part)
    return [term for term in terms if term not in STOP]


def bm25(query: str, rows: list[dict]) -> list[tuple[str, float]]:
    if not rows:
        return []
    counts = [Counter(tokenize(row['text'])) for row in rows]
    lengths = [sum(c.values()) for c in counts]
    average = sum(lengths) / len(rows) or 1
    query_terms = set(tokenize(query))
    frequency = Counter(term for c in counts for term in c.keys())
    scored = []
    for row, count, length in zip(rows, counts, lengths):
        score = 0.0
        for term in query_terms:
            tf = count[term]
            if tf:
                idf = math.log(1 + (len(rows) - frequency[term] + 0.5) / (frequency[term] + 0.5))
                score += idf * tf * 2.5 / (tf + 1.5 * (0.25 + 0.75 * length / average))
        if score > 0:
            scored.append((row['id'], score))
    return sorted(scored, key=lambda pair: (-pair[1], pair[0]))


def cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        raise ValueError('向量维度不一致，请重建索引。')
    denom = math.sqrt(sum(x*x for x in a) * sum(x*x for x in b))
    return sum(x*y for x, y in zip(a, b)) / denom if denom else 0.0


def reciprocal_rank_fusion(*rankings: list[tuple[str, float]]) -> list[tuple[str, float]]:
    scores: dict[str, float] = defaultdict(float)
    for ranking in rankings:
        for rank, (key, _) in enumerate(ranking, 1):
            scores[key] += 1 / (60 + rank)
    return sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))


class LocalEmbedder:
    def __init__(self, model: str, device: str = 'auto'):
        self.model_name = model
        self.device = device
        self._model = None
        self._lock = Lock()

    def encode(self, texts: list[str]) -> list[list[float]]:
        with self._lock:
            return self._encode(texts)

    def encode_query(self, text: str) -> list[float]:
        if 'bge-' in self.model_name.lower() and '-zh' in self.model_name.lower():
            text = '为这个句子生成表示以用于检索相关文章：' + text
        return self.encode([text])[0]

    @property
    def runtime(self):
        return {'loaded': self._model is not None,
                'device': str(self._model.device) if self._model is not None else self.device}

    def _encode(self, texts: list[str]) -> list[list[float]]:
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:
                raise ValueError('语义检索需先安装 requirements-models.txt 中的依赖。') from exc
            self._model = SentenceTransformer(
                self.model_name,
                device=None if self.device == 'auto' else self.device,
                trust_remote_code=False,
            )
        return self._model.encode(texts, normalize_embeddings=True, batch_size=16).tolist()
