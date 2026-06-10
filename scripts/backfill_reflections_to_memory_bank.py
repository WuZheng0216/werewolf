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

from werewolf_ai.memory_store import save_review_memory_candidates
from werewolf_ai.models import Role, to_jsonable


RUN_GLOB = "data/memory/evolution_runs/*.json"
LOG_GLOB = "logs/games/*.json"
SUMMARY_DIR = Path("data") / "memory" / "review_backfills"
MEMORY_BANK_PATH = Path("data") / "memory" / "memory_bank.json"
UNSUPPORTED_RULE_TERMS = ("警徽", "警长", "警徽流", "撕警徽", "移交警徽")
GENERIC_RECOMMENDATIONS = {
    "保留公开信息来源，发言中说明判断依据和投票计划。",
}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill ok-but-unwritten review candidates and failed reviews into memory_bank.json."
    )
    parser.add_argument("--run-glob", default=RUN_GLOB)
    parser.add_argument("--logs-glob", default=LOG_GLOB)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--include-missing",
        action="store_true",
        help="Also synthesize reviews for archives that have no llm_review field. Defaults to failed reviews only.",
    )
    args = parser.parse_args()

    run_paths = sorted(Path(".").glob(args.run_glob))
    archive_paths = sorted(Path(".").glob(args.logs_glob))
    archive_cache = _load_archives(archive_paths)

    summary: dict[str, Any] = {
        "created_at": _stamp(),
        "dry_run": args.dry_run,
        "ok_unwritten_backfilled": [],
        "failed_reviews_backfilled": [],
        "missing_reviews_backfilled": [],
        "skipped": [],
        "updated_run_files": [],
        "updated_archive_files": [],
    }
    dirty_archives: set[str] = set()
    handled_archives: set[str] = set()

    for run_path in run_paths:
        try:
            run_payload = _read_json(run_path)
        except Exception as exc:
            summary["skipped"].append({"path": str(run_path), "reason": f"run_json_error: {exc}"})
            continue
        run_dirty = False
        for history_index, history in enumerate(_as_list(run_payload.get("history"))):
            if not isinstance(history, dict):
                continue
            version_id = str(history.get("version_id") or "unknown")
            archives = [str(path) for path in _as_list(history.get("archive_paths"))]
            reviews = _as_list(history.get("llm_reviews"))
            while len(reviews) < len(archives):
                reviews.append({"review_status": "missing"})
                run_dirty = True
            for review_index, archive_ref in enumerate(archives):
                archive_key = _norm_path(Path(archive_ref))
                archive_payload = archive_cache.get(archive_key)
                review = reviews[review_index] if isinstance(reviews[review_index], dict) else {"review_status": "missing"}
                context = _context_from_archive(archive_payload, fallback_version=version_id, archive_path=archive_ref)
                changed, entry = _process_review(
                    review,
                    context=context,
                    source_kind="evolution_run",
                    source_path=str(run_path),
                    history_index=history_index,
                    review_index=review_index,
                    dry_run=args.dry_run,
                    include_missing=args.include_missing,
                )
                if entry:
                    summary[entry["category"]].append(entry["data"])
                if changed:
                    reviews[review_index] = review
                    run_dirty = True
                    handled_archives.add(archive_key)
                    if archive_payload is not None:
                        _sync_archive_review(archive_payload, review)
                        dirty_archives.add(archive_key)
            history["llm_reviews"] = reviews
        if run_dirty:
            run_payload.setdefault("reflection_backfills", []).append(
                {"backfilled_at": _stamp(), "script": Path(__file__).name, "dry_run": args.dry_run}
            )
            if not args.dry_run:
                _write_json(run_path, run_payload)
            summary["updated_run_files"].append(str(run_path))

    for archive_key, archive_payload in archive_cache.items():
        if archive_key in handled_archives:
            continue
        review = archive_payload.get("llm_review")
        if not isinstance(review, dict):
            review = {"review_status": "missing"}
            archive_payload["llm_review"] = review
        context = _context_from_archive(archive_payload, fallback_version=None, archive_path=archive_key)
        changed, entry = _process_review(
            review,
            context=context,
            source_kind="archive",
            source_path=archive_key,
            history_index=None,
            review_index=None,
            dry_run=args.dry_run,
            include_missing=args.include_missing,
        )
        if entry:
            summary[entry["category"]].append(entry["data"])
        if changed:
            dirty_archives.add(archive_key)

    for archive_key in sorted(dirty_archives):
        archive_payload = archive_cache.get(archive_key)
        if archive_payload is None:
            continue
        archive_path = Path(archive_key)
        if not args.dry_run:
            _write_json(archive_path, archive_payload)
        summary["updated_archive_files"].append(str(archive_path))

    summary["counts"] = {
        "ok_unwritten_backfilled": len(summary["ok_unwritten_backfilled"]),
        "failed_reviews_backfilled": len(summary["failed_reviews_backfilled"]),
        "missing_reviews_backfilled": len(summary["missing_reviews_backfilled"]),
        "updated_run_files": len(summary["updated_run_files"]),
        "updated_archive_files": len(summary["updated_archive_files"]),
        "skipped": len(summary["skipped"]),
    }
    if not args.dry_run:
        SUMMARY_DIR.mkdir(parents=True, exist_ok=True)
        summary_path = SUMMARY_DIR / f"reflection_backfill_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json"
        _write_json(summary_path, summary)
        summary["summary_path"] = str(summary_path)

    print(json.dumps(to_jsonable(summary), ensure_ascii=False, indent=2))
    return 0


