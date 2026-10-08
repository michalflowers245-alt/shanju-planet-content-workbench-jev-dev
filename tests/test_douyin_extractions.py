from __future__ import annotations

from pathlib import Path
import queue
import threading

import pytest

from backend.app.db import Database
from backend.app.douyin_extractions import DouyinExtractionService
from backend.app.platform_browser import PlatformBrowserError


URL = "https://www.douyin.com/video/7450123456789012345"
NOTE_URL = "https://www.douyin.com/note/7450123456789012345"


class DetailCollector:
    def extract_douyin_detail(self, url: str, *, search_keyword: str = "", title_hint: str = ""):
        assert url == URL
        return {
            "url": url,
            "video_id": "7450123456789012345",
            "observed_video_id": "7450123456789012345",
            "title": "真实视频标题",
            "author": "真实作者",
            "published_body": "这是一段平台发布文案\n#相亲",
            "published_body_source": "页面结构化发布文案",
            "platform_subtitle": "这是页面字幕，不是发布文案。",
            "subtitle_source": "页面可见字幕",
            "metrics": {"likes": 1200},
            "asr_status": "NOT_ATTEMPTED",
            "ocr_status": "NOT_ATTEMPTED",
            "video_status": "MEDIA_UNAVAILABLE",
            "status": "PARTIAL",
            "failure_stage": "media_not_archived",
            "error_message": "未下载或归档完整视频；平台发布文案与可见字幕已分别保存。",
            "diagnostics": {"media_policy": "video_not_downloaded"},
        }


class MismatchCollector:
    def extract_douyin_detail(self, *_args, **_kwargs):
        raise PlatformBrowserError(
            "详情页视频 ID 与搜索结果不一致，已拒绝使用其他视频内容",
            code="identity_mismatch",
            diagnostics={"target_video_id": "7450123456789012345", "observed_video_id": "7450999999999999999"},
        )


class StructuredCaptionCollector(DetailCollector):
    def extract_douyin_detail(self, url: str, *, search_keyword: str = "", title_hint: str = ""):
        result = super().extract_douyin_detail(url, search_keyword=search_keyword, title_hint=title_hint)
        result["published_body_source"] = "详情页结构化发布文案"
        result["status"] = "COMPLETE"
        result["failure_stage"] = ""
        result["error_message"] = "已从匹配视频详情读取结构化发布文案。"
        result["diagnostics"] = {"copy_completeness": "verified_structured_caption"}
        return result


def _visibility_restriction_diagnostics(**outcome_changes):
    return {
        "target_video_id": "7450123456789012345", "network_detail_payload_match_count": 0,
        "network_detail_outcomes": [{
            "http_status": 200, "platform_status_code": 0, "reason": "platform_visibility_restricted",
            "filter_reason": "status_audit_self_see", "filter_detail_video_id": "7450123456789012345",
            **outcome_changes,
        }],
    }


class RestrictedCollector:
    def __init__(self, diagnostics=None):
        self.diagnostics = diagnostics if diagnostics is not None else _visibility_restriction_diagnostics()
        self.calls = 0

    def extract_douyin_detail(self, *_args, **_kwargs):
        self.calls += 1
        raise PlatformBrowserError(
            "平台限制这条内容的可见性，未返回正文", code="post_unavailable", diagnostics=self.diagnostics,
        )


@pytest.mark.parametrize("retained_caption", [False, True])
def test_repeated_radar_search_does_not_requeue_confirmed_restriction(tmp_path, retained_caption):
    db = Database(tmp_path / "restricted-auto-intake.sqlite3")
    db.initialize()
    workspace = db.create_workspace("限制记录不重复搜索入队")
    collector = RestrictedCollector()
    service = DouyinExtractionService(db=db, collector=StructuredCaptionCollector() if retained_caption else collector)
    if retained_caption:
        service.extract(workspace["id"], URL)
        service.collector = collector
    original = service.extract(workspace["id"], URL)
    payload = {"items": [{"platform": "douyin", "url": NOTE_URL, "title": "重复搜索结果"}]}

    for _ in range(5):
        assert service.enqueue_results(workspace["id"], payload) == 0

    assert collector.calls == 1
    assert service._jobs.empty() and not service._queued
    assert service.get(workspace["id"], URL) == original
    if retained_caption:
        assert original["published_body"]
        assert original["diagnostics"]["last_attempt_status"] == "POST_UNAVAILABLE"


def test_explicit_retry_of_confirmed_restriction_still_runs_selected_item(tmp_path):
    db = Database(tmp_path / "restricted-explicit-retry.sqlite3")
    db.initialize()
    workspace = db.create_workspace("限制记录允许用户显式重试")
    collector = RestrictedCollector()
    service = DouyinExtractionService(db=db, collector=collector)
    service.extract(workspace["id"], URL)
    retried = threading.Event()

    class NowVisibleCollector(StructuredCaptionCollector):
        def extract_douyin_detail(self, url, *, search_keyword="", title_hint=""):
            result = super().extract_douyin_detail(url, search_keyword=search_keyword, title_hint=title_hint)
            retried.set()
            return result

    service.collector = NowVisibleCollector()
    service.start()
    try:
        assert service.enqueue(workspace["id"], URL, force_retry=True)
        assert retried.wait(timeout=2)
        service._jobs.join()
        assert service.get(workspace["id"], URL)["status"] == "COMPLETE"
    finally:
        service.stop()


@pytest.mark.parametrize("outcome_changes", [
    {"filter_reason": "status_unknown_future"},
    {"filter_reason": "status_audit_self_see "},
    {"filter_detail_video_id": "7450999999999999999"},
    {"reason": "missing_detail_object"},
    {"http_status": 403},
    {"platform_status_code": False},
    {"platform_status_code": 1},
])
def test_unconfirmed_or_other_id_reason_does_not_block_ordinary_intake(tmp_path, outcome_changes):
    db = Database(tmp_path / "unconfirmed-intake.sqlite3")
    db.initialize()
    workspace = db.create_workspace("未知限制不推测封锁")
    service = DouyinExtractionService(
        db=db, collector=RestrictedCollector(_visibility_restriction_diagnostics(**outcome_changes)),
    )
    service.extract(workspace["id"], URL)
    assert service.enqueue(workspace["id"], URL)


