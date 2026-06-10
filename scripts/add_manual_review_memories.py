from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any


MEMORY_BANK_PATH = Path("data") / "memory" / "memory_bank.json"
MANUAL_DIR = Path("data") / "memory" / "manual_reviews"
SOURCE_RUN_FILE = "data/memory/evolution_runs/run_20260524_185658_465152_evolved_r1.json"
SOURCE_ARCHIVES = [
    "logs/games/20260524_183752_110772_game-20280522-0a0a46.json",
    "logs/games/20260524_185658_352050_game-20280523-1f94a3.json",
    "logs/games/20260524_175818_920774_game-20270522-3a036d.json",
]

CORRUPTED_MANUAL_IDS = {
    "villager-4070548a518f",
    "villager-69f9504cd180",
    "seer-3362cb9b0fd4",
    "witch-f24601908c43",
    "hunter-99fbcdac0270",
    "werewolf-eef8e302d1e5",
}


MANUAL_REVIEW = {
    "schema_version": 1,
    "kind": "codex_manual_review",
    "source_run_file": SOURCE_RUN_FILE,
    "source_archives": SOURCE_ARCHIVES,
    "summary": (
        "最新仿真显示规则边界已稳定，无警徽流污染；主要问题转向好人侧对唯一预言家查杀的优先级不足、"
        "女巫跳身份信息不完整、猎人开枪证据不足，以及狼队仍需避免共边票型。"
    ),
    "observations": [
        "evolved_r1 三局好人全胜，综合评分 82.75，失误率 0.67，表现稳定。",
        "evolved_r2 三局狼人全胜但综合评分 72.10，票质 57.65，失误率 4.0，因此未晋级。",
        "game-20280522 中唯一预言家 P7 查杀 P1，但好人被 P3/P5 的模板化软嫌疑带偏，连续放逐猎人和女巫。",
        "game-20280523 中 P8 查杀 P5 后死亡，好人能推出 P3，但猎人死亡后开枪带走好人 P7，最终被狼队抗推 P4。",
    ],
    "memory_candidates": [
        {
            "role": "villager",
            "memory": "当场上只有唯一预言家起跳并给出明确查杀，且没有强对跳或硬反证时，查杀目标优先级应高于模板化发言、划水等软嫌疑；若不投查杀，必须明确说明该预言家为何不可信。",
            "confidence": 0.97,
            "evidence_event_ids": [15, 18, 19, 21, 23, 27],
            "evidence_note": "game-20280522：P7 单跳查杀 P1 后，好人转投 P3 导致猎人出局。",
        },
        {
            "role": "villager",
            "memory": "模板化发言和划水是追问线索，不是硬身份信息；白天归票时应优先比较查验结果、夜间倒牌、对跳关系和投票链，再处理发言风格问题。",
            "confidence": 0.96,
            "evidence_event_ids": [14, 15, 18, 27, 35, 48],
            "evidence_note": "game-20280522：好人连续把发言模板化作为主线，忽略更硬的查杀和倒牌信息。",
        },
        {
            "role": "seer",
            "memory": "预言家报查杀时要把夜间查验结果和公开发言辅助判断分开表述，明确查验结果才是核心证据，并反复要求好人优先归票查杀目标，防止狼队用软嫌疑分票。",
            "confidence": 0.96,
            "evidence_event_ids": [15, 16, 24, 25, 27],
            "evidence_note": "game-20280522/20280523：查杀已报出，但部分好人因软嫌疑弃票或转票。",
        },
        {
            "role": "witch",
            "memory": "女巫被质疑后跳身份时要一次性报完整时间线：解药是否使用、毒药是否使用及目标、为何当时没有站边；半截披露会被狼队抓住漏洞抗推。",
            "confidence": 0.96,
            "evidence_event_ids": [38, 39, 40, 41, 48],
            "evidence_note": "game-20280522：P5 跳女巫时没有完整说明毒药目标，被狼队和好人共同抗推出局。",
        },
        {
            "role": "hunter",
            "memory": "猎人死亡开枪前应按证据强度排序目标：公开查杀、冲票真预言家、与已出局狼共票、多轮带错节奏优先；只有软嫌疑或身份不明时应谨慎开枪。",
            "confidence": 0.95,
            "evidence_event_ids": [53, 54, 56, 57, 58],
            "evidence_note": "game-20280523：P2 猎人死亡后带走好人 P7，削弱好人轮次。",
        },
        {
            "role": "werewolf",
            "memory": "狼队可以利用好人对模板化发言、划水和解释不完整的敏感，把归票从查杀狼转向软嫌疑目标；但参与带票的狼人必须使用不同公开理由并分散票型，避免暴露共边。",
            "confidence": 0.95,
            "evidence_event_ids": [17, 18, 25, 40, 41, 47],
            "evidence_note": "game-20280522：狼队成功把好人注意力从查杀转向 P3/P5，但仍多次出现共边票型风险。",
        },
    ],
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Write Codex-curated manual review memories.")
    parser.add_argument("--manual-file", type=Path, default=None)
    args = parser.parse_args()

    now = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    manual_path = args.manual_file or MANUAL_DIR / f"manual_review_{now}.json"
    MANUAL_DIR.mkdir(parents=True, exist_ok=True)

    bank = _read_bank()
    items = bank.setdefault("items", [])
    items[:] = [
        item
        for item in items
        if item.get("id") not in CORRUPTED_MANUAL_IDS and not _is_corrupted_manual_item(item)
    ]

    saved_ids: list[str] = []
    for candidate in MANUAL_REVIEW["memory_candidates"]:
        item = _upsert_memory_item(items, candidate, now, manual_path)
        saved_ids.append(str(item["id"]))

    bank["updated_at"] = now
    bank["items"] = sorted(
        items,
        key=lambda item: (str(item.get("role")), -float(item.get("confidence", 0.0)), str(item.get("memory"))),
    )
    _write_json(MEMORY_BANK_PATH, bank)

    payload = dict(MANUAL_REVIEW)
    payload["created_at"] = now
    payload["memory_bank_item_ids"] = saved_ids
    _write_json(manual_path, payload)

    print(json.dumps({"manual_review_file": str(manual_path), "saved_ids": saved_ids}, ensure_ascii=False, indent=2))
    return 0


def _read_bank() -> dict[str, Any]:
    if not MEMORY_BANK_PATH.exists():
        return {"schema_version": 1, "kind": "memory_bank", "items": []}
    return json.loads(MEMORY_BANK_PATH.read_text(encoding="utf-8"))


def _upsert_memory_item(items: list[dict[str, Any]], candidate: dict[str, Any], now: str, manual_path: Path) -> dict[str, Any]:
    role = str(candidate["role"])
    memory = str(candidate["memory"]).strip()
    normalized = _normalize(memory)
    item = _find_item(items, role, normalized)
    source = {
        "version_id": "codex_manual_review",
        "game_id": "run_20260524_185658_465152_evolved_r1",
        "batch": {"source": "codex_manual_review", "manual_review_file": str(manual_path)},
        "archive_path": SOURCE_RUN_FILE,
        "source_archives": SOURCE_ARCHIVES,
        "evidence_event_ids": list(candidate.get("evidence_event_ids") or []),
        "evidence_note": candidate.get("evidence_note"),
        "confidence": float(candidate["confidence"]),
        "created_at": now,
    }
    if item is None:
        item = {
            "id": _memory_item_id(role, normalized),
            "role": role,
            "memory": memory,
            "normalized_memory": normalized,
            "status": "approved_auto",
            "confidence": float(candidate["confidence"]),
            "source": "codex_manual_review",
            "created_at": now,
            "updated_at": now,
            "seen_count": 1,
            "sources": [source],
        }
        items.append(item)
    else:
        item["updated_at"] = now
        item["seen_count"] = int(item.get("seen_count", 0)) + 1
        item["confidence"] = max(float(item.get("confidence", 0.0)), float(candidate["confidence"]))
        item["status"] = "approved_auto"
        item.setdefault("sources", []).append(source)
    return item


def _find_item(items: list[dict[str, Any]], role: str, normalized: str) -> dict[str, Any] | None:
    for item in items:
        if item.get("role") == role and item.get("normalized_memory") == normalized:
            return item
    return None


def _is_corrupted_manual_item(item: dict[str, Any]) -> bool:
    if item.get("source") != "codex_manual_review":
        return False
    memory = str(item.get("memory") or "")
    return memory and set(memory) <= {"?"}


def _memory_item_id(role: str, normalized: str) -> str:
    digest = hashlib.sha1(f"{role}:{normalized}".encode("utf-8")).hexdigest()[:12]
    return f"{role}-{digest}"


def _normalize(text: str) -> str:
    return "".join(text.strip().lower().split())


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
