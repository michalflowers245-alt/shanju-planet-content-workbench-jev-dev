from __future__ import annotations

import re
import threading
from collections import Counter
from typing import Any, Callable

from .db import Database, database, json_dumps, json_loads, new_id, utc_now
from .jev_classifier import JEV_CANDIDATES_PER_REQUEST, JEV_RUBRIC_VERSION, JevClassifier, JevError, jev_classifier
from .platform_browser import (
    PlatformBrowserCollector, PlatformBrowserError, _douyin_video_id, _public_post_url,
    platform_browser_collector,
)
from .security import SecretVault, vault


ACTIVE_BATCH_STATUSES = {"QUEUED", "COLLECTING", "CLASSIFYING"}
RESUMABLE_BATCH_STATUSES = {"AWAITING_JEV_KEY", "USER_ACTION_REQUIRED", "INTERRUPTED", "PARTIAL", "FAILED"}
USER_ACTION_CODES = {"browser_not_running", "browser_page_required", "login_required", "verification_required", "platform_limited"}
MAX_BATCH_QUERIES = 96
INITIAL_BATCH_QUERIES = 48
QUERY_EXTENSION_STEP = 24


DEFAULT_MATCHMAKING_PERSONAS: list[dict[str, Any]] = [
    {
        "id": "youzipi_changsha",
        "label": "柚子皮在长沙",
        "public_role": "长沙本地线下相亲与交友活动的组织者，以组织者视角分享可核验的公开观察。",
        "audience": "生活在长沙、希望扩大异性社交圈的成年单身青年。",
        "business": "用真实、克制的内容建立本地信任，帮助合适的成年人了解并参与线下社交与相亲活动。",
        "search_terms": [
            "长沙相亲", "长沙脱单", "长沙同城交友", "长沙线下交友", "相亲活动", "单身社交圈",
            "第一次参加相亲活动", "同城脱单", "线下认识异性", "相亲活动避坑",
        ],
        "pillars": [
            {"id": "changsha_local_social", "label": "长沙同城社交", "description": "长沙本地成年单身人群的真实社交处境与选择。"},
            {"id": "offline_event_preparation", "label": "线下活动准备", "description": "参加线下交友或相亲活动前后的准备、体验和边界。"},
            {"id": "organizer_observation", "label": "组织者观察", "description": "不暴露个人身份、不虚构数据的活动组织观察。"},
            {"id": "expand_social_circle", "label": "扩大社交圈", "description": "帮助成年单身者用低风险方式扩大真实社交圈。"},
            {"id": "dating_safety_boundary", "label": "安全与边界", "description": "线下见面、信息保护、尊重和安全边界。"},
        ],
        "guardrails": [
            "不编造活动人数、配对率、成功案例或聊天记录",
            "不承诺脱单结果",
            "不泄露参加者身份与联系方式",
            "不制造性别对立或羞辱单身者",
        ],
    },
    {
        "id": "xiaodouzi_matchmaker",
        "label": "爱牵线的小豆子",
        "public_role": "面向成年单身女性的红娘与婚恋沟通内容账号，用实用判断帮助她们减少相亲误判。",
        "audience": "社交圈较小、希望认真认识合适对象的成年单身女性。",
        "business": "通过真实、尊重、可执行的婚恋内容建立专业信任，承接合适的婚恋咨询与牵线需求。",
        "search_terms": [
            "女生相亲", "女性脱单", "社交圈小怎么脱单", "第一次相亲", "相亲聊天", "相亲对象判断",
            "红娘建议", "相亲避坑", "婚恋边界", "择偶标准", "单身女性社交",
        ],
        "pillars": [
            {"id": "women_dating_reality", "label": "女性脱单现实", "description": "成年单身女性在现实社交和婚恋选择中的具体问题。"},
            {"id": "first_date_communication", "label": "初次见面与沟通", "description": "第一次相亲、聊天、表达和后续沟通。"},
            {"id": "partner_choice", "label": "择偶判断", "description": "看人、价值观、稳定性、责任和关系边界。"},
            {"id": "matchmaker_practical_advice", "label": "红娘实用建议", "description": "不承诺结果、可执行且尊重双方的红娘建议。"},
            {"id": "self_protection", "label": "安全与自我保护", "description": "隐私、财务、情感和线下见面的安全边界。"},
        ],
        "guardrails": [
            "不默认特定城市",
            "不编造客户故事、聊天记录或牵线成功案例",
            "不承诺脱单或婚姻结果",
            "不制造性别对立、年龄羞辱或焦虑营销",
        ],
    },
]


class RadarBatchError(RuntimeError):
    pass


class RadarBatchBusyError(RadarBatchError):
    pass


