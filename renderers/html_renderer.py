"""
기존 news_briefing.py의 에디토리얼 스타일 HTML 템플릿을 그대로 계승하되,
입력을 "카테고리별 issue dict"에서 "정렬된 Event payload 리스트"로 바꿨다.

렌더링 구조:
  1) 상단 "한눈에 보기" — 전체 이벤트의 제목만 모은 목차 (본문 카드로 점프)
  2) 관심사 섹션 — interests.yaml 키워드별로 그룹핑, 걸린 건 전부 노출
  3) "그 외 주요 이슈" 섹션 — 키워드엔 안 걸렸지만 중요도가 높은 이벤트

"관심사는 개수를 자르지 않고 전부, 그 외는 중요한 것만"이 이 구조의 핵심이라
예전처럼 TOP N만 뽑아 보여주는 보드는 두지 않는다. 대신 목차로 길이를 감당한다.

카드에는 신뢰도(reliability_score)와 NEW/UPDATE 배지, 참여 소스 개수,
매칭된 관심 키워드를 표시한다.
"""

from __future__ import annotations

from datetime import datetime

from pipeline.prefilter import InterestMatcher

GENERAL_SECTION_TITLE = "그 외 주요 이슈"


def _badge_issue_type(issue_type: str) -> str:
    if issue_type == "update":
        return '<span class="badge badge-update">UPDATE</span>'
    return '<span class="badge badge-new">NEW</span>'


def _reliability_label(score: float) -> str:
    if score >= 90:
        return "매우 높음"
    if score >= 75:
        return "높음"
    if score >= 55:
        return "보통"
    return "낮음(참고용)"


def render_glossary(glossary) -> str:
    if not glossary:
        return ""
    items = "".join(
        f'<div class="gloss-item"><span class="gloss-term">{g.term}</span>'
        f'<span class="gloss-def">{g.explanation}</span></div>'
        for g in glossary
    )
    return f"""
    <div class="block">
      <div class="block-label">알아두면 좋은 용어</div>
      <div class="glossary">{items}</div>
    </div>"""


def render_background_knowledge(text: str) -> str:
    if not text:
        return ""
    return f"""
    <div class="block bg-knowledge">
      <div class="block-label">더 알아두면 좋은 배경지식</div>
      <p>{text}</p>
    </div>"""


def render_entities(entities) -> str:
    names = entities.all_names()
    if not names:
        return ""
    chips = "".join(f'<span class="entity-chip">{n}</span>' for n in names[:8])
    return f'<div class="entities">{chips}</div>'


def render_interest_tags(interest_tags: list[str]) -> str:
    if not interest_tags:
        return ""
    chips = "".join(f'<span class="kw-chip">{t}</span>' for t in interest_tags)
    return f'<span class="kw-chips">{chips}</span>'


def render_event_card(payload: dict) -> str:
    glossary_html = render_glossary(payload["glossary"])
    bg_knowledge_html = render_background_knowledge(payload["background_knowledge"])
    entities_html = render_entities(payload["entities"])
    link = payload["source_links"][0] if payload["source_links"] else "#"
    n_sources = len(payload["source_ids"])

    return f"""
  <article class="card" id="{payload["anchor"]}">
    <div class="card-meta">
      {_badge_issue_type(payload["issue_type"])}
      <span class="meta-item">신뢰도 {_reliability_label(payload["reliability_score"])}</span>
      <span class="meta-item">소스 {n_sources}곳</span>
      {render_interest_tags(payload.get("interest_tags", []))}
    </div>
    <h2>{payload["title"]}</h2>
    <p class="tldr">{payload.get("tldr", "")}</p>
    {entities_html}

    <div class="block">
      <div class="block-label">배경</div>
      <p>{payload["background"]}</p>
    </div>

    <div class="block">
      <div class="block-label">무슨 일이 있었나</div>
      <p>{payload["details"]}</p>
    </div>
    {bg_knowledge_html}
    {glossary_html}
    <div class="block">
      <div class="block-label">찬반 관점</div>
      <div class="stance">
        <div class="stance-box">
          <div class="label">기대·지지하는 입장</div>
          <p>{payload["support_view"]}</p>
        </div>
        <div class="stance-box">
          <div class="label">우려·비판하는 입장</div>
          <p>{payload["concern_view"]}</p>
        </div>
      </div>
    </div>

    <div class="outlook">
      <b>전망</b> — {payload["outlook"]}
    </div>

    <a class="source" href="{link}" target="_blank">원문 보기 →</a>
  </article>"""


