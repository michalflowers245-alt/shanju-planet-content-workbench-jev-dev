from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import backend.app.main as main_module
import backend.app.rewrite as rewrite_module
from backend.app.db import Database, json_dumps, utc_now
from backend.app.providers import ModelResult
from backend.app.rewrite import RewriteError, RewriteService
from scripts.migrate_rewrite_v1 import migrate


POST_ID = "68c6f07d000000001d01abcd"
POST_URL = f"https://www.xiaohongshu.com/explore/{POST_ID}"
RAW_BODY = "  第一行保留空格  \r\n\n第二行🙂\n#原帖话题"


class RewriteModels:
    def __init__(self, *, invalid_stage: str = "") -> None:
        self.invalid_stage = invalid_stage
        self.calls: list[str] = []

    def generate_json(self, workspace_id, task_id, role_key, system_prompt, payload, **kwargs):
        self.calls.append(role_key)
        count = int(payload["request"]["image_count"])
        if role_key == "rewrite_analysis":
            data = {
                "core_topic": "把一个真实的困境讲清楚",
                "target_audience": "正在犹豫的普通读者",
                "emotional_hook": "先承认自己卡住的那个瞬间",
                "original_structure": [{"section": "开头", "purpose": "进入场景", "evidence_from_source": "正文首段"}],
                "transferable_elements": ["具体场景"],
                "must_avoid": ["复写原句"],
                "source_fidelity_notes": ["逐字原文已校验"],
            }
        elif role_key == "rewrite_strategy":
            data = {
                "rewritten_angle": "从今天能做的一步切入",
                "target_reader": "需要实际行动的人",
                "differentiation": "使用新的叙事和观察，而不复制原帖表达",
                "narrative_arc": [{"step": "开场", "purpose": "让读者认出处境", "reader_takeaway": "知道问题在哪"}],
                "style_rules": ["说人话"],
                "source_usage_plan": ["仅借鉴问题意识"],
                "boundaries": ["不编造经历"],
            }
        elif role_key == "rewrite_copy":
            data = {
                "title": "你真正卡住的，可能不是能力",
                "alternative_titles": ["先把这一步做清楚", "别急着把自己逼太紧"],
                "body": "我最近发现，很多人不是做不到，而是不知道从哪里开始。\n\n先把眼前这一件小事做完，再决定下一步。",
                "tags": ["成长", "内容创作", "生活观察"],
                "comment_cta": "你最近最想先做清楚的是哪一步？",
                "cover_copy": "先把这一步做清楚",
                "copy_segments": [
                    {"id": "seg_01", "label": "开场", "text": "很多人不是做不到。"},
                    {"id": "seg_02", "label": "行动", "text": "先把小事做完。"},
                ],
            }
        elif role_key == "rewrite_image_plan":
            data = {
                "style_bible": {
                    "visual_tone": "自然日常、克制明亮",
                    "palette": ["奶油白", "浅粉", "木色"],
                    "lighting": "午后窗边自然光",
                    "continuity_rules": ["同一室内空间", "同一人物不正脸"],
                    "avoid": ["库存图感", "过度摆拍"],
                },
                "image_cards": [
                    {
                        "id": f"img_{index:02d}",
                        "ordinal": index,
                        "role": "cover" if index == 1 else "scene",
                        "caption": f"第 {index} 张画面",
                        "prompt": f"生活感场景 {index}，自然光，统一色调",
                        "related_copy_segment": "seg_01" if index % 2 else "seg_02",
                        "visual_notes": "保持前后画面连续。",
                    }
                    for index in range(1, count + 1)
                ],
            }
        else:
            data = {
                "decision": "PASS",
                "quality_checks": {"source_boundary": "pass", "image_mapping": "pass"},
                "issues": [],
                "corrections": [],
                "final_notes": "内容已核对为独立新稿，配图与正文段落相连。",
            }
        if self.invalid_stage and role_key == self.invalid_stage:
            if role_key == "rewrite_image_plan":
                data["image_cards"][0]["related_copy_segment"] = "seg_does_not_exist"
            else:
                data.pop("title", None)
        return ModelResult(data, "fake-model", 10, 20, 30, 5)


