"""Execute the shipped browser parsers against isolated, offline page state.

These tests run the actual JavaScript embedded in platform_browser.py, rather
than returning a canned parser result from a mocked page.evaluate call.  Node's
VM receives a minimal document and no browser, network, cookies, or user data.
"""

from __future__ import annotations

import ast
import inspect
import json
import shutil
import subprocess
import textwrap
from typing import Any

import pytest

from backend.app.platform_browser import PlatformBrowserCollector


POST_ID = "68c6f07d000000001d01abcd"
POST_URL = f"https://www.xiaohongshu.com/explore/{POST_ID}"
VIDEO_ID = "7450123456789012345"
VIDEO_URL = f"https://www.douyin.com/video/{VIDEO_ID}"


def _parser_expression(platform: str) -> str:
    method = (
        PlatformBrowserCollector.archive_xiaohongshu_detail
        if platform == "xiaohongshu"
        else PlatformBrowserCollector.extract_douyin_detail
    )
    tree = ast.parse(textwrap.dedent(inspect.getsource(method)))
    expressions = []
    for node in ast.walk(tree):
        if platform == "xiaohongshu":
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "evaluate"
                and node.args
            ):
                continue
            value = node.args[0]
        else:
            if not (
                isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == "expression" for target in node.targets)
            ):
                continue
            value = node.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str) and "exactCandidates" in value.value:
            expressions.append(value.value)
    assert len(expressions) == 1, "The test must execute exactly one production detail parser"
    return expressions[0]


@pytest.fixture(scope="module")
def node_executable() -> str:
    executable = shutil.which("node")
    if not executable:
        pytest.skip("Node.js is needed to exercise the embedded page parsers offline")
    return executable


def _parse(node_executable: str, platform: str, state: Any, *, root_name: str = "__INITIAL_STATE__", mounted_nodes: list | None = None) -> dict[str, Any]:
    harness = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const location = new URL(input.url);
const document = {
  title: '离线原帖测试',
  body: {innerText: ''},
  querySelector: () => null,
  querySelectorAll: selector => selector === '*' ? (input.mountedNodes || []) : [],
};
const context = vm.createContext({
  window: {[input.rootName]: input.state},
  document, location, URL,
  Node: {TEXT_NODE: 3, ELEMENT_NODE: 1},
});
const script = input.platform === 'xiaohongshu'
  ? '(' + input.expression + ')(' + JSON.stringify(input.targetId) + ')'
  : input.expression;
