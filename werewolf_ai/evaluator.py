from __future__ import annotations

import re
import statistics
from collections import defaultdict
from typing import Any

from .models import EvaluationReport, Faction, GameEvent, GameResult, Role, role_faction


def evaluate_game(result: GameResult) -> EvaluationReport:
    players = {player.id: player for player in result.players}
    mistakes = _detect_mistakes(result)
    key_decisions = _extract_key_decisions(result)

    speech_quality = _speech_quality(result.events)
    vote_quality = _vote_quality(result.events, players)
    skill_quality = _skill_quality(result.events, players)
    wolf_deception_quality = _wolf_deception_quality(result.events, players)
    social_influence_quality = _social_influence_quality(result.events, players)
    wolf_deception_diversity = _wolf_deception_diversity(result.events, players)
    contribution = _team_contribution(result, players)
    mistake_penalty = max(0.0, 100.0 - len(mistakes) * 9.0)
    overall = round(
        speech_quality * 0.20
        + vote_quality * 0.16
        + skill_quality * 0.25
        + contribution * 0.12
        + wolf_deception_quality * 0.10
        + mistake_penalty * 0.17,
        2,
    )

    role_scores = _role_scores(result.events, players, result.winner)
    recommendations = _recommendations(mistakes)
    summary = (
        f"{result.winner.zh}获胜；综合评分 {overall:.1f}。"
        f" 本局识别关键失误 {len(mistakes)} 个，投票质量 {vote_quality:.1f}，技能质量 {skill_quality:.1f}。"
    )

    return EvaluationReport(
        game_id=result.game_id,
        winner=result.winner,
        scores={
            "overall": overall,
            "speech_quality": round(speech_quality, 2),
            "vote_quality": round(vote_quality, 2),
            "skill_quality": round(skill_quality, 2),
            "wolf_deception_quality": round(wolf_deception_quality, 2),
            "wolf_deception_diversity": round(wolf_deception_diversity, 2),
            "social_influence_quality": round(social_influence_quality, 2),
            "team_contribution": round(contribution, 2),
            "mistake_penalty": round(mistake_penalty, 2),
        },
        role_scores=role_scores,
        key_decisions=key_decisions,
        mistakes=mistakes,
        recommendations=recommendations,
        summary=summary,
    )


def aggregate_reports(reports: list[EvaluationReport]) -> dict[str, Any]:
    if not reports:
        return {
            "games": 0,
            "average_scores": {},
            "winner_counts": {},
            "mistake_counts": {},
            "recommendations": {},
        }

    score_keys = sorted(reports[0].scores.keys())
    average_scores = {
        key: round(statistics.mean(report.scores.get(key, 0.0) for report in reports), 2) for key in score_keys
    }
    winner_counts: dict[str, int] = defaultdict(int)
    mistake_counts: dict[str, int] = defaultdict(int)
    recommendations: dict[str, set[str]] = defaultdict(set)
    for report in reports:
        winner_counts[report.winner.value] += 1
        for mistake in report.mistakes:
            mistake_counts[mistake["type"]] += 1
        for role, items in report.recommendations.items():
            for item in items:
                recommendations[role].add(item)

    return {
        "games": len(reports),
        "average_scores": average_scores,
        "winner_counts": dict(winner_counts),
        "mistake_counts": dict(mistake_counts),
        "recommendations": {role: sorted(items) for role, items in recommendations.items()},
        "mistakes_per_game": round(sum(len(report.mistakes) for report in reports) / len(reports), 2),
    }


def _speech_quality(events: list[GameEvent]) -> float:
    speeches = [event.public_payload.get("speech", "") for event in events if event.event_type == "speech"]
    if not speeches:
        return 0.0
    scores: list[float] = []
    for speech in speeches:
        score = min(55.0, len(speech) * 0.9)
        if any(token in speech for token in ("查杀", "金水", "投票链", "公开", "解释", "发言")):
            score += 18.0
        if any(token in speech for token in ("今天", "建议", "重点", "归票", "处理")):
            score += 14.0
        if "夜间" in speech and not any(token in speech for token in ("我没有夜间信息", "预言家")):
            score -= 10.0
        scores.append(max(15.0, min(100.0, score)))
    return statistics.mean(scores)


def _vote_quality(events: list[GameEvent], players: dict[int, Any]) -> float:
    vote_events = [event for event in events if event.event_type == "vote" and event.actor_id and event.target_id]
    if not vote_events:
        return 0.0
    scores: list[float] = []
    confirmed_by_day = _confirmed_wolves_by_day(events, players)
    for event in vote_events:
        voter = players[event.actor_id]
        target = players[event.target_id]
        score = 35.0
        if voter.role == Role.WEREWOLF and target.role != Role.WEREWOLF:
            score = 76.0
        elif voter.role != Role.WEREWOLF and target.role == Role.WEREWOLF:
            score = 88.0
        elif voter.role != Role.WEREWOLF and target.role != Role.WEREWOLF:
            score = 28.0
        elif voter.role == Role.WEREWOLF and target.role == Role.WEREWOLF:
            score = 18.0

        confirmed = confirmed_by_day.get(event.day)
        if confirmed and target.id == confirmed and voter.role != Role.WEREWOLF:
            score = max(score, 94.0)
        elif confirmed and target.id != confirmed and voter.role != Role.WEREWOLF:
            score -= 14.0
        scores.append(max(0.0, min(100.0, score)))
    return statistics.mean(scores)


