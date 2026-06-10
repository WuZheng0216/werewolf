from __future__ import annotations

import json
import os
import random
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .models import ActionType, AgentDecision, Phase, PrivateObservation, Role, StrategyVersion, game_rule_summary, to_jsonable
from .skill_usage import select_relevant_skills


DEFAULT_ARK_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
DEFAULT_ARK_MODEL = "ep-20260514115354-k4jz4"
DEFAULT_DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_DASHSCOPE_MODEL = "deepseek-v4-flash"
DEFAULT_DASHSCOPE_GENERATION_BASE_URL = "https://dashscope.aliyuncs.com/api/v1"
DEFAULT_DASHSCOPE_GENERATION_MODEL = "deepseek-v3.2"
DEFAULT_DECISION_MAX_TOKENS = 1500
DEFAULT_SPEECH_MAX_CHARS = 700
DEFAULT_REASON_MAX_CHARS = 360
_RATE_LIMIT_LOCK = threading.Lock()
_RATE_LIMIT_UNTIL_BY_PROVIDER: dict[str, float] = {}
UNSUPPORTED_RULE_TERMS = ("警徽", "警长", "警徽流", "撕警徽", "移交警徽")


class LLMError(RuntimeError):
    pass


class ChatClient(Protocol):
    config: "ChatConfig"

    def chat(self, messages: list[dict[str, str]], *, temperature: float, max_tokens: int) -> str:
        ...


@dataclass
class ChatConfig:
    provider: str
    api_key: str
    model: str
    base_url: str
    timeout_seconds: float = 60.0
    max_retries: int = 2
    extra_body: dict[str, Any] = field(default_factory=dict)


@dataclass
class ArkConfig:
    api_key: str
    model: str = DEFAULT_ARK_MODEL
    base_url: str = DEFAULT_ARK_BASE_URL
    timeout_seconds: float = 60.0
    max_retries: int = 2

    @classmethod
    def from_env(cls) -> "ArkConfig | None":
        load_dotenv()
        api_key = os.environ.get("ARK_API_KEY") or os.environ.get("VOLCENGINE_API_KEY")
        if not api_key:
            return None
        return cls(
            api_key=api_key,
            model=os.environ.get("ARK_MODEL", DEFAULT_ARK_MODEL),
            base_url=os.environ.get("ARK_BASE_URL", DEFAULT_ARK_BASE_URL),
            timeout_seconds=float(os.environ.get("ARK_TIMEOUT_SECONDS", "60")),
            max_retries=int(os.environ.get("ARK_MAX_RETRIES", "2")),
        )

    def to_chat_config(self) -> ChatConfig:
        return ChatConfig(
            provider="ark",
            api_key=self.api_key,
            model=self.model,
            base_url=self.base_url,
            timeout_seconds=self.timeout_seconds,
            max_retries=self.max_retries,
        )


@dataclass
class DashScopeConfig:
    api_key: str
    model: str = DEFAULT_DASHSCOPE_MODEL
    base_url: str = DEFAULT_DASHSCOPE_BASE_URL
    timeout_seconds: float = 45.0
    max_retries: int = 1
    enable_thinking: bool = True

    @classmethod
    def from_env(cls) -> "DashScopeConfig | None":
        load_dotenv()
        api_key = os.environ.get("DASHSCOPE_API_KEY")
        if not api_key:
            return None
        return cls(
            api_key=api_key,
            model=os.environ.get("DASHSCOPE_MODEL", DEFAULT_DASHSCOPE_MODEL),
            base_url=os.environ.get("DASHSCOPE_BASE_URL", DEFAULT_DASHSCOPE_BASE_URL),
            timeout_seconds=float(os.environ.get("DASHSCOPE_TIMEOUT_SECONDS", "45")),
            max_retries=int(os.environ.get("DASHSCOPE_MAX_RETRIES", "1")),
            enable_thinking=_env_bool("DASHSCOPE_ENABLE_THINKING", True),
        )

    def to_chat_config(self) -> ChatConfig:
        extra_body: dict[str, Any] = {}
        if self.enable_thinking:
            extra_body["enable_thinking"] = True
        return ChatConfig(
            provider="dashscope",
            api_key=self.api_key,
            model=self.model,
            base_url=self.base_url,
            timeout_seconds=self.timeout_seconds,
            max_retries=self.max_retries,
            extra_body=extra_body,
        )


@dataclass
class DashScopeGenerationConfig:
    api_key: str
    model: str = DEFAULT_DASHSCOPE_GENERATION_MODEL
    base_url: str = DEFAULT_DASHSCOPE_GENERATION_BASE_URL
    timeout_seconds: float = 90.0
    max_retries: int = 1
    enable_thinking: bool = True

    @classmethod
    def from_env(cls) -> "DashScopeGenerationConfig | None":
        load_dotenv()
        api_key = os.environ.get("DASHSCOPE_API_KEY")
        if not api_key:
            return None
        return cls(
            api_key=api_key,
            model=os.environ.get("DASHSCOPE_GENERATION_MODEL", DEFAULT_DASHSCOPE_GENERATION_MODEL),
            base_url=os.environ.get("DASHSCOPE_GENERATION_BASE_URL", DEFAULT_DASHSCOPE_GENERATION_BASE_URL),
            timeout_seconds=float(os.environ.get("DASHSCOPE_GENERATION_TIMEOUT_SECONDS", "90")),
            max_retries=int(os.environ.get("DASHSCOPE_GENERATION_MAX_RETRIES", "1")),
            enable_thinking=_env_bool("DASHSCOPE_GENERATION_ENABLE_THINKING", True),
        )

    def to_chat_config(self) -> ChatConfig:
        return ChatConfig(
            provider="dashscope-native",
            api_key=self.api_key,
            model=self.model,
            base_url=self.base_url,
            timeout_seconds=self.timeout_seconds,
            max_retries=self.max_retries,
            extra_body={"enable_thinking": self.enable_thinking, "api_mode": "generation"},
        )


def load_dotenv(path: str | Path = ".env") -> None:
    env_path = Path(path)
    if not env_path.is_absolute():
        env_path = Path.cwd() / env_path
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().lstrip("\ufeff")
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


