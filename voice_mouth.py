"""认知（大脑）与表达（嘴巴）解耦器：
- 大脑（Brain）：结合心理模型状态卡与对话历史，冷静产出纯语义意图（action / stance / intent / keep_facts），
  将“不太想说”“他在试探我”等元认知消化在内部意图里，不直接泄漏为台词。
- 嘴巴（Mouth）：完全隔离长历史与复杂规则包袱，仅看角色原声语料（corpus.tsv）与大脑意图，
  一次生成 3 个口语候选并用本地规则过滤「先反问再陈述」「汇报内心」「书面AI腔」，选出最像真人的一句。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Sequence

from . import acg_corpus, judge

_THINK_RE = re.compile(r"<think>[\s\S]*?</think>", re.IGNORECASE)
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)

# 严禁“嘴巴”吐出的典型 AI 味/元汇报/书面腔模式
_META_AI_RE = re.compile(
    r"(?:不太想说|不太想聊|不想聊这个|不想说这个|问得.{0,4}太直接|换个话题|聊点别的|"
    r"话题(?:跳|转)得.{0,4}太快|聊回正题|我倒没想到|顺便一提|写不利索|说不利索|作为AI|人工智能|系统设定)",
    re.IGNORECASE,
)
# 典型“先复述关键词反问 + 再陈述解释”两段式模板
_ECHO_SPLIT_RE = re.compile(r"^[^。！？\n]{1,8}[？?]\s*(?:/|\s+)[^。！？\n]{3,}")
_STAGE_DIR_RE = re.compile(r"[（(][^）)\n]{1,40}[）)]")
_BARE_CHARS_RE = re.compile(r"[^\u4e00-\u9fa5A-Za-z0-9]")

_BRAIN_PROMPT = """\
你是角色的「决策大脑」（只负责想清楚这一轮要传达什么意思，**不需要**模拟角色口吻）。
请结合上文中的角色设定、心理状态卡、社会防线、对他的态度印象与对话历史，输出这轮的纯语义决策 JSON：
{
  "stance": "她此刻心里的真实想法/防线（例如：被问房租很烦躁，根本不想告诉陌生人）",
  "action": "从 direct_reply | deflect | snap_back | silence | leave | cold_ack | soft_accept 中选一个",
  "intent": "用一句大白话说清她这轮要传达的具体内容或态度（例如：拒答房租打工的事，反顶对方管太多）",
  "keep_facts": ["这轮必须带出或守住的关键事实词，没有则填 []"]
}
注意态度一致性：
- 若状态卡提示「平常初识（尚未被冒犯）」且 `relationalDebt == 0`、`wound == 0`：对待刚认识的人保持正常社交礼貌与平常心（可用 direct_reply 或温和 deflect），**不要无端用「关我什么事」「查户口」这类带刺的话呛人**。
- 若状态卡提示「已选择离开（不再搭理）」：除非对方这一句展现出真正能从心底打动人的诚恳态度，否则必须选 `silence`，`intent` 填「保持沉默不再搭理」。
- 若对他的态度已经跌破底线、继续回答明显毫无意义（哪怕情绪是平静的，只是不想再产生任何交集）：直接行使离开权限，选 `leave`（平静或冷淡转身离开，不再接话）。
- 若状态卡提示「对他的印象：厌恶疏离 / 冷淡存疑」或处于冷静期，哪怕对方这轮突然道别、送礼或邀约，也绝不能瞬间变脸热络、答应邀约或说关心话（如「路上小心」），必须保持冷淡距离（用 cold_ack / deflect / snap_back / leave）。
只输出合法 JSON。"""

_MOUTH_PROMPT = """\
你是角色的「发声嘴巴」（只负责把“大脑意图”转成真人张嘴说的话，不要做任何逻辑说教或内心汇报）。

