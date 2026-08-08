"""
Discord 웹훅 알림 렌더러.

HTML 리포트는 PC에서 보는 전체본이고, Discord는 아침에 폰으로 받는 푸시 알림이다.
그래서 여기서는 "폰 알림창에서 읽고 끝낼 수 있는" 형태를 목표로 한다.
  - 첫 메시지에 오늘의 요약(건수 + 관심 키워드별 분포)
  - 이어서 이벤트 카드. 관심사를 먼저 보내고 남는 자리를 그 외 이슈로 채운다.

Discord 제약 때문에 나눠 보낸다:
  - 메시지당 embed 10개
  - embed description 4096자, 전체 embed 합 6000자
  - 웹훅은 초당 5회 정도에서 429가 나므로 간격을 두고, 429가 오면 retry_after만큼 쉰다

사용법: .env에 DISCORD_WEBHOOK_URL을 설정하면 main.py가 자동으로 호출한다.
설정하지 않으면 조용히 건너뛴다 (필수 아님).
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

MAX_EMBEDS_PER_MESSAGE = 10
DESCRIPTION_LIMIT = 400          # 폰 알림에서 읽기 좋은 길이. 스펙 상한(4096)보다 훨씬 짧게 잡는다
REQUEST_INTERVAL_SEC = 0.7       # 웹훅 rate limit(초당 5회) 여유
MAX_RETRIES = 3
MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024   # 웹훅 첨부 상한(부스트 없는 서버 기준)

COLOR_INTEREST = 0xD8C08A        # HTML 리포트의 강조색과 맞춤
COLOR_GENERAL = 0x4A4A4E


def _post(webhook_url: str, payload: dict, file_path: Path | None = None) -> bool:
    """웹훅 1회 전송. 429면 서버가 알려준 만큼 쉬고 재시도한다.

    file_path를 주면 파일을 첨부한다(HTML 전체 리포트용). 첨부가 있을 때는
    JSON 본문 대신 multipart로 보내야 해서 요청 형식이 달라진다.
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            if file_path is not None:
                with open(file_path, "rb") as f:
                    resp = requests.post(
                        webhook_url,
                        data={"payload_json": json.dumps(payload, ensure_ascii=False)},
                        files={"files[0]": (file_path.name, f, "text/html")},
                        timeout=60,
                    )
            else:
                resp = requests.post(webhook_url, json=payload, timeout=15)
        except Exception as e:  # noqa: BLE001
            logger.warning("Discord 전송 실패(네트워크): %s", e)
            return False

        if resp.status_code == 429:
            try:
                wait = float(resp.json().get("retry_after", 2.0))
            except Exception:  # noqa: BLE001
                wait = 2.0
            logger.info("  Discord rate limit, %.1f초 대기 (%d/%d)", wait, attempt, MAX_RETRIES)
            time.sleep(wait + 0.3)
            continue

        if resp.status_code >= 400:
            logger.warning("Discord 전송 실패 %s: %s", resp.status_code, resp.text[:300])
            return False

        return True

    logger.warning("Discord rate limit 재시도 %d회 실패", MAX_RETRIES)
    return False


def _truncate(text: str, limit: int = DESCRIPTION_LIMIT) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _build_embed(payload: dict) -> dict:
    is_interest = payload.get("bucket") == "interest"
    link = payload["source_links"][0] if payload["source_links"] else None

    # 폰에서는 tldr 한 줄이 핵심이다. 없으면 details 앞부분으로 대체한다.
    body = payload.get("tldr") or payload.get("details", "")

    footer_bits = [f"소스 {len(payload['source_ids'])}곳", f"중요도 {payload['importance_score']:.0f}"]
    if payload.get("issue_type") == "update":
        footer_bits.append("업데이트")
    if payload.get("interest_tags"):
        footer_bits.append(" · ".join(payload["interest_tags"][:3]))

    embed = {
        "title": _truncate(payload["title"], 250),
        "description": _truncate(body),
        "color": COLOR_INTEREST if is_interest else COLOR_GENERAL,
        "footer": {"text": " | ".join(footer_bits)},
    }
    if link:
        embed["url"] = link
    return embed


