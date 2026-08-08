"""
Article -> Event 클러스터링.

같은 구체적 사건을 다루는 여러 소스의 기사를 하나의 클러스터로 묶는다.
Event -> Story 연결(story_linker.py)은 더 넓은 시간창과 느슨한 기준을 쓰는
별도 문제라 MVP 범위에서는 제외했다 (향후 규칙 기반으로 우선 구현 추천 —
설계 문서 4절 참고).

알고리즘: 시간창 내에서 그리디 클러스터링.
  1) 기사를 발행시각 순으로 정렬
  2) 각 기사를 순회하며, 이미 만들어진 클러스터 중 시간창 안에 있고
     제목+요약 임베딩 코사인 유사도가 threshold를 넘는 클러스터가 있으면 합류
  3) 없으면 새 클러스터 생성
"""

from __future__ import annotations

import logging
from datetime import timedelta

import numpy as np

from embeddings.base import Embedder
from models.schema import RawArticle

logger = logging.getLogger(__name__)

DEFAULT_TIME_WINDOW_HOURS = 36

# 임베딩 모델(multilingual-e5-base) 기준으로 실제 수집 데이터에 돌려 잡은 값이다.
# 쌍끼리만 비교하면 0.84가 경계지만, 아래 그리디 클러스터링은 센트로이드를 멤버
# 평균으로 갱신해서 클러스터가 커질수록 중심이 '평균적인 뉴스'로 뭉개진다. 그러면
# 무관한 기사까지 끌어당기므로 실제로는 더 높게 잡아야 한다.
#   0.84 -> 클러스터 9개 (8개 매체가 통째로 한 덩어리)
#   0.88 -> 114개 (부동산 클러스터에 해수부 세액공제가 섞임)
#   0.90 -> 246개 (환율/부동산세제/신용사면 등 같은 사건만 정확히 묶임)
# 모델을 바꾸면 이 값도 반드시 다시 재야 한다.
DEFAULT_SIMILARITY_THRESHOLD = 0.90


def _cosine_sim(a: list[float], b: list[float]) -> float:
    a = np.array(a)
    b = np.array(b)
    denom = (np.linalg.norm(a) * np.linalg.norm(b))
    if denom == 0:
        return 0.0
    return float(np.dot(a, b) / denom)


def cluster_articles_into_events(
    articles: list[RawArticle],
    embedder: Embedder,
    time_window_hours: int = DEFAULT_TIME_WINDOW_HOURS,
    similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
) -> list[list[RawArticle]]:
    if not articles:
        return []

    texts = [f"{a.title}. {a.summary}"[:500] for a in articles]
    vectors = embedder.embed(texts)

    # published_at이 없는 기사는 정렬을 위해 지금 시각으로 취급하지 않고
    # 리스트 맨 뒤로 보낸다 (원본 수집 순서를 존중).
    indexed = list(range(len(articles)))
    indexed.sort(key=lambda i: (articles[i].published_at is None, articles[i].published_at))

    clusters: list[dict] = []  # [{"indices": [...], "centroid": vector, "time": datetime}]
    window = timedelta(hours=time_window_hours)

    for i in indexed:
        article = articles[i]
        vector = vectors[i]

        best_cluster = None
        best_sim = 0.0
        for cluster in clusters:
            if article.published_at and cluster["time"]:
                if abs(article.published_at - cluster["time"]) > window:
                    continue
            sim = _cosine_sim(vector, cluster["centroid"])
            if sim > best_sim:
                best_sim = sim
                best_cluster = cluster

        if best_cluster is not None and best_sim >= similarity_threshold:
            best_cluster["indices"].append(i)
            # 센트로이드를 클러스터 내 평균으로 갱신
            member_vectors = [vectors[j] for j in best_cluster["indices"]]
            best_cluster["centroid"] = np.mean(member_vectors, axis=0).tolist()
            if article.published_at:
                best_cluster["time"] = article.published_at
        else:
            clusters.append(
                {
                    "indices": [i],
                    "centroid": vector,
                    "time": article.published_at,
                }
            )

    result = [[articles[i] for i in c["indices"]] for c in clusters]
    logger.info("기사 %d건 -> 이벤트 클러스터 %d개", len(articles), len(result))
    return result
