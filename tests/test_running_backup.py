"""Full online backups use only temporary databases/files, including fake secrets."""

import json
import os
import sqlite3
from contextlib import closing

import pytest

from deploy.oci import backup_data as backup


IDLE = {
    "service_active": True, "worker_count": 1, "main_pid": 123,
    "pdf_jobs": 0, "established_connections": 0, "writable_data_files": 0,
}


@pytest.fixture
def running_app(tmp_path):
    app = tmp_path / "app"
    (app / "data/uploads").mkdir(parents=True)
    (app / "data/pages/1").mkdir(parents=True)
    (app / "frontend_dist/assets").mkdir(parents=True)
    with closing(sqlite3.connect(app / "data/app.db")) as db, db:
        db.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY, status TEXT)")
        db.execute("INSERT INTO documents VALUES (1, 'review_pending')")
    (app / "data/uploads/1.pdf").write_bytes(b"temporary PDF")
    (app / "data/pages/1/1.png").write_bytes(b"temporary PNG")
    (app / "frontend_dist/index.html").write_bytes(b"<html>temporary frontend</html>")
    (app / "frontend_dist/assets/app.js").write_bytes(b"console.log(1)")
    (app / ".env").write_bytes(b"TEST_SECRET=synthetic-secret-not-for-output\n")
    unit = tmp_path / "boinun.service"
    unit.write_bytes(b"[Service]\nUser=opc\nEnvironment=TEST_ONLY=synthetic\n")
    return app, tmp_path / "private-backups", unit


def test_running_backup_includes_all_sources_and_wal(running_app, capsys):
    app, root, unit = running_app
    with closing(sqlite3.connect(app / "data/app.db")) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("INSERT INTO documents VALUES (2, 'published')")
        writer.commit()
        destination = backup.backup_app(app, root, unit, idle_check=lambda: IDLE)
    backup.verify_backup(destination)
    manifest = json.loads((destination / "manifest.json").read_text())
    assert manifest["version"] == 2
    assert manifest["consistency"] == "live-idle-checked"
    assert {record["path"] for record in manifest["files"]} == {
        "data/app.db", "data/uploads/1.pdf", "data/pages/1/1.png",
        "frontend_dist/index.html", "frontend_dist/assets/app.js",
        "private/.env", "private/boinun.service",
    }
    assert (destination / "private/.env").read_bytes() == (app / ".env").read_bytes()
    assert (destination / "private/boinun.service").read_bytes() == unit.read_bytes()
    with closing(sqlite3.connect(destination / "data/app.db")) as restored:
        assert restored.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 2
    assert "synthetic-secret-not-for-output" not in capsys.readouterr().out
    if os.name == "posix":
        assert all(path.stat().st_mode & 0o077 == 0 for path in [destination, *destination.rglob("*")])


@pytest.mark.parametrize("activity", ["pdf_jobs", "established_connections", "writable_data_files"])
def test_running_backup_refuses_observed_activity(running_app, activity):
    app, root, unit = running_app
    with pytest.raises(ValueError, match="Active requests"):
        backup.backup_app(app, root, unit, idle_check=lambda: {**IDLE, activity: 1})
    assert not root.exists()


def test_running_backup_refuses_processing_documents_and_git_destination(running_app):
    app, root, unit = running_app
    with pytest.raises(ValueError, match="outside"):
        backup.backup_app(app, app / "backups", unit, idle_check=lambda: IDLE)
    with closing(sqlite3.connect(app / "data/app.db")) as db, db:
        db.execute("UPDATE documents SET status='processing'")
    with pytest.raises(ValueError, match="Processing documents"):
        backup.backup_app(app, root, unit, idle_check=lambda: IDLE)
    assert not root.exists()


@pytest.mark.parametrize("change", ["database", "file", "new-file"])
def test_running_backup_rejects_changes_during_copy(running_app, monkeypatch, change):
    app, root, unit = running_app
    original = backup._copy_private
    changed = False

    def copy_then_change(source, target):
        nonlocal changed
        original(source, target)
        if changed:
            return
        changed = True
        if change == "database":
            with closing(sqlite3.connect(app / "data/app.db")) as db, db:
                db.execute("UPDATE documents SET status='published'")
        elif change == "file":
            (app / "frontend_dist/index.html").write_bytes(b"changed while backing up")
        else:
            (app / "data/uploads/2.pdf").write_bytes(b"new upload")

    monkeypatch.setattr(backup, "_copy_private", copy_then_change)
    with pytest.raises(ValueError, match="changed"):
        backup.backup_app(app, root, unit, idle_check=lambda: IDLE)
    assert all(path.name.startswith(".partial-") for path in root.iterdir())


def test_running_backup_checks_sources_after_verification(running_app, monkeypatch):
    app, root, unit = running_app
    original = backup.verify_backup

    def verify_then_change(directory, **kwargs):
        original(directory, **kwargs)
        (app / "frontend_dist/index.html").write_bytes(b"late change")

    monkeypatch.setattr(backup, "verify_backup", verify_then_change)
    with pytest.raises(ValueError, match="changed"):
        backup.backup_app(app, root, unit, idle_check=lambda: IDLE)
    partial = next(root.iterdir())
    assert partial.name.startswith(".partial-")
    assert (partial / "manifest.json").exists()
    with pytest.raises(ValueError, match="partial backup"):
        original(partial)


def test_running_backup_refuses_new_activity_and_service_restarts(running_app):
    app, root, unit = running_app
    states = iter([IDLE, {**IDLE, "main_pid": 456}])
    with pytest.raises(ValueError, match="service process changed"):
        backup.backup_app(app, root, unit, idle_check=lambda: next(states))


def test_running_backup_verifier_detects_private_file_change(running_app):
    app, root, unit = running_app
    destination = backup.backup_app(app, root, unit, idle_check=lambda: IDLE)
    (destination / "private/.env").write_bytes(b"changed synthetic configuration")
    with pytest.raises(ValueError, match="checksum"):
        backup.verify_backup(destination)
