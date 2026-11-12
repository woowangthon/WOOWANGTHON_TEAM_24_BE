# 데이터 형식 (확정 2026-11-09, 수정 2026-11-11)

기능명세서 6장을 바탕으로, 빠져 있던 이름과 규칙을 채운 확정본이다. 바꿀 때는 영향받는 담당자 둘이 합의하고 이 파일과 예시를 같이 고친다.

| 예시 파일 | API | 쓰는 화면 |
|---|---|---|
| `document-list.json` | `GET /api/documents` | 대시보드, "검수 필요 N건"·"학생 신고 N건" 배지 (F5, F9) |
| `document-detail.json` | `GET /api/documents/{id}` | 검수 화면 (F6) |
| `student-document-detail.json` | `GET /api/student/documents/{id}` | 학생 열람 화면 (F7) |

- `document-detail.json`은 검수 중인 문서(검수 필요 1건 남음), `student-document-detail.json`은 같은 문서를 검수·공개한 뒤의 모습이다.
- 예시를 고치면 `python -m pytest tests/test_examples.py -q`로 규칙을 지키는지 확인한다.

## 공통 규칙

1. **id는 전부 DB가 붙이는 숫자다.** 문서·페이지·그림 모두. AI 함수가 주는 `"p1-e1"`은 페이지 안 번호라 문서끼리 겹치므로 저장하지 않는다.
2. **배열 칸은 null을 쓰지 않는다.** `labels`, `uncertain_parts`, `flag_reasons`, `elements`는 비면 `[]`.
3. **`table`은 `type`이 `table`일 때만** `{"headers": [...], "rows": [[...]]}`이고, 나머지는 `null`. DB 칸 이름은 `table_data`지만 API에서는 `table`.
   - 머리글 행이 없는 표는 `headers: []`이고 모든 행이 `rows`에 들어간다. 머리글을 지어내지 않는다.
   - 여러 행에 걸친 병합 칸은 첫 행에만 값이 있고 아래 행은 `""`다. 병합 사실은 `description`에 쓴다.
4. **`ai_original`은 null이 아니라 항상 `{"alt": "...", "description": "..."}`** 이다. AI가 처음 만든 문장이고, 검수자가 고치지 않았으면 지금 `alt`·`description`과 같다. 고친 그림(`edited`)만 다르다. DB에는 type·labels·table까지 보관하지만 API는 alt·description만 준다. 검수 화면의 "AI 원문 보기"와 발표의 수정 비율 집계에 쓴다.
5. **값 목록** (철자까지 그대로)
   - 문서 `status`: `processing`, `review_pending`, `published`, `failed`
   - 그림 `type`: `diagram`, `table`, `graph`, `photo`, `other`
   - 그림 `review_status`: `auto_passed`, `needs_review`, `approved`, `edited`
6. **시간**은 `"2026-11-06T21:30:00+09:00"` 형식. SQLite `CURRENT_TIMESTAMP`는 `"2026-11-06 12:30:00"`(UTC)이라 API에서 바꿔서 준다.
7. **오류 응답**은 FastAPI 기본 형식 `{"detail": "화면에 그대로 보여 줄 한국어 문장"}`. 예: `{"detail": "PDF는 20MB 이하여야 합니다."}`

## 검수 필요 사유 (`flag_reasons`)

AI 모듈(`app/ai/flags.py`)이 만드는 문구 그대로다. 프론트는 문자열을 그대로 보여 준다. 예시 문서의 사유는 실제 코드로 계산한 값이다.

| 규칙 | 문구 |
|---|---|
| R1 | `R1: AI가 확신하지 못한 부분: (uncertain_parts 항목)` — 항목마다 1줄 |
| R2 | `R2: 그림 속 글자 "(라벨)", "(라벨)"를 원문에서 찾지 못함` — 못 찾은 라벨을 한 줄에 모음 |
| R3 | `R3: 원문 글자가 없어 자동 확인 불가` |
| R4 | `R4: 숫자가 들어간 그래프` |
| R5 | `R5: AI 교차 검토가 그림과 다를 수 있다고 본 문장: "(설명의 일부)"` — 의심 문장마다 1줄 |
| R5 | `R5: AI 교차 검토에서 설명에 빠진 내용이 있을 수 있어 확인이 필요합니다.` — 빠진 내용 지적이 있으면 1줄 |
| R5 | `R5: AI 교차 검토를 완료하지 못해 검수자의 확인이 필요합니다.` — 검토가 형식 오류로 끝난 쪽 |
| 학생 신고 | `학생 신고: (내용)` — 내용이 비면 `학생 신고` |

