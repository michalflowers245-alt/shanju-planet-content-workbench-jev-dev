from __future__ import annotations

import hashlib
from pathlib import Path
import time
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
import pytest

import backend.app.main as main_module
from backend.app.db import Database, json_dumps, json_loads
from backend.app.monitoring import TopicRadarService, _query_terms, _relevance_score
from backend.app.xhs_extractions import XiaohongshuExtractionService


class FakePublicSearch:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    def search(self, workspace_id: str, query: str, limit: int = 8):
        self.calls.append((workspace_id, query, limit))
        return [
            {
                "title": f"{query}：公开趋势样例",
                "url": f"https://example.test/{hashlib.sha256(query.encode()).hexdigest()[:12]}",
                "excerpt": "来自公开搜索的测试线索。",
                "published_at": "2026-09-14",
                "credibility": 0.65,
                "search_mode": "fake_public_search",
            }
        ]


class FakeBrowser:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    def search(self, platform: str, query: str, limit: int = 12):
        self.calls.append((platform, query, limit))
        source_id = hashlib.sha256(f"{platform}:{query}".encode()).hexdigest()[:12]
        return [{
            "source_key": source_id,
            "title": f"{query}：平台可见热门内容",
            "url": f"https://www.{platform}.com/{'explore' if platform == 'xiaohongshu' else 'video'}/{source_id}",
            "excerpt": "这是浏览器当前搜索页实际显示的文案内容。",
            "author": "平台作者",
            "published_at": None,
            "metrics": {"likes": 321},
            "raw": {"rank": 1, "collection": "visible_search_card"},
        }]


class FakeRadarVault:
    def get(self, reference: str | None) -> str | None:
        return "valid-test-key" if reference == "fake:jev" else None


class FakeRadarClassifier:
    model = "jev-test"
    endpoint = "https://api.typesafe.ai/v1/systemone"

    def classify_many(self, api_key: str, candidates: list[dict], persona: dict) -> list[dict]:
        assert api_key == "valid-test-key"
        assert persona.get("business")
        return [{
            "route": "A",
            "pillar": "expand_social_circle",
            "audience_need": "expand_social_circle",
            "scores": {"persona_fit": 2.8, "business_fit": 2.7, "content_value": 2.5},
            "risks": {"gender_antagonism": 0.01, "overclaim_or_promise": 0.01, "privacy_risk": 0.01, "off_scope": 0.01},
            "risk_flags": [],
            "confidence": 0.91,
            "composite_score": 0.88,
            "reason": "与人设和业务高度匹配",
            "model": "jev-test",
            "rubric_version": "matchmaking-radar-v1",
            "usage": {"input_tokens": 20, "output_tokens": 5},
            "answers": {},
        } for _ in candidates]


def enable_fake_jev(db: Database, workspace_id: str) -> None:
    db.execute(
        """INSERT INTO jev_settings
        (workspace_id,secret_ref,enabled,verification_status,updated_at)
        VALUES(?,'fake:jev',1,'verified','2026-09-23T00:00:00+00:00')""",
        (workspace_id,),
    )


def radar_service(db: Database, browser=None) -> TopicRadarService:
    return TopicRadarService(
        db=db,
        public_search=FakePublicSearch(),
        browser_collector=browser or FakeBrowser(),
        classifier=FakeRadarClassifier(),
        secret_vault=FakeRadarVault(),
    )


class MixedHeatBrowser:
    def search(self, platform: str, query: str, limit: int = 12):
        prefix = "explore" if platform == "xiaohongshu" else "video"
        host = "xiaohongshu" if platform == "xiaohongshu" else "douyin"
        rows = [
            ("unrelated", "相亲相爱电竞夺冠", "赛事集锦", 999999),
            ("warm", "长沙相亲真实体验", "线下活动复盘", 120),
            ("hot", "长沙线下相亲活动避坑", "相亲参与者最在意什么", 8800),
        ]
        return [{
            "source_key": f"{platform}-{key}",
            "title": title,
            "url": f"https://www.{host}.com/{prefix}/{platform}-{key}",
            "excerpt": excerpt,
            "author": "作者",
            "published_at": None,
            "metrics": {"likes": likes},
            "raw": {"rank": index, "collection": "visible_search_card"},
        } for index, (key, title, excerpt, likes) in enumerate(rows, start=1)]


