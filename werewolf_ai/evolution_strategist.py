from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any

from .llm import ChatClient, build_chat_client_from_env, extract_json_object
from .models import EvaluationReport, Role, StrategyVersion, to_jsonable


DEFAULT_STRATEGIST_MAX_TOKENS = 2400
DEFAULT_STRATEGIST_TIMEOUT_SECONDS = 240.0
DEFAULT_STRATEGIST_MAX_RETRIES = 2


def plan_evolution_with_llm(
    *,
    baseline_aggregate: dict[str, Any],
    candidate_aggregate: dict[str, Any] | None,
    current_profiles: dict[Role, StrategyVersion],
    candidate_profiles: dict[Role, StrategyVersion] | None = None,
    baseline_reports: list[EvaluationReport] | None = None,
    candidate_reports: list[EvaluationReport] | None = None,
    baseline_reviews: list[dict[str, Any]] | None = None,
    candidate_reviews: list[dict[str, Any]] | None = None,
    promotion: dict[str, Any] | None = None,
    llm_provider: str,
    llm_model: str | None,
    enabled: bool = True,
    purpose: str = "next_candidate",
) -> dict[str, Any]:
    if not enabled:
        plan = _skipped_plan("disabled")
        plan["purpose"] = purpose
        return plan
    client = build_chat_client_from_env(llm_provider, llm_model)
    if client is None:
        plan = _skipped_plan("no_chat_client")
        plan["purpose"] = purpose
        return plan
    _configure_strategist_client(client)
    try:
        plan = LLMEvolutionStrategist(client).plan(
            baseline_aggregate=baseline_aggregate,
            candidate_aggregate=candidate_aggregate,
            current_profiles=current_profiles,
            candidate_profiles=candidate_profiles,
            baseline_reports=baseline_reports or [],
            candidate_reports=candidate_reports or [],
            baseline_reviews=baseline_reviews or [],
            candidate_reviews=candidate_reviews or [],
            promotion=promotion,
            purpose=purpose,
        )
        plan.setdefault("purpose", purpose)
        return plan
    except Exception as exc:
        return {
            "plan_status": "failed",
            "purpose": purpose,
            "error": type(exc).__name__,
            "message": str(exc),
            "metadata": {
                "planner_backend": "llm",
                "llm_provider": client.config.provider,
                "llm_model": client.config.model,
                "llm_timeout_seconds": client.config.timeout_seconds,
                "llm_max_retries": client.config.max_retries,
            },
        }


def strategist_skill_cards(plan: dict[str, Any] | None) -> dict[Role, list[dict[str, Any]]]:
    grouped: dict[Role, list[dict[str, Any]]] = {role: [] for role in Role}
    if not plan or plan.get("plan_status") != "ok":
        return grouped
    for role_value, role_plan in (plan.get("role_plans") or {}).items():
        role = _valid_role(role_value)
        if not role or not isinstance(role_plan, dict):
            continue
        for operation in _as_list(role_plan.get("skill_operations"))[:6]:
            if not isinstance(operation, dict):
                continue
            if str(operation.get("operation") or "add").lower() not in {"add", "refine", "reinforce"}:
                continue
            skill = operation.get("skill") if isinstance(operation.get("skill"), dict) else operation
            card = _normalize_skill_card(role, skill, source_plan_id=str(plan.get("plan_id") or "strategy_plan"))
            if card:
                grouped[role].append(card)
    return grouped


def strategist_memory_updates(plan: dict[str, Any] | None) -> dict[Role, list[str]]:
    grouped: dict[Role, list[str]] = {role: [] for role in Role}
    if not plan or plan.get("plan_status") != "ok":
        return grouped
    for role_value, role_plan in (plan.get("role_plans") or {}).items():
        role = _valid_role(role_value)
        if not role or not isinstance(role_plan, dict):
            continue
        focus = _short(role_plan.get("focus"), 160)
        if focus:
            grouped[role].append(f"进化规划重点：{focus}")
        for item in _as_list(role_plan.get("memory_updates"))[:4]:
            text = _short(item, 180)
            if text:
                grouped[role].append(f"进化规划记忆：{text}")
    return grouped


