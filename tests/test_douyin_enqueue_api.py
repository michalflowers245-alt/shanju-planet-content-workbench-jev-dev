from fastapi.testclient import TestClient

from backend.app import main as main_module
from backend.app.db import Database
from backend.app.douyin_extractions import DouyinExtractionService


def test_background_extract_returns_persisted_job_without_waiting_for_browser(tmp_path, monkeypatch):
    db = Database(tmp_path / "queue-api.sqlite3")
    db.initialize()
    workspace = db.create_workspace("后台提取验证")

    class Collector:
        def extract_douyin_detail(self, *args, **kwargs):
            raise AssertionError("HTTP handler must not wait for the browser")

    service = DouyinExtractionService(db=db, collector=Collector())
    monkeypatch.setattr(main_module, "database", db)
    monkeypatch.setattr(main_module, "douyin_extraction_service", service)
    url = "https://www.douyin.com/video/7450123456789012345"
    payload = {"workspace_id": workspace["id"], "url": url, "title_hint": "候选标题"}
    # No lifespan here: this test deliberately keeps the worker stopped.
    client = TestClient(main_module.app)
    try:
        result = client.post("/api/topic-radar/douyin/extract?background=true", json=payload)
        assert result.status_code == 202, result.text
        body = result.json()
        assert body["queued"] is True
        assert body["extraction"]["status"] == "QUEUED"
        assert body["extraction"]["published_body"] == ""
        repeat = client.post("/api/topic-radar/douyin/extract?background=true", json=payload)
        assert repeat.status_code == 202
        assert repeat.json()["queued"] is False
        assert repeat.json()["extraction"]["id"] == body["extraction"]["id"]
        listed = client.get("/api/topic-radar/douyin/extractions", params={"workspace_id": workspace["id"]})
        assert listed.status_code == 200
        assert listed.json()[0]["id"] == body["extraction"]["id"]
        invalid = client.post("/api/topic-radar/douyin/extract?background=true", json={**payload, "url": "https://www.douyin.com/"})
        assert invalid.status_code == 422
    finally:
        client.close()
