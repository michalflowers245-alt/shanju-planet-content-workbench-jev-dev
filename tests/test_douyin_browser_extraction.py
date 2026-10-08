from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from backend.app import platform_browser as platform_browser_module
from backend.app.platform_browser import PlatformBrowserCollector, PlatformBrowserError, _HiddenCDPTarget


VIDEO_ID = "7450123456789012345"
VIDEO_URL = f"https://www.douyin.com/video/{VIDEO_ID}"


class FakeHiddenTarget:
    payload: dict[str, object] = {}
    expression = ""
    network_responses: list[dict[str, object]] = []
    network_bodies: dict[str, bytes] = {}
    network_limits: tuple[int, int] | None = None
    network_read_count = 0

    def __init__(self, _websocket_url: str) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def navigate(self, _url: str) -> None:
        return None

    def enable_network(self, *, max_total_buffer_size=0, max_resource_buffer_size=0) -> None:
        type(self).network_limits = (max_total_buffer_size, max_resource_buffer_size)

    def take_network_responses(self) -> list[dict[str, object]]:
        type(self).network_read_count += 1
        responses = type(self).network_responses
        type(self).network_responses = []
        return responses

    def response_body(self, request_id: str) -> bytes | None:
        return type(self).network_bodies.get(request_id)

    def evaluate(self, expression: str):
        type(self).expression = expression
        return type(self).payload

    def load_network_resource_to_file(self, _url: str, target_path: str | Path, **_kwargs):
        Path(target_path).write_bytes(b"temporary-mp4")
        return {"success": True, "status": 200, "headers": {"content-type": "video/mp4"}, "bytes_written": 13}


class SequencedFakeHiddenTarget(FakeHiddenTarget):
    payloads: list[dict[str, object]] = []
    evaluate_count = 0

    def evaluate(self, expression: str):
        type(self).expression = expression
        index = type(self).evaluate_count
        type(self).evaluate_count += 1
        return type(self).payloads[min(index, len(type(self).payloads) - 1)]


def _collector(monkeypatch: pytest.MonkeyPatch, payload: dict[str, object]) -> PlatformBrowserCollector:
    monkeypatch.setattr(platform_browser_module, "_HiddenCDPTarget", FakeHiddenTarget)
    FakeHiddenTarget.payload = payload
    collector = PlatformBrowserCollector(cdp_url="http://127.0.0.1:1")
    monkeypatch.setattr(collector, "status", lambda: {"running": True})
    monkeypatch.setattr(collector, "_browser_websocket_url", lambda: "ws://127.0.0.1/devtools/browser/test")
    return collector


def test_explicit_media_request_transcribes_even_when_publication_caption_is_complete(monkeypatch):
    collector = _collector(monkeypatch, {
        "finalUrl": VIDEO_URL, "pageTitle": "已取得原文", "visibleText": "已取得原文",
        "observedVideoId": VIDEO_ID, "publishedBody": "  完整发布文案\n",
        "captionSource": "详情页结构化发布文案", "videoElements": 1,
        "mediaUrlCandidates": ["https://v3-web.douyinvod.com/video.mp4"],
    })
    monkeypatch.setattr(FakeHiddenTarget, "network_responses", [])
    calls = []

    def transcribe(page, candidates):
        calls.append(candidates)
        return {"asr_status": "BROWSER_MEDIA_TRANSCRIBED", "asr_text": "单独的口播全文。", "video_status": "TRANSIENT_MEDIA_PROCESSED"}

    monkeypatch.setattr(collector, "_transcribe_douyin_media", transcribe)
    caption_only = collector.extract_douyin_detail(VIDEO_URL)
    assert calls == []
    assert caption_only["status"] == "COMPLETE"
    result = collector.extract_douyin_detail(VIDEO_URL, transcribe_media=True)
    assert len(calls) == 1
    assert result["status"] == "COMPLETE"
    assert result["published_body"] == "  完整发布文案\n"
    assert result["asr_text"] == "单独的口播全文。"


