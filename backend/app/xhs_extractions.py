from __future__ import annotations

import logging
import queue
import re
import threading
import urllib.parse
from typing import Any

from .db import Database, database, json_dumps, json_loads, new_id, utc_now
from .platform_browser import PlatformBrowserCollector, PlatformBrowserError, _safe_url, platform_browser_collector


JSON_COLUMNS = {
    "tags_json": ("tags", []),
    "metrics_json": ("metrics", {}),
    "field_sources_json": ("field_sources", {}),
    "diagnostics_json": ("diagnostics", {}),
    "body_evidence_json": ("body_evidence", {}),
    "body_rich_json": ("body_rich", []),
}


def _safe_error_message(value: str) -> str:
    text = re.sub(r"(?i)(authorization|cookie|token|xsec_token|signature|sign)=([^\s&]+)", r"\1=[REDACTED]", value)
    return re.sub(
        r"https?://[^\s]+",
        lambda match: urllib.parse.urlunsplit((*urllib.parse.urlsplit(match.group(0))[:3], "", "")),
        text,
    )[:500]


class XiaohongshuExtractionService:
    """Persist the result of reading one visible Xiaohongshu post detail page."""

    def __init__(
        self,
        db: Database = database,
        collector: PlatformBrowserCollector = platform_browser_collector,
    ) -> None:
        self.db = db
        self.collector = collector
        self._jobs: queue.Queue[tuple[str, str, str, str]] = queue.Queue()
        self._queued: set[tuple[str, str]] = set()
        self._queue_lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        # Resume jobs left behind if the workbench was closed during extraction.
        for row in self.db.all(
            "SELECT workspace_id,url,search_keyword,title FROM xhs_post_extractions WHERE status IN ('queued','extracting')"
        ):
            self._put_job(str(row["workspace_id"]), str(row["url"]), str(row.get("search_keyword") or ""), str(row.get("title") or ""))
        self._thread = threading.Thread(target=self._loop, daemon=True, name="xhs-extraction-worker")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    @staticmethod
    def _public_url(value: str) -> str:
        safe = _safe_url("xiaohongshu", value)
        if not safe:
            return ""
        parsed = urllib.parse.urlsplit(safe)
        if not re.search(r"/(?:explore|discovery/item)/[A-Za-z0-9]+", parsed.path):
            return ""
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))

    @classmethod
    def canonical_post_url(cls, value: str) -> str:
        """Return the stable, credential-free form of a supported XHS post URL."""
        return cls._public_url(value)

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
    ) -> bool:
        """Queue a post once and expose its progress to the UI immediately."""
        public_url = self._public_url(url)
        if not public_url:
            return False
        existing = self.get(workspace_id, public_url)
        if existing and existing.get("archive_status") == "COMPLETE":
            return False
        if not self._put_job(workspace_id, public_url, search_keyword.strip()[:180], title_hint.strip()[:500]):
            return False
        now = utc_now()
        if existing:
            self.db.execute(
                """UPDATE xhs_post_extractions
                SET search_keyword=?,title=?,status='queued',failure_stage='',error_message='',updated_at=?
                WHERE workspace_id=? AND url=?""",
                (search_keyword.strip()[:180], title_hint.strip()[:500] or existing.get("title") or "", now, workspace_id, public_url),
            )
        else:
            self.db.execute(
                """INSERT INTO xhs_post_extractions
                (id,workspace_id,url,search_keyword,title,status,extracted_at,updated_at)
                VALUES(?,?,?,?,?,'queued',?,?)""",
                (new_id("xhs"), workspace_id, public_url, search_keyword.strip()[:180], title_hint.strip()[:500], now, now),
            )
        return True

    def ensure_archive_record(
        self, workspace_id: str, url: str, *, search_keyword: str = "", title_hint: str = "",
    ) -> dict[str, Any]:
        """Create the minimal scoped record needed by the strict archive queue.

        This deliberately does not start the legacy OCR/ASR extractor. The archive
        worker reads and verifies the exact post directly in the authorized browser.
        """
        public_url = self._public_url(url)
        if not public_url:
            raise ValueError("不是可识别的小红书原帖链接")
        existing = self.get(workspace_id, public_url)
        if existing:
            return existing
        now = utc_now()
        extraction_id = new_id("xhs")
        self.db.execute(
            """INSERT OR IGNORE INTO xhs_post_extractions
            (id,workspace_id,url,search_keyword,title,status,extracted_at,updated_at)
            VALUES(?,?,?,?,?,'ready_to_archive',?,?)""",
            (
                extraction_id, workspace_id, public_url, search_keyword.strip()[:180],
                title_hint.strip()[:500], now, now,
            ),
        )
        # Another radar/API producer can create this same URL between the read
        # and insert. Resolve the shared URL after the conflict-safe insert.
        return self.get(workspace_id, public_url) or {}

    def enqueue_results(
        self,
        workspace_id: str,
        payload: dict[str, Any],
        limit: int = 30,
        *,
        archive_enqueue: Any | None = None,
        resume_after_user_action: bool = False,
    ) -> int:
        """Queue actual new strict archives; skipped candidates never consume the budget."""
        summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else json_loads(payload.get("summary_json"), {})
        candidates: list[tuple[str, str, str]] = []
        for recommendation in (summary or {}).get("recommendations", []):
            if not isinstance(recommendation, dict) or recommendation.get("evidence_platform") != "xiaohongshu":
                continue
            titles = recommendation.get("source_titles") or []
            candidates.append((
                str(recommendation.get("evidence_url") or ""),
                str(titles[0] if titles else recommendation.get("title") or ""),
                str(recommendation.get("search_query") or (summary or {}).get("query") or ""),
            ))
        for source in payload.get("sources") or []:
            if not isinstance(source, dict):
                continue
            metadata = source.get("metadata") if isinstance(source.get("metadata"), dict) else {}
            if metadata.get("platform") == "xiaohongshu" or source.get("platform") == "xiaohongshu":
                candidates.append((str(source.get("url") or ""), str(source.get("title") or ""), str(payload.get("topic") or "")))
        for item in payload.get("items") or []:
            if not isinstance(item, dict) or item.get("platform") != "xiaohongshu":
                continue
            raw = item.get("raw") if isinstance(item.get("raw"), dict) else json_loads(item.get("raw_json"), {})
            candidates.append((str(item.get("url") or ""), str(item.get("title") or ""), str((raw or {}).get("search_query") or (summary or {}).get("query") or "")))
        added, seen = 0, set()
        # Every Xiaohongshu card surfaced by one radar refresh must receive a
        # strict archive job.  The archive worker remains serial, so widening
        # this intake does not run multiple browser collectors concurrently.
        target = max(1, min(int(limit), 500))
        for url, title, query_text in candidates:
            public_url = self._public_url(url)
            if not public_url or public_url in seen:
                continue
            seen.add(public_url)
            try:
                record = self.ensure_archive_record(
                    workspace_id, public_url, search_keyword=query_text, title_hint=title,
                )
                if record.get("archive_status") == "COMPLETE":
                    continue
                active = self.db.one(
                    """SELECT id,status FROM xhs_archive_jobs WHERE workspace_id=? AND extraction_id=?
                    AND status IN ('QUEUED','RUNNING','PAUSED') ORDER BY created_at DESC LIMIT 1""",
                    (workspace_id, record["id"]),
                )
                if active and not (resume_after_user_action and active.get("status") == "PAUSED"):
                    continue
                if archive_enqueue is None:
                    from .xhs_archive import xhs_archive_service
                    archive_enqueue = xhs_archive_service.enqueue
                if active:
                    self.db.execute(
                        """UPDATE xhs_post_extractions SET status='ready_to_archive',failure_stage='',
                        error_message='',updated_at=? WHERE workspace_id=? AND id=?""",
                        (utc_now(), workspace_id, record["id"]),
                    )
                if resume_after_user_action:
                    job = archive_enqueue(
                        workspace_id, str(record["id"]), resume_after_user_action=True,
                    )
                else:
                    job = archive_enqueue(workspace_id, str(record["id"]))
                if not isinstance(job, dict):
                    raise RuntimeError("归档队列未返回任务状态")
                queued_new = job.get("queued_new", job.get("status") in {"QUEUED", "RUNNING"})
                if not queued_new:
                    continue
            except Exception as exc:
                self._record_archive_enqueue_failure(workspace_id, public_url, title, query_text, exc)
                continue
            added += 1
            if added >= target:
                break
        return added

    def _record_archive_enqueue_failure(
        self, workspace_id: str, url: str, title: str, query_text: str, exc: Exception,
    ) -> None:
        """Retain a retryable per-post diagnostic without copying exception secrets."""
        now = utc_now()
        message = f"该帖子暂未加入正文队列（{type(exc).__name__}）；可单独重试。"
        try:
            self.db.execute(
                """INSERT OR IGNORE INTO xhs_post_extractions
                (id,workspace_id,url,search_keyword,title,status,extracted_at,updated_at)
                VALUES(?,?,?,?,?,'failed',?,?)""",
                (new_id("xhs"), workspace_id, url, query_text[:180], title[:500], now, now),
            )
            self.db.execute(
                """UPDATE xhs_post_extractions SET
                status=CASE WHEN verification_status='VERIFIED' OR archive_status='COMPLETE' THEN status ELSE 'failed' END,
                failure_stage='archive_enqueue_failed',error_message=?,updated_at=?
                WHERE workspace_id=? AND url=?""",
                (message, now, workspace_id, url),
            )
        except Exception as diagnostic_error:
            # A broken database cannot persist its own failure; keep the rest
            # of the intake isolated and log only the safe exception type.
            logging.getLogger(__name__).warning(
                "XHS queue diagnostic persistence failed: %s", type(diagnostic_error).__name__,
            )

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                workspace_id, url, search_keyword, title_hint = self._jobs.get(timeout=0.5)
            except queue.Empty:
                continue
            key = (workspace_id, url)
            try:
                self.db.execute(
                    "UPDATE xhs_post_extractions SET status='extracting',updated_at=? WHERE workspace_id=? AND url=?",
                    (utc_now(), workspace_id, url),
                )
                self.extract(
                    workspace_id, url, search_keyword=search_keyword, title_hint=title_hint, force=True,
                )
            except Exception as exc:
                self.db.execute(
                    """UPDATE xhs_post_extractions
                    SET status='failed',failure_stage='worker_error',error_message=?,updated_at=?
                    WHERE workspace_id=? AND url=?""",
                    (f"自动提取失败：{_safe_error_message(str(exc))}", utc_now(), workspace_id, url),
                )
            finally:
                with self._queue_lock:
                    self._queued.discard(key)
                self._jobs.task_done()

    @staticmethod
    def decode(row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        value = dict(row)
        for column, (public_name, fallback) in JSON_COLUMNS.items():
            value[public_name] = json_loads(value.pop(column, None), fallback)
        screenshot = value.pop("screenshot_path", "")
        value["screenshot_url"] = f"/api/topic-radar/xhs/captures/{screenshot}" if screenshot else ""
        return value

    def list(self, workspace_id: str, limit: int = 500) -> list[dict[str, Any]]:
        rows = self.db.all(
            "SELECT * FROM xhs_post_extractions WHERE workspace_id=? ORDER BY extracted_at DESC LIMIT ?",
            (workspace_id, max(1, min(limit, 500))),
        )
        return [self._with_archive(self.decode(row) or {}) for row in rows]

    def get(self, workspace_id: str, url: str) -> dict[str, Any] | None:
        decoded = self.decode(self.db.one(
            "SELECT * FROM xhs_post_extractions WHERE workspace_id=? AND url=?",
            (workspace_id, url),
        ))
        return self._with_archive(decoded) if decoded else None

    def get_by_id(self, workspace_id: str, extraction_id: str) -> dict[str, Any] | None:
        decoded = self.decode(self.db.one(
            "SELECT * FROM xhs_post_extractions WHERE workspace_id=? AND id=?",
            (workspace_id, extraction_id),
        ))
        return self._with_archive(decoded) if decoded else None

    def _with_archive(self, value: dict[str, Any]) -> dict[str, Any]:
        workspace_id, extraction_id = str(value["workspace_id"]), str(value["id"])
        archive_id = str(value.get("latest_archive_id") or "")
        archive = (
            self.db.one(
                "SELECT * FROM xhs_archive_versions WHERE workspace_id=? AND extraction_id=? AND id=?",
                (workspace_id, extraction_id, archive_id),
            )
            if archive_id
            else self.db.one(
                """SELECT * FROM xhs_archive_versions WHERE workspace_id=? AND extraction_id=?
                ORDER BY captured_at DESC LIMIT 1""",
                (workspace_id, extraction_id),
            )
        )
        if archive:
            archive["body_evidence"] = json_loads(archive.pop("body_evidence_json", None), {})
            archive["body_rich"] = json_loads(archive.pop("body_rich_json", None), [])
            body_verified = bool(
                archive.get("body_raw") is not None
                and str(archive.get("body_raw") or "").strip()
                and archive.get("verification_status") == "VERIFIED"
                and archive.get("post_id") == archive.get("observed_post_id")
            )
            stored_body_status = str(archive["body_evidence"].get("body_extraction_status") or "")
            structure_changed = bool(
                stored_body_status == "PAGE_STRUCTURE_CHANGED"
                or (
                    not body_verified
                    and archive.get("body_source") == "expanded_dom"
                    and str(archive.get("body_raw") or "").strip()
                )
            )
            has_unverified_text = bool(str(archive.get("body_raw") or "").strip())
            archive["body_extraction_status"] = (
                "VERIFIED" if body_verified
                else "PAGE_STRUCTURE_CHANGED" if structure_changed
                else "UNVERIFIED" if has_unverified_text
                else "MISSING"
            )
            archive["body_extraction_message"] = (
                "同一 post_id 的结构化正文已验证"
                if body_verified
                else "仅发现页面可见 DOM 文本，当前解析器无法从受支持的结构化正文来源核验；可能是平台页面结构变化。该文本不会作为原帖全文展示。"
                if structure_changed
                else "读取到文本，但来源或帖子身份尚未通过原文校验"
                if has_unverified_text
                else "严格归档未读到可验证的正文"
            )
            value["body_raw"] = archive.get("body_raw") if body_verified else None
            value["body_normalized"] = archive.get("body_normalized") if body_verified else None
            assets = self.db.all(
                "SELECT * FROM xhs_post_assets WHERE workspace_id=? AND archive_id=? ORDER BY role,ordinal",
                (workspace_id, archive["id"]),
            )
            for asset in assets:
                asset["evidence"] = json_loads(asset.pop("evidence_json", None), {})
                asset["download_url"] = (
                    f"/api/topic-radar/xhs/archives/{archive['id']}/assets/{asset['id']}?workspace_id={urllib.parse.quote(workspace_id)}"
                    if asset.get("status") == "SAVED" else ""
                )
            archive["assets"] = assets
            value["body_extraction_status"] = archive["body_extraction_status"]
        else:
            value["body_raw"] = None
            value["body_normalized"] = None
            legacy_body_present = any(
                str(value.get(field) or "").strip()
                for field in ("body", "merged_copy")
            )
            value["body_extraction_status"] = "UNVERIFIED" if legacy_body_present else "PENDING"
        value["archive"] = archive
        job = self.db.one(
            """SELECT * FROM xhs_archive_jobs WHERE workspace_id=? AND extraction_id=?
            ORDER BY created_at DESC LIMIT 1""",
            (workspace_id, extraction_id),
        )
        if job:
            job["progress"] = json_loads(job.pop("progress_json", None), {})
        value["archive_job"] = job
        return value

    def extract(
        self,
        workspace_id: str,
        url: str,
        *,
        search_keyword: str = "",
        title_hint: str = "",
        force: bool = False,
        archive_enqueue: Any | None = None,
    ) -> dict[str, Any]:
        """Use strict, ID-bound archiving for every public extraction request.

        The older DOM/meta path treated search snippets and cleaned visible text
        as a successful post body.  Keep any historical evidence in place, but
        never produce a new body through that path.  The archive worker is the
        sole source of ``body_raw`` and independently tracks body verification
        from image/rich-media completeness.
        """
        record = self.ensure_archive_record(
            workspace_id,
            url,
            search_keyword=search_keyword,
            title_hint=title_hint,
        )
        if record.get("archive_status") == "COMPLETE" and not force:
            return record

        now = utc_now()
        self.db.execute(
            """UPDATE xhs_post_extractions SET search_keyword=?,
            title=CASE WHEN ?<>'' THEN ? ELSE title END,
            status='ready_to_archive',failure_stage='',error_message='',updated_at=?
            WHERE workspace_id=? AND id=? AND archive_status<>'COMPLETE'""",
            (
                search_keyword.strip()[:180], title_hint.strip()[:500], title_hint.strip()[:500],
                now, workspace_id, str(record["id"]),
            ),
        )
        if archive_enqueue is None:
            from .xhs_archive import xhs_archive_service
            archive_enqueue = xhs_archive_service.enqueue
        job = archive_enqueue(workspace_id, str(record["id"]))
        result = self.get_by_id(workspace_id, str(record["id"])) or record
        # A queued result must be visible immediately even if a lightweight test
        # queue or external adapter does not persist job state into this DB.
        result["archive_job"] = job
        result["body_extraction_status"] = (
            result.get("archive", {}).get("body_extraction_status")
            if isinstance(result.get("archive"), dict)
            else result.get("body_extraction_status") or "PENDING"
        )
        # Older records may still contain cleaned DOM/meta text. Preserve that
        # material in the history listing, but do not return it from this
        # extraction entry as if it were the post's verified original copy.
        result["body"] = ""
        result["merged_copy"] = ""
        return result


xhs_extraction_service = XiaohongshuExtractionService()