def test_manual_topic_xhs_favorite_uses_a_workspace_known_extraction_only(tmp_path: Path):
    db = Database(tmp_path / "topic-favorite-snapshot.sqlite3")
    db.initialize()
    radar = TopicRadarService(db=db, public_search=FakePublicSearch(), browser_collector=FakeBrowser())
    xhs = XiaohongshuExtractionService(db=db, collector=object())
    workspace = db.create_workspace("主题搜索收藏")
    other = db.create_workspace("其他工作区")
    url = "https://www.xiaohongshu.com/explore/68c6f07d000000001d01abcd?xsec_token=temporary"
    extraction = xhs.ensure_archive_record(
        workspace["id"], url, title_hint="服务端已知的小红书标题",
    )

    favorite = radar.favorite_xiaohongshu_extraction(workspace["id"], extraction)
    assert favorite["platform"] == "xiaohongshu"
    assert favorite["title"] == "服务端已知的小红书标题"
    assert favorite["url"] == "https://www.xiaohongshu.com/explore/68c6f07d000000001d01abcd"
    assert favorite["raw"]["source_kind"] == "xiaohongshu_extraction"
    assert "xsec_token" not in json_dumps(favorite)

    repeated = radar.favorite_xiaohongshu_extraction(workspace["id"], extraction)
    assert repeated["id"] == favorite["id"]
    assert len(radar.list_favorites(workspace["id"])) == 1
    with pytest.raises(ValueError, match="不属于"):
        radar.favorite_xiaohongshu_extraction(other["id"], extraction)


def test_ip_daily_radar_derives_only_broad_terms_and_never_saves_profile_text(tmp_path: Path):
    db = Database(tmp_path / "daily-radar.sqlite3")
    db.initialize()
    public_search = FakePublicSearch()
    service = radar_service(db)
    workspace = db.create_workspace("IP 每日热点")
    enable_fake_jev(db, workspace["id"])
    private_profile_phrase = "我的姓名和联系方式绝不应该进入搜索或结果"
    db.execute(
        "UPDATE brand_profiles SET profile_json=? WHERE workspace_id=?",
        (json_dumps({"common_info": f"我在长沙做相亲婚恋内容，关注线下交友和脱单。{private_profile_phrase}"}), workspace["id"]),
    )

    monitor = service.ensure_ip_daily_monitor(workspace["id"])
    assert monitor["monitor_kind"] == "ip_daily"
    assert monitor["platforms_json"] == '["web"]'
    assert service.ensure_ip_daily_monitor(workspace["id"])["id"] == monitor["id"]

    run = service.run_monitor(monitor["id"], workspace["id"])
    summary = json_loads(run["summary_json"], {})
    assert run["status"] == "DONE"
    assert summary["monitor_kind"] == "ip_daily"
    assert summary["profile_based"] is True
    assert [item["platform"] for item in summary["platforms"]] == ["xiaohongshu", "douyin"]
    recommendations = summary["recommendations"]
    # The fake browser exposes exactly one real post per platform.  The UI
    # must not pad that with profile-only cards which have no matching body.
    assert len(recommendations) == 2
    assert {item["persona"] for item in recommendations}.issuperset({
        "柚子皮 · 同城线下相亲", "小豆子 · 女性脱单红娘",
    })
    assert public_search.calls == []
    for recommendation in recommendations:
        assert recommendation["evidence_url"]
        assert recommendation["source_urls"] == [recommendation["evidence_url"]]
        links = recommendation["platform_search_links"]
        assert [item["platform"] for item in links] == ["douyin", "xiaohongshu"]
        assert [urlsplit(item["url"]).hostname for item in links] == ["www.douyin.com", "www.xiaohongshu.com"]
        assert all(item["url"].startswith("https://") for item in links)
        assert all("example.test" not in url for url in recommendation["source_urls"])
    serialized = json_dumps({"summary": summary, "items": run["items"]})
    assert private_profile_phrase not in serialized

    repeat = service.run_monitor(monitor["id"], workspace["id"])
    repeat_summary = json_loads(repeat["summary_json"], {})
    # A refresh is a current-page snapshot, not a "never show this post again"
    # filter.  Repeated cards remain visible when they were genuinely read in
    # this round; is_new/new_count still tells the UI whether they are new.
    assert len(repeat["items"]) == 2
    assert all(item["is_new"] == 0 for item in repeat["items"])
    assert repeat_summary["new_count"] == 0
    assert repeat_summary["previously_seen_count"] == 2
    assert len(repeat_summary["recommendations"]) == 2
    assert repeat_summary["freshness_mode"] == "current_snapshot"