def test_old_restriction_does_not_override_latest_unconfirmed_failure(tmp_path):
    db = Database(tmp_path / "latest-restriction-evidence.sqlite3")
    db.initialize()
    workspace = db.create_workspace("只判断最新限制证据")
    old = _visibility_restriction_diagnostics()
    latest = {"error_code": "post_unavailable", **_visibility_restriction_diagnostics(filter_reason="status_unknown_future")}
    diagnostic = {**old, "last_attempt_status": "POST_UNAVAILABLE", "last_attempt_diagnostics": latest}
    service = DouyinExtractionService(db=db, collector=RestrictedCollector(diagnostic))
    service.extract(workspace["id"], URL)
    assert service.enqueue(workspace["id"], URL)


def test_douyin_detail_keeps_published_caption_and_subtitle_separate(tmp_path: Path):
    db = Database(tmp_path / "douyin.sqlite3")
    db.initialize()
    workspace = db.create_workspace("抖音证据")
    service = DouyinExtractionService(db=db, collector=DetailCollector())

    value = service.extract(workspace["id"], URL, search_keyword="相亲故事")

    assert value["status"] == "PARTIAL"
    assert value["video_id"] == value["observed_video_id"]
    assert value["published_body"] == "这是一段平台发布文案\n#相亲"
    assert value["platform_subtitle"] == "这是页面字幕，不是发布文案。"
    assert value["published_body"] != value["platform_subtitle"]
    assert value["asr_text"] == ""
    assert value["asr_status"] == "NOT_ATTEMPTED"
    assert value["video_status"] == "MEDIA_UNAVAILABLE"


def test_first_queued_detail_attempt_is_not_counted_as_a_retry(tmp_path: Path):
    db = Database(tmp_path / "douyin-attempt-history.sqlite3")
    db.initialize()
    workspace = db.create_workspace("抖音尝试记录")
    service = DouyinExtractionService(db=db, collector=StructuredCaptionCollector())

    assert service.enqueue(workspace["id"], URL, search_keyword="相亲", title_hint="候选标题") is True
    queued = service.get(workspace["id"], URL)
    assert queued is not None
    assert queued["retry_count"] == 0
    assert queued["last_attempt_at"] == ""

    first = service.extract(workspace["id"], URL, search_keyword="相亲", title_hint="候选标题")
    assert first["retry_count"] == 0
    assert first["last_attempt_at"]

    old_attempt = "2000-01-01T00:00:00+00:00"
    db.execute(
        "UPDATE douyin_post_extractions SET last_attempt_at=? WHERE workspace_id=? AND url=?",
        (old_attempt, workspace["id"], URL),
    )
    retried = service.extract(workspace["id"], URL, search_keyword="相亲", title_hint="候选标题")
    assert retried["retry_count"] == 1
    assert retried["last_attempt_at"] != old_attempt


def test_enqueue_commits_row_before_fast_worker_can_consume_job(tmp_path: Path):
    db = Database(tmp_path / "douyin-fast-worker.sqlite3")
    db.initialize()
    workspace = db.create_workspace("快速提取不被入队覆盖")
    service = DouyinExtractionService(db=db, collector=StructuredCaptionCollector())
    seen_states = []

    class ImmediateWorkerQueue(queue.Queue):
        def put(self, job, *args, **kwargs):
            super().put(job, *args, **kwargs)
            # Execute at the first instant that the worker can observe a job.
            row = service.get(workspace["id"], URL)
            seen_states.append(row["status"] if row else None)
            service.extract(workspace["id"], URL)

    service._jobs = ImmediateWorkerQueue()
    assert service.enqueue(workspace["id"], URL)

    assert seen_states == ["QUEUED"]
    saved = service.get(workspace["id"], URL)
    assert saved["status"] == "COMPLETE"
    assert saved["published_body"] == "这是一段平台发布文案\n#相亲"


def test_enqueue_persistence_failure_does_not_publish_or_reserve_job(tmp_path: Path, monkeypatch):
    db = Database(tmp_path / "douyin-enqueue-persistence-failure.sqlite3")
    db.initialize()
    workspace = db.create_workspace("入队失败可重试")
    service = DouyinExtractionService(db=db, collector=StructuredCaptionCollector())
    execute = db.execute

    def fail_insert(sql, params=()):
        if "INSERT INTO douyin_post_extractions" in sql:
            raise RuntimeError("test persistence failure")
        return execute(sql, params)

    monkeypatch.setattr(db, "execute", fail_insert)
    with pytest.raises(RuntimeError, match="test persistence failure"):
        service.enqueue(workspace["id"], URL)

    assert service._jobs.empty()
    assert (workspace["id"], URL) not in service._queued
    monkeypatch.setattr(db, "execute", execute)
    assert service.enqueue(workspace["id"], URL)


def test_duplicate_enqueue_does_not_reset_inflight_record(tmp_path: Path):
    db = Database(tmp_path / "douyin-inflight-deduplication.sqlite3")
    db.initialize()
    workspace = db.create_workspace("进行中的任务不重复入队")
    service = DouyinExtractionService(db=db, collector=StructuredCaptionCollector())
    assert service.enqueue(workspace["id"], URL, title_hint="原始标题")
    db.execute(
        "UPDATE douyin_post_extractions SET status='TRANSCRIBING',asr_status='PROCESSING' WHERE workspace_id=? AND url=?",
        (workspace["id"], URL),
    )

    assert not service.enqueue(workspace["id"], URL, title_hint="重复标题", force_retry=True)

    saved = service.get(workspace["id"], URL)
    assert saved["status"] == "TRANSCRIBING"
    assert saved["asr_status"] == "PROCESSING"
    assert saved["title"] == "原始标题"
    assert service._jobs.qsize() == 1


