"""
Event의 최종 중요도(importance_score)를 계산한다.

importance_score = w1 * reliability_component
                  + w2 * corroboration_bonus
                  + w3 * personalization_score

신뢰도가 높은 소스가 여러 개 동시에 보도했을수록(corroboration), 그리고
사용자 관심사와 맞을수록 점수가 올라간다. 단순 언급 빈도가 아니라
"출처 신뢰도까지 고려한" 랭킹을 만드는 게 이 단계의 핵심 목표다.

키워드 매칭과 신뢰도 집계는 prefilter와 규칙이 같아야 하므로
(같은 이벤트가 사전 선별에서는 관심사였는데 여기서는 아닌 상황을 막기 위해)
prefilter의 구현을 그대로 가져다 쓴다.
"""

from __future__ import annotations

import logging

import yaml

from pipeline.prefilter import InterestMatcher, reliability_stats

logger = logging.getLogger(__name__)

WEIGHT_RELIABILITY = 0.5
WEIGHT_CORROBORATION = 0.2
WEIGHT_PERSONALIZATION = 0.3


def load_interests(config_path: str) -> dict:
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _personalization_score(event_payload: dict, matcher: InterestMatcher) -> tuple[float, list[str]]:
    """LLM이 생성한 제목/본문/엔티티에서 관심 키워드를 다시 찾는다.

    사전 선별 단계에서 원본 기사로 이미 찾은 키워드(prefilter_keywords)와 합집합을
    취한다. LLM이 원문을 한국어로 풀어 쓰면서 원래 있던 영문 키워드가 사라지는
    경우가 있어, 원본에서 걸린 건 유지해줘야 한다.
    """
    text = " ".join(
        [
            event_payload.get("title", ""),
            event_payload.get("tldr", ""),
            event_payload.get("details", ""),
            " ".join(event_payload.get("category_tags", [])),
            " ".join(event_payload["entities"].all_names()),
        ]
    )

    matched = list(matcher.match(text))
    for keyword in event_payload.get("prefilter_keywords", []):
        if keyword not in matched:
            matched.append(keyword)

    if not matched:
        return 0.0, []

    total = sum(matcher.weight_of(k) for k in matched)
    # 0~100 스케일로 정규화 (대충 가중치 합 5 정도를 100으로 봄)
    normalized = min(100.0, total / 5.0 * 100.0)
    # 표시 순서는 키워드 가중치 내림차순으로 통일
    matched.sort(key=lambda k: -matcher.weight_of(k))
    return normalized, matched


def score_events(event_payloads: list[dict], source_registry: dict[str, dict], interests_cfg: dict) -> list[dict]:
    matcher = InterestMatcher(interests_cfg)

    for payload in event_payloads:
        reliability_score, high_tier_count, _all_low = reliability_stats(
            payload["source_ids"], source_registry
        )
        corroboration_bonus = min(100.0, high_tier_count * 25.0)  # 소스 2개면 50, 4개 이상이면 100
        personalization_score, interest_tags = _personalization_score(payload, matcher)

        importance = (
            WEIGHT_RELIABILITY * reliability_score
            + WEIGHT_CORROBORATION * corroboration_bonus
            + WEIGHT_PERSONALIZATION * personalization_score
        )

        payload["reliability_score"] = round(reliability_score, 1)
        payload["importance_score"] = round(importance, 1)
        payload["interest_tags"] = interest_tags

        # 사전 선별에서는 원본 기사만 봤기 때문에 general로 분류됐지만, LLM이 풀어 쓴
        # 본문에서 관심 키워드가 드러나는 경우가 있다. 그때는 관심사로 승격시킨다.
        if interest_tags and payload.get("bucket") != "interest":
            payload["bucket"] = "interest"

    event_payloads.sort(key=lambda p: p["importance_score"], reverse=True)

    n_interest = sum(1 for p in event_payloads if p.get("bucket") == "interest")
    logger.info(
        "랭킹 완료: 이벤트 %d개 (관심사 %d + 그 외 주요이슈 %d)",
        len(event_payloads),
        n_interest,
        len(event_payloads) - n_interest,
    )

    return event_payloads
