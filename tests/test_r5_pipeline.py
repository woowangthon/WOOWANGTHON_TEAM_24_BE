"""R5 연결 테스트: 업로드된 PDF → analyze_document → DB 저장까지 (AI 호출은 가짜).

python -m pytest tests/test_r5_pipeline.py -q
"""
import pymupdf
import pytest

import app.ai
import app.db as db
import app.pdf_processing as pp
from app.ai import AIQuotaExhausted
from app.ai.flags import R5_FAILED


def _el(i, page_no, labels):
    return {"id": f"p{page_no}-e{i}", "type": "diagram", "title": f"그림{i}", "alt": "a",
            "description": "왼쪽에서 오른쪽으로 화살표가 있습니다.", "labels": labels,
            "table": None, "uncertain_parts": []}


@pytest.fixture
def doc(tmp_path, monkeypatch):
    """2쪽 PDF를 올린 상태(페이지 추출까지)의 문서 id."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "data" / "app.db")
    monkeypatch.setattr(pp, "DB_PATH", tmp_path / "data" / "app.db")
    db.init_db()
    pdf = pymupdf.open()
    for _ in range(2):
        pdf.new_page().insert_text((72, 72), "Client Server")
    doc_id = db.create_document("t", "c", "s.pdf", 2, pdf.tobytes())
    pp.extract_document_pages(doc_id)
    return doc_id


@pytest.fixture
def fake_ai(monkeypatch):
    """1쪽: 자동 통과 후보 1개(e1) + R2로 걸리는 그림 1개(e2) / 2쪽: R2로 걸리는 그림만."""
    calls = {"review": []}

    def analyze(image_png, page_no):
        if page_no == 1:
            return {"page_no": 1, "elements": [_el(1, 1, ["Client"]), _el(2, 1, ["없는글자"])]}
        return {"page_no": 2, "elements": [_el(1, 2, ["없는글자"])]}

    def review(image_png, page):
        calls["review"].append(page["page_no"])
        return calls["result"]

    monkeypatch.setattr(app.ai, "analyze_page", analyze)
    monkeypatch.setattr(app.ai, "review_page", review)
    monkeypatch.delenv("AI_REVIEW", raising=False)
    calls["result"] = {"p1-e1": ["설명: 왼쪽에서 오른쪽으로 / 그림: 양방향"], "p1-e2": ["설명: x / 그림: y"]}
    return calls


def _elements(doc_id):
    detail = db.get_document_detail(doc_id)
    return detail["status"], [e for p in detail["pages"] for e in p["elements"]]


def test_r5_flags_auto_pass_candidate_only(doc, fake_ai):
    pp.analyze_document(doc)
    status, els = _elements(doc)
    assert status == "review_pending"
    assert fake_ai["review"] == [1]                      # 후보가 없는 2쪽은 검토 호출 안 함
    e1, e2, e3 = els
    assert e1["review_status"] == "needs_review" and e1["flagged"] is True
    assert e1["flag_reasons"] == ['R5: AI 교차 검토가 그림과 다를 수 있다고 본 문장: "왼쪽에서 오른쪽으로"']
    assert e1["description"] == "왼쪽에서 오른쪽으로 화살표가 있습니다."   # 설명은 고치지 않음
    assert all(not r.startswith("R5") for r in e2["flag_reasons"] + e3["flag_reasons"])


def test_r5_no_issue_stays_auto_passed(doc, fake_ai):
    fake_ai["result"] = {"p1-e1": [], "p1-e2": []}
    pp.analyze_document(doc)
    _, els = _elements(doc)
    assert els[0]["review_status"] == "auto_passed" and els[0]["flag_reasons"] == []


def test_r5_review_failure_goes_to_needs_review(doc, fake_ai):
    fake_ai["result"] = None
    pp.analyze_document(doc)
    _, els = _elements(doc)
    assert els[0]["review_status"] == "needs_review" and els[0]["flag_reasons"] == [R5_FAILED]


def test_r5_off_skips_review(doc, fake_ai, monkeypatch):
    monkeypatch.setenv("AI_REVIEW", "off")
    pp.analyze_document(doc)
    _, els = _elements(doc)
    assert fake_ai["review"] == [] and els[0]["review_status"] == "auto_passed"


def test_r5_quota_error_is_not_swallowed(doc, fake_ai, monkeypatch):
    def quota(image_png, page):
        raise AIQuotaExhausted("하루 한도")
    monkeypatch.setattr(app.ai, "review_page", quota)
    with pytest.raises(AIQuotaExhausted):               # 하위 프로세스 실패 → 부모가 문서 failed
        pp.analyze_document(doc)
    assert db.get_document_detail(doc)["status"] == "processing"