def test_explicit_single_retry_wakes_paused_worker_without_resuming_other_rows(tmp_path: Path):
    db = Database(tmp_path / "douyin-explicit-retry-paused-worker.sqlite3")
    db.initialize()
    workspace = db.create_workspace("仅恢复用户选中的视频")
    processed = []
    completed = threading.Event()
    waiting_on_gate = threading.Event()
    service = DouyinExtractionService(db=db, collector=StructuredCaptionCollector())

    class ObservedResumeGate(threading.Event):
        def wait(self, timeout=None):
            if not self.is_set():
                waiting_on_gate.set()
            return super().wait(timeout)

    service._resume_gate = ObservedResumeGate()
    other_url = "https://www.douyin.com/video/7450123456789012346"
    service.enqueue(workspace["id"], URL)
    service.enqueue(workspace["id"], other_url)
    service._pause_remaining_jobs("NEEDS_BROWSER", "没有可用页面")
    extract = service.extract

    def record_extract(workspace_id, url, **kwargs):
        result = extract(workspace_id, url, **kwargs)
        processed.append(url)
        completed.set()
        return result

    service.extract = record_extract
    service.start()
    try:
        # Reproduce a running worker parked after the browser becomes unusable.
        service._pause_remaining_jobs("NEEDS_BROWSER", "没有可用页面")
        assert not service._resume_gate.is_set()
        assert waiting_on_gate.wait(timeout=2), "worker must already be waiting on the paused gate"
        assert service.enqueue(workspace["id"], URL, force_retry=True)
        assert completed.wait(timeout=3), "explicit retry must wake the paused worker"
        assert processed == [URL]
        assert service.get(workspace["id"], URL)["status"] == "COMPLETE"
        parked = service.get(workspace["id"], other_url)
        assert parked["status"] == "NEEDS_BROWSER"
        assert parked["failure_stage"] == "queue_paused:needs_browser"
    finally:
        service.stop()


def test_douyin_identity_mismatch_is_visible_and_does_not_replace_good_record(tmp_path: Path):
    db = Database(tmp_path / "douyin-mismatch.sqlite3")
    db.initialize()
    workspace = db.create_workspace("抖音身份")
    good = DouyinExtractionService(db=db, collector=DetailCollector())
    first = good.extract(workspace["id"], URL)
    assert first["published_body"]

    failed = DouyinExtractionService(db=db, collector=MismatchCollector()).extract(workspace["id"], URL)

    assert failed["status"] == "IDENTITY_MISMATCH"
    assert failed["failure_stage"] == "identity_mismatch"
    # The old evidence remains on disk/in the record instead of being
    # overwritten by data from the wrong same-title video.
    assert failed["published_body"] == first["published_body"]
    assert failed["video_id"] == "7450123456789012345"


def test_structured_published_caption_can_be_complete_without_archiving_video(tmp_path: Path):
    db = Database(tmp_path / "douyin-complete-copy.sqlite3")
    db.initialize()
    workspace = db.create_workspace("抖音完整发布文案")
    service = DouyinExtractionService(db=db, collector=StructuredCaptionCollector())

    value = service.extract(workspace["id"], URL, search_keyword="相亲故事")

    assert value["status"] == "COMPLETE"
    assert value["published_body"] == "这是一段平台发布文案\n#相亲"
    assert value["published_body_source"] == "详情页结构化发布文案"
    assert value["video_status"] == "MEDIA_UNAVAILABLE"


def test_structured_caption_round_trip_preserves_original_whitespace_and_line_breaks(tmp_path: Path):
    original = "\n  第一行原文。\r\n第二行 #话题  \n"

    class ExactCaptionCollector(StructuredCaptionCollector):
        def extract_douyin_detail(self, url: str, *, search_keyword: str = "", title_hint: str = ""):
            result = super().extract_douyin_detail(url, search_keyword=search_keyword, title_hint=title_hint)
            result["published_body"] = original
            return result

    db = Database(tmp_path / "douyin-exact-caption.sqlite3")
    db.initialize()
    workspace = db.create_workspace("原文往返一致")
    service = DouyinExtractionService(db=db, collector=ExactCaptionCollector())

    saved = service.extract(workspace["id"], URL)
    reopened = service.get(workspace["id"], URL)

    assert saved["published_body"] == original
    assert reopened is not None and reopened["published_body"] == original
    assert reopened["status"] == "COMPLETE"


def test_partial_refresh_preserves_complete_caption_after_explicit_requeue(tmp_path: Path):
    original = "\n  已核验完整原文第一段。\r\n完整原文第二段 #相亲  \n"

    class OriginalCaptionCollector(StructuredCaptionCollector):
        def extract_douyin_detail(self, url: str, **_kwargs):
            result = super().extract_douyin_detail(url)
            result["published_body"] = original
            return result

    class MetadataOnlyCollector(DetailCollector):
        def extract_douyin_detail(self, url: str, **_kwargs):
            result = super().extract_douyin_detail(url)
            result["published_body"] = "本次只能看到的元数据片段…"
            result["published_body_source"] = "详情页元数据文案片段"
            result["failure_stage"] = "caption_not_verified_complete"
            result["diagnostics"] = {"copy_completeness": "detail_metadata_text_only"}
            return result

    db = Database(tmp_path / "douyin-preserve-complete-caption.sqlite3")
    db.initialize()
    workspace = db.create_workspace("刷新仍保留已核验全文")
    service = DouyinExtractionService(db=db, collector=OriginalCaptionCollector())
    first = service.extract(workspace["id"], URL)

    assert service.enqueue(workspace["id"], URL, force_retry=True)
    assert service.get(workspace["id"], URL)["status"] == "QUEUED"
    service.collector = MetadataOnlyCollector()
    refreshed = service.extract(workspace["id"], URL)

    assert refreshed["status"] == "COMPLETE"
    assert refreshed["published_body"] == original
    assert refreshed["published_body_source"] == first["published_body_source"]
    assert refreshed["observed_video_id"] == refreshed["video_id"]
    assert refreshed["diagnostics"]["copy_completeness"] == "verified_structured_caption"
    assert refreshed["diagnostics"]["last_attempt_status"] == "PARTIAL"
    assert refreshed["diagnostics"]["last_attempt_failure_stage"] == "caption_not_verified_complete"
    assert refreshed["diagnostics"]["retained_caption_extracted_at"] == first["extracted_at"]
    assert "此前" in refreshed["error_message"]
    assert refreshed["retry_count"] == 1