def group_payloads(sorted_payloads: list[dict], interests_cfg: dict) -> list[tuple[str, list[dict]]]:
    """(그룹명, 이벤트들) 목록을 만든다.

    관심사 버킷은 매칭된 키워드 중 하나를 대표로 삼아 그룹을 나눈다. 한 이벤트가
    여러 키워드에 걸려도 카드가 중복 노출되지 않게 하기 위해서다
    (나머지 키워드는 카드 안에 칩으로 표시된다).

    대표는 "오늘 가장 적게 등장한 키워드"로 고른다. 가중치가 같을 때 포괄적인
    키워드가 이기면(AI vs Anthropic) 특정 키워드 섹션이 텅 비고 AI 섹션만
    비대해져서, 키워드별로 나눈 의미가 사라지기 때문이다.
    그룹 순서는 interests.yaml에 적힌 가중치 순서를 따른다.
    """
    matcher = InterestMatcher(interests_cfg)

    def is_interest(p: dict) -> bool:
        return p.get("bucket") == "interest" and bool(p.get("interest_tags"))

    interest_payloads = [p for p in sorted_payloads if is_interest(p)]
    general = [p for p in sorted_payloads if not is_interest(p)]

    frequency: dict[str, int] = {}
    for p in interest_payloads:
        for tag in p["interest_tags"]:
            frequency[tag] = frequency.get(tag, 0) + 1

    interest_groups: dict[str, list[dict]] = {}
    for p in interest_payloads:
        representative = min(
            p["interest_tags"], key=lambda t: (frequency[t], -matcher.weight_of(t))
        )
        interest_groups.setdefault(representative, []).append(p)

    groups = [
        (keyword, interest_groups[keyword])
        for keyword in matcher.keywords_in_order()
        if keyword in interest_groups
    ]
    if general:
        groups.append((GENERAL_SECTION_TITLE, general))
    return groups


def render_index(groups: list[tuple[str, list[dict]]]) -> str:
    """전체 이벤트 제목만 모은 목차.

    관심사를 전부 싣다 보니 페이지가 길어져서, 위에서 한 번에 훑고
    원하는 카드로 바로 내려갈 수 있게 한다.
    """
    blocks = []
    for gi, (name, payloads) in enumerate(groups, start=1):
        items = "".join(
            f'<a class="idx-item" href="#{p["anchor"]}">'
            f'<span class="idx-dot"></span>{p["title"]}</a>'
            for p in payloads
        )
        blocks.append(
            f'<div class="idx-group"><div class="idx-group-name">'
            f'<a href="#cat-{gi}">{name}</a> <span class="idx-count">{len(payloads)}</span>'
            f"</div>{items}</div>"
        )
    return f"""
<section class="category" id="cat-index">
  <div class="cat-label">
    <span class="cat-index">★</span>
    <span class="cat-name">한눈에 보기</span>
  </div>
  <div class="index">{"".join(blocks)}</div>
</section>"""


def render_sections(groups: list[tuple[str, list[dict]]]) -> tuple[str, str]:
    sections = []
    for i, (name, payloads) in enumerate(groups, start=1):
        cards = "".join(render_event_card(p) for p in payloads)
        sections.append(
            f"""
<section class="category" id="cat-{i}">
  <div class="cat-label">
    <span class="cat-index">{i:02d}</span>
    <span class="cat-name">{name}</span>
    <span class="cat-count">{len(payloads)}건</span>
  </div>
  {cards}
</section>"""
        )
    nav_chips = "".join(
        f'<a class="chip" href="#cat-{i}">{name}</a>' for i, (name, _) in enumerate(groups, start=1)
    )
    return "".join(sections), nav_chips


