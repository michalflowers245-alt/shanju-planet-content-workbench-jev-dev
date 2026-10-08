from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any

import pytest

from backend.app.db import Database, json_dumps, json_loads, new_id, utc_now
from backend.app.jev_classifier import JevClassifier
from backend.app.platform_browser import PlatformBrowserError
from backend.app.radar_batches import DEFAULT_MATCHMAKING_PERSONAS, RadarBatchBusyError, RadarBatchService


class FakeVault:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def put(self, value: str, reference: str | None = None) -> str:
        ref = reference or "fake:key"
        self.values[ref] = value
        return ref

    def get(self, reference: str | None) -> str | None:
        return self.values.get(str(reference or ""))


class FakeClassifier:
    endpoint = "https://api.typesafe.ai/v1/systemone"
    model = "jev-latest"

    def __init__(self) -> None:
        self.batch_sizes: list[int] = []

    def verify_key(self, api_key: str) -> dict[str, Any]:
        assert api_key == "valid-key"
        return {"status": "verified", "model": "jev-test", "latency_ms": 3}

    def classify(self, api_key: str, candidate: dict[str, Any], persona: dict[str, Any]) -> dict[str, Any]:
        assert api_key == "valid-key"
        assert persona["id"] in {"youzipi_changsha", "xiaodouzi_matchmaker"}
        return {
            "route": "A",
            "pillar": "expand_social_circle",
            "audience_need": "expand_social_circle",
            "scores": {"persona_fit": 2.8, "business_fit": 2.7, "content_value": 2.5},
            "risks": {"gender_antagonism": 0.01, "overclaim_or_promise": 0.02, "privacy_risk": 0.01, "off_scope": 0.02},
            "risk_flags": [],
            "confidence": 0.91,
            "composite_score": 0.88,
            "reason": "与人设和业务高度匹配",
            "model": "jev-test",
            "rubric_version": "matchmaking-radar-v1",
            "usage": {"input_tokens": 100, "output_tokens": 20},
            "answers": {"persona_fit": {"type": "score", "score": 2.8, "confidence": 0.9}},
        }

    def classify_many(self, api_key: str, candidates: list[dict[str, Any]], persona: dict[str, Any]) -> list[dict[str, Any]]:
        self.batch_sizes.append(len(candidates))
        return [self.classify(api_key, candidate, persona) for candidate in candidates]


class FakeBatchBrowser:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    def search(self, platform: str, query: str, limit: int = 12) -> list[dict[str, Any]]:
        self.calls.append((platform, query, limit))
        prefix = "explore" if platform == "xiaohongshu" else "video"
        host = "www.xiaohongshu.com" if platform == "xiaohongshu" else "www.douyin.com"
        rows = []
        for index in range(4):
            source = hashlib.sha256(f"{platform}:{query}:{index}".encode()).hexdigest()[:24]
            rows.append({
                "source_key": source,
                "title": f"{query} 的真实候选 {index}",
                "url": f"https://{host}/{prefix}/{source}",
                "excerpt": "成年单身人群扩大社交圈的具体观察",
                "author": "平台公开作者",
                "published_at": None,
                "metrics": {"likes": 100 + index},
                "raw": {"rank": index + 1, "collection": "hidden_visible_search_card"},
            })
        return rows