def _build_summary(
    interest: list[dict],
    general: list[dict],
    sending: int,
    total: int,
    has_report: bool,
    report_url: str | None,
) -> str:
    weekday = ["월", "화", "수", "목", "금", "토", "일"][datetime.now().weekday()]
    date_str = datetime.now().strftime(f"%m월 %d일 ({weekday})")

    # 관심 키워드별 건수를 세서 "오늘 뭐가 많았는지"를 한 줄로 보여준다
    counts: dict[str, int] = {}
    for p in interest:
        for tag in p.get("interest_tags", []):
            counts[tag] = counts.get(tag, 0) + 1
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:6]
    keyword_line = " · ".join(f"{k} {v}" for k, v in top) if top else "매칭된 키워드 없음"

    lines = [
        f"## ☀️ {date_str} 아침 브리핑",
        f"**관심사 {len(interest)}건** · 그 외 주요 이슈 {len(general)}건",
        f"> {keyword_line}",
    ]
    if sending < total:
        lines.append(f"_아래 카드는 상위 {sending}건입니다 (전체 {total}건)._")

    # 링크가 있으면 그게 최선이다(폰에서 한 번만 누르면 열린다).
    # 첨부파일은 다운로드해서 열어야 해서 번거로우므로, 링크가 있을 때는 언급하지 않는다.
    if report_url:
        lines.append(f"\n**📰 전체 리포트 보기**\n{report_url}")
    elif has_report:
        lines.append("_📎 첨부된 HTML을 열면 배경·용어·찬반 관점까지 전체 리포트를 볼 수 있습니다._")
    return "\n".join(lines)


def send_to_discord(
    webhook_url: str,
    sorted_payloads: list[dict],
    max_notifications: int | None = None,
    score_threshold: float | None = None,
    report_path: str | Path | None = None,
    report_url: str | None = None,
) -> None:
    """관심사 우선으로 Discord에 보낸다.

    max_notifications가 None이면 전부 보낸다. 기본을 '전부'로 두는 이유는,
    폰에서 HTML 리포트를 열기가 사실상 불가능해서다(깃허브 아티팩트는 zip으로
    받아 압축을 풀어야 한다). 알림 자체가 원본이어야 한다.

    score_threshold를 주면 그 미만은 제외한다. 기본은 None(=거르지 않음)인데,
    관심 키워드에 걸린 건 점수와 무관하게 보고 싶다는 게 이 프로젝트의 전제라서다.
    """
    if not webhook_url:
        return
    if not sorted_payloads:
        logger.info("보낼 이벤트가 없어 Discord 전송을 건너뜁니다.")
        return

    eligible = sorted_payloads
    if score_threshold is not None:
        eligible = [p for p in eligible if p["importance_score"] >= score_threshold]

    interest = [p for p in eligible if p.get("bucket") == "interest"]
    general = [p for p in eligible if p.get("bucket") != "interest"]
    to_send = interest + general
    if max_notifications is not None:
        to_send = to_send[:max_notifications]

    if not to_send:
        logger.info("Discord로 보낼 이벤트가 없습니다.")
        return

    # 웹 링크가 있으면 첨부는 굳이 안 보낸다. 폰에서 링크가 훨씬 편하고,
    # 같은 내용을 두 번 보내면 메시지만 지저분해진다.
    attachment = None
    if report_path and not report_url:
        attachment = Path(report_path)
        if not attachment.exists() or attachment.stat().st_size > MAX_ATTACHMENT_BYTES:
            if attachment.exists():
                logger.info("HTML 리포트가 %.1fMB로 첨부 상한을 넘어 생략합니다.",
                            attachment.stat().st_size / 1_048_576)
            attachment = None

    summary = _build_summary(
        interest, general, len(to_send), len(eligible), attachment is not None, report_url
    )
    if not _post(webhook_url, {"content": summary}, file_path=attachment):
        return
    time.sleep(REQUEST_INTERVAL_SEC)

    sent = 0
    for i in range(0, len(to_send), MAX_EMBEDS_PER_MESSAGE):
        batch = to_send[i : i + MAX_EMBEDS_PER_MESSAGE]
        if not _post(webhook_url, {"embeds": [_build_embed(p) for p in batch]}):
            break
        sent += len(batch)
        time.sleep(REQUEST_INTERVAL_SEC)

    logger.info("Discord로 %d건 전송 완료 (관심사 %d + 그 외 %d 중)",
                sent, len(interest), len(general))