def test_daily_radar_keeps_the_full_xhs_page_for_automatic_body_archiving(tmp_path: Path):
    class DenseBrowser:
        def search(self, platform: str, query: str, limit: int = 12):
            count = min(limit, 14 if platform == "xiaohongshu" else 8)
            prefix = "explore" if platform == "xiaohongshu" else "video"
            return [{
                "source_key": f"{platform}-{index}",
                "title": f"长沙热门帖子 {index}" if platform == "xiaohongshu" else f"脱单热门视频 {index}",
                "url": f"https://www.{platform}.com/{prefix}/{index:024d}",
                "excerpt": "平台搜索页可见内容",
                "author": "平台作者",
                "published_at": None,
                "metrics": {"likes": 1000 - index},
                "raw": {"rank": index + 1, "collection": "visible_search_card"},
            } for index in range(count)]

    db = Database(tmp_path / "daily-full-page.sqlite3")
    db.initialize()
    service = TopicRadarService(db=db, public_search=FakePublicSearch(), browser_collector=DenseBrowser())
    workspace = db.create_workspace("每日全部原帖")
    db.execute(
        "UPDATE brand_profiles SET profile_json=? WHERE workspace_id=?",
        (json_dumps({"common_info": "我在长沙做相亲、脱单与线下交友内容"}), workspace["id"]),
    )

    monitor = service.ensure_ip_daily_monitor(workspace["id"])
    run = service.run_monitor(monitor["id"], workspace["id"])
    xhs_items = [item for item in run["items"] if item["platform"] == "xiaohongshu"]

    assert len(xhs_items) == 12
    assert all(item["source_mode"] == "managed_background_browser" for item in xhs_items)


def test_one_off_topic_discovery_returns_sources_and_copy_ready_angles(tmp_path: Path):
    db = Database(tmp_path / "discovery.sqlite3")
    db.initialize()
    public_search = FakePublicSearch()
    service = radar_service(db)
    workspace = db.create_workspace("主题搜索")
    enable_fake_jev(db, workspace["id"])
    result = service.discover_topic(workspace["id"], "同城相亲活动")

    assert result["search_mode"] == "managed_background_browser"
    assert result["source_count"] == 2
    assert result["visible_source_count"] == 2
    assert len(result["angles"]) == result["source_count"]
    assert all(item.get("evidence_url") for item in result["angles"])
    assert result["angles"][0]["topic"] == "同城相亲活动"
    assert [item["platform"] for item in result["platform_search_links"]] == ["douyin", "xiaohongshu"]
    assert result["angles"][0]["source_urls"][0].startswith("https://www.")


def test_one_off_search_keeps_unclassified_sources_but_never_recommends_without_jev(tmp_path: Path):
    db = Database(tmp_path / "discovery-awaits-jev.sqlite3")
    db.initialize()
    service = TopicRadarService(db=db, public_search=FakePublicSearch(), browser_collector=FakeBrowser())
    workspace = db.create_workspace("未配置 JEV")

    result = service.discover_topic(workspace["id"], "同城相亲活动")

    assert result["source_count"] == 2
    assert result["jev_classification"]["status"] == "AWAITING_JEV_KEY"
    assert result["angles"] == []
    assert all(item["classification_status"] == "AWAITING_JEV_KEY" for item in result["sources"])


def test_daily_hotspots_wait_for_jev_before_marking_the_run_complete(tmp_path: Path):
    db = Database(tmp_path / "daily-awaits-jev.sqlite3")
    db.initialize()
    service = TopicRadarService(db=db, public_search=FakePublicSearch(), browser_collector=FakeBrowser())
    workspace = db.create_workspace("每日热点等待 JEV")
    db.execute(
        "UPDATE brand_profiles SET profile_json=? WHERE workspace_id=?",
        (json_dumps({"common_info": "我在长沙做相亲和线下交友内容"}), workspace["id"]),
    )

    run = service.run_monitor(service.ensure_ip_daily_monitor(workspace["id"])["id"], workspace["id"])
    summary = json_loads(run["summary_json"], {})

    assert run["status"] == "PARTIAL"
    assert summary["jev_classification"]["status"] == "AWAITING_JEV_KEY"
    assert summary["recommendations"] == []
    assert "请在设置中连接并验证 JEV Key" in run["error_message"]


