"""社会学模型设置页：人际吸引模型（S1）、人际关系模型（S2）、人际互动与防御模型（S3）。

与「心理模型」（L1–L8）强关联：
  - 共享的 L8 社会动机与人际边界参数双向同步写入 `traits.json` 与 `social_traits.json`；
  - 从 `<state_root>/state/mind.json` 实时读取人际吸引、厌恶排斥、关系阶段、冷静戒备与防御层级。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from . import mind_page, social_params

logger = logging.getLogger(__name__)

PAGE_ID = "social_settings"
PAGE_TITLE = "社会学模型"

_LABELS: dict[str, str] = {
    "attraction.familiarityWeight": "暴露熟悉度吸引权重",
    "attraction.similarityWeight": "兴趣与三观共鸣权重",
    "attraction.competenceWeight": "能力 / 靠谱度吸引权重",
    "attraction.warmthWeight": "善意照料与温暖权重",
    "attraction.repulsionPenalty": "冒犯扣分 / 厌恶积累强度",
    "attraction.friendThreshold": "跨入友好接纳门槛",
    "relationship.trustGainRate": "正向信任累积速率",
    "relationship.grudgeDecay": "生气 / 厌恶余波保留率",
    "relationship.repairThreshold": "道歉修复所需诚意门槛",
    "relationship.coolOffBase": "冒犯后冷静戒备时长（0–8 轮映射）",
    "interaction.defenseTrigger": "显性防御与冷拒触发线",
    "interaction.retaliationDrive": "受挫硬气顶回 vs 冷淡拉距倾向",
    **{k: mind_page._LABELS[k] for k in social_params.SHARED_L8_KEYS if k in mind_page._LABELS},
}

_DESCRIPTIONS: dict[str, str] = {
    "attraction.familiarityWeight": "随对话轮次自然增加熟悉感的权重（过高易变得自来熟）",
    "attraction.similarityWeight": "共同话题与想法共鸣对人际吸引的拉升力度",
    "attraction.competenceWeight": "对方靠谱/有见地时产生的钦佩吸引权重",
    "attraction.warmthWeight": "对方温柔体贴、赠送心意时拉升吸引的权重",
    "attraction.repulsionPenalty": "被贬低、命令或否定存在时，厌恶排斥（Repulsion）抬升倍率",
    "attraction.friendThreshold": "净吸引（吸引-厌恶）低于此线时拒绝普通交友邀约",
    "relationship.trustGainRate": "友好互动下信任与亲近关系的推进步长",
    "relationship.grudgeDecay": "每轮自然消气保留系数：0.86=生气余波持续多轮，0.20=一秒消气",
    "relationship.repairThreshold": "处于戒备/反感态时，道歉抵扣所需连续诚意度",
    "relationship.coolOffBase": "触发连续挑衅后强制进入冷静戒备期的轮次长度（×8 换算）",
    "interaction.defenseTrigger": "厌恶或边界受损达到此值时自动锁住迎合、进入冷拒/设防",
    "interaction.retaliationDrive": "高=更倾向硬气顶回/驳斥；低=更倾向冷淡少话、拉远距离",
    **{k: mind_page._DESCRIPTIONS[k] for k in social_params.SHARED_L8_KEYS if k in mind_page._DESCRIPTIONS},
}

READONLY_FIELDS: tuple[tuple[str, str], ...] = (
    ("attraction", "人际吸引分 Attraction（0–1）"),
    ("repulsion", "厌恶 / 排斥分 Repulsion（0–1）"),
    ("netAttraction", "净人际吸引 Net Attraction（-1 ~ +1）"),
    ("relStage", "当前人际关系阶段（Relationship Stage）"),
    ("coolOffTurns", "冷静戒备剩余轮次（Cool-off）"),
    ("exchangeBalance", "社会交换对等账本（-1 失衡 ~ +1 互惠）"),
    ("defenseTier", "当前互动防御层级（Defense Tier）"),
    ("interactionPosture", "人际互动姿态（Interaction Posture）"),
    ("attitudeStance", "长时态度档位（Attitude Stance）"),
    ("attitudeScore", "综合态度分（-1 反感 ~ +1 信任）"),
    ("affectiveValence", "好感度 / 情感评价（-1 ~ +1）"),
    ("respectTrust", "尊重与安全信任度（0–1）"),
    ("perceivedSincerity", "诚意 / 靠谱度感知（0–1）"),
    ("relationalDebt", "累积心结账本（0–1，极慢衰减）"),
    ("disengaged", "是否已行使离开权限（不再搭理）"),
    ("disengageTurns", "离开后保持沉默轮次"),
)


def _live_social_state(state_root: Path, chat_key_prefix: str) -> dict[str, Any]:
    path = Path(state_root) / "state" / "mind.json"
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict) or not raw:
        return {}
    candidates = [
        (key, entry)
        for key, entry in raw.items()
        if isinstance(entry, dict) and key.startswith(chat_key_prefix)
    ] or [(key, entry) for key, entry in raw.items() if isinstance(entry, dict)]
    if not candidates:
        return {}
    _chat_key, entry = max(candidates, key=lambda kv: float(kv[1].get("updatedAt") or 0))
    soc_model = entry.get("sociology") if isinstance(entry.get("sociology"), dict) else {}
    att_model = entry.get("attitude") if isinstance(entry.get("attitude"), dict) else {}
    merged = dict(soc_model)
    for k in (
        "attitudeStance",
        "attitudeScore",
        "affectiveValence",
        "respectTrust",
        "perceivedSincerity",
        "relationalDebt",
        "disengaged",
        "disengageTurns",
    ):
        if k in att_model:
            merged[k] = att_model[k]
    return merged


def build_schema(
    values: dict[str, Any],
    live_state: dict[str, Any],
    *,
    pointer_options: list[dict[str, str]] | None = None,
    current_pointer_id: str = "",
) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    if pointer_options:
        groups.append({
            "id": "target_pointer",
            "title": "目标角色",
            "description": "切换正在编辑的社会学模型参数所属角色指针。",
            "fields": [{
                "key": "_pointer_id",
                "label": "目标角色",
                "type": "select",
                "defaultValue": current_pointer_id or pointer_options[0]["value"],
                "options": pointer_options,
                "description": "切换角色只加载该角色的社会学参数，不覆盖他人",
            }],
        })

    for layer in social_params.LAYERS:
        fields = [
            {
                "key": key,
                "label": _LABELS.get(key, key),
                "type": "number",
                "defaultValue": float(values.get(key, social_params.DEFAULT_SOCIAL_TRAITS.get(key, 0.5))),
                "min": 0.0,
                "max": 1.0,
                "step": 0.05,
                "description": _DESCRIPTIONS.get(key, ""),
            }
            for key in layer["keys"]
        ]
        groups.append({
            "id": layer["id"],
            "title": layer["title"],
            "description": layer["description"],
            "fields": fields,
        })

    if live_state:
        ro_fields = [
            {
                "key": f"soc.{k}",
                "label": label,
                "type": "text",
                "defaultValue": str(live_state[k]),
                "readonly": True,
                "description": "来自最近一次对话的社会学模型实时快照",
            }
            for k, label in READONLY_FIELDS
            if k in live_state
        ]
        if ro_fields:
            groups.append({
                "id": "live_sociology",
                "title": "当下人际关系与防御实时快照（只读）",
                "description": "由人际吸引、关系账本与互动防御三层协同算出的当前状态。",
                "fields": ro_fields,
            })

    return groups


def register_page(register, state_root: Path, chat_key_prefix: str, *, registry=None, on_changed=None) -> bool:
    try:
        from sdk.types import FrontendConfigContribution, FrontendPageContribution

        def _active_target():
            if registry is None:
                return state_root, chat_key_prefix, None, [], ""
            pointers = registry.list_pointers()
            active_id = registry.active_edit_id()
            pointer = registry.get_pointer(active_id) or (pointers[0] if pointers else None)
            if pointer is None:
                return state_root, chat_key_prefix, None, [], ""
            profile = registry.load_profile(pointer)
            p_root = registry.resolve_state_root(pointer)
            p_prefix = pointer.chat_key_prefix or chat_key_prefix
            seed = profile.resolve_path(profile.traits_file, plugin_root=registry.plugin_root)
            opts = [{"value": p.id, "label": f"{p.character_name or p.id}（{p.id}）"} for p in pointers]
            return p_root, p_prefix, seed, opts, pointer.id

        init_root, init_prefix, init_seed, init_opts, init_pid = _active_target()
        init_eff = social_params.effective(init_root, seed_path=init_seed)
        schema = build_schema(
            init_eff,
            _live_social_state(init_root, init_prefix),
            pointer_options=init_opts,
            current_pointer_id=init_pid,
        )

        def _load() -> dict[str, Any]:
            t_root, t_prefix, seed, opts, pid = _active_target()
            eff = social_params.effective(t_root, seed_path=seed)
            live = _live_social_state(t_root, t_prefix)
            fresh = build_schema(eff, live, pointer_options=opts, current_pointer_id=pid)
            if fresh and schema != fresh:
                schema[:] = fresh
            current: dict[str, Any] = dict(eff)
            for k, _lbl in READONLY_FIELDS:
                if k in live:
                    current[f"soc.{k}"] = live[k]
            if pid:
                current["_pointer_id"] = pid
            return current

        def _save(values: Any) -> None:
            t_root, _t_prefix, seed, _opts, current_pid = _active_target()
            if registry is not None and isinstance(values, dict) and "_pointer_id" in values:
                picked = str(values.get("_pointer_id") or "").strip()
                if picked and picked != current_pid and registry.set_active_edit(picked):
                    _load()
                    return
            if isinstance(values, dict):
                patch = {k: values.get(k) for k in social_params.DEFAULT_SOCIAL_TRAITS if k in values}
                social_params.save(t_root, patch, seed_path=seed)
            _load()
            if on_changed is not None:
                on_changed()

        social_html = Path(__file__).resolve().parent / "web" / "social.html"
        if social_html.is_file() and hasattr(register, "register_frontend_page"):
            register.register_frontend_page(
                FrontendPageContribution(
                    page_id=PAGE_ID,
                    title=PAGE_TITLE,
                    entry=str(social_html),
                    kind="settings",
                    description="人际吸引（S1）、人际关系（S2）与人际互动防御（S3）社会学模型，与心理模型强关联。",
                    order=42.0,
                )
            )
        register.register_frontend_config_page(
            FrontendConfigContribution(
                page_id=PAGE_ID,
                title=PAGE_TITLE,
                description="配置角色的人际吸引权重、生气消散速率、修复门槛与社会互动防御模型，与心理模型 L8 双向同步。",
                schema=schema,
                load_values=_load,
                save_values=_save,
                kind="settings",
                order=42.0,
            )
        )
        logger.info("nanami_persona: 已注册「%s」页面（%d 组）", PAGE_TITLE, len(schema))
        return True
    except Exception:  # noqa: BLE001
        logger.exception("nanami_persona: 注册社会学模型设置页失败")
        return False