class VisionRewriteModels(RewriteModels):
    def __init__(self) -> None:
        super().__init__()
        self.image_inputs: list[str] | None = None

    def resolve_profile(self, workspace_id: str, role_key: str):
        return {"provider": "deepseek", "model": "deepseek-v4-flash-vision-exp"}

    def generate_json(self, workspace_id, task_id, role_key, system_prompt, payload, **kwargs):
        if role_key == "rewrite_analysis":
            self.image_inputs = kwargs.get("image_inputs")
        return super().generate_json(workspace_id, task_id, role_key, system_prompt, payload, **kwargs)


def setup_source(db: Database, *, verified: bool = True, other_workspace: bool = False) -> tuple[dict, str, str]:
    workspace = db.create_workspace("改写测试")
    extraction_id = "xhs_rewrite_source"
    archive_id = "xhsa_rewrite_source"
    now = utc_now()
    db.execute(
        """INSERT INTO xhs_post_extractions
        (id,workspace_id,url,post_key,title,author,tags_json,metrics_json,status,extracted_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,'success',?,?)""",
        (extraction_id, workspace["id"], POST_URL, POST_ID, "一篇可验证的原帖", "原帖作者", json_dumps(["原话题"]), json_dumps({"likes": 888}), now, now),
    )
    db.execute(
        """INSERT INTO xhs_archive_versions
        (id,workspace_id,extraction_id,post_id,observed_post_id,archive_path,body_raw,body_normalized,body_source,
         body_evidence_json,archive_status,verification_status,captured_at,collector_version)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            archive_id, workspace["id"], extraction_id, POST_ID, POST_ID, "test/archive", RAW_BODY,
            "第一行保留空格\n第二行🙂\n#原帖话题", "embedded_state", json_dumps({"same_post_id": True}),
            "COMPLETE", "VERIFIED" if verified else "UNVERIFIED", now, "test",
        ),
    )
    for ordinal in range(1, 3):
        db.execute(
            """INSERT INTO xhs_post_assets
            (id,workspace_id,archive_id,role,ordinal,source_ref,source_kind,source_host,rendition,mime_detected,
             extension,width,height,byte_size,sha256,relative_path,status,evidence_json,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                f"asset_{ordinal}", workspace["id"], archive_id, "carousel", ordinal,
                f"https://cdn.example.test/{ordinal}.jpg", "network_response", "cdn.example.test", "browser_returned",
                "image/jpeg", "jpg", 1080, 1440, 12, f"sha{ordinal}", f"images/{ordinal}.jpg", "SAVED", "{}", now,
            ),
        )
    if other_workspace:
        db.create_workspace("其他工作区")
    return workspace, extraction_id, archive_id


def test_rewrite_job_preserves_verified_raw_source_and_generates_full_package(tmp_path: Path):
    db = Database(tmp_path / "rewrite.sqlite3")
    db.initialize()
    workspace, extraction_id, archive_id = setup_source(db)
    models = RewriteModels()
    service = RewriteService(db, models)

    queued = service.create(
        workspace["id"], extraction_id, archive_id=archive_id,
        options={"target_platform": "xiaohongshu", "rewrite_strength": "deep", "persona": "真实生活方式创作者", "image_count": 4},
    )
    assert queued["source_snapshot"]["body_raw"] == RAW_BODY
    assert queued["source_snapshot"]["body_sha256"]
    assert queued["source_snapshot"]["saved_image_count"] == 2

    completed = service.run_job(queued["id"])
    assert completed and completed["status"] == "COMPLETED"
    assert completed["result"]["copy_generation"]["title"]
    assert len(completed["result"]["image_plan_generation"]["image_cards"]) == 4
    assert completed["result"]["review"]["decision"] == "PASS"
    assert models.calls == ["rewrite_analysis", "rewrite_strategy", "rewrite_copy", "rewrite_image_plan", "rewrite_review"]
    assert db.one("SELECT body_raw FROM xhs_archive_versions WHERE id=?", (archive_id,))["body_raw"] == RAW_BODY

    filename, markdown = service.export_markdown(workspace["id"], queued["id"])
    assert filename.endswith(".md")
    assert "系列配图方案" in markdown
    assert "原帖正文哈希" in markdown


