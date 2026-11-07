# AI 모듈 (`app/ai/`)

기능명세서 F3(그림 설명 생성)·F4(검수 필요 선별) 구현. 담당 박선호, 백업 이준원.

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
from app.ai import analyze_page, flag_elements, AIQuotaExhausted, AIUnavailable

for page in pages:                                   # 한 쪽씩 순서대로
    try:
        result = analyze_page(page.png, page_no=page.no)
    except (AIQuotaExhausted, AIUnavailable):
        document.status = "failed"                   # 남은 쪽을 억지로 채우지 않는다
        break
    result = flag_elements(result, page.text)        # F2에서 뽑은 텍스트 레이어
    save(result)                                      # 6장 DB의 elements로
    document.processed_pages += 1
```

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
| R4 | graph 타입이고 라벨·설명·표에 숫자가 있으면 | "수치가 들어간 그래프"를 코드로 옮긴 것 |
| 재시도 | 형식 오류만 1회. 분당 한도 429는 서버가 알려 준 시간만큼 기다렸다 재시도(횟수 미포함), 5xx·네트워크는 최대 5회 | 한도 초과가 그림 오류처럼 검수 필요로 쌓이지 않게 |
| 응답 스키마 | 표 rows를 `{"cells": [...]}`로 받아 `[[...]]`로 변환, id·page_no는 코드가 붙임 | 중첩 배열 스키마 지원이 모델마다 확실하지 않음. 모델이 번호를 지어내지 않게 |

## 한계 (발표 "오류와 한계"에 쓸 것)

- R2는 지어낸 글자만 잡는다. 화살표 방향·연결 관계 오류는 못 잡는다(명세 5장).
- "A", "1" 같은 한 글자 라벨은 원문 어딘가에 거의 항상 있어서 R2 검사가 사실상 안 된다.
- 무료 등급 분당 15회 한도 때문에 호출 사이에 약 4.2초를 둔다. 30쪽이면 대기만 2분 이상이고,
  실제 시간은 호출 응답 시간까지 더해진다. 실험 리포트의 '평균 초/쪽'으로 계산할 것.
- 무료 등급 하루 500회(3.5 Flash-Lite), 20회(3.8 Flash). 개발 중 반복 테스트는 AI_MODE=mock으로.

## 출처

- Google Gen AI SDK for Python (`google-genai`), Gemini API — 외부 기술
- PyMuPDF — 페이지 렌더링·텍스트 추출 (AGPL-3.0, 출처 표기 필요)
