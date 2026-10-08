from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import time
from pathlib import Path

import pytest
from PIL import Image

from backend.app.db import Database, utc_now
from backend.app.platform_browser import PlatformBrowserError
from backend.app.xhs_archive import XiaohongshuArchiveRepository, XiaohongshuArchiveService
from backend.app.xhs_extractions import XiaohongshuExtractionService
from scripts.migrate_xhs_archive_v2 import migrate
from scripts.migrate_xhs_fidelity_v3 import migrate as migrate_v3


POST_ID = "68c6f07d000000001d01abcd"
POST_URL = f"https://www.xiaohongshu.com/explore/{POST_ID}"


def image_bytes(color: tuple[int, int, int]) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (24, 32), color).save(output, format="PNG")
    return output.getvalue()


def setup_db(tmp_path: Path) -> tuple[Database, dict, str]:
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    workspace = db.create_workspace("归档测试")
    extraction_id = "xhs_test_extraction"
    now = utc_now()
    db.execute(
        """INSERT INTO xhs_post_extractions
        (id,workspace_id,url,post_key,title,status,extracted_at,updated_at)
        VALUES(?,?,?,?,?,'partial',?,?)""",
        (extraction_id, workspace["id"], POST_URL, POST_ID, "同标题不等于同帖子", now, now),
    )
    return db, workspace, extraction_id


def capture(image_count: int = 6, *, missing: set[int] | None = None) -> dict:
    missing = missing or set()
    raw = "第一行  保留两个空格\n\n第二行🙂\n#原话题"
    assets = []
    for ordinal in range(1, image_count + 1):
        assets.append({
            "role": "carousel",
            "ordinal": ordinal,
            "source_url": f"https://ci.xiaohongshu.test/images/{ordinal}.png?token=secret",
            "source_kind": "embedded_state+network_response",
            "ordered_by": "page_state",
            "mime_declared": "image/png",
            "http_status": 200 if ordinal not in missing else 503,
            "bytes": None if ordinal in missing else image_bytes((ordinal * 20, 10, 80)),
            "error_code": "HTTP_503" if ordinal in missing else "",
        })
    assets.append({
        "role": "cover",
        "ordinal": 1,
        "source_url": "https://ci.xiaohongshu.test/cover.png?token=secret",
        "source_kind": "og_image+network_response",
        "ordered_by": "cover_metadata",
        "mime_declared": "image/png",
        "http_status": 200,
        "bytes": image_bytes((255, 80, 120)),
    })
    return {
        "target_post_id": POST_ID,
        "observed_post_id": POST_ID,
        "canonical_url": POST_URL,
        "title": "测试图文",
        "author": "作者",
        "body_raw": raw,
        "body_source": "embedded_state",
        "body_verified": True,
        "body_evidence": {"same_post_id": True},
        "identity_evidence": {
            "state_post_id": POST_ID,
            "detail_url_post_id": POST_ID,
        },
        "expected_image_count": image_count,
        "video_count": 0,
        "assets": assets,
        "diagnostics": {},
    }


def test_archive_preserves_raw_body_and_all_six_images_in_order(tmp_path: Path):
    db, workspace, extraction_id = setup_db(tmp_path)
    repository = XiaohongshuArchiveRepository(db, tmp_path / "archives")

    result = repository.persist(workspace["id"], extraction_id, capture())

    assert result.archive_status == "COMPLETE"
    assert result.expected_asset_count == 6
    assert result.saved_asset_count == 6
    version = db.one("SELECT * FROM xhs_archive_versions WHERE id=?", (result.archive_id,))
    archive_dir = tmp_path / "archives" / version["archive_path"]
    assert (archive_dir / "body_raw.txt").read_text() == capture()["body_raw"]
    rows = db.all(
        "SELECT ordinal,status,relative_path,sha256,source_ref FROM xhs_post_assets WHERE archive_id=? AND role='carousel' ORDER BY ordinal",
        (result.archive_id,),
    )
    assert [row["ordinal"] for row in rows] == [1, 2, 3, 4, 5, 6]
    assert all(row["status"] == "SAVED" for row in rows)
    assert all("token=" not in row["source_ref"] for row in rows)
    assert all((archive_dir / row["relative_path"]).read_bytes() for row in rows)
    assert (archive_dir / "manifest.json").is_file()
    assert (archive_dir / "checksums.sha256").is_file()
    assert json.loads(version["body_evidence_json"])["identity_evidence"] == {
        "state_post_id": POST_ID,
        "detail_url_post_id": POST_ID,
        "signals_agree": True,
    }


