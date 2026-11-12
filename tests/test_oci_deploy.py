"""Deployment tools only operate on temporary data or mocked HTTP responses."""

import json
import sqlite3
from contextlib import closing

import pytest

from deploy.oci.backup_data import backup_data, verify_backup
from deploy.oci import smoke_check


@pytest.fixture
def data_dir(tmp_path):
    directory = tmp_path / "data"
    directory.mkdir()
    with closing(sqlite3.connect(directory / "app.db")) as db, db:
        db.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY, status TEXT)")
        db.execute("INSERT INTO documents VALUES (1, 'review_pending')")
    (directory / "uploads").mkdir()
    (directory / "uploads" / "1.pdf").write_bytes(b"temporary PDF")
    (directory / "pages" / "1").mkdir(parents=True)
    (directory / "pages" / "1" / "1.png").write_bytes(b"temporary PNG")
    return directory


def test_backup_includes_uncheckpointed_wal_and_preserves_assets(data_dir, tmp_path):
    # Keep the WAL connection open so copying only app.db would lose this row.
    with closing(sqlite3.connect(data_dir / "app.db")) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("INSERT INTO documents VALUES (2, 'published')")
        writer.commit()
        assert (data_dir / "app.db-wal").is_file()
        destination = backup_data(data_dir, tmp_path / "backups", quiesced=True)
        verify_backup(destination)
        with closing(sqlite3.connect(destination / "app.db")) as restored:
            assert restored.execute("SELECT * FROM documents ORDER BY id").fetchall() == [
                (1, "review_pending"), (2, "published"),
            ]
        for name in ("uploads/1.pdf", "pages/1/1.png"):
            assert (destination / name).read_bytes() == (data_dir / name).read_bytes()
        assert not (destination / "app.db-wal").exists()
        assert not (destination / "app.db-shm").exists()
    again = backup_data(data_dir, tmp_path / "backups", quiesced=True)
    assert again != destination
    assert destination.is_dir()


def test_backup_requires_quiescence(data_dir, tmp_path):
    with pytest.raises(ValueError, match="Stop the service"):
        backup_data(data_dir, tmp_path / "backups", quiesced=False)
    with closing(sqlite3.connect(data_dir / "app.db")) as db, db:
        db.execute("UPDATE documents SET status = 'processing'")
    with pytest.raises(ValueError, match="Processing documents"):
        backup_data(data_dir, tmp_path / "backups", quiesced=True)
    assert not (tmp_path / "backups").exists()


def test_backup_refuses_missing_db_and_destination_inside_data(data_dir, tmp_path):
    with pytest.raises(ValueError, match="outside"):
        backup_data(data_dir, data_dir / "backups", quiesced=True)
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError, match="existing app.db"):
        backup_data(empty, tmp_path / "backups", quiesced=True)
    assert not (empty / "app.db").exists()


def test_backup_detects_corruption_and_manifest_path_escape(data_dir, tmp_path):
    destination = backup_data(data_dir, tmp_path / "backups", quiesced=True)
    (destination / "uploads" / "1.pdf").write_bytes(b"changed")
    with pytest.raises(ValueError, match="checksum"):
        verify_backup(destination)
    manifest_path = destination / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][0]["path"] = "../../must-not-be-opened"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="leaves"):
        verify_backup(destination)


@pytest.fixture
def http_responses(monkeypatch):
    index = b'<html><script src="/assets/app.js"></script><link rel="stylesheet" href="/assets/app.css"></html>'
    response = smoke_check.Response
    mapping = {
        "/api/health": response(200, "application/json", b'{"status":"ok"}'),
        "/docs": response(200, "text/html", b"Swagger UI"),
        "/api/documents/not-an-integer": response(422, "application/json", b'{"detail":[]}'),
        "/": response(200, "text/html", index),
        "/dashboard": response(200, "text/html", index),
        "/review/8": response(200, "text/html", index),
        "/assets/app.js": response(200, "text/javascript", b"console.log(1)"),
        "/assets/app.css": response(200, "text/css", b"body{}"),
        "/openapi.json": response(200, "application/json", b'{"paths":{"/api/health":{}}}'),
    }
    for path in ("/api", "/api/", "/api/not-existing"):
        mapping[path] = response(404, "application/json", json.dumps({"detail": "없는 API 주소예요."}).encode())
    monkeypatch.setattr(smoke_check, "fetch", lambda url: mapping[url.removeprefix("http://localhost:8000")])
    return mapping


def test_smoke_checks_valid_responses(http_responses):
    smoke_check.run_checks("http://localhost:8000")


@pytest.mark.parametrize("path", ["/api/health", "/api/not-existing", "/api/documents/not-an-integer", "/assets/app.js"])
def test_smoke_checks_fail_when_html_shadows_routes(http_responses, path):
    http_responses[path] = http_responses["/"]
    with pytest.raises(ValueError):
        smoke_check.run_checks("http://localhost:8000")


def test_smoke_checks_reject_different_spa_html(http_responses):
    http_responses["/dashboard"].body = b"Different HTML"
    with pytest.raises(ValueError, match="SPA navigation"):
        smoke_check.run_checks("http://localhost:8000")


def test_smoke_checks_reject_credentials_and_external_assets(http_responses):
    with pytest.raises(ValueError, match="without credentials"):
        smoke_check.run_checks("http://secret@localhost:8000")
    for path in ("/", "/dashboard", "/review/8"):
        http_responses[path].body = b'<script src="https://another-host.example/app.js"></script>'
    with pytest.raises(ValueError, match="another origin"):
        smoke_check.run_checks("http://localhost:8000")
