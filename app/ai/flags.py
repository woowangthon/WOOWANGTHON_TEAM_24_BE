"""F4 검수 필요 자동 선별 — 기능명세서 5장 규칙 R1~R4.

flag_elements(page_result, page_text) -> dict
  analyze_page 결과를 받아 그림마다 "flagged", "flag_reasons"를 붙인 새 dict를 돌려준다.
  원본은 바꾸지 않는다.

규칙을 어떻게 해석했는지 (명세에 없던 부분은 [해석]으로 표시):
- R1: uncertain_parts가 1개 이상이면 항목마다 사유 1줄.
- R3: 페이지 텍스트가 비어 있으면(스캔 페이지) 모든 그림에 R3.
      [해석] 텍스트가 있는 페이지라도 type이 photo이고 labels가 있으면 R3.
      사진 속 글자는 텍스트 레이어에 없으므로 R2로 대조하면 "못 찾음"이라는 틀린 사유가 붙는다.
- R2: R3가 아닌 그림의 labels를 페이지 텍스트에서 찾는다. 못 찾은 글자를 한 사유에 모은다.
      대소문자, 공백, 기호를 없애고 비교한다 (NFKC 정규화 포함, 전각 문자 대응).
      한계: "A", "1" 같은 한 글자 라벨은 거의 항상 찾아져서 사실상 검사가 안 된다.
- R4: [해석] type이 graph이고 labels·description·표 안에 숫자가 있으면 R4.
"""
from __future__ import annotations

import copy
import re
import unicodedata

_NON_WORD = re.compile(r"[\W_]+", re.UNICODE)
_DIGIT = re.compile(r"\d")


def normalize(text: str) -> str:
    """R2 비교용 정규화: NFKC → 소문자 → 글자·숫자 말고 전부 제거."""
    text = unicodedata.normalize("NFKC", text or "")
    return _NON_WORD.sub("", text.lower())


def _has_number(el: dict) -> bool:
    parts = list(el.get("labels") or [])
    parts.append(el.get("description") or "")
    table = el.get("table") or {}
    parts.extend(table.get("headers") or [])
    for row in table.get("rows") or []:
        parts.extend(row)
    return any(_DIGIT.search(p) for p in parts)


def check_labels(labels: list[str], page_text: str) -> list[tuple[str, bool]]:
    """라벨마다 (라벨, 원문에 있음 여부). 정규화 후 빈 라벨(기호만)은 빼고 돌려준다."""
    norm_text = normalize(page_text)
    out = []
    for label in labels or []:
        n = normalize(label)
        if not n:
            continue
        out.append((label, n in norm_text))
    return out


def flag_element(el: dict, page_text: str) -> list[str]:
    reasons: list[str] = []

    # R1 AI가 확신 못 함
    for part in el.get("uncertain_parts") or []:
        reasons.append(f"R1: AI가 확신하지 못한 부분: {part}")

    labels = el.get("labels") or []
    no_text_layer = not normalize(page_text)
    photo_with_text = el.get("type") == "photo" and bool(labels)

    if no_text_layer or photo_with_text:
        # R3 대조 불가
        reasons.append("R3: 원문 글자가 없어 자동 확인 불가")
    else:
        # R2 글자 대조 실패
        missing = [label for label, found in check_labels(labels, page_text) if not found]
        if missing:
            quoted = ", ".join(f'"{m}"' for m in missing)
            reasons.append(f"R2: 그림 속 글자 {quoted}를 원문에서 찾지 못함")

    # R4 수치 자료
    if el.get("type") == "graph" and _has_number(el):
        reasons.append("R4: 숫자가 들어간 그래프")

    return reasons


def flag_elements(page_result: dict, page_text: str) -> dict:
    result = copy.deepcopy(page_result)
    for el in result.get("elements", []):
        reasons = flag_element(el, page_text)
        el["flagged"] = bool(reasons)
        el["flag_reasons"] = reasons
    return result