def test_archive_preserves_whitespace_punctuation_hashtags_and_emoji_byte_for_byte(tmp_path: Path):
    db, workspace, extraction_id = setup_db(tmp_path)
    repository = XiaohongshuArchiveRepository(db, tmp_path / "archives")
    value = capture()
    exact_raw = "  首行：『符号』  \r\n\t第二行👩🏽‍💻🔥\n\n#长沙相亲 #脱单\n末行……！？  "
    value["body_raw"] = exact_raw

    result = repository.persist(workspace["id"], extraction_id, value)

    version = db.one("SELECT * FROM xhs_archive_versions WHERE id=?", (result.archive_id,))
    archive_dir = tmp_path / "archives" / version["archive_path"]
    expected_bytes = exact_raw.encode("utf-8")
    assert version["body_raw"] == exact_raw
    assert (archive_dir / "body_raw.txt").read_bytes() == expected_bytes
    assert version["body_sha256"] == hashlib.sha256(expected_bytes).hexdigest()
    assert version["body_normalized"] != exact_raw

    evidence = json.loads(version["body_evidence_json"])
    integrity = evidence["format_integrity"]
    assert integrity["byte_count"] == len(expected_bytes)
    assert integrity["sha256"] == version["body_sha256"]
    assert integrity["lf_count"] == 4
    assert integrity["cr_count"] == 1
    assert integrity["tab_count"] == 1
    assert integrity["leading_whitespace_preserved"] is True
    assert integrity["trailing_whitespace_preserved"] is True


def test_identity_mismatch_is_rejected_without_writing_archive(tmp_path: Path):
    db, workspace, extraction_id = setup_db(tmp_path)
    repository = XiaohongshuArchiveRepository(db, tmp_path / "archives")
    value = capture()
    value["observed_post_id"] = "different_post"

    with pytest.raises(PlatformBrowserError) as error:
        repository.persist(workspace["id"], extraction_id, value)

    assert error.value.code == "identity_mismatch"
    assert db.one("SELECT id FROM xhs_archive_versions") is None


def test_archive_binds_collector_identity_to_the_original_selected_url(tmp_path: Path):
    """A self-consistent collector result must still match the selected post."""
    db, workspace, extraction_id = setup_db(tmp_path)
    repository = XiaohongshuArchiveRepository(db, tmp_path / "archives")
    value = capture()
    value["target_post_id"] = "68c6f07d000000001d99beef"
    value["observed_post_id"] = "68c6f07d000000001d99beef"

    with pytest.raises(PlatformBrowserError) as error:
        repository.persist(workspace["id"], extraction_id, value)

    assert error.value.code == "identity_mismatch"
    assert error.value.diagnostics["requested_post_id"] == POST_ID
    assert db.one("SELECT id FROM xhs_archive_versions") is None


def test_archive_rejects_conflicting_state_and_detail_url_identity_evidence(tmp_path: Path):
    db, workspace, extraction_id = setup_db(tmp_path)
    repository = XiaohongshuArchiveRepository(db, tmp_path / "archives")
    value = capture()
    value["identity_evidence"] = {
        "state_post_id": POST_ID,
        "detail_url_post_id": "68c6f07d000000001d99beef",
    }

    with pytest.raises(PlatformBrowserError) as error:
        repository.persist(workspace["id"], extraction_id, value)

    assert error.value.code == "identity_mismatch"
    assert error.value.diagnostics["detail_url_post_id"] == "68c6f07d000000001d99beef"
    assert db.one("SELECT id FROM xhs_archive_versions") is None


def test_blank_body_is_preserved_but_never_marked_as_verified_original(tmp_path: Path):
    db, workspace, extraction_id = setup_db(tmp_path)
    repository = XiaohongshuArchiveRepository(db, tmp_path / "archives")
    value = capture()
    value["body_raw"] = " \r\n\t "
    value["body_verified"] = True

    result = repository.persist(workspace["id"], extraction_id, value)

    version = db.one(
        "SELECT body_raw,verification_status,archive_status FROM xhs_archive_versions WHERE id=?",
        (result.archive_id,),
    )
    assert version == {
        "body_raw": " \r\n\t ",
        "verification_status": "UNVERIFIED",
        "archive_status": "PARTIAL",
    }
    archive_dir = tmp_path / "archives" / db.one(
        "SELECT archive_path FROM xhs_archive_versions WHERE id=?", (result.archive_id,)
    )["archive_path"]
    assert (archive_dir / "body_raw.txt").read_bytes() == b" \r\n\t "


def test_partial_archive_can_be_completed_and_failed_attempt_does_not_replace_complete(tmp_path: Path):
    db, workspace, extraction_id = setup_db(tmp_path)
    repository = XiaohongshuArchiveRepository(db, tmp_path / "archives")
    partial = repository.persist(workspace["id"], extraction_id, capture(missing={4}))
    assert partial.archive_status == "PARTIAL"
    assert partial.missing_ordinals == [4]

    complete = repository.persist(workspace["id"], extraction_id, capture())
    assert complete.archive_status == "COMPLETE"
    current = db.one("SELECT archive_status,latest_archive_id FROM xhs_post_extractions WHERE id=?", (extraction_id,))
    assert current == {"archive_status": "COMPLETE", "latest_archive_id": complete.archive_id}

    later_partial = repository.persist(workspace["id"], extraction_id, capture(missing={2}))
    assert later_partial.archive_status == "PARTIAL"
    current = db.one("SELECT archive_status,latest_archive_id FROM xhs_post_extractions WHERE id=?", (extraction_id,))
    assert current == {"archive_status": "COMPLETE", "latest_archive_id": complete.archive_id}
    assert db.one("SELECT COUNT(*) AS count FROM xhs_archive_versions")["count"] == 3
    visible = XiaohongshuExtractionService(db=db).get_by_id(workspace["id"], extraction_id)
    assert visible["archive"]["id"] == complete.archive_id
    assert visible["archive"]["archive_status"] == "COMPLETE"
    assert visible["body_extraction_status"] == "VERIFIED"


