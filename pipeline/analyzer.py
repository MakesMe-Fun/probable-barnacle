"""
클러스터(같은 사건을 다루는 Article 묶음) 단위로 LLM을 호출해
Event의 서술형 필드를 채운다.

어떤 제공자(Gemini/Groq)를 쓰는지는 llm_client가 정하고, 여기서는
"프롬프트를 주면 JSON 문자열이 온다"까지만 안다.

기존 news_briefing.py의 PROMPT_TEMPLATE 설계 철학(배경/설명/용어해설/찬반/전망)을
그대로 계승하되, 다음을 추가했다:
  - tldr: "3줄 요약" 모드용 한 문장 핵심 요약
  - entities: 구조화된 관련 기업/인물/국가/기관 (주식·정책 자동 연결의 기반)
  - category_tags: 이 사건이 속하는 카테고리 (여러 개 가능)
"""

from __future__ import annotations

import json
import logging
import re

from models.schema import Entities, GlossaryItem, RawArticle
from pipeline.llm_client import QuotaExhausted  # noqa: F401  (호출부가 analyzer 경유로 잡는다)

logger = logging.getLogger(__name__)

# 출력 길이 상한은 제공자마다 유불리가 정반대라(Gemini는 요청 수 과금이라 넉넉히,
# Groq은 예약분을 토큰 한도에서 깎으니 빠듯하게) llm_client 쪽에서 정한다.
TEMPERATURE = 0.4

EVENT_SCHEMA = """{{
  "title": "이슈를 압축한 헤드라인 (원 기사 제목을 그대로 베끼지 말 것)",
  "tldr": "이 사건을 정말 한 문장으로 요약 (3줄 요약/30초 요약 모드에서 사용됨)",
  "background": "이 이슈가 왜 나오게 됐는지 배경을 3~5문장으로 설명",
  "details": "정확히 무슨 일이 일어났는지 6~10문장으로 아주 상세하게 설명",
  "background_knowledge": "이 이슈를 이해하는 데 필요한 더 넓은 배경지식 3~5문장. 없으면 빈 문자열",
  "glossary": [
    {{"term": "전문용어", "explanation": "쉬운 설명 2~3문장"}}
  ],
  "support_view": "긍정/지지 관점과 근거 2~4문장",
  "concern_view": "우려/비판 관점과 근거 2~4문장",
  "outlook": "앞으로의 전개 전망 3~4문장",
  "category_tags": ["이 사건이 속하는 카테고리 (정치/경제/IT·과학/사회/국제/AI/LLM 등 자유롭게, 1~3개)"],
  "entities": {{
    "companies": [{{"name": "관련 기업명"}}],
    "people": [{{"name": "관련 인물명"}}],
    "countries": [{{"name": "관련 국가명"}}],
    "organizations": [{{"name": "관련 기관/단체명"}}]
  }}
}}"""

COMMON_RULES = """주의사항:
- glossary는 생소한 용어가 있을 때 최대한 채워줘 (보통 2~5개). 정말 없으면 빈 배열.
- entities는 실제로 기사에 등장하거나 사건에 직접 관련된 것만. 없는 카테고리는 빈 배열로.
- 모든 내용은 일반인도 이해할 수 있는 친절한 한국어로 작성. 기사가 영어여도 한국어로 작성해줘."""

SINGLE_PROMPT = """다음은 여러 언론사가 '같은 하나의 사건'에 대해 보도한 기사들이야. \
(어느 소스가 몇 개인지는 신경 쓰지 말고, 사건 자체에만 집중해서 분석해줘)

{articles_text}

이 기사들을 종합해서 아래 JSON 스키마 형식으로만 응답해. 다른 설명, 코드블록 표시(```) 없이 \
순수 JSON 객체 하나만 출력해.

""" + EVENT_SCHEMA + """

""" + COMMON_RULES + """
- 반드시 순수 JSON 객체만 출력하고, 앞뒤에 다른 텍스트를 붙이지 마."""

BATCH_PROMPT = """아래에 서로 '다른' 사건 {n}개가 있어. 각 사건은 === 사건 N === 으로 구분돼 있고, \
한 사건 안의 기사들은 같은 일을 여러 언론사가 보도한 거야.

사건끼리는 서로 관계가 없으니 절대 섞지 말고, 각각 독립적으로 분석해줘.

{blocks}

JSON 배열 하나만 출력해. 배열 원소는 사건 {n}개와 1:1로 대응하고, 각 원소에 "index"로 \
사건 번호(1~{n})를 반드시 넣어줘. 사건 순서대로, 하나도 빠뜨리지 말고 {n}개를 다 채워줘.

[
  {{"index": 1, ...아래 스키마...}},
  {{"index": 2, ...}}
]

각 원소의 스키마:
""" + EVENT_SCHEMA + """

""" + COMMON_RULES + """
- 반드시 순수 JSON 배열만 출력하고, 앞뒤에 다른 텍스트를 붙이지 마."""


def _strip_code_fence(raw: str) -> str:
    return re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())


def _cluster_block(cluster: list[RawArticle]) -> str:
    return "\n\n".join(
        f"[기사 {i+1} / 출처: {a.source_name}] {a.title}\n{a.summary}\n링크: {a.url}"
        for i, a in enumerate(cluster)
    )