class LLMEvolutionStrategist:
    def __init__(self, chat_client: ChatClient, *, max_tokens: int | None = None):
        self.chat_client = chat_client
        self.max_tokens = max_tokens or _env_int("LLM_STRATEGIST_MAX_TOKENS", DEFAULT_STRATEGIST_MAX_TOKENS)

    def plan(
        self,
        *,
        baseline_aggregate: dict[str, Any],
        candidate_aggregate: dict[str, Any] | None,
        current_profiles: dict[Role, StrategyVersion],
        candidate_profiles: dict[Role, StrategyVersion] | None,
        baseline_reports: list[EvaluationReport],
        candidate_reports: list[EvaluationReport],
        baseline_reviews: list[dict[str, Any]],
        candidate_reviews: list[dict[str, Any]],
        promotion: dict[str, Any] | None,
        purpose: str,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        messages = self._messages(
            baseline_aggregate=baseline_aggregate,
            candidate_aggregate=candidate_aggregate,
            current_profiles=current_profiles,
            candidate_profiles=candidate_profiles,
            baseline_reports=baseline_reports,
            candidate_reports=candidate_reports,
            baseline_reviews=baseline_reviews,
            candidate_reviews=candidate_reviews,
            promotion=promotion,
            purpose=purpose,
        )
        content = self.chat_client.chat(messages, temperature=0.12, max_tokens=self.max_tokens)
        raw = extract_json_object(content)
        plan = normalize_strategy_plan(raw)
        plan["metadata"] = {
            "planner_backend": "llm",
            "llm_provider": self.chat_client.config.provider,
            "llm_model": self.chat_client.config.model,
            "llm_max_output_tokens": self.max_tokens,
            "llm_timeout_seconds": self.chat_client.config.timeout_seconds,
            "llm_max_retries": self.chat_client.config.max_retries,
            "llm_latency_ms": round((time.perf_counter() - started) * 1000, 1),
        }
        return plan

    def _messages(
        self,
        *,
        baseline_aggregate: dict[str, Any],
        candidate_aggregate: dict[str, Any] | None,
        current_profiles: dict[Role, StrategyVersion],
        candidate_profiles: dict[Role, StrategyVersion] | None,
        baseline_reports: list[EvaluationReport],
        candidate_reports: list[EvaluationReport],
        baseline_reviews: list[dict[str, Any]],
        candidate_reviews: list[dict[str, Any]],
        promotion: dict[str, Any] | None,
        purpose: str,
    ) -> list[dict[str, str]]:
        system = (
            "你是 AI 狼人杀多智能体系统的进化规划器。"
            "你不直接控制单局行动，而是根据多局指标提出下一代 Agent 的策略假设、skill 更新和 memory 更新。"
            "你的建议必须可审计、可 AB 验证，不能绕过现有晋级规则。"
            "只输出 JSON 对象，不要 Markdown，不要额外解释。"
        )
        payload = {
            "task": "基于评测数据生成下一轮自进化计划。LLM 负责提出方向，规则评测负责裁判。",
            "purpose": purpose,
            "baseline": {
                "aggregate": baseline_aggregate,
                "mistake_examples": _mistake_examples(baseline_reports),
                "review_lessons": _review_lessons(baseline_reviews),
            },
            "candidate": {
                "aggregate": candidate_aggregate,
                "mistake_examples": _mistake_examples(candidate_reports),
                "review_lessons": _review_lessons(candidate_reviews),
                "promotion": promotion,
            }
            if candidate_aggregate is not None
            else None,
            "current_skills": _profile_skill_summary(current_profiles),
            "candidate_skills": _profile_skill_summary(candidate_profiles or {}),
            "output_schema": {
                "plan_id": "短字符串",
                "diagnosis": [
                    {
                        "claim": "基于数据的诊断",
                        "evidence": "必须引用 aggregate/mistake/promotion 中的具体数值或错误类型",
                    }
                ],
                "role_plans": {
                    "werewolf": {
                        "focus": "下一轮该角色的主攻方向",
                        "memory_updates": ["可写入长期记忆的一句话原则"],
                        "skill_operations": [
                            {
                                "operation": "add/refine/reinforce/deprioritize",
                                "reason": "为什么改",
                                "skill": {
                                    "title": "技能名",
                                    "trigger": "什么局面触发",
                                    "procedure": ["可选行动步骤，不是强制命令"],
                                    "avoid": ["需要避免的错误"],
                                    "tags": ["role", "topic"],
                                    "confidence": "0-1",
                                },
                            }
                        ],
                    }
                },
                "next_experiment": {
                    "focus": "下一轮 AB 要验证的假设",
                    "recommended_games": "建议局数",
                    "acceptance_criteria": {"metric_or_mistake": "increase/decrease/not_worse"},
                },
                "risks": ["可能的副作用"],
            },
            "hard_constraints": [
                "不要输出固定座位或固定目标打法，例如必须刀 P4、必须投 P7。",
                "skill 是可选策略工具，不是强制动作。",
                "必须保留信息隔离：不要建议好人使用夜间私有狼队信息。",
                "狼人可以策略性不诚实，但欺骗必须能用公开历史包装，不能编造不存在事件。",
                "如果候选未晋级，重点诊断为什么方向失败，并给下一轮修正假设。",
                "role_plans 只能使用 werewolf/seer/witch/hunter/villager 这些键。",
            ],
        }
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]


