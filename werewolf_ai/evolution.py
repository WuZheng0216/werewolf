from __future__ import annotations

import time
import re
from dataclasses import replace
from collections.abc import Callable
from typing import Any

from .agents import default_strategy_versions, evolved_strategy_versions
from .archive import archive_error_payload, archive_game_payload, build_game_payload, serialize_event
from .engine import GameEngine
from .evaluator import aggregate_reports, evaluate_game
from .memory_store import (
    MEMORY_BANK_PATH,
    MEMORY_DIR,
    load_latest_promoted,
    load_role_memory_profiles,
    save_review_memory_candidates,
    save_evolution_run,
    save_latest_promoted,
    save_role_memory_snapshot,
    update_evolution_run,
)
from .models import EvaluationReport, GameResult, Role, StrategyVersion, to_jsonable
from .player_profiles import PLAYER_PROFILES_PATH
from .llm import build_chat_client_from_env, rate_limit_retry_delay_from_text
from .reviewer import review_game_with_llm
from .evolution_strategist import (
    plan_evolution_with_llm,
    strategist_memory_updates,
    strategist_skill_cards,
)
from .skill_evolution import (
    SKILL_EVOLUTION_MODE,
    WORKFLOW_EVOLUTION_MODE,
    build_role_skills,
    evolve_skill_cards,
    format_skill_for_memory,
    normalize_evolution_mode,
)


PROMOTION_OVERALL_TOLERANCE = 0.5
PROMOTION_OVERALL_IMPROVEMENT = 0.5
PROMOTION_MISTAKE_IMPROVEMENT = 0.25
PROMOTION_MISTAKE_TOLERANCE = 0.25


def promotion_decision(
    *,
    current_score: float,
    current_mistakes: float,
    candidate_score: float,
    candidate_mistakes: float,
) -> dict[str, Any]:
    """Decide whether a candidate strategy version should be promoted.

    A lower mistake rate is valuable for the C-line self-evolution story, but
    it should not promote a version whose overall game quality clearly regresses.
    """

    score_delta = round(candidate_score - current_score, 4)
    mistake_delta = round(candidate_mistakes - current_mistakes, 4)
    score_improved = score_delta >= PROMOTION_OVERALL_IMPROVEMENT
    mistakes_not_much_worse = mistake_delta <= PROMOTION_MISTAKE_TOLERANCE
    mistakes_improved = mistake_delta <= -PROMOTION_MISTAKE_IMPROVEMENT
    score_not_much_worse = score_delta >= -PROMOTION_OVERALL_TOLERANCE
    promoted = (score_improved and mistakes_not_much_worse) or (mistakes_improved and score_not_much_worse)
    if promoted and score_improved:
        reason = "overall_improved"
    elif promoted:
        reason = "mistakes_reduced_without_overall_regression"
    elif mistakes_improved and not score_not_much_worse:
        reason = "rejected_overall_regression_too_large"
    elif score_improved and not mistakes_not_much_worse:
        reason = "rejected_mistake_regression_too_large"
    else:
        reason = "rejected_no_significant_improvement"
    return {
        "promoted": promoted,
        "reason": reason,
        "score_delta": score_delta,
        "mistake_delta": mistake_delta,
        "thresholds": {
            "overall_tolerance": PROMOTION_OVERALL_TOLERANCE,
            "overall_improvement": PROMOTION_OVERALL_IMPROVEMENT,
            "mistake_improvement": PROMOTION_MISTAKE_IMPROVEMENT,
            "mistake_tolerance": PROMOTION_MISTAKE_TOLERANCE,
        },
    }


