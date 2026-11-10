"""examples/*.json이 데이터 형식 규칙(examples/CONTRACT.md)을 지키는지 확인한다.
예시를 고친 뒤 실행: python -m pytest tests/test_examples.py -q
"""
import json
from pathlib import Path

EX = Path(__file__).resolve().parents[1] / "examples"

DOC_STATUS = {"processing", "review_pending", "published", "failed"}
REVIEW_STATUS = {"auto_passed", "needs_review", "approved", "edited"}
TYPES = {"diagram", "table", "graph", "photo", "other"}
ELEMENT_KEYS = {"id", "type", "title", "alt", "description", "labels", "table",
                "uncertain_parts", "flagged", "flag_reasons", "review_status", "ai_original"}
STUDENT_ELEMENT_KEYS = {"id", "type", "title", "alt", "description", "table", "review_status"}
REASON_PREFIXES = ("R1: ", "R2: ", "R3: ", "R4: ", "학생 신고")


def load(name):
    return json.loads((EX / name).read_text(encoding="utf-8"))


def check_table(e):
    if e["type"] == "table":
        assert set(e["table"]) == {"headers", "rows"}
        assert all(isinstance(r, list) for r in e["table"]["rows"])
    else:
        assert e["table"] is None


def detail_elements():
    d = load("document-detail.json")
    return [e for p in d["pages"] for e in p["elements"]]


def test_document_detail():
    d = load("document-detail.json")
    assert d["status"] in DOC_STATUS and isinstance(d["id"], int)
    ids = []
    for p in d["pages"]:
        assert set(p) == {"id", "page_no", "text", "elements"}
        assert isinstance(p["id"], int) and isinstance(p["page_no"], int) and isinstance(p["text"], str)
        for e in p["elements"]:
            assert set(e) == ELEMENT_KEYS
            assert isinstance(e["id"], int)
            assert e["type"] in TYPES and e["review_status"] in REVIEW_STATUS
            for k in ("labels", "uncertain_parts", "flag_reasons"):
                assert isinstance(e[k], list)
            assert e["flagged"] == bool(e["flag_reasons"])
            if not e["flagged"]:
                assert e["review_status"] in {"auto_passed", "approved", "edited"}
            if e["review_status"] == "auto_passed":
                assert not e["flagged"]
            check_table(e)
            ids.append(e["id"])
    assert len(ids) == len(set(ids)), "그림 id가 겹침"


def test_flag_reasons_use_code_wording():
    for e in detail_elements():
        for reason in e["flag_reasons"]:
            assert reason.startswith(REASON_PREFIXES), reason
        r1 = [r for r in e["flag_reasons"] if r.startswith("R1: ")]
        assert r1 == [f"R1: AI가 확신하지 못한 부분: {u}" for u in e["uncertain_parts"]]


def test_ai_original_is_object():
    for e in detail_elements():
        assert set(e["ai_original"]) == {"alt", "description"}
        same = e["ai_original"] == {"alt": e["alt"], "description": e["description"]}
        # 고친 그림만 AI 원문과 다르다
        assert same == (e["review_status"] != "edited"), e["id"]


def test_document_list_counts_match_detail():
    lst = load("document-list.json")
    d = load("document-detail.json")
    for doc in lst:
        assert doc["status"] in DOC_STATUS
        assert 0 <= doc["processed_pages"] <= doc["total_pages"]
    same = next(x for x in lst if x["id"] == d["id"])
    needs = sum(e["review_status"] == "needs_review" for e in detail_elements())
    assert same["needs_review_count"] == needs


def test_student_detail_hides_internal_fields():
    s = load("student-document-detail.json")
    for p in s["pages"]:
        assert set(p) == {"page_no", "text", "elements"}
        for e in p["elements"]:
            assert set(e) == STUDENT_ELEMENT_KEYS
            assert e["review_status"] in REVIEW_STATUS
            check_table(e)


def test_student_detail_matches_published_detail():
    """학생 예시는 검수 예시 문서를 검수·공개한 뒤 모습이다: 같은 그림, 남은 검수 필요 0건."""
    s = load("student-document-detail.json")
    student = {e["id"]: e for p in s["pages"] for e in p["elements"]}
    reviewer = {e["id"]: e for e in detail_elements()}
    assert student.keys() == reviewer.keys()
    assert all(e["review_status"] != "needs_review" for e in student.values())
