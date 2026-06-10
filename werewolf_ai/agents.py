from __future__ import annotations

from dataclasses import replace

from .models import (
    AgentDecision,
    PrivateObservation,
    Role,
    StrategyVersion,
)
from .llm import LLMDecisionClient


def default_strategy_versions(version_id: str = "initial") -> dict[Role, StrategyVersion]:
    """Return one strategy profile per role.

    These profiles intentionally expose strategy memory and knobs as data. The
    evolution manager mutates only this layer, never source code.
    """

    if version_id == "evolved":
        return evolved_strategy_versions()

    return {
        Role.WEREWOLF: StrategyVersion(
            role=Role.WEREWOLF,
            version_id=version_id,
            prompt_name="wolf_shadow_v1",
            prompt_summary="伪装成谨慎好人，寻找神职，但早期偶尔跟票过重。",
            strategy_memory=[
                "白天不要直接暴露狼队视角。",
                "优先攻击发言强势、可能带队的好人。",
                "早期投票避免三狼集中裸冲同一好人；理由不足时可以分票、弃票、轻踩队友或选择另一个公开可疑目标。",
                "狼队可以尝试悍跳、软对跳、深水、切割和分线施压，但每个座位要用不同公开理由包装，不要像同一套模板。",
                "狼人允许策略性不诚实，但只能扭曲解释公开发言和票型，不能编造不存在的事件、查验或夜间信息。",
                "当场上单预言家过于稳定时，狼队要主动考虑软对跳、质疑查验链、倒钩或制造第二视角，而不是全员默认认下。",
            ],
            parameters={"decision_mode": "llm_only"},
        ),
        Role.SEER: StrategyVersion(
            role=Role.SEER,
            version_id=version_id,
            prompt_name="seer_cautious_v1",
            prompt_summary="重视隐藏身份，但查到狼后有时披露不足。",
            strategy_memory=[
                "前两天先积累查验信息，不要过早暴露。",
                "发言中可以给出轻微站边，但不要把全部信息一次交出。",
                "查到狼人、被强质疑或场上需要带队时公开身份收益更高；只有金水或信息不足时可以选择隐忍和铺垫后续查验计划。",
            ],
            parameters={"decision_mode": "llm_only"},
        ),
        Role.WITCH: StrategyVersion(
            role=Role.WITCH,
            version_id=version_id,
            prompt_name="witch_conservative_v1",
            prompt_summary="药水使用保守，毒药偶尔被情绪票影响。",
            strategy_memory=[
                "解药尽量留到关键轮次。",
                "毒药需要结合发言和投票，不要只凭单点怀疑。",
                "首夜是否救人需要结合被刀目标和局势判断，不是固定动作。",
            ],
            parameters={"decision_mode": "llm_only"},
        ),
        Role.HUNTER: StrategyVersion(
            role=Role.HUNTER,
            version_id=version_id,
            prompt_name="hunter_patient_v1",
            prompt_summary="倾向藏身份，开枪时容易保守。",
            strategy_memory=[
                "死亡时如果没有强逻辑，可以不开枪避免误伤。",
                "发言以普通好人视角分析局势。",
            ],
            parameters={"decision_mode": "llm_only"},
        ),
        Role.VILLAGER: StrategyVersion(
            role=Role.VILLAGER,
            version_id=version_id,
            prompt_name="villager_basic_v1",
            prompt_summary="根据公开发言投票，但归票能力不足。",
            strategy_memory=[
                "没有技能信息，只根据公开发言、投票和死亡信息推理。",
                "不要假装拥有夜间信息。",
            ],
            parameters={"decision_mode": "llm_only"},
        ),
    }


def evolved_strategy_versions(parent: dict[Role, StrategyVersion] | None = None) -> dict[Role, StrategyVersion]:
    base = parent or default_strategy_versions("initial")
    evolved: dict[Role, StrategyVersion] = {}
    for role, profile in base.items():
        params = dict(profile.parameters)
        memory = list(profile.strategy_memory)
        prompt_name = profile.prompt_name.replace("_v1", "_v2")

        if role == Role.WEREWOLF:
            params["evolution_focus"] = "wolf_bluffing_and_pack_vote_discipline"
            memory.extend(
                [
                    "狼队白天不要三狼集中裸冲同一个目标，除非已有公开理由。",
                    "夜晚优先处理公开跳预言家、强势归票者和被普遍信任的好人。",
                    "当场上单预言家过于稳定时，狼队可以自主选择悍跳、软对跳、质疑查验链或切割被查队友，而不是只隐藏身份。",
                    "每名狼人都要结合自己的座位人格形成独立发言和投票理由，避免 P2/P3 式同模板暴露。",
                    "悍跳或对跳预言家时，要补齐查验对象、查验结论、后续查验计划和公开证据包装，避免只喊身份不成链。",
                    "狼队不一定都要保护悍跳狼，至少一名狼人可以倒钩或轻踩队友以换取深水身份。",
                ]
            )
        elif role == Role.SEER:
            params["evolution_focus"] = "inspection_disclosure_and_vote_leadership"
            memory.extend(
                [
                    "查到狼人后通常应在白天明确报出查杀并带票；如果选择隐忍，必须有更高收益的明确理由。",
                    "如果已有金水，要用金水建立可信站边，不要只给模糊判断。",
                    "没有查杀时不必机械首日跳出，可以用软站边、后续查验计划和公开逻辑逐步建立可信度。",
                ]
            )
        elif role == Role.WITCH:
            params["evolution_focus"] = "medicine_timing_and_poison_discipline"
            memory.extend(
                [
                    "首夜救人是常见但非强制选择；如果被刀目标价值不高或局势需要保药，可以选择不救并说明风险。",
                    "毒药只优先给公开查杀或多轮高嫌疑目标，避免毒死强好人。",
                ]
            )
        elif role == Role.HUNTER:
            params["evolution_focus"] = "shot_selection_from_public_evidence"
            memory.extend(
                [
                    "死亡开枪时优先带走公开查杀或投票链最高嫌疑。",
                    "白天发言跟随可信预言家信息，不做无依据反打。",
                ]
            )
        elif role == Role.VILLAGER:
            params["evolution_focus"] = "public_reasoning_and_vote_coordination"
            memory.extend(
                [
                    "出现明确查杀时优先围绕查杀归票。",
                    "用投票链和发言矛盾识别狼人，不做纯随机票。",
                ]
            )

        evolved[role] = replace(
            profile,
            version_id="evolved",
            prompt_name=prompt_name,
            prompt_summary=f"进化版：{profile.prompt_summary} 已吸收复盘反思，降低随机性并强化阵营目标。",
            strategy_memory=_dedupe(memory),
            parameters=params,
            parent_id=profile.version_id,
            promoted=True,
        )
    return evolved


class RoleAgent:
    def __init__(self, profile: StrategyVersion, llm_decider: LLMDecisionClient | None = None):
        self.profile = profile
        self.llm_decider = llm_decider

    def decide(self, observation: PrivateObservation, _rng: object | None = None) -> AgentDecision:
        if self.llm_decider is None:
            raise RuntimeError("LLM decider is required. Rule-based role agents have been removed.")
        return self.llm_decider.decide(observation, self.profile)


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result
