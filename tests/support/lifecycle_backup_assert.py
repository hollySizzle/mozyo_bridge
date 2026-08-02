"""Shared assertion for the backup-first migration contract (Redmine #14756)."""
def assert_backup_preserves(testcase, backup_sqlite, source_bytes, *, table="lane_lifecycle_records"):
    """The backup preserves the store's CONTENT, not its bytes (Redmine #14756 j#96956 F5).

    These assertions used to compare the backup byte-for-byte with the pre-migration file.
    That was never the contract — it was a property of ``shutil.copy2``, and review j#96956
    established that a main-file copy is not a recovery point at all: under WAL journalling
    it can omit committed rows entirely. The backup is now a staged logical snapshot taken
    through SQLite, which is content-identical and deliberately NOT byte-identical (page
    ordering and freelist differ). So the property is pinned where it actually lives.
    """
    import sqlite3 as _sqlite3
    import tempfile as _tempfile
    from pathlib import Path as _Path

    with _tempfile.TemporaryDirectory() as _t:
        original = _Path(_t) / "original.sqlite"
        original.write_bytes(source_bytes)
        want = _sqlite3.connect(f"file:{original}?mode=ro", uri=True)
        got = _sqlite3.connect(f"file:{backup_sqlite}?mode=ro", uri=True)
        try:
            testcase.assertEqual(
                got.execute("PRAGMA user_version").fetchone()[0],
                want.execute("PRAGMA user_version").fetchone()[0],
            )
            testcase.assertEqual(
                got.execute(f"SELECT * FROM {table}").fetchall(),
                want.execute(f"SELECT * FROM {table}").fetchall(),
            )
        finally:
            want.close()
            got.close()
