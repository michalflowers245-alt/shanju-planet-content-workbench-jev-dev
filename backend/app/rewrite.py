from __future__ import annotations

"""A source-preserving, staged Xiaohongshu rewrite workflow.

This workflow deliberately does not reuse ``copy_tasks``.  A rewrite starts
from an immutable, verified archive snapshot and carries its own stages,
revisions and exports.  That separation is important: original copy is
evidence, while the generated package is a new work and must never overwrite
or disguise the source.
"""

import base64
import hashlib
import io
import json
import queue
import threading
import urllib.parse
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Iterable

from .config import XHS_ARCHIVE_DIR
from .db import Database, database, json_dumps, json_loads, new_id, utc_now
from .providers import DEEPSEEK_PROVIDER, DEEPSEEK_VISION_MODELS, ModelGateway, ModelResult, ProviderError, model_gateway


STAGES = ("analysis", "strategy", "copy_generation", "image_plan_generation", "review")
STAGE_ROLES = {
    "analysis": "rewrite_analysis",
    "strategy": "rewrite_strategy",
    "copy_generation": "rewrite_copy",
    "image_plan_generation": "rewrite_image_plan",
    "review": "rewrite_review",
}
PROMPT_VERSION = "rewrite-v1"


class RewriteError(RuntimeError):
    """A user-facing rewrite failure that is safe to show in the workbench."""


class RewriteValidationError(RewriteError):
    pass


@dataclass(frozen=True)
class StageDefinition:
    name: str
    schema: dict[str, Any]
    system_prompt: str


_COMMON = """
你是内容改写工作台中的一个严谨环节。输入里的 source_snapshot 是用户已归档的原帖资料；
它只能用于理解事实、结构、情绪和视觉线索，绝不能把原帖原句、标题句式、段落顺序或作者口吻近似复写进新内容。
不虚构原帖没有写过的事实、人物经历、数据、效果、品牌承诺或评论反馈。不得把 OCR、摘要、字幕当作原文。
输出必须是中文、自然、具体、可直接给人使用的内容。避免空泛鸡汤、夸张承诺、套路化"不是…而是…"句式和 AI 腔。
""".strip()


def _schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": required,
    }


STAGE_DEFINITIONS: dict[str, StageDefinition] = {
    "analysis": StageDefinition(
        "analysis",
        _schema(
            {
                "core_topic": {"type": "string"},
                "target_audience": {"type": "string"},
                "emotional_hook": {"type": "string"},
                "original_structure": {"type": "array", "items": {"type": "object"}},
                "transferable_elements": {"type": "array", "items": {"type": "string"}},
                "must_avoid": {"type": "array", "items": {"type": "string"}},
                "source_fidelity_notes": {"type": "array", "items": {"type": "string"}},
            },
            ["core_topic", "target_audience", "emotional_hook", "original_structure", "transferable_elements", "must_avoid", "source_fidelity_notes"],
        ),
        _COMMON + "\n\n阶段：分析。只分析原帖的主题、节奏、受众和可迁移的表达机制，不生成新文案。"
        "如果 payload 中标记已提供视觉参考图，请同时观察画面内容、构图、色调和连贯性；如果只提供了图片清单，"
        "只能基于清单说图片资料可用，不能假装看过图片内容。"
        " original_structure 的每项包含 section、purpose、evidence_from_source。",
    ),
    "strategy": StageDefinition(
        "strategy",
        _schema(
            {
                "rewritten_angle": {"type": "string"},
                "target_reader": {"type": "string"},
                "differentiation": {"type": "string"},
                "narrative_arc": {"type": "array", "items": {"type": "object"}},
                "style_rules": {"type": "array", "items": {"type": "string"}},
                "source_usage_plan": {"type": "array", "items": {"type": "string"}},
                "boundaries": {"type": "array", "items": {"type": "string"}},
            },
            ["rewritten_angle", "target_reader", "differentiation", "narrative_arc", "style_rules", "source_usage_plan", "boundaries"],
        ),
        _COMMON + "\n\n阶段：策略。基于分析决定新的切口、人设、平台结构和不能越过的事实边界。"
        " narrative_arc 每项包含 step、purpose、reader_takeaway。",
    ),
    "copy_generation": StageDefinition(
        "copy_generation",
        _schema(
            {
                "title": {"type": "string"},
                "alternative_titles": {"type": "array", "items": {"type": "string"}},
                "body": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "comment_cta": {"type": "string"},
                "cover_copy": {"type": "string"},
                "copy_segments": {"type": "array", "items": {"type": "object"}},
            },
            ["title", "alternative_titles", "body", "tags", "comment_cta", "cover_copy", "copy_segments"],
        ),
        _COMMON + "\n\n阶段：完整文案。写一篇全新、可发布的内容，不得改写成原帖的同义替换。"
        "body 必须有清楚分段和自然换行。copy_segments 按正文真实段落切分，每项包含 id、label、text。",
    ),
    "image_plan_generation": StageDefinition(
        "image_plan_generation",
        _schema(
            {
                "style_bible": {"type": "object"},
                "image_cards": {"type": "array", "items": {"type": "object"}},
            },
            ["style_bible", "image_cards"],
        ),
        _COMMON + "\n\n阶段：系列配图策划。不要声称已经生成图片。给出一套生活感、统一、连续的视觉方案，"
        "避免库存图感、过度摆拍、无关文字海报。style_bible 必须包含 visual_tone、palette、lighting、"
        "continuity_rules、avoid。image_cards 必须逐张包含 id、ordinal、role、caption、prompt、related_copy_segment、visual_notes。"
        "角色可以是 cover、scene、detail、quote、ending；每张都对应 copy_segments 的一个 id。",
    ),
    "review": StageDefinition(
        "review",
        _schema(
            {
                "decision": {"type": "string"},
                "quality_checks": {"type": "object"},
                "issues": {"type": "array", "items": {"type": "string"}},
                "corrections": {"type": "array", "items": {"type": "string"}},
                "final_notes": {"type": "string"},
            },
            ["decision", "quality_checks", "issues", "corrections", "final_notes"],
        ),
        _COMMON + "\n\n阶段：审核。核查事实边界、与原帖的相似风险、自然表达、平台可读性、"
        "标题/正文/标签/评论引导/封面字是否齐全，以及每张图是否对应正文。decision 只能是 PASS 或 REVISE。",
    ),
}