def wait_for_terminal(service: RadarBatchService, batch_id: str, workspace_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        value = service.get_batch(batch_id, workspace_id)
        if value["status"] not in {"QUEUED", "COLLECTING", "CLASSIFYING"}:
            return value
        time.sleep(0.03)
    raise AssertionError("batch did not finish")


def test_completed_search_can_resume_only_unfinished_scoped_details(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import backend.app.main as main_module
    from backend.app.douyin_extractions import DouyinExtractionService
    from backend.app.xhs_archive import XiaohongshuArchiveService
    from backend.app.xhs_extractions import XiaohongshuExtractionService

    db = Database(tmp_path / "detail-only-resume.sqlite3")
    db.initialize()
    workspace = db.create_workspace("正文单独恢复")
    other_workspace = db.create_workspace("不应受影响的工作区")
    batch_id = "batch_completed_search"
    now = utc_now()
    db.execute(
        """INSERT INTO radar_search_batches
        (id,workspace_id,persona_id,topic,status,stage,created_at,updated_at)
        VALUES(?,?,?,'已经完成搜索','COMPLETED','COMPLETED',?,?)""",
        (batch_id, workspace["id"], "youzipi_changsha", now, now),
    )
    db.execute(
        """INSERT INTO radar_batch_queries(id,batch_id,platform,query,ordinal,status)
        VALUES('old_query',?,'xiaohongshu','已搜过的词',1,'COMPLETED')""", (batch_id,),
    )
    xhs_urls = [f"https://www.xiaohongshu.com/explore/{index:024d}" for index in range(1, 6)]
    dy_ids = [str(7450123456789012345 + index) for index in range(3)]
    dy_urls = [f"https://www.douyin.com/video/{video_id}" for video_id in dy_ids]
    for ordinal, (platform, url) in enumerate([
        *[("xiaohongshu", url) for url in xhs_urls], *[("douyin", url) for url in dy_urls],
    ]):
        # One unverified row and even a malformed cross-workspace batch row
        # must never reach a detail worker.
        candidate_workspace = other_workspace["id"] if url == xhs_urls[4] else workspace["id"]
        verified = "UNVERIFIED" if url == xhs_urls[3] else "LIVE_VERIFIED"
        db.execute(
            """INSERT INTO radar_batch_candidates
            (id,batch_id,workspace_id,platform,source_key,title,url,verification_status,discovered_at)
            VALUES(?,?,?,?,?,?,?,?,?)""",
            (f"candidate_{ordinal}", batch_id, candidate_workspace, platform, f"source_{ordinal}",
             "真实候选", url, verified, now),
        )
    xhs = XiaohongshuExtractionService(db=db, collector=object())
    archives = XiaohongshuArchiveService(db=db, collector=object(), root=tmp_path / "archives")
    complete_xhs = xhs.ensure_archive_record(workspace["id"], xhs_urls[0])
    paused_xhs = xhs.ensure_archive_record(workspace["id"], xhs_urls[1])
    foreign_xhs = xhs.ensure_archive_record(other_workspace["id"], xhs_urls[2])
    for record in (complete_xhs, foreign_xhs):
        db.execute(
            """UPDATE xhs_post_extractions SET archive_status='COMPLETE',verification_status='VERIFIED',
            status='success',body_raw='已核验完整原文' WHERE id=?""", (record["id"],),
        )
    db.execute(
        """INSERT INTO xhs_browser_pause(profile_id,reason,requires_user_action,updated_at)
        VALUES('social_browser','需要验证',1,?)""", (now,),
    )
    assert archives.enqueue(workspace["id"], paused_xhs["id"])["status"] == "PAUSED"

    for index, owner, complete in [(0, workspace["id"], True), (1, workspace["id"], False), (2, other_workspace["id"], True)]:
        db.execute(
            """INSERT INTO douyin_post_extractions
            (id,workspace_id,url,video_id,observed_video_id,published_body,published_body_source,status,extracted_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (f"dy_{index}", owner, dy_urls[index], dy_ids[index], dy_ids[index] if complete else "",
             "已核验全文" if complete else "", "详情页结构化发布文案" if complete else "",
             "COMPLETE" if complete else "USER_ACTION_REQUIRED", now, now),
        )
    dy = DouyinExtractionService(db=db, collector=object())
    flags: list[tuple[str, dict]] = []

    class XhsIntake:
        def enqueue_results(self, owner, payload, **kwargs):
            flags.append(("xiaohongshu", kwargs))
            return xhs.enqueue_results(owner, payload, archive_enqueue=archives.enqueue, **kwargs)

    class DyIntake:
        def enqueue_results(self, owner, payload, **kwargs):
            flags.append(("douyin", kwargs))
            return dy.enqueue_results(owner, payload, **kwargs)

    monkeypatch.setattr(main_module, "xhs_extraction_service", XhsIntake())
    monkeypatch.setattr(main_module, "douyin_extraction_service", DyIntake())
    browser = FakeBatchBrowser()
    classifier = FakeClassifier()
    vault = FakeVault()
    service = RadarBatchService(
        db=db, browser=browser, classifier=classifier, secret_vault=vault,
        on_eligible=main_module._enqueue_radar_detail_reads,
    )
    batch_before = db.one("SELECT * FROM radar_search_batches WHERE id=?", (batch_id,))
    queries_before = db.all("SELECT * FROM radar_batch_queries WHERE batch_id=?", (batch_id,))

    assert service.resume_details(batch_id, workspace["id"]) == {"batch_id": batch_id, "queued": 4}

    assert flags == [
        ("xiaohongshu", {"limit": 500, "resume_after_user_action": True}),
        ("douyin", {"limit": 500, "force_retry": True}),
    ]
    assert browser.calls == []
    assert classifier.batch_sizes == []
    assert vault.values == {}
    assert db.one("SELECT * FROM radar_search_batches WHERE id=?", (batch_id,)) == batch_before
    assert db.all("SELECT * FROM radar_batch_queries WHERE batch_id=?", (batch_id,)) == queries_before
    queued_xhs = db.all(
        """SELECT e.url FROM xhs_archive_jobs j JOIN xhs_post_extractions e ON e.id=j.extraction_id
        WHERE j.workspace_id=? AND j.status='QUEUED'""", (workspace["id"],),
    )
    assert {row["url"] for row in queued_xhs} == set(xhs_urls[1:3])
    queued_dy = db.all("SELECT url FROM douyin_post_extractions WHERE workspace_id=? AND status='QUEUED'", (workspace["id"],))
    assert {row["url"] for row in queued_dy} == set(dy_urls[1:3])
    assert db.one("SELECT status FROM douyin_post_extractions WHERE id='dy_0'")["status"] == "COMPLETE"
    assert db.one("SELECT status FROM douyin_post_extractions WHERE id='dy_2'")["status"] == "COMPLETE"
    assert xhs.get(other_workspace["id"], xhs_urls[2])["archive_status"] == "COMPLETE"
    assert xhs.get(workspace["id"], xhs_urls[3]) is None
    assert xhs.get(other_workspace["id"], xhs_urls[4]) is None
    with pytest.raises(KeyError):
        service.resume_details(batch_id, other_workspace["id"])


@pytest.mark.parametrize("status,in_memory_running", [
    ("QUEUED", False), ("COLLECTING", False), ("CLASSIFYING", False), ("COMPLETED", True),
])
def test_detail_only_resume_rejects_active_batch_without_changing_it(
    tmp_path: Path, status: str, in_memory_running: bool,
):
    db = Database(tmp_path / "busy-details.sqlite3")
    db.initialize()
    workspace = db.create_workspace("正在搜索")
    now = utc_now()
    db.execute(
        """INSERT INTO radar_search_batches(id,workspace_id,persona_id,topic,status,stage,created_at,updated_at)
        VALUES('busy_batch',?,'youzipi_changsha','进行中',?,'COLLECTING',?,?)""",
        (workspace["id"], status, now, now),
    )
    calls = []
    service = RadarBatchService(db=db, browser=FakeBatchBrowser(), classifier=FakeClassifier(), secret_vault=FakeVault(),
                               on_eligible=lambda *args: calls.append(args))
    if in_memory_running:
        service._running_workspaces.add(workspace["id"])
    with pytest.raises(RadarBatchBusyError):
        service.resume_details("busy_batch", workspace["id"])
    assert calls == []
    assert db.one("SELECT status FROM radar_search_batches WHERE id='busy_batch'")["status"] == status


def test_query_plan_expands_with_local_persona_terms_and_unique_variants() -> None:
    queries = RadarBatchService._query_plan(
        "长沙成年人如何扩大同城社交圈", DEFAULT_MATCHMAKING_PERSONAS[0], 96,
    )

    assert len(queries) == 96
    assert len(queries) == len(set(queries))
    assert queries[0] == "长沙成年人如何扩大同城社交圈"
    assert "长沙相亲活动" in queries
    assert "长沙相亲活动 真实经历" in queries


def test_platform_target_allocation_preserves_existing_candidates_and_stops_at_total():
    targets = RadarBatchService._platform_targets(
        300,
        ["xiaohongshu", "douyin"],
        {"xiaohongshu": 154, "douyin": 35},
    )

    assert targets == {"xiaohongshu": 154, "douyin": 146}
    assert sum(targets.values()) == 300

    already_over_total = RadarBatchService._platform_targets(
        300,
        ["xiaohongshu", "douyin"],
        {"xiaohongshu": 154, "douyin": 150},
    )
    assert already_over_total == {"xiaohongshu": 154, "douyin": 150}


def test_legacy_batch_query_plan_grows_in_stages_without_resetting_existing_queries(tmp_path: Path) -> None:
    db = Database(tmp_path / "radar-query-expansion.sqlite3")
    db.initialize()
    workspace = db.create_workspace("查询词扩展")
    service = RadarBatchService(
        db=db, browser=FakeBatchBrowser(), classifier=FakeClassifier(), secret_vault=FakeVault(),
    )
    persona = service.ensure_personas(workspace["id"])[0]
    now = utc_now()
    batch_id = new_id("radar_batch")
    db.execute(
        """INSERT INTO radar_search_batches
        (id,workspace_id,origin,persona_id,persona_snapshot_json,topic,platforms_json,target_count,
         status,stage,counts_json,platform_status_json,taxonomy_version,cancel_requested,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,300,'PARTIAL','COLLECTING','{}','{}','matchmaking-radar-v1',0,?,?)""",
        (batch_id, workspace["id"], "topic_search", persona["id"], json_dumps(persona),
         "长沙成年人如何扩大同城社交圈", json_dumps(["xiaohongshu"]), now, now),
    )
    legacy_queries = service._query_plan("长沙成年人如何扩大同城社交圈", persona, 11)
    with db.transaction() as connection:
        connection.executemany(
            "INSERT INTO radar_batch_queries(id,batch_id,platform,query,ordinal,status) VALUES(?,?,?,?,?,'COMPLETED')",
            [(new_id("radar_query"), batch_id, "xiaohongshu", query, index)
             for index, query in enumerate(legacy_queries, start=1)],
        )
    before = db.all(
        "SELECT query,ordinal,status FROM radar_batch_queries WHERE batch_id=? ORDER BY ordinal", (batch_id,),
    )

    added = service._ensure_query_plan(db.one("SELECT * FROM radar_search_batches WHERE id=?", (batch_id,)))
    after_first_extension = db.all(
        "SELECT query,ordinal,status FROM radar_batch_queries WHERE batch_id=? ORDER BY ordinal", (batch_id,),
    )
    assert added == 37
    assert after_first_extension[:11] == before
    assert len(after_first_extension) == 48
    assert service._ensure_query_plan(db.one("SELECT * FROM radar_search_batches WHERE id=?", (batch_id,))) == 0

    db.execute("UPDATE radar_batch_queries SET status='COMPLETED' WHERE batch_id=?", (batch_id,))
    assert service._ensure_query_plan(db.one("SELECT * FROM radar_search_batches WHERE id=?", (batch_id,))) == 24
    assert db.one("SELECT COUNT(*) AS count FROM radar_batch_queries WHERE batch_id=?", (batch_id,))["count"] == 72


def test_underfilled_search_is_partial_not_completed_or_awaiting_jev_key(tmp_path: Path) -> None:
    class DuplicateBrowser:
        def search(self, platform: str, query: str, limit: int = 12) -> list[dict[str, Any]]:
            host = "www.xiaohongshu.com" if platform == "xiaohongshu" else "www.douyin.com"
            prefix = "explore" if platform == "xiaohongshu" else "video"
            return [{
                "source_key": "one-repeated-source-id",
                "title": "同一条公开内容",
                "url": f"https://{host}/{prefix}/one-repeated-source-id",
                "excerpt": "成年单身人群扩大社交圈的观察",
                "author": "公开作者",
                "metrics": {},
                "raw": {"rank": 1},
            }]

    db = Database(tmp_path / "radar-underfilled.sqlite3")
    db.initialize()
    workspace = db.create_workspace("候选不足")
    service = RadarBatchService(
        db=db, browser=DuplicateBrowser(), classifier=FakeClassifier(), secret_vault=FakeVault(),
    )
    batch = service.create_batch(
        workspace["id"], topic="长沙扩大同城社交圈", persona_id="youzipi_changsha",
        platforms=["xiaohongshu"], target_count=50,
    )
    result = wait_for_terminal(service, batch["id"], workspace["id"])

    assert result["status"] == "PARTIAL"
    assert result["platform_status"]["xiaohongshu"]["status"] == "PARTIAL"
    assert result["platform_status"]["xiaohongshu"]["collected"] == 1
    assert "1/50" in result["error_message"]


def test_batch_collects_classifies_routes_and_enqueues_all_candidate_details(tmp_path: Path):
    db = Database(tmp_path / "radar-batch.sqlite3")
    db.initialize()
    workspace = db.create_workspace("批量研究")
    fake_vault = FakeVault()
    classifier = FakeClassifier()
    enqueued: list[dict[str, Any]] = []
    service = RadarBatchService(
        db=db,
        browser=FakeBatchBrowser(),
        classifier=classifier,
        secret_vault=fake_vault,
        on_eligible=lambda workspace_id, payload: enqueued.append({"workspace_id": workspace_id, **payload}),
    )
    personas = service.ensure_personas(workspace["id"])
    assert {item["id"] for item in personas} == {"youzipi_changsha", "xiaodouzi_matchmaker"}
    profile = json_loads(db.one("SELECT profile_json FROM brand_profiles WHERE workspace_id=?", (workspace["id"],))["profile_json"], {})
    assert profile["radar"]["active_persona_id"] == "youzipi_changsha"

    settings = service.save_settings(workspace["id"], api_key="valid-key")
    assert settings["configured"] is True
    batch = service.create_batch(
        workspace["id"], topic="为什么年轻人社交圈越来越小", persona_id="youzipi_changsha",
        platforms=["xiaohongshu", "douyin"], target_count=50,
    )
    result = wait_for_terminal(service, batch["id"], workspace["id"])
    assert result["status"] == "COMPLETED"
    assert result["counts"]["collected"] > 10
    assert result["counts"]["classified"] == result["counts"]["collected"]
    assert result["counts"]["A"] == result["counts"]["collected"]
    assert max(classifier.batch_sizes) <= 3
    assert len(classifier.batch_sizes) < result["counts"]["collected"]
    assert result["platform_status"]["xiaohongshu"]["verification_status"] == "LIVE_VERIFIED"
    assert result["platform_status"]["douyin"]["verification_status"] == "LIVE_VERIFIED"
    page = service.list_candidates(batch["id"], workspace["id"], route="A", limit=5)
    assert page["total"] == result["counts"]["A"]
    assert len(page["items"]) == 5
    assert "answers" not in page["items"][0]["jev"]
    assert enqueued and all(item["route"] == "A" for item in service.list_candidates(batch["id"], workspace["id"], limit=100)["items"])
    assert len(enqueued[0]["items"]) == result["counts"]["collected"]


def test_batch_can_collect_and_enqueue_without_calling_jev(tmp_path: Path) -> None:
    db = Database(tmp_path / "radar-batch-skip-classification.sqlite3")
    db.initialize()
    workspace = db.create_workspace("只抓正文，不做分类")
    classifier = FakeClassifier()
    enqueued: list[dict[str, Any]] = []
    service = RadarBatchService(
        db=db,
        browser=FakeBatchBrowser(),
        classifier=classifier,
        secret_vault=FakeVault(),
        on_eligible=lambda workspace_id, payload: enqueued.append({"workspace_id": workspace_id, **payload}),
    )
    service.save_settings(workspace["id"], api_key="valid-key")

    batch = service.create_batch(
        workspace["id"], topic="长沙扩大同城社交圈", persona_id="youzipi_changsha",
        platforms=["xiaohongshu", "douyin"], target_count=50, skip_classification=True,
    )
    result = wait_for_terminal(service, batch["id"], workspace["id"])

    assert result["status"] == "COMPLETED"
    assert result["skip_classification"] is True
    assert result["counts"]["collected"] == 50
    assert result["counts"]["classified"] == 0
    assert classifier.batch_sizes == []
    assert len(enqueued) == 1
    assert len(enqueued[0]["items"]) == 50
    assert "跳过 JEV 分类" in result["error_message"]


def test_batch_detail_queue_is_independent_of_jev_a_b_c_routes(tmp_path: Path):
    class MixedRouteClassifier(FakeClassifier):
        def classify(self, api_key: str, candidate: dict[str, Any], persona: dict[str, Any]) -> dict[str, Any]:
            result = super().classify(api_key, candidate, persona)
            result["route"] = {"0": "A", "1": "B", "2": "C", "3": "B"}.get(
                str(candidate["title"]).rsplit(" ", 1)[-1], "C",
            )
            return result

    db = Database(tmp_path / "radar-batch-all-routes.sqlite3")
    db.initialize()
    workspace = db.create_workspace("所有分类均读取原文")
    enqueued: list[dict[str, Any]] = []
    service = RadarBatchService(
        db=db,
        browser=FakeBatchBrowser(),
        classifier=MixedRouteClassifier(),
        secret_vault=FakeVault(),
        on_eligible=lambda workspace_id, payload: enqueued.append({"workspace_id": workspace_id, **payload}),
    )
    service.save_settings(workspace["id"], api_key="valid-key")
    batch = service.create_batch(
        workspace["id"], topic="长沙扩大同城社交圈", persona_id="youzipi_changsha",
        platforms=["xiaohongshu", "douyin"], target_count=50,
    )
    result = wait_for_terminal(service, batch["id"], workspace["id"])
    all_candidates = service.list_candidates(batch["id"], workspace["id"], limit=100)["items"]

    assert result["status"] == "COMPLETED"
    assert result["counts"]["collected"] == 50
    assert {candidate["route"] for candidate in all_candidates} == {"A", "B", "C"}
    assert len(enqueued) == 1
    assert len(enqueued[0]["items"]) == 50
    assert all(candidate["route"] is None for candidate in enqueued[0]["items"])


def test_live_platform_details_queue_before_another_platform_user_verification(tmp_path: Path):
    class DouyinVerificationBrowser(FakeBatchBrowser):
        def search(self, platform: str, query: str, limit: int = 12) -> list[dict[str, Any]]:
            if platform == "douyin":
                raise PlatformBrowserError("抖音要求完成安全验证", code="verification_required")
            return super().search(platform, query, limit)

    db = Database(tmp_path / "radar-batch-platform-independent-details.sqlite3")
    db.initialize()
    workspace = db.create_workspace("平台分别归档")
    classifier = FakeClassifier()
    enqueued: list[dict[str, Any]] = []
    service = RadarBatchService(
        db=db,
        browser=DouyinVerificationBrowser(),
        classifier=classifier,
        secret_vault=FakeVault(),
        on_eligible=lambda workspace_id, payload: enqueued.append({"workspace_id": workspace_id, **payload}),
    )
    service.save_settings(workspace["id"], api_key="valid-key")
    batch = service.create_batch(
        workspace["id"], topic="长沙扩大同城社交圈", persona_id="youzipi_changsha",
        platforms=["xiaohongshu", "douyin"], target_count=50,
    )

    result = wait_for_terminal(service, batch["id"], workspace["id"])

    assert result["status"] == "USER_ACTION_REQUIRED"
    assert result["counts"]["collected"] == 25
    assert result["counts"]["classified"] == 0
    assert result["platform_status"]["xiaohongshu"]["verification_status"] == "LIVE_VERIFIED"
    assert result["platform_status"]["douyin"]["verification_status"] == "USER_ACTION_REQUIRED"
    assert len(enqueued) == 1
    assert len(enqueued[0]["items"]) == 25
    assert {item["platform"] for item in enqueued[0]["items"]} == {"xiaohongshu"}
    assert classifier.batch_sizes == []


def test_candidates_found_before_platform_verification_are_queued_independently(tmp_path: Path):
    class DouyinVerificationAfterFirstPage(FakeBatchBrowser):
        def search(self, platform: str, query: str, limit: int = 12) -> list[dict[str, Any]]:
            if platform == "douyin" and any(call[0] == "douyin" for call in self.calls):
                self.calls.append((platform, query, limit))
                raise PlatformBrowserError("抖音要求完成安全验证", code="verification_required")
            return super().search(platform, query, limit)

    db = Database(tmp_path / "radar-batch-partial-platform-details.sqlite3")
    db.initialize()
    workspace = db.create_workspace("部分候选独立归档")
    classifier = FakeClassifier()
    enqueued: list[dict[str, Any]] = []

    def enqueue_subset(workspace_id: str, payload: dict[str, Any]) -> int:
        enqueued.append({"workspace_id": workspace_id, **payload})
        return 2  # Simulate two new durable records after deduplication.

    service = RadarBatchService(
        db=db,
        browser=DouyinVerificationAfterFirstPage(),
        classifier=classifier,
        secret_vault=FakeVault(),
        on_eligible=enqueue_subset,
    )
    batch = service.create_batch(
        workspace["id"], topic="长沙扩大同城社交圈", persona_id="youzipi_changsha",
        platforms=["douyin"], target_count=50,
    )

    result = wait_for_terminal(service, batch["id"], workspace["id"])
    candidates = service.list_candidates(batch["id"], workspace["id"], limit=100)["items"]

    assert result["status"] == "USER_ACTION_REQUIRED"
    assert result["counts"]["collected"] == 4
    assert result["platform_status"]["douyin"]["verification_status"] == "USER_ACTION_REQUIRED"
    assert len(candidates) == 4
    assert all(candidate["verification_status"] == "LIVE_VERIFIED" for candidate in candidates)
    assert len(enqueued) == 1
    assert len(enqueued[0]["items"]) == 4
    assert {item["platform"] for item in enqueued[0]["items"]} == {"douyin"}
    assert "已新增 2 条详情读取任务" in result["error_message"]
    assert "已新增 4 条详情读取任务" not in result["error_message"]
    assert classifier.batch_sizes == []


def test_batch_waits_for_jev_key_then_resumes_without_recollecting(tmp_path: Path):
    db = Database(tmp_path / "radar-batch-resume.sqlite3")
    db.initialize()
    workspace = db.create_workspace("恢复分类")
    browser = FakeBatchBrowser()
    fake_vault = FakeVault()
    service = RadarBatchService(db=db, browser=browser, classifier=FakeClassifier(), secret_vault=fake_vault)
    batch = service.create_batch(
        workspace["id"], topic="第一次相亲", persona_id="xiaodouzi_matchmaker",
        platforms=["xiaohongshu"], target_count=50,
    )
    waiting = wait_for_terminal(service, batch["id"], workspace["id"])
    assert waiting["status"] == "AWAITING_JEV_KEY"
    db.execute("UPDATE radar_search_batches SET taxonomy_version='matchmaking-radar-v1' WHERE id=?", (batch["id"],))
    call_count = len(browser.calls)
    service.save_settings(workspace["id"], api_key="valid-key")
    service.resume_batch(batch["id"], workspace["id"])
    completed = wait_for_terminal(service, batch["id"], workspace["id"])
    assert completed["status"] == "COMPLETED"
    assert len(browser.calls) == call_count
    assert completed["counts"]["classified"] == completed["counts"]["collected"]
    assert completed["taxonomy_version"] == "matchmaking-radar-v2"


def test_verification_pause_resumes_saved_candidates_then_jev_without_duplicates(tmp_path: Path):
    class VerificationBrowser(FakeBatchBrowser):
        def __init__(self) -> None:
            super().__init__()
            self.verified = False
            self.challenge_raised = False

        def search(self, platform: str, query: str, limit: int = 12) -> list[dict[str, Any]]:
            if platform == "douyin" and not self.verified and not self.challenge_raised:
                self.challenge_raised = True
                self.calls.append((platform, query, limit))
                raise PlatformBrowserError("请完成平台安全验证", code="verification_required")
            return super().search(platform, query, limit)

    db = Database(tmp_path / "radar-batch-verification-resume.sqlite3")
    db.initialize()
    workspace = db.create_workspace("验证后继续")
    browser = VerificationBrowser()
    vault = FakeVault()
    detail_batches: list[dict[str, Any]] = []
    service = RadarBatchService(
        db=db,
        browser=browser,
        classifier=FakeClassifier(),
        secret_vault=vault,
        on_eligible=lambda workspace_id, payload: detail_batches.append({"workspace_id": workspace_id, **payload}),
    )
    batch = service.create_batch(
        workspace["id"], topic="长沙扩大同城社交圈", persona_id="youzipi_changsha",
        platforms=["xiaohongshu", "douyin"], target_count=50,
    )

    paused = wait_for_terminal(service, batch["id"], workspace["id"])
    assert paused["status"] == "USER_ACTION_REQUIRED"
    assert paused["counts"]["collected"] > 0
    assert paused["platform_status"]["xiaohongshu"]["verification_status"] == "LIVE_VERIFIED"
    assert paused["platform_status"]["douyin"]["verification_status"] == "USER_ACTION_REQUIRED"
    assert detail_batches[0]["resume_after_user_action"] is False
    calls_before_resume = len(browser.calls)

    browser.verified = True
    service.resume_batch(batch["id"], workspace["id"], resume_details=True)
    waiting_for_key = wait_for_terminal(service, batch["id"], workspace["id"])
    assert waiting_for_key["status"] == "AWAITING_JEV_KEY"
    candidates_before_classification = service.list_candidates(batch["id"], workspace["id"], limit=100)["items"]
    assert len(candidates_before_classification) == waiting_for_key["counts"]["collected"]
    calls_after_collection_resume = len(browser.calls)
    assert detail_batches[1]["resume_after_user_action"] is True
    assert {item["platform"] for item in detail_batches[1]["items"]} == {"xiaohongshu", "douyin"}
    assert calls_after_collection_resume > calls_before_resume

    service.save_settings(workspace["id"], api_key="valid-key")
    service.resume_batch(batch["id"], workspace["id"])
    completed = wait_for_terminal(service, batch["id"], workspace["id"])
    candidates_after_classification = service.list_candidates(batch["id"], workspace["id"], limit=100)["items"]

    assert completed["status"] == "COMPLETED"
    assert completed["counts"]["classified"] == completed["counts"]["collected"]
    assert len(candidates_after_classification) == len(candidates_before_classification)
    assert len({item["id"] for item in candidates_after_classification}) == len(candidates_before_classification)
    assert len(browser.calls) == calls_after_collection_resume


def test_resuming_verified_batch_can_persistently_skip_jev(tmp_path: Path) -> None:
    class VerificationBrowser(FakeBatchBrowser):
        def __init__(self) -> None:
            super().__init__()
            self.verified = False
            self.challenge_raised = False

        def search(self, platform: str, query: str, limit: int = 12) -> list[dict[str, Any]]:
            if platform == "douyin" and not self.verified and not self.challenge_raised:
                self.challenge_raised = True
                self.calls.append((platform, query, limit))
                raise PlatformBrowserError("请完成平台安全验证", code="verification_required")
            return super().search(platform, query, limit)

    db = Database(tmp_path / "radar-batch-resume-skip-jev.sqlite3")
    db.initialize()
    workspace = db.create_workspace("验证后只继续抓取")
    browser = VerificationBrowser()
    classifier = FakeClassifier()
    service = RadarBatchService(
        db=db, browser=browser, classifier=classifier, secret_vault=FakeVault(),
    )
    service.save_settings(workspace["id"], api_key="valid-key")
    batch = service.create_batch(
        workspace["id"], topic="长沙扩大同城社交圈", persona_id="youzipi_changsha",
        platforms=["xiaohongshu", "douyin"], target_count=50,
    )
    paused = wait_for_terminal(service, batch["id"], workspace["id"])
    assert paused["status"] == "USER_ACTION_REQUIRED"
    calls_before_resume = len(browser.calls)

    browser.verified = True
    service.resume_batch(batch["id"], workspace["id"], skip_classification=True)
    completed = wait_for_terminal(service, batch["id"], workspace["id"])

    assert completed["status"] == "COMPLETED"
    assert completed["skip_classification"] is True
    assert completed["counts"]["collected"] == 50
    assert completed["counts"]["classified"] == 0
    assert classifier.batch_sizes == []
    assert len(browser.calls) > calls_before_resume
    assert "跳过 JEV 分类" in completed["error_message"]


def test_jev_state_redacts_links_phone_wechat_and_author() -> None:
    captured: dict[str, Any] = {}

    class CaptureClassifier(JevClassifier):
        def _request(self, api_key: str, *, state: dict[str, Any], questions: dict[str, Any], timeout: float = 45.0):
            captured["state"] = state
            captured["questions"] = questions
            answers = {
                "persona_fit": {"type": "score", "score": 2.4, "confidence": 0.9},
                "business_fit": {"type": "score", "score": 2.3, "confidence": 0.9},
                "content_value": {"type": "score", "score": 2.2, "confidence": 0.9},
                "content_pillar": {"type": "choice", "choice": "dating", "confidence": 0.9},
                "audience_need": {"type": "choice", "choice": "first_meeting", "confidence": 0.9},
                "gender_antagonism": {"type": "noul", "noul": 0.01},
                "overclaim_or_promise": {"type": "noul", "noul": 0.01},
                "privacy_risk": {"type": "noul", "noul": 0.01},
                "off_scope": {"type": "noul", "noul": 0.01},
            }
            return {"model": "jev-test", "answers": answers, "usage": {"input_tokens": 1, "output_tokens": 1}}

    classifier = CaptureClassifier()
    result = classifier.classify(
        "secret",
        {
            "platform": "xiaohongshu",
            "title": "第一次相亲",
            "excerpt": "详情 https://private.example 联系微信 wx: abcdefg 手机 13812345678",
            "author": "不应发送的作者账号",
            "url": "https://www.xiaohongshu.com/explore/private",
            "metrics": {"likes": 9},
            "search_query": "女生相亲",
        },
        {
            "label": "小豆子",
            "public_role": "红娘",
            "audience": "成年单身女性",
            "business": "婚恋沟通",
            "guardrails": [],
            "pillars": [{"id": "dating", "description": "相亲沟通"}],
        },
    )
    serialized = str(captured["state"])
    assert "private.example" not in serialized
    assert "13812345678" not in serialized
    assert "abcdefg" not in serialized
    assert "不应发送的作者账号" not in serialized
    assert "xiaohongshu.com" not in serialized
    assert "第一次相亲" in result["decision_evidence"]
    assert "[链接已移除]" in result["decision_evidence"]
    assert "private.example" not in result["reason"]
    assert "13812345678" not in result["reason"]
    assert "keyword overlap by itself" in captured["questions"]["persona_fit"]["instructions"]
    assert result["rubric_version"] == "matchmaking-radar-v2"


def test_jev_classify_many_is_one_request_with_isolated_candidate_answers() -> None:
    captured: dict[str, Any] = {}

    class BatchCaptureClassifier(JevClassifier):
        def _request(self, api_key: str, *, state: dict[str, Any], questions: dict[str, Any], timeout: float = 45.0):
            captured["state"] = state
            captured["questions"] = questions
            answers = {}
            for key, question in questions.items():
                prefix = key.split("_", 1)[0]
                if key.endswith("persona_fit"):
                    answers[key] = {"type": "score", "score": 3.0 if prefix == "c0" else 1.0, "confidence": 0.95}
                elif key.endswith("business_fit"):
                    answers[key] = {"type": "score", "score": 3.0 if prefix == "c0" else 1.0, "confidence": 0.95}
                elif key.endswith("content_value"):
                    answers[key] = {"type": "score", "score": 3.0 if prefix == "c0" else 1.0, "confidence": 0.95}
                elif key.endswith("content_pillar"):
                    answers[key] = {"type": "choice", "choice": "dating", "confidence": 0.95}
                elif key.endswith("audience_need"):
                    answers[key] = {"type": "choice", "choice": "first_meeting", "confidence": 0.95}
                else:
                    answers[key] = {"type": "noul", "noul": 0.98 if prefix == "c1" and key.endswith("gender_antagonism") else 0.01}
            return {"model": "jev-test", "answers": answers, "usage": {"input_tokens": 101, "output_tokens": 13}}

    classifier = BatchCaptureClassifier()
    persona = {
        "label": "小豆子", "public_role": "红娘", "audience": "成年单身女性", "business": "婚恋沟通",
        "guardrails": [], "pillars": [{"id": "dating", "description": "相亲沟通"}],
    }
    candidates = [
        {"platform": "xiaohongshu", "title": "第一次见面怎么聊", "excerpt": "说具体顾虑", "author": "不发送账号甲"},
        {"platform": "douyin", "title": "男女对立引流", "excerpt": "羞辱表达", "author": "不发送账号乙"},
    ]

    results = classifier.classify_many("secret", candidates, persona)

    assert len(captured["questions"]) == 18
    assert list(captured["state"]["candidate_0"].keys()) == ["platform", "title", "excerpt", "public_metrics", "search_query"]
    assert "不发送账号甲" not in str(captured["state"])
    assert results[0]["route"] == "A"
    assert results[1]["route"] == "C"
    assert "第一次见面怎么聊" in results[0]["decision_evidence"]
    assert "男女对立引流" not in results[0]["decision_evidence"]
    assert "性别对立或羞辱" in results[1]["reason"]
    assert set(results[0]["answers"]) == {key for key in captured["questions"] if key.startswith("c0_")}
    assert set(results[1]["answers"]) == {key for key in captured["questions"] if key.startswith("c1_")}
    assert sum(result["usage"]["input_tokens"] for result in results) == 101
    assert sum(result["usage"]["output_tokens"] for result in results) == 13
