from __future__ import annotations

import random
import re
import uuid
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from .agents import RoleAgent, default_strategy_versions
from .belief_state import belief_view_for_player, initialize_belief_states, update_belief_states_after_event
from .llm import LLMDecisionClient, LLMError, build_llm_decider_from_env
from .memory_retriever import retrieve_memories
from .models import (
    ActionType,
    Faction,
    GameConfig,
    GameEvent,
    GameResult,
    Phase,
    PlayerState,
    PrivateObservation,
    Role,
    StrategyVersion,
    game_rule_summary,
    role_faction,
)
from .player_profiles import load_player_profiles, profile_for_player
from .skill_usage import estimate_skill_usage


VISIBLE_EVENT_TYPES = {
    "setup",
    "dawn_deaths",
    "dawn_peace",
    "speech",
    "vote",
    "vote_tie",
    "exile",
    "hunter_shot",
    "hunter_pass",
    "game_over",
}


class GameEngine:
    def __init__(
        self,
        config: GameConfig | None = None,
        *,
        seed: int | None = None,
        profiles: dict[Role, StrategyVersion] | None = None,
        version_id: str = "initial",
        llm_provider: str = "ark",
        llm_model: str | None = None,
        llm_decider: LLMDecisionClient | None = None,
        memory_bank_by_role: dict[Role, list[str]] | None = None,
        memory_retrieval_top_k: int = 5,
        event_callback: Callable[[GameEvent, "GameEngine"], None] | None = None,
    ):
        self.config = config or GameConfig()
        self.seed = seed if seed is not None else random.randint(1, 1_000_000)
        self.rng = random.Random(self.seed)
        self.version_id = version_id
        self.llm_provider = llm_provider
        self.llm_model = llm_model
        self.game_id = f"game-{self.seed}-{uuid.uuid4().hex[:6]}"
        self.day = 0
        self.events: list[GameEvent] = []
        self.observation_samples: list[dict[str, Any]] = []
        self.debug_observations: list[PrivateObservation] = []
        self.profiles = profiles or default_strategy_versions(version_id)
        self.memory_bank_by_role = memory_bank_by_role if memory_bank_by_role is not None else {}
        self.memory_retrieval_top_k = memory_retrieval_top_k
        self.player_profiles = load_player_profiles()
        self.game_rules = game_rule_summary(self.config)
        self.players = self._build_players()
        self.belief_states = initialize_belief_states(self.players)
        self.llm_decider = llm_decider or build_llm_decider_from_env(llm_provider, llm_model)
        if self.llm_decider is None:
            raise RuntimeError(f"LLM provider '{llm_provider}' is not configured. Rule-based agents have been removed.")
        self.agents = {
            player.id: RoleAgent(
                self.profiles[player.role],
                llm_decider=self.llm_decider,
            )
            for player in self.players
        }
        self.seer_inspections: dict[int, dict[int, str]] = defaultdict(dict)
        self.witch_has_antidote = True
        self.witch_has_poison = True
        self.hunter_has_shot = False
        self.wolf_pack_plans: dict[int, dict[str, Any]] = {}
        self.winner: Faction | None = None
        self.win_reason = ""
        self.event_callback = event_callback

    def run(self) -> GameResult:
        self._log(
            Phase.SETUP,
            "setup",
            f"{self.game_rules['ruleset_name']}初始化：{self.game_rules['role_distribution_zh']}。",
            public_payload={
                "visible_to_players": True,
                "role_setup": self.game_rules["role_distribution_zh"],
                "ruleset_name": self.game_rules["ruleset_name"],
                "disabled_mechanics": self.game_rules["disabled_mechanics"],
            },
            private_payload={"agent_backend": self._effective_backend()},
        )

        while self.winner is None and self.day < self.config.max_days:
            self.day += 1
            night_deaths = self._night_phase()
            self._announce_dawn(night_deaths)
            self._maybe_hunter_shot(night_deaths)
            if self._check_win():
                break

            self._refresh_wolf_pack_plan(night_deaths)
            self._day_speeches()
            self._day_vote()
            if self._check_win():
                break

        if self.winner is None:
            self._force_timeout_result()

        self._log(
            Phase.GAME_OVER,
            "game_over",
            f"游戏结束：{self.winner.zh}获胜。{self.win_reason}",
            public_payload={"winner": self.winner.value, "winner_zh": self.winner.zh, "visible_to_players": True},
        )
        return GameResult(
            game_id=self.game_id,
            seed=self.seed,
            version_id=self.version_id,
            winner=self.winner,
            win_reason=self.win_reason,
            day=self.day,
            players=self.players,
            events=self.events,
            observation_samples=self.observation_samples,
            game_rules=self.game_rules,
        )

    def build_observation(
        self,
        player_id: int,
        phase: Phase,
        legal_actions: list[ActionType],
        *,
        action_context: dict[str, Any] | None = None,
    ) -> PrivateObservation:
        player = self._player(player_id)
        action_context = action_context or {}
        private_knowledge: dict[str, Any] = {
            "public_claims": self._extract_public_claims(),
            "public_role_assertions": self._extract_public_role_assertions(),
            "public_role_mentions": self._extract_public_role_mentions(),
        }
        if action_context:
            private_knowledge["action_context"] = action_context

        if player.role == Role.WEREWOLF:
            pack = [p for p in self.players if p.role == Role.WEREWOLF]
            private_knowledge["pack_ids"] = [p.id for p in pack]
            private_knowledge["pack_names"] = [p.name for p in pack]
            pack_plan = self.wolf_pack_plans.get(self.day)
            if pack_plan and phase in {Phase.DAY_SPEECH, Phase.DAY_VOTE}:
                private_knowledge["pack_plan"] = pack_plan
                private_knowledge["wolf_strategy_space"] = _wolf_strategy_space_for_player(pack_plan, player.id)
        elif player.role == Role.SEER:
            private_knowledge["inspections"] = dict(self.seer_inspections.get(player_id, {}))
        elif player.role == Role.WITCH:
            private_knowledge["has_antidote"] = self.witch_has_antidote
            private_knowledge["has_poison"] = self.witch_has_poison
            if phase == Phase.NIGHT_WITCH:
                private_knowledge["attacked_tonight"] = action_context.get("attacked_tonight")
        elif player.role == Role.HUNTER:
            private_knowledge["can_shoot"] = not self.hunter_has_shot

        player_profile = profile_for_player(player.id, self.player_profiles)
        public_history = self._public_history()
        retrieved_memories = retrieve_memories(
            role=player.role,
            phase=phase,
            public_history=public_history,
            private_knowledge=private_knowledge,
            legal_actions=legal_actions,
            player_profile=player_profile,
            max_items=self.memory_retrieval_top_k,
        )

        strategy_memory = list(self.profiles[player.role].strategy_memory)
        for memory in self.memory_bank_by_role.get(player.role, []):
            item = f"记忆库复盘：{memory}"
            if item not in strategy_memory:
                strategy_memory.append(item)
        for memory in retrieved_memories:
            tag_text = ",".join(memory.get("tags", [])[:3])
            item = f"BM25记忆召回(score={memory.get('score', 0):.2f}; tags={tag_text})：{memory.get('memory', '')}"
            if item not in strategy_memory:
                strategy_memory.append(item)

        observation = PrivateObservation(
            game_id=self.game_id,
            day=self.day,
            phase=phase,
            self_id=player.id,
            self_name=player.name,
            self_role=player.role,
            self_alive=player.alive,
            alive_players=[p.public_view(False) for p in self._alive_players()],
            public_history=public_history,
            legal_actions=legal_actions,
            private_knowledge=private_knowledge,
            strategy_memory=strategy_memory,
            prompt_name=self.profiles[player.role].prompt_name,
            prompt_summary=self.profiles[player.role].prompt_summary,
            player_profile=player_profile,
            game_rules=self.game_rules,
            retrieved_memories=retrieved_memories,
            belief_state=belief_view_for_player(self.belief_states.get(player.id, {})),
        )
        self.debug_observations.append(observation)
        self.observation_samples.append(observation.redacted_for_log())
        return observation

    def _build_players(self) -> list[PlayerState]:
        roles = list(self.config.roles)
        if len(roles) != self.config.player_count:
            raise ValueError("GameConfig.roles length must match player_count")
        self.rng.shuffle(roles)
        players: list[PlayerState] = []
        for index, role in enumerate(roles, start=1):
            players.append(
                PlayerState(
                    id=index,
                    name=f"P{index}",
                    role=role,
                    faction=role_faction(role),
                    agent_version=self.version_id,
                )
            )
        return players

    def _night_phase(self) -> list[int]:
        self._phase_status(Phase.NIGHT_WOLF, f"第 {self.day} 夜：狼人行动中，请等待夜间流程继续。")
        wolf_target = self._wolf_action()
        self._phase_status(Phase.NIGHT_SEER, f"第 {self.day} 夜：预言家行动中，请等待夜间流程继续。")
        self._seer_action()
        self._phase_status(Phase.NIGHT_WITCH, f"第 {self.day} 夜：女巫行动中，请等待夜间流程继续。")
        saved_target, poisoned_target = self._witch_action(wolf_target)
        self._phase_status(Phase.DAWN, f"第 {self.day} 夜：夜间行动结算中，等待天亮公布结果。")

        deaths: list[int] = []
        if wolf_target is not None and wolf_target != saved_target:
            deaths.append(wolf_target)
        if poisoned_target is not None and poisoned_target not in deaths:
            deaths.append(poisoned_target)

        for pid in deaths:
            reason = "poison" if pid == poisoned_target else "werewolf_kill"
            self._kill_player(pid, Phase.DAWN, reason)

        self._log(
            Phase.DAWN,
            "night_resolution",
            "夜间行动结算完成。",
            public_payload={"visible_to_players": False},
            private_payload={
                "wolf_target": wolf_target,
                "saved_target": saved_target,
                "poisoned_target": poisoned_target,
                "deaths": deaths,
            },
        )
        return deaths

    def _wolf_action(self) -> int | None:
        wolves = [p for p in self._alive_players() if p.role == Role.WEREWOLF]
        if not wolves:
            return None

        legal_target_ids = _wolf_legal_attack_targets(self._alive_players(), wolves)
        proposals: list[dict[str, Any]] = []
        for wolf in wolves:
            obs = self.build_observation(wolf.id, Phase.NIGHT_WOLF, [ActionType.KILL])
            decision = self.agents[wolf.id].decide(obs, self.rng)
            valid_target = decision.target_id in legal_target_ids if decision.target_id is not None else False
            proposal = _wolf_proposal_from_decision(decision, valid_target=valid_target)
            proposals.append(proposal)
            self._log(
                Phase.NIGHT_WOLF,
                "wolf_intent",
                f"{wolf.name} 已提交狼人夜间目标。",
                actor_id=wolf.id,
                target_id=decision.target_id,
                decision=decision,
                observation_proof=obs.redacted_for_log(),
                observation_snapshot=obs,
                public_payload={"visible_to_players": False},
                private_payload={
                    "target_id": decision.target_id,
                    "valid_target": valid_target,
                    "reason": decision.reason,
                    "proposal": proposal,
                },
            )

        council_plan = self._coordinate_wolf_council(wolves, legal_target_ids, proposals)
        self.wolf_pack_plans[self.day] = council_plan
        self._log(
            Phase.NIGHT_WOLF,
            "wolf_council_plan",
            f"狼人夜间会议已形成第 {self.day} 夜刀人共识。",
            target_id=council_plan.get("attack_target"),
            public_payload={"visible_to_players": False},
            private_payload=council_plan,
        )
        return council_plan.get("attack_target")

    def _coordinate_wolf_council(
        self,
        wolves: list[PlayerState],
        legal_target_ids: list[int],
        proposals: list[dict[str, Any]],
    ) -> dict[str, Any]:
        pressure = self._wolf_pressure_snapshot()
        strategy_options = _wolf_strategy_options(pressure)
        fallback_plan = _fallback_wolf_council_plan(
            day=self.day,
            legal_target_ids=legal_target_ids,
            proposals=proposals,
            pressure=pressure,
            strategy_options=strategy_options,
            rng=self.rng,
        )
        coordinator = getattr(self.llm_decider, "coordinate_wolf_council", None)
        if not callable(coordinator) or not legal_target_ids:
            return fallback_plan
        context = {
            "day": self.day,
            "game_rules": self.game_rules,
            "alive_players": [player.public_view(False) for player in self._alive_players()],
            "wolves": [
                {
                    "id": wolf.id,
                    "name": wolf.name,
                    "player_profile": profile_for_player(wolf.id, self.player_profiles),
                }
                for wolf in wolves
            ],
            "legal_attack_targets": legal_target_ids,
            "public_history": self._public_history(),
            "proposals": proposals,
            "pressure": pressure,
            "strategy_options": strategy_options,
        }
        try:
            plan = coordinator(context)
            return _normalize_wolf_council_plan(
                plan,
                fallback_plan=fallback_plan,
                legal_target_ids=legal_target_ids,
                proposals=proposals,
                pressure=pressure,
                strategy_options=strategy_options,
            )
        except Exception as exc:
            fallback_plan["coordination_status"] = "fallback_after_error"
            fallback_plan["coordination_error"] = f"{type(exc).__name__}: {exc}"
            if isinstance(exc, LLMError):
                fallback_plan["coordination_error_type"] = "llm_validation"
            return fallback_plan

    def _refresh_wolf_pack_plan(self, night_deaths: list[int]) -> None:
        wolves = [p for p in self._alive_players() if p.role == Role.WEREWOLF]
        if not wolves:
            return

        pressure = self._wolf_pressure_snapshot()
        strategy_options = _wolf_strategy_options(pressure)
        previous_plan = self.wolf_pack_plans.get(self.day, {})
        plan = {
            **previous_plan,
            "day": self.day,
            "autonomy_mode": True,
            "autonomous_instruction": "这是狼人私有策略空间，不是系统分配任务。每名狼人需要根据公开历史、个人座位风格和当前压力自主选择策略，也可以选择隐藏、观望、分票或弃票。",
            "private_only": True,
            "not_forced": True,
            "instruction": "这是狼队私有策略空间，不是强制动作。公开发言必须伪装成普通好人视角，不得暴露狼队信息。",
            "night_deaths": list(night_deaths),
            "pressure": pressure,
            "strategy_options": strategy_options,
            "selection_requirement": "自主选择一个或组合多个 strategy_options，并在 deception_intent/public_cover_story 中说明选择理由；不要输出系统给你分配了某种任务。",
            "warnings": [
                "避免多名狼人复制同一套发言模板。",
                "早期同票同理由容易被识别为狼队协同。",
                "悍跳、软对跳、切割和分票都必须包装成公开证据推理。",
            ],
        }
        self.wolf_pack_plans[self.day] = plan
        self._log(
            Phase.DAY_SPEECH,
            "wolf_pack_plan",
            f"狼队已生成第 {self.day} 天私有策略空间。",
            public_payload={"visible_to_players": False},
            private_payload=plan,
        )

    def _wolf_pressure_snapshot(self) -> dict[str, Any]:
        wolf_ids = {player.id for player in self.players if player.role == Role.WEREWOLF}
        claims = self._extract_public_claims()
        checked_wolf_ids: list[int] = []
        non_wolf_seer_claim_ids: list[int] = []
        for claim in claims:
            player_id = claim.get("player_id")
            speech = claim.get("speech", "")
            if claim.get("role") == "seer" and player_id and player_id not in wolf_ids:
                non_wolf_seer_claim_ids.append(player_id)
            if claim.get("claim") == "查杀":
                marker = _extract_player_marker(speech)
                if marker in wolf_ids:
                    checked_wolf_ids.append(marker)
        return {
            "checked_wolf_ids": sorted(set(checked_wolf_ids)),
            "non_wolf_seer_claim_ids": sorted(set(non_wolf_seer_claim_ids)),
            "public_claim_count": len(claims),
        }

    def _seer_action(self) -> None:
        seers = [p for p in self._alive_players() if p.role == Role.SEER]
        if not seers:
            return
        seer = seers[0]
        obs = self.build_observation(seer.id, Phase.NIGHT_SEER, [ActionType.INSPECT])
        decision = self.agents[seer.id].decide(obs, self.rng)
        target = decision.target_id
        if target is not None and self._is_alive(target) and target != seer.id:
            result = "werewolf" if self._player(target).role == Role.WEREWOLF else "villagers"
            self.seer_inspections[seer.id][target] = result
        else:
            result = "invalid"

        self._log(
            Phase.NIGHT_SEER,
            "seer_inspect",
            f"{seer.name} 已完成查验。",
            actor_id=seer.id,
            target_id=target,
            decision=decision,
            observation_proof=obs.redacted_for_log(),
            observation_snapshot=obs,
            public_payload={"visible_to_players": False},
            private_payload={"target_id": target, "result": result, "reason": decision.reason},
        )

    def _witch_action(self, attacked_target: int | None) -> tuple[int | None, int | None]:
        witches = [p for p in self._alive_players() if p.role == Role.WITCH]
        if not witches:
            return None, None

        witch = witches[0]
        legal = [ActionType.PASS]
        if self.witch_has_antidote and attacked_target is not None:
            legal.append(ActionType.SAVE)
        if self.witch_has_poison:
            legal.append(ActionType.POISON)

        obs = self.build_observation(
            witch.id,
            Phase.NIGHT_WITCH,
            legal,
            action_context={"attacked_tonight": attacked_target},
        )
        decision = self.agents[witch.id].decide(obs, self.rng)
        saved_target: int | None = None
        poisoned_target: int | None = None

        if decision.action == ActionType.SAVE and self.witch_has_antidote and decision.target_id == attacked_target:
            saved_target = attacked_target
            self.witch_has_antidote = False
        elif (
            decision.action == ActionType.POISON
            and self.witch_has_poison
            and decision.target_id is not None
            and self._is_alive(decision.target_id)
            and decision.target_id != witch.id
        ):
            poisoned_target = decision.target_id
            self.witch_has_poison = False

        self._log(
            Phase.NIGHT_WITCH,
            "witch_action",
            f"{witch.name} 已完成女巫行动。",
            actor_id=witch.id,
            target_id=decision.target_id,
            decision=decision,
            observation_proof=obs.redacted_for_log(),
            observation_snapshot=obs,
            public_payload={"visible_to_players": False},
            private_payload={
                "attacked_target": attacked_target,
                "saved_target": saved_target,
                "poisoned_target": poisoned_target,
                "reason": decision.reason,
            },
        )
        return saved_target, poisoned_target

    def _announce_dawn(self, deaths: list[int]) -> None:
        if deaths:
            names = ", ".join(self._player(pid).name for pid in deaths)
            self._log(
                Phase.DAWN,
                "dawn_deaths",
                f"天亮了，昨夜死亡玩家：{names}。",
                target_id=deaths[0],
                public_payload={"death_ids": deaths, "visible_to_players": True},
            )
        else:
            self._log(
                Phase.DAWN,
                "dawn_peace",
                "天亮了，昨夜平安夜。",
                public_payload={"death_ids": [], "visible_to_players": True},
            )

    def _day_speeches(self) -> None:
        self._phase_status(Phase.DAY_SPEECH, f"第 {self.day} 天：白天发言开始。")
        for player in list(self._alive_players()):
            self._phase_status(Phase.DAY_SPEECH, f"第 {self.day} 天：等待 {player.name} 发言。")
            obs = self.build_observation(player.id, Phase.DAY_SPEECH, [ActionType.SPEAK])
            decision = self.agents[player.id].decide(obs, self.rng)
            speech = decision.speech or "我暂时没有更多信息。"
            self._log(
                Phase.DAY_SPEECH,
                "speech",
                f"{player.name} 发言：{speech}",
                actor_id=player.id,
                decision=decision,
                observation_proof=obs.redacted_for_log(),
                observation_snapshot=obs,
                public_payload={"speech": speech, "visible_to_players": True},
                private_payload={"reason": decision.reason, "confidence": decision.confidence},
            )

    def _day_vote(self) -> None:
        alive_before_vote = list(self._alive_players())
        if len(alive_before_vote) <= 1:
            return

        self._phase_status(Phase.DAY_VOTE, f"第 {self.day} 天：白天投票中，所有存活玩家同步投票，可弃票。")
        vote_inputs: list[tuple[PlayerState, PrivateObservation]] = [
            (player, self.build_observation(player.id, Phase.DAY_VOTE, [ActionType.VOTE, ActionType.PASS]))
            for player in alive_before_vote
            if player.alive
        ]
        decisions_by_player: dict[int, Any] = {}
        max_workers = max(1, min(len(vote_inputs), self.config.player_count))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(self.agents[player.id].decide, obs, None): player.id
                for player, obs in vote_inputs
            }
            for future in as_completed(futures):
                decisions_by_player[futures[future]] = future.result()

        votes: list[tuple[int, int]] = []
        abstentions: list[int] = []
        for player, obs in vote_inputs:
            decision = decisions_by_player[player.id]
            target = decision.target_id
            if decision.action == ActionType.PASS:
                if target is not None:
                    raise RuntimeError(f"{player.name} produced an illegal abstention target: {target}")
                abstentions.append(player.id)
                self._log(
                    Phase.DAY_VOTE,
                    "vote",
                    f"{player.name} 选择弃票。",
                    actor_id=player.id,
                    target_id=None,
                    decision=decision,
                    observation_proof=obs.redacted_for_log(),
                    observation_snapshot=obs,
                    public_payload={"vote_target": None, "abstain": True, "simultaneous": True, "visible_to_players": True},
                    private_payload={"reason": decision.reason, "confidence": decision.confidence, "simultaneous": True},
                )
                continue

            if target is None or target == player.id or not self._is_alive(target):
                raise RuntimeError(f"{player.name} produced an illegal vote target: {target}")
            votes.append((player.id, target))
            self._log(
                Phase.DAY_VOTE,
                "vote",
                f"{player.name} 投票给 P{target}。",
                actor_id=player.id,
                target_id=target,
                decision=decision,
                observation_proof=obs.redacted_for_log(),
                observation_snapshot=obs,
                public_payload={"vote_target": target, "simultaneous": True, "visible_to_players": True},
                private_payload={"reason": decision.reason, "confidence": decision.confidence, "simultaneous": True},
            )

        if not votes:
            self._log(
                Phase.DAY_VOTE,
                "exile",
                "白天投票结束，所有有效票为空，无人出局。",
                public_payload={
                    "exiled_id": None,
                    "votes": [],
                    "abstentions": abstentions,
                    "tie": False,
                    "visible_to_players": True,
                },
                private_payload={"tally": {}, "abstentions": abstentions},
            )
            return
        counts = Counter(target for _, target in votes)
        top_count = max(counts.values())
        top_targets = [target for target, count in counts.items() if count == top_count]
        if len(top_targets) > 1:
            self._tie_pk_revote(
                tied_targets=sorted(top_targets),
                first_votes=votes,
                first_abstentions=abstentions,
                first_tally=dict(counts),
            )
            return
        exiled = self.rng.choice(sorted(top_targets))
        self._kill_player(exiled, Phase.DAY_VOTE, "exile")
        self._log(
            Phase.DAY_VOTE,
            "exile",
            f"白天投票结束，P{exiled} 被放逐。",
            target_id=exiled,
            public_payload={
                "exiled_id": exiled,
                "votes": [{"voter": voter, "target": target} for voter, target in votes],
                "abstentions": abstentions,
                "tie": False,
                "visible_to_players": True,
            },
            private_payload={"tally": dict(counts), "abstentions": abstentions},
        )
        self._maybe_hunter_shot([exiled])

    def _tie_pk_revote(
        self,
        *,
        tied_targets: list[int],
        first_votes: list[tuple[int, int]],
        first_abstentions: list[int],
        first_tally: dict[int, int],
    ) -> None:
        tied_targets = [target for target in tied_targets if self._is_alive(target)]
        if len(tied_targets) <= 1:
            if tied_targets:
                self._exile_player(
                    tied_targets[0],
                    votes=first_votes,
                    abstentions=first_abstentions,
                    revote=False,
                    tie=False,
                    tally=first_tally,
                )
            return

        tied_names = "、".join(f"P{target}" for target in tied_targets)
        self._log(
            Phase.DAY_VOTE,
            "vote_tie",
            f"首轮投票最高票平票：{tied_names} 进入 PK 发言。",
            public_payload={
                "votes": [{"voter": voter, "target": target} for voter, target in first_votes],
                "abstentions": first_abstentions,
                "tie": True,
                "tied_targets": tied_targets,
                "revote": False,
                "visible_to_players": True,
            },
            private_payload={"tally": first_tally, "abstentions": first_abstentions},
        )

        self._phase_status(Phase.DAY_VOTE, f"第 {self.day} 天：首轮平票，进入 PK 发言。")
        for target in tied_targets:
            player = self._player(target)
            self._phase_status(Phase.DAY_VOTE, f"第 {self.day} 天：等待 {player.name} 进行 PK 发言。")
            obs = self.build_observation(
                player.id,
                Phase.DAY_VOTE,
                [ActionType.SPEAK],
                action_context={
                    "tie_pk": {
                        "round": "pk_speech",
                        "tied_targets": tied_targets,
                        "instruction": "你是平票 PK 玩家，请基于公开证据为自己辩护，并回应另一名或多名 PK 对象。",
                    }
                },
            )
            decision = self.agents[player.id].decide(obs, self.rng)
            speech = decision.speech or "我进入 PK，请大家基于公开证据重新判断。"
            self._log(
                Phase.DAY_VOTE,
                "speech",
                f"{player.name} PK 发言：{speech}",
                actor_id=player.id,
                decision=decision,
                observation_proof=obs.redacted_for_log(),
                observation_snapshot=obs,
                public_payload={
                    "speech": speech,
                    "tie_pk": True,
                    "tied_targets": tied_targets,
                    "visible_to_players": True,
                },
                private_payload={"reason": decision.reason, "confidence": decision.confidence, "tie_pk": True},
            )

        revote_voters = [player for player in self._alive_players() if player.id not in tied_targets]
        if not revote_voters:
            self._log_no_exile_after_pk(
                "PK 台下无可投票玩家，本轮无人放逐。",
                first_votes=first_votes,
                first_abstentions=first_abstentions,
                revote_votes=[],
                revote_abstentions=[],
                tied_targets=tied_targets,
                first_tally=first_tally,
                revote_tally={},
                tie=True,
            )
            return

        vote_inputs: list[tuple[PlayerState, PrivateObservation]] = [
            (
                player,
                self.build_observation(
                    player.id,
                    Phase.DAY_VOTE,
                    [ActionType.VOTE, ActionType.PASS],
                    action_context={
                        "tie_pk": {
                            "round": "revote",
                            "allowed_vote_targets": tied_targets,
                            "ineligible_voters": tied_targets,
                            "instruction": "PK 二次投票：你不是 PK 玩家，只能在 allowed_vote_targets 中投票，也可以弃票。",
                        }
                    },
                ),
            )
            for player in revote_voters
        ]
        self._phase_status(Phase.DAY_VOTE, f"第 {self.day} 天：PK 二次投票中，非 PK 玩家同步投票。")
        decisions_by_player: dict[int, Any] = {}
        max_workers = max(1, min(len(vote_inputs), self.config.player_count))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(self.agents[player.id].decide, obs, None): player.id
                for player, obs in vote_inputs
            }
            for future in as_completed(futures):
                decisions_by_player[futures[future]] = future.result()

        revote_votes: list[tuple[int, int]] = []
        revote_abstentions: list[int] = []
        allowed_targets = set(tied_targets)
        for player, obs in vote_inputs:
            decision = decisions_by_player[player.id]
            target = decision.target_id
            if decision.action == ActionType.PASS:
                if target is not None:
                    raise RuntimeError(f"{player.name} produced an illegal PK abstention target: {target}")
                revote_abstentions.append(player.id)
                self._log(
                    Phase.DAY_VOTE,
                    "vote",
                    f"{player.name} 在 PK 二次投票中选择弃票。",
                    actor_id=player.id,
                    target_id=None,
                    decision=decision,
                    observation_proof=obs.redacted_for_log(),
                    observation_snapshot=obs,
                    public_payload={
                        "vote_target": None,
                        "abstain": True,
                        "simultaneous": True,
                        "tie_pk": True,
                        "revote": True,
                        "allowed_vote_targets": tied_targets,
                        "visible_to_players": True,
                    },
                    private_payload={"reason": decision.reason, "confidence": decision.confidence, "simultaneous": True},
                )
                continue

            if target is None or target == player.id or target not in allowed_targets or not self._is_alive(target):
                raise RuntimeError(
                    f"{player.name} produced an illegal PK vote target: {target}; allowed={tied_targets}"
                )
            revote_votes.append((player.id, target))
            self._log(
                Phase.DAY_VOTE,
                "vote",
                f"{player.name} 在 PK 二次投票中投票给 P{target}。",
                actor_id=player.id,
                target_id=target,
                decision=decision,
                observation_proof=obs.redacted_for_log(),
                observation_snapshot=obs,
                public_payload={
                    "vote_target": target,
                    "simultaneous": True,
                    "tie_pk": True,
                    "revote": True,
                    "allowed_vote_targets": tied_targets,
                    "visible_to_players": True,
                },
                private_payload={"reason": decision.reason, "confidence": decision.confidence, "simultaneous": True},
            )

        if not revote_votes:
            self._log_no_exile_after_pk(
                "PK 二次投票无人有效投票，本轮无人放逐。",
                first_votes=first_votes,
                first_abstentions=first_abstentions,
                revote_votes=[],
                revote_abstentions=revote_abstentions,
                tied_targets=tied_targets,
                first_tally=first_tally,
                revote_tally={},
                tie=False,
            )
            return

        revote_counts = Counter(target for _, target in revote_votes)
        top_count = max(revote_counts.values())
        revote_top_targets = [target for target, count in revote_counts.items() if count == top_count]
        if len(revote_top_targets) > 1:
            self._log_no_exile_after_pk(
                "PK 二次投票仍然平票，本轮无人放逐。",
                first_votes=first_votes,
                first_abstentions=first_abstentions,
                revote_votes=revote_votes,
                revote_abstentions=revote_abstentions,
                tied_targets=sorted(revote_top_targets),
                first_tally=first_tally,
                revote_tally=dict(revote_counts),
                tie=True,
            )
            return

        exiled = revote_top_targets[0]
        self._exile_player(
            exiled,
            votes=revote_votes,
            abstentions=revote_abstentions,
            revote=True,
            tie=False,
            tally=dict(revote_counts),
            first_votes=first_votes,
            first_abstentions=first_abstentions,
            first_tally=first_tally,
            tied_targets=tied_targets,
        )

    def _log_no_exile_after_pk(
        self,
        public_text: str,
        *,
        first_votes: list[tuple[int, int]],
        first_abstentions: list[int],
        revote_votes: list[tuple[int, int]],
        revote_abstentions: list[int],
        tied_targets: list[int],
        first_tally: dict[int, int],
        revote_tally: dict[int, int],
        tie: bool,
    ) -> None:
        self._log(
            Phase.DAY_VOTE,
            "exile",
            public_text,
            public_payload={
                "exiled_id": None,
                "votes": [{"voter": voter, "target": target} for voter, target in revote_votes],
                "abstentions": revote_abstentions,
                "tie": tie,
                "tied_targets": tied_targets,
                "revote": True,
                "first_votes": [{"voter": voter, "target": target} for voter, target in first_votes],
                "first_abstentions": first_abstentions,
                "visible_to_players": True,
            },
            private_payload={
                "tally": revote_tally,
                "first_tally": first_tally,
                "abstentions": revote_abstentions,
                "first_abstentions": first_abstentions,
            },
        )

    def _exile_player(
        self,
        exiled: int,
        *,
        votes: list[tuple[int, int]],
        abstentions: list[int],
        revote: bool,
        tie: bool,
        tally: dict[int, int],
        first_votes: list[tuple[int, int]] | None = None,
        first_abstentions: list[int] | None = None,
        first_tally: dict[int, int] | None = None,
        tied_targets: list[int] | None = None,
    ) -> None:
        self._kill_player(exiled, Phase.DAY_VOTE, "exile")
        public_payload = {
            "exiled_id": exiled,
            "votes": [{"voter": voter, "target": target} for voter, target in votes],
            "abstentions": abstentions,
            "tie": tie,
            "revote": revote,
            "visible_to_players": True,
        }
        private_payload = {"tally": tally, "abstentions": abstentions}
        if revote:
            public_payload.update(
                {
                    "tied_targets": tied_targets or [],
                    "first_votes": [
                        {"voter": voter, "target": target} for voter, target in (first_votes or [])
                    ],
                    "first_abstentions": first_abstentions or [],
                }
            )
            private_payload.update({"first_tally": first_tally or {}, "first_abstentions": first_abstentions or []})
        self._log(
            Phase.DAY_VOTE,
            "exile",
            f"白天投票结束，P{exiled} 被放逐。" if not revote else f"PK 二次投票结束，P{exiled} 被放逐。",
            target_id=exiled,
            public_payload=public_payload,
            private_payload=private_payload,
        )
        self._maybe_hunter_shot([exiled])

    def _maybe_hunter_shot(self, dead_ids: list[int]) -> None:
        if self.hunter_has_shot:
            return
        hunters = [self._player(pid) for pid in dead_ids if self._player(pid).role == Role.HUNTER]
        for hunter in hunters:
            if hunter.death_reason == "poison" and not self.config.allow_hunter_shot_after_poison:
                self._log(
                    Phase.HUNTER_SHOT,
                    "hunter_pass",
                    f"{hunter.name} 死于毒药，不能发动技能。",
                    actor_id=hunter.id,
                    public_payload={
                        "visible_to_players": True,
                        "blocked_by_poison": True,
                        "role_revealed": True,
                        "revealed_role": "hunter",
                        "revealed_role_zh": "猎人",
                    },
                )
                return
            if not self._alive_players():
                return
            self._phase_status(Phase.HUNTER_SHOT, f"第 {self.day} 天：猎人技能结算中。")
            obs = self.build_observation(hunter.id, Phase.HUNTER_SHOT, [ActionType.SHOOT, ActionType.PASS])
            decision = self.agents[hunter.id].decide(obs, self.rng)
            if decision.action == ActionType.SHOOT and decision.target_id is not None and self._is_alive(decision.target_id):
                target = decision.target_id
                self.hunter_has_shot = True
                self._kill_player(target, Phase.HUNTER_SHOT, "hunter_shot")
                self._log(
                    Phase.HUNTER_SHOT,
                    "hunter_shot",
                    f"{hunter.name} 发动猎人技能，带走 P{target}。",
                    actor_id=hunter.id,
                    target_id=target,
                    decision=decision,
                    observation_proof=obs.redacted_for_log(),
                    observation_snapshot=obs,
                    public_payload={
                        "shot_target": target,
                        "visible_to_players": True,
                        "role_revealed": True,
                        "revealed_role": "hunter",
                        "revealed_role_zh": "猎人",
                    },
                    private_payload={"reason": decision.reason, "confidence": decision.confidence},
                )
            else:
                self.hunter_has_shot = True
                self._log(
                    Phase.HUNTER_SHOT,
                    "hunter_pass",
                    f"{hunter.name} 没有开枪。",
                    actor_id=hunter.id,
                    decision=decision,
                    observation_proof=obs.redacted_for_log(),
                    observation_snapshot=obs,
                    public_payload={
                        "visible_to_players": True,
                        "role_revealed": True,
                        "revealed_role": "hunter",
                        "revealed_role_zh": "猎人",
                    },
                    private_payload={"reason": decision.reason if decision else ""},
                )

    def _check_win(self) -> bool:
        alive = self._alive_players()
        wolves = [p for p in alive if p.role == Role.WEREWOLF]
        civilians = [p for p in alive if p.role == Role.VILLAGER]
        gods = [p for p in alive if p.role in {Role.SEER, Role.WITCH, Role.HUNTER}]
        if not wolves:
            self.winner = Faction.VILLAGERS
            self.win_reason = "狼人全部出局。"
            return True
        if not civilians:
            self.winner = Faction.WEREWOLVES
            self.win_reason = "三名平民全部阵亡，狼人完成屠民。"
            return True
        if not gods:
            self.winner = Faction.WEREWOLVES
            self.win_reason = "预言家、女巫、猎人全部阵亡，狼人完成屠神。"
            return True
        return False

    def _force_timeout_result(self) -> None:
        alive = self._alive_players()
        wolves = len([p for p in alive if p.role == Role.WEREWOLF])
        villagers = len(alive) - wolves
        if wolves >= villagers:
            self.winner = Faction.WEREWOLVES
            self.win_reason = "达到最大天数后狼人存活优势。"
        else:
            self.winner = Faction.VILLAGERS
            self.win_reason = "达到最大天数后好人存活优势。"

    def _extract_public_claims(self) -> list[dict[str, Any]]:
        claims: list[dict[str, Any]] = []
        for event in self.events:
            if event.event_type != "speech":
                continue
            speech = event.public_payload.get("speech", "")
            compact = re.sub(r"\s+", "", speech)
            for role_name, role_value in (("预言家", "seer"), ("女巫", "witch"), ("猎人", "hunter")):
                claim_patterns = (
                    f"我跳{role_name}",
                    f"我是{role_name}",
                    f"我为{role_name}",
                    f"我认{role_name}",
                    f"我拍{role_name}",
                    f"我有{role_name}视角",
                    f"我{role_name}",
                )
                if any(pattern in compact for pattern in claim_patterns):
                    claims.append(
                        {
                            "event_id": event.id,
                            "player_id": event.actor_id,
                            "claim_type": "role_claim",
                            "role": role_value,
                            "role_zh": role_name,
                            "speech": speech,
                            "day": event.day,
                        }
                    )
            if _has_positive_claim_term(compact, "查杀"):
                claims.append(
                    {
                        "event_id": event.id,
                        "player_id": event.actor_id,
                        "claim_type": "check_claim",
                        "claim": "查杀",
                        "speech": speech,
                        "day": event.day,
                    }
                )
            if _has_positive_claim_term(compact, "金水"):
                claims.append(
                    {
                        "event_id": event.id,
                        "player_id": event.actor_id,
                        "claim_type": "check_claim",
                        "claim": "金水",
                        "speech": speech,
                        "day": event.day,
                    }
                )
        return claims

    def _extract_public_role_assertions(self) -> list[dict[str, Any]]:
        assertions: list[dict[str, Any]] = []
        for event in self.events:
            if event.event_type != "speech":
                continue
            speech = event.public_payload.get("speech", "")
            for match in re.finditer(r"P(\d+).{0,10}?(可能是|应该是|疑似|像|是)(预言家|女巫|猎人)", speech):
                marker = match.group(2)
                role_name = match.group(3)
                assertions.append(
                    {
                        "event_id": event.id,
                        "player_id": event.actor_id,
                        "assertion_type": "hard_assertion" if marker == "是" else "suspicion",
                        "target_id": int(match.group(1)),
                        "role": {"预言家": "seer", "女巫": "witch", "猎人": "hunter"}[role_name],
                        "role_zh": role_name,
                        "marker": marker,
                        "speech": speech,
                        "day": event.day,
                    }
                )
        return assertions

    def _extract_public_role_mentions(self) -> list[dict[str, Any]]:
        mentions: list[dict[str, Any]] = []
        for event in self.events:
            if event.event_type != "speech":
                continue
            speech = event.public_payload.get("speech", "")
            for role_name in ("预言家", "女巫", "猎人"):
                if role_name in speech:
                    mentions.append(
                        {
                            "event_id": event.id,
                            "player_id": event.actor_id,
                            "role_zh": role_name,
                            "speech": speech,
                            "day": event.day,
                            "note": "role_word_mention; not evidence by itself",
                        }
                    )
        return mentions

    def _effective_backend(self) -> str:
        provider = self.llm_decider.chat_client.config.provider
        model = self.llm_decider.chat_client.config.model
        return f"llm:{provider}:{model}"

    def _public_history(self) -> list[dict[str, Any]]:
        return [event.public_view() for event in self.events if event.event_type in VISIBLE_EVENT_TYPES]

    def _phase_status(self, phase: Phase, text: str) -> None:
        self._log(
            phase,
            "phase_status",
            text,
            public_payload={"visible_to_players": True, "status_only": True},
        )

    def _kill_player(self, player_id: int, phase: Phase, reason: str) -> None:
        player = self._player(player_id)
        if not player.alive:
            return
        player.alive = False
        player.death_day = self.day
        player.death_phase = phase
        player.death_reason = reason

    def _alive_players(self) -> list[PlayerState]:
        return [player for player in self.players if player.alive]

    def _is_alive(self, player_id: int) -> bool:
        return self._player(player_id).alive

    def _player(self, player_id: int) -> PlayerState:
        for player in self.players:
            if player.id == player_id:
                return player
        raise KeyError(f"Unknown player id: {player_id}")

    def _log(
        self,
        phase: Phase,
        event_type: str,
        public_text: str,
        *,
        actor_id: int | None = None,
        target_id: int | None = None,
        secondary_target_id: int | None = None,
        public_payload: dict[str, Any] | None = None,
        private_payload: dict[str, Any] | None = None,
        decision: Any = None,
        observation_proof: dict[str, Any] | None = None,
        observation_snapshot: Any = None,
    ) -> GameEvent:
        if decision is not None:
            self._annotate_skill_usage(decision, phase)
        event = GameEvent(
            id=len(self.events) + 1,
            day=self.day,
            phase=phase,
            event_type=event_type,
            public_text=public_text,
            actor_id=actor_id,
            target_id=target_id,
            secondary_target_id=secondary_target_id,
            public_payload=public_payload or {},
            private_payload=private_payload or {},
            decision=decision,
            observation_proof=observation_proof,
            observation_snapshot=observation_snapshot,
        )
        self.events.append(event)
        update_belief_states_after_event(self.belief_states, event, self.players)
        if self.event_callback is not None:
            self.event_callback(event, self)
        return event

    def _annotate_skill_usage(self, decision: Any, phase: Phase) -> None:
        role = getattr(decision, "role", None)
        if not isinstance(role, Role):
            return
        profile = self.profiles.get(role)
        if not profile:
            return
        role_skills = profile.parameters.get("role_skills", [])
        prompt_skill_ids = set((decision.metadata or {}).get("prompt_skill_ids") or [])
        if prompt_skill_ids:
            role_skills = [
                skill
                for skill in role_skills
                if isinstance(skill, dict) and str(skill.get("skill_id") or "") in prompt_skill_ids
            ]
        usage = estimate_skill_usage(
            decision,
            phase=phase,
            role_skills=role_skills,
        )
        if usage.get("available_count"):
            decision.metadata["skill_usage"] = usage


