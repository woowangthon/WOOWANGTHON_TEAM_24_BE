"""Back up a quiesced data directory with SQLite's online backup API."""

import argparse
import hashlib
import json
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def verify_backup(directory: Path) -> None:
    directory = directory.resolve(strict=True)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("version") != 1 or manifest.get("quiesced") is not True:
        raise ValueError("Unsupported or incomplete backup manifest.")
    expected = set()
    for entry in manifest["files"]:
        path = (directory / entry["path"]).resolve()
        if not path.is_relative_to(directory) or path == directory:
            raise ValueError("A manifest path leaves the backup directory.")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if path.stat().st_size != entry["size"] or digest != entry["sha256"]:
            raise ValueError("A backup file failed checksum verification.")
        expected.add(path.relative_to(directory).as_posix())
    actual = {path.relative_to(directory).as_posix() for path in directory.rglob("*") if path.is_file()}
    if "app.db" not in expected or actual != expected | {"manifest.json"}:
        raise ValueError("The backup file list does not match its manifest.")
    with closing(sqlite3.connect((directory / "app.db").as_uri() + "?mode=ro", uri=True)) as db:
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--backup-root", type=Path)
    parser.add_argument("--quiesced", action="store_true")
    parser.add_argument("--verify", type=Path, help="Verify a completed backup without restoring it.")
    args = parser.parse_args()
    if args.verify and (args.data_dir or args.backup_root or args.quiesced):
        parser.error("Use --verify separately from backup options.")
    if not args.verify and not (args.data_dir and args.backup_root):
        parser.error("Backup requires --data-dir and --backup-root.")
    try:
        if args.verify:
            verify_backup(args.verify)
            print("Backup verification: PASS")
            return
        destination = backup_data(args.data_dir, args.backup_root, quiesced=args.quiesced)
    except (ValueError, KeyError, OSError, sqlite3.Error) as error:
        parser.exit(1, f"Backup failed: {error}\n")
    print(f"Backup complete: {destination}")


if __name__ == "__main__":
    main()
