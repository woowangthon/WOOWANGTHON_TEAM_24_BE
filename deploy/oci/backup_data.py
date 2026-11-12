"""Private SQLite/file backups, including an observed-idle running application."""

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import stat
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

if __package__:
    from .runtime_check import inspect_runtime, require_idle
else:
    from runtime_check import inspect_runtime, require_idle


def verify_backup(directory: Path, *, allow_partial: bool = False) -> None:
    directory = directory.resolve(strict=True)
    if directory.name.startswith(".partial-") and not allow_partial:
        raise ValueError("A partial backup is not a completed recovery point.")
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    version = manifest.get("version")
    if version not in (1, 2) or (version == 1 and manifest.get("quiesced") is not True):
        raise ValueError("Unsupported or incomplete backup manifest.")
    if version == 2 and manifest.get("consistency") != "live-idle-checked":
        raise ValueError("Unsupported application backup consistency mode.")
    if any(path.is_symlink() for path in directory.rglob("*")):
        raise ValueError("Symlinks are not allowed in a backup.")
    expected = set()
    for entry in manifest["files"]:
        relative = Path(entry["path"])
        if relative.is_absolute() or ".." in relative.parts or relative.as_posix() in expected:
            raise ValueError("A manifest path leaves the backup directory or is duplicated.")
        path = (directory / entry["path"]).resolve()
        if not path.is_relative_to(directory) or path == directory:
            raise ValueError("A manifest path leaves the backup directory.")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if path.stat().st_size != entry["size"] or digest != entry["sha256"]:
            raise ValueError("A backup file failed checksum verification.")
        expected.add(path.relative_to(directory).as_posix())
    actual = {path.relative_to(directory).as_posix() for path in directory.rglob("*") if path.is_file()}
    database = "data/app.db" if version == 2 else "app.db"
    if database not in expected or actual != expected | {"manifest.json"}:
        raise ValueError("The backup file list does not match its manifest.")
    if version == 2:
        if not {"private/.env", "private/boinun.service"}.issubset(expected):
            raise ValueError("Private configuration backups are missing.")
        for name in ("data/uploads", "data/pages", "frontend_dist"):
            if not (directory / name).is_dir():
                raise ValueError("An application backup directory is missing.")
        if os.name == "posix":
            for path in [directory, *directory.rglob("*")]:
                if stat.S_IMODE(path.stat().st_mode) != (0o700 if path.is_dir() else 0o600):
                    raise ValueError("Backup permissions must be 0700 directories and 0600 files.")
    with closing(sqlite3.connect((directory / database).as_uri() + "?mode=ro", uri=True)) as db:
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("Backup database integrity check failed.")


