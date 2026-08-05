"""
핵심 데이터 모델: Article -> Event -> Story

Article : 한 소스에서 수집된 원본 기사 단위
Event   : 여러 소스의 Article이 같은 구체적 사건을 다룰 때 하나로 묶은 단위
          (지금 프로젝트에서 실제로 분석/랭킹/렌더링의 기본 단위)
Story   : 시간에 걸쳐 이어지는 여러 Event를 묶는 장기 서사 단위
          (MVP에서는 스키마만 두고, 실제 자동 연결 로직은 이후 단계에서 구현)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, date


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@dataclass
class RawArticle:
    """Collector가 반환하는 정규화된 원본 기사."""

    source_id: str
    source_name: str
    title: str
    summary: str
    url: str
    published_at: datetime | None
    language: str
    category_tags: list[str] = field(default_factory=list)
    id: str = field(default_factory=lambda: new_id("art"))


@dataclass
class GlossaryItem:
    term: str
    explanation: str


@dataclass
class Entities:
    """구조화된 엔티티. 향후 주식 티커/섹터 등을 붙일 수 있도록
    문자열 리스트가 아니라 dict 리스트로 설계한다.

    예: companies = [{"name": "OpenAI", "ticker": None, "type": "private"}]
    """

    companies: list[dict] = field(default_factory=list)
    people: list[dict] = field(default_factory=list)
    countries: list[dict] = field(default_factory=list)
    organizations: list[dict] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict | None) -> "Entities":
        d = d or {}

        def norm(items):
            out = []
            for it in items or []:
                if isinstance(it, str):
                    out.append({"name": it})
                elif isinstance(it, dict) and "name" in it:
                    out.append(it)
            return out

        return cls(
            companies=norm(d.get("companies")),
            people=norm(d.get("people")),
            countries=norm(d.get("countries")),
            organizations=norm(d.get("organizations")),
        )

    def all_names(self) -> list[str]:
        names = []
        for group in (self.companies, self.people, self.countries, self.organizations):
            names.extend(item.get("name", "") for item in group)
        return [n for n in names if n]


@dataclass
class Event:
    """분석 파이프라인의 핵심 산출물. 하나의 구체적 사건."""

    id: str
    title: str
    tldr: str                      # "3줄 요약" 모드용 1문장 핵심 요약
    background: str
    details: str
    background_knowledge: str
    glossary: list[GlossaryItem]
    support_view: str
    concern_view: str
    outlook: str

    article_ids: list[str]
    source_ids: list[str]          # 이 이벤트에 기여한 소스 id들 (중복 제거됨)
    source_links: list[str]        # 원문 링크들

    entities: Entities
    category_tags: list[str]
    interest_tags: list[str]       # 매칭된 관심 키워드

    issue_type: str                # "new" | "update"
    story_id: str | None

    reliability_score: float       # 참여 소스 신뢰도 집계
    importance_score: float        # 최종 랭킹 점수 (ranker.py가 채움)

    event_date: date
    created_at: datetime


@dataclass
class Story:
    """여러 Event를 시간순으로 묶는 장기 서사."""

    id: str
    title: str
    description: str
    entities: Entities
    category_tags: list[str]
    event_ids: list[str] = field(default_factory=list)
    status: str = "ongoing"        # "ongoing" | "closed"
    user_tracked: bool = False     # "이 이슈는 계속 추적할까요?" 버튼 결과
    first_seen_at: datetime | None = None
    last_updated_at: datetime | None = None