const result = vm.runInContext(script, context, {timeout: 1000});
process.stdout.write(JSON.stringify(result));
"""
    process = subprocess.run(
        [node_executable, "-e", harness],
        input=json.dumps({
            "platform": platform,
            "expression": _parser_expression(platform),
            "state": state,
            "rootName": root_name,
            "mountedNodes": mounted_nodes,
            "url": POST_URL if platform == "xiaohongshu" else VIDEO_URL,
            "targetId": POST_ID if platform == "xiaohongshu" else VIDEO_ID,
        }, ensure_ascii=False),
        text=True,
        capture_output=True,
        timeout=5,
        check=True,
    )
    return json.loads(process.stdout)


def _react_detail(**overrides):
    return {"awemeId": VIDEO_ID, "awemeType": 68, "authorInfo": {"nickname": "作者"},
            "video": {}, "desc": "图文正文", **overrides}


@pytest.mark.parametrize("prop_key", ["__reactProps$offline", "__reactFiber$offline"])
def test_douyin_mounted_detail_props_preserve_raw_caption(node_executable, prop_key):
    raw = "  图文全文\r\n\n57张图🙂 #同城\u200b  "
    props = {"awemeInfo": _react_detail(desc=raw, authenticationToken="PRIVATE_NOT_FOR_OUTPUT")}
    result = _parse(node_executable, "douyin", {}, mounted_nodes=[{
        prop_key: {"memoizedProps": props} if "Fiber" in prop_key else props,
    }])
    assert result["publishedBody"] == raw
    assert result["observedVideoId"] == VIDEO_ID
    assert result["captionSource"] == "详情页结构化发布文案"
    assert result["selectedCandidateStateSource"] == "react_detail_props"
    assert result["reactDetailCandidateCount"] == 1
    assert "PRIVATE_NOT_FOR_OUTPUT" not in json.dumps(result)


@pytest.mark.parametrize("record", [
    _react_detail(awemeId="7450999999999999999"),
    {"awemeId": VIDEO_ID, "desc": "评论不是正文", "commentId": "comment1"},
    {"awemeId": VIDEO_ID, "desc": "搜索摘要不是正文", "authorInfo": {}, "video": {}},
    _react_detail(awemeType=None),
    _react_detail(authorInfo=None),
    _react_detail(video=None),
    *(_react_detail(**{field: "comment1"}) for field in ["commentId", "comment_id", "cid", "replyId", "reply_id"]),
])
def test_douyin_react_fallback_rejects_wrong_id_or_weak_shape(node_executable, record):
    result = _parse(node_executable, "douyin", {}, mounted_nodes=[{"__reactProps$x": record}])
    assert result["publishedBody"] == ""
    assert result["reactDetailCandidateCount"] == 0


def test_douyin_comment_props_can_contain_independent_post_detail(node_executable):
    result = _parse(node_executable, "douyin", {}, mounted_nodes=[{"__reactProps$x": {
        **_react_detail(commentId="comment1", desc="评论不能作为正文"),
        "awemeInfo": _react_detail(desc="独立的原帖完整正文"),
    }}])
    assert result["publishedBody"] == "独立的原帖完整正文"
    assert result["reactDetailCandidateCount"] == 1


def test_douyin_react_detail_can_replace_share_snippet_but_not_regular_state(node_executable):
    props = [{"__reactProps$x": _react_detail(desc="完整图文正文")}]
    result = _parse(node_executable, "douyin", {"aweme_id": VIDEO_ID,
        "share_info": {"share_desc": "片段…"}}, mounted_nodes=props)
    assert result["publishedBody"] == "完整图文正文"
    result = _parse(node_executable, "douyin", {"aweme_id": VIDEO_ID, "desc": "现有已核验路径"}, mounted_nodes=props)
    assert result["publishedBody"] == "现有已核验路径"
    assert result["reactVisitedObjectCount"] == 0


def test_douyin_react_fallback_does_not_walk_full_fiber_or_unbounded_dom(node_executable):
    detail = _react_detail()
    result = _parse(node_executable, "douyin", {}, mounted_nodes=[
        {"__reactFiber$x": {"return": {"memoizedProps": detail}, "memoizedProps": {"_owner": detail}}},
        *({} for _ in range(2999)), {"__reactProps$x": detail},
    ])
    assert result["publishedBody"] == ""
    assert result["reactDetailCandidateCount"] == 0


def test_douyin_react_fallback_has_depth_and_object_limits(node_executable):
    nested = _react_detail()
    for _ in range(16):
        nested = {"child": nested}
    result = _parse(node_executable, "douyin", {}, mounted_nodes=[{"__reactProps$x": nested}])
    assert result["publishedBody"] == ""
    assert result["reactVisitedObjectCount"] == 15
    result = _parse(node_executable, "douyin", {}, mounted_nodes=[{
        "__reactProps$x": {"padding": [{} for _ in range(60000)], "detail": _react_detail()},
    }])
    assert result["publishedBody"] == ""
    assert result["reactVisitedObjectCount"] == 60000
    result = _parse(node_executable, "douyin", {}, mounted_nodes=[{
        "__reactProps$x": {"padding": [0 for _ in range(100000)], "detail": _react_detail()},
    }])
    assert result["publishedBody"] == ""
    assert result["reactVisitedPropertyCount"] == 100000


@pytest.mark.parametrize("root_name", ["__INLINE_PLAYER_DATA__", "__MF_DATA_FETCH_MAP__"])
def test_douyin_reads_current_page_state_roots_only_for_exact_post(node_executable, root_name):
    raw = "  图文正文\n保留段落🙂 #同城  "
    result = _parse(node_executable, "douyin", {
        "other": {"aweme_id": "7450123456789012399", "desc": "别帖不能替代"},
        "detail": {"aweme_id": VIDEO_ID, "desc": raw},
    }, root_name=root_name)
    assert result["observedVideoId"] == VIDEO_ID
    assert result["publishedBody"] == raw
    assert result["captionSource"] == "详情页结构化发布文案"


@pytest.mark.parametrize("empty_desc", ["", " \r\n\t "])
def test_xhs_empty_desc_does_not_hide_nonempty_note_desc(node_executable: str, empty_desc: str):
    raw = "  第一段\r\n\n第二段\u200b🙂 #原话题  "
    result = _parse(node_executable, "xiaohongshu", {
        "note": {"noteId": POST_ID, "desc": empty_desc, "noteDesc": raw},
    })

    assert result["bodyRaw"] == raw
    assert result["selectedBodyField"] == "noteDesc"
    assert result["bodyVerified"] is True
    assert result["observedId"] == result["statePostId"] == POST_ID


def test_xhs_metadata_stub_cannot_outrank_same_post_full_body(node_executable: str):
    raw = "完整发布正文。\n#长沙"
    result = _parse(node_executable, "xiaohongshu", {
        "searchCard": {
            "noteId": POST_ID, "desc": "", "title": "很多元数据的卡片",
            "imageList": [{"url": "https://example.invalid/image.png"}],
            "user": {"nickname": "作者"},
        },
        "detail": {"noteId": POST_ID, "desc": raw},
    })

    assert result["stateCandidateCount"] == 2
    assert result["bodyRaw"] == raw
    assert result["bodyVerified"] is True


def test_xhs_body_from_different_post_is_never_selected(node_executable: str):
    result = _parse(node_executable, "xiaohongshu", {
        "selected": {"noteId": POST_ID, "title": "目标正文还未加载"},
        "recommended": {
            "noteId": "68c6f07d000000001d99beef", "desc": "其他帖的完整正文不可混入",
            "title": "其他帖", "imageList": [], "user": {"nickname": "别的作者"},
        },
    })

    assert result["stateCandidateCount"] == 1
    assert result["statePostId"] == POST_ID
    assert result["bodyRaw"] is None
    assert result["bodyVerified"] is False


def test_douyin_structured_desc_preserves_all_original_characters(node_executable: str):
    raw = " \t\u200b第一行  保留两个空格\r\n\n第二行👩🏽‍💻 #话题\u200b \t\n"
    result = _parse(node_executable, "douyin", {"detail": {
        "aweme_id": VIDEO_ID,
        "desc": raw,
        "share_info": {"share_desc": "用于分享的片段"},
    }})

    assert result["observedVideoId"] == VIDEO_ID
    assert result["publishedBody"] == raw
    assert result["captionSource"] == "详情页结构化发布文案"
    assert result["selectedCandidateCaptionField"] == "desc"


def test_douyin_share_snippet_does_not_claim_complete_structured_caption(node_executable: str):
    snippet = "截断的分享文案…"
    result = _parse(node_executable, "douyin", {"detail": {
        "aweme_id": VIDEO_ID,
        "share_info": {"share_desc": snippet},
    }})

    assert result["observedVideoId"] == VIDEO_ID
    assert result["publishedBody"] == snippet
    assert result["selectedCandidateCaptionField"] == "share_info.share_desc"
    assert result["captionSource"] != "详情页结构化发布文案"
    assert "片段" in result["captionSource"]


def test_douyin_wrong_video_desc_does_not_fill_target_caption(node_executable: str):
    result = _parse(node_executable, "douyin", {
        "detail": {"aweme_id": VIDEO_ID, "author": {"nickname": "目标作者"}},
        "recommended": {"aweme_id": "7450999999999999999", "desc": "其他视频正文不可混入"},
    })

    assert result["observedVideoId"] == VIDEO_ID
    assert result["structuredCandidateCount"] == 1
    assert result["publishedBody"] == ""
    assert result["captionSource"] == ""
