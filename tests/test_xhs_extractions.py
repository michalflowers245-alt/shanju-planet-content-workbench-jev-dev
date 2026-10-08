from __future__ import annotations

from pathlib import Path
import json
import time

from fastapi.testclient import TestClient
import pytest

import backend.app.main as main_module
import backend.app.platform_browser as platform_browser_module
from backend.app.db import Database, utc_now
from backend.app.platform_browser import (
    PlatformBrowserCollector,
    PlatformBrowserError,
    _HiddenCDPTarget,
    _best_xhs_search_result,
    _configured_social_cdp_port,
    _configured_social_profile_dir,
    _page_access_state,
    _search_url,
    _safe_url,
    _wait_for_xhs_detail_hydration,
)
from backend.app.config import STATE_DIR
from backend.app.xhs_extractions import XiaohongshuExtractionService
from backend.app.xhs_archive import XiaohongshuArchiveService


POST_URL = "https://www.xiaohongshu.com/explore/68c6f07d000000001d01abcd"


def _confirm_queued(queued: list[tuple[str, str]]):
    def enqueue(workspace_id: str, extraction_id: str) -> dict:
        queued.append((workspace_id, extraction_id))
        return {"id": f"job_{extraction_id}", "status": "QUEUED"}
    return enqueue


def test_social_browser_profile_directory_defaults_and_can_reuse_an_existing_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """A separate test state can safely point at the user's existing profile.

    The path is referenced in place: this configuration must not imply a
    Cookie export or a copied browser profile.
    """
    monkeypatch.delenv("WORKBENCH_SOCIAL_PROFILE_DIR", raising=False)
    assert _configured_social_profile_dir() == STATE_DIR / "social_browser"

    existing_profile = tmp_path / "existing-dedicated-chrome-profile"
    monkeypatch.setenv("WORKBENCH_SOCIAL_PROFILE_DIR", str(existing_profile))
    assert _configured_social_profile_dir() == existing_profile.resolve()


