from __future__ import annotations

import inspect
import queue
import re
import threading
import time
import urllib.parse
from typing import Any

from .db import Database, database, json_dumps, json_loads, new_id, utc_now
from .platform_browser import PlatformBrowserCollector, PlatformBrowserError, _douyin_video_id, _public_post_url, platform_browser_collector
from .speech_transcription import transcription_quality


def _safe_error_message(value: str) -> str:
    text = re.sub(r"(?i)(authorization|cookie|token|signature|sign)=([^\s&]+)", r"\1=[REDACTED]", str(value))
    return re.sub(
        r"https?://[^\s]+",
        lambda match: urllib.parse.urlunsplit((*urllib.parse.urlsplit(match.group(0))[:3], "", "")),
        text,
    )[:500]


def _has_confirmed_visibility_restriction(record: dict[str, Any], video_id: str) -> bool:
    """Only the latest exact-ID, observed restriction stops automatic intake."""
    if record.get("status") != "POST_UNAVAILABLE" or record.get("video_id") != video_id:
        return False
    diagnostic = record.get("diagnostics")
    if not isinstance(diagnostic, dict):
        return False
    # Retained complete captions keep their original proof at the top level;
    # the most recent failure is nested separately. Never act on older proof.
    if "last_attempt_diagnostics" in diagnostic:
        if diagnostic.get("last_attempt_status") != "POST_UNAVAILABLE":
            return False
        diagnostic = diagnostic.get("last_attempt_diagnostics")
    if not isinstance(diagnostic, dict) or (
        diagnostic.get("error_code") != "post_unavailable"
        or diagnostic.get("target_video_id") != video_id
        or diagnostic.get("network_detail_payload_match_count") != 0
    ):
        return False
    outcomes = diagnostic.get("network_detail_outcomes")
    if not isinstance(outcomes, list):
        return False
    return any(
        isinstance(outcome, dict)
        and outcome.get("reason") == "platform_visibility_restricted"
        and outcome.get("filter_reason") == "status_audit_self_see"
        and outcome.get("filter_detail_video_id") == video_id
        and outcome.get("http_status") == 200
        and outcome.get("platform_status_code") == 0
        and not isinstance(outcome.get("platform_status_code"), bool)
        for outcome in outcomes
    )