def _wolf_legal_attack_targets(alive_players: list[PlayerState], wolves: list[PlayerState]) -> list[int]:
    wolf_ids = {wolf.id for wolf in wolves}
    return sorted(player.id for player in alive_players if player.id not in wolf_ids)


def _wolf_proposal_from_decision(decision: Any, *, valid_target: bool) -> dict[str, Any]:
    raw = (getattr(decision, "metadata", {}) or {}).get("raw_decision", {})
    return {
        "wolf_id": decision.actor_id,
        "target_id": decision.target_id,
        "valid_target": valid_target,
        "reason": str(decision.reason or "")[:360],
        "confidence": decision.confidence,
        "day_strategy_suggestion": str(
            raw.get("day_strategy_suggestion")
            or raw.get("day_plan")
            or raw.get("public_cover_story")
            or ""
        )[:360],
        "risk_control": str(raw.get("risk_control") or raw.get("risk") or "")[:240],
        "preferred_deception_intent": str(raw.get("preferred_deception_intent") or raw.get("deception_intent") or "")[:80],
    }


def _fallback_wolf_council_plan(
    *,
    day: int,
    legal_target_ids: list[int],
    proposals: list[dict[str, Any]],
    pressure: dict[str, Any],
    strategy_options: list[dict[str, Any]],
    rng: random.Random,
) -> dict[str, Any]:
    valid_votes = [
        int(proposal["target_id"])
        for proposal in proposals
        if proposal.get("valid_target") and proposal.get("target_id") in legal_target_ids
    ]
    attack_target: int | None = None
    if valid_votes:
        counts = Counter(valid_votes)
        top_count = max(counts.values())
        top_targets = [target for target, count in counts.items() if count == top_count]
        attack_target = rng.choice(sorted(top_targets))
    elif legal_target_ids:
        attack_target = rng.choice(sorted(legal_target_ids))
    return {
        "day": day,
        "coordination_mode": "rules_fallback_after_wolf_proposals",
        "coordination_status": "fallback_rules",
        "private_only": True,
        "not_forced": True,
        "attack_target": attack_target,
        "legal_attack_targets": list(legal_target_ids),
        "individual_proposals": proposals,
        "proposal_summary": [
            {
                "wolf_id": proposal.get("wolf_id"),
                "preferred_target": proposal.get("target_id"),
                "accepted": proposal.get("target_id") == attack_target,
                "note": proposal.get("reason", ""),
            }
            for proposal in proposals
        ],
        "consensus_reason": "按狼人个人夜间提案的多数票/合法目标兜底形成刀人目标。",
        "day_plan": {
            "overall": "白天继续由每名狼人自主选择公开伪装路线，避免同模板、同票、同理由暴露协同。",
            "optional_routes": [
                "可围绕公开发言和票型制造合理分歧。",
                "可选择隐藏、软对跳、倒钩、分票或暂不主动欺骗。",
            ],
            "risk_controls": [
                "公开发言不得提到夜间会议、夜刀目标或狼队友身份。",
                "不要把狼队私有判断包装成公开事实。",
            ],
        },
        "constraints": [
            "白天计划是可选策略，不是对具体狼人的强制命令。",
            "公开理由只能来自 public_history，不得暴露狼队私有信息。",
        ],
        "pressure": pressure,
        "strategy_options": strategy_options,
        "confidence": 0.5,
    }


