from __future__ import annotations

import hashlib
import io
import json
import mimetypes
import os
import queue
import re
import shutil
import threading
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image

from .config import XHS_ARCHIVE_DIR
from .db import Database, database, json_dumps, json_loads, new_id, utc_now
from .platform_browser import PlatformBrowserCollector, PlatformBrowserError, platform_browser_collector


COLLECTOR_VERSION = "xhs-archive-v2.4-clipper-layout"
PARSER_VERSION = "xhs-parser-v4"
BROWSER_PROFILE_ID = "social_browser"
ACTIVE_JOB_STATUSES = ("QUEUED", "RUNNING")


def _redact_error(value: str) -> str:
    text = re.sub(r"(?i)(authorization|cookie|token|xsec_token|signature|sign)=([^\s&]+)", r"\1=[REDACTED]", value)
    return re.sub(r"https?://[^\s]+", lambda item: _safe_source_ref(item.group(0))[0] or "[REDACTED_URL]", text)[:500]


def _safe_failure_diagnostics(value: Any) -> dict[str, Any]:
    """Persist only identity/recovery facts, never raw platform responses."""
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for key in ("target_post_id", "observed_post_id", "requested_post_id", "stored_post_id", "state_post_id", "detail_url_post_id"):
        identifier = value.get(key)
        if isinstance(identifier, str) and re.fullmatch(r"[a-fA-F0-9]{24}", identifier):
            result[key] = identifier
    for key in ("final_url", "candidate_url", "recovered_url", "requested_url", "action_url"):
        url = value.get(key)
        if not isinstance(url, str):
            continue
        try:
            parsed = urllib.parse.urlsplit(url)
            host = parsed.hostname or ""
            if parsed.scheme not in {"http", "https"} or not (host == "xiaohongshu.com" or host.endswith(".xiaohongshu.com")):
                continue
            # Do not retain userinfo, query strings, or fragments even when an
            # exception accidentally supplies an unsanitized access URL.
            result[key] = urllib.parse.urlunsplit((parsed.scheme, host, parsed.path[:500], "", ""))
        except ValueError:
            continue
    for key in ("recovery_attempts", "recovery_query_count", "attempted_query_count", "retry_count", "recovery_result_count", "recovery_unique_id_count", "exact_match_count", "title_match_count"):
        count = value.get(key)
        if isinstance(count, int) and not isinstance(count, bool) and 0 <= count <= 10000:
            result[key] = count
    if isinstance(value.get("attempted_queries"), list):
        result.setdefault("recovery_query_count", min(len(value["attempted_queries"]), 10000))
    reasons = {
        "post_unavailable", "login_required", "verification_required", "platform_limited",
        "browser_not_running", "browser_page_required", "same_id_not_found", "similar_title_different_id",
        "access_url_expired", "exact_id_recovered", "no_exact_id_result", "original_url_unavailable", "no_search_match",
        "limited_search_no_exact_id",
    }
    for key in ("reason", "recovery_reason", "access_state"):
        if isinstance(value.get(key), str) and value[key] in reasons:
            result[key] = value[key]
    if isinstance(value.get("recovery_stop_reason"), str) and value["recovery_stop_reason"] in {
        "exact_id_found", "stable_results", "timeout", "invalid_snapshot", "no_queries",
        "login_required", "verification_required", "platform_limited",
    }:
        result["recovery_stop_reason"] = value["recovery_stop_reason"]
    for key in ("same_post_id", "signals_agree", "exact_id_found", "recovery_attempted"):
        if isinstance(value.get(key), bool):
            result[key] = value[key]
    return result


def normalize_body(value: str) -> str:
    """Create searchable text without mutating the archived raw body."""
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    output: list[str] = []
    blank = False
    for line in lines:
        if line:
            output.append(line)
            blank = False
        elif output and not blank:
            output.append("")
            blank = True
    return "\n".join(output).strip()


def _safe_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "_", value or "")[:160]
    if not cleaned:
        raise ValueError("归档目录标识为空")
    return cleaned


def _safe_source_ref(value: str) -> tuple[str, str]:
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        return "", ""
    if parsed.scheme not in {"http", "https"}:
        return "", ""
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")), parsed.hostname or ""


def _xhs_post_id_from_url(value: str) -> str:
    """Return the canonical post ID embedded in a persisted XHS detail URL.

    The archive worker is deliberately stricter than its collector.  A
    collector can only prove that the page it read is self-consistent; the
    persisted extraction record is the source that proves which post was
    requested in the first place.  Keep this parser local and query-free so it
    is safe to use again just before a version is written.
    """
    try:
        path = urllib.parse.urlsplit(str(value or "")).path
    except ValueError:
        return ""
    match = re.search(r"/(?:explore|discovery/item)/([A-Za-z0-9]+)(?:/|$)", path)
    return match.group(1) if match else ""


def _image_metadata(payload: bytes, declared: str | None) -> tuple[str, str, int | None, int | None]:
    detected = ""
    extension = ""
    width: int | None = None
    height: int | None = None
    try:
        with Image.open(io.BytesIO(payload)) as image:
            image.verify()
        with Image.open(io.BytesIO(payload)) as image:
            width, height = image.size
            detected = Image.MIME.get(image.format or "", "")
            extension = f".{(image.format or '').lower()}" if image.format else ""
    except Exception:
        detected = ""
    mime = detected or str(declared or "").split(";", 1)[0].strip().lower()
    if not extension and mime:
        extension = mimetypes.guess_extension(mime) or ""
    if extension == ".jpe":
        extension = ".jpg"
    return mime, extension, width, height


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


@dataclass
class ArchiveResult:
    archive_id: str
    archive_status: str
    verification_status: str
    archive_path: str
    expected_asset_count: int | None
    saved_asset_count: int
    missing_ordinals: list[int]


