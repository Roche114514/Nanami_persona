"""心理模型设置页：把模型的**量化属性按层**摆出来，可调、可回出厂。

和「人格模型设置」分开：那一页管接线（守卫/契约/判官/称呼），这一页只管**这个心理模型本身**：

    L1 气质（快慢与自制）        effortfulControl / arousalBaseline
    L2 需要剖面（她缺什么）      relatedness / competence / safety / autonomy / socialEfficacy / surgency
    L3 自我结构（怎么评价自己）  domainEfficacy(game, nonGame) / 私我·公我意识 / 披露门槛
    L4 评价性担忧（在乎谁怎么看）evaluativeSensitivity / 三种疼的阈值与衰减
    当下状态（只读）            energy / need / moodV / moodA / agenda …（从 mind.json 读）

写入走 `mind_params.save()` → `<state_root>/state/traits.json`，
mind.mjs 启动时贴上覆盖（文件不存在＝完全按出厂常量，行为零变化）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from . import mind_params

logger = logging.getLogger(__name__)

PAGE_ID = "mind_settings"
PAGE_TITLE = "心理模型"

# 只读展示的当下状态（换一行说明它是"量"不是"设定"）
READONLY_FIELDS: tuple[tuple[str, str], ...] = (
    ("energy", "精力（会被「他在说话」的地板抬到 0.35）"),
    ("need", "睡眠压力（0 睡饱 / 1 很困）"),
    ("moodV", "心情效价（-1 低 / +1 高）"),
    ("moodA", "心情唤醒（0 平静 / 1 高）"),
    ("moodWarmth", "温度（冷—热）"),
    ("woundGame", "「只会游戏」那根刺"),
    ("woundHuman", "「是不是人」那根刺"),
    ("woundSkill", "「你连这个都不会」那根刺"),
    ("agenda", "这一轮想干什么"),
)


def _field_for(key: str, value: Any) -> dict[str, Any]:
    label = _label_of(key)
    if isinstance(value, str):
        return {
            "key": key, "label": label, "type": "select",
            "defaultValue": value,
            "options": [{"value": one, "label": one} for one in ("low", "medium", "high")],
            "description": _description_of(key),
        }
    return {
        "key": key, "label": label, "type": "number",
        "defaultValue": float(value), "min": 0.0, "max": 1.0, "step": 0.05,
        "description": _description_of(key),
    }


_LABELS = {
    "effortfulControl": "努力控制",
    "arousalBaseline": "基础唤醒",
    "needProfile.relatedness": "关系需要",
    "needProfile.competence": "胜任需要",
    "needProfile.safety": "安全需要",
    "needProfile.autonomy": "自主需要",
    "socialEfficacy": "社会效能",
    "surgency": "外向度",
    "domainEfficacy.game": "游戏域效能",
    "domainEfficacy.nonGame": "非游戏域效能",
    "privateSelfConsciousness": "私我意识",
    "publicSelfConsciousness": "公我意识",
    "selfDisclosureThreshold": "自我披露门槛",
    "evaluativeSensitivity": "评价性敏感",
    "woundThreshold": "疼的阈值",
    "woundGameDecay": "「只会游戏」衰减",
    "woundHumanDecay": "「是不是人」衰减",
    "woundSkillDecay": "「不会这个」衰减",
}

_DESCRIPTIONS = {
    "effortfulControl": "高＝先想再开口",
    "arousalBaseline": "低＝话慢·易困",
    "needProfile.relatedness": "想要「有人在」",
    "needProfile.competence": "锚在游戏这一域",
    "needProfile.safety": "被否定时先缩回",
    "needProfile.autonomy": "低＝顺从型",
    "socialEfficacy": "低＝不抢话·先问",
    "surgency": "低＝话慢·不表演",
    "domainEfficacy.game": "游戏域·高",
    "domainEfficacy.nonGame": "非游戏域·低",
    "privateSelfConsciousness": "高＝大量内省",
    "publicSelfConsciousness": "低＝不做形象管理",
    "selfDisclosureThreshold": "高＝被问才说",
    "evaluativeSensitivity": "只对亲近者敏感",
    "woundThreshold": "超阈值才进卡片",
    "woundGameDecay": "每轮乘这个系数",
    "woundHumanDecay": "每轮乘这个系数",
    "woundSkillDecay": "每轮乘这个系数",
}


def _label_of(key: str) -> str:
    return _LABELS.get(key, key.split(".")[-1])


def _description_of(key: str) -> str:
    return _DESCRIPTIONS.get(key, "")


def build_schema(values: dict[str, Any], live_state: dict[str, Any]) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    for layer in mind_params.LAYERS:
        fields = []
        for key in layer["keys"]:
            if key in values:
                fields.append(_field_for(key, values[key]))
        if fields:
            groups.append({
                "id": layer["id"],
                "title": layer["title"],
                "description": layer["description"],
                "fields": fields,
            })

    readonly = []
    for key, label in READONLY_FIELDS:
        value = live_state.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            readonly.append({"key": f"state.{key}", "label": label, "type": "number",
                             "defaultValue": round(float(value), 2), "min": -1.0, "max": 1.0,
                             "step": 0.01, "description": "只读·每轮算的量"})
        elif isinstance(value, str) and value:
            readonly.append({"key": f"state.{key}", "label": label, "type": "text",
                             "defaultValue": value, "description": "只读·每轮算的量"})
    if readonly:
        groups.append({
            "id": "now",
            "title": "当下状态（只读）",
            "description": "这些是每轮算出来的量，不是设定；要让她累一点，请调上面的气质或去找她说话。",
            "fields": readonly,
        })
    return groups


def values_of(effective: dict[str, Any]) -> dict[str, Any]:
    return {key: effective[key] for key in mind_params.DEFAULT_TRAITS if key in effective}


def _live_state(state_root: Path, chat_key_prefix: str) -> dict[str, Any]:
    """读最近活跃的那个会话的状态（页面只用来展示）。"""
    path = Path(state_root) / "state" / "mind.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict) or not raw:
        return {}
    entries = [
        (str(key), value) for key, value in raw.items()
        if isinstance(value, dict) and (not chat_key_prefix or str(key).startswith(chat_key_prefix))
    ]
    if not entries:
        entries = [(str(key), value) for key, value in raw.items() if isinstance(value, dict)]
    if not entries:
        return {}
    entries.sort(key=lambda pair: float(pair[1].get("lastAt") or pair[1].get("lastHerAt") or 0), reverse=True)
    return dict(entries[0][1])


def register_page(register, state_root: Path, chat_key_prefix: str) -> bool:
    try:
        from sdk.types import FrontendConfigContribution

        schema = build_schema(values_of(mind_params.effective(state_root)),
                              _live_state(state_root, chat_key_prefix))

        def _load() -> dict[str, Any]:
            current = values_of(mind_params.effective(state_root))
            fresh = build_schema(current, _live_state(state_root, chat_key_prefix))
            if fresh and schema != fresh:
                schema[:] = fresh
            # 只读项也一起给值，页面才显示得出来
            state = _live_state(state_root, chat_key_prefix)
            for key, _label in READONLY_FIELDS:
                if key in state:
                    current[f"state.{key}"] = state[key]
            return current

        def _save(values: Any) -> None:
            patch = {key: values.get(key) for key in mind_params.DEFAULT_TRAITS if key in values}
            mind_params.save(state_root, patch)

        register.register_frontend_config_page(
            FrontendConfigContribution(
                page_id=PAGE_ID,
                title=PAGE_TITLE,
                description=(
                    "这个心理模型的量化属性，按 L1 气质 / L2 需要 / L3 自我 / L4 评价性担忧 分层。"
                    "保存写入 <state_root>/state/traits.json，下一轮生效；删掉那个文件就回到出厂值。"
                ),
                schema=schema,
                load_values=_load,
                save_values=_save,
                kind="settings",
                order=41.0,
            )
        )
        logger.info("nanami_persona: 已注册「%s」页面（%d 层）", PAGE_TITLE, len(schema))
        return True
    except Exception:  # noqa: BLE001
        logger.exception("nanami_persona: 注册心理模型设置页失败")
        return False
