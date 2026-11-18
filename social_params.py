"""社会学模型量化参数（人际吸引 / 人际关系 / 人际互动）读写与校验。

独立持久化于 `<state_root>/state/social_traits.json`，与心理模型 `traits.json`
强关联（共享的社会动机与边界字段双向同步），并由 `toolkit/mind.mjs` 在每轮
推理前统一加载，参与人际吸引计算、关系阶段跃迁与防御阻尼门控。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from . import mind_params

logger = logging.getLogger(__name__)

SOCIAL_TRAITS_FILE = "social_traits.json"

DEFAULT_SOCIAL_TRAITS: dict[str, float] = {
    # ── S1 人际吸引模型（Interpersonal Attraction）────────────────────
    "attraction.familiarityWeight": 0.45,   # 暴露效应 / 熟悉度吸引权重
    "attraction.similarityWeight": 0.65,    # 价值观与兴趣共鸣吸引权重
    "attraction.competenceWeight": 0.50,    # 能力 / 靠谱度吸引权重
    "attraction.warmthWeight": 0.55,        # 善意照料与温度吸引权重
    "attraction.repulsionPenalty": 0.75,    # 冒犯/贬低导致的厌恶排斥惩罚强度
    "attraction.friendThreshold": 0.42,     # 跨入友好/愿意并肩玩耍的吸引门槛
    # ── S2 人际关系模型（Interpersonal Relationship）──────────────────
    "relationship.trustGainRate": 0.45,     # 正向互动累积信任的速率
    "relationship.grudgeDecay": 0.86,       # 厌恶/生气余波每轮保留率（越高消气越慢）
    "relationship.repairThreshold": 0.62,   # 受伤/戒备后道歉修复所需诚意门槛
    "relationship.coolOffBase": 0.375,      # 连续冒犯触发冷静戒备的时长比例（0–1 映射 0–8 轮，0.375 ≈ 3 轮）
    # ── S3 人际互动模型（Social Interaction & Defense）──────────────
    "interaction.defenseTrigger": 0.45,     # 触发显性防御与冷拒的排斥阈值
    "interaction.retaliationDrive": 0.35,   # 受挫时硬气顶回 vs 冷淡拉距的倾向
    # ── 与 L8 心理模型双向同步的核心社会学因子 ──────────────────────
    "social.affiliation": 0.60,
    "social.achievement": 0.65,
    "social.power": 0.30,
    "social.aggression": 0.15,
    "social.prosocial": 0.75,
    "social.exclusivityNeed": 0.45,
    "social.reciprocitySensitivity": 0.70,
    "social.normCompliance": 0.75,
}

# 与 mind_params.DEFAULT_TRAITS 共享的 L8 键（双向联动）
SHARED_L8_KEYS: tuple[str, ...] = (
    "social.affiliation",
    "social.achievement",
    "social.power",
    "social.aggression",
    "social.prosocial",
    "social.exclusivityNeed",
    "social.reciprocitySensitivity",
    "social.normCompliance",
)

LAYERS: tuple[dict[str, Any], ...] = (
    {
        "id": "S1",
        "title": "S1 人际吸引模型（吸引来源与冒犯排斥）",
        "description": "决定角色看重哪些特质而产生好感，以及被冒犯、否定或居高临下对待时厌恶感（Repulsion）抬升有多快。",
        "keys": (
            "attraction.familiarityWeight",
            "attraction.similarityWeight",
            "attraction.competenceWeight",
            "attraction.warmthWeight",
            "attraction.repulsionPenalty",
            "attraction.friendThreshold",
        ),
    },
    {
        "id": "S2",
        "title": "S2 人际关系模型（关系跃迁、消气速度与修复门槛）",
        "description": "控制信任积累快慢、被惹生气后余波保留多久（grudgeDecay），以及对方道歉后需多久才解除冷静戒备。",
        "keys": (
            "relationship.trustGainRate",
            "relationship.grudgeDecay",
            "relationship.repairThreshold",
            "relationship.coolOffBase",
        ),
    },
    {
        "id": "S3",
        "title": "S3 人际互动与社会动机（防御门槛、边界与五大动机）",
        "description": "控制防御触发线与硬气顶回倾向；以下五大社会动机与人际边界直接与心理模型 L8 双向同步。",
        "keys": (
            "interaction.defenseTrigger",
            "interaction.retaliationDrive",
            *SHARED_L8_KEYS,
        ),
    },
)


def social_traits_path(state_root: Path) -> Path:
    return Path(state_root) / "state" / SOCIAL_TRAITS_FILE


def _read_raw(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception:
        logger.debug("nanami_persona: 读取 social_traits.json 失败 %s", path, exc_info=True)
        return {}
    if isinstance(raw, dict) and isinstance(raw.get("socialTraits"), dict):
        return dict(raw["socialTraits"])
    return dict(raw) if isinstance(raw, dict) else {}


def effective(state_root: Path, *, seed_path: Path | None = None) -> dict[str, float]:
    out = dict(DEFAULT_SOCIAL_TRAITS)
    # 先从心理模型 traits.json 继承共享 L8 键，保持强关联
    mind_eff = mind_params.effective(state_root, seed_path=seed_path)
    for k in SHARED_L8_KEYS:
        if k in mind_eff:
            out[k] = float(mind_eff[k])
    target = social_traits_path(state_root)
    for k, v in mind_params._flatten(_read_raw(target)).items():
        if k in DEFAULT_SOCIAL_TRAITS:
            try:
                out[k] = round(max(0.0, min(1.0, float(v))), 3)
            except (TypeError, ValueError):
                pass
    return out


def coerce(values: dict[str, Any]) -> dict[str, float]:
    out: dict[str, float] = {}
    for key in DEFAULT_SOCIAL_TRAITS:
        if key not in values:
            continue
        try:
            num = float(values[key])
        except (TypeError, ValueError):
            continue
        out[key] = round(max(0.0, min(1.0, num)), 3)
    return out


def save(state_root: Path, values: dict[str, Any], *, seed_path: Path | None = None) -> None:
    """写入 social_traits.json，并将共享 L8 键双向同步回心理模型 traits.json。"""
    flat = mind_params._flatten(coerce(values))
    patch = {k: v for k, v in flat.items() if DEFAULT_SOCIAL_TRAITS.get(k) != v}
    path = social_traits_path(state_root)
    try:
        if not patch:
            if path.is_file():
                path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": 1,
                "socialTraits": mind_params._unflatten(patch),
                "note": "社会学模型量化参数（人际吸引 / 关系 / 互动）；与心理模型 L8 强联动。",
            }
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
    except OSError:
        logger.exception("nanami_persona: 保存 social_traits.json 失败 %s", path)

    # 双向同步 L8 到心理模型 traits.json
    shared_patch = {k: flat[k] for k in SHARED_L8_KEYS if k in flat}
    if shared_patch:
        cur_mind = mind_params.effective(state_root, seed_path=seed_path)
        cur_mind.update(shared_patch)
        mind_params.save(state_root, cur_mind)
