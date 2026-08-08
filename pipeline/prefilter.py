"""
LLM을 부르기 전에, 토큰을 한 톨도 쓰지 않고 계산할 수 있는 신호만으로
클러스터를 두 갈래로 나눈다.

  A. 관심사(interest) — interests.yaml의 키워드가 원본 기사 제목/요약에 등장.
     "관심 키워드에 걸린 건 빠짐없이 본다"가 목표라서 개수를 자르지 않는다.
     (안전장치로 max_interest_events 상한만 두고, 잘릴 때는 반드시 로그를 남긴다.)

  B. 주요 이슈(general) — 키워드는 안 걸렸지만 신뢰도 높은 매체 여러 곳이
     같은 사건을 동시에 보도한 것. "지정 키워드가 아니어도 이슈화된 것"에 해당한다.
     이쪽은 top_general_events개만 추린다.

여기서 쓰는 재료(소스 신뢰도, 교차 보도 매체 수, 키워드 등장 여부)는 전부
원본 기사 메타데이터만으로 구할 수 있다. LLM 분석은 이 단계를 통과한
클러스터에 대해서만 수행되므로, 호출 수와 토큰이 크게 줄어든다.
"""

from __future__ import annotations

import logging
import re

from models.schema import RawArticle

logger = logging.getLogger(__name__)

RELIABILITY_TIER_RANK = {"very_high": 4, "high": 3, "medium": 2, "low": 1, "unverified": 0}

# 사전 점수(LLM 없이 구하는 중요도) 가중치.
#
# ranker의 최종 점수와 달리 교차 보도에 훨씬 큰 비중을 준다. 이 점수는
# "키워드에 안 걸렸지만 이슈화된 것"을 고르는 데만 쓰이는데, 신뢰도 비중이 높으면
# 신뢰도 높은 매체의 단신(지역 행사 같은)이 여러 매체가 동시에 다룬 사건을
# 이겨버리기 때문이다. 몇 곳이 같이 보도했는지가 이슈화의 직접 신호다.
PRESCORE_WEIGHT_RELIABILITY = 0.35
PRESCORE_WEIGHT_CORROBORATION = 0.65

_ASCII_TOKEN = re.compile(r"^[A-Za-z0-9.&+\-]+$")


def _compile_token(token: str) -> re.Pattern:
    """ASCII 토큰에는 단어 경계를 강제한다.

    이게 없으면 'AI'가 said / again / email / campaign 같은 단어 속에서
    걸려버려서, 영문 기사가 죄다 관심사로 분류된다. 한글 토큰은 조사가
    붙는 특성상 부분 일치가 오히려 맞으므로(경제 -> 경제성장률) 그대로 둔다.
    """
    if _ASCII_TOKEN.match(token):
        return re.compile(
            rf"(?<![A-Za-z0-9]){re.escape(token)}(?![A-Za-z0-9])", re.IGNORECASE
        )
    return re.compile(re.escape(token))


