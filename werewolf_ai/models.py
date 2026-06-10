from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field, is_dataclass
from enum import Enum
from typing import Any


class Role(str, Enum):
    WEREWOLF = "werewolf"
    SEER = "seer"
    WITCH = "witch"
    HUNTER = "hunter"
    VILLAGER = "villager"

    @property
    def zh(self) -> str:
        return {
            Role.WEREWOLF: "狼人",
            Role.SEER: "预言家",
            Role.WITCH: "女巫",
            Role.HUNTER: "猎人",
            Role.VILLAGER: "村民",
        }[self]


class Faction(str, Enum):
    WEREWOLVES = "werewolves"
    VILLAGERS = "villagers"

    @property
    def zh(self) -> str:
        return "狼人阵营" if self == Faction.WEREWOLVES else "好人阵营"


class Phase(str, Enum):
    SETUP = "setup"
    NIGHT_WOLF = "night_wolf"
    NIGHT_SEER = "night_seer"
    NIGHT_WITCH = "night_witch"
    DAWN = "dawn"
    DAY_SPEECH = "day_speech"
    DAY_VOTE = "day_vote"
    HUNTER_SHOT = "hunter_shot"
    GAME_OVER = "game_over"

    @property
    def zh(self) -> str:
        return {
            Phase.SETUP: "初始化",
            Phase.NIGHT_WOLF: "夜晚-狼人",
            Phase.NIGHT_SEER: "夜晚-预言家",
            Phase.NIGHT_WITCH: "夜晚-女巫",
            Phase.DAWN: "天亮",
            Phase.DAY_SPEECH: "白天发言",
            Phase.DAY_VOTE: "白天投票",
            Phase.HUNTER_SHOT: "猎人开枪",
            Phase.GAME_OVER: "游戏结束",
        }[self]


class ActionType(str, Enum):
    KILL = "kill"
    INSPECT = "inspect"
    SAVE = "save"
    POISON = "poison"
    SPEAK = "speak"
    VOTE = "vote"
    SHOOT = "shoot"
    PASS = "pass"


def role_faction(role: Role) -> Faction:
    return Faction.WEREWOLVES if role == Role.WEREWOLF else Faction.VILLAGERS


@dataclass(frozen=True)
class GameConfig:
    """Rule knobs for the challenge demo.

    The default is a 9-player common contest-friendly setup:
    3 werewolves, seer, witch, hunter, and 3 villagers.
    """

    player_count: int = 9
    roles: tuple[Role, ...] = (
        Role.WEREWOLF,
        Role.WEREWOLF,
        Role.WEREWOLF,
        Role.SEER,
        Role.WITCH,
        Role.HUNTER,
        Role.VILLAGER,
        Role.VILLAGER,
        Role.VILLAGER,
    )
    max_days: int = 8
    reveal_roles_on_death: bool = False
    allow_hunter_shot_after_poison: bool = False


