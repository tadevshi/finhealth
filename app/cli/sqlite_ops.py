"""WAL-safe SQLite backup and restore helpers.

``copy_stopped_container_db`` backs the README stopped-container backup
variant: after the application container is stopped, operators may copy
``finhealth.db`` plus any adjacent WAL/SHM files and then verify the copied
database with ``PRAGMA integrity_check``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

COUNT_TABLES = ("transactions", "statements", "credit_cards", "banks")


@dataclass(frozen=True, slots=True)
class BackupManifest:
    database: str
    counts: dict[str, int]
    integrity: str


def _sqlite_path(url: str) -> Path:
    parsed = urlparse(url)
    if parsed.scheme not in {"sqlite", "sqlite+aiosqlite"}:
        raise ValueError("SQLite operation requires a SQLite database URL")
    if parsed.netloc and parsed.netloc not in {"", "."}:
        raise ValueError("SQLite operation requires a local SQLite path")
    if url.startswith(f"{parsed.scheme}:///") and not url.startswith(f"{parsed.scheme}:////"):
        path = unquote(url.split(":///", 1)[1])
    else:
        path = unquote(parsed.path)
        if path.startswith("//"):
            path = path[1:]
    if not path:
        raise ValueError("SQLite URL must include a database path")
    return Path(path)


def _integrity(path: Path) -> str:
    try:
        with sqlite3.connect(path) as conn:
            result = conn.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.DatabaseError as exc:
        raise ValueError(f"integrity check failed for {path}") from exc
    value = str(result[0]) if result else ""
    if value != "ok":
        raise ValueError(f"integrity check failed for {path}: {value}")
    return value


def _counts(path: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    with sqlite3.connect(path) as conn:
        for table in COUNT_TABLES:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            counts[table] = (
                int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) if exists else 0
            )
    return counts


def _assert_destination_idle(path: Path) -> None:
    """Fail if a SQLite destination has an active writer before mutation."""
    if not path.exists():
        return
    try:
        with sqlite3.connect(path, timeout=0.05) as conn:
            conn.execute("BEGIN IMMEDIATE")
            quick_check = conn.execute("PRAGMA quick_check").fetchone()
            conn.rollback()
    except sqlite3.OperationalError as exc:
        raise RuntimeError("destination busy; stop the application before restore") from exc
    except sqlite3.DatabaseError as exc:
        raise ValueError(f"destination is not a valid SQLite database: {path}") from exc
    if not quick_check or str(quick_check[0]) != "ok":
        raise ValueError(f"destination quick_check failed for {path}: {quick_check[0]}")


def _write_manifest(path: Path, manifest: BackupManifest) -> None:
    path.with_suffix(path.suffix + ".manifest.json").write_text(
        json.dumps(asdict(manifest), indent=2, sort_keys=True),
        encoding="utf-8",
    )


def backup(*, source_url: str, destination: Path) -> BackupManifest:
    """Create a verified SQLite backup using the sqlite3 backup API."""
    source = _sqlite_path(source_url)
    if source.resolve() == destination.resolve():
        raise ValueError("backup destination must differ from source")
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=destination.name, suffix=".tmp", dir=destination.parent)
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        with sqlite3.connect(source) as src, sqlite3.connect(temp_path) as dst:
            src.backup(dst)
        integrity = _integrity(temp_path)
        manifest = BackupManifest(
            database=str(destination), counts=_counts(temp_path), integrity=integrity
        )
        temp_path.replace(destination)
        _write_manifest(destination, manifest)
        return manifest
    finally:
        if temp_path.exists():
            temp_path.unlink()


def copy_stopped_container_db(source_dir: str | Path, dest_path: str | Path) -> Path:
    """Copy a stopped container's SQLite DB plus WAL/SHM sidecars."""
    source = Path(source_dir) / "finhealth.db"
    destination = Path(dest_path)
    if not source.exists():
        raise FileNotFoundError(f"SQLite database not found: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(source) + suffix)
        if sidecar.exists():
            shutil.copy2(sidecar, Path(str(destination) + suffix))
    _integrity(destination)
    return destination


def restore(*, backup_path: Path, destination_url: str) -> BackupManifest:
    """Validate and atomically restore a SQLite backup."""
    destination = _sqlite_path(destination_url)
    if backup_path.resolve() == destination.resolve():
        raise ValueError("restore backup and destination must differ")
    integrity = _integrity(backup_path)
    manifest = BackupManifest(
        database=str(destination), counts=_counts(backup_path), integrity=integrity
    )
    _assert_destination_idle(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=destination.name, suffix=".restore", dir=destination.parent
    )
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        with sqlite3.connect(backup_path) as src, sqlite3.connect(temp_path) as dst:
            src.backup(dst)
        _integrity(temp_path)
        for suffix in ("-wal", "-shm"):
            stale = Path(str(destination) + suffix)
            if stale.exists():
                stale.unlink()
        temp_path.replace(destination)
        post_integrity = _integrity(destination)
        post_counts = _counts(destination)
        if post_counts != manifest.counts:
            raise RuntimeError(
                f"post-restore count verification failed: expected {manifest.counts}, got {post_counts}"
            )
        manifest = BackupManifest(
            database=str(destination), counts=post_counts, integrity=post_integrity
        )
        _write_manifest(destination, manifest)
        return manifest
    finally:
        if temp_path.exists():
            temp_path.unlink()


def main() -> None:
    parser = argparse.ArgumentParser(description="Verified SQLite backup/restore")
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("backup")
    b.add_argument("source_url")
    b.add_argument("destination", type=Path)
    r = sub.add_parser("restore")
    r.add_argument("backup_path", type=Path)
    r.add_argument("destination_url")
    args = parser.parse_args()
    if args.command == "backup":
        manifest = backup(source_url=args.source_url, destination=args.destination)
        output = asdict(manifest)
    else:
        manifest = restore(backup_path=args.backup_path, destination_url=args.destination_url)
        output = {
            **asdict(manifest),
            "post_verification": {
                "integrity": manifest.integrity,
                "counts_delta": dict.fromkeys(manifest.counts, 0),
            },
        }
    print(json.dumps(output, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
