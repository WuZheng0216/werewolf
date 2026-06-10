from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from werewolf_ai.codex_skill_lab import run_codex_skill_lab_batch
from werewolf_ai.llm import load_dotenv
from werewolf_ai.models import to_jsonable


def main() -> None:
    load_dotenv()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Run Doubao games and write a Codex review pack for skill-lab evolution."
    )
    parser.add_argument("--version", default="latest", help="Role-memory version to sample.")
    parser.add_argument("--games", type=int, default=5)
    parser.add_argument("--llm-provider", default="ark")
    parser.add_argument("--llm-model", default=None)
    parser.add_argument("--seed-offset", type=int, default=80_000)
    parser.add_argument("--output-dir", default="logs/codex_skill_lab")
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="Enable LLM preflight. By default it is skipped so 429 waits are visible as game retry events.",
    )
    parser.add_argument(
        "--internal-llm-retries",
        type=int,
        default=1,
        help="Temporary ARK_MAX_RETRIES for this sampling job. Lower values make 429 waits visible sooner.",
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    progress_path = output_dir / f"{stamp}_progress.jsonl"
    review_pack_path = output_dir / f"{stamp}_codex_review_pack.json"
    error_path = output_dir / f"{stamp}_error.json"

    def progress(event_name: str, data: dict[str, Any]) -> None:
        compact_data = _compact_progress_data(data)
        row = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "event": event_name,
            "data": to_jsonable(compact_data),
        }
        with progress_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(json.dumps(row, ensure_ascii=False), flush=True)

    try:
        payload = run_codex_skill_lab_batch(
            version=args.version,
            games=args.games,
            llm_provider=args.llm_provider,
            llm_model=args.llm_model,
            seed_offset=args.seed_offset,
            skip_preflight=not args.preflight,
            internal_llm_retries=args.internal_llm_retries,
            progress_callback=progress,
        )
        payload = to_jsonable(
            {
                **payload,
                "job": {
                    "version": args.version,
                    "games": args.games,
                    "llm_provider": args.llm_provider,
                    "llm_model": args.llm_model,
                    "seed_offset": args.seed_offset,
                    "skip_preflight": not args.preflight,
                    "internal_llm_retries": args.internal_llm_retries,
                    "progress_path": str(progress_path),
                    "review_pack_path": str(review_pack_path),
                },
            }
        )
        review_pack_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(
            json.dumps(
                {
                    "status": "completed",
                    "review_pack_path": str(review_pack_path),
                    "progress_path": str(progress_path),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    except Exception as exc:
        payload = {
            "status": "failed",
            "error": type(exc).__name__,
            "message": str(exc),
            "progress_path": str(progress_path),
            "error_path": str(error_path),
        }
        error_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(payload, ensure_ascii=False), flush=True)
        raise


def _compact_progress_data(data: dict[str, Any]) -> dict[str, Any]:
    compact = dict(data)
    compact.pop("strategy_versions", None)
    if "event" in compact and isinstance(compact["event"], dict):
        compact["event"] = _compact_event(compact["event"])
    if "summary" in compact and isinstance(compact["summary"], dict):
        summary = compact["summary"]
        compact["summary"] = {
            "day": summary.get("day"),
            "alive_count": summary.get("alive_count"),
            "dead_count": summary.get("dead_count"),
            "wolves_alive": summary.get("wolves_alive"),
            "civilians_alive": summary.get("civilians_alive"),
            "gods_alive": summary.get("gods_alive"),
            "winner": summary.get("winner"),
            "win_reason": summary.get("win_reason"),
        }
    if "players" in compact and isinstance(compact["players"], list):
        compact["players"] = [
            {
                "id": item.get("id"),
                "name": item.get("name"),
                "role": item.get("role"),
                "alive": item.get("alive"),
            }
            for item in compact["players"]
            if isinstance(item, dict)
        ]
    if "game" in compact and isinstance(compact["game"], dict):
        game = compact["game"]
        compact["game"] = {
            "game_id": game.get("game_id"),
            "seed": game.get("seed"),
            "version_id": game.get("version_id"),
            "winner": game.get("winner"),
            "win_reason": game.get("win_reason"),
            "day": game.get("day"),
        }
    return compact


def _compact_event(event: dict[str, Any]) -> dict[str, Any]:
    decision = event.get("decision") if isinstance(event.get("decision"), dict) else None
    metadata = (decision.get("metadata") if decision else None) or {}
    usage = metadata.get("skill_usage") if isinstance(metadata.get("skill_usage"), dict) else {}
    matched_skills = usage.get("matched_skills") if isinstance(usage.get("matched_skills"), list) else []
    compact_decision = None
    if decision:
        compact_decision = {
            "action": decision.get("action"),
            "target_id": decision.get("target_id"),
            "secondary_target_id": decision.get("secondary_target_id"),
            "speech": _short_text(decision.get("speech"), 220),
            "reason": _short_text(decision.get("reason"), 180),
            "confidence": decision.get("confidence"),
            "llm_total_elapsed_ms": metadata.get("llm_total_elapsed_ms"),
            "llm_call_count": metadata.get("llm_call_count"),
            "skill_recall_selected_count": metadata.get("skill_recall_selected_count"),
            "skill_recall_available_count": metadata.get("skill_recall_available_count"),
            "matched_skills": [
                {
                    "skill_id": skill.get("skill_id"),
                    "title": skill.get("title"),
                    "score": skill.get("score"),
                }
                for skill in matched_skills[:3]
                if isinstance(skill, dict)
            ],
        }
    return {
        "id": event.get("id"),
        "day": event.get("day"),
        "phase": event.get("phase"),
        "event_type": event.get("event_type"),
        "public_text": _short_text(event.get("public_text"), 220),
        "actor_id": event.get("actor_id"),
        "target_id": event.get("target_id"),
        "secondary_target_id": event.get("secondary_target_id"),
        "decision": compact_decision,
    }


def _short_text(value: Any, max_chars: int) -> str:
    text = str(value or "")
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3] + "..."


if __name__ == "__main__":
    main()
