"""SQLite backup and restore hardening tests."""

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from app.cli.sqlite_ops import _counts, backup, copy_stopped_container_db, restore


def _db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE transactions (id INTEGER PRIMARY KEY, value TEXT)")
        conn.execute("CREATE TABLE statements (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE credit_cards (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE banks (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO transactions(value) VALUES ('a'), ('b')")


def test_backup_passes_integrity_and_writes_manifest(tmp_path: Path) -> None:
    """Threat matrix: backup API snapshot passes integrity_check and records counts."""
    src = tmp_path / "finhealth.db"
    dst = tmp_path / "backup.db"
    _db(src)

    manifest = backup(source_url=f"sqlite:///{src}", destination=dst)

    assert dst.exists()
    assert manifest.counts["transactions"] == 2
    assert dst.with_suffix(".db.manifest.json").exists()
    with sqlite3.connect(dst) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_restore_preserves_counts_and_removes_wal_files(tmp_path: Path) -> None:
    """Threat matrix: restore validates then atomically replaces destination and stale WAL/SHM."""
    backup_db = tmp_path / "backup.db"
    dst = tmp_path / "finhealth.db"
    _db(backup_db)
    _db(dst)
    dst.with_suffix(".db-wal").write_text("stale", encoding="utf-8")
    dst.with_suffix(".db-shm").write_text("stale", encoding="utf-8")

    manifest = restore(backup_path=backup_db, destination_url=f"sqlite:///{dst}")

    assert manifest.counts["transactions"] == 2
    assert not dst.with_suffix(".db-wal").exists()
    assert not dst.with_suffix(".db-shm").exists()


def test_restore_rejects_busy_destination_before_mutation(tmp_path: Path) -> None:
    """Threat matrix: a writer-present destination fails before any file mutation."""
    backup_db = tmp_path / "backup.db"
    dst = tmp_path / "finhealth.db"
    _db(backup_db)
    _db(dst)
    before = dst.read_bytes()
    wal = dst.with_suffix(".db-wal")
    shm = dst.with_suffix(".db-shm")
    wal.write_text("stale", encoding="utf-8")
    shm.write_text("stale", encoding="utf-8")

    conn = sqlite3.connect(dst)
    try:
        conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(RuntimeError, match="destination busy"):
            restore(backup_path=backup_db, destination_url=f"sqlite:///{dst}")
    finally:
        conn.rollback()
        conn.close()

    assert dst.read_bytes() == before
    assert wal.exists()
    assert shm.exists()


def test_restore_post_verification_counts_and_integrity(tmp_path: Path) -> None:
    """Restore returns only after destination integrity and counts match the backup."""
    src = tmp_path / "source.db"
    backup_db = tmp_path / "backup.db"
    dst = tmp_path / "finhealth.db"
    _db(src)
    manifest = backup(source_url=f"sqlite:///{src}", destination=backup_db)
    _db(dst)

    restored = restore(backup_path=backup_db, destination_url=f"sqlite:///{dst}")

    assert restored.integrity == "ok"
    assert restored.counts == manifest.counts
    assert _counts(dst) == manifest.counts
    with sqlite3.connect(dst) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_restore_post_verification_endpoint_smoke(tmp_path: Path) -> None:
    """Restored DB counts match the manifest through SQLAlchemy's app-facing path."""
    src = tmp_path / "source.db"
    backup_db = tmp_path / "backup.db"
    dst = tmp_path / "finhealth.db"
    _db(src)
    manifest = backup(source_url=f"sqlite:///{src}", destination=backup_db)
    _db(dst)

    restored = restore(backup_path=backup_db, destination_url=f"sqlite:///{dst}")

    assert _counts(dst) == manifest.counts == restored.counts
    engine = create_engine(f"sqlite:///{dst}")
    try:
        with engine.connect() as conn:
            sqlalchemy_counts = {
                table: conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
                for table in ("transactions", "statements", "credit_cards", "banks")
            }
    finally:
        engine.dispose()
    assert sqlalchemy_counts == manifest.counts


def test_restore_rejects_corrupt_and_non_sqlite_before_mutation(tmp_path: Path) -> None:
    """Threat matrix: corrupt backups and non-SQLite URLs fail before destination mutation."""
    corrupt = tmp_path / "corrupt.db"
    dst = tmp_path / "finhealth.db"
    corrupt.write_text("not sqlite", encoding="utf-8")
    _db(dst)
    before = dst.read_bytes()

    with pytest.raises(ValueError, match="integrity"):
        restore(backup_path=corrupt, destination_url=f"sqlite:///{dst}")
    assert dst.read_bytes() == before

    with pytest.raises(ValueError, match="SQLite"):
        backup(source_url="postgresql://example/db", destination=tmp_path / "x.db")


def test_restore_rejects_non_sqlite_url(tmp_path: Path) -> None:
    """Threat matrix: restore rejects non-SQLite destinations before filesystem mutation."""
    backup_db = tmp_path / "backup.db"
    sentinel = tmp_path / "sentinel.txt"
    _db(backup_db)
    sentinel.write_text("unchanged", encoding="utf-8")

    with pytest.raises(ValueError, match="SQLite"):
        restore(backup_path=backup_db, destination_url="postgresql://example/db")

    assert sentinel.read_text(encoding="utf-8") == "unchanged"


def test_stopped_container_backup_copy_passes_integrity(tmp_path: Path) -> None:
    """Stopped-container filesystem copy preserves a valid SQLite snapshot."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    source = data_dir / "finhealth.db"
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE lifecycle (id INTEGER PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO lifecycle(value) VALUES ('stopped-copy')")
        conn.commit()

    destination = tmp_path / "backups" / "finhealth-stopped.db"
    copied = copy_stopped_container_db(data_dir, destination)

    assert copied == destination
    with sqlite3.connect(copied) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT value FROM lifecycle").fetchone()[0] == "stopped-copy"
