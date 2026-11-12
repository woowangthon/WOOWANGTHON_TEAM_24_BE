"""HTTP regression tests using only temporary builds, uploads, images, and SQLite."""

from urllib.parse import quote

import pymupdf
import pytest
from fastapi.testclient import TestClient

import app.db as db
import app.main as main
import app.pdf_processing as pdf_processing


INDEX = "<!doctype html><html><body>Temporary frontend</body></html>"


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "data" / "app.db"
    for module in (db, main, pdf_processing):
        monkeypatch.setattr(module, "DB_PATH", db_path)
    monkeypatch.setattr(main, "DIST", tmp_path / "frontend_dist")
    with TestClient(main.app) as client:
        yield client


@pytest.fixture
def build(client):
    main.DIST.mkdir()
    (main.DIST / "index.html").write_text(INDEX, encoding="utf-8")
    return main.DIST


def test_health_and_docs_without_build(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["content-type"].startswith("application/json")
    response = client.get("/docs")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Swagger UI" in response.text


@pytest.mark.parametrize("path", ["/", "/dashboard", "/review/8", "/assets/app.js"])
def test_missing_index(client, path):
    # An asset without index.html still gets the specified missing-build error.
    (main.DIST / "assets").mkdir(parents=True)
    (main.DIST / "assets" / "app.js").write_text("console.log('test')")
    response = client.get(path)
    assert response.status_code == 404
    assert response.json() == {"detail": "화면 파일이 아직 배포되지 않았어요."}


@pytest.mark.parametrize("path", ["/", "/dashboard", "/review/8", "/apiary"])
def test_spa_navigation(client, build, path):
    response = client.get(path)
    assert response.status_code == 200
    assert response.text == INDEX
    assert response.headers["content-type"].startswith("text/html")


@pytest.mark.parametrize(
    "filename,content,media_types",
    [
        ("app.js", b"console.log('temporary');", ("text/javascript", "application/javascript")),
        ("app.css", b"body { color: blue; }", ("text/css",)),
        ("image.png", b"\x89PNG\r\n\x1a\n", ("image/png",)),
    ],
)
def test_static_assets(client, build, filename, content, media_types):
    (build / "assets").mkdir()
    (build / "assets" / filename).write_bytes(content)
    response = client.get(f"/assets/{filename}")
    assert response.status_code == 200
    assert response.content == content
    assert response.headers["content-type"].split(";")[0] in media_types


@pytest.mark.parametrize("has_build", [False, True])
@pytest.mark.parametrize("path", ["/api", "/api/", "/api/not-existing"])
def test_unknown_api_always_returns_json(client, has_build, path):
    if has_build:
        main.DIST.mkdir()
        (main.DIST / "index.html").write_text(INDEX, encoding="utf-8")
    response = client.get(path)
    assert response.status_code == 404
    assert response.json() == {"detail": "없는 API 주소예요."}
    assert response.headers["content-type"].startswith("application/json")


def test_cannot_serve_files_outside_dist(client, build, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("must never be served", encoding="utf-8")
    paths = [
        "/%2e%2e/outside.txt",
        "/..%2foutside.txt",
        "/..%5coutside.txt",
        "/%5c..%5coutside.txt",
        "/" + quote(str(outside), safe=""),
    ]
    for path in paths:
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.text == INDEX, path


def test_openapi_excludes_fallback(client, build):
    response = client.get("/openapi.json")
    assert response.status_code == 200
    paths = response.json()["paths"]
    assert "/api/health" in paths
    assert "/" not in paths
    assert not any("full_path" in path for path in paths)
    assert main.app.routes[-1].path == "/{full_path:path}"


@pytest.mark.parametrize(
    "method,path,body,status,detail",
    [
        ("GET", "/api/documents/999", None, 404, "문서를 찾을 수 없습니다."),
        ("GET", "/api/documents/999/progress", None, 404, "문서를 찾을 수 없습니다."),
        ("GET", "/api/student/documents/999", None, 404, "공개된 문서를 찾을 수 없습니다."),
        ("GET", "/api/pages/999/image", None, 404, "페이지를 찾을 수 없습니다."),
        ("PATCH", "/api/elements/999", {"action": "approve"}, 404, "그림을 찾을 수 없습니다."),
        ("POST", "/api/documents/999/publish", None, 404, "문서를 찾을 수 없습니다."),
        ("GET", "/api/documents/not-an-integer", None, 422, None),
        ("PATCH", "/api/elements/999", {"action": "invalid"}, 422, None),
        ("POST", "/api/documents", None, 422, None),
    ],
)
def test_existing_api_errors(client, build, method, path, body, status, detail):
    response = client.request(method, path, json=body)
    assert response.status_code == status
    assert response.headers["content-type"].startswith("application/json")
    if detail is not None:
        assert response.json() == {"detail": detail}
    else:
        assert isinstance(response.json()["detail"], list)


def test_existing_api_upload_review_publish_and_student_flow(client, build, monkeypatch):
    # Record the background job instead of starting PDF/AI subprocesses.
    jobs = []
    monkeypatch.setattr(main, "process_document_in_background", jobs.append)
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((72, 72), "Client Server")
        pdf_bytes = pdf.tobytes()
    response = client.post(
        "/api/documents",
        data={"title": " Test lecture ", "course": " Test course "},
        files={"file": ("test.pdf", pdf_bytes, "application/pdf")},
    )
    assert response.status_code == 201
    document_id = response.json()["id"]
    assert jobs == [document_id]
    url = f"/api/documents/{document_id}"
    response = client.get(f"{url}/progress")
    assert response.status_code == 200
    assert response.json() == {"status": "processing", "processed_pages": 0, "total_pages": 1}
    assert client.get("/api/student/documents").json() == []
    assert client.get(f"/api/student/documents/{document_id}").status_code == 404
    assert client.post(f"{url}/publish").status_code == 409

    # Extract the temporary PDF and supply a fixed analysis without calling AI.
    pdf_processing.extract_document_pages(document_id)
    element = {
        "type": "diagram", "title": "Test diagram", "alt": "Original alt",
        "description": "Original description", "labels": [], "table": None,
        "uncertain_parts": [], "flagged": True, "flag_reasons": ["Test review reason"],
    }
    assert db.save_document_analysis(document_id, {1: [element]})
    response = client.get(url)
    assert response.status_code == 200
    document = response.json()
    assert document["status"] == "review_pending"
    page = document["pages"][0]
    element_id = page["elements"][0]["id"]
    response = client.get(f"/api/pages/{page['id']}/image")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content == (db.DB_PATH.parent / "pages" / str(document_id) / "1.png").read_bytes()
    response = client.get("/api/documents")
    assert response.status_code == 200
    assert response.json()[0]["needs_review_count"] == 1
    assert response.json()[0]["title"] == "Test lecture"
    response = client.post(f"{url}/publish")
    assert response.status_code == 409
    assert response.json() == {"detail": "검수 필요 그림이 1건 남아 있어요."}

    element_url = f"/api/elements/{element_id}"
    response = client.patch(element_url, json={"action": "approve"})
    assert response.status_code == 200
    assert response.json()["review_status"] == "approved"
    response = client.patch(
        element_url,
        json={"action": "edit", "alt": "Edited alt", "description": "Edited description"},
    )
    assert response.status_code == 200
    assert response.json()["review_status"] == "edited"
    assert response.json()["ai_original"] == {"alt": "Original alt", "description": "Original description"}
    response = client.post(f"{url}/publish")
    assert response.status_code == 200
    assert response.json() == {"status": "published"}
    response = client.get("/api/student/documents")
    assert response.status_code == 200
    assert [document["id"] for document in response.json()] == [document_id]
    response = client.get(f"/api/student/documents/{document_id}")
    assert response.status_code == 200
    student_element = response.json()["pages"][0]["elements"][0]
    assert set(student_element) == {"id", "type", "title", "alt", "description", "table", "review_status"}
    assert student_element["alt"] == "Edited alt"
    response = client.post(f"{url}/publish")
    assert response.status_code == 409
    assert response.json() == {"detail": "이미 공개된 문서입니다."}