def test_captionless_subtitle_refresh_cannot_erase_verified_caption(tmp_path: Path):
    class SubtitleOnlyCollector(DetailCollector):
        def extract_douyin_detail(self, url: str, **_kwargs):
            result = super().extract_douyin_detail(url)
            result["published_body"] = ""
            result["published_body_source"] = ""
            result["failure_stage"] = "detail_caption_missing"
            return result

    db = Database(tmp_path / "douyin-subtitle-refresh.sqlite3")
    db.initialize()
    workspace = db.create_workspace("字幕与原文独立保存")
    service = DouyinExtractionService(db=db, collector=StructuredCaptionCollector())
    first = service.extract(workspace["id"], URL)
    service.collector = SubtitleOnlyCollector()
    refreshed = service.extract(workspace["id"], URL)

    assert refreshed["published_body"] == first["published_body"]
    assert refreshed["status"] == "COMPLETE"
    assert refreshed["platform_subtitle"] == "这是页面字幕，不是发布文案。"
    assert refreshed["diagnostics"]["last_attempt_status"] == "PARTIAL"


def test_failed_attempt_preserves_caption_proof_for_later_partial_refresh(tmp_path: Path):
    db = Database(tmp_path / "douyin-caption-proof-through-failure.sqlite3")
    db.initialize()
    workspace = db.create_workspace("失败后恢复仍保留完整原文")
    service = DouyinExtractionService(db=db, collector=StructuredCaptionCollector())
    original = service.extract(workspace["id"], URL)

    service.collector = MismatchCollector()
    failed = service.extract(workspace["id"], URL)
    assert failed["status"] == "IDENTITY_MISMATCH"
    assert failed["published_body"] == original["published_body"]
    assert failed["diagnostics"]["last_attempt_status"] == "IDENTITY_MISMATCH"
    assert failed["diagnostics"]["last_attempt_diagnostics"]["error_code"] == "identity_mismatch"

    class MetadataOnlyCollector(DetailCollector):
        def extract_douyin_detail(self, url: str, **_kwargs):
            result = super().extract_douyin_detail(url)
            result["published_body"] = "元数据片段…"
            result["published_body_source"] = "详情页元数据文案片段"
            return result

    service.collector = MetadataOnlyCollector()
    assert service.enqueue(workspace["id"], URL, force_retry=True)
    refreshed = service.extract(workspace["id"], URL)

    assert refreshed["status"] == "COMPLETE"
    assert refreshed["published_body"] == original["published_body"]
    assert refreshed["extracted_at"] == original["extracted_at"]
    assert refreshed["diagnostics"]["last_attempt_status"] == "PARTIAL"


def test_later_verified_complete_caption_can_replace_previous_complete_caption(tmp_path: Path):
    class UpdatedCaptionCollector(StructuredCaptionCollector):
        def extract_douyin_detail(self, url: str, **_kwargs):
            result = super().extract_douyin_detail(url)
            result["published_body"] = "作者更新后的完整发布文案。"
            return result

    db = Database(tmp_path / "douyin-updated-caption.sqlite3")
    db.initialize()
    workspace = db.create_workspace("允许更新已核验正文")
    service = DouyinExtractionService(db=db, collector=StructuredCaptionCollector())
    service.extract(workspace["id"], URL)
    service.collector = UpdatedCaptionCollector()
    refreshed = service.extract(workspace["id"], URL)

    assert refreshed["status"] == "COMPLETE"
    assert refreshed["published_body"] == "作者更新后的完整发布文案。"
    assert "last_attempt_status" not in refreshed["diagnostics"]


def test_connected_browser_without_usable_page_requires_browser_action(tmp_path: Path):
    class NoPageCollector:
        def extract_douyin_detail(self, *_args, **_kwargs):
            raise PlatformBrowserError("采集浏览器没有可用页面，请打开采集页面后继续。", code="browser_page_required")

    db = Database(tmp_path / "douyin-browser-page-required.sqlite3")
    db.initialize()
    workspace = db.create_workspace("采集页面恢复")
    service = DouyinExtractionService(db=db, collector=NoPageCollector())
    value = service.extract(workspace["id"], URL)

    assert value["status"] == "NEEDS_BROWSER"
    assert value["failure_stage"] == "browser_page_required"
    assert value["published_body"] == ""


def test_local_transcription_is_saved_separately_and_only_after_video_id_verification(tmp_path: Path):
    db = Database(tmp_path / "douyin-local-asr.sqlite3")
    db.initialize()
    workspace = db.create_workspace("本机语音转写")
    service = DouyinExtractionService(db=db, collector=StructuredCaptionCollector())
    first = service.extract(workspace["id"], URL)

    saved = service.save_local_transcription(workspace["id"], URL, {
        "text": "视频里的口播全文。",
        "language": "zh-CN",
        "engine": "Apple SpeechAnalyzer (offline, on-device)",
        "model": "SpeechTranscriber zh-CN",
        "source": "USER_PROVIDED_LOCAL_MEDIA",
        "media_sha256": "a" * 64,
        "message": "由用户选择的本机文件转写。",
    })

    assert saved["status"] == first["status"] == "COMPLETE"
    assert saved["published_body"] == first["published_body"]
    assert saved["asr_text"] == "视频里的口播全文。"
    assert saved["asr_source"] == "USER_PROVIDED_LOCAL_MEDIA"
    assert saved["asr_media_sha256"] == "a" * 64

    db.execute(
        "UPDATE douyin_post_extractions SET status='IDENTITY_MISMATCH',observed_video_id=? WHERE workspace_id=? AND url=?",
        ("7450999999999999999", workspace["id"], URL),
    )
    try:
        service.save_local_transcription(workspace["id"], URL, {"text": "不应关联"})
    except ValueError as exc:
        assert "ID" in str(exc)
    else:
        raise AssertionError("mismatched video ID must reject local transcript association")