def test_detail_reader_marks_only_exact_structured_caption_as_complete(monkeypatch: pytest.MonkeyPatch):
    collector = _collector(monkeypatch, {
        "finalUrl": VIDEO_URL,
        "pageTitle": "同城相亲的三个细节",
        "visibleText": "同城相亲的三个细节",
        "observedVideoId": VIDEO_ID,
        "title": "同城相亲的三个细节",
        "author": "小豆子",
        "publishedBody": "完整的发布文案第一段\n\n第二段 #相亲",
        "captionSource": "详情页结构化发布文案",
        "platformSubtitle": "第一句口播。\n第二句口播。",
        "subtitleSource": "video.textTracks 字幕轨",
        "subtitleLineCount": 2,
        "videoElements": 1,
        "metrics": {"likes": 128},
    })

    result = collector.extract_douyin_detail(VIDEO_URL, search_keyword="相亲")

    assert result["status"] == "COMPLETE"
    assert result["published_body"] == "完整的发布文案第一段\n\n第二段 #相亲"
    assert result["published_body_source"] == "详情页结构化发布文案"
    assert result["platform_subtitle"] == "第一句口播。\n第二句口播。"
    assert result["video_status"] == "MEDIA_UNAVAILABLE"
    assert result["diagnostics"]["copy_completeness"] == "verified_structured_caption"
    assert collector._platform_sessions["douyin"]["state"] == platform_browser_module._SESSION_UNKNOWN
    assert "__INITIAL_STATE__" in FakeHiddenTarget.expression
    assert "video.textTracks" in FakeHiddenTarget.expression
    assert "const exactCandidates = candidates.filter" in FakeHiddenTarget.expression
    assert "const candidatesWithCaption = exactCandidates.filter" in FakeHiddenTarget.expression
    assert "const captionFieldFor = item =>" in FakeHiddenTarget.expression
    assert "typeof item[field] === 'string' && clean(item[field])" in FakeHiddenTarget.expression
    assert "captionFor(right).length - captionFor(left).length" in FakeHiddenTarget.expression
    assert "candidates.find(item" not in FakeHiddenTarget.expression


def test_browser_transcript_truncation_reaches_saved_quality_without_downgrading_caption(monkeypatch, tmp_path):
    from backend.app.db import Database
    from backend.app.douyin_extractions import DouyinExtractionService

    collector = _collector(monkeypatch, {
        "finalUrl": VIDEO_URL, "pageTitle": "完整发布文案", "visibleText": "完整发布文案",
        "observedVideoId": VIDEO_ID, "publishedBody": "  完整发布文案\n",
        "captionSource": "详情页结构化发布文案", "videoElements": 1,
        "mediaUrlCandidates": ["https://v3-web.douyinvod.com/video.mp4"],
    })
    monkeypatch.setattr(FakeHiddenTarget, "network_responses", [])
    monkeypatch.setattr(collector, "_transcribe_douyin_media", lambda *_args: {
        "asr_status": "BROWSER_MEDIA_TRANSCRIBED", "asr_text": "达到上限后留下的口播片段。",
        "asr_text_truncated": True, "asr_segments": [{"text": "口播片段", "confidence": 0.99}],
        "video_status": "TRANSIENT_MEDIA_PROCESSED",
    })
    db = Database(tmp_path / "truncated-browser-transcript.sqlite3")
    db.initialize()
    workspace = db.create_workspace("浏览器转写截断标记")
    service = DouyinExtractionService(db=db, collector=collector)
    assert service.enqueue(workspace["id"], VIDEO_URL, transcribe_media=True)

    result = service.extract(workspace["id"], VIDEO_URL)

    assert result["status"] == "COMPLETE"
    assert result["published_body"] == "  完整发布文案\n"
    assert result["asr_text"] == "达到上限后留下的口播片段。"
    assert result["diagnostics"]["asr_text_truncated"] is True
    assert result["asr_quality"]["status"] == "INCOMPLETE"


