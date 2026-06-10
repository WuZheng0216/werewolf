from __future__ import annotations

import os
from collections import Counter
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any

from .evolution import EvolutionManager
from .models import Role, StrategyVersion, to_jsonable
from .service import get_profiles
from .skill_evolution import SKILL_EVOLUTION_MODE


def run_codex_skill_lab_batch(
    *,
    version: str = "latest",
    games: int = 5,
    llm_provider: str = "ark",
    llm_model: str | None = None,
    seed_offset: int = 80_000,
    skip_preflight: bool = True,
    internal_llm_retries: int | None = 1,
    progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Run Doubao games for Codex-side skill review without auto-evolving skills.

    This mode is intentionally different from EvolutionManager.run_evolution:
    it preserves the current role skills during gameplay, but disables LLM
    review, strategist planning, memory-bank writes, role-memory snapshots, and
    latest promotion. The output is an evidence pack for a human/Codex review.
    """

    profiles = get_profiles(version)
    version_id = _profile_version_id(profiles, fallback=version)
    manager = EvolutionManager(
        llm_provider=llm_provider,
        llm_model=llm_model,
        archive_games=True,
        enable_llm_review=False,
        enable_strategy_planning=False,
        evolution_mode=SKILL_EVOLUTION_MODE,
    )
    if progress_callback:
        progress_callback(
            "codex_skill_lab_started",
            {
                "mode": "codex_skill_lab",
                "version": version,
                "version_id": version_id,
                "games": games,
                "seed_offset": seed_offset,
                "llm_provider": llm_provider,
                "llm_model": llm_model,
                "skip_preflight": skip_preflight,
                "internal_llm_retries": internal_llm_retries,
                "writes_disabled": _writes_disabled(),
            },
        )
    with (
        _temporary_env("LLM_PREFLIGHT_ENABLED", "false" if skip_preflight else None),
        _temporary_env("ARK_MAX_RETRIES", str(internal_llm_retries) if internal_llm_retries is not None else None),
    ):
        batch = manager.run_batch(
            profiles=profiles,
            version_id=version_id,
            games=games,
            seed_offset=seed_offset,
            progress_callback=progress_callback,
        )
    payload = {
        "mode": "codex_skill_lab",
        "codex_review_required": True,
        "review_owner": "codex",
        "experiment_owner": "doubao",
        "version": version,
        "version_id": version_id,
        "games": games,
        "seed_offset": seed_offset,
        "aggregate": batch["aggregate"],
        "archives": batch.get("archive_paths", []),
        "errors": batch.get("errors", []),
        "completed_games": batch.get("completed_games", 0),
        "attempted_games": batch.get("attempted_games", 0),
        "requested_games": batch.get("requested_games", games),
        "llm_provider": llm_provider,
        "llm_model": llm_model,
        "skip_preflight": skip_preflight,
        "internal_llm_retries": internal_llm_retries,
        "writes_disabled": _writes_disabled(),
        "current_skill_summary": summarize_profile_skills(profiles),
        "codex_review_protocol": [
            "Read aggregate scores and top mistake types before editing any skill.",
            "Inspect 2-5 archived games that support the target failure pattern.",
            "Create a candidate role_memory_<version>.json instead of editing latest_promoted.json.",
            "Keep skill cards as optional decision tools; do not force fixed actions.",
            "Verify candidate with frozen eval or skill ablation before promotion.",
        ],
    }
    if progress_callback:
        progress_callback("codex_skill_lab_completed", payload)
    return to_jsonable(payload)


def summarize_profile_skills(profiles: dict[Role, StrategyVersion]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for role, profile in profiles.items():
        skills = [
            skill
            for skill in (profile.parameters.get("role_skills", []) if isinstance(profile.parameters, dict) else [])
            if isinstance(skill, dict)
        ]
        source_types = Counter(str(skill.get("source_type") or skill.get("source") or "unknown") for skill in skills)
        summary[role.value] = {
            "version_id": profile.version_id,
            "skill_count": len(skills),
            "source_types": dict(source_types),
            "skills": [
                {
                    "skill_id": skill.get("skill_id"),
                    "title": skill.get("title"),
                    "trigger": skill.get("trigger"),
                    "source_type": skill.get("source_type"),
                    "skill_version": skill.get("skill_version"),
                    "support_count": skill.get("support_count"),
                    "confidence": skill.get("confidence"),
                }
                for skill in skills[:12]
            ],
        }
    return summary


def _profile_version_id(profiles: dict[Role, StrategyVersion], *, fallback: str) -> str:
    version_ids = {profile.version_id for profile in profiles.values() if profile.version_id}
    if len(version_ids) == 1:
        return next(iter(version_ids))
    return fallback


def _writes_disabled() -> dict[str, bool]:
    return {
        "llm_review": True,
        "memory_bank": True,
        "strategy_planning": True,
        "role_memory_snapshot": True,
        "latest_promoted": True,
        "auto_skill_evolution": True,
    }


@contextmanager
def _temporary_env(key: str, value: str | None):
    sentinel = object()
    original: object | str = os.environ.get(key, sentinel)
    if value is not None:
        os.environ[key] = value
    try:
        yield
    finally:
        if original is sentinel:
            os.environ.pop(key, None)
        else:
            os.environ[key] = str(original)
