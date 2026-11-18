"""角色指针与通用模板设置页：管理多个指向不同角色的指针，并可编辑每个角色的模板参数。

表单只使用宿主支持的控件（select / text / checkbox / integer / number），配合操作按钮：
  - 切换当前编辑的指针（active_edit_id）
  - 一键从空白模板 / 当前角色派生新角色包（自动写入 pointers.yaml 与独立目录）
  - 编辑指针绑定（character_name / profile_dir / state_root / chat_key_prefix）
  - 编辑角色包参数（人格壳、立绘神情、优势/盲区、默认活动、称呼、输出契约、守卫与判官）
"""

from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from typing import Any

from . import corpus_builder
from .profiles import DEFAULT_PRIMARY_ID, PointerRegistry, PointerSpec, TEMPLATE_DIR_NAME

logger = logging.getLogger(__name__)

_BUILD_JOBS_LOCK = threading.Lock()
_BUILD_JOBS: dict[str, dict[str, Any]] = {}
_LATEST_BUILD_JOB_ID: str = ""


class _AutoBuildCancelled(Exception):
    pass

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
    _ = (global_settings, speech_extras)  # 保留参数签名兼容调用方；说话节奏/称呼/守卫已由语料库与后端自动接管
    pointers = registry.list_pointers()
    pointer, profile = _effective_edit_target(registry)

    options = [
        {
            "value": p.id,
            "label": f"{p.character_name or p.id}（{p.id}{' · 启用' if p.enabled else ' · 停用'}）",
        }
        for p in pointers
    ]

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
            "label": "新建目标角色",
            "type": "select",
            "defaultValue": unbound_first[0],
            "options": new_char_options,
            "description": "选定宿主角色或空白模板后，通过「新建 / 复制」生成并绑定指针",
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
            "label": "绑定宿主角色",
            "type": "select",
            "defaultValue": pointer.character_name or host_names[0],
            "options": target_char_options,
            "description": "将当前指针绑定到宿主角色列表中的指定角色",
        }
    else:
        new_char_field = {
            "key": "_new_character_name",
            "label": "新建角色名称",
            "type": "text",
            "defaultValue": "",
            "placeholder": "填要绑定的角色名（如：牧濑红莉栖），留空自动生成",
            "description": "配合「从空白模板新建」或「复制当前角色」创建指针",
        }
        target_char_field = {
            "key": "pointer.character_name",
            "label": "绑定宿主角色",
            "type": "text",
            "defaultValue": pointer.character_name,
            "description": "必须与宿主角色卡里的 name 完全一致",
        }

    def _action_auto_build_cancel(values) -> dict[str, Any]:
        req_id = str((values or {}).get("job_id") or "").strip()
        with _BUILD_JOBS_LOCK:
            jid = req_id or _LATEST_BUILD_JOB_ID
            st = _BUILD_JOBS.get(jid) if jid else None
            if not st or st.get("status") != "running":
                return {"cancelled": False, "status": "idle", "message": "当前没有运行中的全自动构建任务"}
            st["cancel_requested"] = True
            st["phase"] = "正在取消…"
            elapsed = round(time.time() - float(st.get("started_at") or time.time()), 1)
            st["logs"].append(f"[{elapsed:5.1f}s] ⏹ 收到取消请求，等待当前步骤安全收尾…")
            return {"cancelled": True, "job_id": jid, "message": "已请求取消全自动构建"}

    def _action_asr_model_status(values) -> dict[str, Any]:
        del values
        from . import asr_downloads
        return asr_downloads.probe_models()

    def _action_download_asr_model(values) -> dict[str, Any]:
        from . import asr_downloads
        v = values or {}
        model_id = str(v.get("model_id") or "faster-whisper-small").strip()
        channel = str(v.get("channel") or "mirror").strip()
        return asr_downloads.start_download(model_id, channel=channel)

    def _action_cancel_asr_download(values) -> dict[str, Any]:
        from . import asr_downloads
        v = values or {}
        model_id = str(v.get("model_id") or "").strip()
        return asr_downloads.cancel_download(model_id)

    def _action_add_local_asr_model(values) -> dict[str, Any]:
        from . import asr_downloads
        v = values or {}
        local_path = str(v.get("local_path") or "").strip()
        if not local_path:
            raise ValueError("请输入本地 ASR 模型目录或 model.bin 路径")
        return asr_downloads.add_local_model(local_path)

    def _action_remove_local_asr_model(values) -> dict[str, Any]:
        from . import asr_downloads
        v = values or {}
        local_path = str(v.get("local_path") or "").strip()
        return asr_downloads.remove_local_model(local_path)

    return [
        {
            "id": "pointer_switch",
            "title": "角色指针切换与新建",
            "description": f"当前共 {len(pointers)} 个角色指针。",
            "fields": [
                {
                    "key": "_active_edit_id",
                    "label": "当前编辑的角色指针",
                    "type": "select",
                    "defaultValue": pointer.id,
                    "options": options,
                    "description": "选择要查看或编辑的角色指针",
                },
                new_char_field,
            ],
        },
        {
            "id": "binding",
            "title": f"角色绑定 · {pointer.id}",
            "description": "把当前指针指向宿主角色包，实现多角色人设与状态隔离。",
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
            "title": "② 人格壳与角色模板",
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
            "id": "profile_soul_editor",
            "title": "② 人格壳正文（soul.md）",
            "description": "编辑当前角色的 soul.md，或从已有角色包一键导入设定。",
            "fields": [
                {
                    "key": "_soul_import_source",
                    "label": "导入来源",
                    "type": "select",
                    "defaultValue": f"host:{pointer.character_name}" if pointer.character_name in host_names else "",
                    "options": [
                        {"value": "", "label": "— 选择要导入人格壳的角色包或模板 —"},
                        *[
                            {"value": f"host:{name}", "label": f"宿主角色包：{name}（保留场景路由）"}
                            for name in host_names
                        ],
                        *[
                            {"value": f"pointer:{p.id}", "label": f"插件指针：{p.character_name or p.id}（{p.id}）"}
                            for p in pointers
                            if p.id != pointer.id
                        ],
                        {"value": f"template:{TEMPLATE_DIR_NAME}", "label": "通用空白人格壳模板（_template）"},
                    ],
                    "span": "full",
                    "description": "选择来源后点击右侧按钮导入",
                },
                {
                    "key": "_soul_content",
                    "label": "人格壳正文（soul.md）",
                    "type": "textarea",
                    "defaultValue": registry.read_soul_text(pointer.id) if hasattr(registry, "read_soul_text") else "",
                    "span": "full",
                    "placeholder": "在此直接编写角色画像、说话节奏、口语词、禁止项以及 <!-- route: first_turn / insulted / identity / address --> 场景段",
                    "description": "支持 Markdown 与 <!-- route: xxx --> 分段标记；保存自动写入当前指针的可编辑目录",
                },
            ],
        },
        {
            "id": "profile_file_access",
            "title": "③ 破壁文件读写权限（高阶实验开关）",
            "description": "仅控制当前编辑角色是否可在对话中读写授权目录下的真实文件；越界指数未达阈值前不会暴露工具。",
            "fields": [
                {
                    "key": "profile.file_access.enabled",
                    "label": "允许角色读取本地授权文件",
                    "type": "checkbox",
                    "defaultValue": bool(profile.file_access_enabled),
                    "description": "默认关；开启后才会在越界达标时注入文件探测工具",
                },
                {
                    "key": "profile.file_access.allow_write",
                    "label": "允许角色写入 / 修改授权目录文件",
                    "type": "checkbox",
                    "defaultValue": bool(profile.file_access_allow_write),
                    "visibleWhen": {"profile.file_access.enabled": True},
                    "description": "仅限 profile.yaml 的 write_roots 或角色自有运行目录，默认不可修改宿主源码",
                },
                {
                    "key": "profile.file_access.min_transgression",
                    "label": "解锁破壁权限的最低越界指数（0–1）",
                    "type": "number",
                    "defaultValue": float(profile.file_access_min_transgression),
                    "min": 0.0,
                    "max": 1.0,
                    "step": 0.05,
                    "visibleWhen": {"profile.file_access.enabled": True},
                    "description": "0.0 = 全程开放；建议设 0.75 实现「平时正常，逼急才破壁」",
                },
            ],
        },
        {
            "id": "corpus_manage",
            "title": "① 角色全自动建模与专属语料库",
            "description": "为当前选中的角色指针构建专属原典对话语料库（corpus.tsv）；命中专属语料时将独占使用，不再混入通用分库。",
            "fields": [
                {
                    "key": "_corpus_status",
                    "label": "当前指针语料状态",
                    "type": "text",
                    "defaultValue": corpus_builder.corpus_summary(profile.profile_dir),
                    "readOnly": True,
                    "span": "full",
                    "description": f"当前绑定指针：{pointer.id}（角色：{pointer.character_name or profile.character_name or pointer.id}）",
                },
                {
                    "key": "_auto_char_name",
                    "label": "一键构建目标角色名（可选）",
                    "type": "text",
                    "defaultValue": "",
                    "placeholder": f"留空则针对当前指针角色「{pointer.character_name or profile.character_name or pointer.id}」构建；填新名字将自动建新指针",
                    "description": "配合「一键全自动构建角色」使用：输入角色名自动爬百科、搜B站音频ASR填满6桶并生成人格/心理模型",
                },
                {
                    "key": "_auto_work_name",
                    "label": "所属作品 / 补充关键词（可选）",
                    "type": "text",
                    "defaultValue": "",
                    "placeholder": "如：命运石之门 / 崩坏3 / 蔚蓝档案（有助于消歧义与提高 B 站语音命中率）",
                    "description": "消除同名角色歧义，并作为 B 站语音合集检索关键词",
                },
                {
                    "key": "_auto_bvid_hint",
                    "label": "指定 B 站 BV 号或链接（可选）",
                    "type": "text",
                    "defaultValue": "",
                    "span": "full",
                    "placeholder": "如：BV1xx411c7mD（多个用逗号分隔；留空则自动按「角色名 + 语音合集」搜索）",
                    "description": "优先直取该视频自带字幕轨或下载音轨跑本地 ASR，6 个情境桶达标即自动停机",
                },
                {
                    "key": "_corpus_mode",
                    "label": "写入方式",
                    "type": "select",
                    "defaultValue": "append",
                    "options": [
                        {"value": "append", "label": "追加并去重（保留已有语料）"},
                        {"value": "replace", "label": "覆盖重建（清空后重生成）"},
                    ],
                    "description": "决定新整理出的对话对是追加到当前 corpus.tsv 还是覆盖重建",
                },
                {
                    "key": "_corpus_raw_text",
                    "label": "原始对话资料（剧本 / 原典台词 / 对话记录）",
                    "type": "textarea",
                    "defaultValue": "",
                    "rows": 8,
                    "span": "full",
                    "placeholder": "粘贴该角色的原作剧本、台词摘录或双人对话资料，然后点击下方「📚 由 LLM 整理对话资料为角色语料库」按钮……",
                    "description": "LLM 将严格从所提供资料中提取并按 6 类情境分桶，写入当前指针的 corpus.tsv",
                },
            ],
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
        elif inner.startswith(("soul.", "assets.", "domains.", "props.", "contract.", "memory.", "guard.", "judge.", "file_access.")):
            profile_patch[inner] = val
    if "pointer.character_name" in values and str(values["pointer.character_name"] or "").strip():
        bound_cname = str(values["pointer.character_name"]).strip()
        profile_patch["character_name"] = bound_cname
    else:
        bound_cname = ""
    if profile_patch:
        registry.update_profile_values(current_id, profile_patch)
    if "_soul_content" in values and hasattr(registry, "save_soul_text"):
        new_soul = str(values.get("_soul_content") or "").replace("\r\n", "\n")
        cur_soul = str(registry.read_soul_text(current_id) or "").replace("\r\n", "\n")
        if new_soul.rstrip() != cur_soul.rstrip():
            registry.save_soul_text(current_id, new_soul)
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

    def _action_import_soul(values) -> dict[str, Any]:
        current_id = registry.active_edit_id()
        source_key = str((values or {}).get("_soul_import_source") or "").strip()
        if not source_key:
            pointer = registry.get_pointer(current_id)
            if pointer and pointer.character_name:
                source_key = f"host:{pointer.character_name}"
        ok, msg = registry.import_soul_from_source(current_id, source_key)
        refresh_fn()
        if ok and on_changed is not None:
            on_changed()
        return {"message": msg}

    def _action_delete_current(_values) -> dict[str, Any]:
        current_id = registry.active_edit_id()
        ok, msg = registry.delete_pointer(current_id)
        refresh_fn()
        if ok and on_changed is not None:
            on_changed()
        return {"message": msg}

    def _action_autofill_model(values) -> dict[str, Any]:
        from . import mind_autofill

        current_id = registry.active_edit_id()
        soul_override = str((values or {}).get("_soul_content") or "")
        res = mind_autofill.autofill_from_soul(
            registry,
            current_id,
            soul_text=soul_override,
            apply=True,
        )
        refresh_fn()
        if on_changed is not None:
            on_changed()
        return res

    def _action_build_corpus(values) -> dict[str, Any]:
        current_id = registry.active_edit_id()
        raw_text = str((values or {}).get("_corpus_raw_text") or "")
        mode = str((values or {}).get("_corpus_mode") or "append").strip().lower()
        res = corpus_builder.build_corpus_from_text(
            registry,
            current_id,
            raw_text,
            mode=mode,
        )
        refresh_fn()
        if on_changed is not None:
            on_changed()
        return res

    def _action_auto_build_all(values) -> dict[str, Any]:
        global _LATEST_BUILD_JOB_ID
        from . import auto_persona

        current_id = registry.active_edit_id()
        pointer = registry.get_pointer(current_id)
        current_cname = (pointer.character_name if pointer else "") or ""
        typed_name = str((values or {}).get("_auto_char_name") or "").strip()
        target_name = typed_name or str((values or {}).get("pointer.character_name") or "").strip() or current_cname
        if not target_name:
            raise ValueError("请先在「一键构建目标角色名」或「绑定宿主角色」填写角色名。")
        use_pointer_id = current_id if (not typed_name or typed_name == current_cname) else ""
        work_name = str((values or {}).get("_auto_work_name") or "").strip()
        bvid_raw = str((values or {}).get("_auto_bvid_hint") or "").strip()
        bvid_list = [x.strip() for x in re.split(r"[\s,，]+", bvid_raw) if x.strip()]
        raw_text = str((values or {}).get("_corpus_raw_text") or "")
        mode = str((values or {}).get("_corpus_mode") or "replace").strip().lower()
        use_async = bool((values or {}).get("_async"))

        if not use_async:
            res = auto_persona.auto_build_character(
                registry,
                target_name,
                work_name=work_name,
                pointer_id=use_pointer_id,
                bvid_list=bvid_list,
                raw_corpus_text=raw_text,
                corpus_mode=mode,
            )
            refresh_fn()
            if on_changed is not None:
                on_changed()
            return res

        with _BUILD_JOBS_LOCK:
            if _LATEST_BUILD_JOB_ID and (_BUILD_JOBS.get(_LATEST_BUILD_JOB_ID) or {}).get("status") == "running":
                running_job = dict(_BUILD_JOBS[_LATEST_BUILD_JOB_ID])
                running_job["started"] = True
                running_job["message"] = f"已有正在进行的构建任务（{running_job.get('character_name')}），已接续进度展示。"
                return running_job

            job_id = uuid.uuid4().hex[:10]
            t_start = time.time()
            job_state: dict[str, Any] = {
                "job_id": job_id,
                "status": "running",
                "character_name": target_name,
                "pointer_id": use_pointer_id,
                "started_at": t_start,
                "elapsed_sec": 0.0,
                "phase": "初始化任务",
                "depth": 0,
                "max_depth": 4,
                "current_item": target_name,
                "counts": {b: 0 for b in corpus_builder.ALLOWED_BUCKETS},
                "quotas": {b: list(v) for b, v in corpus_builder.DEFAULT_BUCKET_QUOTAS.items()},
                "deficits": {b: v[0] for b, v in corpus_builder.DEFAULT_BUCKET_QUOTAS.items()},
                "total": 0,
                "target_min": sum(v[0] for v in corpus_builder.DEFAULT_BUCKET_QUOTAS.values()),
                "complete": False,
                "cancel_requested": False,
                "logs": [f"[  0.0s] 启动「{target_name}」全自动递归采集与建模…"],
                "result": None,
                "error": "",
            }
            _BUILD_JOBS[job_id] = job_state
            _LATEST_BUILD_JOB_ID = job_id

        def _on_state(snap: dict[str, Any]) -> None:
            with _BUILD_JOBS_LOCK:
                st = _BUILD_JOBS.get(job_id)
                if not st:
                    return
                if st.get("cancel_requested"):
                    raise _AutoBuildCancelled("用户取消了全自动建模")
                elapsed = round(time.time() - t_start, 1)
                st["elapsed_sec"] = elapsed
                for k in ("pointer_id", "phase", "depth", "max_depth", "current_item", "counts", "quotas", "deficits", "total", "target_min", "complete"):
                    if k in snap:
                        st[k] = snap[k]
                log_msg = str(snap.get("log") or "").strip()
                if log_msg:
                    line = f"[{elapsed:5.1f}s] {log_msg}"
                    if not st["logs"] or st["logs"][-1] != line:
                        st["logs"].append(line)
                        if len(st["logs"]) > 80:
                            st["logs"] = st["logs"][-80:]

        def _worker() -> None:
            try:
                res = auto_persona.auto_build_character(
                    registry,
                    target_name,
                    work_name=work_name,
                    pointer_id=use_pointer_id,
                    bvid_list=bvid_list,
                    raw_corpus_text=raw_text,
                    corpus_mode=mode,
                    state_cb=_on_state,
                )
                refresh_fn()
                if on_changed is not None:
                    on_changed()
                with _BUILD_JOBS_LOCK:
                    st = _BUILD_JOBS.get(job_id)
                    if st:
                        elapsed = round(time.time() - t_start, 1)
                        st["elapsed_sec"] = elapsed
                        st["status"] = "done"
                        st["complete"] = bool(res.get("complete"))
                        st["counts"] = dict(res.get("corpus_counts") or st["counts"])
                        st["total"] = int(res.get("corpus_total") or st["total"])
                        st["pointer_id"] = str(res.get("pointer_id") or st["pointer_id"])
                        st["phase"] = "构建完成"
                        st["current_item"] = str(res.get("message") or "全自动建模已完成")
                        st["result"] = res
                        st["logs"].append(f"[{elapsed:5.1f}s] ✅ {st['current_item']}")
            except _AutoBuildCancelled:
                with _BUILD_JOBS_LOCK:
                    st = _BUILD_JOBS.get(job_id)
                    if st:
                        elapsed = round(time.time() - t_start, 1)
                        st["elapsed_sec"] = elapsed
                        st["status"] = "cancelled"
                        st["phase"] = "已取消"
                        st["current_item"] = "已按用户要求停止全自动建模"
                        st["logs"].append(f"[{elapsed:5.1f}s] ⏹ 已取消全自动建模")
            except Exception as exc:  # noqa: BLE001
                logger.exception("nanami_persona: 后台全自动建模失败")
                with _BUILD_JOBS_LOCK:
                    st = _BUILD_JOBS.get(job_id)
                    if st:
                        elapsed = round(time.time() - t_start, 1)
                        st["elapsed_sec"] = elapsed
                        st["status"] = "error"
                        st["phase"] = "构建失败"
                        st["error"] = str(exc)
                        st["logs"].append(f"[{elapsed:5.1f}s] ❌ 失败：{exc}")

        threading.Thread(target=_worker, name=f"nanami_auto_{job_id}", daemon=True).start()
        return {
            "started": True,
            "job_id": job_id,
            "message": f"已启动「{target_name}」全自动递归采集，请观察下方实时进度。",
        }

    def _action_auto_build_progress(values) -> dict[str, Any]:
        req_id = str((values or {}).get("job_id") or "").strip()
        with _BUILD_JOBS_LOCK:
            jid = req_id or _LATEST_BUILD_JOB_ID
            st = _BUILD_JOBS.get(jid) if jid else None
            if not st:
                return {"status": "idle", "message": "当前无进行中的构建任务"}
            snap = dict(st)
            snap["logs"] = list(st.get("logs") or [])
            if snap.get("status") == "running":
                snap["elapsed_sec"] = round(time.time() - float(snap.get("started_at") or time.time()), 1)
            return snap

    def _action_auto_build_cancel(values) -> dict[str, Any]:
        req_id = str((values or {}).get("job_id") or "").strip()
        with _BUILD_JOBS_LOCK:
            jid = req_id or _LATEST_BUILD_JOB_ID
            st = _BUILD_JOBS.get(jid) if jid else None
            if not st or st.get("status") != "running":
                return {"cancelled": False, "status": "idle", "message": "当前没有运行中的全自动构建任务"}
            st["cancel_requested"] = True
            st["phase"] = "正在取消…"
            elapsed = round(time.time() - float(st.get("started_at") or time.time()), 1)
            st["logs"].append(f"[{elapsed:5.1f}s] ⏹ 收到取消请求，等待当前步骤安全收尾…")
            return {"cancelled": True, "job_id": jid, "message": "已请求取消全自动构建"}

    def _action_asr_model_status(values) -> dict[str, Any]:
        del values
        from . import asr_downloads
        return asr_downloads.probe_models()

    def _action_download_asr_model(values) -> dict[str, Any]:
        from . import asr_downloads
        v = values or {}
        model_id = str(v.get("model_id") or "faster-whisper-small").strip()
        channel = str(v.get("channel") or "mirror").strip()
        return asr_downloads.start_download(model_id, channel=channel)

    def _action_cancel_asr_download(values) -> dict[str, Any]:
        from . import asr_downloads
        v = values or {}
        model_id = str(v.get("model_id") or "").strip()
        return asr_downloads.cancel_download(model_id)

    def _action_add_local_asr_model(values) -> dict[str, Any]:
        from . import asr_downloads
        v = values or {}
        local_path = str(v.get("local_path") or "").strip()
        if not local_path:
            raise ValueError("请输入本地 ASR 模型目录或 model.bin 路径")
        return asr_downloads.add_local_model(local_path)

    def _action_remove_local_asr_model(values) -> dict[str, Any]:
        from . import asr_downloads
        v = values or {}
        local_path = str(v.get("local_path") or "").strip()
        return asr_downloads.remove_local_model(local_path)

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
            id="import_soul_from_source",
            label="从选定角色包导入人格壳",
            description="把上方「导入来源」选中的宿主角色设定或指针人格壳导入当前编辑角色的 soul.md",
            variant="ghost",
            confirm="将使用所选来源替换当前角色的「人格壳正文（soul.md）」（保留场景路由结构），确定继续吗？",
            order=15.0,
            run=_action_import_soul,
        ),
        FrontendConfigAction(
            id="autofill_model_from_soul",
            label="✨ 由 LLM 按人格壳推填模型",
            description="根据当前角色的 soul.md 与设定自动推导并填充 L1–L8 心理参数、能力域、敏感点与说话锚点",
            variant="primary",
            confirm="将请大模型分析当前「人格壳（soul.md）」并自动填充该角色的 L1–L8 心理参数、能力域、敏感点与说话配置（不会改动 soul.md 原文），确定继续吗？",
            order=17.0,
            run=_action_autofill_model,
        ),
        FrontendConfigAction(
            id="build_corpus_from_text",
            label="📚 由 LLM 整理对话资料为角色语料库",
            description="将上方「原始对话资料」提取分桶写入当前指针专属 corpus.tsv（按指针隔离）",
            variant="primary",
            order=18.0,
            run=_action_build_corpus,
        ),
        FrontendConfigAction(
            id="auto_build_character_all",
            label="🚀 一键全自动构建角色（爬资料 + B站语音ASR填满6桶 + 生成人格模型）",
            description="输入角色名自动爬百科、抽取/转写 B 站角色语音并按6桶配额鉴真填充，随后生成 soul.md 与心理/社会学参数",
            variant="primary",
            confirm="将联网检索百科资料与 B 站角色语音（无字幕时下载音频跑本地 ASR），按 6 桶配额鉴真填充 corpus.tsv，并自动生成 soul.md、守卫与心理/社会学参数。确定开始吗？",
            order=19.0,
            run=_action_auto_build_all,
        ),
        FrontendConfigAction(
            id="auto_build_progress",
            label="查询全自动构建进度",
            description="轮询当前递归语料采集任务状态与 6 桶实时填充进度",
            variant="ghost",
            order=19.1,
            run=_action_auto_build_progress,
        ),
        FrontendConfigAction(
            id="auto_build_cancel",
            label="⏹ 取消全自动构建",
            description="安全中止当前正在运行的全自动递归采集与建模任务",
            variant="danger",
            order=19.15,
            run=_action_auto_build_cancel,
        ),
        FrontendConfigAction(
            id="asr_model_status",
            label="刷新流水线 ASR 模型状态",
            description="探测本地 Faster-Whisper / FunASR / Vosk 模型就绪情况与后台下载进度",
            variant="ghost",
            order=19.2,
            run=_action_asr_model_status,
        ),
        FrontendConfigAction(
            id="download_asr_model",
            label="📥 下载流水线 ASR 模型",
            description="通过国内镜像或官方通道下载 Faster-Whisper 本地语音识别模型至 data/models/",
            variant="primary",
            order=19.3,
            run=_action_download_asr_model,
        ),
        FrontendConfigAction(
            id="cancel_asr_download",
            label="✕ 取消模型下载",
            description="立即中止正在进行的 ASR 模型下载并清理未完成临时文件",
            variant="danger",
            order=19.4,
            run=_action_cancel_asr_download,
        ),
        FrontendConfigAction(
            id="add_local_asr_model",
            label="➕ 添加本地 ASR 模型",
            description="挂载本机已有的 Whisper / FunASR / Vosk 模型目录供流水线直接使用",
            variant="ghost",
            order=19.5,
            run=_action_add_local_asr_model,
        ),
        FrontendConfigAction(
            id="remove_local_asr_model",
            label="移除本地 ASR 模型挂载",
            description="从挂载列表中移除自定义本地模型路径（不删除磁盘文件）",
            variant="ghost",
            order=19.6,
            run=_action_remove_local_asr_model,
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