def test_detail_read_callback_queues_all_routes_and_unclassified_candidates(monkeypatch: pytest.MonkeyPatch):
    queued: dict[str, Any] = {"xhs": None, "douyin": None}
    limits: dict[str, int] = {}

    class QueueCapture:
        def __init__(self, key: str) -> None:
            self.key = key

        def enqueue_results(self, workspace_id: str, payload: dict[str, Any], *, limit: int) -> int:
            queued[self.key] = payload
            limits[self.key] = limit
            return 7 if self.key == "xhs" else 2

    monkeypatch.setattr(main_module, "xhs_extraction_service", QueueCapture("xhs"))
    monkeypatch.setattr(main_module, "douyin_extraction_service", QueueCapture("douyin"))
    newly_queued = main_module._enqueue_radar_detail_reads("workspace-test", {
        "items": [
            {"platform": "douyin", "route": "A", "url": "https://www.douyin.com/video/1"},
            {"platform": "douyin", "route": "B", "url": "https://www.douyin.com/video/2"},
            {"platform": "xiaohongshu", "raw_json": json_dumps({"jev": {"route": "C"}})},
            {"platform": "xiaohongshu", "url": "https://www.xiaohongshu.com/explore/unclassified"},
        ],
        "sources": [
            {"platform": "xiaohongshu", "jev": {"route": "A"}, "url": "https://www.xiaohongshu.com/explore/1"},
            {"platform": "xiaohongshu", "jev": {"route": "B"}, "url": "https://www.xiaohongshu.com/explore/2"},
        ],
        "summary": {"recommendations": [
            {"evidence_platform": "xiaohongshu", "route": "A", "evidence_url": "https://www.xiaohongshu.com/explore/1"},
            {"evidence_platform": "xiaohongshu", "route": "B", "evidence_url": "https://www.xiaohongshu.com/explore/2"},
            {"evidence_platform": "xiaohongshu", "route": "C", "evidence_url": "https://www.xiaohongshu.com/explore/3"},
        ]},
    })

    # The callback passes one shared payload; platform-specific services filter
    # their own posts without this layer discarding B/C or unclassified rows.
    assert [item.get("route") for item in queued["douyin"]["items"]] == ["A", "B", None, None]
    assert json_loads(queued["douyin"]["items"][2]["raw_json"], {})["jev"]["route"] == "C"
    assert [item.get("jev", {}).get("route") for item in queued["xhs"]["sources"]] == ["A", "B"]
    assert [item["route"] for item in queued["xhs"]["summary"]["recommendations"]] == ["A", "B", "C"]
    assert len(queued["xhs"]["items"]) == 4
    assert limits == {"xhs": 500, "douyin": 500}
    assert newly_queued == 9


def test_user_action_resume_forces_douyin_caption_refetch_and_resumes_xhs_archive(monkeypatch: pytest.MonkeyPatch):
    calls: dict[str, dict[str, Any]] = {}

    class QueueCapture:
        def __init__(self, key: str) -> None:
            self.key = key

        def enqueue_results(self, workspace_id: str, payload: dict[str, Any], *, limit: int, **kwargs: Any) -> int:
            calls[self.key] = {"workspace_id": workspace_id, "payload": payload, "limit": limit, **kwargs}
            return 1

    monkeypatch.setattr(main_module, "xhs_extraction_service", QueueCapture("xhs"))
    monkeypatch.setattr(main_module, "douyin_extraction_service", QueueCapture("douyin"))

    queued = main_module._enqueue_radar_detail_reads("workspace-test", {
        "items": [
            {"platform": "xiaohongshu", "url": "https://www.xiaohongshu.com/explore/post-1"},
            {"platform": "douyin", "url": "https://www.douyin.com/video/7450123456789012345"},
        ],
        "resume_after_user_action": True,
    })

    assert queued == 2
    assert calls["xhs"]["resume_after_user_action"] is True
    assert calls["douyin"]["force_retry"] is True
    assert calls["xhs"]["limit"] == calls["douyin"]["limit"] == 500


