"""LLM 제공자 추상화 (Gemini / Groq).

analyzer가 특정 SDK에 묶이지 않도록 "프롬프트를 넣으면 JSON 문자열이 나온다"는
인터페이스 하나만 노출한다. .env에 어떤 키가 있는지로 제공자를 자동 선택하므로,
키를 바꿔 넣는 것만으로 제공자를 갈아탈 수 있다.

왜 Gemini를 우선하는가
  Groq 무료 티어는 '하루 토큰 총량'(10만)으로 막혀 있어서, 이벤트를 하나 더 볼
  때마다 한도를 갉아먹는다. 이 프로젝트의 목표가 "관심사에 걸린 건 빠짐없이 보기"라
  커버리지를 늘릴수록 손해 보는 구조와 정면으로 충돌한다.
  Gemini 무료 티어는 '하루 요청 수' 기준이라 이벤트를 몇 개 보든 1건은 1건이다.

Gemini 무료 티어의 실제 한도 (2026-08-05 실측)
  하루 20회 / 모델당. 웹에 돌아다니는 250·1000회는 옛 정보다.
  중요한 건 이게 **모델별로 따로 잡힌다**는 점이다.
    quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier, quotaValue: 20
  그래서 한 모델이 소진되면 다음 모델로 넘어가며 계속 진행한다. 이벤트마다
  분석 모델이 달라지지만, 중간에 끊겨 절반만 나오는 것보다는 낫다.

  주의: gemini-flash-latest 같은 별칭은 실제 모델과 한도를 공유한다
  (별칭으로 호출해도 429 메시지에 model: gemini-3.6-flash 로 찍힌다).
  그래서 순환 목록에는 구체 모델명만 넣는다.

모델명 주의
  Gemini 2.5 계열은 신규 API 키로는 더 이상 호출되지 않는다
  (404 "no longer available to new users", 2026-08-05 확인).
"""

from __future__ import annotations

import logging
import os
import re
import time

logger = logging.getLogger(__name__)

# 하루 한도가 모델당 20회뿐이라 한 모델로는 브리핑 하나도 못 끝낸다.
# 품질 좋은 순으로 늘어놓고, 소진되면 다음으로 넘어간다.
# 전부 실제 호출로 동작을 확인한 구체 모델명이다(별칭은 한도를 공유하므로 제외).
DEFAULT_GEMINI_MODELS = [
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
]
GROQ_MODEL = "llama-3.3-70b-versatile"

# 무료 티어 실측값 (2026-08-05). 모델당 하루 요청 수.
GEMINI_FREE_REQUESTS_PER_DAY = 20

# 무료 티어 분당 요청 제한(RPM). 모르는 모델은 보수적으로 잡고,
# 실제 값이 다르면 .env의 GEMINI_RPM으로 덮어쓴다.
GEMINI_RPM = {
    "gemini-3.6-flash": 10,
    "gemini-3.5-flash": 10,
    "gemini-3.5-flash-lite": 15,
    "gemini-3.1-flash-lite": 15,
}
DEFAULT_RPM = 5

# Gemini는 '요청 수'로 과금하므로 출력 토큰을 아낄 이유가 없다. 넉넉히 잡아
# 응답이 잘려 JSON 파싱에 실패하는 쪽을 막는 게 이득이다.
# (Groq은 정반대라 GroqClient에서 따로 낮게 잡는다)
GEMINI_MAX_OUTPUT_TOKENS = 8192

# Groq은 실제 출력량이 아니라 max_tokens '예약분'을 일일 토큰 한도에서 깎는다.
# 429의 Requested 값으로 확인: max_tokens 4000일 때 4821, 2500일 때 3321
# -> 차이가 정확히 1500이고 나머지 821이 프롬프트다. 실측 출력이 1,800토큰
# 안팎이라 잘림 여유만 두고 2500으로 잡는다.
GROQ_MAX_OUTPUT_TOKENS = 2500

# Groq 무료 티어는 하루 10만 토큰이고 1회 호출이 프롬프트 821 + 예약분을 먹으므로,
# 대략 이 정도가 하루 최대 호출 수다.
GROQ_FREE_REQUESTS_PER_DAY = 100_000 // (821 + GROQ_MAX_OUTPUT_TOKENS)

MAX_RETRIES = 3


class QuotaExhausted(Exception):
    """API 한도에 걸려 남은 클러스터를 시도해봐야 소용없는 상태.

    분당 제한처럼 기다리면 풀리는 것과 달리, 일일 한도는 재시도로 극복되지 않는다.
    호출부가 즉시 중단할 수 있도록 일반 실패와 구분해서 올린다.
    """


