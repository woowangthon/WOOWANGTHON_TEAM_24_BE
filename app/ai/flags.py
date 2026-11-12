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
      description의 단계 번호 "1) 2)"는 세지 않는다 (프롬프트가 붙이게 해서 모든 그래프가 걸림).
      한계: "막대 3개"처럼 개수를 숫자로 쓴 문장은 여전히 R4로 걸린다.
      description을 아예 빼지 않는 이유: 축 눈금 없이 막대 높이만 보고 값을 추정해 쓴 경우를 잡으려고.

R5 (apply_review, 이슈 #7에서 결정): flag_elements 뒤에 AI 교차 검토(review.py) 결과를 붙인다.
- R1~R4로 이미 걸린 그림은 그대로 두고, 자동 통과 후보(flagged=False)에만 적용한다.
- 사유에는 설명의 의심 문장만 넣고 AI가 말한 "그림: …" 정정 내용은 넣지 않는다.
  11/10 실험에서 정정 내용 자체가 틀린 경우(HTTS→HTTPS)가 있었다.
- 설명은 자동으로 고치지 않는다.
- 검토 실패(review=None)면 자동 통과 후보 전부를 R5 실패 사유로 검수 필요에 넘긴다.
"""
from __future__ import annotations

import copy
import re
import unicodedata

_NON_WORD = re.compile(r"[\W_]+", re.UNICODE)
_DIGIT = re.compile(r"\d")
# 프롬프트가 description에 붙이게 한 단계 번호 "1) 2) 3)". 줄 처음이나 공백 뒤에 오는 것만 뺀다.
# "최댓값(40)"처럼 괄호 안 숫자는 공백 뒤가 아니라서 남는다.
_STEP_NUM = re.compile(r"(?:^|(?<=\s))\d{1,2}\)", re.MULTILINE)


def normalize(text: str) -> str:
    """R2 비교용 정규화: NFKC → 소문자 → 글자·숫자 말고 전부 제거."""
    text = unicodedata.normalize("NFKC", text or "")
    return _NON_WORD.sub("", text.lower())


def _has_number(el: dict) -> bool:
    parts = list(el.get("labels") or [])
    parts.append(_STEP_NUM.sub("", el.get("description") or ""))
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


# ---------------------------------------------------------------- R5 AI 교차 검토

R5_FAILED = "R5: AI 교차 검토를 완료하지 못해 검수자의 확인이 필요합니다."
R5_MISSING = "R5: AI 교차 검토에서 설명에 빠진 내용이 있을 수 있어 확인이 필요합니다."
R5_SUSPECT = 'R5: AI 교차 검토가 그림과 다를 수 있다고 본 문장: "{}"'
_R5_QUOTE_MAX = 120

_ISSUE = re.compile(r"^\s*설명\s*:\s*(.*?)\s*/\s*그림\s*:\s*(.*)$", re.DOTALL)
_MISSING_MARKS = {"", "(빠짐)", "빠짐", "(없음)", "없음", "-"}


def split_issue(issue: str) -> tuple[str, str] | None:
    """교차 검토 지적 "설명: A / 그림: B" → (A, B). 형식이 다르면 None."""
    m = _ISSUE.match(issue or "")
    if not m:
        return None
    return m.group(1).strip(), m.group(2).strip()


def review_reasons(issues: list[str]) -> list[str]:
    """한 그림의 지적 목록 → R5 사유. 의심 문장마다 1줄, 빠진 내용은 몇 개든 1줄."""
    reasons: list[str] = []
    missing = False
    for issue in issues:
        parts = split_issue(issue)
        if parts is None:
            continue  # parse_review가 이미 걸렀다. 여기 왔다면 사유로 쓰지 않는다
        quote = parts[0].strip(' "“”\'')
        if quote in _MISSING_MARKS:
            missing = True
            continue
        if len(quote) > _R5_QUOTE_MAX:
            quote = quote[:_R5_QUOTE_MAX] + "…"
        reason = R5_SUSPECT.format(quote)
        if reason not in reasons:
            reasons.append(reason)
    if missing:
        reasons.append(R5_MISSING)
    return reasons


def apply_review(page_result: dict, review: dict[str, list[str]] | None) -> dict:
    """flag_elements 결과에 R5를 붙인 새 dict. 자동 통과 후보(flagged=False)에만 적용한다.

    review: review_page 결과. 그림 id → 지적 목록, 검토 실패면 None.
    """
    result = copy.deepcopy(page_result)
    for el in result.get("elements", []):
        if el.get("flagged"):
            continue
        if review is None:
            reasons = [R5_FAILED]
        else:
            reasons = review_reasons(review.get(el.get("id"), []))
        if reasons:
            el["flagged"] = True
            el["flag_reasons"] = list(el.get("flag_reasons") or []) + reasons
    return result