def test_douyin_detail_payload_requires_exact_top_level_aweme_and_preserves_caption():
    caption = "\n长沙同城活动的完整发布文案。 #长沙\n"
    raw_body = json.dumps({
        "aweme_detail": {
            "aweme_id": VIDEO_ID,
            "desc": caption,
            "author": {"nickname": "长沙活动号"},
            "statistics": {"digg_count": 27, "comment_count": 3},
            "create_time": 1763740800,
        },
    }, ensure_ascii=False).encode("utf-8")

    result = platform_browser_module._douyin_detail_payload(raw_body, VIDEO_ID)

    assert result is not None
    assert result["video_id"] == VIDEO_ID
    assert result["published_body"] == caption
    assert result["published_body_length"] == len(caption)
    assert result["caption_field"] == "desc"
    assert result["author"] == "长沙活动号"
    assert result["metrics"] == {"likes": 27, "comments": 3}
    assert platform_browser_module._douyin_detail_payload(raw_body, "7450999999999999999") is None


def test_douyin_detail_payload_does_not_accept_comment_aweme_id_or_search_text():
    raw_body = json.dumps({
        "comments": [{"aweme_id": VIDEO_ID, "text": "评论内容不能作为发布文案"}],
        "search_result": {"aweme_id": VIDEO_ID, "desc": "搜索摘要不能作为发布文案"},
    }, ensure_ascii=False).encode("utf-8")

    assert platform_browser_module._douyin_detail_payload(raw_body, VIDEO_ID) is None


def test_detail_reader_uses_exact_same_origin_network_aweme_response(monkeypatch: pytest.MonkeyPatch):
    caption = "长沙露营活动真实发布文案 #长沙同城 #交友"
    body = json.dumps({
        "aweme_detail": {
            "aweme_id": VIDEO_ID,
            "desc": caption,
            "author": {"nickname": "长沙不熟社交"},
            "statistics": {"digg_count": 72, "comment_count": 6, "share_count": 4},
            "create_time": 1763740800,
        },
    }, ensure_ascii=False).encode("utf-8")
    FakeHiddenTarget.network_responses = [{
        "request_id": "detail-1",
        "url": f"https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id={VIDEO_ID}&signature=secret",
        "status": 200,
        "headers": {"content-type": "application/json; charset=utf-8", "content-length": str(len(body))},
    }]
    FakeHiddenTarget.network_bodies = {"detail-1": body}
    FakeHiddenTarget.network_read_count = 0
    monkeypatch.setattr(platform_browser_module, "_DOUYIN_DETAIL_READ_TIMEOUT_SECONDS", 0.01)
    collector = _collector(monkeypatch, {
        "finalUrl": VIDEO_URL,
        "pageTitle": "长沙露营活动 - 抖音",
        "visibleText": "长沙露营活动",
        "observedVideoId": VIDEO_ID,
        "pageHasDetailContent": True,
        "title": "长沙露营活动",
        "author": "长沙不熟社交",
        "publishedBody": "只有部分详情元数据 #长沙",
        "captionSource": "详情页元数据文案片段",
        "metrics": {},
    })
    monkeypatch.setattr(
        collector, "_transcribe_douyin_media",
        lambda *_args: pytest.fail("a complete original caption should not trigger unrelated ASR"),
    )

    result = collector.extract_douyin_detail(VIDEO_URL)

    assert result["status"] == "COMPLETE"
    assert result["video_id"] == result["observed_video_id"] == VIDEO_ID
    assert result["published_body"] == caption
    assert result["published_body_source"] == "详情页结构化发布文案"
    assert result["author"] == "长沙不熟社交"
    assert result["metrics"] == {"likes": 72, "comments": 6, "shares": 4}
    assert result["diagnostics"]["network_detail_response_count"] == 1
    assert result["diagnostics"]["network_detail_payload_match_count"] == 1
    assert result["diagnostics"]["network_capture_available"] is True
    assert result["diagnostics"]["network_detail_response_path"] == "/aweme/v1/web/aweme/detail/"
    assert "signature" not in str(result["diagnostics"])
    assert FakeHiddenTarget.network_read_count == 1
    assert FakeHiddenTarget.network_limits == (24 * 1024 * 1024, 4 * 1024 * 1024)


