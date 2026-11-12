import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

import app.db as db
from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    # 실제 DB 대신 테스트용 임시 DB 사용
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()

    with sqlite3.connect(db.DB_PATH) as connection:
        for doc_id, status in [(1, "published"), (2, "processing")]:
            connection.execute(
                """
                INSERT INTO documents
                    (id, title, course, filename, status, total_pages, processed_pages)
                VALUES (?, '테스트', '과목', 'test.pdf', ?, 1, 1)
                """,
                (doc_id, status),
            )
            connection.execute(
                """
                INSERT INTO pages (id, document_id, page_no, image_path, text)
                VALUES (?, ?, 1, '', '')
                """,
                (doc_id, doc_id),
            )
            connection.execute(
                """
                INSERT INTO elements
                    (id, page_id, type, title, alt, description, labels,
                     uncertain_parts, flagged, flag_reasons, review_status, ai_original)
                VALUES (?, ?, 'diagram', '그림', '요약', '설명', '[]',
                        '[]', 0, '[]', 'auto_passed', ?)
                """,
                (doc_id, doc_id, json.dumps({"alt": "요약", "description": "설명"})),
            )

    with TestClient(app) as test_client:
        yield test_client


def test_published_element_can_be_reported(client):
    response = client.post(
        "/api/elements/1/reports",
        json={"message": "화살표가 반대예요"},
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True}

    detail = client.get("/api/documents/1").json()
    element = detail["pages"][0]["elements"][0]
    assert detail["status"] == "published"
    assert element["review_status"] == "needs_review"
    assert element["flagged"] is True
    assert "학생 신고: 화살표가 반대예요" in element["flag_reasons"]

    documents = client.get("/api/documents").json()
    document = next(item for item in documents if item["id"] == 1)
    assert document["report_count"] == 1


def test_unpublished_or_missing_element_cannot_be_reported(client):
    for element_id in (2, 9999):
        response = client.post(
            f"/api/elements/{element_id}/reports",
            json={"message": ""},
        )
        assert response.status_code == 404

    with sqlite3.connect(db.DB_PATH) as connection:
        assert connection.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 0


def test_empty_message_is_allowed(client):
    response = client.post("/api/elements/1/reports", json={"message": ""})
    assert response.status_code == 200

    detail = client.get("/api/documents/1").json()
    assert "학생 신고" in detail["pages"][0]["elements"][0]["flag_reasons"]