def _process_review(
    review: dict[str, Any],
    *,
    context: dict[str, Any],
    source_kind: str,
    source_path: str,
    history_index: int | None,
    review_index: int | None,
    dry_run: bool,
    include_missing: bool,
) -> tuple[bool, dict[str, Any] | None]:
    status = review.get("review_status")
    has_candidates = bool(review.get("memory_candidates"))
    has_ids = bool(review.get("memory_bank_item_ids"))
    save_context = {
        "version_id": context["version_id"],
        "game_id": context["game_id"],
        "batch": context.get("batch"),
        "archive_path": context.get("archive_path"),
    }
    if status == "ok" and has_candidates and not has_ids:
        ids = [] if dry_run else save_review_memory_candidates(review, **save_context)
        if ids and not dry_run:
            _annotate_memory_bank_sources(
                ids,
                save_context,
                review_backend="llm_existing_review_backfill",
                backfill_type="ok_candidates_to_memory_bank",
                source_kind=source_kind,
                source_path=source_path,
            )
        review["memory_bank_item_ids"] = ids
        _append_backfill_metadata(
            review,
            {
                "backfilled_at": _stamp(),
                "backfill_type": "ok_candidates_to_memory_bank",
                "source_kind": source_kind,
                "source_path": source_path,
                "dry_run": dry_run,
            },
        )
        return True, {
            "category": "ok_unwritten_backfilled",
            "data": _entry(context, source_kind, source_path, history_index, review_index, ids, "ok_candidates_to_memory_bank"),
        }

    if status == "failed" or (include_missing and status in {None, "", "missing"}):
        archive_payload = context.get("_archive_payload")
        if not isinstance(archive_payload, dict):
            return False, {
                "category": "skipped",
                "data": _entry(context, source_kind, source_path, history_index, review_index, [], "missing_archive_payload"),
            }
        previous_review = dict(review)
        synthesized = _synthesize_review(archive_payload, previous_review=previous_review)
        ids = [] if dry_run else save_review_memory_candidates(synthesized, **save_context)
        if ids and not dry_run:
            _annotate_memory_bank_sources(
                ids,
                save_context,
                review_backend="codex_manual_backfill",
                backfill_type="manual_report_synthesis",
                source_kind=source_kind,
                source_path=source_path,
            )
        synthesized["memory_bank_item_ids"] = ids
        synthesized["backfill_metadata"] = {
            "backfilled_at": _stamp(),
            "backfill_type": "manual_report_synthesis",
            "source_kind": source_kind,
            "source_path": source_path,
            "previous_review_status": status,
            "dry_run": dry_run,
        }
        review.clear()
        review.update(synthesized)
        category = "failed_reviews_backfilled" if status == "failed" else "missing_reviews_backfilled"
        return True, {
            "category": category,
            "data": _entry(context, source_kind, source_path, history_index, review_index, ids, "manual_report_synthesis"),
        }

    return False, None