def test_social_browser_cdp_port_defaults_to_existing_profile_port_and_can_be_overridden(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("WORKBENCH_SOCIAL_CDP_PORT", raising=False)
    assert _configured_social_cdp_port() == 9230

    monkeypatch.setenv("WORKBENCH_SOCIAL_CDP_PORT", "9444")
    assert _configured_social_cdp_port() == 9444


class FakeDetailCollector:
    def __init__(self, *, failure: PlatformBrowserError | None = None) -> None:
        self.failure = failure
        self.calls: list[str] = []
        self.contexts: list[tuple[str, str]] = []

    def extract_xiaohongshu_detail(self, url: str, *, search_keyword: str = "", title_hint: str = ""):
        self.calls.append(url)
        self.contexts.append((search_keyword, title_hint))
        if self.failure:
            raise self.failure
        return {
            "url": url,
            "post_key": "68c6f07d000000001d01abcd",
            "title": "第一次参加线下相亲",
            "author": "小豆子",
            "published_at": "2026-09-15",
            "body": "这是详情页正文。",
            "image_text": "这是图片里的文字。",
            "video_subtitle": "这是页面可见字幕。",
            "video_speech": "",
            "merged_copy": "这是详情页正文。\n\n这是图片里的文字。\n\n这是页面可见字幕。",
            "tags": ["#相亲", "#长沙脱单"],
            "metrics": {"likes": 1200},
            "screenshot_name": "68c6f07d000000001d01abcd/page.png",
            "status": "success",
            "completeness": 100,
            "field_sources": {"body": "网页 DOM", "image_text": "macOS Vision OCR"},
            "ocr_status": "success",
            "ocr_message": "已识别图片文字。",
            "asr_status": "subtitle_available",
            "asr_message": "已读取页面可见字幕。",
            "diagnostics": {"image_count": 1, "video_count": 1},
        }


def test_xhs_extract_queues_strict_archive_instead_of_returning_legacy_dom_copy(tmp_path: Path):
    db = Database(tmp_path / "xhs.sqlite3")
    db.initialize()
    workspace = db.create_workspace("小红书提取")
    collector = FakeDetailCollector()
    service = XiaohongshuExtractionService(db=db, collector=collector)
    queued: list[tuple[str, str]] = []

    def enqueue(workspace_id: str, extraction_id: str):
        queued.append((workspace_id, extraction_id))
        return {"id": "job_strict", "status": "QUEUED", "progress": {"message": "已进入严格归档队列"}}

    result = service.extract(
        workspace["id"], POST_URL, search_keyword="长沙相亲", archive_enqueue=enqueue,
    )

    assert result["status"] == "ready_to_archive"
    assert result["body_extraction_status"] == "PENDING"
    assert result["body"] == ""
    assert result["archive"] is None
    assert result["archive_job"]["status"] == "QUEUED"
    assert service.list(workspace["id"])[0]["url"] == POST_URL
    assert queued == [(workspace["id"], result["id"])]
    assert collector.calls == []


def test_xhs_saved_library_can_reload_up_to_five_hundred_extractions(tmp_path: Path):
    db = Database(tmp_path / "xhs-library-limit.sqlite3")
    db.initialize()
    workspace = db.create_workspace("小红书历史库")
    service = XiaohongshuExtractionService(db=db, collector=FakeDetailCollector())
    now = "2026-09-26T00:00:00+00:00"
    for index in range(205):
        post_id = f"{index:024x}"
        db.execute(
            """INSERT INTO xhs_post_extractions
            (id,workspace_id,url,title,status,extracted_at,updated_at)
            VALUES(?,?,?,?,?,?,?)""",
            (
                f"xhs_library_{index}", workspace["id"],
                f"https://www.xiaohongshu.com/explore/{post_id}",
                f"归档测试 {index}", "ready_to_archive", now, now,
            ),
        )

    saved = service.list(workspace["id"])

    assert len(saved) == 205
    assert saved[0]["title"].startswith("归档测试")


def test_xhs_extract_preserves_old_visible_text_as_unverified_history(tmp_path: Path):
    db = Database(tmp_path / "xhs-legacy-history.sqlite3")
    db.initialize()
    workspace = db.create_workspace("小红书旧版历史")
    now = "2026-09-24T00:00:00+00:00"
    db.execute(
        """INSERT INTO xhs_post_extractions
        (id,workspace_id,url,post_key,title,body,status,extracted_at,updated_at)
        VALUES('xhs_old',?,?,?,?,?,'success',?,?)""",
        (workspace["id"], POST_URL, "68c6f07d000000001d01abcd", "旧记录", "未经身份核验的旧版文本", now, now),
    )
    service = XiaohongshuExtractionService(db=db, collector=FakeDetailCollector())

    result = service.extract(
        workspace["id"], POST_URL,
        archive_enqueue=lambda *_args: {"id": "job_strict", "status": "QUEUED", "progress": {}},
    )

    assert result["id"] == "xhs_old"
    assert result["status"] == "ready_to_archive"
    assert result["body"] == ""
    assert result["merged_copy"] == ""
    assert result["body_extraction_status"] == "UNVERIFIED"
    assert result["archive"] is None
    assert service.get(workspace["id"], POST_URL)["body"] == "未经身份核验的旧版文本"


def test_radar_results_queue_strict_archives_and_skips_candidates_without_spending_budget(tmp_path: Path):
    db = Database(tmp_path / "xhs-auto.sqlite3")
    db.initialize()
    workspace = db.create_workspace("自动提取")
    service = XiaohongshuExtractionService(db=db, collector=FakeDetailCollector())
    queued: list[tuple[str, str]] = []
    added = service.enqueue_results(workspace["id"], {
            "topic": "线下相亲",
            "sources": [{
                "platform": "xiaohongshu",
                "url": POST_URL + "?xsec_token=temporary",
                "title": "第一次参加线下相亲",
                "metadata": {"platform": "xiaohongshu"},
            }],
        }, archive_enqueue=_confirm_queued(queued))
    assert added == 1
    result = service.get(workspace["id"], POST_URL)
    assert result and result["status"] == "ready_to_archive"
    assert queued == [(workspace["id"], result["id"])]


def test_batch_resume_requeues_verification_paused_xhs_detail_jobs(tmp_path: Path):
    db = Database(tmp_path / "xhs-resume-paused-batch.sqlite3")
    db.initialize()
    workspace = db.create_workspace("验证后继续小红书详情")
    extraction_service = XiaohongshuExtractionService(db=db, collector=FakeDetailCollector())
    archive_service = XiaohongshuArchiveService(
        db=db, collector=FakeDetailCollector(), root=tmp_path / "archives",
    )
    record = extraction_service.ensure_archive_record(
        workspace["id"], POST_URL, search_keyword="线下相亲", title_hint="第一次参加线下相亲",
    )
    db.execute(
        """INSERT INTO xhs_browser_pause(profile_id,reason,retry_after,requires_user_action,updated_at)
        VALUES('social_browser','verification_required',NULL,1,?)""",
        (utc_now(),),
    )
    paused = archive_service.enqueue(workspace["id"], record["id"])
    assert paused["status"] == "PAUSED"

    added = extraction_service.enqueue_results(
        workspace["id"],
        {"topic": "线下相亲", "items": [{"platform": "xiaohongshu", "url": POST_URL, "title": "第一次参加线下相亲"}]},
        archive_enqueue=archive_service.enqueue,
        resume_after_user_action=True,
    )

    jobs = archive_service.list_jobs(workspace["id"], record["id"])
    resumed_jobs = [job for job in jobs if job["revision"] == paused["revision"] + 1]
    assert added == 1
    assert len(resumed_jobs) == 1
    assert resumed_jobs[0]["status"] == "QUEUED"
    assert db.one("SELECT reason FROM xhs_browser_pause WHERE profile_id='social_browser'") is None


def test_batch_resume_retries_failed_browser_start_xhs_jobs_while_clearing_verification_pause(tmp_path: Path):
    db = Database(tmp_path / "xhs-resume-browser-failure.sqlite3")
    db.initialize()
    workspace = db.create_workspace("验证后恢复浏览器启动失败任务")
    extraction_service = XiaohongshuExtractionService(db=db, collector=FakeDetailCollector())
    archive_service = XiaohongshuArchiveService(
        db=db, collector=FakeDetailCollector(), root=tmp_path / "archives",
    )
    record = extraction_service.ensure_archive_record(
        workspace["id"], POST_URL, search_keyword="线下相亲", title_hint="第一次参加线下相亲",
    )
    db.execute(
        """INSERT INTO xhs_browser_pause(profile_id,reason,retry_after,requires_user_action,updated_at)
        VALUES('social_browser','verification_required',NULL,1,?)""",
        (utc_now(),),
    )
    failed = archive_service.enqueue(workspace["id"], record["id"])
    assert failed["status"] == "PAUSED"
    db.execute(
        "UPDATE xhs_archive_jobs SET status='FAILED',stage='FAILED',error_code='BROWSER_NOT_RUNNING' WHERE id=?",
        (failed["id"],),
    )

    added = extraction_service.enqueue_results(
        workspace["id"],
        {"topic": "线下相亲", "items": [{"platform": "xiaohongshu", "url": POST_URL, "title": "第一次参加线下相亲"}]},
        archive_enqueue=archive_service.enqueue,
        resume_after_user_action=True,
    )

    jobs = archive_service.list_jobs(workspace["id"], record["id"])
    resumed = next(job for job in jobs if job["revision"] == failed["revision"] + 1)
    assert added == 1
    assert resumed["status"] == "QUEUED"
    assert db.one("SELECT reason FROM xhs_browser_pause WHERE profile_id='social_browser'") is None


def test_radar_results_automatically_queue_every_visible_xhs_post(tmp_path: Path):
    db = Database(tmp_path / "xhs-auto-all.sqlite3")
    db.initialize()
    workspace = db.create_workspace("全部自动归档")
    service = XiaohongshuExtractionService(db=db, collector=FakeDetailCollector())
    queued: list[tuple[str, str]] = []
    sources = [
        {
            "platform": "xiaohongshu",
            "url": f"https://www.xiaohongshu.com/explore/68c6f07d000000001d01ab{i:02d}",
            "title": f"第 {i + 1} 篇原帖",
            "metadata": {"platform": "xiaohongshu"},
        }
        for i in range(8)
    ]

    added = service.enqueue_results(
        workspace["id"],
        {"topic": "线下相亲", "sources": sources},
        archive_enqueue=_confirm_queued(queued),
    )

    assert added == 8
    assert len(queued) == 8
    assert len(service.list(workspace["id"])) == 8


def test_radar_archive_intake_supports_large_batches_without_a_hidden_30_post_cap(tmp_path: Path):
    db = Database(tmp_path / "xhs-auto-large-batch.sqlite3")
    db.initialize()
    workspace = db.create_workspace("大批量自动归档")
    service = XiaohongshuExtractionService(db=db, collector=FakeDetailCollector())
    queued: list[tuple[str, str]] = []
    sources = [
        {
            "platform": "xiaohongshu",
            "url": f"https://www.xiaohongshu.com/explore/{index:024d}",
            "title": f"第 {index + 1} 篇原帖",
            "metadata": {"platform": "xiaohongshu"},
        }
        for index in range(41)
    ]

    added = service.enqueue_results(
        workspace["id"],
        {"topic": "线下相亲", "sources": sources},
        limit=500,
        archive_enqueue=_confirm_queued(queued),
    )

    assert added == 41
    assert len(queued) == 41
    assert len(service.list(workspace["id"], limit=100)) == 41


@pytest.mark.parametrize("failure_point", ["record", "enqueue"])
def test_radar_archive_intake_isolates_one_failure_and_keeps_remaining_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_point: str,
):
    db = Database(tmp_path / "isolated-intake.sqlite3")
    db.initialize()
    workspace = db.create_workspace("逐条入队隔离")
    service = XiaohongshuExtractionService(db=db, collector=FakeDetailCollector())
    good_url = "https://www.xiaohongshu.com/explore/68c6f07d000000001d01abce"
    original_ensure = service.ensure_archive_record
    called: list[str] = []

    def ensure(workspace_id, url, **kwargs):
        if failure_point == "record" and url == POST_URL:
            raise RuntimeError("cookie=never-store-me https://example.invalid/?token=private")
        return original_ensure(workspace_id, url, **kwargs)

    def enqueue(workspace_id, extraction_id):
        record = service.get_by_id(workspace_id, extraction_id)
        called.append(record["url"])
        if failure_point == "enqueue" and record["url"] == POST_URL:
            raise RuntimeError("cookie=never-store-me https://example.invalid/?token=private")
        return {"id": "good_job", "status": "QUEUED", "queued_new": True}

    monkeypatch.setattr(service, "ensure_archive_record", ensure)
    added = service.enqueue_results(
        workspace["id"],
        {"items": [
            {"platform": "xiaohongshu", "url": POST_URL, "title": "出错的条目"},
            {"platform": "xiaohongshu", "url": good_url, "title": "仍可入队的条目"},
        ]},
        archive_enqueue=enqueue,
        limit=1,
    )

    assert added == 1
    assert called[-1] == good_url
    failed = service.get(workspace["id"], POST_URL)
    assert failed["status"] == "failed"
    assert failed["failure_stage"] == "archive_enqueue_failed"
    assert "RuntimeError" in failed["error_message"]
    assert "never-store-me" not in json.dumps(failed)
    assert "private" not in json.dumps(failed)


