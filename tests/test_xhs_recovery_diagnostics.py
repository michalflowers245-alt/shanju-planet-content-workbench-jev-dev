"""Offline coverage for same-ID link recovery and content-free diagnostics."""
import json
from types import SimpleNamespace

import pytest

from backend.app import platform_browser as browser
from backend.app.xhs_archive import _safe_failure_diagnostics


TARGET = "6a9859ea00000000290199cf"
OTHER = "6a9859ea00000000290199ca"
BASE = "https://www.xiaohongshu.com"


def row(post_id, *, signed=False, title="不同标题"):
    path = "search_result" if signed else "explore"
    suffix = "?xsec_token=synthetic-private-token&xsec_source=pc_search" if signed else ""
    return {"href": f"{BASE}/{path}/{post_id}{suffix}", "title": title}


@pytest.mark.parametrize("signed_first", [False, True])
def test_same_id_existing_access_link_wins_regardless_of_order(signed_first):
    rows = [row(TARGET), row(TARGET, signed=True)]
    if signed_first:
        rows.reverse()
    chosen = browser._best_xhs_search_result(rows, TARGET, "")
    assert chosen == f"{BASE}/explore/{TARGET}?xsec_token=synthetic-private-token&xsec_source=pc_search"


def test_wrong_id_access_link_never_supplies_token_to_target():
    chosen = browser._best_xhs_search_result([row(OTHER, signed=True), row(TARGET)], TARGET, "不同标题")
    assert chosen == f"{BASE}/explore/{TARGET}"
    assert browser._best_xhs_search_result([row(OTHER, signed=True)], TARGET, "不同标题") == ""


def test_tokenless_same_id_remains_fallback_without_inventing_token():
    assert browser._best_xhs_search_result([row(TARGET)], TARGET, "") == f"{BASE}/explore/{TARGET}"


@pytest.mark.parametrize("url", [
    f"https://www.xiaohongshu.com.evil.test/explore/{TARGET}?xsec_token=bad",
    f"https://username:password@www.xiaohongshu.com/explore/{TARGET}?xsec_token=bad",
    f"https://www.xiaohongshu.com:9999/explore/{TARGET}?xsec_token=bad",
    f"https://www.xiaohongshu.com/explore/{TARGET}/other-post?xsec_token=bad",
])
def test_nonordinary_access_links_cannot_replace_legal_fallback(url):
    chosen = browser._best_xhs_search_result([{"href": url}, row(TARGET)], TARGET, "")
    assert chosen == f"{BASE}/explore/{TARGET}"


class SnapshotPage:
    def __init__(self, value):
        self.value = value

    def evaluate(self, _expression):
        return self.value

    def wait_for_timeout(self, _milliseconds):
        pass


def test_recovery_diagnostics_count_unique_ids_without_copy_or_urls():
    page = SnapshotPage({
        "rows": [row(OTHER), row(TARGET), row(TARGET, signed=True)],
        "bodyText": "不应进入诊断的文案",
    })
    result = browser._wait_for_xhs_recovery_search(page, TARGET)
    assert result["recovery_diagnostics"] == {
        "recovery_result_count": 3,
        "recovery_unique_id_count": 2,
        "recovery_stop_reason": "exact_id_found",
        "exact_id_found": True,
    }
    serialized = json.dumps(result["recovery_diagnostics"], ensure_ascii=False)
    for private in ("synthetic-private-token", "不应进入", TARGET, OTHER, "https://"):
        assert private not in serialized


@pytest.mark.parametrize("value,seconds,reason", [
    ({"rows": [row(OTHER)]}, 5, "stable_results"),
    ({"rows": []}, 0, "timeout"),
    ({"rows": [row(OTHER)], "accessHints": "安全验证"}, 5, "verification_required"),
    (None, 5, "invalid_snapshot"),
])
def test_recovery_diagnostics_distinguish_bounded_stop_reasons(value, seconds, reason):
    diagnostics = browser._wait_for_xhs_recovery_search(SnapshotPage(value), TARGET, timeout_seconds=seconds)["recovery_diagnostics"]
    assert diagnostics["recovery_stop_reason"] == reason
    assert diagnostics["exact_id_found"] is False


