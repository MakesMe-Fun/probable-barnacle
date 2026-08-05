"""
sources.yaml을 읽어 각 소스에 맞는 Collector를 인스턴스화하고,
전체 소스를 동시에(스레드풀) 호출해 하나의 RawArticle 리스트로 합친다.

새 소스 타입을 추가하려면:
  1. collectors/xxx_collector.py 에 Collector 구현체 작성
  2. 아래 COLLECTOR_MAP 에 "type 문자열": 클래스 한 줄 추가
  3. config/sources.yaml 에 해당 type의 소스 항목 추가
그게 전부다. 파이프라인의 다른 부분은 건드릴 필요가 없다.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

import yaml

from collectors.base import Collector
from collectors.rss_collector import RSSCollector
from models.schema import RawArticle

logger = logging.getLogger(__name__)

COLLECTOR_MAP: dict[str, Collector] = {
    "rss": RSSCollector(),
    # "news_api": NewsAPICollector(),      # 향후 구현
    # "social": SocialCollector(),         # 향후 구현 (Reddit/X)
    # "blog": BlogCollector(),             # RSS 없는 기업 블로그
    # "gov_press": GovPressCollector(),    # 정부 보도자료
}


def load_sources(config_path: str) -> list[dict]:
    with open(config_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return [s for s in data.get("sources", []) if s.get("enabled", False)]


def build_source_registry(sources: list[dict]) -> dict[str, dict]:
    """source_id -> source_config 매핑. ranker/state_store에서 신뢰도 조회에 쓴다."""
    return {s["id"]: s for s in sources}


def collect_all(config_path: str, max_workers: int = 8) -> tuple[list[RawArticle], dict[str, dict]]:
    sources = load_sources(config_path)
    registry = build_source_registry(sources)

    articles: list[RawArticle] = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_source = {}
        for source in sources:
            collector = COLLECTOR_MAP.get(source["type"])
            if collector is None:
                logger.warning(
                    "[%s] type=%s 에 대한 Collector가 아직 구현되지 않았습니다. 건너뜁니다.",
                    source["name"],
                    source["type"],
                )
                continue
            future = executor.submit(collector.collect, source)
            future_to_source[future] = source

        for future in as_completed(future_to_source):
            source = future_to_source[future]
            try:
                result = future.result()
                articles.extend(result)
            except Exception as e:  # noqa: BLE001
                logger.warning("[%s] 수집 중 예외 발생, 이 소스는 건너뜁니다: %s", source["name"], e)

    logger.info("전체 소스에서 기사 %d건 수집 완료", len(articles))
    return articles, registry
