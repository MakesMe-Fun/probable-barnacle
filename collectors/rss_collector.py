from __future__ import annotations

import logging
from datetime import datetime

import feedparser

from collectors.base import Collector
from models.schema import RawArticle

logger = logging.getLogger(__name__)

# 소스당 가져올 기사 수 상한. 기본 15는 피드가 주는 양의 30%밖에 안 써서
# (연합뉴스 120건 중 15건, 조선일보 100건 중 15건) 아침 브리핑에 쓰기엔 너무 좁았다.
# sources.yaml에서 소스별로 max_articles 로 덮어쓸 수 있다.
MAX_ARTICLES_PER_SOURCE = 100

# feedparser의 기본 User-Agent("feedparser/6.x")를 그대로 쓰면 봇으로 보고
# 403을 주는 언론사가 있다. 브라우저 UA로 요청한다.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class RSSCollector(Collector):
    """임의의 RSS/Atom 피드 URL을 정규화된 RawArticle로 변환한다.

    국내외 대부분의 언론사, 그리고 RSS를 제공하는 기업 블로그(OpenAI 등)가
    전부 이 하나의 구현체로 커버된다.
    """

    def collect(self, source_config: dict) -> list[RawArticle]:
        source_id = source_config["id"]
        source_name = source_config["name"]
        url = source_config["endpoint"]

        try:
            feed = feedparser.parse(url, agent=USER_AGENT)
        except Exception as e:  # noqa: BLE001
            logger.warning("[%s] RSS 요청 자체가 실패했습니다: %s", source_name, e)
            return []

        if feed.bozo and not feed.entries:
            # HTTP 상태를 같이 찍어준다. 403/404면 URL이나 봇 차단 문제이고,
            # 200인데 파싱만 실패한 거면 피드 자체가 깨진 것이라 대응이 다르다.
            logger.warning(
                "[%s] RSS 파싱 실패로 보입니다 (HTTP %s / %s). URL을 확인해주세요: %s",
                source_name,
                getattr(feed, "status", "?"),
                getattr(feed, "bozo_exception", "unknown"),
                url,
            )
            return []

        limit = source_config.get("max_articles") or MAX_ARTICLES_PER_SOURCE
        articles: list[RawArticle] = []
        for entry in feed.entries[:limit]:
            published_at = self._parse_published(entry)
            articles.append(
                RawArticle(
                    source_id=source_id,
                    source_name=source_name,
                    title=entry.get("title", "").strip(),
                    summary=entry.get("summary", entry.get("description", "")).strip(),
                    url=entry.get("link", ""),
                    published_at=published_at,
                    language=source_config.get("language", "ko"),
                    category_tags=source_config.get("category_tags", []),
                )
            )

        if not articles:
            logger.warning("[%s] 기사를 0건 수집했습니다: %s", source_name, url)
        else:
            logger.info("[%s] 기사 %d건 수집", source_name, len(articles))

        return articles

    @staticmethod
    def _parse_published(entry) -> datetime | None:
        for key in ("published_parsed", "updated_parsed"):
            t = entry.get(key)
            if t:
                try:
                    return datetime(*t[:6])
                except Exception:  # noqa: BLE001
                    pass
        return None
