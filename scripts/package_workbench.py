from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DESKTOP = Path.home() / "Desktop"
OUTPUT_PREFIX = "闪聚星球内容工作台-JEV登录态完整口播与历史补齐版-ASR分段置信度与历史恢复版"
OUTPUT = DESKTOP / f"{OUTPUT_PREFIX}-{datetime.now().strftime('%Y-%m-%d')}.zip"
STATE_DIR = ROOT / ".workbench"
RUNTIME_DATABASE = STATE_DIR / "copy_workbench.sqlite3"
RUNTIME_ARCHIVES = STATE_DIR / "xhs_archives"
PACKAGED_DATABASE = ROOT / "database" / "copy_workbench.sqlite3"
PACKAGED_ARCHIVES = ROOT / "database" / "xhs_archives"
DATABASE_ENTRY = "database/copy_workbench.sqlite3"
ARCHIVES_ENTRY = Path("database") / "xhs_archives"

EXCLUDED_DIRS = {
    ".git", ".venv", ".workbench", ".workbench-test", ".pytest_cache",
    ".workbench-background-test", "node_modules", "__pycache__", ".cache",
    ".tmp", ".tmp-test", ".package-staging-final-20260920a",
    ".social_browser", "social_browser", "browser_profile",
}
EXCLUDED_SUFFIXES = {".pyc", ".log", ".tmp", ".sqlite3-shm", ".sqlite3-wal", ".tsbuildinfo"}
EXCLUDED_NAMES = {
    ".env", "cookies", "cookies.sqlite", "local state", "token.json",
    "login data", "login data for account",
}
FORBIDDEN_ZIP_PARTS = {
    ".workbench", ".workbench-test", "node_modules", ".git",
    ".social_browser", "social_browser", "browser_profile",
}


def _inside(path: Path, root: Path) -> bool:
    resolved = path.resolve()
    parent = root.resolve()
    return resolved == parent or parent in resolved.parents


def allowed(path: Path) -> bool:
    if not _inside(path, ROOT):
        return False
    parts = path.relative_to(ROOT).parts
    if any(part in EXCLUDED_DIRS or part.startswith(".tmp") for part in parts):
        return False
    if path.name.lower() in EXCLUDED_NAMES:
        return False
    if path.name.lower().endswith(("-shm", "-wal")):
        return False
    if path.suffix.lower() in EXCLUDED_SUFFIXES:
        return False
    return path.is_file()


