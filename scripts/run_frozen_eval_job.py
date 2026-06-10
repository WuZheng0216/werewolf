from __future__ import annotations

import argparse
import json
import os
import sys
import time
from contextlib import ExitStack, contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.build_frozen_eval_review_pack import build_review_pack
from scripts.gate_frozen_eval_summary import gate_summary
from scripts.summarize_frozen_eval_result import summarize
from werewolf_ai.llm import load_dotenv
from werewolf_ai.models import to_jsonable
from werewolf_ai.service import run_frozen_eval


def main() -> None:
    load_dotenv()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Run frozen eval with progress/result/error artifacts.")
    parser.add_argument("--version", required=True)
    parser.add_argument("--baseline-version", required=True)
    parser.add_argument("--games", type=int, default=1)
    parser.add_argument("--llm-provider", default="ark")
    parser.add_argument("--llm-model", default=None)
    parser.add_argument("--output-dir", default="logs/frozen_eval_smoke")
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--internal-llm-retries", type=int, default=1)
    parser.add_argument("--no-compact-summary", action="store_true")
    parser.add_argument(
        "--no-review-pack",
        action="store_true",
        help="Skip compact target-mistake review pack generation after the result is written.",
    )
    parser.add_argument(
        "--max-runtime-seconds",
        type=float,
        default=None,
        help="Optional wall-clock limit for the job. Checked from progress callbacks; 0 disables it.",
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    progress_path = output_dir / f"{stamp}_progress.jsonl"
    result_path = output_dir / f"{stamp}_frozen_eval_result.json"
    summary_path = output_dir / f"{stamp}_frozen_eval_result_compact_summary.json"
    gate_path = output_dir / f"{stamp}_frozen_eval_gate.json"
    review_pack_path = output_dir / f"{stamp}_frozen_eval_result_skill_review_pack.json"
    error_path = output_dir / f"{stamp}_error.json"
    started_at = time.monotonic()
    max_runtime_seconds = _resolve_max_runtime_seconds(args.max_runtime_seconds)

    def progress(event_name: str, data: dict[str, Any]) -> None:
        _raise_if_runtime_exceeded(started_at, max_runtime_seconds)
        row = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "event": event_name,
            "data": to_jsonable(_compact_progress_data(data)),
        }
        with progress_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(json.dumps(row, ensure_ascii=False), flush=True)

    try:
        with _temporary_env_overrides(
            {
                "LLM_PREFLIGHT_ENABLED": "true" if args.preflight else "false",
                **_retry_env_overrides(args.llm_provider, args.llm_model, args.internal_llm_retries),
            }
        ):
            payload = run_frozen_eval(
                version=args.version,
                baseline_version=args.baseline_version,
                games=args.games,
                llm_provider=args.llm_provider,
                llm_model=args.llm_model,
                progress_callback=progress,
            )
        payload = to_jsonable(
            {
                **payload,
                "job": {
                    "version": args.version,
                    "baseline_version": args.baseline_version,
                    "games": args.games,
                    "llm_provider": args.llm_provider,
                    "llm_model": args.llm_model,
                    "skip_preflight": not args.preflight,
                    "internal_llm_retries": args.internal_llm_retries,
                    "max_runtime_seconds": max_runtime_seconds,
                    "progress_path": str(progress_path),
                    "result_path": str(result_path),
                    "compact_summary_path": str(summary_path) if not args.no_compact_summary else None,
                    "gate_path": str(gate_path) if not args.no_compact_summary else None,
                    "review_pack_path": str(review_pack_path) if not args.no_review_pack else None,
                },
            }
        )
        result_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        if not args.no_compact_summary:
            summary = summarize(payload, result_path=result_path)
            summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            gate = gate_summary(summary, summary_path=summary_path)
            gate_path.write_text(json.dumps(gate, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if not args.no_review_pack:
            review_pack = build_review_pack(result_path)
            review_pack_path.write_text(json.dumps(review_pack, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(
            json.dumps(
                {
                    "status": "completed",
                    "result_path": str(result_path),
                    "progress_path": str(progress_path),
                    "compact_summary_path": str(summary_path) if not args.no_compact_summary else None,
                    "gate_path": str(gate_path) if not args.no_compact_summary else None,
                    "review_pack_path": str(review_pack_path) if not args.no_review_pack else None,
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
    if "aggregate" in compact and isinstance(compact["aggregate"], dict):
        aggregate = compact["aggregate"]
        compact["aggregate"] = {
            "average_scores": aggregate.get("average_scores"),
            "winner_counts": aggregate.get("winner_counts"),
            "mistake_counts": aggregate.get("mistake_counts"),
            "mistakes_per_game": aggregate.get("mistakes_per_game"),
        }
    return compact


def _resolve_max_runtime_seconds(value: float | None) -> float | None:
    if value is None:
        raw = os.environ.get("FROZEN_EVAL_MAX_RUNTIME_SECONDS")
        if raw is None or not raw.strip():
            return None
        try:
            value = float(raw)
        except ValueError:
            return None
    if value is None or value <= 0:
        return None
    return value


def _raise_if_runtime_exceeded(started_at: float, max_runtime_seconds: float | None) -> None:
    if max_runtime_seconds is None:
        return
    elapsed = time.monotonic() - started_at
    if elapsed > max_runtime_seconds:
        raise TimeoutError(
            f"frozen eval exceeded max runtime: elapsed={elapsed:.1f}s limit={max_runtime_seconds:.1f}s"
        )


def _retry_env_overrides(provider: str, model: str | None, retries: int | None) -> dict[str, str]:
    if retries is None:
        return {}
    provider_key = (provider or "").strip().lower().replace("_", "-")
    model_key = (model or "").strip().lower()
    value = str(retries)
    if provider_key in {"ark", "doubao"}:
        return {"ARK_MAX_RETRIES": value}
    if provider_key in {"dashscope-native", "dashscope-generation", "deepseek-v32", "deepseek-v3.2"}:
        return {"DASHSCOPE_GENERATION_MAX_RETRIES": value}
    if provider_key in {"dashscope", "deepseek", "aliyun", "bailian"} and "v3.2" in model_key:
        return {"DASHSCOPE_GENERATION_MAX_RETRIES": value}
    if provider_key in {"dashscope", "deepseek", "aliyun", "bailian"}:
        return {"DASHSCOPE_MAX_RETRIES": value}
    return {}


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


@contextmanager
def _temporary_env_overrides(values: dict[str, str | None]):
    with ExitStack() as stack:
        for key, value in values.items():
            stack.enter_context(_temporary_env(key, value))
        yield


if __name__ == "__main__":
    main()
