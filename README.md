# 개인 AI 뉴스 비서 (MVP)

여러 언론사의 뉴스를 수집해 같은 사건을 하나의 이벤트로 묶고, 신뢰도와 개인 관심사를
반영해 중요도 순으로 정리한 뒤 모바일 스타일 HTML 브리핑으로 보여주는 개인 뉴스 비서입니다.
설계 배경과 전체 아키텍처는 별도로 전달드린 `news_source_strategy.md`를 참고해주세요.

## 빠른 시작

```bash
cd news_assistant
python -m venv .venv && source .venv/bin/activate   # 선택 사항
pip install -r requirements.txt

cp .env.example .env
# .env 파일을 열어 GEMINI_API_KEY 를 채워주세요 (https://aistudio.google.com/apikey)
# GROQ_API_KEY 도 지원합니다. 둘 다 있으면 Gemini를 씁니다.

python main.py
```

> **Windows 주의** — `pip install -r requirements.txt`가 torch 2.13을 설치하면
> `OSError: [WinError 1114] ... c10.dll` 로 실패할 수 있습니다. requirements.txt에
> `torch<2.7` 을 걸어뒀지만, 이미 깔렸다면 아래로 다시 설치하세요.
> ```bash
> pip install --index-url https://download.pytorch.org/whl/cpu "torch==2.6.0"
> ```

### 왜 Gemini를 권장하나

Groq 무료 티어는 **하루 토큰 총량**(10만) 기준이라, 이벤트를 하나 더 볼 때마다
한도를 갉아먹습니다. 이 프로젝트는 "관심사에 걸린 건 빠짐없이 보기"가 목표라
커버리지를 늘릴수록 손해 보는 구조와 충돌합니다. 실측으로 91개 이벤트 분석에
약 30만 토큰이 필요해 하루 한 번도 완주하지 못합니다.

Gemini 무료 티어는 **하루 요청 수** 기준이라 이벤트가 몇 개든 1건은 1건입니다.

| 모델 | 분당 | 하루 | 91건 기준 |
|---|---|---|---|
| `gemini-2.5-flash` (기본) | 10 | 250 | 하루 2회, 1회당 약 9분 |
| `gemini-2.5-flash-lite` | 15 | 1,000 | 하루 10회, 1회당 약 6분 |

