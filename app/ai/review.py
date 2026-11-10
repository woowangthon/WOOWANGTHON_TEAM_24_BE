"""AI 교차 검토 (실험) — 만든 설명을 이미지와 다시 대조해 다른 곳을 찾는다.

review_page_with_stats(image_png, page_result, *, model=None) -> (dict[str, list[str]] | None, CallStats)
  page_result는 analyze_page 결과(flag_elements 전후 모두 가능).
  돌려주는 dict는 그림 id("p11-e1") → 지적 목록. 지적이 없으면 빈 목록.
  형식 오류가 2번 나면 None (검토 실패. 지적 없음과 구분한다).

아직 백엔드(flag_elements, DB)에는 연결하지 않았다. scripts/try_pages.py --review 로만 쓴다.
효과가 확인되면 지적이 있는 그림을 새 규칙(예: R5)으로 검수 필요에 넣는 것을 팀에 제안한다.
그때는 기능명세서 5장과 CONTRACT.md의 사유 표를 같이 고쳐야 한다.

한계
- 설명을 만든 모델과 같은 모델이면 같은 실수(글자 오독 등)를 반복할 수 있다. --review-model로 바꿔 볼 수 있다.
- 호출이 쪽마다 1번 더 든다(그림이 없는 쪽은 부르지 않는다).
"""
from __future__ import annotations

import json
import logging
import os
import time

from pydantic import BaseModel, ValidationError

from . import analyze as A
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
    """응답 → 그림 id별 지적 목록. index가 범위를 벗어나면 형식 오류(ValueError)."""
    if not text:
        raise ValueError("빈 응답")
    out = ReviewOut.model_validate(json.loads(text))
    result: dict[str, list[str]] = {eid: [] for eid in element_ids}
    for item in out.elements:
        if not 1 <= item.index <= len(element_ids):
            raise ValueError(f"없는 그림 index {item.index}")
        issues = [s.strip() for s in item.issues if s.strip()]
        result[element_ids[item.index - 1]].extend(issues)
    return result


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