class OpenAICompatibleChatClient:
    """Small OpenAI-compatible Chat Completions client.

    Secrets are only read from environment variables or local .env and are
    never serialized into game logs, reports, or frontend payloads.
    """

    def __init__(self, config: ChatConfig):
        self.config = config

    def chat(self, messages: list[dict[str, str]], *, temperature: float = 0.2, max_tokens: int = 420) -> str:
        url = f"{self.config.base_url.rstrip('/')}/chat/completions"
        body: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        body.update(self.config.extra_body)
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")

        last_error: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            _wait_for_rate_limit_cooldown(self.config.provider)
            request = urllib.request.Request(
                url,
                data=payload,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.config.api_key}",
                },
            )
            retry_delay = 1.5 * (attempt + 1)
            try:
                with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
                    response_body = response.read().decode("utf-8")
                data = json.loads(response_body)
                message = data["choices"][0]["message"]
                content = message.get("content")
                if isinstance(content, str) and content.strip():
                    return content
                reasoning = message.get("reasoning_content")
                if isinstance(reasoning, str) and reasoning.strip():
                    return reasoning
                raise LLMError("Chat response did not contain message content")
            except urllib.error.HTTPError as exc:
                error_text = _safe_http_error(exc, self.config.provider)
                retry_delay = _http_retry_delay(exc, attempt, error_text)
                if exc.code == 429:
                    _record_rate_limit_cooldown(self.config.provider, retry_delay)
                last_error = LLMError(error_text)
            except (urllib.error.URLError, TimeoutError, socket.timeout, KeyError, json.JSONDecodeError) as exc:
                last_error = LLMError(f"{self.config.provider} chat request failed: {type(exc).__name__}")
            if attempt < self.config.max_retries:
                time.sleep(retry_delay)
        raise last_error or LLMError(f"{self.config.provider} chat request failed")


class ArkChatClient(OpenAICompatibleChatClient):
    def __init__(self, config: ArkConfig | ChatConfig):
        if isinstance(config, ArkConfig):
            config = config.to_chat_config()
        super().__init__(config)

    @classmethod
    def from_env(cls) -> "ArkChatClient | None":
        config = ArkConfig.from_env()
        return cls(config) if config else None


class DashScopeChatClient(OpenAICompatibleChatClient):
    def __init__(self, config: DashScopeConfig | ChatConfig):
        if isinstance(config, DashScopeConfig):
            config = config.to_chat_config()
        super().__init__(config)

    @classmethod
    def from_env(cls) -> "DashScopeChatClient | None":
        config = DashScopeConfig.from_env()
        return cls(config) if config else None


class DashScopeGenerationChatClient:
    """DashScope native Generation API client for thinking models.

    DeepSeek v3.2 is exposed through DashScope's Generation.call API in the
    sample provided by the platform, so this client intentionally does not use
    the OpenAI-compatible `/chat/completions` path.
    """

    def __init__(self, config: DashScopeGenerationConfig | ChatConfig):
        if isinstance(config, DashScopeGenerationConfig):
            config = config.to_chat_config()
        self.config = config

    @classmethod
    def from_env(cls) -> "DashScopeGenerationChatClient | None":
        config = DashScopeGenerationConfig.from_env()
        return cls(config) if config else None

    def chat(self, messages: list[dict[str, str]], *, temperature: float = 0.2, max_tokens: int = 520) -> str:
        try:
            import dashscope
            from dashscope import Generation
        except ImportError as exc:  # pragma: no cover - depends on local install.
            raise LLMError("dashscope package is required for dashscope-native. Run: pip install dashscope") from exc

        dashscope.base_http_api_url = self.config.base_url.rstrip("/")
        last_error: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            _wait_for_rate_limit_cooldown(self.config.provider)
            try:
                response = Generation.call(
                    api_key=self.config.api_key,
                    model=self.config.model,
                    messages=messages,
                    result_format="message",
                    enable_thinking=bool(self.config.extra_body.get("enable_thinking", True)),
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                status_code = _field(response, "status_code")
                if status_code != 200:
                    code = _field(response, "code", "")
                    message = _field(response, "message", "")
                    raise LLMError(f"dashscope-native HTTP {status_code}: {code} {message}")

                output = _field(response, "output", {})
                choices = _field(output, "choices", [])
                if choices:
                    message = _field(choices[0], "message", {})
                    content = _field(message, "content", "")
                    if isinstance(content, str) and content.strip():
                        return content
                    reasoning = _field(message, "reasoning_content", "")
                    if isinstance(reasoning, str) and reasoning.strip():
                        return reasoning

                text = _field(output, "text", "")
                if isinstance(text, str) and text.strip():
                    return text
                raise LLMError("DashScope Generation response did not contain message content")
            except Exception as exc:
                last_error = exc if isinstance(exc, LLMError) else LLMError(f"dashscope-native request failed: {type(exc).__name__}")
            if attempt < self.config.max_retries:
                retry_delay = _retry_delay_from_text(last_error, attempt)
                if _looks_like_rate_limit(last_error):
                    _record_rate_limit_cooldown(self.config.provider, retry_delay)
                time.sleep(retry_delay)
        raise last_error or LLMError("dashscope-native request failed")


class LLMDecisionClient:
    def __init__(
        self,
        chat_client: ChatClient,
        *,
        max_history_events: int = 28,
        max_decision_retries: int = 2,
        max_output_tokens: int | None = None,
    ):
        self.chat_client = chat_client
        self.max_history_events = max_history_events
        self.max_decision_retries = max_decision_retries
        self.max_output_tokens = max_output_tokens or _env_int("LLM_DECISION_MAX_TOKENS", DEFAULT_DECISION_MAX_TOKENS)

    def decide(self, observation: PrivateObservation, profile: StrategyVersion) -> AgentDecision:
        all_role_skills = [skill for skill in profile.parameters.get("role_skills", []) if isinstance(skill, dict)]
        recalled_skills = select_relevant_skills(
            observation=observation,
            role_skills=all_role_skills,
            max_items=_env_int("LLM_SKILL_RECALL_TOP_K", 3),
        )
        messages = self._messages(observation, profile, callable_skills=recalled_skills)
        last_error: LLMError | None = None
        decision_started = time.perf_counter()
        call_latencies_ms: list[float] = []
        for attempt in range(self.max_decision_retries + 1):
            call_started = time.perf_counter()
            try:
                content = self.chat_client.chat(
                    messages,
                    temperature=_temperature_for_role(observation.self_role),
                    max_tokens=self.max_output_tokens,
                )
            finally:
                call_latencies_ms.append(round((time.perf_counter() - call_started) * 1000, 1))
            try:
                raw = extract_json_object(content)
                decision = _decision_from_raw(raw, observation)
                break
            except LLMError as exc:
                last_error = exc
                if attempt >= self.max_decision_retries:
                    raise
                messages = self._repair_messages(messages, observation, str(exc))
        else:  # pragma: no cover - loop always breaks or raises.
            raise last_error or LLMError("LLM 决策生成失败。")

        config_extra = getattr(self.chat_client.config, "extra_body", {}) or {}
        decision.metadata.update(
            {
                "decision_backend": "llm",
                "prompt_name": profile.prompt_name,
                "version_id": profile.version_id,
                "llm_provider": self.chat_client.config.provider,
                "llm_model": self.chat_client.config.model,
                "llm_reasoning_enabled": bool(config_extra.get("enable_thinking", False)),
                "llm_max_output_tokens": self.max_output_tokens,
                "llm_repair_attempts": attempt,
                "llm_call_count": len(call_latencies_ms),
                "llm_call_latencies_ms": call_latencies_ms,
                "llm_latency_ms": round(sum(call_latencies_ms), 1),
                "llm_total_elapsed_ms": round((time.perf_counter() - decision_started) * 1000, 1),
                "skill_recall_mode": "relevance_top_k",
                "skill_recall_available_count": len(all_role_skills),
                "skill_recall_selected_count": len(recalled_skills),
                "prompt_skill_ids": [str(skill.get("skill_id") or "") for skill in recalled_skills],
                "prompt_skill_recall": [
                    {
                        "skill_id": skill.get("skill_id"),
                        "title": skill.get("title"),
                        "recall_score": skill.get("recall_score"),
                        "recall_signals": skill.get("recall_signals", [])[:4],
                    }
                    for skill in recalled_skills
                ],
            }
        )
        return decision

    def coordinate_wolf_council(self, context: dict[str, Any]) -> dict[str, Any]:
        """Generate a private werewolf night council plan from wolf proposals."""

        messages = self._wolf_council_messages(context)
        max_tokens = _env_int("LLM_WOLF_COUNCIL_MAX_TOKENS", 1200)
        max_retries = _env_int("LLM_WOLF_COUNCIL_MAX_RETRIES", 1)
        last_error: LLMError | None = None
        started = time.perf_counter()
        call_latencies_ms: list[float] = []
        for attempt in range(max_retries + 1):
            call_started = time.perf_counter()
            try:
                content = self.chat_client.chat(
                    messages,
                    temperature=_env_float("LLM_WOLF_COUNCIL_TEMPERATURE", 0.35),
                    max_tokens=max_tokens,
                )
            finally:
                call_latencies_ms.append(round((time.perf_counter() - call_started) * 1000, 1))
            try:
                raw = extract_json_object(content)
                plan = _wolf_council_plan_from_raw(raw, context)
                plan["coordination_metadata"] = {
                    "decision_backend": "llm_wolf_council",
                    "llm_provider": self.chat_client.config.provider,
                    "llm_model": self.chat_client.config.model,
                    "llm_call_count": len(call_latencies_ms),
                    "llm_call_latencies_ms": call_latencies_ms,
                    "llm_latency_ms": round(sum(call_latencies_ms), 1),
                    "llm_total_elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
                    "llm_repair_attempts": attempt,
                    "llm_max_output_tokens": max_tokens,
                }
                return plan
            except LLMError as exc:
                last_error = exc
                if attempt >= max_retries:
                    raise
                repair_payload = {
                    "错误": str(exc),
                    "要求": "你上一次狼队会议 JSON 未通过校验。请只重新输出合法 JSON，不要解释。",
                    "legal_attack_targets": context.get("legal_attack_targets", []),
                    "输出格式": _wolf_council_output_schema(),
                }
                messages = messages + [{"role": "user", "content": json.dumps(repair_payload, ensure_ascii=False)}]
        raise last_error or LLMError("狼人夜间会议生成失败。")

    def _wolf_council_messages(self, context: dict[str, Any]) -> list[dict[str, str]]:
        system = (
            "你是狼人杀系统中的狼队夜间会议协调器。"
            "你只能使用输入中的公开历史、狼队成员提案、狼队互认信息和当前规则。"
            "你不能读取或编造预言家查验、女巫药水、猎人身份等狼队不可见信息。"
            "你的任务不是给每个狼人强制分配白天动作，而是生成狼队私有 pack_plan：共同刀人目标、共识理由、白天可选协同方向和风险控制。"
            "输出必须是一个 JSON 对象，不要 Markdown，不要额外解释。"
        )
        payload = {
            "任务": "根据存活狼人个人夜间提案，生成本夜狼队会议 pack_plan。",
            "游戏规则_GameRules": context.get("game_rules", {}),
            "day": context.get("day"),
            "alive_players": context.get("alive_players", []),
            "wolves": context.get("wolves", []),
            "legal_attack_targets": context.get("legal_attack_targets", []),
            "public_history": (context.get("public_history") or [])[-self.max_history_events :],
            "individual_proposals": context.get("proposals", []),
            "pressure": context.get("pressure", {}),
            "strategy_options": context.get("strategy_options", []),
            "硬性约束": [
                "attack_target 必须来自 legal_attack_targets。",
                "不得选择狼人自己或狼队友作为夜刀目标。",
                "consensus_reason 只能基于公开历史、提案和狼队可见信息，不得引用神职私有信息。",
                "day_plan 只能给可选方向，不得强制某个座位必须执行某行动。",
                "白天计划不得要求狼人公开提到狼队会议、pack_plan、夜刀或狼队友身份。",
            ],
            "输出格式": _wolf_council_output_schema(),
        }
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]

    def _messages(
        self,
        obs: PrivateObservation,
        profile: StrategyVersion,
        *,
        callable_skills: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, str]]:
        rules = obs.game_rules or game_rule_summary()
        system = (
            "你是狼人杀多智能体系统中的一个角色 Agent。"
            "你只能使用本轮提供的 PrivateObservation 和 GameRules，不得假设、读取或编造未出现的隐藏信息。"
            "当前 GameRules 高于你预训练中熟悉的其他狼人杀规则，也高于策略记忆。"
            "你要围绕自己的阵营目标进行策略决策：狼人可以伪装和误导，好人必须基于公开证据推理。"
            "如果你是狼人，公开发言允许策略性不诚实，但欺骗必须与 public_history 兼容，不能编造不存在的公开事件或规则。"
            "公开发言时不得泄露自己不应公开的私有夜间信息。"
            "BeliefState 是你自己的连续认知白板，只包含你可见的信息；决策应延续其中的怀疑、信任、公开承诺和计划。"
            "只输出一个 JSON 对象，不要 Markdown，不要解释推理过程；reason 字段只写简短、可审计的理由。"
        )
        user_payload = {
            "任务说明": "你需要扮演当前角色，在狼人杀对局中输出本阶段唯一合法决策。",
            "游戏规则_GameRules": rules,
            "当前角色": {
                "role": obs.self_role.value,
                "role_zh": obs.self_role.zh,
                "prompt_summary": profile.prompt_summary,
                "strategy_memory": _compatible_strategy_memory(obs.strategy_memory)[-8:],
                "retrieved_memories": obs.retrieved_memories[-5:],
                "evolution_mode": profile.parameters.get("evolution_mode", "workflow"),
                "skill_recall": {
                    "mode": "relevance_top_k",
                    "available_count": len(profile.parameters.get("role_skills", []) or []),
                    "selected_count": len(callable_skills or []),
                    "instruction": "这些是根据当前局面召回的候选技能。先判断 trigger 是否真的成立，再自主决定是否采用。",
                },
                "callable_skills": (callable_skills or [])[:6],
            },
            "座位人格_PlayerProfile": obs.player_profile,
            "连续认知_BeliefState": obs.belief_state,
            "私有观察_PrivateObservation": {
                "game_id": obs.game_id,
                "day": obs.day,
                "phase": obs.phase.value,
                "phase_zh": obs.phase.zh,
                "self": {
                    "id": obs.self_id,
                    "name": obs.self_name,
                    "role": obs.self_role.value,
                    "alive": obs.self_alive,
                },
                "alive_players": obs.alive_players,
                "public_history": obs.public_history[-self.max_history_events :],
                "private_knowledge": obs.private_knowledge,
                "legal_actions": [action.value for action in obs.legal_actions],
            },
            "狼人自主策略空间_WolfStrategySpace": (
                obs.private_knowledge.get("wolf_strategy_space") if obs.self_role == Role.WEREWOLF else None
            ),
            "输出格式": {
                "action": "必须是 legal_actions 中的一个字符串",
                "target_id": "整数玩家 id 或 null",
                "speech": "字符串；action=speak 时必须填写公开发言，其他动作可为空",
                "reason": "简短公开审计理由，不要写隐藏推理链",
                "confidence": "0 到 1 之间的数字",
                "evidence_event_ids": "数组；引用 public_history 中支持你判断的事件 id。没有可引用公开证据时输出 []",
                "quoted_evidence": "数组；从被引用事件中摘录的短句，不要编造原文。没有可引用公开证据时输出 []",
                "deception_intent": "狼人白天发言/投票时必填；可为 hide、soft_counter、fake_seer、bus_teammate、split_vote、pressure_good、none",
                "public_cover_story": "狼人白天发言/投票时必填。说明你自主选择的策略如何基于 public_history 包装成普通好人视角；不要声称系统分配了某个任务",
            },
            "动作规则": self._action_rules(obs),
            "证据引用规则": [
                "白天发言、白天投票、猎人开枪时，如果 public_history 非空，必须至少引用一个 evidence_event_ids。",
                "evidence_event_ids 只能来自本次输入的 public_history[*].id。",
                "quoted_evidence 必须是对应公开事件中的原文短句或高度贴近的短摘录；不要把自己推断出的结论写成引用。",
                "优先引用原始发言/投票事件。引用其他玩家的总结时，必须在 reason 或 speech 中说明这是该玩家的判断，不要当成原始事实。",
                "如果无法找到公开证据，不要声称某玩家自跳、断言、怀疑或说过某身份；可以改写为“我没有看到直接证据”。",
                "如果 callable_skills 非空，先判断技能触发条件是否符合当前局面；技能是可选策略工具，不是强制动作。",
            ],
            "硬性约束": [
                "action 必须严格来自 legal_actions。",
                "target_id 必须是存活玩家 id 或 null。",
                "kill、inspect、poison、vote、shoot 必须填写 target_id，且不能选择自己。",
                "pass 和 speak 的 target_id 必须为 null。",
                "投票阶段如果 legal_actions 包含 pass，可以用 action=pass,target_id=null 表示弃票；弃票不计入票数，但可能降低阵营贡献。",
                "如果 private_knowledge.action_context.tie_pk.round=revote，vote 的 target_id 只能来自 allowed_vote_targets。",
                "save 只能在 private_knowledge.attacked_tonight 不为 null 时使用，且 target_id 必须等于 attacked_tonight；如果不救人，请输出 action=pass 且 target_id=null。",
                "村民不能声称自己有夜间信息。",
                "狼人公开发言不能暴露狼队友、狼队身份或夜间刀人目标。",
                "狼人可以在身份、立场、怀疑对象、站边和验人叙事上做策略性不诚实；但只能扭曲解释公开事实，不能伪造不存在的发言、投票、查验或夜间信息。",
                "狼人如果输入中存在 WolfStrategySpace，它只是可选策略空间，不是系统任务；你需要根据 public_history、个人座位风格和当前局势自主选择 deception_intent，也可以选择 none/hide/弃票/观望。",
                "狼人白天发言/投票时，deception_intent 和 public_cover_story 必须说明你自主选择的伪装意图与公开包装；不要只写“我是好人”、不要声称被系统分配了某种打法、不要无差别跟随队友。",
                "狼人如果悍跳或对跳预言家，必须给出自洽的查验对象、查验结论、后续查验计划和公开理由；如果选择软对跳，可以质疑单预言家的时机、查验链和收益。",
                "public_claims 只表示玩家自跳身份或声称查验结果。",
                "public_role_assertions 只是其他玩家对身份的猜测或判断，不是本人自跳。",
                "public_role_mentions 只表示发言中出现了角色词，不能单独当成身份依据。",
                "预言家查验结果只表示阵营，不表示具体身份；好人、金水、villagers 都兼容猎人、女巫、预言家和村民。若你是猎人/女巫/预言家，被预言家报好人或金水不是报错身份。",
                "预言家公开报验人时，结论只能写“好人阵营/金水”或“狼人阵营/查杀”；不能写“村民牌”“平民”“普通村民”“村民身份”，因为查验不识别具体身份。",
                "某玩家声称“别人给我报错身份”只是一条自述线索；除非该玩家身份已经被公开技能结算、死亡公开或规则验证，否则不能把这句话当作某预言家必假的铁证。",
                "如果你说某玩家自跳或断言了某身份，reason 必须引用 public_history 中可见的公开证据。",
                "输出 JSON 必须包含 evidence_event_ids 和 quoted_evidence 两个字段。",
                "本局没有警长、警徽、警徽流、撕警徽、移交警徽、警长票权重；不要在 speech 或 reason 中使用这些不存在的机制。",
                "预言家可以表达“后续查验计划”，但不要称为警徽流。",
                "如果策略记忆或 public_history 中出现与 GameRules 冲突的术语，以 GameRules 为准。",
                "只输出 JSON 对象，不要输出额外文字。",
            ],
            "本阶段策略提醒": self._phase_strategy_notes(obs),
            "人格执行要求": [
                "保持座位人格中的 speech_style、risk_preference 和 vote_style，不要和其他玩家形成模板化发言。",
                "狼人尤其要遵守 anti_template_rule：即使知道队友，也要保持个人视角和独立投票理由，避免多人同理由同票暴露共边。",
                "如果你的投票目标与他人相同，必须在 reason 或 speech 中给出来自自己观察的独立公开证据。",
            ],
        }
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ]

    def _repair_messages(
        self,
        previous_messages: list[dict[str, str]],
        obs: PrivateObservation,
        error: str,
    ) -> list[dict[str, str]]:
        rules = obs.game_rules or game_rule_summary()
        repair_payload = {
            "错误": error,
            "要求": "你上一次输出的 JSON 决策未通过规则校验。请只重新输出一个合法 JSON 对象，不要解释。",
            "游戏规则_GameRules": rules,
            "legal_actions": [action.value for action in obs.legal_actions],
            "private_knowledge": obs.private_knowledge,
            "动作规则": self._action_rules(obs),
            "证据引用规则": [
                "必须输出 evidence_event_ids 和 quoted_evidence。",
                "evidence_event_ids 只能引用 public_history 中出现过的 id。",
                "如果白天公开决策有 public_history，请至少引用一个事件 id。",
            ],
            "特别提醒": (
                "如果 action=save，target_id 必须严格等于 private_knowledge.attacked_tonight；"
                "如果不满足或不想救人，请输出 {\"action\":\"pass\",\"target_id\":null,\"speech\":\"\",\"reason\":\"不使用技能。\",\"confidence\":0.5,"
                "\"evidence_event_ids\":[],\"quoted_evidence\":[]}。"
                "本局没有警长、警徽或警徽流；相关说法都不是合法规则依据。"
            ),
        }
        return previous_messages + [{"role": "user", "content": json.dumps(repair_payload, ensure_ascii=False)}]

    def _action_rules(self, obs: PrivateObservation) -> dict[str, Any]:
        attacked = obs.private_knowledge.get("attacked_tonight")
        tie_pk = obs.private_knowledge.get("action_context", {}).get("tie_pk", {})
        allowed_vote_targets = tie_pk.get("allowed_vote_targets") or []
        vote_rule = "白天投票；target_id 必须是非自己的存活玩家。"
        if allowed_vote_targets:
            vote_rule = (
                "PK 二次投票；target_id 必须从 private_knowledge.action_context.tie_pk.allowed_vote_targets "
                f"中选择，当前只能投 {allowed_vote_targets}。"
            )
        return {
            "kill": "狼人夜间刀人；target_id 必须是非自己、非狼队友的存活玩家。",
            "inspect": "预言家查验；target_id 必须是非自己的存活玩家。",
            "save": {
                "说明": "女巫使用解药。",
                "严格规则": "target_id 必须等于 private_knowledge.attacked_tonight。",
                "当前_attacked_tonight": attacked,
                "合法_save_target_id": attacked,
                "不救人": "输出 action=pass 且 target_id=null。",
            },
            "poison": "女巫使用毒药；target_id 必须是非自己的存活玩家。",
            "speak": "白天公开发言；speech 必填，target_id 必须为 null。",
            "vote": vote_rule,
            "shoot": "猎人开枪；target_id 必须是非自己的存活玩家。",
            "pass": "跳过行动；target_id 必须为 null。在投票阶段表示弃票，不计入票数；只有证据明显不足或投票会严重误伤时才建议弃票。",
        }

    def _phase_strategy_notes(self, obs: PrivateObservation) -> list[str]:
        notes: list[str] = []
        tie_pk = obs.private_knowledge.get("action_context", {}).get("tie_pk", {})
        if tie_pk.get("round") == "pk_speech":
            notes.extend(
                [
                    f"当前是平票 PK 发言，PK 对象为 {tie_pk.get('tied_targets', [])}。",
                    "你的目标是回应首轮票型和公开质疑，争取台下玩家在二次投票中改票；不要把自称身份当作系统认证身份。",
                ]
            )
        elif tie_pk.get("round") == "revote":
            notes.extend(
                [
                    f"当前是 PK 二次投票，只能在 {tie_pk.get('allowed_vote_targets', [])} 中选择投票目标，或在证据不足时弃票。",
                    "二次投票仍平票将无人放逐；reason 需要比较 PK 双方的公开证据，而不是沿用首轮结论。",
                ]
            )
        pack_plan = obs.private_knowledge.get("pack_plan")
        if obs.self_role == Role.WEREWOLF and pack_plan:
            notes.extend(
                [
                    "WolfStrategySpace 是狼人私有策略空间，不是系统分配任务；你需要自主选择 deception_intent。",
                    "可以选择隐藏、软对跳、悍跳、倒钩、分票、抗推、弃票或暂不主动欺骗；如果公开证据不支持高风险策略，应选择更稳妥动作。",
                    "无论选择什么策略，公开发言必须包装成普通好人视角，不能说出 WolfStrategySpace、pack_plan、狼队友或夜间信息。",
                ]
            )
        if obs.self_role == Role.WEREWOLF and obs.phase == Phase.DAY_VOTE:
            notes.extend(
                [
                    "狼人投票不要默认跟随狼队友；早期三狼集中投同一个好人会形成明显票型。",
                    "如果要投狼队友正在攻击的目标，必须有独立公开证据；否则优先考虑分票、弃票、轻踩队友或投向另一个公开可疑目标。",
                    "投票理由只能基于公开发言和公开票型，不要暴露你知道谁是狼队友。",
                ]
            )
        if obs.self_role == Role.WEREWOLF and obs.phase == Phase.DAY_SPEECH:
            notes.extend(
                [
                    "发言不要复读前一名狼队友的措辞；保持普通好人的独立观察口吻。",
                    "可以轻微质疑狼队友来制造距离，但不要泄露狼队协同。",
                    "不要只重复“我是好人牌”。你需要选择一个清晰的公开欺骗意图：隐藏、软质疑单预言家、悍跳/对跳、倒钩队友、制造分票、拉拢好人或抗推好人。",
                    "欺骗不是编故事。所有攻击、站边和假查验都必须能引用 public_history 中已经发生的发言或票型来包装。",
                    "当场上只有单预言家且没有强查杀时，狼人可以软对跳、质疑查验链、质疑后续查验计划或制造可信的第二视角，不要全员默认认下。",
                    "如果悍跳预言家，必须说清昨夜查验了谁、给出查杀/金水结果、下一晚想查谁，以及为什么这些说法能被公开发言支撑。",
                ]
            )
        if obs.self_role == Role.SEER and obs.phase == Phase.DAY_SPEECH:
            notes.extend(
                [
                    "预言家不是每局都必须机械首日跳出；查到狼人、被强烈质疑、场上出现对跳或需要带队时，公开身份收益更高。",
                    "如果只有金水或信息不足，可以选择隐忍、软铺垫站边或说明后续查验计划；但不要使用警徽流等本局不存在的机制。",
                    "发言前请在 reason 中简短比较“现在公开身份”和“隐忍一轮/软铺垫”的收益，避免把首日起跳当成固定动作。",
                    "一旦选择公开预言家身份，必须清楚区分夜间查验结果和公开推理，不要把推理包装成查验。",
                    "如果公开报金水，请说“P几是好人阵营/金水”，不要说“村民牌、平民、普通村民、村民身份”。",
                ]
            )
        if obs.self_role == Role.WITCH and obs.phase == Phase.NIGHT_WITCH:
            notes.extend(
                [
                    "女巫首夜救人是可选策略，不是固定动作；要结合被刀目标、药水价值和当前轮次判断。",
                    "行动前请在 reason 中简短比较“立即救人”和“保留解药”的收益；非自救时要说明被刀目标为什么值得救。",
                    "毒药需要更高置信度，优先绑定公开查杀、多轮强狼面或关键反逻辑；证据不足时可以保留。",
                ]
            )
        return notes


