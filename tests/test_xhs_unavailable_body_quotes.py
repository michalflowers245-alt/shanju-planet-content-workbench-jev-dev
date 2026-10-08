"""Offline checks: a quoted deletion phrase is not a deleted note.

The evidence probe runs its production JavaScript in Node, without any browser,
network, login state, or production database.
"""

from __future__ import annotations

import json
import ast
import inspect
import shutil
import subprocess
import textwrap

import pytest

import backend.app.platform_browser as browser


POST_ID = "68c6f07d000000001d01abcd"
POST_URL = f"https://www.xiaohongshu.com/explore/{POST_ID}"
RAW_BODY = "第一段原文。\n\n聊天记录显示‘已删除’，不代表这个人不值得交往。\n#关系"


@pytest.fixture(scope="module")
def node_executable():
    executable = shutil.which("node")
    if not executable:
        pytest.skip("Node.js is needed to exercise the production evidence probe offline")
    return executable


class EvidencePage:
    def __init__(self, node_executable, **overrides):
        self.node_executable = node_executable
        self.calls = 0
        self.input = {
            "expression": browser._XHS_VISIBLE_BODY_QUOTE_EXPRESSION,
            "targetId": POST_ID,
            "url": POST_URL,
            "title": "关系里的边界 - 小红书",
            "state": {"noteId": POST_ID, "desc": RAW_BODY},
            "renderedBody": RAW_BODY,
            "pageText": f"作者\n{RAW_BODY}\n发布于湖南",
            **overrides,
        }

    def evaluate(self, expression, target):
        assert expression == browser._XHS_VISIBLE_BODY_QUOTE_EXPRESSION
        assert target == POST_ID
        self.calls += 1
        harness = r"""
const fs = require('node:fs'), vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const ancestor = input.ancestorStyle ? {
  hidden: false, parentElement: null, getAttribute: () => null, style: input.ancestorStyle,
} : null;
const node = {
  innerText: input.renderedBody,
  textContent: input.renderedBody,
  hidden: input.hidden || false,
  parentElement: ancestor,
  style: input.style || {},
  getAttribute: name => name === 'aria-hidden' && input.ariaHidden ? 'true' : null,
  getClientRects: () => input.noRect ? [] : [{width: 320, height: 100}],
};
const document = {
  title: input.title,
  body: {innerText: input.pageText},
  querySelectorAll: selector => selector === '#detail-desc' && !input.missingNode ? [node] : [],
};
const context = vm.createContext({document, window: {__INITIAL_STATE__: input.state},
  location: new URL(input.url),
  getComputedStyle: node => ({display: 'block', visibility: 'visible', opacity: '1', ...node.style}),
});
const script = '(' + input.expression + ')(' + JSON.stringify(input.targetId) + ')';
process.stdout.write(JSON.stringify(vm.runInContext(script, context, {timeout: 1000})));
"""
        result = subprocess.run(
            [self.node_executable, "-e", harness], input=json.dumps(self.input, ensure_ascii=False),
            text=True, capture_output=True, timeout=5, check=True,
        )
        return json.loads(result.stdout)


def snapshot(**overrides):
    return {"url": POST_URL, "title": "关系里的边界 - 小红书", "text": RAW_BODY, **overrides}


def test_quote_override_is_wired_only_into_both_archive_gates():
    def calls(method):
        tree = ast.parse(textwrap.dedent(inspect.getsource(method)))
        return [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name) and node.func.id == "_xhs_unavailable_is_body_quote"]

    archive_calls = calls(browser.PlatformBrowserCollector.archive_xiaohongshu_detail)
    assert len(archive_calls) == 2
    assert {call.args[2].id for call in archive_calls} == {"initial", "data"}
    assert all(call.args[1].id == "target_post_id" for call in archive_calls)
    assert calls(browser.PlatformBrowserCollector.extract_xiaohongshu_detail) == []


