from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from werewolf_ai.skill_change_log import DEFAULT_SKILL_CHANGE_LOG, append_skill_change, load_skill_change_log


def main() -> None:
    parser = argparse.ArgumentParser(description="Append or inspect structured AI Werewolf skill change records.")
    parser.add_argument("--input", help="JSON file containing a skill change entry to append.")
    parser.add_argument("--log", default=str(DEFAULT_SKILL_CHANGE_LOG), help="Skill change log path.")
    parser.add_argument("--show", action="store_true", help="Print the current change log.")
    args = parser.parse_args()

    log_path = Path(args.log)
    if args.show:
        print(json.dumps(load_skill_change_log(log_path), ensure_ascii=False, indent=2))
        return
    if not args.input:
        parser.error("--input is required unless --show is used")
    entry = _read_entry(Path(args.input))
    saved = append_skill_change(entry, log_path)
    print(json.dumps({"saved": saved, "log": str(log_path)}, ensure_ascii=False, indent=2))


def _read_entry(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Skill change entry must be a JSON object")
    return payload


if __name__ == "__main__":
    main()