def build_chat_client_from_env(provider: str = "ark", model: str | None = None) -> ChatClient | None:
    provider = normalize_provider(provider)
    if provider == "dashscope-native":
        config = DashScopeGenerationConfig.from_env()
        if not config:
            return None
        if model:
            config.model = model
        return DashScopeGenerationChatClient(config)

    if provider == "dashscope":
        load_dotenv()
        requested_model = model or os.environ.get("DASHSCOPE_MODEL", DEFAULT_DASHSCOPE_MODEL)
        if _uses_dashscope_generation(requested_model):
            config = DashScopeGenerationConfig.from_env()
            if not config:
                return None
            config.model = requested_model
            return DashScopeGenerationChatClient(config)
        config = DashScopeConfig.from_env()
        if not config:
            return None
        if model:
            config.model = model
        return DashScopeChatClient(config)

    config = ArkConfig.from_env()
    if not config:
        return None
    if model:
        config.model = model
    return ArkChatClient(config)


def build_llm_decider_from_env(provider: str = "ark", model: str | None = None) -> LLMDecisionClient | None:
    client = build_chat_client_from_env(provider, model)
    return LLMDecisionClient(client) if client else None


def normalize_provider(provider: str | None) -> str:
    value = (provider or "ark").strip().lower()
    if value in {"doubao", "ark", "volcengine", "bytedance"}:
        return "ark"
    if value in {"dashscope-native", "dashscope_native", "dashscope-generation", "dashscope_generation", "deepseek-v32", "deepseek-v3.2"}:
        return "dashscope-native"
    if value in {"deepseek", "dashscope", "aliyun", "bailian"}:
        return "dashscope"
    return "ark"