def test_refresh_does_not_erase_previously_saved_local_transcript(tmp_path: Path):
    db = Database(tmp_path / "douyin-preserve-asr.sqlite3")
    db.initialize()
    workspace = db.create_workspace("保留转写")
    service = DouyinExtractionService(db=db, collector=StructuredCaptionCollector())
    service.extract(workspace["id"], URL)
    service.save_local_transcription(workspace["id"], URL, {
        "text": "已人工选择的本机口播稿。", "source": "USER_PROVIDED_LOCAL_MEDIA",
        "media_sha256": "c" * 64, "message": "保留现有转写。",
    })

    refreshed = service.extract(workspace["id"], URL)

    assert refreshed["published_body"] == "这是一段平台发布文案\n#相亲"
    assert refreshed["asr_text"] == "已人工选择的本机口播稿。"
    assert refreshed["asr_source"] == "USER_PROVIDED_LOCAL_MEDIA"
    assert refreshed["asr_media_sha256"] == "c" * 64


def test_douyin_queue_accepts_all_jv_candidates_beyond_old_thirty_item_cap(tmp_path: Path):
    db = Database(tmp_path / "douyin-batch.sqlite3")
    db.initialize()
    workspace = db.create_workspace("抖音批量正文")
    service = DouyinExtractionService(db=db, collector=DetailCollector())
    items = [
        {
            "platform": "douyin",
            "url": f"https://www.douyin.com/video/{7450123456789010000 + index}",
            "title": f"相亲观察 {index}",
            "raw": {"search_query": "相亲观察"},
        }
        for index in range(45)
    ]

    added = service.enqueue_results(workspace["id"], {"items": items})

    assert added == 45
    assert service._jobs.qsize() == 45
    assert len(service.list(workspace["id"], limit=500)) == 45


def test_search_result_text_stays_separate_when_detail_read_fails(tmp_path: Path):
    db = Database(tmp_path / "douyin-search-text.sqlite3")
    db.initialize()
    workspace = db.create_workspace("候选摘要独立保存")

    class EmptyDetailCollector:
        def extract_douyin_detail(self, url: str, **_kwargs):
            return {
                "url": url, "video_id": url.rsplit("/", 1)[-1], "observed_video_id": "",
                "published_body": "", "published_body_source": "", "status": "FAILED",
                "failure_stage": "content_extract", "error_message": "详情页没有返回可核验正文。",
            }

    service = DouyinExtractionService(db=db, collector=EmptyDetailCollector())
    excerpt = "搜索结果卡片摘要；绝不能冒充详情页发布文案。"
    added = service.enqueue_results(workspace["id"], {"items": [{
        "platform": "douyin", "url": URL, "title": "候选标题", "excerpt": excerpt,
    }]})
    assert added == 1

    value = service.extract(workspace["id"], URL, search_keyword="相亲", title_hint="候选标题")

    assert value["status"] == "FAILED"
    assert value["published_body"] == ""
    assert value["published_body_source"] == ""
    assert value["search_result_text"] == excerpt
    assert value["search_result_text_source"] == "搜索结果卡片摘要"


def test_video_and_note_routes_dedupe_to_one_stable_aweme_record(tmp_path: Path):
    db = Database(tmp_path / "douyin-route-dedupe.sqlite3")
    db.initialize()
    workspace = db.create_workspace("作品路由去重")
    service = DouyinExtractionService(db=db, collector=DetailCollector())

    added = service.enqueue_results(workspace["id"], {"items": [
        {"platform": "douyin", "url": URL, "title": "视频路由"},
        {"platform": "douyin", "url": NOTE_URL, "title": "note 路由"},
    ]})

    assert service.canonical_video_url(NOTE_URL) == URL
    assert added == 1
    assert service._jobs.qsize() == 1
    assert len(service.list(workspace["id"], limit=500)) == 1


def test_legacy_card_fallback_is_migrated_out_of_published_body(tmp_path: Path):
    db = Database(tmp_path / "douyin-legacy-card-fallback.sqlite3")
    db.initialize()
    workspace = db.create_workspace("历史候选摘要迁移")
    service = DouyinExtractionService(db=db, collector=DetailCollector())
    value = service.extract(workspace["id"], URL, search_keyword="相亲", title_hint="候选标题")
    legacy_text = "旧版误放在发布文案字段的搜索候选标题"
    db.execute(
        """UPDATE douyin_post_extractions SET status='PARTIAL',published_body=?,
        published_body_source='同一视频搜索结果卡片可见发布文案',
        failure_stage='detail_caption_fallback',diagnostics_json='{}' WHERE workspace_id=? AND url=?""",
        (legacy_text, workspace["id"], URL),
    )

    db.initialize()
    migrated = service.get(workspace["id"], URL)

    assert migrated["published_body"] == ""
    assert migrated["published_body_source"] == ""
    assert migrated["search_result_text"] == f"候选标题\n\n{legacy_text}"
    assert migrated["search_result_text_source"] == "搜索候选标题提示（非正文）；并保留旧版搜索回退文本（非核验详情正文）"
    assert migrated["observed_video_id"] == ""
    assert migrated["failure_stage"] == "detail_caption_missing"
    assert migrated["diagnostics"]["legacy_search_text_reclassified"] == 1


def test_caption_only_extraction_gets_one_logged_in_asr_upgrade_attempt(tmp_path: Path):
    db = Database(tmp_path / "douyin-asr-upgrade.sqlite3")
    db.initialize()
    workspace = db.create_workspace("登录态转写升级")
    service = DouyinExtractionService(db=db, collector=StructuredCaptionCollector())
    first = service.extract(workspace["id"], URL)
    assert first["status"] == "COMPLETE"
    assert first["asr_text"] == ""

    queued = service.enqueue(workspace["id"], URL, search_keyword="相亲", title_hint="标题")
    assert queued is True
    assert service._jobs.qsize() == 1

    db.execute(
        "UPDATE douyin_post_extractions SET status='COMPLETE',retry_count=2 WHERE workspace_id=? AND url=?",
        (workspace["id"], URL),
    )
    retry_service = DouyinExtractionService(db=db, collector=StructuredCaptionCollector())
    assert retry_service.enqueue(workspace["id"], URL, search_keyword="相亲", title_hint="标题") is False