- R5(이슈 #7)는 R1~R4에 걸리지 않은 자동 통과 후보에만 붙는다. R5가 붙으면 `flagged`가 true가 되어 `needs_review`로 저장된다. DB·API 칸은 바뀌지 않는다.
- R5 사유에는 설명의 의심 문장만 들어가고, AI가 제안한 정정 내용은 들어가지 않는다. 설명은 자동으로 고치지 않는다.

## 상태가 바뀌는 규칙

**그림 (`review_status`)**
- AI 결과 저장: `flagged`가 true면 `needs_review`, false면 `auto_passed`
- 검수자 승인: `approved` (사유 `flag_reasons`는 지우지 않고 남긴다)
- 검수자가 고친 뒤 승인: `edited`. `ai_original`은 바꾸지 않는다.
- 학생 신고: `needs_review`로 돌리고, `flagged`를 true로, `flag_reasons` 끝에 학생 신고 사유를 추가한다.

**문서 (`status`)**
- 업로드 직후 `processing` → 분석이 끝나면 `review_pending`
- AI 하루 한도 초과 등으로 분석이 멈추면 `failed`
- `POST /api/documents/{id}/publish`는 **문서가 `review_pending`이고 `needs_review`가 0건일 때만** 성공하고 `published`가 된다. 자동 통과 그림도 함께 공개된다. 서버와 공개 버튼이 같은 조건을 쓴다.
  - 이미 `published`면 409와 `{"detail": "이미 공개된 문서입니다."}`
  - `processing`이나 `failed`면 409와 `{"detail": "분석이 끝난 문서만 공개할 수 있어요."}`
  - `needs_review`가 남아 있으면 409와 `{"detail": "검수 필요 그림이 N건 남아 있어요."}`
- 공개 뒤 학생 신고나 수정이 생겨도 문서는 `published`를 유지하고, 고친 그림만 바로 바뀐다.

## 요청 본문

| API | 본문 | 응답 |
|---|---|---|
| `POST /api/login` | `{"role": "reviewer"}` 또는 `{"role": "student"}` | `{"role": "reviewer"}` |
| `POST /api/documents` | multipart: `file`, `title`, `course` | `{"id": 1}` |
| `GET /api/documents/{id}/progress` | — | `{"status": "processing", "processed_pages": 4, "total_pages": 12}` |
| `GET /api/pages/{id}/image` | — (`id`는 detail의 페이지 `id`, `page_no` 아님) | PNG |
| `PATCH /api/elements/{id}` | `{"action": "approve"}` 또는 `{"action": "edit", "alt": "...", "description": "..."}` | 바뀐 그림 1개 (detail의 그림과 같은 모양, `ai_original` 포함) |
| `POST /api/documents/{id}/publish` | — | `{"status": "published"}` |
| `POST /api/elements/{id}/reports` | `{"message": "화살표 방향이 반대 같아요"}` (빈 문자열 가능) | `{"ok": true}` |

## 화면 표시 규칙

- **검수 화면:** `needs_review`를 위로, `flag_reasons`를 경고색으로. "검수 완료 x / N"의 N은 `flagged`가 true인 그림 수, x는 그중 `approved`·`edited` 수 (예시 문서는 2 / 3). 문서가 `review_pending`이고 `needs_review`가 0이면 "학생에게 공개" 버튼을 켠다.
- **학생 화면:** 공개(`published`)된 문서만 나온다. 설명마다 글로 붙인다.
  - `approved`·`edited`: "검수자 확인"
  - `auto_passed`: "AI 설명(자동 통과)"
  - `needs_review`(공개 뒤 신고가 들어와 다시 검수 중): "검수자가 다시 확인 중"
  - 학생 API에는 `labels`, `uncertain_parts`, `flagged`, `flag_reasons`, `ai_original`을 넣지 않는다.
- **목록 배지:** `needs_review_count`(검수 필요 N건), `report_count`(학생 신고 N건: 신고가 있고 아직 `needs_review`인 그림 수).