def game_rule_summary(config: GameConfig | None = None) -> dict[str, Any]:
    """Structured rule context shared with agents and post-game reviewers."""

    config = config or GameConfig()
    counts = Counter(config.roles)
    return {
        "ruleset_name": "9人标准局-无警长版",
        "player_count": config.player_count,
        "role_distribution": {
            "werewolf": counts[Role.WEREWOLF],
            "seer": counts[Role.SEER],
            "witch": counts[Role.WITCH],
            "hunter": counts[Role.HUNTER],
            "villager": counts[Role.VILLAGER],
        },
        "role_distribution_zh": f"{counts[Role.WEREWOLF]}狼、{counts[Role.SEER]}预言家、{counts[Role.WITCH]}女巫、{counts[Role.HUNTER]}猎人、{counts[Role.VILLAGER]}村民",
        "factions": {
            "werewolves": "狼人阵营：狼人互知队友，夜晚共同刀人，白天可伪装、误导、分票或冲票。",
            "villagers": "好人阵营：预言家、女巫、猎人、村民同属好人阵营，只能基于自己权限内的信息推理。",
        },
        "phase_order": [
            "夜晚狼人刀人",
            "夜晚预言家查验",
            "夜晚女巫可救人或毒人",
            "天亮公布死亡结果",
            "白天顺序发言",
            "白天同时投票，可弃票",
            "放逐结算，猎人符合条件时可开枪",
            "胜负判定",
        ],
        "night_rules": {
            "werewolf": "狼人夜晚只能刀非自己、非狼队友的存活玩家。",
            "seer": "预言家每晚查验一名非自己的存活玩家，只知道阵营结果：werewolf 表示狼人阵营，villagers/好人/金水表示好人阵营；查验不会给出具体身份，因此猎人、女巫、预言家、村民被报好人/金水都不冲突。预言家公开报验人时只能说“好人阵营/金水”或“狼人阵营/查杀”，不能把好人阵营结果说成“村民牌、平民、普通村民、村民身份”。",
            "witch": "女巫有一瓶解药和一瓶毒药；解药只能救当夜被刀目标；毒药只能毒一名非自己的存活玩家。",
            "hunter": "猎人没有夜间主动技能。",
            "villager": "村民没有夜间信息或夜间技能。",
        },
        "day_rules": {
            "speech": "白天发言只能引用公开历史和自己允许公开的信息。",
            "vote": "投票阶段所有存活玩家同时投票，不能看到本轮其他人的票后再跟票；可以弃票，弃票不计入票数。",
            "tie_pk": "首轮最高票平票时，平票玩家依次 PK 发言；随后非 PK 存活玩家只能在平票对象中二次投票或弃票。",
            "exile": "二次投票得票最高者被放逐；二次仍平票或无人有效票时本轮无人放逐。",
            "role_reveal": "默认死亡不公开真实身份，除非公开发言中有人自跳、他人猜测，或触发猎人死亡后的公开技能结算。猎人开枪、不开枪、被毒无法开枪都属于公开事件；该事件公开后，所有玩家都知道该玩家是猎人。",
        },
        "hunter_rules": {
            "can_shoot": "猎人被夜刀或白天放逐死亡时可开枪。",
            "public_resolution": "猎人死亡后的技能结算公开进行：若开枪，公开带走目标；若选择不开枪，也公开为猎人没有开枪；若被女巫毒死且当前规则禁止开枪，也公开为猎人被毒不能开枪。",
            "poison_rule": "当前规则下猎人被女巫毒死不能开枪。" if not config.allow_hunter_shot_after_poison else "当前规则下猎人被毒死也可以开枪。",
        },
        "win_conditions": [
            "狼人全灭，好人阵营获胜。",
            "三名村民全部阵亡，狼人屠民获胜。",
            "三名神职（预言家、女巫、猎人）全部阵亡，狼人屠神获胜。",
            "不能把任意三个好人阵亡直接判定为屠边；必须区分村民边和神职边。",
            "达到最大天数仍未分出胜负时按引擎超时规则结算。",
        ],
        "disabled_mechanics": [
            "本局没有警长竞选。",
            "本局没有警徽。",
            "本局没有警徽流、撕警徽、移交警徽、警长归票权或警长票权重。",
        ],
        "terminology": {
            "seer_plan": "预言家可以说“后续查验计划”或“下一晚想查验的对象”，不要称为警徽流。",
            "claims": "public_claims 只表示公开自跳或公开声称查验结果；public_role_assertions 只是他人判断，不等于本人自跳。",
            "seer_check_result": "预言家查验结果是阵营结果，不是具体身份。好人、金水、villagers 均表示好人阵营，兼容猎人/女巫/预言家/村民；不能因为自己是神职就认为给自己发金水的预言家报错身份。预言家不能把阵营查验包装成“村民牌、平民、普通村民、村民身份”等具体身份判断。",
            "self_identity_counterclaim": "玩家声称“别人给我报错身份”只是一种自述或自跳线索；除非该玩家身份已通过公开技能结算、死亡公开或明确自跳并被规则验证，否则其他玩家不能把它当作铁证。",
        },
        "rule_priority": "以上当前规则高于模型预训练中的其他狼人杀版本，也高于策略记忆；如果策略记忆与当前规则冲突，必须忽略该记忆。",
    }