class _RateLimiter:
    """분당 요청 수를 넘지 않도록 호출 직전에 필요한 만큼만 잠든다.

    무료 티어는 분당 제한이 빡빡해서(모델에 따라 5~15회), 수십 개 클러스터를
    연속으로 쏘면 곧바로 429가 난다. 재시도로 수습하는 것보다 애초에 간격을
    맞추는 쪽이 로그도 깨끗하고 전체 소요 시간도 짧다.
    """

    def __init__(self, rpm: int):
        self.rpm = max(1, rpm)
        self._calls: list[float] = []

    def acquire(self) -> None:
        now = time.monotonic()
        self._calls = [t for t in self._calls if now - t < 60.0]
        if len(self._calls) >= self.rpm:
            wait = 60.0 - (now - self._calls[0]) + 0.1
            if wait > 0:
                logger.info("  분당 제한(%d RPM) 때문에 %.1f초 대기합니다.", self.rpm, wait)
                time.sleep(wait)
            now = time.monotonic()
            self._calls = [t for t in self._calls if now - t < 60.0]
        self._calls.append(time.monotonic())


class GeminiClient:
    name = "Gemini"
    max_output_tokens = GEMINI_MAX_OUTPUT_TOKENS

    def __init__(self, api_key: str, models: list[str] | None = None, rpm: int | None = None):
        from google import genai

        self._models = list(models or DEFAULT_GEMINI_MODELS)
        self._exhausted: set[str] = set()
        self._rpm_override = rpm
        self._client = genai.Client(api_key=api_key)
        self._limiter = _RateLimiter(self._rpm_for(self._models[0]))

    def _rpm_for(self, model: str) -> int:
        return self._rpm_override or GEMINI_RPM.get(model, DEFAULT_RPM)

    @property
    def daily_request_budget(self) -> int:
        """오늘 몇 번이나 부를 수 있는지의 낙관적 추정치.

        무료 티어는 모델당 하루 20회다. 오늘 이미 얼마나 썼는지는 API가
        알려주지 않으므로 '아직 안 쓴 상태' 기준으로 계산한다. 실제로 모자라면
        호출 도중 모델 순환으로 드러나고, 다 떨어지면 그 시점에서 멈춘다.
        """
        remaining_models = [m for m in self._models if m not in self._exhausted]
        return len(remaining_models) * GEMINI_FREE_REQUESTS_PER_DAY

    @property
    def model(self) -> str:
        """지금 쓰고 있는 모델. 전부 소진됐으면 마지막 것을 가리킨다."""
        for m in self._models:
            if m not in self._exhausted:
                return m
        return self._models[-1]

    def _mark_exhausted(self, model: str) -> bool:
        """이 모델의 하루 한도를 소진 처리하고, 넘어갈 모델이 남았는지 반환."""
        self._exhausted.add(model)
        remaining = [m for m in self._models if m not in self._exhausted]
        if not remaining:
            return False
        nxt = remaining[0]
        self._limiter = _RateLimiter(self._rpm_for(nxt))
        logger.warning(
            "  %s 의 하루 한도(20회)를 다 썼습니다. %s 로 넘어갑니다. (남은 모델 %d개)",
            model,
            nxt,
            len(remaining),
        )
        return True

    def _config(self, temperature: float):
        from google.genai import types

        return types.GenerateContentConfig(
            temperature=temperature,
            max_output_tokens=self.max_output_tokens,
            # JSON을 강제해두면 코드펜스(```json)를 벗겨낼 필요가 없다.
            response_mime_type="application/json",
            # thinking_config는 일부러 안 건다. Gemini 3.x 상당수가
            # thinking_budget=0을 400 INVALID_ARGUMENT로 거부하고(3.6-flash,
            # 3.5-flash-lite, *-latest 별칭에서 확인), 켜둬도 생각 토큰은
            # max_output_tokens와 별개로 계산돼서 본문이 잘리지 않는다.
        )

    def complete(self, prompt: str, temperature: float) -> str | None:
        from google.genai import errors

        attempt = 0
        while attempt < MAX_RETRIES:
            attempt += 1
            model = self.model
            self._limiter.acquire()
            try:
                response = self._client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=self._config(temperature),
                )
            except errors.ClientError as e:
                message = str(e)
                code = getattr(e, "code", None)

                # 모델이 사라졌거나 무료 티어에서 빠진 경우도 '이 모델은 못 쓴다'로
                # 보고 다음 모델로 넘긴다. 목록이 다 떨어지면 그때 알린다.
                if code == 404 or (code == 429 and "limit: 0" in message):
                    if self._mark_exhausted(model):
                        attempt -= 1  # 모델을 바꿨으니 이번 시도는 세지 않는다
                        continue
                    raise QuotaExhausted(
                        f"쓸 수 있는 Gemini 모델이 없습니다. 마지막 응답: {message[:200]}"
                    ) from e

                if code != 429:
                    logger.warning("Gemini 호출 실패, 이 클러스터는 건너뜁니다: %s", e)
                    return None

                # 하루 한도는 기다려도 안 풀린다. 다음 모델로 넘어간다.
                if "PerDay" in message:
                    if self._mark_exhausted(model):
                        attempt -= 1
                        continue
                    raise QuotaExhausted(message) from e

                # 분당 한도는 기다리면 풀린다.
                if attempt >= MAX_RETRIES:
                    logger.warning("Gemini 분당 제한 재시도 %d회 실패, 건너뜁니다.", MAX_RETRIES)
                    return None
                delay = _retry_delay_seconds(message, default=20.0 * attempt)
                logger.info("  Gemini 분당 제한, %.0f초 후 재시도 (%d/%d)", delay, attempt, MAX_RETRIES)
                time.sleep(delay)
                continue
            except Exception as e:  # noqa: BLE001
                logger.warning("Gemini 호출 실패, 이 클러스터는 건너뜁니다: %s", e)
                return None

            text = (response.text or "").strip()
            if not text:
                logger.warning("Gemini가 빈 응답을 반환했습니다. 이 클러스터는 건너뜁니다.")
                return None
            return text
        return None