class DouyinExtractionService:
    """Durable, workspace-scoped Douyin detail records.

    The worker is deliberately serial because it uses the same dedicated
    browser profile as Xiaohongshu.  A failed read only updates the failure
    fields of an earlier useful record; it never replaces verified metadata
    with an empty result. COMPLETE describes a verified full published caption,
    while video_status independently describes whether media was archived.
    """

    def __init__(self, db: Database = database, collector: PlatformBrowserCollector = platform_browser_collector) -> None:
        self.db = db
        self.collector = collector
        self._jobs: queue.Queue[tuple[str, str, str, str]] = queue.Queue()
        self._queued: set[tuple[str, str]] = set()
        self._queue_lock = threading.RLock()
        self._stop = threading.Event()
        self._resume_gate = threading.Event()
        self._resume_gate.set()
        self._thread: threading.Thread | None = None

    @staticmethod
    def canonical_video_url(value: str) -> str:
        url = _public_post_url("douyin", value)
        video_id = _douyin_video_id(url)
        if not video_id:
            return ""
        parsed = urllib.parse.urlsplit(url)
        # Douyin redirects some video shares to /note/<id>. Canonicalize both
        # routes to one stable workspace key so the same aweme cannot fork
        # into duplicate extraction records.
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, f"/video/{video_id}", "", ""))

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._resume_gate.set()
        # A desktop app may close while a local ASR subprocess is running.
        # Re-queue those rows instead of leaving a permanent spinner; no media
        # file survives its TemporaryDirectory across process shutdown.
        self.db.execute(
            """UPDATE douyin_post_extractions SET status='QUEUED',
            asr_status=CASE WHEN asr_status='REQUESTED' THEN 'REQUESTED' ELSE 'NOT_ATTEMPTED' END,
            asr_source=CASE WHEN asr_text<>'' THEN asr_source ELSE '' END,
            asr_message='上次本机转写未完成；工作台已将这条记录放回队列。',updated_at=?
            WHERE status='TRANSCRIBING' AND (asr_text='' OR asr_status='REQUESTED')""",
            (utc_now(),),
        )
        for row in self.db.all("SELECT workspace_id,url,search_keyword,title FROM douyin_post_extractions WHERE status='QUEUED'"):
            self._put_job(str(row["workspace_id"]), str(row["url"]), str(row.get("search_keyword") or ""), str(row.get("title") or ""))
        self._thread = threading.Thread(target=self._loop, daemon=True, name="douyin-extraction-worker")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._resume_gate.set()
        if self._thread:
            self._thread.join(timeout=2)

    def resume_pending(self, workspace_id: str) -> int:
        """Resume only queue items parked after a login/verification/platform wall."""
        rows = self.db.all(
            """SELECT url,search_keyword,title FROM douyin_post_extractions
            WHERE workspace_id=? AND failure_stage LIKE 'queue_paused:%' ORDER BY extracted_at""",
            (workspace_id,),
        )
        added = 0
        for row in rows:
            url = str(row.get("url") or "")
            if not url:
                continue
            self.db.execute(
                """UPDATE douyin_post_extractions SET status='QUEUED',failure_stage='',error_message='',updated_at=?
                WHERE workspace_id=? AND url=? AND failure_stage LIKE 'queue_paused:%'""",
                (utc_now(), workspace_id, url),
            )
            if self._put_job(workspace_id, url, str(row.get("search_keyword") or ""), str(row.get("title") or "")):
                added += 1
        self._resume_gate.set()
        return added

    def _pause_remaining_jobs(self, status: str, reason: str) -> None:
        """Park pending detail reads instead of hammering a logged-out/limited session."""
        self._resume_gate.clear()
        parked: list[tuple[str, str, str, str]] = []
        while True:
            try:
                job = self._jobs.get_nowait()
            except queue.Empty:
                break
            parked.append(job)
            self._jobs.task_done()
        now = utc_now()
        for workspace_id, url, _search_keyword, _title_hint in parked:
            self.db.execute(
                """UPDATE douyin_post_extractions SET status=?,failure_stage=?,error_message=?,updated_at=?
                WHERE workspace_id=? AND url=?""",
                (status, f"queue_paused:{status.lower()}", reason[:1000], now, workspace_id, url),
            )
            with self._queue_lock:
                self._queued.discard((workspace_id, url))

    def _put_job(self, workspace_id: str, url: str, search_keyword: str, title_hint: str) -> bool:
        key = (workspace_id, url)
        with self._queue_lock:
            if key in self._queued:
                return False
            self._queued.add(key)
            self._jobs.put((workspace_id, url, search_keyword, title_hint))
        return True

    def enqueue(
        self, workspace_id: str, url: str, *, search_keyword: str = "", title_hint: str = "",
        search_result_text: str = "", force_retry: bool = False, transcribe_media: bool = False,
    ) -> bool:
        canonical = self.canonical_video_url(url)
        if not canonical:
            return False
        # A deliberate transcript request is an explicit retry, independent
        # of the automatic caption retry budget.
        force_retry = force_retry or transcribe_media
        # Persist before publishing to the worker. The same lock also keeps
        # concurrent producers from resetting an already queued/running row.
        # A failed DB write leaves no ghost job or deduplication reservation.
        with self._queue_lock:
            if (workspace_id, canonical) in self._queued:
                if force_retry:
                    self._resume_gate.set()
                return False
            existing = self.get(workspace_id, canonical)
            # Ordinary repeated radar searches should not redo completed ASR.
            # An explicit post-verification batch resume is different: it must
            # re-read the original publication caption even when ASR already
            # exists, while preserving that separate transcript as evidence.
            if existing:
                if not force_retry and _has_confirmed_visibility_restriction(existing, _douyin_video_id(canonical)):
                    return False
                has_transcript = bool(existing.get("asr_text")) or existing.get("asr_status") in {
                    "BROWSER_MEDIA_TRANSCRIBED", "USER_MEDIA_TRANSCRIBED",
                }
                needs_review = (existing.get("asr_quality") or {}).get("status") in {"NEEDS_REVIEW", "INCOMPLETE"}
                if has_transcript and not (transcribe_media and needs_review) and (transcribe_media or not force_retry):
                    return False
                if (
                    existing.get("status") in {"PARTIAL", "COMPLETE"}
                    and int(existing.get("retry_count") or 0) >= 2
                    and not force_retry
                ):
                    return False
            now = utc_now()
            incoming_search_text = str(search_result_text or "").strip()[:4000]
            existing_search_text = str((existing or {}).get("search_result_text") or "")
            saved_search_text = incoming_search_text or existing_search_text or title_hint.strip()[:500]
            saved_search_source = (
                "搜索结果卡片摘要" if incoming_search_text else
                str((existing or {}).get("search_result_text_source") or "") if existing_search_text else
                "搜索候选标题提示（非正文）" if saved_search_text else ""
            )
            asr_requested = transcribe_media or (existing or {}).get("asr_status") == "REQUESTED"
            pending_asr_status = "REQUESTED" if asr_requested else "NOT_ATTEMPTED"
            if existing:
                self.db.execute(
                    """UPDATE douyin_post_extractions SET status='QUEUED',search_keyword=?,title=?,
                    search_result_text=CASE WHEN ?<>'' THEN ? ELSE search_result_text END,
                    search_result_text_source=CASE WHEN ?<>'' THEN ? ELSE search_result_text_source END,
                    failure_stage='',error_message='',
                    asr_status=CASE WHEN ?='REQUESTED' THEN 'REQUESTED' WHEN asr_text<>'' THEN asr_status ELSE 'NOT_ATTEMPTED' END,
                    asr_source=CASE WHEN asr_text<>'' THEN asr_source ELSE '' END,
                    asr_message=CASE WHEN asr_text<>'' THEN asr_message ELSE '' END,
                    updated_at=?
                    WHERE workspace_id=? AND url=?""",
                    (search_keyword[:180], title_hint[:500] or existing.get("title") or "",
                     saved_search_text, saved_search_text, saved_search_text, saved_search_source,
                     pending_asr_status, now, workspace_id, canonical),
                )
            else:
                self.db.execute(
                    """INSERT INTO douyin_post_extractions
                    (id,workspace_id,url,video_id,search_keyword,title,search_result_text,search_result_text_source,status,asr_status,extracted_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,'QUEUED',?,?,?)""",
                    (new_id("dy"), workspace_id, canonical, _douyin_video_id(canonical), search_keyword[:180],
                     title_hint[:500], saved_search_text, saved_search_source, pending_asr_status, now, now),
                )
            added = self._put_job(workspace_id, canonical, search_keyword, title_hint)
            if added and force_retry:
                # An explicit single-item retry must wake the worker. Rows
                # parked by _pause_remaining_jobs stay in SQLite and are not
                # bulk-resumed by this action.
                self._resume_gate.set()
            return added

    def enqueue_results(
        self,
        workspace_id: str,
        payload: dict[str, Any],
        limit: int = 500,
        *,
        force_retry: bool = False,
        **_: Any,
    ) -> int:
        candidates: list[tuple[str, str, str, str]] = []
        for item in payload.get("items") or []:
            if isinstance(item, dict) and item.get("platform") == "douyin":
                raw = item.get("raw") if isinstance(item.get("raw"), dict) else json_loads(item.get("raw_json"), {})
                candidates.append((
                    str(item.get("url") or ""), str(item.get("title") or ""),
                    str((raw or {}).get("search_query") or payload.get("topic") or ""),
                    str(item.get("excerpt") or ""),
                ))
        for source in payload.get("sources") or []:
            if not isinstance(source, dict):
                continue
            metadata = source.get("metadata") if isinstance(source.get("metadata"), dict) else {}
            if source.get("platform") == "douyin" or metadata.get("platform") == "douyin":
                candidates.append((
                    str(source.get("url") or ""), str(source.get("title") or ""),
                    str(payload.get("topic") or ""), str(source.get("excerpt") or ""),
                ))
        added = 0
        seen: set[str] = set()
        for url, title, query, excerpt in candidates:
            canonical = self.canonical_video_url(url)
            if not canonical or canonical in seen:
                continue
            seen.add(canonical)
            if self.enqueue(
                workspace_id, canonical, search_keyword=query, title_hint=title,
                search_result_text=excerpt, force_retry=force_retry,
            ):
                added += 1
            if added >= max(1, min(int(limit), 500)):
                break
        return added

    def enqueue_missing_transcripts(self, workspace_id: str, limit: int = 500) -> int:
        """Explicitly retry every unresolved transcript after user action.

        Automatic search retries stay capped in ``enqueue``. This explicit
        recovery action also includes rows parked by a login wall and rows
        whose earlier anonymous attempts exhausted that automatic cap.
        """
        rows = self.db.all(
            """SELECT * FROM douyin_post_extractions
            WHERE workspace_id=?
            AND status IN ('PARTIAL','COMPLETE','USER_ACTION_REQUIRED','NEEDS_BROWSER','NEEDS_ATTENTION','FAILED')
            ORDER BY extracted_at ASC""",
            (workspace_id,),
        )
        added = 0
        for row in rows:
            decoded = self.decode(row) or {}
            has_transcript = bool(row.get("asr_text")) or row.get("asr_status") in {
                "BROWSER_MEDIA_TRANSCRIBED", "USER_MEDIA_TRANSCRIBED",
            }
            if has_transcript and (decoded.get("asr_quality") or {}).get("status") not in {"NEEDS_REVIEW", "INCOMPLETE"}:
                continue
            if self.enqueue(
                workspace_id, str(row.get("url") or ""),
                search_keyword=str(row.get("search_keyword") or ""),
                title_hint=str(row.get("title") or ""),
                force_retry=True, transcribe_media=True,
            ):
                added += 1
            if added >= max(1, min(int(limit), 500)):
                break
        return added

    def _loop(self) -> None:
        while not self._stop.is_set():
            if not self._resume_gate.wait(timeout=0.5):
                continue
            try:
                workspace_id, url, search_keyword, title_hint = self._jobs.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                result = self.extract(workspace_id, url, search_keyword=search_keyword, title_hint=title_hint)
                if result.get("status") in {"USER_ACTION_REQUIRED", "NEEDS_ATTENTION", "NEEDS_BROWSER"}:
                    self._pause_remaining_jobs(str(result["status"]), str(result.get("error_message") or "平台要求处理后再继续"))
            except Exception as exc:
                # ``extract`` already persists expected platform failures; this
                # fallback only guards malformed worker inputs.
                self.db.execute(
                    """UPDATE douyin_post_extractions SET status='FAILED',failure_stage='worker_error',error_message=?,updated_at=?
                    WHERE workspace_id=? AND url=?""",
                    (f"自动提取失败：{_safe_error_message(str(exc))}", utc_now(), workspace_id, url),
                )
            finally:
                with self._queue_lock:
                    self._queued.discard((workspace_id, url))
                self._jobs.task_done()
                # Keep a serialized, modest cadence across a large JEV-A
                # result set; never fan out hundreds of detail-page requests.
                if not self._stop.is_set() and self._resume_gate.is_set():
                    time.sleep(1.2)

    @staticmethod
    def decode(row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        value = dict(row)
        for column, key, fallback in (
            ("metrics_json", "metrics", {}),
            ("asr_segments_json", "asr_segments", []),
            ("asr_confidence_json", "asr_confidence", {}),
            ("asr_low_confidence_json", "asr_low_confidence", []),
            ("diagnostics_json", "diagnostics", {}),
        ):
            value[key] = json_loads(value.pop(column, None), fallback)
        value["asr_quality"] = transcription_quality(
            str(value.get("asr_text") or ""), value.get("asr_segments"), value.get("asr_confidence"),
            text_truncated=bool((value.get("diagnostics") or {}).get("asr_text_truncated")),
        )
        return value

    def list(self, workspace_id: str, limit: int = 500) -> list[dict[str, Any]]:
        rows = self.db.all(
            "SELECT * FROM douyin_post_extractions WHERE workspace_id=? ORDER BY extracted_at DESC LIMIT ?",
            (workspace_id, max(1, min(int(limit), 500))),
        )
        return [self.decode(row) or {} for row in rows]

    def get(self, workspace_id: str, url: str) -> dict[str, Any] | None:
        return self.decode(self.db.one(
            "SELECT * FROM douyin_post_extractions WHERE workspace_id=? AND url=?", (workspace_id, url),
        ))

    def save_local_transcription(self, workspace_id: str, url: str, result: dict[str, Any]) -> dict[str, Any]:
        canonical = self.canonical_video_url(url)
        if not canonical:
            raise ValueError("不是可识别的抖音视频原帖链接")
        existing = self.get(workspace_id, canonical)
        if not existing:
            raise LookupError("这条视频尚未进入当前工作区的详情记录")
        video_id = str(existing.get("video_id") or "")
        observed_video_id = str(existing.get("observed_video_id") or "")
        if not video_id or video_id != observed_video_id:
            raise ValueError("视频 ID 尚未通过一致性校验，不能关联本机转写结果")
        if existing.get("status") not in {"PARTIAL", "COMPLETE"}:
            raise ValueError("请先成功读取并核验这条视频的详情")
        now = utc_now()
        self.db.execute(
            """UPDATE douyin_post_extractions SET asr_status=?,asr_text=?,asr_language=?,asr_engine=?,asr_model=?,
            asr_source=?,asr_message=?,asr_media_sha256=?,asr_segments_json=?,asr_confidence_json=?,
            asr_low_confidence_json=?,diagnostics_json=?,updated_at=? WHERE workspace_id=? AND url=?""",
            (
                "USER_MEDIA_TRANSCRIBED", str(result.get("text") or "")[:60000],
                str(result.get("language") or "zh-CN")[:30], str(result.get("engine") or "")[:120],
                str(result.get("model") or "")[:120], str(result.get("source") or "USER_PROVIDED_LOCAL_MEDIA")[:80],
                str(result.get("message") or "")[:1000], str(result.get("media_sha256") or "")[:64],
                json_dumps(result.get("segments") or []), json_dumps(result.get("confidence") or {}),
                json_dumps(result.get("low_confidence") or []),
                json_dumps({**(existing.get("diagnostics") or {}), "asr_text_truncated": bool(result.get("text_truncated") or len(str(result.get("text") or "")) > 60000)}),
                now, workspace_id, canonical,
            ),
        )
        return self.get(workspace_id, canonical) or {}

    def extract(self, workspace_id: str, url: str, *, search_keyword: str = "", title_hint: str = "") -> dict[str, Any]:
        canonical = self.canonical_video_url(url)
        if not canonical:
            raise ValueError("不是可识别的抖音视频原帖链接")
        existing = self.get(workspace_id, canonical)
        transcribe_media = bool(
            existing and existing.get("asr_status") == "REQUESTED"
        )
        now = utc_now()
        target_video_id = _douyin_video_id(canonical)
        existing_status = str((existing or {}).get("status") or "")
        previous_attempt_exists = bool(existing and (
            existing.get("last_attempt_at")
            or existing_status not in {"QUEUED", "TRANSCRIBING"}
        ))
        retry_count = int((existing or {}).get("retry_count") or 0) + int(previous_attempt_exists)
        if not existing:
            # Create the row before navigation so long on-device transcription
            # can expose a truthful in-progress state to the workbench.
            self.db.execute(
                """INSERT INTO douyin_post_extractions
                (id,workspace_id,url,video_id,search_keyword,title,search_result_text,search_result_text_source,status,extracted_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,'QUEUED',?,?)""",
                (new_id("dy"), workspace_id, canonical, target_video_id, search_keyword[:180], title_hint[:500],
                 title_hint[:500], "搜索候选标题提示（非正文）" if title_hint.strip() else "", now, now),
            )
        self.db.execute(
            """UPDATE douyin_post_extractions
            SET last_attempt_at=?,retry_count=?,updated_at=?
            WHERE workspace_id=? AND url=?""",
            (now, retry_count, now, workspace_id, canonical),
        )

        def mark_transcribing(progress: dict[str, Any]) -> None:
            self.db.execute(
                """UPDATE douyin_post_extractions SET status='TRANSCRIBING',
                asr_status=CASE WHEN asr_status='REQUESTED' THEN 'REQUESTED' ELSE 'PROCESSING' END,
                asr_source=CASE WHEN asr_text<>'' THEN asr_source ELSE ? END,
                asr_message=?,updated_at=? WHERE workspace_id=? AND url=?""",
                (
                    str(progress.get("asr_source") or "BROWSER_LOGGED_IN_MEDIA")[:80],
                    str(progress.get("asr_message") or "正在检查登录态媒体并进行本机转写。")[:1000],
                    utc_now(), workspace_id, canonical,
                ),
            )

        try:
            method = self.collector.extract_douyin_detail
            try:
                parameters = list(inspect.signature(method).parameters.values())
                supports_progress = any(
                    item.name == "progress_callback" or item.kind == inspect.Parameter.VAR_KEYWORD
                    for item in parameters
                )
                supports_transcription_request = any(
                    item.name == "transcribe_media" or item.kind == inspect.Parameter.VAR_KEYWORD
                    for item in parameters
                )
            except (TypeError, ValueError):
                supports_progress = False
                supports_transcription_request = False
            options: dict[str, Any] = {"search_keyword": search_keyword, "title_hint": title_hint}
            if supports_progress:
                options["progress_callback"] = mark_transcribing
            if transcribe_media and supports_transcription_request:
                options["transcribe_media"] = True
            result = method(canonical, **options)
        except PlatformBrowserError as exc:
            status_by_code = {
                "browser_not_running": "NEEDS_BROWSER", "browser_missing": "NEEDS_BROWSER",
                "browser_page_required": "NEEDS_BROWSER",
                "login_required": "USER_ACTION_REQUIRED", "verification_required": "USER_ACTION_REQUIRED",
                "platform_limited": "NEEDS_ATTENTION", "post_unavailable": "POST_UNAVAILABLE",
                "identity_mismatch": "IDENTITY_MISMATCH", "invalid_video_url": "INVALID_URL",
            }
            result = {
                "url": canonical, "video_id": target_video_id,
                "observed_video_id": str(getattr(exc, "diagnostics", {}).get("observed_video_id") or ""),
                "title": title_hint, "status": status_by_code.get(exc.code, "FAILED"),
                "video_status": "MEDIA_UNAVAILABLE", "failure_stage": exc.code,
                "error_message": _safe_error_message(str(exc)), "diagnostics": {"error_code": exc.code, **getattr(exc, "diagnostics", {})},
            }

        return self._save_result(
            workspace_id, canonical, result, search_keyword=search_keyword,
            title_hint=title_hint, retry_count=retry_count, now=utc_now(),
        )

    def _save_result(
        self,
        workspace_id: str,
        canonical: str,
        result: dict[str, Any],
        *,
        search_keyword: str,
        title_hint: str,
        retry_count: int,
        now: str,
    ) -> dict[str, Any]:
        existing = self.get(workspace_id, canonical)
        target_video_id = _douyin_video_id(canonical)
        status = str(result.get("status") or "FAILED").upper()
        search_result_text = str(
            result.get("search_result_text") or (existing or {}).get("search_result_text") or title_hint or ""
        )[:4000]
        search_result_text_source = str(
            result.get("search_result_text_source") or (existing or {}).get("search_result_text_source")
            or ("搜索候选标题提示（非正文）" if search_result_text and title_hint else "")
        )[:120]
        values = {
            "id": str((existing or {}).get("id") or new_id("dy")), "workspace_id": workspace_id,
            "url": canonical, "video_id": str(result.get("video_id") or target_video_id),
            "observed_video_id": str(result.get("observed_video_id") or ""), "search_keyword": search_keyword[:180],
            "title": str(result.get("title") or title_hint)[:500], "author": str(result.get("author") or "")[:300],
            "published_at": result.get("published_at") or None, "metrics_json": json_dumps(result.get("metrics") or {}),
            "search_result_text": search_result_text,
            "search_result_text_source": search_result_text_source,
            "published_body": str(result.get("published_body") or "")[:30000],
            "published_body_source": str(result.get("published_body_source") or "")[:120],
            "platform_subtitle": str(result.get("platform_subtitle") or "")[:30000],
            "subtitle_source": str(result.get("subtitle_source") or "")[:120],
            "asr_status": str(result.get("asr_status") or "NOT_ATTEMPTED")[:60], "asr_text": str(result.get("asr_text") or "")[:60000],
            "asr_segments_json": json_dumps(result.get("asr_segments") or []), "asr_language": str(result.get("asr_language") or "")[:30],
            "asr_engine": str(result.get("asr_engine") or "")[:120], "asr_model": str(result.get("asr_model") or "")[:120],
            "asr_source": str(result.get("asr_source") or "")[:80], "asr_message": str(result.get("asr_message") or "")[:1000],
            "asr_media_sha256": str(result.get("asr_media_sha256") or "")[:64],
            "asr_confidence_json": json_dumps(result.get("asr_confidence") or {}), "asr_low_confidence_json": json_dumps(result.get("asr_low_confidence") or []),
            "ocr_status": str(result.get("ocr_status") or "NOT_ATTEMPTED")[:60], "ocr_text": str(result.get("ocr_text") or "")[:30000],
            "video_status": str(result.get("video_status") or "MEDIA_UNAVAILABLE")[:80], "status": status,
            "failure_stage": str(result.get("failure_stage") or "")[:120], "error_message": str(result.get("error_message") or "")[:2000],
            "diagnostics_json": json_dumps(result.get("diagnostics") or {}), "retry_count": retry_count,
            "last_attempt_at": str((existing or {}).get("last_attempt_at") or now),
            "extracted_at": now, "updated_at": now,
        }
        # A later page may expose only a metadata excerpt or subtitles. Keep
        # the previously verified caption even after enqueue changed its row
        # status to QUEUED; acquisition evidence, not that transient status,
        # establishes what we already have. Record the partial refresh
        # separately so retaining old full text cannot imply fresh success.
        previous_diagnostics = (existing or {}).get("diagnostics") or {}
        has_verified_caption = bool(
            existing
            and existing.get("published_body")
            and existing.get("published_body_source") == "详情页结构化发布文案"
            and existing.get("video_id") == target_video_id
            and existing.get("observed_video_id") == target_video_id
            and (
                existing.get("status") == "COMPLETE"
                or previous_diagnostics.get("copy_completeness") == "verified_structured_caption"
            )
        )
        if status != "COMPLETE" and has_verified_caption:
            # Keep this proof through failed refreshes too; otherwise a
            # COMPLETE -> login failure -> PARTIAL sequence could still erase
            # the original text after failure diagnostics replaced the proof.
            values["diagnostics_json"] = json_dumps({
                **previous_diagnostics,
                "copy_completeness": "verified_structured_caption",
                "last_attempt_status": status,
                "last_attempt_failure_stage": values["failure_stage"],
                "last_attempt_diagnostics": result.get("diagnostics") or {},
                "retained_caption_extracted_at": (
                    previous_diagnostics.get("retained_caption_extracted_at") or existing["extracted_at"]
                ),
            })
        if status == "PARTIAL" and has_verified_caption:
            for column in ("published_body", "published_body_source", "video_id", "observed_video_id", "extracted_at"):
                values[column] = existing[column]
            values["status"] = "COMPLETE"
            values["failure_stage"] = "caption_refresh_partial_preserved"
            values["error_message"] = "本次详情仅返回部分内容；已保留此前核验的完整发布文案，本次未重新核验全文。"
        # A refresh that cannot re-read media must not erase a transcript that
        # the user already supplied or that an earlier logged-in pass produced.
        if existing and not values["asr_text"] and existing.get("asr_text"):
            for column in (
                "asr_text", "asr_language", "asr_engine", "asr_model", "asr_source", "asr_media_sha256",
            ):
                values[column] = existing.get(column) or values[column]
            if existing.get("asr_status") != "REQUESTED":
                values["asr_status"] = existing.get("asr_status") or values["asr_status"]
                values["asr_message"] = existing.get("asr_message") or values["asr_message"]
            else:
                values["asr_message"] += " 已保留上次识别文本，质量标记仍需核对。"
            for column, key, default in (
                ("asr_segments_json", "asr_segments", []), ("asr_confidence_json", "asr_confidence", {}),
                ("asr_low_confidence_json", "asr_low_confidence", []),
            ):
                values[column] = json_dumps(existing.get(key) or default)
            values["video_status"] = existing.get("video_status") or values["video_status"]
        diagnostics = json_loads(values["diagnostics_json"], {})
        diagnostics["asr_text_truncated"] = (
            bool(result.get("asr_text_truncated") or len(str(result.get("asr_text") or "")) > 60000)
            if result.get("asr_text") else bool(previous_diagnostics.get("asr_text_truncated"))
        )
        values["diagnostics_json"] = json_dumps(diagnostics)
        # Never overwrite the useful content of a prior successful/partial
        # extraction with a later browser/login failure.
        if existing and status not in {"PARTIAL", "COMPLETE"}:
            self.db.execute(
                """UPDATE douyin_post_extractions SET status=?,failure_stage=?,error_message=?,diagnostics_json=?,
                search_result_text=CASE WHEN search_result_text='' THEN ? ELSE search_result_text END,
                search_result_text_source=CASE WHEN search_result_text_source='' THEN ? ELSE search_result_text_source END,
                retry_count=?,last_attempt_at=?,updated_at=?
                WHERE workspace_id=? AND url=?""",
                (status, values["failure_stage"], values["error_message"], values["diagnostics_json"],
                 values["search_result_text"], values["search_result_text_source"], retry_count,
                 values["last_attempt_at"], now, workspace_id, canonical),
            )
            return self.get(workspace_id, canonical) or {}
        columns = list(values)
        placeholders = ",".join("?" for _ in columns)
        updates = ",".join(f"{key}=excluded.{key}" for key in columns if key not in {"id", "workspace_id"})
        self.db.execute(
            f"INSERT INTO douyin_post_extractions ({','.join(columns)}) VALUES ({placeholders}) "
            f"ON CONFLICT(workspace_id,url) DO UPDATE SET {updates}", tuple(values[key] for key in columns),
        )
        return self.get(workspace_id, canonical) or {}


douyin_extraction_service = DouyinExtractionService()