def _synthesize_review(archive_payload: dict[str, Any], *, previous_review: dict[str, Any]) -> dict[str, Any]:
    game = archive_payload.get("game") if isinstance(archive_payload.get("game"), dict) else {}
    report = archive_payload.get("report") if isinstance(archive_payload.get("report"), dict) else {}
    mistakes = [item for item in _as_list(report.get("mistakes")) if isinstance(item, dict)]
    recommendations = report.get("recommendations") if isinstance(report.get("recommendations"), dict) else {}
    events = [item for item in _as_list(game.get("events")) if isinstance(item, dict)]
    candidates = _memory_candidates_from_mistakes(mistakes)
    candidates.extend(_memory_candidates_from_recommendations(recommendations))
    _ensure_werewolf_deception_candidate(candidates, events, game)
    candidates = _dedupe_candidates(candidates)[:12]
    return {
        "review_status": "ok",
        "game_summary": _summary_from_report(game, report),
        "good_cases": _good_cases_from_report(report),
        "bad_cases": _bad_cases_from_mistakes(mistakes),
        "role_reflections": _role_reflections(recommendations, candidates),
        "memory_candidates": candidates,
        "confidence": 0.74 if previous_review.get("review_status") == "failed" else 0.68,
        "metadata": {
            "review_backend": "codex_manual_backfill",
            "source": "report_mistakes_recommendations_and_events",
            "previous_review_status": previous_review.get("review_status"),
            "previous_error": previous_review.get("error"),
            "previous_message": previous_review.get("message"),
        },
    }


