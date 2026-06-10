from __future__ import annotations

from typing import Any

from .agents import default_strategy_versions, evolved_strategy_versions
from .archive import archive_game_payload, build_game_payload, serialize_event, serialize_game
from .engine import run_game
from .evaluator import evaluate_game
from .evolution import EvolutionManager
from .memory_store import load_latest_promoted, load_role_memory_profiles, save_review_memory_candidates
from .models import Role, StrategyVersion, to_jsonable
from .reviewer import review_game_with_llm


def get_profiles(version: str) -> dict[Role, StrategyVersion]:
    if version in {"latest", "latest_promoted", "promoted"}:
        latest = load_latest_promoted()
        if latest:
            persisted_latest = load_role_memory_profiles(str(latest.get("version_id") or "initial"))
            if persisted_latest:
                return persisted_latest
    persisted = load_role_memory_profiles(version)
    if persisted:
        return persisted
    if version == "evolved":
        persisted_evolved = load_role_memory_profiles("evolved_r1")
        if persisted_evolved:
            return persisted_evolved
    if version in {"evolved", "evolved_r1", "evolved_r2"}:
        return evolved_strategy_versions(default_strategy_versions("initial"))
    return default_strategy_versions("initial")


def run_demo_game(
    *,
    seed: int | None = None,
    version: str = "initial",
    llm_provider: str = "ark",
    llm_model: str | None = None,
) -> dict[str, Any]:
    profiles = get_profiles(version)
    result = run_game(
        seed=seed,
        profiles=profiles,
        version_id=version,
        llm_provider=llm_provider,
        llm_model=llm_model,
    )
    report = evaluate_game(result)
    llm_review = review_game_with_llm(
        result,
        report,
        llm_provider=llm_provider,
        llm_model=llm_model,
        enabled=True,
    )
    if llm_review is not None:
        bank_item_ids = save_review_memory_candidates(llm_review, version_id=version, game_id=result.game_id)
        if bank_item_ids:
            llm_review["memory_bank_item_ids"] = bank_item_ids
    payload = build_game_payload(
        result,
        report,
        profiles,
        llm_provider=llm_provider,
        llm_model=llm_model,
        llm_review=llm_review,
    )
    payload["archive_path"] = archive_game_payload(payload)
    return payload


def run_evolution_demo(
    *,
    rounds: int = 2,
    games_per_round: int = 20,
    base_version: str = "initial",
    llm_provider: str = "ark",
    llm_model: str | None = None,
    evolution_mode: str = "workflow",
) -> dict[str, Any]:
    manager = EvolutionManager(llm_provider=llm_provider, llm_model=llm_model, evolution_mode=evolution_mode)
    data = manager.run_evolution(rounds=rounds, games_per_round=games_per_round, base_version=base_version)
    return to_jsonable({**data, "llm_provider": llm_provider, "llm_model": llm_model, "evolution_mode": manager.evolution_mode})


