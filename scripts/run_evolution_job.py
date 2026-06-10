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

from werewolf_ai.evolution import EvolutionManager
from werewolf_ai.llm import load_dotenv
from werewolf_ai.models import to_jsonable


def main() -> None:
    load_dotenv()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Run an AI Werewolf evolution job and persist progress.")
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--games", type=int, default=10, help="Games per version in each evolution round.")
    parser.add_argument("--base-version", default="latest")
    parser.add_argument("--llm-provider", default="ark")
    parser.add_argument("--llm-model", default=None)
    parser.add_argument("--evolution-mode", choices=["workflow", "skill"], default="workflow")
    parser.add_argument("--seed", type=int, default=20260521)
    parser.add_argument("--output-dir", default="logs/evolution_jobs")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    progress_path = output_dir / f"{stamp}_progress.jsonl"
    result_path = output_dir / f"{stamp}_result.json"
    error_path = output_dir / f"{stamp}_error.json"

    def progress(event_name: str, data: dict[str, Any]) -> None:
        row = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "event": event_name,
            "data": to_jsonable(data),
        }
        with progress_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(json.dumps(row, ensure_ascii=False), flush=True)

    try:
        manager = EvolutionManager(
            seed=args.seed,
            llm_provider=args.llm_provider,
            llm_model=args.llm_model,
            evolution_mode=args.evolution_mode,
        )
        result = manager.run_evolution(
            rounds=args.rounds,
            games_per_round=args.games,
            base_version=args.base_version,
            progress_callback=progress,
        )
        payload = to_jsonable(
            {
                **result,
                "job": {
                    "rounds": args.rounds,
                    "games_per_round": args.games,
                    "base_version": args.base_version,
                    "llm_provider": args.llm_provider,
                    "llm_model": args.llm_model,
                    "evolution_mode": result.get("evolution_mode", args.evolution_mode),
                    "seed": args.seed,
                    "progress_path": str(progress_path),
                    "result_path": str(result_path),
                },
            }
        )
        result_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"status": "completed", "result_path": str(result_path)}, ensure_ascii=False), flush=True)
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


if __name__ == "__main__":
    main()
