from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import EvaluationReport, GameEvent, GameResult, Role, StrategyVersion, to_jsonable
from .skill_usage import summarize_skill_usage


def build_game_payload(
    result: GameResult,
    report: EvaluationReport,
    profiles: dict[Role, StrategyVersion],
    *,
    llm_provider: str,
    llm_model: str | None,
    batch: dict[str, Any] | None = None,
    llm_review: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "game": serialize_game(result),
        "report": to_jsonable(report),
        "strategy_versions": {role.value: to_jsonable(profile) for role, profile in profiles.items()},
        "private_observation_proof": result.observation_samples[:80],
        "skill_usage_summary": summarize_skill_usage(result.events),
        "agent_backend": "llm",
        "llm_provider": llm_provider,
        "llm_model": llm_model,
    }
    if batch:
        payload["batch"] = to_jsonable(batch)
    if llm_review is not None:
        payload["llm_review"] = to_jsonable(llm_review)
    return payload


def archive_completed_game(
    result: GameResult,
    report: EvaluationReport,
    profiles: dict[Role, StrategyVersion],
    *,
    llm_provider: str,
    llm_model: str | None,
    batch: dict[str, Any] | None = None,
    llm_review: dict[str, Any] | None = None,
) -> str:
    payload = build_game_payload(
        result,
        report,
        profiles,
        llm_provider=llm_provider,
        llm_model=llm_model,
        batch=batch,
        llm_review=llm_review,
    )
    payload["archive_path"] = archive_game_payload(payload)
    return str(payload["archive_path"])


def archive_game_payload(payload: dict[str, Any]) -> str:
    game = payload.get("game", {})
    log_dir = Path("logs") / "games"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    game_id = str(game.get("game_id", "game")).replace("/", "_").replace("\\", "_")
    path = log_dir / f"{stamp}_{game_id}.json"
    archived_payload = {**payload, "archive_path": str(path)}
    path.write_text(json.dumps(archived_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def archive_error_payload(payload: dict[str, Any]) -> str:
    log_dir = Path("logs") / "games"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    version = str(payload.get("version_id", "batch")).replace("/", "_").replace("\\", "_")
    index = str(payload.get("game_index", "error")).replace("/", "_").replace("\\", "_")
    path = log_dir / f"{stamp}_error_{version}_{index}.json"
    archived_payload = {**payload, "archive_path": str(path)}
    path.write_text(json.dumps(archived_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def serialize_game(result: GameResult) -> dict[str, Any]:
    return {
        "game_id": result.game_id,
        "seed": result.seed,
        "version_id": result.version_id,
        "winner": result.winner.value,
        "winner_zh": result.winner.zh,
        "win_reason": result.win_reason,
        "day": result.day,
        "game_rules": to_jsonable(result.game_rules),
        "players": [
            {
                **player.public_view(True),
                "role": player.role.value,
                "role_zh": player.role.zh,
                "faction": player.faction.value,
                "faction_zh": player.faction.zh,
                "agent_version": player.agent_version,
            }
            for player in result.players
        ],
        "events": [serialize_event(event) for event in result.events],
        "public_timeline": [event.public_view() for event in result.events if event.public_payload.get("visible_to_players", True)],
        "summary": _game_summary(result),
    }


def serialize_event(event: GameEvent) -> dict[str, Any]:
    decision = event.decision
    return {
        "id": event.id,
        "day": event.day,
        "phase": event.phase.value,
        "phase_zh": event.phase.zh,
        "event_type": event.event_type,
        "public_text": event.public_text,
        "actor_id": event.actor_id,
        "target_id": event.target_id,
        "secondary_target_id": event.secondary_target_id,
        "public_payload": to_jsonable(event.public_payload),
        "private_payload": to_jsonable(event.private_payload),
        "observation_proof": to_jsonable(event.observation_proof),
        "observation_snapshot": to_jsonable(event.observation_snapshot),
        "decision": to_jsonable(decision) if decision else None,
    }


def _game_summary(result: GameResult) -> dict[str, Any]:
    alive = [player for player in result.players if player.alive]
    wolves_alive = [player for player in alive if player.role == Role.WEREWOLF]
    villagers_alive = [player for player in alive if player.role != Role.WEREWOLF]
    civilians_alive = [player for player in alive if player.role == Role.VILLAGER]
    gods_alive = [player for player in alive if player.role in {Role.SEER, Role.WITCH, Role.HUNTER}]
    deaths = [player for player in result.players if not player.alive]
    return {
        "alive_count": len(alive),
        "dead_count": len(deaths),
        "wolves_alive": len(wolves_alive),
        "villagers_alive": len(villagers_alive),
        "civilians_alive": len(civilians_alive),
        "gods_alive": len(gods_alive),
        "death_order": [
            {
                "id": player.id,
                "name": player.name,
                "role_zh": player.role.zh,
                "day": player.death_day,
                "reason": player.death_reason,
            }
            for player in sorted(deaths, key=lambda item: (item.death_day or 0, item.id))
        ],
    }