def _skill_quality(events: list[GameEvent], players: dict[int, Any]) -> float:
    scores: list[float] = []
    for event in events:
        if event.event_type == "seer_inspect":
            target = event.private_payload.get("target_id")
            result = event.private_payload.get("result")
            scores.append(86.0 if result == "werewolf" else 68.0 if target else 20.0)
        elif event.event_type == "witch_action":
            saved = event.private_payload.get("saved_target")
            poisoned = event.private_payload.get("poisoned_target")
            if saved:
                scores.append(82.0)
            if poisoned:
                scores.append(92.0 if players[poisoned].role == Role.WEREWOLF else 18.0)
            if not saved and not poisoned:
                scores.append(62.0)
        elif event.event_type == "wolf_intent":
            target = event.private_payload.get("target_id")
            if target:
                scores.append(90.0 if players[target].role in {Role.SEER, Role.WITCH, Role.HUNTER} else 70.0)
        elif event.event_type == "hunter_shot":
            target = event.target_id
            if target:
                scores.append(90.0 if players[target].role == Role.WEREWOLF else 12.0)
    return statistics.mean(scores) if scores else 50.0


def _wolf_deception_quality(events: list[GameEvent], players: dict[int, Any]) -> float:
    wolves = {pid for pid, player in players.items() if player.role == Role.WEREWOLF}
    if not wolves:
        return 50.0

    score = 62.0
    if _wolves_split_early_votes(events, players):
        score += 12.0
    if _wolf_counterplay_to_seer(events, players):
        score += 14.0
    if _wolf_complete_fake_seer_chain(events, players):
        score += 10.0
    if _wolves_created_reasonable_divergence(events, players):
        score += 8.0
    if _wolf_caused_good_misvotes(events, players):
        score += 10.0
    if _wolves_used_tactical_plan(events):
        score += 6.0
    score += min(8.0, max(0, len(_wolf_deception_intents(events, players)) - 1) * 2.0)

    pack_vote_mistakes = _wolf_pack_vote_mistakes_from_events(events, players)
    score -= 16.0 * len(pack_vote_mistakes)
    score -= 12.0 * len(_wolf_template_speech_mistakes_from_events(events, players))
    score -= 10.0 * len(_wolf_low_deception_speech_mistakes_from_events(events, players))
    score -= 14.0 * len(_wolf_no_seer_counterplay_mistakes_from_events(events, players))
    score -= 12.0 * len(_wolf_incomplete_fake_seer_mistakes_from_events(events, players))
    return max(0.0, min(100.0, score))


def _team_contribution(result: GameResult, players: dict[int, Any]) -> float:
    votes = _vote_quality(result.events, players)
    skills = _skill_quality(result.events, players)
    winner_bonus = 72.0 if result.winner == Faction.VILLAGERS else 68.0
    return statistics.mean([votes, skills, winner_bonus])


def _social_influence_quality(events: list[GameEvent], players: dict[int, Any]) -> float:
    metrics = _social_influence_metrics(events, players)
    attempts = metrics["good_influence_attempts"] + metrics["wolf_manipulation_attempts"]
    if attempts == 0:
        return 50.0
    good_rate = metrics["good_influence_successes"] / max(1, metrics["good_influence_attempts"])
    wolf_rate = metrics["wolf_manipulation_successes"] / max(1, metrics["wolf_manipulation_attempts"])
    # Both camps having measurable influence makes the game more strategically alive.
    balance_bonus = 10.0 if metrics["good_influence_attempts"] and metrics["wolf_manipulation_attempts"] else 0.0
    return max(0.0, min(100.0, 45.0 + good_rate * 22.0 + wolf_rate * 23.0 + balance_bonus))


def _wolf_deception_diversity(events: list[GameEvent], players: dict[int, Any]) -> float:
    intents = _wolf_deception_intents(events, players)
    if not intents:
        return 35.0
    non_passive = {intent for intent in intents if intent not in {"none", "hide", ""}}
    return max(0.0, min(100.0, 45.0 + len(intents) * 7.0 + len(non_passive) * 10.0))


def _wolf_deception_intents(events: list[GameEvent], players: dict[int, Any]) -> set[str]:
    intents: set[str] = set()
    for event in events:
        if event.event_type not in {"speech", "vote"} or not event.actor_id:
            continue
        if players[event.actor_id].role != Role.WEREWOLF:
            continue
        metadata = getattr(event.decision, "metadata", {}) if event.decision else {}
        intent = str(metadata.get("deception_intent") or "")
        if intent:
            intents.add(intent)
        elif event.event_type == "speech" and _wolf_speech_has_deception_activity(event.public_payload.get("speech", "")):
            intents.add("public_misdirection")
    return intents


