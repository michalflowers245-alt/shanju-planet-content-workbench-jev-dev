from __future__ import annotations

import hashlib
import re
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .db import Database, database, json_dumps, json_loads, new_id, utc_now
from .jev_classifier import JEV_CANDIDATES_PER_REQUEST, JevClassifier, JevError, jev_classifier
from .platform_browser import PlatformBrowserCollector, PlatformBrowserError, platform_browser_collector
from .providers import SearchGateway, sanitize_provider_text
from .security import SecretVault, vault


# ``web`` is a first-party, public-web discovery source.  It is deliberately
# distinct from Xiaohongshu and Douyin so the UI never presents public-search
# signals as if they were platform-native metrics.
SUPPORTED_PLATFORMS = {"xiaohongshu", "douyin", "web"}

# Only broad content-domain terms are extracted from a saved IP profile.  This
# avoids turning personal details, contact information, or arbitrary profile
# prose into external search queries.  The list intentionally covers the
# common creator directions supported by the workbench and can fall back to a
# neutral public-trend query when no match is available.
IP_DOMAIN_TERMS = (
    "人工智能", "AI", "企业服务", "内容创作", "短视频", "小红书", "抖音",
    "相亲", "婚恋", "脱单", "恋爱", "两性", "情感", "关系", "婚姻", "同城交友", "社交", "约会", "单身",
    "女性成长", "个人成长", "年轻人", "大学生", "教育", "教培", "留学", "求职", "职场", "创业", "副业",
    "心理", "健康", "母婴", "亲子", "美妆", "服装", "摄影", "旅游", "宠物", "家居", "餐饮", "本地生活",
    "电商", "品牌", "营销", "理财", "房产", "健身", "营养", "阅读", "法律", "医疗",
)
FALLBACK_IP_TERMS = ("年轻人生活", "内容创作", "个人成长")
_PII_OR_CONTACT_RE = re.compile(
    r"(?:\d{6,}|@|微信|微\s*信|手机号|手机号码|电话|住址|地址|身份证|QQ|邮箱|mail)", re.IGNORECASE
)

# These are broad, non-sensitive routing markers.  When a profile matches
# this creator category, the daily radar uses the two execution plans that
# ship with this workbench: a local/offline matchmaking account and a
# first-person female dating-and-matchmaker account.  It never exposes the
# profile prose that caused the routing decision.
MATCHMAKING_PROFILE_MARKERS = frozenset({
    "相亲", "婚恋", "脱单", "红娘", "同城交友", "线下交友", "单身", "恋爱", "择偶", "约会",
})
SAFE_MATCHMAKING_MARKETS = ("长沙",)

# A daily refresh is a verification boundary: a transient CDP/page failure
# must not quietly turn yesterday's cards into a successful refresh.  A small
# bounded retry handles Chrome briefly releasing its DevTools socket without
# repeatedly hammering either platform.
PLATFORM_SEARCH_MAX_ATTEMPTS = 2
TRANSIENT_PLATFORM_SEARCH_CODES = frozenset({
    "page_timeout",
    "page_read_error",
    "browser_connect_error",
    "background_collector_unavailable",
    "no_visible_results",
    "no_valid_results",
})

# A managed CDP target reads the same rendered platform page while keeping the
# user's Chrome tabs untouched.  Older saved rows retain ``visible_browser``
# for backwards-compatible history reads.
MANAGED_BACKGROUND_SOURCE_MODE = "managed_background_browser"
LIVE_PLATFORM_SOURCE_MODES = frozenset({"visible_browser", MANAGED_BACKGROUND_SOURCE_MODE})


class RadarError(RuntimeError):
    """A safe, user-facing error from a platform discovery run."""


class RadarBusyError(RadarError):
    pass


def _next_run_at(interval_hours: int) -> str:
    value = datetime.now(timezone.utc) + timedelta(hours=interval_hours)
    return value.isoformat(timespec="seconds")


def _stable_key(value: str) -> str:
    return hashlib.sha256(value.strip().lower().encode("utf-8")).hexdigest()[:32]


def _clean_platforms(values: list[str]) -> list[str]:
    platforms = []
    for raw in values:
        value = str(raw or "").strip().lower()
        if value in SUPPORTED_PLATFORMS and value not in platforms:
            platforms.append(value)
    if not platforms:
        raise ValueError("请至少选择一个平台")
    return platforms


def douyin_web_search_link(query: str) -> str:
    """A platform-native HTTPS search page, safe to save as a task source."""
    clean_query = _text(query, 180)
    return f"https://www.douyin.com/search/{urllib.parse.quote(clean_query, safe='')}?type=video"


def xiaohongshu_web_search_link(query: str) -> str:
    """A platform-native HTTPS search page, without scraping any note data."""
    clean_query = _text(query, 180)
    return "https://www.xiaohongshu.com/search_result/?" + urllib.parse.urlencode({"keyword": clean_query})


def _official_platform_search_links(query: str) -> list[dict[str, str]]:
    """Return the two creator-facing search actions for a safely scoped query."""
    return [
        {
            "platform": "douyin",
            "label": "抖音搜索",
            "url": douyin_web_search_link(query),
            "source_mode": "official_search",
        },
        {
            "platform": "xiaohongshu",
            "label": "小红书搜索",
            "url": xiaohongshu_web_search_link(query),
            "source_mode": "official_search",
        },
    ]


def _text(value: Any, limit: int = 500) -> str:
    """Normalize display/search text without preserving markup or control chars."""
    clean = re.sub(r"<[^>]+>", " ", str(value or ""))
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean[:limit]


_MATCHMAKING_QUERY_ALIASES = (
    "相亲", "找对象", "脱单", "征婚", "婚恋", "红娘", "媒婆", "牵线", "交友", "恋爱",
)
_QUERY_CONCEPT_ALIASES: dict[str, tuple[str, ...]] = {
    "长沙": ("长沙", "星沙", "湖南"),
    "同城": ("同城", "本地", "线下"),
    "线下活动": ("线下活动", "相亲活动", "交友活动", "线下交友", "聚会"),
    "活动": ("活动", "聚会", "局"),
    "女生": ("女生", "女孩", "女性"),
    "男生": ("男生", "男孩", "男性"),
}
for _matchmaking_alias in _MATCHMAKING_QUERY_ALIASES:
    _QUERY_CONCEPT_ALIASES[_matchmaking_alias] = _MATCHMAKING_QUERY_ALIASES
_QUERY_CONCEPT_KEYS = tuple(sorted(_QUERY_CONCEPT_ALIASES, key=len, reverse=True))


def _query_term_groups(query: str) -> list[tuple[str, ...]]:
    """Split safe compact Chinese topic concepts without weakening relevance.

    Platform search boxes naturally accept ``长沙相亲`` without spaces, but
    treating it as one literal term discards actual cards titled ``长沙找对象``
    or ``湖南脱单``.  We only split a compact Chinese phrase when known routing
    concepts cover it (apart from a one-character suffix such as ``贴``).  An
    unknown phrase stays intact, so the filter does not turn a vague match into
    a relevant source merely because it contains one Chinese character.
    """
    raw_terms = [
        term.lower() for term in re.split(r"[\s,，、/|]+", _text(query, 180))
        if len(term.strip()) >= 2
    ]
    if not raw_terms:
        return []

    def aliases_for(term: str) -> tuple[str, ...]:
        return _QUERY_CONCEPT_ALIASES.get(term, (term,))

    if len(raw_terms) > 1:
        return [aliases_for(term) for term in raw_terms]

    compact = raw_terms[0]
    if not re.fullmatch(r"[\u3400-\u9fff]+", compact):
        return [aliases_for(compact)]

    groups: list[tuple[str, ...]] = []
    unmatched = 0
    cursor = 0
    while cursor < len(compact):
        concept = next(
            (candidate for candidate in _QUERY_CONCEPT_KEYS if compact.startswith(candidate, cursor)),
            "",
        )
        if not concept:
            unmatched += 1
            cursor += 1
            continue
        groups.append(aliases_for(concept))
        cursor += len(concept)

    # ``长沙相亲`` and ``长沙相亲贴`` are both clear combinations.  Do not
    # segment a natural-language phrase whose unmatched part carries meaning.
    if len(groups) >= 2 and unmatched <= 1:
        return groups
    return [aliases_for(compact)]


def _query_terms(query: str) -> list[str]:
    """Return meaningful words used to reject accidental string matches."""
    return [group[0] for group in _query_term_groups(query)]


def _relevance_score(item: dict[str, Any], query: str, *, minimum_hits: int | None = None) -> int:
    term_groups = _query_term_groups(query)
    if not term_groups:
        return 0
    haystack = f"{_text(item.get('title'), 500)} {_text(item.get('excerpt'), 2000)}".lower()
    hits = sum(1 for aliases in term_groups if any(term in haystack for term in aliases))
    # Daily searches deliberately contain several concrete terms. Requiring
    # two prevents accidental matches such as “相亲相爱” from winning merely
    # because an unrelated video has a large like count.
    required_hits = minimum_hits if minimum_hits is not None else (2 if len(term_groups) >= 2 else 1)
    if hits < min(max(required_hits, 1), len(term_groups)):
        return -1
    return hits


