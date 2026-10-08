"""Read-only, content-free acceptance summary for one radar candidate batch.

No application imports, browser access, API calls, database migrations or file
writes. Optional integrity checks read only UTF-8 body files, never images.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any
import urllib.parse


def _object(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _identity(platform: str, url: str) -> tuple[str, str]:
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return "", ""
    domain = {"xiaohongshu": "xiaohongshu.com", "douyin": "douyin.com"}.get(platform, "")
    host = parsed.hostname or ""
    if not domain or parsed.scheme != "https" or not (host == domain or host.endswith("." + domain)):
        return "", ""
    pattern = r"/(?:explore|discovery/item|search_result)/([A-Za-z0-9]+)(?:/|$)" if platform == "xiaohongshu" else r"/(?:video|note)/(\d+)(?:/|$)"
    match = re.search(pattern, parsed.path)
    if not match:
        return "", ""
    post_id = match.group(1)
    path = f"/explore/{post_id}" if platform == "xiaohongshu" else f"/video/{post_id}"
    return post_id, urllib.parse.urlunsplit(("https", parsed.netloc, path, "", ""))


def _one(connection: sqlite3.Connection, sql: str, args: tuple[Any, ...]) -> dict[str, Any] | None:
    row = connection.execute(sql, args).fetchone()
    return dict(row) if row else None


def audit_batch(
    db_path: Path, batch_id: str, *, workspace_id: str | None = None,
    verify_body_hashes: bool = False, verify_files: bool = False, archive_root: Path | None = None,
) -> dict[str, Any]:
    db_path = Path(db_path).resolve()
    root = Path(archive_root or db_path.parent / "xhs_archives").resolve()
    check_hashes = verify_body_hashes or verify_files
    con = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA query_only=ON")
        con.execute("BEGIN")
        batch = _one(con, "SELECT workspace_id,status,target_count FROM radar_search_batches WHERE id=?", (batch_id,))
        if not batch or (workspace_id is not None and batch["workspace_id"] != workspace_id):
            raise LookupError("Batch not found in the requested workspace")
        owner = batch["workspace_id"]
        candidates = con.execute(
            "SELECT platform,url,verification_status FROM radar_batch_candidates WHERE batch_id=? AND workspace_id=?",
            (batch_id, owner),
        ).fetchall()
        totals = {platform: Counter({
            "candidates": 0, "verified_body_complete": 0, "unverified": 0, "queued": 0,
            "unavailable": 0, "missing_record": 0, "paused": 0, "identity_mismatch": 0,
            "integrity_failed": 0,
        }) for platform in ("xiaohongshu", "douyin")}
        totals["xiaohongshu"].update({"body_complete_media_complete": 0, "body_complete_media_partial": 0})
        integrity = Counter({"hashes_checked": 0, "files_checked": 0, "hash_failures": 0, "file_failures": 0})
        for candidate in candidates:
            platform = candidate["platform"]
            if platform not in totals:
                continue
            counts = totals[platform]
            counts["candidates"] += 1
            target_id, canonical = _identity(platform, candidate["url"])
            if not target_id or candidate["verification_status"] != "LIVE_VERIFIED":
                counts["unverified"] += 1
                continue
            table = "xhs_post_extractions" if platform == "xiaohongshu" else "douyin_post_extractions"
            key = "post_key" if platform == "xiaohongshu" else "video_id"
            record = _one(con, f"SELECT * FROM {table} WHERE workspace_id=? AND url=?", (owner, canonical))
            if not record:
                record = _one(con, f"SELECT * FROM {table} WHERE workspace_id=? AND {key}=? ORDER BY updated_at DESC LIMIT 1", (owner, target_id))
            if not record:
                counts["missing_record"] += 1
                counts["unverified"] += 1
                continue
            job: dict[str, Any] = {}
            version: dict[str, Any] | None = None
            if platform == "xiaohongshu":
                job = _one(con, "SELECT status,error_code FROM xhs_archive_jobs WHERE workspace_id=? AND extraction_id=? ORDER BY revision DESC,created_at DESC LIMIT 1", (owner, record["id"])) or {}
                if record.get("latest_archive_id"):
                    version = _one(con, "SELECT * FROM xhs_archive_versions WHERE workspace_id=? AND extraction_id=? AND id=?", (owner, record["id"], record["latest_archive_id"]))
                else:
                    version = _one(con, "SELECT * FROM xhs_archive_versions WHERE workspace_id=? AND extraction_id=? ORDER BY captured_at DESC,id DESC LIMIT 1", (owner, record["id"]))
                proof = _object((version or {}).get("body_evidence_json"))
                body = (version or {}).get("body_raw")
                valid = bool(
                    version and version["post_id"] == target_id == version["observed_post_id"]
                    and version["verification_status"] == "VERIFIED" and version["body_source"] == "embedded_state"
                    and proof.get("selected_body_field") in {"desc", "noteDesc"}
                    and isinstance(body, str) and body.strip()
                )
                if valid and check_hashes:
                    integrity["hashes_checked"] += 1
                    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
                    if digest != version.get("body_sha256") or digest != record.get("body_sha256") or body != record.get("body_raw"):
                        valid = False
                        integrity["hash_failures"] += 1
                        counts["integrity_failed"] += 1
                if valid and verify_files:
                    integrity["files_checked"] += 1
                    body_file = (root / str(version["archive_path"]) / "body_raw.txt").resolve()
                    try:
                        file_ok = body_file.is_relative_to(root) and body_file.read_bytes() == body.encode("utf-8")
                    except OSError:
                        file_ok = False
                    if not file_ok:
                        valid = False
                        integrity["file_failures"] += 1
                        counts["integrity_failed"] += 1
            else:
                proof = _object(record.get("diagnostics_json"))
                valid = bool(
                    record.get("video_id") == target_id == record.get("observed_video_id")
                    and str(record.get("published_body") or "").strip()
                    and record.get("published_body_source") == "详情页结构化发布文案"
                    and (record.get("status") == "COMPLETE" or proof.get("copy_completeness") == "verified_structured_caption")
                )
            if valid:
                counts["verified_body_complete"] += 1
                if platform == "xiaohongshu":
                    counts["body_complete_media_complete" if version["archive_status"] == "COMPLETE" else "body_complete_media_partial"] += 1
                continue
            state = str(record.get("status") or "").upper()
            job_state = str(job.get("status") or "").upper()
            failure = str(job.get("error_code") or record.get("failure_stage") or "").upper()
            if job_state in {"QUEUED", "RUNNING"} or state in {"QUEUED", "EXTRACTING", "TRANSCRIBING"}:
                counts["queued"] += 1
            elif state == "UNAVAILABLE" or failure == "POST_UNAVAILABLE":
                counts["unavailable"] += 1
            else:
                counts["unverified"] += 1
                if job_state == "PAUSED" or state in {"NEEDS_LOGIN", "NEEDS_VERIFICATION", "NEEDS_BROWSER", "NEEDS_ATTENTION", "USER_ACTION_REQUIRED", "PLATFORM_LIMITED"}:
                    counts["paused"] += 1
                if state == "IDENTITY_MISMATCH" or failure == "IDENTITY_MISMATCH":
                    counts["identity_mismatch"] += 1
        verified = sum(value["verified_body_complete"] for value in totals.values())
        count = len(candidates)
        return {
            "batch_id": batch_id,
            "snapshot_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "search_status": batch["status"], "target_count": batch["target_count"],
            "candidate_count": count, "missing_search_candidates": max(0, int(batch["target_count"]) - count),
            "verified_body_complete": verified,
            "candidate_body_coverage_percent": round(100 * verified / count, 2) if count else 0.0,
            "platforms": {key: dict(value) for key, value in totals.items()},
            "integrity": {"body_hash_check_enabled": check_hashes, "body_file_check_enabled": verify_files, **dict(integrity)},
        }
    finally:
        con.close()


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=root / ".workbench" / "copy_workbench.sqlite3")
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--workspace-id")
    parser.add_argument("--verify-body-hashes", action="store_true")
    parser.add_argument("--verify-files", action="store_true", help="Also compare body_raw.txt bytes; never read images")
    parser.add_argument("--archive-root", type=Path)
    args = parser.parse_args(argv)
    try:
        result = audit_batch(args.db, args.batch_id, workspace_id=args.workspace_id,
                             verify_body_hashes=args.verify_body_hashes, verify_files=args.verify_files,
                             archive_root=args.archive_root)
    except (sqlite3.Error, LookupError, OSError, ValueError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": "无法读取指定批次；未修改数据库。"}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