def test_batch_recovery_queues_saved_caption_only_rows_for_login_asr(tmp_path: Path):
    db = Database(tmp_path / "douyin-asr-backfill.sqlite3")
    db.initialize()
    workspace = db.create_workspace("补齐历史口播")

    class BatchCollector:
        def extract_douyin_detail(self, url: str, **_kwargs):
            video_id = url.rsplit("/", 1)[-1]
            return {
                "url": url, "video_id": video_id, "observed_video_id": video_id,
                "published_body": "完整发布文案。", "published_body_source": "详情页结构化发布文案",
                "status": "COMPLETE", "asr_status": "NOT_ATTEMPTED", "video_status": "MEDIA_UNAVAILABLE",
            }

    service = DouyinExtractionService(db=db, collector=BatchCollector())
    ids = ["7450123456789012345", "7450123456789012346", "7450123456789012347"]
    urls = [f"https://www.douyin.com/video/{video_id}" for video_id in ids]
    for url in urls:
        service.extract(workspace["id"], url, search_keyword="相亲", title_hint="相亲案例")
    service.save_local_transcription(workspace["id"], urls[1], {"text": "已有口播转写。", "source": "USER_PROVIDED_LOCAL_MEDIA"})
    db.execute(
        "UPDATE douyin_post_extractions SET retry_count=2 WHERE workspace_id=? AND url=?",
        (workspace["id"], urls[2]),
    )

    queued = service.enqueue_missing_transcripts(workspace["id"])

    assert queued == 2
    assert service._jobs.qsize() == 2
    states = {row["url"]: row for row in service.list(workspace["id"], limit=500)}
    assert states[urls[0]]["status"] == "QUEUED"
    assert states[urls[0]]["asr_status"] == "REQUESTED"
    assert states[urls[1]]["asr_text"] == "已有口播转写。"
    assert states[urls[2]]["status"] == "QUEUED"


def test_explicit_transcript_backfill_passes_intent_and_keeps_complete_caption(tmp_path: Path):
    calls = []

    class TranscriptCollector(StructuredCaptionCollector):
        def extract_douyin_detail(self, url, *, transcribe_media=False, progress_callback=None, **_kwargs):
            calls.append(transcribe_media)
            result = super().extract_douyin_detail(url)
            if transcribe_media:
                progress_callback({"asr_message": "正在转写明确请求的口播。"})
                result.update(asr_text="这是独立保存的口播转写。", asr_status="BROWSER_MEDIA_TRANSCRIBED",
                              asr_source="BROWSER_LOGGED_IN_MEDIA")
            return result

    db = Database(tmp_path / "douyin-explicit-asr.sqlite3")
    db.initialize()
    workspace = db.create_workspace("完整发布文案也可补齐口播")
    service = DouyinExtractionService(db=db, collector=TranscriptCollector())
    original = service.extract(workspace["id"], URL)
    assert calls == [False]

    assert service.enqueue_missing_transcripts(workspace["id"]) == 1
    assert service.get(workspace["id"], URL)["asr_status"] == "REQUESTED"
    result = service.extract(workspace["id"], URL)

    assert calls == [False, True]
    assert result["published_body"] == original["published_body"]
    assert result["status"] == "COMPLETE"
    assert result["asr_text"] == "这是独立保存的口播转写。"
    assert result["asr_status"] == "BROWSER_MEDIA_TRANSCRIBED"


def test_explicit_transcription_intent_survives_crash_and_worker_restart(tmp_path: Path):
    db = Database(tmp_path / "douyin-asr-restart.sqlite3")
    db.initialize()
    workspace = db.create_workspace("补齐口播跨重启恢复")
    original_service = DouyinExtractionService(db=db, collector=StructuredCaptionCollector())
    original = original_service.extract(workspace["id"], URL)
    assert original_service.enqueue(workspace["id"], URL, transcribe_media=True)

    class InterruptedCollector:
        def extract_douyin_detail(self, url, *, transcribe_media=False, progress_callback=None, **_kwargs):
            assert transcribe_media is True
            progress_callback({"asr_message": "模拟转写期间退出。"})
            raise RuntimeError("simulated interruption")

    original_service.collector = InterruptedCollector()
    with pytest.raises(RuntimeError, match="simulated interruption"):
        original_service.extract(workspace["id"], URL)
    interrupted = original_service.get(workspace["id"], URL)
    assert interrupted["status"] == "TRANSCRIBING"
    assert interrupted["asr_status"] == "REQUESTED"

    class RestartedCollector(StructuredCaptionCollector):
        def extract_douyin_detail(self, url, *, transcribe_media=False, **_kwargs):
            assert transcribe_media is True
            result = super().extract_douyin_detail(url)
            result.update(asr_text="重启后完成的口播转写。", asr_status="BROWSER_MEDIA_TRANSCRIBED",
                          asr_source="BROWSER_LOGGED_IN_MEDIA")
            return result

    # A new service and connection represent the new process; only SQLite
    # carries the explicit request forward, not any old queue tuple or flag.
    restarted = DouyinExtractionService(db=Database(db.path), collector=RestartedCollector())
    completed = threading.Event()
    extract = restarted.extract

    def record_completion(*args, **kwargs):
        result = extract(*args, **kwargs)
        completed.set()
        return result

    restarted.extract = record_completion
    restarted.start()
    try:
        assert completed.wait(timeout=3)
        recovered = restarted.get(workspace["id"], URL)
        assert recovered["status"] == "COMPLETE"
        assert recovered["published_body"] == original["published_body"]
        assert recovered["asr_text"] == "重启后完成的口播转写。"
    finally:
        restarted.stop()