class GroqClient:
    name = "Groq"
    max_output_tokens = GROQ_MAX_OUTPUT_TOKENS
    daily_request_budget = GROQ_FREE_REQUESTS_PER_DAY

    def __init__(self, api_key: str, model: str = GROQ_MODEL):
        from groq import Groq

        self.model = model
        self._client = Groq(api_key=api_key)

    def complete(self, prompt: str, temperature: float) -> str | None:
        from groq import RateLimitError

        try:
            response = self._client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=temperature,
                max_tokens=self.max_output_tokens,
            )
        except RateLimitError as e:
            # Groq 무료 티어의 429는 사실상 일일 토큰 소진이라 재시도 의미가 없다.
            raise QuotaExhausted(str(e)) from e
        except Exception as e:  # noqa: BLE001
            logger.warning("Groq 호출 실패, 이 클러스터는 건너뜁니다: %s", e)
            return None
        return (response.choices[0].message.content or "").strip()


def _retry_delay_seconds(message: str, default: float) -> float:
    """에러 메시지에 서버가 알려준 대기 시간이 있으면 그걸 쓴다."""
    m = re.search(r"retryDelay['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)s", message)
    if m:
        return float(m.group(1)) + 1.0
    return default


def create_client():
    """.env에 있는 키를 보고 제공자를 고른다. Gemini 우선, 없으면 Groq."""
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    groq_key = os.environ.get("GROQ_API_KEY", "").strip()

    if gemini_key:
        # GEMINI_MODEL에 쉼표로 여러 개를 적으면 그 순서대로 순환한다.
        configured = [m.strip() for m in os.environ.get("GEMINI_MODEL", "").split(",") if m.strip()]
        models = configured or DEFAULT_GEMINI_MODELS
        rpm_override = os.environ.get("GEMINI_RPM", "").strip()
        rpm = int(rpm_override) if rpm_override.isdigit() else None
        client = GeminiClient(gemini_key, models, rpm=rpm)
        logger.info(
            "LLM 제공자: Gemini · 모델 %d개 순환 (%s) · %d RPM",
            len(models),
            " -> ".join(models),
            client._limiter.rpm,
        )
        logger.info(
            "  무료 티어는 모델당 하루 20회입니다. 소진되면 다음 모델로 넘어갑니다 "
            "(총 %d회까지 가능).",
            len(models) * 20,
        )
        return client

    if groq_key:
        logger.info("LLM 제공자: Groq (%s)", GROQ_MODEL)
        logger.info(
            "  GEMINI_API_KEY를 .env에 넣으면 Gemini로 자동 전환됩니다 "
            "(무료 한도가 요청 수 기준이라 이 파이프라인에 더 유리합니다)."
        )
        return GroqClient(groq_key)

    raise SystemExit(
        "LLM API 키가 없습니다. .env에 아래 중 하나를 넣어주세요.\n"
        "  GEMINI_API_KEY=...  (권장) https://aistudio.google.com/apikey 에서 발급\n"
        "  GROQ_API_KEY=...           https://console.groq.com 에서 발급"
    )