하루에 여러 번 돌리려면 `.env`에 `GEMINI_MODEL=gemini-2.5-flash-lite` 를 넣으세요.
(무료 한도는 변동됩니다 — https://aistudio.google.com/rate-limit 에서 확인)

처음 실행하면 로컬 임베딩 모델(`sentence-transformers`, 다국어)이 자동으로
다운로드됩니다 (수백 MB, 최초 1회만). 완료되면 `output/news_briefing_YYYYMMDD.html`이
생성되고 브라우저가 자동으로 열립니다.

## 파이프라인 흐름

```
config/sources.yaml
        │
        ▼
collectors/registry.py  ──▶  (여러 소스 동시 수집) ──▶ RawArticle[]
        │
        ▼
pipeline/dedup.py        ──▶ 임베딩 유사도로 클러스터링 ──▶ Event 후보 묶음
        │
        ▼
pipeline/prefilter.py    ──▶ LLM 호출 전 관심사/주요이슈 분류 (토큰 0)
        │
        ▼
pipeline/analyzer.py     ──▶ 클러스터별 LLM 호출 ──▶ Event 상세 필드
        │   (pipeline/llm_client.py 가 Gemini/Groq 중 하나를 자동 선택)
        │
        ▼
pipeline/ranker.py       ──▶ 신뢰도 + 개인화 스코어링 ──▶ 중요도순 정렬
        │
        ▼
pipeline/state_store.py  ──▶ SQLite 저장, 신규/업데이트 판별
        │
        ▼
renderers/html_renderer.py, discord_renderer.py ──▶ 최종 출력
```

## RSS 소스 상태 (2026-08-05 확인)

활성화된 8개 소스 — 연합뉴스, 경향신문, 조선일보, 매일경제, 전자신문,
BBC World, TechCrunch, The Verge — 는 전부 실제 수집을 확인했습니다.

**한국경제는 비활성화했습니다.** RSS 엔드포인트는 살아있지만 WAF가 브라우저
User-Agent까지 403으로 막아서 우회할 방법이 없습니다. 같은 경제지 자리를
매일경제로 대체했습니다.

로그에 `RSS 파싱 실패로 보입니다`가 뜨면 **HTTP 상태코드가 같이 찍힙니다**:
- **404** — 언론사가 URL을 바꾼 것. `sources.yaml`의 `endpoint`만 고치면 되고
  코드는 건드릴 필요가 없습니다. (조선일보가 `?outputType=xml` 누락으로 이 경우였습니다)
- **403** — 봇 차단. `rss_collector.py`가 이미 브라우저 UA로 요청하므로,
  이 경우는 대체 소스를 찾는 편이 낫습니다.

하나의 소스가 실패해도 나머지 수집과 전체 파이프라인은 정상 동작합니다.

## ⚠️ 알려진 문제: 클러스터링 정확도

현재 임베딩 모델(`paraphrase-multilingual-MiniLM-L12-v2`)이 한국어 기사에서
**무관한 사건을 같은 클러스터로 묶습니다.** 실측 예: "홈플러스 정식 개장"과
"허영 의원 협약" 의 코사인 유사도가 **0.864** 로 나옵니다.

이 때문에 "여러 매체가 동시 보도 = 이슈화" 신호가 신뢰할 수 없는 상태입니다.
임계값 조정으로는 해결되지 않고(0.72에서도 오합쳐짐), 임베딩 모델 교체가
필요합니다. 한국어에 강한 대안: `BAAI/bge-m3`, `intfloat/multilingual-e5-base`,
`jhgan/ko-sroberta-multitask`.

## 소스/관심사 추가하기

새 언론사(RSS)를 추가하려면 `config/sources.yaml`에 항목만 추가하면 됩니다.
관심 키워드를 조정하려면 `config/interests.yaml`을 수정하세요.

RSS가 아닌 새로운 소스 타입(News API, Reddit, X, 정부 보도자료 등)을 추가하려면:
1. `collectors/xxx_collector.py`에 `Collector`를 상속한 구현체 작성
2. `collectors/registry.py`의 `COLLECTOR_MAP`에 한 줄 등록
3. `sources.yaml`에 해당 `type`의 소스 추가

파이프라인의 나머지 단계는 수정할 필요가 없습니다.

## 아직 구현되지 않은 것 (설계는 반영되어 있음)

- Event → Story 연결의 정교한 로직 (현재는 엔티티 겹침 기반의 단순 규칙만 구현)
- 관심사 자동 학습 (interaction_log 테이블은 만들어져 있으나 아직 아무도 기록하지 않음)
- "지난 브리핑 이후 변경된 내용만 보기" UI (briefing_run 테이블은 있으나 렌더러에서 아직 활용 안 함)
- 3줄/30초/5분 요약 모드 전환 UI (Event.tldr 필드는 이미 채워지고 있음)
- 관련 주식 티커 자동 매핑 (entities 구조는 준비되어 있으나 룩업 테이블은 미구현)
- "계속 추적" 버튼 UI (Story.user_tracked 필드는 스키마에 있음)

## 폴더 구조

```
news_assistant/
├── config/            # sources.yaml, interests.yaml
├── collectors/         # 소스 추상화 계층
├── embeddings/          # Embedder 인터페이스 + 로컬 구현체
├── models/               # Article/Event/Story 데이터 클래스
├── pipeline/              # dedup / analyzer / ranker / state_store
├── renderers/              # html_renderer / discord_renderer
├── data/                    # briefing.db (실행 시 생성)
├── output/                   # 생성된 HTML 리포트
└── main.py                    # 오케스트레이션
```