def test_topic_discovery_filters_accidental_matches_and_sorts_visible_heat(tmp_path: Path):
    db = Database(tmp_path / "hot-radar.sqlite3")
    db.initialize()
    service = TopicRadarService(
        db=db, public_search=FakePublicSearch(), browser_collector=MixedHeatBrowser(),
    )
    workspace = db.create_workspace("热门筛选")
    result = service.discover_topic(workspace["id"], "长沙 相亲 线下活动")

    assert result["source_count"] == 4
    assert all("电竞" not in item["title"] for item in result["sources"])
    for platform in ("xiaohongshu", "douyin"):
        rows = [item for item in result["sources"] if item["platform"] == platform]
        assert [item["metrics"]["likes"] for item in rows] == [8800, 120]
        assert rows[0]["raw"]["relevance_score"] >= 2


def test_manual_topic_discovery_segments_compact_cjk_location_and_matchmaking_terms(tmp_path: Path):
    """`长沙相亲` should keep real `长沙找对象` cards without accepting noise."""
    class CompactChineseBrowser:
        def search(self, platform: str, query: str, limit: int = 12):
            assert query == "长沙相亲"
            prefix = "explore" if platform == "xiaohongshu" else "video"
            return [
                {
                    "source_key": f"{platform}-relevant",
                    "title": "长沙找对象，认真认识一个人" if platform == "xiaohongshu" else "长沙同城脱单经验",
                    "url": f"https://www.{platform}.com/{prefix}/{platform}-relevant",
                    "excerpt": "线下见面前怎么筛选，避免盲目相亲。",
                    "author": "真实作者",
                    "published_at": None,
                    "metrics": {"likes": 8800},
                    "raw": {"rank": 1, "collection": "visible_search_card"},
                },
                {
                    "source_key": f"{platform}-noise",
                    "title": "相亲相爱电竞夺冠",
                    "url": f"https://www.{platform}.com/{prefix}/{platform}-noise",
                    "excerpt": "赛事集锦",
                    "author": "无关作者",
                    "published_at": None,
                    "metrics": {"likes": 999999},
                    "raw": {"rank": 2, "collection": "visible_search_card"},
                },
            ]

    assert _query_terms("长沙相亲") == ["长沙", "相亲"]
    assert _relevance_score(
        {"title": "长沙找对象，认真认识一个人", "excerpt": ""}, "长沙相亲",
    ) == 2
    assert _relevance_score(
        {"title": "相亲相爱电竞夺冠", "excerpt": ""}, "长沙相亲",
    ) == -1

    db = Database(tmp_path / "compact-cjk-topic.sqlite3")
    db.initialize()
    service = TopicRadarService(
        db=db, public_search=FakePublicSearch(), browser_collector=CompactChineseBrowser(),
    )
    workspace = db.create_workspace("紧凑中文主题")

    result = service.discover_topic(workspace["id"], "长沙相亲")

    assert result["search_mode"] == "managed_background_browser"
    assert result["visible_source_count"] == 2
    assert {row["source_key"] for row in result["sources"]} == {
        "xiaohongshu-relevant", "douyin-relevant",
    }