def _snapshot_database(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise FileNotFoundError(f"工作台数据库不存在：{source}")
    source_uri = f"{source.resolve().as_uri()}?mode=ro"
    source_connection = sqlite3.connect(source_uri, uri=True, timeout=15)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
        # A distributable single-file snapshot must not depend on a WAL/SHM
        # sidecar that the ZIP intentionally excludes.
        destination_connection.execute("PRAGMA journal_mode=DELETE")
        result = destination_connection.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise RuntimeError("交付数据库一致性检查失败")
    finally:
        destination_connection.close()
        source_connection.close()


def _validate_archive_tree(database_path: Path, archive_root: Path) -> tuple[int, int]:
    connection = sqlite3.connect(f"{database_path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if not {"xhs_archive_versions", "xhs_post_assets"}.issubset(tables):
            return 0, 0

        archives = connection.execute(
            "SELECT id,archive_path,manifest_sha256 FROM xhs_archive_versions"
        ).fetchall()
        saved_asset_count = 0
        root = archive_root.resolve()
        for archive in archives:
            folder = (root / archive["archive_path"]).resolve()
            if not _inside(folder, root) or not folder.is_dir():
                raise RuntimeError(f"归档目录缺失或越界：{archive['id']}")
            manifest = folder / "manifest.json"
            checksum_file = folder / "checksums.sha256"
            if not manifest.is_file() or not checksum_file.is_file():
                raise RuntimeError(f"归档清单缺失：{archive['id']}")
            if hashlib.sha256(manifest.read_bytes()).hexdigest() != archive["manifest_sha256"]:
                raise RuntimeError(f"归档 manifest 与数据库不匹配：{archive['id']}")

            lines = [line for line in checksum_file.read_text(encoding="utf-8").splitlines() if line.strip()]
            if not lines:
                raise RuntimeError(f"归档校验清单为空：{archive['id']}")
            for line in lines:
                try:
                    expected_hash, relative_name = line.split("  ", 1)
                except ValueError as exc:
                    raise RuntimeError(f"归档校验清单格式错误：{archive['id']}") from exc
                file_path = (folder / relative_name).resolve()
                if not _inside(file_path, folder) or not file_path.is_file():
                    raise RuntimeError(f"归档文件缺失或越界：{archive['id']}")
                if hashlib.sha256(file_path.read_bytes()).hexdigest() != expected_hash:
                    raise RuntimeError(f"归档文件校验失败：{archive['id']}")

            assets = connection.execute(
                "SELECT relative_path,sha256 FROM xhs_post_assets WHERE archive_id=? AND status='SAVED'",
                (archive["id"],),
            ).fetchall()
            for asset in assets:
                file_path = (folder / asset["relative_path"]).resolve()
                if not _inside(file_path, folder) or not file_path.is_file():
                    raise RuntimeError(f"已保存图片缺失：{archive['id']}")
                if hashlib.sha256(file_path.read_bytes()).hexdigest() != asset["sha256"]:
                    raise RuntimeError(f"已保存图片与数据库校验值不匹配：{archive['id']}")
            saved_asset_count += len(assets)
        return len(archives), saved_asset_count
    finally:
        connection.close()


def _project_files(runtime_database: bool, runtime_archives: bool) -> list[Path]:
    files = []
    for path in ROOT.rglob("*"):
        if not allowed(path):
            continue
        relative = path.relative_to(ROOT)
        parts = relative.parts
        if runtime_database and relative == PACKAGED_DATABASE.relative_to(ROOT):
            continue
        if runtime_archives and parts[:2] == ("database", "xhs_archives"):
            continue
        if runtime_database and parts[:2] == ("database", "backups"):
            continue
        files.append(path)
    return sorted(files)


def _validate_zip(path: Path, archive_count: int) -> int:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise RuntimeError("交付包出现重复文件路径")
        if DATABASE_ENTRY not in names:
            raise RuntimeError("交付包缺少 SQLite 数据库副本")
        forbidden = [
            name for name in names
            if any(part.lower() in FORBIDDEN_ZIP_PARTS for part in Path(name).parts)
            or Path(name).name.lower() in EXCLUDED_NAMES
            or Path(name).name.lower().endswith(("-wal", "-shm"))
        ]
        if forbidden:
            raise RuntimeError(f"交付包包含禁止内容：{forbidden[:3]}")
        if archive_count and not any(name.startswith("database/xhs_archives/") for name in names):
            raise RuntimeError("数据库含小红书归档，但交付包缺少归档文件")
        bad_member = archive.testzip()
        if bad_member:
            raise RuntimeError(f"交付包文件损坏：{bad_member}")
        return len(names)


def create_package(output: Path = OUTPUT) -> dict[str, int | Path]:
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise FileExistsError(f"为保护已有交付包，不覆盖：{output}")

    use_runtime_database = RUNTIME_DATABASE.is_file()
    database_source = RUNTIME_DATABASE if use_runtime_database else PACKAGED_DATABASE
    use_runtime_archives = use_runtime_database and RUNTIME_ARCHIVES.is_dir()
    archive_source = RUNTIME_ARCHIVES if use_runtime_archives else PACKAGED_ARCHIVES

    with tempfile.TemporaryDirectory(prefix=".workbench-delivery-", dir=output.parent) as temp_dir:
        temp_root = Path(temp_dir)
        snapshot = temp_root / "copy_workbench.sqlite3"
        staged_zip = temp_root / output.name
        _snapshot_database(database_source, snapshot)
        archive_count, asset_count = _validate_archive_tree(snapshot, archive_source)

        with zipfile.ZipFile(staged_zip, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for path in _project_files(use_runtime_database, use_runtime_archives):
                archive.write(path, path.relative_to(ROOT).as_posix())
            archive.write(snapshot, DATABASE_ENTRY)
            if archive_source.is_dir():
                for path in sorted(item for item in archive_source.rglob("*") if item.is_file()):
                    if not _inside(path, archive_source):
                        raise RuntimeError("小红书归档包含越界路径")
                    archive.write(path, (ARCHIVES_ENTRY / path.relative_to(archive_source)).as_posix())

        package_files = _validate_zip(staged_zip, archive_count)
        # Hard-link publication is atomic and refuses to replace an existing delivery.
        os.link(staged_zip, output)

    return {
        "path": output,
        "files": package_files,
        "archives": archive_count,
        "assets": asset_count,
        "bytes": output.stat().st_size,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="打包闪聚星球 JEV 工作台，不携带登录资料")
    parser.add_argument("--output", type=Path, default=OUTPUT, help="交付 ZIP 路径；不覆盖已有文件")
    args = parser.parse_args()
    result = create_package(args.output)
    print(
        f"{result['path']}\nfiles={result['files']}\n"
        f"xhs_archives={result['archives']}\nsaved_assets={result['assets']}\n"
        f"bytes={result['bytes']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