def test_verified_same_note_body_quote_is_not_an_unavailable_page(node_executable):
    page = EvidencePage(node_executable)
    assert browser._page_access_state(RAW_BODY) == "post_unavailable", "generic gate stays unchanged"
    assert browser._xhs_unavailable_is_body_quote(page, POST_ID, snapshot()) is True
    assert page.calls == 1


@pytest.mark.parametrize("overrides", [
    {"missingNode": True},
    {"noRect": True},
    {"hidden": True},
    {"ariaHidden": True},
    {"style": {"visibility": "hidden"}},
    {"ancestorStyle": {"opacity": "0"}},
    {"state": {"noteId": "68c6f07d000000001d01eeee", "desc": RAW_BODY}},
    {"state": {"id": POST_ID, "desc": RAW_BODY}},  # A comment/user id is not note identity.
    {"state": {"noteId": POST_ID, "description": RAW_BODY}},  # Metadata is not full-body proof.
    {"renderedBody": "只显示了正文的开头"},
    {"pageText": "当前笔记暂时无法浏览"},  # Hidden old state/node text absent from visible page.
])
def test_stale_hidden_mismatched_or_weak_body_cannot_clear_error(node_executable, overrides):
    page = EvidencePage(node_executable, **overrides)
    assert browser._xhs_unavailable_is_body_quote(page, POST_ID, snapshot()) is False


@pytest.mark.parametrize("marker", ["笔记已删除", "安全验证", "请先登录", "访问过于频繁"])
def test_error_or_gate_outside_matching_body_still_blocks(node_executable, marker):
    page = EvidencePage(node_executable, pageText=f"{RAW_BODY}\n{marker}")
    assert browser._xhs_unavailable_is_body_quote(page, POST_ID, snapshot()) is False


@pytest.mark.parametrize("values", [
    {"url": "https://www.xiaohongshu.com/404?error_code=300031"},
    {"url": f"{POST_URL}?error_code=300031"},
    {"url": f"https://www.xiaohongshu.com/search_result/{POST_ID}"},
    {"url": "https://www.xiaohongshu.com/explore/68c6f07d000000001d01eeee"},
    {"title": "笔记已删除"},
])
def test_error_route_title_or_different_detail_id_never_uses_state_as_override(node_executable, values):
    page = EvidencePage(node_executable)
    assert browser._xhs_unavailable_is_body_quote(page, POST_ID, snapshot(**values)) is False
    assert page.calls == 0


def test_probe_navigation_change_cannot_clear_previous_snapshot_error(node_executable):
    page = EvidencePage(node_executable, url="https://www.xiaohongshu.com/404")
    assert browser._xhs_unavailable_is_body_quote(page, POST_ID, snapshot()) is False


def archive_collector(tmp_path, monkeypatch, node_executable, *, initial=None, final=None, evidence=None):
    class EmptyLocators:
        def all(self):
            return []

    class OfflinePage:
        def __init__(self, _url):
            self.context = object()
            self.visited = []
            self.probe = EvidencePage(node_executable, **(evidence or {}))

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

        def on(self, *_args):
            pass

        def goto(self, url, **_kwargs):
            self.visited.append(url)
            assert url == POST_URL, "a live matching body must not initiate recovery search"

        def wait_for_timeout(self, *_args):
            pass

        def get_by_text(self, *_args):
            return EmptyLocators()

        def evaluate(self, expression, target=None):
            if expression == browser._XHS_VISIBLE_BODY_QUOTE_EXPRESSION:
                return self.probe.evaluate(expression, target)
            if expression == browser._XHS_DETAIL_READINESS_EXPRESSION:
                return {"structuredBodyReady": True, "renderedBodyLength": len(RAW_BODY)}
            if "exactCandidates" not in expression:
                return initial or snapshot()
            return {
                "bodyRaw": RAW_BODY, "bodySource": "embedded_state", "bodyVerified": True,
                "bodyRich": [], "richAlignment": "EXACT", "selectedBodyField": "desc",
                "selectedStateScore": 120, "stateCandidateCount": 1,
                "observedId": POST_ID, "statePostId": POST_ID, "detailUrlPostId": POST_ID,
                "expectedImageCount": 0, "images": [], "coverUrl": "",
                "orderedBy": "page_state", "title": "关系里的边界", "author": "作者",
                "canonical": POST_URL, "videoCount": 0, "finalUrl": POST_URL,
                "pageTitle": "关系里的边界 - 小红书", "visibleText": RAW_BODY,
                **(final or {}),
            }

        def close(self):
            pass

    page = OfflinePage("unused")
    collector = browser.PlatformBrowserCollector(profile_dir=tmp_path / "profile")
    monkeypatch.setattr(collector, "status", lambda: {"running": True})
    monkeypatch.setattr(collector, "_browser_websocket_url", lambda: "ws://unused")
    monkeypatch.setattr(browser, "_BackgroundPage", lambda _url: page)
    monkeypatch.setattr(collector, "_xhs_access_error", lambda code, **kwargs: browser.PlatformBrowserError("gate", code=code))
    return collector, page