class EvolutionManager:
    """Prompt/memory evolution loop for the C-line advanced task."""

    def __init__(
        self,
        seed: int = 20260521,
        *,
        llm_provider: str = "ark",
        llm_model: str | None = None,
        llm_decider_factory: Callable[[], Any] | None = None,
        archive_games: bool | None = None,
        enable_llm_review: bool | None = None,
        enable_strategy_planning: bool | None = None,
        evolution_mode: str = WORKFLOW_EVOLUTION_MODE,
    ):
        self.seed = seed
        self.llm_provider = llm_provider
        self.llm_model = llm_model
        self.evolution_mode = normalize_evolution_mode(evolution_mode)
        self.llm_decider_factory = llm_decider_factory
        self.archive_games = llm_decider_factory is None if archive_games is None else archive_games
        self.enable_llm_review = llm_decider_factory is None if enable_llm_review is None else enable_llm_review
        self.enable_strategy_planning = (
            llm_decider_factory is None if enable_strategy_planning is None else enable_strategy_planning
        )
        self.memory_bank_by_role: dict[Role, list[str]] = {}
        self._llm_preflight: dict[str, Any] | None = None

    def run_batch(
        self,
        *,
        profiles: dict[Role, StrategyVersion],
        version_id: str,
        games: int = 20,
        seed_offset: int = 0,
        seed_sequence: list[int] | None = None,
        progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        if seed_sequence is not None:
            games = len(seed_sequence)
        results: list[GameResult] = []
        reports: list[EvaluationReport] = []
        llm_reviews: list[dict[str, Any]] = []
        archive_paths: list[str] = []
        errors: list[dict[str, Any]] = []
        refill_failed = _env_bool("EVOLUTION_REFILL_FAILED_GAMES", True)
        max_extra_attempts = _env_int("EVOLUTION_MAX_EXTRA_GAME_ATTEMPTS", max(2, games))
        max_attempts = games + max(0, max_extra_attempts) if refill_failed else games
        max_attempts = max(games, max_attempts)
        if self.llm_decider_factory is None:
            self._ensure_llm_preflight(progress_callback)
        attempt_index = 0
        while len(results) < games and attempt_index < max_attempts:
            current_attempt = attempt_index
            attempt_index += 1
            index = len(results)
            if seed_sequence is not None:
                game_seed = seed_sequence[index]
            else:
                game_seed = self.seed + seed_offset + current_attempt
            batch = {
                "version_id": version_id,
                "game_index": index,
                "attempt_index": current_attempt,
                "games_requested": games,
                "max_attempts": max_attempts,
                "seed": game_seed,
                "seed_offset": seed_offset,
                "seed_sequence": seed_sequence,
            }
            try:
                def on_event(event, engine: GameEngine) -> None:
                    if progress_callback:
                        progress_callback(
                            "game_event",
                            {
                                "batch": batch,
                                "event": serialize_event(event),
                                "summary": _live_summary(engine),
                            },
                        )

                llm_decider = self.llm_decider_factory() if self.llm_decider_factory else None
                engine = GameEngine(
                    seed=game_seed,
                    profiles=profiles,
                    version_id=version_id,
                    llm_provider=self.llm_provider,
                    llm_model=self.llm_model,
                    llm_decider=llm_decider,
                    memory_bank_by_role=self.memory_bank_by_role,
                    event_callback=on_event,
                )
                if progress_callback:
                    progress_callback(
                        "game_started",
                        {
                            "batch": batch,
                            "game_id": engine.game_id,
                            "seed": engine.seed,
                            "version_id": version_id,
                            "agent_backend": "llm",
                            "llm_provider": self.llm_provider,
                            "llm_model": self.llm_model,
                            "players": [_serialize_player(player) for player in engine.players],
                            "strategy_versions": {role.value: to_jsonable(profile) for role, profile in profiles.items()},
                            "progress": {
                                "requested_games": games,
                                "completed_games": len(results),
                                "failed_games": len(errors),
                                "attempted_games": attempt_index,
                                "max_attempts": max_attempts,
                                "current_game_index": index,
                            },
                        },
                    )
                result = engine.run()
                report = evaluate_game(result)
                if progress_callback and self.enable_llm_review:
                    progress_callback(
                        "review_started",
                        {
                            "batch": batch,
                            "game_id": result.game_id,
                            "version_id": version_id,
                        },
                    )
                llm_review = review_game_with_llm(
                    result,
                    report,
                    llm_provider=self.llm_provider,
                    llm_model=self.llm_model,
                    enabled=self.enable_llm_review,
                )
                if llm_review is not None:
                    llm_reviews.append(llm_review)
                    bank_item_ids = save_review_memory_candidates(
                        llm_review,
                        version_id=version_id,
                        game_id=result.game_id,
                        batch=batch,
                    )
                    if bank_item_ids:
                        llm_review["memory_bank_item_ids"] = bank_item_ids
                    if progress_callback:
                        progress_callback(
                            "review_completed",
                            {
                                "batch": batch,
                                "game_id": result.game_id,
                                "version_id": version_id,
                                "llm_review": llm_review,
                            },
                        )
                results.append(result)
                reports.append(report)
                payload = build_game_payload(
                    result,
                    report,
                    profiles,
                    llm_provider=self.llm_provider,
                    llm_model=self.llm_model,
                    batch=batch,
                    llm_review=llm_review,
                )
                if self.archive_games:
                    payload["archive_path"] = archive_game_payload(payload)
                    archive_paths.append(str(payload["archive_path"]))
                if progress_callback:
                    progress_callback(
                        "game_completed",
                        {
                            **payload,
                            "progress": {
                                "requested_games": games,
                                "completed_games": len(results),
                                "failed_games": len(errors),
                                "attempted_games": attempt_index,
                                "max_attempts": max_attempts,
                                "current_game_index": index,
                            },
                        },
                    )
                _maybe_wait_between_games(
                    completed_games=len(results),
                    requested_games=games,
                    progress_callback=progress_callback,
                    version_id=version_id,
                    batch=batch,
                )
            except Exception as exc:
                error: dict[str, Any] = {
                    **batch,
                    "agent_backend": "llm",
                    "llm_provider": self.llm_provider,
                    "llm_model": self.llm_model,
                    "error": type(exc).__name__,
                    "message": str(exc),
                }
                if self.archive_games:
                    error["archive_path"] = archive_error_payload(error)
                errors.append(error)
                if progress_callback:
                    progress_callback(
                        "game_failed",
                        {
                            **error,
                            "progress": {
                                "requested_games": games,
                                "completed_games": len(results),
                                "failed_games": len(errors),
                                "attempted_games": attempt_index,
                                "max_attempts": max_attempts,
                                "current_game_index": index,
                            },
                        },
                    )
                if _is_fatal_llm_error(exc):
                    if progress_callback:
                        progress_callback(
                            "batch_aborted",
                            {
                                "version_id": version_id,
                                "reason": "fatal_llm_error",
                                "error": error,
                                "progress": {
                                    "requested_games": games,
                                    "completed_games": len(results),
                                    "failed_games": len(errors),
                                    "attempted_games": attempt_index,
                                    "max_attempts": max_attempts,
                                    "current_game_index": index,
                                },
                            },
                        )
                    raise RuntimeError(
                        f"Batch aborted for {version_id} after fatal LLM error: "
                        f"{type(exc).__name__} {exc}"
                    ) from exc
                delay = _retry_delay_for_exception(exc)
                if delay > 0 and len(results) < games and attempt_index < max_attempts:
                    if progress_callback:
                        progress_callback(
                            "game_retry_wait",
                            {
                                "version_id": version_id,
                                "delay_seconds": delay,
                                "error": error,
                                "progress": {
                                    "requested_games": games,
                                    "completed_games": len(results),
                                    "failed_games": len(errors),
                                    "attempted_games": attempt_index,
                                    "max_attempts": max_attempts,
                                    "current_game_index": index,
                                },
                            },
                        )
                    time.sleep(delay)

        if not reports:
            first_error = errors[0] if errors else {}
            raise RuntimeError(
                f"No completed games for {version_id}; {len(errors)} games failed. "
                f"First error: {first_error.get('error', 'unknown')} {first_error.get('message', '')}"
            )

        return {
            "version_id": version_id,
            "games": results,
            "reports": reports,
            "aggregate": aggregate_reports(reports),
            "llm_reviews": llm_reviews,
            "archive_paths": archive_paths,
            "errors": errors,
            "requested_games": games,
            "completed_games": len(results),
            "completed_seeds": [result.seed for result in results],
            "attempted_games": attempt_index,
            "max_attempts": max_attempts,
        }

    def _ensure_llm_preflight(self, progress_callback: Callable[[str, dict[str, Any]], None] | None = None) -> dict[str, Any]:
        if self._llm_preflight is not None:
            return self._llm_preflight
        if not _env_bool("LLM_PREFLIGHT_ENABLED", True):
            self._llm_preflight = {"status": "skipped", "reason": "LLM_PREFLIGHT_ENABLED=false"}
            return self._llm_preflight

        if progress_callback:
            progress_callback(
                "llm_preflight_started",
                {"llm_provider": self.llm_provider, "llm_model": self.llm_model},
            )
        try:
            client = build_chat_client_from_env(self.llm_provider, self.llm_model)
            if client is None:
                raise RuntimeError(f"No chat client configured for provider={self.llm_provider!r}")
            started = time.perf_counter()
            content = client.chat(
                [
                    {"role": "system", "content": "你是健康检查助手。"},
                    {"role": "user", "content": "只输出 OK。"},
                ],
                temperature=0.0,
                max_tokens=16,
            )
            self._llm_preflight = {
                "status": "ok",
                "llm_provider": client.config.provider,
                "llm_model": client.config.model,
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                "response_preview": str(content).strip()[:40],
            }
            if progress_callback:
                progress_callback("llm_preflight_completed", self._llm_preflight)
            return self._llm_preflight
        except Exception as exc:
            self._llm_preflight = {
                "status": "failed",
                "llm_provider": self.llm_provider,
                "llm_model": self.llm_model,
                "error": type(exc).__name__,
                "message": str(exc),
            }
            if progress_callback:
                progress_callback("llm_preflight_failed", self._llm_preflight)
            raise RuntimeError(
                f"LLM preflight failed for provider={self.llm_provider}, model={self.llm_model}: "
                f"{type(exc).__name__} {exc}"
            ) from exc

    def _plan_next_evolution(
        self,
        *,
        baseline_aggregate: dict[str, Any],
        candidate_aggregate: dict[str, Any] | None,
        current_profiles: dict[Role, StrategyVersion],
        candidate_profiles: dict[Role, StrategyVersion] | None,
        baseline_reports: list[EvaluationReport],
        candidate_reports: list[EvaluationReport] | None = None,
        baseline_reviews: list[dict[str, Any]] | None = None,
        candidate_reviews: list[dict[str, Any]] | None = None,
        promotion: dict[str, Any] | None = None,
        purpose: str,
        version_id: str,
        progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        enabled = (
            self.enable_strategy_planning
            and self.enable_llm_review
            and _env_bool("LLM_STRATEGIST_ENABLED", True)
        )
        if progress_callback:
            progress_callback(
                "strategist_started",
                {
                    "version_id": version_id,
                    "purpose": purpose,
                    "enabled": enabled,
                    "llm_provider": self.llm_provider,
                    "llm_model": self.llm_model,
                },
            )
        plan = plan_evolution_with_llm(
            baseline_aggregate=baseline_aggregate,
            candidate_aggregate=candidate_aggregate,
            current_profiles=current_profiles,
            candidate_profiles=candidate_profiles,
            baseline_reports=baseline_reports,
            candidate_reports=candidate_reports or [],
            baseline_reviews=baseline_reviews or [],
            candidate_reviews=candidate_reviews or [],
            promotion=promotion,
            llm_provider=self.llm_provider,
            llm_model=self.llm_model,
            enabled=enabled,
            purpose=purpose,
        )
        if progress_callback:
            progress_callback(
                "strategist_completed",
                {
                    "version_id": version_id,
                    "purpose": purpose,
                    "plan": plan,
                },
            )
        return plan

    def evolve_profiles(
        self,
        base_profiles: dict[Role, StrategyVersion],
        reports: list[EvaluationReport],
        *,
        version_id: str,
        llm_reviews: list[dict[str, Any]] | None = None,
        strategist_plan: dict[str, Any] | None = None,
    ) -> dict[Role, StrategyVersion]:
        aggregate = aggregate_reports(reports)
        recommended = aggregate.get("recommendations", {})
        review_memories = _review_memory_candidates_by_role(llm_reviews or [])
        planned_memories = strategist_memory_updates(strategist_plan)
        planned_skills = strategist_skill_cards(strategist_plan)
        role_skills = (
            build_role_skills(reports, llm_reviews or [])
            if self.evolution_mode == SKILL_EVOLUTION_MODE
            else {role: [] for role in Role}
        )
        candidate = evolved_strategy_versions(base_profiles)
        result: dict[Role, StrategyVersion] = {}

        for role, profile in candidate.items():
            memories = list(profile.strategy_memory)
            for recommendation in recommended.get(role.value, []):
                memories.append(f"复盘记忆：{recommendation}")
            for memory in review_memories.get(role.value, []):
                memories.append(f"上帝视角复盘：{memory}")
            for memory in planned_memories.get(role, []):
                memories.append(memory)
            params = dict(profile.parameters)
            params["decision_mode"] = "llm_only"
            params["evolution_mode"] = self.evolution_mode
            params["last_reviewed_mistakes"] = sum(aggregate.get("mistake_counts", {}).values())
            if strategist_plan and strategist_plan.get("plan_status") == "ok":
                role_plan = (strategist_plan.get("role_plans") or {}).get(role.value, {}) or {}
                params["last_strategist_plan_id"] = strategist_plan.get("plan_id")
                params["last_strategist_focus"] = role_plan.get("focus")
            if self.evolution_mode == SKILL_EVOLUTION_MODE:
                learned_skills = list(role_skills.get(role, [])) + list(planned_skills.get(role, []))
                skill_evolution = evolve_skill_cards(
                    params.get("role_skills", []),
                    learned_skills,
                    max_items=8,
                    version_id=version_id,
                )
                merged_skills = skill_evolution["skills"]
                skill_operations = skill_evolution["operations"]
                params["role_skills"] = merged_skills
                params["skill_count"] = len(merged_skills)
                params["skill_evolution_summary"] = skill_evolution["summary"]
                params["skill_evolution_operations"] = skill_operations
                params["skill_history"] = (list(params.get("skill_history", [])) + skill_operations)[-40:]
                params["skill_evolution_note"] = (
                    "先根据当前 PrivateObservation 判断技能触发条件，再自主决定是否调用；技能不是强制命令。"
                )
                for skill in learned_skills[:4]:
                    memories.append(f"自主技能：{format_skill_for_memory(skill)}")
                for operation in skill_operations[:4]:
                    op = operation.get("operation")
                    title = operation.get("title")
                    version = operation.get("skill_version")
                    memories.append(f"Skill evolution {op}: {title} v{version}")

            result[role] = replace(
                profile,
                version_id=version_id,
                strategy_memory=_dedupe(memories),
                parameters=params,
                parent_id=base_profiles[role].version_id,
                promoted=False,
            )
        return result

    def run_evolution(
        self,
        *,
        rounds: int = 2,
        games_per_round: int = 20,
        base_version: str = "initial",
        progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        requested_base_version = base_version or "initial"
        base_version_id, current_profiles, base_memory_file = self._resolve_base_profiles(requested_base_version)
        base_round_number = _version_round_number(base_version_id)
        current_version_id = base_version_id
        memory_files: list[dict[str, str]] = []
        if progress_callback:
            progress_callback(
                "version_started",
                {
                    "version_id": base_version_id,
                    "requested_base_version": requested_base_version,
                    "base_version": base_version_id,
                    "base_memory_file": base_memory_file,
                    "round_index": 0,
                    "rounds": rounds,
                    "games_per_round": games_per_round,
                    "evolution_mode": self.evolution_mode,
                },
            )
        baseline = self.run_batch(
            profiles=current_profiles,
            version_id=base_version_id,
            games=games_per_round,
            seed_offset=base_round_number * 10_000,
            progress_callback=progress_callback,
        )

        baseline_reflections = self._reflections_from_reports(baseline["reports"], baseline.get("llm_reviews", []))
        baseline_memory = save_role_memory_snapshot(
            version_id=base_version_id,
            profiles=current_profiles,
            aggregate=baseline["aggregate"],
            reflections=baseline_reflections,
            source_archives=baseline.get("archive_paths", []),
            errors=baseline.get("errors", []),
            parent_id=_common_parent_id(current_profiles),
            promoted=True,
            llm_provider=self.llm_provider,
            llm_model=self.llm_model,
            round_index=0,
            llm_reviews=baseline.get("llm_reviews", []),
        )
        memory_files.append(baseline_memory)
        history: list[dict[str, Any]] = [
            {
                "version_id": base_version_id,
                "parent_id": _common_parent_id(current_profiles),
                "aggregate": baseline["aggregate"],
                "promoted": True,
                "reflections": baseline_reflections,
                "archive_paths": baseline.get("archive_paths", []),
                "errors": baseline.get("errors", []),
                "llm_reviews": baseline.get("llm_reviews", []),
                "requested_games": baseline.get("requested_games", games_per_round),
                "completed_games": baseline.get("completed_games", len(baseline["reports"])),
                "attempted_games": baseline.get("attempted_games", baseline.get("completed_games", len(baseline["reports"]))),
                "memory_file": baseline_memory["latest_path"],
                "memory_snapshot": baseline_memory["snapshot_path"],
            }
        ]
        current_reports = baseline["reports"]
        current_llm_reviews = baseline.get("llm_reviews", [])
        current_score = baseline["aggregate"]["average_scores"].get("overall", 0.0)
        current_mistakes = baseline["aggregate"].get("mistakes_per_game", 99.0)
        if progress_callback:
            progress_callback(
                "version_completed",
                {
                    "version_id": base_version_id,
                    "requested_base_version": requested_base_version,
                    "base_version": base_version_id,
                    "base_memory_file": base_memory_file,
                    "round_index": 0,
                    "aggregate": baseline["aggregate"],
                    "promoted": True,
                    "archive_paths": baseline.get("archive_paths", []),
                    "errors": baseline.get("errors", []),
                    "llm_reviews": baseline.get("llm_reviews", []),
                    "completed_games": baseline.get("completed_games", len(baseline["reports"])),
                    "attempted_games": baseline.get("attempted_games", baseline.get("completed_games", len(baseline["reports"]))),
                    "requested_games": baseline.get("requested_games", games_per_round),
                    "reflections": baseline_reflections,
                    "memory_file": baseline_memory["latest_path"],
                    "memory_snapshot": baseline_memory["snapshot_path"],
                },
            )

        pending_strategy_plan: dict[str, Any] = {"plan_status": "skipped", "reason": "no_candidate_rounds"}
        if rounds > 0:
            pending_strategy_plan = self._plan_next_evolution(
                baseline_aggregate=baseline["aggregate"],
                candidate_aggregate=None,
                current_profiles=current_profiles,
                candidate_profiles=None,
                baseline_reports=current_reports,
                candidate_reports=[],
                baseline_reviews=current_llm_reviews,
                candidate_reviews=[],
                promotion=None,
                purpose="baseline_to_candidate",
                version_id=base_version_id,
                progress_callback=progress_callback,
            )
            history[0]["strategist_plan"] = pending_strategy_plan

        for round_index in range(1, rounds + 1):
            version_number = base_round_number + round_index
            version_id = _candidate_version_id(version_number, self.evolution_mode)
            parent_id = current_version_id
            parent_profiles = current_profiles
            parent_reports = current_reports
            parent_llm_reviews = current_llm_reviews
            parent_aggregate = aggregate_reports(parent_reports)
            input_strategy_plan = pending_strategy_plan
            candidate_profiles = self.evolve_profiles(
                current_profiles,
                current_reports,
                version_id=version_id,
                llm_reviews=current_llm_reviews,
                strategist_plan=input_strategy_plan,
            )
            if progress_callback:
                progress_callback(
                    "version_started",
                    {
                        "version_id": version_id,
                        "parent_id": parent_id,
                        "base_version": base_version_id,
                        "round_index": round_index,
                        "version_number": version_number,
                        "rounds": rounds,
                        "games_per_round": games_per_round,
                        "evolution_mode": self.evolution_mode,
                    },
                )
            candidate = self.run_batch(
                profiles=candidate_profiles,
                version_id=version_id,
                games=games_per_round,
                seed_offset=version_number * 10_000,
                progress_callback=progress_callback,
            )
            candidate_score = candidate["aggregate"]["average_scores"].get("overall", 0.0)
            candidate_mistakes = candidate["aggregate"].get("mistakes_per_game", 99.0)
            promotion = promotion_decision(
                current_score=current_score,
                current_mistakes=current_mistakes,
                candidate_score=candidate_score,
                candidate_mistakes=candidate_mistakes,
            )
            promoted = bool(promotion["promoted"])

            if promoted:
                current_profiles = {
                    role: replace(profile, promoted=True) for role, profile in candidate_profiles.items()
                }
                current_reports = candidate["reports"]
                current_llm_reviews = candidate.get("llm_reviews", [])
                current_score = candidate_score
                current_mistakes = candidate_mistakes
                current_version_id = version_id

            round_strategy_plan = self._plan_next_evolution(
                baseline_aggregate=parent_aggregate,
                candidate_aggregate=candidate["aggregate"],
                current_profiles=parent_profiles,
                candidate_profiles=candidate_profiles,
                baseline_reports=parent_reports,
                candidate_reports=candidate["reports"],
                baseline_reviews=parent_llm_reviews,
                candidate_reviews=candidate.get("llm_reviews", []),
                promotion=promotion,
                purpose="post_candidate_comparison",
                version_id=version_id,
                progress_callback=progress_callback,
            )
            pending_strategy_plan = round_strategy_plan

            candidate_reflections = self._reflections_from_reports(candidate["reports"], candidate.get("llm_reviews", []))
            saved_profiles = current_profiles if promoted else candidate_profiles
            candidate_memory = save_role_memory_snapshot(
                version_id=version_id,
                profiles=saved_profiles,
                aggregate=candidate["aggregate"],
                reflections=candidate_reflections,
                source_archives=candidate.get("archive_paths", []),
                errors=candidate.get("errors", []),
                parent_id=parent_id,
                promoted=promoted,
                llm_provider=self.llm_provider,
                llm_model=self.llm_model,
                round_index=round_index,
                llm_reviews=candidate.get("llm_reviews", []),
            )
            memory_files.append(candidate_memory)
            history.append(
                {
                    "version_id": version_id,
                    "parent_id": parent_id,
                    "aggregate": candidate["aggregate"],
                    "promoted": promoted,
                    "promotion": promotion,
                    "input_strategist_plan": input_strategy_plan,
                    "strategist_plan": round_strategy_plan,
                    "reflections": candidate_reflections,
                    "strategy_versions": saved_profiles,
                    "archive_paths": candidate.get("archive_paths", []),
                    "errors": candidate.get("errors", []),
                    "llm_reviews": candidate.get("llm_reviews", []),
                    "requested_games": candidate.get("requested_games", games_per_round),
                    "completed_games": candidate.get("completed_games", len(candidate["reports"])),
                    "attempted_games": candidate.get("attempted_games", candidate.get("completed_games", len(candidate["reports"]))),
                    "memory_file": candidate_memory["latest_path"],
                    "memory_snapshot": candidate_memory["snapshot_path"],
                }
            )
            if progress_callback:
                progress_callback(
                    "version_completed",
                    {
                        "version_id": version_id,
                        "parent_id": parent_id,
                        "base_version": base_version_id,
                        "round_index": round_index,
                        "version_number": version_number,
                        "aggregate": candidate["aggregate"],
                        "promoted": promoted,
                        "promotion": promotion,
                        "input_strategist_plan": input_strategy_plan,
                        "strategist_plan": round_strategy_plan,
                        "archive_paths": candidate.get("archive_paths", []),
                        "errors": candidate.get("errors", []),
                        "llm_reviews": candidate.get("llm_reviews", []),
                        "completed_games": candidate.get("completed_games", len(candidate["reports"])),
                        "attempted_games": candidate.get("attempted_games", candidate.get("completed_games", len(candidate["reports"]))),
                        "requested_games": candidate.get("requested_games", games_per_round),
                        "reflections": candidate_reflections,
                        "memory_file": candidate_memory["latest_path"],
                        "memory_snapshot": candidate_memory["snapshot_path"],
                    },
                )

        final_version = current_version_id
        final_history = _history_item(history, final_version)
        final_memory = _memory_file(memory_files, final_version)
        result = {
            "history": _strip_profiles(history),
            "leaderboard": self._leaderboard(history),
            "final_version": final_version,
            "final_profiles": current_profiles,
            "requested_base_version": requested_base_version,
            "base_version": base_version_id,
            "base_memory_file": base_memory_file,
            "baseline_report_sample": baseline["reports"][0] if baseline["reports"] else None,
            "memory_files": memory_files,
            "memory_bank_file": str(MEMORY_BANK_PATH),
            "player_profiles_file": str(PLAYER_PROFILES_PATH),
            "llm_provider": self.llm_provider,
            "llm_model": self.llm_model,
            "evolution_mode": self.evolution_mode,
        }
        result["memory_run_file"] = save_evolution_run(result)
        result["latest_promoted_file"] = save_latest_promoted(
            version_id=final_version,
            memory_file=final_memory.get("latest_path") or final_history.get("memory_file"),
            snapshot_path=final_memory.get("snapshot_path") or final_history.get("memory_snapshot"),
            run_file=result["memory_run_file"],
            aggregate=final_history.get("aggregate", {}),
            base_version=base_version_id,
            llm_provider=self.llm_provider,
            llm_model=self.llm_model,
        )
        update_evolution_run(result["memory_run_file"], result)
        return result

    def _resolve_base_profiles(
        self, requested_base_version: str
    ) -> tuple[str, dict[Role, StrategyVersion], str | None]:
        requested = (requested_base_version or "initial").strip() or "initial"
        if requested in {"latest", "latest_promoted", "promoted"}:
            latest = load_latest_promoted()
            if latest:
                version_id = str(latest.get("version_id") or "initial")
                profiles = load_role_memory_profiles(version_id)
                if profiles:
                    return version_id, profiles, latest.get("memory_file")
            return "initial", default_strategy_versions("initial"), None

        persisted = load_role_memory_profiles(requested)
        if persisted:
            return requested, persisted, None

        if requested == "evolved":
            persisted_evolved = load_role_memory_profiles("evolved_r1")
            if persisted_evolved:
                return "evolved_r1", persisted_evolved, None
            return "evolved", evolved_strategy_versions(default_strategy_versions("initial")), None

        if requested.startswith(("evolved_r", "evolved_workflow_r", "evolved_skill_r")):
            return requested, evolved_strategy_versions(default_strategy_versions("initial")), None

        return "initial", default_strategy_versions("initial"), None

    def ab_test(self, *, games: int = 20) -> dict[str, Any]:
        initial_profiles = default_strategy_versions("initial")
        evolved_profiles = evolved_strategy_versions(initial_profiles)
        initial = self.run_batch(profiles=initial_profiles, version_id="initial", games=games, seed_offset=30_000)
        evolved = self.run_batch(profiles=evolved_profiles, version_id="evolved", games=games, seed_offset=30_000)
        evolved_good_profiles = dict(initial_profiles)
        for role in (Role.SEER, Role.WITCH, Role.HUNTER, Role.VILLAGER):
            evolved_good_profiles[role] = evolved_profiles[role]
        evolved_wolf_profiles = dict(initial_profiles)
        evolved_wolf_profiles[Role.WEREWOLF] = evolved_profiles[Role.WEREWOLF]
        evolved_good = self.run_batch(
            profiles=evolved_good_profiles,
            version_id="evolved_good_vs_initial_wolves",
            games=games,
            seed_offset=40_000,
        )
        evolved_wolf = self.run_batch(
            profiles=evolved_wolf_profiles,
            version_id="evolved_wolves_vs_initial_good",
            games=games,
            seed_offset=50_000,
        )
        initial_score = initial["aggregate"]["average_scores"].get("overall", 0.0)
        evolved_score = evolved["aggregate"]["average_scores"].get("overall", 0.0)
        initial_mistakes = initial["aggregate"].get("mistakes_per_game", 0.0)
        evolved_mistakes = evolved["aggregate"].get("mistakes_per_game", 0.0)
        return {
            "games": games,
            "initial": initial["aggregate"],
            "evolved": evolved["aggregate"],
            "delta": {
                "overall": round(evolved_score - initial_score, 2),
                "mistakes_per_game": round(evolved_mistakes - initial_mistakes, 2),
                "villager_win_rate": round(
                    _win_rate(evolved["aggregate"], "villagers") - _win_rate(initial["aggregate"], "villagers"),
                    3,
                ),
                "werewolf_win_rate": round(
                    _win_rate(evolved["aggregate"], "werewolves") - _win_rate(initial["aggregate"], "werewolves"),
                    3,
                ),
            },
            "matchups": {
                "evolved_good_vs_initial_wolves": {
                    "aggregate": evolved_good["aggregate"],
                    "evolved_side_win_rate": _win_rate(evolved_good["aggregate"], "villagers"),
                },
                "evolved_wolves_vs_initial_good": {
                    "aggregate": evolved_wolf["aggregate"],
                    "evolved_side_win_rate": _win_rate(evolved_wolf["aggregate"], "werewolves"),
                },
            },
            "archives": {
                "initial": initial.get("archive_paths", []),
                "evolved": evolved.get("archive_paths", []),
                "evolved_good_vs_initial_wolves": evolved_good.get("archive_paths", []),
                "evolved_wolves_vs_initial_good": evolved_wolf.get("archive_paths", []),
            },
            "errors": {
                "initial": initial.get("errors", []),
                "evolved": evolved.get("errors", []),
                "evolved_good_vs_initial_wolves": evolved_good.get("errors", []),
                "evolved_wolves_vs_initial_good": evolved_wolf.get("errors", []),
            },
            "completed_games": {
                "initial": initial.get("completed_games", 0),
                "evolved": evolved.get("completed_games", 0),
                "evolved_good_vs_initial_wolves": evolved_good.get("completed_games", 0),
                "evolved_wolves_vs_initial_good": evolved_wolf.get("completed_games", 0),
            },
            "interpretation": (
                "进化版通过策略记忆强化查杀披露、归票和药水纪律。"
                " 若 overall 为正且 mistakes_per_game 为负，说明进化有效降低关键失误。"
            ),
        }

    def _reflections_from_reports(
        self,
        reports: list[EvaluationReport],
        llm_reviews: list[dict[str, Any]] | None = None,
    ) -> dict[str, list[str]]:
        aggregate = aggregate_reports(reports)
        reflections: dict[str, list[str]] = {}
        llm_reflections = _review_reflections_by_role(llm_reviews or [])
        for role in Role:
            items = aggregate.get("recommendations", {}).get(role.value, [])
            if not items:
                items = ["保持当前策略，继续记录可解释发言、投票和技能理由。"]
            combined = [f"规则复盘：{item}" for item in items[:3]]
            combined.extend(f"LLM复盘：{item}" for item in llm_reflections.get(role.value, [])[:3])
            reflections[role.value] = _dedupe(combined)[:5]
        return reflections

    def _leaderboard(self, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows = []
        for item in history:
            aggregate = item["aggregate"]
            rows.append(
                {
                    "version_id": item["version_id"],
                    "promoted": item["promoted"],
                    "overall": aggregate.get("average_scores", {}).get("overall", 0.0),
                    "vote_quality": aggregate.get("average_scores", {}).get("vote_quality", 0.0),
                    "skill_quality": aggregate.get("average_scores", {}).get("skill_quality", 0.0),
                    "mistakes_per_game": aggregate.get("mistakes_per_game", 0.0),
                    "villager_wins": aggregate.get("winner_counts", {}).get("villagers", 0),
                    "werewolf_wins": aggregate.get("winner_counts", {}).get("werewolves", 0),
                }
            )
        return sorted(rows, key=lambda row: (row["overall"], -row["mistakes_per_game"]), reverse=True)


def _serialize_player(player) -> dict[str, Any]:
    return {
        **player.public_view(True),
        "role": player.role.value,
        "role_zh": player.role.zh,
        "faction": player.faction.value,
        "faction_zh": player.faction.zh,
        "agent_version": player.agent_version,
    }


def _live_summary(engine: GameEngine) -> dict[str, Any]:
    alive = [player for player in engine.players if player.alive]
    wolves_alive = [player for player in alive if player.role == Role.WEREWOLF]
    civilians_alive = [player for player in alive if player.role == Role.VILLAGER]
    gods_alive = [player for player in alive if player.role in {Role.SEER, Role.WITCH, Role.HUNTER}]
    return {
        "day": engine.day,
        "alive_count": len(alive),
        "dead_count": len(engine.players) - len(alive),
        "wolves_alive": len(wolves_alive),
        "civilians_alive": len(civilians_alive),
        "gods_alive": len(gods_alive),
        "winner": engine.winner.value if engine.winner else None,
        "win_reason": engine.win_reason,
        "players": [_serialize_player(player) for player in engine.players],
    }


def _win_rate(aggregate: dict[str, Any], faction: str) -> float:
    games = max(1, int(aggregate.get("games", 0)))
    return aggregate.get("winner_counts", {}).get(faction, 0) / games


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _review_memory_candidates_by_role(reviews: list[dict[str, Any]]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {role.value: [] for role in Role}
    for review in reviews:
        if review.get("review_status") != "ok":
            continue
        for item in review.get("memory_candidates", []) or []:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "")
            memory = str(item.get("memory") or "").strip()
            confidence = _bounded_float(item.get("confidence"), default=0.0)
            if role in grouped and memory and confidence >= 0.55:
                grouped[role].append(memory)
    return {role: _dedupe(items)[:6] for role, items in grouped.items()}


def _review_reflections_by_role(reviews: list[dict[str, Any]]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {role.value: [] for role in Role}
    for review in reviews:
        if review.get("review_status") != "ok":
            continue
        raw = review.get("role_reflections", {})
        if not isinstance(raw, dict):
            continue
        for role in Role:
            for item in raw.get(role.value, []) or []:
                text = str(item or "").strip()
                if text:
                    grouped[role.value].append(text)
    return {role: _dedupe(items)[:6] for role, items in grouped.items()}


def _bounded_float(value: Any, *, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(0.0, min(1.0, parsed))


def _env_bool(key: str, default: bool) -> bool:
    import os

    value = os.environ.get(key)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(key: str, default: int) -> int:
    import os

    value = os.environ.get(key)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    import os

    value = os.environ.get(key)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _is_fatal_llm_error(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    fatal_tokens = (
        "authenticationerror",
        "unauthorized",
        " http 401",
        "api key doesn't exist",
        "invalid api key",
        "permissiondenied",
        "forbidden",
        " http 403",
    )
    return any(token in text for token in fatal_tokens)


def _retry_delay_for_exception(exc: Exception) -> float:
    text = f"{type(exc).__name__}: {exc}".lower()
    if "429" in text or "ratelimit" in text or "rate limit" in text or "tpm" in text or "rpm" in text:
        return max(
            0.0,
            _env_float("EVOLUTION_RATE_LIMIT_WAIT_SECONDS", 0.0),
            rate_limit_retry_delay_from_text(text, 0),
        )
    if "timeout" in text or "timed out" in text or "remotedisconnected" in text or "connection reset" in text:
        return max(0.0, _env_float("EVOLUTION_RETRY_WAIT_SECONDS", 4.0))
    if "jsondecodeerror" in text or "json parse" in text or "invalid control character" in text:
        return max(0.0, _env_float("EVOLUTION_JSON_RETRY_WAIT_SECONDS", 1.0))
    return max(0.0, _env_float("EVOLUTION_RETRY_WAIT_SECONDS", 0.0))


def _maybe_wait_between_games(
    *,
    completed_games: int,
    requested_games: int,
    progress_callback: Callable[[str, dict[str, Any]], None] | None,
    version_id: str,
    batch: dict[str, Any],
) -> None:
    delay = max(0.0, _env_float("EVOLUTION_GAME_COOLDOWN_SECONDS", 0.0))
    if delay <= 0 or completed_games >= requested_games:
        return
    if progress_callback:
        progress_callback(
            "game_cooldown",
            {
                "version_id": version_id,
                "delay_seconds": delay,
                "batch": batch,
                "progress": {
                    "requested_games": requested_games,
                    "completed_games": completed_games,
                    "failed_games": None,
                },
            },
        )
    time.sleep(delay)


def _candidate_version_id(version_number: int, evolution_mode: str) -> str:
    mode = normalize_evolution_mode(evolution_mode)
    base_id = f"evolved_{mode}_r{version_number}"
    if not _version_memory_exists(base_id):
        return base_id
    suffix = 2
    while _version_memory_exists(f"{base_id}_v{suffix}"):
        suffix += 1
    return f"{base_id}_v{suffix}"


def _version_memory_exists(version_id: str) -> bool:
    return (MEMORY_DIR / f"role_memory_{_safe_version_name(version_id)}.json").exists()


def _safe_version_name(version_id: str) -> str:
    return version_id.replace("/", "_").replace("\\", "_").replace(" ", "_")


def _version_round_number(version_id: str) -> int:
    if version_id == "evolved":
        return 1
    match = re.search(r"evolved(?:_[a-z]+)?_r(\d+)", version_id)
    if not match:
        return 0
    return max(0, int(match.group(1)))


def _common_parent_id(profiles: dict[Role, StrategyVersion]) -> str | None:
    parent_ids = {profile.parent_id for profile in profiles.values() if profile.parent_id}
    if len(parent_ids) == 1:
        return next(iter(parent_ids))
    return None


def _history_item(history: list[dict[str, Any]], version_id: str) -> dict[str, Any]:
    for item in reversed(history):
        if item["version_id"] == version_id:
            return item
    return history[-1] if history else {}


def _memory_file(memory_files: list[dict[str, str]], version_id: str) -> dict[str, str]:
    for item in reversed(memory_files):
        if item.get("version_id") == version_id:
            return item
    return {}


def _last_promoted(history: list[dict[str, Any]]) -> str:
    for item in reversed(history):
        if item["promoted"]:
            return item["version_id"]
    return "initial"


def _strip_profiles(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    stripped = []
    for item in history:
        data = {key: value for key, value in item.items() if key != "strategy_versions"}
        stripped.append(data)
    return stripped
