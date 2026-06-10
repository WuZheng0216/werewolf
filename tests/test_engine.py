import json
import os
import unittest
from types import SimpleNamespace

from werewolf_ai.agents import default_strategy_versions
from werewolf_ai.engine import GameEngine, run_game
from werewolf_ai.evaluator import evaluate_game
from werewolf_ai.evolution import EvolutionManager, _version_round_number, promotion_decision
from werewolf_ai.models import (
    ActionType,
    AgentDecision,
    Faction,
    GameEvent,
    GameResult,
    Phase,
    PlayerState,
    Role,
    role_faction,
)


class GameEngineTests(unittest.TestCase):
    def test_fifty_games_complete(self) -> None:
        for seed in range(50):
            result = run_game(seed=seed + 1000, llm_decider=ScriptedLLMDecider())
            self.assertIn(result.winner, {Faction.VILLAGERS, Faction.WEREWOLVES})
            self.assertLessEqual(result.day, 8)
            self.assertEqual(result.events[-1].event_type, "game_over")
            self.assertEqual(len(result.players), 9)
            self.assertEqual(sum(1 for player in result.players if player.role == Role.WEREWOLF), 3)

    def test_information_isolation(self) -> None:
        engine = GameEngine(seed=42, llm_decider=ScriptedLLMDecider())
        result = engine.run()
        self.assertGreater(len(result.observation_samples), 0)

        forbidden_public_tokens = {"wolf_target", "poisoned_target", "saved_target", "result"}
        for observation in engine.debug_observations:
            public_blob = json.dumps(observation.public_history, ensure_ascii=False)
            for token in forbidden_public_tokens:
                self.assertNotIn(token, public_blob)

            keys = set(observation.private_knowledge.keys())
            if observation.self_role == Role.VILLAGER:
                self.assertNotIn("pack_ids", keys)
                self.assertNotIn("inspections", keys)
                self.assertNotIn("attacked_tonight", keys)
            if observation.self_role == Role.WEREWOLF:
                self.assertIn("pack_ids", keys)
                self.assertNotIn("inspections", keys)
                self.assertNotIn("attacked_tonight", keys)
            if observation.self_role == Role.SEER:
                self.assertNotIn("pack_ids", keys)

    def test_roles_have_distinct_prompts_and_memory(self) -> None:
        profiles = default_strategy_versions("initial")
        prompt_names = {profile.prompt_name for profile in profiles.values()}
        self.assertEqual(len(prompt_names), len(profiles))
        for role in Role:
            self.assertIn(role, profiles)
            self.assertGreater(len(profiles[role].strategy_memory), 0)
            self.assertGreater(len(profiles[role].parameters), 0)

    def test_observation_includes_seat_persona(self) -> None:
        engine = GameEngine(seed=42, llm_decider=ScriptedLLMDecider())
        engine.day = 1
        observation = engine.build_observation(1, Phase.DAY_SPEECH, [ActionType.SPEAK])
        self.assertEqual(observation.player_profile["seat_id"], 1)
        self.assertIn("persona", observation.player_profile)
        self.assertIn("vote_style", observation.player_profile)
        self.assertEqual(observation.game_rules["ruleset_name"], "9人标准局-无警长版")
        self.assertIn("本局没有警徽", " ".join(observation.game_rules["disabled_mechanics"]))
        self.assertIn("阵营结果", observation.game_rules["night_rules"]["seer"])
        self.assertIn("被报好人/金水都不冲突", observation.game_rules["night_rules"]["seer"])
        self.assertIn("不能把好人阵营结果说成", observation.game_rules["night_rules"]["seer"])
        self.assertIn("seer_check_result", observation.game_rules["terminology"])
        self.assertIn("self_identity_counterclaim", observation.game_rules["terminology"])

    def test_negated_check_terms_do_not_create_false_claims(self) -> None:
        engine = GameEngine(seed=42, llm_decider=ScriptedLLMDecider())
        engine.day = 1
        speech = (
            "P6目前是唯一起跳预言家的玩家，报了P1金水，也说后续要验P5。"
            "现在还没有查杀，等后面有人对跳再比较。"
        )
        engine._log(
            Phase.DAY_SPEECH,
            "speech",
            f"P9 发言：{speech}",
            actor_id=9,
            public_payload={"speech": speech, "visible_to_players": True},
        )

        public_claims = engine._extract_public_claims()
        self.assertTrue(any(claim.get("claim") == "金水" for claim in public_claims))
        self.assertFalse(any(claim.get("claim") == "查杀" for claim in public_claims))

        observation = engine.build_observation(1, Phase.DAY_SPEECH, [ActionType.SPEAK])
        belief_claims = observation.belief_state["claims"]
        self.assertTrue(
            any(claim.get("target_id") == 1 and claim.get("claim_result") == "villagers" for claim in belief_claims)
        )
        self.assertFalse(
            any(claim.get("target_id") == 5 and claim.get("claim_result") == "werewolf" for claim in belief_claims)
        )

    def test_observation_includes_continuous_belief_state(self) -> None:
        engine = GameEngine(seed=42, llm_decider=ScriptedLLMDecider())
        engine.day = 1
        observation = engine.build_observation(1, Phase.DAY_SPEECH, [ActionType.SPEAK])
        self.assertEqual(observation.belief_state["player_id"], 1)
        self.assertIn("summary", observation.belief_state)
        self.assertIn("top_suspicions", observation.belief_state)
        self.assertIn("usage_guidance", observation.belief_state)
        proof = observation.redacted_for_log()
        self.assertIn("belief_state", proof)
        self.assertIn("claim_count", proof["belief_state"])

    def test_wolf_pack_plan_is_private_to_wolves(self) -> None:
        engine = GameEngine(seed=43, llm_decider=ScriptedLLMDecider())
        engine.day = 1
        engine._refresh_wolf_pack_plan([])
        wolf = next(player for player in engine.players if player.role == Role.WEREWOLF)
        non_wolf = next(player for player in engine.players if player.role != Role.WEREWOLF)

        wolf_observation = engine.build_observation(wolf.id, Phase.DAY_SPEECH, [ActionType.SPEAK])
        good_observation = engine.build_observation(non_wolf.id, Phase.DAY_SPEECH, [ActionType.SPEAK])

        self.assertIn("pack_plan", wolf_observation.private_knowledge)
        self.assertNotIn("pack_plan", good_observation.private_knowledge)
        plan = wolf_observation.private_knowledge["pack_plan"]
        self.assertTrue(plan["private_only"])
        self.assertTrue(plan["not_forced"])
        self.assertTrue(plan["autonomy_mode"])
        self.assertGreaterEqual(len(plan["strategy_options"]), 1)
        self.assertIn("wolf_strategy_space", wolf_observation.private_knowledge)
        self.assertNotIn("wolf_self_assignment", wolf_observation.private_knowledge)
        self.assertEqual(engine.events[-1].event_type, "wolf_pack_plan")
        self.assertFalse(engine.events[-1].public_payload["visible_to_players"])

        self.assertIn("known_teammates", wolf_observation.belief_state["private_notes"])
        self.assertNotIn("known_teammates", good_observation.belief_state["private_notes"])

    def test_wolf_night_council_uses_llm_coordinator_target(self) -> None:
        decider = CoordinatedWolfCouncilDecider()
        engine = GameEngine(seed=43, llm_decider=decider)
        engine.day = 1

        target = engine._wolf_action()

        self.assertEqual(target, decider.context["legal_attack_targets"][-1])
        council_events = [event for event in engine.events if event.event_type == "wolf_council_plan"]
        self.assertEqual(len(council_events), 1)
        payload = council_events[0].private_payload
        self.assertEqual(payload["coordination_status"], "llm_ok")
        self.assertEqual(payload["coordination_mode"], "llm_wolf_council")
        self.assertGreaterEqual(len(payload["individual_proposals"]), 1)
        self.assertFalse(council_events[0].public_payload["visible_to_players"])

    def test_belief_state_tracks_public_claims(self) -> None:
        engine = GameEngine(seed=44, llm_decider=ScriptedLLMDecider())
        engine.day = 1
        engine._log(
            Phase.DAY_SPEECH,
            "speech",
            "P2 发言：我是预言家，昨夜查验 P3 是金水。",
            actor_id=2,
            public_payload={"speech": "我是预言家，昨夜查验 P3 是金水。", "visible_to_players": True},
        )

        observation = engine.build_observation(3, Phase.DAY_SPEECH, [ActionType.SPEAK])
        claims = observation.belief_state["claims"]
        self.assertTrue(any(claim.get("player_id") == 2 and claim.get("claim_role") == "seer" for claim in claims))
        seer_hypotheses = observation.belief_state["role_hypotheses"]["seer"]
        self.assertTrue(any(item.get("player_id") == 2 for item in seer_hypotheses))
        self.assertGreater(engine.belief_states[3]["suspicions"]["3"]["trust_score"], 0.5)

    def test_private_seer_belief_does_not_leak_to_others(self) -> None:
        engine = GameEngine(seed=45, llm_decider=ScriptedLLMDecider())
        engine.day = 1
        seer = next(player for player in engine.players if player.role == Role.SEER)
        other = next(player for player in engine.players if player.id != seer.id and player.role != Role.SEER)
        target = next(player for player in engine.players if player.id not in {seer.id, other.id})
        engine._log(
            Phase.NIGHT_SEER,
            "seer_inspect",
            f"{seer.name} 已完成查验。",
            actor_id=seer.id,
            target_id=target.id,
            public_payload={"visible_to_players": False},
            private_payload={"target_id": target.id, "result": "werewolf", "reason": "test"},
        )

        seer_observation = engine.build_observation(seer.id, Phase.DAY_SPEECH, [ActionType.SPEAK])
        other_observation = engine.build_observation(other.id, Phase.DAY_SPEECH, [ActionType.SPEAK])
        self.assertTrue(seer_observation.belief_state["private_notes"]["inspections"])
        self.assertNotIn("inspections", other_observation.belief_state["private_notes"])

    def test_werewolves_win_by_slaughtering_civilians_only(self) -> None:
        engine = GameEngine(seed=7, llm_decider=ScriptedLLMDecider())
        _set_alive_by_role(engine, {Role.WEREWOLF: 3, Role.VILLAGER: 0, Role.SEER: 1, Role.WITCH: 1, Role.HUNTER: 1})
        self.assertTrue(engine._check_win())
        self.assertEqual(engine.winner, Faction.WEREWOLVES)
        self.assertIn("屠民", engine.win_reason)

    def test_werewolves_win_by_slaughtering_gods_only(self) -> None:
        engine = GameEngine(seed=8, llm_decider=ScriptedLLMDecider())
        _set_alive_by_role(engine, {Role.WEREWOLF: 3, Role.VILLAGER: 3, Role.SEER: 0, Role.WITCH: 0, Role.HUNTER: 0})
        self.assertTrue(engine._check_win())
        self.assertEqual(engine.winner, Faction.WEREWOLVES)
        self.assertIn("屠神", engine.win_reason)

    def test_three_good_deaths_is_not_automatic_slaughter(self) -> None:
        engine = GameEngine(seed=9, llm_decider=ScriptedLLMDecider())
        _set_alive_by_role(engine, {Role.WEREWOLF: 3, Role.VILLAGER: 1, Role.SEER: 1, Role.WITCH: 1, Role.HUNTER: 0})
        self.assertFalse(engine._check_win())
        self.assertIsNone(engine.winner)

    def test_event_callback_streams_each_logged_event(self) -> None:
        streamed = []
        engine = GameEngine(
            seed=10,
            llm_decider=ScriptedLLMDecider(),
            event_callback=lambda event, _engine: streamed.append(event.event_type),
        )
        result = engine.run()
        self.assertEqual(len(streamed), len(result.events))
        self.assertEqual(streamed[0], "setup")
        self.assertEqual(streamed[-1], "game_over")

    def test_vote_observations_do_not_include_same_round_votes(self) -> None:
        engine = GameEngine(seed=14, llm_decider=ScriptedLLMDecider())
        result = engine.run()
        vote_events = [event for event in result.events if event.event_type == "vote"]
        self.assertGreater(len(vote_events), 0)
        for event in vote_events:
            observation = event.observation_snapshot
            self.assertIsNotNone(observation)
            same_round_votes = [
                item
                for item in observation.public_history
                if item.get("event_type") == "vote" and item.get("day") == event.day
            ]
            self.assertEqual(same_round_votes, [])
            self.assertTrue(event.public_payload.get("simultaneous"))

    def test_all_abstain_causes_no_exile(self) -> None:
        engine = GameEngine(seed=15, llm_decider=ScriptedLLMDecider(abstain_all_votes=True))
        engine.day = 1
        alive_before = {player.id for player in engine._alive_players()}
        engine._day_vote()
        self.assertEqual({player.id for player in engine._alive_players()}, alive_before)
        vote_events = [event for event in engine.events if event.event_type == "vote"]
        self.assertEqual(len(vote_events), len(alive_before))
        self.assertTrue(all(event.public_payload.get("abstain") for event in vote_events))
        exile = engine.events[-1]
        self.assertEqual(exile.event_type, "exile")
        self.assertIsNone(exile.public_payload.get("exiled_id"))
        self.assertEqual(set(exile.public_payload.get("abstentions", [])), alive_before)

    def test_abstentions_do_not_count_in_vote_tally(self) -> None:
        engine = GameEngine(seed=16, llm_decider=ScriptedLLMDecider(abstain_voters={1, 2, 3}))
        engine.day = 1
        engine._day_vote()
        exile = engine.events[-1]
        self.assertEqual(exile.event_type, "exile")
        self.assertEqual(set(exile.public_payload.get("abstentions", [])), {1, 2, 3})
        effective_votes = exile.public_payload.get("votes", [])
        self.assertTrue(all(vote["voter"] not in {1, 2, 3} for vote in effective_votes))
        self.assertEqual(sum(exile.private_payload.get("tally", {}).values()), len(effective_votes))

    def test_tied_vote_causes_no_exile(self) -> None:
        engine = GameEngine(
            seed=17,
            llm_decider=ScriptedLLMDecider(
                abstain_voters={2, 3, 4, 6, 7},
                vote_targets={1: 8, 5: 8, 8: 1, 9: 1},
            ),
        )
        engine.day = 1
        alive_before = {player.id for player in engine._alive_players()}
        engine._day_vote()

        exile = engine.events[-1]
        self.assertEqual(exile.event_type, "exile")
        self.assertTrue(exile.public_payload.get("tie"))
        self.assertIsNone(exile.public_payload.get("exiled_id"))
        self.assertEqual(exile.public_payload.get("tied_targets"), [1, 8])
        self.assertTrue(exile.public_payload.get("revote"))
        self.assertTrue(any(event.event_type == "vote_tie" for event in engine.events))
        pk_speeches = [
            event
            for event in engine.events
            if event.event_type == "speech" and event.public_payload.get("tie_pk")
        ]
        self.assertEqual({event.actor_id for event in pk_speeches}, {1, 8})
        self.assertEqual({player.id for player in engine._alive_players()}, alive_before)

    def test_role_mentions_are_not_identity_claims(self) -> None:
        engine = GameEngine(seed=11, llm_decider=ScriptedLLMDecider())
        engine.day = 1
        engine._log(
            Phase.DAY_SPEECH,
            "speech",
            "P2 发言：我觉得女巫应该先藏好，不要暴露。",
            actor_id=2,
            public_payload={"speech": "我觉得女巫应该先藏好，不要暴露。", "visible_to_players": True},
        )
        claims = engine._extract_public_claims()
        mentions = engine._extract_public_role_mentions()
        self.assertFalse(any(claim.get("role") == "witch" and claim.get("claim_type") == "role_claim" for claim in claims))
        self.assertTrue(any(mention.get("role_zh") == "女巫" for mention in mentions))

    def test_role_assertions_are_separate_from_claims(self) -> None:
        engine = GameEngine(seed=13, llm_decider=ScriptedLLMDecider())
        engine.day = 1
        engine._log(
            Phase.DAY_SPEECH,
            "speech",
            "P4 发言：P6 像女巫，但我没有实锤。",
            actor_id=4,
            public_payload={"speech": "P6 像女巫，但我没有实锤。", "visible_to_players": True},
        )
        claims = engine._extract_public_claims()
        assertions = engine._extract_public_role_assertions()
        self.assertFalse(any(claim.get("claim_type") == "role_assertion" for claim in claims))
        self.assertTrue(
            any(
                assertion.get("target_id") == 6
                and assertion.get("role") == "witch"
                and assertion.get("assertion_type") == "suspicion"
                for assertion in assertions
            )
        )

    def test_explicit_role_claim_is_recorded(self) -> None:
        engine = GameEngine(seed=12, llm_decider=ScriptedLLMDecider())
        engine.day = 1
        engine._log(
            Phase.DAY_SPEECH,
            "speech",
            "P4 发言：我跳女巫，昨晚信息先不说。",
            actor_id=4,
            public_payload={"speech": "我跳女巫，昨晚信息先不说。", "visible_to_players": True},
        )
        claims = engine._extract_public_claims()
        self.assertTrue(any(claim.get("role") == "witch" and claim.get("claim_type") == "role_claim" for claim in claims))


