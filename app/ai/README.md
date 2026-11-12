# AI 모듈 (`app/ai/`)

기능명세서 F3(그림 설명 생성)·F4(검수 필요 선별, R1~R5) 구현. 담당 박선호, 백업 이준원.

## 설치·확인

```bash
pip install -r requirements-ai.txt
cp .env.example .env              # 키 넣기. .env는 커밋 금지 (저장소가 Public)
python -m pytest tests/test_ai.py -q
AI_MODE=mock python scripts/try_pages.py sample.pdf --pages 1-5   # 키 없이 흐름 확인
python scripts/try_pages.py sample.pdf --pages 1-5                # 실제 호출
```

## 백엔드에서 쓰는 법

```python
from app.ai import (analyze_page, flag_elements, apply_review, review_enabled, review_page,
                    AIQuotaExhausted, AIUnavailable)

for page in pages:                                   # 한 쪽씩 순서대로
    try:
        result = analyze_page(page.png, page_no=page.no)
        result = flag_elements(result, page.text)    # R1~R4. F2에서 뽑은 텍스트 레이어
        if review_enabled() and any(not e["flagged"] for e in result["elements"]):
            result = apply_review(result, review_page(page.png, result))   # R5
    except (AIQuotaExhausted, AIUnavailable):
        document.status = "failed"                   # 남은 쪽을 억지로 채우지 않는다
        break
    save(result)                                      # 6장 DB의 elements로
```

실제 연결은 `app/pdf_processing.py`의 `analyze_document`.

## 이준원 님과 맞출 것

1. **계약 변경 제안:** `analyze_page(image_png)` → `analyze_page(image_png, page_no)`. 결과의 `page_no`와 id `"p3-e1"`을 만들려면 페이지 번호가 필요하다.
   결과의 그림 id(`"p3-e1"`)는 페이지 안 번호라 문서끼리 겹친다. DB·API의 그림 id는 DB 숫자를 쓰고(`examples/CONTRACT.md`), 이 값은 저장하지 않는다.
2. **uvicorn worker는 1개.** 호출 간격(무료 등급 분당 한도)을 프로세스 안에서 지키기 때문에, worker가 여럿이면 429가 난다. 여러 쪽을 동시에 호출해도 어차피 줄을 서므로 빨라지지 않는다.
3. **하루 한도 초과는 예외로 올라온다.** `AIQuotaExhausted`를 받으면 문서를 `failed`로. 한도는 한국 시간 17:00에 초기화된다(태평양 자정). 키가 아니라 프로젝트 단위라 둘의 테스트가 한도를 같이 쓴다.
4. **'분석 실패' 그림.** 형식 오류가 2번 나면 `title: "분석 실패"`, 설명이 빈 그림 1개가 들어가고 R1로 검수 필요가 된다. 검수 화면에서 설명을 비운 채 승인하지 못하게 막는 게 좋다(현지 님 공유).

## 명세에 없어서 해석한 부분

| 항목 | 해석 | 이유 |
|---|---|---|
| R3 | 페이지 텍스트가 비면 모든 그림, 텍스트가 있어도 글자가 든 사진(photo)이면 R3 | 사진 속 글자는 텍스트 레이어에 없어 R2가 "못 찾음"이라는 틀린 사유를 붙인다 |
| R4 | graph 타입이고 라벨·설명·표에 숫자가 있으면. 설명의 단계 번호 "1) 2)"는 세지 않음 | "수치가 들어간 그래프"를 코드로 옮긴 것. 단계 번호는 프롬프트가 붙이게 해서 모든 그래프가 걸렸음 |
| 응답 검증 | title·alt·description이 공백뿐이거나, 표에 데이터 행이 없거나 칸이 전부 비었거나, 행의 칸 수가 머리글(머리글 없는 표는 1행)과 다르면 형식 오류로 보고 재시도. 원본에 머리글 행이 없는 표는 `headers: []` | 빈 설명이 검수 없이 자동 통과하지 않게 |
| 재시도 | 형식 오류만 1회. 분당 한도 429는 서버가 알려 준 시간만큼 기다렸다 재시도(횟수 미포함), 5xx·네트워크는 최대 5회 | 한도 초과가 그림 오류처럼 검수 필요로 쌓이지 않게 |
| 응답 스키마 | 표 rows를 `{"cells": [...]}`로 받아 `[[...]]`로 변환, id·page_no는 코드가 붙임 | 중첩 배열 스키마 지원이 모델마다 확실하지 않음. 모델이 번호를 지어내지 않게 |

