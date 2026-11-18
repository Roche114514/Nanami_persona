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
    # L2 需要剖面（马斯洛需要层次：安全 / 归属 / 胜任 / 尊重 / 自我实现 / 自我超越 + 自主性）
    "needProfile.relatedness": 0.7,
    "needProfile.competence": 0.7,
    "needProfile.safety": 0.5,
    "needProfile.esteem": 0.55,
    "needProfile.selfActualization": 0.85,
    "needProfile.transcendence": 0.85,
    "needProfile.autonomy": "low",
    # L4 社会效能（"接不住话"的可算来源）
    "socialEfficacy": 0.3,
    "surgency": 0.25,               # 外向度：低（话慢、不表演）
    # 伤与信念的阈值/衰减（标定产物，见 mind.mjs 注释：换卷子要重标）
    "woundThreshold": 0.6,
    "woundGameDecay": 0.7,
    "woundHumanDecay": 0.5,
    "woundSkillDecay": 0.7,
    # L5 ACT（情感控制理论）三维 EPA 基准与偏离平衡
    "act.evalAnchor": 0.70,
    "act.potencyAnchor": 0.48,
    "act.activityAnchor": 0.36,
    "act.deflectionThreshold": 0.32,
    "act.restoreStrength": 0.65,
    # L6 动力学稳态、图式与语用节律
    "dynamics.hysteresisInertia": 0.65,
    "dynamics.schemaPermission": 0.75,
    "dynamics.questionRestraint": 0.60,
    "dynamics.ellipsisBudget": 0.60,
    "dynamics.verboseTighten": 0.50,
    # L7 依恋风格与斯腾伯格浪漫发展（全通用参数，由角色包覆盖具体倾向）
    "attachment.anxiety": 0.25,
    "attachment.avoidance": 0.55,
    "attachment.romanceOpenness": 0.40,
    "attachment.romanceUnlockThreshold": 0.55,
    "attachment.romancePace": 0.50,
    # L8 社会动机与互动结构（与 L2 马斯洛/L7 依恋纵向耦合：五大社会动机 + 三个调节边界）
    "social.affiliation": 0.60,
    "social.achievement": 0.65,
    "social.power": 0.30,
    "social.aggression": 0.15,
    "social.prosocial": 0.75,
    "social.exclusivityNeed": 0.45,
    "social.reciprocitySensitivity": 0.70,
    "social.normCompliance": 0.75,
}

# 层的展示信息（通用心理模型设置页分组用）
LAYERS: tuple[dict[str, Any], ...] = (
    {
        "id": "L1",
        "title": "L1 气质基线（反应快慢与自制力）",
        "description": "努力控制决定先思后言的抑制深度；基础唤醒决定语速快慢、活跃基线与易疲劳程度。",
        "keys": ("effortfulControl", "arousalBaseline"),
    },
    {
        "id": "L2",
        "title": "L2 马斯洛心理需要层次（安全 / 归属 / 尊重 / 自我实现 / 自我超越）",
        "description": "量化从安全、归属、胜任、尊严到自我实现与超越利他的层次权重；高价值冲突下自我实现与超越可压过安全需要。",
        "keys": (
            "needProfile.safety",
            "needProfile.relatedness",
            "needProfile.competence",
            "needProfile.esteem",
            "needProfile.selfActualization",
            "needProfile.transcendence",
            "needProfile.autonomy",
            "socialEfficacy",
            "surgency",
        ),
    },
    {
        "id": "L3",
        "title": "L3 自我结构（效能锚点与披露阈值）",
        "description": "划分主优势域与泛域的自我效能差异，以及私我内省、公我形象管理与自我披露门槛。",
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
        "title": "L4 评价敏感与衰减（防御与恢复）",
        "description": "对他人评价的敏感权重，以及三类常见评价敏感点被触发后的每轮自然衰减率。",
        "keys": (
            "evaluativeSensitivity",
            "woundThreshold",
            "woundGameDecay",
            "woundHumanDecay",
            "woundSkillDecay",
        ),
    },
    {
        "id": "L5",
        "title": "L5 ACT 情感控制（语义锚点与偏离张力）",
        "description": "Affect Control Theory 三维 EPA 基准点（0.5 为中性）及偏离纠偏力度，统一驱动面对过捧、越界或冷场的平衡方向。",
        "keys": (
            "act.evalAnchor",
            "act.potencyAnchor",
            "act.activityAnchor",
            "act.deflectionThreshold",
            "act.restoreStrength",
        ),
    },
    {
        "id": "L6",
        "title": "L6 稳态吸引盆与语用节律（惯性、图式与节奏）",
        "description": "控制防备/投入稳态切换的滞回阻尼、陌生域直觉类比开放度，以及近轮发问/省略号/句幅的语用节流。",
        "keys": (
            "dynamics.hysteresisInertia",
            "dynamics.schemaPermission",
            "dynamics.questionRestraint",
            "dynamics.ellipsisBudget",
            "dynamics.verboseTighten",
        ),
    },
    {
        "id": "L7",
        "title": "L7 依恋风格与恋爱发展引擎（Attachment × Sternberg）",
        "description": "量化成人依恋二维（焦虑/回避）与斯腾伯格爱的三角演化开关：决定保持纯友人线，还是跨过好感门槛后依速率乘子自然生发亲密、心动与承诺。",
        "keys": (
            "attachment.anxiety",
            "attachment.avoidance",
            "attachment.romanceOpenness",
            "attachment.romanceUnlockThreshold",
            "attachment.romancePace",
        ),
    },
    {
        "id": "L8",
        "title": "L8 社会动机模型（亲和 / 成就 / 权力 / 侵犯 / 利他 × 社会边界）",
        "description": "承接 L2 马斯洛需要与 L7 依恋状态，在社会交互面转化为亲和、成就、权力、挫折—侵犯、亲社会五大社会动机，并受排他、对等敏度与规范阻尼调制。",
        "keys": (
            "social.affiliation",
            "social.achievement",
            "social.power",
            "social.aggression",
            "social.prosocial",
            "social.exclusivityNeed",
            "social.reciprocitySensitivity",
            "social.normCompliance",
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


def effective(state_root: Path, *, seed_path: Path | None = None) -> dict[str, Any]:
    """出厂值 + 文件覆盖（页面要显示"现在到底是多少"）。

    若 `<state_root>/state/traits.json` 尚不存在、但角色包自带了 `seed_path`（如自定义角色模板），
    会先把角色包的默认特质当作基底读进来，仍保证删除运行时文件即可回退到该角色基线。
    """
    out = dict(DEFAULT_TRAITS)
    target = traits_path(state_root)
    if not target.is_file() and seed_path is not None and Path(seed_path).is_file():
        for key, value in _flatten(_read_raw(Path(seed_path))).items():
            if key in DEFAULT_TRAITS:
                out[key] = value
    for key, value in _flatten(_read_raw(target)).items():
        if key in DEFAULT_TRAITS:
            out[key] = value
    return out


def ensure_seeded(state_root: Path, seed_path: Path | None) -> None:
    """若目标指针的 `<state_root>/state/traits.json` 不存在且角色包自带 `seed_path`，复制一份作为初值。"""
    if seed_path is None or not Path(seed_path).is_file():
        return
    target = traits_path(state_root)
    if target.is_file() or target.resolve() == Path(seed_path).resolve():
        return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(Path(seed_path).read_text(encoding="utf-8"), encoding="utf-8")
    except OSError:
        logger.debug("nanami_persona: 复制种子 traits.json 失败", exc_info=True)


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
