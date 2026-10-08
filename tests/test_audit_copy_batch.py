from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from backend.app.db import Database, json_dumps, utc_now
from scripts.audit_copy_batch import audit_batch, main


@pytest.fixture
def sample_batch(tmp_path: Path):
    db = Database(tmp_path / "audit.sqlite3")
    db.initialize()
    workspace = db.create_workspace("验收当前工作区")
    other = db.create_workspace("其他工作区")
    batch_id = "audit_batch"
    now = utc_now()
    db.execute(
        """INSERT INTO radar_search_batches(id,workspace_id,persona_id,topic,status,created_at,updated_at,target_count)
        VALUES(?,?,'persona','不得输出私有主题','COMPLETED',?,?,10)""",
        (batch_id, workspace["id"], now, now),
    )
    archive_root = tmp_path / "xhs_archives"
    body = "  不得打印的正文\r\n🙂 #话题\u200b  "
    for index in range(1, 9):
        platform = "xiaohongshu" if index <= 6 else "douyin"
        post_id = f"{index:024d}" if platform == "xiaohongshu" else str(7450123456789012340 + index)
        url = f"https://www.xiaohongshu.com/explore/{post_id}" if platform == "xiaohongshu" else f"https://www.douyin.com/video/{post_id}"
        candidate_url = url + "?xsec_token=NEVER_PRINT_SIGNATURE" if index == 1 else url
        db.execute(
            """INSERT INTO radar_batch_candidates
            (id,batch_id,workspace_id,platform,source_key,title,url,discovered_at)
            VALUES(?,?,?,?,?,'不得打印的标题',?,?)""",
            (f"candidate_{index}", batch_id, workspace["id"], platform, f"hash_not_post_id_{index}", candidate_url, now),
        )
        if platform == "xiaohongshu":
            owner = other["id"] if index == 6 else workspace["id"]
            verified = index in {1, 2, 6}
            status = "success" if verified else "ready_to_archive" if index == 4 else "unavailable" if index == 5 else "partial"
            media = "COMPLETE" if index in {1, 6} else "PARTIAL"
            digest = hashlib.sha256(body.encode()).hexdigest()
            db.execute(
                """INSERT INTO xhs_post_extractions
                (id,workspace_id,url,post_key,status,body,body_raw,body_sha256,latest_archive_id,verification_status,archive_status,extracted_at,updated_at)
                VALUES(?,?,?,?,?,'只有摘要也不能算原文',?,?,?,?,?,?,?)""",
                (f"xhs_{index}", owner, url, post_id, status, body if verified else None,
                 digest if verified else None, f"version_{index}" if verified else None,
                 "VERIFIED" if verified else "UNVERIFIED", media, now, now),
            )
            if verified:
                relative = f"{owner}/{post_id}/version_{index}"
                db.execute(
                    """INSERT INTO xhs_archive_versions
                    (id,workspace_id,extraction_id,post_id,observed_post_id,archive_path,body_raw,body_source,body_evidence_json,
                     body_sha256,archive_status,verification_status,captured_at,collector_version)
                    VALUES(?,?,?,?,?,?,?,'embedded_state',?,?,?,'VERIFIED',?,'test')""",
                    (f"version_{index}", owner, f"xhs_{index}", post_id, post_id, relative, body,
                     json_dumps({"selected_body_field": "desc", "body_extraction_status": "VERIFIED"}), digest, media, now),
                )
                directory = archive_root / relative
                directory.mkdir(parents=True)
                (directory / "body_raw.txt").write_bytes(body.encode())
            if index == 4:
                db.execute(
                    """INSERT INTO xhs_archive_jobs(id,workspace_id,extraction_id,operation,idempotency_key,browser_profile_id,status,stage,revision,created_at,updated_at)
                    VALUES('queued_job',?,?,'archive','test','social_browser','QUEUED','QUEUED',2,?,?)""",
                    (owner, f"xhs_{index}", now, now),
                )
        else:
            db.execute(
                """INSERT INTO douyin_post_extractions
                (id,workspace_id,url,video_id,observed_video_id,published_body,published_body_source,asr_text,status,extracted_at,updated_at)
                VALUES(?,?,?,?,?,?,?,'ASR不是发布全文',?,?,?)""",
                (f"dy_{index}", workspace["id"], url, post_id, post_id, body,
                 "详情页结构化发布文案" if index == 7 else "详情页元数据文案片段",
                 "COMPLETE" if index == 7 else "PARTIAL", now, now),
            )
    return db, batch_id, workspace, other, archive_root