def _memory_candidates_from_mistakes(mistakes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for mistake in mistakes:
        kind = str(mistake.get("type") or "")
        role = str(mistake.get("role") or _role_from_mistake_type(kind) or "")
        memory = _memory_for_mistake(kind)
        if not role or not memory:
            continue
        result.append(
            {
                "role": role,
                "memory": memory,
                "evidence_event_ids": _evidence_ids(mistake),
                "confidence": 0.9 if role == Role.WEREWOLF.value else 0.86,
            }
        )
    return result


def _memory_candidates_from_recommendations(recommendations: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    valid_roles = {role.value for role in Role}
    for role, values in recommendations.items():
        if role not in valid_roles:
            continue
        for text in _as_list(values):
            memory = _clean_memory_text(text)
            if not memory:
                continue
            result.append({"role": role, "memory": memory, "evidence_event_ids": [], "confidence": 0.82})
    return result


def _ensure_werewolf_deception_candidate(
    candidates: list[dict[str, Any]],
    events: list[dict[str, Any]],
    game: dict[str, Any],
) -> None:
    players = {int(player.get("id")): str(player.get("role")) for player in _as_list(game.get("players")) if isinstance(player, dict)}
    wolf_event_ids = [
        int(event.get("id"))
        for event in events
        if event.get("event_type") in {"speech", "vote"}
        and event.get("actor_id") in players
        and players.get(event.get("actor_id")) == Role.WEREWOLF.value
        and isinstance(event.get("id"), int)
    ]
    if not wolf_event_ids:
        return
    if any(
        item.get("role") == Role.WEREWOLF.value
        and any(token in str(item.get("memory") or "") for token in ("欺骗", "伪装", "对跳", "悍跳", "倒钩", "分票"))
        for item in candidates
    ):
        return
    candidates.append(
        {
            "role": Role.WEREWOLF.value,
            "memory": "狼人公开发言可以策略性不诚实，但应围绕公开事实设计自洽的伪装、质疑、倒钩或分票计划，避免只重复“我是好人”或多狼同模板同理由冲票。",
            "evidence_event_ids": wolf_event_ids[:5],
            "confidence": 0.9,
        }
    )


def _memory_for_mistake(kind: str) -> str:
    mapping = {
        "werewolf_exposed_pack_vote": "狼人白天投票要避免多狼在缺少独立公开证据时集中裸冲同一目标，可通过分票、倒钩、轻踩队友或不同理由制造自然票型。",
        "werewolf_low_deception_speech": "狼人发言不能长期停留在“我是好人”的低质量伪装，应基于公开发言和票型制造合理分歧，并为后续投票留下可复用理由。",
        "werewolf_no_seer_counterplay": "当场上只有单预言家且没有强查杀时，狼人可选择软对跳、质疑查验链或制造第二视角，而不是全员默认认下。",
        "werewolf_incomplete_fake_seer_claim": "狼人悍跳或对跳预言家时，要补齐查验对象、查验结论、后续查验计划和公开证据包装，避免只喊身份不成链。",
        "seer_failed_to_reveal_wolf": "预言家查到狼人后，应在白天明确报出查验轮次、目标 P 号和查杀结论，并把归票目标绑定到公开查杀上。",
        "witch_poisoned_good": "女巫使用毒药前应优先绑定公开查杀、多轮高嫌疑或明确票型证据；信息不足时保留毒药通常比盲毒更稳。",
        "hunter_shot_good": "猎人死亡开枪前应按证据强度排序目标：公开查杀、共票关系、多轮带错节奏优先；只有软嫌疑时应谨慎开枪或不开枪。",
        "villager_bad_follow_vote": "村民跟票前要说明可见证据来源，优先比较查验结果、票型链和发言矛盾，避免只因他人归票就放弃独立判断。",
    }
    return mapping.get(kind, "")


def _bad_cases_from_mistakes(mistakes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cases = []
    for mistake in mistakes[:8]:
        cases.append(
            {
                "role": str(mistake.get("role") or _role_from_mistake_type(str(mistake.get("type") or "")) or ""),
                "player_id": mistake.get("actor_id"),
                "event_ids": _evidence_ids(mistake),
                "reason": str(mistake.get("message") or mistake.get("type") or ""),
                "lesson": _memory_for_mistake(str(mistake.get("type") or "")) or "后续决策应绑定公开证据、阵营收益和可追溯理由，避免只做结果导向判断。",
            }
        )
    return cases


def _good_cases_from_report(report: dict[str, Any]) -> list[dict[str, Any]]:
    key_decisions = [item for item in _as_list(report.get("key_decisions")) if isinstance(item, dict)]
    cases = []
    for item in key_decisions[:5]:
        cases.append(
            {
                "role": str(item.get("role") or _role_from_mistake_type(str(item.get("type") or "")) or ""),
                "player_id": item.get("actor_id"),
                "event_ids": _evidence_ids(item),
                "reason": str(item.get("reason") or item.get("message") or item.get("type") or "关键节点处理较好。"),
                "lesson": "保留这种把公开证据、行动目标和投票计划连起来的决策方式。",
            }
        )
    return cases


def _role_reflections(recommendations: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, list[str]]:
    reflections: dict[str, list[str]] = {role.value: [] for role in Role}
    for role in reflections:
        for item in _as_list(recommendations.get(role)):
            memory = _clean_memory_text(item)
            if memory:
                reflections[role].append(memory)
        for candidate in candidates:
            if candidate.get("role") == role:
                reflections[role].append(str(candidate.get("memory") or ""))
        reflections[role] = _dedupe_texts(reflections[role])[:3]
    return reflections


def _summary_from_report(game: dict[str, Any], report: dict[str, Any]) -> str:
    winner = game.get("winner") or report.get("winner") or "unknown"
    summary = str(report.get("summary") or "").strip()
    if summary:
        return summary
    return f"本局 {winner} 阵营获胜；该复盘由已有评测报告人工回填生成。"


def _clean_memory_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text or text in GENERIC_RECOMMENDATIONS:
        return ""
    if any(term in text for term in UNSUPPORTED_RULE_TERMS):
        return ""
    return text


def _dedupe_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    seen = set()
    for item in candidates:
        role = str(item.get("role") or "")
        memory = _clean_memory_text(item.get("memory")) or str(item.get("memory") or "").strip()
        if role not in {role.value for role in Role} or not memory:
            continue
        if any(term in memory for term in UNSUPPORTED_RULE_TERMS):
            continue
        key = (role, "".join(memory.lower().split()))
        if key in seen:
            continue
        seen.add(key)
        cleaned = dict(item)
        cleaned["role"] = role
        cleaned["memory"] = memory
        cleaned["evidence_event_ids"] = _evidence_ids(cleaned)
        cleaned["confidence"] = _bounded_float(cleaned.get("confidence"), 0.82)
        result.append(cleaned)
    return result


def _dedupe_texts(values: list[str]) -> list[str]:
    result = []
    seen = set()
    for value in values:
        text = str(value or "").strip()
        key = "".join(text.lower().split())
        if not text or key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def _role_from_mistake_type(kind: str) -> str | None:
    if kind.startswith("werewolf_"):
        return Role.WEREWOLF.value
    if kind.startswith("seer_"):
        return Role.SEER.value
    if kind.startswith("witch_"):
        return Role.WITCH.value
    if kind.startswith("hunter_"):
        return Role.HUNTER.value
    if kind.startswith("villager_"):
        return Role.VILLAGER.value
    return None


def _evidence_ids(item: dict[str, Any]) -> list[int]:
    ids = item.get("evidence_event_ids")
    if not isinstance(ids, list):
        ids = item.get("event_ids")
    if not isinstance(ids, list):
        raw = item.get("event_id") or item.get("id")
        ids = [raw] if raw is not None else []
    result = []
    for raw in ids:
        try:
            event_id = int(raw)
        except (TypeError, ValueError):
            continue
        if event_id not in result:
            result.append(event_id)
    return result


def _context_from_archive(archive_payload: dict[str, Any] | None, *, fallback_version: str | None, archive_path: str) -> dict[str, Any]:
    game = archive_payload.get("game") if isinstance(archive_payload, dict) and isinstance(archive_payload.get("game"), dict) else {}
    batch = archive_payload.get("batch") if isinstance(archive_payload, dict) and isinstance(archive_payload.get("batch"), dict) else {}
    return {
        "version_id": str(game.get("version_id") or fallback_version or "unknown"),
        "game_id": str(game.get("game_id") or Path(archive_path).stem),
        "batch": batch,
        "archive_path": str(archive_path),
        "_archive_payload": archive_payload,
    }


def _sync_archive_review(archive_payload: dict[str, Any], review: dict[str, Any]) -> None:
    old_review = archive_payload.get("llm_review")
    if isinstance(old_review, dict) and old_review is not review:
        archive_payload.setdefault("llm_review_backfills", []).append(
            {
                "backfilled_at": _stamp(),
                "previous_review_status": old_review.get("review_status"),
                "new_review_status": review.get("review_status"),
            }
        )
    archive_payload["llm_review"] = to_jsonable(review)


def _entry(
    context: dict[str, Any],
    source_kind: str,
    source_path: str,
    history_index: int | None,
    review_index: int | None,
    ids: list[str],
    action: str,
) -> dict[str, Any]:
    return {
        "action": action,
        "source_kind": source_kind,
        "source_path": source_path,
        "history_index": history_index,
        "review_index": review_index,
        "version_id": context.get("version_id"),
        "game_id": context.get("game_id"),
        "archive_path": context.get("archive_path"),
        "memory_bank_item_ids": ids,
    }


def _append_backfill_metadata(review: dict[str, Any], item: dict[str, Any]) -> None:
    existing = review.get("backfill_metadata")
    if isinstance(existing, list):
        existing.append(item)
    elif isinstance(existing, dict):
        review["backfill_metadata"] = [existing, item]
    else:
        review["backfill_metadata"] = [item]


def _annotate_memory_bank_sources(
    ids: list[str],
    context: dict[str, Any],
    *,
    review_backend: str,
    backfill_type: str,
    source_kind: str,
    source_path: str,
) -> None:
    if not MEMORY_BANK_PATH.exists():
        return
    try:
        bank = _read_json(MEMORY_BANK_PATH)
    except Exception:
        return
    changed = False
    wanted = set(ids)
    for item in bank.get("items", []):
        if not isinstance(item, dict) or item.get("id") not in wanted:
            continue
        for source in _as_list(item.get("sources")):
            if not isinstance(source, dict):
                continue
            if (
                source.get("version_id") == context.get("version_id")
                and source.get("game_id") == context.get("game_id")
                and _norm_path(Path(str(source.get("archive_path") or ""))) == _norm_path(Path(str(context.get("archive_path") or "")))
            ):
                source["review_backend"] = review_backend
                source["backfill_type"] = backfill_type
                source["backfill_source_kind"] = source_kind
                source["backfill_source_path"] = source_path
                changed = True
    if changed:
        _write_json(MEMORY_BANK_PATH, bank)


def _load_archives(paths: list[Path]) -> dict[str, dict[str, Any]]:
    result = {}
    for path in paths:
        try:
            payload = _read_json(path)
        except Exception:
            continue
        if isinstance(payload, dict) and isinstance(payload.get("game"), dict) and isinstance(payload.get("report"), dict):
            result[_norm_path(path)] = payload
            archive_path = payload.get("archive_path")
            if archive_path:
                result[_norm_path(Path(str(archive_path)))] = payload
    return result


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _bounded_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(0.0, min(1.0, parsed))


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
