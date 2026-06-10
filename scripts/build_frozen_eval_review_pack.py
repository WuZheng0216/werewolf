from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


DEFAULT_TARGET_MISTAKE = "werewolf_exposed_pack_vote"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build a compact, evidence-focused review pack from a frozen-eval result."
    )
    parser.add_argument("path", help="Frozen-eval output directory or *_frozen_eval_result.json")
    parser.add_argument("--output", help="Output JSON path. Defaults beside the result file.")
    parser.add_argument("--target-mistake", default=DEFAULT_TARGET_MISTAKE)
    parser.add_argument("--max-events-per-case", type=int, default=14)
    parser.add_argument("--max-text-chars", type=int, default=260)
    parser.add_argument(
        "--include-night",
        action="store_true",
        help="Include night/private events in snippets. Default keeps the review pack focused on public discussion.",
    )
    args = parser.parse_args()

    result_path = _resolve_result_path(Path(args.path))
    output_path = (
        Path(args.output)
        if args.output
        else result_path.with_name(result_path.stem + "_skill_review_pack.json")
    )
    pack = build_review_pack(
        result_path,
        target_mistake=args.target_mistake,
        max_events_per_case=args.max_events_per_case,
        max_text_chars=args.max_text_chars,
        include_night=args.include_night,
    )
    output_path.write_text(json.dumps(pack, ensure_ascii=False, indent=2), encoding="utf-8")
    print(str(output_path))


def build_review_pack(
    result_path: Path,
    *,
    target_mistake: str = DEFAULT_TARGET_MISTAKE,
    max_events_per_case: int = 14,
    max_text_chars: int = 260,
    include_night: bool = False,
) -> dict[str, Any]:
    result = _read_json(result_path)
    cases: list[dict[str, Any]] = []
    archives = result.get("archives") if isinstance(result.get("archives"), dict) else {}
    for side in ("candidate", "baseline"):
        for archive in archives.get(side) or []:
            archive_path = Path(str(archive))
            if not archive_path.exists():
                archive_path = result_path.parent / str(archive)
            if not archive_path.exists():
                cases.append(
                    {
                        "side": side,
                        "archive_path": str(archive),
                        "error": "archive_missing",
                    }
                )
                continue
            cases.extend(
                _cases_from_archive(
                    side,
                    archive_path,
                    target_mistake=target_mistake,
                    max_events_per_case=max_events_per_case,
                    max_text_chars=max_text_chars,
                    include_night=include_night,
                )
            )

    return {
        "type": "frozen_eval_skill_review_pack",
        "source_result_path": str(result_path),
        "candidate_version": result.get("version_id") or result.get("requested_version"),
        "baseline_version": result.get("baseline_version_id") or result.get("requested_baseline_version"),
        "llm_provider": result.get("llm_provider"),
        "llm_model": result.get("llm_model"),
        "target_mistake": target_mistake,
        "include_night": include_night,
        "completed_games": result.get("completed_games"),
        "seed_alignment": result.get("seed_alignment"),
        "aggregate": {
            "candidate": _aggregate_side(result.get("candidate")),
            "baseline": _aggregate_side(result.get("baseline")),
            "delta": result.get("delta"),
            "errors": result.get("errors"),
        },
        "case_count": len([case for case in cases if not case.get("error")]),
        "cases": cases,
        "review_protocol": [
            "First compare aggregate target mistake deltas and gate result.",
            "Use these snippets only to explain the failure mechanism before changing a skill.",
            "Prefer refining triggers/procedure/avoid clauses; do not hard-code fixed actions.",
        ],
    }


def _cases_from_archive(
    side: str,
    archive_path: Path,
    *,
    target_mistake: str,
    max_events_per_case: int,
    max_text_chars: int,
    include_night: bool,
) -> list[dict[str, Any]]:
    archive = _read_json(archive_path)
    game = archive.get("game") if isinstance(archive.get("game"), dict) else {}
    report = archive.get("report") if isinstance(archive.get("report"), dict) else {}
    events = game.get("events") if isinstance(game.get("events"), list) else []
    players = game.get("players") if isinstance(game.get("players"), list) else []
    mistakes = [
        item
        for item in (report.get("mistakes") or [])
        if isinstance(item, dict) and item.get("type") == target_mistake
    ]
    cases: list[dict[str, Any]] = []
    for mistake in mistakes:
        participants = _participants(mistake)
        selected = _select_events(
            events,
            mistake,
            participants,
            max_events_per_case,
            include_night=include_night,
        )
        cases.append(
            {
                "side": side,
                "archive_path": str(archive_path),
                "game_id": game.get("game_id"),
                "seed": game.get("seed"),
                "version_id": game.get("version_id"),
                "winner": game.get("winner"),
                "mistake": mistake,
                "relevant_players": _compact_players(players, participants),
                "events": [_compact_event(event, max_text_chars=max_text_chars) for event in selected],
            }
        )
    return cases


