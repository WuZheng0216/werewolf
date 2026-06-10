from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean, median
from typing import Any


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize a Codex Skill Lab batch without calling any LLM.")
    parser.add_argument(
        "path",
        nargs="?",
        default="logs/codex_skill_lab",
        help="Batch directory, progress JSONL, review pack JSON, or logs/codex_skill_lab root.",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    args = parser.parse_args()

    target = Path(args.path)
    summary = summarize_target(target)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print_text_summary(summary)


def summarize_target(target: Path) -> dict[str, Any]:
    batch_dir = _resolve_batch_dir(target)
    progress_path = _latest_file(batch_dir, "*_progress.jsonl")
    review_pack_path = _latest_file(batch_dir, "*_codex_review_pack.json")
    error_path = _latest_file(batch_dir, "*_error.json")

    progress_rows = _read_progress_rows(progress_path) if progress_path else []
    review_pack = _read_json(review_pack_path) if review_pack_path else None
    error_payload = _read_json(error_path) if error_path else None

    return {
        "batch_dir": str(batch_dir),
        "progress_path": str(progress_path) if progress_path else None,
        "review_pack_path": str(review_pack_path) if review_pack_path else None,
        "error_path": str(error_path) if error_path else None,
        "status": _status(progress_rows, review_pack, error_payload),
        "progress": _summarize_progress(progress_rows),
        "review_pack": _summarize_review_pack(review_pack),
        "error": error_payload,
    }


def print_text_summary(summary: dict[str, Any]) -> None:
    progress = summary["progress"]
    review = summary["review_pack"]
    print(f"status: {summary['status']}")
    print(f"batch_dir: {summary['batch_dir']}")
    print(f"progress_path: {summary['progress_path']}")
    print(f"review_pack_path: {summary['review_pack_path']}")
    print(f"progress_events: {progress['event_count']}")
    print(f"completed_games_seen: {progress['completed_games_seen']}")
    print(f"last_event: {progress['last_event']}")
    print(f"latency_ms: {progress['latency_ms']}")
    print(f"slow_decisions: {progress['slow_decisions']}")
    print(f"wolf_day_votes: {progress['wolf_day_votes']}")
    print(f"wolf_skill_matches: {progress['wolf_skill_matches']}")
    if review:
        print(f"review_completed_games: {review.get('completed_games')}")
        print(f"review_requested_games: {review.get('requested_games')}")
        print(f"review_errors: {review.get('error_count')}")
        print(f"review_aggregate: {review.get('aggregate')}")


def _resolve_batch_dir(target: Path) -> Path:
    if target.is_file():
        return target.parent
    if _latest_file(target, "*_progress.jsonl") or _latest_file(target, "*_codex_review_pack.json"):
        return target
    children = [child for child in target.glob("batch_*") if child.is_dir()]
    if not children:
        return target
    return max(children, key=lambda child: child.stat().st_mtime)


def _latest_file(directory: Path, pattern: str) -> Path | None:
    if not directory.exists():
        return None
    matches = [path for path in directory.glob(pattern) if path.is_file()]
    if not matches:
        return None
    return max(matches, key=lambda path: path.stat().st_mtime)


def _read_json(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _read_progress_rows(path: Path | None) -> list[dict[str, Any]]:
    if path is None or not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            rows.append({"event": "invalid_json_line", "raw": line[:300]})
    return rows


def _status(
    progress_rows: list[dict[str, Any]],
    review_pack: dict[str, Any] | None,
    error_payload: dict[str, Any] | None,
) -> str:
    if review_pack:
        return "completed"
    if error_payload:
        return "failed"
    if progress_rows:
        return "running_or_interrupted"
    return "empty"


def _summarize_progress(rows: list[dict[str, Any]]) -> dict[str, Any]:
    event_counts: Counter[str] = Counter()
    latencies: list[float] = []
    slow_decisions: list[dict[str, Any]] = []
    completed_games: set[str] = set()
    wolf_day_votes: dict[str, Counter[str]] = defaultdict(Counter)
    wolf_skill_matches: Counter[str] = Counter()
    wolf_ids_by_game: dict[str, set[str]] = {}
    last_event: dict[str, Any] | None = None

    for row in rows:
        event_name = str(row.get("event") or "")
        event_counts[event_name] += 1
        data = row.get("data") if isinstance(row.get("data"), dict) else {}
        batch = data.get("batch") if isinstance(data.get("batch"), dict) else {}
        event = data.get("event") if isinstance(data.get("event"), dict) else {}
        decision = event.get("decision") if isinstance(event.get("decision"), dict) else None
        game_key = _game_key(batch)
        last_event = _compact_last_event(row, batch, event)

        if event_name == "game_started":
            wolf_ids = _wolf_ids_from_players(data.get("players"))
            if wolf_ids:
                wolf_ids_by_game[game_key] = wolf_ids

        if event_name == "game_completed":
            completed_games.add(_completed_game_key(data, batch))

        if decision:
            latency = decision.get("llm_total_elapsed_ms")
            if isinstance(latency, (int, float)):
                latencies.append(float(latency))
                slow_decisions.append(
                    {
                        "latency_ms": round(float(latency), 2),
                        "game": game_key,
                        "day": event.get("day"),
                        "phase": event.get("phase"),
                        "event_type": event.get("event_type"),
                        "actor_id": event.get("actor_id"),
                        "action": decision.get("action"),
                        "target_id": decision.get("target_id"),
                    }
                )

            matched_skills = decision.get("matched_skills")
            if isinstance(matched_skills, list) and _is_wolf_actor(event, wolf_ids_by_game.get(game_key)):
                for skill in matched_skills:
                    if isinstance(skill, dict) and skill.get("skill_id"):
                        wolf_skill_matches[str(skill["skill_id"])] += 1

            if (
                event.get("phase") == "day_vote"
                and event.get("event_type") == "vote"
                and _is_wolf_actor(event, wolf_ids_by_game.get(game_key))
            ):
                actor = str(event.get("actor_id"))
                target = _vote_target(decision)
                vote_key = f"{game_key}_d{event.get('day')}"
                wolf_day_votes[vote_key][f"P{actor}->{target}"] += 1

    return {
        "event_count": len(rows),
        "event_counts": dict(event_counts),
        "completed_games_seen": len(completed_games),
        "last_event": last_event,
        "latency_ms": _latency_summary(latencies),
        "slow_decisions": sorted(slow_decisions, key=lambda item: item["latency_ms"], reverse=True)[:8],
        "wolf_day_votes": _summarize_wolf_votes(wolf_day_votes),
        "wolf_skill_matches": dict(wolf_skill_matches.most_common(12)),
    }


def _summarize_review_pack(review_pack: dict[str, Any] | None) -> dict[str, Any] | None:
    if not review_pack:
        return None
    aggregate = review_pack.get("aggregate") if isinstance(review_pack.get("aggregate"), dict) else {}
    return {
        "completed_games": review_pack.get("completed_games"),
        "requested_games": review_pack.get("requested_games"),
        "attempted_games": review_pack.get("attempted_games"),
        "archive_count": len(review_pack.get("archives") or []),
        "error_count": len(review_pack.get("errors") or []),
        "aggregate": aggregate,
    }


def _compact_last_event(row: dict[str, Any], batch: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    decision = event.get("decision") if isinstance(event.get("decision"), dict) else {}
    return {
        "time": row.get("time"),
        "event": row.get("event"),
        "game_index": batch.get("game_index"),
        "attempt_index": batch.get("attempt_index"),
        "day": event.get("day"),
        "phase": event.get("phase"),
        "event_type": event.get("event_type"),
        "actor_id": event.get("actor_id"),
        "action": decision.get("action"),
        "target_id": decision.get("target_id"),
        "latency_ms": decision.get("llm_total_elapsed_ms"),
    }


def _completed_game_key(data: dict[str, Any], batch: dict[str, Any]) -> str:
    archive_path = data.get("archive_path")
    if archive_path:
        return str(archive_path)
    game = data.get("game")
    if isinstance(game, dict) and game.get("game_id"):
        return str(game["game_id"])
    if data.get("game_id"):
        return str(data["game_id"])
    return _game_key(batch)


def _latency_summary(latencies: list[float]) -> dict[str, Any]:
    if not latencies:
        return {"count": 0}
    ordered = sorted(latencies)
    return {
        "count": len(ordered),
        "avg": round(mean(ordered), 2),
        "median": round(median(ordered), 2),
        "max": round(ordered[-1], 2),
        "p95": round(ordered[int((len(ordered) - 1) * 0.95)], 2),
    }


def _summarize_wolf_votes(votes_by_game: dict[str, Counter[str]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for game_key, votes in votes_by_game.items():
        targets: Counter[str] = Counter()
        vote_rows: list[dict[str, str]] = []
        for vote_key, count in votes.items():
            actor, target = vote_key.split("->", 1)
            targets[target] += count
            vote_rows.append({"actor": actor, "target": target})
        rows.append(
            {
                "game": game_key,
                "wolf_votes": sorted(vote_rows, key=lambda item: item["actor"]),
                "target_counts": dict(targets),
                "max_same_target": max(targets.values()) if targets else 0,
            }
        )
    return rows


def _wolf_ids_from_players(players: Any) -> set[str]:
    if not isinstance(players, list):
        return set()
    wolf_ids: set[str] = set()
    for player in players:
        if not isinstance(player, dict):
            continue
        if player.get("role") == "werewolf" and player.get("id") is not None:
            wolf_ids.add(str(player["id"]))
    return wolf_ids


def _is_wolf_actor(event: dict[str, Any], wolf_ids: set[str] | None) -> bool:
    actor = event.get("actor_id")
    if actor is None:
        return False
    if wolf_ids:
        return str(actor) in wolf_ids
    return str(actor) in {"7", "8", "9"}


def _game_key(batch: dict[str, Any]) -> str:
    return f"g{batch.get('game_index')}_a{batch.get('attempt_index')}_s{batch.get('seed')}"


def _vote_target(decision: dict[str, Any]) -> str:
    action = decision.get("action")
    target = decision.get("target_id")
    if action == "pass" or target in {None, ""}:
        return "pass"
    return f"P{target}"


if __name__ == "__main__":
    main()
