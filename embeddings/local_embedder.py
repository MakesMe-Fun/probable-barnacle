from __future__ import annotations

import logging

from embeddings.base import Embedder

logger = logging.getLogger(__name__)

# 한국어 기사 구분 성능 때문에 paraphrase-multilingual-MiniLM-L12-v2에서 갈아탔다.
# 실측(2026-08-06): 같은 사건 쌍의 최저 유사도와 무관한 쌍의 최고 유사도 사이 간격
#   MiniLM-L12-v2         : -0.178  (뒤집혀 있어 임계값으로 가를 수 없음.
#                                    "홈플러스 개장" vs "허영 의원 협약" = 0.905)
#   multilingual-e5-base  : +0.063  (권장 임계값 0.84)
#   multilingual-e5-small : +0.056  (권장 임계값 0.85)
DEFAULT_MODEL = "intfloat/multilingual-e5-base"

# e5 계열은 입력에 접두사를 붙이도록 학습돼 있어서, 안 붙이면 성능이 떨어진다.
# 기사끼리의 대칭 비교라 query/passage 구분 없이 "query: "로 통일한다.
E5_PREFIX = "query: "


class LocalSentenceTransformerEmbedder(Embedder):
    """무료·오프라인 다국어 임베딩. 한국어+영어 기사를 함께 다루기 위해
    다국어 모델을 기본값으로 사용한다.

    모델은 최초 사용 시 자동 다운로드되어 로컬에 캐시된다 (약 1GB).
    """

    def __init__(self, model_name: str = DEFAULT_MODEL):
        from sentence_transformers import SentenceTransformer  # 지연 import

        logger.info("임베딩 모델 로딩 중: %s (최초 1회는 다운로드 때문에 오래 걸릴 수 있습니다)", model_name)
        self._model = SentenceTransformer(model_name)
        self._prefix = E5_PREFIX if "e5" in model_name.lower() else ""

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if self._prefix:
            texts = [self._prefix + t for t in texts]
        vectors = self._model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        return vectors.tolist()
