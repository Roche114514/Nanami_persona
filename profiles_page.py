"""角色指针与通用模板设置页：管理多个指向不同角色的指针，并可编辑每个角色的模板参数。

表单只使用宿主支持的控件（select / text / checkbox / integer / number），配合操作按钮：
  - 切换当前编辑的指针（active_edit_id）
  - 一键从空白模板 / 当前角色派生新角色包（自动写入 pointers.yaml 与独立目录）
  - 编辑指针绑定（character_name / profile_dir / state_root / chat_key_prefix）
  - 编辑角色包参数（人格壳、立绘神情、优势/盲区、默认活动、称呼、输出契约、守卫与判官）
"""

from __future__ import annotations

import logging
from typing import Any

from .profiles import DEFAULT_PRIMARY_ID, PointerRegistry, PointerSpec, TEMPLATE_DIR_NAME

logger = logging.getLogger(__name__)

PAGE_ID = "persona_profiles"
PAGE_TITLE = "角色指针与模板"


def _effective_edit_target(registry: PointerRegistry) -> tuple[PointerSpec, Any]:
    pointers = registry.list_pointers()
    active_id = registry.active_edit_id()
    pointer = registry.get_pointer(active_id) or (pointers[0] if pointers else PointerSpec(id=DEFAULT_PRIMARY_ID))
    profile = registry.load_profile(pointer)
    return pointer, profile


