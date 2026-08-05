from __future__ import annotations

import logging

from embeddings.base import Embedder

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "paraphrase-multilingual-MiniLM-L12-v2"


class LocalSentenceTransformerEmbedder(Embedder):
    """무료·오프라인 다국어 임베딩. 한국어+영어 기사를 함께 다루기 위해
    다국어 모델을 기본값으로 사용한다.

    모델은 최초 사용 시 자동 다운로드되어 로컬에 캐시된다 (수백 MB).
    """

    def __init__(self, model_name: str = DEFAULT_MODEL):
        from sentence_transformers import SentenceTransformer  # 지연 import

        logger.info("임베딩 모델 로딩 중: %s (최초 1회는 다운로드 때문에 오래 걸릴 수 있습니다)", model_name)
        self._model = SentenceTransformer(model_name)

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors = self._model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        return vectors.tolist()