@dataclass
class PlayerState:
    id: int
    name: str
    role: Role
    faction: Faction
    alive: bool = True
    death_day: int | None = None
    death_phase: Phase | None = None
    death_reason: str | None = None
    agent_version: str = "initial"

    def public_view(self, reveal_role: bool = False) -> dict[str, Any]:
        data = {
            "id": self.id,
            "name": self.name,
            "alive": self.alive,
            "death_day": self.death_day,
            "death_reason": self.death_reason,
        }
        if reveal_role:
            data["role"] = self.role.value
            data["role_zh"] = self.role.zh
            data["faction"] = self.faction.value
        return data


@dataclass
class AgentDecision:
    actor_id: int
    role: Role
    action: ActionType
    target_id: int | None = None
    secondary_target_id: int | None = None
    speech: str = ""
    reason: str = ""
    confidence: float = 0.5
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class PrivateObservation:
    game_id: str
    day: int
    phase: Phase
    self_id: int
    self_name: str
    self_role: Role
    self_alive: bool
    alive_players: list[dict[str, Any]]
    public_history: list[dict[str, Any]]
    legal_actions: list[ActionType]
    private_knowledge: dict[str, Any]
    strategy_memory: list[str]
    prompt_name: str
    prompt_summary: str
    player_profile: dict[str, Any] = field(default_factory=dict)
    game_rules: dict[str, Any] = field(default_factory=dict)
    retrieved_memories: list[dict[str, Any]] = field(default_factory=list)
    belief_state: dict[str, Any] = field(default_factory=dict)

    def redacted_for_log(self) -> dict[str, Any]:
        """Small proof artifact that keeps private data local to this agent."""

        return {
            "self_id": self.self_id,
            "self_role": self.self_role.value,
            "phase": self.phase.value,
            "legal_actions": [action.value for action in self.legal_actions],
            "private_keys": sorted(self.private_knowledge.keys()),
            "public_event_count": len(self.public_history),
            "strategy_memory_count": len(self.strategy_memory),
            "retrieved_memory_count": len(self.retrieved_memories),
            "belief_state": {
                "summary": self.belief_state.get("summary"),
                "claim_count": len(self.belief_state.get("claims", [])),
                "top_suspicions": self.belief_state.get("top_suspicions", [])[:3],
                "self_commitments": self.belief_state.get("self_commitments", [])[-3:],
            },
            "ruleset_name": self.game_rules.get("ruleset_name"),
            "player_profile": {
                "seat_id": self.player_profile.get("seat_id"),
                "persona": self.player_profile.get("persona"),
                "risk_preference": self.player_profile.get("risk_preference"),
            },
        }


@dataclass
class GameEvent:
    id: int
    day: int
    phase: Phase
    event_type: str
    public_text: str
    actor_id: int | None = None
    target_id: int | None = None
    secondary_target_id: int | None = None
    public_payload: dict[str, Any] = field(default_factory=dict)
    private_payload: dict[str, Any] = field(default_factory=dict)
    decision: AgentDecision | None = None
    observation_proof: dict[str, Any] | None = None
    observation_snapshot: dict[str, Any] | None = None

    def public_view(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "day": self.day,
            "phase": self.phase.value,
            "phase_zh": self.phase.zh,
            "event_type": self.event_type,
            "public_text": self.public_text,
            "actor_id": self.actor_id,
            "target_id": self.target_id,
            "secondary_target_id": self.secondary_target_id,
            "public_payload": to_jsonable(self.public_payload),
        }


@dataclass
class StrategyVersion:
    role: Role
    version_id: str
    prompt_name: str
    prompt_summary: str
    strategy_memory: list[str] = field(default_factory=list)
    parameters: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, float] = field(default_factory=dict)
    promoted: bool = False
    parent_id: str | None = None


@dataclass
class GameResult:
    game_id: str
    seed: int
    version_id: str
    winner: Faction
    win_reason: str
    day: int
    players: list[PlayerState]
    events: list[GameEvent]
    observation_samples: list[dict[str, Any]]
    game_rules: dict[str, Any] = field(default_factory=dict)


@dataclass
class EvaluationReport:
    game_id: str
    winner: Faction
    scores: dict[str, float]
    role_scores: dict[str, dict[str, float]]
    key_decisions: list[dict[str, Any]]
    mistakes: list[dict[str, Any]]
    recommendations: dict[str, list[str]]
    summary: str


def to_jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {key: to_jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(item) for item in value]
    return value
