"""LLM 按「人格层」智能推填「人格 / 心理模型」参数。

读取当前角色指针的 `soul.md`（含编辑器未保存草稿）及宿主角色卡设定，
请宿主配置的大模型一次推导并填充：
  1. L1–L8 全部 44 项量化心理 / 动力学 / 依恋 / 社会动机参数（写入 traits.json）
  2. L3/L4 指标在该角色语境下的命名（domains.*_label）
  3. 角色专属 L3 自定义能力域（custom_domains）与 L4 自定义敏感点（wounds）
  4. 说话节奏、称呼习惯与判官角色锚点提示（contract / memory / judge.system_prompt）
"""

from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path
from typing import Any, Callable, Mapping

from . import judge, mind_page, mind_params, social_page, social_params

logger = logging.getLogger(__name__)

_THINK_RE = re.compile(r"<think>[\s\S]*?</think>", re.IGNORECASE)
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)

_LABEL_KEYS: tuple[str, ...] = (
    "domains.primary_label",
    "domains.non_primary_label",
    "domains.wound_a_label",
    "domains.wound_b_label",
    "domains.wound_c_label",
)

_CALIBRATION_GUIDE = """参数校准原则（务必忠于角色设定本身，不要一律给中间温和值 0.5）：
1. L1 气质与行为策略区分：
   - 冷静/克制/腹黑/清醒算计型：effortfulControl 高（0.70–0.95），arousalBaseline 偏低到适中（0.20–0.55）。
   - 直爽/元气/急躁/冲动爆发型：effortfulControl 偏低（0.15–0.45），arousalBaseline 高（0.65–0.92），surgency 高。
2. L2 马斯洛需要与自主性：
   - 温和顺从 / 依赖型：needProfile.autonomy = "low"；独立主见强 / 傲娇 / 主导控制型：needProfile.autonomy = "high"。
   - 牺牲守护 / 执念誓言型：needProfile.transcendence 与 selfActualization 可给高值（0.75–0.95）；极度利己自保型：needProfile.safety 高而 transcendence 低。
3. L7 依恋与浪漫发展：
   - 严格纯友人/无恋爱线角色：attachment.romanceOpenness = 0.0。
   - 慢热/克制/傲娇：attachment.avoidance 偏高（0.55–0.85），romanceUnlockThreshold 偏高（0.55–0.75），romancePace 适中。
   - 极度粘人/病娇/害怕被抛弃：attachment.anxiety 高（0.75–0.95），attachment.romanceOpenness 高（0.70–1.0）。
4. L8 社会动机与病娇/反常人格边界（关键）：
   - 普通正常角色：social.normCompliance 较高（0.65–0.90），social.aggression 较低（0.05–0.25），social.exclusivityNeed 适中（0.25–0.55）。
   - 病娇 / 偏执独占 / 不择手段型（需配合低道德刹车才会呈现清醒压迫或越界行为）：
     * social.normCompliance 必须压低（0.05–0.20）
     * social.exclusivityNeed 极高（0.82–0.98）
     * social.aggression（0.70–0.95）与 social.power（0.70–0.95）拉高
     * 若属「清醒疯狂 / 暗中算计」而非大喊大叫，保持 effortfulControl ≥ 0.72、arousalBaseline ≤ 0.50
5. L3 能力域与 L4 敏感点：
   - custom_domains：提炼 1–3 个该角色独有的专长/擅长领域（patterns 用 `|` 分隔 3–6 个中文触发关键词）。
   - custom_wounds：提炼 1–3 个最容易刺痛或激怒该角色的雷区/敏感点（patterns 用 `|` 分隔 3–6 个关键词）。
6. 说话节奏与判官：
   - contract.max_dialog_items（整数 1–8）、contract.max_speech_chars（整数 15–160）、memory.address_gap_turns（整数 0–30）。
   - judge.system_prompt：写 1–2 句具体判定准则，告诉语义判官“怎样像 AI 客服腔，怎样才是这个角色本人的自然口吻”。"""


def _param_reference_lines() -> str:
    """从 mind_params 与 social_params 实时生成 L1–L8 + S1–S3 全量参数说明，避免清单漂移。"""
    lines: list[str] = []
    for layer in mind_params.LAYERS:
        lines.append(f"[{layer['id']}] {layer['title']}")
        for key in layer["keys"]:
            base = mind_params.DEFAULT_TRAITS.get(key)
            label = mind_page._LABELS.get(key, key)
            desc = mind_page._DESCRIPTIONS.get(key, "")
            typ = '"low"|"medium"|"high"' if isinstance(base, str) else "0.00–1.00"
            lines.append(f"  - {key} ({typ}) [{label}]: {desc}")
    for layer in social_params.LAYERS:
        extra_keys = [k for k in layer["keys"] if k not in social_params.SHARED_L8_KEYS]
        if not extra_keys:
            continue
        lines.append(f"[{layer['id']}] {layer['title']}")
        for key in extra_keys:
            label = social_page._LABELS.get(key, key)
            desc = social_page._DESCRIPTIONS.get(key, "")
            lines.append(f"  - {key} (0.00–1.00) [{label}]: {desc}")
    return "\n".join(lines)