def test_topic_radar_http_api_returns_saved_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = Database(tmp_path / "radar-api.sqlite3")
    db.initialize()
    service = radar_service(db)

    class Worker:
        def start(self):
            pass

        def stop(self):
            pass

        def wake(self):
            pass

    monkeypatch.setenv("WORKBENCH_DISABLE_WORKERS", "1")
    monkeypatch.setattr(main_module, "database", db)
    monkeypatch.setattr(main_module, "topic_radar_service", service)
    monkeypatch.setattr(main_module, "topic_radar_worker", Worker())
    workspace = db.create_workspace("API 雷达")
    enable_fake_jev(db, workspace["id"])

    with TestClient(main_module.app) as client:
        db.execute(
            "UPDATE brand_profiles SET profile_json=? WHERE workspace_id=?",
            (json_dumps({"common_info": "专注同城相亲与婚恋关系内容"}), workspace["id"]),
        )
        daily = client.post("/api/topic-radar/ip-daily/enable", json={"workspace_id": workspace["id"]})
        assert daily.status_code == 201, daily.text
        assert daily.json()["monitor"]["monitor_kind"] == "ip_daily"
        first_id = daily.json()["latest_run"]["id"]
        first = daily.json()["latest_run"]
        deadline = time.monotonic() + 2
        while first["status"] == "RUNNING" and time.monotonic() < deadline:
            time.sleep(0.02)
            first = next(item for item in client.get(
                f"/api/topic-radar/monitors/{daily.json()['monitor']['id']}/runs",
                params={"workspace_id": workspace["id"], "limit": 5},
            ).json() if item["id"] == first_id)
        assert first["summary"]["monitor_kind"] == "ip_daily"
        daily_refresh = client.post("/api/topic-radar/ip-daily/run", json={"workspace_id": workspace["id"]})
        assert daily_refresh.status_code == 202, daily_refresh.text
        assert daily_refresh.json()["monitor"]["monitor_kind"] == "ip_daily"
        refresh_id = daily_refresh.json()["latest_run"]["id"]
        deadline = time.monotonic() + 2
        refreshed = daily_refresh.json()["latest_run"]
        while refreshed["status"] == "RUNNING" and time.monotonic() < deadline:
            time.sleep(0.02)
            runs = client.get(
                f"/api/topic-radar/monitors/{daily_refresh.json()['monitor']['id']}/runs",
                params={"workspace_id": workspace["id"], "limit": 5},
            ).json()
            refreshed = next(item for item in runs if item["id"] == refresh_id)
        assert refreshed["status"] == "DONE"
        assert refreshed["summary"]["monitor_kind"] == "ip_daily"
        listed_after_daily = client.get("/api/topic-radar/monitors", params={"workspace_id": workspace["id"]})
        assert {row["monitor_kind"] for row in listed_after_daily.json()} == {"ip_daily"}

        discovered = client.post("/api/topic-radar/topic-search", json={
            "workspace_id": workspace["id"], "topic": "第一次相亲怎么聊天",
        })
        assert discovered.status_code == 200, discovered.text
        source_urls = [item["url"] for item in discovered.json()["platform_search_links"]]
        copy_task = client.post("/api/topic-radar/topic-search/create", json={
            "workspace_id": workspace["id"],
            "topic": "第一次相亲怎么聊天",
            "product_mode": "douyin_emotion",
            "source_urls": source_urls,
            "constraints": {"radar_platform_searches": discovered.json()["platform_search_links"]},
        })
        assert copy_task.status_code == 202, copy_task.text
        task = copy_task.json()["task"]
        assert task["content_type"] == "short_video"
        assert task["platform"] == "抖音"
        assert task["constraints"]["product_mode"] == "douyin_emotion"
        assert task["constraints"]["radar_origin"] == "topic_search"
        assert task["constraints"]["radar_platform_searches"][0]["platform"] == "douyin"


def test_manual_topic_source_favorite_http_uses_known_scoped_xhs_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    db = Database(tmp_path / "topic-favorite-api.sqlite3")
    db.initialize()
    radar = TopicRadarService(db=db, public_search=FakePublicSearch(), browser_collector=FakeBrowser())
    xhs = XiaohongshuExtractionService(db=db, collector=object())
    workspace = db.create_workspace("主题搜索 API 收藏")
    other = db.create_workspace("别的工作区")
    known_url = "https://www.xiaohongshu.com/explore/68c6f07d000000001d01abcd"
    xhs.ensure_archive_record(workspace["id"], known_url, title_hint="服务器可信标题")

    class Worker:
        def start(self): pass
        def stop(self): pass
        def wake(self): pass

    monkeypatch.setenv("WORKBENCH_DISABLE_WORKERS", "1")
    monkeypatch.setattr(main_module, "database", db)
    monkeypatch.setattr(main_module, "topic_radar_service", radar)
    monkeypatch.setattr(main_module, "xhs_extraction_service", xhs)
    monkeypatch.setattr(main_module, "topic_radar_worker", Worker())

    with TestClient(main_module.app) as client:
        payload = {
            "workspace_id": workspace["id"],
            "source": {
                "platform": "xiaohongshu",
                "url": known_url + "?xsec_token=must-not-be-saved",
                "title": "客户端伪造标题 secret=never-store",
                "author": "客户端作者",
                "excerpt": "客户端摘要 token=never-store",
                "metrics": {"likes": 999999},
            },
        }
        created = client.post("/api/topic-radar/favorites/source-snapshot", json=payload)
        assert created.status_code == 201, created.text
        value = created.json()
        assert value["title"] == "服务器可信标题"
        assert value["url"] == known_url
        serialized = json_dumps(value)
        assert "must-not-be-saved" not in serialized
        assert "never-store" not in serialized

        # The same syntactically valid source is not usable from another workspace.
        cross_workspace = client.post("/api/topic-radar/favorites/source-snapshot", json={
            **payload,
            "workspace_id": other["id"],
        })
        assert cross_workspace.status_code == 404

        unknown = client.post("/api/topic-radar/favorites/source-snapshot", json={
            **payload,
            "source": {**payload["source"], "url": "https://www.xiaohongshu.com/explore/68c6f07d000000001d01abce"},
        })
        assert unknown.status_code == 404

        invalid_platform = client.post("/api/topic-radar/favorites/source-snapshot", json={
            **payload,
            "source": {**payload["source"], "url": "https://example.test/explore/68c6f07d000000001d01abcd"},
        })
        assert invalid_platform.status_code == 422