def test_rewrite_regenerate_invalidates_dependent_outputs_but_keeps_source_snapshot(tmp_path: Path):
    db = Database(tmp_path / "rewrite-revision.sqlite3")
    db.initialize()
    workspace, extraction_id, archive_id = setup_source(db)
    service = RewriteService(db, RewriteModels())
    job = service.create(workspace["id"], extraction_id, archive_id=archive_id, options={"image_count": 3})
    service.run_job(job["id"])

    regenerated = service.regenerate(workspace["id"], job["id"], "copy_generation")
    assert regenerated["revision"] == 2
    assert "analysis" in regenerated["result"]
    assert "strategy" in regenerated["result"]
    assert "copy_generation" not in regenerated["result"]
    assert regenerated["source_snapshot"]["body_raw"] == RAW_BODY

    completed = service.run_job(job["id"], from_stage="copy_generation")
    assert completed and completed["status"] == "COMPLETED"
    stages = [step["stage"] for step in completed["steps"] if step["revision"] == 2]
    assert stages == ["copy_generation", "image_plan_generation", "review"]


def test_rewrite_rejects_unverified_or_cross_workspace_sources(tmp_path: Path):
    db = Database(tmp_path / "rewrite-safety.sqlite3")
    db.initialize()
    workspace, extraction_id, archive_id = setup_source(db, verified=False, other_workspace=True)
    service = RewriteService(db, RewriteModels())
    with pytest.raises(RewriteError, match="尚未通过归档校验"):
        service.create(workspace["id"], extraction_id, archive_id=archive_id)
    other = db.one("SELECT id FROM workspaces WHERE id!=? ORDER BY created_at DESC LIMIT 1", (workspace["id"],))
    with pytest.raises(RewriteError, match="找不到"):
        service.create(other["id"], extraction_id, archive_id=archive_id)


def test_rewrite_invalid_structured_model_output_is_not_published(tmp_path: Path):
    db = Database(tmp_path / "rewrite-invalid.sqlite3")
    db.initialize()
    workspace, extraction_id, archive_id = setup_source(db)
    service = RewriteService(db, RewriteModels(invalid_stage="rewrite_copy"))
    job = service.create(workspace["id"], extraction_id, archive_id=archive_id, options={"image_count": 3})
    failed = service.run_job(job["id"])
    assert failed and failed["status"] == "FAILED"
    assert failed["stage"] == "copy_generation"
    assert "copy_generation" not in failed["result"]
    step = [value for value in failed["steps"] if value["stage"] == "copy_generation"][0]
    assert step["status"] == "FAILED"


def test_regenerated_image_plan_cannot_reference_a_missing_copy_segment(tmp_path: Path):
    db = Database(tmp_path / "rewrite-image-segment.sqlite3")
    db.initialize()
    workspace, extraction_id, archive_id = setup_source(db)
    service = RewriteService(db, RewriteModels())
    job = service.create(workspace["id"], extraction_id, archive_id=archive_id, options={"image_count": 3})
    completed = service.run_job(job["id"])
    assert completed and completed["status"] == "COMPLETED"

    # A later image-plan regeneration must not retain the old plan if the
    # model points a card at a segment that does not exist in the frozen copy.
    service.models = RewriteModels(invalid_stage="rewrite_image_plan")
    regenerated = service.regenerate(workspace["id"], job["id"], "image_plan_generation")
    assert "image_plan_generation" not in regenerated["result"]
    assert "review" not in regenerated["result"]

    failed = service.run_job(job["id"], from_stage="image_plan_generation")
    assert failed and failed["status"] == "FAILED"
    assert failed["stage"] == "image_plan_generation"
    assert "image_plan_generation" not in failed["result"]
    assert "review" not in failed["result"]
    latest_step = next(
        step for step in failed["steps"]
        if step["stage"] == "image_plan_generation" and step["revision"] == regenerated["revision"]
    )
    assert latest_step["status"] == "FAILED"
    assert "关联的正文分段不存在" in (latest_step["error_message"] or "")


