"""Optional local cross encoder; scores query/document pairs, not answer confidence."""
import math
from threading import Lock


class LocalReranker:
    def __init__(self, model: str, device: str = 'auto'):
        self.model_name = model
        self.device = device
        self._model = None
        self._lock = Lock()

    @property
    def runtime(self):
        return {'loaded': self._model is not None,
                'device': str(self._model.model.device) if self._model is not None else self.device,
                'max_length': 512}

    def score(self, question: str, texts: list[str]) -> list[float]:
        if not texts:
            return []
        with self._lock:
            if self._model is None:
                try:
                    from sentence_transformers import CrossEncoder
                except ImportError as exc:
                    raise ValueError('重排序需先安装 requirements-models.txt 中的依赖。') from exc
                self._model = CrossEncoder(
                    self.model_name, device=None if self.device == 'auto' else self.device,
                    max_length=512, trust_remote_code=False,
                )
            from torch.nn import Identity
            values = self._model.predict(
                [(question, text) for text in texts], batch_size=8, show_progress_bar=False,
                activation_fn=Identity(), convert_to_numpy=True,
            ).tolist()
            if len(values) != len(texts) or any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in values):
                raise ValueError('重排序模型必须为每个候选返回一个有限分数。')
            return [float(value) for value in values]