class VerificationBrowser:
    def search(self, platform: str, query: str, limit: int = 12):
        from backend.app.platform_browser import PlatformBrowserError
        raise PlatformBrowserError("平台要求完成安全验证，请完成后重试", code="verification_required")


def test_daily_radar_does_not_republish_cached_cards_during_verification(tmp_path: Path):
    db = Database(tmp_path / "cached-radar.sqlite3")
    db.initialize()
    service = TopicRadarService(db=db, public_search=FakePublicSearch(), browser_collector=FakeBrowser())
    workspace = db.create_workspace("双平台缓存")
    db.execute(
        "UPDATE brand_profiles SET profile_json=? WHERE workspace_id=?",
        (json_dumps({"common_info": "我在长沙做相亲、脱单与线下交友内容"}), workspace["id"]),
    )
    monitor = service.ensure_ip_daily_monitor(workspace["id"])
    first = service.run_monitor(monitor["id"], workspace["id"])
    assert {item["platform"] for item in first["items"]} == {"xiaohongshu", "douyin"}

    service.browser = VerificationBrowser()
    second = service.run_monitor(monitor["id"], workspace["id"])
    summary = json_loads(second["summary_json"], {})
    by_platform = {item["platform"]: item for item in summary["platforms"]}

    assert by_platform["xiaohongshu"]["count"] == 0
    assert by_platform["douyin"]["count"] == 0
    assert by_platform["xiaohongshu"]["previously_seen_count"] == 0
    assert by_platform["douyin"]["previously_seen_count"] == 0
    assert by_platform["xiaohongshu"]["status"] == "user_action_required"
    assert by_platform["douyin"]["status"] == "user_action_required"
    assert by_platform["xiaohongshu"]["source_mode"] == "live_browser_unverified"
    assert by_platform["douyin"]["source_mode"] == "live_browser_unverified"
    assert second["status"] == "FAILED"
    assert second["items"] == []
    assert summary["recommendations"] == []


def test_daily_radar_retries_a_transient_background_page_failure(tmp_path: Path):
    from backend.app.platform_browser import PlatformBrowserError

    class RetryOnceBrowser(FakeBrowser):
        def __init__(self) -> None:
            super().__init__()
            self.attempts: dict[str, int] = {}

        def search(self, platform: str, query: str, limit: int = 12):
            self.attempts[platform] = self.attempts.get(platform, 0) + 1
            if self.attempts[platform] == 1:
                raise PlatformBrowserError("后台页面短暂超时", code="page_timeout")
            return super().search(platform, query, limit)

    db = Database(tmp_path / "retry-live.sqlite3")
    db.initialize()
    browser = RetryOnceBrowser()
    service = radar_service(db, browser)
    workspace = db.create_workspace("短暂页面故障")
    enable_fake_jev(db, workspace["id"])
    db.execute(
        "UPDATE brand_profiles SET profile_json=? WHERE workspace_id=?",
        (json_dumps({"common_info": "我在长沙做相亲、脱单与线下交友内容"}), workspace["id"]),
    )

    run = service.run_monitor(service.ensure_ip_daily_monitor(workspace["id"])["id"], workspace["id"])
    summary = json_loads(run["summary_json"], {})

    assert run["status"] == "DONE"
    assert browser.attempts == {"xiaohongshu": 2, "douyin": 2}
    assert all(item["live_verified"] is True for item in summary["platforms"])
    assert all(item["attempt_count"] == 2 for item in summary["platforms"])
    assert all(item["source_mode"] == "managed_background_browser" for item in run["items"])


