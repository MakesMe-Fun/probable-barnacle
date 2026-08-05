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

PROMPT_TEMPLATE = """다음은 여러 언론사가 '같은 하나의 사건'에 대해 보도한 기사들이야. \
(어느 소스가 몇 개인지는 신경 쓰지 말고, 사건 자체에만 집중해서 분석해줘)

{articles_text}

이 기사들을 종합해서 아래 JSON 스키마 형식으로만 응답해. 다른 설명, 코드블록 표시(```) 없이 \
순수 JSON 객체 하나만 출력해.

{{
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
}}

주의사항:
- glossary는 생소한 용어가 있을 때 최대한 채워줘 (보통 2~5개). 정말 없으면 빈 배열.
- entities는 실제로 기사에 등장하거나 사건에 직접 관련된 것만. 없는 카테고리는 빈 배열로.
- 모든 내용은 일반인도 이해할 수 있는 친절한 한국어로 작성. 기사가 영어여도 한국어로 작성해줘.
- 반드시 순수 JSON 객체만 출력하고, 앞뒤에 다른 텍스트를 붙이지 마."""


def _strip_code_fence(raw: str) -> str:
    return re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())


def analyze_cluster(client, cluster: list[RawArticle]) -> dict | None:
    if not cluster:
        return None

    articles_text = "\n\n".join(
        f"[기사 {i+1} / 출처: {a.source_name}] {a.title}\n{a.summary}\n링크: {a.url}"
        for i, a in enumerate(cluster)
    )
    prompt = PROMPT_TEMPLATE.format(articles_text=articles_text)

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