class EvaluatorTests(unittest.TestCase):
    def test_evaluator_detects_crafted_missteps(self) -> None:
        players = [
            PlayerState(1, "P1", Role.WITCH, role_faction(Role.WITCH)),
            PlayerState(2, "P2", Role.HUNTER, role_faction(Role.HUNTER)),
            PlayerState(3, "P3", Role.VILLAGER, role_faction(Role.VILLAGER), alive=False, death_day=1),
            PlayerState(4, "P4", Role.WEREWOLF, role_faction(Role.WEREWOLF)),
        ]
        events = [
            GameEvent(
                id=1,
                day=1,
                phase=Phase.NIGHT_WITCH,
                event_type="witch_action",
                public_text="女巫行动完成。",
                actor_id=1,
                target_id=3,
                private_payload={"poisoned_target": 3, "reason": "误判"},
            ),
            GameEvent(
                id=2,
                day=1,
                phase=Phase.HUNTER_SHOT,
                event_type="hunter_shot",
                public_text="猎人带走 P3。",
                actor_id=2,
                target_id=3,
                private_payload={"reason": "误判"},
            ),
            GameEvent(
                id=3,
                day=1,
                phase=Phase.DAY_VOTE,
                event_type="vote",
                public_text="P3 投票给 P1。",
                actor_id=3,
                target_id=1,
                public_payload={"visible_to_players": True},
            ),
        ]
        result = GameResult(
            game_id="crafted",
            seed=1,
            version_id="test",
            winner=Faction.WEREWOLVES,
            win_reason="crafted",
            day=1,
            players=players,
            events=events,
            observation_samples=[],
        )
        report = evaluate_game(result)
        mistake_types = {mistake["type"] for mistake in report.mistakes}
        self.assertIn("witch_poisoned_good", mistake_types)
        self.assertIn("hunter_shot_good", mistake_types)

    def test_evaluator_scores_wolf_template_speech(self) -> None:
        players = [
            PlayerState(1, "P1", Role.WEREWOLF, role_faction(Role.WEREWOLF)),
            PlayerState(2, "P2", Role.WEREWOLF, role_faction(Role.WEREWOLF)),
            PlayerState(3, "P3", Role.WEREWOLF, role_faction(Role.WEREWOLF)),
            PlayerState(4, "P4", Role.SEER, role_faction(Role.SEER)),
            PlayerState(5, "P5", Role.VILLAGER, role_faction(Role.VILLAGER)),
        ]
        repeated = "我先基于公开信息观察，当前重点关注发言和票型，暂时不做强归票。"
        events = [
            GameEvent(
                id=1,
                day=1,
                phase=Phase.DAY_SPEECH,
                event_type="speech",
                public_text=f"P1 发言：{repeated}",
                actor_id=1,
                public_payload={"speech": repeated, "visible_to_players": True},
            ),
            GameEvent(
                id=2,
                day=1,
                phase=Phase.DAY_SPEECH,
                event_type="speech",
                public_text=f"P2 发言：{repeated}",
                actor_id=2,
                public_payload={"speech": repeated, "visible_to_players": True},
            ),
        ]
        result = GameResult(
            game_id="wolf-template",
            seed=2,
            version_id="test",
            winner=Faction.VILLAGERS,
            win_reason="crafted",
            day=1,
            players=players,
            events=events,
            observation_samples=[],
        )
        report = evaluate_game(result)
        self.assertIn("wolf_deception_quality", report.scores)
        self.assertLess(report.scores["wolf_deception_quality"], 62.0)
        self.assertIn("werewolf_template_speech", {mistake["type"] for mistake in report.mistakes})

    def test_evaluator_flags_low_deception_wolf_speech(self) -> None:
        players = [
            PlayerState(1, "P1", Role.WEREWOLF, role_faction(Role.WEREWOLF)),
            PlayerState(2, "P2", Role.WEREWOLF, role_faction(Role.WEREWOLF)),
            PlayerState(3, "P3", Role.VILLAGER, role_faction(Role.VILLAGER)),
            PlayerState(4, "P4", Role.SEER, role_faction(Role.SEER)),
        ]
        events = [
            GameEvent(
                id=1,
                day=1,
                phase=Phase.DAY_SPEECH,
                event_type="speech",
                public_text="P1 发言：我是好人牌，先听大家发言。",
                actor_id=1,
                public_payload={"speech": "我是好人牌，先听大家发言。"},
            ),
            GameEvent(
                id=2,
                day=1,
                phase=Phase.DAY_SPEECH,
                event_type="speech",
                public_text="P4 发言：我是预言家，昨夜查验 P3 是金水，后续查验 P1。",
                actor_id=4,
                public_payload={"speech": "我是预言家，昨夜查验 P3 是金水，后续查验 P1。"},
            ),
        ]
        result = GameResult(
            game_id="wolf-low-deception",
            seed=3,
            version_id="test",
            winner=Faction.VILLAGERS,
            win_reason="crafted",
            day=1,
            players=players,
            events=events,
            observation_samples=[],
        )
        report = evaluate_game(result)
        mistake_types = {mistake["type"] for mistake in report.mistakes}

        self.assertIn("werewolf_low_deception_speech", mistake_types)
        self.assertIn("werewolf_no_seer_counterplay", mistake_types)
        self.assertLess(report.scores["wolf_deception_quality"], 55.0)

    def test_evaluator_flags_incomplete_fake_seer_claim(self) -> None:
        players = [
            PlayerState(1, "P1", Role.WEREWOLF, role_faction(Role.WEREWOLF)),
            PlayerState(2, "P2", Role.VILLAGER, role_faction(Role.VILLAGER)),
            PlayerState(3, "P3", Role.SEER, role_faction(Role.SEER)),
        ]
        events = [
            GameEvent(
                id=1,
                day=1,
                phase=Phase.DAY_SPEECH,
                event_type="speech",
                public_text="P1 发言：我对跳预言家，P2 查杀。",
                actor_id=1,
                public_payload={"speech": "我对跳预言家，P2 查杀。"},
            )
        ]
        result = GameResult(
            game_id="wolf-incomplete-fake-seer",
            seed=4,
            version_id="test",
            winner=Faction.VILLAGERS,
            win_reason="crafted",
            day=1,
            players=players,
            events=events,
            observation_samples=[],
        )
        report = evaluate_game(result)

        self.assertIn("werewolf_incomplete_fake_seer_claim", {mistake["type"] for mistake in report.mistakes})

    def test_evaluator_flags_mechanical_power_role_patterns(self) -> None:
        players = [
            PlayerState(1, "P1", Role.SEER, role_faction(Role.SEER)),
            PlayerState(2, "P2", Role.WITCH, role_faction(Role.WITCH)),
            PlayerState(3, "P3", Role.VILLAGER, role_faction(Role.VILLAGER)),
            PlayerState(4, "P4", Role.WEREWOLF, role_faction(Role.WEREWOLF)),
        ]
        events = [
            GameEvent(
                id=1,
                day=1,
                phase=Phase.NIGHT_SEER,
                event_type="seer_inspect",
                public_text="P1 已完成查验。",
                actor_id=1,
                target_id=3,
                private_payload={"target_id": 3, "result": "villagers"},
            ),
            GameEvent(
                id=2,
                day=1,
                phase=Phase.NIGHT_WITCH,
                event_type="witch_action",
                public_text="P2 已完成女巫行动。",
                actor_id=2,
                target_id=3,
                private_payload={"attacked_target": 3, "saved_target": 3},
            ),
            GameEvent(
                id=3,
                day=1,
                phase=Phase.DAY_SPEECH,
                event_type="speech",
                public_text="P1 发言：我是预言家，昨夜查验 P3 是金水，后续查验 P4。",
                actor_id=1,
                public_payload={"speech": "我是预言家，昨夜查验 P3 是金水，后续查验 P4。"},
            ),
        ]
        report = evaluate_game(
            GameResult(
                game_id="mechanical-power",
                seed=5,
                version_id="test",
                winner=Faction.WEREWOLVES,
                win_reason="crafted",
                day=1,
                players=players,
                events=events,
                observation_samples=[],
            )
        )
        mistake_types = {mistake["type"] for mistake in report.mistakes}

        self.assertIn("seer_mechanical_day1_claim", mistake_types)
        self.assertIn("witch_mechanical_n1_save", mistake_types)

    def test_evaluator_allows_conditional_seer_claim_and_witch_self_save(self) -> None:
        players = [
            PlayerState(1, "P1", Role.SEER, role_faction(Role.SEER)),
            PlayerState(2, "P2", Role.WITCH, role_faction(Role.WITCH)),
            PlayerState(3, "P3", Role.WEREWOLF, role_faction(Role.WEREWOLF)),
        ]
        events = [
            GameEvent(
                id=1,
                day=1,
                phase=Phase.NIGHT_SEER,
                event_type="seer_inspect",
                public_text="P1 已完成查验。",
                actor_id=1,
                target_id=3,
                private_payload={"target_id": 3, "result": "werewolf"},
            ),
            GameEvent(
                id=2,
                day=1,
                phase=Phase.NIGHT_WITCH,
                event_type="witch_action",
                public_text="P2 已完成女巫行动。",
                actor_id=2,
                target_id=2,
                private_payload={"attacked_target": 2, "saved_target": 2},
            ),
            GameEvent(
                id=3,
                day=1,
                phase=Phase.DAY_SPEECH,
                event_type="speech",
                public_text="P1 发言：我是预言家，昨夜查验 P3 是查杀，今天优先归票 P3。",
                actor_id=1,
                public_payload={"speech": "我是预言家，昨夜查验 P3 是查杀，今天优先归票 P3。"},
            ),
        ]
        report = evaluate_game(
            GameResult(
                game_id="conditional-power",
                seed=6,
                version_id="test",
                winner=Faction.VILLAGERS,
                win_reason="crafted",
                day=1,
                players=players,
                events=events,
                observation_samples=[],
            )
        )
        mistake_types = {mistake["type"] for mistake in report.mistakes}

        self.assertNotIn("seer_mechanical_day1_claim", mistake_types)
        self.assertNotIn("witch_mechanical_n1_save", mistake_types)

    def test_evaluator_does_not_treat_soft_counter_as_incomplete_fake_seer(self) -> None:
        players = [
            PlayerState(1, "P1", Role.WEREWOLF, role_faction(Role.WEREWOLF)),
            PlayerState(2, "P2", Role.SEER, role_faction(Role.SEER)),
            PlayerState(3, "P3", Role.VILLAGER, role_faction(Role.VILLAGER)),
        ]
        events = [
            GameEvent(
                id=1,
                day=1,
                phase=Phase.DAY_SPEECH,
                event_type="speech",
                public_text="P1 发言：我质疑 P2 的查验链，等有没有对跳预言家，先看后续。",
                actor_id=1,
                public_payload={"speech": "我质疑 P2 的查验链，等有没有对跳预言家，先看后续。"},
            )
        ]
        report = evaluate_game(
            GameResult(
                game_id="soft-counter",
                seed=7,
                version_id="test",
                winner=Faction.WEREWOLVES,
                win_reason="crafted",
                day=1,
                players=players,
                events=events,
                observation_samples=[],
            )
        )

        self.assertNotIn("werewolf_incomplete_fake_seer_claim", {mistake["type"] for mistake in report.mistakes})