class XiaohongshuArchiveRepository:
    def __init__(self, db: Database = database, root: Path = XHS_ARCHIVE_DIR) -> None:
        self.db = db
        self.root = root

    def persist(self, workspace_id: str, extraction_id: str, capture: dict[str, Any]) -> ArchiveResult:
        target_post_id = str(capture.get("target_post_id") or "")
        observed_post_id = str(capture.get("observed_post_id") or "")
        extraction = self.db.one(
            "SELECT url,post_key FROM xhs_post_extractions WHERE workspace_id=? AND id=?",
            (workspace_id, extraction_id),
        )
        if not extraction:
            raise ValueError("小红书记录不存在或不属于当前工作区")
        requested_post_id = _xhs_post_id_from_url(str(extraction.get("url") or ""))
        stored_post_id = str(extraction.get("post_key") or "")
        if not requested_post_id:
            raise PlatformBrowserError(
                "归档记录缺少可校验的小红书原帖 ID，已拒绝写入归档",
                code="identity_mismatch",
                diagnostics={"stored_post_id": stored_post_id},
            )
        raw_identity_evidence = capture.get("identity_evidence")
        if not isinstance(raw_identity_evidence, dict):
            raw_identity_evidence = {}
        state_post_id = str(raw_identity_evidence.get("state_post_id") or "")
        detail_url_post_id = str(raw_identity_evidence.get("detail_url_post_id") or "")
        identity_evidence = (
            {
                "state_post_id": state_post_id,
                "detail_url_post_id": detail_url_post_id,
                "signals_agree": not (
                    state_post_id and detail_url_post_id and state_post_id != detail_url_post_id
                ),
            }
            if state_post_id or detail_url_post_id
            else {}
        )
        if not target_post_id or target_post_id != observed_post_id:
            raise PlatformBrowserError(
                "详情页帖子 ID 与目标帖子不一致，已拒绝写入归档",
                code="identity_mismatch",
                diagnostics={"target_post_id": target_post_id, "observed_post_id": observed_post_id},
            )
        # The target is derived from the URL that was originally selected from
        # search.  Never let a stale or malformed collector change that identity
        # merely because its own target and observed IDs happen to agree.
        if requested_post_id and target_post_id != requested_post_id:
            raise PlatformBrowserError(
                "归档结果与所选原帖链接的帖子 ID 不一致，已拒绝写入归档",
                code="identity_mismatch",
                diagnostics={
                    "requested_post_id": requested_post_id,
                    "target_post_id": target_post_id,
                    "observed_post_id": observed_post_id,
                },
            )
        if stored_post_id and target_post_id != stored_post_id:
            raise PlatformBrowserError(
                "归档结果与已有原帖记录的帖子 ID 不一致，已拒绝写入归档",
                code="identity_mismatch",
                diagnostics={
                    "requested_post_id": requested_post_id,
                    "stored_post_id": stored_post_id,
                    "target_post_id": target_post_id,
                    "observed_post_id": observed_post_id,
                },
            )
        # The browser collector records the ID found in structured page state
        # separately from the final detail URL.  When either signal exists, it
        # must point back to the selected post; a disagreement can otherwise
        # hide a redirect to a similarly titled post.
        if state_post_id and state_post_id != target_post_id:
            raise PlatformBrowserError(
                "页面状态中的帖子 ID 与所选原帖不一致，已拒绝写入归档",
                code="identity_mismatch",
                diagnostics={
                    "target_post_id": target_post_id,
                    "observed_post_id": observed_post_id,
                    "state_post_id": state_post_id,
                    "detail_url_post_id": detail_url_post_id,
                },
            )
        if detail_url_post_id and detail_url_post_id != target_post_id:
            raise PlatformBrowserError(
                "最终详情页 URL 的帖子 ID 与所选原帖不一致，已拒绝写入归档",
                code="identity_mismatch",
                diagnostics={
                    "target_post_id": target_post_id,
                    "observed_post_id": observed_post_id,
                    "state_post_id": state_post_id,
                    "detail_url_post_id": detail_url_post_id,
                },
            )

        body_raw = capture.get("body_raw")
        if body_raw is not None and not isinstance(body_raw, str):
            body_raw = str(body_raw)
        body_normalized = normalize_body(body_raw or "") if body_raw is not None else None
        # Whitespace is preserved byte-for-byte in body_raw, but an empty or
        # whitespace-only field is not evidence that a complete original body
        # was obtained.
        body_verified = bool(capture.get("body_verified") and isinstance(body_raw, str) and body_raw.strip())
        body_source = str(capture.get("body_source") or "unavailable")
        # We deliberately do not promote expanded DOM text to original copy.
        # When that fallback is the only non-empty source, expose the parser
        # mismatch as a first-class state instead of the generic "unverified"
        # message; it may indicate that the platform's page structure changed.
        page_structure_changed = bool(
            not body_verified
            and body_source == "expanded_dom"
            and isinstance(body_raw, str)
            and body_raw.strip()
        )
        body_extraction_status = (
            "VERIFIED" if body_verified
            else "PAGE_STRUCTURE_CHANGED" if page_structure_changed
            else "UNVERIFIED" if isinstance(body_raw, str) and body_raw.strip()
            else "MISSING"
        )
        body_extraction_message = (
            "同一 post_id 的结构化正文已验证"
            if body_verified
            else "仅发现页面可见 DOM 文本，当前解析器无法从受支持的结构化正文来源核验；可能是平台页面结构变化。该文本不会作为原帖全文展示。"
            if page_structure_changed
            else "读取到文本，但来源或帖子身份尚未通过原文校验"
            if body_extraction_status == "UNVERIFIED"
            else "严格归档未读到可验证的正文"
        )
        body_rich = capture.get("body_rich") if isinstance(capture.get("body_rich"), list) else []
        body_raw_bytes = body_raw.encode("utf-8") if body_raw is not None else b""
        body_sha256 = hashlib.sha256(body_raw_bytes).hexdigest() if body_raw is not None else None
        body_evidence = dict(capture.get("body_evidence") or {})
        body_evidence["body_extraction_status"] = body_extraction_status
        if page_structure_changed:
            body_evidence["body_extraction_code"] = "PAGE_STRUCTURE_CHANGED"
            body_evidence["body_extraction_message"] = body_extraction_message
        if identity_evidence:
            body_evidence["identity_evidence"] = identity_evidence
        if body_raw is not None:
            body_evidence["format_integrity"] = {
                "encoding": "utf-8",
                "byte_count": len(body_raw_bytes),
                "sha256": body_sha256,
                "lf_count": body_raw.count("\n"),
                "cr_count": body_raw.count("\r"),
                "tab_count": body_raw.count("\t"),
                "leading_whitespace_preserved": bool(body_raw[:1] and body_raw[:1].isspace()),
                "trailing_whitespace_preserved": bool(body_raw[-1:] and body_raw[-1:].isspace()),
            }
        unresolved_rich = sum(1 for token in body_rich if isinstance(token, dict) and token.get("type") == "unresolved")
        has_inline_rich = any(isinstance(token, dict) and token.get("type") == "inline_image" for token in body_rich)
        rich_alignment = str(capture.get("body_rich_alignment") or "UNAVAILABLE")
        expected = capture.get("expected_image_count")
        expected_count = int(expected) if isinstance(expected, int) and expected >= 0 else None
        archive_id = new_id("xhsa")
        captured_at = utc_now()
        workspace_part = _safe_component(workspace_id)
        post_part = _safe_component(target_post_id)
        archive_part = _safe_component(archive_id)
        final_dir = self.root / workspace_part / post_part / archive_part
        staging_dir = final_dir.with_name(f".{final_dir.name}.part")
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        (staging_dir / "images").mkdir(parents=True, exist_ok=True)

        asset_rows: list[dict[str, Any]] = []
        manifest_assets: list[dict[str, Any]] = []
        saved_carousel: set[int] = set()
        cover_discovered = False
        cover_saved = False
        for candidate in capture.get("assets") or []:
            if not isinstance(candidate, dict):
                continue
            role = str(candidate.get("role") or "carousel")
            if role not in {"carousel", "cover", "media_part"}:
                continue
            ordinal = max(1, int(candidate.get("ordinal") or 1))
            if role == "cover":
                cover_discovered = True
            asset_id = new_id("xhsi")
            raw_url = str(candidate.get("source_url") or "")
            source_ref, source_host = _safe_source_ref(raw_url)
            payload = candidate.get("bytes")
            if not isinstance(payload, (bytes, bytearray)):
                payload = None
            declared = str(candidate.get("mime_declared") or "") or None
            http_status = candidate.get("http_status") if isinstance(candidate.get("http_status"), int) else None
            status = "FAILED" if candidate.get("error_code") or (http_status is not None and http_status >= 400) else "MISSING"
            error_code = str(candidate.get("error_code") or "RESPONSE_BODY_UNAVAILABLE")
            relative_path: str | None = None
            sha256: str | None = None
            byte_size: int | None = None
            mime_detected = ""
            extension = ""
            width = candidate.get("width") if isinstance(candidate.get("width"), int) else None
            height = candidate.get("height") if isinstance(candidate.get("height"), int) else None
            if payload:
                mime_detected, extension, detected_width, detected_height = _image_metadata(bytes(payload), declared)
                width = detected_width or width
                height = detected_height or height
                if mime_detected.startswith("image/") and extension:
                    filename = f"{role}-{ordinal:03d}{extension}"
                    target = staging_dir / "images" / filename
                    target.write_bytes(bytes(payload))
                    relative_path = str(target.relative_to(staging_dir))
                    sha256 = hashlib.sha256(payload).hexdigest()
                    byte_size = len(payload)
                    status = "SAVED"
                    error_code = ""
                    if role == "carousel":
                        saved_carousel.add(ordinal)
                    elif role == "cover":
                        cover_saved = True
                else:
                    error_code = "INVALID_IMAGE_BYTES"
            row = {
                "id": asset_id,
                "workspace_id": workspace_id,
                "archive_id": archive_id,
                "role": role,
                "ordinal": ordinal,
                "source_ref": source_ref,
                "source_kind": str(candidate.get("source_kind") or "browser_response")[:80],
                "source_host": source_host,
                "rendition": str(candidate.get("rendition") or "browser_returned")[:80],
                "mime_declared": declared,
                "mime_detected": mime_detected or None,
                "extension": extension or None,
                "width": width,
                "height": height,
                "duration_ms": None,
                "byte_size": byte_size,
                "bytes_expected": candidate.get("bytes_expected") if isinstance(candidate.get("bytes_expected"), int) else None,
                "sha256": sha256,
                "relative_path": relative_path,
                "status": status,
                "http_status": http_status,
                "error_code": error_code or None,
                "retry_after": None,
                "evidence_json": json_dumps({
                    "source": str(candidate.get("source_kind") or "browser_response"),
                    "ordered_by": str(candidate.get("ordered_by") or "page_state"),
                    "subtype": str(candidate.get("subtype") or ""),
                    "token_index": candidate.get("token_index") if isinstance(candidate.get("token_index"), int) else None,
                }),
                "created_at": captured_at,
            }
            asset_rows.append(row)
            manifest_assets.append({key: value for key, value in row.items() if key not in {"workspace_id", "archive_id", "evidence_json"}})

        saved_count = len(saved_carousel)
        missing = [] if expected_count is None else [value for value in range(1, expected_count + 1) if value not in saved_carousel]
        has_video = bool(capture.get("video_count"))
        identity_ok = target_post_id == observed_post_id
        images_complete = expected_count is not None and expected_count > 0 and saved_count == expected_count and not missing
        rich_assets_complete = all(
            row["status"] == "SAVED"
            for row in asset_rows
            if row["role"] == "media_part" and json_loads(row["evidence_json"], {}).get("subtype") == "inline_image"
        )
        rich_fidelity_ok = unresolved_rich == 0 and rich_assets_complete and (not has_inline_rich or rich_alignment == "EXACT")
        complete = identity_ok and body_verified and images_complete and (not cover_discovered or cover_saved) and not has_video and rich_fidelity_ok
        archive_status = "COMPLETE" if complete else "PARTIAL"
        verification_status = "VERIFIED" if identity_ok and body_verified else "UNVERIFIED"
        content_sha256 = body_sha256

        metadata = {
            "archive_id": archive_id,
            "workspace_id": workspace_id,
            "extraction_id": extraction_id,
            "post_id": target_post_id,
            "observed_post_id": observed_post_id,
            "canonical_url": str(capture.get("canonical_url") or ""),
            "title": str(capture.get("title") or ""),
            "author": str(capture.get("author") or ""),
            "published_at": capture.get("published_at"),
            "body_source": body_source,
            "body_verified": body_verified,
            "body_extraction_status": body_extraction_status,
            "body_extraction_message": body_extraction_message,
            "identity_evidence": identity_evidence,
            "expected_image_count": expected_count,
            "saved_image_count": saved_count,
            "missing_ordinals": missing,
            "video_count": int(capture.get("video_count") or 0),
            "media_status": "MEDIA_UNAVAILABLE" if has_video else "NOT_APPLICABLE",
            "archive_status": archive_status,
            "verification_status": verification_status,
            "captured_at": captured_at,
            "collector_version": COLLECTOR_VERSION,
            "parser_version": PARSER_VERSION,
            "body_sha256": body_sha256,
            "body_format_integrity": body_evidence.get("format_integrity"),
            "body_rich_token_count": len(body_rich),
            "body_rich_unresolved_count": unresolved_rich,
            "body_rich_alignment": rich_alignment,
        }
        diagnostics = {
            "failure_reasons": [
                *([] if body_verified else ["BODY_UNVERIFIED"]),
                *([] if not page_structure_changed else ["PAGE_STRUCTURE_CHANGED"]),
                *([] if expected_count is not None else ["EXPECTED_IMAGE_COUNT_UNKNOWN"]),
                *([] if not missing else ["MISSING_IMAGE_ORDINALS"]),
                *([] if not cover_discovered or cover_saved else ["COVER_MISSING"]),
                *([] if not has_video else ["MEDIA_UNAVAILABLE"]),
                *([] if unresolved_rich == 0 else ["RICH_BODY_UNRESOLVED"]),
                *([] if rich_assets_complete else ["INLINE_ASSET_MISSING"]),
                *([] if not has_inline_rich or rich_alignment == "EXACT" else ["RICH_BODY_ALIGNMENT_UNVERIFIED"]),
            ],
            "missing_ordinals": missing,
            "identity_evidence": identity_evidence,
            "collector": capture.get("diagnostics") or {},
        }
        raw_body_path = staging_dir / "body_raw.txt"
        raw_body_path.write_bytes(body_raw_bytes)
        if body_raw is not None and hashlib.sha256(raw_body_path.read_bytes()).hexdigest() != body_sha256:
            shutil.rmtree(staging_dir, ignore_errors=True)
            raise OSError("逐字原文写入后的字节校验失败，已停止归档")
        (staging_dir / "body_normalized.txt").write_bytes((body_normalized or "").encode("utf-8"))
        (staging_dir / "body_rich.json").write_bytes(_json_bytes(body_rich))
        (staging_dir / "metadata.json").write_bytes(_json_bytes(metadata))
        (staging_dir / "diagnostics.json").write_bytes(_json_bytes(diagnostics))
        manifest = {"version": 2, "assets": manifest_assets}
        manifest_bytes = _json_bytes(manifest)
        (staging_dir / "manifest.json").write_bytes(manifest_bytes)
        manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        checksum_lines = []
        for path in sorted(item for item in staging_dir.rglob("*") if item.is_file()):
            checksum_lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(staging_dir)}")
        (staging_dir / "checksums.sha256").write_text("\n".join(checksum_lines) + "\n", encoding="utf-8")
        final_dir.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging_dir, final_dir)

        with self.db.transaction() as connection:
            current = connection.execute(
                "SELECT archive_status,latest_archive_id,verification_status FROM xhs_post_extractions WHERE workspace_id=? AND id=?",
                (workspace_id, extraction_id),
            ).fetchone()
            if not current:
                raise ValueError("小红书记录不存在或不属于当前工作区")
            connection.execute(
                """INSERT INTO xhs_archive_versions
                (id,workspace_id,extraction_id,post_id,observed_post_id,archive_path,body_raw,body_normalized,
                 body_source,body_evidence_json,body_rich_json,body_sha256,parser_version,
                 archive_status,verification_status,content_sha256,manifest_sha256,captured_at,collector_version)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    archive_id, workspace_id, extraction_id, target_post_id, observed_post_id,
                    str(final_dir.relative_to(self.root)), body_raw, body_normalized,
                    body_source,
                    json_dumps(body_evidence), json_dumps(body_rich), body_sha256, PARSER_VERSION, archive_status,
                    verification_status, content_sha256, manifest_sha256, captured_at, COLLECTOR_VERSION,
                ),
            )
            for row in asset_rows:
                columns = list(row)
                connection.execute(
                    f"INSERT INTO xhs_post_assets({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                    tuple(row[column] for column in columns),
                )
            # A verified body may belong to a PARTIAL archive because an image
            # failed.  A later empty/DOM-only read must not hide that known-good
            # text by selecting an unverified version, even though both archive
            # attempts are PARTIAL.  Keep the failed attempt in version history.
            keep_existing = (
                current[0] == "COMPLETE" and archive_status != "COMPLETE"
            ) or (
                current[2] == "VERIFIED" and verification_status != "VERIFIED"
            )
            if not keep_existing:
                current_failure_stage = "" if complete else "archive_partial"
                current_error_message = "" if complete else (
                    ("正文已核验，图片或版式未完整归档" if body_verified else "本次归档正文尚未通过完整核验")
                    + (f"（{'、'.join(diagnostics['failure_reasons'])}）" if diagnostics["failure_reasons"] else "")
                )
                connection.execute(
                    """UPDATE xhs_post_extractions SET
                    post_key=?,
                    title=CASE WHEN ?<>'' THEN ? ELSE title END,
                    author=CASE WHEN ?<>'' THEN ? ELSE author END,
                    published_at=COALESCE(?,published_at),
                    body_raw=CASE WHEN ? THEN ? ELSE body_raw END,
                    body_normalized=CASE WHEN ? THEN ? ELSE body_normalized END,
                    body_source=CASE WHEN ? THEN ? ELSE body_source END,
                    body_evidence_json=CASE WHEN ? THEN ? ELSE body_evidence_json END,
                    body_rich_json=CASE WHEN ? THEN ? ELSE body_rich_json END,
                    body_sha256=CASE WHEN ? THEN ? ELSE body_sha256 END,
                    parser_version=CASE WHEN ? THEN ? ELSE parser_version END,
                    status=?,archive_status=?,verification_status=?,content_sha256=?,latest_archive_id=?,
                    expected_asset_count=?,saved_asset_count=?,failure_stage=?,error_message=?,diagnostics_json=?,
                    updated_at=? WHERE workspace_id=? AND id=?""",
                    (
                        target_post_id,
                        str(capture.get("title") or ""), str(capture.get("title") or "")[:500],
                        str(capture.get("author") or ""), str(capture.get("author") or "")[:300],
                        capture.get("published_at") or None,
                        int(body_verified), body_raw, int(body_verified), body_normalized,
                        int(body_verified), body_source,
                        int(body_verified), json_dumps(body_evidence),
                        int(body_verified), json_dumps(body_rich), int(body_verified), body_sha256,
                        int(body_verified), PARSER_VERSION,
                        "success" if archive_status == "COMPLETE" else "partial",
                        archive_status, verification_status, content_sha256, archive_id,
                        expected_count, saved_count, current_failure_stage, current_error_message, json_dumps(diagnostics),
                        captured_at, workspace_id, extraction_id,
                    ),
                )
        return ArchiveResult(
            archive_id=archive_id,
            archive_status=archive_status,
            verification_status=verification_status,
            archive_path=str(final_dir),
            expected_asset_count=expected_count,
            saved_asset_count=saved_count,
            missing_ordinals=missing,
        )


class XiaohongshuArchiveService:
    def __init__(
        self,
        db: Database = database,
        collector: PlatformBrowserCollector = platform_browser_collector,
        root: Path = XHS_ARCHIVE_DIR,
        collector_timeout_seconds: float | None = None,
        transient_retry_delay_seconds: float = 2.0,
    ) -> None:
        self.db = db
        self.collector = collector
        self.repository = XiaohongshuArchiveRepository(db, root)
        self.collector_timeout_seconds = max(
            0.01,
            float(collector_timeout_seconds if collector_timeout_seconds is not None else os.getenv("XHS_ARCHIVE_TIMEOUT_SECONDS", "90")),
        )
        self.transient_retry_delay_seconds = max(0.0, float(transient_retry_delay_seconds))
        self._jobs: queue.Queue[str] = queue.Queue()
        self._queued: set[str] = set()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        now = utc_now()
        self.db.execute(
            """UPDATE xhs_archive_jobs SET status='QUEUED',stage='RECOVERED_AFTER_RESTART',
            lease_owner=NULL,lease_expires_at=NULL,heartbeat_at=?,updated_at=? WHERE status='RUNNING'""",
            (now, now),
        )
        # Older builds could leave the card in ready_to_archive after its
        # terminal job had already failed or paused.  Reconcile those rows on
        # every startup so the UI cannot spin forever after a restart.
        for row in self.db.all(
            """SELECT j.* FROM xhs_archive_jobs j
            JOIN xhs_post_extractions e
              ON e.workspace_id=j.workspace_id AND e.id=j.extraction_id
            WHERE e.status IN ('queued','extracting','ready_to_archive')
              AND j.status IN ('FAILED','PAUSED')
              AND j.id=(SELECT j2.id FROM xhs_archive_jobs j2
                        WHERE j2.workspace_id=j.workspace_id AND j2.extraction_id=j.extraction_id
                        ORDER BY j2.created_at DESC LIMIT 1)"""
        ):
            code = str(row.get("error_code") or "ARCHIVE_FAILED")
            progress = json_loads(row.get("progress_json"), {})
            status = {
                "POST_UNAVAILABLE": "unavailable",
                "IDENTITY_MISMATCH": "identity_mismatch",
                "LOGIN_REQUIRED": "needs_login",
                "VERIFICATION_REQUIRED": "needs_verification",
                "PLATFORM_LIMITED": "platform_limited",
                "BROWSER_PAGE_REQUIRED": "needs_browser",
                "BROWSER_NOT_RUNNING": "needs_browser",
            }.get(code.upper(), "failed")
            self._update_extraction_failure(
                row, status, code, str(progress.get("message") or code), now,
            )
        for row in self.db.all("SELECT id FROM xhs_archive_jobs WHERE status='QUEUED' ORDER BY created_at"):
            self._queue(str(row["id"]))
        self._thread = threading.Thread(target=self._loop, daemon=True, name="xhs-archive-worker")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _queue(self, job_id: str) -> None:
        with self._lock:
            if job_id in self._queued:
                return
            self._queued.add(job_id)
            self._jobs.put(job_id)

    def enqueue(
        self,
        workspace_id: str,
        extraction_id: str,
        *,
        resume_after_user_action: bool = False,
    ) -> dict[str, Any]:
        extraction = self.db.one(
            "SELECT id FROM xhs_post_extractions WHERE workspace_id=? AND id=?",
            (workspace_id, extraction_id),
        )
        if not extraction:
            raise ValueError("小红书记录不存在或不属于当前工作区")
        active = self.db.one(
            """SELECT * FROM xhs_archive_jobs WHERE workspace_id=? AND extraction_id=?
            AND operation='archive' AND status IN ('QUEUED','RUNNING') ORDER BY created_at DESC LIMIT 1""",
            (workspace_id, extraction_id),
        )
        if active:
            result = self.decode_job(active)
            self._relink_open_batch_items(workspace_id, extraction_id, result)
            return {**result, "queued_new": False}
        if resume_after_user_action:
            self.db.execute("DELETE FROM xhs_browser_pause WHERE profile_id=?", (BROWSER_PROFILE_ID,))
        pause = self.db.one("SELECT * FROM xhs_browser_pause WHERE profile_id=?", (BROWSER_PROFILE_ID,))
        revision_row = self.db.one(
            "SELECT COALESCE(MAX(revision),0) AS revision FROM xhs_archive_jobs WHERE workspace_id=? AND extraction_id=?",
            (workspace_id, extraction_id),
        ) or {"revision": 0}
        revision = int(revision_row["revision"] or 0) + 1
        job_id = new_id("xhsj")
        now = utc_now()
        status = "PAUSED" if pause else "QUEUED"
        stage = "USER_ACTION_REQUIRED" if pause else "QUEUED"
        self.db.execute(
            """INSERT INTO xhs_archive_jobs
            (id,workspace_id,extraction_id,operation,idempotency_key,browser_profile_id,status,stage,
             progress_json,attempt,revision,created_at,updated_at,error_code)
            VALUES(?,?,?,'archive',?,?,?,?,?,0,?,?,?,?)""",
            (
                job_id, workspace_id, extraction_id, f"archive:{extraction_id}:{revision}",
                BROWSER_PROFILE_ID, status, stage, json_dumps({"message": pause["reason"] if pause else "已进入归档队列"}),
                revision, now, now, "BROWSER_PAUSED" if pause else None,
            ),
        )
        self._relink_open_batch_items(workspace_id, extraction_id, {"id": job_id, "status": status})
        if status == "QUEUED":
            self._queue(job_id)
        return {**(self.get_job(workspace_id, job_id) or {}), "queued_new": status == "QUEUED"}

    def _relink_open_batch_items(self, workspace_id: str, extraction_id: str, job: dict[str, Any]) -> None:
        """Let an unfinished hand-selected batch follow an explicit retry.

        Completed/failed historical attempts remain tied to their original
        batch item. A paused attempt has not finished, so leaving its original
        job ID attached would make the batch appear paused forever.
        """
        self.db.execute(
            """UPDATE xhs_archive_batch_items SET job_id=?,status=?,error_code=NULL,updated_at=?
            WHERE workspace_id=? AND extraction_id=? AND (
                (job_id IS NULL AND status IN ('SELECTED','WAITING'))
                OR job_id IN (
                    SELECT id FROM xhs_archive_jobs WHERE workspace_id=? AND extraction_id=?
                    AND status IN ('QUEUED','RUNNING','PAUSED')
                )
            )""",
            (
                job["id"], "WAITING" if job.get("status") == "QUEUED" else str(job.get("status") or "WAITING"),
                utc_now(), workspace_id, extraction_id, workspace_id, extraction_id,
            ),
        )

    @staticmethod
    def decode_job(row: dict[str, Any]) -> dict[str, Any]:
        result = dict(row)
        result["progress"] = json_loads(result.pop("progress_json", None), {})
        return result

    def get_job(self, workspace_id: str, job_id: str) -> dict[str, Any] | None:
        row = self.db.one("SELECT * FROM xhs_archive_jobs WHERE workspace_id=? AND id=?", (workspace_id, job_id))
        return self.decode_job(row) if row else None

    def list_jobs(self, workspace_id: str, extraction_id: str | None = None) -> list[dict[str, Any]]:
        if extraction_id:
            rows = self.db.all(
                "SELECT * FROM xhs_archive_jobs WHERE workspace_id=? AND extraction_id=? ORDER BY created_at DESC LIMIT 20",
                (workspace_id, extraction_id),
            )
        else:
            rows = self.db.all(
                "SELECT * FROM xhs_archive_jobs WHERE workspace_id=? ORDER BY created_at DESC LIMIT 100",
                (workspace_id,),
            )
        return [self.decode_job(row) for row in rows]

    def create_batch(self, workspace_id: str, extraction_ids: list[str]) -> dict[str, Any]:
        """Persist an explicit user selection and queue every selected post serially."""
        unique_ids = list(dict.fromkeys(str(value) for value in extraction_ids if value))
        if not unique_ids:
            raise ValueError("至少选择一篇小红书帖子")
        if len(unique_ids) > 100:
            raise ValueError("一次最多选择 100 篇帖子")
        placeholders = ",".join("?" for _ in unique_ids)
        found = self.db.all(
            f"SELECT id FROM xhs_post_extractions WHERE workspace_id=? AND id IN ({placeholders})",
            (workspace_id, *unique_ids),
        )
        found_ids = {str(row["id"]) for row in found}
        if found_ids != set(unique_ids):
            raise ValueError("选中的帖子包含无效记录或不属于当前工作区")
        batch_id = new_id("xhsb")
        now = utc_now()
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT INTO xhs_archive_batches(id,workspace_id,status,selected_count,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (batch_id, workspace_id, "SELECTED", len(unique_ids), now, now),
            )
            for ordinal, extraction_id in enumerate(unique_ids, 1):
                connection.execute(
                    """INSERT INTO xhs_archive_batch_items
                    (id,batch_id,workspace_id,extraction_id,ordinal,status,created_at,updated_at)
                    VALUES(?,?,?,?,?,'SELECTED',?,?)""",
                    (new_id("xhsbi"), batch_id, workspace_id, extraction_id, ordinal, now, now),
                )
        for extraction_id in unique_ids:
            try:
                job = self.enqueue(workspace_id, extraction_id)
                self.db.execute(
                    """UPDATE xhs_archive_batch_items SET job_id=?,status=?,updated_at=?
                    WHERE batch_id=? AND extraction_id=?""",
                    (job.get("id"), "WAITING" if job.get("status") == "QUEUED" else str(job.get("status") or "WAITING"), utc_now(), batch_id, extraction_id),
                )
            except Exception as exc:
                self.db.execute(
                    """UPDATE xhs_archive_batch_items SET status='FAILED',error_code=?,updated_at=?
                    WHERE batch_id=? AND extraction_id=?""",
                    (_redact_error(str(exc)), utc_now(), batch_id, extraction_id),
                )
        return self.get_batch(workspace_id, batch_id) or {}

    def get_batch(self, workspace_id: str, batch_id: str) -> dict[str, Any] | None:
        batch = self.db.one(
            "SELECT * FROM xhs_archive_batches WHERE workspace_id=? AND id=?",
            (workspace_id, batch_id),
        )
        if not batch:
            return None
        rows = self.db.all(
            """SELECT i.id,i.ordinal,i.extraction_id,i.job_id,i.error_code,i.status AS stored_status,
                      e.url,e.title,e.archive_status,j.status AS job_status,j.stage,j.progress_json,j.error_code AS job_error
               FROM xhs_archive_batch_items i
               JOIN xhs_post_extractions e ON e.workspace_id=i.workspace_id AND e.id=i.extraction_id
               LEFT JOIN xhs_archive_jobs j ON j.workspace_id=i.workspace_id AND j.id=i.job_id
               WHERE i.workspace_id=? AND i.batch_id=? ORDER BY i.ordinal""",
            (workspace_id, batch_id),
        )
        items: list[dict[str, Any]] = []
        counts = {key: 0 for key in ("selected", "waiting", "running", "complete", "partial", "paused", "failed", "invalid")}
        for row in rows:
            item = dict(row)
            progress = json_loads(item.pop("progress_json", None), {})
            job_status = str(item.pop("job_status", "") or "")
            archive_status = str(item.get("archive_status") or "")
            if job_status == "RUNNING":
                status = "running"
            elif job_status == "PAUSED":
                status = "paused"
            elif job_status == "FAILED":
                status = "failed"
            elif job_status == "QUEUED":
                status = "waiting"
            elif job_status == "COMPLETE" or (not job_status and archive_status == "COMPLETE"):
                status = "complete"
            elif job_status == "PARTIAL" or (not job_status and archive_status == "PARTIAL"):
                status = "partial"
            elif item["stored_status"] == "FAILED":
                status = "invalid"
            else:
                status = "selected"
            counts[status] += 1
            item["status"] = status
            item["progress"] = progress
            items.append(item)
        done = counts["complete"] + counts["partial"] + counts["failed"] + counts["invalid"]
        overall = "COMPLETE" if done == len(items) else "RUNNING" if counts["running"] else "PAUSED" if counts["paused"] else "QUEUED"
        return {**batch, "status": overall, "counts": counts, "items": items}

    def _heartbeat(self, job_id: str, stage: str, message: str) -> None:
        now = utc_now()
        self.db.execute(
            """UPDATE xhs_archive_jobs SET status='RUNNING',stage=?,progress_json=?,heartbeat_at=?,updated_at=?
            WHERE id=?""",
            (stage, json_dumps({"message": message}), now, now, job_id),
        )

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                job_id = self._jobs.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._run_job(job_id)
            finally:
                with self._lock:
                    self._queued.discard(job_id)
                self._jobs.task_done()

    def _run_job(self, job_id: str) -> None:
        job = self.db.one("SELECT * FROM xhs_archive_jobs WHERE id=?", (job_id,))
        if not job or job["status"] != "QUEUED":
            return
        extraction = self.db.one(
            "SELECT * FROM xhs_post_extractions WHERE workspace_id=? AND id=?",
            (job["workspace_id"], job["extraction_id"]),
        )
        if not extraction:
            self._finish_error(job, "EXTRACTION_NOT_FOUND", "归档来源记录不存在")
            return
        self._heartbeat(job_id, "READING_AUTHORIZED_PAGE", "正在读取登录浏览器中的同一篇帖子")
        self.db.execute("UPDATE xhs_archive_jobs SET attempt=attempt+1 WHERE id=?", (job_id,))
        attempted_at = utc_now()
        self.db.execute(
            """UPDATE xhs_post_extractions SET
            retry_count=retry_count+CASE WHEN last_attempt_at='' THEN 0 ELSE 1 END,
            last_attempt_at=?,updated_at=?
            WHERE workspace_id=? AND id=?""",
            (attempted_at, attempted_at, str(job["workspace_id"]), str(job["extraction_id"])),
        )
        try:
            capture = self._collect_with_transient_retry(
                job_id,
                str(extraction["url"]),
                search_keyword=str(extraction.get("search_keyword") or ""),
                title_hint=str(extraction.get("title") or ""),
            )
            self._heartbeat(job_id, "VERIFYING_AND_WRITING", "正在校验正文和图片字节并写入版本归档")
            result = self.repository.persist(str(job["workspace_id"]), str(job["extraction_id"]), capture)
            now = utc_now()
            status = "COMPLETE" if result.archive_status == "COMPLETE" else "PARTIAL"
            self.db.execute(
                """UPDATE xhs_archive_jobs SET status=?,stage='FINISHED',progress_json=?,heartbeat_at=?,
                error_code=NULL,updated_at=? WHERE id=?""",
                (
                    status,
                    json_dumps({
                        "message": "图文已完整归档" if status == "COMPLETE" else "已保存可取得内容，可继续补齐缺失项",
                        "archive_id": result.archive_id,
                        "expected_asset_count": result.expected_asset_count,
                        "saved_asset_count": result.saved_asset_count,
                        "missing_ordinals": result.missing_ordinals,
                    }),
                    now, now, job_id,
                ),
            )
        except PlatformBrowserError as exc:
            if exc.code in {
                "login_required", "verification_required", "platform_limited",
                "browser_page_required", "browser_not_running",
            }:
                self._pause_profile_jobs(job, exc.code, str(exc), diagnostics=exc.diagnostics)
            else:
                self._finish_error(job, exc.code.upper(), _redact_error(str(exc)), diagnostics=exc.diagnostics)
        except Exception as exc:
            self._finish_error(job, "ARCHIVE_FAILED", f"归档失败：{_redact_error(str(exc))}")

    def _pause_profile_jobs(
        self, job: dict[str, Any], code: str, message: str, *, diagnostics: Any = None,
    ) -> None:
        """Retain unattempted work when a shared browser needs user action."""
        now = utc_now()
        message = _redact_error(message)
        profile_id = str(job.get("browser_profile_id") or BROWSER_PROFILE_ID)
        browser_required = code in {"browser_page_required", "browser_not_running"}
        stage = "NEEDS_BROWSER" if browser_required else "USER_ACTION_REQUIRED"
        status = {
            "login_required": "needs_login",
            "verification_required": "needs_verification",
            "platform_limited": "platform_limited",
        }.get(code, "needs_browser")
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO xhs_browser_pause(profile_id,reason,retry_after,requires_user_action,updated_at)
                VALUES(?,?,NULL,1,?) ON CONFLICT(profile_id) DO UPDATE SET reason=excluded.reason,
                retry_after=excluded.retry_after,requires_user_action=1,updated_at=excluded.updated_at""",
                (profile_id, message, now),
            )
            paused_jobs = connection.execute(
                """SELECT * FROM xhs_archive_jobs WHERE browser_profile_id=?
                AND (id=? OR status='QUEUED')""",
                (profile_id, job["id"]),
            ).fetchall()
            connection.execute(
                """UPDATE xhs_archive_jobs SET status='PAUSED',stage=?,progress_json=?,
                error_code=?,heartbeat_at=?,updated_at=? WHERE browser_profile_id=?
                AND (id=? OR status='QUEUED')""",
                (stage, json_dumps({"message": message}), code.upper(), now, now, profile_id, job["id"]),
            )
            safe_diagnostics = _safe_failure_diagnostics(diagnostics)
            if safe_diagnostics:
                # Only the attempted item owns these identity observations;
                # pending items share the pause reason, not another post's ID.
                connection.execute(
                    "UPDATE xhs_archive_jobs SET progress_json=? WHERE id=?",
                    (json_dumps({"message": message, "diagnostics": safe_diagnostics}), job["id"]),
                )
        for paused in paused_jobs:
            self._update_extraction_failure(dict(paused), status, code, message, now)

    def _collect_with_deadline(self, url: str, **kwargs: Any) -> dict[str, Any]:
        """Cancel timed-out work and never stack collectors behind its lock."""
        import inspect

        result: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)
        cancel_event = threading.Event()
        # Reserve cleanup/serialization time so a verified body plus unfinished
        # images can return PARTIAL before the external job budget expires.
        timeout = self.collector_timeout_seconds
        deadline = time.monotonic() + timeout - min(2.0, timeout * 0.1)
        options = dict(kwargs)
        try:
            parameters = inspect.signature(self.collector.archive_xiaohongshu_detail).parameters
            accepts_options = any(item.kind == inspect.Parameter.VAR_KEYWORD for item in parameters.values())
            for key, value in (("deadline", deadline), ("cancel_event", cancel_event)):
                if accepts_options or key in parameters:
                    options[key] = value
        except (TypeError, ValueError):
            pass

        def collect() -> None:
            try:
                result.put(("ok", self.collector.archive_xiaohongshu_detail(url, **options)))
            except BaseException as exc:  # pass the original typed error back to the worker
                result.put(("error", exc))

        with self._lock:
            previous = getattr(self, "_collector_inflight", None)
            if previous is not None and previous.is_alive():
                raise PlatformBrowserError(
                    "上次超时的页面读取尚在退出，本次没有启动重复采集；请稍后重试", code="archive_timeout",
                )
            thread = threading.Thread(target=collect, daemon=True, name="xhs-archive-cdp-call")
            self._collector_inflight = thread
            thread.start()
        thread.join(timeout=timeout)
        if thread.is_alive():
            cancel_event.set()
            raise PlatformBrowserError(
                "读取小红书页面超时，已请求取消；后台退出前不会启动重复采集，请稍后重试",
                code="archive_timeout",
            )
        state, value = result.get_nowait()
        if state == "error":
            if isinstance(value, TimeoutError):
                raise PlatformBrowserError("小红书页面读取已超出时间预算", code="archive_timeout") from value
            raise value
        return value

    def _collect_with_transient_retry(self, job_id: str, url: str, **kwargs: Any) -> dict[str, Any]:
        """Retry brief CDP connection contention without losing a post.

        The radar and archive workers share one browser serially, but Chrome can
        still take a few seconds to release its DevTools connection after a
        search.  Retry only that transport failure; content, identity, login,
        verification and platform errors keep their original semantics.
        """
        for attempt in range(3):
            try:
                return self._collect_with_deadline(url, **kwargs)
            except PlatformBrowserError as exc:
                message = str(exc).lower()
                transient = exc.code == "archive_collect_failed" and any(marker in message for marker in (
                    "connect_over_cdp", "retrieving websocket url", "websocket", "connection refused",
                ))
                if not transient or attempt >= 2:
                    raise
                self._heartbeat(
                    job_id,
                    "RETRYING_BROWSER_CONNECTION",
                    f"浏览器连接繁忙，正在自动重试（{attempt + 2}/3）",
                )
                if self.transient_retry_delay_seconds:
                    time.sleep(self.transient_retry_delay_seconds)
        raise RuntimeError("unreachable")

    def _finish_error(
        self, job: dict[str, Any], code: str, message: str, *, diagnostics: Any = None,
    ) -> None:
        now = utc_now()
        progress = {"message": message}
        safe_diagnostics = _safe_failure_diagnostics(diagnostics)
        if safe_diagnostics:
            progress["diagnostics"] = safe_diagnostics
        self.db.execute(
            """UPDATE xhs_archive_jobs SET status='FAILED',stage='FAILED',progress_json=?,error_code=?,
            heartbeat_at=?,updated_at=? WHERE id=?""",
            (json_dumps(progress), code[:100], now, now, job["id"]),
        )
        status = {
            "POST_UNAVAILABLE": "unavailable",
            "IDENTITY_MISMATCH": "identity_mismatch",
            "LOGIN_REQUIRED": "needs_login",
            "VERIFICATION_REQUIRED": "needs_verification",
            "PLATFORM_LIMITED": "platform_limited",
        }.get(code.upper(), "failed")
        self._update_extraction_failure(job, status, code, message, now)

    def _update_extraction_failure(
        self,
        job: dict[str, Any],
        status: str,
        code: str,
        message: str,
        now: str,
    ) -> None:
        """End pending UI state without replacing a previously useful body/archive."""
        self.db.execute(
            """UPDATE xhs_post_extractions
            SET status=CASE
                  WHEN archive_status='COMPLETE'
                    OR (status IN ('success','partial') AND (
                      COALESCE(body_raw,'')<>'' OR COALESCE(body,'')<>'' OR COALESCE(merged_copy,'')<>''
                    )) THEN status
                  ELSE ?
                END,
                failure_stage=?,error_message=?,updated_at=?
            WHERE workspace_id=? AND id=?""",
            (
                status,
                str(code).lower()[:100],
                _redact_error(message)[:2000],
                now,
                str(job["workspace_id"]),
                str(job["extraction_id"]),
            ),
        )

    def latest_archive(self, workspace_id: str, extraction_id: str) -> dict[str, Any] | None:
        row = self.db.one(
            """SELECT v.* FROM xhs_archive_versions v
            JOIN xhs_post_extractions e ON e.workspace_id=v.workspace_id AND e.id=v.extraction_id
            WHERE v.workspace_id=? AND v.extraction_id=?
            ORDER BY (v.id=e.latest_archive_id) DESC,v.captured_at DESC LIMIT 1""",
            (workspace_id, extraction_id),
        )
        if not row:
            return None
        result = dict(row)
        result["body_evidence"] = json_loads(result.pop("body_evidence_json", None), {})
        result["body_rich"] = json_loads(result.pop("body_rich_json", None), [])
        assets = self.db.all(
            "SELECT * FROM xhs_post_assets WHERE workspace_id=? AND archive_id=? ORDER BY role,ordinal",
            (workspace_id, row["id"]),
        )
        for asset in assets:
            asset["evidence"] = json_loads(asset.pop("evidence_json", None), {})
            asset["download_url"] = (
                f"/api/topic-radar/xhs/archives/{row['id']}/assets/{asset['id']}?workspace_id={urllib.parse.quote(workspace_id)}"
                if asset.get("status") == "SAVED" else ""
            )
        result["assets"] = assets
        return result

    def resolve_asset(self, workspace_id: str, archive_id: str, asset_id: str) -> tuple[Path, str]:
        row = self.db.one(
            """SELECT a.relative_path,a.mime_detected,v.archive_path FROM xhs_post_assets a
            JOIN xhs_archive_versions v ON v.workspace_id=a.workspace_id AND v.id=a.archive_id
            WHERE a.workspace_id=? AND a.archive_id=? AND a.id=? AND a.status='SAVED'""",
            (workspace_id, archive_id, asset_id),
        )
        if not row or not row.get("relative_path"):
            raise FileNotFoundError("归档图片不存在")
        base = (self.repository.root / row["archive_path"]).resolve()
        target = (base / row["relative_path"]).resolve()
        if self.repository.root.resolve() not in target.parents or not target.is_file():
            raise FileNotFoundError("归档图片不存在")
        return target, str(row.get("mime_detected") or "application/octet-stream")


xhs_archive_service = XiaohongshuArchiveService()
