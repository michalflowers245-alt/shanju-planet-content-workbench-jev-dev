#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.migrate_xhs_archive_v2 import _backup, _statements


DEFAULT_DATABASE = PROJECT_ROOT / ".workbench" / "copy_workbench.sqlite3"
MIGRATION_SQL = PROJECT_ROOT / "migrations" / "xhs_fidelity_v3.sql"
EXTRACTION_COLUMNS = (
    ("body_rich_json", "TEXT NOT NULL DEFAULT '[]'"),
    ("body_sha256", "TEXT"),
    ("parser_version", "TEXT NOT NULL DEFAULT ''"),
)
ARCHIVE_COLUMNS = EXTRACTION_COLUMNS


def _add_columns(connection: sqlite3.Connection, table: str, columns: tuple[tuple[str, str], ...]) -> list[str]:
    existing = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    added: list[str] = []
    for name, definition in columns:
        if name not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
            added.append(f"{table}.{name}")
    return added


def migrate(database: Path) -> dict[str, object]:
    if not database.is_file():
        raise FileNotFoundError(f"数据库不存在：{database}")
    script = MIGRATION_SQL.read_text(encoding="utf-8")
    with sqlite3.connect(database, timeout=30) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        try:
            added = _add_columns(connection, "xhs_post_extractions", EXTRACTION_COLUMNS)
            added.extend(_add_columns(connection, "xhs_archive_versions", ARCHIVE_COLUMNS))
            for statement in _statements(script):
                connection.execute(statement)
            connection.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('schema_version','9')")
            errors = connection.execute("PRAGMA foreign_key_check").fetchall()
            if errors:
                raise RuntimeError(f"外键检查失败：{errors[:3]}")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        return {
            "database": str(database),
            "added_columns": added,
            "schema_version": connection.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0],
            "integrity": connection.execute("PRAGMA integrity_check").fetchone()[0],
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="安全迁移小红书正文保真与批量归档 V3 数据结构")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--backup", type=Path)
    args = parser.parse_args()
    database = args.database.expanduser().resolve()
    if args.backup:
        _backup(database, args.backup.expanduser().resolve())
        print(f"backup={args.backup.expanduser().resolve()}")
    for key, value in migrate(database).items():
        print(f"{key}={value}")


if __name__ == "__main__":
    main()
