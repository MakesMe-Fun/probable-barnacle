from __future__ import annotations

from abc import ABC, abstractmethod


class Embedder(ABC):
    """임베딩 제공자 추상 인터페이스.

    dedup.py / story_linker.py는 이 인터페이스에만 의존한다. 로컬 모델에서
    OpenAI/Cohere 임베딩 API로 바꾸고 싶으면 이 클래스를 상속한 구현체를
    하나 추가하고 main.py에서 생성하는 부분만 바꾸면 된다.
    """

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError
