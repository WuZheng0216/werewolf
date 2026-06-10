from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from werewolf_ai.llm import build_chat_client_from_env
from werewolf_ai.memory_store import save_review_memory_candidates
from werewolf_ai.models import (
    ActionType,
    AgentDecision,
    EvaluationReport,
    Faction,
    GameEvent,
    GameResult,
    Phase,
    PlayerState,
    Role,
    game_rule_summary,
    role_faction,
    to_jsonable,
)
from werewolf_ai.reviewer import LLMGameReviewer, _configure_review_client


REVIEWABLE_FAILURE_STATUSES = {None, "", "failed", "skipped"}
REVIEW_BACKFILL_DIR = Path("data") / "memory" / "review_backfills"


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill failed or missing post-game reviews from archived game logs.")
    parser.add_argument("--run-file", type=Path, help="Optional evolution run file whose archive_paths should be backfilled.")
    parser.add_argument("--logs-glob", default="logs/games/*.json", help="Archive glob used when --run-file is not set.")
    parser.add_argument("--max", type=int, default=5, help="Maximum number of failed/missing archives to process.")
    parser.add_argument("--provider", default=None, help="LLM provider, e.g. ark, dashscope, dashscope-native.")
    parser.add_argument("--model", default=None, help="Optional LLM model override.")
    parser.add_argument("--mode", choices=("llm", "heuristic"), default="llm")
    parser.add_argument("--fallback", choices=("none", "heuristic"), default="heuristic")
    parser.add_argument("--timeout-seconds", type=float, default=240.0)
    parser.add_argument("--max-retries", type=int, default=1)
    parser.add_argument("--max-events", type=int, default=60)
    parser.add_argument("--include-ok", action="store_true", help="Re-review archives even when llm_review is already ok.")
    parser.add_argument("--dry-run", action="store_true", help="Do not write archives, run files, or memory bank.")
    args = parser.parse_args()

    archives = _candidate_archives(args.run_file, args.logs_glob)
    selected = _select_archives(archives, include_ok=args.include_ok, limit=args.max)
    if not selected:
        print("No failed or missing reviews found.")
        return 0

    client = None
    if args.mode == "llm":
        provider = args.provider or _infer_provider(selected[0][1]) or "ark"
        model = args.model or selected[0][1].get("llm_model")
        client = build_chat_client_from_env(provider, model)
        if client is None and args.fallback == "none":
            raise SystemExit(f"Could not build LLM client for provider={provider!r}, model={model!r}.")
        if client is not None:
            _configure_review_client(client)
            client.config.timeout_seconds = args.timeout_seconds
            client.config.max_retries = args.max_retries

    processed: list[dict[str, Any]] = []
    review_by_archive: dict[str, dict[str, Any]] = {}
    for path, payload in selected:
        old_review = payload.get("llm_review")
        result = _game_result_from_payload(payload)
        report = _report_from_payload(payload)
        review: dict[str, Any]
        try:
            if args.mode == "heuristic" or client is None:
                review = _heuristic_review(result, report, reason="llm_client_unavailable" if args.mode == "llm" else "requested")
            else:
                review = LLMGameReviewer(client, max_events=args.max_events).review_game(result, report)
        except Exception as exc:
            if args.fallback != "heuristic":
                review = {
                    "review_status": "failed",
                    "error": type(exc).__name__,
                    "message": str(exc),
                    "metadata": {"review_backend": "backfill", "fallback": "none"},
                }
            else:
                review = _heuristic_review(result, report, reason=f"{type(exc).__name__}: {exc}")

        if review.get("review_status") == "ok":
            ids: list[str] = []
            if not args.dry_run:
                ids = save_review_memory_candidates(
                    review,
                    version_id=result.version_id,
                    game_id=result.game_id,
                    batch=(payload.get("batch") if isinstance(payload.get("batch"), dict) else None),
                    archive_path=str(path),
                )
            review["memory_bank_item_ids"] = ids

        stamp = _stamp()
        review["backfill_metadata"] = {
            "backfilled_at": stamp,
            "archive_path": str(path),
            "previous_review_status": _review_status(old_review),
            "dry_run": args.dry_run,
        }
        processed.append(
            {
                "archive_path": str(path),
                "game_id": result.game_id,
                "previous_review_status": _review_status(old_review),
                "new_review_status": review.get("review_status"),
                "review_backend": (review.get("metadata") or {}).get("review_backend"),
                "memory_candidate_count": len(review.get("memory_candidates") or []),
                "memory_bank_item_ids": review.get("memory_bank_item_ids", []),
                "error": review.get("message") if review.get("review_status") != "ok" else None,
            }
        )
        review_by_archive[_norm_path(path)] = review

        if not args.dry_run:
            payload.setdefault("llm_review_backfills", []).append(
                {
                    "backfilled_at": stamp,
                    "previous_review": old_review,
                    "new_review_status": review.get("review_status"),
                }
            )
            payload["llm_review"] = to_jsonable(review)
            _write_json(path, payload)

    if args.run_file and not args.dry_run:
        _update_run_file(args.run_file, review_by_archive)

    summary = {
        "created_at": _stamp(),
        "run_file": str(args.run_file) if args.run_file else None,
        "mode": args.mode,
        "fallback": args.fallback,
        "dry_run": args.dry_run,
        "processed_count": len(processed),
        "ok_count": sum(1 for item in processed if item["new_review_status"] == "ok"),
        "processed": processed,
    }
    if not args.dry_run:
        REVIEW_BACKFILL_DIR.mkdir(parents=True, exist_ok=True)
        summary_path = REVIEW_BACKFILL_DIR / f"backfill_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json"
        _write_json(summary_path, summary)
        summary["summary_path"] = str(summary_path)

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def _candidate_archives(run_file: Path | None, logs_glob: str) -> list[Path]:
    if run_file:
        data = _read_json(run_file)
        paths: list[Path] = []
        for history in data.get("history", []) if isinstance(data.get("history"), list) else []:
            if not isinstance(history, dict):
                continue
            for archive_path in history.get("archive_paths", []) or []:
                if archive_path:
                    paths.append(Path(archive_path))
        return paths
    return sorted(Path(".").glob(logs_glob), key=lambda path: path.stat().st_mtime, reverse=True)


def _select_archives(paths: list[Path], *, include_ok: bool, limit: int) -> list[tuple[Path, dict[str, Any]]]:
    selected: list[tuple[Path, dict[str, Any]]] = []
    seen: set[str] = set()
    for path in paths:
        norm = _norm_path(path)
        if norm in seen or not path.exists():
            continue
        seen.add(norm)
        try:
            payload = _read_json(path)
        except json.JSONDecodeError:
            continue
        if "game" not in payload or "report" not in payload:
            continue
        status = _review_status(payload.get("llm_review"))
        if include_ok or status in REVIEWABLE_FAILURE_STATUSES:
            selected.append((path, payload))
        if len(selected) >= limit:
            break
    return selected


def _game_result_from_payload(payload: dict[str, Any]) -> GameResult:
    game = payload["game"]
    players = [_player_from_dict(player) for player in game.get("players", [])]
    events = [_event_from_dict(event, players) for event in game.get("events", [])]
    return GameResult(
        game_id=str(game.get("game_id") or "unknown-game"),
        seed=int(game.get("seed") or 0),
        version_id=str(game.get("version_id") or "unknown"),
        winner=Faction(str(game.get("winner"))),
        win_reason=str(game.get("win_reason") or ""),
        day=int(game.get("day") or 0),
        players=players,
        events=events,
        observation_samples=list(payload.get("private_observation_proof") or []),
        game_rules=dict(game.get("game_rules") or game_rule_summary()),
    )


def _report_from_payload(payload: dict[str, Any]) -> EvaluationReport:
    report = payload["report"]
    return EvaluationReport(
        game_id=str(report.get("game_id") or payload.get("game", {}).get("game_id") or "unknown-game"),
        winner=Faction(str(report.get("winner"))),
        scores=dict(report.get("scores") or {}),
        role_scores=dict(report.get("role_scores") or {}),
        key_decisions=list(report.get("key_decisions") or []),
        mistakes=list(report.get("mistakes") or []),
        recommendations=dict(report.get("recommendations") or {}),
        summary=str(report.get("summary") or ""),
    )


def _player_from_dict(data: dict[str, Any]) -> PlayerState:
    role = Role(str(data.get("role")))
    return PlayerState(
        id=int(data.get("id") or 0),
        name=str(data.get("name") or f"P{data.get('id')}"),
        role=role,
        faction=Faction(str(data.get("faction") or role_faction(role).value)),
        alive=bool(data.get("alive", True)),
        death_day=data.get("death_day"),
        death_phase=Phase(str(data["death_phase"])) if data.get("death_phase") else None,
        death_reason=data.get("death_reason"),
        agent_version=str(data.get("agent_version") or "unknown"),
    )


def _event_from_dict(data: dict[str, Any], players: list[PlayerState]) -> GameEvent:
    decision_data = data.get("decision")
    return GameEvent(
        id=int(data.get("id") or 0),
        day=int(data.get("day") or 0),
        phase=Phase(str(data.get("phase"))),
        event_type=str(data.get("event_type") or ""),
        public_text=str(data.get("public_text") or ""),
        actor_id=data.get("actor_id"),
        target_id=data.get("target_id"),
        secondary_target_id=data.get("secondary_target_id"),
        public_payload=dict(data.get("public_payload") or {}),
        private_payload=dict(data.get("private_payload") or {}),
        decision=_decision_from_dict(decision_data, players) if isinstance(decision_data, dict) else None,
        observation_proof=data.get("observation_proof"),
        observation_snapshot=data.get("observation_snapshot"),
    )


def _decision_from_dict(data: dict[str, Any], players: list[PlayerState]) -> AgentDecision:
    actor_id = int(data.get("actor_id") or 0)
    actor_role = data.get("role") or next((player.role.value for player in players if player.id == actor_id), Role.VILLAGER.value)
    return AgentDecision(
        actor_id=actor_id,
        role=Role(str(actor_role)),
        action=ActionType(str(data.get("action"))),
        target_id=data.get("target_id"),
        secondary_target_id=data.get("secondary_target_id"),
        speech=str(data.get("speech") or ""),
        reason=str(data.get("reason") or ""),
        confidence=float(data.get("confidence", 0.5)),
        metadata=dict(data.get("metadata") or {}),
    )


def _heuristic_review(result: GameResult, report: EvaluationReport, *, reason: str) -> dict[str, Any]:
    recommendations = {
        role.value: [str(item) for item in (report.recommendations.get(role.value) or [])[:3]]
        for role in Role
    }
    mistakes = report.mistakes[:8]
    key_decisions = report.key_decisions[:8]
    bad_cases = [_case_from_mistake(item) for item in mistakes]
    good_cases = [_case_from_key_decision(item) for item in key_decisions if item.get("type") not in {case.get("type") for case in mistakes}][:5]
    memory_candidates = []
    for role in Role:
        for memory in recommendations.get(role.value, []):
            if memory:
                memory_candidates.append(
                    {
                        "role": role.value,
                        "memory": memory,
                        "evidence_event_ids": [],
                        "confidence": 0.82,
                    }
                )
    for mistake in mistakes:
        memory = _memory_from_mistake(mistake)
        role = _role_from_mistake(mistake)
        if memory and role:
            memory_candidates.append(
                {
                    "role": role,
                    "memory": memory,
                    "evidence_event_ids": _ids_from_case(mistake),
                    "confidence": 0.86,
                }
            )
    return {
        "review_status": "ok",
        "game_summary": f"{result.winner.zh}获胜。{report.summary}",
        "good_cases": good_cases,
        "bad_cases": bad_cases,
        "role_reflections": recommendations,
        "memory_candidates": memory_candidates[:12],
        "confidence": 0.72,
        "metadata": {
            "review_backend": "heuristic_backfill",
            "fallback_reason": reason,
            "source": "report_mistakes_and_recommendations",
        },
    }


def _case_from_mistake(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": _role_from_mistake(item) or "villager",
        "player_id": item.get("actor_id"),
        "event_ids": _ids_from_case(item),
        "reason": str(item.get("message") or item.get("reason") or item.get("type") or "规则评测识别到关键失误。"),
        "lesson": _memory_from_mistake(item) or "下次需要把公开证据、行动目标和阵营收益绑定后再决策。",
    }


def _case_from_key_decision(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": _role_from_phase_or_type(item),
        "player_id": item.get("actor_id"),
        "event_ids": _ids_from_case(item),
        "reason": str(item.get("reason") or item.get("type") or "关键节点决策清晰。"),
        "lesson": "保留清晰的公开理由，并在后续发言和投票中持续引用可见证据。",
    }


def _memory_from_mistake(item: dict[str, Any]) -> str:
    kind = str(item.get("type") or "")
    if kind == "werewolf_exposed_pack_vote":
        return "狼人白天投票应避免多名狼人在缺少独立公开证据时集中裸冲同一目标，可通过分票、轻踩队友或不同理由制造自然票型。"
    if kind == "seer_failed_to_reveal_wolf":
        return "预言家查到狼人后，应在合适白天明确报出查验轮次、P号和查杀结论，并推动可执行的归票方向。"
    if kind == "witch_poisoned_good":
        return "女巫使用毒药前应优先绑定公开查杀、多轮高嫌疑或明确票型证据，信息不足时保留毒药通常更稳。"
    if kind == "hunter_shot_good":
        return "猎人开枪前应优先选择多轮公开证据支持的高嫌疑目标，缺少硬证据时谨慎开枪。"
    return str(item.get("recommendation") or "")


def _role_from_mistake(item: dict[str, Any]) -> str | None:
    kind = str(item.get("type") or "")
    if kind.startswith("werewolf_"):
        return "werewolf"
    if kind.startswith("seer_"):
        return "seer"
    if kind.startswith("witch_"):
        return "witch"
    if kind.startswith("hunter_"):
        return "hunter"
    return None


def _role_from_phase_or_type(item: dict[str, Any]) -> str:
    phase = str(item.get("phase") or "")
    kind = str(item.get("type") or "")
    if "wolf" in phase or kind.startswith("wolf"):
        return "werewolf"
    if "seer" in phase:
        return "seer"
    if "witch" in phase:
        return "witch"
    if "hunter" in phase:
        return "hunter"
    return "villager"


def _ids_from_case(item: dict[str, Any]) -> list[int]:
    for key in ("event_ids", "evidence_event_ids"):
        value = item.get(key)
        if isinstance(value, list):
            return [int(raw) for raw in value if isinstance(raw, int)]
    raw_id = item.get("event_id") or item.get("id")
    return [int(raw_id)] if isinstance(raw_id, int) else []


def _update_run_file(run_file: Path, review_by_archive: dict[str, dict[str, Any]]) -> None:
    data = _read_json(run_file)
    updated = 0
    for history in data.get("history", []) if isinstance(data.get("history"), list) else []:
        if not isinstance(history, dict):
            continue
        archives = list(history.get("archive_paths") or [])
        reviews = list(history.get("llm_reviews") or [])
        while len(reviews) < len(archives):
            reviews.append({"review_status": "missing"})
        for index, archive in enumerate(archives):
            review = review_by_archive.get(_norm_path(Path(archive)))
            if not review:
                continue
            reviews[index] = to_jsonable(review)
            updated += 1
        history["llm_reviews"] = reviews
    data.setdefault("review_backfills", []).append({"backfilled_at": _stamp(), "updated_reviews": updated})
    _write_json(run_file, data)


def _infer_provider(payload: dict[str, Any]) -> str | None:
    review = payload.get("llm_review") if isinstance(payload.get("llm_review"), dict) else {}
    metadata = review.get("metadata") if isinstance(review.get("metadata"), dict) else {}
    return payload.get("llm_provider") or metadata.get("llm_provider")


def _review_status(review: Any) -> str | None:
    if not isinstance(review, dict):
        return None
    return review.get("review_status")


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2), encoding="utf-8")


def _norm_path(path: Path) -> str:
    return str(path).replace("/", "\\").lower()


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


if __name__ == "__main__":
    raise SystemExit(main())
