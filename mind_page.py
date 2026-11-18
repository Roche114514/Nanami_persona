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

# 只读展示的当下动态状态（通用心理学指标；可通过角色包的 labels 覆写名称）
READONLY_FIELDS: tuple[tuple[str, str], ...] = (
    ("energy", "当下精力水平（0 极疲 / 1 充沛）"),
    ("need", "疲劳 / 休息压力（0 清醒 / 1 极困）"),
    ("moodV", "情绪效价 Valence（-1 消极低落 / +1 积极愉悦）"),
    ("moodA", "情绪唤醒 Arousal（0 平静 / 1 高唤醒）"),
    ("moodWarmth", "社交情绪温度 Warmth（疏离—温暖）"),
    ("woundGame", "评价敏感 A（领域单一化否定累积）"),
    ("woundHuman", "评价敏感 B（存在 / 身份质疑累积）"),
    ("woundSkill", "评价敏感 C（具体能力否定累积）"),
    ("agenda", "当下心理倾向（Agenda）"),
    ("maslowDominant", "马斯洛当下胜出需要层"),
    ("maslowScores", "六层紧迫度快照（生理/安全/归属/尊重/实现/超越）"),
    ("maslowInversion", "价值优先于安全（Priority Inversion）"),
    ("romanceStage", "关系与爱恋阶段（Sternberg）"),
    ("romanceAffinity", "恋爱好感综合分（亲密×0.75 + 信任×0.25）"),
    ("romanceThreshold", "当前恋爱线解锁门槛"),
    ("romanceIntimacy", "亲密度 Intimacy（0 陌生 / 1 深厚）"),
    ("romancePassion", "心动 / 激情 Passion（0 平静 / 1 强烈）"),
    ("romanceCommitment", "承诺度 Commitment（0 临时 / 1 坚定）"),
    ("romanceWarmth", "双向心意暖意 Mutual Warmth（0–1）"),
    ("romanceLock", "反恋爱脑尊重锁状态"),
    ("romanceScores", "情感三角与对等度（亲密/心动/承诺/对等）"),
    ("needConflict", "当下内在动机冲突（Ambivalence）"),
    ("socialStance", "L8 社会动机姿态与连接反转"),
    ("aggressionStyle", "L8 侵犯策略风格（冲动 / 清醒算计）"),
    ("socialMotives", "五大社会动机实时激活度（亲和/成就/权力/侵犯/利他）"),
    ("frustration", "挫折累积度 Frustration（0 平静 / 1 极高）"),
    ("abandonmentPanic", "分离与失去恐慌（0 安稳 / 1 极强）"),
    ("exclusivityTension", "关系排他/失衡张力（0 平稳 / 1 极强）"),
    ("transgressionIndex", "越界潜势 / 破壁指数（0–1）"),
    ("boundaryCompare", "连结拉力 vs 边界阻力"),
    ("fileAccessStatus", "破壁文件读写权限实时状态"),
    ("mentalLoad", "心理与认知负荷（0 轻松 / 1 高压）"),
    ("dynamicsBasin", "稳态吸引盆（calm 常态 / engaged 投入 / guarded 防备）"),
    ("actDeflection", "最近 ACT 偏离度 D（互动张力参考）"),
)


def _field_for(key: str, value: Any, *, label_overrides: dict[str, str] | None = None) -> dict[str, Any]:
    label = (label_overrides or {}).get(key) or _label_of(key)
    if isinstance(value, str):
        return {
            "key": key, "label": label, "type": "select",
            "defaultValue": value,
            "options": [
                {"value": "low", "label": "low（顺从 / 随和）"},
                {"value": "medium", "label": "medium（平衡）"},
                {"value": "high", "label": "high（独立 / 主导）"},
            ],
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
    "needProfile.relatedness": "归属与连结需要",
    "needProfile.competence": "胜任需要",
    "needProfile.safety": "安全需要（自保权重）",
    "needProfile.esteem": "尊重与尊严需要",
    "needProfile.selfActualization": "自我实现需要",
    "needProfile.transcendence": "自我超越需要（利他担当）",
    "needProfile.autonomy": "自主需要",
    "socialEfficacy": "社会效能",
    "surgency": "外向度",
    "domainEfficacy.game": "主优势域效能（Primary）",
    "domainEfficacy.nonGame": "泛域 / 跨领域效能（Non-Primary）",
    "privateSelfConsciousness": "私我意识",
    "publicSelfConsciousness": "公我意识",
    "selfDisclosureThreshold": "自我披露门槛",
    "evaluativeSensitivity": "评价性敏感",
    "woundThreshold": "敏感点触发阈值",
    "woundGameDecay": "敏感点 A 衰减率",
    "woundHumanDecay": "敏感点 B 衰减率",
    "woundSkillDecay": "敏感点 C 衰减率",
    "act.evalAnchor": "ACT 评价基线 E（亲善—疏离）",
    "act.potencyAnchor": "ACT 力度基线 P（柔顺—主导）",
    "act.activityAnchor": "ACT 活跃基线 A（沉静—跳脱）",
    "act.deflectionThreshold": "ACT 偏离触发阈值 D",
    "act.restoreStrength": "ACT 张力拉平强度",
    "dynamics.hysteresisInertia": "吸引盆滞回惯性",
    "dynamics.schemaPermission": "陌生域直觉类比开放度",
    "dynamics.questionRestraint": "语用追问克制度",
    "dynamics.ellipsisBudget": "停顿省略号「……」预算",
    "dynamics.verboseTighten": "句幅防膨胀收紧度",
    "attachment.anxiety": "依恋焦虑度（Attachment Anxiety）",
    "attachment.avoidance": "依恋回避度（Attachment Avoidance）",
    "attachment.romanceOpenness": "浪漫发展开放度（0=纯友人线）",
    "attachment.romanceUnlockThreshold": "恋爱线好感解锁阈值",
    "attachment.romancePace": "情感演化速率乘子",
    "social.affiliation": "亲和动机基线（nAff · 源于依恋）",
    "social.achievement": "成就动机基线（nAch · 源于胜任与自我实现）",
    "social.power": "权力动机基线（nPow · 影响力与掌控）",
    "social.aggression": "侵犯动机基线（Agg · 挫折—侵犯转化）",
    "social.prosocial": "亲社会 / 利他动机（Pro · 源于超越）",
    "social.exclusivityNeed": "二元关系排他性与独占敏感度",
    "social.reciprocitySensitivity": "社会交换对等与尊严防线敏度",
    "social.normCompliance": "社会规范内化与礼貌边界约束力",
}