def test_verification_page_does_not_read_captured_detail_response_bodies(monkeypatch: pytest.MonkeyPatch):
    FakeHiddenTarget.network_responses = []
    FakeHiddenTarget.network_bodies = {}
    FakeHiddenTarget.network_read_count = 0
    collector = _collector(monkeypatch, {
        "finalUrl": VIDEO_URL,
        "pageTitle": "安全验证 - 抖音",
        "visibleText": "请完成安全验证后继续",
        "observedVideoId": "",
        "pageHasDetailContent": False,
        "publishedBody": "",
        "captionSource": "",
    })
    monkeypatch.setattr(platform_browser_module, "_DOUYIN_DETAIL_READ_TIMEOUT_SECONDS", 0.01)

    with pytest.raises(PlatformBrowserError) as caught:
        collector.extract_douyin_detail(VIDEO_URL)

    assert caught.value.code == "verification_required"
    assert FakeHiddenTarget.network_read_count == 0


def _restricted_network_response(monkeypatch, *, reason="status_audit_self_see", video_id=VIDEO_ID, lost_body=False):
    raw = json.dumps({
        "status_code": 0, "aweme_detail": None,
        "filter_detail": {"aweme_id": video_id, "filter_reason": reason, "notice": "SECRET"},
    }).encode()
    response = {"request_id": "restriction", "url": "https://www.douyin.com/aweme/v1/web/aweme/detail/?signature=SECRET",
                "status": 200, "headers": {"content-type": "application/json"}}
    responses = [response]
    if lost_body:
        responses.insert(0, {**response, "request_id": "lost"})
    monkeypatch.setattr(FakeHiddenTarget, "network_responses", responses)
    monkeypatch.setattr(FakeHiddenTarget, "network_bodies", {"restriction": raw})
    original_body = FakeHiddenTarget.response_body

    def response_body(self, request_id):
        if request_id == "lost":
            raise RuntimeError("body_unavailable")
        return original_body(self, request_id)

    monkeypatch.setattr(FakeHiddenTarget, "response_body", response_body)
    monkeypatch.setattr(platform_browser_module, "_DOUYIN_DETAIL_READ_TIMEOUT_SECONDS", 0.001)


def _restricted_metadata_payload():
    return {
        "finalUrl": VIDEO_URL.replace("/video/", "/note/"), "pageTitle": "相亲 - 抖音",
        "visibleText": "相亲", "observedVideoId": VIDEO_ID,
        "publishedBody": "只剩下元数据，不能冒充原文", "captionSource": "详情页元数据文案片段",
        "videoElements": 1, "mediaUrlCandidates": [],
    }


def test_verification_page_never_starts_playback_even_with_stale_detail(monkeypatch):
    collector = _collector(monkeypatch, {
        **_restricted_metadata_payload(), "visibleText": "请完成安全验证",
        "publishedBody": "旧页面完整文案", "captionSource": "详情页结构化发布文案",
    })
    monkeypatch.setattr(FakeHiddenTarget, "network_responses", [])
    monkeypatch.setattr(FakeHiddenTarget, "network_read_count", 0)
    monkeypatch.setattr(collector, "_transcribe_douyin_media", lambda *_: pytest.fail("no ASR on verification"))
    with pytest.raises(PlatformBrowserError) as caught:
        collector.extract_douyin_detail(VIDEO_URL, transcribe_media=True)
    assert caught.value.code == "verification_required"
    assert "video.play" not in FakeHiddenTarget.expression
    assert FakeHiddenTarget.network_read_count == 0


@pytest.mark.parametrize("lost_body", [False, True])
def test_same_id_restriction_stops_before_playback_asr_or_redirect_retry(monkeypatch, lost_body):
    collector = _collector(monkeypatch, _restricted_metadata_payload())
    _restricted_network_response(monkeypatch, lost_body=lost_body)
    navigations = []
    monkeypatch.setattr(FakeHiddenTarget, "navigate", lambda self, url: navigations.append(url))
    monkeypatch.setattr(collector, "_transcribe_douyin_media", lambda *_: pytest.fail("restricted content must not attempt ASR"))

    with pytest.raises(PlatformBrowserError) as caught:
        collector.extract_douyin_detail(VIDEO_URL, transcribe_media=True)

    assert caught.value.code == "post_unavailable"
    assert str(caught.value) == "平台限制这条内容的可见性，未返回正文"
    assert navigations == [VIDEO_URL]
    assert "video.play" not in FakeHiddenTarget.expression
    assert "SECRET" not in json.dumps(caught.value.diagnostics)
    assert any(item["reason"] == "platform_visibility_restricted"
               for item in caught.value.diagnostics["network_detail_outcomes"])


