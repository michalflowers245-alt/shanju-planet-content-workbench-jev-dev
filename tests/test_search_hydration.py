import pytest

from backend.app import platform_browser as module
from backend.app.platform_browser import PlatformBrowserCollector, PlatformBrowserError


@pytest.mark.parametrize("platform", ["xiaohongshu", "douyin"])
def test_search_waits_for_later_cards_instead_of_returning_first_partial_paint(monkeypatch, platform):
    counts = [1, 1, 2, 3]
    calls = []

    class Target:
        def __init__(self, url): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def navigate(self, url): pass
        def evaluate(self, expression):
            count = counts[min(len(calls), len(counts) - 1)]
            calls.append(count)
            return {"rows": [{"href": f"https://example.test/{i}", "title": f"标题{i}"} for i in range(count)]}

    collector = PlatformBrowserCollector()
    monkeypatch.setattr(collector, "_browser_websocket_url", lambda: "ws://test")
    monkeypatch.setattr(module, "_HiddenCDPTarget", Target)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    result = collector._hidden_search_snapshot(platform, "同城社交", 3)
    assert len(result["rows"]) == 3
    assert calls == [1, 1, 2, 3]


def test_search_stops_on_verification_even_if_earlier_cards_had_loaded(monkeypatch):
    calls = []

    class Target:
        def __init__(self, url): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def navigate(self, url): pass
        def evaluate(self, expression):
            calls.append(1)
            return {"rows": [{"href": "https://example.test/1", "title": "标题"}], "accessHints": "安全验证" if len(calls) > 1 else ""}

    collector = PlatformBrowserCollector()
    monkeypatch.setattr(collector, "_browser_websocket_url", lambda: "ws://test")
    monkeypatch.setattr(module, "_HiddenCDPTarget", Target)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    with pytest.raises(PlatformBrowserError) as error:
        collector._hidden_search_snapshot("douyin", "同城社交", 3)
    assert error.value.code == "verification_required"
    assert len(calls) == 2