def _host_character_context(registry: Any, char_name: str) -> str:
    if not char_name or not hasattr(registry, "list_host_characters"):
        return ""
    for item in registry.list_host_characters():
        if str(item.get("name") or "").strip() != char_name:
            continue
        parts: list[str] = []
        brief = str(item.get("character_brief") or "").strip()
        setting = str(item.get("character_setting") or "").strip()
        if brief:
            parts.append(f"角色简介：{brief}")
        if setting:
            parts.append(f"宿主设定：{setting[:2000]}")
        return "\n".join(parts)
    return ""


def _build_prompts(
    *,
    character_name: str,
    display_name: str,
    soul_text: str,
    host_context: str,
    extra_context: str = "",
) -> tuple[str, str]:
    system_prompt = (
        "你是计算心理学与社会学角色人格建模专家。请根据用户提供的角色「人格壳（soul.md）」、设定文本与语料样本，"
        "推导并填写整套量化心理模型（L1–L8）与社会学模型（S1–S3）参数。\n\n"
        "参数定义参考（必须严格使用以下键名）：\n"
        f"{_param_reference_lines()}\n\n"
        f"{_CALIBRATION_GUIDE}\n\n"
        "必须只输出一个合法 JSON 对象（不要 markdown 围栏，不要额外解释），字段结构如下：\n"
        "{\n"
        '  "summary": "1-2句中文诊断，概括该角色核心性格张力及关键参数设定的依据",\n'
        '  "traits": { "<L1–L8 心理参数键值对，数值范围 0.0–1.0，needProfile.autonomy 为 low|medium|high>": ... },\n'
        '  "social_traits": { "<S1–S3 社会学参数 attraction.* / relationship.* / interaction.*，范围 0.0–1.0>": ... },\n'
        '  "labels": {\n'
        '    "domains.primary_label": "角色最核心主优势域名称（10字内）",\n'
        '    "domains.non_primary_label": "泛域/不擅长域名称（12字内）",\n'
        '    "domains.wound_a_label": "敏感点A名称（14字内）",\n'
        '    "domains.wound_b_label": "敏感点B名称（14字内）",\n'
        '    "domains.wound_c_label": "敏感点C名称（14字内）"\n'
        "  },\n"
        '  "custom_domains": [\n'
        '    {"label": "专长领域名", "efficacy": 0.85, "patterns": "关键词1|关键词2|关键词3"}\n'
        "  ],\n"
        '  "custom_wounds": [\n'
        '    {"label": "敏感点名称", "decay": 0.70, "weight": 0.80, "patterns": "触发词1|触发词2|触发词3"}\n'
        "  ],\n"
        '  "profile": {\n'
        '    "contract.max_dialog_items": 3,\n'
        '    "contract.max_speech_chars": 48,\n'
        '    "memory.address_suffix": "",\n'
        '    "memory.address_honorific": "",\n'
        '    "memory.address_gap_turns": 8,\n'
        '    "judge.system_prompt": "判官锚点提示（留空表示用通用）"\n'
        "  }\n"
        "}"
    )

    sections: list[str] = [
        f"角色名：{character_name or display_name or '未命名角色'}",
    ]
    if display_name and display_name != character_name:
        sections.append(f"模板显示名：{display_name}")
    if host_context:
        sections.append(host_context)
    clean_soul = str(soul_text or "").strip()
    if clean_soul:
        sections.append("人格壳正文（soul.md）：\n" + clean_soul[:6000])
    if extra_context and str(extra_context).strip():
        sections.append("补充考据与语料摘要：\n" + str(extra_context).strip()[:2500])
    sections.append("请完整推算并输出 JSON。")
    return system_prompt, "\n\n".join(sections)


def _extract_json_dict(raw_text: str) -> dict[str, Any]:
    cleaned = _THINK_RE.sub("", str(raw_text or "")).strip()
    cleaned = _FENCE_RE.sub("", cleaned).strip()
    for candidate in judge._json_candidates(cleaned):
        try:
            parsed = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict) and parsed:
            return parsed
    raise ValueError("大模型返回内容不是有效 JSON")