HTML_SHELL = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>오늘의 브리핑</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Noto+Serif+KR:wght@600;700;900&family=Pretendard&display=swap" rel="stylesheet">
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  :root{{
    --bg:#0B0B0C; --paper:#141416; --ink:#EDEDED; --ink-soft:#9A9A9E; --rule:#2B2B2E;
    --accent:#D8C08A;
  }}
  body{{ background:var(--bg); color:var(--ink); font-family:'Pretendard', -apple-system, sans-serif;
    max-width:480px; margin:0 auto; padding-bottom:60px; }}
  header.top{{ position:sticky; top:0; z-index:10; background:var(--bg); padding:26px 20px 0;
    border-bottom:1px solid var(--rule); }}
  .date{{ font-size:11px; letter-spacing:0.14em; color:var(--ink-soft); text-transform:uppercase; }}
  h1.masthead{{ font-family:'Noto Serif KR', serif; font-weight:900; font-size:30px; margin-top:6px;
    letter-spacing:-0.02em; padding-bottom:16px; }}
  .chips{{ display:flex; gap:0; border-top:1px solid var(--rule); overflow-x:auto; }}
  .chip{{ flex:1; text-align:center; font-size:11px; font-weight:600; letter-spacing:0.03em;
    padding:10px 6px; color:var(--ink-soft); text-decoration:none; white-space:nowrap; }}
  section.category{{ padding:32px 20px 6px; scroll-margin-top:90px; }}
  .cat-label{{ display:flex; align-items:baseline; gap:10px; margin-bottom:18px;
    border-bottom:1px solid var(--rule); padding-bottom:10px; }}
  .cat-index{{ font-family:'Noto Serif KR', serif; font-size:12px; color:var(--ink-soft); }}
  .cat-name{{ font-family:'Noto Serif KR', serif; font-weight:700; font-size:16px; letter-spacing:0.02em; }}
  .cat-count{{ margin-left:auto; font-size:11px; color:var(--ink-soft); }}
  .index{{ display:flex; flex-direction:column; gap:18px; }}
  .idx-group-name{{ font-size:11px; font-weight:700; letter-spacing:0.1em; color:var(--accent);
    margin-bottom:7px; text-transform:uppercase; }}
  .idx-group-name a{{ color:var(--accent); text-decoration:none; }}
  .idx-count{{ color:var(--ink-soft); font-weight:600; letter-spacing:0; }}
  .idx-item{{ display:flex; align-items:baseline; gap:8px; font-size:13px; line-height:1.55;
    color:var(--ink); text-decoration:none; padding:4px 0; }}
  .idx-dot{{ flex:none; width:3px; height:3px; border-radius:50%; background:var(--ink-soft);
    transform:translateY(-3px); }}
  .kw-chips{{ display:inline-flex; gap:4px; flex-wrap:wrap; }}
  .kw-chip{{ font-size:10px; font-weight:600; padding:2px 6px; border-radius:2px;
    background:rgba(216,192,138,0.14); color:var(--accent); }}
  article.card{{ border-bottom:1px solid var(--rule); padding:0 0 24px; margin-bottom:24px; }}
  .card-meta{{ display:flex; align-items:center; gap:8px; margin-bottom:10px; flex-wrap:wrap; }}
  .badge{{ font-size:10px; font-weight:700; padding:2px 7px; border-radius:2px; letter-spacing:0.05em; }}
  .badge-new{{ background:var(--accent); color:#1a1a1a; }}
  .badge-update{{ background:#3a3a3d; color:var(--ink); }}
  .meta-item{{ font-size:11px; color:var(--ink-soft); }}
  .card h2{{ font-family:'Noto Serif KR', serif; font-weight:700; font-size:19px; line-height:1.4;
    margin-bottom:6px; letter-spacing:-0.01em; }}
  .tldr{{ font-size:13px; color:var(--ink-soft); font-style:italic; margin-bottom:10px; }}
  .entities{{ margin-bottom:14px; display:flex; flex-wrap:wrap; gap:6px; }}
  .entity-chip{{ font-size:11px; padding:3px 8px; border:1px solid var(--rule); border-radius:12px;
    color:var(--ink-soft); }}
  .block{{ margin-bottom:14px; }}
  .block-label{{ font-size:10px; font-weight:700; letter-spacing:0.12em; color:var(--ink-soft);
    margin-bottom:6px; text-transform:uppercase; }}
  .block p{{ font-size:14px; line-height:1.75; color:var(--ink); }}
  .bg-knowledge{{ border-left:2px solid var(--rule); padding-left:12px; }}
  .bg-knowledge p{{ color:var(--ink-soft); font-style:italic; }}
  .glossary{{ border:1px solid var(--rule); border-radius:2px; }}
  .gloss-item{{ display:flex; flex-direction:column; padding:9px 12px; border-bottom:1px solid var(--rule); }}
  .gloss-item:last-child{{ border-bottom:none; }}
  .gloss-term{{ font-size:12px; font-weight:700; color:var(--ink); margin-bottom:2px; }}
  .gloss-def{{ font-size:12.5px; line-height:1.55; color:var(--ink-soft); }}
  .stance{{ display:grid; grid-template-columns:1fr 1fr; gap:1px; background:var(--rule); margin-top:6px;
    border:1px solid var(--rule); }}
  .stance-box{{ background:var(--paper); padding:12px 13px; }}
  .stance-box .label{{ font-size:10px; font-weight:700; letter-spacing:0.06em; margin-bottom:5px;
    color:var(--ink-soft); text-transform:uppercase; }}
  .stance-box p{{ font-size:13px; line-height:1.6; color:var(--ink); }}
  .outlook{{ font-size:13px; line-height:1.7; color:var(--ink-soft); padding-top:12px; margin-top:2px;
    border-top:1px dashed var(--rule); }}
  .outlook b{{ color:var(--ink); font-weight:600; }}
  a.source{{ display:inline-block; margin-top:14px; font-size:12px; color:var(--ink-soft);
    text-decoration:none; border-bottom:1px solid var(--ink-soft); padding-bottom:1px; }}
  footer{{ text-align:center; padding:30px 20px; font-size:11px; color:var(--ink-soft); }}
</style>
</head>
<body>
<header class="top">
  <div class="date">{date_str}</div>
  <h1 class="masthead">오늘의 브리핑</h1>
  <nav class="chips">
    <a class="chip" href="#cat-index">목차</a>
    {nav_chips}
  </nav>
</header>
{index}
{sections}
<footer>개인 AI 뉴스 비서 · {n_sources_used}개 소스 · 이벤트 {n_events}건 (관심사 {n_interest}건)</footer>
</body>
</html>"""


def build_html(sorted_payloads: list[dict], interests_cfg: dict, n_sources_used: int) -> str:
    weekday = ["월", "화", "수", "목", "금", "토", "일"][datetime.now().weekday()]
    date_str = datetime.now().strftime(f"%Y년 %m월 %d일 ({weekday}) · 브리핑")

    for i, p in enumerate(sorted_payloads, start=1):
        p["anchor"] = f"ev-{i}"

    groups = group_payloads(sorted_payloads, interests_cfg)
    index_html = render_index(groups)
    sections, nav_chips = render_sections(groups)

    n_interest = sum(1 for p in sorted_payloads if p.get("bucket") == "interest")

    return HTML_SHELL.format(
        date_str=date_str,
        nav_chips=nav_chips,
        index=index_html,
        sections=sections,
        n_sources_used=n_sources_used,
        n_events=len(sorted_payloads),
        n_interest=n_interest,
    )
