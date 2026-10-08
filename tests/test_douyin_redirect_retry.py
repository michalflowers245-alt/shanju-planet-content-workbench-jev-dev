import json

import pytest

from backend.app import platform_browser as module


POST_ID = "7464004539922877759"
URL = f"https://www.douyin.com/video/{POST_ID}"
NOTE = f"https://www.douyin.com/note/{POST_ID}"


class Target:
    def __init__(self, payload, *, after_retry=None):
        self.payload = payload
        self.after_retry = after_retry or payload
        self.visits = []
        self.body_reads = 0

    def __enter__(self): return self
    def __exit__(self, *_args): pass
    def enable_network(self, **_kwargs): pass
    def navigate(self, url): self.visits.append(url)
    def evaluate(self, _expr): return self.after_retry if len(self.visits) > 1 else self.payload

    def take_network_responses(self):
        return [{"request_id": "detail", "url": "https://www.douyin.com/aweme/v1/web/aweme/detail/",
                 "status": 200, "headers": {"content-type": "application/json"}}]

    def response_body(self, _request):
        self.body_reads += 1
        if len(self.visits) == 1:
            raise RuntimeError("Renderer response buffer was lost")
        return json.dumps({"aweme_detail": {"aweme_id": POST_ID, "desc": "  完整图文正文\n"}}, ensure_ascii=False).encode()


def setup(monkeypatch, *, after_retry=None, final_url=NOTE):
    target = Target({"finalUrl": final_url, "observedVideoId": POST_ID,
                     "publishedBody": "网页元数据摘要", "captionSource": "详情页元数据文案片段"},
                    after_retry=after_retry)
    collector = module.PlatformBrowserCollector()
    monkeypatch.setattr(collector, "status", lambda: {"running": True})
    monkeypatch.setattr(collector, "_browser_websocket_url", lambda: "ws://test")
    monkeypatch.setattr(module, "_HiddenCDPTarget", lambda *_args: target)
    monkeypatch.setattr(module, "_DOUYIN_DETAIL_READ_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(module, "_DOUYIN_REDIRECT_RETRY_WAIT_SECONDS", 0.01)
    monkeypatch.setattr(collector, "_transcribe_douyin_media", lambda *_args: {})
    return collector, target


def test_lost_note_response_reopens_only_observed_same_id_route_once(monkeypatch):
    collector, target = setup(monkeypatch)
    result = collector.extract_douyin_detail(URL)
    assert target.visits == [URL, NOTE]
    assert target.body_reads == 2
    assert result["status"] == "COMPLETE"
    assert result["published_body"] == "  完整图文正文\n"
    assert result["diagnostics"]["redirect_body_retry_count"] == 1


@pytest.mark.parametrize("data,error", [
    ({"finalUrl": NOTE, "visibleText": "请完成安全验证", "observedVideoId": POST_ID}, "verification_required"),
    ({"finalUrl": "https://www.douyin.com/note/999", "observedVideoId": "999"}, "identity_mismatch"),
])
def test_retry_stops_at_challenge_or_different_post(monkeypatch, data, error):
    collector, target = setup(monkeypatch, after_retry=data)
    with pytest.raises(module.PlatformBrowserError) as caught:
        collector.extract_douyin_detail(URL)
    assert caught.value.code == error
    assert target.body_reads == 1


def test_no_speculative_note_retry_for_video_route(monkeypatch):
    collector, target = setup(monkeypatch, final_url=URL)
    result = collector.extract_douyin_detail(URL)
    assert target.visits == [URL]
    assert result["status"] == "PARTIAL"
    assert result["diagnostics"]["redirect_body_retry_count"] == 0