_DESCRIPTIONS = {
    "effortfulControl": "行为与言语抑制控制度：高＝先思后言、克制冲动；低＝直爽脱口而出",
    "arousalBaseline": "基础唤醒基线：高＝活跃敏捷不易困；低＝沉静舒缓、容易显疲",
    "needProfile.relatedness": "对人际连结、陪伴与共同体关系的内在需要强度",
    "needProfile.competence": "在重视或擅长领域中感受到「能胜任」的内在需要强度",
    "needProfile.safety": "安全自保与风险回避权重：高＝危机或威胁下更优先护住自身安危与边界",
    "needProfile.esteem": "内在道德一致性与自我尊重权重：不愿推卸责任或违背自身信念",
    "needProfile.selfActualization": "实现核心使命与个人价值信念的驱动力：在关键抉择时可对抗低层匮乏退缩",
    "needProfile.transcendence": "在真实同伴危机或重大价值冲突中愿将他者/信念置于自身安全之上的程度（无意义试探不触发）",
    "needProfile.autonomy": "自主倾向：low＝温和顺从／medium＝弹性平衡／high＝高自主抗拒支配",
    "socialEfficacy": "社交主动性与掌控感：高＝擅长接话与带节奏／低＝慢热内敛、不抢话",
    "surgency": "外倾活跃度：高＝热情外放／低＝安静收敛、不做情绪表演",
    "domainEfficacy.game": "角色核心主优势域的自我效能感（高＝笃定自信）",
    "domainEfficacy.nonGame": "非主优势域 / 陌生领域的自我效能感（低＝更易坦承不熟或谨慎）",
    "privateSelfConsciousness": "私我意识：向内关注自身思绪、感受与内省整理的程度",
    "publicSelfConsciousness": "公我意识：在意他人目光与进行外部形象管理的程度",
    "selfDisclosureThreshold": "自我披露门槛：高＝慢热、被问及或关系变深后才敞开；低＝乐于主动倾诉",
    "evaluativeSensitivity": "对他人评价、赞美或贬低的敏感权重（1.0＝全额吸收）",
    "woundThreshold": "负面评价刺激累积达到此阈值后，才显著驱动当前防御状态",
    "woundGameDecay": "敏感点 A（如能力单一化指控）每轮自然衰减保留系数（越小消退越快）",
    "woundHumanDecay": "敏感点 B（如存在性质疑）每轮自然衰减保留系数",
    "woundSkillDecay": "敏感点 C（如具体能力否定）每轮自然衰减保留系数",
    "act.evalAnchor": "ACT 基本认同点 E0（0 批判疏离 ↔ 0.5 中性 ↔ 1 亲和温暖）",
    "act.potencyAnchor": "ACT 基本力度点 P0（0 弱气顺从 ↔ 0.5 平和持中 ↔ 1 强势主导）",
    "act.activityAnchor": "ACT 基本活跃点 A0（0 沉静缓慢 ↔ 0.5 适中 ↔ 1 高活跃跳脱）",
    "act.deflectionThreshold": "互动印象偏离基准达到此阈值后才介入纠偏张力（越高越迟钝宽容）",
    "act.restoreStrength": "高偏离时往回拉平局面的力度（0 不主动拉平 / 1 强烈守住本位）",
    "dynamics.hysteresisInertia": "进入防备/投入稳态后的粘滞惯性（越大越不易因单句话骤然跳变）",
    "dynamics.schemaPermission": "遇陌生事物时允许借用自身设定熟悉经验作直觉类比的倾向（0 只说不懂）",
    "dynamics.questionRestraint": "抑制「为延续聊天而在句尾硬抛问题」的语用克制力度",
    "dynamics.ellipsisBudget": "允许「……」停顿出现的比例参考线；滑动窗超标时自动提醒直接把话说完",
    "dynamics.verboseTighten": "当连续回复越说越长时自动拉回简短节奏的收紧强度",
    "attachment.anxiety": "越高越敏感察觉冷淡与疏远，在意自己是否被厌烦，得到安抚后回暖快",
    "attachment.avoidance": "越高越倾向在动心或亲密升温时克制保留，出现「想靠近又收半步」的张力",
    "attachment.romanceOpenness": "0=严格维持纯同伴/友人线（Passion锁定0）；>0 允许在跨过解锁阈值后萌发恋爱情感",
    "attachment.romanceUnlockThreshold": "亲密与信任综合需达到此好感门槛，才会开启萌芽→暧昧→伴侣的恋爱发展线",
    "attachment.romancePace": "控制亲密/心动/承诺每次互动的累积步长：0 极慢热长对话；0.5 均衡；1.0 适配短对话快速推进",
    "social.affiliation": "源于 L2 归属与 L7 依恋：极高时把保住眼前的关系置于个人面子与安全之前",
    "social.achievement": "源于 L2 胜任/自我实现与 L3 域效能：驱动面对挑战时追求破局、卓越与证明能力",
    "social.power": "社会影响与掌控欲：高利他时化作护持主导；高排他低规范时化作个人化去留掌控",
    "social.aggression": "核心诉求受阻或遭敌对挫折时的侵犯转化倾向：高值且低规范时生出冷酷压迫、威胁或极端占有感",
    "social.prosocial": "不计回报帮助、托底与成全他者的社会驱动力（与侵犯动机互为对照极）",
    "social.exclusivityNeed": "对注意力唯一性与排他闭环的诉求强度：越高越容易把外部转移或冷落算作重大挫折",
    "social.reciprocitySensitivity": "对居高临下与地位不对等的防卫权重：高=守住平等边界；极低=为换取连结可退让自尊",
    "social.normCompliance": "日常道德、礼貌与规则常识对侵犯和极端的刹车力度：正常角色偏高，极端人格极低",
}