def backup_data(data_dir: Path, backup_root: Path, *, quiesced: bool) -> Path:
    if not quiesced:
        raise ValueError("Stop the service and all data writers before using --quiesced.")
    data_dir = data_dir.resolve(strict=True)
    backup_root = backup_root.resolve()
    if backup_root.is_relative_to(data_dir):
        raise ValueError("The backup destination must be outside the data directory.")
    source_db = data_dir / "app.db"
    if not source_db.is_file():
        raise ValueError("An existing app.db is required; no database will be created.")
    for name in ("uploads", "pages"):
        directory = data_dir / name
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise ValueError(f"Expected an ordinary {name} directory.")
        if directory.exists() and any(path.is_symlink() for path in directory.rglob("*")):
            raise ValueError(f"Symlinks in {name} must be reviewed before backing up.")

    with closing(sqlite3.connect(source_db.as_uri() + "?mode=ro", uri=True)) as source:
        processing = source.execute(
            "SELECT COUNT(*) FROM documents WHERE status = 'processing'"
        ).fetchone()[0]
        if processing:
            raise ValueError("Processing documents remain; drain background jobs before backup.")

        backup_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        name = f"{stamp}-{uuid4().hex[:8]}"
        partial = backup_root / f".partial-{name}"
        partial.mkdir(mode=0o700)
        with closing(sqlite3.connect(partial / "app.db")) as target:
            source.backup(target)
            target.execute("PRAGMA journal_mode=DELETE")
            if target.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise ValueError("Backup database integrity check failed.")

    for directory in ("uploads", "pages"):
        origin = data_dir / directory
        if origin.is_dir():
            shutil.copytree(origin, partial / directory)
        else:
            (partial / directory).mkdir()

    files = []
    for path in sorted(partial.rglob("*")):
        if path.is_file():
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            files.append({
                "path": path.relative_to(partial).as_posix(),
                "size": path.stat().st_size,
                "sha256": digest,
            })
    manifest = {"version": 1, "created_at": stamp, "quiesced": True, "files": files}
    (partial / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    completed = backup_root / name
    partial.rename(completed)
    return completed


def _sources(app_dir: Path, service_file: Path) -> dict[str, Path]:
    sources = {}
    for name in ("data/uploads", "data/pages", "frontend_dist"):
        root = app_dir / name
        if root.is_symlink() or not root.is_dir():
            raise ValueError("Expected ordinary uploads, pages, and frontend_dist directories.")
        for path in sorted(root.rglob("*")):
            if path.is_symlink() or not (path.is_dir() or path.is_file()):
                raise ValueError("Unsupported file type in application backup sources.")
            if path.is_file():
                sources[path.relative_to(app_dir).as_posix()] = path
    for name, path in {"private/.env": app_dir / ".env", "private/boinun.service": service_file}.items():
        if path.is_symlink() or not path.is_file():
            raise ValueError("Existing regular environment and service files are required.")
        sources[name] = path
    return sources


def _fingerprint(path: Path) -> dict:
    before = path.stat()
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    after = path.stat()
    attributes = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    if any(getattr(before, name) != getattr(after, name) for name in attributes):
        raise ValueError("A source file changed while being read; defer backup.")
    return {"size": after.st_size, "sha256": digest, "mtime_ns": after.st_mtime_ns, "ctime_ns": after.st_ctime_ns}


def _copy_private(source: Path, target: Path) -> None:
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output, source.open("rb") as original:
        shutil.copyfileobj(original, output)


def _check_stable(source, app_dir, service_file, idle_check, before_runtime, before_files, before_version):
    after_runtime = idle_check()
    require_idle(after_runtime)
    after_files = {name: _fingerprint(path) for name, path in _sources(app_dir, service_file).items()}
    if before_files != after_files or before_runtime["main_pid"] != after_runtime["main_pid"]:
        raise ValueError("Application files or service process changed; partial backup retained.")
    if source.execute("SELECT COUNT(*) FROM documents WHERE status = 'processing'").fetchone()[0]:
        raise ValueError("Processing started during backup; partial backup retained.")
    if source.execute("PRAGMA data_version").fetchone()[0] != before_version:
        raise ValueError("Database changed during backup; partial backup retained.")
    return after_runtime


def backup_app(app_dir: Path, backup_root: Path, service_file: Path, *, idle_check=None) -> Path:
    """Refuse active/changing data; does not stop the service or lock out writers."""
    app_dir = app_dir.resolve(strict=True)
    backup_root = backup_root.resolve()
    if backup_root.is_relative_to(app_dir):
        raise ValueError("Application backups must be outside the application/Git directory.")
    if service_file.name != "boinun.service":
        raise ValueError("This backup profile preserves the existing boinun.service.")
    database = app_dir / "data/app.db"
    if database.is_symlink() or not database.is_file():
        raise ValueError("An existing regular app.db is required.")
    idle_check = idle_check or (lambda: inspect_runtime(app_dir))
    before_runtime = idle_check()
    require_idle(before_runtime)
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as source:
        before_version = source.execute("PRAGMA data_version").fetchone()[0]
        if source.execute("SELECT COUNT(*) FROM documents WHERE status = 'processing'").fetchone()[0]:
            raise ValueError("Processing documents remain; defer the full backup.")
        sources = _sources(app_dir, service_file)
        before_files = {name: _fingerprint(path) for name, path in sources.items()}
        backup_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name == "posix" and stat.S_IMODE(backup_root.stat().st_mode) != 0o700:
            raise ValueError("Use an owner-only 0700 backup root.")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        name = f"{stamp}-{uuid4().hex[:8]}"
        partial = backup_root / f".partial-{name}"
        partial.mkdir(mode=0o700)
        (partial / "data").mkdir(mode=0o700)
        destination_db = partial / "data/app.db"
        destination_db.touch(mode=0o600, exist_ok=False)
        with closing(sqlite3.connect(destination_db)) as target:
            source.backup(target)
            target.execute("PRAGMA journal_mode=DELETE")
        for path, original in sources.items():
            _copy_private(original, partial / path)
        for path in ("data/uploads", "data/pages", "frontend_dist"):
            (partial / path).mkdir(mode=0o700, parents=True, exist_ok=True)
        after_runtime = _check_stable(
            source, app_dir, service_file, idle_check, before_runtime, before_files, before_version
        )
        files = []
        for path in sorted(partial.rglob("*")):
            if path.is_dir():
                path.chmod(0o700)
            elif path.is_file():
                path.chmod(0o600)
                record = _fingerprint(path)
                relative = path.relative_to(partial).as_posix()
                if relative in before_files and (record["size"], record["sha256"]) != (before_files[relative]["size"], before_files[relative]["sha256"]):
                    raise ValueError("Copied content differs from the stable source; partial backup retained.")
                files.append({"path": relative, "size": record["size"], "sha256": record["sha256"]})
        manifest = {
            "version": 2, "created_at": datetime.now(timezone.utc).isoformat(),
            "consistency": "live-idle-checked", "service": "boinun.service",
            "source_app": str(app_dir), "runtime_before": before_runtime, "runtime_after": after_runtime,
            "files": files,
            "limitations": "No global filesystem transaction; requests, jobs, commits and file changes were checked during the backup window.",
        }
        with (partial / "manifest.json").open("x", encoding="utf-8") as stream:
            json.dump(manifest, stream, indent=2)
            stream.write("\n")
        (partial / "manifest.json").chmod(0o600)
        verify_backup(partial, allow_partial=True)
        # Keep the source connection open and check again after destination verification.
        _check_stable(source, app_dir, service_file, idle_check, before_runtime, before_files, before_version)
        completed = backup_root / name
        partial.rename(completed)
        return completed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--backup-root", type=Path)
    parser.add_argument("--quiesced", action="store_true")
    parser.add_argument("--app-dir", type=Path)
    parser.add_argument("--service-file", type=Path, default=Path("/etc/systemd/system/boinun.service"))
    parser.add_argument("--live-idle", action="store_true", help="Back up a running, observed-idle boinun app; refuse changes.")
    parser.add_argument("--verify", type=Path, help="Verify a completed backup without restoring it.")
    args = parser.parse_args()
    if args.verify and (args.data_dir or args.app_dir or args.backup_root or args.quiesced or args.live_idle):
        parser.error("Use --verify separately from backup options.")
    if args.app_dir and (not args.live_idle or args.data_dir or args.quiesced):
        parser.error("Application backup requires --app-dir, --live-idle, --backup-root.")
    if not args.verify and not (args.backup_root and (args.app_dir or args.data_dir)):
        parser.error("Backup requires --backup-root and either --app-dir or --data-dir.")
    if args.live_idle and not args.app_dir:
        parser.error("--live-idle requires --app-dir.")
    try:
        if args.verify:
            verify_backup(args.verify)
            print("Backup verification: PASS")
            return
        if args.app_dir:
            destination = backup_app(args.app_dir, args.backup_root, args.service_file)
        else:
            destination = backup_data(args.data_dir, args.backup_root, quiesced=args.quiesced)
    except (ValueError, KeyError, OSError, sqlite3.Error) as error:
        parser.exit(1, f"Backup failed: {error}\n")
    print(f"Backup complete: {destination}")


if __name__ == "__main__":
    main()
