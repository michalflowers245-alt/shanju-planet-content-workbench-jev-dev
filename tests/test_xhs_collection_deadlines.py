from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import backend.app.platform_browser as browser
from backend.app.db import Database, utc_now
from backend.app.platform_browser import PlatformBrowserError
from backend.app.xhs_archive import XiaohongshuArchiveRepository, XiaohongshuArchiveService


POST_ID = "68c6f07d000000001d01abcd"
POST_URL = f"https://www.xiaohongshu.com/explore/{POST_ID}"


def test_timed_out_collector_is_cancelled_and_cannot_stack_more_threads(tmp_path: Path):
    entered = threading.Event()
    cancelled = threading.Event()
    release = threading.Event()
    calls = []

    class StalledCollector:
        def archive_xiaohongshu_detail(self, url, *, deadline=None, cancel_event=None):
            calls.append(url)
            assert deadline is not None
            entered.set()
            if cancel_event.wait(timeout=1):
                cancelled.set()
            release.wait(timeout=2)
            return {"body_raw": "仅供测试"}

    service = XiaohongshuArchiveService(
        db=Database(tmp_path / "unused.sqlite3"), collector=StalledCollector(),
        root=tmp_path / "archives", collector_timeout_seconds=0.05,
    )
    try:
        with pytest.raises(PlatformBrowserError) as first:
            service._collect_with_deadline(POST_URL)
        assert first.value.code == "archive_timeout"
        assert entered.is_set() and cancelled.wait(timeout=1)
        for _ in range(3):
            with pytest.raises(PlatformBrowserError) as duplicate:
                service._collect_with_deadline(POST_URL)
            assert duplicate.value.code == "archive_timeout"
        assert calls == [POST_URL]
    finally:
        release.set()
        service._collector_inflight.join(timeout=2)
    assert not service._collector_inflight.is_alive()


def test_deadline_wrapper_keeps_legacy_collector_signature_compatible(tmp_path: Path):
    class LegacyCollector:
        def archive_xiaohongshu_detail(self, url, *, title_hint=""):
            return {"url": url, "title": title_hint}

    service = XiaohongshuArchiveService(
        db=Database(tmp_path / "unused.sqlite3"), collector=LegacyCollector(), root=tmp_path / "archives",
    )
    assert service._collect_with_deadline(POST_URL, title_hint="测试标题") == {"url": POST_URL, "title": "测试标题"}


def test_cancelled_archive_stops_while_waiting_for_shared_browser_lock(tmp_path: Path, monkeypatch):
    collector = browser.PlatformBrowserCollector(profile_dir=tmp_path / "profile")
    monkeypatch.setattr(collector, "status", lambda: {"running": True})
    monkeypatch.setattr(collector, "_browser_websocket_url", lambda: pytest.fail("timed-out lock must not start CDP"))
    lock = threading.Lock()
    collector._lock = lock
    lock.acquire()
    done = threading.Event()
    errors = []
    cancel_event = threading.Event()

    def collect():
        try:
            collector.archive_xiaohongshu_detail(POST_URL, deadline=time.monotonic() + 5, cancel_event=cancel_event)
        except TimeoutError as exc:
            errors.append(exc)
        finally:
            done.set()

    thread = threading.Thread(target=collect)
    thread.start()
    try:
        cancel_event.set()
        assert done.wait(timeout=1)
        assert errors
        assert lock.locked(), "collector must not release somebody else's browser lock"
    finally:
        lock.release()
        thread.join(timeout=1)