def test_radar_archive_intake_does_not_count_paused_jobs_as_newly_queued(tmp_path: Path):
    db = Database(tmp_path / "paused-intake.sqlite3")
    db.initialize()
    workspace = db.create_workspace("暂停不冒充入队")
    service = XiaohongshuExtractionService(db=db, collector=FakeDetailCollector())
    archives = XiaohongshuArchiveService(db=db, collector=object(), root=tmp_path / "archives")
    db.execute(
        """INSERT INTO xhs_browser_pause(profile_id,reason,retry_after,requires_user_action,updated_at)
        VALUES('social_browser','需要本人验证',NULL,1,?)""",
        (utc_now(),),
    )

    added = service.enqueue_results(
        workspace["id"], {"items": [{"platform": "xiaohongshu", "url": POST_URL, "title": "暂缓读取"}]},
        archive_enqueue=archives.enqueue,
    )

    assert added == 0
    assert archives.list_jobs(workspace["id"])[0]["status"] == "PAUSED"


def test_radar_archive_intake_does_not_count_deduplicated_job_as_new(tmp_path: Path):
    db = Database(tmp_path / "deduplicated-intake.sqlite3")
    db.initialize()
    workspace = db.create_workspace("并发去重计数")
    service = XiaohongshuExtractionService(db=db, collector=FakeDetailCollector())
    added = service.enqueue_results(
        workspace["id"], {"items": [{"platform": "xiaohongshu", "url": POST_URL, "title": "已由其他入口入队"}]},
        archive_enqueue=lambda *_args: {"id": "existing_job", "status": "RUNNING", "queued_new": False},
    )
    assert added == 0