def _heat_score(item: dict[str, Any]) -> float:
    """Rank by metrics the platform actually made visible on the card."""
    metrics = item.get("metrics") if isinstance(item.get("metrics"), dict) else {}

    def number(key: str) -> float:
        try:
            return max(0.0, float(metrics.get(key) or 0))
        except (TypeError, ValueError):
            return 0.0

    return (
        number("plays") * 0.02
        + number("likes")
        + number("comments") * 3
        + number("favorites") * 2
        + number("shares") * 4
        + number("visible_engagement")
    )


def _profile_text(value: Any) -> str:
    """Collect profile prose locally only; its raw value is never returned or saved in radar results."""
    if isinstance(value, dict):
        return " ".join(_profile_text(item) for key, item in value.items() if not str(key).startswith("_"))
    if isinstance(value, (list, tuple, set)):
        return " ".join(_profile_text(item) for item in value)
    clean = _text(value, 4000)
    # A profile editor may keep a public bio and a contact method in the same
    # long field.  Discard only contact-bearing clauses so the rest of the IP
    # direction can still be used locally for safe, broad routing.
    clauses = re.split(r"[\r\n。；;！？!?，,]+", clean)
    return " ".join(clause for clause in clauses if clause and not _PII_OR_CONTACT_RE.search(clause))


def _ip_topic_anchors(profile: dict[str, Any]) -> tuple[list[str], bool]:
    """Return privacy-filtered, broad domain anchors for public trend search.

    The profile remains a local source of routing context.  We only emit a
    predefined domain label if it appears in profile text, never a free-form
    sentence, a name, contact detail, or an address.
    """
    profile_text = _profile_text(profile)
    has_context = bool(profile_text)
    lowered = profile_text.lower()
    anchors: list[str] = []
    for term in IP_DOMAIN_TERMS:
        if term.lower() in lowered and term not in anchors:
            anchors.append(term)
        if len(anchors) >= 3:
            break
    # Keep the fallback useful on a newly-created workspace while making it
    # clear in the run summary that it did not originate from a complete IP.
    return (anchors or list(FALLBACK_IP_TERMS), bool(anchors and has_context))


def _daily_ip_directions(profile_text: str, anchors: list[str]) -> list[dict[str, str]]:
    """Build creator-ready daily directions without leaking profile prose.

    Public-web results can help us assess whether there is fresh discussion,
    but they must not replace the platform links a creator needs to inspect.
    Matchmaking profiles therefore receive the two attached account plans as
    concrete content lanes, while other profiles receive safe domain lanes.
    """
    lowered = profile_text.lower()
    is_matchmaking = any(marker.lower() in lowered for marker in MATCHMAKING_PROFILE_MARKERS)
    if is_matchmaking:
        market = next((name for name in SAFE_MATCHMAKING_MARKETS if name in profile_text), "同城")
        return [
            {
                "persona_id": "youzipi_changsha",
                "persona": "柚子皮 · 同城线下相亲",
                "pillar": "线下交友活动 / 扩大异性圈",
                "search_query": f"{market} 相亲 线下交友活动",
                "title": f"{market}单身的人，为什么又开始走进线下交友活动？",
                "reason": "从本地单身人群愿不愿意出门认识新人切入，再落到活动、匹配与安全感这些真实顾虑。",
            },
            {
                "persona_id": "xiaodouzi_matchmaker",
                "persona": "小豆子 · 女性脱单红娘",
                "pillar": "圈子小 / 主动认识异性",
                "search_query": "圈子小 女生 脱单",
                "title": "圈子小的女生，到底怎么开始脱单？",
                "reason": "从“想恋爱但身边没有合适的人”的具体困境说起，给出能开始执行的认识方式。",
            },
            {
                "persona_id": "both_personas",
                "persona": "红娘案例 · 双账号共用",
                "pillar": "相亲上岸 / 匹配观察",
                "search_query": "红娘 相亲 上岸 案例",
                "title": "红娘看过的相亲上岸，真正卡在了哪个细节？",
                "reason": "用一个可匿名的匹配场景讲清择偶、沟通与见面节奏，避免泛泛讲情感道理。",
            },
        ]

    directions: list[dict[str, str]] = []
    for anchor in anchors[:3]:
        directions.append({
            "persona": "已保存的 IP 方向",
            "pillar": anchor,
            "search_query": f"{anchor} 内容",
            "title": f"「{anchor}」里，大家最近在认真讨论什么？",
            "reason": "先去两个平台看真实内容和评论，再把讨论落回你的目标受众正在面对的具体选择。",
        })
    return directions


def _valid_public_url(value: Any) -> str:
    candidate = str(value or "").strip()
    try:
        parsed = urllib.parse.urlsplit(candidate)
    except ValueError:
        return ""
    return candidate if parsed.scheme in {"http", "https"} and parsed.netloc else ""