class InterestMatcher:
    """interests.yaml의 키워드를 정규식으로 미리 컴파일해두고 재사용한다.

    공백이 들어간 키워드("미국 정책")는 토큰이 전부 등장해야 매칭으로 본다.
    단순 부분 문자열로 보면 "미국의 정책" 같은 표현을 놓치기 때문이다.

    aliases에 다른 표기를 적어두면 그중 하나만 걸려도 매칭으로 친다. 한국 기사는
    'NVIDIA'가 아니라 '엔비디아'라고 쓰기 때문에, 이게 없으면 국내 기사를 통째로
    놓친다. 표시는 대표 keyword 하나로 통일된다.
    """

    def __init__(self, interests_cfg: dict):
        self.entries = []
        for item in interests_cfg.get("interests", []):
            keyword = item.get("keyword", "").strip()
            if not keyword:
                continue
            spellings = [keyword] + [a.strip() for a in item.get("aliases", []) if a.strip()]
            self.entries.append(
                {
                    "keyword": keyword,
                    "weight": float(item.get("weight", 1.0)),
                    # 표기별로 토큰 패턴 묶음을 하나씩 만든다.
                    # 표기 안에서는 AND(토큰 전부), 표기끼리는 OR.
                    "spellings": [[_compile_token(t) for t in s.split()] for s in spellings],
                }
            )
        # 가중치가 큰 키워드가 앞에 오도록 정렬 (그룹핑 기준으로도 쓰인다)
        self.entries.sort(key=lambda e: -e["weight"])

    @staticmethod
    def _matches(entry: dict, text: str) -> bool:
        return any(
            all(p.search(text) for p in patterns) for patterns in entry["spellings"]
        )

    def match(self, text: str) -> list[str]:
        """등장한 키워드 목록을 가중치 내림차순으로 반환."""
        return [e["keyword"] for e in self.entries if self._matches(e, text)]

    def weight_of(self, keyword: str) -> float:
        for e in self.entries:
            if e["keyword"] == keyword:
                return e["weight"]
        return 0.0

    def score(self, text: str) -> tuple[float, list[str]]:
        """0~100 스케일의 개인화 점수와 매칭된 키워드."""
        matched = self.match(text)
        if not matched:
            return 0.0, []
        total = sum(self.weight_of(k) for k in matched)
        # 대략 가중치 합 5 정도면 만점으로 본다
        return min(100.0, total / 5.0 * 100.0), matched

    def keywords_in_order(self) -> list[str]:
        return [e["keyword"] for e in self.entries]


def reliability_stats(
    source_ids: list[str], source_registry: dict[str, dict]
) -> tuple[float, int, bool]:
    """(가장 높은 소스 신뢰도, high 이상 등급 소스 수, 전부 저신뢰인지) 반환."""
    scores = []
    high_tier_count = 0
    all_low = True

    for sid in source_ids:
        cfg = source_registry.get(sid, {})
        rel = cfg.get("reliability", {})
        scores.append(rel.get("base_score", 50))
        tier = rel.get("tier", "unverified")
        if RELIABILITY_TIER_RANK.get(tier, 0) >= RELIABILITY_TIER_RANK["high"]:
            high_tier_count += 1
        if RELIABILITY_TIER_RANK.get(tier, 0) > RELIABILITY_TIER_RANK["low"]:
            all_low = False

    if not scores:
        return 50.0, 0, True
    return max(scores), high_tier_count, all_low


def cluster_text(cluster: list[RawArticle]) -> str:
    """클러스터에 속한 원본 기사의 제목/요약을 이어 붙인 검색용 텍스트.

    category_tags는 일부러 뺐다. rss_collector가 sources.yaml의 '소스 단위'
    카테고리를 기사마다 그대로 복사해 넣기 때문에(연합뉴스 기사 = 전부 경제/정치/사회),
    이걸 매칭에 쓰면 '경제' 같은 키워드가 해당 매체 기사 전부에 걸려버린다.
    """
    parts = []
    for a in cluster:
        parts.append(a.title)
        parts.append(a.summary)
    return " ".join(p for p in parts if p)


