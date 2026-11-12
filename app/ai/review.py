"""AI 교차 검토 (R5, 이슈 #7) — 만든 설명을 이미지와 다시 대조해 다른 곳을 찾는다.

review_page(image_png, page_result) -> dict[str, list[str]] | None
review_page_with_stats(image_png, page_result, *, model=None) -> (같은 값, CallStats)
  page_result는 analyze_page 결과(flag_elements 전후 모두 가능).
  돌려주는 dict는 그림 id("p11-e1") → 지적 목록. 지적이 없으면 빈 목록.
  형식 오류가 2번 나면 None (검토 실패. 지적 없음과 구분한다).
  하루 한도 초과·서버 오류 반복은 analyze_page와 같이 AIQuotaExhausted·AIUnavailable을 던진다
  → 문서 failed. 호출 간격 제한도 analyze_page와 같은 것을 쓴다(_generate).

지적은 "설명: (설명의 해당 부분) / 그림: (그림에서 보이는 것)" 형식이어야 한다.
형식이 다르면 형식 오류로 보고 다시 묻는다. 사유로 바꾸는 것은 flags.apply_review가 한다.

연결: app/pdf_processing.py가 flag_elements 뒤에 자동 통과 후보가 있는 쪽에서만 부른다.
AI_REVIEW=off면 부르지 않는다(review_enabled).

한계
- 설명을 만든 모델과 같은 모델이면 같은 실수(글자 오독 등)를 반복할 수 있다. AI_REVIEW_MODEL로 바꿀 수 있다.
- 호출이 그림 있는 쪽마다 1번 더 든다. 11/10 실험: 쪽당 2.8~8.4초.
- 처음 보는 10쪽 실험에서 틀린 그림 2개 중 1개만 잡았고, 맞는 그림 7개 중 3개를 잘못 의심했다.
"""
from __future__ import annotations

import json
import logging
import os
import time

from pydantic import BaseModel, ValidationError

from . import analyze as A
from .flags import split_issue
from .prompt import REVIEW_PROMPT

log = logging.getLogger("app.ai")


class ElementReview(BaseModel):
    index: int
    issues: list[str]


class ReviewOut(BaseModel):
    elements: list[ElementReview]


def _review_input(page_result: dict) -> str:
    """모델에 보낼 설명 JSON. 검토에 필요 없는 칸(flag 결과 등)은 뺀다."""
    items = []
    for i, el in enumerate(page_result.get("elements", []), start=1):
        item = {"index": i, "type": el["type"], "title": el["title"], "alt": el["alt"],
                "description": el["description"], "labels": el.get("labels") or []}
        if el.get("table"):
            item["table"] = el["table"]
        items.append(item)
    return json.dumps({"elements": items}, ensure_ascii=False, indent=1)


def parse_review(text: str | None, element_ids: list[str]) -> dict[str, list[str]]:
    """응답 → 그림 id별 지적 목록.

    형식 오류(ValueError): 빈 응답, JSON 아님, index 범위 밖,
    지적이 "설명: … / 그림: …" 형식이 아님(사유에 넣을 의심 문장을 뽑을 수 없으므로).
    """
    if not text:
        raise ValueError("빈 응답")
    out = ReviewOut.model_validate(json.loads(text))
    result: dict[str, list[str]] = {eid: [] for eid in element_ids}
    for item in out.elements:
        if not 1 <= item.index <= len(element_ids):
            raise ValueError(f"없는 그림 index {item.index}")
        issues = [s.strip() for s in item.issues if s.strip()]
        for issue in issues:
            if split_issue(issue) is None:
                raise ValueError(f"지적 형식이 다름: {issue[:60]}")
        result[element_ids[item.index - 1]].extend(issues)
    return result


def review_enabled() -> bool:
    """AI_REVIEW=off(또는 0, false, no)면 R5 교차 검토를 하지 않는다. 기본은 켬."""
    return os.getenv("AI_REVIEW", "on").strip().lower() not in ("off", "0", "false", "no")


def review_page(image_png: bytes, page_result: dict) -> dict[str, list[str]] | None:
    return review_page_with_stats(image_png, page_result)[0]


def review_page_with_stats(image_png: bytes, page_result: dict, *,
                           model: str | None = None) -> tuple[dict[str, list[str]] | None, A.CallStats]:
    model = model or os.getenv("AI_REVIEW_MODEL") or os.getenv("AI_MODEL", A.DEFAULT_MODEL)
    stats = A.CallStats(model=model)
    started = time.monotonic()
    element_ids = [el["id"] for el in page_result.get("elements", [])]

    if not element_ids:          # 그림이 없는 쪽은 검토할 것이 없다
        return {}, stats

    if os.getenv("AI_MODE", "live").lower() == "mock":
        stats.seconds = time.monotonic() - started
        return {eid: [] for eid in element_ids}, stats

    from google.genai import types

    config = A._make_config(ReviewOut)
    contents = [types.Part.from_bytes(data=image_png, mime_type="image/png"),
                REVIEW_PROMPT, "[검토할 설명]\n" + _review_input(page_result)]
    page_no = page_result.get("page_no", "?")

    while True:
        resp = A._generate(model, contents, config, stats, page_no)
        try:
            result = parse_review(resp.text, element_ids)
        except (ValueError, ValidationError) as e:
            stats.format_failures += 1
            stats.errors.append(f"검토 형식 오류: {str(e)[:120]}")
            log.warning("[%s] p%s 검토 응답 형식 오류 (%d/2)", model, page_no, stats.format_failures)
            if stats.format_failures >= 2:
                stats.seconds = time.monotonic() - started
                return None, stats
            continue
        stats.seconds = time.monotonic() - started
        return result, stats
