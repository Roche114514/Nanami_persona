"""L0 环境感知与适应层参数管理（纯标准库）：
1. Itti-Koch 显著图 + Easterbrook 唤醒窄化（注意力与感官过滤）
2. Groves-Thompson 双过程习惯化 + Active Inference（陌生环境渐进适应）
3. Tolman 认知地图 + ACT-R 记忆激活衰减（局部空间记忆与防幻觉）
4. Borbély 双过程睡眠 + Roenneberg 时型（昼夜作息节律）

仅在角色处于「文字世界」中时生效；日常主对话完全不启用。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Mapping

WORLD_PERCEPTION_FILE = "world_perception.json"

DEFAULT_WORLD_PERCEPTION: dict[str, Any] = {
    "enabled": True,
    "sensory_radius": 5.0,
    "attentional_breadth": 4,
    "novelty_weight": 0.55,
    "ior_decay": 0.60,
    "initial_caution": 0.65,
    "habituation_rate": 0.25,
    "social_buffering": 0.60,
    "memory_decay": 0.45,
    "chronotype_offset": 0.0,
    "circadian_strength": 0.75,
}

BOUNDS: dict[str, tuple[float, float]] = {
    "sensory_radius": (1.5, 12.0),
    "attentional_breadth": (2.0, 8.0),
    "novelty_weight": (0.0, 1.0),
    "ior_decay": (0.0, 1.0),
    "initial_caution": (0.0, 1.0),
    "habituation_rate": (0.05, 0.80),
    "social_buffering": (0.0, 1.0),
    "memory_decay": (0.10, 0.90),
    "chronotype_offset": (-3.0, 3.0),
    "circadian_strength": (0.0, 1.0),
}


def perception_path(state_root: Path) -> Path:
    return Path(state_root) / "state" / WORLD_PERCEPTION_FILE


def _clamp_f(val: Any, lo: float, hi: float, default: float) -> float:
    try:
        num = float(val)
    except (TypeError, ValueError):
        return default
    if num != num:
        return default
    return round(max(lo, min(hi, num)), 3)


def coerce(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    src = raw if isinstance(raw, Mapping) else {}
    out = dict(DEFAULT_WORLD_PERCEPTION)
    if "enabled" in src:
        v = src["enabled"]
        if isinstance(v, bool):
            out["enabled"] = v
        elif isinstance(v, (int, float)):
            out["enabled"] = bool(v)
        else:
            out["enabled"] = str(v).strip().lower() not in ("false", "0", "no", "off", "")
    for key, (lo, hi) in BOUNDS.items():
        if key in src:
            val = _clamp_f(src[key], lo, hi, float(DEFAULT_WORLD_PERCEPTION[key]))
            out[key] = int(round(val)) if key == "attentional_breadth" else val
    return out


def load(state_root: Path) -> dict[str, Any]:
    path = perception_path(state_root)
    if not path.is_file():
        return dict(DEFAULT_WORLD_PERCEPTION)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return coerce(raw if isinstance(raw, Mapping) else {})
    except (OSError, ValueError):
        return dict(DEFAULT_WORLD_PERCEPTION)


def save(state_root: Path, patch: Mapping[str, Any] | None) -> dict[str, Any]:
    cur = load(state_root)
    if isinstance(patch, Mapping):
        cur.update({k: v for k, v in patch.items() if k in DEFAULT_WORLD_PERCEPTION})
    cleaned = coerce(cur)
    cleaned["updatedAt"] = round(time.time(), 3)
    path = perception_path(state_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(cleaned, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return cleaned


def reset(state_root: Path) -> dict[str, Any]:
    return save(state_root, DEFAULT_WORLD_PERCEPTION)


def infer_from_traits(state_root: Path, *, soul_text: str = "") -> dict[str, Any]:
    """根据角色现有 L1-L3 traits.json 与 soul.md 自动推填贴合性格的 L0 环境感知参数。"""
    traits_file = Path(state_root) / "state" / "traits.json"
    traits: dict[str, Any] = {}
    if traits_file.is_file():
        try:
            raw = json.loads(traits_file.read_text(encoding="utf-8"))
            traits = raw.get("traits") if isinstance(raw.get("traits"), dict) else (raw if isinstance(raw, dict) else {})
        except (OSError, ValueError):
            traits = {}

    need = traits.get("needProfile") if isinstance(traits.get("needProfile"), dict) else {}
    safety = float(need.get("safety", 0.50))
    relatedness = float(need.get("relatedness", 0.60))
    surgency = float(traits.get("surgency", 0.50))
    effortful = float(traits.get("effortfulControl", 0.65))
    arousal = float(traits.get("arousalBaseline", 0.45))

    txt = str(soul_text or "")
    night_owl = any(k in txt for k in ("熬夜", "通宵", "夜猫子", "游戏", "打到深夜", "困"))
    early_bird = any(k in txt for k in ("早起", "规律", "晨跑", "准时"))
    chrono = 1.5 if night_owl else (-1.0 if early_bird else 0.0)

    inferred = {
        "enabled": load(state_root).get("enabled", True),
        "sensory_radius": round(4.0 + surgency * 3.0 - (1.0 - effortful) * 0.5, 2),
        "attentional_breadth": max(2, min(7, int(round(3 + surgency * 2 + (1.0 - arousal) * 1)))),
        "novelty_weight": round(0.35 + surgency * 0.35 + safety * 0.15, 2),
        "ior_decay": round(0.45 + effortful * 0.30, 2),
        "initial_caution": round(max(0.15, min(0.95, 0.35 + safety * 0.55 - surgency * 0.25)), 2),
        "habituation_rate": round(max(0.08, min(0.65, 0.16 + surgency * 0.26 - safety * 0.12)), 2),
        "social_buffering": round(max(0.20, min(0.95, 0.35 + relatedness * 0.50)), 2),
        "memory_decay": round(max(0.20, min(0.75, 0.60 - effortful * 0.25)), 2),
        "chronotype_offset": chrono,
        "circadian_strength": round(max(0.45, min(0.95, 0.60 + effortful * 0.25)), 2),
    }
    return save(state_root, inferred)