def _safe_text(value: Any, field: str, *, max_length: int = 30000) -> str:
    if not isinstance(value, str):
        raise RewriteValidationError(f"模型返回的 {field} 不是文字")
    if not value.strip():
        raise RewriteValidationError(f"模型返回的 {field} 为空")
    if len(value) > max_length:
        raise RewriteValidationError(f"模型返回的 {field} 过长")
    return value


def _safe_string_list(value: Any, field: str, *, min_items: int = 0, max_items: int = 30) -> list[str]:
    if not isinstance(value, list):
        raise RewriteValidationError(f"模型返回的 {field} 不是列表")
    clean = [str(item).strip() for item in value if isinstance(item, str) and item.strip()]
    if len(clean) < min_items:
        raise RewriteValidationError(f"模型返回的 {field} 内容不足")
    return clean[:max_items]


def _safe_object_list(value: Any, field: str, *, min_items: int = 1, max_items: int = 30) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise RewriteValidationError(f"模型返回的 {field} 不是列表")
    clean = [dict(item) for item in value if isinstance(item, dict)]
    if len(clean) < min_items:
        raise RewriteValidationError(f"模型返回的 {field} 内容不足")
    return clean[:max_items]


def _validate_output(
    stage: str,
    data: Any,
    image_count: int,
    *,
    copy_segment_ids: set[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise RewriteValidationError("模型返回的改写结果不是对象")
    if stage == "analysis":
        return {
            "core_topic": _safe_text(data.get("core_topic"), "核心主题", max_length=500),
            "target_audience": _safe_text(data.get("target_audience"), "目标受众", max_length=500),
            "emotional_hook": _safe_text(data.get("emotional_hook"), "情绪切口", max_length=1000),
            "original_structure": _safe_object_list(data.get("original_structure"), "原帖结构"),
            "transferable_elements": _safe_string_list(data.get("transferable_elements"), "可迁移元素", min_items=1),
            "must_avoid": _safe_string_list(data.get("must_avoid"), "避免项", min_items=1),
            "source_fidelity_notes": _safe_string_list(data.get("source_fidelity_notes"), "原帖证据说明", min_items=1),
        }
    if stage == "strategy":
        return {
            "rewritten_angle": _safe_text(data.get("rewritten_angle"), "改写角度", max_length=1000),
            "target_reader": _safe_text(data.get("target_reader"), "目标读者", max_length=500),
            "differentiation": _safe_text(data.get("differentiation"), "差异化", max_length=1000),
            "narrative_arc": _safe_object_list(data.get("narrative_arc"), "叙事结构"),
            "style_rules": _safe_string_list(data.get("style_rules"), "表达规则", min_items=1),
            "source_usage_plan": _safe_string_list(data.get("source_usage_plan"), "原帖使用计划", min_items=1),
            "boundaries": _safe_string_list(data.get("boundaries"), "边界", min_items=1),
        }
    if stage == "copy_generation":
        segments = _safe_object_list(data.get("copy_segments"), "正文分段", min_items=2, max_items=20)
        seen: set[str] = set()
        normalized: list[dict[str, str]] = []
        for index, segment in enumerate(segments, start=1):
            segment_id = _safe_text(segment.get("id"), f"正文分段 {index} ID", max_length=80)
            if segment_id in seen:
                raise RewriteValidationError("正文分段 ID 不能重复")
            seen.add(segment_id)
            normalized.append({
                "id": segment_id,
                "label": _safe_text(segment.get("label"), f"正文分段 {index} 标题", max_length=120),
                "text": _safe_text(segment.get("text"), f"正文分段 {index} 内容", max_length=8000),
            })
        return {
            "title": _safe_text(data.get("title"), "标题", max_length=100),
            "alternative_titles": _safe_string_list(data.get("alternative_titles"), "备选标题", min_items=2, max_items=8),
            "body": _safe_text(data.get("body"), "正文", max_length=30000),
            "tags": _safe_string_list(data.get("tags"), "标签", min_items=3, max_items=12),
            "comment_cta": _safe_text(data.get("comment_cta"), "评论引导", max_length=500),
            "cover_copy": _safe_text(data.get("cover_copy"), "封面文案", max_length=100),
            "copy_segments": normalized,
        }
    if stage == "image_plan_generation":
        style = data.get("style_bible")
        if not isinstance(style, dict):
            raise RewriteValidationError("模型返回的视觉风格不是对象")
        # A series image plan is only useful when every card points to a real
        # section of the generated copy.  Do not allow a model to invent an
        # arbitrary segment ID and leave the UI with an untraceable card.
        valid_segment_ids = copy_segment_ids or set()
        if not valid_segment_ids:
            raise RewriteValidationError("配图方案缺少可关联的正文分段")
        cards = _safe_object_list(data.get("image_cards"), "配图卡", min_items=image_count, max_items=12)
        if len(cards) != image_count:
            raise RewriteValidationError(f"需要 {image_count} 张配图方案，模型返回了 {len(cards)} 张")
        normalized_cards: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for index, card in enumerate(cards, start=1):
            card_id = _safe_text(card.get("id"), f"配图 {index} ID", max_length=80)
            if card_id in seen_ids:
                raise RewriteValidationError("配图 ID 不能重复")
            seen_ids.add(card_id)
            ordinal = card.get("ordinal")
            if not isinstance(ordinal, int) or ordinal != index:
                raise RewriteValidationError("配图序号必须从 1 开始连续排列")
            related_copy_segment = _safe_text(card.get("related_copy_segment"), f"配图 {index} 正文关联", max_length=80)
            if related_copy_segment not in valid_segment_ids:
                raise RewriteValidationError(f"配图 {index} 关联的正文分段不存在")
            normalized_cards.append({
                "id": card_id,
                "ordinal": ordinal,
                "role": _safe_text(card.get("role"), f"配图 {index} 角色", max_length=40),
                "caption": _safe_text(card.get("caption"), f"配图 {index} 文案", max_length=300),
                "prompt": _safe_text(card.get("prompt"), f"配图 {index} 提示词", max_length=3000),
                "related_copy_segment": related_copy_segment,
                "visual_notes": _safe_text(card.get("visual_notes"), f"配图 {index} 说明", max_length=1000),
            })
        return {
            "style_bible": {
                "visual_tone": _safe_text(style.get("visual_tone"), "视觉基调", max_length=500),
                "palette": _safe_string_list(style.get("palette"), "配色", min_items=2, max_items=8),
                "lighting": _safe_text(style.get("lighting"), "光线", max_length=500),
                "continuity_rules": _safe_string_list(style.get("continuity_rules"), "统一规则", min_items=2),
                "avoid": _safe_string_list(style.get("avoid"), "视觉避免项", min_items=2),
            },
            "image_cards": normalized_cards,
        }
    if stage == "review":
        decision = _safe_text(data.get("decision"), "审核结论", max_length=20).upper()
        if decision not in {"PASS", "REVISE"}:
            raise RewriteValidationError("审核结论只能是 PASS 或 REVISE")
        checks = data.get("quality_checks")
        if not isinstance(checks, dict):
            raise RewriteValidationError("模型返回的质量检查不是对象")
        return {
            "decision": decision,
            "quality_checks": checks,
            "issues": _safe_string_list(data.get("issues"), "审核问题", min_items=0),
            "corrections": _safe_string_list(data.get("corrections"), "修改建议", min_items=0),
            "final_notes": _safe_text(data.get("final_notes"), "审核说明", max_length=2000),
        }
    raise RewriteValidationError("未知的改写阶段")


class RewriteService:
    def __init__(self, db: Database = database, models: ModelGateway = model_gateway) -> None:
        self.db = db
        self.models = models
        self._jobs: queue.Queue[tuple[str, str | None]] = queue.Queue()
        self._queued: set[str] = set()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        # A process may have been closed while a model call was in flight.  No
        # partially generated payload is published; resume from the recorded
        # stage on the next start.
        for row in self.db.all("SELECT id,stage,status FROM rewrite_jobs WHERE status IN ('QUEUED','RUNNING')"):
            stage = str(row.get("stage") or "QUEUED")
            self._put(str(row["id"]), None if stage == "QUEUED" else stage)
        self._thread = threading.Thread(target=self._loop, daemon=True, name="rewrite-workbench-worker")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _put(self, job_id: str, from_stage: str | None) -> bool:
        with self._lock:
            if job_id in self._queued:
                return False
            self._queued.add(job_id)
            self._jobs.put((job_id, from_stage))
        return True

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                job_id, from_stage = self._jobs.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                self.run_job(job_id, from_stage=from_stage)
            finally:
                with self._lock:
                    self._queued.discard(job_id)
                self._jobs.task_done()

    def _archive_source(self, workspace_id: str, extraction_id: str, archive_id: str | None = None) -> dict[str, Any]:
        extraction = self.db.one(
            "SELECT * FROM xhs_post_extractions WHERE workspace_id=? AND id=?",
            (workspace_id, extraction_id),
        )
        if not extraction:
            raise RewriteError("找不到这篇小红书归档记录")
        if archive_id:
            archive = self.db.one(
                "SELECT * FROM xhs_archive_versions WHERE workspace_id=? AND extraction_id=? AND id=?",
                (workspace_id, extraction_id, archive_id),
            )
        else:
            archive = self.db.one(
                """SELECT * FROM xhs_archive_versions WHERE workspace_id=? AND extraction_id=?
                ORDER BY captured_at DESC LIMIT 1""",
                (workspace_id, extraction_id),
            )
        if not archive:
            raise RewriteError("请先完成“保存原图与正文”，再按这篇改写")
        if archive.get("post_id") != archive.get("observed_post_id"):
            raise RewriteError("原帖身份未通过校验，不能用于改写")
        if extraction.get("post_key") and str(extraction.get("post_key")) != str(archive.get("post_id")):
            raise RewriteError("归档帖子与搜索结果身份不一致，不能用于改写")
        raw = archive.get("body_raw")
        if not isinstance(raw, str) or raw == "":
            raise RewriteError("这篇帖子没有可校验的逐字原文；OCR、摘要和字幕不能用于改写")
        if str(archive.get("verification_status") or "") != "VERIFIED":
            raise RewriteError("这篇原文尚未通过归档校验，请先补齐或重新归档")
        assets = self.db.all(
            """SELECT * FROM xhs_post_assets WHERE workspace_id=? AND archive_id=?
            AND role IN ('carousel','cover') ORDER BY CASE role WHEN 'cover' THEN 0 ELSE 1 END, ordinal""",
            (workspace_id, archive["id"]),
        )
        images: list[dict[str, Any]] = []
        for asset in assets:
            if asset.get("status") != "SAVED":
                continue
            # Never store original signed links.  The URL below is an
            # authenticated local workbench route scoped by workspace.
            local_url = (
                f"/api/topic-radar/xhs/archives/{archive['id']}/assets/{asset['id']}"
                f"?workspace_id={urllib.parse.quote(workspace_id)}"
            )
            images.append({
                "asset_id": asset["id"],
                "role": asset["role"],
                "ordinal": int(asset["ordinal"]),
                "mime": asset.get("mime_detected") or asset.get("mime_declared") or "",
                "extension": asset.get("extension") or "",
                "width": asset.get("width"),
                "height": asset.get("height"),
                "byte_size": asset.get("byte_size"),
                "sha256": asset.get("sha256") or "",
                "download_url": local_url,
            })
        expected = extraction.get("expected_asset_count")
        try:
            expected_count = int(expected) if expected is not None else None
        except (TypeError, ValueError):
            expected_count = None
        image_quality = "COMPLETE" if str(archive.get("archive_status")) == "COMPLETE" else "PARTIAL"
        return {
            "extraction_id": extraction["id"],
            "archive_id": archive["id"],
            "post_id": archive["post_id"],
            "title": str(extraction.get("title") or ""),
            "author": str(extraction.get("author") or ""),
            "url": str(extraction.get("url") or ""),
            # Do not normalize or trim raw text.  It is persisted verbatim.
            "body_raw": raw,
            "body_sha256": archive.get("body_sha256") or hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            "body_source": archive.get("body_source") or "",
            "verification_status": archive.get("verification_status") or "",
            "archive_status": archive.get("archive_status") or "",
            "image_quality": image_quality,
            "expected_image_count": expected_count,
            "saved_image_count": len([image for image in images if image["role"] == "carousel"]),
            "images": images,
            "visual_reference": {
                "available_image_count": len(images),
                "analysis_mode": "vision_if_supported_else_metadata_only",
            },
            "tags": json_loads(extraction.get("tags_json"), []),
            "metrics": json_loads(extraction.get("metrics_json"), {}),
            "captured_at": archive.get("captured_at"),
        }

    @staticmethod
    def _request_options(value: dict[str, Any] | None) -> dict[str, Any]:
        value = dict(value or {})
        platform = str(value.get("target_platform") or "xiaohongshu").strip().lower()
        if platform not in {"xiaohongshu", "douyin", "moments"}:
            raise RewriteError("目标平台只能是小红书、抖音或朋友圈")
        strength = str(value.get("rewrite_strength") or "balanced").strip().lower()
        if strength not in {"light", "balanced", "deep"}:
            raise RewriteError("改写强度只能是轻度、均衡或深度")
        persona = str(value.get("persona") or "自然、真诚、有具体生活观察的创作者").strip()
        if not persona or len(persona) > 300:
            raise RewriteError("请填写不超过 300 字的人设说明")
        count = value.get("image_count", 6)
        try:
            image_count = int(count)
        except (TypeError, ValueError) as exc:
            raise RewriteError("图片数量必须是数字") from exc
        if image_count < 3 or image_count > 9:
            raise RewriteError("图片数量需在 3 到 9 张之间")
        return {
            "target_platform": platform,
            "rewrite_strength": strength,
            "persona": persona,
            "image_count": image_count,
            "additional_brief": str(value.get("additional_brief") or "").strip()[:1200],
        }

    def create(
        self,
        workspace_id: str,
        extraction_id: str,
        *,
        archive_id: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        snapshot = self._archive_source(workspace_id, extraction_id, archive_id)
        request = self._request_options(options)
        now = utc_now()
        job_id = new_id("rewrite")
        self.db.execute(
            """INSERT INTO rewrite_jobs
            (id,workspace_id,source_extraction_id,source_archive_id,source_key,request_json,source_snapshot_json,
             result_json,status,stage,revision,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,'QUEUED',1,?,?)""",
            (
                job_id, workspace_id, snapshot["extraction_id"], snapshot["archive_id"],
                f"xiaohongshu:{snapshot['post_id']}", json_dumps(request), json_dumps(snapshot),
                json_dumps({}), "QUEUED", now, now,
            ),
        )
        self._put(job_id, None)
        return self.get(workspace_id, job_id) or {}

    def get(self, workspace_id: str, job_id: str) -> dict[str, Any] | None:
        row = self.db.one("SELECT * FROM rewrite_jobs WHERE workspace_id=? AND id=?", (workspace_id, job_id))
        if not row:
            return None
        value = dict(row)
        value["request"] = json_loads(value.pop("request_json", None), {})
        value["source_snapshot"] = json_loads(value.pop("source_snapshot_json", None), {})
        value["result"] = json_loads(value.pop("result_json", None), {})
        steps = self.db.all(
            "SELECT * FROM rewrite_job_steps WHERE workspace_id=? AND job_id=? ORDER BY created_at ASC",
            (workspace_id, job_id),
        )
        value["steps"] = [self._decode_step(step) for step in steps]
        return value

    @staticmethod
    def _decode_step(row: dict[str, Any]) -> dict[str, Any]:
        value = dict(row)
        value["input"] = json_loads(value.pop("input_json", None), {})
        value["output"] = json_loads(value.pop("output_json", None), {})
        return value

    def regenerate(self, workspace_id: str, job_id: str, stage: str) -> dict[str, Any]:
        if stage not in STAGES:
            raise RewriteError("请选择要重新生成的环节")
        job = self.get(workspace_id, job_id)
        if not job:
            raise RewriteError("改写任务不存在")
        revision = int(job.get("revision") or 1) + 1
        result = dict(job.get("result") or {})
        start = STAGES.index(stage)
        for name in STAGES[start:]:
            result.pop(name, None)
        now = utc_now()
        self.db.execute(
            """UPDATE rewrite_jobs SET status='QUEUED',stage=?,revision=?,result_json=?,error_code=NULL,error_message=NULL,
            completed_at=NULL,updated_at=? WHERE workspace_id=? AND id=?""",
            (stage, revision, json_dumps(result), now, workspace_id, job_id),
        )
        self._put(job_id, stage)
        return self.get(workspace_id, job_id) or {}

    def run_job(self, job_id: str, *, from_stage: str | None = None) -> dict[str, Any] | None:
        raw = self.db.one("SELECT * FROM rewrite_jobs WHERE id=?", (job_id,))
        if not raw:
            return None
        workspace_id = str(raw["workspace_id"])
        job = self.get(workspace_id, job_id)
        if not job:
            return None
        start_stage = from_stage if from_stage in STAGES else "analysis"
        start_index = STAGES.index(start_stage)
        result = dict(job.get("result") or {})
        request = dict(job.get("request") or {})
        source = dict(job.get("source_snapshot") or {})
        revision = int(job.get("revision") or 1)
        now = utc_now()
        self.db.execute(
            "UPDATE rewrite_jobs SET status='RUNNING',stage=?,started_at=COALESCE(started_at,?),updated_at=? WHERE id=?",
            (start_stage, now, now, job_id),
        )
        try:
            for stage in STAGES[start_index:]:
                self.db.execute(
                    "UPDATE rewrite_jobs SET stage=?,updated_at=? WHERE id=?",
                    (stage, utc_now(), job_id),
                )
                payload = self._stage_payload(stage, source, request, result)
                output = self._run_stage(workspace_id, job_id, stage, revision, payload, request)
                result[stage] = output
                self.db.execute(
                    "UPDATE rewrite_jobs SET result_json=?,updated_at=? WHERE id=?",
                    (json_dumps(result), utc_now(), job_id),
                )
            self.db.execute(
                """UPDATE rewrite_jobs SET status='COMPLETED',stage='COMPLETED',result_json=?,completed_at=?,
                error_code=NULL,error_message=NULL,updated_at=? WHERE id=?""",
                (json_dumps(result), utc_now(), utc_now(), job_id),
            )
        except (RewriteError, ProviderError) as exc:
            code = getattr(exc, "code", "validation_error" if isinstance(exc, RewriteValidationError) else "rewrite_error")
            self.db.execute(
                """UPDATE rewrite_jobs SET status='FAILED',error_code=?,error_message=?,updated_at=? WHERE id=?""",
                (str(code)[:100], str(exc)[:800], utc_now(), job_id),
            )
        except Exception as exc:  # defensive persistence; no model/internal detail leaks to UI
            self.db.execute(
                """UPDATE rewrite_jobs SET status='FAILED',error_code='internal_error',error_message=?,updated_at=? WHERE id=?""",
                ("改写任务未能完成，请重新生成该环节", utc_now(), job_id),
            )
        return self.get(workspace_id, job_id)

    def _stage_payload(
        self,
        stage: str,
        source: dict[str, Any],
        request: dict[str, Any],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        # The raw source stays immutable here.  Stages receive only prior
        # structured outputs, never a mutable copy of an earlier draft.
        payload: dict[str, Any] = {
            "source_snapshot": source,
            "request": request,
            "instruction": "只返回该阶段的 JSON，不要附加解释。",
        }
        if stage != "analysis":
            payload["analysis"] = result.get("analysis")
        if stage in {"copy_generation", "image_plan_generation", "review"}:
            payload["strategy"] = result.get("strategy")
        if stage in {"image_plan_generation", "review"}:
            payload["copy_package"] = result.get("copy_generation")
        if stage == "review":
            payload["image_plan"] = result.get("image_plan_generation")
        return payload

    def _run_stage(
        self,
        workspace_id: str,
        job_id: str,
        stage: str,
        revision: int,
        payload: dict[str, Any],
        request: dict[str, Any],
    ) -> dict[str, Any]:
        definition = STAGE_DEFINITIONS[stage]
        step_id = new_id("rstep")
        now = utc_now()
        self.db.execute(
            """INSERT OR REPLACE INTO rewrite_job_steps
            (id,workspace_id,job_id,stage,revision,status,prompt_version,input_json,output_json,created_at,updated_at)
            VALUES(?,?,?,?,?,'RUNNING',?,?,?, ?,?)""",
            (step_id, workspace_id, job_id, stage, revision, PROMPT_VERSION, json_dumps(payload), json_dumps({}), now, now),
        )
        image_inputs = self._visual_reference_inputs(workspace_id, job_id, payload) if stage == "analysis" else None
        if stage == "analysis":
            source = payload.get("source_snapshot") if isinstance(payload.get("source_snapshot"), dict) else {}
            payload["source_image_analysis"] = {
                "mode": "vision_preview" if image_inputs else "metadata_only",
                "provided_count": len(image_inputs or []),
                "available_count": int((source.get("visual_reference") or {}).get("available_image_count") or 0),
            }
        try:
            model_result: ModelResult = self.models.generate_json(
                workspace_id,
                None,
                STAGE_ROLES[stage],
                definition.system_prompt,
                payload,
                schema=definition.schema,
                web_search=False,
                image_inputs=image_inputs,
                rewrite_job_id=job_id,
            )
            copy_segment_ids: set[str] | None = None
            if stage == "image_plan_generation":
                copy_package = payload.get("copy_package")
                raw_segments = copy_package.get("copy_segments") if isinstance(copy_package, dict) else []
                copy_segment_ids = {
                    str(segment.get("id"))
                    for segment in raw_segments
                    if isinstance(segment, dict) and isinstance(segment.get("id"), str) and segment.get("id").strip()
                }
            output = _validate_output(
                stage,
                model_result.data,
                int(request.get("image_count") or 6),
                copy_segment_ids=copy_segment_ids,
            )
        except (ProviderError, RewriteError) as exc:
            self.db.execute(
                """UPDATE rewrite_job_steps SET status='FAILED',error_code=?,error_message=?,updated_at=?
                WHERE job_id=? AND stage=? AND revision=?""",
                (str(getattr(exc, "code", "validation_error"))[:100], str(exc)[:800], utc_now(), job_id, stage, revision),
            )
            raise
        self.db.execute(
            """UPDATE rewrite_job_steps SET status='COMPLETED',model_profile_id=?,latency_ms=?,output_json=?,updated_at=?
            WHERE job_id=? AND stage=? AND revision=?""",
            (model_result.profile_id, model_result.latency_ms, json_dumps(output), utc_now(), job_id, stage, revision),
        )
        return output

    def _visual_reference_inputs(self, workspace_id: str, job_id: str, payload: dict[str, Any]) -> list[str] | None:
        """Return in-memory visual previews only when the selected model is known vision-capable.

        The archive itself remains byte-for-byte untouched.  Previews are not
        stored in the database, export, logs, or source snapshot; they exist
        only for this one model call.  Text-only models receive image metadata
        instead of a false claim that visual contents were analysed.
        """
        try:
            resolver = getattr(self.models, "resolve_profile", None)
            profile = resolver(workspace_id, "rewrite_analysis") if callable(resolver) else None
        except Exception:
            profile = None
        if not isinstance(profile, dict):
            return None
        if str(profile.get("provider") or "") != DEEPSEEK_PROVIDER:
            return None
        if str(profile.get("model") or "") not in DEEPSEEK_VISION_MODELS:
            return None
        source = payload.get("source_snapshot") if isinstance(payload.get("source_snapshot"), dict) else {}
        archive_id = str(source.get("archive_id") or "")
        if not archive_id:
            return None
        archive = self.db.one(
            "SELECT archive_path FROM xhs_archive_versions WHERE workspace_id=? AND id=?",
            (workspace_id, archive_id),
        )
        if not archive:
            return None
        archive_root = self._resolve_archive_path(str(archive.get("archive_path") or ""))
        if not archive_root:
            return None
        assets = self.db.all(
            """SELECT relative_path,mime_detected,mime_declared,ordinal FROM xhs_post_assets
            WHERE workspace_id=? AND archive_id=? AND role IN ('carousel','cover') AND status='SAVED'
            ORDER BY CASE role WHEN 'cover' THEN 0 ELSE 1 END,ordinal""",
            (workspace_id, archive_id),
        )
        previews: list[str] = []
        for asset in assets[:6]:
            relative = str(asset.get("relative_path") or "")
            candidate = (archive_root / relative).resolve()
            if not relative or archive_root not in candidate.parents or not candidate.is_file():
                continue
            preview = self._image_preview_data_url(candidate, str(asset.get("mime_detected") or asset.get("mime_declared") or ""))
            if preview:
                previews.append(preview)
        return previews or None

    @staticmethod
    def _resolve_archive_path(value: str) -> Path | None:
        if not value:
            return None
        root = XHS_ARCHIVE_DIR.resolve()
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = root / candidate
        try:
            resolved = candidate.resolve()
        except OSError:
            return None
        return resolved if resolved == root or root in resolved.parents else None

    @staticmethod
    def _image_preview_data_url(path: Path, declared_mime: str) -> str | None:
        """Shrink a visual reference in memory while retaining archive originals untouched."""
        try:
            from PIL import Image
            with Image.open(path) as image:
                image = image.convert("RGB")
                image.thumbnail((1440, 1440))
                output = io.BytesIO()
                image.save(output, format="JPEG", quality=86, optimize=True)
                value = output.getvalue()
        except Exception:
            return None
        # Six bounded previews keep a vision prompt responsive on a local app.
        if not value or len(value) > 1_500_000:
            return None
        return "data:image/jpeg;base64," + base64.b64encode(value).decode("ascii")

    def export_markdown(self, workspace_id: str, job_id: str) -> tuple[str, str]:
        job = self.get(workspace_id, job_id)
        if not job:
            raise RewriteError("改写任务不存在")
        if job.get("status") != "COMPLETED":
            raise RewriteError("改写任务尚未完成，暂不能导出")
        result = job.get("result") or {}
        copy = result.get("copy_generation") or {}
        plan = result.get("image_plan_generation") or {}
        source = job.get("source_snapshot") or {}
        if not copy or not plan:
            raise RewriteError("改写内容包不完整，暂不能导出")
        lines = [
            f"# {copy.get('title', '未命名改写内容')}", "",
            "## 内容设置", "",
            f"- 平台：{job.get('request', {}).get('target_platform', '')}",
            f"- 人设：{job.get('request', {}).get('persona', '')}",
            f"- 改写强度：{job.get('request', {}).get('rewrite_strength', '')}", "",
            "## 标题备选", "",
            *[f"- {title}" for title in copy.get("alternative_titles", [])], "",
            "## 封面文案", "", str(copy.get("cover_copy") or ""), "",
            "## 正文", "", str(copy.get("body") or ""), "",
            "## 标签", "", " ".join(f"#{tag.lstrip('#')}" for tag in copy.get("tags", [])), "",
            "## 评论区引导", "", str(copy.get("comment_cta") or ""), "",
            "## 系列配图方案", "",
            f"- 视觉基调：{plan.get('style_bible', {}).get('visual_tone', '')}",
            f"- 配色：{'、'.join(plan.get('style_bible', {}).get('palette', []))}",
            f"- 光线：{plan.get('style_bible', {}).get('lighting', '')}",
            f"- 连续规则：{'；'.join(plan.get('style_bible', {}).get('continuity_rules', []))}", "",
        ]
        for card in plan.get("image_cards", []):
            lines.extend([
                f"### {card.get('ordinal')}. {card.get('role')}｜{card.get('caption')}", "",
                f"关联正文：{card.get('related_copy_segment')}", "",
                f"生成提示词：{card.get('prompt')}", "",
                f"画面说明：{card.get('visual_notes')}", "",
            ])
        lines.extend([
            "## 原帖核对信息（只读）", "",
            f"- 原帖标题：{source.get('title', '')}",
            f"- 原帖链接：{source.get('url', '')}",
            f"- 原帖正文哈希：{source.get('body_sha256', '')}",
            f"- 原图：已保存 {source.get('saved_image_count', 0)} 张；状态 {source.get('image_quality', '')}", "",
            "---", "此导出是基于已校验原帖生成的新内容包；原帖正文与原图仍以归档记录为准。",
        ])
        filename = f"改写内容包-{job_id}.md"
        return filename, "\n".join(lines)


rewrite_service = RewriteService()
