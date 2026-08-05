"""
Collector 추상 인터페이스.

새 소스 타입(News API, Reddit, X, 기업 블로그 스크레이퍼, 정부 보도자료 등)을
추가할 때는 이 클래스를 상속한 구현체 하나만 작성하고, registry.py의
COLLECTOR_MAP에 등록하면 된다. 파이프라인의 나머지 단계(dedup/analyzer/
ranker/state_store/renderers)는 전혀 수정할 필요가 없다.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from models.schema import RawArticle


class Collector(ABC):
    """모든 소스 타입이 구현해야 하는 공통 인터페이스."""

    @abstractmethod
    def collect(self, source_config: dict) -> list[RawArticle]:
        """source_config(sources.yaml의 한 항목)를 받아 RawArticle 리스트를 반환한다.

        구현체는 반드시 예외를 삼키고 빈 리스트를 반환해야 한다 (한 소스의
        실패가 전체 파이프라인을 죽이면 안 된다). 대신 실패 사실은 로깅한다.
        """
        raise NotImplementedError
