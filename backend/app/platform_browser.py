from __future__ import annotations

import difflib
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import tempfile
import time
import urllib.parse
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

import httpx

from .config import RADAR_CAPTURE_DIR, STATE_DIR
from .speech_transcription import (
    ALLOWED_MEDIA_SUFFIXES,
    MAX_MEDIA_BYTES,
    SpeechTranscriptionError,
    local_asr_status,
    transcribe_media_path,
)


def _configured_social_cdp_port() -> int:
    """Use the dedicated social browser's established CDP port by default.

    A persistent Chrome profile can only be owned by one browser process. If
    the workbench tries to launch the same profile on a different port, Chrome
    forwards the launch to the existing process and the workbench then waits
    forever for a DevTools endpoint that will never appear. Keep an explicit
    override for installations that provision another port.
    """
    override = os.getenv("WORKBENCH_SOCIAL_CDP_PORT", "").strip()
    return int(override) if override else 9230


CDP_PORT = _configured_social_cdp_port()
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"

# Search pages can paint the navigation shell several seconds before their
# result cards.  Keep the bounded wait long enough to observe that ordinary
# hydration, while leaving retries to the caller instead of spinning forever.
_BACKGROUND_SEARCH_READY_TIMEOUT_SECONDS = 14.0
_BACKGROUND_SEARCH_POLL_SECONDS = 0.25
_DOUYIN_DETAIL_READ_TIMEOUT_SECONDS = 12.0
_DOUYIN_DETAIL_HYDRATION_GRACE_SECONDS = 2.1
_DOUYIN_REDIRECT_RETRY_WAIT_SECONDS = 3.0
_XHS_DETAIL_HYDRATION_TIMEOUT_SECONDS = 4.5
_BROWSER_PAGE_REQUIRED_MESSAGE = (
    "采集浏览器没有打开页面，后台读取尚不可用。请点击“打开双平台浏览器”，"
    "在专用窗口完成登录或验证后继续。"
)


def _configured_social_profile_dir() -> Path:
    """Resolve the dedicated browser profile without ever copying session data.

    An isolated workbench runtime can point at the already-authorized local
    Chrome profile by setting ``WORKBENCH_SOCIAL_PROFILE_DIR``.  When omitted
    (or deliberately left blank), preserve the original per-state-directory
    location exactly: ``STATE_DIR / 'social_browser'``.
    """
    override = os.getenv("WORKBENCH_SOCIAL_PROFILE_DIR", "").strip()
    if not override:
        return STATE_DIR / "social_browser"
    return Path(override).expanduser().resolve()


PROFILE_DIR = _configured_social_profile_dir()
OCR_SCRIPT = Path(__file__).with_name("macos_vision_ocr.swift")

_SESSION_READY = "READY"
_SESSION_LOGIN_REQUIRED = "LOGIN_REQUIRED"
_SESSION_VERIFICATION_REQUIRED = "VERIFICATION_REQUIRED"
_SESSION_PLATFORM_LIMITED = "PLATFORM_LIMITED"
_SESSION_UNKNOWN = "UNKNOWN"
_SESSION_STATES = frozenset({
    _SESSION_READY,
    _SESSION_LOGIN_REQUIRED,
    _SESSION_VERIFICATION_REQUIRED,
    _SESSION_PLATFORM_LIMITED,
    _SESSION_UNKNOWN,
})

_DOUYIN_MEDIA_HOST_SUFFIXES = (
    "douyinvod.com", "snssdk.com", "pstatp.com", "bytecdn.cn",
    "bytevideo.com", "toutiaocloud.com", "byteoversea.com",
)


def _safe_douyin_media_url(value: Any) -> str:
    """Accept only HTTPS media URLs on known ByteDance/Douyin delivery hosts."""
    candidate = str(value or "").strip()
    if not candidate or len(candidate) > 12000:
        return ""
    try:
        parsed = urllib.parse.urlsplit(candidate)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme.lower() != "https" or not hostname or parsed.username or parsed.password:
            return ""
        if parsed.port not in (None, 443):
            return ""
        if not any(hostname == suffix or hostname.endswith("." + suffix) for suffix in _DOUYIN_MEDIA_HOST_SUFFIXES):
            return ""
        if parsed.path.lower().endswith((".m3u8", ".mpd", ".m4s")):
            return ""
        return urllib.parse.urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, ""))
    except ValueError:
        return ""

# Keep the checks intentionally conservative.  A page only enters READY after
# actual result cards were read; a live DevTools endpoint alone is not proof of
# a usable Xiaohongshu/Douyin session.
# A login form commonly includes wording such as ``验证码登录``.  That is not
# the same as a security challenge: treat it as a missing session unless the
# page also explicitly asks the user to complete a safety/human check.
#
# Keep generic ``验证码``/``captcha`` words separate.  They are enough to
# identify a verification page when there is no login UI, but they must not
# override an explicit QR/phone login form merely because a login method uses
# a verification code.
_STRONG_VERIFICATION_MARKERS = (
    "安全验证", "验证后继续", "完成验证", "请完成验证", "请进行验证",
    "滑动验证", "人机验证", "请拖动滑块", "行为验证", "风控验证",
)
_GENERIC_VERIFICATION_MARKERS = ("验证码", "captcha", "challenge")
_LOGIN_MARKERS = (
    "扫码登录", "二维码登录", "手机号登录", "验证码登录", "短信验证码登录",
    "密码登录", "账号登录", "登录后查看", "登录后继续", "请先登录",
)
_PLATFORM_LIMIT_MARKERS = (
    "ip存在风险", "访问过于频繁", "网络环境", "error_code=300012", "请求过于频繁",
    "操作频繁", "访问频繁", "请求受限", "环境异常",
)
_UNAVAILABLE_MARKERS = (
    "内容不存在", "笔记不存在", "已删除", "页面不存在", "当前笔记暂时无法浏览",
    "error_code=300031", "你访问的页面不见了",
)


def _debug_platform(event: str) -> None:
    if os.getenv("WORKBENCH_DEBUG_PLATFORM") == "1":
        print(f"[platform-browser] {event}", file=sys.stderr, flush=True)