# 内置指标对应的「先写名字、再调数字」映射表：(trait_key, profile_flat_key, domain_label_key, ui_label, placeholder)
_PAIR_LABEL_EDITORS: dict[str, tuple[str, str, str, str]] = {
    "domainEfficacy.game": (
        "domains.primary_label", "domainEfficacy.game",
        "主优势域 · 自定义名称", "留空使用默认「主优势域效能（Primary）」",
    ),
    "domainEfficacy.nonGame": (
        "domains.non_primary_label", "domainEfficacy.nonGame",
        "泛域 / 非主域 · 自定义名称", "留空使用默认「泛域 / 跨领域效能（Non-Primary）」",
    ),
    "woundGameDecay": (
        "domains.wound_a_label", "woundGame",
        "敏感点 A · 自定义名称", "先输入敏感点名称（留空使用「敏感点 A」）",
    ),
    "woundHumanDecay": (
        "domains.wound_b_label", "woundHuman",
        "敏感点 B · 自定义名称", "先输入敏感点名称（留空使用「敏感点 B」）",
    ),
    "woundSkillDecay": (
        "domains.wound_c_label", "woundSkill",
        "敏感点 C · 自定义名称", "先输入敏感点名称（留空使用「敏感点 C」）",
    ),
}

_LABEL_EDITORS: tuple[tuple[str, str, str, str, str, str], ...] = (
    # (field_key, layer_id, profile_flat_key, domain_label_key, ui_label, placeholder)
    ("_label.domainEfficacy.game", "L3", *_PAIR_LABEL_EDITORS["domainEfficacy.game"]),
    ("_label.domainEfficacy.nonGame", "L3", *_PAIR_LABEL_EDITORS["domainEfficacy.nonGame"]),
    ("_label.woundGame", "L4", *_PAIR_LABEL_EDITORS["woundGameDecay"]),
    ("_label.woundHuman", "L4", *_PAIR_LABEL_EDITORS["woundHumanDecay"]),
    ("_label.woundSkill", "L4", *_PAIR_LABEL_EDITORS["woundSkillDecay"]),
)

_PAIR_FIELD_KEYS: dict[str, str] = {
    "domainEfficacy.game": "_label.domainEfficacy.game",
    "domainEfficacy.nonGame": "_label.domainEfficacy.nonGame",
    "woundGameDecay": "_label.woundGame",
    "woundHumanDecay": "_label.woundHuman",
    "woundSkillDecay": "_label.woundSkill",
}


def _label_of(key: str) -> str:
    return _LABELS.get(key, key.split(".")[-1])


def _description_of(key: str) -> str:
    return _DESCRIPTIONS.get(key, "")


