"""心理模型的量化参数：把 `TRAITS` 与几处阈值/衰减做成**可写的外部文件**。

为什么需要这一层：`TRAITS` 原本是 `toolkit/mind.mjs` 里的常量（气质、需要剖面、域效能…），
改一次要动源码。这里改成"外部覆盖表"：

    <state_root>/state/traits.json      ← 设置页写它
    mind.mjs 启动时读它，覆盖同名键；文件不存在就完全按原常量走（行为零变化）

所以：**页面上的每一项都能改，且不改模型代码；删掉这个文件就回到出厂值。**

字段清单与 `mind.mjs` 的 TRAITS 一一对应（那边是源头，这里是镜像；改那边记得同步这里）。
层级按模型注释里的 L1–L4 分。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

TRAITS_FILE = "traits.json"

# 出厂值（= mind.mjs 的 TRAITS）。层级注释是模型自己的分层口径。
DEFAULT_TRAITS: dict[str, Any] = {
    # L1 气质
    "effortfulControl": 0.8,        # 努力控制：先把想说的在心里整理（高）
    "arousalBaseline": 0.35,        # 基础唤醒：低（慢、困）
    # L3 自我结构
    "privateSelfConsciousness": 0.75,   # 私我意识：高（大量内省）
    "publicSelfConsciousness": 0.2,     # 公我意识：低（不做形象管理）
    "selfDisclosureThreshold": 0.8,     # 自我披露门槛：高（只在被问时才有）
    "domainEfficacy.game": 0.85,        # 域限定自我效能：游戏高
    "domainEfficacy.nonGame": 0.3,      # 非游戏低
    # L4 评价性担忧
    "evaluativeSensitivity": 0.7,   # 只对亲近者敏感
    # L2 需要剖面
    "needProfile.relatedness": 0.7,
    "needProfile.competence": 0.7,
    "needProfile.safety": 0.5,
    "needProfile.autonomy": "low",
    # L4 社会效能（"接不住话"的可算来源）
    "socialEfficacy": 0.3,
    "surgency": 0.25,               # 外向度：低（话慢、不表演）
    # 伤与信念的阈值/衰减（标定产物，见 mind.mjs 注释：换卷子要重标）
    "woundThreshold": 0.6,
    "woundGameDecay": 0.7,
    "woundHumanDecay": 0.5,
    "woundSkillDecay": 0.7,
}

# 层的展示信息（设置页分组用）
LAYERS: tuple[dict[str, Any], ...] = (
    {
        "id": "L1",
        "title": "L1 气质（快慢与自制）",
        "description": "努力控制高＝先在心里整理再开口；唤醒低＝话慢、容易困。",
        "keys": ("effortfulControl", "arousalBaseline"),
    },
    {
        "id": "L2",
        "title": "L2 需要剖面（她缺什么）",
        "description": "关系性与胜任高、自主低（顺从型）；社会效能与外向度低，是她「接不住话」的可算来源。",
        "keys": (
            "needProfile.relatedness",
            "needProfile.competence",
            "needProfile.safety",
            "needProfile.autonomy",
            "socialEfficacy",
            "surgency",
        ),
    },
    {
        "id": "L3",
        "title": "L3 自我结构（她怎么评价自己）",
        "description": "自我价值锚在「游戏胜任」和「不给人添麻烦」两个域；域限定自我效能就体现在这里。",
        "keys": (
            "domainEfficacy.game",
            "domainEfficacy.nonGame",
            "privateSelfConsciousness",
            "publicSelfConsciousness",
            "selfDisclosureThreshold",
        ),
    },
    {
        "id": "L4",
        "title": "L4 评价性担忧（她在乎谁怎么看）",
        "description": "只对亲近者敏感；三种疼的阈值与衰减是标定产物，换语料要重标。",
        "keys": (
            "evaluativeSensitivity",
            "woundThreshold",
            "woundGameDecay",
            "woundHumanDecay",
            "woundSkillDecay",
        ),
    },
)

LAYER_OF = {key: layer["id"] for layer in LAYERS for key in layer["keys"]}


def traits_path(state_root: Path) -> Path:
    """与 `mind.json` 同级：`<state_root>/state/traits.json`（mind.mjs 也是从那里读）。"""
    return Path(state_root) / "state" / TRAITS_FILE


def _read_raw(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception:
        logger.exception("nanami_persona: traits.json 读坏了，按出厂值处理 %s", path)
        return {}
    if isinstance(raw, dict) and isinstance(raw.get("traits"), dict):
        return dict(raw["traits"])
    return dict(raw) if isinstance(raw, dict) else {}


def effective(state_root: Path) -> dict[str, Any]:
    """出厂值 + 文件覆盖（页面要显示"现在到底是多少"）。"""
    out = dict(DEFAULT_TRAITS)
    for key, value in _flatten(_read_raw(traits_path(state_root))).items():
        if key in DEFAULT_TRAITS:
            out[key] = value
    return out


def _flatten(patch: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in patch.items():
        if isinstance(value, dict):
            for inner, inner_value in value.items():
                out[f"{key}.{inner}"] = inner_value
        else:
            out[str(key)] = value
    return out


def _unflatten(flat: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in flat.items():
        if "." in key:
            head, tail = key.split(".", 1)
            out.setdefault(head, {})
            if isinstance(out[head], dict):
                out[head][tail] = value
        else:
            out[key] = value
    return out


def coerce(values: dict[str, Any]) -> dict[str, Any]:
    """按出厂值的类型折算：数字夹在 0–1（autonomy 是枚举）。"""
    out: dict[str, Any] = {}
    for key, base in DEFAULT_TRAITS.items():
        if key not in values:
            continue
        raw = values.get(key)
        if isinstance(base, str):
            text = str(raw or "").strip().lower()
            out[key] = text if text in ("low", "medium", "high") else base
            continue
        try:
            number = float(raw)
        except (TypeError, ValueError):
            continue
        out[key] = round(max(0.0, min(1.0, number)), 3)
    return out


def save(state_root: Path, values: dict[str, Any]) -> None:
    """写覆盖表。只写**和出厂值不同**的项；全一样就删文件（回到出厂）。"""
    flat = _flatten(coerce(values))
    patch = {key: value for key, value in flat.items() if DEFAULT_TRAITS.get(key) != value}
    path = traits_path(state_root)
    try:
        if not patch:
            if path.is_file():
                path.unlink()
                logger.info("nanami_persona: 参数全回出厂值，已删除 %s", path)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "traits": _unflatten(patch),
                   "note": "心理模型量化参数的覆盖表；删除本文件即回到出厂值。"}
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, path)
        logger.info("nanami_persona: 已保存 %d 项参数 → %s", len(patch), path)
    except OSError:
        logger.exception("nanami_persona: 保存参数失败 %s", path)