@pytest.mark.parametrize("reason,video_id", [
    ("status_unknown_future", VIDEO_ID), ("status_audit_self_see", "7450999999999999999"),
])
def test_unknown_or_other_id_filter_is_not_mapped_to_post_unavailable(monkeypatch, reason, video_id):
    collector = _collector(monkeypatch, {**_restricted_metadata_payload(), "videoElements": 0})
    _restricted_network_response(monkeypatch, reason=reason, video_id=video_id)
    monkeypatch.setattr(collector, "_transcribe_douyin_media", lambda *_: {"asr_status": "MEDIA_UNAVAILABLE"})

    result = collector.extract_douyin_detail(VIDEO_URL)

    assert result["status"] == "PARTIAL"
    assert result["published_body_source"] == "详情页元数据文案片段"
    outcome = result["diagnostics"]["network_detail_outcomes"][0]
    assert outcome["reason"] != "platform_visibility_restricted"
    if video_id == VIDEO_ID:
        assert outcome["filter_reason"] == reason


def test_restriction_is_persisted_without_erasing_previously_verified_caption(monkeypatch, tmp_path):
    from backend.app.db import Database
    from backend.app.douyin_extractions import DouyinExtractionService

    original = "  此前核验的完整原文\n段落和空格保留。  "
    collector = _collector(monkeypatch, {
        **_restricted_metadata_payload(), "publishedBody": original, "captionSource": "详情页结构化发布文案",
    })
    monkeypatch.setattr(FakeHiddenTarget, "network_responses", [])
    db = Database(tmp_path / "restriction-preserves-caption.sqlite3")
    db.initialize()
    workspace = db.create_workspace("限制响应不覆盖完整正文")
    service = DouyinExtractionService(db=db, collector=collector)
    first = service.extract(workspace["id"], VIDEO_URL)
    assert first["status"] == "COMPLETE"

    monkeypatch.setattr(FakeHiddenTarget, "payload", _restricted_metadata_payload())
    _restricted_network_response(monkeypatch)
    monkeypatch.setattr(collector, "_transcribe_douyin_media", lambda *_: pytest.fail("restricted content must not attempt ASR"))
    assert service.enqueue(workspace["id"], VIDEO_URL, force_retry=True)
    result = service.extract(workspace["id"], VIDEO_URL)

    assert result["status"] == "POST_UNAVAILABLE"
    assert result["published_body"] == original
    assert result["published_body_source"] == first["published_body_source"]
    assert result["extracted_at"] == first["extracted_at"]
    assert result["diagnostics"]["copy_completeness"] == "verified_structured_caption"
    latest = result["diagnostics"]["last_attempt_diagnostics"]
    assert latest["error_code"] == "post_unavailable"
    assert latest["network_detail_outcomes"][0]["filter_reason"] == "status_audit_self_see"
    assert "SECRET" not in json.dumps(service.get(workspace["id"], VIDEO_URL))