def run_frozen_eval(
    *,
    version: str,
    baseline_version: str = "initial",
    games: int = 20,
    llm_provider: str = "ark",
    llm_model: str | None = None,
    progress_callback: Any | None = None,
) -> dict[str, Any]:
    """Evaluate two fixed versions without writing review/evolution memory."""

    candidate_profiles = get_profiles(version)
    baseline_profiles = get_profiles(baseline_version)
    candidate_version_id = _profile_version_id(candidate_profiles, fallback=version)
    baseline_version_id = _profile_version_id(baseline_profiles, fallback=baseline_version)
    manager = EvolutionManager(
        llm_provider=llm_provider,
        llm_model=llm_model,
        archive_games=True,
        enable_llm_review=False,
        enable_strategy_planning=False,
    )
    seed_offset = 60_000
    if progress_callback:
        progress_callback(
            "frozen_eval_started",
            {
                "mode": "frozen_eval",
                "frozen": True,
                "games": games,
                "seed_offset": seed_offset,
                "requested_version": version,
                "version_id": candidate_version_id,
                "requested_baseline_version": baseline_version,
                "baseline_version_id": baseline_version_id,
                "llm_provider": llm_provider,
                "llm_model": llm_model,
                "writes_disabled": _frozen_writes_disabled(),
            },
        )

    def wrap_progress(side: str) -> Any:
        def _progress(event_name: str, data: dict[str, Any]) -> None:
            if not progress_callback:
                return
            progress_callback(
                event_name,
                {
                    **data,
                    "eval_side": side,
                    "candidate_version_id": candidate_version_id,
                    "baseline_version_id": baseline_version_id,
                    "frozen": True,
                },
            )

        return _progress

    candidate = manager.run_batch(
        profiles=candidate_profiles,
        version_id=candidate_version_id,
        games=games,
        seed_offset=seed_offset,
        progress_callback=wrap_progress("candidate") if progress_callback else None,
    )
    if progress_callback:
        progress_callback(
            "frozen_eval_batch_completed",
            {
                "eval_side": "candidate",
                "version_id": candidate_version_id,
                "aggregate": candidate["aggregate"],
                "completed_games": candidate.get("completed_games", 0),
                "errors": candidate.get("errors", []),
                "archives": candidate.get("archive_paths", []),
                "completed_seeds": candidate.get("completed_seeds", []),
            },
        )
    baseline_seed_sequence = list(candidate.get("completed_seeds", []))
    if progress_callback:
        progress_callback(
            "frozen_eval_seed_alignment",
            {
                "candidate_completed_seeds": baseline_seed_sequence,
                "baseline_seed_sequence": baseline_seed_sequence,
                "reason": (
                    "Baseline uses the candidate's completed seeds so retries caused by rate limits "
                    "do not make the frozen comparison drift onto different games."
                ),
            },
        )
    baseline = manager.run_batch(
        profiles=baseline_profiles,
        version_id=baseline_version_id,
        games=games,
        seed_offset=seed_offset,
        seed_sequence=baseline_seed_sequence,
        progress_callback=wrap_progress("baseline") if progress_callback else None,
    )
    candidate_score = candidate["aggregate"]["average_scores"].get("overall", 0.0)
    baseline_score = baseline["aggregate"]["average_scores"].get("overall", 0.0)
    candidate_mistakes = candidate["aggregate"].get("mistakes_per_game", 0.0)
    baseline_mistakes = baseline["aggregate"].get("mistakes_per_game", 0.0)
    payload = {
            "mode": "frozen_eval",
            "frozen": True,
            "writes_disabled": _frozen_writes_disabled(),
            "games": games,
            "seed_offset": seed_offset,
            "requested_version": version,
            "version_id": candidate_version_id,
            "requested_baseline_version": baseline_version,
            "baseline_version_id": baseline_version_id,
            "candidate": candidate["aggregate"],
            "baseline": baseline["aggregate"],
            "delta": {
                "overall": round(candidate_score - baseline_score, 2),
                "mistakes_per_game": round(candidate_mistakes - baseline_mistakes, 2),
                "villager_win_rate": round(
                    _win_rate(candidate["aggregate"], "villagers") - _win_rate(baseline["aggregate"], "villagers"),
                    3,
                ),
                "werewolf_win_rate": round(
                    _win_rate(candidate["aggregate"], "werewolves") - _win_rate(baseline["aggregate"], "werewolves"),
                    3,
                ),
            },
            "archives": {
                "candidate": candidate.get("archive_paths", []),
                "baseline": baseline.get("archive_paths", []),
            },
            "errors": {
                "candidate": candidate.get("errors", []),
                "baseline": baseline.get("errors", []),
            },
            "completed_games": {
                "candidate": candidate.get("completed_games", 0),
                "baseline": baseline.get("completed_games", 0),
            },
            "seed_alignment": {
                "mode": "candidate_completed_seeds",
                "candidate_completed_seeds": candidate.get("completed_seeds", []),
                "baseline_completed_seeds": baseline.get("completed_seeds", []),
            },
            "llm_provider": llm_provider,
            "llm_model": llm_model,
            "interpretation": (
                "冻结测评不会写入 memory bank、不会生成新策略版本、不会更新 latest_promoted；"
                "它只用于验证固定版本在同一批种子附近的稳定表现。"
            ),
        }
    if progress_callback:
        progress_callback("frozen_eval_completed", payload)
    return to_jsonable(payload)


def run_ab_demo(*, games: int = 20, llm_provider: str = "ark", llm_model: str | None = None) -> dict[str, Any]:
    manager = EvolutionManager(llm_provider=llm_provider, llm_model=llm_model)
    data = manager.ab_test(games=games)
    return to_jsonable({**data, "llm_provider": llm_provider, "llm_model": llm_model})


def _profile_version_id(profiles: dict[Role, StrategyVersion], *, fallback: str) -> str:
    version_ids = {profile.version_id for profile in profiles.values() if profile.version_id}
    if len(version_ids) == 1:
        return next(iter(version_ids))
    return fallback


def _win_rate(aggregate: dict[str, Any], faction: str) -> float:
    winners = aggregate.get("winner_counts", {})
    total = sum(int(value) for value in winners.values())
    if total <= 0:
        return 0.0
    return int(winners.get(faction, 0)) / total


def _frozen_writes_disabled() -> dict[str, bool]:
    return {
        "llm_review": True,
        "memory_bank": True,
        "strategy_planning": True,
        "role_memory_snapshot": True,
        "latest_promoted": True,
    }