def _call_llm(
    system_prompt: str,
    user_prompt: str,
    *,
    adapter_provider: Callable[[], Any] | None = None,
    timeout: float = 45.0,
) -> dict[str, Any]:
    provider = adapter_provider or judge._default_adapter
    adapter = provider()
    if adapter is None:
        raise RuntimeError("未检测到可用的宿主大模型，请先在模型设置中配置 API")

    result: dict[str, Any] = {}
    done = threading.Event()

    def _invoke(*, use_json_format: bool) -> str:
        kwargs: dict[str, Any] = {
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
            "temperature": 0.2,
            "max_tokens": 4096,
        }
        if use_json_format:
            kwargs["response_format"] = {"type": "json_object"}
        response = adapter.chat(**kwargs)
        decoded = adapter.decode_response(response)
        return str(getattr(decoded, "content", "") or "")

    def work() -> None:
        try:
            try:
                content = _invoke(use_json_format=True)
                result["data"] = _extract_json_dict(content)
            except Exception:
                # 兼容 Gemini / 部分中转网关不支持 response_format={"type": "json_object"} 的情况
                content = _invoke(use_json_format=False)
                result["data"] = _extract_json_dict(content)
        except Exception as exc:  # noqa: BLE001
            result["error"] = exc
        finally:
            done.set()

    thread = threading.Thread(target=work, name="nanami-mind-autofill", daemon=True)
    thread.start()
    if not done.wait(max(5.0, float(timeout))):
        raise TimeoutError(f"大模型推填超时（>{timeout:.0f}s），请稍后重试")
    if "error" in result:
        raise RuntimeError(f"调用大模型失败：{result['error']}") from result["error"]
    return dict(result.get("data") or {})


def _clamp_int(val: Any, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(hi, int(round(float(val)))))
    except (TypeError, ValueError):
        return default