@pytest.mark.parametrize("initial", [None, snapshot(text="普通首页文字")])
def test_archive_keeps_exact_body_when_initial_or_final_snapshot_quotes_deleted(
    tmp_path, monkeypatch, node_executable, initial,
):
    collector, page = archive_collector(tmp_path, monkeypatch, node_executable, initial=initial)
    result = collector.archive_xiaohongshu_detail(POST_URL)
    assert result["body_raw"] == RAW_BODY
    assert result["body_verified"] is True
    assert result["observed_post_id"] == POST_ID
    assert page.visited == [POST_URL]
    assert page.probe.calls == (2 if initial is None else 1)


@pytest.mark.parametrize("phase", ["initial", "final"])
@pytest.mark.parametrize("marker,code", [
    ("请先登录", "login_required"), ("安全验证", "verification_required"), ("访问过于频繁", "platform_limited"),
])
def test_archive_session_gates_win_even_with_visible_verified_body(
    tmp_path, monkeypatch, node_executable, phase, marker, code,
):
    initial = snapshot(text=f"{RAW_BODY}\n{marker}") if phase == "initial" else snapshot(text="普通正文")
    final = {"visibleText": f"{RAW_BODY}\n{marker}"} if phase == "final" else None
    collector, page = archive_collector(tmp_path, monkeypatch, node_executable, initial=initial, final=final)
    with pytest.raises(browser.PlatformBrowserError) as caught:
        collector.archive_xiaohongshu_detail(POST_URL)
    assert caught.value.code == code
    assert page.probe.calls == 0


def test_archive_final_real_error_does_not_accept_hidden_old_state(tmp_path, monkeypatch, node_executable):
    collector, _page = archive_collector(
        tmp_path, monkeypatch, node_executable, initial=snapshot(text="正在加载"),
        final={"visibleText": "当前笔记暂时无法浏览"},
        evidence={"hidden": True, "pageText": "当前笔记暂时无法浏览"},
    )
    with pytest.raises(browser.PlatformBrowserError) as caught:
        collector.archive_xiaohongshu_detail(POST_URL)
    assert caught.value.code == "post_unavailable"


@pytest.mark.parametrize("phase,code", [
    ("initial", "access_url_recovery_failed"), ("final", "post_unavailable"),
])
def test_archive_404_route_stays_unavailable_despite_matching_old_body(
    tmp_path, monkeypatch, node_executable, phase, code,
):
    error_url = "https://www.xiaohongshu.com/404?error_code=300031"
    initial = snapshot(url=error_url, text="内容不存在") if phase == "initial" else snapshot(text="正在加载")
    final = {"finalUrl": error_url, "visibleText": "内容不存在"} if phase == "final" else None
    collector, page = archive_collector(tmp_path, monkeypatch, node_executable, initial=initial, final=final)
    with pytest.raises(browser.PlatformBrowserError) as caught:
        collector.archive_xiaohongshu_detail(POST_URL)
    assert caught.value.code == code
    assert page.probe.calls == 0