def test_detail_reader_waits_for_caption_hydration_instead_of_stopping_at_document_title(
    monkeypatch: pytest.MonkeyPatch,
):
    shell = {
        "finalUrl": VIDEO_URL,
        "pageTitle": "抖音",
        "visibleText": "页面加载中",
        # The current URL can reveal the target ID before page data arrives.
        "observedVideoId": VIDEO_ID,
        "publishedBody": "",
        "captionSource": "",
    }
    hydrated = {
        **shell,
        "pageTitle": "同城相亲的三个细节",
        "visibleText": "同城相亲的三个细节 完整发布文案",
        "title": "同城相亲的三个细节",
        "author": "小豆子",
        "publishedBody": "详情页加载完成后的完整发布文案。 #相亲",
        "captionSource": "详情页结构化发布文案",
        "metrics": {"likes": 128},
    }

    class SequencedTarget(SequencedFakeHiddenTarget):
        payloads = [shell, hydrated]
        evaluate_count = 0

    collector = _collector(monkeypatch, shell)
    monkeypatch.setattr(platform_browser_module, "_HiddenCDPTarget", SequencedTarget)

    result = collector.extract_douyin_detail(VIDEO_URL)

    assert SequencedTarget.evaluate_count == 2
    assert result["status"] == "COMPLETE"
    assert result["published_body"] == "详情页加载完成后的完整发布文案。 #相亲"
    assert result["published_body_source"] == "详情页结构化发布文案"


def test_search_candidate_title_is_never_promoted_to_published_body(monkeypatch: pytest.MonkeyPatch):
    collector = _collector(monkeypatch, {
        "finalUrl": VIDEO_URL,
        "pageTitle": "抖音",
        "visibleText": "页面加载中",
        "observedVideoId": VIDEO_ID,
        "pageHasDetailContent": False,
        "publishedBody": "",
        "captionSource": "",
    })
    monkeypatch.setattr(platform_browser_module, "_DOUYIN_DETAIL_READ_TIMEOUT_SECONDS", 0.01)

    result = collector.extract_douyin_detail(VIDEO_URL, title_hint="搜索结果卡片里的一段摘要，不是发布文案")

    assert result["status"] == "FAILED"
    assert result["published_body"] == ""
    assert result["published_body_source"] == ""
    assert result["title"] == "搜索结果卡片里的一段摘要，不是发布文案"


def test_detail_reader_does_not_treat_requested_url_as_observed_video_identity(monkeypatch: pytest.MonkeyPatch):
    collector = _collector(monkeypatch, {
        "finalUrl": VIDEO_URL,
        "pageTitle": "",
        "visibleText": "",
        "observedVideoId": "",
        "pageHasDetailContent": False,
        "publishedBody": "",
        "captionSource": "",
    })
    monkeypatch.setattr(platform_browser_module, "_DOUYIN_DETAIL_READ_TIMEOUT_SECONDS", 0.01)

    with pytest.raises(PlatformBrowserError) as caught:
        collector.extract_douyin_detail(VIDEO_URL, title_hint="搜索标题")

    assert caught.value.code == "detail_not_loaded"
    assert caught.value.diagnostics["target_video_id"] == VIDEO_ID
    assert caught.value.diagnostics["page_has_detail_content"] is False


def test_redirected_note_route_metadata_remains_a_partial_caption(monkeypatch: pytest.MonkeyPatch):
    collector = _collector(monkeypatch, {
        "finalUrl": f"https://www.douyin.com/note/{VIDEO_ID}",
        "pageTitle": "真实作品标题 - 抖音",
        "visibleText": "真实作品标题",
        "observedVideoId": VIDEO_ID,
        "pageHasDetailContent": True,
        "title": "真实作品标题",
        "publishedBody": "详情页元数据中的文案片段 #测试",
        "captionSource": "详情页元数据文案片段",
    })

    result = collector.extract_douyin_detail(VIDEO_URL)

    assert result["video_id"] == result["observed_video_id"] == VIDEO_ID
    assert result["status"] == "PARTIAL"
    assert result["published_body"] == "详情页元数据中的文案片段 #测试"
    assert result["published_body_source"] == "详情页元数据文案片段"
    assert result["failure_stage"] == "caption_not_verified_complete"
    assert result["diagnostics"]["copy_completeness"] == "detail_metadata_text_only"


def test_detail_reader_rejects_wrong_video_identity_even_if_caption_looks_valid(monkeypatch: pytest.MonkeyPatch):
    collector = _collector(monkeypatch, {
        "finalUrl": VIDEO_URL,
        "pageTitle": "同名视频",
        "visibleText": "同名视频",
        "observedVideoId": "7450999999999999999",
        "publishedBody": "错误视频的文案",
        "captionSource": "详情页结构化发布文案",
    })

    with pytest.raises(PlatformBrowserError) as caught:
        collector.extract_douyin_detail(VIDEO_URL)

    assert caught.value.code == "identity_mismatch"
    assert caught.value.diagnostics["target_video_id"] == VIDEO_ID


