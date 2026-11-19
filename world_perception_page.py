"""环境感知与适应层独立设置页（L0）：
- 总开关（仅在角色处于「文字世界」中时生效，日常对话不启用）
- Itti-Koch + Easterbrook（注意力显著度与感官门控）
- Groves-Thompson + Active Inference（陌生环境渐进适应曲线）
- Tolman + ACT-R（局部认知地图与记忆激活衰减）
- Borbély + Roenneberg（双过程昼夜作息与时型）
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from . import world_perception

logger = logging.getLogger(__name__)

PAGE_ID = "world_perception_settings"
PAGE_TITLE = "环境感知与适应层"

_STAGE_CN = {
    "unfamiliar": "初入陌生期（警惕/探索优先）",
    "exploring": "试探适应期（逐步尝试偏好物件）",
    "familiar": "熟悉放松期（日常偏好主导）",
    "settled": "归属稳态期（安全基地）",
}


def _live_world_perception_summary(project_root: Path, character_name: str) -> dict[str, str]:
    p_file = project_root / "data" / "plugins" / "community.nanami.world" / "perception_state.json"
    if not p_file.is_file():
        return {}
    try:
        raw = json.loads(p_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    worlds = raw.get("worlds") if isinstance(raw.get("worlds"), dict) else {}
    lines: list[str] = []
    for sid, actors_map in worlds.items():
        if not isinstance(actors_map, dict):
            continue
        for aid, st in actors_map.items():
            if not isinstance(st, dict):
                continue
            cname = str(st.get("host_character") or st.get("actor_name") or "").strip()
            if character_name and cname and cname != character_name:
                continue
            fam = float(st.get("familiarity") or 0.0)
            stage = _STAGE_CN.get(str(st.get("stage") or "unfamiliar"), str(st.get("stage") or ""))
            mmap = st.get("mental_map") if isinstance(st.get("mental_map"), dict) else {}
            lines.append(f"[{sid}/{aid}] 熟悉度={fam:.2f}｜{stage}｜已记住物件={len(mmap)}件")
    return {"live.summary": "；".join(lines) if lines else "暂无该角色在文字世界中的适应记录"}


def build_schema(
    eff: dict[str, Any],
    live_info: dict[str, str],
    *,
    pointer_options: list[dict[str, str]] | None = None,
    current_pointer_id: str = "",
) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = [
        {
            "id": "l0_switch",
            "title": "环境感知总开关（仅世界内生效）",
            "description": "开启后，角色在文字世界中将遵循注意力过滤、陌生适应曲线与昼夜生物钟；日常主聊天不受影响。",
            "fields": [
                {
                    "key": "enabled",
                    "label": "启用 L0 环境感知与适应层",
                    "type": "boolean",
                    "defaultValue": bool(eff.get("enabled", True)),
                    "description": "关闭后角色在文字世界中退回基础直觉行为",
                }
            ],
        },
        {
            "id": "l0_attention",
            "title": "1. 注意力与感官门控（Itti–Koch 显著图 + Easterbrook 唤醒窄化）",
            "description": "决定角色进屋后会注意到哪几样物件，而不是全知扫描整个房间。",
            "fields": [
                {
                    "key": "sensory_radius",
                    "label": "感官注意半径（格：1.5–12.0）",
                    "type": "number",
                    "min": 1.5,
                    "max": 12.0,
                    "step": 0.5,
                    "defaultValue": float(eff.get("sensory_radius", 5.0)),
                    "description": "距离越远、困倦或专心打游戏时有效注意半径会自动收窄",
                },
                {
                    "key": "attentional_breadth",
                    "label": "工作记忆焦点上限 Top-K（2–8 件）",
                    "type": "integer",
                    "min": 2,
                    "max": 8,
                    "step": 1,
                    "defaultValue": int(eff.get("attentional_breadth", 4)),
                    "description": "同一时刻真正进入角色意识并影响对话的最高显著度物品数量",
                },
                {
                    "key": "novelty_weight",
                    "label": "新异/变化刺激权重（0–1）",
                    "type": "number",
                    "min": 0.0,
                    "max": 1.0,
                    "step": 0.05,
                    "defaultValue": float(eff.get("novelty_weight", 0.55)),
                    "description": "房间光线变化、刚被移动或尚未细看过的物体吸引注意力的权重",
                },
                {
                    "key": "ior_decay",
                    "label": "返回抑制 IOR 强度（0–1）",
                    "type": "number",
                    "min": 0.0,
                    "max": 1.0,
                    "step": 0.05,
                    "defaultValue": float(eff.get("ior_decay", 0.60)),
                    "description": "刚交互/刚看过的物品短期内降低显著度，避免一直死磕同一件物体",
                },
            ],
        },
        {
            "id": "l0_adaptation",
            "title": "2. 陌生环境适应曲线（Groves–Thompson 习惯化 + Active Inference）",
            "description": "模拟人进入陌生空间时从警惕观察、试探熟悉到放松归属的渐进过程。",
            "fields": [
                {
                    "key": "initial_caution",
                    "label": "初入陌生警惕度（0–1）",
                    "type": "number",
                    "min": 0.0,
                    "max": 1.0,
                    "step": 0.05,
                    "defaultValue": float(eff.get("initial_caution", 0.65)),
                    "description": "越高越会在陌生房间先观察确认安全，不会一进门就玩游戏或倒头大睡",
                },
                {
                    "key": "habituation_rate",
                    "label": "习惯化适应速率 k（0.05–0.80）",
                    "type": "number",
                    "min": 0.05,
                    "max": 0.80,
                    "step": 0.05,
                    "defaultValue": float(eff.get("habituation_rate", 0.25)),
                    "description": "每次安全观察与互动后熟悉度提升的快慢（外向/随性更高，慢热/高戒备更低）",
                },
                {
                    "key": "social_buffering",
                    "label": "熟人社会缓冲系数（0–1）",
                    "type": "number",
                    "min": 0.0,
                    "max": 1.0,
                    "step": 0.05,
                    "defaultValue": float(eff.get("social_buffering", 0.60)),
                    "description": "同屋有相熟同伴时加快适应陌生环境的缓冲倍率",
                },
                {
                    "key": "memory_decay",
                    "label": "认知地图记忆衰减 d（Tolman + ACT-R：0.10–0.90）",
                    "type": "number",
                    "min": 0.10,
                    "max": 0.90,
                    "step": 0.05,
                    "defaultValue": float(eff.get("memory_decay", 0.45)),
                    "description": "亲眼看过或用过的物件才记入局部认知地图；越高越容易淡忘角落里的旧物",
                },
            ],
        },
        {
            "id": "l0_circadian",
            "title": "3. 昼夜作息节律（Borbély 双过程睡眠 + Roenneberg 时型）",
            "description": "让精力（Process S）与世界时钟昼夜节律（Process C）耦合，避免半夜醒来打游戏或白天乱睡。",
            "fields": [
                {
                    "key": "chronotype_offset",
                    "label": "时型偏移（-3.0 晨型 ~ +3.0 夜猫子，小时）",
                    "type": "number",
                    "min": -3.0,
                    "max": 3.0,
                    "step": 0.5,
                    "defaultValue": float(eff.get("chronotype_offset", 0.0)),
                    "description": "正值偏晚睡晚起（如 +1.5），负值偏早睡早起（如 -1.0）",
                },
                {
                    "key": "circadian_strength",
                    "label": "昼夜节律门控强度（0–1）",
                    "type": "number",
                    "min": 0.0,
                    "max": 1.0,
                    "step": 0.05,
                    "defaultValue": float(eff.get("circadian_strength", 0.75)),
                    "description": "越高越严格遵守世界昼夜作息，深夜未到清晨前不会轻易醒来",
                },
            ],
        },
    ]

    if live_info.get("live.summary"):
        groups.append({
            "id": "l0_live",
            "title": "当下世界适应状态（只读）",
            "fields": [
                {
                    "key": "live.summary",
                    "label": "各世界适应进度",
                    "type": "text",
                    "readonly": True,
                    "defaultValue": live_info["live.summary"],
                }
            ],
        })

    if pointer_options:
        groups.append({
            "id": "target_pointer",
            "title": "当前编辑的角色指针",
            "fields": [
                {
                    "key": "_pointer_id",
                    "label": "编辑目标角色",
                    "type": "select",
                    "defaultValue": current_pointer_id or pointer_options[0]["value"],
                    "options": pointer_options,
                }
            ],
        })

    return groups


def register_page(register, state_root: Path, chat_key_prefix: str, *, registry=None, on_changed=None) -> bool:
    try:
        from sdk.types import FrontendConfigAction, FrontendConfigContribution, FrontendPageContribution

        project_root = Path(state_root).resolve().parents[1] if Path(state_root).resolve().name == "nanami" else Path.cwd()

        def _active_target():
            if registry is None:
                return state_root, "", [], "", ""
            pointers = registry.list_pointers()
            active_id = registry.active_edit_id()
            pointer = registry.get_pointer(active_id) or (pointers[0] if pointers else None)
            if pointer is None:
                return state_root, "", [], "", ""
            p_root = registry.resolve_state_root(pointer)
            pdir = registry.resolve_profile_dir(pointer) if hasattr(registry, "resolve_profile_dir") else p_root.parent
            soul_p = pdir / "soul.md"
            soul_txt = soul_p.read_text(encoding="utf-8")[:1200] if soul_p.is_file() else ""
            opts = [{"value": p.id, "label": f"{p.character_name or p.id}（{p.id}）"} for p in pointers]
            return p_root, str(pointer.character_name or pointer.id), opts, pointer.id, soul_txt

        init_root, init_cname, init_opts, init_pid, _ = _active_target()
        init_eff = world_perception.load(init_root)
        schema = build_schema(
            init_eff,
            _live_world_perception_summary(project_root, init_cname),
            pointer_options=init_opts,
            current_pointer_id=init_pid,
        )

        def _load() -> dict[str, Any]:
            t_root, cname, opts, pid, _ = _active_target()
            eff = world_perception.load(t_root)
            live = _live_world_perception_summary(project_root, cname)
            fresh = build_schema(eff, live, pointer_options=opts, current_pointer_id=pid)
            if fresh and schema != fresh:
                schema[:] = fresh
            cur = dict(eff)
            cur.update(live)
            if pid:
                cur["_pointer_id"] = pid
            return cur

        def _save(values: Any) -> None:
            t_root, _cname, _opts, current_pid, _ = _active_target()
            if registry is not None and isinstance(values, dict) and "_pointer_id" in values:
                picked = str(values.get("_pointer_id") or "").strip()
                if picked and picked != current_pid and registry.set_active_edit(picked):
                    _load()
                    return
            if isinstance(values, dict):
                world_perception.save(t_root, values)
                if callable(on_changed):
                    on_changed()

        def _act_autofill(_v: dict[str, Any]) -> dict[str, Any]:
            t_root, cname, _opts, _pid, soul_txt = _active_target()
            res = world_perception.infer_from_traits(t_root, soul_text=soul_txt)
            if callable(on_changed):
                on_changed()
            return {"ok": True, "message": f"已按「{cname}」性格推填 L0 环境感知参数", "values": res}

        def _act_reset(_v: dict[str, Any]) -> dict[str, Any]:
            t_root, cname, _opts, _pid, _ = _active_target()
            res = world_perception.reset(t_root)
            if callable(on_changed):
                on_changed()
            return {"ok": True, "message": f"已恢复「{cname}」L0 默认参数", "values": res}

        wp_html = Path(__file__).resolve().parent / "web" / "world_perception.html"
        if wp_html.is_file() and hasattr(register, "register_frontend_page"):
            register.register_frontend_page(
                FrontendPageContribution(
                    page_id=PAGE_ID,
                    title=PAGE_TITLE,
                    entry=str(wp_html),
                    kind="settings",
                    description="配置 L0 环境感知开关、注意力显著度、陌生适应曲线、认知地图与昼夜作息（仅世界内生效）。",
                    order=44.0,
                )
            )
        register.register_frontend_config_page(
            FrontendConfigContribution(
                page_id=PAGE_ID,
                title=PAGE_TITLE,
                kind="settings",
                description="配置角色进入文字世界时的注意力门控、陌生环境适应曲线、认知地图与昼夜作息节律（日常对话不启用）。",
                schema=schema,
                load_values=_load,
                save_values=_save,
                actions=[
                    FrontendConfigAction(
                        id="autofill_world_perception",
                        label="✨ 按角色性格一键推填 L0 参数",
                        run=_act_autofill,
                    ),
                    FrontendConfigAction(
                        id="reset_world_perception",
                        label="🔄 恢复 L0 默认值",
                        run=_act_reset,
                    ),
                ],
                order=44.0,
            )
        )
        return True
    except Exception:  # noqa: BLE001
        logger.exception("nanami_persona: 注册环境感知与适应层设置页失败")
        return False
