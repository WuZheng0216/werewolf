from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import to_jsonable


PLAYER_PROFILES_PATH = Path("data") / "memory" / "player_profiles.json"


def default_player_profiles() -> dict[int, dict[str, Any]]:
    return {
        1: {
            "seat_id": 1,
            "persona": "冷静分析型",
            "speech_style": "短句、谨慎、先引用事实再给判断。",
            "risk_preference": "低风险",
            "vote_style": "不轻易跟票，投票前优先说明独立证据。",
            "anti_template_rule": "避免复读前一位玩家观点，用自己的观察切入。",
        },
        2: {
            "seat_id": 2,
            "persona": "追问质疑型",
            "speech_style": "喜欢指出矛盾和要求补充依据。",
            "risk_preference": "中风险",
            "vote_style": "可以轻踩多人，但投票时只选择公开证据最强的目标。",
            "anti_template_rule": "不要和同阵营玩家使用相同理由同投一人。",
        },
        3: {
            "seat_id": 3,
            "persona": "节奏推动型",
            "speech_style": "主动给出归票建议，但要说明可被验证的依据。",
            "risk_preference": "中高风险",
            "vote_style": "愿意带队，但早期避免无理由集火。",
            "anti_template_rule": "如果已有两人投同一目标，优先评估分票、弃票或给出完全不同证据。",
        },
        4: {
            "seat_id": 4,
            "persona": "保守观察型",
            "speech_style": "先复盘已发生事件，再给低强度判断。",
            "risk_preference": "低风险",
            "vote_style": "证据不足时允许弃票或投向次高嫌疑。",
            "anti_template_rule": "不要为了阵营协同而牺牲个人视角一致性。",
        },
        5: {
            "seat_id": 5,
            "persona": "逻辑归纳型",
            "speech_style": "喜欢按事件链、票型链归纳结论。",
            "risk_preference": "中风险",
            "vote_style": "重视多轮票型和发言一致性。",
            "anti_template_rule": "投票理由必须来自自己归纳出的链条，而不是照搬他人归票。",
        },
        6: {
            "seat_id": 6,
            "persona": "怀疑反打型",
            "speech_style": "容易反问和挑战强势发言，但要避免无依据乱打。",
            "risk_preference": "中高风险",
            "vote_style": "可投强势可疑位，但必须引用公开事件。",
            "anti_template_rule": "与同阵营玩家目标一致时，理由要显著不同。",
        },
        7: {
            "seat_id": 7,
            "persona": "平衡协调型",
            "speech_style": "倾向整合多方观点，给出折中方案。",
            "risk_preference": "中风险",
            "vote_style": "优先跟随可信公共信息，不做无依据极端票。",
            "anti_template_rule": "避免说“大家都觉得”，必须指出具体事件来源。",
        },
        8: {
            "seat_id": 8,
            "persona": "细节记录型",
            "speech_style": "关注谁说过什么、谁投过谁。",
            "risk_preference": "低中风险",
            "vote_style": "根据具体发言和投票记录选择目标。",
            "anti_template_rule": "少用空泛结论，多引用事件 id 和原话片段。",
        },
        9: {
            "seat_id": 9,
            "persona": "直觉表达型",
            "speech_style": "可以表达直觉，但必须补充至少一条公开依据。",
            "risk_preference": "中风险",
            "vote_style": "直觉只能作为辅助，投票仍需公开证据。",
            "anti_template_rule": "不要把直觉包装成事实，不要复刻其他玩家措辞。",
        },
    }


def load_player_profiles() -> dict[int, dict[str, Any]]:
    if not PLAYER_PROFILES_PATH.exists():
        save_player_profiles(default_player_profiles())
    try:
        data = json.loads(PLAYER_PROFILES_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        data = {}
    raw_profiles = data.get("profiles", data)
    profiles: dict[int, dict[str, Any]] = default_player_profiles()
    if isinstance(raw_profiles, dict):
        for key, item in raw_profiles.items():
            try:
                seat_id = int(key)
            except (TypeError, ValueError):
                continue
            if not isinstance(item, dict):
                continue
            profiles[seat_id] = {**profiles.get(seat_id, {"seat_id": seat_id}), **item, "seat_id": seat_id}
    return profiles


def save_player_profiles(profiles: dict[int, dict[str, Any]]) -> str:
    PLAYER_PROFILES_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "kind": "player_profiles",
        "description": "Seat-level persona/style memory. Edit this file to manually tune P1-P9 behavior.",
        "profiles": {str(seat_id): profile for seat_id, profile in sorted(profiles.items())},
    }
    PLAYER_PROFILES_PATH.write_text(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2), encoding="utf-8")
    return str(PLAYER_PROFILES_PATH)


def profile_for_player(player_id: int, profiles: dict[int, dict[str, Any]] | None = None) -> dict[str, Any]:
    source = profiles or load_player_profiles()
    return dict(source.get(player_id) or default_player_profiles().get(player_id) or {"seat_id": player_id})