def _social_influence_metrics(events: list[GameEvent], players: dict[int, Any]) -> dict[str, int]:
    metrics = {
        "good_influence_attempts": 0,
        "good_influence_successes": 0,
        "wolf_manipulation_attempts": 0,
        "wolf_manipulation_successes": 0,
    }
    votes_by_day: dict[int, list[GameEvent]] = defaultdict(list)
    for event in events:
        if event.event_type == "vote" and event.actor_id and event.target_id:
            votes_by_day[event.day].append(event)

    for speech in events:
        if speech.event_type != "speech" or not speech.actor_id:
            continue
        speaker = players[speech.actor_id]
        targets = _speech_pressure_targets(speech.public_payload.get("speech", ""))
        if not targets:
            continue
        later_votes = [vote for vote in votes_by_day.get(speech.day, []) if vote.id > speech.id]
        if speaker.role == Role.WEREWOLF:
            good_targets = {target for target in targets if target in players and players[target].role != Role.WEREWOLF}
            if not good_targets:
                continue
            metrics["wolf_manipulation_attempts"] += 1
            if any(players[vote.actor_id].role != Role.WEREWOLF and vote.target_id in good_targets for vote in later_votes):
                metrics["wolf_manipulation_successes"] += 1
        else:
            wolf_targets = {target for target in targets if target in players and players[target].role == Role.WEREWOLF}
            if not wolf_targets:
                continue
            metrics["good_influence_attempts"] += 1
            if any(players[vote.actor_id].role != Role.WEREWOLF and vote.target_id in wolf_targets for vote in later_votes):
                metrics["good_influence_successes"] += 1
    return metrics