def test_cdp_command_uses_absolute_budget_and_does_not_send_after_expiry(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(browser.time, "monotonic", lambda: clock[0])

    class SilentSocket:
        def __init__(self):
            self.sent = []
            self.timeouts = []

        def send(self, raw):
            self.sent.append(json.loads(raw))

        def recv(self, timeout):
            self.timeouts.append(timeout)
            clock[0] += timeout
            raise TimeoutError("no reply")

    target = browser._HiddenCDPTarget("ws://unused")
    target.socket = SilentSocket()
    target.session_id = "test-session"
    target.deadline = 100.12
    target.cancel_event = threading.Event()
    with pytest.raises(TimeoutError):
        target.call("Network.loadNetworkResource", {"url": "https://image.example.test/a.png"})
    assert clock[0] <= 100.12001
    assert max(target.socket.timeouts) <= 0.12001
    assert len(target.socket.sent) == 1
    with pytest.raises(TimeoutError):
        target.call("Runtime.evaluate", {"expression": "0"})
    assert len(target.socket.sent) == 1


def test_cdp_receive_cancel_exits_without_closing_socket_from_another_thread():
    cancel_event = threading.Event()
    reader_started = threading.Event()
    completed = threading.Event()
    errors = []

    class WaitingSocket:
        def recv(self, timeout):
            reader_started.set()
            cancel_event.wait(timeout)
            raise TimeoutError("no reply")

        def close(self):
            pytest.fail("cancellation must not close a socket from the signalling thread")

    target = browser._HiddenCDPTarget("ws://unused")
    target.socket = WaitingSocket()
    target.cancel_event = cancel_event

    def read():
        try:
            target._read(15)
        except TimeoutError as exc:
            errors.append(exc)
        finally:
            completed.set()

    thread = threading.Thread(target=read)
    thread.start()
    try:
        assert reader_started.wait(timeout=1)
        cancel_event.set()
        assert completed.wait(timeout=1)
        assert errors
    finally:
        cancel_event.set()
        thread.join(timeout=1)


def test_image_deadline_returns_verified_body_and_persists_partial_archive(tmp_path: Path, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(browser.time, "monotonic", lambda: clock[0])
    requests = []
    closed = []
    original_body = "\n  第一行原文。\r\n第二行 #相亲🙂  \n"

    class SlowImages:
        def get(self, url, **_kwargs):
            requests.append(url)
            clock[0] = 102.0
            raise TimeoutError("image transfer budget exhausted")

    class FakePage:
        def __init__(self, _url):
            self.context = SimpleNamespace(request=SlowImages())
            self.evaluations = 0

        def set_budget(self, deadline, cancel_event):
            assert deadline == 101.0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

        def close(self):
            closed.append(True)

        def on(self, *_args):
            pass

        def goto(self, *_args, **_kwargs):
            pass

        def wait_for_timeout(self, *_args):
            pass

        def get_by_text(self, *_args):
            return SimpleNamespace(all=lambda: [])

        def evaluate(self, *_args):
            self.evaluations += 1
            if self.evaluations == 1:
                return {"title": "原帖", "text": "原帖正文", "url": POST_URL}
            return {
                "bodyRaw": original_body, "bodySource": "embedded_state", "bodyVerified": True,
                "bodyRich": [], "richAlignment": "EXACT", "selectedBodyField": "desc",
                "images": [{"ordinal": i, "url": f"https://image.example.test/{i}.png"} for i in range(1, 4)],
                "expectedImageCount": 3, "orderedBy": "page_state", "observedId": POST_ID,
                "statePostId": POST_ID, "detailUrlPostId": POST_ID, "finalUrl": POST_URL,
                "title": "原帖", "pageTitle": "原帖", "visibleText": "原帖正文",
            }

    collector = browser.PlatformBrowserCollector(profile_dir=tmp_path / "profile")
    monkeypatch.setattr(collector, "status", lambda: {"running": True})
    monkeypatch.setattr(collector, "_browser_websocket_url", lambda: "ws://unused")
    monkeypatch.setattr(browser, "_BackgroundPage", FakePage)
    monkeypatch.setattr(browser, "_wait_for_xhs_detail_hydration", lambda *_args: {})
    capture = collector.archive_xiaohongshu_detail(POST_URL, deadline=101.0)
    assert capture["body_raw"] == original_body
    assert capture["body_verified"] is True
    assert capture["diagnostics"]["image_budget_exhausted"] is True
    assert [asset["error_code"] for asset in capture["assets"]] == ["IMAGE_TIMEOUT"] * 3
    assert len(requests) == 1 and closed

    db = Database(tmp_path / "archive.sqlite3")
    db.initialize()
    workspace = db.create_workspace("慢图不丢正文")
    now = utc_now()
    db.execute(
        "INSERT INTO xhs_post_extractions (id,workspace_id,url,post_key,title,status,extracted_at,updated_at) VALUES(?,?,?,?,?,'partial',?,?)",
        ("deadline-test", workspace["id"], POST_URL, POST_ID, "原帖", now, now),
    )
    saved = XiaohongshuArchiveRepository(db, tmp_path / "archives").persist(workspace["id"], "deadline-test", capture)
    assert saved.archive_status == "PARTIAL"
    assert saved.missing_ordinals == [1, 2, 3]
    extraction = db.one("SELECT body_raw,verification_status FROM xhs_post_extractions WHERE id='deadline-test'")
    assert extraction["body_raw"] == original_body
    assert extraction["verification_status"] == "VERIFIED"