## 한계 (발표 "오류와 한계"에 쓸 것)

- R2는 지어낸 글자만 잡는다. 화살표 방향·연결 관계 오류는 못 잡는다(명세 5장).
- "A", "1" 같은 한 글자 라벨은 원문 어딘가에 거의 항상 있어서 R2 검사가 사실상 안 된다.
- 무료 등급 분당 15회 한도 때문에 호출 사이에 약 4.2초를 둔다. 30쪽이면 대기만 2분 이상이고,
  실제 시간은 호출 응답 시간까지 더해진다. 실험 리포트의 '평균 초/쪽'으로 계산할 것.
- 무료 등급 하루 500회(3.5 Flash-Lite), 20회(3.8 Flash). 개발 중 반복 테스트는 AI_MODE=mock으로.
- 11/10 샘플 10쪽 실측(프롬프트 v4): 자동 통과 그림 6개 중 3개에 화살표·구간 오류, 1개에 선 위 숫자 누락. 표본이 작아 비율은 추정.

## R5 AI 교차 검토 (`review.py`, 이슈 #7에서 결정)

설명을 만든 뒤 이미지와 설명을 다시 보내 그림과 다른 곳을 찾게 하고, 지적이 있으면 검수 필요로 보낸다.

- **대상:** R1~R4에 걸리지 않은 자동 통과 후보만. 후보가 없는 쪽은 검토 호출을 하지 않는다.
- **사유:** 설명의 의심 문장만 넣고, AI가 말한 "그림: …" 정정 내용은 넣지 않는다(11/10 실험에서 정정 자체가 틀린 경우가 있었음). 설명은 자동으로 고치지 않는다.
  - `R5: AI 교차 검토가 그림과 다를 수 있다고 본 문장: "…"` — 의심 문장마다 1줄
  - `R5: AI 교차 검토에서 설명에 빠진 내용이 있을 수 있어 확인이 필요합니다.` — 빠진 내용 지적이 있으면 1줄
  - `R5: AI 교차 검토를 완료하지 못해 검수자의 확인이 필요합니다.` — 형식 오류 2번(검토 실패)이면 그 쪽의 후보 전부
- **실패 처리:** 하루 한도 초과·서버 오류 반복은 분석과 같이 예외 → 문서 failed. 호출 간격 제한도 분석과 같은 것을 쓴다.
- **끄기:** `.env`에 `AI_REVIEW=off`. 검토 모델만 바꾸려면 `AI_REVIEW_MODEL`.
- **시간·한도:** 그림 있는 쪽마다 호출 1번 더(11/10 실험 쪽당 2.8~8.4초). 하루 500회 한도로 약 250쪽.
- **한계:** 처음 보는 10쪽 실험에서 틀린 그림 2개 중 1개만 잡았고, 맞는 그림 7개 중 3개를 잘못 의심했다. 같은 모델이라 같은 실수를 반복할 수 있다. 학생 화면 "AI 설명(자동 통과)" 표시와 학생 신고는 유지한다.

실험 스크립트(모든 그림을 검토하고 리포트에 지적을 그대로 적는다. 서버는 자동 통과 후보만 검토):

```bash
python scripts/try_pages.py "시연용 샘플.pdf" --pages 2,3,9-13,15,22,26 --review
python scripts/try_pages.py "시연용 샘플.pdf" --pages 11 --review --review-model gemini-3.8-flash   # 검토 모델만 바꾸기
```

## 출처

- Google Gen AI SDK for Python (`google-genai`), Gemini API — 외부 기술
- PyMuPDF — 페이지 렌더링·텍스트 추출 (AGPL-3.0, 출처 표기 필요)