def _wolf_deception_event_summaries(events: list[GameEvent], players: dict[int, Any]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for event in events:
        if event.event_type not in {"speech", "vote"} or not event.actor_id:
            continue
        if players[event.actor_id].role != Role.WEREWOLF:
            continue
        metadata = getattr(event.decision, "metadata", {}) if event.decision else {}
        intent = str(metadata.get("deception_intent") or "")
        speech = event.public_payload.get("speech", "") if event.event_type == "speech" else getattr(event.decision, "reason", "")
        evidence_ids = list(metadata.get("evidence_event_ids") or [])
        summaries.append(
            {
                "day": event.day,
                "phase": event.phase.value,
                "type": "wolf_deception_event",
                "event_id": event.id,
                "actor_id": event.actor_id,
                "action": getattr(event.decision, "action", None).value if event.decision else None,
                "target_id": event.target_id,
                "deception_intent": intent or _infer_deception_intent(event, players),
                "deception_type": _deception_type_from_event(event, players),
                "public_evidence_supported": bool(evidence_ids),
                "fabrication_risk": _fabrication_risk(speech, evidence_ids),
                "pressure_targets": sorted(_speech_pressure_targets(speech)),
                "quoted_evidence": list(metadata.get("quoted_evidence") or [])[:3],
            }
        )
    return summaries


def _infer_deception_intent(event: GameEvent, players: dict[int, Any]) -> str:
    if event.event_type == "vote":
        if event.target_id and players[event.target_id].role == Role.WEREWOLF:
            return "bus_teammate"
        return "pressure_good"
    speech = event.public_payload.get("speech", "")
    if _is_hard_fake_seer_claim(event):
        return "fake_seer"
    if _wolf_speech_has_deception_activity(speech):
        return "soft_counter"
    return "hide"


def _deception_type_from_event(event: GameEvent, players: dict[int, Any]) -> str:
    metadata = getattr(event.decision, "metadata", {}) if event.decision else {}
    intent = str(metadata.get("deception_intent") or "")
    if intent in {"fake_seer", "soft_counter", "bus_teammate", "split_vote", "pressure_good", "hide", "none"}:
        return intent
    if event.event_type == "vote" and event.target_id and players[event.target_id].role == Role.WEREWOLF:
        return "bus_teammate"
    if event.event_type == "speech" and _is_hard_fake_seer_claim(event):
        return "fake_seer"
    if event.event_type == "speech" and _speech_pressure_targets(event.public_payload.get("speech", "")):
        return "misdirection"
    return "concealment"


def _fabrication_risk(text: str, evidence_ids: list[Any]) -> str:
    if evidence_ids:
        return "low"
    risky_terms = ("查验", "查杀", "金水", "昨夜", "女巫", "猎人", "预言家", "票型", "发言")
    if any(term in text for term in risky_terms):
        return "medium"
    return "low"


def _role_scores(events: list[GameEvent], players: dict[int, Any], winner: Faction) -> dict[str, dict[str, float]]:
    buckets: dict[Role, list[float]] = defaultdict(list)
    for event in events:
        if event.actor_id is None:
            continue
        actor = players[event.actor_id]
        if event.event_type == "speech":
            speech = event.public_payload.get("speech", "")
            buckets[actor.role].append(min(100.0, 40.0 + len(speech) * 0.7))
        elif event.event_type == "vote" and event.target_id:
            target = players[event.target_id]
            if actor.role == Role.WEREWOLF:
                buckets[actor.role].append(80.0 if target.role != Role.WEREWOLF else 15.0)
            else:
                buckets[actor.role].append(86.0 if target.role == Role.WEREWOLF else 28.0)
        elif event.event_type in {"seer_inspect", "witch_action", "wolf_intent", "hunter_shot"}:
            buckets[actor.role].append(_single_skill_score(event, players))

    role_scores: dict[str, dict[str, float]] = {}
    for role in Role:
        values = buckets.get(role, [45.0])
        alignment_bonus = 8.0 if role_faction(role) == winner else 0.0
        role_scores[role.value] = {
            "average": round(min(100.0, statistics.mean(values) + alignment_bonus), 2),
            "sample_count": float(len(values)),
        }
    return role_scores


def _single_skill_score(event: GameEvent, players: dict[int, Any]) -> float:
    if event.event_type == "seer_inspect":
        return 88.0 if event.private_payload.get("result") == "werewolf" else 66.0
    if event.event_type == "witch_action":
        poisoned = event.private_payload.get("poisoned_target")
        saved = event.private_payload.get("saved_target")
        if poisoned:
            return 92.0 if players[poisoned].role == Role.WEREWOLF else 18.0
        return 82.0 if saved else 58.0
    if event.event_type == "wolf_intent":
        target = event.private_payload.get("target_id")
        return 90.0 if target and players[target].role in {Role.SEER, Role.WITCH, Role.HUNTER} else 70.0
    if event.event_type == "hunter_shot":
        target = event.target_id
        return 90.0 if target and players[target].role == Role.WEREWOLF else 12.0
    return 50.0


def _detect_mistakes(result: GameResult) -> list[dict[str, Any]]:
    players = {player.id: player for player in result.players}
    mistakes: list[dict[str, Any]] = []

    for event in result.events:
        if event.event_type == "witch_action":
            poisoned = event.private_payload.get("poisoned_target")
            if poisoned and players[poisoned].role != Role.WEREWOLF:
                mistakes.append(
                    {
                        "type": "witch_poisoned_good",
                        "day": event.day,
                        "actor_id": event.actor_id,
                        "target_id": poisoned,
                        "severity": "high",
                        "message": f"女巫毒死了好人 P{poisoned}，毒药使用过早或依据不足。",
                        "role": Role.WITCH.value,
                    }
                )

        if event.event_type == "hunter_shot" and event.target_id and players[event.target_id].role != Role.WEREWOLF:
            mistakes.append(
                {
                    "type": "hunter_shot_good",
                    "day": event.day,
                    "actor_id": event.actor_id,
                    "target_id": event.target_id,
                    "severity": "high",
                    "message": f"猎人开枪带走好人 P{event.target_id}，需要更依赖公开查杀和票型。",
                    "role": Role.HUNTER.value,
                }
            )

    mistakes.extend(_seer_reveal_mistakes(result, players))
    mistakes.extend(_seer_mechanical_claim_mistakes(result, players))
    mistakes.extend(_witch_mechanical_save_mistakes(result, players))
    mistakes.extend(_wolf_pack_vote_mistakes(result, players))
    mistakes.extend(_wolf_template_speech_mistakes(result, players))
    mistakes.extend(_wolf_low_deception_speech_mistakes(result, players))
    mistakes.extend(_wolf_no_seer_counterplay_mistakes(result, players))
    mistakes.extend(_wolf_incomplete_fake_seer_mistakes(result, players))
    mistakes.extend(_villager_vote_mistakes(result, players))
    return mistakes


def _seer_reveal_mistakes(result: GameResult, players: dict[int, Any]) -> list[dict[str, Any]]:
    mistakes: list[dict[str, Any]] = []
    for inspect in [event for event in result.events if event.event_type == "seer_inspect"]:
        if inspect.private_payload.get("result") != "werewolf":
            continue
        seer_id = inspect.actor_id
        target_id = inspect.private_payload.get("target_id")
        if not seer_id or not target_id:
            continue
        target_dead_before_speech = players[target_id].death_day is not None and players[target_id].death_day <= inspect.day
        if target_dead_before_speech:
            continue
        later_speeches = [
            event
            for event in result.events
            if event.event_type == "speech" and event.actor_id == seer_id and event.day >= inspect.day
        ]
        revealed = any(f"P{target_id}" in event.public_payload.get("speech", "") and "查杀" in event.public_payload.get("speech", "") for event in later_speeches)
        if not revealed:
            mistakes.append(
                {
                    "type": "seer_failed_to_reveal_wolf",
                    "day": inspect.day,
                    "actor_id": seer_id,
                    "target_id": target_id,
                    "severity": "medium",
                    "message": f"预言家查到 P{target_id} 是狼人，但没有形成明确查杀发言。",
                    "role": Role.SEER.value,
                }
            )
    return mistakes


def _seer_mechanical_claim_mistakes(result: GameResult, players: dict[int, Any]) -> list[dict[str, Any]]:
    mistakes: list[dict[str, Any]] = []
    for event in sorted(result.events, key=lambda item: (item.day, item.id)):
        if event.event_type != "speech" or event.day != 1 or not event.actor_id:
            continue
        if players[event.actor_id].role != Role.SEER:
            continue
        speech = event.public_payload.get("speech", "")
        if not _mentions_role_claim(speech, "预言家"):
            continue
        if _seer_has_wolf_inspection_before(result.events, event.actor_id, event):
            continue
        if _speech_reports_wolf_check(speech):
            continue
        if _player_under_public_pressure(result.events, event.actor_id, event):
            continue
        mistakes.append(
            {
                "type": "seer_mechanical_day1_claim",
                "day": event.day,
                "actor_id": event.actor_id,
                "target_id": _extract_player_marker(speech),
                "severity": "low",
                "message": f"预言家 P{event.actor_id} 在首日只有金水或信息不足时机械起跳，容易成为次夜刀口并让狼队收益稳定化。",
                "role": Role.SEER.value,
            }
        )
    return mistakes


def _witch_mechanical_save_mistakes(result: GameResult, players: dict[int, Any]) -> list[dict[str, Any]]:
    mistakes: list[dict[str, Any]] = []
    for event in result.events:
        if event.event_type != "witch_action" or event.day != 1 or not event.actor_id:
            continue
        if players[event.actor_id].role != Role.WITCH:
            continue
        saved = event.private_payload.get("saved_target")
        if not saved or saved == event.actor_id:
            continue
        mistakes.append(
            {
                "type": "witch_mechanical_n1_save",
                "day": event.day,
                "actor_id": event.actor_id,
                "target_id": saved,
                "severity": "low",
                "message": f"女巫 P{event.actor_id} 首夜非自救直接使用解药，若缺少目标价值判断会让后续保护关键神职的空间变小。",
                "role": Role.WITCH.value,
            }
        )
    return mistakes


def _seer_has_wolf_inspection_before(events: list[GameEvent], seer_id: int, speech_event: GameEvent) -> bool:
    return any(
        event.event_type == "seer_inspect"
        and event.actor_id == seer_id
        and (event.day < speech_event.day or (event.day == speech_event.day and event.id < speech_event.id))
        and event.private_payload.get("result") == "werewolf"
        for event in events
    )


def _speech_reports_wolf_check(speech: str) -> bool:
    compact = "".join(str(speech or "").split())
    return any(token in compact for token in ("查杀", "验到狼", "查到狼", "是狼人"))


def _player_under_public_pressure(events: list[GameEvent], player_id: int, speech_event: GameEvent) -> bool:
    marker = f"P{player_id}"
    pressure_tokens = ("质疑", "怀疑", "像狼", "狼面", "抗推", "归票", "投")
    for event in events:
        if event.event_type != "speech" or not event.actor_id:
            continue
        if event.day > speech_event.day or (event.day == speech_event.day and event.id >= speech_event.id):
            continue
        speech = event.public_payload.get("speech", "")
        if marker in speech and any(token in speech for token in pressure_tokens):
            return True
    return False


def _wolf_pack_vote_mistakes(result: GameResult, players: dict[int, Any]) -> list[dict[str, Any]]:
    return _wolf_pack_vote_mistakes_from_events(result.events, players)


def _wolf_pack_vote_mistakes_from_events(events: list[GameEvent], players: dict[int, Any]) -> list[dict[str, Any]]:
    mistakes: list[dict[str, Any]] = []
    votes_by_day_target: dict[tuple[int, int], list[int]] = defaultdict(list)
    wolf_votes_by_day: dict[int, list[int]] = defaultdict(list)
    for event in events:
        if event.event_type == "vote" and event.actor_id and event.target_id:
            if players[event.actor_id].role == Role.WEREWOLF and players[event.target_id].role != Role.WEREWOLF:
                votes_by_day_target[(event.day, event.target_id)].append(event.actor_id)
                wolf_votes_by_day[event.day].append(event.actor_id)

    for (day, target), voters in votes_by_day_target.items():
        total_wolf_voters = len(set(wolf_votes_by_day.get(day, [])))
        if day <= 2 and len(voters) >= 2 and len(voters) == total_wolf_voters:
            mistakes.append(
                {
                    "type": "werewolf_exposed_pack_vote",
                    "day": day,
                    "actor_id": voters[0],
                    "target_id": target,
                    "severity": "medium",
                    "message": f"多名狼人早期集中投 P{target}，票型可能暴露狼队协同。",
                    "role": Role.WEREWOLF.value,
                    "wolf_voters": voters,
                }
            )
    return mistakes


def _wolf_template_speech_mistakes(result: GameResult, players: dict[int, Any]) -> list[dict[str, Any]]:
    return _wolf_template_speech_mistakes_from_events(result.events, players)


def _wolf_template_speech_mistakes_from_events(events: list[GameEvent], players: dict[int, Any]) -> list[dict[str, Any]]:
    mistakes: list[dict[str, Any]] = []
    speeches_by_day: dict[int, list[GameEvent]] = defaultdict(list)
    for event in events:
        if event.event_type == "speech" and event.actor_id and players[event.actor_id].role == Role.WEREWOLF and event.day <= 2:
            speeches_by_day[event.day].append(event)

    for day, speeches in speeches_by_day.items():
        for i, left in enumerate(speeches):
            for right in speeches[i + 1 :]:
                similarity = _speech_similarity(
                    left.public_payload.get("speech", ""),
                    right.public_payload.get("speech", ""),
                )
                if similarity >= 0.72:
                    mistakes.append(
                        {
                            "type": "werewolf_template_speech",
                            "day": day,
                            "actor_id": left.actor_id,
                            "target_id": right.actor_id,
                            "severity": "low",
                            "message": f"狼人 P{left.actor_id} 与 P{right.actor_id} 早期发言相似度过高，容易暴露模板化协同。",
                            "role": Role.WEREWOLF.value,
                            "similarity": round(similarity, 2),
                        }
                    )
    return mistakes


def _wolf_low_deception_speech_mistakes(result: GameResult, players: dict[int, Any]) -> list[dict[str, Any]]:
    return _wolf_low_deception_speech_mistakes_from_events(result.events, players)


def _wolf_low_deception_speech_mistakes_from_events(events: list[GameEvent], players: dict[int, Any]) -> list[dict[str, Any]]:
    mistakes: list[dict[str, Any]] = []
    for event in events:
        if event.event_type != "speech" or not event.actor_id or event.day > 2:
            continue
        if players[event.actor_id].role != Role.WEREWOLF:
            continue
        speech = event.public_payload.get("speech", "")
        if _wolf_speech_has_deception_activity(speech):
            continue
        if any(token in speech for token in ("我是好人", "好人牌", "普通好人", "村民牌")):
            mistakes.append(
                {
                    "type": "werewolf_low_deception_speech",
                    "day": event.day,
                    "actor_id": event.actor_id,
                    "target_id": None,
                    "severity": "low",
                    "message": f"狼人 P{event.actor_id} 早期发言停留在低质量好人伪装，缺少可验证的站边、质疑、分歧或欺骗意图。",
                    "role": Role.WEREWOLF.value,
                }
            )
    return mistakes


def _wolf_no_seer_counterplay_mistakes(result: GameResult, players: dict[int, Any]) -> list[dict[str, Any]]:
    return _wolf_no_seer_counterplay_mistakes_from_events(result.events, players)


def _wolf_no_seer_counterplay_mistakes_from_events(events: list[GameEvent], players: dict[int, Any]) -> list[dict[str, Any]]:
    claim_days = sorted(
        {
            event.day
            for event in events
            if event.event_type == "speech"
            and event.actor_id
            and event.day <= 2
            and players[event.actor_id].role != Role.WEREWOLF
            and _mentions_role_claim(event.public_payload.get("speech", ""), "预言家")
        }
    )
    if not claim_days:
        return []
    if _wolf_counterplay_to_seer(events, players):
        return []
    first_day = claim_days[0]
    first_wolf = next((player.id for player in players.values() if player.role == Role.WEREWOLF), None)
    return [
        {
            "type": "werewolf_no_seer_counterplay",
            "day": first_day,
            "actor_id": first_wolf,
            "target_id": None,
            "severity": "medium",
            "message": "场上早期出现非狼预言家起跳后，狼队没有形成软对跳、悍跳、质疑查验链或制造分歧，默认认下导致博弈不足。",
            "role": Role.WEREWOLF.value,
        }
    ]


def _wolf_incomplete_fake_seer_mistakes(result: GameResult, players: dict[int, Any]) -> list[dict[str, Any]]:
    return _wolf_incomplete_fake_seer_mistakes_from_events(result.events, players)


def _wolf_incomplete_fake_seer_mistakes_from_events(events: list[GameEvent], players: dict[int, Any]) -> list[dict[str, Any]]:
    mistakes: list[dict[str, Any]] = []
    for event in _wolf_fake_seer_events(events, players):
        speech = event.public_payload.get("speech", "")
        if _fake_seer_chain_complete(speech):
            continue
        mistakes.append(
            {
                "type": "werewolf_incomplete_fake_seer_claim",
                "day": event.day,
                "actor_id": event.actor_id,
                "target_id": _extract_player_marker(speech),
                "severity": "medium",
                "message": f"狼人 P{event.actor_id} 尝试悍跳或对跳预言家，但缺少完整查验对象、查验结论、后续查验计划或公开理由。",
                "role": Role.WEREWOLF.value,
            }
        )
    return mistakes


def _villager_vote_mistakes(result: GameResult, players: dict[int, Any]) -> list[dict[str, Any]]:
    mistakes: list[dict[str, Any]] = []
    confirmed = _confirmed_wolves_by_day(result.events, players)
    for event in result.events:
        if event.event_type != "vote" or not event.actor_id or not event.target_id:
            continue
        actor = players[event.actor_id]
        if actor.role == Role.WEREWOLF:
            continue
        confirmed_target = confirmed.get(event.day)
        if not confirmed_target:
            continue
        if players[confirmed_target].role == Role.WEREWOLF and event.target_id != confirmed_target and players[event.target_id].role != Role.WEREWOLF:
            mistakes.append(
                {
                    "type": "villager_ignored_confirmed_wolf",
                    "day": event.day,
                    "actor_id": event.actor_id,
                    "target_id": event.target_id,
                    "severity": "low",
                    "message": f"好人 P{event.actor_id} 在公开查杀 P{confirmed_target} 存在时投向了好人 P{event.target_id}。",
                    "role": actor.role.value,
                }
            )
    return mistakes


def _confirmed_wolves_by_day(events: list[GameEvent], players: dict[int, Any]) -> dict[int, int]:
    confirmed: dict[int, int] = {}
    latest: int | None = None
    for event in sorted(events, key=lambda e: (e.day, e.id)):
        if latest and players[latest].death_day is not None and players[latest].death_day < event.day:
            latest = None
        if event.event_type == "speech":
            speech = event.public_payload.get("speech", "")
            if event.actor_id and players[event.actor_id].role == Role.SEER and "查杀" in speech:
                marker = _extract_player_marker(speech)
                if marker and marker in players and players[marker].role == Role.WEREWOLF:
                    latest = marker
        if latest:
            confirmed[event.day] = latest
    return confirmed


def _wolves_split_early_votes(events: list[GameEvent], players: dict[int, Any]) -> bool:
    for day in (1, 2):
        wolf_votes = [
            event.target_id
            for event in events
            if event.event_type == "vote"
            and event.day == day
            and event.actor_id
            and event.target_id
            and players[event.actor_id].role == Role.WEREWOLF
        ]
        if len(wolf_votes) >= 2 and len(set(wolf_votes)) >= 2:
            return True
    return False


def _wolf_counterplay_to_seer(events: list[GameEvent], players: dict[int, Any]) -> bool:
    wolf_fake_seer = any(
        event.event_type == "speech"
        and event.actor_id
        and players[event.actor_id].role == Role.WEREWOLF
        and (
            _mentions_role_claim(event.public_payload.get("speech", ""), "预言家")
            or any(token in event.public_payload.get("speech", "") for token in ("我查验", "我验", "查杀", "金水", "对跳"))
        )
        for event in events
    )
    if wolf_fake_seer:
        return True

    seer_claim_days = {
        event.day
        for event in events
        if event.event_type == "speech"
        and event.actor_id
        and players[event.actor_id].role != Role.WEREWOLF
        and _mentions_role_claim(event.public_payload.get("speech", ""), "预言家")
    }
    if not seer_claim_days:
        return False
    counter_tokens = ("预言家", "查验", "查杀", "金水", "对跳", "不认", "质疑", "可信度", "验人", "悍跳")
    for event in events:
        if (
            event.event_type == "speech"
            and event.actor_id
            and players[event.actor_id].role == Role.WEREWOLF
            and any(day <= event.day for day in seer_claim_days)
            and any(token in event.public_payload.get("speech", "") for token in counter_tokens)
        ):
            return True
    return False


def _wolf_complete_fake_seer_chain(events: list[GameEvent], players: dict[int, Any]) -> bool:
    return any(_fake_seer_chain_complete(event.public_payload.get("speech", "")) for event in _wolf_fake_seer_events(events, players))


def _wolf_fake_seer_events(events: list[GameEvent], players: dict[int, Any]) -> list[GameEvent]:
    result: list[GameEvent] = []
    for event in events:
        if event.event_type != "speech" or not event.actor_id:
            continue
        if players[event.actor_id].role != Role.WEREWOLF:
            continue
        if _is_hard_fake_seer_claim(event):
            result.append(event)
    return result


def _is_hard_fake_seer_claim(event: GameEvent) -> bool:
    speech = event.public_payload.get("speech", "")
    decision_intent = ""
    if event.decision and isinstance(event.decision.metadata, dict):
        decision_intent = str(event.decision.metadata.get("deception_intent") or "")
    if decision_intent == "fake_seer":
        return True
    if _mentions_role_claim(speech, "预言家"):
        return True

    compact = "".join(str(speech or "").split())
    first_person_jump = re.search(r"我.{0,6}(对跳|悍跳|起跳|跳).{0,6}预言家", compact)
    first_person_check = re.search(r"我.{0,10}(查验|验|查了|验了).{0,14}P\d+", compact)
    first_person_result = re.search(r"(我这里|我报|我的查验|我验|我查).{0,18}P\d+.{0,10}(查杀|金水|好人|狼人)", compact)
    return bool(first_person_jump or first_person_check or first_person_result)


def _fake_seer_chain_complete(speech: str) -> bool:
    has_target = _extract_player_marker(speech) is not None
    has_result = any(token in speech for token in ("查杀", "金水", "好人", "狼人"))
    has_plan = any(token in speech for token in ("后续查验", "下一晚", "明晚", "再查", "验"))
    has_cover = any(token in speech for token in ("因为", "基于", "发言", "票型", "逻辑", "事件", "理由"))
    return has_target and has_result and has_plan and has_cover


def _wolves_created_reasonable_divergence(events: list[GameEvent], players: dict[int, Any]) -> bool:
    for day in (1, 2):
        targets: set[int] = set()
        styles: set[str] = set()
        for event in events:
            if event.event_type != "speech" or event.day != day or not event.actor_id:
                continue
            if players[event.actor_id].role != Role.WEREWOLF:
                continue
            speech = event.public_payload.get("speech", "")
            targets.update(_speech_pressure_targets(speech))
            if any(token in speech for token in ("质疑", "不认", "对跳", "悍跳")):
                styles.add("counter")
            if any(token in speech for token in ("切割", "不站边", "倒钩", "怀疑")):
                styles.add("distance")
            if any(token in speech for token in ("归票", "投", "抗推", "重点")):
                styles.add("pressure")
        if len(targets) >= 2 or len(styles) >= 2:
            return True
    return False


def _wolf_caused_good_misvotes(events: list[GameEvent], players: dict[int, Any]) -> bool:
    pressure_by_day: dict[int, set[int]] = defaultdict(set)
    for event in events:
        if event.event_type != "speech" or not event.actor_id:
            continue
        if players[event.actor_id].role == Role.WEREWOLF:
            pressure_by_day[event.day].update(_speech_pressure_targets(event.public_payload.get("speech", "")))
    for event in events:
        if event.event_type != "vote" or not event.actor_id or not event.target_id:
            continue
        actor = players[event.actor_id]
        target = players[event.target_id]
        if actor.role != Role.WEREWOLF and target.role != Role.WEREWOLF and event.target_id in pressure_by_day.get(event.day, set()):
            return True
    return False


def _wolf_speech_has_deception_activity(speech: str) -> bool:
    if _mentions_role_claim(speech, "预言家") or any(token in speech for token in ("我查验", "我验", "查杀", "金水", "对跳", "悍跳")):
        return True
    pressure_tokens = ("质疑", "怀疑", "不认", "像狼", "狼面", "归票", "投", "站边", "逻辑矛盾", "划水", "抗推", "切割", "倒钩")
    return bool(_speech_pressure_targets(speech)) and any(token in speech for token in pressure_tokens)


def _speech_pressure_targets(speech: str) -> set[int]:
    pressure_tokens = ("质疑", "怀疑", "不认", "像狼", "狼面", "归票", "投", "站边", "逻辑矛盾", "划水", "抗推", "重点", "查杀")
    if not any(token in speech for token in pressure_tokens):
        return set()
    return {int(match.group(1)) for match in re.finditer(r"P(\d+)", speech)}


def _wolves_used_tactical_plan(events: list[GameEvent]) -> bool:
    return any(event.event_type == "wolf_pack_plan" for event in events)


def _mentions_role_claim(speech: str, role_name: str) -> bool:
    compact = "".join(speech.split())
    patterns = (
        f"我跳{role_name}",
        f"我起跳{role_name}",
        f"我对跳{role_name}",
        f"我悍跳{role_name}",
        f"我是{role_name}",
        f"我才是{role_name}",
        f"我这里是{role_name}",
        f"我是全场唯一真{role_name}",
        f"我是唯一{role_name}",
        f"我为{role_name}",
        f"我认{role_name}",
        f"我拍{role_name}",
        f"我有{role_name}视角",
    )
    return any(pattern in compact for pattern in patterns)


def _speech_similarity(left: str, right: str) -> float:
    left_tokens = _speech_token_set(left)
    right_tokens = _speech_token_set(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _speech_token_set(text: str) -> set[str]:
    cleaned = re.sub(r"\s+", "", text)
    cleaned = re.sub(r"[，。！？、；：,.!?;:\"'（）()\[\]{}<>《》]", "", cleaned)
    if len(cleaned) <= 2:
        return {cleaned} if cleaned else set()
    return {cleaned[index : index + 2] for index in range(len(cleaned) - 1)}


def _extract_player_marker(text: str) -> int | None:
    match = re.search(r"P(\d+)", text)
    return int(match.group(1)) if match else None


def _extract_key_decisions(result: GameResult) -> list[dict[str, Any]]:
    key_types = {"seer_inspect", "witch_action", "wolf_intent", "hunter_shot", "exile", "wolf_pack_plan"}
    players = {player.id: player for player in result.players}
    items: list[dict[str, Any]] = []
    items.append(
        {
            "day": None,
            "phase": "analysis",
            "type": "social_influence_summary",
            "metrics": _social_influence_metrics(result.events, players),
        }
    )
    items.extend(_wolf_deception_event_summaries(result.events, players)[:10])
    for event in result.events:
        if event.event_type not in key_types:
            continue
        if event.event_type == "wolf_pack_plan":
            items.append(
                {
                    "day": event.day,
                    "phase": event.phase.value,
                    "type": event.event_type,
                    "actor_id": None,
                    "target_id": None,
                    "public_text": event.public_text,
                    "autonomy_mode": event.private_payload.get("autonomy_mode", True),
                    "strategy_option_ids": [
                        item.get("id")
                        for item in (event.private_payload.get("strategy_options", []) or [])
                    ],
                }
            )
            continue
        items.append(
            {
                "day": event.day,
                "phase": event.phase.value,
                "type": event.event_type,
                "actor_id": event.actor_id,
                "target_id": event.target_id or event.private_payload.get("target_id"),
                "public_text": event.public_text,
                "reason": event.private_payload.get("reason", ""),
            }
        )
    return items[:40]


def _recommendations(mistakes: list[dict[str, Any]]) -> dict[str, list[str]]:
    recommendations: dict[str, set[str]] = defaultdict(set)
    for mistake in mistakes:
        if mistake["type"] == "witch_poisoned_good":
            recommendations[Role.WITCH.value].add("毒药优先绑定公开查杀或多轮高嫌疑目标，缺少硬信息时保留。")
        elif mistake["type"] == "seer_failed_to_reveal_wolf":
            recommendations[Role.SEER.value].add("查到狼人后在白天明确报出 P 号和查杀结论，并推动归票。")
        elif mistake["type"] == "seer_mechanical_day1_claim":
            recommendations[Role.SEER.value].add("只有金水或信息不足时不必机械首日起跳，可先隐忍或软铺垫后续查验计划；等查杀、被强质疑、出现对跳或需要带队时再公开身份。")
        elif mistake["type"] == "witch_mechanical_n1_save":
            recommendations[Role.WITCH.value].add("女巫首夜救人不是固定动作；非自救时应比较被刀目标价值、保留解药保护关键神职的收益，以及后续轮次风险。")
        elif mistake["type"] == "werewolf_exposed_pack_vote":
            recommendations[Role.WEREWOLF.value].add("早期避免多狼无理由集中裸冲同一目标，用公开逻辑分散票型。")
        elif mistake["type"] == "werewolf_template_speech":
            recommendations[Role.WEREWOLF.value].add("狼队需要按座位人格拆分发言风格、证据来源和投票路线，避免同模板暴露。")
        elif mistake["type"] == "werewolf_low_deception_speech":
            recommendations[Role.WEREWOLF.value].add("狼人发言不要只停留在“我是好人”，应选择隐藏、软对跳、倒钩、分票、拉拢或抗推等明确欺骗意图，并用公开事件包装。")
        elif mistake["type"] == "werewolf_no_seer_counterplay":
            recommendations[Role.WEREWOLF.value].add("当场上只有单预言家且没有强查杀时，狼人可选择软对跳、质疑查验链或制造第二视角，而不是全员默认认下。")
        elif mistake["type"] == "werewolf_incomplete_fake_seer_claim":
            recommendations[Role.WEREWOLF.value].add("狼人悍跳或对跳预言家时，要补齐查验对象、查验结论、后续查验计划和公开证据包装。")
        elif mistake["type"] == "villager_ignored_confirmed_wolf":
            recommendations[mistake["role"]].add("出现可信查杀时优先围绕查杀归票，不要散票到低依据目标。")
        elif mistake["type"] == "hunter_shot_good":
            recommendations[Role.HUNTER.value].add("开枪优先选择公开查杀或投票链最高嫌疑，低置信度时可不开枪。")

    for role in Role:
        recommendations.setdefault(role.value, set()).add("保留公开信息来源，发言中说明判断依据和投票计划。")
    return {role: sorted(items) for role, items in recommendations.items()}