def test_existing_transcript_is_not_enqueued_again_even_for_explicit_backfill(tmp_path: Path):
    db = Database(tmp_path / "douyin-asr-do-not-repeat.sqlite3")
    db.initialize()
    workspace = db.create_workspace("已有转写不重复处理")
    service = DouyinExtractionService(db=db, collector=StructuredCaptionCollector())
    service.extract(workspace["id"], URL)
    service.save_local_transcription(workspace["id"], URL, {"text": "已有本机口播。"})

    assert not service.enqueue(workspace["id"], URL, force_retry=True, transcribe_media=True)
    assert service._jobs.empty()
    assert service.get(workspace["id"], URL)["asr_status"] == "USER_MEDIA_TRANSCRIBED"


def test_low_confidence_transcript_can_be_explicitly_retried_without_changing_caption_completeness(tmp_path: Path):
    db = Database(tmp_path / "douyin-low-confidence-retry.sqlite3")
    db.initialize()
    workspace = db.create_workspace("低置信度口播可重试")
    service = DouyinExtractionService(db=db, collector=StructuredCaptionCollector())
    original = service.extract(workspace["id"], URL)
    saved = service.save_local_transcription(workspace["id"], URL, {
        "text": "现在9一我", "segments": [{"text": "现在9一我", "confidence": 0.4032}],
        "confidence": {"mean": 0.4032, "assessed_segment_count": 1},
    })
    assert saved["asr_quality"]["status"] == "NEEDS_REVIEW"
    assert saved["status"] == "COMPLETE"
    assert saved["published_body"] == original["published_body"]
    assert not service.enqueue(workspace["id"], URL), "ordinary searches must not redo ASR"
    assert service.enqueue_missing_transcripts(workspace["id"]) == 1
    pending = service.get(workspace["id"], URL)
    assert pending["asr_status"] == "REQUESTED"
    assert pending["asr_text"] == "现在9一我"

    class BetterCollector(StructuredCaptionCollector):
        def extract_douyin_detail(self, url, *, transcribe_media=False, **_kwargs):
            assert transcribe_media is True
            result = super().extract_douyin_detail(url)
            result.update(asr_text="重新识别得到的文本。", asr_status="BROWSER_MEDIA_TRANSCRIBED",
                          asr_segments=[{"text": "重新识别得到的文本。", "confidence": 0.9}])
            return result

    service.collector = BetterCollector()
    refreshed = service.extract(workspace["id"], URL)
    assert refreshed["status"] == "COMPLETE"
    assert refreshed["published_body"] == original["published_body"]
    assert refreshed["asr_quality"]["status"] == "REVIEW_NOT_FLAGGED"
    assert refreshed["asr_text"] == "重新识别得到的文本。"
    assert DouyinExtractionService(db=db, collector=BetterCollector()).enqueue_missing_transcripts(workspace["id"]) == 0


def test_caption_refresh_preserves_asr_review_evidence_and_truncation(tmp_path: Path):
    db = Database(tmp_path / "douyin-asr-review-evidence.sqlite3")
    db.initialize()
    workspace = db.create_workspace("保留口播审核证据")
    service = DouyinExtractionService(db=db, collector=StructuredCaptionCollector())
    service.extract(workspace["id"], URL)
    original = service.save_local_transcription(workspace["id"], URL, {
        "text": "截断的口播片段。", "text_truncated": True,
        "segments": [{"text": "截断的口播片段。", "confidence": 0.4}],
        "confidence": {"mean": 0.4, "assessed_segment_count": 1},
        "low_confidence": [{"text": "截断的口播片段。", "confidence": 0.4}],
    })
    assert original["asr_quality"]["status"] == "INCOMPLETE"

    refreshed = service.extract(workspace["id"], URL)

    for field in ("asr_text", "asr_segments", "asr_confidence", "asr_low_confidence", "asr_quality"):
        assert refreshed[field] == original[field]
    assert refreshed["status"] == "COMPLETE"
    assert refreshed["diagnostics"]["asr_text_truncated"] is True


def test_unsuccessful_explicit_asr_retry_keeps_old_reviewable_text_and_failure_reason(tmp_path: Path):
    db = Database(tmp_path / "douyin-asr-retry-failed-quality.sqlite3")
    db.initialize()
    workspace = db.create_workspace("重试失败仍保留可核对片段")
    service = DouyinExtractionService(db=db, collector=StructuredCaptionCollector())
    service.extract(workspace["id"], URL)
    service.save_local_transcription(workspace["id"], URL, {
        "text": "低置信度片段", "segments": [{"text": "低置信度片段", "confidence": 0.4}],
    })
    assert service.enqueue_missing_transcripts(workspace["id"]) == 1

    class FailedAsrCollector(StructuredCaptionCollector):
        def extract_douyin_detail(self, url, *, transcribe_media=False, progress_callback=None, **_kwargs):
            assert transcribe_media is True
            progress_callback({"asr_source": "BROWSER_LOGGED_IN_MEDIA"})
            assert service.get(workspace["id"], URL)["asr_source"] == "USER_PROVIDED_LOCAL_MEDIA"
            result = super().extract_douyin_detail(url)
            result.update(asr_status="TRANSCRIPTION_FAILED", asr_message="本次没有取得可处理媒体。")
            return result

    service.collector = FailedAsrCollector()
    refreshed = service.extract(workspace["id"], URL)
    assert refreshed["status"] == "COMPLETE"
    assert refreshed["asr_quality"]["status"] == "NEEDS_REVIEW"
    assert refreshed["asr_text"] == "低置信度片段"
    assert refreshed["asr_status"] == "TRANSCRIPTION_FAILED"
    assert refreshed["asr_source"] == "USER_PROVIDED_LOCAL_MEDIA"
    assert "本次没有取得可处理媒体" in refreshed["asr_message"]