def test_rewrite_uses_in_memory_visual_previews_only_for_a_vision_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from PIL import Image

    db = Database(tmp_path / "rewrite-vision.sqlite3")
    db.initialize()
    workspace, extraction_id, archive_id = setup_source(db)
    archive_root = tmp_path / "xhs_archives" / "test" / "archive"
    image_path = archive_root / "images" / "1.jpg"
    image_path.parent.mkdir(parents=True)
    Image.new("RGB", (40, 50), (220, 120, 150)).save(image_path, format="JPEG")
    db.execute("UPDATE xhs_archive_versions SET archive_path=? WHERE id=?", (str(archive_root), archive_id))
    db.execute("DELETE FROM xhs_post_assets WHERE archive_id=? AND ordinal>1", (archive_id,))
    monkeypatch.setattr(rewrite_module, "XHS_ARCHIVE_DIR", tmp_path / "xhs_archives")
    models = VisionRewriteModels()
    service = RewriteService(db, models)
    job = service.create(workspace["id"], extraction_id, archive_id=archive_id, options={"image_count": 3})
    result = service.run_job(job["id"])

    assert result and result["status"] == "COMPLETED"
    assert models.image_inputs and models.image_inputs[0].startswith("data:image/jpeg;base64,")
    # Preview bytes are not persisted in the job snapshot or stage inputs.
    saved = service.get(workspace["id"], job["id"])
    assert "data:image" not in str(saved)
    assert image_path.read_bytes()


def test_rewrite_http_api_create_get_regenerate_and_export(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = Database(tmp_path / "rewrite-api.sqlite3")
    db.initialize()
    workspace, extraction_id, archive_id = setup_source(db)
    service = RewriteService(db, RewriteModels())

    class Worker:
        def start(self): pass
        def stop(self): pass
        def wake(self): pass

    monkeypatch.setenv("WORKBENCH_DISABLE_WORKERS", "1")
    monkeypatch.setattr(main_module, "database", db)
    monkeypatch.setattr(main_module, "rewrite_service", service)
    monkeypatch.setattr(main_module, "topic_radar_worker", Worker())

    with TestClient(main_module.app) as client:
        created = client.post("/api/rewrite/jobs", json={
            "workspace_id": workspace["id"], "extraction_id": extraction_id, "archive_id": archive_id,
            "target_platform": "xiaohongshu", "rewrite_strength": "balanced", "persona": "测试创作者", "image_count": 3,
        })
        assert created.status_code == 202, created.text
        job_id = created.json()["id"]
        service.run_job(job_id)
        read = client.get(f"/api/rewrite/jobs/{job_id}", params={"workspace_id": workspace["id"]})
        assert read.status_code == 200
        assert read.json()["status"] == "COMPLETED"
        export = client.get(f"/api/rewrite/jobs/{job_id}/export", params={"workspace_id": workspace["id"]})
        assert export.status_code == 200
        assert "text/markdown" in export.headers["content-type"]
        regenerate = client.post(f"/api/rewrite/jobs/{job_id}/regenerate", json={"workspace_id": workspace["id"], "stage": "review"})
        assert regenerate.status_code == 202
        assert regenerate.json()["status"] == "QUEUED"


def test_rewrite_migration_is_idempotent_on_an_isolated_database_copy(tmp_path: Path):
    path = tmp_path / "rewrite-migration.sqlite3"
    db = Database(path)
    db.initialize()
    first = migrate(path)
    second = migrate(path)
    assert first["integrity"] == "ok"
    assert second["integrity"] == "ok"
    assert first["schema_version"] == "10"
    with db.connect() as connection:
        assert connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='rewrite_jobs'").fetchone()
        assert "rewrite_job_id" in {row[1] for row in connection.execute("PRAGMA table_info(usage_calls)")}