def test_detail_reader_transcribes_allowlisted_logged_in_media_locally(monkeypatch: pytest.MonkeyPatch):
    collector = _collector(monkeypatch, {
        "finalUrl": VIDEO_URL,
        "pageTitle": "视频标题",
        "visibleText": "视频标题",
        "observedVideoId": VIDEO_ID,
        "title": "视频标题",
        "publishedBody": "",
        "platformSubtitle": "",
        "videoElements": 1,
        "mediaUrlCandidates": ["https://v3-web.douyinvod.com/video.mp4?sign=secret"],
    })
    monkeypatch.setattr(platform_browser_module, "local_asr_status", lambda: {"available": True, "installed": True})
    monkeypatch.setattr(platform_browser_module, "transcribe_media_path", lambda path, **kwargs: {
        "text": "本机识别的完整口播。", "language": "zh-CN", "engine": "Apple SpeechAnalyzer",
        "model": "SpeechTranscriber zh-CN", "source": kwargs["source"], "message": kwargs["message"],
        "media_sha256": "b" * 64, "media_bytes": Path(path).stat().st_size, "duration_seconds": 5.0,
        "segments": [{"text": "本机识别的完整口播。", "start_seconds": 0.1, "end_seconds": 4.9, "confidence": 0.88}],
        "confidence": {"mean": 0.88, "minimum": 0.88, "maximum": 0.88, "assessed_segment_count": 1, "low_confidence_threshold": 0.65},
        "low_confidence": [],
    })
    progress: list[dict[str, object]] = []

    result = collector.extract_douyin_detail(VIDEO_URL, progress_callback=progress.append)

    assert result["status"] == "PARTIAL"
    assert result["asr_status"] == "BROWSER_MEDIA_TRANSCRIBED"
    assert result["asr_source"] == "BROWSER_LOGGED_IN_MEDIA"
    assert result["asr_text"] == "本机识别的完整口播。"
    assert result["asr_segments"][0]["start_seconds"] == 0.1
    assert result["asr_confidence"]["mean"] == 0.88
    assert result["asr_low_confidence"] == []
    assert result["video_status"] == "TRANSIENT_MEDIA_PROCESSED"
    assert result["diagnostics"]["media_policy"] == "temporary_local_asr_no_archive"
    assert "secret" not in str(result["diagnostics"])
    assert progress and progress[0]["asr_status"] == "PROCESSING"


def test_cdp_media_stream_is_written_to_temp_file_with_a_hard_byte_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    target = _HiddenCDPTarget("ws://unused")
    body = b"short-video-media"
    read_count = 0

    def fake_call(method: str, params=None):
        nonlocal read_count
        if method == "Page.getFrameTree":
            return {"result": {"frameTree": {"frame": {"id": "frame-1"}}}}
        if method == "Network.loadNetworkResource":
            return {"result": {"resource": {
                "success": True, "httpStatusCode": 200,
                "headers": {"content-type": "video/mp4", "content-length": str(len(body))},
                "stream": "stream-1",
            }}}
        if method == "IO.read":
            read_count += 1
            return {"result": {"data": base64.b64encode(body).decode(), "base64Encoded": True, "eof": True}}
        if method == "IO.close":
            return {"result": {}}
        raise AssertionError(method)

    monkeypatch.setattr(target, "call", fake_call)
    media = tmp_path / "media.mp4"

    response = target.load_network_resource_to_file("https://v3-web.douyinvod.com/video.mp4?sig=x", media)

    assert response["success"] is True
    assert response["bytes_written"] == len(body)
    assert media.read_bytes() == body
    assert read_count == 1

    too_small = tmp_path / "too-small.mp4"
    with pytest.raises(RuntimeError, match="processing limit"):
        target.load_network_resource_to_file("https://v3-web.douyinvod.com/video.mp4?sig=x", too_small, max_bytes=4)
    assert not too_small.exists()