def normalize_strategy_plan(raw: dict[str, Any]) -> dict[str, Any]:
    plan_id = _short(raw.get("plan_id"), 80) or _plan_id(raw)
    diagnosis = []
    for item in _as_list(raw.get("diagnosis"))[:8]:
        if isinstance(item, dict):
            claim = _short(item.get("claim"), 220)
            evidence = _short(item.get("evidence"), 220)
            if claim:
                diagnosis.append({"claim": claim, "evidence": evidence})
        else:
            text = _short(item, 220)
            if text:
                diagnosis.append({"claim": text, "evidence": ""})

    role_plans: dict[str, Any] = {}
    raw_role_plans = raw.get("role_plans") if isinstance(raw.get("role_plans"), dict) else {}
    for role in Role:
        item = raw_role_plans.get(role.value) if isinstance(raw_role_plans.get(role.value), dict) else {}
        focus = _short(item.get("focus"), 180)
        memory_updates = [_short(value, 180) for value in _as_list(item.get("memory_updates"))[:5]]
        memory_updates = [value for value in memory_updates if value]
        skill_operations = []
        for operation in _as_list(item.get("skill_operations"))[:6]:
            if not isinstance(operation, dict):
                continue
            op_name = str(operation.get("operation") or "add").lower()
            if op_name not in {"add", "refine", "reinforce", "deprioritize", "retire"}:
                op_name = "add"
            skill = operation.get("skill") if isinstance(operation.get("skill"), dict) else operation
            normalized_skill = _normalize_skill_payload(skill)
            if normalized_skill or op_name in {"deprioritize", "retire"}:
                skill_operations.append(
                    {
                        "operation": op_name,
                        "reason": _short(operation.get("reason"), 220),
                        "skill": normalized_skill,
                    }
                )
        role_plans[role.value] = {
            "focus": focus,
            "memory_updates": memory_updates,
            "skill_operations": skill_operations,
        }

    return {
        "plan_status": "ok",
        "plan_id": plan_id,
        "diagnosis": diagnosis,
        "role_plans": role_plans,
        "next_experiment": _normalize_next_experiment(raw.get("next_experiment")),
        "risks": [_short(item, 180) for item in _as_list(raw.get("risks"))[:8] if _short(item, 180)],
    }