class EvolutionTests(unittest.TestCase):
    def test_ab_uses_injected_llm_decider(self) -> None:
        data = EvolutionManager(seed=20260521, llm_decider_factory=ScriptedLLMDecider).ab_test(games=3)
        self.assertEqual(data["games"], 3)
        self.assertIn("evolved_good_vs_initial_wolves", data["matchups"])
        self.assertIn("evolved_wolves_vs_initial_good", data["matchups"])
        self.assertIn("overall", data["delta"])

    def test_promotion_rejects_large_overall_regression(self) -> None:
        decision = promotion_decision(
            current_score=79.36,
            current_mistakes=1.33,
            candidate_score=77.23,
            candidate_mistakes=1.0,
        )
        self.assertFalse(decision["promoted"])
        self.assertEqual(decision["reason"], "rejected_overall_regression_too_large")

    def test_promotion_accepts_balanced_mistake_reduction(self) -> None:
        decision = promotion_decision(
            current_score=80.0,
            current_mistakes=1.5,
            candidate_score=79.7,
            candidate_mistakes=1.1,
        )
        self.assertTrue(decision["promoted"])
        self.assertEqual(decision["reason"], "mistakes_reduced_without_overall_regression")

    def test_promotion_accepts_clear_overall_gain_if_mistakes_stable(self) -> None:
        decision = promotion_decision(
            current_score=80.0,
            current_mistakes=1.5,
            candidate_score=80.7,
            candidate_mistakes=1.7,
        )
        self.assertTrue(decision["promoted"])
        self.assertEqual(decision["reason"], "overall_improved")

    def test_version_round_number_supports_mode_specific_versions(self) -> None:
        self.assertEqual(_version_round_number("evolved_r7"), 7)
        self.assertEqual(_version_round_number("evolved_workflow_r8"), 8)
        self.assertEqual(_version_round_number("evolved_skill_r9_v2"), 9)
        self.assertEqual(_version_round_number("initial"), 0)

    def test_run_batch_refills_failed_games(self) -> None:
        old_values = {
            key: os.environ.get(key)
            for key in (
                "EVOLUTION_REFILL_FAILED_GAMES",
                "EVOLUTION_MAX_EXTRA_GAME_ATTEMPTS",
                "EVOLUTION_RETRY_WAIT_SECONDS",
                "EVOLUTION_GAME_COOLDOWN_SECONDS",
            )
        }
        calls = {"count": 0}

        def factory():
            calls["count"] += 1
            if calls["count"] == 1:
                return ExplodingLLMDecider()
            return ScriptedLLMDecider()

        try:
            os.environ["EVOLUTION_REFILL_FAILED_GAMES"] = "true"
            os.environ["EVOLUTION_MAX_EXTRA_GAME_ATTEMPTS"] = "2"
            os.environ["EVOLUTION_RETRY_WAIT_SECONDS"] = "0"
            os.environ["EVOLUTION_GAME_COOLDOWN_SECONDS"] = "0"
            data = EvolutionManager(
                seed=20260521,
                llm_decider_factory=factory,
                archive_games=False,
                enable_llm_review=False,
            ).run_batch(
                profiles=default_strategy_versions("test"),
                version_id="test_refill",
                games=1,
            )

            self.assertEqual(data["completed_games"], 1)
            self.assertEqual(data["attempted_games"], 2)
            self.assertEqual(len(data["errors"]), 1)
        finally:
            for key, value in old_values.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