def triage_clusters(
    clusters: list[list[RawArticle]],
    source_registry: dict[str, dict],
    interests_cfg: dict,
    budget: int | None = None,
) -> list[dict]:
    """LLM으로 넘길 클러스터를 골라 버킷 라벨과 함께 반환한다.

    budget은 오늘 쓸 수 있는 LLM 호출 수다. 무료 티어에서는 이게 실질적인
    천장이라, 넘칠 때 무엇을 포기할지가 중요하다. 관심사를 먼저 채우고
    남는 만큼만 그 외 이슈에 쓴다 — 관심사가 이 브리핑의 존재 이유이므로.

    반환 항목: {"cluster", "bucket", "matched_keywords", "prescore"}
    bucket은 "interest" 또는 "general".
    """
    matcher = InterestMatcher(interests_cfg)
    # 두 상한 모두 null(=제한 없음)이 기본이다. 개수로 자르면 "오늘 이슈가 많은 날"에
    # 정작 볼 게 잘려나가므로, 무엇을 볼지는 개수가 아니라 기준(키워드 매칭 / 신뢰도)으로
    # 정하고 상한은 사고 방지용으로만 남겨둔다.
    max_interest = interests_cfg.get("max_interest_events")
    top_general = interests_cfg.get("top_general_events")
    exclude_low = interests_cfg.get("exclude_low_reliability_only_from_top", True)

    interest: list[dict] = []
    general: list[dict] = []

    for cluster in clusters:
        matched = matcher.match(cluster_text(cluster))
        source_ids = list(dict.fromkeys(a.source_id for a in cluster))
        reliability, high_tier_count, all_low = reliability_stats(source_ids, source_registry)
        corroboration = min(100.0, high_tier_count * 25.0)
        prescore = (
            PRESCORE_WEIGHT_RELIABILITY * reliability
            + PRESCORE_WEIGHT_CORROBORATION * corroboration
        )

        item = {
            "cluster": cluster,
            "matched_keywords": matched,
            "prescore": round(prescore, 1),
            "all_low": all_low,
            "n_sources": len(source_ids),
        }
        (interest if matched else general).append(item)

    # 관심사: 키워드 가중치 -> 사전 점수 순
    interest.sort(
        key=lambda it: (
            -max(matcher.weight_of(k) for k in it["matched_keywords"]),
            -it["prescore"],
        )
    )
    # 그 외: 사전 점수 순. 저신뢰 소스 단독 사건은 여기서 제외한다
    # (관심사 버킷에는 이 필터를 적용하지 않는다 — 관심 키워드는 빠짐없이 보는 게 목적)
    if exclude_low:
        dropped_low = sum(1 for it in general if it["all_low"])
        general = [it for it in general if not it["all_low"]]
        if dropped_low:
            logger.info("  그 외 후보 중 저신뢰 소스 단독 %d개 제외", dropped_low)
    general.sort(key=lambda it: -it["prescore"])

    if max_interest is not None and len(interest) > max_interest:
        logger.warning(
            "  관심사 클러스터 %d개 중 상위 %d개만 분석합니다 (max_interest_events 상한). "
            "%d개가 제외되니 상한을 올리거나 키워드를 좁혀주세요.",
            len(interest),
            max_interest,
            len(interest) - max_interest,
        )
        interest = interest[:max_interest]

    if top_general is not None and len(general) > top_general:
        logger.info(
            "  그 외 클러스터 %d개 중 사전 점수 상위 %d개만 분석합니다 (top_general_events 상한).",
            len(general),
            top_general,
        )
        general = general[:top_general]

    # LLM 호출 예산 배분: 관심사를 먼저 채우고 남는 만큼만 그 외 이슈에 쓴다.
    if budget is not None and len(interest) + len(general) > budget:
        if len(interest) >= budget:
            logger.warning(
                "  ⚠ 관심사 클러스터가 %d개인데 오늘 쓸 수 있는 LLM 호출은 %d회뿐입니다. "
                "상위 %d개만 분석하고 관심사 %d개와 그 외 %d개를 전부 포기합니다.",
                len(interest),
                budget,
                budget,
                len(interest) - budget,
                len(general),
            )
            interest = interest[:budget]
            general = []
        else:
            room = budget - len(interest)
            logger.info(
                "  LLM 호출 예산 %d회: 관심사 %d개를 먼저 채우고 그 외 %d개 중 상위 %d개를 씁니다.",
                budget,
                len(interest),
                len(general),
                room,
            )
            general = general[:room]

    for it in interest:
        it["bucket"] = "interest"
    for it in general:
        it["bucket"] = "general"

    selected = interest + general
    logger.info(
        "사전 선별 완료: 전체 클러스터 %d개 -> LLM 분석 %d개 (관심사 %d + 주요이슈 %d)",
        len(clusters),
        len(selected),
        len(interest),
        len(general),
    )
    return selected