def analyze_cluster(client, cluster: list[RawArticle]) -> dict | None:
    if not cluster:
        return None

    prompt = SINGLE_PROMPT.format(articles_text=_cluster_block(cluster))
    raw = client.complete(prompt, temperature=TEMPERATURE)
    if raw is None:
        return None

    raw = _strip_code_fence(raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("%s 응답 JSON 파싱 실패. 원본 일부: %s", client.name, raw[:300])
        return None

    return data


def _analyze_batch(client, clusters: list[list[RawArticle]]) -> list[dict | None]:
    """여러 클러스터를 한 번의 호출로 분석한다.

    무료 티어가 '요청 수'로 제한되기 때문에(모델당 하루 20회) 사건 하나에 호출
    하나를 쓰면 하루 80건이 천장이다. 묶어 보내면 같은 호출 수로 몇 배를 처리한다.

    응답이 깨지거나 개수가 안 맞으면 절반으로 쪼개 다시 시도한다. 통째로 버리면
    사건 여러 개가 한꺼번에 날아가서다.
    """
    if not clusters:
        return []
    if len(clusters) == 1:
        return [analyze_cluster(client, clusters[0])]

    blocks = "\n\n".join(
        f"=== 사건 {i+1} ===\n{_cluster_block(c)}" for i, c in enumerate(clusters)
    )
    prompt = BATCH_PROMPT.format(n=len(clusters), blocks=blocks)

    raw = client.complete(prompt, temperature=TEMPERATURE)
    if raw is None:
        # 호출 자체가 실패한 경우(재시도까지 소진). 쪼개서 다시 불러봐야 같은 이유로
        # 실패하며 남은 호출 수만 태운다. 이 묶음은 포기한다.
        logger.warning("%s 배치 호출 실패로 사건 %d건을 건너뜁니다.", client.name, len(clusters))
        return [None] * len(clusters)

    parsed: list | None = None
    try:
        candidate = json.loads(_strip_code_fence(raw))
        if isinstance(candidate, list):
            parsed = candidate
        else:
            logger.warning("%s 배치 응답이 배열이 아닙니다.", client.name)
    except json.JSONDecodeError:
        logger.warning("%s 배치 응답 JSON 파싱 실패. 원본 일부: %s", client.name, raw[:300])

    if parsed is not None:
        results: list[dict | None] = [None] * len(clusters)
        leftovers = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            idx = item.get("index")
            if isinstance(idx, int) and 1 <= idx <= len(clusters):
                results[idx - 1] = item
            else:
                leftovers.append(item)
        # index가 빠진 응답은 남은 자리에 순서대로 채운다
        for item in leftovers:
            for i in range(len(results)):
                if results[i] is None:
                    results[i] = item
                    break
        if all(r is not None for r in results):
            return results
        missing = [i + 1 for i, r in enumerate(results) if r is None]
        logger.info("  배치 %d건 중 %s번이 비어 재시도합니다.", len(clusters), missing)

    mid = len(clusters) // 2
    return _analyze_batch(client, clusters[:mid]) + _analyze_batch(client, clusters[mid:])


def analyze_clusters(client, clusters: list[list[RawArticle]], on_progress=None) -> list[dict | None]:
    """클러스터 전체를 제공자별 배치 크기에 맞춰 나눠 분석한다.

    도중에 한도가 소진되면 거기서 멈추되, 그때까지 분석한 건 그대로 돌려준다.
    (예외를 그냥 올리면 이미 성공한 수십 건까지 함께 버려진다)
    """
    batch_size = max(1, getattr(client, "batch_size", 1))
    results: list[dict | None] = []
    for start in range(0, len(clusters), batch_size):
        chunk = clusters[start : start + batch_size]
        if on_progress:
            on_progress(start, len(chunk))
        try:
            results.extend(_analyze_batch(client, chunk))
        except QuotaExhausted as e:
            logger.error(
                "API 한도 소진으로 %d/%d에서 분석을 멈춥니다. 지금까지 분석한 %d건으로 "
                "브리핑을 만듭니다. 상세: %s",
                start,
                len(clusters),
                sum(1 for r in results if r is not None),
                str(e)[:200],
            )
            break
    results.extend([None] * (len(clusters) - len(results)))
    return results


def build_event_payload(cluster: list[RawArticle], analysis: dict) -> dict:
    """analyzer 출력(dict) + cluster(원본 기사)를 합쳐 Event 생성에 필요한
    필드를 만든다. (Event 객체 자체는 ranker/state_store 단계에서 조립)
    """
    glossary = [
        GlossaryItem(term=g.get("term", ""), explanation=g.get("explanation", ""))
        for g in analysis.get("glossary", [])
    ]
    entities = Entities.from_dict(analysis.get("entities"))

    return {
        "title": analysis.get("title", cluster[0].title),
        "tldr": analysis.get("tldr", ""),
        "background": analysis.get("background", ""),
        "details": analysis.get("details", ""),
        "background_knowledge": analysis.get("background_knowledge", ""),
        "glossary": glossary,
        "support_view": analysis.get("support_view", ""),
        "concern_view": analysis.get("concern_view", ""),
        "outlook": analysis.get("outlook", ""),
        "category_tags": analysis.get("category_tags", []) or list({t for a in cluster for t in a.category_tags}),
        "entities": entities,
        "article_ids": [a.id for a in cluster],
        "source_ids": list(dict.fromkeys(a.source_id for a in cluster)),
        "source_links": [a.url for a in cluster],
    }