def _normalize_skill_payload(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    title = _short(raw.get("title"), 80)
    trigger = _short(raw.get("trigger"), 180)
    procedure = [_short(item, 180) for item in _as_list(raw.get("procedure"))[:5]]
    avoid = [_short(item, 140) for item in _as_list(raw.get("avoid"))[:4]]
    tags = [_short(item, 40) for item in _as_list(raw.get("tags"))[:6]]
    procedure = [item for item in procedure if item]
    avoid = [item for item in avoid if item]
    tags = [item for item in tags if item]
    if not title or not trigger or not procedure:
        return {}
    return {
        "title": title,
        "trigger": trigger,
        "procedure": procedure,
        "avoid": avoid,
        "tags": tags,
        "confidence": _bounded_float(raw.get("confidence"), default=0.72),
    }


def _normalize_skill_card(role: Role, raw: dict[str, Any], *, source_plan_id: str) -> dict[str, Any] | None:
    skill = _normalize_skill_payload(raw)
    if not skill:
        return None
    source_key = f"{role.value}:{skill['title']}:{skill['trigger']}:{source_plan_id}"
    digest = hashlib.sha1(source_key.encode("utf-8")).hexdigest()[:10]
    source_type_digest = hashlib.sha1(f"{skill['title']}:{skill['trigger']}".encode("utf-8")).hexdigest()[:10]
    return {
        "skill_id": f"{role.value}_strategist_skill_{digest}",
        "role": role.value,
        "title": skill["title"],
        "trigger": skill["trigger"],
        "procedure": skill["procedure"],
        "avoid": skill["avoid"],
        "tags": [role.value, "strategist", *skill.get("tags", [])],
        "source": "llm_strategist",
        "source_type": f"strategist_{source_type_digest}",
        "confidence": skill["confidence"],
        "support_count": 1,
        "source_plan_id": source_plan_id,
    }


def _normalize_next_experiment(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    criteria = raw.get("acceptance_criteria") if isinstance(raw.get("acceptance_criteria"), dict) else {}
    return {
        "focus": _short(raw.get("focus"), 180),
        "recommended_games": _positive_int(raw.get("recommended_games"), default=10),
        "acceptance_criteria": {str(key): _short(value, 80) for key, value in criteria.items()},
    }


def _configure_strategist_client(client: ChatClient) -> None:
    config = client.config
    current_timeout = _positive_float(getattr(config, "timeout_seconds", 0), default=0.0)
    current_retries = _positive_int(getattr(config, "max_retries", 0), default=0)
    config.timeout_seconds = _env_float(
        "LLM_STRATEGIST_TIMEOUT_SECONDS",
        max(current_timeout, DEFAULT_STRATEGIST_TIMEOUT_SECONDS),
    )
    config.max_retries = _env_int(
        "LLM_STRATEGIST_MAX_RETRIES",
        max(current_retries, DEFAULT_STRATEGIST_MAX_RETRIES),
    )


def _profile_skill_summary(profiles: dict[Role, StrategyVersion]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for role, profile in profiles.items():
        skills = profile.parameters.get("role_skills", []) if isinstance(profile.parameters, dict) else []
        summary[role.value] = [
            {
                "skill_id": skill.get("skill_id"),
                "title": skill.get("title"),
                "trigger": skill.get("trigger"),
                "source_type": skill.get("source_type"),
                "confidence": skill.get("confidence"),
                "support_count": skill.get("support_count"),
            }
            for skill in skills[:8]
            if isinstance(skill, dict)
        ]
    return summary


def _mistake_examples(reports: list[EvaluationReport]) -> list[dict[str, Any]]:
    examples = []
    for report in reports[:6]:
        for mistake in report.mistakes[:6]:
            examples.append(
                {
                    "game_id": report.game_id,
                    "type": mistake.get("type"),
                    "role": mistake.get("role"),
                    "day": mistake.get("day"),
                    "actor_id": mistake.get("actor_id"),
                    "target_id": mistake.get("target_id"),
                    "message": mistake.get("message"),
                }
            )
    return examples[:18]


def _review_lessons(reviews: list[dict[str, Any]]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {role.value: [] for role in Role}
    for review in reviews[:6]:
        if review.get("review_status") != "ok":
            continue
        for item in _as_list(review.get("memory_candidates")):
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "")
            memory = _short(item.get("memory"), 160)
            if role in grouped and memory:
                grouped[role].append(memory)
    return {role: _dedupe(items)[:5] for role, items in grouped.items()}


def _skipped_plan(reason: str) -> dict[str, Any]:
    return {
        "plan_status": "skipped",
        "reason": reason,
        "role_plans": {role.value: {"focus": "", "memory_updates": [], "skill_operations": []} for role in Role},
        "diagnosis": [],
        "next_experiment": {},
        "risks": [],
    }


def _plan_id(raw: dict[str, Any]) -> str:
    digest = hashlib.sha1(json.dumps(to_jsonable(raw), ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:10]
    return f"plan_{digest}"


def _valid_role(value: Any) -> Role | None:
    try:
        return Role(str(value))
    except ValueError:
        return None


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value is None:
        return []
    return [value]


def _short(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def _bounded_float(value: Any, *, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(0.0, min(1.0, parsed))


def _positive_float(value: Any, *, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(0.0, parsed)


def _positive_int(value: Any, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(0, parsed)


def _env_int(key: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(key, default)))
    except (TypeError, ValueError):
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return max(0.0, float(os.environ.get(key, default)))
    except (TypeError, ValueError):
        return default


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result