class PlatformBrowserError(RuntimeError):
    def __init__(self, message: str, *, code: str = "browser_error", diagnostics: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.diagnostics = diagnostics or {}


def _connect_over_cdp(playwright: Any, cdp_url: str):
    """Attach to newer Chrome without applying unsupported context defaults.

    Chrome 152 can reject Playwright's Browser.setDownloadBehavior command for
    an externally launched persistent profile.  Playwright 1.62 exposes
    no_defaults specifically for this kind of CDP attachment.  Keep a fallback
    for packaged installations that still use an older Playwright release.
    """
    try:
        return playwright.chromium.connect_over_cdp(cdp_url, no_defaults=True, timeout=6000)
    except TypeError:
        return playwright.chromium.connect_over_cdp(cdp_url, timeout=6000)


def _clean_xhs_title_hint(value: str) -> str:
    """Recover the real platform title from a generated recommendation title."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    quoted = re.search(r"《(.+?)》", text)
    if quoted:
        text = quoted.group(1).strip()
    text = re.sub(r"\s*[-–—]\s*小红书\s*$", "", text).strip()
    return text[:180]


def _search_url(platform: str, query: str) -> str:
    clean = str(query or "").strip()[:180]
    if platform == "xiaohongshu":
        return "https://www.xiaohongshu.com/search_result/?" + urllib.parse.urlencode({"keyword": clean})
    if platform == "douyin":
        return f"https://www.douyin.com/search/{urllib.parse.quote(clean, safe='')}?type=video"
    raise ValueError("不支持的平台")


def _chrome_path() -> str:
    candidates: list[str] = []
    if sys.platform == "darwin":
        candidates.extend([
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            str(Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        ])
    elif os.name == "nt":
        for root in (os.getenv("PROGRAMFILES"), os.getenv("PROGRAMFILES(X86)"), os.getenv("LOCALAPPDATA")):
            if root:
                candidates.extend([
                    str(Path(root) / "Google/Chrome/Application/chrome.exe"),
                    str(Path(root) / "Microsoft/Edge/Application/msedge.exe"),
                ])
    else:
        for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge"):
            value = shutil.which(name)
            if value:
                candidates.append(value)
    return next((value for value in candidates if Path(value).is_file()), "")


def _metric_number(value: str) -> int | float | None:
    clean = str(value or "").strip().replace(",", "")
    match = re.search(r"(\d+(?:\.\d+)?)\s*([万wW千kK]?)", clean)
    if not match:
        return None
    number = float(match.group(1))
    unit = match.group(2).lower()
    if unit in {"万", "w"}:
        number *= 10000
    elif unit in {"千", "k"}:
        number *= 1000
    return int(number) if number.is_integer() else round(number, 1)


def _visible_metrics(text: str) -> dict[str, int | float]:
    values: dict[str, int | float] = {}
    patterns = {
        "likes": r"(?:点赞|获赞|赞)\s*[:：]?\s*(\d+(?:\.\d+)?\s*[万wW千kK]?)|(\d+(?:\.\d+)?\s*[万wW千kK]?)\s*(?:点赞|获赞|赞)",
        "comments": r"(?:评论)\s*[:：]?\s*(\d+(?:\.\d+)?\s*[万wW千kK]?)|(\d+(?:\.\d+)?\s*[万wW千kK]?)\s*(?:评论)",
        "shares": r"(?:分享|转发)\s*[:：]?\s*(\d+(?:\.\d+)?\s*[万wW千kK]?)|(\d+(?:\.\d+)?\s*[万wW千kK]?)\s*(?:分享|转发)",
        "favorites": r"(?:收藏)\s*[:：]?\s*(\d+(?:\.\d+)?\s*[万wW千kK]?)|(\d+(?:\.\d+)?\s*[万wW千kK]?)\s*(?:收藏)",
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            number = _metric_number(match.group(1) or match.group(2) or "")
            if number is not None:
                values[key] = number
    # Xiaohongshu search cards often show one bare engagement number after
    # the author/date without a visible "赞" label.  Preserve it under a
    # neutral name rather than claiming which native metric it represents.
    if not values:
        candidates = re.findall(r"(?:^|\s)(\d+(?:\.\d+)?\s*[万wW千kK]?)(?=\s|$)", str(text or ""))
        if candidates:
            number = _metric_number(candidates[-1])
            if number is not None:
                values["visible_engagement"] = number
    return values


def _safe_url(platform: str, value: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(str(value or ""))
    except ValueError:
        return ""
    host = parsed.hostname or ""
    allowed = host == "douyin.com" or host.endswith(".douyin.com") if platform == "douyin" else host == "xiaohongshu.com" or host.endswith(".xiaohongshu.com")
    if parsed.scheme != "https" or not allowed:
        return ""
    # Result links may need xsec_token to open, but unrelated tracking data is discarded.
    keep = {key: values for key, values in urllib.parse.parse_qs(parsed.query, keep_blank_values=True).items() if key in {"xsec_token", "xsec_source", "type"}}
    path = parsed.path
    # The visible Xiaohongshu result card points at /search_result/<note-id>
    # and carries the short-lived access token.  A hidden SEO link points at
    # /explore/<note-id> without that token and is rejected with error 300031.
    # Store the canonical post path while preserving only the access fields.
    if platform == "xiaohongshu":
        result_match = re.fullmatch(r"/search_result/([A-Za-z0-9]+)", path.rstrip("/"))
        if result_match:
            path = f"/explore/{result_match.group(1)}"
            if keep.get("xsec_token") and not any(keep.get("xsec_source") or []):
                keep["xsec_source"] = ["pc_search"]
    query = urllib.parse.urlencode(keep, doseq=True)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, query, ""))


def _diagnostic_url(value: str) -> str:
    """Keep a useful page location in logs without persisting access tokens."""
    try:
        parsed = urllib.parse.urlsplit(str(value or ""))
    except ValueError:
        return ""
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _public_post_url(platform: str, value: str) -> str:
    """Return a stable shareable URL; temporary platform access data stays in memory."""
    safe = _safe_url(platform, value)
    if platform != "xiaohongshu" or not safe:
        return safe
    parsed = urllib.parse.urlsplit(safe)
    if re.search(r"/(?:explore|discovery/item)/[A-Za-z0-9]+", parsed.path):
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    return safe


def _douyin_video_id(value: str) -> str:
    """Return the only identity accepted by the Douyin detail reader.

    Search-card text and a creator name are deliberately not identity hints:
    a card can be refreshed or reordered while a task is pending.  Detail
    extraction is therefore limited to a stable Douyin ``/video/<id>`` or
    ``/note/<id>`` route. Douyin can redirect a video share URL to the latter.
    """
    safe = _safe_url("douyin", value)
    if not safe:
        return ""
    match = re.search(r"/(?:video|note)/(\d+)(?:/|$)", urllib.parse.urlsplit(safe).path)
    return match.group(1) if match else ""


def _douyin_detail_payload(
    raw_body: bytes, target_video_id: str, *, diagnostic: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Accept only the exact aweme_detail object returned by the visible page.

    Comment endpoints also repeat ``aweme_id`` on every comment, so this
    parser intentionally requires the detail response's top-level
    ``aweme_detail`` object and a direct caption field. It never scans comment
    text or treats OCR/ASR/search summaries as a platform caption.
    """
    diagnostic = diagnostic if diagnostic is not None else {}
    diagnostic["reason"] = "invalid_json"
    try:
        text = raw_body.decode("utf-8", "replace").lstrip("\ufeff\n\r\t ")
        if text.startswith(")]}'"):
            text = text.split("\n", 1)[-1]
        payload = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        diagnostic["reason"] = "non_object_json"
        return None
    status_code = payload.get("status_code")
    if isinstance(status_code, (int, float)) and not isinstance(status_code, bool):
        diagnostic["platform_status_code"] = status_code
    detail = payload.get("aweme_detail")
    if not isinstance(detail, dict):
        diagnostic["reason"] = "missing_detail_object"
        filtered = payload.get("filter_detail")
        if "aweme_detail" in payload and detail is None and isinstance(filtered, dict):
            # A filtered record can describe a different item. Never infer
            # target visibility from an unmatched ID, free text, or metadata.
            if filtered.get("aweme_id") != target_video_id:
                diagnostic["reason"] = "filter_identity_mismatch"
                return None
            diagnostic["filter_detail_video_id"] = target_video_id
            filter_reason = filtered.get("filter_reason")
            if isinstance(filter_reason, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", filter_reason):
                # Save only a bounded enum-like code, never notice/icon URLs,
                # messages, response bodies, headers, or signing parameters.
                diagnostic["filter_reason"] = filter_reason
            if (
                status_code == 0 and not isinstance(status_code, bool)
                and filter_reason == "status_audit_self_see"
            ):
                # This code was observed with a same-ID null detail. Its
                # private/audit mechanics are not known or inferred here.
                diagnostic["reason"] = "platform_visibility_restricted"
        return None
    observed = str(detail.get("aweme_id") or detail.get("awemeId") or detail.get("item_id") or detail.get("itemId") or "")
    if observed != target_video_id:
        diagnostic["reason"] = "identity_mismatch"
        return None
    caption = ""
    caption_field = ""
    for field in ("desc", "description", "video_desc", "videoDesc", "caption"):
        value = detail.get(field)
        if isinstance(value, str) and value.strip():
            # Preserve the platform-provided string exactly. The UI may trim
            # around it for display, but the stored source must retain any
            # intentional leading/trailing whitespace and line breaks.
            caption, caption_field = value, field
            break
    if not caption:
        diagnostic["reason"] = "empty_caption"
        return None
    diagnostic["reason"] = "verified_caption"
    author = detail.get("author") if isinstance(detail.get("author"), dict) else {}
    stats = detail.get("statistics") if isinstance(detail.get("statistics"), dict) else {}
    metrics: dict[str, int | float] = {}
    metric_fields = {
        "likes": "digg_count", "comments": "comment_count", "shares": "share_count",
        "favorites": "collect_count", "plays": "play_count",
    }
    for name, field in metric_fields.items():
        value = stats.get(field)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            metrics[name] = int(value) if float(value).is_integer() else float(value)
    created = detail.get("create_time") or ""
    published_at = ""
    try:
        if isinstance(created, (int, float)) or re.fullmatch(r"\d{10}", str(created)):
            published_at = datetime.fromtimestamp(int(created), timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        published_at = ""
    return {
        "video_id": observed,
        "published_body": caption,
        "published_body_length": len(caption),
        "caption_field": caption_field,
        "author": str(author.get("nickname") or author.get("name") or "")[:300],
        "title": str(detail.get("title") or "")[:500],
        "published_at": published_at or None,
        "metrics": metrics,
    }


def _read_douyin_detail_response(page: Any, target_video_id: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Read bounded JSON bodies from the same-origin detail request only.

    The browser has already requested these resources as part of its normal
    visible-page load. This does not issue a separate platform request or
    synthesize cookies/signatures; it only inspects a small number of captured
    same-origin detail responses and rejects mismatched aweme IDs.
    """
    diagnostics: dict[str, Any] = {
        "network_detail_response_count": 0, "network_detail_payload_match_count": 0,
        "network_detail_outcomes": [],
    }
    try:
        responses = page.take_network_responses()
    except Exception:
        return None, diagnostics
    relevant: list[dict[str, Any]] = []
    seen: set[str] = set()
    for response in responses:
        if not isinstance(response, dict):
            continue
        request_id = str(response.get("request_id") or "")
        try:
            parsed = urllib.parse.urlsplit(str(response.get("url") or ""))
        except ValueError:
            continue
        host = (parsed.hostname or "").lower()
        path = parsed.path.lower()
        if host != "douyin.com" and not host.endswith(".douyin.com"):
            continue
        if not path.startswith("/aweme/v1/web/aweme/detail") or not request_id or request_id in seen:
            continue
        seen.add(request_id)
        relevant.append(response)
    diagnostics["network_detail_response_count"] = len(relevant)
    for response in relevant[:8]:
        outcome: dict[str, Any] = {}
        diagnostics["network_detail_outcomes"].append(outcome)
        headers = response.get("headers") if isinstance(response.get("headers"), dict) else {}
        normalized_headers = {str(key).lower(): value for key, value in headers.items()}
        content_type = str(normalized_headers.get("content-type") or "")
        try:
            status = int(response.get("status") or 0)
        except (TypeError, ValueError):
            outcome["reason"] = "invalid_http_status"
            continue
        outcome["http_status"] = status
        if status != 200 or "json" not in content_type.lower():
            outcome["reason"] = "http_error" if status != 200 else "non_json_response"
            continue
        try:
            content_length = int(normalized_headers.get("content-length") or 0)
        except (TypeError, ValueError):
            content_length = 0
        if content_length > 8 * 1024 * 1024:
            outcome["reason"] = "body_too_large"
            continue
        try:
            body = page.response_body(str(response.get("request_id") or ""))
        except Exception:
            outcome["reason"] = "body_unavailable"
            continue
        if not body or len(body) > 8 * 1024 * 1024:
            outcome["reason"] = "empty_body" if not body else "body_too_large"
            continue
        outcome["body_bytes"] = len(body)
        payload = _douyin_detail_payload(body, target_video_id, diagnostic=outcome)
        if payload:
            diagnostics["network_detail_payload_match_count"] += 1
            try:
                diagnostics["network_detail_response_path"] = urllib.parse.urlsplit(str(response.get("url") or "")).path[:180]
            except ValueError:
                diagnostics["network_detail_response_path"] = ""
            return payload, diagnostics
    return None, diagnostics


def _xhs_search_result_candidate(row: Any) -> tuple[str, str]:
    """Read an ordinary result link without inventing access parameters."""
    if not isinstance(row, dict):
        return "", ""
    url = _safe_url("xiaohongshu", str(row.get("href") or ""))
    if not url:
        return "", ""
    try:
        parsed = urllib.parse.urlsplit(url)
        if parsed.username or parsed.password or parsed.port not in (None, 443):
            return "", ""
    except ValueError:
        return "", ""
    match = re.fullmatch(r"/(?:explore|discovery/item)/([A-Za-z0-9]+)/?", parsed.path)
    return (url, match.group(1)) if match else ("", "")


def _best_xhs_search_result(
    rows: list[dict[str, Any]], post_key: str, title_hint: str,
) -> str:
    """Prefer an existing same-ID access link over its tokenless duplicate.

    Tokens only come from the ordinary loaded result cards and remain in
    memory. A similar title or a different post's access token is never used.
    """
    fallback = ""
    for row in rows:
        url, candidate_key = _xhs_search_result_candidate(row)
        if not url or candidate_key != post_key:
            continue
        if not fallback:
            fallback = url
        access_fields = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        if any(str(value).strip() for value in access_fields.get("xsec_token", [])):
            return url
    return fallback


def _similar_xhs_search_result(
    rows: list[dict[str, Any]], post_key: str, title_hint: str,
) -> tuple[str, str]:
    """Find a title match only to report IDENTITY_MISMATCH, never to archive it."""
    normalized_hint = re.sub(r"\W+", "", str(title_hint or "")).lower()
    best_url, best_key, best_score = "", "", 0.0
    for row in rows:
        url = _safe_url("xiaohongshu", str(row.get("href") or ""))
        if not url:
            continue
        path = urllib.parse.urlsplit(url).path
        match = re.search(r"/(?:explore|discovery/item)/([A-Za-z0-9]+)", path)
        candidate_key = match.group(1) if match else ""
        if not candidate_key or candidate_key == post_key:
            continue
        candidate_title = re.sub(r"\W+", "", str(row.get("title") or row.get("text") or "")).lower()
        if not normalized_hint or not candidate_title:
            continue
        score = difflib.SequenceMatcher(None, normalized_hint, candidate_title[: max(len(normalized_hint) * 2, 30)]).ratio()
        if normalized_hint in candidate_title or candidate_title in normalized_hint:
            score = max(score, 0.86)
        if score > best_score:
            best_url, best_key, best_score = url, candidate_key, score
    return (best_url, best_key) if best_score >= 0.58 else ("", "")


def _page_access_state(value: Any) -> str:
    """Classify an authorized platform page before treating it as missing.

    Platform risk pages frequently contain generic phrases such as “temporarily
    unavailable”.  They must be reported as a session/risk problem before a
    selected post is marked unavailable, otherwise a transient verification
    wall poisons the archive history with false POST_UNAVAILABLE failures.
    """
    text = urllib.parse.unquote(str(value or "")).lower()
    has_login_ui = any(marker.lower() in text for marker in _LOGIN_MARKERS)
    has_strong_verification = any(
        marker.lower() in text for marker in _STRONG_VERIFICATION_MARKERS
    )

    # A concrete safety/human challenge wins even if the page also exposes a
    # login button.  Conversely, a QR/phone login form can legitimately say
    # ``验证码登录`` or include a generic CAPTCHA library name, so its explicit
    # login UI wins over those generic words.
    if has_strong_verification:
        return "verification_required"
    if has_login_ui:
        return "login_required"
    if any(marker.lower() in text for marker in _GENERIC_VERIFICATION_MARKERS):
        return "verification_required"
    if any(marker.lower() in text for marker in _PLATFORM_LIMIT_MARKERS):
        return "platform_limited"
    if any(marker.lower() in text for marker in _UNAVAILABLE_MARKERS):
        return "post_unavailable"
    return ""


_XHS_VISIBLE_BODY_QUOTE_EXPRESSION = r'''targetId => {
  // A stale state object alone cannot disprove an unavailable page. Require
  // the same note's actual visible detail body, then inspect everything else.
  const normalize = value => String(value || '').replace(/\s+/g, ' ').trim();
  const visible = node => {
    if (!node || ![...node.getClientRects()].some(rect => rect.width > 0 && rect.height > 0)) return false;
    for (let current = node; current; current = current.parentElement) {
      const style = getComputedStyle(current);
      if (current.hidden || current.getAttribute('aria-hidden') === 'true' || style.display === 'none'
          || ['hidden','collapse'].includes(style.visibility) || style.opacity === '0') return false;
    }
    return true;
  };
  const selectors = ['[data-testid="note-content"]','[data-testid="post-content"]','#detail-desc',
    '.note-content .desc','[class*="note-detail"] [class*="desc"]'];
  const bodies = [];
  for (const selector of selectors) for (const node of document.querySelectorAll(selector)) {
    if (visible(node) && typeof node.innerText === 'string' && normalize(node.innerText)) bodies.push(node.innerText);
  }
  if (!bodies.length) return {samePostVisibleBody: false};
  const roots = [];
  for (const key of ['__INITIAL_STATE__','__INITIAL_DATA__','__APOLLO_STATE__']) {
    try { if (window[key] && typeof window[key] === 'object') roots.push(window[key]); } catch {}
  }
  for (const node of document.querySelectorAll('script#__NEXT_DATA__,script[type="application/json"]')) {
    try {
      const raw = (node.textContent || '').trim();
      if (raw.length >= 2 && raw.length <= 5000000) roots.push(JSON.parse(raw));
    } catch {}
  }
  const seen = new WeakSet(); let visited = 0; let matchedBody = '';
  const walk = (value, depth = 0) => {
    if (!value || typeof value !== 'object' || depth > 12 || seen.has(value) || visited >= 30000 || matchedBody) return;
    seen.add(value); visited += 1;
    const namedId = ['noteId','note_id','postId','post_id'].map(key => value[key]).find(id => typeof id === 'string' && id);
    const id = namedId || (typeof value.id === 'string' ? value.id : '');
    const noteShape = typeof value.noteDesc === 'string' || typeof value.noteTitle === 'string'
      || Array.isArray(value.imageList) || Array.isArray(value.image_list) || Array.isArray(value.images)
      || (typeof value.title === 'string' && (value.user || value.author));
    if (id === targetId && (namedId || noteShape)) {
      for (const field of ['desc','noteDesc']) {
        if (typeof value[field] !== 'string' || !normalize(value[field])) continue;
        matchedBody = bodies.find(body => normalize(body) === normalize(value[field])) || '';
        if (matchedBody) return;
      }
    }
    try { for (const child of Object.values(value)) walk(child, depth + 1); } catch {}
  };
  roots.forEach(root => walk(root));
  const pageText = document.body?.innerText || '';
  const normalizedPage = normalize(pageText), normalizedBody = normalize(matchedBody);
  // Do not clear an error based on a truncated page snapshot or a textContent
  // fallback that could include an invisible previous detail.
  if (!normalizedBody || pageText.length > 100000 || !normalizedPage.includes(normalizedBody)) {
    return {samePostVisibleBody: false};
  }
  return {samePostVisibleBody: true, url: location.href, title: document.title || '',
    outsideText: normalizedPage.replace(normalizedBody, '')};
}'''


def _xhs_unavailable_is_body_quote(page: Any, target_post_id: str, snapshot: dict[str, Any]) -> bool:
    """Clear only an unavailable phrase proven to belong to a visible same-ID body."""
    def target_detail(url: str) -> bool:
        safe_url = _safe_url("xiaohongshu", url)
        # _safe_url also converts search-card links; this evidence must use
        # the actual current route, not a canonicalized card/canonical URL.
        path = urllib.parse.urlsplit(url).path.rstrip("/") if safe_url else ""
        return path in {f"/explore/{target_post_id}", f"/discovery/item/{target_post_id}"}

    url = str(snapshot.get("url") or snapshot.get("finalUrl") or "")
    title = str(snapshot.get("pageTitle") or snapshot.get("title") or "")
    if not target_detail(url) or _page_access_state(f"{url} {title}"):
        return False
    try:
        evidence = page.evaluate(_XHS_VISIBLE_BODY_QUOTE_EXPRESSION, target_post_id)
    except TimeoutError:
        raise
    except Exception:
        return False
    if not isinstance(evidence, dict) or evidence.get("samePostVisibleBody") is not True:
        return False
    if not all(isinstance(evidence.get(key), str) for key in ("url", "title", "outsideText")):
        return False
    return target_detail(str(evidence.get("url") or "")) and not _page_access_state(
        f"{evidence.get('url')} {evidence.get('title')} {evidence.get('outsideText')}"
    )


_XHS_DETAIL_READINESS_EXPRESSION = r'''targetId => {
  const roots = [];
  for (const key of ['__INITIAL_STATE__','__INITIAL_DATA__','__APOLLO_STATE__']) {
    try { if (window[key] && typeof window[key] === 'object') roots.push(window[key]); } catch {}
  }
  for (const node of document.querySelectorAll('script#__NEXT_DATA__,script[type="application/json"]')) {
    try {
      const raw = (node.textContent || '').trim();
      if (raw.length < 2 || raw.length > 5000000) continue;
      const parsed = JSON.parse(raw);
      if (parsed && typeof parsed === 'object') roots.push(parsed);
    } catch {}
  }
  const bodyFields = ['desc','noteDesc','description','content'];
  const postId = value => {
    for (const key of ['noteId','note_id','postId','post_id','id']) {
      try { if (typeof value?.[key] === 'string' && value[key]) return value[key]; } catch {}
    }
    return '';
  };
  const seen = new WeakSet(); let structuredBodyReady = false; let visited = 0;
  const walk = (value, depth = 0) => {
    if (!value || typeof value !== 'object' || depth > 12 || seen.has(value) || visited > 30000 || structuredBodyReady) return;
    seen.add(value); visited += 1;
    const id = postId(value);
    const hasBody = bodyFields.some(key => typeof value[key] === 'string' && value[key].trim());
    const looksLikePost = hasBody || Array.isArray(value.imageList) || Array.isArray(value.image_list)
      || Array.isArray(value.images) || typeof value.noteTitle === 'string'
      || (typeof value.title === 'string' && (value.user || value.author));
    if (id === targetId && looksLikePost && hasBody) { structuredBodyReady = true; return; }
    try { for (const child of Object.values(value)) walk(child, depth + 1); } catch {}
  };
  roots.forEach(root => walk(root));
  const visible = node => !!(node && (node.getClientRects().length || node.offsetParent !== null));
  const selectors = ['[data-testid="note-content"]','[data-testid="post-content"]','#detail-desc','.note-content .desc','[class*="note-detail"] [class*="desc"]'];
  let renderedBody = '';
  for (const selector of selectors) {
    const node = [...document.querySelectorAll(selector)].find(visible);
    const text = node ? String(node.innerText ?? node.textContent ?? '') : '';
    if (text.trim().length > renderedBody.trim().length) renderedBody = text;
  }
  return {
    structuredBodyReady,
    renderedBodyLength: renderedBody.length,
    renderedBodySample: renderedBody.slice(0, 8000),
    pageTitle: document.title || '',
    finalUrl: location.href,
    visibleText: (document.body?.innerText || '').slice(0, 2000),
  };
}'''


def _wait_for_xhs_detail_hydration(
    page: Any,
    target_post_id: str,
    *,
    timeout_seconds: float = _XHS_DETAIL_HYDRATION_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Wait briefly for the exact post's structured caption before parsing.

    XHS can emit ``DOMContentLoaded`` before its note state has hydrated. A
    single early read then sees an empty or fallback DOM for some otherwise
    accessible posts. This bounded probe waits for the requested post ID and
    body field, or for visible detail text to settle, without clicking,
    authenticating, or bypassing any platform gate.
    """
    started = time.monotonic()
    deadline = started + max(0.0, timeout_seconds)
    attempts = 0
    stable_dom_polls = 0
    previous_dom_sample = ""
    reason = "timeout"
    access_state = ""
    last: dict[str, Any] = {}
    while True:
        try:
            value = page.evaluate(_XHS_DETAIL_READINESS_EXPRESSION, target_post_id)
        except Exception:
            reason = "probe_failed"
            break
        attempts += 1
        if not isinstance(value, dict):
            reason = "probe_unavailable"
            break
        last = value
        # Kept for deterministic fakes and older adapters that return the
        # final extraction payload instead of the lightweight probe shape.
        if value.get("structuredBodyReady") or (
            value.get("bodyVerified") is True and str(value.get("observedId") or "") == target_post_id
        ):
            reason = "structured_body_ready"
            break
        access_state = _page_access_state(
            f"{value.get('finalUrl')} {value.get('pageTitle')} {value.get('visibleText')}"
        )
        if access_state:
            reason = "platform_gate_or_unavailable"
            break
        sample = str(value.get("renderedBodySample") or "")
        elapsed = time.monotonic() - started
        if sample and sample == previous_dom_sample:
            stable_dom_polls += 1
        else:
            stable_dom_polls = 0
        previous_dom_sample = sample
        # Give delayed structured state a chance to arrive even when a DOM
        # body is already visible; only use the visible-text fallback after it
        # has remained unchanged for a full second.
        if sample and elapsed >= 2.0 and stable_dom_polls >= 4:
            reason = "visible_body_stable"
            break
        if elapsed >= max(0.0, timeout_seconds):
            reason = "timeout"
            break
        wait_ms = max(1, min(250, int((deadline - time.monotonic()) * 1000)))
        try:
            page.wait_for_timeout(wait_ms)
        except Exception:
            time.sleep(min(wait_ms / 1000, max(0.0, deadline - time.monotonic())))
    return {
        "wait_ms": round((time.monotonic() - started) * 1000),
        "attempts": attempts,
        "reason": reason,
        "access_state": access_state,
        "structured_body_ready": bool(last.get("structuredBodyReady") or last.get("bodyVerified")),
        "rendered_body_length": int(last.get("renderedBodyLength") or 0),
    }


def _search_card_expression(platform: str, limit: int) -> str:
    """Return a self-contained CDP evaluation expression for visible cards.

    Douyin's current search UI puts the post URL inside a ``search-result-card``
    wrapper.  Looking only for generic ``/video/`` links can race the UI: the
    cards may be hydrated before the link list is stable.  Start from those
    card wrappers, then resolve their child link.  Xiaohongshu keeps its
    result-link-first route because the result URL carries the short-lived
    access token needed for the subsequent authorized detail read.
    """
    return r'''(() => {
      const platform = __PLATFORM__;
      const limit = __LIMIT__;
      const xhsWanted = 'section.note-item a.cover[href*="/search_result/"],section.note-item a.title[href*="/search_result/"],a[href*="/explore/"],a[href*="/discovery/item/"]';
      const douyinCards = [...document.querySelectorAll('.search-result-card,[class*="search-result-card"]')];
      const candidates = platform === 'xiaohongshu'
        ? [...document.querySelectorAll(xhsWanted)].map(link => ({link, card: null}))
        : douyinCards.map(card => ({
            card,
            link: card.querySelector('a[href*="/video/"],a[href*="/note/"],a[href]')
          })).filter(value => value.link);
      const seen = new Set(); const output = [];
      for (const candidate of candidates) {
        const link = candidate.link;
        const href = link.href || ''; if (!href || seen.has(href)) continue;
        if (platform === 'xiaohongshu' && link.offsetParent === null && !link.matches('section.note-item a.cover,section.note-item a.title')) continue;
        let best = candidate.card || link;
        let bestText = (best.innerText || '').trim();
        if (!candidate.card) {
          let node = link;
          for (let i = 0; i < 7 && node.parentElement; i += 1) {
            node = node.parentElement; const text = (node.innerText || '').trim();
            if (text.length >= 8 && text.length <= 1200 && text.length >= bestText.length) { best = node; bestText = text; }
            if (node.matches('article,li,section,[data-e2e*="search"],[class*="note-item"],[class*="search-card"]')) break;
          }
        }
        const image = best.querySelector('img[alt]');
        const lines = bestText.split(/\n+/).map(v => v.trim()).filter(Boolean);
        const platformTitle = platform === 'xiaohongshu' ? (best.querySelector('.title')?.innerText || '') : '';
        const title = platformTitle || link.getAttribute('title') || link.getAttribute('aria-label') || image?.alt || lines.find(v => v.length > 5 && !/^\d{1,2}:\d{2}$/.test(v) && !/^\d+(\.\d+)?[万wWkK]?$/.test(v) && !/^@/.test(v) && v !== '合集') || '';
        const authorNode = best.querySelector('[class*="author"] [class*="name"],[class*="author"],[class*="user-name"],[class*="nickname"]');
        let metricText = '';
        if (platform === 'xiaohongshu') metricText = (best.querySelector('.like-wrapper .count')?.innerText || '').trim();
        else {
          const durationIndex = lines.findIndex(v => /^\d{1,2}:\d{2}$/.test(v));
          if (durationIndex >= 0 && /^\d+(\.\d+)?[万wWkK]?$/.test(lines[durationIndex + 1] || '')) metricText = lines[durationIndex + 1];
        }
        seen.add(href); output.push({href, title, text: bestText, author: (authorNode?.innerText || '').trim(), metricText});
        if (output.length >= limit) break;
      }
      const visibleChallenge = [...document.querySelectorAll('iframe')].some(frame => {
        const src = String(frame.src || '');
        if (!/(?:captcha|verify|challenge)/i.test(src)) return false;
        const style = getComputedStyle(frame); const rect = frame.getBoundingClientRect();
        return style.display !== 'none' && style.visibility !== 'hidden' && Number(style.opacity || 1) > 0 && rect.width > 16 && rect.height > 16;
      });
      return {
        rows: output,
        bodyText: (document.body?.innerText || '').slice(0, 3000),
        pageTitle: document.title || '',
        url: location.href,
        resultCardCount: platform === 'douyin' ? douyinCards.length : document.querySelectorAll('section.note-item,[class*="note-item"]').length,
        accessHints: visibleChallenge ? '安全验证' : ''
      };
    })()'''.replace("__PLATFORM__", json.dumps(platform, ensure_ascii=False)).replace("__LIMIT__", str(min(max(int(limit), 1), 24)))


def _remaining_budget(seconds: float, deadline: float | None, cancel_event: threading.Event | None) -> float:
    if cancel_event is not None and cancel_event.is_set():
        raise TimeoutError("archive collection cancelled")
    remaining = seconds if deadline is None else min(seconds, deadline - time.monotonic())
    if remaining <= 0:
        raise TimeoutError("archive collection deadline exceeded")
    return remaining


def _wait_for_xhs_recovery_search(page: Any, post_id: str, *, timeout_seconds: float = 5.0) -> dict[str, Any]:
    """Let search cards hydrate before declaring a saved post unrecoverable.

    Finding the first card is not evidence that the target post is absent.
    Only an exact post ID can supply a fresh access URL; similar titles remain
    diagnostics, never substitutes. This reads the ordinary loaded search UI.
    """
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    expression = "() => " + _search_card_expression("xiaohongshu", 24)
    previous: tuple[str, ...] = ()
    stable = 0
    snapshot: dict[str, Any] = {}
    stop_reason = "invalid_snapshot"
    while True:
        value = page.evaluate(expression)
        if not isinstance(value, dict):
            break
        snapshot = value
        rows = value.get("rows") or []
        state = _page_access_state(f"{value.get('url')} {value.get('pageTitle')} {value.get('bodyText')} {value.get('accessHints')}")
        if state in {"login_required", "verification_required", "platform_limited"}:
            stop_reason = state
            break
        if _best_xhs_search_result(rows, post_id, ""):
            stop_reason = "exact_id_found"
            break
        fingerprint = tuple(str(row.get("href") or "") for row in rows if isinstance(row, dict))
        stable = stable + 1 if fingerprint and fingerprint == previous else 0
        previous = fingerprint
        if stable >= 4:
            stop_reason = "stable_results"
            break
        if time.monotonic() >= deadline:
            stop_reason = "timeout"
            break
        page.wait_for_timeout(250)
    rows = snapshot.get("rows") if isinstance(snapshot.get("rows"), list) else []
    observed_ids = {post_key for row in rows if (post_key := _xhs_search_result_candidate(row)[1])}
    # This nested object is safe to persist. The surrounding snapshot and its
    # result links are ephemeral browser data and must not be logged.
    return {**snapshot, "recovery_diagnostics": {
        "recovery_result_count": sum(isinstance(row, dict) for row in rows),
        "recovery_unique_id_count": len(observed_ids),
        "recovery_stop_reason": stop_reason,
        "exact_id_found": post_id in observed_ids,
    }}


@contextmanager
def _deadline_lock(lock: Any, deadline: float | None, cancel_event: threading.Event | None):
    if deadline is None and cancel_event is None:
        with lock:
            yield
        return
    while not lock.acquire(timeout=_remaining_budget(0.2, deadline, cancel_event)):
        pass
    try:
        _remaining_budget(1, deadline, cancel_event)
        yield
    finally:
        lock.release()


class _HiddenCDPTarget:
    """A short-lived background Chrome target used for unattended reads.

    Playwright's CDP connection intentionally exposes only visible pages.  A
    raw DevTools target lets the workbench read a page without focusing Chrome
    or accumulating tabs.  ``background=True`` alone only means that Chrome
    does not focus the new target: it still creates a regular tab that can be
    seen in the tab strip while a slow archive is running.  That was the
    source of the unexpected ``/explore/<post-id>`` pages after a radar
    refresh.

    Every unattended target therefore uses Chrome's stronger
    ``hidden=True`` option as well.  If this Chrome does not support a hidden
    target, collection fails explicitly; it must *not* silently fall back to
    a visible background tab.  Login/captcha handling has its own explicit
    user-action page and is the only path allowed to create a visible target.
    """

    def __init__(self, websocket_url: str) -> None:
        self.websocket_url = websocket_url
        self.socket: Any = None
        self.target_id = ""
        self.session_id = ""
        self.creation_mode = "hidden"
        self._next_id = 1
        self._child_messages: list[dict[str, Any]] = []
        self._events: list[dict[str, Any]] = []
        self.deadline: float | None = None
        self.cancel_event: threading.Event | None = None

    def remaining_budget(self, seconds: float) -> float:
        return _remaining_budget(seconds, self.deadline, self.cancel_event)

    def __enter__(self) -> "_HiddenCDPTarget":
        try:
            from websockets.sync.client import connect
        except Exception as exc:  # pragma: no cover - dependency is pinned
            raise PlatformBrowserError("后台采集组件不可用，请重新安装工作台依赖", code="background_collector_unavailable") from exc
        try:
            self.socket = connect(
                self.websocket_url,
                proxy=None,
                open_timeout=self.remaining_budget(6),
                close_timeout=1,
                ping_interval=None,
                # Network.getResponseBody returns base64 for binary media, so
                # allow one bounded 32 MiB image plus encoding overhead.
                max_size=48 * 1024 * 1024,
            )
            # Do not weaken this to ``background=True`` alone.  Chrome shows
            # such a target as an ordinary, unfocused tab, which violates the
            # unattended-collection contract while an archive takes time.
            target = self._root_call("Target.createTarget", {
                "url": "about:blank",
                "background": True,
                "hidden": True,
            })
            self.target_id = str((target.get("result") or {}).get("targetId") or "")
            if not self.target_id:
                raise RuntimeError("hidden target id missing")
            attached = self._root_call("Target.attachToTarget", {
                "targetId": self.target_id,
                "flatten": False,
            })
            self.session_id = str((attached.get("result") or {}).get("sessionId") or "")
            if not self.session_id:
                raise RuntimeError("hidden target session missing")
            self.call("Page.enable")
            self.call("Runtime.enable")
            # Hidden targets report a visible document in current Chrome, but
            # explicitly restoring the active lifecycle state avoids an
            # implementation-dependent frozen/throttled document.  This does
            # not focus or expose the target.  Older Chrome builds may not
            # implement this command; hidden-target support itself remains the
            # non-negotiable requirement.
            try:
                self.call("Page.setWebLifecycleState", {"state": "active"})
            except Exception:
                pass
            return self
        except (PlatformBrowserError, TimeoutError):
            self.close()
            raise
        except Exception as exc:
            self.close()
            if "Hidden target can be created only when remote debugging is enabled" in str(exc):
                # Chrome also emits this message when its debugging endpoint
                # is running but the last frame target has been closed.
                # Retrying collection cannot restore that missing page.
                raise PlatformBrowserError(
                    _BROWSER_PAGE_REQUIRED_MESSAGE, code="browser_page_required",
                ) from exc
            raise PlatformBrowserError("后台采集浏览器连接失败，请稍后重新刷新", code="background_collector_unavailable") from exc

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.close()

    def _read(self, timeout: float = 8.0) -> dict[str, Any] | None:
        if self.socket is None:
            raise RuntimeError("CDP socket closed")
        read_deadline = time.monotonic() + self.remaining_budget(timeout)
        while True:
            remaining = self.remaining_budget(read_deadline - time.monotonic())
            try:
                # A cancelled archive wakes within one polling interval. The
                # collector thread owns all socket reads and socket cleanup.
                raw = self.socket.recv(timeout=min(remaining, 0.2) if self.cancel_event is not None else remaining)
                break
            except TimeoutError:
                self.remaining_budget(read_deadline - time.monotonic())
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        message = json.loads(str(raw))
        if message.get("method") == "Target.receivedMessageFromTarget":
            params = message.get("params") if isinstance(message.get("params"), dict) else {}
            if str(params.get("sessionId") or "") == self.session_id:
                try:
                    child = json.loads(str(params.get("message") or "{}"))
                except json.JSONDecodeError:
                    child = {}
                if isinstance(child, dict):
                    if child.get("method"):
                        self._events.append(child)
                    else:
                        self._child_messages.append(child)
            return None
        return message if isinstance(message, dict) else None

    def _root_call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.socket is None:
            raise RuntimeError("CDP socket closed")
        call_budget = self.remaining_budget(10)
        request_id = self._next_id
        self._next_id += 1
        self.socket.send(json.dumps({"id": request_id, "method": method, "params": params or {}}))
        deadline = time.monotonic() + call_budget
        while time.monotonic() < deadline:
            message = self._read(max(0.05, deadline - time.monotonic()))
            if message and message.get("id") == request_id:
                if message.get("error"):
                    raise RuntimeError(str(message["error"]))
                return message
        raise TimeoutError(f"CDP {method} timed out")

    def _pop_child(self, request_id: int) -> dict[str, Any] | None:
        for index, message in enumerate(self._child_messages):
            if message.get("id") == request_id:
                return self._child_messages.pop(index)
        return None

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.socket is None or not self.session_id:
            raise RuntimeError("CDP target is not attached")
        call_budget = self.remaining_budget(15)
        child_id = self._next_id
        self._next_id += 1
        root_id = self._next_id
        self._next_id += 1
        child_message = json.dumps({"id": child_id, "method": method, "params": params or {}})
        self.socket.send(json.dumps({
            "id": root_id,
            "method": "Target.sendMessageToTarget",
            "params": {"sessionId": self.session_id, "message": child_message},
        }))
        root_done = False
        deadline = time.monotonic() + call_budget
        while time.monotonic() < deadline:
            # A child Runtime result can legitimately arrive before the root
            # Target.sendMessageToTarget acknowledgement.  Do not pop it until
            # that ACK is seen, otherwise the result is silently discarded and
            # the background collector waits until its timeout.
            if root_done:
                child = self._pop_child(child_id)
                if child is not None:
                    if child.get("error"):
                        raise RuntimeError(str(child["error"]))
                    return child
            message = self._read(max(0.05, deadline - time.monotonic()))
            if message and message.get("id") == root_id:
                if message.get("error"):
                    raise RuntimeError(str(message["error"]))
                root_done = True
        raise TimeoutError(f"target {method} timed out")

    def navigate(self, url: str) -> None:
        self.call("Page.navigate", {"url": url})

    def evaluate(self, expression: str) -> Any:
        response = self.call("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
            "userGesture": False,
        })
        result = (response.get("result") or {}).get("result") or {}
        if "value" in result:
            return result["value"]
        if (response.get("result") or {}).get("exceptionDetails"):
            raise RuntimeError("page script failed")
        return None

    def enable_network(
        self,
        *,
        max_total_buffer_size: int = 192 * 1024 * 1024,
        max_resource_buffer_size: int = 32 * 1024 * 1024,
    ) -> None:
        # Keep enough short-lived CDP buffer for a normal multi-image note.
        # Individual resources remain bounded and archive code still marks a
        # missing body as PARTIAL rather than claiming a complete original.
        self.call("Network.enable", {
            "maxTotalBufferSize": max(1024 * 1024, int(max_total_buffer_size)),
            "maxResourceBufferSize": max(1024 * 1024, int(max_resource_buffer_size)),
        })
        try:
            # Video URLs may navigate to the same post's /note route. Keep
            # already received bodies across a renderer change, within the
            # same short-lived, bounded DevTools session. This does not replay
            # requests or preserve cookies/profile data outside Chrome.
            self.call("Network.configureDurableMessages", {
                "maxTotalBufferSize": max(1024 * 1024, int(max_total_buffer_size)),
                "maxResourceBufferSize": max(1024 * 1024, int(max_resource_buffer_size)),
            })
        except Exception:
            # Older Chrome versions lack this optional command. DOM/state
            # parsing and ordinary network response reading remain available.
            pass

    def take_network_responses(self) -> list[dict[str, Any]]:
        """Return response metadata observed while this short-lived page ran."""
        responses: list[dict[str, Any]] = []
        remaining: list[dict[str, Any]] = []
        for event in self._events:
            if event.get("method") != "Network.responseReceived":
                remaining.append(event)
                continue
            params = event.get("params") if isinstance(event.get("params"), dict) else {}
            response = params.get("response") if isinstance(params.get("response"), dict) else {}
            responses.append({
                "request_id": str(params.get("requestId") or ""),
                "url": str(response.get("url") or ""),
                "status": int(response.get("status") or 0),
                "headers": dict(response.get("headers") or {}),
            })
        self._events = remaining
        return responses

    def response_body(self, request_id: str) -> bytes | None:
        if not request_id:
            return None
        # ResponseReceived precedes loadingFinished.  Asking Chrome for the
        # body too early is intermittent for multi-image posts, so wait a
        # bounded interval for the matching completion event first.
        # In normal operation loadingFinished is already queued.  If Chrome
        # omits it (for example a cache edge case), wait only briefly before
        # attempting the body so one missing event cannot stall a 6+ image
        # archive for minutes.
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if any(
                event.get("method") in {"Network.loadingFinished", "Network.loadingFailed"}
                and str((event.get("params") or {}).get("requestId") or "") == request_id
                for event in self._events
            ):
                break
            try:
                self._read(min(0.35, max(0.05, deadline - time.monotonic())))
            except Exception:
                break
        response = self.call("Network.getResponseBody", {"requestId": request_id})
        result = response.get("result") if isinstance(response.get("result"), dict) else {}
        body = result.get("body")
        if not isinstance(body, str):
            return None
        try:
            return base64.b64decode(body) if result.get("base64Encoded") else body.encode("utf-8")
        except Exception:
            return None

    def load_network_resource(self, url: str, *, max_bytes: int = 25 * 1024 * 1024) -> dict[str, Any]:
        """Load a page resource through Chrome without exposing a new tab.

        ``fetch()`` inside the page is subject to the CDN's CORS policy.  XHS
        carousel URLs are often perfectly valid in the authorized browser but
        reject that JavaScript fetch, which previously left every image as
        ``HTTP_0``.  CDP's Network.loadNetworkResource uses the same browser
        session and returns an IO stream without navigating or showing a page.
        """
        frame_tree = self.call("Page.getFrameTree")
        frame_id = str(
            ((((frame_tree.get("result") or {}).get("frameTree") or {}).get("frame") or {}).get("id") or "")
        )
        if not frame_id:
            raise RuntimeError("page frame id missing")
        response = self.call("Network.loadNetworkResource", {
            "frameId": frame_id,
            "url": url,
            "options": {"disableCache": False, "includeCredentials": True},
        })
        resource = ((response.get("result") or {}).get("resource") or {})
        status = int(resource.get("httpStatusCode") or 0)
        headers = {str(key).lower(): str(value) for key, value in dict(resource.get("headers") or {}).items()}
        stream = str(resource.get("stream") or "")
        payload = bytearray()
        try:
            while stream:
                part = (self.call("IO.read", {"handle": stream, "size": 1024 * 1024}).get("result") or {})
                raw = str(part.get("data") or "")
                chunk = base64.b64decode(raw) if part.get("base64Encoded") else raw.encode("utf-8")
                if len(payload) + len(chunk) > max_bytes:
                    raise RuntimeError("network resource exceeds archive limit")
                payload.extend(chunk)
                if part.get("eof"):
                    break
        finally:
            if stream:
                try:
                    self.call("IO.close", {"handle": stream})
                except Exception:
                    pass
        return {
            "success": bool(resource.get("success")),
            "status": status,
            "headers": headers,
            "body": bytes(payload),
            "net_error": str(resource.get("netErrorName") or ""),
        }

    def load_network_resource_to_file(
        self,
        url: str,
        target_path: str | Path,
        *,
        max_bytes: int = MAX_MEDIA_BYTES,
        timeout_seconds: float = 240,
    ) -> dict[str, Any]:
        """Stream a browser-authorized public media response to a temporary file.

        Unlike ``load_network_resource`` this does not buffer a video in RAM.
        The caller owns a TemporaryDirectory and deletes the file after local
        ASR, so no signed media URL or video archive is persisted.
        """
        frame_tree = self.call("Page.getFrameTree")
        frame_id = str(
            ((((frame_tree.get("result") or {}).get("frameTree") or {}).get("frame") or {}).get("id") or "")
        )
        if not frame_id:
            raise RuntimeError("page frame id missing")
        response = self.call("Network.loadNetworkResource", {
            "frameId": frame_id,
            "url": url,
            "options": {"disableCache": False, "includeCredentials": True},
        })
        resource = ((response.get("result") or {}).get("resource") or {})
        status = int(resource.get("httpStatusCode") or 0)
        headers = {str(key).lower(): str(value) for key, value in dict(resource.get("headers") or {}).items()}
        stream = str(resource.get("stream") or "")
        output_path = Path(target_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        content_length = headers.get("content-length", "").strip()
        if content_length.isdigit() and int(content_length) > max_bytes:
            if stream:
                try:
                    self.call("IO.close", {"handle": stream})
                except Exception:
                    pass
            raise RuntimeError("browser media exceeds the local processing limit")
        written = 0
        deadline = time.monotonic() + timeout_seconds
        try:
            with output_path.open("wb") as output:
                while stream:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("browser media transfer timed out")
                    part = (self.call("IO.read", {"handle": stream, "size": 1024 * 1024}).get("result") or {})
                    raw = str(part.get("data") or "")
                    chunk = base64.b64decode(raw) if part.get("base64Encoded") else raw.encode("utf-8")
                    written += len(chunk)
                    if written > max_bytes:
                        raise RuntimeError("browser media exceeds the local processing limit")
                    output.write(chunk)
                    if part.get("eof"):
                        break
            if status not in {200, 206} or written == 0:
                raise RuntimeError("browser media response was empty or unavailable")
            with output_path.open("rb") as source:
                if source.read(64).lstrip().startswith(b"#EXTM3U"):
                    raise RuntimeError("playlist media is not directly transcribed")
        except Exception:
            output_path.unlink(missing_ok=True)
            raise
        finally:
            if stream:
                try:
                    self.call("IO.close", {"handle": stream})
                except Exception:
                    pass
        return {
            "success": bool(resource.get("success")),
            "status": status,
            "headers": headers,
            "bytes_written": written,
            "net_error": str(resource.get("netErrorName") or ""),
        }

    def capture_screenshot(self, path: str | Path, *, full_page: bool = False, clip: dict[str, float] | None = None) -> None:
        params: dict[str, Any] = {"format": "png", "captureBeyondViewport": bool(full_page)}
        if clip:
            params["clip"] = {**clip, "scale": 1}
        response = self.call("Page.captureScreenshot", params)
        payload = str(((response.get("result") or {}).get("data") or ""))
        if not payload:
            raise RuntimeError("screenshot data missing")
        Path(path).write_bytes(base64.b64decode(payload))

    def close(self) -> None:
        if self.socket is None:
            return
        if self.deadline is not None or self.cancel_event is not None:
            # Cleanup is allowed a small independent budget on this owning
            # thread even when normal work has exhausted its deadline.
            self.deadline = time.monotonic() + 0.5
            self.cancel_event = None
        try:
            if self.target_id:
                self._root_call("Target.closeTarget", {"targetId": self.target_id})
        except Exception:
            pass
        try:
            self.socket.close()
        except Exception:
            pass
        self.socket = None
        self.target_id = ""
        self.session_id = ""


class _BackgroundResponse:
    """Small Playwright-response-shaped value backed by CDP page state."""

    def __init__(self, *, url: str, status: int, headers: dict[str, Any], body_loader: Any) -> None:
        self.url = url
        self.status = status
        self.headers = headers
        self._body_loader = body_loader

    def body(self) -> bytes:
        value = self._body_loader()
        return value if isinstance(value, bytes) else b""


class _BackgroundElementLocator:
    def __init__(self, page: "_BackgroundPage", selector: str, index: int) -> None:
        self.page = page
        self.selector = selector
        self.index = index

    def bounding_box(self) -> dict[str, float] | None:
        return self.page.evaluate(
            r"""({selector, index}) => {
              const node = document.querySelectorAll(selector)[index];
              if (!node) return null;
              const rect = node.getBoundingClientRect();
              return rect.width > 0 && rect.height > 0 ? {x:rect.x,y:rect.y,width:rect.width,height:rect.height} : null;
            }""",
            {"selector": self.selector, "index": self.index},
        )

    def screenshot(self, *, path: str) -> None:
        box = self.bounding_box()
        if not box:
            raise RuntimeError("element is not visible")
        self.page.screenshot(path=path, clip=box)


class _BackgroundLocator:
    def __init__(self, page: "_BackgroundPage", selector: str) -> None:
        self.page = page
        self.selector = selector

    def count(self) -> int:
        value = self.page.evaluate("selector => document.querySelectorAll(selector).length", self.selector)
        return int(value or 0)

    def nth(self, index: int) -> _BackgroundElementLocator:
        return _BackgroundElementLocator(self.page, self.selector, index)


class _BackgroundTextLocator:
    def __init__(self, page: "_BackgroundPage", pattern: Any, index: int) -> None:
        self.page = page
        self.pattern = pattern
        self.index = index

    def _pattern_payload(self) -> dict[str, Any]:
        return {"source": str(getattr(self.pattern, "pattern", self.pattern)), "flags": str(getattr(self.pattern, "flags", "")), "index": self.index}

    def is_visible(self) -> bool:
        value = self.page.evaluate(
            r"""({source, flags, index}) => {
              let rx; try { rx = new RegExp(source, flags.includes('i') ? 'i' : ''); } catch { return false; }
              const nodes=[...document.querySelectorAll('body *')].filter(node=>{
                const text=(node.innerText||node.textContent||'').trim();
                const rect=node.getBoundingClientRect();
                return rect.width>0&&rect.height>0&&rx.test(text)&&[...node.children].every(child=>!rx.test((child.innerText||child.textContent||'').trim()));
              });
              return !!nodes[index];
            }""",
            self._pattern_payload(),
        )
        return bool(value)

    def click(self, timeout: int = 1200) -> None:  # noqa: ARG002 - Playwright-compatible shape
        value = self.page.evaluate(
            r"""({source, flags, index}) => {
              let rx; try { rx = new RegExp(source, flags.includes('i') ? 'i' : ''); } catch { return false; }
              const nodes=[...document.querySelectorAll('body *')].filter(node=>{
                const text=(node.innerText||node.textContent||'').trim();
                const rect=node.getBoundingClientRect();
                return rect.width>0&&rect.height>0&&rx.test(text)&&[...node.children].every(child=>!rx.test((child.innerText||child.textContent||'').trim()));
              });
              const node=nodes[index]; if (!node) return false; node.click(); return true;
            }""",
            self._pattern_payload(),
        )
        if not value:
            raise RuntimeError("matching text is not visible")


class _BackgroundTextLocators:
    def __init__(self, page: "_BackgroundPage", pattern: Any) -> None:
        self.page = page
        self.pattern = pattern

    def all(self) -> list[_BackgroundTextLocator]:
        source = str(getattr(self.pattern, "pattern", self.pattern))
        flags = str(getattr(self.pattern, "flags", ""))
        value = self.page.evaluate(
            r"""({source, flags}) => {
              let rx; try { rx = new RegExp(source, flags.includes('i') ? 'i' : ''); } catch { return 0; }
              return [...document.querySelectorAll('body *')].filter(node=>{
                const text=(node.innerText||node.textContent||'').trim();
                const rect=node.getBoundingClientRect();
                return rect.width>0&&rect.height>0&&rx.test(text)&&[...node.children].every(child=>!rx.test((child.innerText||child.textContent||'').trim()));
              }).length;
            }""",
            {"source": source, "flags": flags},
        )
        return [_BackgroundTextLocator(self.page, self.pattern, index) for index in range(int(value or 0))]


class _BackgroundRequest:
    def __init__(self, page: "_BackgroundPage") -> None:
        self.page = page

    def get(self, url: str, *, timeout: int = 15000, fail_on_status_code: bool = False) -> _BackgroundResponse:  # noqa: ARG002
        return self.page.fetch(url)


class _BackgroundContext:
    def __init__(self, page: "_BackgroundPage") -> None:
        self.request = _BackgroundRequest(page)


class _BackgroundPage:
    """A minimal Page-compatible adapter around a disposable CDP target.

    It intentionally implements only the operations used by the XHS detail
    readers.  This keeps normal automatic extraction and archive work in the
    same short-lived background target as search, while retaining the existing
    DOM/identity/asset logic and the logged-in browser session.
    """

    def __init__(self, websocket_url: str) -> None:
        self._target = _HiddenCDPTarget(websocket_url)
        self.context = _BackgroundContext(self)
        self._response_handlers: list[Any] = []
        self._responses: dict[str, _BackgroundResponse] = {}
        self._closed = False

    def set_budget(self, deadline: float | None, cancel_event: threading.Event | None) -> None:
        self._target.deadline = deadline
        self._target.cancel_event = cancel_event

    def __enter__(self) -> "_BackgroundPage":
        try:
            self._target.__enter__()
            self._target.enable_network()
            return self
        except BaseException:
            self.close()
            raise

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.close()

    def _dispatch_network_events(self) -> None:
        for item in self._target.take_network_responses():
            url = str(item.get("url") or "")
            if not url:
                continue
            headers = {str(key).lower(): str(value) for key, value in dict(item.get("headers") or {}).items()}
            request_id = str(item.get("request_id") or "")
            response = _BackgroundResponse(
                url=url,
                status=int(item.get("status") or 0),
                headers=headers,
                body_loader=lambda request_id=request_id: self._target.response_body(request_id),
            )
            self._responses[url] = response
            for handler in list(self._response_handlers):
                try:
                    handler(response)
                except Exception:
                    pass

    def goto(self, url: str, **_kwargs: Any) -> None:
        self._target.navigate(url)
        self.wait_for_timeout(450)

    def wait_for_timeout(self, milliseconds: int) -> None:
        # Do not block an unbounded amount of time; command evaluations also
        # drain Network events that arrived after navigation.
        seconds = max(0, min(int(milliseconds), 5000)) / 1000
        if seconds:
            seconds = self._target.remaining_budget(seconds)
            if self._target.cancel_event is not None:
                self._target.cancel_event.wait(seconds)
            else:
                time.sleep(seconds)
        self._target.remaining_budget(1)
        try:
            self._target.evaluate("0")
        finally:
            self._dispatch_network_events()

    def wait_for_selector(self, selector: str, **kwargs: Any) -> None:
        timeout = max(100, min(int(kwargs.get("timeout", 5000)), 15000))
        deadline = time.monotonic() + timeout / 1000
        while time.monotonic() < deadline:
            value = self.evaluate("selector => !!document.querySelector(selector)", selector)
            if value:
                return
            self.wait_for_timeout(160)
        raise TimeoutError(f"selector not found: {selector}")

    def evaluate(self, script: str, arg: Any = None) -> Any:
        expression = f"({script})({json.dumps(arg, ensure_ascii=False)})" if arg is not None else f"({script})()"
        value = self._target.evaluate(expression)
        self._dispatch_network_events()
        return value

    def on(self, event: str, handler: Any) -> None:
        if event == "response":
            self._response_handlers.append(handler)

    def get_by_text(self, pattern: Any) -> _BackgroundTextLocators:
        return _BackgroundTextLocators(self, pattern)

    def locator(self, selector: str) -> _BackgroundLocator:
        return _BackgroundLocator(self, selector)

    def screenshot(self, *, path: str, full_page: bool = False, timeout: int = 5000, clip: dict[str, float] | None = None) -> None:  # noqa: ARG002
        self._target.capture_screenshot(path, full_page=full_page, clip=clip)

    def fetch(self, url: str) -> _BackgroundResponse:
        self._dispatch_network_events()
        if url in self._responses:
            return self._responses[url]
        try:
            loaded = self._target.load_network_resource(url)
            if loaded.get("success") or int(loaded.get("status") or 0):
                body = loaded.get("body") if isinstance(loaded.get("body"), bytes) else b""
                return _BackgroundResponse(
                    url=url,
                    status=int(loaded.get("status") or 0),
                    headers=dict(loaded.get("headers") or {}),
                    body_loader=lambda body=body: body,
                )
        except TimeoutError:
            raise
        except Exception:
            pass
        # Same-page fetch keeps the dedicated profile's authorized session in
        # Chrome.  It is only a fallback when a candidate image was not among
        # the browser responses observed during the page load.
        value = self.evaluate(
            r"""async url => {
              try {
                const response = await fetch(url, {credentials:'include'});
                const bytes = new Uint8Array(await response.arrayBuffer());
                let binary=''; const chunk=0x8000;
                for(let index=0; index<bytes.length; index+=chunk) binary += String.fromCharCode(...bytes.subarray(index,index+chunk));
                return {status:response.status,headers:[...response.headers.entries()],body:btoa(binary)};
              } catch (error) { return {status:0,headers:[],error:String(error||'fetch failed'),body:''}; }
            }""",
            url,
        ) or {}
        headers: dict[str, str] = {}
        for entry in value.get("headers") or []:
            if not isinstance(entry, list | tuple) or len(entry) != 2:
                continue
            key, header_value = entry
            if isinstance(key, str):
                headers[key.lower()] = str(header_value)
        payload = str(value.get("body") or "")
        try:
            body = base64.b64decode(payload) if payload else b""
        except Exception:
            body = b""
        return _BackgroundResponse(
            url=url,
            status=int(value.get("status") or 0),
            headers=headers,
            body_loader=lambda: body,
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._target.close()



def _run_local_ocr(image_path: Path) -> dict[str, str]:
    swift = shutil.which("swift")
    if sys.platform != "darwin" or not swift or not OCR_SCRIPT.is_file():
        return {"status": "unavailable", "text": "", "message": "图片文字尚未提取：OCR（光学字符识别）运行环境不可用。"}
    try:
        result = subprocess.run([swift, str(OCR_SCRIPT), str(image_path)], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "failed", "text": "", "message": f"图片文字尚未提取：OCR 运行失败（{str(exc)[:120]}）。"}
    text = result.stdout.strip()
    if result.returncode != 0:
        return {"status": "failed", "text": "", "message": f"图片文字尚未提取：OCR 运行失败（{result.stderr.strip()[:120]}）。"}
    return {"status": "success" if text else "empty", "text": text, "message": "已使用 macOS Vision 在本机识别图片文字。" if text else "OCR 已运行，但没有识别到清晰文字。"}


class PlatformBrowserCollector:
    """Collect rendered platform cards through a dedicated local browser.

    The browser profile keeps the platform's own login session.  The workbench
    never asks for a password, exports cookies, or attempts to solve a captcha.
    Scheduled searches use disposable background CDP targets.  A foreground
    platform page is created only after the user explicitly asks to open the
    login/verification page.
    """

    def __init__(self, cdp_url: str = CDP_URL, profile_dir: Path = PROFILE_DIR) -> None:
        self.cdp_url = cdp_url.rstrip("/")
        self.profile_dir = profile_dir
        self._lock = threading.RLock()
        # Xiaohongshu result cards carry a short-lived xsec_token.  Persist only
        # the clean public URL, but keep the access URL in this process so the
        # automatic detail reader can reuse it immediately after a search.
        self._xhs_access_urls: dict[str, tuple[str, float]] = {}
        self._asr_capability: dict[str, Any] | None = None
        self._platform_sessions: dict[str, dict[str, str]] = {
            platform: {"state": _SESSION_UNKNOWN, "message": "尚未完成本轮页面核验"}
            for platform in ("xiaohongshu", "douyin")
        }
        # A user-action page is never reused by unattended search.  We retain
        # its target id only so repeated explicit "open login page" clicks
        # reuse the same page rather than creating a tab storm.
        self._user_action_targets: dict[str, str] = {}

    def _transcribe_douyin_media(self, page: _HiddenCDPTarget, candidates: Any) -> dict[str, Any]:
        urls: list[str] = []
        if isinstance(candidates, list):
            for candidate in candidates:
                safe = _safe_douyin_media_url(candidate)
                if safe and safe not in urls:
                    urls.append(safe)
        if not urls:
            return {
                "asr_status": "MEDIA_UNAVAILABLE", "asr_source": "BROWSER_LOGGED_IN_MEDIA",
                "asr_message": "详情页没有提供可直接读取的公开视频媒体地址；发布文案与页面字幕仍已分开保存。",
                "video_status": "MEDIA_UNAVAILABLE",
            }

        if self._asr_capability is None:
            try:
                self._asr_capability = local_asr_status()
            except SpeechTranscriptionError as exc:
                self._asr_capability = {"available": False, "installed": False, "message": str(exc)}
        capability = self._asr_capability
        if not capability.get("available") or not capability.get("installed"):
            message = str(capability.get("message") or "本机简体中文语音模型尚未就绪")
            return {
                "asr_status": "MODEL_REQUIRED", "asr_source": "BROWSER_LOGGED_IN_MEDIA",
                "asr_message": f"{message}；请先用本机导入转写完成模型初始化，再重试登录态批量转写。",
                "video_status": "MEDIA_UNAVAILABLE",
            }

        failures: list[str] = []
        for media_url in urls[:3]:
            suffix = Path(urllib.parse.urlsplit(media_url).path).suffix.lower()
            if suffix not in ALLOWED_MEDIA_SUFFIXES:
                suffix = ".mp4"
            try:
                with tempfile.TemporaryDirectory(prefix="douyin-browser-asr-") as temporary_directory:
                    media_path = Path(temporary_directory) / f"media{suffix}"
                    response = page.load_network_resource_to_file(
                        media_url, media_path, max_bytes=MAX_MEDIA_BYTES, timeout_seconds=240,
                    )
                    content_type = str((response.get("headers") or {}).get("content-type") or "").lower()
                    if "mpegurl" in content_type or "dash+xml" in content_type:
                        raise RuntimeError("播放列表不是单文件媒体")
                    if not response.get("success"):
                        raise RuntimeError("浏览器没有返回可处理的媒体流")
                    result = transcribe_media_path(
                        media_path,
                        source="BROWSER_LOGGED_IN_MEDIA",
                        message="使用工作台专用 Chrome 当前登录会话读取本条可访问作品；媒体只在本机临时目录用于转写，完成后自动删除，不上传云端或长期归档。该会话不能访问的作品不会绕过登录、验证码或平台限制。机器识别结果建议人工核对。",
                    )
                    return {
                        "asr_status": "BROWSER_MEDIA_TRANSCRIBED",
                        "asr_source": result["source"], "asr_text": result["text"],
                        "asr_segments": result.get("segments") or [],
                        "asr_confidence": result.get("confidence") or {},
                        "asr_low_confidence": result.get("low_confidence") or [],
                        "asr_language": result["language"], "asr_engine": result["engine"],
                        "asr_model": result["model"], "asr_media_sha256": result["media_sha256"],
                        "asr_message": result["message"],
                        "asr_text_truncated": bool(result.get("text_truncated")),
                        "video_status": "TRANSIENT_MEDIA_PROCESSED",
                        "diagnostics": {"media_bytes": result["media_bytes"], "duration_seconds": result["duration_seconds"]},
                    }
            except SpeechTranscriptionError as exc:
                failures.append(str(exc))
            except Exception:
                failures.append("本机无法读取此作品的媒体流")
        message = failures[-1] if failures else "没有取得可处理的媒体流"
        return {
            "asr_status": "TRANSCRIPTION_FAILED", "asr_source": "BROWSER_LOGGED_IN_MEDIA",
            "asr_message": f"登录态媒体转写未完成：{message}。可见发布文案/字幕不受影响；不会把它们冒充为完整口播。",
            "video_status": "MEDIA_UNAVAILABLE",
        }

    def _set_session_state(self, platform: str, state: str, message: str = "") -> None:
        if platform not in self._platform_sessions:
            return
        clean_state = state if state in _SESSION_STATES else _SESSION_UNKNOWN
        self._platform_sessions[platform] = {
            "state": clean_state,
            "message": str(message or "")[:240],
        }

    def _session_state(self, platform: str) -> str:
        return str(self._platform_sessions.get(platform, {}).get("state") or _SESSION_UNKNOWN)

    def reset_asr_capability_cache(self) -> None:
        """Refresh model readiness after a user-approved local transcription."""
        self._asr_capability = None

    def _browser_websocket_url(self) -> str:
        try:
            response = httpx.get(f"{self.cdp_url}/json/version", timeout=1.5, trust_env=False)
            response.raise_for_status()
            websocket_url = str((response.json() or {}).get("webSocketDebuggerUrl") or "")
        except (httpx.HTTPError, ValueError) as exc:
            raise PlatformBrowserError("后台采集浏览器暂时无法连接，请稍后重新刷新", code="browser_connect_error") from exc
        if not websocket_url.startswith(("ws://", "wss://")):
            raise PlatformBrowserError("后台采集浏览器没有返回可用连接", code="browser_connect_error")
        return websocket_url

    def _find_existing_user_action_target(self, target_url: str) -> str:
        """Find an exact visible platform page after a workbench restart.

        The in-memory target cache prevents duplicate tabs during one process
        lifetime. Chrome's local /json/list endpoint also lets an explicit
        user request reuse its exact page after the workbench process restarts.
        Unrelated tabs are never selected or navigated.
        """
        try:
            response = httpx.get(f"{self.cdp_url}/json/list", timeout=1.5, trust_env=False)
            response.raise_for_status()
            targets = response.json()
        except (httpx.HTTPError, ValueError, TypeError):
            return ""
        if not isinstance(targets, list):
            return ""

        expected_url = urllib.parse.urldefrag(str(target_url or "")).url
        for target in targets:
            if not isinstance(target, dict) or target.get("type") != "page":
                continue
            current_url = urllib.parse.urldefrag(str(target.get("url") or "")).url
            target_id = str(target.get("id") or "").strip()
            if current_url == expected_url and target_id:
                return target_id
        return ""

    def _activate_or_open_user_action_page(self, platform: str, query: str = "", action_url: str = "") -> None:
        """Show a page only when a user explicitly opens it or action is needed."""
        websocket_url = self._browser_websocket_url()
        target_url = action_url if _safe_url(platform, action_url) else _search_url(platform, query)
        existing = self._user_action_targets.get(platform, "")
        if not existing:
            existing = self._find_existing_user_action_target(target_url)
            if existing:
                self._user_action_targets[platform] = existing
        # A raw socket avoids reusing a visible page controlled by a
        # user.  Keeping this compact transport here means the normal search
        # path never creates any foreground target.
        try:
            from websockets.sync.client import connect
            socket = connect(websocket_url, proxy=None, open_timeout=4, close_timeout=1, ping_interval=None)
        except Exception:
            return
        next_id = 1

        def root_call(method: str, params: dict[str, Any]) -> dict[str, Any]:
            nonlocal next_id
            request_id = next_id
            next_id += 1
            socket.send(json.dumps({"id": request_id, "method": method, "params": params}))
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                raw = socket.recv(timeout=max(0.05, deadline - time.monotonic()))
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", "replace")
                message = json.loads(str(raw))
                if message.get("id") == request_id:
                    if message.get("error"):
                        raise RuntimeError(str(message["error"]))
                    return message
            raise TimeoutError(method)

        try:
            # Prefer the exact, remembered or rediscovered page. If Chrome
            # discarded it, create one replacement for this explicit request.
            if existing:
                try:
                    root_call("Target.activateTarget", {"targetId": existing})
                    return
                except Exception:
                    self._user_action_targets.pop(platform, None)
            created = root_call("Target.createTarget", {
                "url": target_url,
                "background": False,
            })
            target_id = str((created.get("result") or {}).get("targetId") or "")
            if target_id:
                self._user_action_targets[platform] = target_id
                root_call("Target.activateTarget", {"targetId": target_id})
        except Exception:
            # The canonical error remains the auth/risk state; this helper is
            # intentionally best effort and never opens an unrelated tab.
            pass
        finally:
            try:
                socket.close()
            except Exception:
                pass

    def _xhs_access_error(self, state: str, *, action_url: str = "", archive: bool = False) -> PlatformBrowserError:
        """Record the real state before reporting an XHS detail as missing.

        A disposable automatic reader must never steal focus for login/CAPTCHA.
        It is closed by its caller and reports the user-action state back to
        the workbench.  The user can then explicitly click ``打开登录页``;
        opening that page is intentionally kept out of this automatic path.
        """
        messages = {
            "verification_required": (
                "小红书要求完成安全验证，请在采集浏览器中处理后点击继续补齐"
                if archive else "小红书要求完成安全验证，请在采集浏览器中完成后重试"
            ),
            "login_required": (
                "小红书登录已失效，请扫码登录后点击继续补齐"
                if archive else "小红书登录已失效，请在采集浏览器扫码登录后重试"
            ),
            "platform_limited": "小红书限制了当前网络或浏览器环境，请稍后在可靠网络中重试",
            "post_unavailable": "帖子不存在、已删除或当前账号不可访问",
        }
        session_state = {
            "verification_required": _SESSION_VERIFICATION_REQUIRED,
            "login_required": _SESSION_LOGIN_REQUIRED,
            "platform_limited": _SESSION_PLATFORM_LIMITED,
        }.get(state, _SESSION_UNKNOWN)
        message = messages.get(state, "小红书详情页暂时无法读取，请稍后重试")
        self._set_session_state("xiaohongshu", session_state, message)
        return PlatformBrowserError(message, code=state)

    def _hidden_search_snapshot(self, platform: str, query: str, limit: int) -> dict[str, Any]:
        """Read one platform search page in a short-lived background target."""
        websocket_url = self._browser_websocket_url()
        target_url = _search_url(platform, query)
        try:
            with _HiddenCDPTarget(websocket_url) as target:
                target.navigate(target_url)
                # One attempt is intentionally bounded; monitoring performs a
                # single explicitly reported retry instead of leaving the UI
                # in a seemingly endless refresh.
                deadline = time.monotonic() + _BACKGROUND_SEARCH_READY_TIMEOUT_SECONDS
                last: dict[str, Any] = {}
                previous_cards: tuple[tuple[str, str], ...] = ()
                stable_polls = 0
                while time.monotonic() < deadline:
                    value = target.evaluate(_search_card_expression(platform, limit))
                    if isinstance(value, dict):
                        last = value
                        access_state = _page_access_state(
                            f"{value.get('url')} {value.get('pageTitle')} {value.get('bodyText')} {value.get('accessHints')}"
                        )
                        # A result card can quote “内容不存在/已删除” while
                        # the search page itself is perfectly healthy.  Only
                        # states that block the whole session belong here;
                        # post_unavailable is decided on the selected detail
                        # page after its identity is known.
                        if access_state in {"verification_required", "login_required", "platform_limited"}:
                            raise PlatformBrowserError(
                                "平台要求完成安全验证，请在采集浏览器中完成后重新搜索"
                                if access_state == "verification_required" else
                                "平台尚未登录，请在采集浏览器中扫码登录后重新搜索"
                                if access_state == "login_required" else
                                "平台限制了当前浏览器或网络环境，请稍后重试"
                                if access_state == "platform_limited" else
                                "平台搜索页暂时不可访问，请稍后重试",
                                code=access_state,
                            )
                        rows = value.get("rows") or []
                        if rows:
                            cards = tuple((str(row.get("href") or ""), str(row.get("title") or "")) for row in rows if isinstance(row, dict))
                            stable_polls = stable_polls + 1 if cards == previous_cards else 0
                            previous_cards = cards
                            # Search shells often paint just the first one or
                            # two cards before the rest of the page hydrates.
                            # Wait for the requested count or a settled list.
                            if len(rows) >= min(max(int(limit), 1), 24) or stable_polls >= 4:
                                return value
                    time.sleep(_BACKGROUND_SEARCH_POLL_SECONDS)
                return last
        except PlatformBrowserError:
            raise
        except Exception as exc:
            raise PlatformBrowserError("后台读取平台搜索页失败，请稍后重新刷新", code="page_read_error") from exc

    def _remember_xhs_access_url(self, value: str) -> str:
        access_url = _safe_url("xiaohongshu", value)
        public_url = _public_post_url("xiaohongshu", value)
        if not access_url or not public_url or "xsec_token=" not in access_url:
            return public_url
        now = time.monotonic()
        self._xhs_access_urls[public_url] = (access_url, now + 30 * 60)
        if len(self._xhs_access_urls) > 120:
            self._xhs_access_urls = {
                key: item for key, item in self._xhs_access_urls.items() if item[1] > now
            }
        return public_url

    def _recent_xhs_access_url(self, public_url: str) -> str:
        item = self._xhs_access_urls.get(public_url)
        if not item:
            return ""
        access_url, expires_at = item
        if expires_at <= time.monotonic():
            self._xhs_access_urls.pop(public_url, None)
            return ""
        return access_url

    @staticmethod
    def _session_action_message(sessions: dict[str, dict[str, str]]) -> str:
        """Describe the platform that actually needs intervention.

        A combined status can have Xiaohongshu ready while Douyin needs a
        security check (or vice versa).  Naming a fixed platform here sends a
        user to the wrong browser tab and makes a recoverable verification
        look broken.
        """
        labels = {"xiaohongshu": "小红书", "douyin": "抖音"}
        states = (
            (_SESSION_VERIFICATION_REQUIRED, "需要完成安全验证"),
            (_SESSION_LOGIN_REQUIRED, "需要重新登录"),
            (_SESSION_PLATFORM_LIMITED, "当前网络或平台环境暂时受限"),
        )
        parts: list[str] = []
        for state, suffix in states:
            names = [
                labels.get(platform, platform)
                for platform, value in sessions.items()
                if str(value.get("state") or _SESSION_UNKNOWN) == state
            ]
            if names:
                parts.append(f"{'、'.join(names)}{suffix}")
        return "；".join(parts)

    def status(self) -> dict[str, Any]:
        chrome = _chrome_path()
        running = False
        page_count: int | None = None
        try:
            response = httpx.get(f"{self.cdp_url}/json/version", timeout=0.8, trust_env=False)
            running = response.is_success
        except httpx.HTTPError:
            pass
        if running:
            try:
                response = httpx.get(f"{self.cdp_url}/json/list", timeout=0.8, trust_env=False)
                targets = response.json() if response.is_success else None
                if isinstance(targets, list):
                    page_count = sum(1 for target in targets if isinstance(target, dict) and target.get("type") == "page")
            except (httpx.HTTPError, ValueError):
                pass
        collector_state = (
            "OFFLINE" if not running else "BROWSER_PAGE_REQUIRED" if page_count == 0
            else "READY" if page_count is not None else "UNKNOWN"
        )
        sessions = {
            platform: dict(value) for platform, value in self._platform_sessions.items()
        }
        if collector_state in {"BROWSER_PAGE_REQUIRED", "OFFLINE"}:
            for platform in sessions:
                self._set_session_state(platform, _SESSION_UNKNOWN, "采集窗口已关闭，需要重新核验平台会话")
                sessions[platform] = dict(self._platform_sessions[platform])
        session_values = [str(value.get("state") or _SESSION_UNKNOWN) for value in sessions.values()]
        if collector_state in {"BROWSER_PAGE_REQUIRED", "OFFLINE"}:
            aggregate = _SESSION_UNKNOWN
        elif running and session_values and all(value == _SESSION_READY for value in session_values):
            aggregate = _SESSION_READY
        elif _SESSION_VERIFICATION_REQUIRED in session_values:
            aggregate = _SESSION_VERIFICATION_REQUIRED
        elif _SESSION_LOGIN_REQUIRED in session_values:
            aggregate = _SESSION_LOGIN_REQUIRED
        elif _SESSION_PLATFORM_LIMITED in session_values:
            aggregate = _SESSION_PLATFORM_LIMITED
        else:
            aggregate = _SESSION_UNKNOWN
        action_message = self._session_action_message(sessions)
        return {
            "available": bool(chrome),
            "running": running,
            "collector_state": collector_state,
            "page_count": page_count,
            "mode": "managed_background_collector",
            "profile_persistent": True,
            "session_storage": "local_chrome_profile",
            "session_state": aggregate,
            "platform_sessions": sessions,
            "message": (
                _BROWSER_PAGE_REQUIRED_MESSAGE
                if collector_state == "BROWSER_PAGE_REQUIRED" else
                "后台采集浏览器已连接；本轮已核验小红书和抖音登录会话。"
                if running and aggregate == _SESSION_READY else
                f"后台采集浏览器已连接；{action_message}。请点击“打开登录页”处理后重新验证。"
                if running and action_message else
                "后台采集浏览器已连接；尚未完成本轮平台会话核验。正常搜索不会切换到前台；需要登录或验证码时请点击“打开登录页”。"
                if running else "请先打开平台采集浏览器并扫码登录一次；以后会复用本机登录状态。"
                if chrome else "没有找到 Google Chrome 或 Microsoft Edge，请先安装浏览器。"
            ),
        }

    def _start(self, initial_url: str) -> None:
        if self.status()["running"]:
            return
        chrome = _chrome_path()
        if not chrome:
            raise PlatformBrowserError("没有找到 Google Chrome 或 Microsoft Edge", code="browser_missing")
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(
            [
                chrome,
                f"--remote-debugging-port={CDP_PORT}",
                "--remote-debugging-address=127.0.0.1",
                f"--user-data-dir={self.profile_dir}",
                "--profile-directory=Default",
                "--no-first-run",
                "--no-default-browser-check",
                initial_url,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.status()["running"]:
                return
            time.sleep(0.25)
        raise PlatformBrowserError("平台采集浏览器启动超时，请关闭后重试", code="browser_start_timeout")

    def open(self, platform: str = "both", query: str = "相亲") -> dict[str, Any]:
        """Open explicit user-action pages without using them for collection.

        Starting the dedicated Chrome profile itself uses ``about:blank`` so
        scheduled work never opens platform pages.  This endpoint is the one
        explicit user request to show login/search pages, which is useful for
        the first QR login or a security check.
        """
        platforms = ["xiaohongshu", "douyin"] if platform == "both" else [platform]
        if any(value not in {"xiaohongshu", "douyin"} for value in platforms):
            raise ValueError("请选择小红书、抖音或双平台")
        with self._lock:
            self._start("about:blank")
            for value in platforms:
                self._activate_or_open_user_action_page(value, query)
        return self.status()

    def search(self, platform: str, query: str, limit: int = 12) -> list[dict[str, Any]]:
        """Read a fresh platform page in a disposable hidden CDP target.

        This never selects or navigates an existing visible tab.  A login or
        CAPTCHA wall is returned as an explicit user-action state; the user
        must choose ``打开登录页`` before any visible platform target exists.
        """
        if platform not in {"xiaohongshu", "douyin"}:
            raise ValueError("不支持的平台")
        if not self.status()["running"]:
            self._set_session_state(platform, _SESSION_UNKNOWN, "采集浏览器未打开")
            raise PlatformBrowserError("平台采集浏览器尚未打开，请先点击“打开双平台浏览器”并登录", code="browser_not_running")
        with self._lock:
            try:
                _debug_platform(f"search:{platform}:hidden-target-start")
                rows = self._hidden_search_snapshot(platform, query, limit)
                _debug_platform(f"search:{platform}:hidden-target-done")
            except PlatformBrowserError as exc:
                state = {
                    "login_required": _SESSION_LOGIN_REQUIRED,
                    "verification_required": _SESSION_VERIFICATION_REQUIRED,
                    "platform_limited": _SESSION_PLATFORM_LIMITED,
                }.get(str(exc.code or "").lower(), _SESSION_UNKNOWN)
                self._set_session_state(platform, state, str(exc))
                raise

        raw_rows = rows.get("rows") if isinstance(rows, dict) else []
        body_text = str(rows.get("bodyText") or "") if isinstance(rows, dict) else ""
        page_title = str(rows.get("pageTitle") or "") if isinstance(rows, dict) else ""
        access_state = _page_access_state(
            f"{page_title} {body_text} {str(rows.get('accessHints') or '') if isinstance(rows, dict) else ''}"
        )
        if access_state in {"verification_required", "login_required", "platform_limited"}:
            state = {
                "login_required": _SESSION_LOGIN_REQUIRED,
                "verification_required": _SESSION_VERIFICATION_REQUIRED,
                "platform_limited": _SESSION_PLATFORM_LIMITED,
            }.get(access_state, _SESSION_UNKNOWN)
            message = (
                "平台要求完成安全验证，请在采集浏览器里完成后重新搜索"
                if access_state == "verification_required" else
                "平台尚未登录，请在采集浏览器里扫码登录后重新搜索"
                if access_state == "login_required" else
                "平台限制了当前浏览器或网络环境，请稍后重试"
                if access_state == "platform_limited" else
                "平台搜索页暂时不可访问，请稍后重试"
            )
            self._set_session_state(platform, state, message)
            raise PlatformBrowserError(message, code=access_state)
        if not raw_rows:
            self._set_session_state(platform, _SESSION_UNKNOWN, "本轮搜索页没有读到内容卡片")
            raise PlatformBrowserError("搜索页暂时没有读到内容卡片，请在采集浏览器确认页面后重试", code="no_visible_results")

        result: list[dict[str, Any]] = []
        for rank, raw in enumerate(raw_rows, start=1):
            if not isinstance(raw, dict):
                continue
            raw_url = str(raw.get("href") or "")
            url = self._remember_xhs_access_url(raw_url) if platform == "xiaohongshu" else _public_post_url(platform, raw_url)
            card_text = re.sub(r"\s+", " ", str(raw.get("text") or "")).strip()[:2000]
            title = re.sub(r"\s+", " ", str(raw.get("title") or "")).strip()[:300]
            if not url or not title:
                continue
            metrics = _visible_metrics(card_text)
            like_count = _metric_number(str(raw.get("metricText") or ""))
            if like_count is not None:
                metrics = {"likes": like_count}
            result.append({
                "source_key": hashlib.sha256(url.encode("utf-8")).hexdigest()[:32],
                "title": title,
                "url": url,
                "excerpt": card_text,
                "author": re.sub(r"\s+", " ", str(raw.get("author") or "")).strip()[:160],
                "published_at": None,
                "metrics": metrics,
                "raw": {"rank": rank, "collection": "hidden_visible_search_card"},
            })
        if not result:
            self._set_session_state(platform, _SESSION_UNKNOWN, "页面有内容，但没有得到可保存的原帖链接")
            raise PlatformBrowserError("平台页面已打开，但没有得到可保存的内容链接", code="no_valid_results")
        self._set_session_state(platform, _SESSION_READY, "已完成本轮后台页面核验")
        return result

    def extract_douyin_detail(
        self,
        value: str,
        *,
        search_keyword: str = "",
        title_hint: str = "",
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
        transcribe_media: bool = False,
    ) -> dict[str, Any]:
        """Read one visible Douyin detail and transcribe accessible media locally.

        The page may expose a platform-published caption and/or a rendered
        subtitle track. They remain separate from local ASR. If the logged-in
        browser exposes a direct, allowlisted public media resource, it is
        streamed to a temporary local file for ASR and deleted immediately.
        Login, verification, private/unsupported media, and platform walls are
        never bypassed.
        """
        url = _public_post_url("douyin", value)
        target_video_id = _douyin_video_id(url)
        if not url or not target_video_id:
            raise PlatformBrowserError("这不是可提取的抖音视频原帖链接", code="invalid_video_url")
        if not self.status()["running"]:
            self._set_session_state("douyin", _SESSION_UNKNOWN, "采集浏览器未打开")
            raise PlatformBrowserError("平台采集浏览器尚未打开，请先点击“打开双平台浏览器”并登录", code="browser_not_running")

        expression = r'''(() => {
          const clean = value => String(value ?? '').replace(/\u200b/g, '').trim();
          const number = value => {
            const raw = String(value ?? '').replace(/,/g, '');
            const match = raw.match(/(\d+(?:\.\d+)?)\s*([万wWkK]?)/);
            if (!match) return null;
            let n = Number(match[1]); const unit = match[2].toLowerCase();
            if (unit === '万' || unit === 'w') n *= 10000;
            if (unit === 'k') n *= 1000;
            return Number.isFinite(n) ? Math.round(n) : null;
          };
          const roots = [];
          const rootNames = ['__INITIAL_STATE__','_SSR_HYDRATED_DATA','__NEXT_DATA__','__UNIVERSAL_DATA_FOR_REHYDRATION__','__RENDER_DATA__','__ROUTER_DATA__','__INLINE_PLAYER_DATA__','__MF_DATA_FETCH_MAP__'];
          for (const key of rootNames) { try { if (window[key]) roots.push(window[key]); } catch (_) {} }
          const parseEmbeddedJson = raw => {
            const candidates = [raw];
            try { const decoded = decodeURIComponent(raw); if (decoded !== raw) candidates.push(decoded); } catch (_) {}
            for (const candidate of candidates) {
              try {
                const parsed = JSON.parse(candidate);
                if (typeof parsed === 'string') { try { const nested = JSON.parse(parsed); if (nested) return nested; } catch (_) {} }
                if (parsed) return parsed;
              } catch (_) {}
            }
            return null;
          };
          for (const node of document.querySelectorAll('script[type="application/json"],script[id*="RENDER"],script[id*="SIGI"],script[id*="STATE"],script[id*="INITIAL"]')) {
            try {
              const raw = node.textContent || '';
              if (raw.length > 8 * 1024 * 1024) continue;
              const data = parseEmbeddedJson(raw); if (data) roots.push(data);
            } catch (_) {}
          }
          const candidates = [];
          const seen = new WeakSet();
          const walk = value => {
            if (!value || typeof value !== 'object' || seen.has(value)) return;
            seen.add(value);
            const id = clean(value.aweme_id || value.awemeId || value.item_id || value.itemId);
            if (id) candidates.push(value);
            for (const child of Object.values(value)) walk(child);
          };
          roots.forEach(walk);
          const urlId = (location.pathname.match(/\/(?:video|note)\/(\d+)/) || [])[1] || '';
          const exactCandidates = candidates.filter(item => clean(item.aweme_id || item.awemeId || item.item_id || item.itemId) === urlId);
          const captionFieldFor = item => {
            for (const field of ['desc','description','video_desc','videoDesc','caption']) {
              if (typeof item[field] === 'string' && clean(item[field])) return field;
            }
            return typeof item.share_info?.share_desc === 'string' && clean(item.share_info.share_desc)
              ? 'share_info.share_desc' : '';
          };
          const captionFor = item => {
            const field = captionFieldFor(item);
            // Preserve publication text exactly; clean() is only a presence
            // check. Leading spaces and zero-width characters belong to it.
            return field === 'share_info.share_desc' ? item.share_info.share_desc : field ? item[field] : '';
          };
          const captionFieldRank = item => {
            const field = captionFieldFor(item);
            return field === 'desc' ? 5
              : ['description','video_desc','videoDesc'].includes(field) ? 4
                : field === 'caption' ? 3 : field === 'share_info.share_desc' ? 2 : 0;
          };
          const metadataScore = item => {
            let score = 0;
            if (item.video || item.video_info || item.videoInfo) score += 20;
            if (item.author || item.authorInfo) score += 10;
            if (item.statistics || item.stats) score += 10;
            if (item.create_time || item.createTime) score += 5;
            if (item.video_text || item.videoText || item.subtitle_infos || item.subtitleInfos || item.subtitles || item.captions) score += 5;
            return score;
          };
          // Some gallery pages hydrate their detail into mounted React props
          // rather than a global JSON root or a readable detail response.
          // Read only the current detail's strongly shaped record; comment
          // props, telemetry IDs and recommendation IDs are not body proof.
          const reactDetails = new WeakSet();
          let reactVisited = 0, reactDetailCount = 0, reactPropertiesVisited = 0;
          if (urlId && !exactCandidates.some(item => captionFieldRank(item) >= 3)) {
            const reactSeen = new WeakSet();
            const visitProps = (value, depth = 0) => {
              if (!value || typeof value !== 'object' || depth > 14
                  || reactVisited >= 60000 || reactPropertiesVisited >= 100000 || reactSeen.has(value)
                  || typeof value.nodeType === 'number') return;
              reactSeen.add(value); reactVisited += 1;
              const id = clean(value.aweme_id || value.awemeId || value.item_id || value.itemId);
              const type = value.awemeType ?? value.aweme_type;
              const author = value.authorInfo || value.author;
              const media = value.video || value.videoInfo || value.video_info
                || value.images || value.imageInfos || value.image_post_info;
              const isComment = ['commentId','comment_id','cid','replyId','reply_id']
                .some(key => value[key] !== undefined && value[key] !== null);
              if (id === urlId && Number.isFinite(type) && author && typeof author === 'object'
                  && media && typeof media === 'object' && !isComment
                  && typeof value.desc === 'string' && clean(value.desc)) {
                exactCandidates.push(value); reactDetails.add(value); reactDetailCount += 1;
              }
              try {
                for (const key in value) {
                  if (reactVisited >= 60000 || reactPropertiesVisited >= 100000) break;
                  if (!Object.prototype.hasOwnProperty.call(value, key)) continue;
                  reactPropertiesVisited += 1;
                  if (['return','stateNode','_owner','alternate','sibling'].includes(key)
                      || /token|cookie|authorization/i.test(key)) continue;
                  visitProps(value[key], depth + 1);
                }
              } catch (_) {}
            };
            let nodeCount = 0;
            for (const node of document.querySelectorAll('*')) {
              if (nodeCount++ >= 3000 || reactVisited >= 60000 || reactPropertiesVisited >= 100000) break;
              try {
                for (const key of Object.getOwnPropertyNames(node)) {
                  if (key.startsWith('__reactProps$')) visitProps(node[key]);
                  else if (key.startsWith('__reactFiber$')) visitProps(node[key]?.memoizedProps);
                }
              } catch (_) {}
            }
          }
          const candidatesWithCaption = exactCandidates.filter(item => captionFor(item));
          const selectionPool = candidatesWithCaption.length ? candidatesWithCaption : exactCandidates;
          selectionPool.sort((left, right) =>
            captionFieldRank(right) - captionFieldRank(left)
            || captionFor(right).length - captionFor(left).length
            || metadataScore(right) - metadataScore(left)
          );
          const chosen = selectionPool[0] || {};
          const chosenCaptionFieldRank = captionFieldRank(chosen);
          const chosenCaptionLength = captionFor(chosen).length;
          const chosenMetadataScore = metadataScore(chosen);
          const structuredId = clean(chosen.aweme_id || chosen.awemeId || chosen.item_id || chosen.itemId);
          const canonicalUrl = clean(document.querySelector('meta[property="og:url"]')?.content || document.querySelector('link[rel="canonical"]')?.href || '');
          const canonicalId = canonicalUrl ? ((new URL(canonicalUrl, location.href).pathname.match(/\/(?:video|note)\/(\d+)/) || [])[1] || '') : '';
          // A URL requested by our own code is not proof that the platform
          // rendered that video's detail. Prefer an ID in the hydrated item;
          // accept platform document metadata only when it names the same ID.
          const chosenId = structuredId || (canonicalId === urlId ? canonicalId : '');
          const text = clean(document.body?.innerText || '');
          const firstText = selectors => {
            for (const selector of selectors) {
              const node = document.querySelector(selector);
              const value = clean(node?.innerText || node?.textContent || '');
              if (value) return value;
            }
            return '';
          };
          const author = chosen.author || chosen.authorInfo || {};
          const stats = chosen.statistics || chosen.stats || {};
          const structuredCaption = captionFor(chosen);
          const visibleCaption = firstText(['[data-e2e="video-desc"]','[data-e2e="video-desc-container"]','[class*="video-desc"]','[class*="videoDesc"]']);
          const metadataCaption = clean(document.querySelector('meta[property="og:description"]')?.content || document.querySelector('meta[name="description"]')?.content || '');
          const caption = structuredCaption || visibleCaption || metadataCaption;
          const subtitleRows = [];
          let subtitleSource = '';
          const subtitlePayload = chosen.video_text || chosen.videoText || chosen.subtitle_infos || chosen.subtitleInfos || chosen.subtitle || chosen.subtitles || chosen.captions || chosen.caption_list || [];
          const appendSubtitle = item => {
            if (typeof item === 'string') subtitleRows.push(clean(item));
            else if (item && typeof item === 'object') {
              const value = clean(item.text || item.content || item.caption || item.text_content || item.subtitle_text);
              if (value) subtitleRows.push(value);
            }
          };
          Array.isArray(subtitlePayload) ? subtitlePayload.forEach(appendSubtitle) : appendSubtitle(subtitlePayload);
          if (subtitleRows.some(Boolean)) subtitleSource = '页面结构化字幕';
          if (!subtitleRows.filter(Boolean).length) {
            for (const video of document.querySelectorAll('video')) {
              try {
                for (const track of Array.from(video.textTracks || [])) {
                  for (const cue of Array.from(track.cues || [])) {
                    const value = clean(cue.text || '');
                    if (value) subtitleRows.push(value);
                  }
                }
              } catch (_) {}
            }
            if (subtitleRows.some(Boolean)) subtitleSource = 'video.textTracks 字幕轨';
          }
          if (!subtitleRows.filter(Boolean).length) {
            for (const trackNode of document.querySelectorAll('track[kind="captions"],track[kind="subtitles"]')) {
              try {
                for (const cue of Array.from(trackNode.track?.cues || [])) {
                  const value = clean(cue.text || '');
                  if (value) subtitleRows.push(value);
                }
              } catch (_) {}
            }
            if (subtitleRows.some(Boolean)) subtitleSource = 'HTML 字幕轨';
          }
          if (!subtitleRows.filter(Boolean).length) {
            document.querySelectorAll('[data-e2e*="subtitle"],[class*="subtitle"],[class*="caption-text"]').forEach(node => subtitleRows.push(clean(node.innerText || node.textContent)));
            if (subtitleRows.some(Boolean)) subtitleSource = '页面可见字幕';
          }
          const mediaUrlCandidates = [];
          const addMediaUrl = value => {
            if (typeof value !== 'string' || !/^https:\/\//i.test(value)) return;
            try {
              const parsed = new URL(value, location.href);
              const host = parsed.hostname.toLowerCase();
              const allowed = ['douyinvod.com','snssdk.com','pstatp.com','bytecdn.cn','bytevideo.com','toutiaocloud.com','byteoversea.com']
                .some(domain => host === domain || host.endsWith('.' + domain));
              if (!allowed || /\.(?:m3u8|mpd|m4s)$/i.test(parsed.pathname)) return;
              parsed.hash = '';
              if (!mediaUrlCandidates.includes(parsed.href)) mediaUrlCandidates.push(parsed.href);
            } catch (_) {}
          };
          const collectPlayAddress = (value, depth = 0) => {
            if (!value || depth > 4 || mediaUrlCandidates.length >= 8) return;
            if (typeof value === 'string') { addMediaUrl(value); return; }
            if (Array.isArray(value)) { value.slice(0, 8).forEach(item => collectPlayAddress(item, depth + 1)); return; }
            if (typeof value !== 'object') return;
            for (const key of ['url_list','urlList','play_addr','playAddr','download_addr','downloadAddr','uri','url']) {
              if (value[key] !== undefined) collectPlayAddress(value[key], depth + 1);
            }
          };
          const videoInfo = chosen.video || chosen.video_info || chosen.videoInfo || {};
          collectPlayAddress(videoInfo);
          for (const rate of (videoInfo.bit_rate || videoInfo.bitRate || []).slice?.(0, 4) || []) {
            collectPlayAddress(rate?.play_addr || rate?.playAddr || rate?.download_addr || rate?.downloadAddr);
          }
          for (const video of document.querySelectorAll('video')) {
            addMediaUrl(video.currentSrc || ''); addMediaUrl(video.src || '');
            for (const source of video.querySelectorAll('source')) addMediaUrl(source.src || source.getAttribute('src') || '');
          }
          const metric = (keys, labels) => {
            for (const key of keys) if (chosen.statistics && chosen.statistics[key] !== undefined) return number(chosen.statistics[key]);
            for (const label of labels) {
              const match = text.match(new RegExp(label + '\\s*([0-9.]+(?:万|w|k)?)', 'i'));
              if (match) return number(match[1]);
            }
            return null;
          };
          const metrics = {};
          const put = (key, value) => { if (value !== null) metrics[key] = value; };
          put('likes', metric(['digg_count','like_count'], ['点赞','赞']));
          put('comments', metric(['comment_count'], ['评论']));
          put('shares', metric(['share_count','forward_count'], ['分享','转发']));
          put('favorites', metric(['collect_count','favorite_count'], ['收藏']));
          put('plays', metric(['play_count','playCount'], ['播放']));
          const pageHasDetailContent = Boolean(
            exactCandidates.length || caption || author.nickname || author.name
            || document.querySelectorAll('video').length || subtitleRows.some(Boolean)
            || Object.keys(metrics).length
          );
          const created = chosen.create_time || chosen.createTime || '';
          const publishedAt = typeof created === 'number' || /^\d{10}$/.test(String(created))
            ? new Date(Number(created) * 1000).toISOString() : clean(created);
          return {
            finalUrl: location.href, pageTitle: document.title || '', visibleText: text.slice(0, 12000),
            observedVideoId: chosenId, pageHasDetailContent,
            title: clean(chosen.share_info?.share_title || chosen.title || firstText(['h1','[data-e2e="video-title"]']) || document.title.replace(/\s*-\s*抖音\s*$/, '')),
            author: clean(author.nickname || author.name || firstText(['[data-e2e="video-author-name"]','[class*="author"] [class*="name"]'])),
            publishedAt, publishedBody: caption, platformSubtitle: [...new Set(subtitleRows.filter(Boolean))].join('\n'),
            metrics, videoElements: document.querySelectorAll('video').length,
            captionSource: structuredCaption ? (captionFieldFor(chosen)==='share_info.share_desc' ? '详情页分享文案片段' : '详情页结构化发布文案') : visibleCaption ? '详情页可见发布文案' : metadataCaption ? '详情页元数据文案片段' : '',
            structuredCandidateCount: exactCandidates.length,
            availableStateKeys: Object.getOwnPropertyNames(window).filter(key => /^_{1,2}[A-Z_]*(?:DATA|STATE|HYDRAT)[A-Z_]*$/.test(key)).slice(0, 30),
            selectedCandidateCaptionField: captionFieldFor(chosen),
            selectedCandidateCaptionFieldRank: chosenCaptionFieldRank,
            selectedCandidateCaptionLength: chosenCaptionLength,
            selectedCandidateMetadataScore: chosenMetadataScore,
            selectedCandidateStateSource: reactDetails.has(chosen) ? 'react_detail_props' : 'page_state',
            reactDetailCandidateCount: reactDetailCount,
            reactVisitedObjectCount: reactVisited,
            reactVisitedPropertyCount: reactPropertiesVisited,
            subtitleSource,
            subtitleLineCount: subtitleRows.filter(Boolean).length,
            mediaUrlCandidates: mediaUrlCandidates.slice(0, 8)
          };
        })()'''
        asr_result: dict[str, Any] = {
            "asr_status": "NOT_ATTEMPTED", "asr_source": "", "asr_message": "",
            "video_status": "MEDIA_UNAVAILABLE",
        }
        network_diagnostics: dict[str, Any] = {
            "network_detail_response_count": 0,
            "network_detail_payload_match_count": 0,
        }
        with self._lock:
            try:
                with _HiddenCDPTarget(self._browser_websocket_url()) as page:
                    try:
                        # The page itself will make the ordinary same-origin
                        # detail request. Capture only a small, bounded
                        # response window; never issue a separate API call.
                        page.enable_network(
                            max_total_buffer_size=24 * 1024 * 1024,
                            max_resource_buffer_size=4 * 1024 * 1024,
                        )
                    except Exception:
                        network_diagnostics["network_capture_available"] = False
                    else:
                        network_diagnostics["network_capture_available"] = True
                    page.navigate(url)
                    deadline = time.monotonic() + _DOUYIN_DETAIL_READ_TIMEOUT_SECONDS
                    detail_evidence_since: float | None = None
                    data: dict[str, Any] = {}
                    while time.monotonic() < deadline:
                        value_data = page.evaluate(expression)
                        if isinstance(value_data, dict):
                            data = value_data
                            observed = str(data.get("observedVideoId") or "")
                            if observed and observed != target_video_id:
                                # A definitive identity mismatch cannot be
                                # repaired by waiting for more hydration.
                                break
                            if (
                                observed == target_video_id
                                and str(data.get("publishedBody") or "").strip()
                                and data.get("captionSource") == "详情页结构化发布文案"
                            ):
                                # Wait for the actual structured caption (or
                                # the bounded timeout) instead of persisting
                                # an incomplete detail response.
                                break
                            access_state = _page_access_state(urllib.parse.unquote(
                                f"{data.get('finalUrl')} {data.get('pageTitle')} {data.get('visibleText')}"
                            ))
                            if access_state in {
                                "verification_required", "login_required", "platform_limited", "post_unavailable",
                            }:
                                break
                            detail_evidence = bool(
                                data.get("publishedBody")
                                or data.get("platformSubtitle")
                                or data.get("author")
                                or data.get("metrics")
                                or data.get("videoElements")
                                or data.get("mediaUrlCandidates")
                            )
                            if observed == target_video_id and detail_evidence:
                                if detail_evidence_since is None:
                                    detail_evidence_since = time.monotonic()
                                elif time.monotonic() - detail_evidence_since >= _DOUYIN_DETAIL_HYDRATION_GRACE_SECONDS:
                                    break
                            else:
                                detail_evidence_since = None
                        time.sleep(0.35)

                    access_text = urllib.parse.unquote(
                        f"{data.get('finalUrl')} {data.get('pageTitle')} {data.get('visibleText')}"
                    )
                    # Do not inspect response bodies on known login, CAPTCHA,
                    # or platform-limit pages. For an ordinary detail page,
                    # accept only the top-level detail record whose aweme ID
                    # exactly matches the requested URL.
                    if _page_access_state(access_text) not in {
                        "verification_required", "login_required", "platform_limited", "post_unavailable",
                    } and network_diagnostics.get("network_capture_available"):
                        response_payload, response_diagnostics = _read_douyin_detail_response(page, target_video_id)
                        network_diagnostics.update(response_diagnostics)
                        final_route = _public_post_url("douyin", str(data.get("finalUrl") or ""))
                        lost_redirect_body = any(
                            item.get("reason") == "body_unavailable"
                            for item in response_diagnostics.get("network_detail_outcomes", [])
                        )
                        visibility_restricted = any(
                            item.get("reason") == "platform_visibility_restricted"
                            and item.get("filter_detail_video_id") == target_video_id
                            for item in response_diagnostics.get("network_detail_outcomes", [])
                        )
                        if (
                            not response_payload and lost_redirect_body and not visibility_restricted
                            and str(data.get("observedVideoId") or "") == target_video_id
                            and _douyin_video_id(final_route) == target_video_id
                            and urllib.parse.urlsplit(final_route).path == f"/note/{target_video_id}"
                        ):
                            # A renderer swap can still race response storage.
                            # Reopen the platform-observed same-ID final route
                            # once. No endpoint replay, route guessing or retry
                            # on a login/challenge/unavailable page is involved.
                            network_diagnostics["redirect_body_retry_count"] = 1
                            page.navigate(final_route)
                            retry_deadline = time.monotonic() + _DOUYIN_REDIRECT_RETRY_WAIT_SECONDS
                            retry_state = ""
                            while time.monotonic() < retry_deadline:
                                value_data = page.evaluate(expression)
                                if isinstance(value_data, dict):
                                    data = value_data
                                    retry_state = _page_access_state(
                                        f"{data.get('finalUrl')} {data.get('pageTitle')} {data.get('visibleText')}"
                                    )
                                    if retry_state or (
                                        str(data.get("observedVideoId") or "") not in {"", target_video_id}
                                    ):
                                        break
                                time.sleep(0.25)
                            if not retry_state and str(data.get("observedVideoId") or "") == target_video_id:
                                response_payload, retried = _read_douyin_detail_response(page, target_video_id)
                                network_diagnostics["redirect_body_retry_initial_outcomes"] = response_diagnostics.get("network_detail_outcomes") or []
                                network_diagnostics.update(retried)
                        if not response_payload and any(
                            item.get("reason") == "platform_visibility_restricted"
                            and item.get("filter_detail_video_id") == target_video_id
                            for item in network_diagnostics.get("network_detail_outcomes", [])
                        ):
                            # Stop before playback, ASR, or any further page
                            # retry. Metadata is not a substitute for the body
                            # which this exact-ID platform response withheld.
                            raise PlatformBrowserError(
                                "平台限制这条内容的可见性，未返回正文",
                                code="post_unavailable",
                                diagnostics={
                                    "target_video_id": target_video_id,
                                    "final_url": _diagnostic_url(str(data.get("finalUrl") or "")),
                                    "copy_completeness": "no_caption",
                                    **network_diagnostics,
                                },
                            )
                        if response_payload:
                            data.update({
                                "observedVideoId": response_payload["video_id"],
                                "pageHasDetailContent": True,
                                "publishedBody": response_payload["published_body"],
                                "captionSource": "详情页结构化发布文案",
                                "title": response_payload.get("title") or data.get("title") or "",
                                "author": response_payload.get("author") or data.get("author") or "",
                                "publishedAt": response_payload.get("published_at") or data.get("publishedAt"),
                                "metrics": response_payload.get("metrics") or data.get("metrics") or {},
                                "networkCaptionField": response_payload.get("caption_field") or "",
                                "networkCaptionLength": response_payload.get("published_body_length") or len(response_payload["published_body"]),
                            })
                    has_verified_structured_caption = bool(
                        str(data.get("captionSource") or "") == "详情页结构化发布文案"
                        and str(data.get("publishedBody") or "").strip()
                    )
                    if (
                        (transcribe_media or not has_verified_structured_caption)
                        and _page_access_state(access_text) not in {
                            "verification_required", "login_required", "platform_limited", "post_unavailable",
                        }
                        and isinstance(data.get("mediaUrlCandidates"), list)
                        and not data.get("mediaUrlCandidates")
                        and int(data.get("videoElements") or 0) > 0
                        and str(data.get("observedVideoId") or "") == target_video_id
                    ):
                        # A muted play attempt is ordinary browser playback,
                        # not an API/signature workaround. It lets currentSrc
                        # resolve on pages that hydrate the player lazily.
                        try:
                            page.evaluate(r"""(() => {
                              const video = document.querySelector('video');
                              if (!video) return false;
                              video.muted = true;
                              const playing = video.play();
                              if (playing && typeof playing.catch === 'function') playing.catch(() => {});
                              return true;
                            })()""")
                            time.sleep(0.8)
                            media_data = page.evaluate(r"""(() => {
                              const urls = [];
                              for (const video of document.querySelectorAll('video')) {
                                for (const raw of [video.currentSrc, video.src, ...[...video.querySelectorAll('source')].map(node => node.src || node.getAttribute('src'))]) {
                                  if (typeof raw !== 'string' || !/^https:\/\//i.test(raw)) continue;
                                  try {
                                    const value = new URL(raw, location.href);
                                    const host = value.hostname.toLowerCase();
                                    const allowed = ['douyinvod.com','snssdk.com','pstatp.com','bytecdn.cn','bytevideo.com','toutiaocloud.com','byteoversea.com']
                                      .some(domain => host === domain || host.endsWith('.' + domain));
                                    if (allowed && !/\.(?:m3u8|mpd|m4s)$/i.test(value.pathname) && !urls.includes(value.href)) urls.push(value.href);
                                  } catch (_) {}
                                }
                              }
                              return urls.slice(0, 8);
                            })()""")
                            if isinstance(media_data, list):
                                data["mediaUrlCandidates"] = media_data
                        except Exception:
                            pass

                    early_diagnostic = urllib.parse.unquote(
                        f"{data.get('finalUrl')} {data.get('pageTitle')} {data.get('visibleText')}"
                    )
                    early_state = _page_access_state(early_diagnostic)
                    has_detail_evidence = bool(
                        data.get("pageHasDetailContent") or data.get("publishedBody") or data.get("platformSubtitle")
                        or data.get("author") or data.get("metrics") or data.get("videoElements")
                        or data.get("mediaUrlCandidates")
                    )
                    early_observed = str(data.get("observedVideoId") or "")
                    if (
                        early_state not in {"verification_required", "login_required", "platform_limited", "post_unavailable"}
                        and early_observed == target_video_id
                        and has_detail_evidence
                        and (transcribe_media or not (
                            str(data.get("captionSource") or "") == "详情页结构化发布文案"
                            and str(data.get("publishedBody") or "").strip()
                        ))
                    ):
                        if progress_callback:
                            try:
                                progress_callback({
                                    "status": "TRANSCRIBING", "asr_status": "PROCESSING",
                                    "asr_source": "BROWSER_LOGGED_IN_MEDIA",
                                    "asr_message": "详情页 ID 已核验；正在检查可访问媒体并尝试本机口播转写。",
                                })
                            except Exception:
                                pass
                        asr_result = self._transcribe_douyin_media(page, data.get("mediaUrlCandidates"))
            except PlatformBrowserError:
                raise
            except Exception as exc:
                raise PlatformBrowserError("后台读取抖音详情页失败，请稍后重试", code="detail_read_error") from exc

        diagnostic_text = urllib.parse.unquote(
            f"{data.get('finalUrl')} {data.get('pageTitle')} {data.get('visibleText')}"
        )
        state = _page_access_state(diagnostic_text)
        if state in {"verification_required", "login_required", "platform_limited"}:
            session = {
                "verification_required": _SESSION_VERIFICATION_REQUIRED,
                "login_required": _SESSION_LOGIN_REQUIRED,
                "platform_limited": _SESSION_PLATFORM_LIMITED,
            }[state]
            message = {
                "verification_required": "抖音要求完成安全验证，请在采集浏览器中处理后点击重新提取",
                "login_required": "抖音登录已失效，请扫码登录后点击重新提取",
                "platform_limited": "抖音限制了当前网络或浏览器环境，请稍后重试",
            }[state]
            self._set_session_state("douyin", session, message)
            raise PlatformBrowserError(message, code=state)
        if state == "post_unavailable":
            raise PlatformBrowserError("视频不存在、已删除或当前账号不可访问", code="post_unavailable")

        observed = str(data.get("observedVideoId") or "")
        if not observed:
            raise PlatformBrowserError(
                "抖音详情页没有呈现可核验的视频内容；已保留搜索候选文本，但不会将其写成发布文案",
                code="detail_not_loaded",
                diagnostics={
                    "target_video_id": target_video_id,
                    "final_url": _diagnostic_url(str(data.get("finalUrl") or "")),
                    "page_title": str(data.get("pageTitle") or "")[:300],
                    "page_has_detail_content": bool(data.get("pageHasDetailContent")),
                    "structured_candidate_count": int(data.get("structuredCandidateCount") or 0),
                },
            )
        if observed != target_video_id:
            raise PlatformBrowserError(
                "详情页视频 ID 与搜索结果不一致，已拒绝使用其他视频内容",
                code="identity_mismatch",
                diagnostics={"target_video_id": target_video_id, "observed_video_id": observed, "final_url": _diagnostic_url(str(data.get("finalUrl") or ""))},
            )
        raw_body = str(data.get("publishedBody") or "")
        body_truncated = len(raw_body) > 30000
        body = raw_body[:30000]
        body_source = str(data.get("captionSource") or "")
        subtitle = str(data.get("platformSubtitle") or "")[:30000]
        # We intentionally never persist a signed video URL or claim that the
        # browser-visible player is a durable video archive.
        asr_text = str(asr_result.get("asr_text") or "")[:60000]
        text_state = "COMPLETE" if body and not body_truncated and body_source == "详情页结构化发布文案" else "PARTIAL" if (body or subtitle or asr_text) else "FAILED"
        result = {
            "platform": "douyin", "url": url, "video_id": target_video_id, "observed_video_id": observed,
            "title": str(data.get("title") or title_hint)[:500], "author": str(data.get("author") or "")[:300],
            "published_at": data.get("publishedAt") or None, "metrics": data.get("metrics") or {},
            "published_body": body, "published_body_source": body_source,
            "platform_subtitle": subtitle, "subtitle_source": str(data.get("subtitleSource") or ""),
            "asr_status": str(asr_result.get("asr_status") or "NOT_ATTEMPTED"),
            "asr_text": asr_text, "asr_source": str(asr_result.get("asr_source") or ""),
            "asr_text_truncated": bool(asr_result.get("asr_text_truncated")),
            "asr_segments": asr_result.get("asr_segments") or [],
            "asr_confidence": asr_result.get("asr_confidence") or {},
            "asr_low_confidence": asr_result.get("asr_low_confidence") or [],
            "asr_message": str(asr_result.get("asr_message") or ""),
            "asr_language": str(asr_result.get("asr_language") or ""),
            "asr_engine": str(asr_result.get("asr_engine") or ""),
            "asr_model": str(asr_result.get("asr_model") or ""),
            "asr_media_sha256": str(asr_result.get("asr_media_sha256") or ""),
            "ocr_status": "NOT_ATTEMPTED", "ocr_text": "",
            "video_status": str(asr_result.get("video_status") or "MEDIA_UNAVAILABLE"),
            "status": text_state,
            "failure_stage": (
                "" if text_state == "COMPLETE" else
                "caption_truncated" if body_truncated else
                "caption_not_verified_complete" if body else
                "detail_caption_missing" if subtitle or asr_text else
                "content_extract"
            ),
            "error_message": (
                "已从匹配视频详情读取结构化发布文案；平台字幕和本机口播转写分别展示，视频媒体不会长期归档。"
                if text_state == "COMPLETE" else
                "详情页返回了未验证完整度的发布文案片段；状态保留为部分内容。平台字幕、口播转写和完整视频仍各自独立标记。"
                if body else
                "详情页未返回发布文案；已分别保存平台字幕和本机口播转写结果。"
                if (subtitle or asr_text) else
                "详情页已打开，但未读到发布文案、平台字幕或口播转写；" + str(asr_result.get("asr_message") or "")
            ),
            "diagnostics": {
                "final_url": _diagnostic_url(str(data.get("finalUrl") or "")), "page_title": str(data.get("pageTitle") or "")[:300],
                "video_element_count": int(data.get("videoElements") or 0),
                "media_policy": "temporary_local_asr_no_archive" if asr_text else "video_not_archived",
                "media_candidate_count": min(len(data.get("mediaUrlCandidates") or []), 8),
                "subtitle_line_count": int(data.get("subtitleLineCount") or 0),
                "structured_candidate_count": int(data.get("structuredCandidateCount") or 0),
                "available_state_keys": data.get("availableStateKeys") or [],
                "selected_candidate_caption_field": str(data.get("selectedCandidateCaptionField") or ""),
                "selected_candidate_caption_field_rank": int(data.get("selectedCandidateCaptionFieldRank") or 0),
                "selected_candidate_caption_length": int(data.get("selectedCandidateCaptionLength") or 0),
                "selected_candidate_metadata_score": int(data.get("selectedCandidateMetadataScore") or 0),
                "selected_candidate_state_source": str(data.get("selectedCandidateStateSource") or ""),
                "react_detail_candidate_count": int(data.get("reactDetailCandidateCount") or 0),
                "react_visited_object_count": int(data.get("reactVisitedObjectCount") or 0),
                "network_detail_response_count": int(network_diagnostics.get("network_detail_response_count") or 0),
                "network_detail_payload_match_count": int(network_diagnostics.get("network_detail_payload_match_count") or 0),
                "network_detail_outcomes": network_diagnostics.get("network_detail_outcomes") or [],
                "redirect_body_retry_count": int(network_diagnostics.get("redirect_body_retry_count") or 0),
                "redirect_body_retry_initial_outcomes": network_diagnostics.get("redirect_body_retry_initial_outcomes") or [],
                "network_detail_response_path": str(network_diagnostics.get("network_detail_response_path") or ""),
                "network_capture_available": bool(network_diagnostics.get("network_capture_available")),
                "network_caption_field": str(data.get("networkCaptionField") or ""),
                "network_caption_length": int(data.get("networkCaptionLength") or 0),
                "copy_completeness": "verified_structured_caption" if text_state == "COMPLETE" else "truncated_structured_caption" if body_truncated else "detail_metadata_text_only" if body and body_source == "详情页元数据文案片段" else "detail_visible_text_only" if body else "speech_asr_requires_human_review" if asr_text else "no_caption",
                **(asr_result.get("diagnostics") if isinstance(asr_result.get("diagnostics"), dict) else {}),
            },
        }
        # A single directly-readable detail URL is not proof that keyword
        # search works for this platform.  Only search() may mark the session
        # READY after it has read real result cards for the current run.
        return result

    def extract_xiaohongshu_detail(
        self, value: str, *, search_keyword: str = "", title_hint: str = "",
    ) -> dict[str, Any]:
        url = _safe_url("xiaohongshu", value)
        if not url or not re.search(r"/(?:explore|discovery/item)/[A-Za-z0-9]+", urllib.parse.urlsplit(url).path):
            raise PlatformBrowserError("这不是可提取的小红书原帖链接", code="invalid_post_url")
        if not self.status()["running"]:
            raise PlatformBrowserError("平台采集浏览器尚未打开，请先点击“打开双平台浏览器”并登录", code="browser_not_running")
        post_key = re.search(r"/(?:explore|discovery/item)/([A-Za-z0-9]+)", urllib.parse.urlsplit(url).path).group(1)
        capture_dir = RADAR_CAPTURE_DIR / post_key
        capture_dir.mkdir(parents=True, exist_ok=True)
        screenshot_path = capture_dir / "page.png"
        resolution_mode = "direct_url"
        original_url = _public_post_url("xiaohongshu", url)
        recent_access_url = self._recent_xhs_access_url(original_url)
        if recent_access_url:
            url = recent_access_url
            resolution_mode = "recent_search_access_url"
        with self._lock:
            page = None
            keep_page_open = False
            try:
                # All automatic detail reads use a disposable background CDP
                # page.  It carries the dedicated profile's session but never
                # selects/reuses the user's visible tabs.
                with _BackgroundPage(self._browser_websocket_url()) as page:
                    context = page.context
                    page.goto(url, wait_until="domcontentloaded", timeout=45000)
                    page.wait_for_timeout(900)
                    initial = page.evaluate(r"""() => ({
                      title: document.title || '',
                      text: (document.body?.innerText || '').slice(0, 4000),
                      url: location.href
                    })""")
                    initial_diagnostic = urllib.parse.unquote(f"{initial.get('url')} {initial.get('title')} {initial.get('text')}")
                    initial_state = _page_access_state(initial_diagnostic)
                    if initial_state in {"verification_required", "login_required", "platform_limited"}:
                        page.close()
                        page = None
                        raise self._xhs_access_error(
                            initial_state,
                            action_url=str(initial.get("url") or url),
                        )
                    unavailable = initial_state == "post_unavailable"
                    if unavailable and (title_hint.strip() or search_keyword.strip()):
                        recovered_url = ""
                        mismatch_url = ""
                        mismatch_post_key = ""
                        attempted_queries: list[str] = []
                        clean_title = _clean_xhs_title_hint(title_hint)
                        for query in (clean_title, title_hint.strip(), search_keyword.strip()):
                            query = query[:180]
                            if not query or query in attempted_queries:
                                continue
                            attempted_queries.append(query)
                            page.goto(_search_url("xiaohongshu", query), wait_until="domcontentloaded", timeout=45000)
                            try:
                                page.wait_for_selector('section.note-item a.cover[href*="/search_result/"]', state="visible", timeout=4000)
                            except Exception:
                                page.wait_for_timeout(700)
                            search_data = page.evaluate(r"""() => ({
                              title: document.title || '',
                              text: (document.body?.innerText || '').slice(0, 5000),
                              rows: [...document.querySelectorAll('section.note-item a.cover[href*="/search_result/"],section.note-item a.title[href*="/search_result/"],a[href*="/explore/"],a[href*="/discovery/item/"]')].filter(link => link.offsetParent !== null).slice(0, 40).map(link => {
                                let node=link, best=link, bestText=(link.innerText||'').trim();
                                for(let i=0;i<7&&node.parentElement;i+=1){ node=node.parentElement; const text=(node.innerText||'').trim(); if(text.length>=bestText.length&&text.length<=1500){best=node;bestText=text;} if(node.matches('article,li,section,[class*="note-item"]'))break; }
                                const title=(best.querySelector('.title')?.innerText||link.getAttribute('title')||bestText.split(/\n+/).find(v=>v.trim().length>5)||'').trim();
                                return {href:link.href||'',title,text:bestText};
                              })
                            })""")
                            search_diagnostic = f"{search_data.get('title')} {search_data.get('text')}"
                            search_state = _page_access_state(search_diagnostic)
                            if search_state in {"verification_required", "login_required", "platform_limited"}:
                                page.close()
                                page = None
                                raise self._xhs_access_error(search_state, action_url=_search_url("xiaohongshu", query))
                            recovered_url = _best_xhs_search_result(search_data.get("rows") or [], post_key, clean_title or title_hint)
                            if recovered_url:
                                break
                            mismatch_url, mismatch_post_key = _similar_xhs_search_result(
                                search_data.get("rows") or [], post_key, clean_title or title_hint,
                            )
                        if recovered_url:
                            url = recovered_url
                            self._remember_xhs_access_url(recovered_url)
                            resolution_mode = "search_result_recovered"
                            page.goto(url, wait_until="domcontentloaded", timeout=45000)
                            page.wait_for_timeout(900)
                        else:
                            page.close()
                            page = None
                            if mismatch_url:
                                raise PlatformBrowserError(
                                    "搜索结果标题相似，但帖子 ID 不一致，已拒绝用其他帖子覆盖原记录",
                                    code="identity_mismatch",
                                    diagnostics={
                                        "target_post_id": post_key,
                                        "observed_post_id": mismatch_post_key,
                                        "candidate_url": _diagnostic_url(mismatch_url),
                                    },
                                )
                            raise PlatformBrowserError(
                                "原帖旧链接已失效，工作台按标题重新搜索后仍未找到同一篇帖子",
                                code="post_unavailable",
                                diagnostics={
                                    "reason": "stale_link_not_found_by_title",
                                    "post_key": post_key,
                                    "clean_title_hint": clean_title,
                                    "attempted_queries": attempted_queries,
                                    "initial_page_title": str(initial.get("title") or "")[:300],
                                    "initial_url": _diagnostic_url(str(initial.get("url") or "")),
                                },
                            )
                    for pattern in (re.compile(r"^展开$"), re.compile(r"^全文$"), re.compile(r"展开正文")):
                        for item in page.get_by_text(pattern).all()[:3]:
                            try:
                                if item.is_visible():
                                    item.click(timeout=1200)
                            except Exception:
                                pass
                    page.wait_for_timeout(250)
                    data = page.evaluate(r"""() => {
                      const clean = v => String(v || '').replace(/\u200b/g,'').replace(/[ \t]+/g,' ').replace(/\n{3,}/g,'\n\n').trim();
                      const visible = n => !!(n && (n.getClientRects().length || n.offsetParent !== null));
                      const texts = selectors => selectors.flatMap(s => [...document.querySelectorAll(s)].filter(visible).map(n => clean(n.innerText || n.textContent)).filter(Boolean));
                      const first = selectors => texts(selectors).sort((a,b) => b.length-a.length)[0] || '';
                      const meta = s => document.querySelector(s)?.getAttribute('content')?.trim() || '';
                      const json = [];
                      for (const n of document.querySelectorAll('script[type="application/ld+json"]')) { try { const v=JSON.parse(n.textContent||'null'); Array.isArray(v)?json.push(...v):v&&json.push(v); } catch {} }
                      const flat=[]; const walk=v=>{ if(!v||typeof v!=='object')return; flat.push(v); Object.values(v).forEach(walk); }; json.forEach(walk);
                      const article=flat.find(v=>v.articleBody||v.headline||/Article|Posting/i.test(String(v['@type']||'')))||{};
                      const author=typeof article.author==='string'?article.author:(article.author?.name||'');
                      const bodySelectors=['[data-testid="note-content"]','[data-testid="post-content"]','#detail-desc','.note-content .desc','[class*="note-detail"] [class*="desc"]','article [class*="content"]'];
                      const subtitle=texts(['[class*="subtitle"]','[class*="caption"]','[data-testid*="subtitle"]']).join('\n');
                      return {
                        title: clean(article.headline || meta('meta[property="og:title"]') || first(['h1','[data-testid="note-title"]','#detail-title','[class*="note-detail"] [class*="title"]'])),
                        author: clean(author || meta('meta[name="author"]') || first(['[data-testid="author-name"]','a[href*="/user/profile"] [class*="name"]','[class*="author"] [class*="name"]','[class*="nickname"]'])),
                        body: clean(article.articleBody || meta('meta[property="og:description"]') || meta('meta[name="description"]') || first(bodySelectors)),
                        publishedAt: clean(article.datePublished || meta('meta[property="article:published_time"]') || first(['time','[data-testid="publish-time"]','[class*="publish-time"]','[class*="date"]'])),
                        tags: [...new Set([...String(article.keywords||'').split(/[,，]/), ...texts(['a[href*="/search_result"]','a[href*="/search?keyword"]','[class*="tag"]']).flatMap(v=>v.split(/\s+/))].map(clean).filter(v=>v.startsWith('#')||v.length>1))].slice(0,30),
                        subtitle: clean(subtitle),
                        visibleText: clean(document.body?.innerText||'').slice(0,30000),
                        pageTitle: document.title, finalUrl: location.href,
                        imageCount: document.querySelectorAll('article img,[class*="note-detail"] img,[class*="swiper"] img').length,
                        videoCount: document.querySelectorAll('video').length
                      };
                    }""")
                    visible_text = str(data.get("visibleText") or "")
                    diagnostic_text = urllib.parse.unquote(f"{data.get('finalUrl')} {data.get('pageTitle')} {visible_text}")
                    final_state = _page_access_state(diagnostic_text)
                    if final_state in {"verification_required", "login_required", "platform_limited"}:
                        page.close()
                        page = None
                        raise self._xhs_access_error(final_state, action_url=str(data.get("finalUrl") or url))
                    if final_state == "post_unavailable":
                        page.close()
                        page = None
                        raise PlatformBrowserError(
                            "帖子不存在、已删除或当前账号不可访问",
                            code="post_unavailable",
                            diagnostics={
                                "reason": "detail_page_unavailable_after_recovery",
                                "post_key": post_key,
                                "page_title": str(data.get("pageTitle") or "")[:300],
                                "final_url": _diagnostic_url(str(data.get("finalUrl") or "")),
                                "resolution_mode": resolution_mode,
                            },
                        )
                    screenshot_saved = False
                    try:
                        page.screenshot(path=str(screenshot_path), full_page=False, timeout=5000)
                        screenshot_saved = screenshot_path.is_file()
                    except Exception:
                        # A platform animation can keep Playwright's screenshot
                        # stability check waiting.  The already-read copy is the
                        # primary result and must still be delivered.
                        screenshot_saved = False
                    image_texts: list[str] = []
                    ocr = {"status": "not_needed", "text": "", "message": "网页正文已读取，不需要 OCR。"}
                    if int(data.get("imageCount") or 0) > 0 and len(str(data.get("body") or "")) < 180:
                        images = page.locator('article img,[class*="note-detail"] img,[class*="swiper"] img')
                        for index in range(min(images.count(), 4)):
                            image = images.nth(index)
                            try:
                                box = image.bounding_box()
                                if not box or box["width"] < 220 or box["height"] < 140:
                                    continue
                                image_path = capture_dir / f"image-{index + 1}.png"
                                image.screenshot(path=str(image_path))
                                current = _run_local_ocr(image_path)
                                ocr = current
                                if current["text"]:
                                    image_texts.append(current["text"])
                            except Exception:
                                continue
                    image_text = "\n\n".join(dict.fromkeys(image_texts))
                    subtitle = str(data.get("subtitle") or "").strip()
                    asr_status = "subtitle_available" if subtitle else "unavailable" if int(data.get("videoCount") or 0) else "not_needed"
                    asr_message = "已读取页面可见字幕。" if subtitle else "视频口播暂未提取：页面没有可读字幕，工作台不会绕过平台限制下载视频。" if int(data.get("videoCount") or 0) else "帖子不是视频，或网页正文已经可读。"
                    blocks = [str(data.get("body") or "").strip(), image_text, subtitle]
                    merged = "\n\n".join(dict.fromkeys(block for block in blocks if block))
                    sources = {}
                    if data.get("title"): sources["title"] = "网页结构化数据或 DOM"
                    if data.get("author"): sources["author"] = "网页结构化数据或 DOM"
                    if data.get("body"): sources["body"] = "网页结构化数据或 DOM"
                    if image_text: sources["image_text"] = "macOS Vision OCR"
                    if subtitle: sources["video_subtitle"] = "页面可见字幕"
                    checks = [data.get("title"), data.get("author"), merged, data.get("publishedAt"), data.get("tags"), _visible_metrics(visible_text), screenshot_saved]
                    completeness = round(sum(bool(item) for item in checks) / len(checks) * 100)
                    result = {
                        "platform": "xiaohongshu", "url": original_url, "post_key": post_key,
                        "title": str(data.get("title") or "")[:500], "author": str(data.get("author") or "")[:300],
                        "published_at": str(data.get("publishedAt") or "")[:100] or None,
                        "body": str(data.get("body") or "")[:30000], "image_text": image_text[:30000],
                        "video_subtitle": subtitle[:30000], "video_speech": "", "merged_copy": merged[:60000],
                        "tags": data.get("tags") or [], "metrics": _visible_metrics(visible_text),
                        "screenshot_name": f"{post_key}/page.png" if screenshot_saved else "", "status": "success" if data.get("title") and merged else "partial" if merged else "failed",
                        "completeness": completeness, "field_sources": sources,
                        "ocr_status": ocr["status"], "ocr_message": ocr["message"],
                        "asr_status": asr_status, "asr_message": asr_message,
                        "failure_stage": "" if merged else "content_extract",
                        "error_message": "" if merged else "详情页已打开，但没有提取到正文、图片文字或字幕。",
                        "diagnostics": {"page_title": data.get("pageTitle"), "final_url": _diagnostic_url(str(data.get("finalUrl") or "")), "resolved_url": _diagnostic_url(url), "resolution_mode": resolution_mode, "visible_text_length": len(visible_text), "image_count": data.get("imageCount"), "video_count": data.get("videoCount"), "screenshot_saved": screenshot_saved},
                    }
                    page.close()
                    page = None
                    return result
            except PlatformBrowserError:
                raise
            except Exception as exc:
                raise PlatformBrowserError(f"提取详情失败：{str(exc)[:180]}", code="detail_extract_failed") from exc
            finally:
                if page is not None and not keep_page_open:
                    try: page.close()
                    except Exception: pass

    def archive_xiaohongshu_detail(
        self, value: str, *, search_keyword: str = "", title_hint: str = "",
        deadline: float | None = None, cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        """Read one authorized detail page and return exact text plus image bytes.

        Temporary signed URLs and response headers remain in memory.  The archive
        repository receives only response bytes and a query-free source reference.
        """
        url = _safe_url("xiaohongshu", value)
        parsed = urllib.parse.urlsplit(url)
        match = re.search(r"/(?:explore|discovery/item)/([A-Za-z0-9]+)", parsed.path)
        if not url or not match:
            raise PlatformBrowserError("这不是可归档的小红书原帖链接", code="invalid_post_url")
        if not self.status()["running"]:
            raise PlatformBrowserError("平台采集浏览器尚未打开，请先打开并登录", code="browser_not_running")
        target_post_id = match.group(1)
        canonical_url = _public_post_url("xiaohongshu", url)
        access_url = self._recent_xhs_access_url(canonical_url) or url
        with _deadline_lock(self._lock, deadline, cancel_event):
            page = None
            keep_page_open = False
            try:
                # The archive reader shares the same managed background target
                # policy as search.  It closes even on identity/media failure,
                # so automatic batches do not leave platform tabs behind.
                background_page = _BackgroundPage(self._browser_websocket_url())
                if hasattr(background_page, "set_budget"):
                    background_page.set_budget(deadline, cancel_event)
                with background_page as page:
                    context = page.context
                    observed_responses: list[Any] = []

                    def remember_response(response: Any) -> None:
                        try:
                            content_type = str(response.headers.get("content-type") or "").lower()
                            if content_type.startswith("image/"):
                                observed_responses.append(response)
                        except Exception:
                            return

                    page.on("response", remember_response)
                    page.goto(access_url, wait_until="domcontentloaded", timeout=45000)
                    page.wait_for_timeout(900)
                    initial = page.evaluate(r"""() => ({
                      title: document.title || '',
                      text: (document.body?.innerText || '').slice(0, 5000),
                      url: location.href
                    })""")
                    initial_text = urllib.parse.unquote(
                        f"{initial.get('url')} {initial.get('title')} {initial.get('text')}"
                    )
                    initial_state = _page_access_state(initial_text)
                    if initial_state in {"verification_required", "login_required", "platform_limited"}:
                        page.close()
                        page = None
                        raise self._xhs_access_error(
                            initial_state,
                            action_url=str(initial.get("url") or access_url),
                            archive=True,
                        )
                    unavailable = initial_state == "post_unavailable" and not _xhs_unavailable_is_body_quote(
                        page, target_post_id, initial,
                    )
                    if unavailable:
                        queries = []
                        clean_title = _clean_xhs_title_hint(title_hint)
                        mismatch_url = ""
                        mismatch_post_id = ""
                        recovered_url = ""
                        recovery_diagnostics: dict[str, Any] = {
                            "recovery_result_count": 0,
                            "recovery_unique_id_count": 0,
                            "recovery_stop_reason": "no_queries",
                            "exact_id_found": False,
                        }
                        for query in (clean_title, title_hint.strip(), search_keyword.strip()):
                            query = query[:180]
                            if not query or query in queries:
                                continue
                            queries.append(query)
                            page.goto(_search_url("xiaohongshu", query), wait_until="domcontentloaded", timeout=45000)
                            try:
                                page.wait_for_selector(
                                    'section.note-item a.cover[href*="/search_result/"]',
                                    state="visible",
                                    timeout=4000,
                                )
                            except Exception:
                                page.wait_for_timeout(700)
                            search_data = _wait_for_xhs_recovery_search(page, target_post_id)
                            recovery_diagnostics = search_data["recovery_diagnostics"]
                            search_text = f"{search_data.get('url')} {search_data.get('pageTitle')} {search_data.get('bodyText')} {search_data.get('accessHints')}"
                            search_state = _page_access_state(search_text)
                            if search_state in {"verification_required", "login_required", "platform_limited"}:
                                page.close()
                                page = None
                                raise self._xhs_access_error(
                                    search_state,
                                    action_url=_search_url("xiaohongshu", query),
                                    archive=True,
                                )
                            # Reuse only links already exposed by this ordinary
                            # authorized result page, each under its own ID.
                            # The cache is memory-only and expires after 30 min.
                            for row in search_data.get("rows") or []:
                                existing_access_url, _ = _xhs_search_result_candidate(row)
                                if existing_access_url:
                                    self._remember_xhs_access_url(existing_access_url)
                            recovered_url = _best_xhs_search_result(
                                search_data.get("rows") or [], target_post_id, clean_title or title_hint,
                            )
                            if recovered_url:
                                break
                            mismatch_url, mismatch_post_id = _similar_xhs_search_result(
                                search_data.get("rows") or [], target_post_id, clean_title or title_hint,
                            )
                        if not recovered_url:
                            if mismatch_url:
                                raise PlatformBrowserError(
                                    "只找到了标题相似但 ID 不同的帖子，已拒绝归档",
                                    code="identity_mismatch",
                                    diagnostics={
                                        **recovery_diagnostics,
                                        "recovery_query_count": len(queries),
                                        "recovery_reason": "similar_title_different_id",
                                        "target_post_id": target_post_id,
                                        "observed_post_id": mismatch_post_id,
                                        "candidate_url": _diagnostic_url(mismatch_url),
                                    },
                                )
                            raise PlatformBrowserError(
                                "本轮有限搜索未找回同一帖子 ID 的可访问链接，尚不能判断原帖是否已删除；可稍后重试",
                                code="access_url_recovery_failed",
                                diagnostics={
                                    **recovery_diagnostics,
                                    "target_post_id": target_post_id,
                                    "recovery_query_count": len(queries),
                                    "recovery_reason": "limited_search_no_exact_id",
                                },
                            )
                        access_url = recovered_url
                        self._remember_xhs_access_url(recovered_url)
                        page.goto(recovered_url, wait_until="domcontentloaded", timeout=45000)
                        page.wait_for_timeout(900)

                    for pattern in (re.compile(r"^展开$"), re.compile(r"^全文$"), re.compile(r"展开正文")):
                        for item in page.get_by_text(pattern).all()[:3]:
                            try:
                                if item.is_visible():
                                    item.click(timeout=1200)
                            except Exception:
                                pass
                    page.wait_for_timeout(300)
                    hydration_diagnostics = _wait_for_xhs_detail_hydration(page, target_post_id)
                    hydration_access_state = str(hydration_diagnostics.get("access_state") or "")
                    if hydration_access_state in {"verification_required", "login_required", "platform_limited"}:
                        page.close()
                        page = None
                        raise self._xhs_access_error(
                            hydration_access_state,
                            action_url=access_url,
                            archive=True,
                        )
                    data = page.evaluate(
                        r"""targetId => {
                          const visible = n => !!(n && (n.getClientRects().length || n.offsetParent !== null));
                          const firstVisible = selectors => {
                            for (const selector of selectors) for (const node of document.querySelectorAll(selector)) {
                              if (visible(node)) return node;
                            }
                            return null;
                          };
                          const stateRoots=[];
                          for (const key of ['__INITIAL_STATE__','__INITIAL_DATA__','__APOLLO_STATE__']) {
                            try { if (window[key] && typeof window[key]==='object') stateRoots.push(window[key]); } catch {}
                          }
                          // Some current XHS detail builds put the same public
                          // page state in JSON script tags rather than a window
                          // global.  Reading these already-rendered values is
                          // still browser-visible collection; it avoids falling
                          // back to whitespace-normalized DOM text when the
                          // exact note state is available.
                          for (const node of document.querySelectorAll('script#__NEXT_DATA__,script[type="application/json"]')) {
                            try {
                              const raw=(node.textContent||'').trim();
                              if(raw.length<2||raw.length>5000000) continue;
                              const parsed=JSON.parse(raw);
                              if(parsed&&typeof parsed==='object') stateRoots.push(parsed);
                            } catch {}
                          }
                          const seen=new WeakSet(); const exactCandidates=[];
                          const explicitPostId=value=>{
                            for(const key of ['noteId','note_id','postId','post_id']) {
                              try { if(typeof value?.[key]==='string'&&value[key]) return value[key]; } catch {}
                            }
                            return '';
                          };
                          const genericId=value=>typeof value?.id==='string'&&value.id?value.id:'';
                          const looksLikePost=value=>{
                            const hasNoteBody=typeof value?.noteDesc==='string';
                            const hasMedia=Array.isArray(value?.imageList)||Array.isArray(value?.image_list)||Array.isArray(value?.images)||!!(value?.video||value?.videoInfo||value?.video_info);
                            const hasNoteTitle=typeof value?.noteTitle==='string';
                            const hasPostShape=typeof value?.title==='string'&&(!!value?.user||!!value?.author||hasMedia);
                            return hasNoteBody||hasMedia||hasNoteTitle||hasPostShape;
                          };
                          const walk=(value,depth=0)=>{
                            if (!value || typeof value!=='object' || depth>12 || seen.has(value)) return;
                            seen.add(value);
                            const namedId=explicitPostId(value);
                            const id=namedId||genericId(value);
                            // A generic `id` is common in comments/users.  It
                            // may only identify a post when the surrounding
                            // object also has an unambiguous note shape.
                            if (id===targetId && (namedId||looksLikePost(value))) exactCandidates.push(value);
                            try { for (const child of Object.values(value)) walk(child,depth+1); } catch {}
                          };
                          stateRoots.forEach(root=>walk(root));
                          const bodyField=value=>['desc','noteDesc','description','content'].find(k=>typeof value?.[k]==='string'&&value[k].trim().length>0)||'';
                          const score=value=>{
                            const field=bodyField(value); let total=0;
                            // An empty card stub must not outrank the same
                            // post's real caption merely because it has images.
                            if(field==='desc'||field==='noteDesc') total+=100;
                            else if(field) total+=10;
                            if(Array.isArray(value?.imageList)||Array.isArray(value?.image_list)||Array.isArray(value?.images)) total+=25;
                            if(typeof value?.title==='string'||typeof value?.noteTitle==='string') total+=10;
                            if(value?.user||value?.author) total+=5;
                            if(String(value?.noteId||value?.note_id||value?.postId||value?.post_id||value?.id||'')===targetId) total+=20;
                            return total;
                          };
                          exactCandidates.sort((a,b)=>score(b)-score(a)||String(b?.[bodyField(b)]||'').length-String(a?.[bodyField(a)]||'').length);
                          const exact=exactCandidates[0]||null;
                          const selectedBodyField=bodyField(exact);
                          const rawFromState=selectedBodyField ? exact[selectedBodyField] : null;
                          const domBody=firstVisible(['[data-testid="note-content"]','[data-testid="post-content"]','#detail-desc','.note-content .desc','[class*="note-detail"] [class*="desc"]']);
                          const bodyRaw=typeof rawFromState==='string' ? rawFromState : (domBody ? (domBody.innerText ?? domBody.textContent ?? '') : null);
                          const bodySource=typeof rawFromState==='string' ? 'embedded_state' : (domBody ? 'expanded_dom' : 'unavailable');
                          const bestImageUrl=img=>{
                            const srcset=(img.getAttribute('srcset')||'').split(',').map(v=>v.trim().split(/\s+/)[0]).filter(Boolean);
                            return [img.currentSrc,...srcset,img.src,img.getAttribute('data-src'),img.getAttribute('data-original')].find(v=>typeof v==='string'&&/^https?:/.test(v))||'';
                          };
                          const richTokens=[];
                          const blockTags=new Set(['address','article','aside','blockquote','div','figcaption','figure','footer','h1','h2','h3','h4','h5','h6','header','li','main','nav','ol','p','pre','section','ul']);
                          const pushBreak=()=>{
                            const last=richTokens[richTokens.length-1];
                            if(!last||last.type!=='break')richTokens.push({type:'break'});
                          };
                          const pushText=text=>{
                            if(!text)return;
                            const last=richTokens[richTokens.length-1];
                            if(last?.type==='text')last.text+=text;
                            else richTokens.push({type:'text',text});
                          };
                          const emitRich=node=>{
                            if(!node||richTokens.length>=5000)return;
                            if(node.nodeType===Node.TEXT_NODE){pushText(node.nodeValue||'');return;}
                            if(node.nodeType!==Node.ELEMENT_NODE)return;
                            const tag=node.tagName.toLowerCase();
                            if(['script','style','noscript','template'].includes(tag))return;
                            if(tag==='br'){pushBreak();return;}
                            if(tag==='img'){
                              const url=bestImageUrl(node); const rect=node.getBoundingClientRect();
                              richTokens.push(url?{type:'inline_image',alt:node.getAttribute('alt')||'',sourceUrl:url,width:node.naturalWidth||Math.round(rect.width)||null,height:node.naturalHeight||Math.round(rect.height)||null}:{type:'unresolved',kind:'inline_image',label:'[未解析图片表情]'});
                              return;
                            }
                            if(tag==='svg'){richTokens.push({type:'unresolved',kind:'inline_svg',label:'[未解析SVG表情]'});return;}
                            const block=blockTags.has(tag);
                            if(block&&richTokens.length)pushBreak();
                            if(tag==='li')pushText(node.parentElement?.tagName?.toLowerCase()==='ol'?`${[...node.parentElement.children].indexOf(node)+1}. `:'• ');
                            for(const child of node.childNodes)emitRich(child);
                            if(block)pushBreak();
                          };
                          if(domBody){emitRich(domBody);while(richTokens[0]?.type==='break')richTokens.shift();while(richTokens[richTokens.length-1]?.type==='break')richTokens.pop();}
                          const domVisibleText=domBody ? (domBody.innerText ?? domBody.textContent ?? '') : null;
                          const richAlignment=typeof bodyRaw==='string' && typeof domVisibleText==='string' ? (bodyRaw===domVisibleText ? 'EXACT' : 'MISMATCH') : 'UNAVAILABLE';
                          const stateImages=exact && (exact.imageList || exact.image_list || exact.images);
                          const pickUrl=item=>{
                            if(typeof item==='string') return item;
                            if(!item||typeof item!=='object') return '';
                            for(const key of ['urlDefault','url_default','url','originalUrl','original_url','urlPre','url_pre']) if(typeof item[key]==='string'&&/^https?:/.test(item[key])) return item[key];
                            for(const key of ['urlList','url_list','urls']) if(Array.isArray(item[key])) { const found=item[key].find(v=>typeof v==='string'&&/^https?:/.test(v)); if(found)return found; }
                            return '';
                          };
                          let orderedBy='unknown'; let images=[]; let expected=null;
                          if(Array.isArray(stateImages) && stateImages.length){
                            images=stateImages.map((item,index)=>({url:pickUrl(item),ordinal:index+1,width:Number(item?.width||item?.imageWidth)||null,height:Number(item?.height||item?.imageHeight)||null,sourceKind:'embedded_state'})).filter(v=>v.url);
                            expected=stateImages.length; orderedBy='page_state';
                          } else {
                            const nodes=[...document.querySelectorAll('[class*="note-detail"] [class*="swiper"] img,[class*="note-detail"] img,article [class*="swiper"] img')].filter(visible);
                            const urls=new Set();
                            for(const node of nodes){
                              const candidates=[node.currentSrc,node.src,node.getAttribute('data-src'),node.getAttribute('data-original'),...(node.getAttribute('srcset')||'').split(',').map(v=>v.trim().split(/\s+/)[0])];
                              const selected=candidates.find(v=>typeof v==='string'&&/^https?:/.test(v));
                              if(!selected||urls.has(selected))continue;
                              const rect=node.getBoundingClientRect(); if(rect.width<180||rect.height<120)continue;
                              urls.add(selected); images.push({url:selected,ordinal:images.length+1,width:node.naturalWidth||null,height:node.naturalHeight||null,sourceKind:'ordered_carousel_dom'});
                            }
                            if(images.length){expected=images.length;orderedBy='carousel_dom';}
                          }
                          const canonical=document.querySelector('link[rel="canonical"]')?.href||location.href;
                          const postPath=/\/(?:explore|discovery\/item)\/([A-Za-z0-9]+)(?:\/|$)/;
                          const finalMatch=canonical.match(postPath) || location.href.match(postPath);
                          const statePostId=exact ? (explicitPostId(exact)||genericId(exact)) : '';
                          const detailUrlPostId=finalMatch?.[1]||'';
                          const observedId=statePostId||detailUrlPostId;
                          const meta=s=>document.querySelector(s)?.getAttribute('content')||'';
                          const title=(exact && String(exact.title||exact.noteTitle||'')) || meta('meta[property="og:title"]') || firstVisible(['h1','[data-testid="note-title"]','#detail-title'])?.innerText || '';
                          const author=(exact && String(exact.user?.nickname||exact.author?.name||'')) || meta('meta[name="author"]') || firstVisible(['[data-testid="author-name"]','a[href*="/user/profile"] [class*="name"]'])?.innerText || '';
                          const publishedAt=(exact && String(exact.time||exact.publishTime||exact.publish_time||'')) || meta('meta[property="article:published_time"]') || '';
                          const coverUrl=meta('meta[property="og:image"]');
                          const selectedScore=score(exact);
                          const bodyVerified=bodySource==='embedded_state' && (selectedBodyField==='desc'||selectedBodyField==='noteDesc') && typeof bodyRaw==='string' && bodyRaw.trim().length>0 && selectedScore>=60;
                          return {bodyRaw,bodySource,bodyVerified,bodyRich:richTokens,richAlignment,stateCandidateCount:exactCandidates.length,selectedStateScore:selectedScore,selectedBodyField,images,expectedImageCount:expected,orderedBy,coverUrl,observedId,statePostId,detailUrlPostId,title,author,publishedAt,canonical,videoCount:document.querySelectorAll('video').length,pageTitle:document.title,finalUrl:location.href,visibleText:(document.body?.innerText||'').slice(0,5000)};
                        }""",
                        target_post_id,
                    )
                    diagnostic_text = urllib.parse.unquote(
                        f"{data.get('finalUrl')} {data.get('pageTitle')} {data.get('visibleText')}"
                    )
                    final_state = _page_access_state(diagnostic_text)
                    if final_state in {"verification_required", "login_required", "platform_limited"}:
                        page.close()
                        page = None
                        raise self._xhs_access_error(
                            final_state,
                            action_url=str(data.get("finalUrl") or access_url),
                            archive=True,
                        )
                    if final_state == "post_unavailable" and not _xhs_unavailable_is_body_quote(
                        page, target_post_id, data,
                    ):
                        raise PlatformBrowserError(
                            "帖子不存在、已删除或当前账号不可访问",
                            code="post_unavailable",
                            diagnostics={
                                "target_post_id": target_post_id,
                                "final_url": _diagnostic_url(str(data.get("finalUrl") or "")),
                            },
                        )
                    state_post_id = str(data.get("statePostId") or "")
                    detail_url_post_id = str(data.get("detailUrlPostId") or "")
                    # Structured state and the final detail URL are independent
                    # identity signals.  Do not let a stale state object hide a
                    # redirect to another post with a similar title.
                    if state_post_id and state_post_id != target_post_id:
                        raise PlatformBrowserError(
                            "页面状态帖子 ID 与目标帖子不一致，已拒绝归档",
                            code="identity_mismatch",
                            diagnostics={
                                "target_post_id": target_post_id,
                                "observed_post_id": state_post_id,
                                "detail_url_post_id": detail_url_post_id,
                                "final_url": _diagnostic_url(str(data.get("finalUrl") or "")),
                            },
                        )
                    if detail_url_post_id and detail_url_post_id != target_post_id:
                        raise PlatformBrowserError(
                            "最终详情页 URL 的帖子 ID 与目标帖子不一致，已拒绝归档",
                            code="identity_mismatch",
                            diagnostics={
                                "target_post_id": target_post_id,
                                "observed_post_id": detail_url_post_id,
                                "state_post_id": state_post_id,
                                "final_url": _diagnostic_url(str(data.get("finalUrl") or "")),
                            },
                        )
                    observed_post_id = str(data.get("observedId") or "")
                    if observed_post_id != target_post_id:
                        raise PlatformBrowserError(
                            "详情页帖子 ID 与目标帖子不一致，已拒绝归档",
                            code="identity_mismatch",
                            diagnostics={
                                "target_post_id": target_post_id,
                                "observed_post_id": observed_post_id,
                                "final_url": _diagnostic_url(str(data.get("finalUrl") or "")),
                            },
                        )

                    candidates: list[dict[str, Any]] = []
                    for item in data.get("images") or []:
                        if not isinstance(item, dict) or not str(item.get("url") or "").startswith(("http://", "https://")):
                            continue
                        candidates.append({
                            "role": "carousel",
                            "ordinal": int(item.get("ordinal") or len(candidates) + 1),
                            "source_url": str(item["url"]),
                            "source_kind": str(item.get("sourceKind") or "page_state"),
                            "ordered_by": str(data.get("orderedBy") or "unknown"),
                            "width": item.get("width"),
                            "height": item.get("height"),
                        })
                    cover_url = str(data.get("coverUrl") or "")
                    if cover_url.startswith(("http://", "https://")):
                        candidates.append({
                            "role": "cover", "ordinal": 1, "source_url": cover_url,
                            "source_kind": "og_image", "ordered_by": "cover_metadata",
                        })
                    for token_index, token in enumerate(data.get("bodyRich") or []):
                        if not isinstance(token, dict) or token.get("type") != "inline_image":
                            continue
                        inline_url = str(token.get("sourceUrl") or "")
                        if not inline_url.startswith(("http://", "https://")):
                            continue
                        candidates.append({
                            "role": "media_part",
                            "ordinal": token_index + 1,
                            "source_url": inline_url,
                            "source_kind": "body_dom_inline_image",
                            "ordered_by": "body_dom_order",
                            "subtype": "inline_image",
                            "token_index": token_index,
                            "width": token.get("width"),
                            "height": token.get("height"),
                        })

                    response_by_url = {str(response.url): response for response in observed_responses}
                    assets: list[dict[str, Any]] = []
                    for candidate in candidates[:40]:
                        source_url = str(candidate["source_url"])
                        payload: bytes | None = None
                        declared = ""
                        http_status: int | None = None
                        source_kind = str(candidate["source_kind"])
                        error_code = ""
                        try:
                            _remaining_budget(15, deadline, cancel_event)
                        except TimeoutError:
                            error_code = "IMAGE_TIMEOUT"
                        observed = response_by_url.get(source_url)
                        if observed is not None and not error_code:
                            try:
                                http_status = int(observed.status)
                                declared = str(observed.headers.get("content-type") or "")
                                if http_status == 200:
                                    payload = observed.body()
                                    source_kind = f"{source_kind}+network_response"
                                else:
                                    error_code = f"HTTP_{http_status}"
                            except TimeoutError:
                                error_code = "IMAGE_TIMEOUT"
                                payload = None
                            except Exception:
                                payload = None
                        if payload is None and error_code != "IMAGE_TIMEOUT":
                            try:
                                response = context.request.get(source_url, timeout=15000, fail_on_status_code=False)
                                http_status = int(response.status)
                                declared = str(response.headers.get("content-type") or declared)
                                if http_status == 200:
                                    body = response.body()
                                    if len(body) <= 25 * 1024 * 1024:
                                        payload = body
                                        source_kind = f"{source_kind}+authorized_context_request"
                                    else:
                                        error_code = "IMAGE_TOO_LARGE"
                                else:
                                    error_code = f"HTTP_{http_status}"
                            except TimeoutError:
                                error_code = "IMAGE_TIMEOUT"
                            except Exception:
                                error_code = error_code or "RESPONSE_BODY_UNAVAILABLE"
                        assets.append({
                            **candidate,
                            "source_kind": source_kind,
                            "mime_declared": declared,
                            "http_status": http_status,
                            "bytes": payload,
                            "error_code": error_code if payload is None else "",
                            "rendition": "browser_returned",
                        })
                    body_rich: list[dict[str, Any]] = []
                    for token_index, token in enumerate(data.get("bodyRich") or []):
                        if not isinstance(token, dict):
                            continue
                        clean_token = {key: value for key, value in token.items() if key != "sourceUrl"}
                        if token.get("type") == "inline_image" and str(token.get("sourceUrl") or "").startswith(("http://", "https://")):
                            clean_token["asset_role"] = "media_part"
                            clean_token["asset_ordinal"] = token_index + 1
                        body_rich.append(clean_token)
                    result = {
                        "target_post_id": target_post_id,
                        "observed_post_id": observed_post_id,
                        "canonical_url": canonical_url,
                        "title": str(data.get("title") or ""),
                        "author": str(data.get("author") or ""),
                        "published_at": str(data.get("publishedAt") or "") or None,
                        "body_raw": data.get("bodyRaw"),
                        "body_source": str(data.get("bodySource") or "unavailable"),
                        "body_verified": bool(data.get("bodyVerified")),
                        "body_rich": body_rich,
                        "body_rich_alignment": str(data.get("richAlignment") or "UNAVAILABLE"),
                        "body_evidence": {
                            "source": str(data.get("bodySource") or "unavailable"),
                            "character_count": len(data.get("bodyRaw") or ""),
                            "same_post_id": True,
                            "state_candidate_count": int(data.get("stateCandidateCount") or 0),
                            "selected_state_score": int(data.get("selectedStateScore") or 0),
                            "selected_body_field": str(data.get("selectedBodyField") or ""),
                            "expanded_before_read": True,
                            "rich_alignment": str(data.get("richAlignment") or "UNAVAILABLE"),
                            "hydration_wait_ms": int(hydration_diagnostics.get("wait_ms") or 0),
                            "hydration_attempts": int(hydration_diagnostics.get("attempts") or 0),
                            "hydration_reason": str(hydration_diagnostics.get("reason") or ""),
                        },
                        "identity_evidence": {
                            "state_post_id": state_post_id,
                            "detail_url_post_id": detail_url_post_id,
                            "signals_agree": not (
                                state_post_id and detail_url_post_id and state_post_id != detail_url_post_id
                            ),
                        },
                        "expected_image_count": data.get("expectedImageCount"),
                        "video_count": int(data.get("videoCount") or 0),
                        "assets": assets,
                        "diagnostics": {
                            "page_title": str(data.get("pageTitle") or "")[:300],
                            "final_url": _diagnostic_url(str(data.get("finalUrl") or "")),
                            "ordered_by": str(data.get("orderedBy") or "unknown"),
                            "candidate_count": len(candidates),
                            "response_image_count": len(observed_responses),
                            "image_budget_exhausted": any(asset.get("error_code") == "IMAGE_TIMEOUT" for asset in assets),
                            "hydration": hydration_diagnostics,
                        },
                    }
                    page.close()
                    page = None
                    return result
            except PlatformBrowserError:
                raise
            except TimeoutError as exc:
                raise PlatformBrowserError("小红书归档读取已超出时间预算", code="archive_timeout") from exc
            except Exception as exc:
                raise PlatformBrowserError(
                    f"图文归档读取失败：{str(exc)[:180]}", code="archive_collect_failed"
                ) from exc
            finally:
                if page is not None and not keep_page_open:
                    try:
                        page.close()
                    except Exception:
                        pass


platform_browser_collector = PlatformBrowserCollector()