def test_partial_archive_reports_verified_body_separately_from_rich_fidelity(tmp_path: Path):
    db, workspace, extraction_id = setup_db(tmp_path)
    repository = XiaohongshuArchiveRepository(db, tmp_path / "archives")
    value = capture()
    value["body_rich"] = [{"type": "inline_image", "alt": "表情"}]
    value["body_rich_alignment"] = "MISMATCH"

    result = repository.persist(workspace["id"], extraction_id, value)
    extraction = XiaohongshuExtractionService(db=db).get_by_id(workspace["id"], extraction_id)

    assert result.archive_status == "PARTIAL"
    assert extraction["archive"]["archive_status"] == "PARTIAL"
    assert extraction["archive"]["verification_status"] == "VERIFIED"
    assert extraction["archive"]["body_extraction_status"] == "VERIFIED"
    assert extraction["body_extraction_status"] == "VERIFIED"


@pytest.mark.parametrize("later_body", [None, "页面仅剩这一小段"])
def test_verified_partial_body_survives_later_unverified_archive(tmp_path: Path, later_body: str | None):
    db, workspace, extraction_id = setup_db(tmp_path)
    repository = XiaohongshuArchiveRepository(db, tmp_path / "archives")
    saved = repository.persist(workspace["id"], extraction_id, capture(missing={4}))
    assert saved.archive_status == "PARTIAL"
    previous = db.one("SELECT * FROM xhs_post_extractions WHERE id=?", (extraction_id,))

    degraded_capture = capture(missing={2})
    degraded_capture.update(body_raw=later_body, body_verified=False, body_source="expanded_dom")
    degraded = repository.persist(workspace["id"], extraction_id, degraded_capture)

    assert degraded.verification_status == "UNVERIFIED"
    current = db.one("SELECT * FROM xhs_post_extractions WHERE id=?", (extraction_id,))
    for key in ("body_raw", "body_sha256", "content_sha256", "verification_status", "latest_archive_id", "saved_asset_count"):
        assert current[key] == previous[key]
    assert db.one("SELECT COUNT(*) AS count FROM xhs_archive_versions")["count"] == 2
    visible = XiaohongshuExtractionService(db=db).get_by_id(workspace["id"], extraction_id)
    assert visible["archive"]["id"] == saved.archive_id
    assert visible["archive"]["body_raw"] == capture()["body_raw"]
    assert visible["body_extraction_status"] == "VERIFIED"
    assert XiaohongshuArchiveService(db, root=tmp_path / "archives").latest_archive(
        workspace["id"], extraction_id,
    )["id"] == saved.archive_id

    # A later verified capture is still allowed to repair the missing media.
    repaired = repository.persist(workspace["id"], extraction_id, capture())
    assert repaired.archive_status == "COMPLETE"
    assert db.one("SELECT latest_archive_id FROM xhs_post_extractions WHERE id=?", (extraction_id,))["latest_archive_id"] == repaired.archive_id


@pytest.mark.parametrize("missing", [set(), {2}])
def test_adopted_archive_replaces_stale_failure_with_current_diagnostics(tmp_path: Path, missing: set[int]):
    db, workspace, extraction_id = setup_db(tmp_path)
    db.execute(
        """UPDATE xhs_post_extractions SET failure_stage='post_unavailable',error_message='旧不可访问原因',
        diagnostics_json='{"old_failure":true}' WHERE id=?""", (extraction_id,),
    )
    repository = XiaohongshuArchiveRepository(db, tmp_path / "archives")
    result = repository.persist(workspace["id"], extraction_id, capture(missing=missing))
    current = db.one("SELECT * FROM xhs_post_extractions WHERE id=?", (extraction_id,))
    diagnostics = json.loads(current["diagnostics_json"])

    assert "old_failure" not in diagnostics
    assert "旧不可访问原因" not in current["error_message"]
    assert current["verification_status"] == "VERIFIED"
    if missing:
        assert result.archive_status == "PARTIAL"
        assert current["failure_stage"] == "archive_partial"
        assert "MISSING_IMAGE_ORDINALS" in current["error_message"]
        assert diagnostics["missing_ordinals"] == [2]
    else:
        assert result.archive_status == "COMPLETE"
        assert current["failure_stage"] == current["error_message"] == ""
        assert diagnostics["failure_reasons"] == []