def fake_collector(monkeypatch, tmp_path, rows, *, gated=False):
    class BackgroundPage:
        def __init__(self, _websocket):
            self.context = SimpleNamespace()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def set_budget(self, *_args):
            pass

        def on(self, *_args):
            pass

        def goto(self, *_args, **_kwargs):
            pass

        def wait_for_timeout(self, *_args):
            pass

        def wait_for_selector(self, *_args, **_kwargs):
            pass

        def evaluate(self, expression, *_args):
            if "section.note-item" in expression:
                return {"rows": rows, "url": f"{BASE}/search_result/", "accessHints": "安全验证" if gated else ""}
            return {"url": f"{BASE}/404?error_code=300031", "title": "内容不存在", "text": "内容不存在"}

        def get_by_text(self, *_args):
            return SimpleNamespace(all=lambda: [])

        def close(self):
            pass

    collector = browser.PlatformBrowserCollector(profile_dir=tmp_path / "profile")
    monkeypatch.setattr(collector, "status", lambda: {"running": True})
    monkeypatch.setattr(collector, "_browser_websocket_url", lambda: "ws://offline.test")
    monkeypatch.setattr(browser, "_BackgroundPage", BackgroundPage)
    wait = browser._wait_for_xhs_recovery_search
    monkeypatch.setattr(browser, "_wait_for_xhs_recovery_search", lambda page, target: wait(page, target, timeout_seconds=0))
    return collector


@pytest.mark.parametrize("similar_title", [False, True])
def test_archive_recovery_failure_propagates_only_safe_finite_search_diagnostics(monkeypatch, tmp_path, similar_title):
    rows = [row(OTHER, signed=True, title="原始标题" if similar_title else "ZZZZ")]
    collector = fake_collector(monkeypatch, tmp_path, rows)
    with pytest.raises(browser.PlatformBrowserError) as failure:
        collector.archive_xiaohongshu_detail(f"{BASE}/explore/{TARGET}", title_hint="原始标题", search_keyword="原查询")
    error = failure.value
    assert error.code == ("identity_mismatch" if similar_title else "access_url_recovery_failed")
    if not similar_title:
        assert "有限搜索" in str(error)
        assert "尚不能判断原帖是否已删除" in str(error)
    assert error.diagnostics["recovery_query_count"] == 2
    assert error.diagnostics["recovery_result_count"] == error.diagnostics["recovery_unique_id_count"] == 1
    assert error.diagnostics["recovery_stop_reason"] == "timeout"
    assert error.diagnostics["exact_id_found"] is False
    assert _safe_failure_diagnostics(error.diagnostics) == error.diagnostics
    serialized = json.dumps(error.diagnostics, ensure_ascii=False)
    for private in ("synthetic-private-token", "原始标题", "原查询", "xsec_token"):
        assert private not in serialized


def test_successful_search_caches_each_existing_access_link_under_its_own_id(monkeypatch, tmp_path):
    collector = fake_collector(monkeypatch, tmp_path, [row(TARGET, signed=True), row(OTHER, signed=True)])

    def stop_before_body(*_args, **_kwargs):
        raise browser.PlatformBrowserError("offline test boundary", code="test_boundary")

    monkeypatch.setattr(browser, "_wait_for_xhs_detail_hydration", stop_before_body)
    with pytest.raises(browser.PlatformBrowserError, match="offline test boundary"):
        collector.archive_xiaohongshu_detail(f"{BASE}/explore/{TARGET}", title_hint="原始标题")
    for post_id in (TARGET, OTHER):
        cached = collector._recent_xhs_access_url(f"{BASE}/explore/{post_id}")
        assert f"/explore/{post_id}?" in cached
        assert "xsec_token=synthetic-private-token" in cached
    assert not (tmp_path / "profile").exists()


def test_gated_search_never_populates_access_link_cache(monkeypatch, tmp_path):
    collector = fake_collector(monkeypatch, tmp_path, [row(TARGET, signed=True), row(OTHER, signed=True)], gated=True)
    with pytest.raises(browser.PlatformBrowserError) as failure:
        collector.archive_xiaohongshu_detail(f"{BASE}/explore/{TARGET}", title_hint="原始标题")
    assert failure.value.code == "verification_required"
    assert collector._xhs_access_urls == {}


@pytest.mark.parametrize("malformed", ["a private arbitrary reason", {"token": "secret"}, ["secret"], 7])
def test_unknown_recovery_stop_reasons_are_not_persisted(malformed):
    assert _safe_failure_diagnostics({"recovery_stop_reason": malformed}) == {}
