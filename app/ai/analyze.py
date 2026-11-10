"""F3 AI 그림 설명 생성.

analyze_page(image_png: bytes, page_no: int) -> dict
  6장 데이터 계약의 "analyze_page 결과"를 돌려준다.
  [계약 변경 제안] 명세의 analyze_page(image_png)에는 page_no가 없지만, 결과의 page_no와
  그림 id("p3-e1")를 만들려면 필요하다. 이준원 님과 합의 후 확정.

환경변수
  AI_MODE         live(기본) | mock
  AI_MODEL        기본 gemini-3.5-flash-lite (정확한 모델 ID는 AI Studio에서 확인)
  GEMINI_API_KEY  live 모드에서 필요. .env에만 두고 저장소에 올리지 않는다 (BE 저장소는 Public).
  AI_RPM          분당 호출 수 상한. 없으면 모델별 기본값(_DEFAULT_RPM).
  AI_MOCK_DELAY   mock 모드에서 페이지당 대기 초 (기본 0.5). 진행률이 움직이는지 보려고 둔다.
  AI_THINKING     minimal | low | medium | high. 비우면 모델 기본값. 낮추면 빨라지고 싸지지만
                  정확도가 떨어질 수 있다 (생각하는 모델에서만 의미 있음, 효과는 실험으로 확인).
  AI_MEDIA_RESOLUTION  low | medium | high. 비우면 모델 기본값. high는 작은 글자·가는 선을
                  더 잘 볼 수 있지만 입력 토큰이 는다 (효과는 실험으로 확인).

실패 처리 (명세 F3 "1번 다시 시도"를 원인별로 나눔)
  - 응답 형식 오류: 1번 다시 시도 → 또 실패하면 '분석 실패' 그림 1개를 넣어 돌려준다.
    이 그림은 uncertain_parts가 있어 flag_elements에서 R1로 검수 필요가 된다.
  - 429 분당 한도: 서버가 알려 준 시간만큼 기다렸다 다시 시도한다 (재시도 횟수에 안 셈).
  - 429 하루 한도: AIQuotaExhausted를 던진다. 남은 페이지를 전부 '분석 실패'로 채우면
    검수자가 다 써야 하므로, 백엔드가 문서를 failed로 두는 게 낫다.
  - 5xx·네트워크 오류: 잠깐 기다렸다 다시 시도, 계속되면 AIUnavailable.
  - 400·401·403·404(키·모델 ID 오류 등): 설정 문제라 그대로 던진다.

호출 간격
  같은 프로세스 안에서 모델별로 호출을 줄 세운다. uvicorn worker를 여러 개 띄우면
  간격이 지켜지지 않으니 worker는 1개로 돌린다.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from .prompt import ANALYZE_PROMPT
from .schema import PageOut

DEFAULT_MODEL = "gemini-3.5-flash-lite"

# 분당 호출 수 기본값. 출처: 팀 프로젝트의 AI Studio 한도 화면 (2026-11-06 확인, 무료 등급).
#   gemini-3.5-flash-lite  RPM 15 / TPM 250K / RPD 500
#   gemini-3.8-flash       RPM 5  / TPM 250K / RPD 20
_DEFAULT_RPM = {
    "gemini-3.5-flash-lite": 15,
    "gemini-3.8-flash": 5,
}
_FALLBACK_RPM = 5

_MAX_TRANSIENT_RETRIES = 5
_MOCK_PATH = Path(__file__).with_name("mock_page.json")
log = logging.getLogger("app.ai")


class AIQuotaExhausted(RuntimeError):
    """하루 호출 한도를 다 씀. 한국 시간 17:00(태평양 자정)에 초기화된다."""


class AIUnavailable(RuntimeError):
    """서버 오류·네트워크 오류가 계속됨."""


@dataclass
class CallStats:
    model: str
    calls: int = 0
    format_failures: int = 0
    rate_waits: int = 0
    transient_errors: int = 0
    seconds: float = 0.0
    prompt_tokens: int = 0      # 입력(이미지+프롬프트)
    output_tokens: int = 0      # 응답 JSON
    thought_tokens: int = 0     # 생각 토큰. 유료 요금은 출력 단가로 매겨진다
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- 호출 간격

_pace_lock = threading.Lock()
_last_call: dict[str, float] = {}


def _rpm_for(model: str) -> float:
    env = os.getenv("AI_RPM")
    if env:
        return float(env)
    return _DEFAULT_RPM.get(model, _FALLBACK_RPM)


def _pace(model: str) -> None:
    """직전 호출 시작으로부터 60/RPM초(+5% 여유)가 지날 때까지 기다린다."""
    interval = 60.0 / _rpm_for(model) * 1.05
    with _pace_lock:
        now = time.monotonic()
        wait = _last_call.get(model, 0.0) + interval - now
        if wait > 0:
            time.sleep(wait)
        _last_call[model] = time.monotonic()


# ---------------------------------------------------------------- 결과 변환

def _to_contract(page: PageOut, page_no: int) -> dict:
    elements = []
    for i, el in enumerate(page.elements, start=1):
        table = None
        if el.type == "table":
            # 머리글이 전부 빈칸이면 머리글 없는 표로 본다(_check_content와 같은 기준)
            headers = list(el.table.headers) if any(h.strip() for h in el.table.headers) else []
            table = {
                "headers": headers,
                "rows": [list(r.cells) for r in el.table.rows],
            }
        elements.append({
            "id": f"p{page_no}-e{i}",
            "type": el.type,
            "title": el.title.strip(),
            "alt": el.alt.strip(),
            "description": el.description.strip(),
            "labels": [s for s in (x.strip() for x in el.labels) if s],
            "table": table,
            "uncertain_parts": [s for s in (x.strip() for x in el.uncertain_parts) if s],
        })
    return {"page_no": page_no, "elements": elements}


def failed_page(page_no: int, why: str) -> dict:
    """분석에 실패한 페이지. R1로 검수 필요가 되도록 uncertain_parts를 채운다."""
    return {
        "page_no": page_no,
        "elements": [{
            "id": f"p{page_no}-e1",
            "type": "other",
            "title": "분석 실패",
            "alt": "",
            "description": "",
            "labels": [],
            "table": None,
            "uncertain_parts": [f"AI가 이 페이지를 분석하지 못함({why}). 검수자가 직접 설명을 써야 함"],
        }],
    }


def parse_response(text: str | None) -> PageOut:
    """응답 문자열 → PageOut. 형식이 틀리면 ValueError 또는 ValidationError."""
    if not text:
        raise ValueError("빈 응답")
    page = PageOut.model_validate(json.loads(text))
    _check_content(page)
    return page


def _check_content(page: PageOut) -> None:
    """칸은 다 있지만 내용이 쓸 수 없는 응답을 형식 오류로 본다 → 기존 재시도 흐름을 탄다.

    스키마(schema.py)에 min_length를 걸지 않고 여기서 검사하는 이유: 스키마 제약은 Gemini에 넘기는
    response_schema까지 바꾼다. failed_page는 alt·description이 비어 있어야 하므로 이 검사를 거치지 않는다.
    """
    for i, el in enumerate(page.elements, start=1):
        for name in ("title", "alt", "description"):
            if not getattr(el, name).strip():
                raise ValueError(f"e{i}: {name}이 비어 있음")
        if el.type != "table":
            continue
        rows = el.table.rows
        if not rows:
            raise ValueError(f"e{i}: 표에 데이터 행이 없음")
        if not any(c.strip() for row in rows for c in row.cells):
            raise ValueError(f"e{i}: 표 칸이 전부 비어 있음")
        # 원본에 머리글 행이 없는 표(예: 4쪽 OSI 표)는 headers가 [] 또는 전부 빈칸으로 온다.
        # 이때는 _to_contract에서 headers를 []로 바꾸고, 첫 행 칸 수에 나머지 행을 맞춘다.
        # 왼쪽 위 칸처럼 일부만 빈 머리글은 머리글 있는 표로 본다.
        headers = el.table.headers
        if any(h.strip() for h in headers):
            width, against = len(headers), "머리글"
        else:
            width, against = len(rows[0].cells), "1행"
        for r, row in enumerate(rows, start=1):
            if len(row.cells) != width:
                raise ValueError(f"e{i}: 표 {r}행 칸 수 {len(row.cells)}개 ≠ {against} {width}개")


# ---------------------------------------------------------------- 429 해석

_RETRY_DELAY = re.compile(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"')


def _is_daily_quota(details) -> bool:
    return "PerDay" in json.dumps(details, ensure_ascii=False)


def _retry_delay(details, default: float = 15.0) -> float:
    m = _RETRY_DELAY.search(json.dumps(details, ensure_ascii=False))
    return float(m.group(1)) + 1.0 if m else default


# ---------------------------------------------------------------- 모킹

def _mock_page(page_no: int) -> dict:
    templates = json.loads(_MOCK_PATH.read_text(encoding="utf-8"))
    t = templates[(page_no - 1) % len(templates)]
    elements = []
    for i, el in enumerate(t["elements"], start=1):
        el = dict(el)
        el["id"] = f"p{page_no}-e{i}"
        elements.append(el)
    return {"page_no": page_no, "elements": elements}


# ---------------------------------------------------------------- 본체

_client = None
_client_lock = threading.Lock()


def _get_client():
    global _client
    with _client_lock:
        if _client is None:
            from google import genai
            from google.genai import types

            # api_key를 안 넘기면 SDK가 GEMINI_API_KEY 환경변수를 읽는다.
            _client = genai.Client(http_options=types.HttpOptions(timeout=120_000))
        return _client


def analyze_page_with_stats(image_png: bytes, page_no: int, *, model: str | None = None) -> tuple[dict, CallStats]:
    model = model or os.getenv("AI_MODEL", DEFAULT_MODEL)
    stats = CallStats(model=model)
    started = time.monotonic()

    if os.getenv("AI_MODE", "live").lower() == "mock":
        time.sleep(float(os.getenv("AI_MOCK_DELAY", "0.5")))
        stats.seconds = time.monotonic() - started
        return _mock_page(page_no), stats

    import httpx
    from google.genai import errors, types

    client = _get_client()
    res = os.getenv("AI_MEDIA_RESOLUTION", "").strip().lower()
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=PageOut,
        media_resolution=getattr(types.MediaResolution, f"MEDIA_RESOLUTION_{res.upper()}") if res else None,
    )
    thinking = os.getenv("AI_THINKING", "").strip().upper()
    if thinking:
        config.thinking_config = types.ThinkingConfig(thinking_level=getattr(types.ThinkingLevel, thinking))
    contents = [types.Part.from_bytes(data=image_png, mime_type="image/png"), ANALYZE_PROMPT]

    while True:
        _pace(model)
        stats.calls += 1
        try:
            resp = client.models.generate_content(model=model, contents=contents, config=config)
        except errors.APIError as e:
            stats.errors.append(f"{e.code} {e.status}")
            if e.code == 429:
                if _is_daily_quota(e.details):
                    raise AIQuotaExhausted(f"{model} 하루 한도 초과") from e
                stats.rate_waits += 1
                if stats.rate_waits > _MAX_TRANSIENT_RETRIES:
                    raise AIUnavailable(f"{model} 분당 한도 초과가 계속됨") from e
                delay = _retry_delay(e.details)
                log.warning("[%s] p%s 분당 한도 초과 → %.0f초 기다렸다 다시 시도 (%d/%d)",
                            model, page_no, delay, stats.rate_waits, _MAX_TRANSIENT_RETRIES)
                time.sleep(delay)
                continue
            if e.code >= 500:
                stats.transient_errors += 1
                if stats.transient_errors > _MAX_TRANSIENT_RETRIES:
                    raise AIUnavailable(f"{model} 서버 오류 {e.code}가 계속됨") from e
                log.warning("[%s] p%s 서버 오류 %s %s → %d초 후 다시 시도 (%d/%d)", model, page_no,
                            e.code, e.status, 5 * stats.transient_errors, stats.transient_errors,
                            _MAX_TRANSIENT_RETRIES)
                time.sleep(5 * stats.transient_errors)
                continue
            raise
        except (httpx.TimeoutException, httpx.TransportError) as e:
            stats.errors.append(type(e).__name__)
            stats.transient_errors += 1
            if stats.transient_errors > _MAX_TRANSIENT_RETRIES:
                raise AIUnavailable(f"네트워크 오류가 계속됨: {e}") from e
            log.warning("[%s] p%s %s(응답 없음·연결 오류) → %d초 후 다시 시도 (%d/%d)", model, page_no,
                        type(e).__name__, 5 * stats.transient_errors, stats.transient_errors,
                        _MAX_TRANSIENT_RETRIES)
            time.sleep(5 * stats.transient_errors)
            continue

        usage = getattr(resp, "usage_metadata", None)
        if usage is not None:
            stats.prompt_tokens += usage.prompt_token_count or 0
            stats.output_tokens += usage.candidates_token_count or 0
            stats.thought_tokens += usage.thoughts_token_count or 0

        try:
            page = parse_response(resp.text)
        except (ValueError, ValidationError) as e:
            stats.format_failures += 1
            stats.errors.append(f"형식 오류: {str(e)[:120]}")
            log.warning("[%s] p%s 응답 형식 오류 (%d/2)", model, page_no, stats.format_failures)
            if stats.format_failures >= 2:
                stats.seconds = time.monotonic() - started
                return failed_page(page_no, "응답 형식 오류 2회"), stats
            continue

        stats.seconds = time.monotonic() - started
        return _to_contract(page, page_no), stats


def analyze_page(image_png: bytes, page_no: int, *, model: str | None = None) -> dict:
    result, _ = analyze_page_with_stats(image_png, page_no, model=model)
    return result