def test_audit_counts_body_separately_from_media_and_excludes_snippets(sample_batch):
    db, batch_id, workspace, _other, _archive_root = sample_batch
    before = db.path.read_bytes()
    report = audit_batch(db.path, batch_id, workspace_id=workspace["id"])

    assert report["candidate_count"] == 8
    assert report["missing_search_candidates"] == 2
    assert report["verified_body_complete"] == 3
    xhs = report["platforms"]["xiaohongshu"]
    assert (xhs["verified_body_complete"], xhs["unverified"], xhs["queued"], xhs["unavailable"]) == (2, 2, 1, 1)
    assert xhs["body_complete_media_complete"] == 1
    assert xhs["body_complete_media_partial"] == 1
    assert xhs["missing_record"] == 1  # The other workspace's full body does not count.
    assert report["platforms"]["douyin"]["verified_body_complete"] == 1
    assert report["platforms"]["douyin"]["unverified"] == 1
    assert report["integrity"]["hashes_checked"] == report["integrity"]["files_checked"] == 0
    assert db.path.read_bytes() == before


def test_audit_optional_hash_and_body_file_checks_detect_corruption(sample_batch):
    db, batch_id, _workspace, _other, root = sample_batch
    good = audit_batch(db.path, batch_id, verify_files=True, archive_root=root)
    assert good["verified_body_complete"] == 3
    assert good["integrity"]["hashes_checked"] == good["integrity"]["files_checked"] == 2

    row = db.one("SELECT archive_path FROM xhs_archive_versions WHERE id='version_2'")
    (root / row["archive_path"] / "body_raw.txt").write_bytes(b"corrupted")
    damaged_file = audit_batch(db.path, batch_id, verify_files=True, archive_root=root)
    assert damaged_file["verified_body_complete"] == 2
    assert damaged_file["integrity"]["file_failures"] == 1
    assert damaged_file["platforms"]["xiaohongshu"]["integrity_failed"] == 1

    db.execute("UPDATE xhs_archive_versions SET body_sha256='wrong' WHERE id='version_1'")
    damaged_hash = audit_batch(db.path, batch_id, verify_body_hashes=True)
    assert damaged_hash["verified_body_complete"] == 2
    assert damaged_hash["integrity"]["hash_failures"] == 1
    assert damaged_hash["integrity"]["files_checked"] == 0


def test_audit_summary_never_prints_copy_titles_or_signed_urls(sample_batch, capsys):
    db, batch_id, _workspace, other, _root = sample_batch
    assert main(["--db", str(db.path), "--batch-id", batch_id]) == 0
    text = capsys.readouterr().out
    assert json.loads(text)["candidate_count"] == 8
    for private in ("不得打印", "不得输出", "NEVER_PRINT_SIGNATURE", "https://", "xsec_token", "ASR不是发布全文"):
        assert private not in text
    with pytest.raises(LookupError):
        audit_batch(db.path, batch_id, workspace_id=other["id"])


def test_missing_database_is_not_created(tmp_path: Path, capsys):
    missing = tmp_path / "missing.sqlite3"
    assert main(["--db", str(missing), "--batch-id", "missing"]) == 2
    assert not missing.exists()
    assert "error" in capsys.readouterr().err