def build_groups(
    registry: PointerRegistry,
    global_settings: Any,
    *,
    speech_extras: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """构建角色指针与模板分组（供「人格模型」统一页复用，避免全局/指针双份冗余）。"""
    pointers = registry.list_pointers()
    pointer, profile = _effective_edit_target(registry)
    ov = profile.overrides or {}
    mem_ov = ov.get("memory") or {}
    cnt_ov = ov.get("contract") or {}
    grd_ov = ov.get("guard") or {}
    jdg_ov = ov.get("judge") or {}

    options = [
        {
            "value": p.id,
            "label": f"{p.character_name or p.id}（{p.id}{' · 启用' if p.enabled else ' · 停用'}）",
        }
        for p in pointers
    ]

    speech_fields = [
        {
            "key": "profile.contract.max_dialog_items",
            "label": "单轮最多气泡数",
            "type": "integer",
            "defaultValue": int(cnt_ov.get("max_dialog_items", global_settings.contract.max_dialog_items)),
            "min": 1,
            "max": 8,
            "step": 1,
            "description": "短句寡言角色建议 2–3",
        },
        {
            "key": "profile.contract.max_speech_chars",
            "label": "单气泡字数上限",
            "type": "integer",
            "defaultValue": int(cnt_ov.get("max_speech_chars", global_settings.contract.max_speech_chars)),
            "min": 10,
            "max": 200,
            "step": 2,
            "description": "控制每句长度节奏",
        },
        {
            "key": "profile.memory.address_suffix",
            "label": "称呼后缀",
            "type": "text",
            "defaultValue": str(mem_ov.get("address_suffix", global_settings.memory.address_suffix)),
            "description": "如「同学」；留空＝直呼其名",
        },
        {
            "key": "profile.memory.address_honorific",
            "label": "非同辈敬称",
            "type": "text",
            "defaultValue": str(mem_ov.get("address_honorific", global_settings.memory.address_honorific)),
            "description": "明确不是同辈时使用的敬称后缀",
        },
        {
            "key": "profile.memory.address_gap_turns",
            "label": "两次称呼间隔轮数",
            "type": "integer",
            "defaultValue": int(mem_ov.get("address_gap_turns", global_settings.memory.address_gap_turns)),
            "min": 0,
            "max": 60,
            "step": 1,
            "description": "控制叫名字的频率，防客服腔",
        },
        {
            "key": "profile.guard.mode",
            "label": "守卫模式",
            "type": "select",
            "defaultValue": str(grd_ov.get("mode", global_settings.guard.mode)),
            "options": [
                {"value": "clean", "label": "clean：命中直接裁剪"},
                {"value": "report", "label": "report：只记日志"},
                {"value": "off", "label": "off：关闭守卫"},
            ],
            "description": "只减不加的本地规则守卫",
        },
        {
            "key": "profile.judge.ai_flavor",
            "label": "大模型判官模式",
            "type": "select",
            "defaultValue": str(jdg_ov.get("ai_flavor", global_settings.judge.ai_flavor)),
            "options": [
                {"value": "clean", "label": "clean：发前判 AI 味"},
                {"value": "report", "label": "report：后台记日志"},
                {"value": "off", "label": "off：关闭判官"},
            ],
            "description": "语义级 AI 味与客服腔检测",
        },
        {
            "key": "profile.judge.system_prompt",
            "label": "判官角色锚点提示（可选）",
            "type": "text",
            "defaultValue": profile.judge_system_prompt,
            "span": "full",
            "placeholder": "留空使用默认；可写「判断这句话像不像 AI 客服，而不是【某角色】本人在说话」",
            "description": "让语义判官适应不同角色的语气基线",
        },
    ]
    if speech_extras:
        speech_fields.extend(speech_extras)

    host_chars = registry.list_host_characters() if hasattr(registry, "list_host_characters") else []
    host_names = [str(c.get("name") or "").strip() for c in host_chars if str(c.get("name") or "").strip()]
    bound_by_char: dict[str, str] = {}
    for p in pointers:
        cname = str(p.character_name or "").strip()
        if cname and cname not in bound_by_char:
            bound_by_char[cname] = p.id

    if host_names:
        unbound_first = sorted(host_names, key=lambda n: (n in bound_by_char, host_names.index(n)))
        new_char_options = [
            {
                "value": name,
                "label": (
                    f"{name}（已在角色列表 · 已绑 {bound_by_char[name]}）"
                    if name in bound_by_char
                    else f"{name}（已在角色列表 · 尚未绑定指针）"
                ),
            }
            for name in unbound_first
        ]
        new_char_options.append({"value": "__auto__", "label": "空白自定义新角色（自动生成编号）"})
        new_char_field: dict[str, Any] = {
            "key": "_new_character_name",
            "label": "选择宿主角色包（给底部新建/复制按钮用）",
            "type": "select",
            "defaultValue": unbound_first[0],
            "options": new_char_options,
            "description": "从宿主已添加的角色列表直接选取，配合底部「从空白模板新建角色」或「复制当前角色为新指针」一键生成并绑定",
        }

        target_char_options = []
        if pointer.character_name and pointer.character_name not in host_names:
            target_char_options.append({
                "value": pointer.character_name,
                "label": f"{pointer.character_name}（当前自定义绑定 · 未在宿主角色列表）",
            })
        for name in host_names:
            other_pid = bound_by_char.get(name, "")
            if other_pid and other_pid != pointer.id:
                lbl = f"{name}（宿主角色包 · 另有指针 {other_pid}）"
            else:
                lbl = f"{name}（宿主角色包）"
            target_char_options.append({"value": name, "label": lbl})
        target_char_field: dict[str, Any] = {
            "key": "pointer.character_name",
            "label": "目标角色（绑定宿主角色包）",
            "type": "select",
            "defaultValue": pointer.character_name or host_names[0],
            "options": target_char_options,
            "description": "直接选择已添加到宿主角色列表的角色（如七海千秋、哈贝特洛特等），无需手动输入",
        }
    else:
        new_char_field = {
            "key": "_new_character_name",
            "label": "新建角色名称（给底部新建/复制按钮用）",
            "type": "text",
            "defaultValue": "",
            "placeholder": "填要绑定的角色名（如：牧濑红莉栖），留空自动生成",
            "description": "配合页面底部「从空白模板新建角色」或「复制当前角色为新指针」按钮使用",
        }
        target_char_field = {
            "key": "pointer.character_name",
            "label": "目标角色（与宿主角色列表一致）",
            "type": "text",
            "defaultValue": pointer.character_name,
            "description": "必须与宿主角色卡里的 name 完全一致",
        }

    return [
        {
            "id": "pointer_switch",
            "title": "角色指针切换与新建",
            "description": f"当前共有 {len(pointers)} 个角色指针；切换后点击保存或使用底部按钮从宿主角色列表新建 / 复制 / 移除。",
            "fields": [
                {
                    "key": "_active_edit_id",
                    "label": "当前编辑的角色指针",
                    "type": "select",
                    "defaultValue": pointer.id,
                    "options": options,
                    "description": "选好后点击保存即可切换到该角色（切换当次不改写目标角色内容）",
                },
                new_char_field,
            ],
        },
        {
            "id": "binding",
            "title": f"当前指针绑定 · {pointer.id}",
            "description": "把宿主角色列表里的角色包指向插件的独立人格壳与状态空间（多角色同台互不串味）。",
            "fields": [
                {
                    "key": "pointer.enabled",
                    "label": "启用这个角色指针",
                    "type": "checkbox",
                    "defaultValue": bool(pointer.enabled),
                    "description": "关掉＝这个角色不接入人格模型",
                },
                target_char_field,
                {
                    "key": "pointer.profile_dir",
                    "label": "角色包目录指针",
                    "type": "text",
                    "defaultValue": pointer.profile_dir,
                    "span": "full",
                    "description": "相对插件/数据目录，或填绝对路径",
                },
                {
                    "key": "pointer.state_root",
                    "label": "独立状态目录（可留空）",
                    "type": "text",
                    "defaultValue": pointer.state_root,
                    "span": "full",
                    "placeholder": "留空＝自动按指针隔离（主指针兼容 data/nanami）",
                    "description": "存 mind.json / person.json / traits.json / dynamics.json",
                },
                {
                    "key": "pointer.chat_key_prefix",
                    "label": "会话键前缀",
                    "type": "text",
                    "defaultValue": pointer.chat_key_prefix,
                    "description": "每个角色与会话各自独立的状态前缀",
                },
            ],
        },
        {
            "id": "profile_identity",
            "title": "① 人格壳与角色模板",
            "description": "读写该指针目录下的 profile.yaml；修改内置角色时会自动在数据目录建立可编辑副本。",
            "fields": [
                {
                    "key": "profile.display_name",
                    "label": "模板显示名",
                    "type": "text",
                    "defaultValue": profile.display_name,
                    "description": "用于列表与日志辨识",
                },
                {
                    "key": "profile.soul.file",
                    "label": "人格壳文件指针",
                    "type": "text",
                    "defaultValue": profile.soul_file,
                    "span": "full",
                    "description": "相对角色包目录（如 soul.md）或绝对路径",
                },
                {
                    "key": "profile.soul.marker",
                    "label": "人格壳抬头标记",
                    "type": "text",
                    "defaultValue": profile.soul_marker,
                    "description": "如：【她是谁】",
                },
                {
                    "key": "profile.soul.route_mode",
                    "label": "人格壳注入模式",
                    "type": "select",
                    "defaultValue": profile.soul_route_mode,
                    "options": [
                        {"value": "auto", "label": "auto：按场景分层按需注入"},
                        {"value": "all", "label": "all：每轮整份注入"},
                    ],
                    "description": "auto 可省 token 并防止稀释状态卡",
                },
                {
                    "key": "profile.assets.mind_expression",
                    "label": "注入立绘神情状态",
                    "type": "checkbox",
                    "defaultValue": bool(profile.mind_expression),
                    "description": "把心理模型判出的表情档位变成状态描述喂给 LLM",
                },
            ],
        },
        {
            "id": "profile_speech",
            "title": "③ 说话节奏、称呼、守卫与判官",
            "description": "按当前角色指针保存说话上限、称呼习惯与出口检测规则。",
            "fields": speech_fields,
        },
    ]


def build_schema(registry: PointerRegistry, global_settings: Any) -> list[dict[str, Any]]:
    return build_groups(registry, global_settings)


def save_pointer_and_profile(registry: PointerRegistry, values: dict[str, Any]) -> tuple[bool, str]:
    """处理切换指针与写入 pointer.*/profile.*；返回 (是否仅切换了指针, 当前编辑指针 id)。"""
    current_id = registry.active_edit_id()
    picked_id = str(values.get("_active_edit_id") or "").strip()
    if picked_id and picked_id != current_id:
        registry.set_active_edit(picked_id)
        return True, picked_id

    pointer_patch: dict[str, Any] = {}
    for k in ("enabled", "character_name", "profile_dir", "state_root", "chat_key_prefix"):
        fkey = f"pointer.{k}"
        if fkey in values:
            pointer_patch[k] = values[fkey]
    if pointer_patch:
        if "character_name" in pointer_patch:
            pointer_patch["character_name"] = str(pointer_patch["character_name"] or "").strip()
        registry.update_pointer(current_id, pointer_patch)

    profile_patch: dict[str, Any] = {}
    for key, val in values.items():
        if not str(key).startswith("profile."):
            continue
        inner = str(key)[len("profile."):]
        if inner == "display_name":
            profile_patch["display_name"] = str(val or "").strip()
        elif inner.startswith(("soul.", "assets.", "domains.", "props.", "contract.", "memory.", "guard.", "judge.")):
            profile_patch[inner] = val
    if "pointer.character_name" in values and str(values["pointer.character_name"] or "").strip():
        bound_cname = str(values["pointer.character_name"]).strip()
        profile_patch["character_name"] = bound_cname
    else:
        bound_cname = ""
    if profile_patch:
        registry.update_profile_values(current_id, profile_patch)
    if bound_cname and hasattr(registry, "sync_bound_host_character"):
        registry.sync_bound_host_character(current_id, bound_cname)
    return False, current_id


def build_actions(registry: PointerRegistry, refresh_fn, on_changed=None) -> list[Any]:
    from sdk.types import FrontendConfigAction

    def _picked_new_name(values: Any) -> str:
        raw = str((values or {}).get("_new_character_name") or "").strip()
        return "" if raw == "__auto__" else raw

    def _action_new_blank(values) -> dict[str, Any]:
        cname = _picked_new_name(values)
        created = registry.scaffold_pointer(character_name=cname, source_id=TEMPLATE_DIR_NAME)
        refresh_fn()
        if on_changed is not None:
            on_changed()
        return {"message": f"已为角色「{created.character_name}」创建并绑定指针 {created.id}"}

    def _action_clone_current(values) -> dict[str, Any]:
        current_id = registry.active_edit_id()
        cname = _picked_new_name(values)
        created = registry.scaffold_pointer(character_name=cname, source_id=current_id)
        refresh_fn()
        if on_changed is not None:
            on_changed()
        return {"message": f"已从 {current_id} 复制并绑定指针 {created.id}（角色：{created.character_name}）"}

    def _action_delete_current(_values) -> dict[str, Any]:
        current_id = registry.active_edit_id()
        ok, msg = registry.delete_pointer(current_id)
        refresh_fn()
        if ok and on_changed is not None:
            on_changed()
        return {"message": msg}

    return [
        FrontendConfigAction(
            id="create_from_template",
            label="从空白模板新建角色",
            description="在数据目录生成独立角色包并注册新指针",
            variant="primary",
            order=10.0,
            run=_action_new_blank,
        ),
        FrontendConfigAction(
            id="clone_current_profile",
            label="复制当前角色为新指针",
            description="基于当前编辑的角色包派生一个新角色",
            variant="ghost",
            order=20.0,
            run=_action_clone_current,
        ),
        FrontendConfigAction(
            id="delete_current_pointer",
            label="移除当前自定义指针",
            description="从 pointers.yaml 移除当前指针（保留默认七海指针）",
            variant="danger",
            confirm="确定要移除当前编辑的角色指针吗？（默认七海千秋指针受保护，不可删除）",
            order=30.0,
            run=_action_delete_current,
        ),
    ]


def values_of(registry: PointerRegistry, global_settings: Any) -> dict[str, Any]:
    schema = build_schema(registry, global_settings)
    return {
        field["key"]: field["defaultValue"]
        for group in schema
        for field in group.get("fields") or []
    }


def register_page(register, registry: PointerRegistry, settings_provider, on_changed=None) -> bool:
    try:
        from sdk.types import FrontendConfigAction, FrontendConfigContribution

        schema = build_schema(registry, settings_provider())

        def _refresh_schema() -> None:
            fresh = build_schema(registry, settings_provider())
            if fresh and schema != fresh:
                schema[:] = fresh

        def _load() -> dict[str, Any]:
            _refresh_schema()
            return values_of(registry, settings_provider())

        def _save(values: Any) -> None:
            if not isinstance(values, dict):
                return
            switched, _ = save_pointer_and_profile(registry, values)
            _refresh_schema()
            if on_changed is not None:
                on_changed()
            if switched:
                return

        actions = build_actions(registry, _refresh_schema, on_changed=on_changed)

        register.register_frontend_config_page(
            FrontendConfigContribution(
                page_id=PAGE_ID,
                title=PAGE_TITLE,
                description=(
                    "管理多个指向不同角色的人格指针与通用模板参数。"
                    "保存后按 mtime 热加载，不用重启。"
                ),
                schema=schema,
                load_values=_load,
                save_values=_save,
                actions=actions,
                kind="settings",
                order=40.5,
            )
        )
        logger.info("nanami_persona: 已注册「%s」页面（%d 组）", PAGE_TITLE, len(schema))
        return True
    except Exception:  # noqa: BLE001
        logger.exception("nanami_persona: 注册角色指针页面失败")
        return False
