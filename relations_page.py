"""态度与情感指针设置页：配置角色对「用户」及「角色包内其他角色」的有向态度与情感指针。

特性：
  - 目标域严格闭环，只允许指向「用户」或宿主角色包 / 指针表里的其他角色；
  - 所有角色间初始预设默认为「不认识（known=False, rel_stage=stranger）」；
  - 提供「一键让 LLM 填写与所有角色包角色的关系」动作：本身不认识就跳过，认识则填入关系定位与共同经历。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from . import relations

logger = logging.getLogger(__name__)

PAGE_ID = "relations_settings"
PAGE_TITLE = "态度与情感指针"

_ACTIVE_TARGETS: dict[str, str] = {}

_RELATION_EDIT_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("known", "是否认识", "checkbox"),
    ("rel_stage", "关系阶段", "select"),
    ("attitude_stance", "态度姿态", "select"),
    ("call_name", "对 TA 的称呼", "text"),
    ("relation_tag", "关系定位", "text"),
    ("shared_memories", "关系与一起经历的事", "textarea"),
    ("last_interaction", "最近互动摘记", "text"),
    ("attitude_score", "综合态度分（-1 ~ +1）", "number"),
    ("affective_valence", "好感度 / 情感评价（-1 ~ +1）", "number"),
    ("trust", "信任度（0 ~ 1）", "number"),
    ("attraction", "吸引度（0 ~ 1）", "number"),
    ("repulsion", "排斥 / 反感（0 ~ 1）", "number"),
    ("warmth", "互动温度（0 ~ 1）", "number"),
    ("social_willingness", "主动交流意愿（0 ~ 1）", "number"),
    ("exchange_balance", "互惠账本（-1 ~ +1）", "number"),
    ("relational_debt", "心结 / 亏欠账本（0 ~ 1）", "number"),
    ("cool_off_turns", "冷静戒备轮次", "integer"),
)


def _stage_options() -> list[dict[str, str]]:
    return [
        {"value": k, "label": relations.STAGE_LABELS.get(k, k)}
        for k in relations.REL_STAGES
    ]


def _stance_options() -> list[dict[str, str]]:
    return [
        {"value": k, "label": relations.STANCE_LABELS.get(k, k)}
        for k in relations.ATTITUDE_STANCES
    ]


def build_schema(
    entry: dict[str, Any],
    *,
    pointer_options: list[dict[str, str]] | None = None,
    current_pointer_id: str = "",
    target_options: list[dict[str, str]] | None = None,
    current_target_key: str = "user",
) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    if pointer_options:
        groups.append({
            "id": "pointer_scope",
            "title": "指针主体与指向目标",
            "description": "态度与情感指针是有向边（主体角色 → 目标对象），仅允许指向用户或角色包内其他角色。",
            "fields": [
                {
                    "key": "_pointer_id",
                    "label": "当前角色（Subject）",
                    "type": "select",
                    "defaultValue": current_pointer_id or pointer_options[0]["value"],
                    "options": pointer_options,
                },
                {
                    "key": "_target_key",
                    "label": "指针指向（Target）",
                    "type": "select",
                    "defaultValue": current_target_key or "user",
                    "options": target_options or [{"value": "user", "label": "👤 用户"}],
                },
            ],
        })

    groups.append({
        "id": "relation_identity",
        "title": "关系定位与共同经历",
        "description": "角色间初始预设默认「不认识」，可通过右上角一键让 LLM 结合设定补全原本相识的角色与共同经历。",
        "fields": [
            {
                "key": "known",
                "label": "是否认识",
                "type": "checkbox",
                "defaultValue": bool(entry.get("known", False)),
                "description": "关闭即表示两人素不相识，世界里初遇会先破冰自我介绍",
            },
            {
                "key": "rel_stage",
                "label": "关系阶段",
                "type": "select",
                "defaultValue": str(entry.get("rel_stage") or "stranger"),
                "options": _stage_options(),
            },
            {
                "key": "attitude_stance",
                "label": "当下态度姿态",
                "type": "select",
                "defaultValue": str(entry.get("attitude_stance") or "neutral"),
                "options": _stance_options(),
            },
            {
                "key": "call_name",
                "label": "她平时如何称呼 TA",
                "type": "text",
                "defaultValue": str(entry.get("call_name") or ""),
                "placeholder": "如：日向君 / 小黑同学（留空则礼貌泛称）",
            },
            {
                "key": "relation_tag",
                "label": "关系定位标签",
                "type": "text",
                "defaultValue": str(entry.get("relation_tag") or ""),
                "placeholder": "如：同班同学 / 游戏伙伴 / 初次见面的室友",
            },
            {
                "key": "shared_memories",
                "label": "两人的关系与一起经历过的事",
                "type": "text",
                "span": "full",
                "defaultValue": str(entry.get("shared_memories") or ""),
                "placeholder": "写下她们如何相识、一起经历过什么重要或日常的事件",
            },
            {
                "key": "last_interaction",
                "label": "最近互动摘记",
                "type": "text",
                "span": "full",
                "defaultValue": str(entry.get("last_interaction") or ""),
            },
        ],
    })

    groups.append({
        "id": "relation_affect",
        "title": "态度与情感量化维度",
        "description": "这些数值与角色此刻的精力/心情一起，决定她在世界里愿不愿意主动找对方搭话。",
        "fields": [
            {"key": "attitude_score", "label": "综合态度分（-1 反感 ~ +1 亲善）", "type": "number", "min": -1.0, "max": 1.0, "step": 0.05, "defaultValue": float(entry.get("attitude_score", 0.0))},
            {"key": "affective_valence", "label": "好感度 / 情感评价（-1 ~ +1）", "type": "number", "min": -1.0, "max": 1.0, "step": 0.05, "defaultValue": float(entry.get("affective_valence", 0.0))},
            {"key": "trust", "label": "信任度（0 ~ 1）", "type": "number", "min": 0.0, "max": 1.0, "step": 0.05, "defaultValue": float(entry.get("trust", 0.55))},
            {"key": "attraction", "label": "人际吸引度（0 ~ 1）", "type": "number", "min": 0.0, "max": 1.0, "step": 0.05, "defaultValue": float(entry.get("attraction", 0.35))},
            {"key": "repulsion", "label": "排斥 / 厌恶值（0 ~ 1）", "type": "number", "min": 0.0, "max": 1.0, "step": 0.05, "defaultValue": float(entry.get("repulsion", 0.0))},
            {"key": "warmth", "label": "互动温度 Warmth（0 ~ 1）", "type": "number", "min": 0.0, "max": 1.0, "step": 0.05, "defaultValue": float(entry.get("warmth", 0.45))},
            {"key": "social_willingness", "label": "主动交流意愿（0 ~ 1）", "type": "number", "min": 0.0, "max": 1.0, "step": 0.05, "defaultValue": float(entry.get("social_willingness", 0.5))},
            {"key": "exchange_balance", "label": "互惠账本（-1 失衡 ~ +1 互惠）", "type": "number", "min": -1.0, "max": 1.0, "step": 0.05, "defaultValue": float(entry.get("exchange_balance", 0.0))},
            {"key": "relational_debt", "label": "心结 / 亏欠值（0 ~ 1）", "type": "number", "min": 0.0, "max": 1.0, "step": 0.05, "defaultValue": float(entry.get("relational_debt", 0.0))},
            {"key": "cool_off_turns", "label": "冷静戒备剩余轮次（0 = 无戒备）", "type": "integer", "min": 0, "max": 20, "step": 1, "defaultValue": int(entry.get("cool_off_turns", 0))},
        ],
    })
    return groups


def _extract_patch(values: dict[str, Any]) -> dict[str, Any]:
    patch: dict[str, Any] = {}
    for key, _lbl, _kind in _RELATION_EDIT_FIELDS:
        if key in values:
            patch[key] = values[key]
    return patch


def register_page(register, state_root: Path, chat_key_prefix: str, *, registry=None, on_changed=None) -> bool:
    try:
        from sdk.types import FrontendConfigAction, FrontendConfigContribution, FrontendPageContribution

        def _resolve_context(requested_target: str = ""):
            if registry is None:
                subject_name = "默认角色"
                rel_map = relations.load_relations(state_root, subject_name=subject_name, chat_key_prefix=chat_key_prefix)
                tkey = requested_target if requested_target in rel_map else "user"
                return state_root, chat_key_prefix, "", subject_name, [], rel_map, tkey

            pointers = registry.list_pointers()
            active_id = registry.active_edit_id()
            pointer = registry.get_pointer(active_id) or (pointers[0] if pointers else None)
            if pointer is None:
                subject_name = "默认角色"
                rel_map = relations.load_relations(state_root, registry=registry, subject_name=subject_name, chat_key_prefix=chat_key_prefix)
                tkey = requested_target if requested_target in rel_map else "user"
                return state_root, chat_key_prefix, "", subject_name, [], rel_map, tkey

            p_root = registry.resolve_state_root(pointer)
            p_prefix = pointer.chat_key_prefix or chat_key_prefix
            profile = registry.load_profile(pointer)
            subject_name = str(pointer.character_name or profile.display_name or pointer.id).strip()
            p_opts = [
                {"value": p.id, "label": f"{p.character_name or p.id}（{p.id}）"}
                for p in pointers
            ]
            rel_map = relations.load_relations(
                p_root,
                registry=registry,
                subject_name=subject_name,
                chat_key_prefix=p_prefix,
            )
            cur_tkey = str(requested_target or _ACTIVE_TARGETS.get(pointer.id) or "user").strip()
            if cur_tkey not in rel_map:
                cur_tkey = "user"
            _ACTIVE_TARGETS[pointer.id] = cur_tkey
            return p_root, p_prefix, pointer.id, subject_name, p_opts, rel_map, cur_tkey

        def _target_options_from_map(rel_map: dict[str, dict[str, Any]]) -> list[dict[str, str]]:
            opts: list[dict[str, str]] = []
            for k, v in rel_map.items():
                icon = "👤" if v.get("target_type") == "user" else "🎭"
                tag = "相识" if v.get("known") else "不认识"
                opts.append({"value": k, "label": f"{icon} {v.get('target_name') or k} [{tag}]"})
            return opts

        init_root, _iprefix, init_pid, _isubj, init_popts, init_rmap, init_tkey = _resolve_context()
        schema = build_schema(
            init_rmap[init_tkey],
            pointer_options=init_popts,
            current_pointer_id=init_pid,
            target_options=_target_options_from_map(init_rmap),
            current_target_key=init_tkey,
        )

        def _load() -> dict[str, Any]:
            t_root, _t_prefix, pid, subj, p_opts, rmap, tkey = _resolve_context()
            t_opts = _target_options_from_map(rmap)
            entry = rmap.get(tkey) or next(iter(rmap.values()))
            fresh = build_schema(
                entry,
                pointer_options=p_opts,
                current_pointer_id=pid,
                target_options=t_opts,
                current_target_key=tkey,
            )
            if fresh and schema != fresh:
                schema[:] = fresh
            current: dict[str, Any] = dict(entry)
            current["_pointer_id"] = pid
            current["_subject_name"] = subj
            current["_target_key"] = tkey
            current["_pointer_options"] = p_opts
            current["_target_options"] = t_opts
            current["_relations_map"] = rmap
            current["_stage_labels"] = dict(relations.STAGE_LABELS)
            current["_stance_labels"] = dict(relations.STANCE_LABELS)
            return current

        def _save(values: Any) -> None:
            if not isinstance(values, dict):
                return
            t_root, _t_prefix, pid, subj, _p_opts, rmap, cur_tkey = _resolve_context()
            if registry is not None and "_pointer_id" in values:
                picked = str(values.get("_pointer_id") or "").strip()
                if picked and picked != pid and registry.set_active_edit(picked):
                    _load()
                    return

            picked_target = str(values.get("_target_key") or values.get("target_key") or cur_tkey).strip()
            has_edit_fields = any(k in values for k, _l, _t in _RELATION_EDIT_FIELDS)
            if picked_target and picked_target != cur_tkey and not has_edit_fields:
                if picked_target in rmap:
                    _ACTIVE_TARGETS[pid] = picked_target
                _load()
                return

            if picked_target not in rmap:
                raise ValueError(f"指针目标非法：{picked_target}")
            _ACTIVE_TARGETS[pid] = picked_target
            patch = _extract_patch(values)
            if patch:
                relations.save_relation(
                    t_root,
                    picked_target,
                    patch,
                    registry=registry,
                    subject_name=subj,
                    manual=True,
                )
            _load()
            if on_changed is not None:
                on_changed()

        def _action_autofill_relations(values: Any) -> dict[str, Any]:
            if registry is None:
                raise ValueError("未检测到角色指针注册表")
            v = dict(values or {})
            pid = str(v.get("_pointer_id") or "").strip()
            if pid:
                registry.set_active_edit(pid)
            active_pid = registry.active_edit_id()
            res = relations.autofill_relations_for_pointer(registry, active_pid)
            page_vals = _load()
            if on_changed is not None:
                on_changed()
            return {"message": res["message"], "result": res, "values": page_vals}

        def _action_save_target(values: Any) -> dict[str, Any]:
            v = dict(values or {})
            t_root, _t_prefix, pid, subj, _p_opts, _rmap, cur_tkey = _resolve_context(
                str(v.get("target_key") or v.get("_target_key") or "")
            )
            tkey = str(v.get("target_key") or v.get("_target_key") or cur_tkey).strip()
            _ACTIVE_TARGETS[pid] = tkey
            patch = _extract_patch(v)
            saved = relations.save_relation(
                t_root,
                tkey,
                patch,
                registry=registry,
                subject_name=subj,
                manual=True,
            )
            page_vals = _load()
            if on_changed is not None:
                on_changed()
            return {"message": f"已保存「{subj} → {saved['target_name']}」的态度与情感指针", "entry": saved, "values": page_vals}

        def _action_reset_target(values: Any) -> dict[str, Any]:
            v = dict(values or {})
            t_root, _t_prefix, pid, subj, _p_opts, _rmap, cur_tkey = _resolve_context(
                str(v.get("target_key") or v.get("_target_key") or "")
            )
            tkey = str(v.get("target_key") or v.get("_target_key") or cur_tkey).strip()
            _ACTIVE_TARGETS[pid] = tkey
            reset_entry = relations.reset_relation(
                t_root,
                tkey,
                registry=registry,
                subject_name=subj,
            )
            page_vals = _load()
            if on_changed is not None:
                on_changed()
            return {"message": f"已将「{subj} → {reset_entry['target_name']}」重置为不认识", "entry": reset_entry, "values": page_vals}

        actions = [
            FrontendConfigAction(
                id="autofill_relations",
                label="✨ 一键 LLM 填写与所有角色包角色的关系",
                description="检查角色设定与各角色包，不认识自动跳过，认识则填入关系与共同经历",
                variant="primary",
                order=10.0,
                run=_action_autofill_relations,
            ),
            FrontendConfigAction(
                id="save_target_relation",
                label="保存当前指针",
                description="保存对当前目标对象的关系与情感指标修改",
                order=20.0,
                run=_action_save_target,
            ),
            FrontendConfigAction(
                id="reset_target_relation",
                label="重置为不认识",
                description="清空手动设定，恢复默认陌生人（stranger）状态",
                variant="danger",
                order=30.0,
                run=_action_reset_target,
            ),
        ]

        rel_html = Path(__file__).resolve().parent / "web" / "relations.html"
        if rel_html.is_file() and hasattr(register, "register_frontend_page"):
            register.register_frontend_page(
                FrontendPageContribution(
                    page_id=PAGE_ID,
                    title=PAGE_TITLE,
                    entry=str(rel_html),
                    kind="settings",
                    description="管理角色指向「用户」及「角色包其他角色」的态度与情感指针，支持一键让 LLM 填写原本相识者的关系与共同经历。",
                    order=43.0,
                )
            )
        register.register_frontend_config_page(
            FrontendConfigContribution(
                page_id=PAGE_ID,
                title=PAGE_TITLE,
                description="有向「态度与情感指针」配置页：严格限定指向用户或角色包其他角色，支持一键 LLM 填写熟识关系与共同经历。",
                schema=schema,
                load_values=_load,
                save_values=_save,
                actions=actions,
                kind="settings",
                order=43.0,
            )
        )
        logger.info("nanami_persona: 已注册「%s」页面（%d 组）", PAGE_TITLE, len(schema))
        return True
    except Exception:  # noqa: BLE001
        logger.exception("nanami_persona: 注册态度与情感指针页面失败")
        return False
