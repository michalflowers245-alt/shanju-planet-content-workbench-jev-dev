#!/usr/bin/env python3
"""Apply the rewrite-workbench additive migration to a chosen SQLite copy.

This runner deliberately takes an explicit database path.  It can create a
SQLite-consistent backup before one idempotent transaction, and never starts
or alters the formal desktop workbench by itself.
"""
from __future__ import annotations

import argparse
import hashlib
import sqlite3
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_SQL = PROJECT_ROOT / "migrations" / "rewrite_v1.sql"


def _statements(script: str):
    buffer: list[str] = []
    for line in script.splitlines():
        buffer.append(line)
        candidate = "\n".join(buffer).strip()
        if candidate and sqlite3.complete_statement(candidate):
            yield candidate
            buffer.clear()
    if "\n".join(buffer).strip():
        raise RuntimeError("迁移 SQL 最后一条语句不完整")


def _backup(source: Path, target: Path) -> str:
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
        src.backup(dst)
    with sqlite3.connect(target) as check:
        integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise RuntimeError(f"备份完整性检查失败：{integrity}")
    return hashlib.sha256(target.read_bytes()).hexdigest()


def migrate(database: Path) -> dict[str, object]:
    if not database.is_file():
        raise FileNotFoundError(f"数据库不存在：{database}")
    script = MIGRATION_SQL.read_text(encoding="utf-8")
    with sqlite3.connect(database, timeout=30) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        required = {"workspaces", "xhs_post_extractions", "xhs_archive_versions", "usage_calls"}
        present = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = sorted(required - present)
        if missing:
            raise RuntimeError(f"数据库不是可迁移的工作台副本，缺少：{', '.join(missing)}")
        connection.execute("BEGIN IMMEDIATE")
        try:
            usage_columns = {row[1] for row in connection.execute("PRAGMA table_info(usage_calls)")}
            added_columns: list[str] = []
            if "rewrite_job_id" not in usage_columns:
                connection.execute("ALTER TABLE usage_calls ADD COLUMN rewrite_job_id TEXT")
                added_columns.append("usage_calls.rewrite_job_id")
            for statement in _statements(script):
                connection.execute(statement)
            connection.execute("CREATE INDEX IF NOT EXISTS idx_usage_calls_rewrite_job ON usage_calls(rewrite_job_id, created_at DESC)")
            connection.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('schema_version','10')")
            foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
            if foreign_key_errors:
                raise RuntimeError(f"外键检查失败：{foreign_key_errors[:3]}")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        return {
            "database": str(database),
            "added_columns": added_columns,
            "schema_version": connection.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0],
            "integrity": connection.execute("PRAGMA integrity_check").fetchone()[0],
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="在隔离 SQLite 副本中迁移改写工作台 V1 数据结构")
    parser.add_argument("--database", type=Path, required=True, help="要迁移的 SQLite 数据库副本")
    parser.add_argument("--backup", type=Path, help="迁移前创建一致性 SQLite 备份")
    args = parser.parse_args()
    database = args.database.expanduser().resolve()
    if args.backup:
        backup = args.backup.expanduser().resolve()
        print(f"backup={backup}")
        print(f"backup_sha256={_backup(database, backup)}")
    for key, value in migrate(database).items():
        print(f"{key}={value}")


if __name__ == "__main__":
    main()