def _clean_text(value: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _clean_platforms(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value in {"xiaohongshu", "douyin"} and value not in result:
            result.append(value)
    if not result:
        raise ValueError("至少选择小红书或抖音中的一个平台")
    return result


class RadarBatchService:
    def __init__(
        self,
        db: Database = database,
        browser: PlatformBrowserCollector = platform_browser_collector,
        classifier: JevClassifier = jev_classifier,
        secret_vault: SecretVault = vault,
        on_eligible: Callable[[str, dict[str, Any]], int | None] | None = None,
    ) -> None:
        self.db = db
        self.browser = browser
        self.classifier = classifier
        self.vault = secret_vault
        self.on_eligible = on_eligible
        self._running_workspaces: set[str] = set()
        self._lock = threading.RLock()

    def ensure_personas(self, workspace_id: str) -> list[dict[str, Any]]:
        row = self.db.one("SELECT profile_json FROM brand_profiles WHERE workspace_id=?", (workspace_id,))
        if not row:
            raise KeyError("品牌工作区不存在")
        profile = json_loads(row.get("profile_json"), {}) or {}
        radar = profile.get("radar") if isinstance(profile.get("radar"), dict) else {}
        personas = radar.get("personas")
        if not isinstance(personas, list) or not personas:
            personas = DEFAULT_MATCHMAKING_PERSONAS
            radar = dict(radar)
            radar["personas"] = personas
            radar["active_persona_id"] = str(radar.get("active_persona_id") or personas[0]["id"])
            radar["persona_source"] = "用户提供的相亲人设执行方案（仅采用公开定位与业务边界）"
            profile["radar"] = radar
            self.db.execute(
                "UPDATE brand_profiles SET profile_json=?,updated_at=? WHERE workspace_id=?",
                (json_dumps(profile), utc_now(), workspace_id),
            )
        return [dict(value) for value in personas if isinstance(value, dict) and value.get("id")]

    def get_persona(self, workspace_id: str, persona_id: str) -> dict[str, Any]:
        for persona in self.ensure_personas(workspace_id):
            if str(persona.get("id")) == persona_id:
                return persona
        raise ValueError("所选人设不存在，请重新选择")

    def settings(self, workspace_id: str) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM jev_settings WHERE workspace_id=?", (workspace_id,))
        if not row:
            return {
                "workspace_id": workspace_id,
                "endpoint": self.classifier.endpoint,
                "model": self.classifier.model,
                "configured": False,
                "enabled": True,
                "verification_status": "unverified",
                "last_checked_at": None,
                "last_error_code": None,
                "last_error_message": None,
            }
        return {
            "workspace_id": workspace_id,
            "endpoint": row.get("endpoint") or self.classifier.endpoint,
            "model": row.get("model") or self.classifier.model,
            "configured": bool(row.get("secret_ref") and self.vault.get(row.get("secret_ref"))),
            "enabled": bool(row.get("enabled")),
            "verification_status": row.get("verification_status") or "unverified",
            "last_checked_at": row.get("last_checked_at"),
            "last_error_code": row.get("last_error_code"),
            "last_error_message": row.get("last_error_message"),
        }

    def save_settings(self, workspace_id: str, *, api_key: str | None, enabled: bool = True) -> dict[str, Any]:
        existing = self.db.one("SELECT * FROM jev_settings WHERE workspace_id=?", (workspace_id,)) or {}
        submitted = (api_key or "").strip()
        current_ref = str(existing.get("secret_ref") or "")
        key = submitted or self.vault.get(current_ref)
        if not key:
            raise JevError("请填写 TypeSafe JEV API Key", code="missing_api_key")
        now = utc_now()
        try:
            check = self.classifier.verify_key(key)
        except JevError as exc:
            if existing:
                self.db.execute(
                    "UPDATE jev_settings SET verification_status='failed',last_checked_at=?,last_error_code=?,last_error_message=?,updated_at=? WHERE workspace_id=?",
                    (now, exc.code, str(exc), now, workspace_id),
                )
            raise
        secret_ref = current_ref
        if submitted:
            secret_ref = self.vault.put(submitted, f"jev:{workspace_id}")
        self.db.execute(
            """INSERT INTO jev_settings
            (workspace_id,endpoint,model,secret_ref,enabled,verification_status,last_checked_at,last_error_code,last_error_message,updated_at)
            VALUES(?,?,?,?,?,'verified',?,NULL,NULL,?)
            ON CONFLICT(workspace_id) DO UPDATE SET endpoint=excluded.endpoint,model=excluded.model,
            secret_ref=excluded.secret_ref,enabled=excluded.enabled,verification_status='verified',
            last_checked_at=excluded.last_checked_at,last_error_code=NULL,last_error_message=NULL,updated_at=excluded.updated_at""",
            (
                workspace_id,
                self.classifier.endpoint,
                str(check.get("model") or self.classifier.model),
                secret_ref,
                int(enabled),
                now,
                now,
            ),
        )
        result = self.settings(workspace_id)
        result["latency_ms"] = check.get("latency_ms")
        return result

    def _api_key(self, workspace_id: str) -> str | None:
        row = self.db.one(
            "SELECT secret_ref,enabled,verification_status FROM jev_settings WHERE workspace_id=?",
            (workspace_id,),
        )
        if not row or not bool(row.get("enabled")):
            return None
        return self.vault.get(row.get("secret_ref"))

    @staticmethod
    def _query_plan(topic: str, persona: dict[str, Any], count: int) -> list[str]:
        topic_text = _clean_text(topic, 180)
        generic = topic_text in {"每日 IP 热点", "IP 自动热点", "今日热点", "相亲热点"}
        terms = [_clean_text(value, 80) for value in (persona.get("search_terms") or [])]
        terms = [value for value in terms if value]
        planned: list[str] = []
        if topic_text and not generic:
            planned.append(topic_text)
        for term in terms:
            if generic or term in topic_text:
                planned.append(term)
            else:
                planned.append(_clean_text(f"{topic_text} {term}", 180))

        # The first plan is intentionally precise. If it under-fills a large
        # batch, broaden with the persona's own search terms and an inferred
        # locality before trying experience-oriented variants. JEV remains the
        # relevance filter after collection; search expansion must not silently
        # redefine the persona or its business.
        local_prefixes: list[str] = []
        for term in terms:
            match = re.match(r"^([\u4e00-\u9fff]{2,5})(?=(?:相亲|脱单|同城|线下))", term)
            if match:
                local_prefixes.append(match.group(1))
        location = ""
        if local_prefixes:
            prefix, occurrences = Counter(local_prefixes).most_common(1)[0]
            if occurrences >= 2:
                location = prefix
        if not location:
            match = re.match(r"^([\u4e00-\u9fff]{2,5})(?=(?:市|成年人|单身|同城))", topic_text)
            if match:
                location = match.group(1)

        localized_terms: list[str] = []
        for term in terms:
            localized = term if not location or term.startswith(location) else _clean_text(f"{location}{term}", 80)
            localized_terms.append(localized)
            planned.extend([localized, term])
        for term in localized_terms:
            for modifier in (
                "真实经历", "体验分享", "攻略", "避坑", "怎么认识", "活动体验",
                "周末活动", "同城活动", "新手体验", "参加感受", "单身活动", "成年人交友",
            ):
                planned.append(_clean_text(f"{term} {modifier}", 180))
        for suffix in ("真实经历", "线下活动", "认识新朋友", "同城社交", "交友体验"):
            if topic_text and not generic:
                planned.append(_clean_text(f"{topic_text} {suffix}", 180))
        if not planned:
            planned = [topic_text or "相亲 脱单"]
        unique: list[str] = []
        seen: set[str] = set()
        for value in planned:
            normalized = re.sub(r"\s+", " ", value).strip()
            if normalized and normalized not in seen:
                seen.add(normalized)
                unique.append(normalized)
        return unique[: max(1, min(count, MAX_BATCH_QUERIES))]

    def _ensure_query_plan(self, batch: dict[str, Any], count: int = MAX_BATCH_QUERIES) -> int:
        platforms = json_loads(batch.get("platforms_json"), []) or []
        if not platforms:
            return 0
        target_count = int(batch.get("target_count") or 300)
        persona = json_loads(batch.get("persona_snapshot_json"), {}) or {}
        query_limit = min(max(int(count), 1), MAX_BATCH_QUERIES)
        planned = self._query_plan(str(batch.get("topic") or ""), persona, query_limit)
        added = 0
        # Read counts before opening the write transaction. Database helpers
        # use independent SQLite connections, and a nested read while holding
        # BEGIN IMMEDIATE is unnecessary here (the batch has a single worker).
        counts = {
            str(platform): self._candidate_count(str(batch["id"]), str(platform))
            for platform in platforms
        }
        targets = self._platform_targets(target_count, platforms, counts)
        with self.db.transaction() as connection:
            for platform in platforms:
                if counts.get(str(platform), 0) >= targets.get(str(platform), target_count):
                    continue
                existing_rows = connection.execute(
                    "SELECT query,ordinal,status FROM radar_batch_queries WHERE batch_id=? AND platform=?",
                    (batch["id"], platform),
                ).fetchall()
                existing = {str(row["query"]) for row in existing_rows}
                has_unsearched = any(str(row["status"]) in {"PENDING", "FAILED", "USER_ACTION_REQUIRED", "RUNNING"} for row in existing_rows)
                if has_unsearched:
                    continue
                existing_count = len(existing_rows)
                desired_count = (
                    INITIAL_BATCH_QUERIES
                    if existing_count < INITIAL_BATCH_QUERIES
                    else existing_count + QUERY_EXTENSION_STEP
                )
                desired_count = min(query_limit, desired_count)
                ordinal = max((int(row["ordinal"] or 0) for row in existing_rows), default=0)
                new_queries = [query for query in planned[:desired_count] if query not in existing]
                connection.executemany(
                    """INSERT INTO radar_batch_queries
                    (id,batch_id,platform,query,ordinal,status) VALUES(?,?,?,?,?,'PENDING')""",
                    [
                        (new_id("radar_query"), batch["id"], platform, query, ordinal + index)
                        for index, query in enumerate(new_queries, start=1)
                    ],
                )
                added += len(new_queries)
        return added

    def create_batch(
        self,
        workspace_id: str,
        *,
        topic: str,
        persona_id: str,
        platforms: list[str],
        target_count: int = 300,
        origin: str = "topic_search",
        skip_classification: bool = False,
    ) -> dict[str, Any]:
        clean_topic = _clean_text(topic, 180)
        if len(clean_topic) < 2:
            raise ValueError("主题至少需要 2 个字")
        selected_platforms = _clean_platforms(platforms)
        target = min(max(int(target_count), 50), 500)
        persona = self.get_persona(workspace_id, persona_id)
        active = self.db.one(
            "SELECT id FROM radar_search_batches WHERE workspace_id=? AND status IN ('QUEUED','COLLECTING','CLASSIFYING') ORDER BY created_at DESC LIMIT 1",
            (workspace_id,),
        )
        if active:
            raise RadarBatchBusyError("当前工作区已有一批内容正在采集或分类，请等待完成后再开始下一批")
        batch_id = new_id("radar_batch")
        now = utc_now()
        platform_targets = self._platform_targets(target, selected_platforms, {})
        query_count = INITIAL_BATCH_QUERIES
        platform_status = {
            platform: {
                "platform": platform,
                "status": "PENDING",
                "verification_status": "UNVERIFIED",
                "target": platform_targets[platform],
                "collected": 0,
                "message": "等待采集",
            }
            for platform in selected_platforms
        }
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO radar_search_batches
                (id,workspace_id,origin,persona_id,persona_snapshot_json,topic,platforms_json,target_count,status,stage,
                 counts_json,platform_status_json,skip_classification,taxonomy_version,cancel_requested,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?, 'QUEUED','QUEUED',?,?,?,?,?,?,?)""",
                (
                    batch_id,
                    workspace_id,
                    _clean_text(origin, 40) or "topic_search",
                    persona_id,
                    json_dumps(persona),
                    clean_topic,
                    json_dumps(selected_platforms),
                    target,
                    json_dumps({"target": target, "collected": 0, "classified": 0, "A": 0, "B": 0, "C": 0}),
                    json_dumps(platform_status),
                    int(skip_classification),
                    JEV_RUBRIC_VERSION,
                    0,
                    now,
                    now,
                ),
            )
            for platform in selected_platforms:
                queries = self._query_plan(clean_topic, persona, query_count)
                connection.executemany(
                    """INSERT INTO radar_batch_queries
                    (id,batch_id,platform,query,ordinal,status) VALUES(?,?,?,?,?,'PENDING')""",
                    [
                        (new_id("radar_query"), batch_id, platform, query, index)
                        for index, query in enumerate(queries, start=1)
                    ],
                )
        self.start_batch(batch_id, workspace_id)
        return self.get_batch(batch_id, workspace_id)

    @staticmethod
    def _platform_targets(
        target_count: int,
        platforms: list[str],
        counts: dict[str, int] | None = None,
    ) -> dict[str, int]:
        """Allocate a total target as evenly as possible without discarding results.

        If an existing platform is already above its even share, preserve that
        count as fixed and reduce the remaining platforms' targets so collection
        still stops at the requested total rather than blindly collecting a
        full half-target on every platform.
        """
        if not platforms:
            return {}
        total = max(0, int(target_count))
        current = {platform: max(0, int((counts or {}).get(platform, 0))) for platform in platforms}
        quotient, remainder = divmod(total, len(platforms))
        balanced = {
            platform: quotient + (1 if index < remainder else 0)
            for index, platform in enumerate(platforms)
        }
        overfilled = {
            platform: current[platform]
            for platform in platforms
            if current[platform] > balanced[platform]
        }
        if not overfilled:
            return balanced

        remaining_platforms = [platform for platform in platforms if platform not in overfilled]
        remaining_target = total - sum(overfilled.values())
        if not remaining_platforms or remaining_target <= 0:
            return current
        quotient, remainder = divmod(remaining_target, len(remaining_platforms))
        targets = dict(overfilled)
        for index, platform in enumerate(remaining_platforms):
            allocated = quotient + (1 if index < remainder else 0)
            targets[platform] = max(current[platform], allocated)
        return targets

    def start_batch(
        self, batch_id: str, workspace_id: str, *, resume_details: bool = False,
    ) -> dict[str, Any]:
        batch = self.db.one("SELECT * FROM radar_search_batches WHERE id=? AND workspace_id=?", (batch_id, workspace_id))
        if not batch:
            raise KeyError("批量研究任务不存在")
        with self._lock:
            if workspace_id in self._running_workspaces:
                raise RadarBatchBusyError("当前工作区已有批量研究正在运行")
            self._running_workspaces.add(workspace_id)
        self.db.execute(
            "UPDATE radar_search_batches SET status='QUEUED',stage='QUEUED',cancel_requested=0,error_message=NULL,updated_at=? WHERE id=?",
            (utc_now(), batch_id),
        )
        started = threading.Event()

        def run() -> None:
            try:
                self.run_batch(batch_id, workspace_id, started, resume_details=resume_details)
            finally:
                with self._lock:
                    self._running_workspaces.discard(workspace_id)
                started.set()

        threading.Thread(target=run, daemon=True, name=f"radar-batch-{batch_id[-8:]}").start()
        started.wait(timeout=0.5)
        return self.get_batch(batch_id, workspace_id)

    def resume_batch(
        self,
        batch_id: str,
        workspace_id: str,
        *,
        skip_classification: bool = False,
        resume_details: bool = False,
    ) -> dict[str, Any]:
        batch = self.db.one("SELECT * FROM radar_search_batches WHERE id=? AND workspace_id=?", (batch_id, workspace_id))
        if not batch:
            raise KeyError("批量研究任务不存在")
        if str(batch.get("status")) not in RESUMABLE_BATCH_STATUSES:
            raise RadarBatchError("当前批次不需要恢复")
        if skip_classification:
            self.db.execute(
                "UPDATE radar_search_batches SET skip_classification=1,updated_at=? WHERE id=? AND workspace_id=?",
                (utc_now(), batch_id, workspace_id),
            )
            batch["skip_classification"] = 1
        self._ensure_query_plan(batch)
        self.db.execute(
            """UPDATE radar_batch_queries SET status='PENDING',error_code=NULL,error_message=NULL,started_at=NULL,completed_at=NULL
            WHERE batch_id=? AND status IN ('FAILED','USER_ACTION_REQUIRED','RUNNING')""",
            (batch_id,),
        )
        self.db.execute(
            "UPDATE radar_batch_candidates SET classification_status='PENDING' WHERE batch_id=? AND classification_status='ERROR'",
            (batch_id,),
        )
        return self.start_batch(batch_id, workspace_id, resume_details=resume_details)

    def resume_details(self, batch_id: str, workspace_id: str) -> dict[str, Any]:
        """Retry this batch's unfinished detail reads without rerunning search/JEV."""
        with self._lock:
            batch = self.db.one(
                "SELECT * FROM radar_search_batches WHERE id=? AND workspace_id=?",
                (batch_id, workspace_id),
            )
            if not batch:
                raise KeyError("批量研究任务不存在")
            active = self.db.one(
                """SELECT id FROM radar_search_batches WHERE workspace_id=?
                AND status IN ('QUEUED','COLLECTING','CLASSIFYING') LIMIT 1""",
                (workspace_id,),
            )
            if active or workspace_id in self._running_workspaces:
                raise RadarBatchBusyError("当前工作区仍有批量研究正在运行，请完成后再恢复正文")
            self._running_workspaces.add(workspace_id)
        try:
            queued = self._enqueue_candidate_details(
                batch, resume_after_user_action=True, unfinished_only=True,
            )
            return {"batch_id": batch_id, "queued": queued}
        finally:
            with self._lock:
                self._running_workspaces.discard(workspace_id)

    def cancel_batch(self, batch_id: str, workspace_id: str) -> dict[str, Any]:
        row = self.db.one("SELECT id FROM radar_search_batches WHERE id=? AND workspace_id=?", (batch_id, workspace_id))
        if not row:
            raise KeyError("批量研究任务不存在")
        self.db.execute(
            "UPDATE radar_search_batches SET cancel_requested=1,updated_at=? WHERE id=?",
            (utc_now(), batch_id),
        )
        return self.get_batch(batch_id, workspace_id)

    def recover_interrupted(self) -> int:
        rows = self.db.all("SELECT id FROM radar_search_batches WHERE status IN ('QUEUED','COLLECTING','CLASSIFYING')")
        if not rows:
            return 0
        now = utc_now()
        self.db.execute(
            """UPDATE radar_search_batches SET status='INTERRUPTED',stage='INTERRUPTED',
            error_message='上次批量研究因工作台重启而中断，可点击继续',completed_at=?,updated_at=?
            WHERE status IN ('QUEUED','COLLECTING','CLASSIFYING')""",
            (now, now),
        )
        self.db.execute(
            "UPDATE radar_batch_queries SET status='PENDING',started_at=NULL WHERE status='RUNNING'",
        )
        return len(rows)

    def _cancelled(self, batch_id: str) -> bool:
        row = self.db.one("SELECT cancel_requested FROM radar_search_batches WHERE id=?", (batch_id,)) or {}
        return bool(row.get("cancel_requested"))

    def _set_batch_state(self, batch_id: str, status: str, stage: str, error: str | None = None, *, complete: bool = False) -> None:
        now = utc_now()
        self.db.execute(
            "UPDATE radar_search_batches SET status=?,stage=?,error_message=?,completed_at=CASE WHEN ? THEN ? ELSE completed_at END,updated_at=? WHERE id=?",
            (status, stage, error, int(complete), now, now, batch_id),
        )

    def _platform_statuses(self, batch_id: str) -> dict[str, Any]:
        row = self.db.one("SELECT platform_status_json FROM radar_search_batches WHERE id=?", (batch_id,)) or {}
        value = json_loads(row.get("platform_status_json"), {}) or {}
        return value if isinstance(value, dict) else {}

    def _save_platform_status(self, batch_id: str, platform: str, value: dict[str, Any]) -> None:
        statuses = self._platform_statuses(batch_id)
        statuses[platform] = value
        self.db.execute(
            "UPDATE radar_search_batches SET platform_status_json=?,updated_at=? WHERE id=?",
            (json_dumps(statuses), utc_now(), batch_id),
        )

    def _candidate_count(self, batch_id: str, platform: str | None = None) -> int:
        if platform:
            row = self.db.one("SELECT COUNT(*) AS count FROM radar_batch_candidates WHERE batch_id=? AND platform=?", (batch_id, platform))
        else:
            row = self.db.one("SELECT COUNT(*) AS count FROM radar_batch_candidates WHERE batch_id=?", (batch_id,))
        return int((row or {}).get("count") or 0)

    def _save_candidates(
        self,
        batch: dict[str, Any],
        platform: str,
        query: str,
        rows: list[dict[str, Any]],
        *,
        max_new: int | None = None,
    ) -> int:
        now = utc_now()
        added = 0
        with self.db.transaction() as connection:
            for row in rows:
                if max_new is not None and added >= max(0, int(max_new)):
                    break
                source_key = _clean_text(row.get("source_key"), 80)
                url = _clean_text(row.get("url"), 2000)
                title = _clean_text(row.get("title"), 500)
                if not source_key or not url or not title:
                    continue
                raw = row.get("raw") if isinstance(row.get("raw"), dict) else {}
                raw = {**raw, "search_query": query, "batch_id": batch["id"]}
                cursor = connection.execute(
                    """INSERT OR IGNORE INTO radar_batch_candidates
                    (id,batch_id,workspace_id,platform,source_key,source_mode,verification_status,search_query,title,url,excerpt,
                     author,published_at,metrics_json,raw_json,classification_status,discovered_at)
                    VALUES(?,?,?,?,?,'managed_browser_hidden_page','LIVE_VERIFIED',?,?,?,?,?,?,?,?,'PENDING',?)""",
                    (
                        new_id("radar_candidate"),
                        batch["id"],
                        batch["workspace_id"],
                        platform,
                        source_key,
                        query,
                        title,
                        url,
                        _clean_text(row.get("excerpt"), 4000),
                        _clean_text(row.get("author"), 300),
                        row.get("published_at"),
                        json_dumps(row.get("metrics") if isinstance(row.get("metrics"), dict) else {}),
                        json_dumps(raw),
                        now,
                    ),
                )
                added += max(cursor.rowcount, 0)
        return added

    def _refresh_counts(self, batch_id: str) -> dict[str, int]:
        row = self.db.one(
            """SELECT COUNT(*) AS collected,
            SUM(CASE WHEN classification_status='CLASSIFIED' THEN 1 ELSE 0 END) AS classified,
            SUM(CASE WHEN classification_status='ERROR' THEN 1 ELSE 0 END) AS errors,
            SUM(CASE WHEN route='A' THEN 1 ELSE 0 END) AS route_a,
            SUM(CASE WHEN route='B' THEN 1 ELSE 0 END) AS route_b,
            SUM(CASE WHEN route='C' THEN 1 ELSE 0 END) AS route_c
            FROM radar_batch_candidates WHERE batch_id=?""",
            (batch_id,),
        ) or {}
        batch = self.db.one("SELECT target_count FROM radar_search_batches WHERE id=?", (batch_id,)) or {}
        counts = {
            "target": int(batch.get("target_count") or 0),
            "collected": int(row.get("collected") or 0),
            "classified": int(row.get("classified") or 0),
            "classification_errors": int(row.get("errors") or 0),
            "A": int(row.get("route_a") or 0),
            "B": int(row.get("route_b") or 0),
            "C": int(row.get("route_c") or 0),
        }
        self.db.execute(
            "UPDATE radar_search_batches SET counts_json=?,updated_at=? WHERE id=?",
            (json_dumps(counts), utc_now(), batch_id),
        )
        return counts

    def _collect(self, batch: dict[str, Any]) -> tuple[bool, list[str]]:
        batch_id = str(batch["id"])
        platforms = json_loads(batch.get("platforms_json"), []) or []
        target_count = int(batch.get("target_count") or 300)
        action_required = False
        errors: list[str] = []
        for platform in platforms:
            current = self._candidate_count(batch_id, platform)
            target_per_platform = self._platform_targets(
                target_count,
                platforms,
                {value: self._candidate_count(batch_id, value) for value in platforms},
            ).get(platform, target_count)
            queries = self.db.all(
                """SELECT * FROM radar_batch_queries WHERE batch_id=? AND platform=?
                AND status IN ('PENDING','FAILED','USER_ACTION_REQUIRED') ORDER BY ordinal""",
                (batch_id, platform),
            )
            platform_failed = False
            for query_row in queries:
                if self._cancelled(batch_id):
                    return action_required, errors
                target_per_platform = self._platform_targets(
                    target_count,
                    platforms,
                    {value: self._candidate_count(batch_id, value) for value in platforms},
                ).get(platform, target_count)
                if current >= target_per_platform:
                    self.db.execute(
                        "UPDATE radar_batch_queries SET status='SKIPPED',completed_at=? WHERE id=?",
                        (utc_now(), query_row["id"]),
                    )
                    continue
                started = utc_now()
                self.db.execute(
                    "UPDATE radar_batch_queries SET status='RUNNING',started_at=?,error_code=NULL,error_message=NULL WHERE id=?",
                    (started, query_row["id"]),
                )
                try:
                    remaining = max(target_per_platform - current, 0)
                    rows = self.browser.search(platform, str(query_row["query"]), min(24, max(12, remaining + 4)))
                    added = self._save_candidates(
                        batch, platform, str(query_row["query"]), rows, max_new=remaining,
                    )
                    current += added
                    self.db.execute(
                        """UPDATE radar_batch_queries SET status='COMPLETED',result_count=?,new_count=?,completed_at=?
                        WHERE id=?""",
                        (len(rows), added, utc_now(), query_row["id"]),
                    )
                    self._save_platform_status(batch_id, platform, {
                        "platform": platform,
                        "status": "COLLECTING",
                        "verification_status": "LIVE_VERIFIED",
                        "target": target_per_platform,
                        "collected": current,
                        "message": f"已完成本轮后台读取，累计 {current} 条去重候选",
                    })
                    self._refresh_counts(batch_id)
                except PlatformBrowserError as exc:
                    code = str(exc.code or "browser_error")
                    query_status = "USER_ACTION_REQUIRED" if code in USER_ACTION_CODES else "FAILED"
                    self.db.execute(
                        """UPDATE radar_batch_queries SET status=?,error_code=?,error_message=?,completed_at=? WHERE id=?""",
                        (query_status, code, str(exc)[:1000], utc_now(), query_row["id"]),
                    )
                    platform_failed = True
                    errors.append(f"{platform}：{str(exc)}")
                    if code in USER_ACTION_CODES:
                        action_required = True
                        self._save_platform_status(batch_id, platform, {
                            "platform": platform,
                            "status": "USER_ACTION_REQUIRED",
                            "verification_status": "USER_ACTION_REQUIRED",
                            "target": target_per_platform,
                            "collected": current,
                            "error_code": code,
                            "message": str(exc),
                        })
                        break
                except Exception as exc:
                    platform_failed = True
                    errors.append(f"{platform}：后台采集失败")
                    self.db.execute(
                        "UPDATE radar_batch_queries SET status='FAILED',error_code='collection_error',error_message=?,completed_at=? WHERE id=?",
                        (type(exc).__name__, utc_now(), query_row["id"]),
                    )
            if not action_required or self._platform_statuses(batch_id).get(platform, {}).get("verification_status") != "USER_ACTION_REQUIRED":
                current = self._candidate_count(batch_id, platform)
                self._save_platform_status(batch_id, platform, {
                    "platform": platform,
                    "status": "COMPLETED" if current >= target_per_platform and not platform_failed else "PARTIAL",
                    "verification_status": "LIVE_VERIFIED" if current > 0 else "UNVERIFIED",
                    "target": target_per_platform,
                    "collected": current,
                    "message": (
                        f"已达到平台目标，得到 {current} 条去重候选"
                        if current >= target_per_platform and not platform_failed
                        else f"当前检索计划结束，得到 {current}/{target_per_platform} 条去重候选；可继续扩展查询"
                        if current else "本轮没有得到可保存的真实候选"
                    ),
                })
        return action_required, errors

    def _classify_pending(self, batch: dict[str, Any], api_key: str) -> list[str]:
        batch_id = str(batch["id"])
        persona = json_loads(batch.get("persona_snapshot_json"), {}) or {}
        errors: list[str] = []
        while True:
            if self._cancelled(batch_id):
                return errors
            candidates = self.db.all(
                """SELECT * FROM radar_batch_candidates WHERE batch_id=? AND classification_status='PENDING'
                ORDER BY discovered_at,id LIMIT 25""",
                (batch_id,),
            )
            if not candidates:
                return errors
            for offset in range(0, len(candidates), JEV_CANDIDATES_PER_REQUEST):
                if self._cancelled(batch_id):
                    return errors
                chunk = candidates[offset:offset + JEV_CANDIDATES_PER_REQUEST]
                payloads = [
                    {
                        **candidate,
                        "metrics": json_loads(candidate.get("metrics_json"), {}) or {},
                    }
                    for candidate in chunk
                ]
                try:
                    results = self.classifier.classify_many(api_key, payloads, persona)
                    if len(results) != len(chunk):
                        raise JevError("JEV 批量分类返回条数不匹配", code="invalid_response")
                except JevError as exc:
                    errors.append(str(exc))
                    now = utc_now()
                    with self.db.transaction() as connection:
                        connection.executemany(
                            """UPDATE radar_batch_candidates SET classification_status='ERROR',route='B',
                            jev_json=?,classified_at=? WHERE id=?""",
                            [
                                (json_dumps({"error_code": exc.code, "message": str(exc), "retryable": exc.retryable}), now, candidate["id"])
                                for candidate in chunk
                            ],
                        )
                    continue
                for candidate, result in zip(chunk, results):
                    if self._cancelled(batch_id):
                        continue
                    usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
                    now = utc_now()
                    with self.db.transaction() as connection:
                        connection.execute(
                            """UPDATE radar_batch_candidates SET classification_status='CLASSIFIED',route=?,pillar=?,audience_need=?,
                            composite_score=?,confidence=?,risk_flags_json=?,jev_json=?,classified_at=? WHERE id=?""",
                            (
                                result["route"],
                                result.get("pillar"),
                                result.get("audience_need"),
                                result.get("composite_score"),
                                result.get("confidence"),
                                json_dumps(result.get("risk_flags") or []),
                                json_dumps(result),
                                now,
                                candidate["id"],
                            ),
                        )
                        connection.execute(
                            """INSERT INTO radar_candidate_classifications
                            (id,candidate_id,batch_id,model,rubric_version,route,composite_score,confidence,result_json,input_tokens,output_tokens,created_at)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (
                                new_id("jev_classification"),
                                candidate["id"],
                                batch_id,
                                result.get("model") or self.classifier.model,
                                result.get("rubric_version") or JEV_RUBRIC_VERSION,
                                result["route"],
                                float(result.get("composite_score") or 0.0),
                                float(result.get("confidence") or 0.0),
                                json_dumps(result),
                                usage.get("input_tokens"),
                                usage.get("output_tokens"),
                                now,
                            ),
                        )
                    self._refresh_counts(batch_id)
        return errors

    def _enqueue_candidate_details(
        self, batch: dict[str, Any], *, resume_after_user_action: bool = False,
        unfinished_only: bool = False,
    ) -> int:
        if not self.on_eligible:
            return 0
        platforms = json_loads(batch.get("platforms_json"), []) or []
        if not platforms:
            return 0
        rows: list[dict[str, Any]] = []
        # A platform-level USER_ACTION_REQUIRED status describes why its
        # search target is incomplete; it must not invalidate candidates that
        # were already individually read and marked LIVE_VERIFIED before the
        # challenge appeared. Queue those exact-ID candidates independently.
        # Their detail workers will pause truthfully if the platform also gates
        # detail access, while successful candidates from either platform can
        # still make progress without waiting for the entire batch target.
        for platform in platforms:
            rows.extend(self.db.all(
                """SELECT * FROM radar_batch_candidates WHERE batch_id=? AND workspace_id=? AND platform=?
                AND verification_status='LIVE_VERIFIED' ORDER BY discovered_at,id""",
                (batch["id"], batch["workspace_id"], platform),
            ))
        if not rows:
            return 0
        items = []
        for row in rows:
            if unfinished_only and self._candidate_detail_complete(str(batch["workspace_id"]), row):
                continue
            items.append({
                "platform": row["platform"],
                "url": row["url"],
                "title": row["title"],
                "route": row["route"],
                "raw": {"search_query": row.get("search_query"), "batch_id": batch["id"]},
            })
        if not items:
            return 0
        queued_count = self.on_eligible(str(batch["workspace_id"]), {
            "topic": batch["topic"], "items": items, "sources": [],
            "resume_after_user_action": resume_after_user_action,
        })
        return max(0, int(queued_count or 0))

    def _candidate_detail_complete(self, workspace_id: str, candidate: dict[str, Any]) -> bool:
        platform = str(candidate.get("platform") or "")
        url = _public_post_url(platform, str(candidate.get("url") or ""))
        if platform == "xiaohongshu":
            post_match = re.search(r"/(?:explore|discovery/item)/([A-Za-z0-9]+)", url)
            post_id = post_match.group(1) if post_match else ""
            if not post_id:
                return False
            return bool(self.db.one(
                """SELECT id FROM xhs_post_extractions WHERE workspace_id=?
                AND (url=? OR post_key=?) AND archive_status='COMPLETE'
                AND verification_status='VERIFIED' AND trim(COALESCE(body_raw,''))<>'' LIMIT 1""",
                (workspace_id, url, post_id),
            ))
        if platform == "douyin":
            video_id = _douyin_video_id(url)
            if not video_id:
                return False
            rows = self.db.all(
                """SELECT status,published_body,diagnostics_json FROM douyin_post_extractions
                WHERE workspace_id=? AND video_id=? AND observed_video_id=?
                AND published_body_source='详情页结构化发布文案'""",
                (workspace_id, video_id, video_id),
            )
            return any(
                str(row.get("published_body") or "").strip()
                and (
                    row.get("status") == "COMPLETE"
                    or (json_loads(row.get("diagnostics_json"), {}) or {}).get("copy_completeness") == "verified_structured_caption"
                )
                for row in rows
            )
        return False

    def run_batch(
        self,
        batch_id: str,
        workspace_id: str,
        started: threading.Event | None = None,
        *,
        resume_details: bool = False,
    ) -> None:
        batch = self.db.one("SELECT * FROM radar_search_batches WHERE id=? AND workspace_id=?", (batch_id, workspace_id))
        if not batch:
            if started:
                started.set()
            return
        self.db.execute(
            """UPDATE radar_search_batches SET status='COLLECTING',stage='COLLECTING',taxonomy_version=?,
            started_at=COALESCE(started_at,?),completed_at=NULL,error_message=NULL,updated_at=? WHERE id=?""",
            (JEV_RUBRIC_VERSION, utc_now(), utc_now(), batch_id),
        )
        if started:
            started.set()
        try:
            action_required, collection_errors = self._collect(batch)
            self._refresh_counts(batch_id)
            if self._cancelled(batch_id):
                self._set_batch_state(batch_id, "CANCELED", "CANCELED", "已按用户要求停止", complete=True)
                return
            # The browser collection phase is fully stopped here. Archive any
            # platform whose own live read completed, even if another platform
            # now needs user verification. JEV classification remains gated on
            # all selected platforms reaching their targets below.
            queued = self._enqueue_candidate_details(batch, resume_after_user_action=resume_details)
            if self._candidate_count(batch_id) == 0:
                status = "USER_ACTION_REQUIRED" if action_required else "FAILED"
                message = "；".join(dict.fromkeys(collection_errors)) or "本轮没有采集到真实候选"
                self._set_batch_state(batch_id, status, status, message, complete=True)
                return
            if action_required:
                message_parts = list(dict.fromkeys(collection_errors)) or ["平台需要本人完成登录或安全验证"]
                if queued:
                    message_parts.append(f"已新增 {queued} 条详情读取任务")
                message = "；".join(message_parts)
                self._set_batch_state(batch_id, "USER_ACTION_REQUIRED", "COLLECTING", message, complete=True)
                return
            platforms = json_loads(batch.get("platforms_json"), []) or []
            platform_targets = self._platform_targets(
                int(batch.get("target_count") or 300),
                platforms,
                {platform: self._candidate_count(batch_id, platform) for platform in platforms},
            )
            underfilled = [
                f"{platform} {self._candidate_count(batch_id, platform)}/{platform_targets.get(platform, 0)}"
                for platform in platforms
                if self._candidate_count(batch_id, platform) < platform_targets.get(platform, 0)
            ]
            if underfilled:
                details = "；".join(underfilled)
                if collection_errors:
                    details += "；" + "；".join(dict.fromkeys(collection_errors))
                if queued:
                    details += f"；已新增 {queued} 条详情读取任务"
                self._set_batch_state(
                    batch_id,
                    "PARTIAL",
                    "COLLECTING",
                    f"当前可用搜索词已完成，但候选仍未达平台目标：{details}。可点击继续扩展搜索。",
                    complete=True,
                )
                return
            if bool(batch.get("skip_classification")):
                self._set_batch_state(
                    batch_id,
                    "COMPLETED",
                    "COMPLETED",
                    "候选搜索已完成；按本批设置跳过 JEV 分类，正文与媒体归档继续独立处理。",
                    complete=True,
                )
                return
            api_key = self._api_key(workspace_id)
            if not api_key:
                message = "候选池已采集完成；请在设置中连接 TypeSafe JEV 后继续分类"
                if queued:
                    message += f"；已新增 {queued} 条详情读取任务"
                self._set_batch_state(
                    batch_id,
                    "AWAITING_JEV_KEY",
                    "CLASSIFYING",
                    message,
                    complete=True,
                )
                return
            self._set_batch_state(batch_id, "CLASSIFYING", "CLASSIFYING")
            current = self.db.one("SELECT * FROM radar_search_batches WHERE id=?", (batch_id,)) or batch
            classification_errors = self._classify_pending(current, api_key)
            counts = self._refresh_counts(batch_id)
            if self._cancelled(batch_id):
                self._set_batch_state(batch_id, "CANCELED", "CANCELED", "已按用户要求停止", complete=True)
                return
            latest = self.db.one("SELECT * FROM radar_search_batches WHERE id=?", (batch_id,)) or current
            statuses = self._platform_statuses(batch_id)
            platforms = json_loads(latest.get("platforms_json"), []) or []
            all_live = all((statuses.get(platform) or {}).get("verification_status") == "LIVE_VERIFIED" for platform in platforms)
            has_errors = bool(collection_errors or classification_errors or counts.get("classification_errors"))
            status = "COMPLETED" if all_live and not has_errors else "PARTIAL"
            message_parts: list[str] = []
            if collection_errors:
                message_parts.extend(collection_errors)
            if classification_errors:
                message_parts.append(f"JEV 分类有 {counts.get('classification_errors', 0)} 条待重试")
            if queued:
                message_parts.append(f"已新增 {queued} 条实时详情读取任务")
            self._set_batch_state(batch_id, status, "COMPLETED", "；".join(dict.fromkeys(message_parts)) or None, complete=True)
        except Exception as exc:
            self._set_batch_state(batch_id, "FAILED", "FAILED", f"批量研究失败：{type(exc).__name__}", complete=True)

    @staticmethod
    def _decode_candidate(row: dict[str, Any]) -> dict[str, Any]:
        value = dict(row)
        value["metrics"] = json_loads(value.pop("metrics_json", None), {}) or {}
        value["raw"] = json_loads(value.pop("raw_json", None), {}) or {}
        value["risk_flags"] = json_loads(value.pop("risk_flags_json", None), []) or []
        jev = json_loads(value.pop("jev_json", None), {}) or {}
        if isinstance(jev, dict):
            jev.pop("answers", None)
        value["jev"] = jev
        return value

    def list_candidates(
        self,
        batch_id: str,
        workspace_id: str,
        *,
        route: str | None = None,
        platform: str | None = None,
        limit: int = 60,
        offset: int = 0,
    ) -> dict[str, Any]:
        batch = self.db.one("SELECT id FROM radar_search_batches WHERE id=? AND workspace_id=?", (batch_id, workspace_id))
        if not batch:
            raise KeyError("批量研究任务不存在")
        clauses = ["batch_id=?"]
        params: list[Any] = [batch_id]
        if route in {"A", "B", "C"}:
            clauses.append("route=?")
            params.append(route)
        if platform in {"xiaohongshu", "douyin"}:
            clauses.append("platform=?")
            params.append(platform)
        where = " AND ".join(clauses)
        total_row = self.db.one(f"SELECT COUNT(*) AS count FROM radar_batch_candidates WHERE {where}", params) or {}
        safe_limit = min(max(int(limit), 1), 100)
        safe_offset = max(int(offset), 0)
        rows = self.db.all(
            f"""SELECT * FROM radar_batch_candidates WHERE {where}
            ORDER BY CASE route WHEN 'A' THEN 0 WHEN 'B' THEN 1 WHEN 'C' THEN 2 ELSE 3 END,
            composite_score DESC,discovered_at DESC LIMIT ? OFFSET ?""",
            (*params, safe_limit, safe_offset),
        )
        return {
            "items": [self._decode_candidate(row) for row in rows],
            "total": int(total_row.get("count") or 0),
            "limit": safe_limit,
            "offset": safe_offset,
        }

    def get_batch(self, batch_id: str, workspace_id: str) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM radar_search_batches WHERE id=? AND workspace_id=?", (batch_id, workspace_id))
        if not row:
            raise KeyError("批量研究任务不存在")
        counts = self._refresh_counts(batch_id)
        value = dict(row)
        value["platforms"] = json_loads(value.pop("platforms_json", None), []) or []
        value["persona"] = json_loads(value.pop("persona_snapshot_json", None), {}) or {}
        value["counts"] = counts
        value.pop("counts_json", None)
        platform_status = json_loads(value.pop("platform_status_json", None), {}) or {}
        platforms = json_loads(row.get("platforms_json"), []) or []
        per_platform_counts = {
            platform: self._candidate_count(batch_id, platform)
            for platform in platforms
        }
        platform_targets = self._platform_targets(
            int(row.get("target_count") or 0), platforms, per_platform_counts,
        )
        for platform in platforms:
            details = dict(platform_status.get(platform) or {})
            details["platform"] = platform
            details["target"] = platform_targets.get(platform, 0)
            details["collected"] = per_platform_counts.get(platform, 0)
            platform_status[platform] = details
        value["platform_status"] = platform_status
        value["cancel_requested"] = bool(value.get("cancel_requested"))
        value["skip_classification"] = bool(value.get("skip_classification"))
        query_rows = self.db.all(
            "SELECT platform,status,COUNT(*) AS count FROM radar_batch_queries WHERE batch_id=? GROUP BY platform,status",
            (batch_id,),
        )
        query_counts: dict[str, dict[str, int]] = {}
        for query in query_rows:
            query_counts.setdefault(str(query["platform"]), {})[str(query["status"])] = int(query["count"] or 0)
        value["query_counts"] = query_counts
        top = self.list_candidates(batch_id, workspace_id, route="A", limit=10)["items"]
        value["recommendations"] = [
            {
                "title": item.get("title"),
                "reason": (item.get("jev") or {}).get("reason"),
                "pillar": item.get("pillar"),
                "platform": item.get("platform"),
                "url": item.get("url"),
                "score": item.get("composite_score"),
            }
            for item in top
        ]
        return value

    def list_batches(self, workspace_id: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.db.all(
            "SELECT id FROM radar_search_batches WHERE workspace_id=? ORDER BY created_at DESC LIMIT ?",
            (workspace_id, min(max(int(limit), 1), 50)),
        )
        return [self.get_batch(str(row["id"]), workspace_id) for row in rows]


radar_batch_service = RadarBatchService()
