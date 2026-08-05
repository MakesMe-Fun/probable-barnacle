"""
SQLite 기반 영속 저장소.

- events / stories : 매일 생성되는 Event/Story 히스토리
- interaction_log   : "관심사 자동 학습"과 "추적 버튼"이 나중에 붙을 때 쓸
                       사용자 행동 로그. MVP에서는 아무도 안 써도 되지만,
                       테이블을 지금 만들어둬야 나중에 과거 데이터 없이
                       바로 학습을 시작할 수 있다.
- briefing_run      : "지난 브리핑 이후 변경된 내용만 보기"를 위한
                       마지막 브리핑 생성/열람 시각 기록.

신규/업데이트 판별(issue_type)은 임베딩 유사도 대신, 최근 며칠간 저장된
이벤트와의 "핵심 엔티티 겹침"으로 가볍게 판단한다. Event -> Story 연결의
1차 구현으로도 겸한다 (설계 문서 4절: Event->Story는 처음엔 규칙 기반 추천).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, date, timedelta

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    story_id TEXT,
    title TEXT,
    tldr TEXT,
    background TEXT,
    details TEXT,
    background_knowledge TEXT,
    glossary_json TEXT,
    support_view TEXT,
    concern_view TEXT,
    outlook TEXT,
    entities_json TEXT,
    category_tags_json TEXT,
    interest_tags_json TEXT,
    issue_type TEXT,
    reliability_score REAL,
    importance_score REAL,
    source_links_json TEXT,
    event_date TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS stories (
    id TEXT PRIMARY KEY,
    title TEXT,
    description TEXT,
    entities_json TEXT,
    category_tags_json TEXT,
    event_ids_json TEXT,
    status TEXT DEFAULT 'ongoing',
    user_tracked INTEGER DEFAULT 0,
    first_seen_at TEXT,
    last_updated_at TEXT
);

CREATE TABLE IF NOT EXISTS interaction_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT,
    story_id TEXT,
    action TEXT,          -- 'viewed' | 'clicked' | 'tracked' | 'untracked' | 'dismissed'
    timestamp TEXT
);

CREATE TABLE IF NOT EXISTS briefing_run (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    generated_at TEXT,
    viewed_at TEXT
);
"""

ENTITY_OVERLAP_MATCH_THRESHOLD = 1  # 겹치는 엔티티 이름이 이 개수 이상이면 같은 사건의 업데이트로 간주
LOOKBACK_DAYS = 5

# 참고: 지금은 엔티티 이름 1개만 겹쳐도 업데이트로 판정하는 단순 규칙이다.
# entities 추출이 비교적 구체적인 고유명사(회사/인물명) 위주라 오탐이 크지 않지만,
# "Google", "미국"처럼 흔한 엔티티가 다수 이벤트에 등장하면 서로 다른 사건이 같은
# story로 묶이는 오탐이 늘어날 수 있다. 실사용하면서 오탐이 잦으면 이 값을 2로
# 올리거나, 엔티티 겹침 대신(혹은 함께) 제목 임베딩 유사도를 추가로 결합하는 걸
# 추천한다 (설계 문서 4절의 Event->Story 연결 고도화와 같은 방향).


def init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def _recent_events(conn: sqlite3.Connection, lookback_days: int = LOOKBACK_DAYS) -> list[dict]:
    cutoff = (date.today() - timedelta(days=lookback_days)).isoformat()
    rows = conn.execute(
        "SELECT id, story_id, title, entities_json, category_tags_json FROM events WHERE event_date >= ?",
        (cutoff,),
    ).fetchall()
    result = []
    for r in rows:
        result.append(
            {
                "id": r[0],
                "story_id": r[1],
                "title": r[2],
                "entity_names": set(json.loads(r[3] or "[]")),
                "category_tags": set(json.loads(r[4] or "[]")),
            }
        )
    return result


def determine_issue_type_and_story(
    conn: sqlite3.Connection, event_payload: dict, entity_names: list[str]
) -> tuple[str, str]:
    """entity 겹침 기준으로 신규/업데이트를 판별하고, 매칭되면 기존 story_id를
    재사용하며, 매칭되지 않으면 새 story_id를 만들어 반환한다.
    """
    recent = _recent_events(conn)
    this_entities = set(entity_names)

    best_match = None
    best_overlap = 0
    for ev in recent:
        overlap = len(this_entities & ev["entity_names"])
        if overlap > best_overlap:
            best_overlap = overlap
            best_match = ev

    if best_match and best_overlap >= ENTITY_OVERLAP_MATCH_THRESHOLD:
        story_id = best_match["story_id"] or f"story_{best_match['id']}"
        return "update", story_id

    from models.schema import new_id

    return "new", new_id("story")


def save_event(conn: sqlite3.Connection, event_payload: dict) -> None:
    entities = event_payload["entities"]
    conn.execute(
        """
        INSERT OR REPLACE INTO events (
            id, story_id, title, tldr, background, details, background_knowledge,
            glossary_json, support_view, concern_view, outlook, entities_json,
            category_tags_json, interest_tags_json, issue_type, reliability_score,
            importance_score, source_links_json, event_date, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            event_payload["id"],
            event_payload["story_id"],
            event_payload["title"],
            event_payload["tldr"],
            event_payload["background"],
            event_payload["details"],
            event_payload["background_knowledge"],
            json.dumps([g.__dict__ for g in event_payload["glossary"]], ensure_ascii=False),
            event_payload["support_view"],
            event_payload["concern_view"],
            event_payload["outlook"],
            json.dumps(entities.all_names(), ensure_ascii=False),
            json.dumps(event_payload["category_tags"], ensure_ascii=False),
            json.dumps(event_payload["interest_tags"], ensure_ascii=False),
            event_payload["issue_type"],
            event_payload["reliability_score"],
            event_payload["importance_score"],
            json.dumps(event_payload["source_links"], ensure_ascii=False),
            event_payload["event_date"],
            event_payload["created_at"],
        ),
    )
    conn.commit()


def record_briefing_run(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO briefing_run (generated_at, viewed_at) VALUES (?, NULL)",
        (datetime.now().isoformat(),),
    )
    conn.commit()


def log_interaction(conn: sqlite3.Connection, event_id: str, story_id: str | None, action: str) -> None:
    """향후 UI에서 호출할 훅. 관심사 자동 학습 / 추적 버튼의 데이터 기반이 된다."""
    conn.execute(
        "INSERT INTO interaction_log (event_id, story_id, action, timestamp) VALUES (?,?,?,?)",
        (event_id, story_id, action, datetime.now().isoformat()),
    )
    conn.commit()