def test_dom_fallback_is_reported_as_possible_page_structure_change_not_original_copy(tmp_path: Path):
    db, workspace, extraction_id = setup_db(tmp_path)
    repository = XiaohongshuArchiveRepository(db, tmp_path / "archives")
    value = capture()
    value["body_source"] = "expanded_dom"
    value["body_verified"] = False
    value["body_evidence"] = {
        "source": "expanded_dom",
        "selected_body_field": "",
        "state_candidate_count": 1,
    }

    result = repository.persist(workspace["id"], extraction_id, value)
    extraction = XiaohongshuExtractionService(db=db).get_by_id(workspace["id"], extraction_id)

    assert result.archive_status == "PARTIAL"
    assert extraction["archive"]["body_extraction_status"] == "PAGE_STRUCTURE_CHANGED"
    assert extraction["archive"]["body_evidence"]["body_extraction_code"] == "PAGE_STRUCTURE_CHANGED"
    assert "可能是平台页面结构变化" in extraction["archive"]["body_extraction_message"]
    assert extraction["body_raw"] is None
    assert extraction["body_extraction_status"] == "PAGE_STRUCTURE_CHANGED"


def test_unverified_structured_text_is_not_mislabeled_as_dom_structure_change(tmp_path: Path):
    db, workspace, extraction_id = setup_db(tmp_path)
    repository = XiaohongshuArchiveRepository(db, tmp_path / "archives")
    value = capture()
    value["body_source"] = "embedded_state"
    value["body_verified"] = False

    repository.persist(workspace["id"], extraction_id, value)
    extraction = XiaohongshuExtractionService(db=db).get_by_id(workspace["id"], extraction_id)

    assert extraction["archive"]["body_extraction_status"] == "UNVERIFIED"
    assert extraction["body_raw"] is None


@pytest.mark.parametrize("http_status", [403, 429])
def test_archive_records_platform_media_http_failures_without_claiming_complete(tmp_path: Path, http_status: int):
    db, workspace, extraction_id = setup_db(tmp_path)
    repository = XiaohongshuArchiveRepository(db, tmp_path / "archives")
    value = capture()
    asset = next(item for item in value["assets"] if item["role"] == "carousel" and item["ordinal"] == 2)
    asset.update({"bytes": None, "http_status": http_status, "error_code": f"HTTP_{http_status}"})

    result = repository.persist(workspace["id"], extraction_id, value)

    assert result.archive_status == "PARTIAL"
    assert result.missing_ordinals == [2]
    row = db.one(
        "SELECT status,http_status,error_code FROM xhs_post_assets WHERE archive_id=? AND role='carousel' AND ordinal=2",
        (result.archive_id,),
    )
    assert row == {"status": "FAILED", "http_status": http_status, "error_code": f"HTTP_{http_status}"}


def test_asset_download_is_scoped_to_workspace(tmp_path: Path):
    db, workspace, extraction_id = setup_db(tmp_path)
    repository = XiaohongshuArchiveRepository(db, tmp_path / "archives")
    service = XiaohongshuArchiveService(db, collector=object(), root=tmp_path / "archives")
    result = repository.persist(workspace["id"], extraction_id, capture())
    asset = db.one("SELECT id FROM xhs_post_assets WHERE archive_id=? AND role='carousel' ORDER BY ordinal LIMIT 1", (result.archive_id,))

    path, media_type = service.resolve_asset(workspace["id"], result.archive_id, asset["id"])
    assert path.is_file()
    assert media_type == "image/png"
    with pytest.raises(FileNotFoundError):
        service.resolve_asset("ws_other", result.archive_id, asset["id"])


class FakeArchiveCollector:
    def __init__(self, value: dict | Exception):
        self.value = value

    def archive_xiaohongshu_detail(self, *_args, **_kwargs):
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class BlockingArchiveCollector:
    def archive_xiaohongshu_detail(self, *_args, **_kwargs):
        time.sleep(0.2)
        return capture()


class FlakyConnectArchiveCollector:
    def __init__(self) -> None:
        self.calls = 0

    def archive_xiaohongshu_detail(self, *_args, **_kwargs):
        self.calls += 1
        if self.calls < 3:
            raise PlatformBrowserError(
                "图文归档读取失败：BrowserType.connect_over_cdp: Timeout retrieving websocket url",
                code="archive_collect_failed",
            )
        return capture()


def wait_job(service: XiaohongshuArchiveService, workspace_id: str, job_id: str) -> dict:
    deadline = time.monotonic() + 3
    job = service.get_job(workspace_id, job_id)
    while job and job["status"] in {"QUEUED", "RUNNING"} and time.monotonic() < deadline:
        time.sleep(0.02)
        job = service.get_job(workspace_id, job_id)
    return job or {}


