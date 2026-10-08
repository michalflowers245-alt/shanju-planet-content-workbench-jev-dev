from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app import main as main_module
from backend.app.db import Database
from backend.app.douyin_extractions import DouyinExtractionService


VIDEO_ID = "7450123456789012345"
VIDEO_URL = f"https://www.douyin.com/video/{VIDEO_ID}"


class VerifiedCollector:
    def extract_douyin_detail(self, url: str, **_kwargs):
        assert url == VIDEO_URL
        return {
            "url": url,
            "video_id": VIDEO_ID,
            "observed_video_id": VIDEO_ID,
            "title": "真实详情标题",
            "published_body": "平台发布文案。",
            "published_body_source": "详情页结构化发布文案",
            "status": "COMPLETE",
            "asr_status": "NOT_ATTEMPTED",
            "video_status": "MEDIA_UNAVAILABLE",
        }


def test_local_asr_endpoint_requires_explicit_same_video_confirmation_and_persists_only_transcript(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = Database(tmp_path / "local-asr-api.sqlite3")
    db.initialize()
    workspace = db.create_workspace("本机转写")
    service = DouyinExtractionService(db=db, collector=VerifiedCollector())
    service.extract(workspace["id"], VIDEO_URL)

    class NoWorkers:
        def start(self): pass
        def stop(self): pass

    monkeypatch.setenv("WORKBENCH_DISABLE_WORKERS", "1")
    monkeypatch.setattr(main_module, "database", db)
    monkeypatch.setattr(main_module, "douyin_extraction_service", service)
    monkeypatch.setattr(main_module, "topic_radar_worker", NoWorkers())
    monkeypatch.setattr(main_module, "transcribe_uploaded_media", lambda stream, _name: {
        "text": "本机音频中的口播。",
        "segments": [{"text": "本机音频中的口播。", "start_seconds": 0.0, "end_seconds": 3.0, "confidence": 0.9}],
        "confidence": {"mean": 0.9, "minimum": 0.9, "maximum": 0.9, "assessed_segment_count": 1, "low_confidence_threshold": 0.65},
        "low_confidence": [],
        "language": "zh-CN",
        "engine": "Apple SpeechAnalyzer (offline, on-device)",
        "model": "SpeechTranscriber zh-CN",
        "source": "USER_PROVIDED_LOCAL_MEDIA",
        "media_sha256": "b" * 64,
        "message": "文件由用户选择并确认。",
    })

    with TestClient(main_module.app) as client:
        rejected = client.post(
            "/api/topic-radar/douyin/transcribe-local",
            data={"workspace_id": workspace["id"], "url": VIDEO_URL},
            files={"file": ("clip.mp4", b"media", "video/mp4")},
        )
        assert rejected.status_code == 422

        response = client.post(
            "/api/topic-radar/douyin/transcribe-local",
            data={"workspace_id": workspace["id"], "url": VIDEO_URL, "confirm_selected_media": "true"},
            files={"file": ("clip.mp4", b"media", "video/mp4")},
        )
        backfill = client.post(
            f"/api/topic-radar/douyin/extractions/retry-missing-transcripts?workspace_id={workspace['id']}"
        )

    assert response.status_code == 200, response.text
    extraction = response.json()["extraction"]
    assert extraction["video_id"] == extraction["observed_video_id"] == VIDEO_ID
    assert extraction["published_body"] == "平台发布文案。"
    assert extraction["asr_text"] == "本机音频中的口播。"
    assert extraction["asr_source"] == "USER_PROVIDED_LOCAL_MEDIA"
    assert extraction["asr_media_sha256"] == "b" * 64
    assert extraction["asr_segments"][0]["end_seconds"] == 3.0
    assert extraction["asr_confidence"]["mean"] == 0.9
    assert extraction["asr_low_confidence"] == []
    assert backfill.status_code == 200
    assert backfill.json() == {"queued": 0}
