"""Small real-platform acceptance run; keeps evidence in an isolated database.

Uses the workbench's existing dedicated browser in place. Never copies login
state. Stops a platform's samples when it asks for login/verification.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.db import Database
from backend.app.douyin_extractions import DouyinExtractionService
from backend.app.platform_browser import PlatformBrowserCollector, PlatformBrowserError
from backend.app.xhs_archive import XiaohongshuArchiveRepository
from backend.app.xhs_extractions import XiaohongshuExtractionService


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", choices=["xiaohongshu", "douyin", "both"], default="both")
    parser.add_argument("--query", default="长沙成年人如何扩大同城社交圈")
    parser.add_argument("--limit", type=int, choices=[1, 2, 3], default=2)
    args = parser.parse_args()
    output_root = ROOT / "test-logs"
    output_root.mkdir(parents=True, exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix="live-copy-", dir=output_root))
    db = Database(output / "acceptance.sqlite3")
    db.initialize()
    workspace = db.create_workspace("真实文案验收（隔离数据）")["id"]
    collector = PlatformBrowserCollector()
    xhs = XiaohongshuExtractionService(db=db, collector=collector)
    repository = XiaohongshuArchiveRepository(db=db, root=output / "xhs_archives")
    douyin = DouyinExtractionService(db=db, collector=collector)
    report = {"query": args.query, "isolated_database": str(db.path), "platforms": {}}
    print(json.dumps({"output": str(output)}, ensure_ascii=False), flush=True)
    platforms = ["xiaohongshu", "douyin"] if args.platform == "both" else [args.platform]
    gates = {"login_required", "verification_required", "platform_limited", "browser_page_required", "browser_not_running"}
    for platform in platforms:
        samples = []
        record = {"search_status": "PENDING", "samples": samples}
        report["platforms"][platform] = record
        try:
            rows = collector.search(platform, args.query, limit=args.limit)
            record.update(search_status="LIVE_VERIFIED", returned=len(rows))
            print(json.dumps({"platform": platform, "search_status": "LIVE_VERIFIED", "returned": len(rows)}, ensure_ascii=False), flush=True)
            for row in rows[:args.limit]:
                started = time.monotonic()
                sample = {"url": row["url"], "title": row["title"]}
                samples.append(sample)
                try:
                    if platform == "xiaohongshu":
                        extraction = xhs.ensure_archive_record(workspace, row["url"], search_keyword=args.query, title_hint=row["title"])
                        capture = collector.archive_xiaohongshu_detail(row["url"], search_keyword=args.query, title_hint=row["title"])
                        result = repository.persist(workspace, extraction["id"], capture)
                        body = str(capture.get("body_raw") or "")
                        saved = xhs.get_by_id(workspace, extraction["id"])
                        sample.update(
                            status=result.archive_status, body_verified=capture.get("body_verified"),
                            requested_id=capture["target_post_id"], observed_id=capture["observed_post_id"],
                            body_chars=len(body), body_sha256=hashlib.sha256(body.encode()).hexdigest(),
                            source=capture.get("body_source"), archive_id=result.archive_id,
                            body_roundtrip_equal=saved.get("body_raw") == body,
                            expected_images=result.expected_asset_count, saved_images=result.saved_asset_count,
                        )
                    else:
                        result = douyin.extract(workspace, row["url"], search_keyword=args.query, title_hint=row["title"])
                        body = result.get("published_body") or ""
                        sample.update(
                            status=result["status"], source=result.get("published_body_source"),
                            requested_id=result.get("video_id"), observed_id=result.get("observed_video_id"),
                            body_chars=len(body), body_sha256=hashlib.sha256(body.encode()).hexdigest(),
                            asr_status=result.get("asr_status"), failure_stage=result.get("failure_stage"),
                            message=result.get("error_message"),
                            body_roundtrip_equal=douyin.get(workspace, row["url"])["published_body"] == body,
                        )
                except PlatformBrowserError as error:
                    sample.update(status="FAILED", code=error.code, message=str(error))
                sample["seconds"] = round(time.monotonic() - started, 2)
                print(json.dumps({"platform": platform, **sample}, ensure_ascii=False), flush=True)
                if sample.get("code") in gates or sample.get("status") in {"NEEDS_BROWSER", "USER_ACTION_REQUIRED", "NEEDS_ATTENTION"}:
                    break
                time.sleep(1.2)
        except PlatformBrowserError as error:
            record.update(search_status="FAILED", code=error.code, message=str(error))
            print(json.dumps({"platform": platform, **record}, ensure_ascii=False), flush=True)
        finally:
            (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