def test_persistent_job_pauses_for_verification_and_resumes(tmp_path: Path):
    db, workspace, extraction_id = setup_db(tmp_path)
    collector = FakeArchiveCollector(PlatformBrowserError("请完成验证", code="verification_required"))
    service = XiaohongshuArchiveService(db, collector=collector, root=tmp_path / "archives")
    service.start()
    try:
        first = service.enqueue(workspace["id"], extraction_id)
        paused = wait_job(service, workspace["id"], first["id"])
        assert paused["status"] == "PAUSED"
        assert db.one("SELECT reason FROM xhs_browser_pause WHERE profile_id='social_browser'")
        assert db.one("SELECT status FROM xhs_post_extractions WHERE id=?", (extraction_id,))["status"] == "needs_verification"
        first_attempt = db.one(
            "SELECT retry_count,last_attempt_at FROM xhs_post_extractions WHERE id=?", (extraction_id,)
        )
        assert first_attempt["retry_count"] == 0
        assert first_attempt["last_attempt_at"]

        collector.value = capture()
        resumed = service.enqueue(workspace["id"], extraction_id, resume_after_user_action=True)
        completed = wait_job(service, workspace["id"], resumed["id"])
        assert completed["status"] == "COMPLETE"
        assert db.one("SELECT reason FROM xhs_browser_pause WHERE profile_id='social_browser'") is None
        retried = db.one(
            "SELECT retry_count,last_attempt_at FROM xhs_post_extractions WHERE id=?", (extraction_id,)
        )
        assert retried["retry_count"] == 1
        assert retried["last_attempt_at"] >= first_attempt["last_attempt_at"]
    finally:
        service.stop()


@pytest.mark.parametrize("code", ["identity_mismatch", "verification_required"])
def test_platform_failure_preserves_safe_job_diagnostics_without_replacing_saved_body(tmp_path: Path, code: str):
    db, workspace, extraction_id = setup_db(tmp_path)
    root = tmp_path / "archives"
    saved = XiaohongshuArchiveRepository(db, root).persist(workspace["id"], extraction_id, capture())
    previous = db.one("SELECT * FROM xhs_archive_versions WHERE id=?", (saved.archive_id,))
    other_id = "68c6f07d000000001d99beef"
    collector = FakeArchiveCollector(PlatformBrowserError("当前原帖尚未恢复", code=code, diagnostics={
        "target_post_id": POST_ID,
        "observed_post_id": other_id,
        "candidate_url": f"https://username:private-password@www.xiaohongshu.com/explore/{other_id}?xsec_token=secret-token#private-fragment",
        "attempted_queries": ["不可保存的私人搜索1", "不可保存的私人搜索2"],
        "recovery_reason": "similar_title_different_id",
        "recovery_attempts": 2,
        "headers": {"Cookie": "private-cookie"},
        "token": "private-token",
        "body": "不可保存的页面正文",
        "unexpected_nested": {"password": "secret"},
    }))
    service = XiaohongshuArchiveService(db=db, collector=collector, root=root)
    job = service.enqueue(workspace["id"], extraction_id)
    service._run_job(job["id"])
    completed = service.get_job(workspace["id"], job["id"])

    assert completed["status"] == ("FAILED" if code == "identity_mismatch" else "PAUSED")
    assert completed["progress"]["diagnostics"] == {
        "target_post_id": POST_ID,
        "observed_post_id": other_id,
        "candidate_url": f"https://www.xiaohongshu.com/explore/{other_id}",
        "recovery_query_count": 2,
        "recovery_reason": "similar_title_different_id",
        "recovery_attempts": 2,
    }
    serialized = json.dumps(completed, ensure_ascii=False)
    for secret in ("private-password", "secret-token", "private-fragment", "private-cookie", "private-token", "不可保存"):
        assert secret not in serialized
    current = db.one("SELECT * FROM xhs_post_extractions WHERE id=?", (extraction_id,))
    assert current["latest_archive_id"] == saved.archive_id
    assert current["body_raw"] == previous["body_raw"]
    assert current["body_sha256"] == previous["body_sha256"]
    assert db.one("SELECT * FROM xhs_archive_versions WHERE id=?", (saved.archive_id,)) == previous


def test_finite_search_failure_stays_retryable_without_claiming_post_unavailable(tmp_path: Path):
    db, workspace, extraction_id = setup_db(tmp_path)
    diagnostics = {
        "target_post_id": POST_ID, "recovery_query_count": 2,
        "recovery_result_count": 24, "recovery_unique_id_count": 12,
        "recovery_stop_reason": "stable_results", "exact_id_found": False,
        "recovery_reason": "limited_search_no_exact_id",
    }
    collector = FakeArchiveCollector(PlatformBrowserError(
        "有限搜索未找回同一帖子 ID 的可访问链接，可稍后重试",
        code="access_url_recovery_failed", diagnostics=diagnostics,
    ))
    service = XiaohongshuArchiveService(db=db, collector=collector, root=tmp_path / "archives")
    job = service.enqueue(workspace["id"], extraction_id)
    service._run_job(job["id"])
    failed = service.get_job(workspace["id"], job["id"])
    assert failed["status"] == "FAILED"
    assert failed["error_code"] == "ACCESS_URL_RECOVERY_FAILED"
    assert failed["progress"]["diagnostics"] == diagnostics
    extraction = db.one("SELECT status,failure_stage FROM xhs_post_extractions WHERE id=?", (extraction_id,))
    assert extraction == {"status": "failed", "failure_stage": "access_url_recovery_failed"}
    retried = service.enqueue(workspace["id"], extraction_id)
    assert retried["status"] == "QUEUED"
    assert retried["id"] != job["id"]