def _normalize_custom_list(
    raw_items: Any,
    *,
    id_prefix: str,
    num_key: str,
    default_num: float,
    default_title: str,
    extra_defaults: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """规范化前端提交的自定义能力域 / 敏感点数组，生成合法且不重名的 id。"""
    if not isinstance(raw_items, list):
        return []
    used_ids: set[str] = set()
    cleaned: list[dict[str, Any]] = []
    for idx, item in enumerate(raw_items, start=1):
        if not isinstance(item, dict):
            continue
        raw_id = str(item.get("id") or "").strip()
        cid = raw_id if raw_id and raw_id not in used_ids else ""
        if not cid:
            seq = idx
            while f"{id_prefix}_{seq}" in used_ids:
                seq += 1
            cid = f"{id_prefix}_{seq}"
        used_ids.add(cid)
        label = str(item.get("label") or "").strip() or f"{default_title} {idx}"
        try:
            num_val = round(max(0.0, min(1.0, float(item.get(num_key, default_num)))), 3)
        except (TypeError, ValueError):
            num_val = default_num
        row: dict[str, Any] = {
            "id": cid,
            "label": label,
            "patterns": str(item.get("patterns") or "").strip(),
            num_key: num_val,
        }
        if extra_defaults:
            for k, dv in extra_defaults.items():
                if k in item and item[k] is not None:
                    try:
                        row[k] = round(max(0.0, min(1.0, float(item[k]))), 3)
                    except (TypeError, ValueError):
                        row[k] = dv
                else:
                    row[k] = dv
        cleaned.append(row)
    return cleaned


def build_schema(
    values: dict[str, Any],
    live_state: dict[str, Any],
    *,
    label_overrides: dict[str, str] | None = None,
    pointer_options: list[dict[str, str]] | None = None,
    current_pointer_id: str = "",
    include_label_editors: bool = False,
    custom_domains: tuple[dict[str, Any], ...] | list[dict[str, Any]] = (),
    custom_wounds: tuple[dict[str, Any], ...] | list[dict[str, Any]] = (),
) -> list[dict[str, Any]]:
    del custom_domains, custom_wounds
    groups: list[dict[str, Any]] = []
    overrides = label_overrides or {}
    for layer in mind_params.LAYERS:
        fields = []
        for key in layer["keys"]:
            if include_label_editors and key in _PAIR_LABEL_EDITORS:
                pkey = _PAIR_FIELD_KEYS[key]
                _prof_key, dkey, ui_label, placeholder = _PAIR_LABEL_EDITORS[key]
                # 先输入自定义名称，再紧跟具体数字
                fields.append({
                    "key": pkey,
                    "label": ui_label,
                    "type": "text",
                    "defaultValue": str(overrides.get(dkey) or ""),
                    "placeholder": placeholder,
                    "description": "先命名这个维度/敏感点（留空即使用默认心理学术语），下一格设置具体数字",
                })
            if key in values:
                fields.append(_field_for(key, values[key], label_overrides=overrides))
        if fields:
            groups.append({
                "id": layer["id"],
                "title": layer["title"],
                "description": layer["description"],
                "fields": fields,
            })

    readonly = []
    for key, label in READONLY_FIELDS:
        shown_label = (label_overrides or {}).get(key) or label
        value = live_state.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            readonly.append({"key": f"state.{key}", "label": shown_label, "type": "number",
                             "defaultValue": round(float(value), 2), "min": -1.0, "max": 1.0,
                             "step": 0.01, "description": "只读·每轮心理学状态机动态推算"})
        elif isinstance(value, str) and value:
            readonly.append({"key": f"state.{key}", "label": shown_label, "type": "text",
                             "defaultValue": value, "description": "只读·每轮心理学状态机动态推算"})
    if readonly:
        groups.append({
            "id": "now",
            "title": "当下状态（只读）",
            "description": "以下是每轮通过心理模型实时推导出的动态量，仅供观察；若要调整角色性格，请调节上方各层人格参数。",
            "fields": readonly,
        })
    if pointer_options:
        groups.append({
            "id": "target_pointer",
            "title": "当前编辑的角色指针",
            "description": "切换后点击保存即可切换到对应角色的心理参数（切换那一次不会覆盖目标角色的现有数值）。",
            "fields": [
                {
                    "key": "_pointer_id",
                    "label": "编辑目标角色",
                    "type": "select",
                    "defaultValue": current_pointer_id or pointer_options[0]["value"],
                    "options": pointer_options,
                    "description": "每个指针有独立的 traits.json 与 mind.json",
                }
            ],
        })
    return groups


def values_of(effective: dict[str, Any]) -> dict[str, Any]:
    return {key: effective[key] for key in mind_params.DEFAULT_TRAITS if key in effective}


def _live_state(
    state_root: Path,
    chat_key_prefix: str,
    *,
    profile: Any = None,
    traits: dict[str, Any] | None = None,
) -> dict[str, Any]:
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
    chat_key, latest = entries[0][0], dict(entries[0][1])
    maslow = latest.get("maslow")
    if isinstance(maslow, dict):
        if maslow.get("dominantLayer"):
            latest["maslowDominant"] = str(maslow["dominantLayer"])
        urg = maslow.get("urgency")
        if isinstance(urg, dict):
            latest["maslowScores"] = (
                f"生理 {float(urg.get('physiological', 0)):.2f} · "
                f"安全 {float(urg.get('safety', 0)):.2f} · "
                f"归属 {float(urg.get('belonging', 0)):.2f} · "
                f"尊重 {float(urg.get('esteem', 0)):.2f} · "
                f"实现 {float(urg.get('selfActualization', 0)):.2f} · "
                f"超越 {float(urg.get('transcendence', 0)):.2f}"
            )
        if "priorityInversion" in maslow:
            latest["maslowInversion"] = "是（自我实现/超越 > 安全）" if maslow.get("priorityInversion") else "否（常规层级）"
        conflict = maslow.get("conflict")
        if isinstance(conflict, dict) and conflict.get("label"):
            latest["needConflict"] = str(conflict["label"])
    rom = latest.get("romance")
    if isinstance(rom, dict):
        stage = str(rom.get("stage") or "stranger")
        tri = str(rom.get("triangleType") or "none")
        intimacy = round(float(rom.get("intimacy", 0.0) or 0.0), 2)
        passion = round(float(rom.get("passion", 0.0) or 0.0), 2)
        commitment = round(float(rom.get("commitment", 0.0) or 0.0), 2)
        warmth = round(float(rom.get("mutualWarmth", 0.0) or 0.0), 2)
        trust_now = float(latest.get("trust", 0.6) if latest.get("trust") is not None else 0.6)
        affinity = rom.get("affinityScore")
        if affinity is None:
            affinity = 0.75 * intimacy + 0.25 * trust_now
        threshold = rom.get("unlockThreshold")
        if threshold is None and isinstance(traits, dict):
            threshold = traits.get("attachment.romanceUnlockThreshold", 0.55)
        if threshold is None:
            threshold = 0.55
        unlocked = bool(rom.get("unlocked"))
        respect_lock = bool(rom.get("respectLock"))

        latest["romanceStage"] = f"{stage}（{tri}{' · 已解锁恋爱线' if unlocked else ' · 未解锁'}）"
        latest["romanceAffinity"] = round(float(affinity), 2)
        latest["romanceThreshold"] = round(float(threshold), 2)
        latest["romanceIntimacy"] = intimacy
        latest["romancePassion"] = passion
        latest["romanceCommitment"] = commitment
        latest["romanceWarmth"] = warmth
        latest["romanceLock"] = "已触发尊重锁（暂停恋爱升温）" if respect_lock else "正常平等（未锁定）"
        latest["romanceScores"] = (
            f"亲密 {intimacy:.2f} · "
            f"心动 {passion:.2f} · "
            f"承诺 {commitment:.2f} · "
            f"对等 {float(latest.get('reciprocityBalance', 0.85)):.2f}"
        )
    soc = latest.get("socialState")
    trans_idx_val = 0.0
    if isinstance(soc, dict):
        stance = str(soc.get("stance") or "balanced")
        ov = " · 连结压倒尊严/安全" if soc.get("connectionOverride") else ""
        latest["socialStance"] = f"{stance}{ov}"
        raw_style = str(soc.get("aggressionStyle") or "none")
        style_map = {
            "calculated": "calculated（清醒算计型）",
            "impulsive": "impulsive（冲动爆发型）",
            "none": "none（常规克制）",
        }
        latest["aggressionStyle"] = style_map.get(raw_style, raw_style)
        mot = soc.get("motives")
        if isinstance(mot, dict):
            latest["socialMotives"] = (
                f"亲和 {float(mot.get('affiliation', 0)):.2f} · "
                f"成就 {float(mot.get('achievement', 0)):.2f} · "
                f"权力 {float(mot.get('power', 0)):.2f} · "
                f"侵犯 {float(mot.get('aggression', 0)):.2f} · "
                f"利他 {float(mot.get('prosocial', 0)):.2f}"
            )
        if soc.get("frustration") is not None:
            latest["frustration"] = round(float(soc.get("frustration") or 0.0), 2)
        if soc.get("abandonmentPanic") is not None:
            latest["abandonmentPanic"] = round(float(soc.get("abandonmentPanic") or 0.0), 2)
        if soc.get("exclusivityTension") is not None:
            latest["exclusivityTension"] = round(float(soc.get("exclusivityTension") or 0.0), 2)
        if soc.get("transgressionIndex") is not None:
            trans_idx_val = round(float(soc.get("transgressionIndex") or 0.0), 2)
            latest["transgressionIndex"] = trans_idx_val
        if soc.get("connectionPull") is not None and soc.get("boundaryResistance") is not None:
            pull_v = float(soc.get("connectionPull") or 0.0)
            res_v = float(soc.get("boundaryResistance") or 0.0)
            latest["boundaryCompare"] = f"连结拉力 {pull_v:.2f} vs 边界阻力 {res_v:.2f}"
    if profile is not None:
        if not getattr(profile, "file_access_enabled", False):
            latest["fileAccessStatus"] = "未开启（常规对话角色）"
        else:
            mode_label = "读写" if getattr(profile, "file_access_allow_write", False) else "只读"
            min_t = float(getattr(profile, "file_access_min_transgression", 0.0) or 0.0)
            if trans_idx_val >= min_t:
                latest["fileAccessStatus"] = f"已解锁{mode_label}（当前越界 {trans_idx_val:.2f} ≥ 阈值 {min_t:.2f}）"
            else:
                latest["fileAccessStatus"] = f"待解锁{mode_label}（当前越界 {trans_idx_val:.2f} < 阈值 {min_t:.2f}）"
    dyn_path = Path(state_root) / "state" / "dynamics.json"
    if dyn_path.is_file():
        try:
            dyn_raw = json.loads(dyn_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            dyn_raw = {}
        if isinstance(dyn_raw, dict):
            dyn_entry = dyn_raw.get(chat_key)
            if not isinstance(dyn_entry, dict) and dyn_raw:
                dyn_entry = next((v for v in dyn_raw.values() if isinstance(v, dict)), None)
            if isinstance(dyn_entry, dict):
                if dyn_entry.get("basin"):
                    latest["dynamicsBasin"] = str(dyn_entry["basin"])
                if dyn_entry.get("last_deflection") is not None:
                    try:
                        latest["actDeflection"] = round(float(dyn_entry["last_deflection"]), 3)
                    except (TypeError, ValueError):
                        pass
    return latest


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
            opts = [
                {"value": p.id, "label": f"{p.character_name or p.id}（{p.id}）"}
                for p in pointers
            ]
            bound_names = {str(p.character_name or "").strip() for p in pointers if p.character_name}
            if hasattr(registry, "list_host_characters"):
                for hc in registry.list_host_characters():
                    hname = str(hc.get("name") or "").strip()
                    if hname and hname not in bound_names:
                        opts.append({
                            "value": f"__host__:{hname}",
                            "label": f"＋ 绑定宿主角色：{hname}（从角色列表新建）",
                        })
            return p_root, p_prefix, (profile.domain_labels, seed, profile.custom_domains, profile.wounds, profile), opts, pointer.id

        init_root, init_prefix, init_meta, init_opts, init_pid = _active_target()
        init_labels, init_seed, init_domains, init_wounds, init_prof = init_meta if init_meta else ({}, None, (), (), None)
        init_eff = mind_params.effective(init_root, seed_path=init_seed)
        use_label_editors = registry is not None
        schema = build_schema(
            values_of(init_eff),
            _live_state(init_root, init_prefix, profile=init_prof, traits=init_eff),
            label_overrides=init_labels,
            pointer_options=init_opts,
            current_pointer_id=init_pid,
            include_label_editors=use_label_editors,
            custom_domains=init_domains,
            custom_wounds=init_wounds,
        )

        def _load() -> dict[str, Any]:
            t_root, t_prefix, meta, opts, pid = _active_target()
            labels, seed, domains, wounds, prof = meta if meta else ({}, None, (), (), None)
            eff = mind_params.effective(t_root, seed_path=seed)
            current = values_of(eff)
            state = _live_state(t_root, t_prefix, profile=prof, traits=eff)
            fresh = build_schema(
                current,
                state,
                label_overrides=labels,
                pointer_options=opts,
                current_pointer_id=pid,
                include_label_editors=use_label_editors,
                custom_domains=domains,
                custom_wounds=wounds,
            )
            if fresh and schema != fresh:
                schema[:] = fresh
            # 只读项也一起给值，页面才显示得出来
            for key, _label in READONLY_FIELDS:
                if key in state:
                    current[f"state.{key}"] = state[key]
            if use_label_editors:
                for fkey, _lid, _pkey, dkey, _ul, _ph in _LABEL_EDITORS:
                    current[fkey] = str((labels or {}).get(dkey) or "")
                current["_custom_domains"] = [dict(x) for x in domains]
                current["_custom_wounds"] = [dict(x) for x in wounds]
            if pid:
                current["_pointer_id"] = pid
            return current

        def _save(values: Any) -> None:
            t_root, _t_prefix, _meta, _opts, current_pid = _active_target()
            if registry is not None and "_pointer_id" in values:
                picked = str(values.get("_pointer_id") or "").strip()
                if picked.startswith("__host__:") and hasattr(registry, "resolve_or_create_host_pointer"):
                    host_name = picked[len("__host__:"):].strip()
                    created = registry.resolve_or_create_host_pointer(host_name)
                    if created is not None:
                        _load()
                        if on_changed is not None:
                            on_changed()
                        return
                if picked and picked != current_pid and registry.set_active_edit(picked):
                    # 切换指针那一次不拿旧角色的表单数值覆写新角色的 traits.json
                    _load()
                    return
            patch = {key: values.get(key) for key in mind_params.DEFAULT_TRAITS if key in values}
            mind_params.save(t_root, patch)
            if registry is not None and current_pid:
                label_patch: dict[str, Any] = {}
                for fkey, _lid, pkey, _dkey, _ul, _ph in _LABEL_EDITORS:
                    if fkey in values:
                        label_patch[pkey] = str(values.get(fkey) or "").strip()
                if label_patch:
                    registry.update_profile_values(current_pid, label_patch)

                if "_custom_domains" in values and isinstance(values.get("_custom_domains"), list):
                    registry.update_custom_domains(
                        current_pid,
                        _normalize_custom_list(
                            values.get("_custom_domains"),
                            id_prefix="domain",
                            num_key="efficacy",
                            default_num=0.5,
                            default_title="自定义能力域",
                        ),
                    )
                if "_custom_wounds" in values and isinstance(values.get("_custom_wounds"), list):
                    registry.update_custom_wounds(
                        current_pid,
                        _normalize_custom_list(
                            values.get("_custom_wounds"),
                            id_prefix="wound",
                            num_key="decay",
                            default_num=0.7,
                            default_title="自定义敏感点",
                            extra_defaults={"weight": 0.7},
                        ),
                    )

            _load()
            if on_changed is not None:
                on_changed()

        mind_html = Path(__file__).resolve().parent / "web" / "mind.html"
        if mind_html.is_file() and hasattr(register, "register_frontend_page"):
            register.register_frontend_page(
                FrontendPageContribution(
                    page_id=PAGE_ID,
                    title=PAGE_TITLE,
                    entry=str(mind_html),
                    kind="settings",
                    description="按 L1 气质 / L2 需要 / L3 自我 / L4 评价性担忧分层配置心理学人格模型，支持箱内增删能力域与敏感点。",
                    order=41.0,
                )
            )
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
