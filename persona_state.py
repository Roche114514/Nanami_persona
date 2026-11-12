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
from dataclasses import dataclass

from . import props as props_module
from . import trust as trust_module

logger = logging.getLogger(__name__)

# 动机 → 落不到嘴上时的说法（闸门关着）
_HELD = {
    props_module.SLEEP: "他问起你困不困，你才说；不主动说自己撑不住了",
    props_module.GAME: "他问起你在做什么，你才说；不主动报自己在玩什么",
    props_module.TOGETHER: "这个念头留在心里；不主动邀他",
    props_module.WITHDRAW: "把话收回来：短，不解释，不接下一句",
}


@dataclass(frozen=True)
class TurnState:
    """这一轮她的"内心到嘴边"全貌。合成完就只有一份，不再分三块。"""

    energy: float = 0.0
    mood: str = ""
    activity: props_module.Activity = props_module.Activity()
    trust: trust_module.TrustState | None = None
    line: str = ""
    because: str = ""

    @property
    def empty(self) -> bool:
        return not self.line


def compose(
    *,
    energy: float | None,
    action: str = "",
    appraisal: str = "",
    his_text: str = "",
    trust_score: float = 0.6,
    rounds: int = 0,
    known_name: bool = False,
) -> TurnState:
    """把这一轮推一遍：评估 → 状态 → 动机 → 自我呈现闸 → 落到嘴上。

    `energy=None` 表示这一轮没有卡片（node 挂了/超时）：**整块不产出**。
    """
    if energy is None:
        return TurnState()
    # ① 评估：归属（他在说他自己？）已经在 derive_activity 里用上了
    # ② 状态 → ③ 动机
    activity = props_module.derive_activity(
        energy=energy,
        action=action,
        appraisal=appraisal,
        his_text=his_text,
    )
    if activity.empty:
        return TurnState()
    # ④ 自我呈现闸
    state = trust_module.state_of(trust=trust_score, rounds=rounds, known_name=known_name)
    # ⑤ 出口：闸开着 = 可以说；闸关着 = 动机降级成"等他问"
    gate_open = state.level != trust_module.STRANGER
    if activity.what == props_module.WITHDRAW:
        line = "这一轮你把话收回来：短，不解释，不接下一句"
        because = activity.why or "被说到了"
    elif gate_open:
        line = f"可以说：{activity.text}——他问起、或者话刚好走到这儿，你就说；不主动播报"
        because = activity.why
    else:
        line = _HELD.get(activity.what, "他问起你才说；不主动铺自己的事")
        because = f"{activity.why}；因为他还没给过你什么"
    return TurnState(
        energy=float(energy),
        mood=str(appraisal or ""),
        activity=activity,
        trust=state,
        line=line,
        because=because,
    )


def block_of(state: TurnState) -> str:
    """拼成提示词里**唯一**的那一段状态补充（卡片由上游给，这里只补"这一轮落到嘴上"）。"""
    if state is None or state.empty:
        return ""
    lines = ["【这一轮】"]
    if state.trust is not None:
        lines.append(f"- 你跟他：{state.trust.level}（{state.trust.because}）")
    why = f"（{state.because}）" if state.because else ""
    lines.append(f"- 落到嘴上：{state.line}{why}")
    return "\n".join(lines)