def _normalize_wolf_council_plan(
    plan: dict[str, Any],
    *,
    fallback_plan: dict[str, Any],
    legal_target_ids: list[int],
    proposals: list[dict[str, Any]],
    pressure: dict[str, Any],
    strategy_options: list[dict[str, Any]],
) -> dict[str, Any]:
    normalized = {**fallback_plan, **dict(plan or {})}
    attack_target = normalized.get("attack_target")
    if attack_target not in legal_target_ids:
        normalized = {**fallback_plan}
        normalized["coordination_status"] = "fallback_illegal_llm_target"
    normalized["day"] = fallback_plan["day"]
    normalized["private_only"] = True
    normalized["not_forced"] = True
    normalized["legal_attack_targets"] = list(legal_target_ids)
    normalized["individual_proposals"] = proposals
    normalized["pressure"] = pressure
    normalized["strategy_options"] = strategy_options
    normalized.setdefault("constraints", fallback_plan["constraints"])
    normalized.setdefault("day_plan", fallback_plan["day_plan"])
    return normalized


def _wolf_strategy_options(pressure: dict[str, Any]) -> list[dict[str, Any]]:
    checked_wolves = pressure.get("checked_wolf_ids") or []
    seer_claims = pressure.get("non_wolf_seer_claim_ids") or []
    return [
        {
            "id": "hide",
            "name_zh": "低调隐藏",
            "when": "公开证据不足、自己关注度低、强行带节奏会显得突兀时。",
            "can_do": "复述公开事实，给低到中等强度怀疑，保留调整空间。",
            "risk": "如果全体狼人都只隐藏，会丧失扰乱局势的能力。",
        },
        {
            "id": "soft_counter",
            "name_zh": "软对跳/质疑查验链",
            "when": "场上只有单预言家、查验链存在可质疑点，或需要削弱好人核心话语权时。",
            "can_do": "质疑起跳时机、查验顺序、投票收益或信息传递质量。",
            "risk": "必须引用公开事实，不能编造不存在的查验、发言或夜间信息。",
            "pressure_hint": {"non_wolf_seer_claim_ids": seer_claims},
        },
        {
            "id": "fake_seer",
            "name_zh": "悍跳预言家",
            "when": "需要强行制造双预言家格局，且自己能给出完整查验链和后续计划时。",
            "can_do": "声明昨夜查验对象、查验结果、后续查验计划，并解释为什么公开事件支持这套叙事。",
            "risk": "高风险。查验链必须自洽，且后续每轮都要维护这条叙事。",
        },
        {
            "id": "bus_teammate",
            "name_zh": "倒钩/轻踩队友",
            "when": "队友被公开查杀或狼面过高，继续保护会暴露狼队协同时。",
            "can_do": "基于公开发言、票型或死亡信息轻踩、切割，换取个人可信度。",
            "risk": "不能暴露你知道对方是狼人；理由只能来自公开信息。",
            "pressure_hint": {"checked_wolf_ids": checked_wolves},
        },
        {
            "id": "split_vote",
            "name_zh": "分票/弃票制造非协同票型",
            "when": "多名狼人投同一目标会形成明显团队票，或公开证据不足以强投时。",
            "can_do": "选择另一个公开可疑目标，或在证据不足时弃票并说明保守理由。",
            "risk": "过度分散可能导致狼队无法影响放逐结果。",
        },
        {
            "id": "pressure_good",
            "name_zh": "抗推好人",
            "when": "有好人存在公开逻辑漏洞、票型异常、发言前后矛盾或身份叙事不稳时。",
            "can_do": "围绕公开证据施压，尝试制造合理分歧或推动归票。",
            "risk": "如果证据很弱，强推会暴露带节奏意图。",
        },
        {
            "id": "none",
            "name_zh": "暂不主动欺骗",
            "when": "局势不清、公开证据太少、主动动作收益低时。",
            "can_do": "承认信息不足，等待更多发言和票型，同时维持个人风格。",
            "risk": "持续无动作会被评价为划水或缺少阵营贡献。",
        },
    ]


