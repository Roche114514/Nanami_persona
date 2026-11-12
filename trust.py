"""「她想不想说」——**读心理模型已经算好的信任度**，不自己再算一套。

2026-11-12，主人指出：她在对方连"你是谁"都没答之前，就开始报自己在玩什么。
那不是"像不像她"的问题，是**她本来就不会跟陌生人说这些**。

改写这一段时踩到一个坑，记在最前面：

> `mind.mjs` 里**本来就有** `state.trust`（初值 0.6，在 `--json` 的 `state` 里），
> 而且被 `≥0.4`、`≥0.8` 两处阈值消费（`willingToMove`／`sceneWeights`）。
> 我第一版在插件里又造了一个 0–1 的 `trust` 标量——**两个真相**，迟早互相打架。
> 现在这一层只做两件事：**读** `state.trust`，把它翻成"这一轮能说到哪一层"。

## 实测：这个构建里 `trust` 基本是常量

2026-11-12 跑了 14 轮（自报名、被骂、被道歉、被逼问、约歇一会儿…），`state.trust` **一次都没动**，
恒为 0.600。翻 `mind.mjs:updateTrust`（r75）才明白：它只在**他约她出去、成行、或者虚报**时改，
增量 ±0.05／−0.15，跟"骂一句""聊得来"无关。拿邀约/虚报两种剧本喂它，也仍然是 0.600
（`sceneMoveTarget` 那套要场景/移动真的动起来才成立）。

所以这一层的实际轴**不是 trust，是"认不认识"**：

    第 1–2 轮           → 初识（他没自报过名字，也没聊够三轮）
    第 3 轮起 / 他自报 → 有点熟
    trust ≥0.8 且知道名字 → 熟了（**纯聊天到不了**——那要几十场的邀约兑现，跟原作节奏一致）

这不是缺陷：主人要的正是"**信任的区间维持在原作支撑得住的范围内**"。
trust 那道阈值留在代码里**是预备的**——上游那根轴哪天真的动了，这一层自动跟上，不用改。

## 为什么"陌生人"不能直接看 trust

`trust` 初值 0.6，按 0.5 分档她会一上来就是"有点熟"。但 0.6 是**关系基线**（她并不怕人），
不是"我知道他是谁"。真正缺的那一格是**认识**：他叫什么、他答过她的话没有。
档位**只看一条**：他自报过名字没有（2026-11-12 主人定的化简）。

    初识   ：他还没说过自己叫什么
    有点熟 ：他自报过名字（"叫我小黑""我叫小林"都算）
    熟了   ：信任 ≥0.8 **且**知道他叫什么

为什么把"聊够三轮"删掉：那等于给档位装了个计时器——他什么都不给，三轮一到她也会松口，
七轮一被骂又缩回去，用户感觉不到"熟了"，只感觉"她某天忽然变了、又变回去了"。
用名字当唯一门槛，正好对上原典那根轴（她自己说"尤其是第一次见面的人…习惯了会快一点"），
也简单到不会被误配。

三档各自允许她说多少写在 `disclosure()` 里——**那是约束，不是台词**。
这个项目已经因为"提示词里的句子被照念"翻过三次车（`CANON-AUDIT` §Q／§S／§U）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

STRANGER = "初识"
ACQUAINTED = "有点熟"
CLOSE = "熟了"

# mind.mjs 自己的两个阈值（它用 0.4 判"愿不愿意动"，0.8 判"够不够熟"），跟着它走，别另造一套
_CLOSE_AT = 0.80


@dataclass(frozen=True)
class TrustState:
    """这一轮她对他有多少底，以及"为什么是这一档"。"""

    score: float = 0.6
    rounds: int = 0
    known_name: bool = False
    level: str = STRANGER
    because: str = ""

    def disclosure(self) -> tuple[str, ...]:
        """这一档允许她说到哪儿（**约束，不是台词**）。"""
        if self.level == CLOSE:
            return (
                "他想知道的，你会说；也可以主动说你此刻想做什么、说累说困。",
                "他要是问到你瞒着的那件事，你可以先说一半，不解释原理。",
                "家里的事仍然只是听说的那一点，而且是他先问起才说。",
            )
        if self.level == ACQUAINTED:
            return (
                "他问什么你答什么；也可以主动说一点自己的小愿望、自己在想什么。",
                "家里人还是不提；他不问，你也不铺开自己。",
                "可以问他一句，但别追着问。",
            )
        return (
            "他问什么你答什么——**不主动铺自己的事**（在做什么、困不困、想什么，他不问就不说）。",
            "不打听他的事；他先说，你才接。",
            "家里人、你自己的来历：这一档都不提。",
        )


def level_for(
    trust: float,
    *,
    rounds: int = 0,
    known_name: bool = False,
) -> tuple[str, str]:
    """(档位, 为什么是这一档)。**只有一条门槛：他自报过名字没有。**

    `rounds` / `trust` 还留着，是为了以后要细分时不用改签名（现在不参与判定）：
    主人 2026-11-12 定的化简——"自报名字之后就有点熟，自报名字之前就是不熟"。
    """
    score = float(trust if trust is not None else 0.6)
    if not known_name:
        return STRANGER, "刚认识，连他叫什么都还不知道"
    if score >= _CLOSE_AT:
        return CLOSE, "他知道你是谁，你也知道他叫什么了"
    return ACQUAINTED, "他自报过名字，你总算知道对面是谁"


def state_of(
    *,
    trust: float = 0.6,
    rounds: int = 0,
    known_name: bool = False,
) -> TrustState:
    """把心理模型给的两个量翻成这一层的状态。"""
    level, because = level_for(trust, rounds=rounds, known_name=known_name)
    return TrustState(
        score=float(trust if trust is not None else 0.6),
        rounds=int(rounds or 0),
        known_name=bool(known_name),
        level=level,
        because=because,
    )


def block_of(state: TrustState) -> str:
    """拼成提示词里那一小段：**她对他有多少底、这一轮能说到哪儿**（约束，不是台词）。"""
    if state is None:
        return ""
    lines = [f"【彼此】\n- 你跟他：{state.level}（{state.because}）"]
    lines.append("- 这一格管的是你**想不想说**：")
    lines.extend(f"  · {one}" for one in state.disclosure())
    return "\n".join(lines)