def test_xhs_extraction_http_api_uses_original_workbench_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = Database(tmp_path / "xhs-api.sqlite3")
    db.initialize()
    workspace = db.create_workspace("原工作台")
    service = XiaohongshuExtractionService(db=db, collector=FakeDetailCollector())

    class Worker:
        def start(self): pass
        def stop(self): pass
        def wake(self): pass

    monkeypatch.setenv("WORKBENCH_DISABLE_WORKERS", "1")
    monkeypatch.setattr(main_module, "database", db)
    monkeypatch.setattr(main_module, "xhs_extraction_service", service)
    monkeypatch.setattr(
        main_module.xhs_archive_service,
        "enqueue",
        lambda _workspace_id, _extraction_id: {
            "id": "job_http", "status": "QUEUED", "progress": {"message": "queued"},
        },
    )
    monkeypatch.setattr(main_module, "topic_radar_worker", Worker())

    with TestClient(main_module.app) as client:
        response = client.post("/api/topic-radar/xhs/extract", json={
            "workspace_id": workspace["id"],
            "url": POST_URL,
            "search_keyword": "线下相亲",
            "title_hint": "搜索页标题",
        })
        assert response.status_code == 200, response.text
        assert response.json()["extraction"]["status"] == "ready_to_archive"
        assert response.json()["extraction"]["body_extraction_status"] == "PENDING"
        assert response.json()["extraction"]["archive_job"]["status"] == "QUEUED"

        listed = client.get("/api/topic-radar/xhs/extractions", params={"workspace_id": workspace["id"]})
        assert listed.status_code == 200
    assert listed.json()[0]["url"] == POST_URL


def test_xhs_search_recovery_prefers_same_post_and_keeps_access_token():
    rows = [
        {
            "href": "https://www.xiaohongshu.com/explore/other-note?xsec_token=wrong&tracking=drop",
            "title": "相似但不是同一篇",
        },
        {
            "href": "https://www.xiaohongshu.com/explore/68c6f07d000000001d01abcd?xsec_token=right-token&xsec_source=pc_search&tracking=drop",
            "title": "第一次参加线下相亲",
        },
    ]

    resolved = _best_xhs_search_result(rows, "68c6f07d000000001d01abcd", "第一次参加线下相亲")

    assert "xsec_token=right-token" in resolved
    assert "xsec_source=pc_search" in resolved
    assert "tracking" not in resolved


def test_xhs_search_recovery_never_replaces_post_with_similar_title():
    rows = [{
        "href": "https://www.xiaohongshu.com/explore/new-note?xsec_token=new-token",
        "title": "长沙相亲｜近期能面基的来",
    }]

    resolved = _best_xhs_search_result(rows, "old-note", "长沙相亲｜近期能面基的来")

    assert resolved == ""


def test_xhs_visible_search_card_becomes_tokenized_original_post_url():
    resolved = _safe_url(
        "xiaohongshu",
        "https://www.xiaohongshu.com/search_result/68c6f07d000000001d01abcd"
        "?xsec_token=access-token&xsec_source=&tracking=drop",
    )

    assert resolved == (
        "https://www.xiaohongshu.com/explore/68c6f07d000000001d01abcd"
        "?xsec_token=access-token&xsec_source=pc_search"
    )


def test_xhs_search_access_url_stays_in_memory_while_public_url_is_clean(tmp_path: Path):
    collector = PlatformBrowserCollector(profile_dir=tmp_path / "profile")
    public_url = collector._remember_xhs_access_url(
        "https://www.xiaohongshu.com/search_result/68c6f07d000000001d01abcd"
        "?xsec_token=access-token&xsec_source=pc_search&tracking=drop"
    )

    assert public_url == POST_URL
    assert collector._recent_xhs_access_url(public_url) == (
        POST_URL + "?xsec_token=access-token&xsec_source=pc_search"
    )


def test_xhs_access_state_prioritizes_login_and_risk_before_missing_post():
    assert _page_access_state("当前笔记暂时无法浏览，请完成安全验证") == "verification_required"
    assert _page_access_state("内容不存在，请扫码登录后查看") == "login_required"
    assert _page_access_state("当前笔记暂时无法浏览，IP存在风险") == "platform_limited"
    assert _page_access_state("当前笔记暂时无法浏览") == "post_unavailable"