@pytest.mark.parametrize("error_code,stage", [
    ("browser_page_required", "NEEDS_BROWSER"),
    ("browser_not_running", "NEEDS_BROWSER"),
    ("verification_required", "USER_ACTION_REQUIRED"),
    ("login_required", "USER_ACTION_REQUIRED"),
    ("platform_limited", "USER_ACTION_REQUIRED"),
])
def test_shared_browser_block_pauses_existing_queue_without_attempting_every_post(
    tmp_path: Path, error_code: str, stage: str,
):
    db, workspace, extraction_id = setup_db(tmp_path)
    second_id = "xhs_second_queued_extraction"
    second_post_id = "68c6f07d000000001d01abce"
    second_url = f"https://www.xiaohongshu.com/explore/{second_post_id}"
    now = utc_now()
    db.execute(
        """INSERT INTO xhs_post_extractions
        (id,workspace_id,url,post_key,title,status,extracted_at,updated_at)
        VALUES(?,?,?,?,?,'queued',?,?)""",
        (second_id, workspace["id"], second_url, second_post_id, "待读取原帖", now, now),
    )
    collector = FakeArchiveCollector(PlatformBrowserError("采集浏览器需要处理", code=error_code))
    service = XiaohongshuArchiveService(db, collector=collector, root=tmp_path / "archives")
    first = service.enqueue(workspace["id"], extraction_id)
    second = service.enqueue(workspace["id"], second_id)
    service._run_job(first["id"])
    service._run_job(second["id"])

    paused_first = service.get_job(workspace["id"], first["id"])
    paused_second = service.get_job(workspace["id"], second["id"])
    assert paused_first["status"] == paused_second["status"] == "PAUSED"
    assert paused_first["stage"] == paused_second["stage"] == stage
    assert paused_second["error_code"] == error_code.upper()
    assert paused_first["attempt"] == 1
    assert paused_second["attempt"] == 0
    untouched = db.one("SELECT last_attempt_at,retry_count FROM xhs_post_extractions WHERE id=?", (second_id,))
    assert untouched == {"last_attempt_at": "", "retry_count": 0}
    if stage == "NEEDS_BROWSER":
        assert db.one("SELECT status FROM xhs_post_extractions WHERE id=?", (second_id,))["status"] == "needs_browser"

    collector.value = capture()
    collector.value.update(
        target_post_id=second_post_id,
        observed_post_id=second_post_id,
        canonical_url=second_url,
        identity_evidence={"state_post_id": second_post_id, "detail_url_post_id": second_post_id},
    )
    resumed = service.enqueue(workspace["id"], second_id, resume_after_user_action=True)
    service._run_job(resumed["id"])
    assert service.get_job(workspace["id"], resumed["id"])["status"] == "COMPLETE"
    assert db.one("SELECT reason FROM xhs_browser_pause WHERE profile_id='social_browser'") is None


def test_stalled_browser_call_reaches_explicit_timeout(tmp_path: Path):
    db, workspace, extraction_id = setup_db(tmp_path)
    service = XiaohongshuArchiveService(
        db,
        collector=BlockingArchiveCollector(),
        root=tmp_path / "archives",
        collector_timeout_seconds=0.02,
    )
    service.start()
    try:
        created = service.enqueue(workspace["id"], extraction_id)
        failed = wait_job(service, workspace["id"], created["id"])
        assert failed["status"] == "FAILED"
        assert failed["error_code"] == "ARCHIVE_TIMEOUT"
        assert "已请求取消" in failed["progress"]["message"]
        assert "不会启动重复采集" in failed["progress"]["message"]
        assert db.one("SELECT latest_archive_id FROM xhs_post_extractions WHERE id=?", (extraction_id,))["latest_archive_id"] is None
        assert db.one("SELECT status FROM xhs_post_extractions WHERE id=?", (extraction_id,))["status"] == "failed"
    finally:
        service.stop()


def test_failed_direct_archive_ends_pending_state_without_overwriting_saved_body(tmp_path: Path):
    db = Database(tmp_path / "failed-state.sqlite3")
    db.initialize()
    workspace = db.create_workspace("失败状态")
    extraction_service = XiaohongshuExtractionService(db, collector=object())
    direct = extraction_service.ensure_archive_record(workspace["id"], POST_URL, title_hint="新帖子")
    collector = FakeArchiveCollector(PlatformBrowserError("原帖不可访问", code="post_unavailable"))
    service = XiaohongshuArchiveService(db, collector=collector, root=tmp_path / "archives")
    service.start()
    try:
        failed = wait_job(service, workspace["id"], service.enqueue(workspace["id"], direct["id"])["id"])
        assert failed["status"] == "FAILED"
        row = db.one("SELECT status,failure_stage,error_message FROM xhs_post_extractions WHERE id=?", (direct["id"],))
        assert row["status"] == "unavailable"
        assert row["failure_stage"] == "post_unavailable"
        assert "不可访问" in row["error_message"]

        db.execute(
            "UPDATE xhs_post_extractions SET status='success',body='old verified visible body' WHERE id=?",
            (direct["id"],),
        )
        failed_again = wait_job(service, workspace["id"], service.enqueue(workspace["id"], direct["id"])["id"])
        assert failed_again["status"] == "FAILED"
        preserved = db.one("SELECT status,body FROM xhs_post_extractions WHERE id=?", (direct["id"],))
        assert preserved == {"status": "success", "body": "old verified visible body"}
    finally:
        service.stop()


