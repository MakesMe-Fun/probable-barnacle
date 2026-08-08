"""
개인 AI 뉴스 비서 — MVP 파이프라인 오케스트레이션

실행 순서:
  1) collectors.registry  : sources.yaml의 여러 소스에서 동시 수집
  2) pipeline.dedup        : 임베딩 유사도로 Article -> Event 클러스터링
  3) pipeline.prefilter    : LLM 호출 전에 관심사/주요이슈 버킷으로 사전 선별
  4) pipeline.analyzer     : 선별된 클러스터만 LLM 호출, 이슈 상세 필드 생성
     pipeline.ranker       : 신뢰도+개인화 기반 importance_score 계산 및 정렬
  5) pipeline.state_store  : SQLite에 저장, 신규/업데이트 판별
  6) renderers.html_renderer / discord_renderer : 결과 전달

실행: python main.py
필요 환경변수: GEMINI_API_KEY 또는 GROQ_API_KEY (둘 중 하나 필수, Gemini 우선),
              GEMINI_MODEL / DISCORD_WEBHOOK_URL (선택)
"""

from __future__ import annotations

import argparse
import logging
import os
import uuid
import webbrowser
from datetime import datetime, date
from pathlib import Path

from dotenv import load_dotenv

from collectors import registry as collector_registry
from embeddings.local_embedder import LocalSentenceTransformerEmbedder
from pipeline import dedup, analyzer, llm_client, prefilter, ranker, state_store
from renderers import html_renderer, discord_renderer

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
SOURCES_CONFIG = BASE_DIR / "config" / "sources.yaml"
INTERESTS_CONFIG = BASE_DIR / "config" / "interests.yaml"
DB_PATH = BASE_DIR / "data" / "briefing.db"
OUTPUT_DIR = BASE_DIR / "output"


