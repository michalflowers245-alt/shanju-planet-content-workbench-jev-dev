from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import zipfile
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "package_workbench.py"
SPEC = importlib.util.spec_from_file_location("package_workbench", MODULE_PATH)
assert SPEC and SPEC.loader
packager = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(packager)


def _create_live_bundle(root: Path) -> tuple[Path, Path, Path]:
    packaged_db = root / "database" / "copy_workbench.sqlite3"
    packaged_db.parent.mkdir(parents=True)
    with sqlite3.connect(packaged_db) as connection:
        connection.execute("CREATE TABLE marker(value TEXT NOT NULL)")
        connection.execute("INSERT INTO marker(value) VALUES('stale packaged database')")

    runtime_db = root / ".workbench" / "copy_workbench.sqlite3"
    runtime_db.parent.mkdir(parents=True)
    archive_relative = "ws-test/post-test/xhsa-test"
    archive_root = root / ".workbench" / "xhs_archives"
    archive_dir = archive_root / archive_relative
    (archive_dir / "images").mkdir(parents=True)
    manifest = json.dumps({"version": 1, "assets": ["images/1.webp"]}).encode()
    image = b"original-image-bytes"
    (archive_dir / "manifest.json").write_bytes(manifest)
    (archive_dir / "images" / "1.webp").write_bytes(image)
    checksum_lines = [
        f"{hashlib.sha256(manifest).hexdigest()}  manifest.json",
        f"{hashlib.sha256(image).hexdigest()}  images/1.webp",
    ]
    (archive_dir / "checksums.sha256").write_text("\n".join(checksum_lines) + "\n", encoding="utf-8")

    with sqlite3.connect(runtime_db) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE marker(value TEXT NOT NULL)")
        connection.execute("INSERT INTO marker(value) VALUES('live runtime database')")
        connection.execute(
            "CREATE TABLE xhs_archive_versions(id TEXT,archive_path TEXT,manifest_sha256 TEXT)"
        )
        connection.execute(
            "CREATE TABLE xhs_post_assets(archive_id TEXT,status TEXT,relative_path TEXT,sha256 TEXT)"
        )
        connection.execute(
            "INSERT INTO xhs_archive_versions VALUES(?,?,?)",
            ("xhsa-test", archive_relative, hashlib.sha256(manifest).hexdigest()),
        )
        connection.execute(
            "INSERT INTO xhs_post_assets VALUES(?,?,?,?)",
            ("xhsa-test", "SAVED", "images/1.webp", hashlib.sha256(image).hexdigest()),
        )

    stale_archives = root / "database" / "xhs_archives"
    stale_archives.mkdir(parents=True)
    (stale_archives / "stale.txt").write_text("must not be bundled", encoding="utf-8")
    browser_profile = root / ".workbench" / "social_browser" / "Default"
    browser_profile.mkdir(parents=True)
    (browser_profile / "Cookies").write_text("must not be bundled", encoding="utf-8")
    return packaged_db, runtime_db, archive_root


def test_package_uses_consistent_runtime_db_and_archives_without_browser_profile(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    packaged_db, runtime_db, archive_root = _create_live_bundle(root)
    (root / "README.md").write_text("project source", encoding="utf-8")
    output = tmp_path / "delivery.zip"
    monkeypatch.setattr(packager, "ROOT", root)
    monkeypatch.setattr(packager, "RUNTIME_DATABASE", runtime_db)
    monkeypatch.setattr(packager, "RUNTIME_ARCHIVES", archive_root)
    monkeypatch.setattr(packager, "PACKAGED_DATABASE", packaged_db)
    monkeypatch.setattr(packager, "PACKAGED_ARCHIVES", root / "database" / "xhs_archives")

    result = packager.create_package(output)

    with zipfile.ZipFile(output) as archive:
        names = archive.namelist()
        assert archive.testzip() is None
        assert "README.md" in names
        assert packager.DATABASE_ENTRY in names
        assert "database/xhs_archives/ws-test/post-test/xhsa-test/manifest.json" in names
        assert "database/xhs_archives/ws-test/post-test/xhsa-test/images/1.webp" in names
        assert not any("stale.txt" in name or "social_browser" in name.lower() or "cookies" in name.lower() for name in names)

        snapshot_path = tmp_path / "packed-copy-workbench.sqlite3"
        snapshot_path.write_bytes(archive.read(packager.DATABASE_ENTRY))
        snapshot = sqlite3.connect(f"file:{snapshot_path}?mode=ro", uri=True)
        assert snapshot.execute("SELECT value FROM marker").fetchone()[0] == "live runtime database"
        assert snapshot.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
        assert snapshot.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        snapshot.close()

    assert result["archives"] == 1
    assert result["assets"] == 1


def test_package_aborts_if_runtime_archive_hashes_do_not_match_database(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    packaged_db, runtime_db, archive_root = _create_live_bundle(root)
    image_path = archive_root / "ws-test/post-test/xhsa-test/images/1.webp"
    image_path.write_bytes(b"modified")
    output = tmp_path / "delivery.zip"
    monkeypatch.setattr(packager, "ROOT", root)
    monkeypatch.setattr(packager, "RUNTIME_DATABASE", runtime_db)
    monkeypatch.setattr(packager, "RUNTIME_ARCHIVES", archive_root)
    monkeypatch.setattr(packager, "PACKAGED_DATABASE", packaged_db)
    monkeypatch.setattr(packager, "PACKAGED_ARCHIVES", root / "database" / "xhs_archives")

    try:
        packager.create_package(output)
    except RuntimeError as exc:
        assert "归档文件校验失败" in str(exc)
    else:
        raise AssertionError("校验不匹配的归档不应进入 ZIP")

    assert not output.exists()
