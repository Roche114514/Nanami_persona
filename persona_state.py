"""把四层合成**一个**推演：评估 → 状态 → 动机 → 自我呈现 → 出口。

## 为什么要有这个文件（结构问题，不是优先级问题）

之前插件把它做成了**三块并列的指令**塞进提示词：

    【此刻】         卡片说"这一轮想干什么"
    【此刻·她想做的事】 props 说"她想做什么"
    【彼此】          trust 说"她能不能说"

三块同时生效，于是同一轮里出现三句互相打的话：卡片说"你这一轮想说自己的事"、
props 说"你在玩游戏"、trust 说"你不主动铺自己的事"。模型只能挑最省事那条——一声「……嗯。」。
给它加"优先级"是补丁；**真正错的是结构：这几件事不是并列的，是一个串行的心理过程。**

## 采用的结构（appraisal 理论 + 自我呈现调节 + 言语行为三个层次）

    ① 评估（appraisal） 他这句话对「我」意味着什么？——含**归属**：这是在说他，还是说我？
                        （Scherer 的 SEC 里前两关：relevance / implication；
                         Lazarus 的 appraisal 也强调"与我有关"优先于"是什么"）
    ② 状态        精力、心里的起伏（mind.mjs 已经算好，这一层只读）
    ③ 动机        她想干什么：睡 / 玩游戏 / 想一起 / 收回来（action tendency）
    ④ 自我呈现闸  这个动机**现在能不能出口、说到哪一层**（Leary 的 self-presentation：
                    自我暴露受"对方会不会拿它伤我"约束；Altman & Taylor：暴露程度随关系上升）
    ⑤ 出口        落到嘴上的形状（长短、称呼、停顿）——由另两层负责，这里只给约束

关键差别：**④ 不是和 ③ 并列的一条指令，而是对 ③ 的闸。**
所以这一层的产物只有一个：**这一轮"落到嘴上"的那一句**。
闸门关着的时候，动机本身要被**降级**，而不是"动机 + 禁令"同时摆给模型看。

## 三条纪律

1. **失败就整块消失**：卡片没算出来（node 挂了/超时）→ 这块不出现。宁可少一行，不许编一行
   （审计 §1 的原始事故：超时那轮冒出"精力 0.00，撑不住了"）。
2. **一块只说一件事**：动机 + 闸门 + 出口约束合成一段，不再分三块。
3. **写的是约束，不是台词**：这个项目因为"提示词里的句子被照念"翻过三次（`CANON-AUDIT` §Q／§S／§U）。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from . import props as props_module
from . import trust as trust_module
from .dynamics import DynamicsSnapshot

logger = logging.getLogger(__name__)

# 动机 → 自我呈现闸关闭时的克制表达（只减不加，不主动交代私事）
_HELD = {
    props_module.SLEEP: "对方问起累不累你才说；不主动播报自己多困",
    props_module.TOGETHER: "这个念头先留在心里；不主动开口邀约",
    props_module.WITHDRAW: "把话收回来：短，不解释，不接下一句",
    props_module.CURIOUS: "只在心里留意着；不急着追问或铺开讲",
    props_module.ASSERT: "稳住自己的判断；点到为止，不多争辩",
    props_module.OPEN: "对方没问起就不主动交代自己在做什么；顺着当前话题自然接一句",
    props_module.AUTONOMOUS: "由你自己的心意自然接话或起话头；不要假装手头有没忙完的固定差事",
}


@dataclass(frozen=True)
class TurnState:
    """这一轮"内心到嘴边"的心理状态汇总。"""

    energy: float = 0.0
    mood: str = ""
    activity: props_module.Activity = props_module.Activity()
    trust: trust_module.TrustState | None = None
    line: str = ""
    because: str = ""
    perspective_slot: str = ""
    turn_slot: str = ""
    basin: str = "calm"

    @property
    def empty(self) -> bool:
        return not self.line


_CARRY_HINTS = {
    "被误判": "上一轮刚被误解，余波未散，语气略收着一点、不刻意讨好",
    "非游戏域被否定": "刚才有点缩住的余波未散，说到不熟的事仍偏短",
    "睡过去了": "刚才迷糊犯困的余神未消，说话可略带慢半拍的困意",
    "被在意": "刚才被照顾到的暖意还在，眼神与态度略微柔和一点",
    "被夸了": "刚才被夸那点不知所措还没完全散，保持轻平自然",
    "关联受挫": "刚才那点空落落还在，不必硬装热情",
}


def compose(
    *,
    energy: float | None,
    action: str = "",
    appraisal: str = "",
    his_text: str = "",
    trust_score: float = 0.6,
    rounds: int = 0,
    known_name: bool = False,
    agenda: str = "",
    mood_v: float | None = None,
    need: float | None = None,
    wound: float | None = None,
    default_activity: dict[str, str] | None = None,
    activity_rules: tuple[dict, ...] | list[dict] = (),
    cost_pick: str = "",
    traits: Mapping[str, Any] | None = None,
    carry_key: str = "",
    carry_val: float = 0.0,
    explicit_request: bool = False,
    repaired: bool = False,
    dynamics: DynamicsSnapshot | None = None,
) -> TurnState:
    """把这一轮推一遍：评估 → 状态 → 心理学自然倾向 → 自我呈现闸 → 落到嘴上。

    `energy=None` 表示这一轮没有卡片（node 挂了/超时）：**整块不产出**。
    """
    if energy is None:
        return TurnState()
    # ① 评估 + ② 状态 → ③ 内在自然意愿
    activity = props_module.derive_activity(
        energy=energy,
        action=action,
        appraisal=appraisal,
        his_text=his_text,
        agenda=agenda,
        mood_v=mood_v,
        need=need,
        wound=wound,
        default_activity=default_activity,
        custom_rules=activity_rules,
    )
    if activity.empty:
        return TurnState()
    # ④ 自我呈现闸（关系档位 + L3/L1 披露与内省/表达调节）
    state = trust_module.state_of(trust=trust_score, rounds=rounds, known_name=known_name)
    t_map = dict(traits) if traits else {}
    disclosure_th = float(t_map.get("selfDisclosureThreshold", 0.8)) if t_map else 0.8
    priv_sc = float(t_map.get("privateSelfConsciousness", 0.75)) if t_map else 0.75
    pub_sc = float(t_map.get("publicSelfConsciousness", 0.2)) if t_map else 0.2
    surgency = float(t_map.get("surgency", 0.25)) if t_map else 0.25
    social_eff = float(t_map.get("socialEfficacy", 0.3)) if t_map else 0.3
    effortful = float(t_map.get("effortfulControl", 0.8)) if t_map else 0.8

    gate_open = state.level != trust_module.STRANGER
    if not gate_open and t_map and surgency >= 0.65 and disclosure_th <= 0.35:
        # 高主动外向且几乎不设防的角色模板：初识也会自然开口表露
        gate_open = True

    because_parts = [activity.why] if activity.why else []
    # ⑤ 出口：由心理倾向、披露门槛与第 9 层体验权衡共同决定
    if activity.what == props_module.WITHDRAW:
        line = "这一轮把话收回来：短，不解释，不接下一句"
        if not because_parts:
            because_parts.append("触发防御边界")
    elif not gate_open:
        held = _HELD.get(activity.what) or f"对方问起你才顺带说（{activity.text}）；不主动铺自己的事"
        line = held
        because_parts.append("尚在初识阶段")
    elif activity.what == props_module.AUTONOMOUS:
        line = "按自己此刻真实的心意决定想聊什么或做什么；不要假装手里有固定的事没做完"
    elif activity.what in (props_module.CURIOUS, props_module.ASSERT, props_module.OPEN):
        line = f"内在倾向：{activity.text}——顺着这个状态自然回应"
    else:
        line = f"可以说：{activity.text}——话走到这儿再自然带出，不刻意播报"

    # 若未触发撤回且信任尚低于自我披露门槛（高内省 + 高门槛），收敛主动自我剖白
    if t_map and gate_open and activity.what != props_module.WITHDRAW:
        if trust_score < disclosure_th and disclosure_th >= 0.6 and priv_sc >= 0.6 and surgency <= 0.45:
            line = f"{line}；心里转了很多但自我披露门槛未过，不主动摊开私事与长段自白"
            because_parts.append("自我披露门槛未跨过")
        elif pub_sc >= 0.65:
            line = f"{line}；会稍微顾及自己在对方眼里的形象与分寸"

    # 第 9 层体验净收益（cost.mjs）调节最终话幅，绝不把算术分数送进提示词
    if activity.what != props_module.WITHDRAW:
        pick = str(cost_pick or "").strip().lower()
        if repaired and pick == "brief":
            line = "对方已给出安抚或修复：负性防备缓和下来，用一句简短平和的话自然接下，不僵硬也不强行热情"
            because_parts.append("接住修复信号")
        elif pick == "brief":
            line = "这一轮能耗偏高或有所顾忌：答短一点、点到为止，不展开长篇解释"
            because_parts.append("权衡后省力简答")
        elif pick == "state":
            line = "虽然有些疲惫，但仍想接住对方这一句：简短回应之余可轻带一丝困倦或状态，不硬撑"
            because_parts.append("权衡后简答并保留在场")
        elif pick == "silent":
            line = "精力极低或此刻无话可推：极简轻应或短暂留白，不强找话接"
            because_parts.append("权衡后低耗留白")

    # 跨轮情绪余波：若本轮未发生新的剧烈评判且余波仍显著，延续情绪底色
    if activity.what != props_module.WITHDRAW and not appraisal and not repaired and float(carry_val or 0.0) >= 0.35:
        carry_hint = _CARRY_HINTS.get(str(carry_key or "").strip())
        if carry_hint:
            line = f"{line}；{carry_hint}"
            because_parts.append(f"情绪余波（{carry_key}）")

    # 合并插件级动力学拓展引擎产出的 ACT 张力、认知图式授权与语用话轮姿态
    perspective_slot = ""
    turn_slot = ""
    basin = "calm"
    if dynamics is not None:
        basin = dynamics.basin or "calm"
        if dynamics.intention_hint and activity.what != props_module.WITHDRAW:
            line = f"{line}；{dynamics.intention_hint}"
            because_parts.append("ACT稳态平衡")
        perspective_slot = dynamics.perspective_slot
        turn_slot = dynamics.turn_slot
    elif (
        t_map
        and activity.what not in (props_module.WITHDRAW, props_module.TOGETHER)
        and str(appraisal or "") not in ("胜任不足", "胜任被邀请")
        and not explicit_request
        and surgency <= 0.4
        and social_eff <= 0.4
        and effortful >= 0.6
    ):
        turn_slot = "外向与社交主动度较低：说出一句直觉观察或轻平回应即可自然停住，不必为了延续话题而排查追问对方细节"
        because_parts.append("低社交维持追问倾向")

    return TurnState(
        energy=float(energy),
        mood=str(appraisal or ""),
        activity=activity,
        trust=state,
        line=line,
        because="；".join(p for p in because_parts if p),
        perspective_slot=perspective_slot,
        turn_slot=turn_slot,
        basin=basin,
    )


def block_of(state: TurnState) -> str:
    """拼成提示词里**唯一**的那一段状态补充（三正交槽位：动机态度 / 陌生域视角 / 话轮节奏）。"""
    if state is None or state.empty:
        return ""
    lines = ["【这一轮】"]
    if state.trust is not None:
        lines.append(f"- 你跟他：{state.trust.level}（{state.trust.because}）")
    why = f"（{state.because}）" if state.because else ""
    lines.append(f"- 落到嘴上：{state.line}{why}")
    if state.perspective_slot:
        lines.append(f"- 视角与边界：{state.perspective_slot}")
    if state.turn_slot:
        lines.append(f"- 话轮节奏：{state.turn_slot}")
    return "\n".join(lines)