def test_startup_reconciles_terminal_job_left_as_ready_to_archive(tmp_path: Path):
    db = Database(tmp_path / "reconcile-state.sqlite3")
    db.initialize()
    workspace = db.create_workspace("重启恢复")
    extraction_service = XiaohongshuExtractionService(db, collector=object())
    direct = extraction_service.ensure_archive_record(workspace["id"], POST_URL, title_hint="卡住的帖子")
    service = XiaohongshuArchiveService(db, collector=object(), root=tmp_path / "archives")
    job = service.enqueue(workspace["id"], direct["id"])
    db.execute(
        "UPDATE xhs_archive_jobs SET status='FAILED',stage='FAILED',error_code='POST_UNAVAILABLE',progress_json=? WHERE id=?",
        (json.dumps({"message": "原帖不可访问"}, ensure_ascii=False), job["id"]),
    )

    service.start()
    try:
        row = db.one("SELECT status,error_message FROM xhs_post_extractions WHERE id=?", (direct["id"],))
        assert row["status"] == "unavailable"
        assert "不可访问" in row["error_message"]
    finally:
        service.stop()


def test_transient_cdp_connection_failure_retries_before_marking_post_failed(tmp_path: Path):
    db, workspace, extraction_id = setup_db(tmp_path)
    collector = FlakyConnectArchiveCollector()
    service = XiaohongshuArchiveService(
        db,
        collector=collector,
        root=tmp_path / "archives",
        transient_retry_delay_seconds=0,
    )
    service.start()
    try:
        created = service.enqueue(workspace["id"], extraction_id)
        completed = wait_job(service, workspace["id"], created["id"])
        assert completed["status"] == "COMPLETE"
        assert collector.calls == 3
    finally:
        service.stop()