def test_explicit_transcription_intent_survives_platform_pause_and_normal_requeue(tmp_path: Path):
    db = Database(tmp_path / "douyin-asr-pause-intent.sqlite3")
    db.initialize()
    workspace = db.create_workspace("登录后仍记得补齐口播")

    class LoginRequiredCollector:
        def extract_douyin_detail(self, *_args, **_kwargs):
            raise PlatformBrowserError("请完成平台登录", code="login_required")

    first_service = DouyinExtractionService(db=db, collector=LoginRequiredCollector())
    assert first_service.enqueue(workspace["id"], URL, transcribe_media=True)
    failed = first_service.extract(workspace["id"], URL)
    assert failed["status"] == "USER_ACTION_REQUIRED"
    assert failed["asr_status"] == "REQUESTED"

    retry_service = DouyinExtractionService(db=db, collector=StructuredCaptionCollector())
    assert retry_service.enqueue(workspace["id"], URL, force_retry=True)
    assert retry_service.get(workspace["id"], URL)["asr_status"] == "REQUESTED"


def test_user_initiated_backfill_recovers_first_login_blocked_row_even_after_auto_retry_cap(tmp_path: Path):
    db = Database(tmp_path / "douyin-login-wall-retry.sqlite3")
    db.initialize()
    workspace = db.create_workspace("登录后重试")

    class LoginWallCollector:
        def extract_douyin_detail(self, *_args, **_kwargs):
            raise PlatformBrowserError("抖音登录已失效，请本人扫码登录", code="login_required")

    service = DouyinExtractionService(db=db, collector=LoginWallCollector())
    blocked = service.extract(workspace["id"], URL, search_keyword="相亲")
    assert blocked["status"] == "USER_ACTION_REQUIRED"
    assert blocked["failure_stage"] == "login_required"
    db.execute(
        "UPDATE douyin_post_extractions SET retry_count=3 WHERE workspace_id=? AND url=?",
        (workspace["id"], URL),
    )

    queued = service.enqueue_missing_transcripts(workspace["id"])

    recovered = service.get(workspace["id"], URL)
    assert queued == 1
    assert service._jobs.qsize() == 1
    assert recovered["status"] == "QUEUED"
    assert recovered["failure_stage"] == ""
    assert recovered["retry_count"] == 3


def test_explicit_batch_resume_retries_partial_douyin_detail_rows(tmp_path: Path):
    db = Database(tmp_path / "douyin-explicit-batch-resume.sqlite3")
    db.initialize()
    workspace = db.create_workspace("验证后继续抖音详情")
    service = DouyinExtractionService(db=db, collector=DetailCollector())
    partial = service.extract(workspace["id"], URL, search_keyword="相亲", title_hint="搜索卡片文案")
    assert partial["status"] == "PARTIAL"
    db.execute(
        "UPDATE douyin_post_extractions SET retry_count=2 WHERE workspace_id=? AND url=?",
        (workspace["id"], URL),
    )

    queued = service.enqueue_results(
        workspace["id"],
        {"items": [{"platform": "douyin", "url": URL, "title": "匹配视频标题"}]},
        force_retry=True,
    )

    resumed = service.get(workspace["id"], URL)
    assert queued == 1
    assert service._jobs.qsize() == 1
    assert resumed["status"] == "QUEUED"
    assert resumed["published_body"] == partial["published_body"]


def test_explicit_batch_resume_refetches_caption_without_erasing_existing_asr(tmp_path: Path):
    db = Database(tmp_path / "douyin-resume-with-asr.sqlite3")
    db.initialize()
    workspace = db.create_workspace("验证后重抓原文且保留口播转写")
    service = DouyinExtractionService(db=db, collector=DetailCollector())
    partial = service.extract(workspace["id"], URL, search_keyword="相亲", title_hint="搜索卡片文案")
    service.save_local_transcription(workspace["id"], URL, {
        "text": "之前保存的口播转写。",
        "source": "USER_PROVIDED_LOCAL_MEDIA",
        "message": "由用户提供的本机视频转写。",
        "media_sha256": "d" * 64,
    })

    queued = service.enqueue_results(
        workspace["id"],
        {"items": [{"platform": "douyin", "url": URL, "title": "匹配视频标题"}]},
        force_retry=True,
    )

    resumed = service.get(workspace["id"], URL)
    assert queued == 1
    assert service._jobs.qsize() == 1
    assert resumed is not None
    assert resumed["status"] == "QUEUED"
    assert resumed["published_body"] == partial["published_body"]
    assert resumed["asr_text"] == "之前保存的口播转写。"
    assert resumed["asr_status"] == "USER_MEDIA_TRANSCRIBED"
    assert resumed["asr_source"] == "USER_PROVIDED_LOCAL_MEDIA"


def test_login_wall_parks_remaining_batch_until_user_retries_after_login(tmp_path: Path):
    db = Database(tmp_path / "douyin-paused-batch.sqlite3")
    db.initialize()
    workspace = db.create_workspace("抖音登录暂停")
    service = DouyinExtractionService(db=db, collector=DetailCollector())
    first_id = "7450123456789012345"
    second_id = "7450123456789012346"
    first_url = f"https://www.douyin.com/video/{first_id}"
    second_url = f"https://www.douyin.com/video/{second_id}"
    service.enqueue(workspace["id"], first_url, search_keyword="相亲")
    service.enqueue(workspace["id"], second_url, search_keyword="相亲")

    service._pause_remaining_jobs("USER_ACTION_REQUIRED", "抖音登录已失效，请扫码登录")
    parked = {row["url"]: row for row in service.list(workspace["id"], limit=500)}
    assert service._jobs.qsize() == 0
    assert parked[second_url]["status"] == "USER_ACTION_REQUIRED"
    assert parked[second_url]["failure_stage"] == "queue_paused:user_action_required"

    resumed = service.resume_pending(workspace["id"])

    assert resumed == 2
    assert service._jobs.qsize() == 2
    requeued = {row["url"]: row for row in service.list(workspace["id"], limit=500)}
    assert requeued[second_url]["status"] == "QUEUED"
    assert requeued[second_url]["failure_stage"] == ""
