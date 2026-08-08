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

# 겹치는 엔티티 이름이 이 개수 이상이면 같은 사건의 업데이트로 간주.
#
# 1이었을 때는 첫 실행에서 79건 중 78건이 "업데이트"로 찍혔다. "한국", "미국",
# "구글" 같은 흔한 엔티티가 하나만 겹쳐도 매칭돼서다. 서로 다른 사건이 같은
# story로 묶이는 것도 같은 원인이라 2로 올린다.
ENTITY_OVERLAP_MATCH_THRESHOLD = 2
LOOKBACK_DAYS = 5


def init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def _recent_events(
    conn: sqlite3.Connection,
    lookback_days: int = LOOKBACK_DAYS,
    before: str | None = None,
) -> list[dict]:
    """최근 lookback_days 안에 저장된 이벤트.

    before(이번 실행 시작 시각)를 주면 그 이후에 저장된 건 제외한다. 이게 없으면
    같은 실행에서 방금 저장한 이벤트끼리 비교하게 되어, 첫 브리핑부터 거의 전부가
    '업데이트'로 찍힌다. "업데이트"는 지난 브리핑에서 이미 본 사건이라는 뜻이어야 한다.
    """
    cutoff = (date.today() - timedelta(days=lookback_days)).isoformat()
    sql = "SELECT id, story_id, title, entities_json, category_tags_json FROM events WHERE event_date >= ?"
    params: list = [cutoff]
    if before:
        sql += " AND created_at < ?"
        params.append(before)
    rows = conn.execute(sql, params).fetchall()
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
    conn: sqlite3.Connection,
    event_payload: dict,
    entity_names: list[str],
    run_started_at: str | None = None,
) -> tuple[str, str]:
    """entity 겹침 기준으로 신규/업데이트를 판별하고, 매칭되면 기존 story_id를
    재사용하며, 매칭되지 않으면 새 story_id를 만들어 반환한다.

    run_started_at을 넘기면 이번 실행에서 저장한 이벤트는 비교 대상에서 뺀다.
    """
    recent = _recent_events(conn, before=run_started_at)
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