def main(open_browser: bool = True) -> None:
    load_dotenv(BASE_DIR / ".env")

    OUTPUT_DIR.mkdir(exist_ok=True)
    DB_PATH.parent.mkdir(exist_ok=True)

    # 키가 어떤 게 들어있는지에 따라 Gemini / Groq이 자동으로 선택된다.
    client = llm_client.create_client()
    conn = state_store.init_db(str(DB_PATH))

    # ── 1. 수집 ──────────────────────────────────────────
    logger.info("=== 1단계: 뉴스 수집 ===")
    articles, source_registry = collector_registry.collect_all(str(SOURCES_CONFIG))
    if not articles:
        logger.error("수집된 기사가 없습니다. config/sources.yaml의 URL들을 확인해주세요.")
        return

    # ── 2. 클러스터링 (Article -> Event 후보) ───────────────
    logger.info("=== 2단계: 이벤트 클러스터링 (임베딩) ===")
    embedder = LocalSentenceTransformerEmbedder()
    clusters = dedup.cluster_articles_into_events(articles, embedder)

    # ── 3. 사전 선별 (LLM 호출 전, 토큰 0) ────────────────
    # 관심 키워드에 걸린 건 전부 통과시키고, 나머지는 신뢰도·교차보도 기준
    # 상위 몇 개만 통과시킨다. LLM 호출 수를 줄이는 동시에
    # "관심사는 빠짐없이 + 그 외는 중요한 것만"이라는 기준을 여기서 확정한다.
    logger.info("=== 3단계: 사전 선별 (관심사 / 주요이슈) ===")
    interests_cfg = ranker.load_interests(str(INTERESTS_CONFIG))
    # 예산은 '호출 수'로 주어지는데 한 호출에 여러 사건을 묶어 보내므로,
    # 실제로 분석 가능한 사건 수는 호출 수 × 배치 크기다.
    requests_left = getattr(client, "daily_request_budget", None)
    batch_size = max(1, getattr(client, "batch_size", 1))
    budget = requests_left * batch_size if requests_left is not None else None
    if budget is not None:
        logger.info(
            "  오늘 분석 가능 사건 수: 약 %d건 (호출 %d회 × 배치 %d건)",
            budget,
            requests_left,
            batch_size,
        )
    selected = prefilter.triage_clusters(clusters, source_registry, interests_cfg, budget=budget)

    if not selected:
        logger.error("사전 선별을 통과한 클러스터가 없습니다. config/interests.yaml을 확인해주세요.")
        return

    # ── 4. 분석 (LLM) ───────────────────────────────────
    logger.info("=== 4단계: 이슈 분석 (%s) ===", client.name)

    def _progress(done: int, size: int) -> None:
        logger.info("  %d~%d / %d 분석 중...", done + 1, done + size, len(selected))

    analyses = analyzer.analyze_clusters(
        client, [item["cluster"] for item in selected], on_progress=_progress
    )

    event_payloads = []
    for item, analysis in zip(selected, analyses):
        if analysis is None:
            continue
        payload = analyzer.build_event_payload(item["cluster"], analysis)
        payload["bucket"] = item["bucket"]
        payload["prefilter_keywords"] = item["matched_keywords"]
        event_payloads.append(payload)

    if not event_payloads:
        logger.error("분석된 이벤트가 없어 리포트를 만들지 못했습니다.")
        return

    n_failed = len(selected) - len(event_payloads)
    if n_failed:
        logger.warning("선별된 %d개 중 %d개가 LLM 분석에 실패해 빠졌습니다.", len(selected), n_failed)

    # ── 4-1. 랭킹 (신뢰도 + 개인화) ───────────────────────
    logger.info("=== 4-1단계: 랭킹 (신뢰도 + 개인화) ===")
    event_payloads = ranker.score_events(event_payloads, source_registry, interests_cfg)

    # ── 5. 신규/업데이트 판별 + 저장 ─────────────────────
    logger.info("=== 5단계: 신규/업데이트 판별 및 저장 ===")
    today = date.today().isoformat()
    now = datetime.now().isoformat()
    for payload in event_payloads:
        payload["id"] = f"event_{uuid.uuid4().hex[:12]}"
        payload["event_date"] = today
        payload["created_at"] = now
        issue_type, story_id = state_store.determine_issue_type_and_story(
            conn, payload, payload["entities"].all_names(), run_started_at=now
        )
        payload["issue_type"] = issue_type
        payload["story_id"] = story_id
        state_store.save_event(conn, payload)

    state_store.record_briefing_run(conn)

    # ── 6. 렌더링 ────────────────────────────────────────
    logger.info("=== 6단계: 렌더링 ===")
    html = html_renderer.build_html(
        event_payloads, interests_cfg=interests_cfg, n_sources_used=len(source_registry)
    )

    filename = f"news_briefing_{date.today().strftime('%Y%m%d')}.html"
    filepath = OUTPUT_DIR / filename
    filepath.write_text(html, encoding="utf-8")
    logger.info("HTML 리포트 생성 완료: %s", filepath)

    # GitHub Pages에 게시할 때는 그날 리포트의 웹 주소를 알림에 넣는다.
    # 폰에서 첨부파일을 받아 여는 것보다 링크 한 번이 훨씬 편하다.
    # 예: REPORT_BASE_URL=https://makesme-fun.github.io/probable-barnacle
    report_base = os.environ.get("REPORT_BASE_URL", "").strip().rstrip("/")
    report_url = (
        f"{report_base}/{date.today().strftime('%Y%m%d')}.html" if report_base else None
    )

    discord_webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if discord_webhook:
        discord_renderer.send_to_discord(
            discord_webhook,
            event_payloads,
            max_notifications=interests_cfg.get("discord_max_events"),
            report_path=filepath,
            report_url=report_url,
        )

    if open_browser:
        webbrowser.open(f"file://{filepath}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="개인 AI 뉴스 비서")
    # 매일 아침 예약 실행에서는 브라우저가 뜨면 곤란하다(로그인 화면 위로 튀어나오거나,
    # 세션이 없는 환경에서는 실패한다). 예약 작업은 --no-browser로 돌린다.
    parser.add_argument(
        "--no-browser", action="store_true", help="완료 후 브라우저를 열지 않습니다 (예약 실행용)"
    )
    args = parser.parse_args()
    main(open_browser=not args.no_browser)
