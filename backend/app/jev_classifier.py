from __future__ import annotations

import re
import time
from typing import Any

import httpx


JEV_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
JEV_MODEL = "jev-latest"
JEV_RUBRIC_VERSION = "matchmaking-radar-v2"
JEV_CANDIDATES_PER_REQUEST = 3


class JevError(RuntimeError):
    def __init__(self, message: str, *, code: str = "jev_error", retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def _safe_text(value: Any, limit: int) -> str:
    text = str(value or "")
    text = re.sub(r"https?://\S+", "[链接已移除]", text, flags=re.I)
    text = re.sub(r"\b1[3-9]\d{9}\b", "[手机号已移除]", text)
    text = re.sub(
        r"(?i)(?:微信|wx|vx|v信|wechat)(?:号|id)?\s*[:：]?\s*(?:(?:微信|wx|vx|v信|wechat)(?:号|id)?\s*[:：]?\s*)?[A-Za-z0-9_-]{5,}",
        "[联系方式已移除]",
        text,
    )
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _score_answer(answers: dict[str, Any], key: str) -> tuple[float, float]:
    value = answers.get(key) if isinstance(answers.get(key), dict) else {}
    try:
        score = float(value.get("score", 0.0))
    except (TypeError, ValueError):
        score = 0.0
    try:
        confidence = float(value.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    return max(0.0, min(score, 3.0)), max(0.0, min(confidence, 1.0))


def _choice_answer(answers: dict[str, Any], key: str, fallback: str) -> tuple[str, float]:
    value = answers.get(key) if isinstance(answers.get(key), dict) else {}
    choice = _safe_text(value.get("choice") or fallback, 80) or fallback
    try:
        confidence = float(value.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    return choice, max(0.0, min(confidence, 1.0))


def _noul_answer(answers: dict[str, Any], key: str) -> float:
    value = answers.get(key) if isinstance(answers.get(key), dict) else {}
    try:
        return max(0.0, min(float(value.get("noul", 0.0)), 1.0))
    except (TypeError, ValueError):
        return 0.0


class JevClassifier:
    """Small TypeSafe System One integration for candidate-level judgments.

    Platform identity and freshness remain deterministic application facts.
    Jev only supplies semantic labels and scores over privacy-safe card text.
    """

    def __init__(self, endpoint: str = JEV_ENDPOINT, model: str = JEV_MODEL) -> None:
        self.endpoint = endpoint
        self.model = model

    def _request(
        self,
        api_key: str,
        *,
        state: dict[str, Any],
        questions: dict[str, Any],
        timeout: float = 45.0,
    ) -> dict[str, Any]:
        key = api_key.strip()
        if not key:
            raise JevError("请先在设置中填写 TypeSafe JEV API Key", code="missing_api_key")
        last_error = "JEV 暂时不可用"
        for attempt in range(3):
            try:
                with httpx.Client(timeout=timeout, follow_redirects=False) as client:
                    response = client.post(
                        self.endpoint,
                        headers={"Authorization": f"Bearer {key}"},
                        json={"state": state, "model": self.model, "questions": questions},
                    )
            except httpx.TimeoutException as exc:
                last_error = "JEV 请求超时"
                if attempt < 2:
                    time.sleep(0.6 * (2**attempt))
                    continue
                raise JevError(last_error, code="timeout", retryable=True) from exc
            except httpx.HTTPError as exc:
                raise JevError("无法连接 TypeSafe JEV 服务", code="connection_error", retryable=True) from exc

            if response.status_code == 401:
                raise JevError("JEV API Key 无效或已失效", code="authentication_error")
            if response.status_code == 422:
                raise JevError("JEV 分类请求格式未通过校验", code="invalid_request")
            if response.status_code in {429, 529} or response.status_code >= 500:
                last_error = "JEV 当前繁忙，已按退避策略重试"
                if attempt < 2:
                    retry_after = response.headers.get("retry-after", "")
                    try:
                        delay = min(max(float(retry_after), 0.5), 4.0)
                    except ValueError:
                        delay = 0.6 * (2**attempt)
                    time.sleep(delay)
                    continue
                raise JevError(last_error, code="rate_limited", retryable=True)
            if not response.is_success:
                raise JevError(f"JEV 返回异常状态（{response.status_code}）", code="http_error")
            try:
                data = response.json()
            except ValueError as exc:
                raise JevError("JEV 返回了无法读取的数据", code="invalid_response") from exc
            if not isinstance(data, dict) or not isinstance(data.get("answers"), dict):
                raise JevError("JEV 返回内容缺少结构化判断", code="invalid_response")
            return data
        raise JevError(last_error, code="jev_unavailable", retryable=True)

    def verify_key(self, api_key: str) -> dict[str, Any]:
        started = time.monotonic()
        result = self._request(
            api_key,
            state={"candidate": {"platform": "test", "title": "线下活动报名须知", "excerpt": "面向成年单身用户的公开活动说明"}},
            questions={
                "is_matchmaking_content": {
                    "type": "noul",
                    "instructions": "Is `candidate` about adult dating, matchmaking, or offline social events?",
                    "criteria": {"true": "Directly relevant", "false": "Unrelated"},
                }
            },
            timeout=20.0,
        )
        return {
            "status": "verified",
            "model": str(result.get("model") or self.model),
            "latency_ms": int((time.monotonic() - started) * 1000),
            "usage": result.get("usage") if isinstance(result.get("usage"), dict) else {},
        }

    @staticmethod
    def _persona_state(persona: dict[str, Any]) -> dict[str, Any]:
        return {
            "label": _safe_text(persona.get("label"), 100),
            "public_role": _safe_text(persona.get("public_role"), 300),
            "audience": _safe_text(persona.get("audience"), 400),
            "business": _safe_text(persona.get("business"), 400),
            "guardrails": [_safe_text(value, 220) for value in (persona.get("guardrails") or [])[:12]],
        }

    @staticmethod
    def _candidate_state(candidate: dict[str, Any]) -> dict[str, Any]:
        metrics = candidate.get("metrics") if isinstance(candidate.get("metrics"), dict) else {}
        safe_metrics = {
            str(key)[:40]: value
            for key, value in metrics.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        }
        return {
            "platform": _safe_text(candidate.get("platform"), 30),
            "title": _safe_text(candidate.get("title"), 300),
            "excerpt": _safe_text(candidate.get("excerpt"), 1400),
            "public_metrics": safe_metrics,
            "search_query": _safe_text(candidate.get("search_query"), 180),
        }

    @staticmethod
    def _questions(persona: dict[str, Any], candidate_ref: str, prefix: str = "") -> dict[str, Any]:
        pillars = persona.get("pillars") if isinstance(persona.get("pillars"), list) else []
        pillar_criteria: dict[str, str] = {}
        for item in pillars[:12]:
            if isinstance(item, dict):
                key = _safe_text(item.get("id") or item.get("label"), 50)
                description = _safe_text(item.get("description") or item.get("label"), 240)
            else:
                key = _safe_text(item, 50)
                description = key
            if key:
                pillar_criteria[key] = description or key
        pillar_criteria["off_scope"] = "The candidate does not materially fit any approved content pillar."

        level_criteria = [
            "0: unrelated or conflicts with the described persona/business",
            "1: weak or generic connection",
            "2: clear and useful connection",
            "3: highly specific, strongly aligned, and suitable for this account",
        ]
        questions = {
            f"{prefix}persona_fit": {
                "type": "score",
                "instructions": (
                    f"Rate how well `{candidate_ref}` fits the identity, audience, and public role in `persona`. "
                    "Judge the described situation and meaning in the candidate title/excerpt. The search_query is retrieval context only. "
                    "Do not count keyword overlap by itself as a fit; when the title/excerpt is vague or unrelated in context, score low and lower confidence."
                ),
                "criteria": level_criteria,
            },
            f"{prefix}business_fit": {
                "type": "score",
                "instructions": (
                    f"Rate how useful `{candidate_ref}` is to the real business described in `persona.business`, without inventing outcomes. "
                    "Require a material connection to the actual service or audience need; keyword overlap or the search query alone is not evidence. "
                    "If the excerpt does not support a business connection, score low rather than infer one."
                ),
                "criteria": level_criteria,
            },
            f"{prefix}content_value": {
                "type": "score",
                "instructions": (
                    f"Rate whether `{candidate_ref}` contains a concrete tension, decision, cost, misconception, or actionable observation that can support an original post. "
                    "Use only what is visible in the candidate title/excerpt; do not fill missing facts from the search query or assumptions."
                ),
                "criteria": [
                    "0: noise, empty engagement bait, or no usable idea",
                    "1: generic discussion with little specificity",
                    "2: contains a concrete and reusable content angle",
                    "3: unusually specific, timely, and rich enough for a strong original angle",
                ],
            },
            f"{prefix}content_pillar": {
                "type": "choice",
                "instructions": (
                    f"Choose the single best approved content pillar for `{candidate_ref}` based on the described content, not search-term overlap. "
                    "Choose off_scope when none materially fit or the available title/excerpt is insufficient to establish a fit."
                ),
                "criteria": pillar_criteria,
            },
            f"{prefix}audience_need": {
                "type": "choice",
                "instructions": f"Which audience need is most central in `{candidate_ref}`?",
                "criteria": {
                    "expand_social_circle": "Expanding a limited social circle or meeting suitable people",
                    "first_meeting": "First dates, first meetings, conversation, or preparation",
                    "partner_choice": "Partner selection, relationship judgment, values, or boundaries",
                    "offline_event": "Joining, designing, or evaluating an offline social or matchmaking event",
                    "emotional_support": "Loneliness, anxiety, confidence, or emotional support around dating",
                    "other_or_unclear": "None of the listed needs is clearly central",
                },
            },
            f"{prefix}gender_antagonism": {
                "type": "noul",
                "instructions": f"Does `{candidate_ref}` rely on gender antagonism, humiliation, demeaning stereotypes, or hostile blame?",
                "criteria": {"true": "Meaningful antagonism or shaming is present", "false": "No such framing"},
            },
            f"{prefix}overclaim_or_promise": {
                "type": "noul",
                "instructions": f"Does `{candidate_ref}` make an unsupported result promise, guaranteed matchmaking claim, or unverifiable success claim?",
                "criteria": {"true": "A risky promise or unsupported claim is present", "false": "No such claim is visible"},
            },
            f"{prefix}privacy_risk": {
                "type": "noul",
                "instructions": f"Does `{candidate_ref}` expose or encourage exposing private contact details, identifiable client stories, or sensitive personal data?",
                "criteria": {"true": "Private or identifying information risk is present", "false": "No such risk is visible"},
            },
            f"{prefix}off_scope": {
                "type": "noul",
                "instructions": f"Is `{candidate_ref}` substantially outside the persona, audience, business, and approved content pillars in `persona`?",
                "criteria": {"true": "Substantially off-scope", "false": "Materially relevant"},
            },
        }
        return questions

    def _interpret(
        self,
        answers: dict[str, Any],
        response: dict[str, Any],
        *,
        candidate: dict[str, Any] | None = None,
        prefix: str = "",
        usage: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        persona_score, persona_confidence = _score_answer(answers, f"{prefix}persona_fit")
        business_score, business_confidence = _score_answer(answers, f"{prefix}business_fit")
        content_score, content_confidence = _score_answer(answers, f"{prefix}content_value")
        pillar, pillar_confidence = _choice_answer(answers, f"{prefix}content_pillar", "off_scope")
        audience_need, audience_confidence = _choice_answer(answers, f"{prefix}audience_need", "other_or_unclear")
        risks = {
            "gender_antagonism": _noul_answer(answers, f"{prefix}gender_antagonism"),
            "overclaim_or_promise": _noul_answer(answers, f"{prefix}overclaim_or_promise"),
            "privacy_risk": _noul_answer(answers, f"{prefix}privacy_risk"),
            "off_scope": _noul_answer(answers, f"{prefix}off_scope"),
        }
        confidence = min(
            persona_confidence,
            business_confidence,
            content_confidence,
            pillar_confidence,
            audience_confidence,
        )
        composite = (
            (persona_score / 3.0) * 0.35
            + (business_score / 3.0) * 0.30
            + (content_score / 3.0) * 0.35
        )
        severe_risk = max(risks.values()) >= 0.72
        if severe_risk or pillar == "off_scope" and risks["off_scope"] >= 0.65:
            route = "C"
        elif confidence < 0.45:
            route = "B"
        elif persona_score >= 1.8 and business_score >= 1.8 and content_score >= 1.6 and composite >= 0.62:
            route = "A"
        elif persona_score < 1.0 or business_score < 1.0 or risks["off_scope"] >= 0.75:
            route = "C"
        else:
            route = "B"

        risk_flags = [key for key, probability in risks.items() if probability >= 0.55]
        evidence_parts = []
        if candidate:
            title = _safe_text(candidate.get("title"), 100)
            excerpt = _safe_text(candidate.get("excerpt"), 180)
            if title:
                evidence_parts.append(f"标题「{title}」")
            if excerpt:
                evidence_parts.append(f"摘要「{excerpt}」")
        decision_evidence = "；".join(evidence_parts) or "候选标题/摘要缺失，现有证据不足"
        risk_labels = {
            "gender_antagonism": "性别对立或羞辱",
            "overclaim_or_promise": "结果承诺或不可核验说法",
            "privacy_risk": "隐私或身份信息",
            "off_scope": "偏离人设/业务范围",
        }
        risk_reason = "、".join(
            f"{risk_labels[key]} {risks[key]:.0%}"
            for key in risk_flags
        ) or "未发现超过复核阈值的高风险信号"
        reason = (
            f"判定依据：{decision_evidence}。评分：人设 {persona_score:.1f}/3，业务 {business_score:.1f}/3，"
            f"内容价值 {content_score:.1f}/3；内容支柱 {pillar}，受众需求 {audience_need}，"
            f"综合分 {composite:.0%}，置信度 {confidence:.0%}。风险：{risk_reason}。"
        )
        return {
            "route": route,
            "pillar": pillar,
            "audience_need": audience_need,
            "scores": {
                "persona_fit": round(persona_score, 4),
                "business_fit": round(business_score, 4),
                "content_value": round(content_score, 4),
            },
            "risks": risks,
            "risk_flags": risk_flags,
            "confidence": round(confidence, 6),
            "composite_score": round(composite, 6),
            "decision_evidence": decision_evidence,
            "reason": reason,
            "model": str(response.get("model") or self.model),
            "rubric_version": JEV_RUBRIC_VERSION,
            "usage": usage if isinstance(usage, dict) else {},
            "answers": answers,
        }

    def classify(self, api_key: str, candidate: dict[str, Any], persona: dict[str, Any]) -> dict[str, Any]:
        safe_candidate = self._candidate_state(candidate)
        response = self._request(
            api_key,
            state={"candidate": safe_candidate, "persona": self._persona_state(persona)},
            questions=self._questions(persona, "candidate"),
        )
        answers = response.get("answers") or {}
        return self._interpret(answers, response, candidate=safe_candidate, usage=response.get("usage"))

    def classify_many(
        self,
        api_key: str,
        candidates: list[dict[str, Any]],
        persona: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Classify several candidates in one JEV call with isolated keyed questions."""
        if not candidates:
            return []
        if len(candidates) > JEV_CANDIDATES_PER_REQUEST:
            raise ValueError(f"每次 JEV 最多分类 {JEV_CANDIDATES_PER_REQUEST} 条候选")
        shared_state: dict[str, Any] = {"persona": self._persona_state(persona), "candidates": {}}
        questions: dict[str, Any] = {}
        prefixes: list[str] = []
        safe_candidates: list[dict[str, Any]] = []
        for index, candidate in enumerate(candidates):
            candidate_ref = f"candidate_{index}"
            prefix = f"c{index}_"
            prefixes.append(prefix)
            safe_candidate = self._candidate_state(candidate)
            safe_candidates.append(safe_candidate)
            shared_state[candidate_ref] = safe_candidate
            questions.update(self._questions(persona, candidate_ref, prefix))
        response = self._request(api_key, state=shared_state, questions=questions)
        all_answers = response.get("answers") or {}
        usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}

        def apportioned_token(name: str, index: int) -> int | None:
            raw = usage.get(name)
            if not isinstance(raw, int) or raw < 0:
                return None
            quotient, remainder = divmod(raw, len(candidates))
            return quotient + int(index < remainder)

        results: list[dict[str, Any]] = []
        for index, prefix in enumerate(prefixes):
            item_answers = {
                key: value for key, value in all_answers.items()
                if key.startswith(prefix)
            }
            allocated_usage = {
                "input_tokens": apportioned_token("input_tokens", index),
                "output_tokens": apportioned_token("output_tokens", index),
                "allocation": "even_split_estimate",
            }
            result = self._interpret(
                item_answers, response, candidate=safe_candidates[index], prefix=prefix, usage=allocated_usage,
            )
            result["answers"] = item_answers
            results.append(result)
        return results


jev_classifier = JevClassifier()
