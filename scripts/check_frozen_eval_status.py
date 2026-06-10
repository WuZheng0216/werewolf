from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Check frozen-eval job status from file metadata only."
    )
    parser.add_argument("path", help="Frozen-eval output directory or a result/summary file")
    parser.add_argument("--pid", action="append", type=int, default=[], help="Optional process id to probe")
    parser.add_argument(
        "--expected-duration-seconds",
        type=int,
        default=_env_int("FROZEN_EVAL_EXPECTED_DURATION_SECONDS"),
        help="Expected wall-clock seconds for this run. Used only to recommend a low-frequency next check.",
    )
    parser.add_argument(
        "--min-check-interval-seconds",
        type=int,
        default=_env_int("FROZEN_EVAL_MIN_CHECK_INTERVAL_SECONDS", 600),
        help="Minimum recommended seconds between polling checks for active jobs.",
    )
    parser.add_argument(
        "--max-runtime-seconds",
        type=int,
        default=_env_int("FROZEN_EVAL_MAX_RUNTIME_SECONDS"),
        help="Configured hard runtime guard for the job. Used only for status warnings.",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    args = parser.parse_args()

    status = build_status(
        Path(args.path),
        pids=args.pid,
        expected_duration_seconds=args.expected_duration_seconds,
        min_check_interval_seconds=args.min_check_interval_seconds,
        max_runtime_seconds=args.max_runtime_seconds,
    )
    if args.json:
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return
    print(_format_text(status))


def build_status(
    path: Path,
    *,
    pids: list[int] | None = None,
    expected_duration_seconds: int | None = None,
    min_check_interval_seconds: int = 600,
    max_runtime_seconds: int | None = None,
) -> dict[str, Any]:
    pids = pids or []
    root = path.parent if path.is_file() else path
    if not root.exists():
        recommendation = _recommend_next_check(
            "missing",
            run_age_seconds=None,
            expected_duration_seconds=expected_duration_seconds,
            min_check_interval_seconds=min_check_interval_seconds,
            max_runtime_seconds=max_runtime_seconds,
        )
        return {
            "path": str(path),
            "root": str(root),
            "exists": False,
            "state": "missing",
            "message": "Output path does not exist.",
            "processes": _process_statuses(pids),
            "run_age_seconds": None,
            "expected_duration_seconds": expected_duration_seconds,
            "max_runtime_seconds": max_runtime_seconds,
            "runtime_budget": _runtime_budget(None, max_runtime_seconds),
            "next_check": recommendation,
        }

    files = [item for item in root.iterdir() if item.is_file()]
    result_files = _matching_metadata(files, "*_frozen_eval_result.json")
    summary_files = _matching_metadata(files, "*_compact_summary.json")
    gate_files = _matching_metadata(files, "*_frozen_eval_gate.json")
    review_pack_files = _matching_metadata(files, "*_skill_review_pack.json")
    error_files = _matching_metadata(files, "*_error.json")
    progress_files = _matching_metadata(files, "*_progress.jsonl")
    stdout_stderr_files = (
        _matching_metadata(files, "*.stdout")
        + _matching_metadata(files, "*.stderr")
        + _matching_metadata(files, "*stdout*.log")
        + _matching_metadata(files, "*stderr*.log")
    )
    newest = max((_metadata(item) for item in files), key=lambda item: item["mtime_epoch"], default=None)
    processes = _process_statuses(pids)
    state = _infer_state(result_files, summary_files, error_files, newest, processes)
    run_age_seconds = _run_age_seconds(progress_files, newest)
    recommendation = _recommend_next_check(
        state,
        run_age_seconds=run_age_seconds,
        expected_duration_seconds=expected_duration_seconds,
        min_check_interval_seconds=min_check_interval_seconds,
        max_runtime_seconds=max_runtime_seconds,
    )

    return {
        "path": str(path),
        "root": str(root),
        "exists": True,
        "state": state,
        "result_count": len(result_files),
        "result_files": result_files,
        "compact_summary_count": len(summary_files),
        "compact_summary_files": summary_files,
        "gate_count": len(gate_files),
        "gate_files": gate_files,
        "review_pack_count": len(review_pack_files),
        "review_pack_files": review_pack_files,
        "error_count": len(error_files),
        "error_files": error_files,
        "progress_count": len(progress_files),
        "progress_files": progress_files,
        "stdout_stderr_files": stdout_stderr_files,
        "newest_file": newest,
        "processes": processes,
        "run_age_seconds": run_age_seconds,
        "expected_duration_seconds": expected_duration_seconds,
        "max_runtime_seconds": max_runtime_seconds,
        "runtime_budget": _runtime_budget(run_age_seconds, max_runtime_seconds),
        "next_check": recommendation,
    }


def _matching_metadata(files: list[Path], pattern: str) -> list[dict[str, Any]]:
    return sorted(
        (_metadata(item) for item in files if item.match(pattern)),
        key=lambda item: item["mtime_epoch"],
    )


def _metadata(path: Path) -> dict[str, Any]:
    stat = path.stat()
    age_seconds = max(0.0, datetime.now().timestamp() - stat.st_mtime)
    return {
        "name": path.name,
        "path": str(path),
        "size_bytes": stat.st_size,
        "mtime": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        "mtime_epoch": stat.st_mtime,
        "age_seconds": round(age_seconds, 1),
    }


def _run_age_seconds(
    progress_files: list[dict[str, Any]],
    newest: dict[str, Any] | None,
) -> float | None:
    started_at = _started_at_epoch(progress_files, newest)
    if started_at is None:
        return None
    return round(max(0.0, datetime.now().timestamp() - started_at), 1)


def _started_at_epoch(
    progress_files: list[dict[str, Any]],
    newest: dict[str, Any] | None,
) -> float | None:
    if progress_files:
        parsed = [
            timestamp
            for timestamp in (_timestamp_from_name(item["name"]) for item in progress_files)
            if timestamp is not None
        ]
        if parsed:
            return min(parsed)
        return min(item["mtime_epoch"] for item in progress_files)
    if newest is None:
        return None
    return newest["mtime_epoch"]


def _timestamp_from_name(name: str) -> float | None:
    match = re.search(r"(?P<date>\d{8})_(?P<time>\d{6})", name)
    if not match:
        return None
    try:
        started = datetime.strptime(match.group("date") + match.group("time"), "%Y%m%d%H%M%S")
    except ValueError:
        return None
    return started.timestamp()


def _recommend_next_check(
    state: str,
    *,
    run_age_seconds: float | None,
    expected_duration_seconds: int | None,
    min_check_interval_seconds: int = 600,
    max_runtime_seconds: int | None = None,
) -> dict[str, Any]:
    if state in {"completed_summary_ready", "result_ready_summary_missing"}:
        return {
            "seconds": 0,
            "at": None,
            "reason": "result_artifact_ready",
        }
    if state == "missing":
        return {
            "seconds": None,
            "at": None,
            "reason": "path_missing_no_polling_needed",
        }

    min_interval = max(60, int(min_check_interval_seconds))
    max_runtime = max(0, int(max_runtime_seconds or 0))
    if max_runtime > 0 and run_age_seconds is not None:
        remaining_to_max = max_runtime - run_age_seconds
        if remaining_to_max <= 0:
            return {
                "seconds": 180,
                "at": datetime.fromtimestamp(datetime.now().timestamp() + 180).strftime("%Y-%m-%d %H:%M:%S"),
                "reason": "past_max_runtime_process_still_alive",
            }
        if remaining_to_max <= min_interval:
            seconds = max(60, int(remaining_to_max + 30))
            return {
                "seconds": seconds,
                "at": datetime.fromtimestamp(datetime.now().timestamp() + seconds).strftime("%Y-%m-%d %H:%M:%S"),
                "reason": "approaching_max_runtime",
            }

    expected = max(0, int(expected_duration_seconds or 0))
    if expected <= 0:
        seconds = min_interval
        reason = "active_or_waiting_without_expected_duration"
    elif run_age_seconds is None:
        seconds = min_interval
        reason = "active_or_waiting_without_start_time"
    elif run_age_seconds < expected * 0.65:
        seconds = max(min_interval, int(expected * 0.65 - run_age_seconds))
        reason = "well_before_expected_completion"
    elif run_age_seconds < expected:
        seconds = max(180, min(min_interval, int(expected - run_age_seconds)))
        reason = "approaching_expected_completion"
    elif run_age_seconds < expected * 1.5:
        seconds = 300
        reason = "past_expected_completion_check_soon"
    else:
        seconds = min_interval
        reason = "well_past_expected_but_no_result_yet"

    return {
        "seconds": seconds,
        "at": datetime.fromtimestamp(datetime.now().timestamp() + seconds).strftime("%Y-%m-%d %H:%M:%S"),
        "reason": reason,
    }


def _runtime_budget(run_age_seconds: float | None, max_runtime_seconds: int | None) -> dict[str, Any] | None:
    max_runtime = max(0, int(max_runtime_seconds or 0))
    if max_runtime <= 0:
        return None
    if run_age_seconds is None:
        return {
            "max_runtime_seconds": max_runtime,
            "remaining_seconds": None,
            "exceeded": None,
        }
    remaining = round(max_runtime - run_age_seconds, 1)
    return {
        "max_runtime_seconds": max_runtime,
        "remaining_seconds": remaining,
        "exceeded": remaining <= 0,
    }


def _infer_state(
    result_files: list[dict[str, Any]],
    summary_files: list[dict[str, Any]],
    error_files: list[dict[str, Any]],
    newest: dict[str, Any] | None,
    processes: list[dict[str, Any]] | None = None,
) -> str:
    if result_files and summary_files:
        return "completed_summary_ready"
    if result_files:
        return "result_ready_summary_missing"
    if any(item.get("alive") for item in (processes or [])):
        return "running_process_alive"
    if error_files:
        return "failed_error_artifact"
    if newest is None:
        return "empty"
    if newest["age_seconds"] <= 180:
        return "running_or_recently_updated"
    return "no_result_yet_stale_or_waiting"


def _process_statuses(pids: list[int]) -> list[dict[str, Any]]:
    return [{"pid": pid, "alive": _process_exists(pid)} for pid in pids]


def _process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(process_query_limited_information, False, int(pid))
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _env_int(name: str, default: int | None = None) -> int | None:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _format_text(status: dict[str, Any]) -> str:
    if not status.get("exists"):
        return f"{status['state']}: {status.get('message', '')}"
    newest = status.get("newest_file") or {}
    lines = [
        f"state: {status['state']}",
        f"root: {status['root']}",
        f"results: {status['result_count']}",
        f"compact summaries: {status['compact_summary_count']}",
        f"gates: {status.get('gate_count', 0)}",
        f"review packs: {status.get('review_pack_count', 0)}",
        f"errors: {status.get('error_count', 0)}",
        f"progress files: {status['progress_count']}",
    ]
    if newest:
        lines.append(
            "newest: "
            f"{newest['name']} ({newest['size_bytes']} bytes, "
            f"{newest['mtime']}, age {newest['age_seconds']}s)"
        )
    if status.get("run_age_seconds") is not None:
        lines.append(f"run age: {status['run_age_seconds']}s")
    if status.get("expected_duration_seconds"):
        lines.append(f"expected duration: {status['expected_duration_seconds']}s")
    runtime_budget = status.get("runtime_budget")
    if runtime_budget:
        remaining = runtime_budget.get("remaining_seconds")
        if remaining is None:
            lines.append(f"max runtime: {runtime_budget['max_runtime_seconds']}s")
        else:
            state = "exceeded" if runtime_budget.get("exceeded") else "remaining"
            lines.append(
                f"max runtime: {runtime_budget['max_runtime_seconds']}s "
                f"({state} {_format_duration(abs(remaining))})"
            )
    next_check = status.get("next_check") or {}
    if next_check:
        seconds = next_check.get("seconds")
        if seconds == 0:
            lines.append(f"next check: now ({next_check.get('reason')})")
        elif seconds is None:
            lines.append(f"next check: none ({next_check.get('reason')})")
        else:
            lines.append(
                f"next check: in {_format_duration(seconds)} "
                f"around {next_check.get('at')} ({next_check.get('reason')})"
            )
    if status.get("processes"):
        live = [str(item["pid"]) for item in status["processes"] if item.get("alive")]
        dead = [str(item["pid"]) for item in status["processes"] if not item.get("alive")]
        lines.append(f"alive pids: {', '.join(live) if live else 'none'}")
        lines.append(f"missing pids: {', '.join(dead) if dead else 'none'}")
    return "\n".join(lines)


def _format_duration(seconds: int | float) -> str:
    seconds = int(seconds)
    minutes, remainder = divmod(seconds, 60)
    if minutes and remainder:
        return f"{minutes}m{remainder}s"
    if minutes:
        return f"{minutes}m"
    return f"{seconds}s"


if __name__ == "__main__":
    main()