def test_page_access_state_treats_douyin_code_login_as_login_not_captcha():
    login_page = "抖音登录 手机号登录 验证码登录 发送验证码"

    assert _page_access_state(login_page) == "login_required"
    # Login pages can also surface a generic CAPTCHA integration label.  It
    # must not turn the required QR/phone login action into a false captcha.
    assert _page_access_state(f"{login_page} captcha") == "login_required"


def test_page_access_state_keeps_explicit_security_challenge_above_login_ui():
    assert _page_access_state("扫码登录后，请完成安全验证") == "verification_required"
    assert _page_access_state("验证码登录，请拖动滑块完成人机验证") == "verification_required"


def test_xhs_detail_hydration_waits_for_the_requested_post_body_before_final_parse():
    target_post_id = POST_URL.rsplit("/", 1)[-1]

    class SequencedProbePage:
        def __init__(self):
            self.values = [
                {"structuredBodyReady": False, "renderedBodyLength": 0},
                {"structuredBodyReady": False, "renderedBodyLength": 0},
                {"structuredBodyReady": True, "renderedBodyLength": 88},
            ]
            self.calls = 0

        def evaluate(self, _expression, received_post_id):
            assert received_post_id == target_post_id
            value = self.values[min(self.calls, len(self.values) - 1)]
            self.calls += 1
            return value

        def wait_for_timeout(self, _milliseconds):
            pass

    page = SequencedProbePage()

    result = _wait_for_xhs_detail_hydration(page, target_post_id, timeout_seconds=1)

    assert result["reason"] == "structured_body_ready"
    assert result["structured_body_ready"] is True
    assert result["attempts"] == 3
    assert page.calls == 3


def test_xhs_detail_hydration_stops_without_waiting_when_platform_requires_user_action():
    class VerificationProbePage:
        calls = 0

        def evaluate(self, _expression, _received_post_id):
            self.calls += 1
            return {
                "structuredBodyReady": False,
                "renderedBodyLength": 0,
                "pageTitle": "小红书安全验证",
                "visibleText": "请完成安全验证",
            }

        def wait_for_timeout(self, _milliseconds):
            raise AssertionError("安全验证状态不应继续等待详情 hydration")

    page = VerificationProbePage()

    result = _wait_for_xhs_detail_hydration(page, "68c6f07d000000001d01abcd")

    assert result["reason"] == "platform_gate_or_unavailable"
    assert result["access_state"] == "verification_required"
    assert result["attempts"] == 1
    assert page.calls == 1