def test_migration_runner_is_idempotent(tmp_path: Path):
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            INSERT INTO schema_meta VALUES('schema_version','7');
            CREATE TABLE xhs_post_extractions(
              id TEXT PRIMARY KEY,
              workspace_id TEXT NOT NULL,
              url TEXT NOT NULL,
              UNIQUE(workspace_id,url)
            );
            """
        )
    first = migrate(path)
    second = migrate(path)
    assert first["schema_version"] == "8"
    assert "body_raw" in first["added_columns"]
    assert second["added_columns"] == []
    assert second["integrity"] == "ok"

    v3_first = migrate_v3(path)
    v3_second = migrate_v3(path)
    assert v3_first["schema_version"] == "9"
    assert "xhs_archive_versions.body_rich_json" in v3_first["added_columns"]
    assert v3_second["added_columns"] == []
    assert v3_second["integrity"] == "ok"


def test_schema_migration_backfills_only_persisted_attempt_history(tmp_path: Path):
    db, workspace, extraction_id = setup_db(tmp_path)
    first_time = "2026-09-25T10:00:00+00:00"
    latest_time = "2026-09-26T10:00:00+00:00"
    db.execute(
        "UPDATE xhs_post_extractions SET failure_stage='verification_required' WHERE id=?",
        (extraction_id,),
    )
    for index, updated_at in enumerate((first_time, latest_time), start=1):
        db.execute(
            """INSERT INTO xhs_archive_jobs
            (id,workspace_id,extraction_id,operation,idempotency_key,browser_profile_id,status,attempt,created_at,updated_at)
            VALUES(?,?,?,'archive',?,'social_browser','PAUSED',1,?,?)""",
            (f"job_{index}", workspace["id"], extraction_id, f"attempt-{index}", updated_at, updated_at),
        )

    queued_extraction_id = "xhs_queued_without_attempt"
    queued_post_id = "68c6f07d000000001d01abce"
    db.execute(
        """INSERT INTO xhs_post_extractions
        (id,workspace_id,url,post_key,status,extracted_at,updated_at)
        VALUES(?,?,?,?,'ready_to_archive',?,?)""",
        (queued_extraction_id, workspace["id"], f"https://www.xiaohongshu.com/explore/{queued_post_id}", queued_post_id, first_time, first_time),
    )

    douyin_url = "https://www.douyin.com/video/7450123456789012345"
    queued_douyin_url = "https://www.douyin.com/video/7450123456789012346"
    db.execute(
        """INSERT INTO douyin_post_extractions
        (id,workspace_id,url,video_id,status,extracted_at,updated_at)
        VALUES('dy_partial',?,?,?,'PARTIAL',?,?)""",
        (workspace["id"], douyin_url, "7450123456789012345", latest_time, latest_time),
    )
    db.execute(
        """INSERT INTO douyin_post_extractions
        (id,workspace_id,url,video_id,status,extracted_at,updated_at)
        VALUES('dy_queued',?,?,?,'QUEUED',?,?)""",
        (workspace["id"], queued_douyin_url, "7450123456789012346", latest_time, latest_time),
    )

    # Emulate a real v15 database so initialize() must apply the v16 change.
    db.execute("ALTER TABLE xhs_post_extractions DROP COLUMN last_attempt_at")
    db.execute("ALTER TABLE douyin_post_extractions DROP COLUMN last_attempt_at")
    db.execute("UPDATE schema_meta SET value='15' WHERE key='schema_version'")
    db.initialize()

    migrated_xhs = db.one(
        "SELECT failure_stage,retry_count,last_attempt_at FROM xhs_post_extractions WHERE id=?",
        (extraction_id,),
    )
    assert migrated_xhs == {
        "failure_stage": "verification_required",
        "retry_count": 1,
        "last_attempt_at": latest_time,
    }
    queued_xhs = db.one(
        "SELECT retry_count,last_attempt_at FROM xhs_post_extractions WHERE id=?",
        (queued_extraction_id,),
    )
    assert queued_xhs == {"retry_count": 0, "last_attempt_at": ""}
    partial_douyin = db.one("SELECT last_attempt_at FROM douyin_post_extractions WHERE id='dy_partial'")
    assert partial_douyin == {"last_attempt_at": latest_time}
    queued_douyin = db.one("SELECT last_attempt_at FROM douyin_post_extractions WHERE id='dy_queued'")
    assert queued_douyin == {"last_attempt_at": ""}


def test_direct_archive_record_does_not_run_legacy_ocr_extractor(tmp_path: Path):
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    workspace = db.create_workspace("直接归档测试")

    class LegacyCollectorMustNotRun:
        def extract_xiaohongshu_detail(self, *_args, **_kwargs):
            raise AssertionError("strict archive setup must not invoke legacy extraction")

    service = XiaohongshuExtractionService(db, collector=LegacyCollectorMustNotRun())
    record = service.ensure_archive_record(
        workspace["id"], POST_URL, search_keyword="测试主题", title_hint="直接保存原图与正文",
    )

    assert record["status"] == "ready_to_archive"
    assert record["url"] == POST_URL
    assert record["search_keyword"] == "测试主题"


def test_explicit_batch_persists_and_queues_all_thirty_selected_posts(tmp_path: Path):
    db = Database(tmp_path / "batch.sqlite3")
    db.initialize()
    workspace = db.create_workspace("批量归档")
    extraction_service = XiaohongshuExtractionService(db, collector=object())
    extraction_ids = []
    for number in range(30):
        post_id = f"68c6f07d000000001d{number:04d}"
        record = extraction_service.ensure_archive_record(
            workspace["id"], f"https://www.xiaohongshu.com/explore/{post_id}", title_hint=f"帖子 {number + 1}",
        )
        extraction_ids.append(record["id"])
    archive_service = XiaohongshuArchiveService(db, collector=object(), root=tmp_path / "archives")

    batch = archive_service.create_batch(workspace["id"], extraction_ids)

    assert batch["selected_count"] == 30
    assert len(batch["items"]) == 30
    assert batch["counts"]["waiting"] == 30
    assert db.one("SELECT COUNT(*) AS count FROM xhs_archive_batch_items WHERE batch_id=?", (batch["id"],))["count"] == 30


def test_paused_manual_batch_follows_single_item_resume_to_completion(tmp_path: Path):
    db, workspace, extraction_id = setup_db(tmp_path)
    collector = FakeArchiveCollector(PlatformBrowserError("请完成验证", code="verification_required"))
    service = XiaohongshuArchiveService(db=db, collector=collector, root=tmp_path / "archives")
    batch = service.create_batch(workspace["id"], [extraction_id])
    first_job = batch["items"][0]["job_id"]
    service._run_job(first_job)
    assert service.get_batch(workspace["id"], batch["id"])["status"] == "PAUSED"

    collector.value = capture()
    resumed = service.enqueue(workspace["id"], extraction_id, resume_after_user_action=True)
    waiting = service.get_batch(workspace["id"], batch["id"])
    assert waiting["items"][0]["job_id"] == resumed["id"]
    assert waiting["counts"]["waiting"] == 1
    assert resumed["queued_new"] is True
    assert service.enqueue(workspace["id"], extraction_id)["queued_new"] is False
    service._run_job(resumed["id"])

    completed = service.get_batch(workspace["id"], batch["id"])
    assert completed["status"] == "COMPLETE"
    assert completed["counts"]["complete"] == 1
    assert completed["counts"]["paused"] == 0
    assert service.get_job(workspace["id"], first_job)["status"] == "PAUSED"

    # A later retry must not rewrite a finished batch's original result.
    later = service.enqueue(workspace["id"], extraction_id)
    assert later["id"] != resumed["id"]
    assert service.get_batch(workspace["id"], batch["id"])["items"][0]["job_id"] == resumed["id"]