class ScriptedLLMDecider:
    def __init__(
        self,
        *,
        abstain_all_votes: bool = False,
        abstain_voters: set[int] | None = None,
        vote_targets: dict[int, int] | None = None,
    ) -> None:
        self.chat_client = SimpleNamespace(
            config=SimpleNamespace(provider="test-llm", model="scripted", extra_body={})
        )
        self.abstain_all_votes = abstain_all_votes
        self.abstain_voters = abstain_voters or set()
        self.vote_targets = vote_targets or {}

    def decide(self, obs, profile) -> AgentDecision:
        action = self._action(obs)
        target = self._target(obs, action)
        speech = ""
        if action == ActionType.SPEAK:
            speech = f"我是{obs.self_name}，只基于公开信息发言，当前先关注票型和发言矛盾。"
        return AgentDecision(
            actor_id=obs.self_id,
            role=obs.self_role,
            action=action,
            target_id=target,
            speech=speech,
            reason=f"test LLM decision for {profile.prompt_name}",
            confidence=0.6,
            metadata={
                "decision_backend": "llm_test",
                "llm_provider": "test-llm",
                "llm_model": "scripted",
                "prompt_name": profile.prompt_name,
            },
        )

    def _action(self, obs) -> ActionType:
        if ActionType.SPEAK in obs.legal_actions:
            return ActionType.SPEAK
        if obs.phase == Phase.DAY_VOTE and ActionType.PASS in obs.legal_actions:
            if self.abstain_all_votes or obs.self_id in self.abstain_voters:
                return ActionType.PASS
        if obs.phase == Phase.HUNTER_SHOT and ActionType.PASS in obs.legal_actions:
            return ActionType.PASS
        if ActionType.KILL in obs.legal_actions:
            return ActionType.KILL
        if ActionType.INSPECT in obs.legal_actions:
            return ActionType.INSPECT
        if ActionType.VOTE in obs.legal_actions:
            return ActionType.VOTE
        if ActionType.SAVE in obs.legal_actions:
            return ActionType.SAVE
        if ActionType.POISON in obs.legal_actions:
            return ActionType.POISON
        return ActionType.PASS

    def _target(self, obs, action: ActionType) -> int | None:
        if action == ActionType.SAVE:
            return obs.private_knowledge.get("attacked_tonight")
        if action == ActionType.VOTE and obs.self_id in self.vote_targets:
            return self.vote_targets[obs.self_id]
        if action not in {ActionType.KILL, ActionType.INSPECT, ActionType.POISON, ActionType.VOTE, ActionType.SHOOT}:
            return None
        excluded = {obs.self_id}
        if action == ActionType.KILL:
            excluded.update(obs.private_knowledge.get("pack_ids", []))
        candidates = [player["id"] for player in obs.alive_players if player["id"] not in excluded]
        return candidates[0] if candidates else None


