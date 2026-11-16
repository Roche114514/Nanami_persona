"""插件级人格动力学拓展中间件：ACT（情感控制）× 吸引盆滞回 × 语用话轮节律 × 认知图式门控。

设计定位（纯通用引擎，不入侵角色包静态设定）：
- Shinsekai 宿主与角色设定负责「她是谁、知道什么、有什么口吻」；
- 本模块仅基于 L1–L4 心理特质、mind.mjs 状态信号与真实对话滑动窗，完成跨轮动力学推演：
    1. ACT（Affect Control Theory）：由特质导出 EPA 锚点 [E, P, A]，计算刺激偏离度并给出拉平张力；
    2. 吸引子滞回（Attractor Hysteresis）：在 calm / engaged / guarded 三个稳态间平滑切换；
    3. 认知图式门控（Slot B）：仅在陌生低胜任域时授权主 LLM 调用角色设定里的参照系作直觉类比；
    4. 语用话轮节律（Slot C）：追踪近轮提问密度与句长漂移，抑制“每句追问 / 客服膨胀”。
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_WINDOW_SIZE = 4
_QUESTION_END_RE = re.compile(r"[？?]\s*$|^(?:什么是|为什么|怎么会|难道)|(?:什么意思|对不对|是不是|好不好)[？?。…\s]*$")
_NEGATIVE_CARRY = frozenset({"被误判", "只剩下游戏这一样", "非游戏域被否定"})


def _clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def _num(mapping: Mapping[str, Any], key: str, default: float) -> float:
    raw = mapping.get(key)
    if raw is None and "." in key:
        node: Any = mapping
        for part in key.split("."):
            if not isinstance(node, Mapping) or part not in node:
                node = None
                break
            node = node[part]
        raw = node
    if raw is None:
        return default
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return default
    return val if val == val else default


def _has_key(mapping: Mapping[str, Any], key: str) -> bool:
    if key in mapping:
        return True
    if "." not in key:
        return False
    node: Any = mapping
    for part in key.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return False
        node = node[part]
    return True


@dataclass(frozen=True)
class EPAPoint:
    """Affect Control Theory 三维语义坐标：Evaluation / Potency / Activity。"""

    e: float = 0.0
    p: float = 0.0
    a: float = 0.0

    def deflection(self, other: EPAPoint) -> float:
        """欧氏平方偏离度 D（只在内部驱动平衡向量，不进提示词）。"""
        return (self.e - other.e) ** 2 + (self.p - other.p) ** 2 + (self.a - other.a) ** 2


@dataclass(frozen=True)
class DynamicsSnapshot:
    """这一轮动力学推演收敛后的三正交出口槽位与内部稳态标记。"""

    basin: str = "calm"
    deflection: float = 0.0
    intention_hint: str = ""      # 合并入 Slot A（动机/张力方向）
    perspective_slot: str = ""    # Slot B：陌生域认知参照系许可（常态为空）
    turn_slot: str = ""           # Slot C：语用学话轮起止与防漂移节律


def fundamental_epa(traits: Mapping[str, Any] | None, *, trust_score: float = 0.6) -> EPAPoint:
    """从 L5 ACT 显式锚点或 L1–L4 心理特质自动导出角色固有 EPA 基准。"""
    t = traits or {}
    need_prof = t.get("needProfile") if isinstance(t.get("needProfile"), Mapping) else {}
    relatedness = _clamp(_num(need_prof, "relatedness", _num(t, "needProfile.relatedness", 0.7)), 0.0, 1.0)
    surgency = _clamp(_num(t, "surgency", 0.25), 0.0, 1.0)
    social_eff = _clamp(_num(t, "socialEfficacy", 0.30), 0.0, 1.0)
    arousal_base = _clamp(_num(t, "arousalBaseline", 0.35), 0.0, 1.0)
    trust = _clamp(trust_score, 0.0, 1.0)

    e_auto = _clamp(0.15 + 0.35 * relatedness - 0.20 * max(0.0, 0.6 - trust))
    p_auto = _clamp(-0.20 + 0.65 * (0.5 * surgency + 0.5 * social_eff))
    a_auto = _clamp((arousal_base - 0.5) * 1.4 + 0.35 * (surgency - 0.4))

    e0 = _clamp(2.0 * _clamp(_num(t, "act.evalAnchor", 0.7), 0.0, 1.0) - 1.0 - 0.15 * max(0.0, 0.6 - trust)) if _has_key(t, "act.evalAnchor") else e_auto
    p0 = _clamp(2.0 * _clamp(_num(t, "act.potencyAnchor", 0.48), 0.0, 1.0) - 1.0) if _has_key(t, "act.potencyAnchor") else p_auto
    a0 = _clamp(2.0 * _clamp(_num(t, "act.activityAnchor", 0.36), 0.0, 1.0) - 1.0) if _has_key(t, "act.activityAnchor") else a_auto
    return EPAPoint(e=e0, p=p0, a=a0)


def transient_epa(raw_state: Mapping[str, Any], appraisal: str) -> tuple[EPAPoint, str]:
    """根据当前轮感知与刺激事件计算瞬时互动印象 EPA 与张力类别。"""
    if bool(raw_state.get("insulted")) or bool(raw_state.get("wrongClaim")) or appraisal == "只剩下游戏这一样":
        return EPAPoint(e=-0.65, p=0.45, a=0.45), "hostile"
    if appraisal == "收到心头好":
        return EPAPoint(e=0.80, p=0.25, a=0.25), "care_favorite"
    if appraisal == "收到陌生礼物":
        return EPAPoint(e=0.20, p=-0.30, a=-0.05), "care_mismatch"
    if bool(raw_state.get("careGiftNow")) or appraisal == "被在意":
        return EPAPoint(e=0.65, p=0.10, a=0.05), "care_gift"
    if bool(raw_state.get("repairedNow")):
        return EPAPoint(e=0.30, p=-0.10, a=-0.15), "repaired"
    if bool(raw_state.get("praiseNow")) and appraisal == "被夸了":
        return EPAPoint(e=0.45, p=0.65, a=0.40), "praise_excess"
    if appraisal == "胜任不足":
        return EPAPoint(e=0.05, p=-0.45, a=0.05), "epistemic_gap"
    if bool(raw_state.get("masteryEngaged")) or appraisal == "胜任被邀请":
        return EPAPoint(e=0.40, p=0.40, a=0.35), "mastery"
    return EPAPoint(e=0.20, p=0.05, a=-0.10), "neutral"


def _act_tension_hint(
    tag: str,
    deflection: float,
    *,
    surgency: float,
    public_sc: float,
    threshold: float = 0.32,
    restore_strength: float = 0.65,
) -> str:
    """把高偏离度按阈值与拉平强度翻译成自然心理张力方向；偏离不大或强度极低时不干预。"""
    if restore_strength < 0.15 or deflection < threshold:
        return ""
    firm = restore_strength >= 0.80
    if tag == "praise_excess" and (surgency <= 0.50 or public_sc <= 0.40 or firm):
        return (
            "偏离度高：明确把夸赞推回事件本身并压平自身姿态，不顺着抬高往上接"
            if firm
            else "被抬高后产生失衡张力：把关注轻轻推回那件事本身，顺势放平语气，不自吹也不过度谦虚"
        )
    if tag == "hostile":
        return "对方越过正常互动位置：稳住自身边界并简短定界，不迎合也不吵闹"
    if tag == "epistemic_gap":
        return "落入陌生低把握区：坦然放低胜任期待，不必为了维持面子强行作答"
    if tag == "care_favorite":
        return "收到的礼物恰好落在你很在意的喜好上：不必压抑兴致，依你本人的风格如实表现出开心与留意细节"
    if tag == "care_mismatch":
        return "收到的东西与你平时喜好或擅长领域不太合拍：诚实带出一点困惑或温柔实话，不必违心客套"
    return ""


class DynamicsEngine:
    """插件级人格动力学运行时：无入侵、可消融、按 chat_key 独立维护滑动惯性。"""

    def __init__(self, state_root: Path | None = None) -> None:
        self._path = (Path(state_root) / "state" / "dynamics.json") if state_root else None
        self._lock = threading.Lock()
        self._store: dict[str, dict[str, Any]] = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        if self._path is None or not self._path.is_file():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {str(k): dict(v) for k, v in raw.items() if isinstance(v, dict)} if isinstance(raw, dict) else {}

    def _save(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(self._store, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            logger.debug("nanami_persona: 写入 dynamics.json 失败（忽略）", exc_info=True)

    def _slot(self, chat_key: str) -> dict[str, Any]:
        key = str(chat_key or "_default")
        entry = self._store.get(key)
        if not isinstance(entry, dict):
            entry = {
                "basin": "calm",
                "basin_streak": 0,
                "last_deflection": 0.0,
                "recent_questions": [],
                "recent_lengths": [],
                "recent_ellipses": [],
            }
            self._store[key] = entry
        return entry

    def _next_basin(
        self,
        current: str,
        streak: int,
        *,
        raw_state: Mapping[str, Any],
        appraisal: str,
        effortful: float,
        inertia: float = 0.65,
    ) -> tuple[str, int]:
        """多稳态吸引盆与滞回转换：受 L6 hysteresisInertia 与 L1 努力控制调制。"""
        wound = float(raw_state.get("wound") or 0.0)
        insulted = bool(raw_state.get("insulted")) or bool(raw_state.get("wrongClaim"))
        repaired = bool(raw_state.get("repairedNow"))
        carry_key = str(raw_state.get("appraisalCarryKey") or "")
        carry_val = float(raw_state.get("appraisalCarry") or 0.0)
        mastery = bool(raw_state.get("masteryEngaged")) or appraisal in ("胜任被邀请", "胜任不足", "收到心头好")

        if insulted or wound >= 0.6:
            target = "guarded"
        elif mastery:
            target = "engaged"
        else:
            target = "calm"

        if target == current:
            return current, streak + 1

        carry_gate = max(0.18, 0.55 - 0.35 * inertia)
        min_streak = 1 if inertia < 0.25 else (2 if inertia < 0.75 else 3)
        if current == "guarded" and not repaired and inertia >= 0.15:
            if carry_key in _NEGATIVE_CARRY and carry_val >= carry_gate:
                return "guarded", streak + 1
            if (effortful >= 0.70 or inertia >= 0.65) and streak < min_streak and target == "engaged":
                return "guarded", streak + 1
        return target, 1

    def evaluate(
        self,
        chat_key: str,
        *,
        raw_state: Mapping[str, Any],
        traits: Mapping[str, Any] | None,
        appraisal: str = "",
        cost_pick: str = "",
    ) -> DynamicsSnapshot:
        """为当前回合执行 ACT + 吸引子 + 认知图式 + 语用话轮联合推演。"""
        t = traits or {}
        surgency = _clamp(_num(t, "surgency", 0.25), 0.0, 1.0)
        social_eff = _clamp(_num(t, "socialEfficacy", 0.30), 0.0, 1.0)
        effortful = _clamp(_num(t, "effortfulControl", 0.80), 0.0, 1.0)
        public_sc = _clamp(_num(t, "publicSelfConsciousness", 0.20), 0.0, 1.0)
        deflect_thresh = _clamp(_num(t, "act.deflectionThreshold", 0.32), 0.0, 1.0)
        restore_strength = _clamp(_num(t, "act.restoreStrength", 0.65), 0.0, 1.0)
        inertia = _clamp(_num(t, "dynamics.hysteresisInertia", 0.65), 0.0, 1.0)
        schema_perm = _clamp(_num(t, "dynamics.schemaPermission", 0.75), 0.0, 1.0)
        q_restraint = _clamp(_num(t, "dynamics.questionRestraint", 0.60), 0.0, 1.0)
        ellipsis_budget = _clamp(_num(t, "dynamics.ellipsisBudget", 0.60), 0.0, 1.0)
        verbose_tighten = _clamp(_num(t, "dynamics.verboseTighten", 0.50), 0.0, 1.0)

        trust_score = float(raw_state.get("trust") if raw_state.get("trust") is not None else 0.6)
        explicit_req = bool(raw_state.get("explicitRequestNow"))
        repaired = bool(raw_state.get("repairedNow"))
        domain_eff = _clamp(_num(raw_state, "turnDomainEfficacy", 0.30), 0.0, 1.0)
        epistemic_gap = appraisal == "胜任不足"

        # 1) ACT 偏离张力
        base_epa = fundamental_epa(t, trust_score=trust_score)
        imp_epa, tag = transient_epa(raw_state, appraisal)
        deflection = round(base_epa.deflection(imp_epa), 4)
        intention_hint = _act_tension_hint(
            tag,
            deflection,
            surgency=surgency,
            public_sc=public_sc,
            threshold=deflect_thresh,
            restore_strength=restore_strength,
        )

        with self._lock:
            entry = self._slot(chat_key)
            basin, streak = self._next_basin(
                str(entry.get("basin") or "calm"),
                int(entry.get("basin_streak") or 0),
                raw_state=raw_state,
                appraisal=appraisal,
                effortful=effortful,
                inertia=inertia,
            )
            entry["basin"] = basin
            entry["basin_streak"] = streak
            entry["last_deflection"] = deflection
            recent_q = list(entry.get("recent_questions") or [])
            recent_l = list(entry.get("recent_lengths") or [])
            recent_e = list(entry.get("recent_ellipses") or [])
            self._save()

        # 2) Slot B：认知图式与礼物偏好分化门控
        perspective_slot = ""
        if tag in ("care_gift", "care_favorite", "care_mismatch") or bool(raw_state.get("careGiftNow")):
            perspective_slot = (
                "礼物偏好分化：仔细看清对方送来的具体物品，严格对照你自身设定里的喜好来决定反应——"
                "特别合心意或戳中兴趣的东西会露出几分雀跃或想细看；寻常吃喝用品自然致谢收下；"
                "不擅长或不合用之物就按性格礼貌实话实说（不要用千篇一律的「放着就好」打发）"
            )
        elif epistemic_gap or (explicit_req and domain_eff <= 0.35 and tag != "hostile"):
            if schema_perm >= 0.25:
                perspective_slot = (
                    "陌生域认知参照：允许借用自身设定里熟悉领域的事物作一次直觉类比，或直接问清楚那是什么；不懂就不装懂，不给百科教程"
                )
            else:
                perspective_slot = "陌生域收束：对不懂的事直接说不知道或问那是什么，不作猜想或比喻展开"

        # 3) Slot C：语用学话轮节奏与滑动窗防漂移（发问 / 省略号 / 句幅）
        turn_parts: list[str] = []
        recent_q_hits = sum(1 for flag in recent_q[-3:] if flag)
        max_q_allowed = 1 if q_restraint >= 0.78 else (2 if q_restraint >= 0.30 else 3)
        if recent_q_hits >= max_q_allowed and not epistemic_gap and tag != "hostile":
            turn_parts.append("近几轮已经连续发问：这一轮只给观察、反应或短句，说完就停，不在句尾抛问题")
        elif repaired and tag != "hostile":
            turn_parts.append("接住修复信号：刚才的紧绷稍微松一拍，平淡柔和收下即可，不用单字硬顶")
        elif (
            q_restraint >= 0.35
            and not epistemic_gap
            and not explicit_req
            and tag != "care_favorite"
            and not bool(raw_state.get("masteryEngaged"))
            and surgency <= 0.40
            and social_eff <= 0.40
            and effortful >= 0.60
            and tag != "hostile"
        ):
            turn_parts.append("低社交维持追问倾向：说出一条直觉感受或轻平判断即可自然收住，不负有帮对方排查或延展话题的义务")
        elif basin == "guarded" and tag == "neutral":
            turn_parts.append("仍处于微防备稳态：保持适度距离感，话不要拉长")

        if len(recent_e) >= 3:
            ell_ratio = sum(1 for flag in recent_e[-4:] if flag) / len(recent_e[-4:])
            if ell_ratio > ellipsis_budget + 0.10:
                turn_parts.append("近几轮「……」停顿略多：这一轮直接把话说出来，尽量少放或不放省略号")

        if len(recent_l) >= 2 and verbose_tighten > 0.05:
            avg_len = sum(recent_l[-3:]) / len(recent_l[-3:])
            len_cap = 60.0 - 30.0 * verbose_tighten
            if avg_len >= len_cap and (surgency <= 0.45 or cost_pick in ("brief", "state")):
                turn_parts.append("近期句子偏长，这一轮自然收短些")

        return DynamicsSnapshot(
            basin=basin,
            deflection=deflection,
            intention_hint=intention_hint,
            perspective_slot=perspective_slot,
            turn_slot="；".join(turn_parts),
        )

    def record_reply(self, chat_key: str, text: str) -> None:
        """回写最终发出给用户的句子特征，更新话轮提问频率、省略号占比与句幅滑动窗。"""
        body = str(text or "").strip()
        if not body:
            return
        clean_len = len(re.sub(r"[，。！？、；：「」『』（）…\.\!\?,\s]", "", body))
        asked = bool(_QUESTION_END_RE.search(body))
        has_ellipsis = ("……" in body) or ("..." in body)
        with self._lock:
            entry = self._slot(chat_key)
            q_hist = list(entry.get("recent_questions") or [])
            l_hist = list(entry.get("recent_lengths") or [])
            e_hist = list(entry.get("recent_ellipses") or [])
            q_hist.append(asked)
            l_hist.append(clean_len)
            e_hist.append(has_ellipsis)
            entry["recent_questions"] = q_hist[-_WINDOW_SIZE:]
            entry["recent_lengths"] = l_hist[-_WINDOW_SIZE:]
            entry["recent_ellipses"] = e_hist[-_WINDOW_SIZE:]
            self._save()