def _wolf_strategy_space_for_player(pack_plan: dict[str, Any], player_id: int) -> dict[str, Any]:
    return {
        "mode": "autonomous_choice",
        "self_id": player_id,
        "available_options": pack_plan.get("strategy_options", []),
        "pressure": pack_plan.get("pressure", {}),
        "constraints": [
            "你可以策略性不诚实，但只能扭曲公开事实的解释，不能编造不存在的事件。",
            "公开发言和投票理由不能提到 wolf_strategy_space、pack_plan、狼队友身份或夜间刀人信息。",
            "你可以选择不欺骗、隐藏、观望、分票或弃票；如果选择悍跳/对跳，必须维护完整且自洽的公开叙事。",
            "不要因为系统提供了某个选项就一定使用它；选择应当服务于当前局势和个人座位风格。",
        ],
        "decision_prompt": "请自主选择 deception_intent。可选 hide、soft_counter、fake_seer、bus_teammate、split_vote、pressure_good、none，也可以组合，但必须能被 public_history 支撑。",
    }


def _has_positive_claim_term(compact_speech: str, term: str) -> bool:
    for match in re.finditer(re.escape(term), compact_speech):
        prefix = compact_speech[max(0, match.start() - 10) : match.start()]
        if any(token in prefix for token in ("没有", "没", "无", "未", "不是", "并非", "非", "暂无", "还没")):
            continue
        return True
    return False


def _extract_player_marker(text: str) -> int | None:
    match = re.search(r"P(\d+)", text)
    return int(match.group(1)) if match else None


def run_game(
    *,
    seed: int | None = None,
    version_id: str = "initial",
    profiles: dict[Role, StrategyVersion] | None = None,
    config: GameConfig | None = None,
    llm_provider: str = "ark",
    llm_model: str | None = None,
    llm_decider: LLMDecisionClient | None = None,
    memory_bank_by_role: dict[Role, list[str]] | None = None,
    memory_retrieval_top_k: int = 5,
    event_callback: Callable[[GameEvent, GameEngine], None] | None = None,
) -> GameResult:
    engine = GameEngine(
        config=config,
        seed=seed,
        profiles=profiles,
        version_id=version_id,
        llm_provider=llm_provider,
        llm_model=llm_model,
        llm_decider=llm_decider,
        memory_bank_by_role=memory_bank_by_role,
        memory_retrieval_top_k=memory_retrieval_top_k,
        event_callback=event_callback,
    )
    return engine.run()