def _select_events(
    events: list[dict[str, Any]],
    mistake: dict[str, Any],
    participants: set[int],
    max_events: int,
    *,
    include_night: bool,
) -> list[dict[str, Any]]:
    day = mistake.get("day")
    scored: list[tuple[int, int, dict[str, Any]]] = []
    for index, event in enumerate(events):
        if day is not None and event.get("day") != day:
            continue
        if not include_night and str(event.get("phase") or "").startswith("night"):
            continue
        score = _event_relevance(event, mistake, participants)
        if score <= 0:
            continue
        scored.append((score, index, event))
    selected = sorted(scored, key=lambda item: (-item[0], item[1]))[:max_events]
    return [item[2] for item in sorted(selected, key=lambda item: item[1])]


def _event_relevance(event: dict[str, Any], mistake: dict[str, Any], participants: set[int]) -> int:
    score = 0
    phase = str(event.get("phase") or "")
    event_type = str(event.get("event_type") or "")
    actor = _int_or_none(event.get("actor_id"))
    target = _int_or_none(event.get("target_id"))
    mistake_target = _int_or_none(mistake.get("target_id"))
    if "vote" in phase or event_type in {"vote", "exile"}:
        score += 8
    if actor in participants:
        score += 6
    if target in participants:
        score += 4
    if mistake_target is not None and target == mistake_target:
        score += 4
    if event_type == "speech" and actor in participants:
        score += 3
    decision = event.get("decision") if isinstance(event.get("decision"), dict) else {}
    usage = ((decision.get("metadata") or {}).get("skill_usage") or {}) if decision else {}
    if usage.get("matched_skills"):
        score += 2
    return score


def _compact_event(event: dict[str, Any], *, max_text_chars: int) -> dict[str, Any]:
    decision = event.get("decision") if isinstance(event.get("decision"), dict) else None
    compact_decision = None
    if decision:
        metadata = decision.get("metadata") if isinstance(decision.get("metadata"), dict) else {}
        usage = metadata.get("skill_usage") if isinstance(metadata.get("skill_usage"), dict) else {}
        matched = usage.get("matched_skills") if isinstance(usage.get("matched_skills"), list) else []
        compact_decision = {
            "action": decision.get("action"),
            "target_id": decision.get("target_id"),
            "secondary_target_id": decision.get("secondary_target_id"),
            "speech": _short_text(decision.get("speech"), max_text_chars),
            "reason": _short_text(decision.get("reason"), max_text_chars),
            "confidence": decision.get("confidence"),
            "matched_skills": [
                {
                    "skill_id": skill.get("skill_id"),
                    "title": skill.get("title"),
                    "score": skill.get("score"),
                    "signals": skill.get("signals"),
                }
                for skill in matched[:5]
                if isinstance(skill, dict)
            ],
        }
    return {
        "id": event.get("id"),
        "day": event.get("day"),
        "phase": event.get("phase"),
        "event_type": event.get("event_type"),
        "actor_id": event.get("actor_id"),
        "target_id": event.get("target_id"),
        "public_text": _short_text(event.get("public_text"), max_text_chars),
        "decision": compact_decision,
    }


def _participants(mistake: dict[str, Any]) -> set[int]:
    ids = {_int_or_none(mistake.get("actor_id")), _int_or_none(mistake.get("target_id"))}
    for item in mistake.get("wolf_voters") or []:
        ids.add(_int_or_none(item))
    return {item for item in ids if item is not None}


def _compact_players(players: list[dict[str, Any]], participants: set[int]) -> list[dict[str, Any]]:
    compact = []
    for player in players:
        player_id = _int_or_none(player.get("id"))
        if player_id not in participants:
            continue
        compact.append(
            {
                "id": player.get("id"),
                "name": player.get("name"),
                "role": player.get("role"),
                "alive": player.get("alive"),
            }
        )
    return compact


def _aggregate_side(side: Any) -> dict[str, Any] | None:
    if not isinstance(side, dict):
        return None
    return {
        "games": side.get("games"),
        "average_scores": side.get("average_scores"),
        "winner_counts": side.get("winner_counts"),
        "mistake_counts": side.get("mistake_counts"),
        "mistakes_per_game": side.get("mistakes_per_game"),
    }


def _resolve_result_path(path: Path) -> Path:
    if path.is_file():
        return path
    matches = sorted(path.glob("*_frozen_eval_result.json"), key=lambda item: item.stat().st_mtime)
    if not matches:
        raise FileNotFoundError(f"No *_frozen_eval_result.json found in {path}")
    return matches[-1]


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _short_text(value: Any, max_chars: int) -> str | None:
    if value is None:
        return None
    text = str(value)
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 1)] + "…"


def _int_or_none(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    main()
