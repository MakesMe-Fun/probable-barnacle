"""생성된 브리핑 HTML을 GitHub Pages로 게시할 docs/ 폴더에 배치한다.

  docs/YYYYMMDD.html  — 그날의 리포트 (디스코드 링크가 이걸 직접 가리킨다)
  docs/index.html     — 날짜별 목록 (지난 브리핑 훑어보기용)

디스코드 알림이 날짜 파일을 바로 가리키므로, 폰에서는 링크 한 번이면 그날 리포트가
열린다. index.html은 "그저께 그거 뭐였지"를 위한 보조 화면이다.

실행: python scripts/publish_report.py   (main.py가 만든 output/*.html을 읽는다)
"""

from __future__ import annotations

import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = BASE_DIR / "output"
DOCS_DIR = BASE_DIR / "docs"

DATE_PATTERN = re.compile(r"^(\d{4})(\d{2})(\d{2})\.html$")

INDEX_TEMPLATE = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>브리핑 보관함</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Noto+Serif+KR:wght@700;900&family=Pretendard&display=swap" rel="stylesheet">
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  :root {{ --bg:#0B0B0C; --ink:#EDEDED; --ink-soft:#9A9A9E; --rule:#2B2B2E; --accent:#D8C08A; }}
  body {{ background:var(--bg); color:var(--ink); font-family:'Pretendard',-apple-system,sans-serif;
    max-width:480px; margin:0 auto; padding:28px 20px 60px; }}
  h1 {{ font-family:'Noto Serif KR',serif; font-weight:900; font-size:26px; letter-spacing:-0.02em; }}
  .sub {{ font-size:12px; color:var(--ink-soft); margin-top:6px; padding-bottom:18px;
    border-bottom:1px solid var(--rule); }}
  a.item {{ display:flex; align-items:baseline; gap:10px; padding:15px 2px;
    border-bottom:1px solid var(--rule); color:var(--ink); text-decoration:none; }}
  a.item:first-of-type .date {{ color:var(--accent); }}
  .date {{ font-family:'Noto Serif KR',serif; font-weight:700; font-size:16px; }}
  .weekday {{ font-size:12px; color:var(--ink-soft); }}
  .latest {{ margin-left:auto; font-size:10px; font-weight:700; letter-spacing:0.06em;
    background:var(--accent); color:#1a1a1a; padding:2px 7px; border-radius:2px; }}
  footer {{ margin-top:26px; font-size:11px; color:var(--ink-soft); text-align:center; }}
</style>
</head>
<body>
  <h1>브리핑 보관함</h1>
  <div class="sub">매일 아침 6시 · 총 {count}일치</div>
  {items}
  <footer>개인 AI 뉴스 비서</footer>
</body>
</html>
"""

WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]


def build_index(dated_files: list[Path]) -> str:
    items = []
    for i, path in enumerate(sorted(dated_files, reverse=True)):
        m = DATE_PATTERN.match(path.name)
        if not m:
            continue
        y, mo, d = (int(g) for g in m.groups())
        weekday = WEEKDAYS[datetime(y, mo, d).weekday()]
        badge = '<span class="latest">최신</span>' if i == 0 else ""
        items.append(
            f'<a class="item" href="{path.name}">'
            f'<span class="date">{y}년 {mo}월 {d}일</span>'
            f'<span class="weekday">{weekday}요일</span>{badge}</a>'
        )
    return INDEX_TEMPLATE.format(count=len(items), items="\n  ".join(items))


def main() -> int:
    reports = sorted(OUTPUT_DIR.glob("news_briefing_*.html"))
    if not reports:
        print("게시할 리포트가 없습니다. main.py를 먼저 실행하세요.", file=sys.stderr)
        return 1

    DOCS_DIR.mkdir(exist_ok=True)
    latest = reports[-1]

    # news_briefing_20260806.html -> 20260806.html
    stamp = latest.stem.replace("news_briefing_", "")
    target = DOCS_DIR / f"{stamp}.html"
    shutil.copyfile(latest, target)
    print(f"게시: {latest.name} -> docs/{target.name}")

    dated = [p for p in DOCS_DIR.glob("*.html") if DATE_PATTERN.match(p.name)]
    (DOCS_DIR / "index.html").write_text(build_index(dated), encoding="utf-8")
    print(f"목차 갱신: docs/index.html ({len(dated)}일치)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