def extract_json_object(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = _loads_json_lenient(text)
    except LLMError as full_error:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise LLMError("LLM response did not contain a JSON object") from full_error
        data = _loads_json_lenient(match.group(0))
    if not isinstance(data, dict):
        raise LLMError("LLM response JSON must be an object")
    return data


def _wolf_council_output_schema() -> dict[str, Any]:
    return {
        "attack_target": "整数玩家 id；必须来自 legal_attack_targets",
        "consensus_reason": "狼队私有共识理由，只能引用公开历史和狼队提案，不得引用越权信息",
        "proposal_summary": [
            {
                "wolf_id": "狼人座位 id",
                "preferred_target": "该狼人提案目标",
                "accepted": "是否采纳其主要意见",
                "note": "简短说明",
            }
        ],
        "day_plan": {
            "overall": "明天狼队整体可选方向，不是强制任务",
            "optional_routes": ["可选协同路线，避免模板化"],
            "risk_controls": ["避免暴露狼队协同的注意事项"],
        },
        "constraints": ["公开发言不能暴露狼队会议、夜刀、队友身份或私有信息"],
        "confidence": "0 到 1 的数字",
    }


def _wolf_council_plan_from_raw(raw: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    legal_targets = {int(item) for item in context.get("legal_attack_targets", [])}
    attack_target = _optional_int(raw.get("attack_target"))
    if attack_target is None or attack_target not in legal_targets:
        raise LLMError(f"wolf council attack_target 必须来自 legal_attack_targets={sorted(legal_targets)}。")
    day_plan = raw.get("day_plan") if isinstance(raw.get("day_plan"), dict) else {}
    return {
        "coordination_mode": "llm_wolf_council",
        "coordination_status": "llm_ok",
        "private_only": True,
        "not_forced": True,
        "attack_target": attack_target,
        "consensus_reason": _short_optional(raw.get("consensus_reason"), 420),
        "proposal_summary": _list_of_dicts(raw.get("proposal_summary"), limit=6),
        "day_plan": {
            "overall": _short_optional(day_plan.get("overall"), 420),
            "optional_routes": _string_list(day_plan.get("optional_routes"), limit=6, item_limit=240),
            "risk_controls": _string_list(day_plan.get("risk_controls"), limit=6, item_limit=240),
        },
        "constraints": _string_list(raw.get("constraints"), limit=8, item_limit=180)
        or [
            "公开发言不能暴露狼队会议、夜刀目标、狼队友身份或夜间私有信息。",
            "白天路线是可选策略，不是对具体狼人的强制命令。",
        ],
        "confidence": _bounded_float(raw.get("confidence"), default=0.65),
        "raw_plan": to_jsonable(raw),
    }


def _loads_json_lenient(text: str) -> Any:
    last_error: json.JSONDecodeError | None = None
    for candidate in (text, _strip_invalid_json_control_chars(text)):
        for strict in (True, False):
            try:
                return json.loads(candidate, strict=strict)
            except json.JSONDecodeError as exc:
                last_error = exc
    if last_error is None:
        raise LLMError("LLM response JSON parse failed")
    raise LLMError(f"LLM response JSON parse failed: {last_error.msg}")


def _strip_invalid_json_control_chars(text: str) -> str:
    return "".join(ch for ch in text if ch in "\t\n\r" or ord(ch) >= 32)


def _decision_from_raw(raw: dict[str, Any], obs: PrivateObservation) -> AgentDecision:
    if "evidence_event_ids" not in raw:
        raise LLMError("必须输出 evidence_event_ids 数组，用于引用 public_history 中的证据事件。")
    if "quoted_evidence" not in raw:
        raise LLMError("必须输出 quoted_evidence 数组，用于保存公开证据短摘录。")

    action_value = str(raw.get("action", "")).strip()
    legal_values = {action.value for action in obs.legal_actions}
    if action_value not in legal_values:
        raise LLMError(f"非法 action：{action_value}。必须从 legal_actions 中选择。")
    action = ActionType(action_value)
    target_id = _optional_int(raw.get("target_id"))
    alive_ids = {player["id"] for player in obs.alive_players}
    if target_id is not None and target_id not in alive_ids:
        raise LLMError(f"非法 target_id：P{target_id} 当前不是存活玩家。")
    if action in {ActionType.SPEAK, ActionType.PASS} and target_id is not None:
        raise LLMError(f"动作 {action.value} 的 target_id 必须为 null。")
    if action in {ActionType.KILL, ActionType.INSPECT, ActionType.POISON, ActionType.VOTE, ActionType.SHOOT}:
        if target_id is None:
            raise LLMError(f"动作 {action.value} 必须填写 target_id。")
        if target_id == obs.self_id:
            raise LLMError(f"动作 {action.value} 不能选择自己。")
        if action == ActionType.KILL and target_id in {int(item) for item in obs.private_knowledge.get("pack_ids", [])}:
            raise LLMError(f"狼人夜间不能刀狼队友 P{target_id}。")
        if action == ActionType.VOTE:
            tie_pk = obs.private_knowledge.get("action_context", {}).get("tie_pk", {})
            allowed_targets = tie_pk.get("allowed_vote_targets") or []
            if allowed_targets and target_id not in {int(item) for item in allowed_targets}:
                raise LLMError(f"PK 二次投票只能投 allowed_vote_targets={allowed_targets}，不能投 P{target_id}。")
    if action == ActionType.SAVE:
        attacked = obs.private_knowledge.get("attacked_tonight")
        if target_id != attacked:
            raise LLMError(f"女巫 save 的 target_id 必须等于 attacked_tonight，当前 attacked_tonight={attacked}。")

    evidence_event_ids = _evidence_ids(raw.get("evidence_event_ids"), obs)
    quoted_evidence = _quoted_evidence(raw.get("quoted_evidence"))
    if quoted_evidence and not evidence_event_ids:
        raise LLMError("quoted_evidence 不能脱离 evidence_event_ids 单独出现。")
    if (
        obs.phase in {Phase.DAY_SPEECH, Phase.DAY_VOTE, Phase.HUNTER_SHOT}
        and obs.public_history
        and action != ActionType.PASS
        and not evidence_event_ids
    ):
        raise LLMError("白天公开决策必须引用至少一个 public_history 事件 id。")

    raw_speech = str(raw.get("speech") or "")
    raw_reason = str(raw.get("reason") or "LLM structured decision.")
    if obs.self_role == Role.SEER and action == ActionType.SPEAK:
        _validate_seer_public_speech(raw_speech)
    deception_intent = _short_optional(raw.get("deception_intent"), 60)
    public_cover_story = _short_optional(raw.get("public_cover_story"), 240)
    if (
        obs.self_role == Role.WEREWOLF
        and obs.phase in {Phase.DAY_SPEECH, Phase.DAY_VOTE}
        and action != ActionType.PASS
        and (not deception_intent or not public_cover_story)
    ):
        raise LLMError("狼人白天发言/投票必须输出 deception_intent 和 public_cover_story，用于说明伪装意图与公开包装。")
    speech_max_chars = _env_int("LLM_SPEECH_MAX_CHARS", DEFAULT_SPEECH_MAX_CHARS)
    reason_max_chars = _env_int("LLM_REASON_MAX_CHARS", DEFAULT_REASON_MAX_CHARS)
    speech = raw_speech[:speech_max_chars]
    reason = raw_reason[:reason_max_chars]
    confidence = _bounded_float(raw.get("confidence"), default=0.55)
    return AgentDecision(
        actor_id=obs.self_id,
        role=obs.self_role,
        action=action,
        target_id=target_id,
        speech=speech,
        reason=reason,
        confidence=confidence,
        metadata={
            "raw_decision": to_jsonable(raw),
            "evidence_event_ids": evidence_event_ids,
            "quoted_evidence": quoted_evidence,
            "raw_speech_length": len(raw_speech),
            "speech_max_chars": speech_max_chars,
            "speech_truncated_by_system": len(raw_speech) > speech_max_chars,
            "raw_reason_length": len(raw_reason),
            "reason_max_chars": reason_max_chars,
            "reason_truncated_by_system": len(raw_reason) > reason_max_chars,
            "deception_intent": deception_intent,
            "public_cover_story": public_cover_story,
        },
    )


def _optional_int(value: Any) -> int | None:
    if value in {None, "", "null"}:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise LLMError(f"target_id 必须是整数或 null：{value!r}")


def _short_optional(value: Any, limit: int) -> str:
    return str(value or "").strip().replace("\n", " ")[:limit]


def _validate_seer_public_speech(speech: str) -> None:
    compact = re.sub(r"\s+", "", speech)
    if not compact or not any(token in compact for token in ("查验", "验了", "验的是", "金水")):
        return
    concrete_identity_terms = ("村民牌", "平民牌", "普通村民", "村民身份", "民牌")
    near_check = (
        r"(查验|验了|验的是|验人|金水).{0,28}("
        + "|".join(map(re.escape, concrete_identity_terms))
        + r")"
    )
    near_identity = (
        r"("
        + "|".join(map(re.escape, concrete_identity_terms))
        + r").{0,28}(查验|验了|验的是|验人|金水)"
    )
    if re.search(near_check, compact) or re.search(near_identity, compact):
        raise LLMError("预言家查验只能公开阵营结果，不能称目标是村民牌/平民/具体身份；请改成“好人阵营”或“金水”。")


def _string_list(value: Any, *, limit: int, item_limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    items: list[str] = []
    for item in value[:limit]:
        text = str(item or "").strip().replace("\n", " ")
        if text and text not in items:
            items.append(text[:item_limit])
    return items


def _list_of_dicts(value: Any, *, limit: int) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    items: list[dict[str, Any]] = []
    for item in value[:limit]:
        if isinstance(item, dict):
            items.append(to_jsonable(item))
    return items


def _evidence_ids(value: Any, obs: PrivateObservation) -> list[int]:
    if value is None:
        raise LLMError("evidence_event_ids 必须是数组，不能省略。")
    if not isinstance(value, list):
        raise LLMError("evidence_event_ids 必须是数组。")
    visible_ids = {int(event["id"]) for event in obs.public_history if event.get("id") is not None}
    ids: list[int] = []
    for item in value[:6]:
        try:
            event_id = int(item)
        except (TypeError, ValueError):
            raise LLMError(f"evidence_event_ids 中存在非整数 id：{item!r}")
        if event_id not in visible_ids:
            raise LLMError(f"evidence_event_ids 只能引用 public_history 中存在的事件 id：{event_id}")
        if event_id not in ids:
            ids.append(event_id)
    return ids


def _quoted_evidence(value: Any) -> list[str]:
    if value is None:
        raise LLMError("quoted_evidence 必须是数组，不能省略。")
    if not isinstance(value, list):
        raise LLMError("quoted_evidence 必须是数组。")
    quotes: list[str] = []
    for item in value[:6]:
        quote = str(item).strip()
        if quote and quote not in quotes:
            quotes.append(quote[:140])
    return quotes


def _bounded_float(value: Any, *, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(0.0, min(1.0, parsed))


def _temperature_for_role(role: Role) -> float:
    if role == Role.WEREWOLF:
        return 0.45
    if role in {Role.SEER, Role.WITCH}:
        return 0.22
    return 0.32


def _compatible_strategy_memory(items: list[str]) -> list[str]:
    return [item for item in items if not any(term in item for term in UNSUPPORTED_RULE_TERMS)]


def _env_bool(key: str, default: bool) -> bool:
    value = os.environ.get(key)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(key: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(key, default)))
    except (TypeError, ValueError):
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return max(0.0, float(os.environ.get(key, default)))
    except (TypeError, ValueError):
        return default


def _uses_dashscope_generation(model: str | None) -> bool:
    value = (model or "").strip().lower()
    return value.startswith("deepseek-v3.2")


def _field(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _safe_http_error(exc: urllib.error.HTTPError, provider: str) -> str:
    try:
        body = exc.read().decode("utf-8", errors="replace")
        body = body[:500]
    except Exception:
        body = ""
    return f"{provider} HTTP {exc.code}: {body}"


def _http_retry_delay(exc: urllib.error.HTTPError, attempt: int, error_text: str = "") -> float:
    retry_after = exc.headers.get("Retry-After") if exc.headers else None
    retry_after_seconds: float | None = None
    if retry_after:
        try:
            retry_after_seconds = max(0.0, float(retry_after))
        except ValueError:
            retry_after_seconds = None
    if exc.code == 429:
        return rate_limit_retry_delay_from_text(error_text, attempt, retry_after_seconds=retry_after_seconds)
    if exc.code >= 500:
        return _env_float("LLM_SERVER_RETRY_SECONDS", 3.0 * (attempt + 1))
    return 1.5 * (attempt + 1)


def _retry_delay_from_text(exc: Exception | None, attempt: int) -> float:
    text = f"{type(exc).__name__}: {exc}".lower() if exc else ""
    if _looks_like_rate_limit_text(text):
        return rate_limit_retry_delay_from_text(text, attempt)
    if "timeout" in text or "timed out" in text:
        return _env_float("LLM_TIMEOUT_RETRY_SECONDS", 4.0)
    return 1.5 * (attempt + 1)


def rate_limit_retry_delay_from_text(
    text: str,
    attempt: int = 0,
    *,
    retry_after_seconds: float | None = None,
) -> float:
    normalized = (text or "").lower()
    if _looks_like_tpm_limit(normalized):
        base = _env_float("LLM_TPM_RATE_LIMIT_RETRY_SECONDS", 75.0)
    elif _looks_like_rpm_limit(normalized):
        base = _env_float("LLM_RPM_RATE_LIMIT_RETRY_SECONDS", 35.0)
    else:
        base = _env_float("LLM_RATE_LIMIT_RETRY_SECONDS", 45.0)
    multiplier = max(1.0, _env_float("LLM_RATE_LIMIT_BACKOFF_MULTIPLIER", 1.4))
    max_delay = _env_float("LLM_RATE_LIMIT_RETRY_MAX_SECONDS", 180.0)
    jitter = _env_float("LLM_RATE_LIMIT_JITTER_SECONDS", 5.0)
    delay = base * (multiplier ** max(0, attempt))
    if retry_after_seconds is not None:
        delay = max(delay, retry_after_seconds)
    if jitter > 0:
        delay += random.uniform(0.0, jitter)
    return min(max_delay, max(0.0, delay))


def _looks_like_rate_limit(exc: Exception | None) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower() if exc else ""
    return _looks_like_rate_limit_text(text)


def _looks_like_rate_limit_text(text: str) -> bool:
    return (
        "429" in text
        or "ratelimit" in text
        or "rate limit" in text
        or "rate_limit" in text
        or "tpm" in text
        or "rpm" in text
        or "endpointtpmexceeded" in text
        or "endpointrpmexceeded" in text
    )


def _looks_like_tpm_limit(text: str) -> bool:
    return "tpm" in text or "endpointtpmexceeded" in text or "tokens per minute" in text


def _looks_like_rpm_limit(text: str) -> bool:
    return "rpm" in text or "endpointrpmexceeded" in text or "requests per minute" in text


def _wait_for_rate_limit_cooldown(provider: str) -> None:
    if not _env_bool("LLM_SHARED_RATE_LIMIT_COOLDOWN", True):
        return
    key = _rate_limit_key(provider)
    while True:
        with _RATE_LIMIT_LOCK:
            wait_seconds = _RATE_LIMIT_UNTIL_BY_PROVIDER.get(key, 0.0) - time.monotonic()
        if wait_seconds <= 0:
            return
        time.sleep(wait_seconds)


def _record_rate_limit_cooldown(provider: str, delay_seconds: float) -> None:
    if not _env_bool("LLM_SHARED_RATE_LIMIT_COOLDOWN", True) or delay_seconds <= 0:
        return
    key = _rate_limit_key(provider)
    cooldown_until = time.monotonic() + delay_seconds
    with _RATE_LIMIT_LOCK:
        _RATE_LIMIT_UNTIL_BY_PROVIDER[key] = max(_RATE_LIMIT_UNTIL_BY_PROVIDER.get(key, 0.0), cooldown_until)


def _rate_limit_key(provider: str) -> str:
    return (provider or "default").strip().lower()