def _sanitize_payload(
    raw: Mapping[str, Any],
    *,
    baseline_traits: Mapping[str, Any],
    baseline_social: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    raw_traits = raw.get("traits") if isinstance(raw.get("traits"), Mapping) else {}
    flat_traits = mind_params._flatten(dict(raw_traits)) if raw_traits else {}
    coerced = mind_params.coerce(flat_traits)
    merged_traits = dict(baseline_traits)
    merged_traits.update(coerced)

    raw_social = raw.get("social_traits") if isinstance(raw.get("social_traits"), Mapping) else {}
    flat_social = mind_params._flatten(dict(raw_social)) if raw_social else {}
    # 兼容 LLM 把 attraction.*/relationship.*/interaction.* 直接塞进 traits 的情况
    for k, v in flat_traits.items():
        if k in social_params.DEFAULT_SOCIAL_TRAITS and k not in flat_social:
            flat_social[k] = v
    coerced_social = social_params.coerce(flat_social)
    merged_social = dict(baseline_social or social_params.DEFAULT_SOCIAL_TRAITS)
    merged_social.update(coerced_social)
    for k in social_params.SHARED_L8_KEYS:
        if k in coerced:
            merged_social[k] = float(coerced[k])

    raw_labels = raw.get("labels") if isinstance(raw.get("labels"), Mapping) else {}
    clean_labels: dict[str, str] = {}
    for k in _LABEL_KEYS:
        if k in raw_labels and raw_labels.get(k) is not None:
            txt = str(raw_labels.get(k) or "").strip()
            if txt:
                clean_labels[k] = txt[:40]

    custom_domains = mind_page._normalize_custom_list(
        list(raw.get("custom_domains") or [])[:4],
        id_prefix="domain",
        num_key="efficacy",
        default_num=0.5,
        default_title="专长领域",
    )
    custom_wounds = mind_page._normalize_custom_list(
        list(raw.get("custom_wounds") or [])[:4],
        id_prefix="wound",
        num_key="decay",
        default_num=0.7,
        default_title="敏感点",
        extra_defaults={"weight": 0.75},
    )

    raw_prof = raw.get("profile") if isinstance(raw.get("profile"), Mapping) else {}
    clean_profile: dict[str, Any] = {}
    if "contract.max_dialog_items" in raw_prof:
        clean_profile["contract.max_dialog_items"] = _clamp_int(raw_prof["contract.max_dialog_items"], 1, 8, 3)
    if "contract.max_speech_chars" in raw_prof:
        clean_profile["contract.max_speech_chars"] = _clamp_int(raw_prof["contract.max_speech_chars"], 10, 200, 48)
    if "memory.address_gap_turns" in raw_prof:
        clean_profile["memory.address_gap_turns"] = _clamp_int(raw_prof["memory.address_gap_turns"], 0, 60, 8)
    if "memory.address_suffix" in raw_prof and raw_prof["memory.address_suffix"] is not None:
        clean_profile["memory.address_suffix"] = str(raw_prof["memory.address_suffix"]).strip()[:12]
    if "memory.address_honorific" in raw_prof and raw_prof["memory.address_honorific"] is not None:
        clean_profile["memory.address_honorific"] = str(raw_prof["memory.address_honorific"]).strip()[:12]
    if "judge.system_prompt" in raw_prof and raw_prof["judge.system_prompt"] is not None:
        clean_profile["judge.system_prompt"] = str(raw_prof["judge.system_prompt"]).strip()[:300]

    summary = str(raw.get("summary") or "").strip()[:300]
    return {
        "summary": summary,
        "traits": merged_traits,
        "social_traits": merged_social,
        "filled_keys": sorted(coerced.keys()),
        "social_filled_keys": sorted(k for k in coerced_social if k not in social_params.SHARED_L8_KEYS),
        "labels": clean_labels,
        "custom_domains": custom_domains,
        "custom_wounds": custom_wounds,
        "profile": clean_profile,
    }


def autofill_from_soul(
    registry: Any,
    pointer_id: str,
    *,
    soul_text: str = "",
    extra_context: str = "",
    adapter_provider: Callable[[], Any] | None = None,
    timeout: float = 45.0,
    apply: bool = True,
) -> dict[str, Any]:
    """按当前指针的人格层（soul.md + 宿主角色设定）请大模型推算并填充人格/心理与社会学模型。"""
    pointer = registry.get_pointer(pointer_id)
    if pointer is None:
        raise ValueError(f"未找到角色指针：{pointer_id}")

    profile = registry.load_profile(pointer)
    live_soul = str(soul_text or "").strip()
    saved_soul = registry.read_soul_text(pointer.id) if hasattr(registry, "read_soul_text") else ""
    effective_soul = live_soul or str(saved_soul or "").strip()
    host_context = _host_character_context(registry, pointer.character_name or "")

    if not effective_soul and not host_context:
        raise ValueError("当前角色的人格壳正文（soul.md）与宿主设定均为空，请先填写或导入角色设定")

    system_prompt, user_prompt = _build_prompts(
        character_name=pointer.character_name or "",
        display_name=profile.display_name or "",
        soul_text=effective_soul,
        host_context=host_context,
        extra_context=extra_context,
    )
    raw_answer = _call_llm(
        system_prompt,
        user_prompt,
        adapter_provider=adapter_provider,
        timeout=timeout,
    )

    state_root = registry.resolve_state_root(pointer)
    seed_path = profile.resolve_path(profile.traits_file, plugin_root=registry.plugin_root)
    baseline = mind_params.effective(state_root, seed_path=seed_path)
    baseline_social = social_params.effective(state_root, seed_path=seed_path)
    parsed = _sanitize_payload(raw_answer, baseline_traits=baseline, baseline_social=baseline_social)

    if apply:
        mind_params.save(state_root, parsed["traits"])
        social_params.save(state_root, parsed["social_traits"], seed_path=seed_path)
        profile_patch = dict(parsed["labels"])
        profile_patch.update(parsed["profile"])
        if profile_patch:
            registry.update_profile_values(pointer.id, profile_patch)
        if parsed["custom_domains"]:
            registry.update_custom_domains(pointer.id, parsed["custom_domains"])
        if parsed["custom_wounds"]:
            registry.update_custom_wounds(pointer.id, parsed["custom_wounds"])

    cname = pointer.character_name or profile.display_name or pointer.id
    filled_n = len(parsed["filled_keys"])
    soc_n = len(parsed["social_filled_keys"])
    dom_n = len(parsed["custom_domains"])
    wnd_n = len(parsed["custom_wounds"])
    msg = (
        f"LLM 已根据人格层为「{cname}」推填 {filled_n} 项 L1–L8 心理参数"
        + (f"、{soc_n} 项 S1–S3 社会学参数" if soc_n else "")
        + (f"、{dom_n} 个能力域" if dom_n else "")
        + (f"、{wnd_n} 个敏感点" if wnd_n else "")
    )
    return {
        "pointer_id": pointer.id,
        "character_name": cname,
        "summary": parsed["summary"] or msg,
        "traits_count": filled_n,
        "social_count": soc_n,
        "domains_count": dom_n,
        "wounds_count": wnd_n,
        "traits": parsed["traits"],
        "social_traits": parsed["social_traits"],
        "labels": parsed["labels"],
        "custom_domains": parsed["custom_domains"],
        "custom_wounds": parsed["custom_wounds"],
        "profile": parsed["profile"],
        "message": msg,
    }