def test_daily_radar_never_uses_cache_after_transient_page_failure(tmp_path: Path):
    from backend.app.platform_browser import PlatformBrowserError

    class AlwaysTransientBrowser:
        def __init__(self) -> None:
            self.calls = 0

        def search(self, platform: str, query: str, limit: int = 12):
            self.calls += 1
            raise PlatformBrowserError("后台读取页面失败", code="page_read_error")

    db = Database(tmp_path / "transient-no-cache.sqlite3")
    db.initialize()
    service = TopicRadarService(db=db, public_search=FakePublicSearch(), browser_collector=FakeBrowser())
    workspace = db.create_workspace("不能伪装刷新")
    db.execute(
        "UPDATE brand_profiles SET profile_json=? WHERE workspace_id=?",
        (json_dumps({"common_info": "我在长沙做相亲、脱单与线下交友内容"}), workspace["id"]),
    )
    monitor = service.ensure_ip_daily_monitor(workspace["id"])
    first = service.run_monitor(monitor["id"], workspace["id"])
    assert first["items"]

    failed_browser = AlwaysTransientBrowser()
    service.browser = failed_browser
    second = service.run_monitor(monitor["id"], workspace["id"])
    summary = json_loads(second["summary_json"], {})

    assert second["status"] == "FAILED"
    assert second["items"] == []
    assert failed_browser.calls == 4  # one bounded retry per platform
    assert all(item["source_mode"] == "live_browser_unverified" for item in summary["platforms"])
    assert all(item["previously_seen_count"] == 0 for item in summary["platforms"])


def test_radar_favorite_is_workspace_scoped_and_survives_later_runs(tmp_path: Path):
    db = Database(tmp_path / "favorite-radar.sqlite3")
    db.initialize()
    service = TopicRadarService(db=db, public_search=FakePublicSearch(), browser_collector=FakeBrowser())
    workspace = db.create_workspace("收藏工作区")
    other = db.create_workspace("其他工作区")
    db.execute(
        "UPDATE brand_profiles SET profile_json=? WHERE workspace_id=?",
        (json_dumps({"common_info": "我在长沙做相亲、脱单与线下交友内容"}), workspace["id"]),
    )
    monitor = service.ensure_ip_daily_monitor(workspace["id"])
    run = service.run_monitor(monitor["id"], workspace["id"])
    source = run["items"][0]

    favorite = service.favorite_source(workspace["id"], source_item_id=source["id"])
    assert favorite["title"] == source["title"]
    assert len(service.list_favorites(workspace["id"])) == 1
    # An item from another workspace cannot be used to create a favorite here.
    with pytest.raises(ValueError, match="不属于"):
        service.favorite_source(other["id"], source_item_id=source["id"])

    service.run_monitor(monitor["id"], workspace["id"])
    assert len(service.list_favorites(workspace["id"])) == 1
    assert service.remove_favorite(workspace["id"], favorite["id"]) is True
    assert service.list_favorites(workspace["id"]) == []


def test_radar_recovers_running_rows_left_by_a_stopped_server(tmp_path: Path):
    db = Database(tmp_path / "interrupted-radar.sqlite3")
    db.initialize()
    service = TopicRadarService(db=db, public_search=FakePublicSearch(), browser_collector=FakeBrowser())
    workspace = db.create_workspace("中断恢复")
    monitor = service.ensure_ip_daily_monitor(workspace["id"])
    db.execute(
        "INSERT INTO topic_monitor_runs(id,monitor_id,workspace_id,status,started_at) VALUES(?,?,?,'RUNNING',?)",
        ("monitor_run_interrupted", monitor["id"], workspace["id"], "2026-09-15T00:00:00+00:00"),
    )
    db.execute("UPDATE topic_monitors SET last_status='RUNNING' WHERE id=?", (monitor["id"],))

    assert service.recover_interrupted_runs() == 1
    recovered = service.get_run("monitor_run_interrupted", workspace["id"])
    assert recovered["status"] == "FAILED"
    assert recovered["completed_at"]
    assert "工作台重启" in recovered["error_message"]
