from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from werewolf_ai.codex_skill_lab import run_codex_skill_lab_batch
from werewolf_ai.service import run_ab_demo, run_demo_game, run_evolution_demo, run_frozen_eval


def main() -> None:
    parser = argparse.ArgumentParser(description="AI Werewolf demo runner")
    subparsers = parser.add_subparsers(dest="command", required=True)

    game = subparsers.add_parser("game", help="Run one 9-player game")
    game.add_argument("--seed", type=int, default=None)
    game.add_argument("--version", default="initial")
    game.add_argument(
        "--llm-provider",
        choices=["ark", "doubao", "dashscope", "deepseek", "dashscope-native", "dashscope_native", "deepseek-v32"],
        default="ark",
    )
    game.add_argument("--llm-model", default=None)

    evolution = subparsers.add_parser("evolution", help="Run reflection/evolution loop")
    evolution.add_argument("--rounds", type=int, default=2)
    evolution.add_argument("--games", type=int, default=20)
    evolution.add_argument("--base-version", default="initial")
    evolution.add_argument("--llm-provider", default="ark")
    evolution.add_argument("--llm-model", default=None)
    evolution.add_argument("--evolution-mode", choices=["workflow", "skill"], default="workflow")

    ab = subparsers.add_parser("ab", help="Run initial vs evolved AB report")
    ab.add_argument("--games", type=int, default=20)

    frozen = subparsers.add_parser("frozen-eval", help="Run fixed-version evaluation without memory/evolution writes")
    frozen.add_argument("--version", default="latest")
    frozen.add_argument("--baseline-version", default="initial")
    frozen.add_argument("--games", type=int, default=20)
    frozen.add_argument("--llm-provider", default="ark")
    frozen.add_argument("--llm-model", default=None)

    codex_lab = subparsers.add_parser(
        "codex-skill-lab",
        help="Run Doubao games for Codex-side skill review without auto-evolution writes",
    )
    codex_lab.add_argument("--version", default="latest")
    codex_lab.add_argument("--games", type=int, default=5)
    codex_lab.add_argument("--llm-provider", default="ark")
    codex_lab.add_argument("--llm-model", default=None)
    codex_lab.add_argument("--seed-offset", type=int, default=80_000)
    codex_lab.add_argument("--preflight", action="store_true")
    codex_lab.add_argument("--internal-llm-retries", type=int, default=1)

    args = parser.parse_args()
    if args.command == "game":
        payload = run_demo_game(
            seed=args.seed,
            version=args.version,
            llm_provider=args.llm_provider,
            llm_model=args.llm_model,
        )
    elif args.command == "evolution":
        payload = run_evolution_demo(
            rounds=args.rounds,
            games_per_round=args.games,
            base_version=args.base_version,
            llm_provider=args.llm_provider,
            llm_model=args.llm_model,
            evolution_mode=args.evolution_mode,
        )
    elif args.command == "frozen-eval":
        payload = run_frozen_eval(
            version=args.version,
            baseline_version=args.baseline_version,
            games=args.games,
            llm_provider=args.llm_provider,
            llm_model=args.llm_model,
        )
    elif args.command == "codex-skill-lab":
        payload = run_codex_skill_lab_batch(
            version=args.version,
            games=args.games,
            llm_provider=args.llm_provider,
            llm_model=args.llm_model,
            seed_offset=args.seed_offset,
            skip_preflight=not args.preflight,
            internal_llm_retries=args.internal_llm_retries,
        )
    else:
        payload = run_ab_demo(games=args.games)

    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