class TopicRadarService:
    def __init__(
        self,
        db: Database = database,
        public_search: SearchGateway | None = None,
        browser_collector: PlatformBrowserCollector | None = None,
        on_results_ready: Callable[[str, dict[str, Any]], None] | None = None,
        classifier: JevClassifier = jev_classifier,
        secret_vault: SecretVault = vault,
    ) -> None:
        self.db = db
        self.public_search = public_search or SearchGateway(db)
        self.browser = browser_collector or platform_browser_collector
        self.on_results_ready = on_results_ready
        self.classifier = classifier
        self.vault = secret_vault
        self._running: set[str] = set()
        self._lock = threading.RLock()

    def _jev_api_key(self, workspace_id: str) -> str | None:
        row = self.db.one(
            "SELECT secret_ref,enabled,verification_status FROM jev_settings WHERE workspace_id=?",
            (workspace_id,),
        )
        if not row or not bool(row.get("enabled")) or row.get("verification_status") != "verified":
            return None
        return self.vault.get(row.get("secret_ref"))

    @staticmethod
    def _profile_personas(profile: dict[str, Any]) -> list[dict[str, Any]]:
        radar = profile.get("radar") if isinstance(profile.get("radar"), dict) else {}
        configured = radar.get("personas") if isinstance(radar.get("personas"), list) else []
        personas = [dict(item) for item in configured if isinstance(item, dict) and item.get("id")]
        if personas:
            return personas
        # This workbench ships the two user-approved matchmaking identities.
        # Import locally so the general monitoring service does not depend on
        # the batch worker during module initialization.
        from .radar_batches import DEFAULT_MATCHMAKING_PERSONAS
        return [dict(item) for item in DEFAULT_MATCHMAKING_PERSONAS]

    @classmethod
    def _persona_for_direction(
        cls, profile: dict[str, Any], direction: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        personas = cls._profile_personas(profile)
        direction = direction or {}
        requested_id = str(direction.get("persona_id") or "")
        radar = profile.get("radar") if isinstance(profile.get("radar"), dict) else {}
        if not requested_id or requested_id == "both_personas":
            requested_id = str(radar.get("active_persona_id") or "")
        match = next((item for item in personas if str(item.get("id")) == requested_id), None)
        if match:
            return match
        label = str(direction.get("persona") or "")
        match = next((item for item in personas if str(item.get("label") or "") in label), None)
        return match or personas[0]

    def _classify_candidates(
        self,
        workspace_id: str,
        profile: dict[str, Any],
        candidates: list[dict[str, Any]],
        *,
        direction_by_query: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if not candidates:
            return {"status": "NO_CANDIDATES", "classified": 0, "A": 0, "B": 0, "C": 0, "errors": 0}
        api_key = self._jev_api_key(workspace_id)
        if not api_key:
            for candidate in candidates:
                candidate["classification_status"] = "AWAITING_JEV_KEY"
                candidate["jev"] = {"route": None, "status": "AWAITING_JEV_KEY"}
                raw = dict(candidate.get("raw") or {})
                raw["classification_status"] = "AWAITING_JEV_KEY"
                candidate["raw"] = raw
            return {
                "status": "AWAITING_JEV_KEY", "classified": 0, "A": 0, "B": 0, "C": 0,
                "errors": 0, "message": "请在设置中连接并验证 JEV Key；未分类内容不会进入推荐或文案深读。",
            }

        groups: dict[str, tuple[dict[str, Any], list[tuple[int, dict[str, Any]]]]] = {}
        for index, candidate in enumerate(candidates):
            raw = candidate.get("raw") if isinstance(candidate.get("raw"), dict) else {}
            query = str(candidate.get("matched_query") or candidate.get("search_query") or raw.get("search_query") or "")
            direction = (direction_by_query or {}).get(query)
            persona = self._persona_for_direction(profile, direction)
            persona_id = str(persona.get("id") or "default")
            if persona_id not in groups:
                groups[persona_id] = (persona, [])
            groups[persona_id][1].append((index, candidate))

        errors = 0
        for persona, entries in groups.values():
            for offset in range(0, len(entries), JEV_CANDIDATES_PER_REQUEST):
                chunk = entries[offset:offset + JEV_CANDIDATES_PER_REQUEST]
                try:
                    results = self.classifier.classify_many(
                        api_key,
                        [
                            {
                                **candidate,
                                "metrics": candidate.get("metrics") or {},
                                "search_query": candidate.get("matched_query")
                                or (candidate.get("raw") or {}).get("search_query") or "",
                            }
                            for _, candidate in chunk
                        ],
                        persona,
                    )
                    if len(results) != len(chunk):
                        raise JevError("JEV 批量分类返回条数不匹配", code="invalid_response")
                except Exception as exc:
                    errors += len(chunk)
                    error_code = exc.code if isinstance(exc, JevError) else "classification_error"
                    error_message = str(exc) if isinstance(exc, JevError) else f"JEV 分类异常（{type(exc).__name__}）"
                    for _, candidate in chunk:
                        candidate["classification_status"] = "ERROR"
                        candidate["jev"] = {"route": None, "status": "ERROR", "error_code": error_code, "message": error_message}
                        raw = dict(candidate.get("raw") or {})
                        raw["classification_status"] = "ERROR"
                        raw["jev"] = candidate["jev"]
                        candidate["raw"] = raw
                    continue
                for (_, candidate), result in zip(chunk, results):
                    candidate["classification_status"] = "CLASSIFIED"
                    candidate["jev"] = result
                    candidate["route"] = result.get("route")
                    raw = dict(candidate.get("raw") or {})
                    raw["jev"] = result
                    raw["classification_status"] = "CLASSIFIED"
                    candidate["raw"] = raw

        counts = {route: sum(1 for candidate in candidates if candidate.get("route") == route) for route in ("A", "B", "C")}
        classified = sum(1 for candidate in candidates if candidate.get("classification_status") == "CLASSIFIED")
        status = "COMPLETED" if classified == len(candidates) else "PARTIAL" if classified else "FAILED"
        return {
            "status": status,
            "classified": classified,
            **counts,
            "errors": errors,
            "message": f"JEV 有 {errors} 条候选分类失败；可在批次中复核或重试。" if errors else None,
        }

    def get_ip_daily_monitor(self, workspace_id: str) -> dict[str, Any] | None:
        return self.db.one(
            """SELECT * FROM topic_monitors
            WHERE workspace_id=? AND monitor_kind='ip_daily'
            ORDER BY updated_at DESC LIMIT 1""",
            (workspace_id,),
        )

    def ensure_ip_daily_monitor(self, workspace_id: str, interval_hours: int = 24) -> dict[str, Any]:
        """Create or reactivate the single daily, profile-routed monitor.

        This stores no copy of profile JSON.  It schedules a first run now;
        the caller can invoke ``run_monitor`` synchronously for an immediate
        result and the local worker handles subsequent daily checks.
        """
        interval = min(max(int(interval_hours), 1), 168)
        now = utc_now()
        with self.db.transaction() as connection:
            existing = connection.execute(
                "SELECT id FROM topic_monitors WHERE workspace_id=? AND monitor_kind='ip_daily' LIMIT 1",
                (workspace_id,),
            ).fetchone()
            if existing:
                monitor_id = str(existing["id"])
                connection.execute(
                    """UPDATE topic_monitors
                    SET enabled=1,interval_hours=?,next_run_at=?,updated_at=? WHERE id=?""",
                    (interval, now, now, monitor_id),
                )
            else:
                monitor_id = new_id("monitor")
                connection.execute(
                    """INSERT INTO topic_monitors
                    (id,workspace_id,monitor_kind,query,platforms_json,interval_hours,enabled,next_run_at,last_status,created_at,updated_at)
                    VALUES(?,?, 'ip_daily', 'IP 自动热点', ?,?,1,?,'IDLE',?,?)""",
                    (monitor_id, workspace_id, json_dumps(["web"]), interval, now, now, now),
                )
        return self.get_monitor(monitor_id, workspace_id)

    def get_monitor(self, monitor_id: str, workspace_id: str | None = None) -> dict[str, Any]:
        sql = "SELECT * FROM topic_monitors WHERE id=?"
        params: tuple[Any, ...] = (monitor_id,)
        if workspace_id:
            sql += " AND workspace_id=?"
            params = (monitor_id, workspace_id)
        row = self.db.one(sql, params)
        if not row:
            raise KeyError("监测主题不存在")
        return row

    def list_monitors(self, workspace_id: str) -> list[dict[str, Any]]:
        return self.db.all(
            """SELECT m.*,
            (SELECT COUNT(*) FROM topic_monitor_items i
             WHERE i.run_id=(SELECT r.id FROM topic_monitor_runs r WHERE r.monitor_id=m.id ORDER BY r.started_at DESC LIMIT 1)
            ) AS latest_item_count
            FROM topic_monitors m WHERE m.workspace_id=? AND m.monitor_kind='ip_daily' ORDER BY m.updated_at DESC""",
            (workspace_id,),
        )

    def update_monitor(self, monitor_id: str, workspace_id: str, *, enabled: bool | None = None, interval_hours: int | None = None) -> dict[str, Any]:
        monitor = self.get_monitor(monitor_id, workspace_id)
        fields: dict[str, Any] = {"updated_at": utc_now()}
        if enabled is not None:
            fields["enabled"] = int(enabled)
            if enabled and not monitor.get("next_run_at"):
                fields["next_run_at"] = utc_now()
        if interval_hours is not None:
            fields["interval_hours"] = min(max(int(interval_hours), 1), 168)
            if bool(monitor.get("enabled")):
                fields["next_run_at"] = _next_run_at(fields["interval_hours"])
        clause = ",".join(f"{key}=?" for key in fields)
        self.db.execute(f"UPDATE topic_monitors SET {clause} WHERE id=?", (*fields.values(), monitor_id))
        return self.get_monitor(monitor_id, workspace_id)

    def list_runs(self, monitor_id: str, workspace_id: str, limit: int = 12) -> list[dict[str, Any]]:
        self.get_monitor(monitor_id, workspace_id)
        rows = self.db.all(
            "SELECT * FROM topic_monitor_runs WHERE monitor_id=? AND workspace_id=? ORDER BY started_at DESC LIMIT ?",
            (monitor_id, workspace_id, min(max(limit, 1), 50)),
        )
        for row in rows:
            row["items"] = self.db.all(
                "SELECT * FROM topic_monitor_items WHERE run_id=? ORDER BY platform, discovered_at DESC",
                (row["id"],),
            )
        return rows

    def recover_interrupted_runs(self) -> int:
        """Close RUNNING rows left by a stopped or upgraded server process."""
        rows = self.db.all("SELECT id,monitor_id FROM topic_monitor_runs WHERE status='RUNNING'")
        if not rows:
            return 0
        now = utc_now()
        message = "上次刷新因工作台重启而中断，请重新刷新"
        with self.db.transaction() as connection:
            for row in rows:
                connection.execute(
                    "UPDATE topic_monitor_runs SET status='FAILED',error_message=?,completed_at=? WHERE id=?",
                    (message, now, row["id"]),
                )
                connection.execute(
                    """UPDATE topic_monitors
                    SET last_status='FAILED',last_error=?,updated_at=?
                    WHERE id=? AND last_status='RUNNING'""",
                    (message, now, row["monitor_id"]),
                )
        return len(rows)

    def start_monitor(self, monitor_id: str, workspace_id: str | None = None) -> dict[str, Any]:
        """Start a refresh in the background and return its RUNNING row quickly."""
        monitor = self.get_monitor(monitor_id, workspace_id)
        with self._lock:
            if monitor_id in self._running:
                raise RadarBusyError("热点正在更新，完成后会自动显示")
            self._running.add(monitor_id)
        started = threading.Event()

        def run() -> None:
            try:
                self.run_monitor(
                    monitor_id,
                    str(monitor["workspace_id"]),
                    _already_claimed=True,
                    _started_event=started,
                )
            finally:
                # Also release a claim if a database error happens before
                # run_monitor reaches its own cleanup block.
                with self._lock:
                    self._running.discard(monitor_id)
                started.set()

        threading.Thread(target=run, daemon=True, name=f"topic-radar-{monitor_id[-8:]}").start()
        started.wait(timeout=1.0)
        runs = self.list_runs(monitor_id, str(monitor["workspace_id"]), limit=1)
        if not runs:
            raise RadarError("热点刷新未能启动，请稍后重试")
        return runs[0]

    def discover_topic(self, workspace_id: str, topic: str, limit: int = 8) -> dict[str, Any]:
        """Search both live platform pages for a user-requested theme.

        A topic result is actionable only when both platform pages were read in
        this round.  Public web indexes are useful diagnostics, but presenting
        them as a fresh Xiaohongshu/Douyin result is exactly the stale-result
        failure this flow must avoid.
        """
        clean_topic = _text(topic, 180)
        if not clean_topic:
            raise ValueError("请填写要搜索的主题")
        if len(clean_topic) < 2:
            raise ValueError("主题至少需要 2 个字")
        wanted = min(max(int(limit), 1), 12)
        rows: list[dict[str, Any]] = []
        summaries: list[dict[str, Any]] = []
        warnings: list[str] = []
        for platform in ("xiaohongshu", "douyin"):
            platform_rows, summary, error = self._platform_sources(
                workspace_id, platform, clean_topic, wanted,
                require_live_verification=True,
                allow_cached_fallback=False,
                allow_public_index_fallback=False,
            )
            rows.extend(platform_rows)
            summaries.append(summary)
            if error:
                warnings.append(error)
        platform_links = _official_platform_search_links(clean_topic)
        both_live_verified = all(
            bool(summary.get("live_verified")) and summary.get("verification_status") == "LIVE_VERIFIED"
            for summary in summaries
        )
        profile_row = self.db.one(
            "SELECT profile_json FROM brand_profiles WHERE workspace_id=?", (workspace_id,)
        ) or {}
        profile = json_loads(profile_row.get("profile_json"), {}) or {}
        active_persona = self._persona_for_direction(profile if isinstance(profile, dict) else {})
        direction = {"persona_id": active_persona.get("id")}
        classification = self._classify_candidates(
            workspace_id,
            profile if isinstance(profile, dict) else {},
            rows,
            direction_by_query={clean_topic: direction},
        )
        angles = self._topic_angles(clean_topic, rows, platform_links) if both_live_verified else []
        visible_count = sum(1 for row in rows if row.get("source_mode") in LIVE_PLATFORM_SOURCE_MODES)
        result = {
            "topic": clean_topic,
            "sources": rows,
            "angles": angles,
            "platform_search_links": platform_links,
            "platforms": summaries,
            "search_mode": MANAGED_BACKGROUND_SOURCE_MODE if visible_count else "live_browser_unverified",
            "source_count": len(rows),
            "visible_source_count": visible_count,
            "live_verified": both_live_verified,
            "jev_classification": classification,
            "message": (
                f"已完成双平台实时读取，共看到 {visible_count} 条平台内容；JEV 分为 A/B/C 后生成 {len(angles)} 个优先主题。"
                if both_live_verified and classification["status"] == "COMPLETED"
                else "本轮尚未同时完成双平台实时读取或 JEV 人设/业务分类；不会把未分类内容当作优先创作依据。"
            ),
            "warning": "；".join(dict.fromkeys([
                *warnings,
                *([str(classification.get("message"))] if classification.get("message") else []),
            ])) or None,
        }
        if self.on_results_ready:
            try:
                self.on_results_ready(workspace_id, result)
            except Exception:
                # Automatic copy extraction must never hide usable radar results.
                pass
        return result

    def list_favorites(self, workspace_id: str, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.db.all(
            """SELECT * FROM radar_favorites WHERE workspace_id=?
            ORDER BY updated_at DESC LIMIT ?""",
            (workspace_id, max(1, min(int(limit), 300))),
        )
        return [self._decode_favorite(row) for row in rows]

    def favorite_source(
        self,
        workspace_id: str,
        *,
        source_item_id: str | None = None,
        platform: str | None = None,
        url: str | None = None,
    ) -> dict[str, Any]:
        """Persist a radar-card snapshot after resolving it inside its workspace."""
        item: dict[str, Any] | None
        if source_item_id:
            item = self.db.one(
                "SELECT * FROM topic_monitor_items WHERE workspace_id=? AND id=?",
                (workspace_id, source_item_id),
            )
        else:
            item = self.db.one(
                """SELECT * FROM topic_monitor_items WHERE workspace_id=? AND platform=? AND url=?
                ORDER BY discovered_at DESC LIMIT 1""",
                (workspace_id, str(platform or ""), str(url or "")),
            )
        if not item:
            raise ValueError("这篇帖子不属于当前工作区的内容雷达，无法收藏")
        now = utc_now()
        favorite_id = new_id("radar_fav")
        self.db.execute(
            """INSERT INTO radar_favorites
            (id,workspace_id,platform,source_key,source_item_id,url,title,author,excerpt,metrics_json,raw_json,note,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,'',?,?)
            ON CONFLICT(workspace_id,platform,source_key) DO UPDATE SET
              source_item_id=excluded.source_item_id,url=excluded.url,title=excluded.title,author=excluded.author,
              excerpt=excluded.excerpt,metrics_json=excluded.metrics_json,raw_json=excluded.raw_json,updated_at=excluded.updated_at""",
            (
                favorite_id, workspace_id, str(item["platform"]), str(item["source_key"]), str(item["id"]),
                str(item.get("url") or ""), str(item.get("title") or ""), str(item.get("author") or ""),
                str(item.get("excerpt") or ""), str(item.get("metrics_json") or "{}"),
                str(item.get("raw_json") or "{}"), now, now,
            ),
        )
        result = self.db.one(
            "SELECT * FROM radar_favorites WHERE workspace_id=? AND platform=? AND source_key=?",
            (workspace_id, str(item["platform"]), str(item["source_key"])),
        )
        return self._decode_favorite(result or {})

    def favorite_xiaohongshu_extraction(
        self,
        workspace_id: str,
        extraction: dict[str, Any],
    ) -> dict[str, Any]:
        """Save a favorite for a manual search card using server-owned data.

        One-off topic-search cards are not rows in ``topic_monitor_items``.
        They do, however, enter the workspace-scoped Xiaohongshu archive queue
        before the UI can offer this action.  The browser only sends an
        identity snapshot; this method ignores its display text and derives
        the persisted favorite from that already-known extraction.  That keeps
        arbitrary client text, cookies, signed URLs, and cross-workspace
        sources out of the favorites store.
        """
        if str(extraction.get("workspace_id") or "") != workspace_id:
            raise ValueError("这篇帖子不属于当前工作区的内容雷达，无法收藏")
        url = str(extraction.get("url") or "")
        parsed = urllib.parse.urlsplit(url)
        host = (parsed.hostname or "").lower()
        if (
            parsed.scheme != "https"
            or not (host == "xiaohongshu.com" or host.endswith(".xiaohongshu.com"))
            or not re.search(r"/(?:explore|discovery/item)/[A-Za-z0-9]+", parsed.path)
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("小红书原帖身份校验未通过，无法收藏")

        source_key = _stable_key(url)
        now = utc_now()
        archive = extraction.get("archive") if isinstance(extraction.get("archive"), dict) else {}
        verified_body = bool(
            archive
            and archive.get("verification_status") == "VERIFIED"
            and archive.get("body_raw") is not None
        )
        # This is a card hint only.  The original body remains in its archive
        # and is never normalized, duplicated, or mixed into a favorite.
        excerpt = (
            "已保存原帖来源；可在归档库查看已校验的逐字正文。"
            if verified_body else "已保存原帖来源；归档完成后可查看逐字正文并改写。"
        )
        metrics = extraction.get("metrics") if isinstance(extraction.get("metrics"), dict) else {}
        metadata = {
            "source_kind": "xiaohongshu_extraction",
            "source_extraction_id": str(extraction.get("id") or ""),
            "source_archive_id": str((archive or {}).get("id") or ""),
            "archive_status": str((archive or {}).get("archive_status") or ""),
            "verification_status": str((archive or {}).get("verification_status") or ""),
        }

        # A source may already have been favorited from a daily radar card
        # whose source key differs from the normalized post URL.  Reuse that
        # row so the same post is never displayed twice in the library.
        existing = self.db.one(
            """SELECT * FROM radar_favorites
            WHERE workspace_id=? AND platform='xiaohongshu' AND url=?
            ORDER BY updated_at DESC LIMIT 1""",
            (workspace_id, url),
        )
        if existing:
            self.db.execute(
                """UPDATE radar_favorites SET title=?,author=?,excerpt=?,metrics_json=?,raw_json=?,updated_at=?
                WHERE workspace_id=? AND id=?""",
                (
                    _text(extraction.get("title"), 500), _text(extraction.get("author"), 300), excerpt,
                    json_dumps(metrics), json_dumps(metadata), now, workspace_id, existing["id"],
                ),
            )
            refreshed = self.db.one(
                "SELECT * FROM radar_favorites WHERE workspace_id=? AND id=?", (workspace_id, existing["id"]),
            )
            return self._decode_favorite(refreshed or {})

        favorite_id = new_id("radar_fav")
        self.db.execute(
            """INSERT INTO radar_favorites
            (id,workspace_id,platform,source_key,source_item_id,url,title,author,excerpt,metrics_json,raw_json,note,created_at,updated_at)
            VALUES(?,?,?,?,NULL,?,?,?,?,?,?, '',?,?)""",
            (
                favorite_id, workspace_id, "xiaohongshu", source_key, url,
                _text(extraction.get("title"), 500), _text(extraction.get("author"), 300), excerpt,
                json_dumps(metrics), json_dumps(metadata), now, now,
            ),
        )
        result = self.db.one(
            "SELECT * FROM radar_favorites WHERE workspace_id=? AND id=?", (workspace_id, favorite_id),
        )
        return self._decode_favorite(result or {})

    def remove_favorite(self, workspace_id: str, favorite_id: str) -> bool:
        return bool(self.db.execute(
            "DELETE FROM radar_favorites WHERE workspace_id=? AND id=?",
            (workspace_id, favorite_id),
        ))

    @staticmethod
    def _decode_favorite(row: dict[str, Any]) -> dict[str, Any]:
        value = dict(row)
        value["metrics"] = json_loads(value.pop("metrics_json", None), {}) or {}
        value["raw"] = json_loads(value.pop("raw_json", None), {}) or {}
        return value

    @staticmethod
    def _is_transient_platform_search_error(error: PlatformBrowserError) -> bool:
        return str(error.code or "").lower() in TRANSIENT_PLATFORM_SEARCH_CODES

    @staticmethod
    def _verification_status_for_platform_error(code: str) -> str:
        normalized = str(code or "").lower()
        if normalized in {"browser_not_running", "browser_page_required", "login_required", "verification_required", "platform_limited"}:
            return "USER_ACTION_REQUIRED"
        return "UNVERIFIED"

    def _platform_sources(
        self, workspace_id: str, platform: str, query: str, limit: int,
        *, minimum_relevance_hits: int | None = None, exclude_source_keys: set[str] | None = None,
        require_live_verification: bool = False,
        allow_cached_fallback: bool = True,
        allow_public_index_fallback: bool = True,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any], str | None]:
        """Read platform cards with an explicit verification contract.

        A normal one-off topic lookup can still offer clearly-labelled public
        index results when the local browser is unavailable.  The daily IP
        radar deliberately passes ``require_live_verification=True``: no
        cached or public-index card may be saved as this refresh's result.
        """
        excluded = exclude_source_keys or set()
        browser_error: PlatformBrowserError | None = None
        raw_rows: list[dict[str, Any]] | None = None
        attempts = 0
        for attempts in range(1, PLATFORM_SEARCH_MAX_ATTEMPTS + 1):
            if progress_callback:
                progress_callback({
                    "stage": "verifying_platform",
                    "platform": platform,
                    "attempt": attempts,
                    "max_attempts": PLATFORM_SEARCH_MAX_ATTEMPTS,
                    "message": (
                        f"正在后台核验{'小红书' if platform == 'xiaohongshu' else '抖音'}"
                        f"（第 {attempts}/{PLATFORM_SEARCH_MAX_ATTEMPTS} 次）…"
                    ),
                })
            try:
                raw_rows = self.browser.search(platform, query, min(max(limit * 3, limit), 24))
                browser_error = None
                break
            except PlatformBrowserError as error:
                browser_error = error
                if self._is_transient_platform_search_error(error) and attempts < PLATFORM_SEARCH_MAX_ATTEMPTS:
                    # Do not re-use an old card while CDP is briefly busy.  A
                    # single short retry is enough to cover Chrome target
                    # hand-off without creating an uncontrolled retry loop.
                    time.sleep(0.35 * attempts)
                    continue
                break

        if raw_rows is not None:
            rows: list[dict[str, Any]] = []
            for row in raw_rows:
                relevance = _relevance_score(row, query, minimum_hits=minimum_relevance_hits)
                if relevance < 0:
                    continue
                raw = dict(row.get("raw") or {})
                raw.update({
                    "search_query": query,
                    "relevance_score": relevance,
                    "account_relation": f"与本轮主题“{query}”直接相关",
                    "content_direction": "提炼原帖中的具体处境与争议，再用当前相亲 IP 的判断给出可验证建议。",
                })
                enriched = {
                    **row,
                    "platform": platform,
                    "source_mode": MANAGED_BACKGROUND_SOURCE_MODE,
                    "credibility": 0.8,
                    "search_mode": MANAGED_BACKGROUND_SOURCE_MODE,
                    "matched_query": query,
                    "raw": raw,
                    "metadata": {
                        "platform": platform,
                        "search_mode": MANAGED_BACKGROUND_SOURCE_MODE,
                        "metrics": row.get("metrics") or {},
                        "author": row.get("author") or "",
                        "account_relation": raw["account_relation"],
                        "content_direction": raw["content_direction"],
                    },
                }
                enriched["raw"]["heat_score"] = round(_heat_score(enriched), 2)
                rows.append(enriched)
            rows.sort(
                key=lambda row: (
                    _heat_score(row),
                    int((row.get("raw") or {}).get("relevance_score") or 0),
                    -int((row.get("raw") or {}).get("rank") or 999),
                ),
                reverse=True,
            )
            candidate_count = len(rows)
            previously_seen_count = sum(1 for row in rows if str(row.get("source_key") or "") in excluded)
            if excluded:
                rows = [row for row in rows if str(row.get("source_key") or "") not in excluded]
            rows = rows[:limit]
            return rows, {
                "platform": platform,
                "status": "success" if rows else "empty",
                "count": len(rows),
                "source_mode": MANAGED_BACKGROUND_SOURCE_MODE,
                "candidate_count": candidate_count,
                "previously_seen_count": previously_seen_count,
                "freshness_mode": "new_only" if excluded else "all",
                "verification_status": "LIVE_VERIFIED",
                "live_verified": True,
                "attempt_count": attempts,
                "message": (
                    f"已完成本轮实时核验，按相关性和页面可见互动数筛出 {len(rows)} 条热门内容。"
                    if rows else (
                        f"已完成本轮实时核验，但没有新的相关帖子，已排除 {previously_seen_count} 条此前展示过的内容。"
                        if excluded else "已完成本轮实时核验，但没有找到相关内容。"
                    )
                ),
            }, None

        browser_error = browser_error or PlatformBrowserError("平台搜索暂时不可用", code="page_read_error")
        browser_message = str(browser_error)
        browser_code = str(browser_error.code or "browser_error").lower()
        verification_status = self._verification_status_for_platform_error(browser_code)
        action_required = verification_status == "USER_ACTION_REQUIRED"

        # The daily card must reflect this run only.  In particular, a live
        # CDP connection with a timeout is not evidence that the old page was
        # checked today, so cached cards and public indexes are both disabled.
        if require_live_verification:
            return [], {
                "platform": platform,
                "status": "user_action_required" if action_required else "error",
                "count": 0,
                "source_mode": "live_browser_unverified",
                "candidate_count": 0,
                "previously_seen_count": 0,
                "freshness_mode": "new_only" if excluded else "all",
                "verification_status": verification_status,
                "live_verified": False,
                "attempt_count": attempts,
                "message": (
                    f"本轮实时核验未完成：{browser_message}。请处理后重新刷新；历史帖子不会替代本轮结果。"
                ),
                "search_url": xiaohongshu_web_search_link(query) if platform == "xiaohongshu" else douyin_web_search_link(query),
            }, browser_message

        # Caching is useful for a manual, explicitly-labelled history view
        # after the user is asked to log in/verify.  It is intentionally not a
        # fallback for transient page/CDP failures: those must stay visible as
        # errors instead of looking like a fresh successful search.
        cache_allowed_for_error = browser_code in {"login_required", "verification_required", "platform_limited"}
        if allow_cached_fallback and cache_allowed_for_error:
            cached, cached_seen_count = self._cached_platform_sources(
                workspace_id, platform, query, limit,
                minimum_relevance_hits=minimum_relevance_hits,
                exclude_source_keys=excluded,
            )
            if cached:
                return cached, {
                    "platform": platform,
                    "status": "history_only", "count": len(cached),
                    "source_mode": "cached_visible_browser",
                    "previously_seen_count": cached_seen_count,
                    "freshness_mode": "new_only" if excluded else "all",
                    "verification_status": verification_status,
                    "live_verified": False,
                    "attempt_count": attempts,
                    "message": f"本轮实时页面需要登录或验证；以下是上次成功保存的 {len(cached)} 条历史内容，不代表本轮核验结果。",
                    "search_url": xiaohongshu_web_search_link(query) if platform == "xiaohongshu" else douyin_web_search_link(query),
                }, browser_message

        if allow_public_index_fallback:
            indexed, index_error = self._indexed_platform_sources(workspace_id, platform, query, limit)
            indexed_seen_count = sum(1 for row in indexed if str(row.get("source_key") or "") in excluded)
            if excluded:
                indexed = [row for row in indexed if str(row.get("source_key") or "") not in excluded]
            if indexed:
                return indexed, {
                    "platform": platform,
                    "status": "index_only", "count": len(indexed),
                    "source_mode": "public_index_fallback",
                    "previously_seen_count": indexed_seen_count,
                    "freshness_mode": "new_only" if excluded else "all",
                    "verification_status": verification_status,
                    "live_verified": False,
                    "attempt_count": attempts,
                    "message": f"本轮平台实时核验未完成；仅找到 {len(indexed)} 条公开网页索引，不能当作平台当前页面内容。",
                }, browser_message or index_error

        return [], {
            "platform": platform,
            "status": "user_action_required" if action_required else "error",
            "count": 0,
            "source_mode": "live_browser_unverified",
            "previously_seen_count": 0,
            "freshness_mode": "new_only" if excluded else "all",
            "verification_status": verification_status,
            "live_verified": False,
            "attempt_count": attempts,
            "message": f"本轮实时核验未完成：{browser_message}",
            "search_url": xiaohongshu_web_search_link(query) if platform == "xiaohongshu" else douyin_web_search_link(query),
        }, browser_message

    def _cached_platform_sources(
        self, workspace_id: str, platform: str, query: str, limit: int,
        *, minimum_relevance_hits: int | None = None, exclude_source_keys: set[str] | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Reuse prior visible cards when the platform temporarily requires verification."""
        rows = self.db.all(
            """SELECT source_key,title,url,excerpt,author,published_at,metrics_json,raw_json,discovered_at
            FROM topic_monitor_items
            WHERE workspace_id=? AND platform=?
              AND source_mode IN ('visible_browser', 'managed_background_browser') AND url!=''
            ORDER BY discovered_at DESC LIMIT 120""",
            (workspace_id, platform),
        )
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        excluded = exclude_source_keys or set()
        previously_seen_count = 0
        for row in rows:
            source_key = str(row.get("source_key") or "")
            if not source_key or source_key in seen:
                continue
            candidate = {
                "source_key": source_key,
                "title": _text(row.get("title"), 500),
                "url": str(row.get("url") or ""),
                "excerpt": _text(row.get("excerpt"), 2000),
                "author": _text(row.get("author"), 300),
                "published_at": row.get("published_at"),
                "metrics": json_loads(row.get("metrics_json"), {}) or {},
                "raw": json_loads(row.get("raw_json"), {}) or {},
            }
            relevance = _relevance_score(candidate, query, minimum_hits=minimum_relevance_hits)
            if relevance < 0:
                continue
            seen.add(source_key)
            if source_key in excluded:
                previously_seen_count += 1
                continue
            raw = dict(candidate["raw"])
            raw.update({
                "search_query": query,
                "relevance_score": relevance,
                "cached_from": row.get("discovered_at"),
                "heat_score": round(_heat_score(candidate), 2),
            })
            result.append({
                **candidate,
                "platform": platform,
                "source_mode": "cached_visible_browser",
                "search_mode": "cached_visible_browser",
                "matched_query": query,
                "credibility": 0.72,
                "raw": raw,
                "metadata": {
                    "platform": platform,
                    "search_mode": "cached_visible_browser",
                    "metrics": candidate["metrics"],
                    "author": candidate["author"],
                },
            })
        result.sort(
            key=lambda item: (_heat_score(item), int((item.get("raw") or {}).get("relevance_score") or 0)),
            reverse=True,
        )
        return result[:limit], previously_seen_count

    def _indexed_platform_sources(self, workspace_id: str, platform: str, query: str, limit: int) -> tuple[list[dict[str, Any]], str | None]:
        domain = "xiaohongshu.com/explore" if platform == "xiaohongshu" else "douyin.com/video"
        rows, error = self._public_sources(workspace_id, f"site:{domain} {query}", limit * 2)
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                host = (urllib.parse.urlsplit(str(row.get("url") or "")).hostname or "").lower()
            except ValueError:
                continue
            expected = "xiaohongshu.com" if platform == "xiaohongshu" else "douyin.com"
            if host != expected and not host.endswith(f".{expected}"):
                continue
            result.append({
                **row,
                "platform": platform,
                "source_mode": "public_index_fallback",
                "author": "",
                "metrics": {},
                "raw": {"search_mode": "public_index_fallback"},
                "metadata": {"platform": platform, "search_mode": "public_index_fallback"},
            })
            if len(result) >= limit:
                break
        return result, error

    def _public_sources(self, workspace_id: str, query: str, limit: int) -> tuple[list[dict[str, Any]], str | None]:
        try:
            raw_rows = self.public_search.search(workspace_id, query, limit)
        except Exception as exc:
            return [], sanitize_provider_text(str(exc), 180) or "公开搜索暂时不可用"
        if not isinstance(raw_rows, list):
            return [], None
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in raw_rows:
            if not isinstance(raw, dict):
                continue
            title = _text(raw.get("title"), 500)
            url = _valid_public_url(raw.get("url"))
            if not title or not url:
                continue
            source_key = _stable_key(url or title)
            if source_key in seen:
                continue
            seen.add(source_key)
            try:
                credibility = max(0.0, min(1.0, float(raw.get("credibility") or 0.5)))
            except (TypeError, ValueError):
                credibility = 0.5
            rows.append({
                "source_key": source_key,
                "title": title,
                "url": url,
                "excerpt": _text(raw.get("excerpt"), 2000),
                "published_at": str(raw.get("published_at") or "")[:100] or None,
                "credibility": credibility,
                "search_mode": _text(raw.get("search_mode") or "public_web", 80),
            })
            if len(rows) >= limit:
                break
        return rows, None

    @staticmethod
    def _topic_angles(
        topic: str,
        sources: list[dict[str, Any]],
        platform_links: list[dict[str, str]] | None = None,
    ) -> list[dict[str, Any]]:
        platform_links = platform_links or _official_platform_search_links(topic)
        platform_urls = [str(item["url"]) for item in platform_links]
        platform_titles = [str(item["label"]) for item in platform_links]
        angles: list[dict[str, Any]] = []
        source_rows = [
            row for row in sources
            if row.get("platform") in {"xiaohongshu", "douyin"}
            and row.get("classification_status") == "CLASSIFIED"
            and (row.get("jev") or {}).get("route") == "A"
        ]
        for source in source_rows[:8]:
            title = _text(source.get("title"), 100)
            if not title:
                continue
            angles.append({
                "route": "A",
                "title": title,
                "topic": topic,
                "reason": f"参考{('小红书' if source.get('platform') == 'xiaohongshu' else '抖音')}原内容《{title[:42]}》，换成你的目标受众能代入的现实场景。",
                "search_query": topic,
                "platform_search_links": platform_links,
                # The creation flow accepts HTTPS source URLs.  Only the two
                # platform-native search pages are carried forward; generic
                # web results remain background context and are never shown as
                # if they were platform content.
                "source_urls": [str(source.get("url") or "")] if source.get("url") else platform_urls,
                "source_titles": [title] if source.get("url") else platform_titles,
                "evidence_url": source.get("url"),
                "evidence_platform": source.get("platform"),
                "visible_metrics": source.get("metrics") or {},
            })
        # When actual platform posts exist, every visible angle must stay tied
        # to one of those posts.  Search-page links are navigation aids, not
        # evidence for a made-up recommendation.
        if angles:
            return angles
        return []

    def _run_ip_daily(
        self, monitor: dict[str, Any], *, progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        profile_row = self.db.one(
            "SELECT profile_json FROM brand_profiles WHERE workspace_id=?", (monitor["workspace_id"],)
        ) or {}
        profile = json_loads(profile_row.get("profile_json"), {}) or {}
        safe_profile = profile if isinstance(profile, dict) else {}
        anchors, profile_based = _ip_topic_anchors(safe_profile)
        directions = _daily_ip_directions(_profile_text(safe_profile), anchors)
        collected: list[dict[str, Any]] = []
        errors: list[str] = []
        platform_summaries: list[dict[str, Any]] = []
        seen_by_platform = self._daily_seen_source_keys(str(monitor["id"]))
        # One focused search per platform keeps the interactive refresh fast.
        # The two platforms use different profile directions, while all three
        # directions still contribute recommendation variants below.
        for platform in ("xiaohongshu", "douyin"):
            if progress_callback:
                progress_callback({
                    "stage": "starting_platform",
                    "platform": platform,
                    "message": f"准备读取{'小红书' if platform == 'xiaohongshu' else '抖音'}当前搜索页…",
                })
            platform_count = 0
            platform_results: list[dict[str, Any]] = []
            direction_index = 0 if platform == "xiaohongshu" else min(1, len(directions) - 1)
            for direction in directions[direction_index:direction_index + 1]:
                # Daily discovery is intentionally live-only.  A failure is
                # actionable state, not permission to re-publish yesterday's
                # cards or public-index snippets as today's verification.
                wanted = 12 if platform == "xiaohongshu" else 8
                rows, summary, error = self._platform_sources(
                    monitor["workspace_id"], platform, str(direction["search_query"]), wanted,
                    minimum_relevance_hits=1,
                    require_live_verification=True,
                    allow_cached_fallback=False,
                    allow_public_index_fallback=False,
                    progress_callback=progress_callback,
                )
                previously_seen = seen_by_platform.get(platform, set())
                for row in rows:
                    raw = dict(row.get("raw") or {})
                    raw.update({
                        "account_relation": _text(direction.get("persona"), 80),
                        "content_pillar": _text(direction.get("pillar"), 100),
                        "content_direction": (
                            f"围绕“{_text(direction.get('pillar'), 80) or _text(direction.get('title'), 80)}”"
                            "拆出原帖里的真实顾虑、冲突和可执行建议。"
                        ),
                        "seen_in_previous_runs": str(row.get("source_key") or "") in previously_seen,
                    })
                    row["raw"] = raw
                collected.extend(rows)
                platform_count += len(rows)
                platform_results.append(summary)
                if error and not bool(summary.get("live_verified")):
                    errors.append(error)

            last = platform_results[-1] if platform_results else {
                "platform": platform,
                "status": "error",
                "count": 0,
                "source_mode": "live_browser_unverified",
                "verification_status": "UNVERIFIED",
                "live_verified": False,
                "attempt_count": 0,
                "message": "本轮实时核验没有启动。",
            }
            previously_seen_count = sum(
                1 for row in collected
                if row.get("platform") == platform
                and str(row.get("source_key") or "") in seen_by_platform.get(platform, set())
            )
            attempt_count = sum(int(item.get("attempt_count") or 0) for item in platform_results)
            platform_summaries.append({
                **last,
                "platform": platform,
                "count": platform_count,
                "previously_seen_count": previously_seen_count,
                "freshness_mode": "current_snapshot",
                "attempt_count": attempt_count,
                "message": (
                    f"已完成本轮实时核验，按相关性和可见互动数筛出 {platform_count} 条热门内容。"
                    if bool(last.get("live_verified")) and platform_count else str(last.get("message") or "本轮实时核验未完成。")
                ),
            })
            if progress_callback:
                progress_callback({
                    "stage": "platform_complete",
                    "platform": platform,
                    "live_verified": bool(last.get("live_verified")),
                    "message": str(last.get("message") or "本轮平台核验已结束。"),
                })

        found: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for item in collected:
            key = str(item["source_key"])
            platform_key = (str(item.get("platform") or ""), key)
            if platform_key not in seen:
                seen.add(platform_key)
                found.append(item)

        directions_by_query = {
            str(direction.get("search_query") or ""): direction
            for direction in directions
        }
        classification = self._classify_candidates(
            str(monitor["workspace_id"]),
            safe_profile,
            found,
            direction_by_query=directions_by_query,
        )
        for item in found:
            raw = dict(item.get("raw") or {})
            raw["classification_status"] = item.get("classification_status")
            if item.get("jev"):
                raw["jev"] = item["jev"]
            item["raw"] = raw
        if classification.get("message"):
            errors.append(str(classification["message"]))

        visible_count = sum(1 for item in found if item.get("source_mode") in LIVE_PLATFORM_SOURCE_MODES)
        cached_count = sum(1 for item in found if item.get("source_mode") == "cached_visible_browser")
        public_count = sum(1 for item in found if item.get("source_mode") == "public_index_fallback")
        completed_platforms = [
            str(item.get("platform")) for item in platform_summaries if bool(item.get("live_verified"))
        ]
        message = (
            f"已完成 {('、'.join('小红书' if item == 'xiaohongshu' else '抖音' for item in completed_platforms) or '0 个')} 平台的本轮实时核验，其中 {visible_count} 条来自当前页面。"
            if profile_based else "尚未识别到可安全检索的 IP 方向，已先整理通用的双平台创作方向。"
        )
        return {
            "found": found,
            "errors": errors,
            "platforms": platform_summaries,
            "summary_extra": {
                "monitor_kind": "ip_daily",
                "profile_based": profile_based,
                "jev_classification": classification,
                "visible_signal_count": visible_count,
                "cached_signal_count": cached_count,
                "public_signal_count": public_count,
                "direction_count": len(directions),
                "freshness_mode": "current_snapshot",
                "previously_seen_count": sum(int(item.get("previously_seen_count") or 0) for item in platform_summaries),
                "message": message,
                "recommendations": self._ip_recommendations(directions, found, allow_profile_fallback=False),
            },
        }

    def _daily_seen_source_keys(self, monitor_id: str) -> dict[str, set[str]]:
        """Return previously surfaced source identities for a daily monitor.

        The result is keyed by platform so an accidental same hash on another
        platform cannot hide a genuinely new post.  We only use records from
        completed prior runs; the current run is created before collection.
        """
        rows = self.db.all(
            """SELECT platform,source_key FROM topic_monitor_items
            WHERE monitor_id=? AND source_key!=''""",
            (monitor_id,),
        )
        values: dict[str, set[str]] = {"xiaohongshu": set(), "douyin": set()}
        for row in rows:
            platform = str(row.get("platform") or "")
            source_key = str(row.get("source_key") or "")
            if platform in values and source_key:
                values[platform].add(source_key)
        return values

    @staticmethod
    def _ip_recommendations(
        directions: list[dict[str, str]], signals: list[dict[str, Any]] | None = None,
        *, allow_profile_fallback: bool = True,
    ) -> list[dict[str, Any]]:
        """Create up to twelve evidence-linked directions from visible platform cards."""
        signals = signals or []
        recommendations: list[dict[str, Any]] = []
        seen_titles: set[str] = set()
        # Alternate between the two platforms after sorting each one by its
        # visible engagement, so one platform cannot fill every card.
        eligible_signals = [
            item for item in signals
            if item.get("classification_status") == "CLASSIFIED"
            and (item.get("jev") or {}).get("route") == "A"
        ]
        by_platform = {
            platform: sorted(
                [item for item in eligible_signals if item.get("platform") == platform],
                key=_heat_score,
                reverse=True,
            )
            for platform in ("xiaohongshu", "douyin")
        }
        balanced_signals: list[dict[str, Any]] = []
        for index in range(6):
            for platform in ("xiaohongshu", "douyin"):
                if index < len(by_platform[platform]):
                    balanced_signals.append(by_platform[platform][index])

        for signal in balanced_signals[:12]:
            source_title = _text(signal.get("title"), 100)
            if not source_title:
                continue
            platform = str(signal.get("platform") or "")
            label = "小红书" if platform == "xiaohongshu" else "抖音"
            matched_query = _text(signal.get("matched_query") or (signal.get("raw") or {}).get("search_query"), 180)
            direction = next(
                (item for item in directions if _text(item.get("search_query"), 180) == matched_query),
                directions[len(recommendations) % max(1, len(directions))],
            )
            title = f"从{label}热帖《{source_title[:34]}》延伸：用户真正关心的是什么？"
            if title in seen_titles:
                continue
            seen_titles.add(title)
            query = matched_query or _text(direction.get("search_query"), 180)
            metrics = signal.get("metrics") or {}
            metric_label = ""
            cached = signal.get("source_mode") == "cached_visible_browser"
            if metrics.get("likes") is not None:
                metric_label = f"{'上次成功采集时' if cached else '当前搜索页'}可见点赞 {metrics['likes']}"
            elif metrics.get("visible_engagement") is not None:
                metric_label = f"{'上次成功采集时' if cached else '当前搜索页'}可见互动 {metrics['visible_engagement']}"
            evidence_phrase = "以此前保存的平台真实表达为证据" if cached else "以平台当前展示的真实表达为证据"
            recommendations.append({
                "route": "A",
                "title": title,
                "topic": title,
                "persona": _text(direction.get("persona"), 80),
                "pillar": _text(direction.get("pillar"), 100),
                "search_query": query,
                "reason": f"{metric_label + '；' if metric_label else ''}{evidence_phrase}，拆出它击中的顾虑、场景和可执行建议，再改写成你的账号视角。",
                "platform_search_links": _official_platform_search_links(query),
                "source_urls": [str(signal.get("url") or "")],
                "source_titles": [source_title],
                "evidence_url": signal.get("url"),
                "evidence_platform": platform,
                "visible_metrics": signal.get("metrics") or {},
            })
        # Only use profile-only prompts when the platform returned no usable
        # post at all.  This prevents cards with a title but no matching body.
        if recommendations:
            return recommendations[:12]
        # A daily refresh must never disguise a stale or profile-only angle as
        # a new source-backed post.  Manual callers can still opt into the
        # historic profile fallback below.
        if not allow_profile_fallback:
            return []
        for direction in directions[:3]:
            query = _text(direction.get("search_query"), 180)
            title = _text(direction.get("title"), 160)
            if not query or not title or title in seen_titles:
                continue
            seen_titles.add(title)
            platform_links = _official_platform_search_links(query)
            recommendations.append({
                "title": title,
                "topic": title,
                "persona": _text(direction.get("persona"), 80),
                "pillar": _text(direction.get("pillar"), 100),
                "search_query": query,
                "reason": _text(direction.get("reason"), 220),
                "platform_search_links": platform_links,
                # Keep the normal source-url contract compatible with task
                # creation, but only with official platform search pages.
                "source_urls": [item["url"] for item in platform_links],
                "source_titles": [item["label"] for item in platform_links],
                "public_signal_count": sum(1 for item in signals if item.get("source_mode") not in LIVE_PLATFORM_SOURCE_MODES),
            })
        variants = (
            ("最容易被忽略的 3 个细节", "把高频讨论拆成可观察的细节，适合清单式内容。"),
            ("为什么有人愿意尝试，有人一看就退出？", "从参与门槛、信任和风险感解释同一主题下的不同选择。"),
            ("第一次遇到这个问题，先做哪一步？", "给新人一条低风险、可验证的最小行动。"),
        )
        for direction in directions:
            query = _text(direction.get("search_query"), 180)
            for suffix, reason in variants:
                if len(recommendations) >= 12:
                    break
                title = f"{_text(direction.get('pillar'), 60)}：{suffix}"
                if title in seen_titles:
                    continue
                seen_titles.add(title)
                links = _official_platform_search_links(query)
                recommendations.append({
                    "title": title, "topic": title,
                    "persona": _text(direction.get("persona"), 80),
                    "pillar": _text(direction.get("pillar"), 100),
                    "search_query": query, "reason": reason,
                    "platform_search_links": links,
                    "source_urls": [item["url"] for item in links],
                    "source_titles": [item["label"] for item in links],
                })
        return recommendations[:12]

    def run_due(self) -> int:
        now = utc_now()
        monitors = self.db.all(
            """SELECT id FROM topic_monitors
            WHERE enabled=1 AND monitor_kind='ip_daily' AND (next_run_at IS NULL OR next_run_at<=?)
            ORDER BY COALESCE(next_run_at, created_at) ASC LIMIT 8""",
            (now,),
        )
        complete = 0
        for monitor in monitors:
            try:
                self.run_monitor(str(monitor["id"]))
                complete += 1
            except RadarBusyError:
                continue
            except Exception:
                # Individual errors are written to the run record and should
                # never stop the local scheduler from checking other topics.
                continue
        return complete

    def run_monitor(
        self,
        monitor_id: str,
        workspace_id: str | None = None,
        *,
        _already_claimed: bool = False,
        _started_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        monitor = self.get_monitor(monitor_id, workspace_id)
        if not _already_claimed:
            with self._lock:
                if monitor_id in self._running:
                    raise RadarBusyError("热点正在更新，完成后会自动显示")
                self._running.add(monitor_id)
        run_id, now = new_id("monitor_run"), utc_now()
        self.db.execute(
            "INSERT INTO topic_monitor_runs(id,monitor_id,workspace_id,status,started_at) VALUES(?,?,?,'RUNNING',?)",
            (run_id, monitor_id, monitor["workspace_id"], now),
        )
        self.db.execute(
            "UPDATE topic_monitors SET last_status='RUNNING',last_error=NULL,updated_at=? WHERE id=?",
            (now, monitor_id),
        )
        kind = str(monitor.get("monitor_kind") or "manual")

        def update_progress(value: dict[str, Any]) -> None:
            """Persist a heartbeat so a slow platform page is never a blank RUNNING state."""
            progress = {
                "in_progress": True,
                "heartbeat_at": utc_now(),
                **value,
            }
            payload = {"monitor_kind": kind, "progress": progress}
            self.db.execute(
                "UPDATE topic_monitor_runs SET summary_json=? WHERE id=? AND status='RUNNING'",
                (json_dumps(payload), run_id),
            )

        update_progress({"stage": "starting", "message": "正在准备本轮实时平台核验…"})
        if _started_event:
            _started_event.set()
        try:
            extra_summary: dict[str, Any] = {"monitor_kind": kind}
            if kind == "ip_daily":
                daily = self._run_ip_daily(monitor, progress_callback=update_progress)
                platform_summaries = daily["platforms"]
                found = daily["found"]
                errors = daily["errors"]
                extra_summary.update(daily["summary_extra"])
            else:
                platforms = _clean_platforms(json_loads(monitor.get("platforms_json"), []))
                platform_summaries: list[dict[str, Any]] = []
                found: list[dict[str, Any]] = []
                errors: list[str] = []
                for platform in platforms:
                    if platform == "web":
                        rows, error = self._public_sources(monitor["workspace_id"], str(monitor["query"]), 8)
                        if error:
                            errors.append(error)
                            platform_summaries.append({
                                "platform": "web", "status": "error", "count": 0,
                                "source_mode": "public_web", "message": error,
                            })
                        else:
                            found.extend({
                                "platform": "web", "source_mode": "public_web", "source_key": row["source_key"],
                                "title": row["title"], "url": row["url"], "excerpt": row["excerpt"],
                                "author": "", "published_at": row["published_at"],
                                "metrics": {"credibility": row["credibility"]},
                                "raw": {"search_mode": row["search_mode"]},
                            } for row in rows)
                            platform_summaries.append({
                                "platform": "web", "status": "success", "count": len(rows),
                                "source_mode": "public_web",
                            })
                        continue
                    rows, summary, error = self._platform_sources(
                        monitor["workspace_id"], platform, str(monitor["query"]), 12,
                    )
                    found.extend(rows)
                    platform_summaries.append(summary)
                    if error:
                        errors.append(error)

            if kind != "ip_daily":
                profile_row = self.db.one(
                    "SELECT profile_json FROM brand_profiles WHERE workspace_id=?", (monitor["workspace_id"],)
                ) or {}
                profile = json_loads(profile_row.get("profile_json"), {}) or {}
                classification = self._classify_candidates(
                    str(monitor["workspace_id"]),
                    profile if isinstance(profile, dict) else {},
                    found,
                )
                extra_summary["jev_classification"] = classification
                if classification.get("message"):
                    errors.append(str(classification["message"]))

            new_count = self._save_items(run_id, monitor, found)
            # A daily new-only run with no unseen cards is valid *only after
            # both platform pages were actually read.  A CDP connection or a
            # cached card is not a completed verification boundary.
            if kind == "ip_daily":
                daily_live_complete = bool(platform_summaries) and all(
                    bool(item.get("live_verified")) and item.get("status") in {"success", "empty"}
                    for item in platform_summaries
                )
                any_daily_live = any(bool(item.get("live_verified")) for item in platform_summaries)
                jev_complete = extra_summary.get("jev_classification", {}).get("status") in {"COMPLETED", "NO_CANDIDATES"}
                status = "DONE" if daily_live_complete and jev_complete and not errors else "PARTIAL" if any_daily_live else "FAILED"
            else:
                has_success = any(item["status"] in {"success", "empty", "index_only", "history_only"} for item in platform_summaries)
                has_action = any(item["status"] in {"manual_search", "login_required", "user_action_required"} for item in platform_summaries)
                jev_complete = extra_summary.get("jev_classification", {}).get("status") in {"COMPLETED", "NO_CANDIDATES"}
                status = "DONE" if has_success and jev_complete and not errors else "PARTIAL" if has_success or has_action else "FAILED"
            summary = {
                "query": monitor["query"],
                "total": len(found),
                "new_count": new_count,
                "platforms": platform_summaries,
            }
            summary.update(extra_summary)
            completed_at = utc_now()
            next_run = _next_run_at(int(monitor.get("interval_hours") or 24)) if bool(monitor.get("enabled")) else None
            self.db.execute(
                """UPDATE topic_monitor_runs SET status=?,summary_json=?,error_message=?,completed_at=? WHERE id=?""",
                (status, json_dumps(summary), "；".join(errors)[:1000] or None, completed_at, run_id),
            )
            self.db.execute(
                """UPDATE topic_monitors
                SET last_run_at=?,next_run_at=?,last_status=?,last_error=?,updated_at=? WHERE id=?""",
                (completed_at, next_run, status, "；".join(errors)[:1000] or None, completed_at, monitor_id),
            )
            result = self.get_run(run_id, monitor["workspace_id"])
            if self.on_results_ready:
                try:
                    self.on_results_ready(str(monitor["workspace_id"]), result)
                except Exception:
                    # The radar result is complete even if its optional local
                    # extraction queue cannot be updated at this moment.
                    pass
            return result
        except Exception as exc:
            message = sanitize_provider_text(str(exc), 500) or "内容雷达本次刷新失败"
            completed_at = utc_now()
            next_run = _next_run_at(int(monitor.get("interval_hours") or 24)) if bool(monitor.get("enabled")) else None
            self.db.execute(
                "UPDATE topic_monitor_runs SET status='FAILED',error_message=?,completed_at=? WHERE id=?",
                (message, completed_at, run_id),
            )
            self.db.execute(
                """UPDATE topic_monitors SET last_run_at=?,next_run_at=?,last_status='FAILED',last_error=?,updated_at=? WHERE id=?""",
                (completed_at, next_run, message, completed_at, monitor_id),
            )
            return self.get_run(run_id, monitor["workspace_id"])
        finally:
            with self._lock:
                self._running.discard(monitor_id)

    def get_run(self, run_id: str, workspace_id: str) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM topic_monitor_runs WHERE id=? AND workspace_id=?", (run_id, workspace_id))
        if not row:
            raise KeyError("监测记录不存在")
        row["items"] = self.db.all(
            "SELECT * FROM topic_monitor_items WHERE run_id=? ORDER BY platform, discovered_at DESC",
            (run_id,),
        )
        return row

    def _save_items(self, run_id: str, monitor: dict[str, Any], items: list[dict[str, Any]]) -> int:
        now = utc_now()
        new_count = 0
        for item in items:
            source_key = str(item.get("source_key") or _stable_key(str(item.get("url") or item.get("title") or "")))
            prior = self.db.one(
                "SELECT id FROM topic_monitor_items WHERE monitor_id=? AND platform=? AND source_key=? LIMIT 1",
                (monitor["id"], item["platform"], source_key),
            )
            is_new = prior is None
            if is_new:
                new_count += 1
            self.db.execute(
                """INSERT OR IGNORE INTO topic_monitor_items
                (id,run_id,monitor_id,workspace_id,platform,source_mode,source_key,title,url,excerpt,author,published_at,metrics_json,raw_json,is_new,discovered_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    new_id("monitor_item"), run_id, monitor["id"], monitor["workspace_id"], item["platform"],
                    item["source_mode"], source_key, str(item.get("title") or "相关内容")[:500],
                    str(item.get("url") or "")[:2000], str(item.get("excerpt") or "")[:4000],
                    str(item.get("author") or "")[:300], str(item.get("published_at") or "") or None,
                    json_dumps(item.get("metrics") or {}), json_dumps(item.get("raw") or {}), int(is_new), now,
                ),
            )
        return new_count


class TopicRadarWorker:
    def __init__(self, service: TopicRadarService, poll_seconds: float = 60) -> None:
        self.service = service
        self.poll_seconds = poll_seconds
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="topic-radar-worker")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=2)

    def wake(self) -> None:
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.service.run_due()
            except Exception:
                pass
            self._wake.wait(self.poll_seconds)
            self._wake.clear()


topic_radar_service = TopicRadarService()
topic_radar_worker = TopicRadarWorker(topic_radar_service)