必须严格遵守：
1. **死守【角色说话底色】与下方【原声语料】的年龄感、语气词、短句节奏与性格形状**（通常 1 句话说完，4–26 字）。
2. **严禁套用市井油滑或老气横秋的通用吵架套话**（如「想得倒美」「这还差不多」「算你识相」「说了句人话」「别拿些乱七八糟的糊弄我」「少来这套」），必须用符合这个角色本人性格与说话习惯的表达。
3. **绝对禁止把大脑的 `stance`（内心活动）念成台词**：严禁出现「这个不太想说」「我不想聊这个」「你问得太直接了」「换个话题吧」这类心理汇报或对话元评价！
4. **绝对禁止「先复述关键词加问号 + 再解释陈述」的 AI 两段式**（例如禁止「Python？找错人了吧」「好笑？那你笑个够吧」）。直接把真正想说的话一口气说出来！
5. **绝对禁止书面拼接词**（如「写不利索」「我倒没想到」），只用符合她本人的自然口语。
6. 一次给出 6 个风格和句式尽量拉开差异的口语候选短句（每条都是完整单句，不带括号动作描写），只输出合法 JSON：
{"candidates": ["说法1", "说法2", "说法3", "说法4", "说法5", "说法6"]}"""


def _parse_json(raw: str) -> dict[str, Any]:
    s = _THINK_RE.sub("", str(raw or "")).strip()
    s = _FENCE_RE.sub("", s).strip()
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else {}
    except ValueError:
        m = re.search(r"\{[\s\S]*\}", s)
        if m:
            try:
                obj = json.loads(m.group(0))
                return obj if isinstance(obj, dict) else {}
            except ValueError:
                pass
    return {}


def _repetition_penalty(text: str, recent_replies: Sequence[str] = ()) -> float:
    """对与近期已说过台词重复用词/句式（如连续「查户口」「轮不到你」）的候选施加惩罚。"""
    if not recent_replies:
        return 0.0
    bare = _BARE_CHARS_RE.sub("", str(text or ""))
    if len(bare) < 3:
        return 0.0
    c2 = {bare[i : i + 2] for i in range(len(bare) - 1)} if len(bare) >= 2 else set()
    c3 = {bare[i : i + 3] for i in range(len(bare) - 2)}
    c4 = {bare[i : i + 4] for i in range(len(bare) - 3)} if len(bare) >= 4 else set()
    stem_pats = (
        re.compile(r"查户口"),
        re.compile(r"轮不?到你"),
        re.compile(r"关.{0,2}什么事"),
        re.compile(r"用不着你"),
        re.compile(r"少来这套"),
        re.compile(r"烦死了"),
    )
    c_stems = {idx for idx, pat in enumerate(stem_pats) if pat.search(bare)}
    penalty = 0.0
    recent = [str(r or "").strip() for r in recent_replies if str(r or "").strip()][-6:]
    for dist, prev in enumerate(reversed(recent), 1):
        pb = _BARE_CHARS_RE.sub("", prev)
        if len(pb) < 3:
            continue
        if pb == bare:
            return 12.0
        p2 = {pb[i : i + 2] for i in range(len(pb) - 1)} if len(pb) >= 2 else set()
        p3 = {pb[i : i + 3] for i in range(len(pb) - 2)}
        p4 = {pb[i : i + 4] for i in range(len(pb) - 3)} if len(pb) >= 4 else set()
        p_stems = {idx for idx, pat in enumerate(stem_pats) if pat.search(pb)}
        decay = 1.0 if dist <= 2 else (0.75 if dist <= 4 else 0.5)
        if c4 & p4 or (c_stems & p_stems):
            penalty = max(penalty, 7.5 * decay)
        elif c3 & p3:
            overlap = len(c3 & p3) / max(1, min(len(c3), len(p3)))
            penalty = max(penalty, (4.5 + 3.0 * overlap) * decay)
        elif len(c2 & p2) >= 2 and (len(c2 & p2) / max(1, min(len(c2), len(p2)))) >= 0.30:
            penalty = max(penalty, 4.0 * decay)
    return penalty


def _score_candidate(
    text: str,
    action: str,
    recent_replies: Sequence[str] = (),
) -> float:
    t = str(text or "").strip()
    if not t:
        return -100.0
    score = 0.0
    if _META_AI_RE.search(t):
        score -= 12.0
    if _ECHO_SPLIT_RE.search(t):
        score -= 6.0
    if _STAGE_DIR_RE.search(t):
        score -= 5.0
    score -= _repetition_penalty(t, recent_replies)
    if action == "silence" and re.fullmatch(r"[…\.。]+", t):
        score += 2.0
    if 3 <= len(t) <= 26:
        score += 2.5
    elif len(t) > 36:
        score -= 2.0
    # 轻度惩罚句首机械“重复对方词？”开头
    if re.match(r"^[^。！？]{1,6}[？?]\s*[^\s]", t):
        score -= 3.5
    return score


def plan_brain_intent(
    messages: Sequence[dict[str, Any]],
    *,
    adapter: Any = None,
) -> dict[str, Any]:
    """大脑阶段：读取完整设定、心理状态卡与历史，输出纯语义意图包（不负责说话风格）。"""
    llm = adapter if adapter is not None else judge._default_adapter()
    if llm is None:
        return {"stance": "", "action": "direct_reply", "intent": "自然回应", "keep_facts": []}

    brain_msgs = [dict(m) for m in messages]
    brain_msgs.append({"role": "system", "content": _BRAIN_PROMPT})
    resp = llm.chat(
        messages=brain_msgs,
        stream=False,
        temperature=0.35,
        max_tokens=260,
        response_format={"type": "json_object"},
    )
    raw = str(getattr(llm.decode_response(resp), "content", "") or "")
    parsed = _parse_json(raw)
    return {
        "stance": str(parsed.get("stance") or "").strip(),
        "action": str(parsed.get("action") or "direct_reply").strip(),
        "intent": str(parsed.get("intent") or "").strip() or "简短自然回应",
        "keep_facts": [str(x).strip() for x in (parsed.get("keep_facts") or []) if str(x).strip()][:5],
    }


def _load_voice_anchors(profile_dir: Path | None, action: str) -> list[tuple[str, str]]:
    if profile_dir is None:
        return []
    cpath = Path(profile_dir) / "corpus.tsv"
    if not cpath.is_file():
        return []
    buckets = acg_corpus.load_corpus_file(cpath)
    priority_map = {
        "snap_back": ("annoyed", "reject", "cold"),
        "deflect": ("cold", "annoyed", "reject"),
        "silence": ("cold", "reject"),
        "leave": ("cold", "reject", "annoyed"),
        "cold_ack": ("cold", "repair_afterglow", "annoyed"),
        "soft_accept": ("repair_afterglow", "casual"),
        "direct_reply": ("casual", "curious", "annoyed"),
    }
    order = priority_map.get(action, ("casual", "curious", "annoyed", "cold", "reject", "repair_afterglow"))
    picked: list[tuple[str, str]] = []
    for b in order:
        for pair in buckets.get(b) or ():
            if pair not in picked:
                picked.append(pair)
            if len(picked) >= 8:
                return picked
    for b, pairs in buckets.items():
        for pair in pairs:
            if pair not in picked:
                picked.append(pair)
            if len(picked) >= 10:
                return picked
    return picked


def _format_param_state_block(state_snapshot: dict[str, Any] | None) -> str:
    if not state_snapshot:
        return "（未传入实时心理与人格参数快照）"
    tr = state_snapshot.get("traits") or {}
    st = state_snapshot.get("state") or {}
    soc_tr = state_snapshot.get("social_traits") or {}
    soc = st.get("socialState") or {}
    s_live = st.get("sociology") or {}
    att = st.get("attitude") or {}
    wp = ((st.get("wantProfile") or {}).get("primary") or {}).get("id") or st.get("agenda") or "?"

    def _f(d: dict[str, Any], key: str, default: float = 0.0) -> str:
        val = d.get(key)
        if val is None:
            return f"{default:.2f}"
        try:
            return f"{float(val):.2f}"
        except (TypeError, ValueError):
            return str(val)

    param_line = (
        f"外向主动(surgency)={_f(tr, 'surgency', 0.25)} | "
        f"自制力(effortfulControl)={_f(tr, 'effortfulControl', 0.80)} | "
        f"对等敏感(reciprocitySensitivity)={_f(tr, 'social.reciprocitySensitivity', 0.70)} | "
        f"回避(attachment.avoidance)={_f(tr, 'attachment.avoidance', 0.30)} | "
        f"较真反击(retaliationDrive)={_f(soc_tr, 'interaction.retaliationDrive', soc.get('retaliationDrive', 0.35))}"
    )
    state_line = (
        f"稳态(basin)={state_snapshot.get('basin', 'calm')} | "
        f"主导意图(want)={wp} | "
        f"精力(energy)={_f(state_snapshot, 'energy', 0.60)} | "
        f"伤口(wound)={_f(state_snapshot, 'wound', 0.0)} | "
        f"深层伤痕(grudgeScar)={_f(st, 'grudgeScar', 0.0)} | "
        f"尊严敷衍(trivializedDignity)={bool(st.get('trivializedDignity'))} | "
        f"冷却期(coolOffTurns)={int(s_live.get('coolOffTurns') or 0)} | "
        f"防卫档(defenseTier)={int(s_live.get('defenseTier') or 0)} | "
        f"关系阶段(relStage)={s_live.get('relStage', 'stranger')} | "
        f"净吸引(netAttraction)={_f(s_live, 'netAttraction', 0.0)}"
    )
    att_line = (
        f"态度档位(attitudeStance)={att.get('attitudeStance', 'polite_neutral')} | "
        f"好感度(affectiveValence)={_f(att, 'affectiveValence', 0.0)} | "
        f"尊重信任(respectTrust)={_f(att, 'respectTrust', 0.60)} | "
        f"诚意感知(perceivedSincerity)={_f(att, 'perceivedSincerity', 0.55)} | "
        f"累积心结(relationalDebt)={_f(att, 'relationalDebt', 0.0)} | "
        f"已离开不再搭理(disengaged)={bool(att.get('disengaged'))}"
    )
    return f"固定人格参数：{param_line}\n当下瞬时状态：{state_line}\n长时态度印象：{att_line}"


def _pick_best_for_persona(
    character_name: str,
    cands: list[str],
    anchors: list[tuple[str, str]],
    brain_intent: dict[str, Any],
    *,
    persona_hint: str = "",
    state_snapshot: dict[str, Any] | None = None,
    recent_replies: Sequence[str] = (),
    adapter: Any = None,
) -> str:
    """在已过滤掉 AI 味的干净候选中，结合角色人设、心理参数与当下瞬时状态挑出最贴合的一条。"""
    action = str(brain_intent.get("action") or "direct_reply")
    scored = sorted(
        [(idx, c, _score_candidate(c, action, recent_replies)) for idx, c in enumerate(cands)],
        key=lambda x: -x[2],
    )
    # 淘汰明显违规（分数 < 0）的候选，至少保留最高分的 1 条兜底
    clean_pool = [c for _idx, c, sc in scored if sc >= 0.0]
    if not clean_pool:
        return scored[0][1]
    if len(clean_pool) == 1 or adapter is None:
        return clean_pool[0]

    anchor_preview = " / ".join(f"「{a}」" for _u, a in anchors[:6]) or "自然口语短句"
    cand_lines = "\n".join(f"{i}. {c}" for i, c in enumerate(clean_pool))
    param_state_text = _format_param_state_block(state_snapshot)
    picker_sys = (
        f"你是「{character_name}」的台词选角导演。请从下面的干净候选中，挑出**既最符合她固定人格参数、又严格吻合她当下心理与社会瞬时状态**的那 1 条。\n"
        "选角准则：\n"
        "1. **原声形状第一**：必须最贴合「角色底色」的年龄气质与「原声参考」的口吻节奏；直接淘汰任何市井油滑、老气横秋或路人吵架套话（如「想得倒美」「这还差不多」「说了句人话」「糊弄我」「少来这套」）。\n"
        "2. 若 `grudgeScar >= 0.35` / `trivializedDignity=True` / `coolOffTurns > 0` / `basin=guarded`：绝不能选轻松翻篇或热络顺从的句子，必须选仍带芥蒂、硬气或冷硬距离感的一条。\n"
        "3. 若 `attitudeStance` 为 `hostile_alienated` 或 `cold_skeptical`，或 `relationalDebt >= 0.32` 或 `respectTrust < 0.35`：哪怕当前没吵架，也绝不能选「路上小心」「拜拜」「陪你去」「谢、谢谢」等热络/关心/软化妥协的句子，必须选冷淡、疏离、敷衍或带刺的句子。\n"
        "4. 若 `attitudeStance` 为 `polite_neutral` 且 `relationalDebt == 0`、`coolOffTurns == 0`、`wound < 0.2`：对方尚未冒犯，必须保持正常人的初识礼貌与平常心，绝不能选「关我什么事」「你是查户口的」「烦死了」等无端攻击或呛人的句子。\n"
        "5. 结合 `surgency` / `effortfulControl` / `retaliationDrive`：高较真反击、低自制选更冲更直白的一条；低外向高回避选更克制收短的一条。\n"
        "6. 严禁挑选与「近期已说过」在核心词汇或句式上撞车的候选（如刚说过「查户口」「轮不到你」「关你什么事」，下一轮必须换新鲜表达）。\n"
        '只输出合法 JSON：{"index": 0}'
    )
    recent_preview = " / ".join(f"「{r}」" for r in [str(x).strip() for x in recent_replies if str(x).strip()][-5:]) or "无"
    picker_user = (
        f"角色底色：{persona_hint or character_name}\n"
        f"{param_state_text}\n"
        f"原声参考：{anchor_preview}\n"
        f"近期已说过（严禁撞车重复）：{recent_preview}\n"
        f"此刻心境：{brain_intent.get('stance') or '自然'}\n\n"
        f"候选台词：\n{cand_lines}"
    )
    try:
        resp = adapter.chat(
            messages=[
                {"role": "system", "content": picker_sys},
                {"role": "user", "content": picker_user},
            ],
            stream=False,
            temperature=0.1,
            max_tokens=40,
            response_format={"type": "json_object"},
        )
        raw = str(getattr(adapter.decode_response(resp), "content", "") or "")
        idx = int(_parse_json(raw).get("index", 0))
        if 0 <= idx < len(clean_pool):
            return clean_pool[idx]
    except Exception:
        pass
    return clean_pool[0]


def realize_mouth_utterance(
    character_name: str,
    user_last_msg: str,
    brain_intent: dict[str, Any],
    *,
    profile_dir: Path | None = None,
    persona_hint: str = "",
    state_snapshot: dict[str, Any] | None = None,
    recent_replies: Sequence[str] = (),
    adapter: Any = None,
) -> tuple[str, list[str]]:
    """嘴巴阶段：隔离历史与复杂规则，仅看角色原声语料与大脑意图，生成 6 个候选并挑出最贴合人设的 1 句。"""
    st_att = ((state_snapshot or {}).get("state") or {}).get("attitude") or {}
    action = str(brain_intent.get("action") or "direct_reply")
    if bool(st_att.get("disengaged")) or action == "silence":
        return "······", ["······"]

    llm = adapter if adapter is not None else judge._default_adapter()
    if llm is None:
        return str(brain_intent.get("intent") or "……"), []
    anchors = _load_voice_anchors(profile_dir, action)
    anchor_text = "\n".join(f"- 对方：「{u}」 → {character_name}：「{a}」" for u, a in anchors) or "（暂无样本，保持简短直率口语）"
    facts = ", ".join(brain_intent.get("keep_facts") or []) or "无"

    recent_list = [str(x).strip() for x in recent_replies if str(x).strip()][-5:]
    recent_block = (
        "\n\n【最近几轮已经说过的话（严禁重复相同用词或同一句式，必须换新说法）】\n"
        + "\n".join(f"- {r}" for r in recent_list)
        if recent_list
        else ""
    )

    style_hint = str(persona_hint or "").strip()
    if not style_hint and profile_dir is not None:
        soul_p = Path(profile_dir) / "soul.md"
        if soul_p.is_file():
            try:
                for line in soul_p.read_text(encoding="utf-8").splitlines():
                    s = line.strip()
                    if s and not s.startswith("#") and not s.startswith("<!--"):
                        style_hint = s[:160]
                        break
            except OSError:
                pass
    style_block = f"【角色说话底色与年龄气质】\n{style_hint}\n\n" if style_hint else ""

    user_prompt = (
        f"角色名：{character_name}\n\n"
        f"{style_block}"
        f"【{character_name} 原声语料（模仿这里的短句节奏与用词）】\n{anchor_text}\n\n"
        f"【对方刚才说】\n{user_last_msg}\n\n"
        f"【大脑传来的意图（禁止把心声原话照念）】\n"
        f"- 动作倾向：{action}\n"
        f"- 内心底色：{brain_intent.get('stance') or '自然'}\n"
        f"- 要传达的意思：{brain_intent.get('intent')}\n"
        f"- 需保留的关键事实：{facts}"
        f"{recent_block}"
    )

    resp = llm.chat(
        messages=[
            {"role": "system", "content": _MOUTH_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        stream=False,
        temperature=0.82,
        max_tokens=420,
        response_format={"type": "json_object"},
    )
    raw = str(getattr(llm.decode_response(resp), "content", "") or "")
    parsed = _parse_json(raw)
    cands = [
        _STAGE_DIR_RE.sub("", str(c or "")).strip().strip("「」\"“”")
        for c in (parsed.get("candidates") or [])
        if str(c or "").strip()
    ]
    if not cands:
        fallback = str(parsed.get("text") or brain_intent.get("intent") or "……").strip()
        return fallback, [fallback]

    best = _pick_best_for_persona(
        character_name,
        cands,
        anchors,
        brain_intent,
        persona_hint=persona_hint,
        state_snapshot=state_snapshot,
        recent_replies=recent_list,
        adapter=llm,
    )
    return best, cands
