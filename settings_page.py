"""插件设置页：把人格模型那几层参数做成能点的表单。

分组对应三层结构：

    ① 人格壳（persona.inject）        她是谁
    ② 心理模型（mind.*）+ 意图（props.*）  她此刻想做什么
    ③ 守卫 / 契约 / 判官              她出口之前怎么被筛

保存走 `settings_write.write_values()`——按行改，**保住 settings.yaml 里那堆注释**
（那些注释里写着每条参数的原典依据，冲掉就是丢研究记录）。
保存后不用重启：设置按 mtime 热加载，下一轮生效。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PAGE_ID = "persona_settings"
PAGE_TITLE = "人格模型设置"


def build_schema(settings: Any) -> list[dict[str, Any]]:
    """→ 宿主能渲染的分组表单。字段类型只用宿主支持的：checkbox / select / number / integer / text。"""
    mind = settings.mind
    guard = settings.guard
    contract = settings.contract
    memory = settings.memory
    props = settings.props
    judge = settings.judge
    persona = settings.persona

    return [
        {
            "id": "basic",
            "title": "基础",
            "description": "接哪个人物、状态存在哪。角色名必须和角色包里的 name 完全一致。",
            "fields": [
                {"key": "enabled", "label": "启用本插件", "type": "checkbox",
                 "defaultValue": bool(settings.enabled), "description": "关掉＝插件不接线"},
                {"key": "character_name", "label": "角色名", "type": "text",
                 "defaultValue": settings.character_name, "description": "必须与角色名一致"},
                {"key": "chat_key_prefix", "label": "会话前缀", "type": "text",
                 "defaultValue": settings.chat_key_prefix,
                 "description": "每会话一份状态"},
                {"key": "log_trace", "label": "记录每轮执行痕迹", "type": "checkbox",
                 "defaultValue": bool(settings.log_trace),
                 "description": "写进日志与 state/"},
            ],
        },
        {
            "id": "shell",
            "title": "① 人格壳",
            "description": "她是谁。关掉就只用角色包里原来的简介。",
            "fields": [
                {"key": "persona.inject", "label": "注入原典版人格壳", "type": "checkbox",
                 "defaultValue": bool(persona.inject),
                 "description": "运行时插进提示词"},
            ],
        },
        {
            "id": "mind",
            "title": "② 心理模型（此刻）",
            "description": "每轮算一张【此刻】卡片。超时就是 fail-open——这一轮不带卡，不会吞她的话。",
            "fields": [
                {"key": "mind.enabled", "label": "启用心理模型", "type": "checkbox",
                 "defaultValue": bool(mind.enabled), "description": "关掉只剩人格壳"},
                {"key": "mind.percept", "label": "读句（大模型判言语行为）", "type": "checkbox",
                 "defaultValue": bool(mind.percept),
                 "description": "每轮多一次大模型调用"},
                {"key": "mind.card_timeout", "label": "算卡片超时（秒）", "type": "number",
                 "defaultValue": float(mind.card_timeout), "min": 1.0, "max": 20.0, "step": 0.5,
                 "description": "建议 ≥ 读句 + 1.5s"},
                {"key": "mind.percept_timeout", "label": "读句预算（秒）", "type": "number",
                 "defaultValue": float(mind.percept_timeout), "min": 0.5, "max": 10.0, "step": 0.5,
                 "visibleWhen": {"mind.percept": True}, "description": "读句那一次的预算"},
                {"key": "mind.her_timeout", "label": "回写超时（秒）", "type": "number",
                 "defaultValue": float(mind.her_timeout), "min": 0.5, "max": 10.0, "step": 0.5, "description": "回写状态的预算"},
                {"key": "mind.guard_timeout", "label": "守卫超时（秒）", "type": "number",
                 "defaultValue": float(mind.guard_timeout), "min": 0.5, "max": 10.0, "step": 0.5, "description": "守卫那一步的预算"},
                {"key": "mind.retries", "label": "失败重试次数", "type": "integer",
                 "defaultValue": int(mind.retries), "min": 0, "max": 3, "step": 1, "description": "算失败时重试几次"},
                {"key": "mind.card_position", "label": "卡片插进哪", "type": "select",
                 "defaultValue": mind.card_position,
                 "options": [{"value": "system", "label": "system：最后追加一条"},
                             {"value": "user", "label": "user：拼在她那句话前面"}], "description": "卡片插进哪类消息"},
                {"key": "mind.writeback", "label": "她说完回写状态", "type": "checkbox",
                 "defaultValue": bool(mind.writeback),
                 "description": "关掉·她的话不改状态"},
            ],
        },
        {
            "id": "props",
            "title": "② 意图（她此刻想做什么）",
            "description": "类别由精力/心里/刚发生的事推导；内容不随机抽。",
            "fields": [
                {"key": "props.enabled", "label": "启用意图层", "type": "checkbox",
                 "defaultValue": bool(props.enabled), "description": "关掉就不算意图"},
                {"key": "props.mode", "label": "生成方式", "type": "select",
                 "defaultValue": props.mode,
                 "options": [{"value": "rules", "label": "rules：规则给短句，不花模型"},
                             {"value": "model", "label": "model：同类别内让模型说具体"}], "description": "rules 不花模型"},
                {"key": "props.timeout", "label": "单次预算（秒）", "type": "number",
                 "defaultValue": float(props.timeout), "min": 0.5, "max": 10.0, "step": 0.5,
                 "visibleWhen": {"props.mode": "model"}, "description": "model 档才用得上"},
            ],
        },
        {
            "id": "guard",
            "title": "③ 发送前守卫",
            "description": "rules 不花模型",
            "fields": [
                {"key": "guard.enabled", "label": "启用守卫", "type": "checkbox",
                 "defaultValue": bool(guard.enabled), "description": "只减不加，不添词"},
                {"key": "guard.mode", "label": "命中后", "type": "select",
                 "defaultValue": guard.mode,
                 "options": [{"value": "clean", "label": "clean：就地改掉"},
                             {"value": "report", "label": "report：只记日志"},
                             {"value": "off", "label": "off：不启用"}], "description": "clean 就地改掉"},
                {"key": "guard.max_chars", "label": "超过多少字跳过", "type": "integer",
                 "defaultValue": int(guard.max_chars), "min": 20, "max": 400, "step": 10,
                 "description": "长段跳过守卫"},
            ],
        },
        {
            "id": "contract",
            "title": "③ 输出契约",
            "description": "宿主默认要求两个气泡；对她是多余的负担。",
            "fields": [
                {"key": "contract.enabled", "label": "启用契约适配", "type": "checkbox",
                 "defaultValue": bool(contract.enabled), "description": "关掉用宿主默认"},
                {"key": "contract.max_dialog_items", "label": "最多几个气泡", "type": "integer",
                 "defaultValue": int(contract.max_dialog_items), "min": 1, "max": 8, "step": 1, "description": "她习惯 1–2 句一条"},
                {"key": "contract.max_speech_chars", "label": "单个气泡字数上限", "type": "integer",
                 "defaultValue": int(contract.max_speech_chars), "min": 10, "max": 200, "step": 2,
                 "description": "原典平均 15 字"},
            ],
        },
        {
            "id": "judge",
            "title": "③ 判官（AI 味 / 垫字）",
            "description": "交给宿主配置的大模型。超时按人话放行。",
            "fields": [
                {"key": "judge.enabled", "label": "启用判官", "type": "checkbox",
                 "defaultValue": bool(judge.enabled), "description": "关掉不做任何判定"},
                {"key": "judge.ai_flavor", "label": "AI 味", "type": "select",
                 "defaultValue": judge.ai_flavor,
                 "options": [{"value": "clean", "label": "clean：发出去之前判，命中就不发"},
                             {"value": "report", "label": "report：答完后判，只记日志"},
                             {"value": "off", "label": "off：不判"}], "description": "clean 会多一次判定"},
                {"key": "judge.lead_trim", "label": "删句首垫字（嗯／诶）", "type": "checkbox",
                 "defaultValue": bool(judge.lead_trim),
                 "description": "删错很掉人味·默认关"},
                {"key": "judge.timeout", "label": "单次判定预算（秒）", "type": "number",
                 "defaultValue": float(judge.timeout), "min": 0.5, "max": 20.0, "step": 0.5, "description": "超时按人话放行"},
            ],
        },
        {
            "id": "memory",
            "title": "称呼与记忆",
            "description": "超时按人话放行",
            "fields": [
                {"key": "memory.learn_user_name", "label": "学习对方的名字", "type": "checkbox",
                 "defaultValue": bool(memory.learn_user_name),
                 "description": "他说「叫我 X」才记"},
                {"key": "memory.inject", "label": "每轮提醒她对方是谁", "type": "checkbox",
                 "defaultValue": bool(memory.inject), "description": "每轮提醒她对方是谁"},
                {"key": "memory.address_suffix", "label": "称呼后缀", "type": "text",
                 "defaultValue": memory.address_suffix,
                 "description": "留空＝只叫名字"},
                {"key": "memory.address_honorific", "label": "非同学时用哪档", "type": "text",
                 "defaultValue": memory.address_honorific,
                 "description": "设计，不是原典"},
                {"key": "memory.address_gap_turns", "label": "两次称呼至少隔几轮", "type": "integer",
                 "defaultValue": int(memory.address_gap_turns), "min": 0, "max": 50, "step": 1,
                 "description": "约 8–10 轮一次"},
            ],
        },
        {
            "id": "runtime",
            "title": "运行环境",
            "description": "换机器才需要动。相对路径按项目根解析。",
            "fields": [
                {"key": "mind.node_exe", "label": "node 可执行文件", "type": "text",
                 "defaultValue": mind.node_exe, "span": "full",
                 "placeholder": "留空 = 从 PATH 找（需要 Node 18+）", "description": "留空＝从 PATH 找"},
                {"key": "mind.toolkit_dir", "label": "工具箱目录", "type": "text",
                 "defaultValue": mind.toolkit_dir, "span": "full",
                 "placeholder": "留空 = 插件自带的 toolkit/ 快照", "description": "留空＝自带快照"},
                {"key": "mind.state_root", "label": "状态根", "type": "text",
                 "defaultValue": mind.state_root, "span": "full",
                 "placeholder": "留空 = <项目根>/data/nanami", "description": "留空＝data/nanami"},
                {"key": "mind.marker", "label": "卡片前的标记行", "type": "text",
                 "defaultValue": mind.marker, "span": "full",
                 "description": "留空＝卡片原文"},
            ],
        },
    ]


def values_of(settings: Any) -> dict[str, Any]:
    """按 schema 的 key 反查当前值（点号键也在这里展开）。"""
    mind = settings.mind
    guard = settings.guard
    contract = settings.contract
    memory = settings.memory
    props = settings.props
    judge = settings.judge
    persona = settings.persona
    return {
        "enabled": bool(settings.enabled),
        "character_name": settings.character_name,
        "chat_key_prefix": settings.chat_key_prefix,
        "log_trace": bool(settings.log_trace),
        "persona.inject": bool(persona.inject),
        "mind.enabled": bool(mind.enabled),
        "mind.node_exe": mind.node_exe,
        "mind.toolkit_dir": mind.toolkit_dir,
        "mind.state_root": mind.state_root,
        "mind.card_timeout": float(mind.card_timeout),
        "mind.her_timeout": float(mind.her_timeout),
        "mind.guard_timeout": float(mind.guard_timeout),
        "mind.retries": int(mind.retries),
        "mind.card_position": mind.card_position,
        "mind.marker": mind.marker,
        "mind.percept": bool(mind.percept),
        "mind.percept_timeout": float(mind.percept_timeout),
        "mind.writeback": bool(mind.writeback),
        "guard.enabled": bool(guard.enabled),
        "guard.mode": guard.mode,
        "guard.max_chars": int(guard.max_chars),
        "contract.enabled": bool(contract.enabled),
        "contract.max_dialog_items": int(contract.max_dialog_items),
        "contract.max_speech_chars": int(contract.max_speech_chars),
        "memory.learn_user_name": bool(memory.learn_user_name),
        "memory.inject": bool(memory.inject),
        "memory.address_suffix": memory.address_suffix,
        "memory.address_honorific": memory.address_honorific,
        "memory.address_gap_turns": int(memory.address_gap_turns),
        "props.enabled": bool(props.enabled),
        "props.mode": props.mode,
        "props.timeout": float(props.timeout),
        "judge.enabled": bool(judge.enabled),
        "judge.timeout": float(judge.timeout),
        "judge.ai_flavor": judge.ai_flavor,
        "judge.lead_trim": bool(judge.lead_trim),
    }


def coerce_values(values: Any, schema: list[dict[str, Any]]) -> dict[str, Any]:
    """把表单值折成能写回 YAML 的类型：按 schema 的 type 决定 bool/int/float/str。"""
    kinds: dict[str, str] = {}
    for group in schema:
        for field in group.get("fields") or []:
            kinds[str(field.get("key"))] = str(field.get("type") or "text")

    def _as_text(raw: Any) -> str:
        return "" if raw is None else str(raw).strip()

    out: dict[str, Any] = {}
    for key, kind in kinds.items():
        if key not in values:
            continue
        raw = values.get(key)
        if kind == "checkbox":
            if isinstance(raw, str):
                out[key] = raw.strip().lower() in ("1", "true", "yes", "on")
            else:
                out[key] = bool(raw)
        elif kind == "number":
            try:
                out[key] = round(float(raw), 4)
            except (TypeError, ValueError):
                continue
        elif kind == "integer":
            try:
                out[key] = int(float(raw))
            except (TypeError, ValueError):
                continue
        else:
            out[key] = _as_text(raw)
    return out


def register_page(register, settings_provider, save_callback) -> bool:
    """注册设置页。失败只记日志，绝不影响人格模型本身。"""
    try:
        from sdk.types import FrontendConfigContribution

        schema = build_schema(settings_provider())

        def _load() -> dict[str, Any]:
            current = settings_provider()
            # 页面每次打开都重建 schema，这样默认值/可见性跟着当前设置走
            fresh = build_schema(current)
            if fresh and schema != fresh:
                schema[:] = fresh
            return values_of(current)

        def _save(values: Any) -> None:
            save_callback(coerce_values(values, schema))

        register.register_frontend_config_page(
            FrontendConfigContribution(
                page_id=PAGE_ID,
                title=PAGE_TITLE,
                description=(
                    "人格模型的三层参数：人格壳、心理模型与意图、守卫与判官。"
                    "保存后下一轮生效（按 mtime 热加载，不用重启）。"
                ),
                schema=schema,
                load_values=_load,
                save_values=_save,
                kind="settings",
                order=40.0,
            )
        )
        logger.info("nanami_persona: 已注册「%s」页面（%d 组）", PAGE_TITLE, len(schema))
        return True
    except Exception:  # noqa: BLE001
        logger.exception("nanami_persona: 注册设置页失败，插件其余功能不受影响")
        return False