class CoordinatedWolfCouncilDecider(ScriptedLLMDecider):
    def __init__(self) -> None:
        super().__init__()
        self.context = {}

    def coordinate_wolf_council(self, context) -> dict:
        self.context = context
        target = context["legal_attack_targets"][-1]
        return {
            "coordination_mode": "llm_wolf_council",
            "coordination_status": "llm_ok",
            "attack_target": target,
            "consensus_reason": "测试协调器选择最后一个合法目标。",
            "proposal_summary": [],
            "day_plan": {
                "overall": "测试计划。",
                "optional_routes": ["保持不同公开理由。"],
                "risk_controls": ["不暴露狼队会议。"],
            },
            "constraints": ["公开发言不能提到夜刀。"],
            "confidence": 0.8,
        }


class ExplodingLLMDecider:
    def __init__(self) -> None:
        self.chat_client = SimpleNamespace(
            config=SimpleNamespace(provider="test-llm", model="exploding", extra_body={})
        )

    def decide(self, obs, profile) -> AgentDecision:
        raise RuntimeError("transient test failure")


def _set_alive_by_role(engine: GameEngine, alive_counts: dict[Role, int]) -> None:
    seen: dict[Role, int] = {role: 0 for role in Role}
    for player in engine.players:
        seen[player.role] += 1
        should_live = seen[player.role] <= alive_counts.get(player.role, 0)
        player.alive = should_live
        player.death_day = None if should_live else 1
        player.death_reason = None if should_live else "test"


if __name__ == "__main__":
    unittest.main()