def test_browser_status_names_the_platform_that_needs_verification(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    class Response:
        is_success = True

        def json(self):
            return [{"type": "page"}]

    collector = PlatformBrowserCollector(profile_dir=tmp_path / "profile")
    monkeypatch.setattr(platform_browser_module.httpx, "get", lambda *_args, **_kwargs: Response())
    collector._set_session_state("xiaohongshu", "READY", "本轮已完成")
    collector._set_session_state("douyin", "VERIFICATION_REQUIRED", "请完成安全验证")

    status = collector.status()

    assert status["session_state"] == "VERIFICATION_REQUIRED"
    assert "抖音需要完成安全验证" in status["message"]
    assert "小红书需要完成安全验证" not in status["message"]
    assert "打开登录页" in status["message"]


def test_automatic_target_is_hidden_and_never_activated(monkeypatch: pytest.MonkeyPatch):
    class Socket:
        def close(self):
            pass

    class Target(_HiddenCDPTarget):
        def __init__(self):
            super().__init__("ws://example.test/devtools/browser/test")
            self.calls: list[tuple[str, dict]] = []

        def _root_call(self, method, params=None):
            params = params or {}
            self.calls.append((method, params))
            if method == "Target.createTarget":
                return {"result": {"targetId": "background-target"}}
            if method == "Target.attachToTarget":
                return {"result": {"sessionId": "session-1"}}
            return {"result": {}}

        def call(self, method, params=None):
            self.calls.append((method, params or {}))
            return {"result": {}}

    import websockets.sync.client

    monkeypatch.setattr(websockets.sync.client, "connect", lambda *args, **kwargs: Socket())
    target = Target()
    with target:
        assert target.creation_mode == "hidden"
        assert (
            "Target.createTarget",
            {"url": "about:blank", "background": True, "hidden": True},
        ) in target.calls
        assert ("Page.setWebLifecycleState", {"state": "active"}) in target.calls

    assert any(method == "Target.closeTarget" for method, _ in target.calls)
    assert not any(method == "Target.activateTarget" for method, _ in target.calls)


def test_targetless_browser_is_not_reported_ready_and_recovers_when_a_page_opens(tmp_path, monkeypatch):
    pages = []

    class Response:
        is_success = True

        def json(self):
            return pages

    collector = PlatformBrowserCollector(profile_dir=tmp_path / "profile")
    monkeypatch.setattr(platform_browser_module.httpx, "get", lambda *args, **kwargs: Response())
    collector._set_session_state("xiaohongshu", "READY")
    collector._set_session_state("douyin", "READY")
    status = collector.status()
    assert status["running"] is True
    assert status["collector_state"] == "BROWSER_PAGE_REQUIRED"
    assert status["page_count"] == 0
    assert status["session_state"] == "UNKNOWN"
    assert "没有打开页面" in status["message"]
    pages.append({"type": "page"})
    assert collector.status()["collector_state"] == "READY"
    assert collector.status()["session_state"] == "UNKNOWN"


def test_chrome_missing_frame_error_requires_page_recovery_without_visible_fallback(monkeypatch):
    class Socket:
        def close(self):
            pass

    target = _HiddenCDPTarget("ws://example.test")
    calls = []

    def root_call(method, params=None):
        calls.append((method, params))
        raise RuntimeError("Hidden target can be created only when remote debugging is enabled")

    import websockets.sync.client
    monkeypatch.setattr(websockets.sync.client, "connect", lambda *args, **kwargs: Socket())
    monkeypatch.setattr(target, "_root_call", root_call)
    with pytest.raises(PlatformBrowserError) as error:
        target.__enter__()
    assert error.value.code == "browser_page_required"
    assert "打开双平台浏览器" in str(error.value)
    assert len(calls) == 1
    assert calls[0][1]["hidden"] is True


def test_automatic_target_fails_instead_of_falling_back_to_visible_background_tab(monkeypatch: pytest.MonkeyPatch):
    class Socket:
        def close(self):
            pass

    class Target(_HiddenCDPTarget):
        def __init__(self):
            super().__init__("ws://example.test/devtools/browser/test")
            self.calls: list[tuple[str, dict]] = []

        def _root_call(self, method, params=None):
            params = params or {}
            self.calls.append((method, params))
            if method == "Target.createTarget":
                raise RuntimeError("hidden target unavailable")
            if method == "Target.attachToTarget":
                return {"result": {"sessionId": "session-1"}}
            return {"result": {}}

        def call(self, method, params=None):
            self.calls.append((method, params or {}))
            return {"result": {}}

    import websockets.sync.client

    monkeypatch.setattr(websockets.sync.client, "connect", lambda *args, **kwargs: Socket())
    target = Target()
    with pytest.raises(PlatformBrowserError, match="后台采集浏览器连接失败"):
        with target:
            pass
    assert (
        "Target.createTarget",
        {"url": "about:blank", "background": True, "hidden": True},
    ) in target.calls
    # There must never be a second Target.createTarget without hidden=True:
    # that "fallback" is a regular Chrome tab while a slow archive runs.
    assert all(
        method != "Target.createTarget" or params.get("hidden") is True
        for method, params in target.calls
    )
    assert not any(method == "Target.activateTarget" for method, _ in target.calls)


def test_background_target_keeps_child_result_that_arrives_before_root_ack():
    class Socket:
        def __init__(self) -> None:
            self.messages: list[str] = []

        def send(self, raw: str) -> None:
            import json

            message = json.loads(raw)
            if message.get("method") != "Target.sendMessageToTarget":
                return
            root_id = message["id"]
            child = json.loads(message["params"]["message"])
            session_id = message["params"]["sessionId"]
            # The child response is intentionally enqueued before the root
            # acknowledgement, reproducing the CDP ordering race.
            self.messages.extend([
                json.dumps({"method": "Target.receivedMessageFromTarget", "params": {
                    "sessionId": session_id,
                    "message": json.dumps({"id": child["id"], "result": {"result": {"value": "ok"}}}),
                }}),
                json.dumps({"id": root_id, "result": {}}),
            ])

        def recv(self, timeout: float):  # noqa: ARG002
            if not self.messages:
                raise TimeoutError("no test message")
            return self.messages.pop(0)

        def close(self):
            pass

    target = _HiddenCDPTarget("ws://example.test")
    target.socket = Socket()
    target.session_id = "session-1"

    result = target.call("Runtime.evaluate", {"expression": "'ok'"})

    assert result["result"]["result"]["value"] == "ok"


def test_platform_search_uses_background_snapshot_and_requires_explicit_open_for_verification(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    collector = PlatformBrowserCollector(profile_dir=tmp_path / "profile")
    monkeypatch.setattr(collector, "status", lambda: {"running": True})
    action_calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        collector,
        "_activate_or_open_user_action_page",
        lambda platform, query="", action_url="": action_calls.append((platform, query, action_url)),
    )
    monkeypatch.setattr(
        collector,
        "_hidden_search_snapshot",
        lambda platform, query, limit: {
            "pageTitle": "小红书搜索",
            "bodyText": "正常搜索内容",
            "rows": [{
                "href": POST_URL,
                "title": "长沙相亲真实体验",
                "text": "长沙相亲真实体验\n作者\n1.2万",
                "author": "作者",
                "metricText": "1.2万",
            }],
        },
    )

    rows = collector.search("xiaohongshu", "长沙相亲", 8)

    assert len(rows) == 1
    assert rows[0]["raw"]["collection"] == "hidden_visible_search_card"
    assert collector.status().get("running") is True
    assert collector._session_state("xiaohongshu") == "READY"
    assert action_calls == []

    # A card may quote another author's “内容不存在/已删除” text.  That is
    # evidence about the card, not a reason to label the whole search session
    # as an unavailable post.
    monkeypatch.setattr(
        collector,
        "_hidden_search_snapshot",
        lambda platform, query, limit: {
            "pageTitle": "小红书搜索",
            "bodyText": "有人说之前的内容不存在，但这里仍有正常结果",
            "rows": [{
                "href": POST_URL,
                "title": "长沙相亲真实体验",
                "text": "有人说内容不存在，但这篇仍可见",
                "author": "作者",
                "metricText": "1.2万",
            }],
        },
    )
    assert collector.search("xiaohongshu", "长沙相亲", 8)
    assert collector._session_state("xiaohongshu") == "READY"

    def verification(*_args, **_kwargs):
        raise PlatformBrowserError("需要安全验证", code="verification_required")

    monkeypatch.setattr(collector, "_hidden_search_snapshot", verification)
    with pytest.raises(PlatformBrowserError, match="安全验证"):
        collector.search("xiaohongshu", "长沙相亲", 8)
    assert collector._session_state("xiaohongshu") == "VERIFICATION_REQUIRED"
    assert action_calls == []


def test_background_douyin_search_waits_for_late_result_cards(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The shell can arrive before Douyin hydrates its result-card links."""
    calls: list[str] = []

    class Target:
        def __init__(self, _websocket_url: str) -> None:
            self.evaluations = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def navigate(self, url: str) -> None:
            calls.append(url)

        def evaluate(self, _expression: str):
            self.evaluations += 1
            if self.evaluations == 1:
                return {
                    "pageTitle": "发现更多精彩视频 - 抖音搜索",
                    "bodyText": "搜索\n综合\n视频\n筛选",
                    "rows": [],
                    "resultCardCount": 0,
                    "accessHints": "",
                    "url": "https://www.douyin.com/search/%E9%95%BF%E6%B2%99%E7%9B%B8%E4%BA%B2?type=video",
                }
            return {
                "pageTitle": "发现更多精彩视频 - 抖音搜索",
                "bodyText": "长沙相亲\n02:44\n4.4万\n长沙相亲脱单经验",
                "rows": [{
                    "href": "https://www.douyin.com/video/7464222635606543676",
                    "title": "长沙相亲脱单经验",
                    "text": "02:44\n4.4万\n长沙相亲脱单经验\n@作者",
                    "author": "@作者",
                    "metricText": "4.4万",
                }],
                "resultCardCount": 1,
                "accessHints": "",
                "url": "https://www.douyin.com/search/%E9%95%BF%E6%B2%99%E7%9B%B8%E4%BA%B2?type=video",
            }

    collector = PlatformBrowserCollector(profile_dir=tmp_path / "profile")
    monkeypatch.setattr(collector, "_browser_websocket_url", lambda: "ws://example.test")
    monkeypatch.setattr(platform_browser_module, "_HiddenCDPTarget", Target)
    monkeypatch.setattr(platform_browser_module, "_BACKGROUND_SEARCH_READY_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(platform_browser_module, "_BACKGROUND_SEARCH_POLL_SECONDS", 0.001)

    snapshot = collector._hidden_search_snapshot("douyin", "长沙相亲", 8)

    assert calls == ["https://www.douyin.com/search/%E9%95%BF%E6%B2%99%E7%9B%B8%E4%BA%B2?type=video"]
    assert snapshot["rows"][0]["href"].endswith("/7464222635606543676")
    expression = platform_browser_module._search_card_expression("douyin", 8)
    assert ".search-result-card" in expression
    assert 'a[href*="/video/"]' in expression


def test_background_search_reports_visible_challenge_instead_of_empty_results(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    collector = PlatformBrowserCollector(profile_dir=tmp_path / "profile")
    monkeypatch.setattr(collector, "status", lambda: {"running": True})
    action_calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        collector,
        "_activate_or_open_user_action_page",
        lambda platform, query="", action_url="": action_calls.append((platform, query, action_url)),
    )
    monkeypatch.setattr(
        collector,
        "_hidden_search_snapshot",
        lambda *_args, **_kwargs: {
            "pageTitle": "发现更多精彩视频 - 抖音搜索",
            "bodyText": "搜索\n综合\n视频",
            "accessHints": "安全验证",
            "rows": [],
        },
    )

    with pytest.raises(PlatformBrowserError) as error:
        collector.search("douyin", "长沙相亲", 8)

    assert error.value.code == "verification_required"
    assert action_calls == []


def test_xhs_detail_and_archive_return_user_action_state_without_opening_a_tab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    created: list[object] = []

    class BackgroundPage:
        def __init__(self, _websocket_url: str) -> None:
            self.context = object()  # no visible Playwright page/context API
            self.closed = False
            created.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

        def goto(self, *_args, **_kwargs):
            pass

        def wait_for_timeout(self, *_args, **_kwargs):
            pass

        def on(self, *_args, **_kwargs):
            pass

        def evaluate(self, *_args, **_kwargs):
            return {"title": "安全验证", "text": "请完成安全验证", "url": POST_URL}

        def close(self):
            self.closed = True

    collector = PlatformBrowserCollector(profile_dir=tmp_path / "profile")
    monkeypatch.setattr(collector, "status", lambda: {"running": True})
    monkeypatch.setattr(collector, "_browser_websocket_url", lambda: "ws://example.test")
    monkeypatch.setattr(platform_browser_module, "_BackgroundPage", BackgroundPage)
    actions: list[str] = []
    monkeypatch.setattr(
        collector,
        "_activate_or_open_user_action_page",
        lambda platform, query="", action_url="": actions.append(action_url),
    )

    with pytest.raises(PlatformBrowserError) as detail_error:
        collector.extract_xiaohongshu_detail(POST_URL)
    assert detail_error.value.code == "verification_required"

    with pytest.raises(PlatformBrowserError) as archive_error:
        collector.archive_xiaohongshu_detail(POST_URL)
    assert archive_error.value.code == "verification_required"

    assert len(created) == 2
    assert all(getattr(page, "closed") for page in created)
    assert actions == []


def test_explicit_browser_open_is_the_only_path_that_creates_user_action_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    collector = PlatformBrowserCollector(profile_dir=tmp_path / "profile")
    starts: list[str] = []
    actions: list[tuple[str, str, str]] = []
    monkeypatch.setattr(collector, "_start", lambda initial_url: starts.append(initial_url))
    monkeypatch.setattr(
        collector,
        "_activate_or_open_user_action_page",
        lambda platform, query="", action_url="": actions.append((platform, query, action_url)),
    )
    monkeypatch.setattr(collector, "status", lambda: {"running": True, "mode": "managed_background_collector"})

    result = collector.open("both", "长沙相亲")

    assert result["running"] is True
    assert starts == ["about:blank"]
    assert actions == [
        ("xiaohongshu", "长沙相亲", ""),
        ("douyin", "长沙相亲", ""),
    ]


def test_explicit_browser_open_reuses_exact_page_after_workbench_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    target_url = _search_url("douyin", "长沙相亲")
    targets = [
        {"id": "unrelated", "type": "page", "url": "https://www.douyin.com/"},
        {"id": "iframe-copy", "type": "iframe", "url": target_url},
        {"id": "existing-search", "type": "page", "url": target_url},
        {"id": "same-url-other-page", "type": "page", "url": target_url},
    ]

    class TargetListResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return targets

    class FakeSocket:
        def __init__(self):
            self.sent: list[str] = []
            self.pending: list[str] = []

        def send(self, message: str) -> None:
            self.sent.append(message)
            request_id = json.loads(message)["id"]
            self.pending.append(json.dumps({"id": request_id, "result": {}}))

        def recv(self, timeout=None) -> str:
            return self.pending.pop(0)

        def close(self) -> None:
            return None

    socket = FakeSocket()
    collector = PlatformBrowserCollector(profile_dir=tmp_path / "profile")
    monkeypatch.setattr(collector, "_browser_websocket_url", lambda: "ws://127.0.0.1/devtools/browser/test")
    monkeypatch.setattr(platform_browser_module.httpx, "get", lambda *_args, **_kwargs: TargetListResponse())
    monkeypatch.setattr("websockets.sync.client.connect", lambda *_args, **_kwargs: socket)

    collector._activate_or_open_user_action_page("douyin", "长沙相亲")

    methods = [json.loads(message)["method"] for message in socket.sent]
    assert methods == ["Target.activateTarget"]
    assert json.loads(socket.sent[0])["params"] == {"targetId": "existing-search"}
    assert collector._user_action_targets["douyin"] == "existing-search"


def test_automatic_archive_reads_a_normal_detail_without_opening_a_user_action_tab(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """The result-ready archive path must not expose a post detail as a tab.

    A normal post is the regression that mattered here: before this guard,
    Chrome created an unfocused but still visible ``/explore/<post-id>`` tab
    while the automatic archive worker was reading it.  The collector may
    show a page only for a verified login/CAPTCHA intervention, never for a
    successful archive.
    """
    created: list[object] = []
    post_id = POST_URL.rsplit("/", 1)[-1]

    class EmptyLocators:
        def all(self):
            return []

    class BackgroundPage:
        def __init__(self, _websocket_url: str) -> None:
            self.context = object()
            self.closed = False
            self.evaluations = 0
            created.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

        def goto(self, *_args, **_kwargs):
            pass

        def wait_for_timeout(self, *_args, **_kwargs):
            pass

        def on(self, *_args, **_kwargs):
            pass

        def get_by_text(self, *_args, **_kwargs):
            return EmptyLocators()

        def evaluate(self, *_args, **_kwargs):
            self.evaluations += 1
            if self.evaluations == 1:
                return {"title": "长沙相亲贴", "text": "正常原帖正文", "url": POST_URL}
            return {
                "bodyRaw": "第一行\n\n#长沙相亲\n保留原文格式",
                "bodySource": "embedded_state",
                "bodyVerified": True,
                "bodyRich": [],
                "richAlignment": "EXACT",
                "stateCandidateCount": 1,
                "selectedStateScore": 95,
                "selectedBodyField": "desc",
                "images": [],
                "expectedImageCount": 0,
                "orderedBy": "page_state",
                "coverUrl": "",
                "observedId": post_id,
                "statePostId": post_id,
                "detailUrlPostId": post_id,
                "title": "长沙相亲贴",
                "author": "作者",
                "publishedAt": "2026-09-19",
                "canonical": POST_URL,
                "videoCount": 0,
                "pageTitle": "长沙相亲贴 - 小红书",
                "finalUrl": POST_URL,
                "visibleText": "正常原帖正文",
            }

        def close(self):
            self.closed = True

    collector = PlatformBrowserCollector(profile_dir=tmp_path / "profile")
    monkeypatch.setattr(collector, "status", lambda: {"running": True})
    monkeypatch.setattr(collector, "_browser_websocket_url", lambda: "ws://example.test")
    monkeypatch.setattr(platform_browser_module, "_BackgroundPage", BackgroundPage)
    actions: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        collector,
        "_activate_or_open_user_action_page",
        lambda platform, query="", action_url="": actions.append((platform, query, action_url)),
    )

    result = collector.archive_xiaohongshu_detail(POST_URL)

    assert result["observed_post_id"] == post_id
    assert result["body_raw"] == "第一行\n\n#长沙相亲\n保留原文格式"
    assert len(created) == 1
    assert created[0].closed is True
    assert actions == []
