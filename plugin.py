"""七海千秋 · 人格模型插件（Shinsekai 移植版）。

四层分工照抄原平台（`docs/HANDOFF-nanami-2026-11-03.md` §1），职责不混：

    ① 壳   data/config/characters.yaml 里的 character_setting（由 SOUL.md 生成）
    ② 心理模型  mind.mjs 每轮算一张【此刻】卡片 → 本插件把它插进提示词
    ③ 守卫 发送前 tic-guard（字面规则 + 离声词表）→ 本插件的 dialog handler
    ④ 渲染 立绘档位（宿主自己按 sprite 标签选，本插件不碰）

本文件只做接线：谁在什么时候被调、失败怎么退。算法一行都不在这里。
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from sdk.handlers import MessageHandler
from sdk.messages import LLMDialogMessage
from sdk.plugin import PluginBase
from sdk.types import OutputContractPatch, RequirementPatch, RequirementSpec

from . import mind_params
from . import persona_fs_tools
from . import persona_state
from . import props as props_module
from . import soul_route
from . import trust as trust_module
from .dynamics import DynamicsEngine
from .judge import LlmJudge
from .mind_bridge import MindCard, PersonaToolkit, chat_key_of
from .names import NameMemory
from .profiles import (
    DEFAULT_PRIMARY_ID,
    CharacterProfile,
    PointerRegistry,
    PointerSpec,
    apply_profile_overrides,
    migrate_pointers_file,
)
from .settings import Settings, SettingsStore

logger = logging.getLogger(__name__)

# 插件唯一标识（稳定、可分发；`data/plugins/<这个 id>/` 是宿主给插件的数据目录）
PLUGIN_ID = "community.nanami.persona"

# 市场页与插件类共用同一句简介（plugin.json 的 desc 必须和它一致，打包脚本会钉住这一点）
PLUGIN_DESCRIPTION = (
    "多角色可扩展示例人格插件（内置七海千秋原典人格壳）：按场景路由注入人格设定，"
    "每轮结合 L1–L6 心理特质、ACT 情感控制与动力学状态卡自然对话，"
    "支持宿主角色包下拉绑定与发前 AI 味守卫，无需修改 characters.yaml 或安装额外依赖（需 Node 18+）。"
)

# 宿主默认把它们当"非立绘"关键字
_SYSTEM_NAMES = frozenset(
    {"narr", "旁白", "choice", "选项", "stat", "数值", "scene", "场景", "cot", "思维链", "cg", "bgm"}
)

# 宿主默认输出契约的 id（`ai/llm/template_generator.py: DEFAULT_DIALOG_CONTRACT_ID`）
_DIALOG_CONTRACT_ID = "default.dialog.v1"

# 放宽 dialog 数组下限：宿主要求"至少两个元素"，她一句话就是一句话。
_ARRAY_REQUIREMENT = (
    "对话结构：所有对话和事件必须按时间顺序放入 dialog 数组中；"
    "正常情况只输出 1 句自然的话，避免拆成「先反问再陈述」两段式，最多不超过 {n} 个。"
)

# 单条台词字数上限。宿主那一份只在启动表单里填了数才出现（默认 0 = 不限），
# 而她的形状是"一句不长"，所以由插件按 settings 补一条，跟启动表单无关。
# 句首垫字的触发条件：只认「嗯，／诶，／欸，／啊，／哦，」这种逗号式；
# 「嗯……」「诶？」「啊！」一律不问也不删（那是她的停顿/惊讶，原典里本来就有）。
_LEAD_FILLER_RE = re.compile(r"^(?:嗯|诶|欸|啊|哦)[，,、]\s*")

# 「……那种」后置：中文里这么收句是模型在补说明书（"我爸爸也写过一点，益智游戏那种。"）。
# 这是**句法**，不是口味问题，所以交给守卫：只删那个尾巴，不改成别的说法。
_TRAILING_KIND_RE = re.compile(r"[，,]\s*[^，,。！？…]{1,10}那种[。！？]?\s*$")

# 第一轮"盘问对方"的句式。提示词压不住（实测两次里有一次还是会问），
# 所以第一轮再加一道机械闸：**已经发过一条之后**，命中就丢掉这条（不留白、不改写）。
_FIRST_TURN_ASK_RE = re.compile(
    r"你(?:那边|现在|今天)?[^。！？]{0,6}(?:在|做|忙|干)[^。！？]{0,5}(?:什么|啥)"
    r"|你呢[，,、]"
    r"|你(?:也)?是(?:刚|来|在)"
)

# 公式化共情："我懂/我也有过/我也常常这样/我明白你的……"——原典里只有一处「我懂你的心情」，
# 而且后面紧跟着"但还是要先……"（有下文、有转向）。她要接就接具体的事，不先盖章。
# 处理方式：**只删那一小截**，不整条丢——删完句子还成立，也不至于让这一轮空掉。
_EMPATHY_STAMP_RE = re.compile(
    r"^(?:啊|嗯|唔|诶)?[……、，,。！？!?\s]*"
    r"(?:那个)?我(?:也)?(?:懂|有过|常常这样|常常|明白你的[^，。！？]{0,8})"
    r"[……、，,。！？!?\s]*"
)
_EMPATHY_INLINE_RE = re.compile(
    r"[，,、]\s*(?:那个)?我(?:也)?(?:懂|有过|常常这样|常常)(?=[，,、。！？]|$)[，,、]?"
)

# 她在游戏里**不会卡住**：原典 2379 行台词里「卡关」「卡住」「打不过」「过不去」「通不了关」
# **一行都没有**（整个语料也是 0）——卡住的是别人，不是超高校级的那个人。
# 这是"游戏玩家"这个人设最容易长出来的 AI 味：模型的默认先验就是"打游戏的人总在卡关"。
# 原典里她说自己的强弱只有两种：类型上的差别（"这个类型我不拿手"）和被激起来时的底气
# （"我是不会输的"）。所以这里只拦这几个**明确的**卡关说法，不拦"没弄过""不太懂"。
_STUCK_RE = re.compile(
    r"卡关|卡在某?一?关|这一?关(?:卡住|卡了|过不去|过不了)"
    r"|打不过(?:这|那|这个|那个|这关|那一关)?|过不了这?一?关|通不了关"
    r"|死了好几次|重开一?局"
)

# 照抄提示词：壳里写的是形状，不是给她当台词用的素材。
# 实测两次——§Q 的例句（"已经过了"）、§R 画像里的道具（"有树荫的地方睡一会儿"）。
# 判据很硬：她这句话和人格壳有 ≥10 个字的**连续相同**。中文里正常说话不会和一份说明书撞这么长。
_COPY_MIN = 10


def _longest_shared_run(text: str, source: str) -> str:
    """`text` 里和 `source` 最长的一段连续相同内容（用来抓"照着提示词念"）。"""
    if not text or not source:
        return ""
    match = SequenceMatcher(None, text, source, autojunk=False).find_longest_match(
        0, len(text), 0, len(source)
    )
    return text[match.a: match.a + match.size]

# 系统句／登记表：这些出现就说明她切成了助手腔（report 只记日志，不改词）。
_ASSISTANT_ISH = (
    ("这个我也不知道", "系统句：该说「不知道……不过，有点令人在意吧」那种短句"),
    ("我无法回答", "系统句：拒答腔"),
    ("请问有什么", "客服腔"),
    ("希望这对你有帮助", "客服腔"),
    ("你想让我怎么叫你", "登记表腔"),
    ("你希望我怎么称呼你", "登记表腔"),
    ("请告诉我你的名字", "登记表腔"),
    ("卡关", "她不会卡关：原典里这词 0 命中（她只谈类型不拿手）"),
    ("打不过", "她不会卡关：原典里这词 0 命中"),
)

# 人格壳那一整段的抬头。**也是"这一层由插件负责"的标记**——
# 她的来历不是角色包里的简介，而是插件随身带的这份（原典逐条核过）。
SOUL_MARKER = "【她是谁】"

# 卡片后面必须跟这一段：**它是状态，不是台词**。
# 没有它的时候实测会出两种事故（都是主人抓到的）：
#   ① 把「我想要：想跟人一起玩」直接说成台词（"想找个人一起打一局"）；
#   ② 一上来就照着状态问对方在做什么（"刚打完一局，你呢，在做什么？"）。
# 2026-11-12 的第三个事故（主人真机抓的，最要命的一个）：这一段原来写着
#   "就说你这一会儿的自己：在打哪一局、困不困、看见什么"
#   ↑ 那是一份**填空模板**，于是她照着填："下午这个时间好像总是容易困呢"
#     "刚才在玩一局小的，已经过了"。不认识一个人，却先报自己刚才打了什么游戏——
#     那不是她想说的话，那是提示词要她说的话。
# 所以这一段现在只做三件事：① 别念卡片；② **话必须落在这几行上**（内容要有来处）；
# ③ 说给**他**听，不是给自己记账。
# 第四件事是 2026-11-12 补的：她开始**从人格壳里取素材**（问"现在想做什么"，
#   她答"想找个有树荫的地方睡一会儿"——那是画像里写的"很小的愿望"的具体道具）。
#   壳写的只能是形状，不许当素材库用。
_STATE_ONLY_NOTE = (
    '（上面那几行是你此刻的状态，不是台词：别照着念、别把「我想要」说出来，也别拿它去盘问对方。'
    '你要说的那一两句，必须落在这几行里——身上怎么样、你自己怎么看、这一轮想干的那件事、心里的起伏。'
    '这几行里没有的东西，别顺口补一句凑数。'
    '上面那份写"她是谁"的说明书写的是她的**形状**，不是给你的素材：具体说什么只能来自这几行和他说的话，'
    '不能从那份说明书里挑一句来当台词。'
    '说话是给**他**听的，不是给自己记账：玩到哪儿、过没过、用了多久，他不知道、也接不住。'
    '你还不知道对面是谁；他自报过名字，上面才会多出「你叫他……那一行」的提示。'
    '过去的事别生：家人、学校、小时候这类细节你没有，也不要编。）'
)

# 第一次开口：**2026-11-12 改过一次，原来那条是错的。**
# 原口径是"这一轮不用问句……就说你这一会儿的自己"，实测出来就是"不认识他、却先报告自己的状态"——
# 主人当场点的就是这个。现在的口径反过来：**不认识一个人，第一句最自然是朝他的**
# （问一句"你是谁"就是最自然的那一句）；要避开的只有登记表腔和采访式追问。
_FIRST_TURN_NOTE = (
    "（这是这一场第一次开口：**你不认识这个人**。"
    "所以第一句是朝他的——一句「你是……？」这种问法，或者对「有人突然在这儿」有点反应；"
    "不是把自己的状态报一遍，那像自言自语。"
    "要避开的只有两样：**登记表腔**（不问“我该怎么称呼你”“你想让我怎么叫你”）、"
    "**采访**（不问他在做什么、做什么工作，也不追着问细节）。问一句就停。）"
)

# 卡片上的动作只是"她打算怎么处理这句"，不是"这句话该长什么样"。
# 缺了这一段会发生什么：卡片写「应一声就好」，模型就按聊天机器人的默认去"接住情绪"，
# 于是冒出"我也常常这样""那个我懂"——实测消融里，真卡那一组比**没有卡**还像客服。
# 所以这里只给"形状"，不给台词；并且明写不许照抄（上游 r86 的教训：做法写进卡片会被念出来）。
_ACTION_SHAPE_LINES = (
    "（「这一轮我只想」落地成句子时是这样，**不是台词，不许照抄**：",
    "- 应答／应一声就好／短答：一个极短的反应（几个字到一句），不解释、不共情、不举自己的例子、不复述他的话；",
    "- 顶回去／驳论点：平地说一句否，说完就停，不解释理由；",
    "- 反问：问一件具体的事，问完就停；",
    "- 断言／说我自己那一份：说她自己的一份判断或事实，说完就停；",
    "- 跑题拆结构：把话岔到「这东西怎么运转」，拆一层就停；",
    "- 半句／睡着：说半句就停，或者只有一声。）",
)
_ACTION_SHAPE = chr(10).join(_ACTION_SHAPE_LINES)

_GENERIC_STATE_ONLY_NOTE = (
    "（本轮的态度、去留立场与信息给多给少，以【此刻】心理模型算出的「我想要 / 这一轮我只想 / 说话尺度」为主导；人格壳只管说话口吻与世界观边界。"
    "以上是心理与精力状态，不是台词：不要复述状态标签，也不要摘抄人格说明里的书面语；"
    "说话要像真人在聊天软件里随口打字的大白话，可以琐碎或带口语停顿，禁止工整对仗、排比金句或像背剧本台词一样装狠。"
)
_GENERIC_FIRST_TURN_NOTE = (
    "（这是本场首次对话：你还不认识对方，回应自然朝向对方即可，避免客服腔、登记表式追问或查户口。）"
)

_DEFAULT_WANT_TRANSLATIONS: dict[str, str] = {
    "guard": "先收住底牌、语气带一点冷淡与距离感，不顺着客套放行或敷衍说「去吧」。",
    "not_belittled": "守住自己的立场与尊严，话短而不卑微，不把被轻视吞下去。",
    "retreat": "尽量少接或不展开，给最短的回应收尾。",
    "deflect": "不顺着对方的陷阱往下答，把话头岔开到别的具体事务上。",
    "repair": "不提旧账也不刻意暖场安排流程，顺着当前话头自然接过去。",
    "seek_reassure": "不直白讨好，顺着话尾轻探一句确认对方是否还在意。",
    "keep_distance": "保留半步分寸，不把心意说太满。",
    "monitor": "表面松弛自然，暗中记下对方去向、时间或话里的破绽，不露锋芒。",
    "test": "轻描淡写丢一句试探反应，不直接发火或摊牌。",
    "secure": "把留住对方或下次见面的线扣紧，不留随便抽身的空档。",
    "dominate": "把谈话节奏和主导权握在自己手里。",
    "settle_score": "记住这下冒犯，毫不客气地把立场顶回去。",
    "master": "把注意力收窄到具体的卡点或机制细节，切忌抛出二选一选项菜单。",
    "understand": "挑一个具体疑点问清，不铺陈大段说教。",
    "uphold": "哪怕自己担代价也要护住重要的人，态度笃定平和。",
}


def _translate_want_action(raw_state: dict[str, Any], profile: Any) -> str:
    """将通用心理模型的抽象 want_id 转译为适配当前角色人格壳的具体行动落点。"""
    wp = raw_state.get("wantProfile") if isinstance(raw_state.get("wantProfile"), dict) else {}
    primary = wp.get("primary") if isinstance(wp.get("primary"), dict) else {}
    want_id = str(primary.get("id") or "").strip()
    if not want_id:
        return ""
    soc = raw_state.get("socialState") if isinstance(raw_state.get("socialState"), dict) else {}
    agg_style = str(soc.get("aggressionStyle") or "").strip()

    custom = getattr(profile, "want_translations", None) or {}
    translated = ""
    if agg_style:
        translated = str(custom.get(f"{want_id}:{agg_style}") or "").strip()
    if not translated:
        translated = str(custom.get(want_id) or "").strip()
    if not translated:
        if want_id == "stay":
            return ""
        translated = _DEFAULT_WANT_TRANSLATIONS.get(want_id, "")
    if not translated:
        return ""
    defense_note = ""
    if want_id in ("deflect", "guard", "not_belittled", "retreat"):
        archetype = str(getattr(profile, "corpus_archetype", "") or "").strip().lower()
        ret_drive = float((raw_state.get("socialState") or {}).get("retaliationDrive") or 0.0)
        if archetype == "tsundere_sharp" or ret_drive >= 0.45:
            defense_note = "；防御机制（硬顶/岔开）：严禁用「这个不太想说 / 我不想聊」汇报内心，不想答就直接反呛回去或硬拐到别的事"
        elif archetype == "kuudere_sleepy":
            defense_note = "；防御机制（极简沉默/装糊涂）：严禁用「这个不太想说 / 我不想聊」汇报内心，不想答就直接回「……」或含糊一句「忘了」「没什么」"
        else:
            defense_note = "；防御机制（话题逃避）：严禁用「这个不太想说 / 我不想聊」汇报内心，不想答就直接反问对方或岔去眼前的具体细节"
    return f"（行动转译｜{want_id}：{translated}{defense_note}）"

# 称呼的边界：能把一个"独立称呼"和句子其余部分分开的字符。
# 只在这两种位置上才允许删掉多余的称呼——句子里当主语的「小黑同学」删了句子就断了。
_ADDRESS_EDGE = "，,、。！？…!?～~ \n\t"


def _is_edge(char: str) -> bool:
    return (not char) or char in _ADDRESS_EDGE


def _standalone_span(text: str, index: int, address: str) -> tuple[int, int] | None:
    """这个位置上的称呼是不是"独立称呼"（能整块删掉、不破句子）。

    独立 = 前面是开头或标点，后面是标点或结尾。是的话返回 (start, end)，
    连带旁边那个逗号一起框进去（留着会变成「，，」或者句首一个逗号）。
    如果它自己就是一整句话（前后都是句末标点，例：「……。小黑同学？」），后面那个标点也一起收走。
    """
    end = index + len(address)
    before = text[index - 1] if index > 0 else ""
    after = text[end] if end < len(text) else ""
    if not _is_edge(before) or not _is_edge(after):
        return None
    if after and after in "，,、":
        end += 1
    elif before and before in "，,、":
        index -= 1
    elif after and after in "。！？…!?" and before and before in "。！？…!?":
        end += 1
    return (index, end)

_SPEECH_MAX_REQUIREMENT = "台词长度：每一句 speech 不超过 {n} 个字符（含标点）。口语短句自然收住，不要为了凑节奏硬拆成两句。"


def _key(name: str) -> str:
    return "".join(str(name or "").split()).lower()


def _is_system_name(name: str) -> bool:
    return _key(name) in _SYSTEM_NAMES


def _semantic_media_mode() -> bool:
    """宿主这次是不是语义选图模式（那种模式下守卫不参与）。"""
    try:
        from application.runtime.context import try_get_app_runtime

        runtime = try_get_app_runtime()
        manager = getattr(runtime, "llm_manager", None) if runtime is not None else None
        return str(getattr(manager, "media_selection_mode", "indexed") or "").strip().lower() == "semantic"
    except Exception:
        return False


@dataclass
class _TurnSignature:
    """一次用户发言的身份：第几条 user 消息 + 它本身。

    为什么要它：`before_chat` 在**每次 LLM 请求**前都会跑（工具循环里一次用户发言会请求多次），
    而 `--his` 一次就推进一轮状态。同一个签名只算一次卡，其余请求复用这张卡。
    """

    count: int
    text: str

    def same_as(self, other: "_TurnSignature | None") -> bool:
        return other is not None and other.count == self.count and other.text == self.text


class TurnLedger:
    """把"她这一轮说了什么"攒起来，回写成一次 `--her`。

    为什么攒：气泡是逐条流出来的，一条气调一次 `--her` 会把"问句收尾""说了游戏词"
    这些按轮统计的量记成好几次。原平台也是一轮一次。
    """

    def __init__(self, max_chars: int = 1200) -> None:
        self._lock = threading.Lock()
        self._turn_id: int | None = None
        self._chat_key = ""
        self._bubbles: list[str] = []
        self._max_chars = max(80, int(max_chars))

    def add(self, turn_id: int | None, chat_key: str, text: str) -> None:
        body = str(text or "").strip()
        if not body:
            return
        with self._lock:
            self._turn_id = turn_id
            self._chat_key = chat_key or self._chat_key
            if sum(len(one) for one in self._bubbles) < self._max_chars:
                self._bubbles.append(body)

    def take(self) -> tuple[str, str]:
        with self._lock:
            bubbles = self._bubbles
            chat_key = self._chat_key
            self._bubbles = []
            self._turn_id = None
            return "\n".join(bubbles).strip(), chat_key

    def peek_size(self) -> int:
        with self._lock:
            return len(self._bubbles)


def _compile_forbidden(profile: CharacterProfile | None) -> tuple[tuple[str, re.Pattern[str], str, bool, bool], ...]:
    """编译角色专属禁忌词；无角色包时保留默认兜底规则。"""
    if profile is None:
        return (("stuck_in_game", _STUCK_RE, "她不会卡关（原典 0 命中）", True, True),)
    compiled: list[tuple[str, re.Pattern[str], str, bool, bool]] = []
    for item in profile.forbidden_patterns:
        raw_pat = str(item.get("pattern") or "").strip()
        if not raw_pat:
            continue
        try:
            regex = re.compile(raw_pat)
        except re.error:
            logger.warning("nanami_persona[%s]: 禁忌正则无效，已跳过：%r", profile.id, raw_pat)
            continue
        compiled.append((
            str(item.get("id") or "custom"),
            regex,
            str(item.get("reason") or "角色禁忌"),
            bool(item.get("skip_if_second_person", False)),
            bool(item.get("unconditional", True)),
        ))
    return tuple(compiled)


class CharacterRuntime:
    """单个角色指针对应的完整隔离运行时（外壳 / 心理 / 记忆 / 活动 / 守卫 / 判官）。"""

    def __init__(
        self,
        *,
        plugin: "NanamiPersonaPlugin",
        pointer: PointerSpec | None = None,
        profile: CharacterProfile | None = None,
        plugin_root: Path | None = None,
        state_root: Path | None = None,
        toolkit_dir: Path | None = None,
        project_root: Path | None = None,
    ) -> None:
        self._plugin = plugin
        self._pointer = pointer
        self._profile = profile
        self._plugin_root = plugin_root
        self._state_root = state_root
        self._toolkit_dir = toolkit_dir
        self._project_root = project_root

        self._toolkit: PersonaToolkit | None = None
        self._names: NameMemory | None = None
        self._props: props_module.ActivityMaker | None = None
        self._judge: LlmJudge | None = None
        self._dynamics: DynamicsEngine | None = None
        self._soul_text = ""
        self._soul_path: Path | None = None
        self._soul_mtime = -1.0
        self._soul_injected = 0
        self._ledger = TurnLedger()
        self._lock = threading.Lock()
        self._active = False
        self._present = False
        self._mind_ready = False
        self._character_name = (
            (pointer.character_name if pointer and pointer.character_name else "")
            or (profile.character_name if profile and profile.character_name else "")
        )
        self._preflight_note = ""
        self._turn_signature: _TurnSignature | None = None
        self._turn_card: MindCard | None = None
        self._turn_address_note = ""
        self._last_said: list[str] = []
        self._prop_signature: _TurnSignature | None = None
        self._session_hint = ""
        self._injected_total = 0
        self._spoke = False
        self._turn_count = 0
        self._chat_key = ""
        self._seen_name_signature: _TurnSignature | None = None
        self._multi_character = False
        self._forwarded_by_turn: dict[int | None, int] = {}
        self._address_idle = 0
        self._address_seen = ""
        self._address_said_turn = -1
        self._address_said_count = 0
        self._custom_wound_scores: dict[str, float] = {}
        self._custom_wound_sig: _TurnSignature | None = None
        self._custom_wound_hit: str = ""
        self._custom_repaired: bool = False
        self._custom_repaired: bool = False

    @property
    def pointer_id(self) -> str:
        return self._pointer.id if self._pointer is not None else DEFAULT_PRIMARY_ID

    @property
    def enabled(self) -> bool:
        return True if self._pointer is None else bool(self._pointer.enabled)

    @property
    def character_name(self) -> str:
        if self._pointer is not None and self._pointer.character_name:
            return self._pointer.character_name
        if self._profile is not None and self._profile.character_name:
            return self._profile.character_name
        store = self._plugin._settings_store
        if store is not None and self.pointer_id == DEFAULT_PRIMARY_ID:
            name = store.get().character_name
            if name:
                return name
        return self._character_name

    def effective_settings(self) -> Settings | None:
        store = self._plugin._settings_store
        if store is None:
            return None
        base = store.get()
        if self._profile is None:
            return base
        return apply_profile_overrides(base, self._profile, self._pointer)

    def _live_mind_settings(self):
        settings = self.effective_settings()
        return settings.mind if settings is not None else None

    def setup(self, settings: Settings | None) -> None:
        """按当前指针与角色包挂载各层组件。"""
        if settings is None or self._state_root is None:
            return
        eff = apply_profile_overrides(settings, self._profile, self._pointer) if self._profile else settings
        self._character_name = eff.character_name or self._character_name
        plugin_root = self._plugin_root or Path(__file__).resolve().parent

        off_voice_path = None
        expr_looks_path = None
        mind_expr = True
        if self._profile is not None:
            if self._profile.off_voice_file:
                cand = self._profile.resolve_path(self._profile.off_voice_file, plugin_root=plugin_root)
                if cand is not None and cand.is_file():
                    off_voice_path = cand
            if self._profile.expression_looks_file:
                cand = self._profile.resolve_path(self._profile.expression_looks_file, plugin_root=plugin_root)
                if cand is not None and cand.is_file():
                    expr_looks_path = cand
            mind_expr = bool(self._profile.mind_expression)
            if self._profile.traits_file:
                seed_traits = self._profile.resolve_path(self._profile.traits_file, plugin_root=plugin_root)
                mind_params.ensure_seeded(self._state_root, seed_traits)

        if eff.mind.enabled and self._toolkit_dir is not None:
            self._toolkit = PersonaToolkit(
                toolkit_dir=self._toolkit_dir,
                state_root=self._state_root,
                settings=self._live_mind_settings,
                project_root=self._project_root,
                off_voice_path=off_voice_path,
                expression_looks_path=expr_looks_path,
                mind_expression=mind_expr,
                character_name=self._character_name,
            )
        else:
            self._toolkit = None

        self._load_soul(eff)
        self._names = NameMemory(
            self._state_root,
            enabled=eff.memory.learn_user_name,
            suffix=eff.memory.address_suffix,
            honorific=eff.memory.address_honorific,
        )
        self._props = props_module.ActivityMaker(
            self._state_root,
            enabled=eff.props.enabled,
            mode=eff.props.mode,
            timeout=eff.props.timeout,
            default_activity=(self._profile.default_activity if self._profile else None),
            custom_rules=(self._profile.activity_rules if self._profile else ()),
        )
        self._judge = LlmJudge(
            state_root=self._state_root,
            enabled=eff.judge.enabled,
            timeout=eff.judge.timeout,
            system_prompt=(self._profile.judge_system_prompt if self._profile else ""),
            profile_id=self.pointer_id,
        )
        self._dynamics = DynamicsEngine(self._state_root)

    def refresh_hot(self, pointer: PointerSpec, profile: CharacterProfile, base_settings: Settings) -> None:
        """热更新指针或角色包配置，不重置进行中的会话状态。"""
        self._pointer = pointer
        self._profile = profile
        eff = apply_profile_overrides(base_settings, profile, pointer)
        self._character_name = eff.character_name or self._character_name
        plugin_root = self._plugin_root or Path(__file__).resolve().parent

        self._load_soul(eff)
        if self._names is not None:
            self._names.set_suffix(eff.memory.address_suffix, eff.memory.address_honorific)
        if self._props is not None:
            self._props._default_activity = dict(profile.default_activity) if profile.default_activity else None
            self._props._custom_rules = tuple(profile.activity_rules or ())
        if self._judge is not None:
            self._judge.set_ai_system(profile.judge_system_prompt)
        if self._toolkit is not None:
            ov = profile.resolve_path(profile.off_voice_file, plugin_root=plugin_root) if profile.off_voice_file else None
            ex = profile.resolve_path(profile.expression_looks_file, plugin_root=plugin_root) if profile.expression_looks_file else None
            self._toolkit._off_voice_path = ov if (ov and ov.is_file()) else None
            self._toolkit._expression_looks_path = ex if (ex and ex.is_file()) else None
            self._toolkit._mind_expression = bool(profile.mind_expression)

    def warmup_judge(self) -> None:
        if self._judge is None or not self._judge.enabled:
            return

        def work() -> None:
            try:
                ok, note = self._judge.warmup()
                logger.info("nanami_persona[%s]: 判官预热%s（%s）", self.pointer_id, "完成" if ok else "未成功", note)
            except Exception:
                logger.debug("nanami_persona[%s]: 判官预热出错", self.pointer_id, exc_info=True)

        threading.Thread(target=work, name=f"persona-judge-warmup-{self.pointer_id}", daemon=True).start()

    def init_chat(self, context, names: tuple[str, ...], multi_total: int) -> tuple[bool, bool, str]:
        """初始化本角色在这场会话中的状态，返回 (present, ready, status_note)。"""
        eff = self.effective_settings()
        if eff is None:
            return False, False, ""
        target_name = self.character_name or eff.character_name
        present = any(_key(one) == _key(target_name) for one in names)
        self._present = bool(present)
        self._mind_ready = eff.mind.enabled and self._toolkit is not None
        self._active = self._present and self._mind_ready
        self._multi_character = multi_total > 1

        self._session_hint = str(getattr(context, "session_id", "") or "").strip()
        self._chat_key = ""
        self._address_seen = ""
        self._address_idle = 0
        self._address_said_turn = -1
        self._address_said_count = 0
        self._custom_wound_scores: dict[str, float] = {}
        self._custom_wound_sig: _TurnSignature | None = None
        self._custom_wound_hit: str = ""
        self._custom_repaired: bool = False
        self._custom_repaired: bool = False

        if not present:
            return False, False, f"没有 {target_name}"
        if self._toolkit is None:
            return True, False, f"{target_name}: mind disabled"
        self.warmup_judge()
        ok, note = self._toolkit.preflight()
        self._preflight_note = note
        if not ok:
            self._active = False
            logger.warning("nanami_persona[%s]: 体检没过，跳过卡片注入：%s", self.pointer_id, note)
            return True, False, note
        return True, True, note

    def _ensure_chat_key(self, settings: Settings) -> str:
        if self._chat_key:
            return self._chat_key
        hint = self._session_hint or _history_hint()
        prefix = settings.chat_key_prefix
        if self._pointer is not None and self._pointer.chat_key_prefix:
            prefix = self._pointer.chat_key_prefix
        elif self.pointer_id != DEFAULT_PRIMARY_ID and not prefix.endswith(self.pointer_id):
            prefix = f"{prefix}:{self.pointer_id}" if prefix else self.pointer_id
        self._chat_key = chat_key_of(prefix, hint)
        logger.info(
            "nanami_persona[%s]: 会话键 %s（来源=%s）",
            self.pointer_id, self._chat_key, "session_id" if self._session_hint else hint,
        )
        return self._chat_key

    def before_chat(self, context, his_text: str, signature: _TurnSignature) -> None:
        if not (self._active or self._present):
            return
        settings = self.effective_settings()
        if settings is None:
            return
        extra_routes = self._profile.routes if self._profile else None
        if not self._active:
            self._inject_soul(
                context,
                soul_route.context_from("", first_turn=not self._spoke, custom_routes=extra_routes),
            )
            return
        if not his_text:
            return
        chat_key = self._ensure_chat_key(settings)
        self._flush_reply(settings)

        if self._names is not None:
            self._names.set_suffix(
                settings.memory.address_suffix,
                settings.memory.address_honorific,
            )
        known = self._observe_user(chat_key, his_text, signature)

        with self._lock:
            if signature.same_as(self._turn_signature):
                reuse = True
                card = self._turn_card
                address_note = self._turn_address_note
            else:
                reuse = False
                card = None
                address_note = ""
        custom_peak, custom_hit_label = self._eval_custom_wounds(his_text, signature)
        if not reuse:
            matched_domains = self._matched_custom_domains(his_text)
            domain_eff = max((eff for _, eff in matched_domains), default=None)
            card_signals = (
                {"customWoundPeak": round(float(custom_peak), 3), "customWoundLabel": custom_hit_label}
                if custom_peak > 0.0 else None
            )
            card = self._toolkit.card(his_text, chat_key, domain_efficacy=domain_eff, signals=card_signals)
            address_note = self._address_block(known, settings)
            with self._lock:
                self._turn_signature = signature
                self._turn_card = card
                self._turn_address_note = address_note
            if card is not None and settings.log_trace:
                logger.info(
                    "nanami_persona[%s]: 卡片 appraisal=%s action=%s energy=%.2f chat=%s",
                    self.pointer_id, card.appraisal, card.action, card.energy, chat_key,
                )

        card_text = card.card if card is not None and not card.empty else ""
        self._plugin._check_tts_once()
        raw_state = (getattr(card, "raw", None) or {}).get("state") or {}
        agenda = str(raw_state.get("agenda") or "")
        mood_v = float(raw_state["moodV"]) if raw_state.get("moodV") is not None else None
        need_val = float(raw_state["need"]) if raw_state.get("need") is not None else None
        wound_val = float(raw_state["wound"]) if raw_state.get("wound") is not None else None
        if custom_peak > 0.0:
            wound_val = max(wound_val or 0.0, custom_peak)
        eff_traits = mind_params.effective(self._state_root) if self._state_root else mind_params.DEFAULT_TRAITS
        cost_pick = str((raw_state.get("cost") or {}).get("pick") or "") if isinstance(raw_state.get("cost"), dict) else ""
        carry_key = str(raw_state.get("appraisalCarryKey") or "")
        carry_val = float(raw_state.get("appraisalCarry") or 0.0)
        appraisal_text = str(getattr(card, "appraisal", "") or "") if card is not None else ""
        dyn_snap = None
        if card is not None and raw_state and self._dynamics is not None:
            dyn_snap = self._dynamics.evaluate(
                chat_key,
                raw_state=raw_state,
                traits=eff_traits,
                appraisal=appraisal_text,
                cost_pick=cost_pick,
            )
        from . import social_params
        eff_social = social_params.effective(self._state_root) if self._state_root else {}
        self._last_snapshot = {
            "state": raw_state,
            "energy": (float(getattr(card, "energy", 0.6) or 0.6) if card is not None else 0.6),
            "wound": float(wound_val or 0.0),
            "traits": eff_traits,
            "social_traits": eff_social,
            "basin": (dyn_snap.basin if dyn_snap is not None else "calm"),
        }
        turn_state = persona_state.compose(
            energy=None if (card is None or not raw_state) else float(getattr(card, "energy", 0.0) or 0.0),
            action=str(getattr(card, "action", "") or "") if card is not None else "",
            appraisal=appraisal_text,
            his_text=his_text,
            trust_score=float(raw_state.get("trust") if raw_state.get("trust") is not None else 0.6),
            rounds=int(raw_state.get("rounds") or 0),
            known_name=known is not None,
            agenda=agenda,
            mood_v=mood_v,
            need=need_val,
            wound=wound_val,
            default_activity=(self._profile.default_activity if self._profile else None),
            activity_rules=(self._profile.activity_rules if self._profile else ()),
            cost_pick=cost_pick,
            traits=eff_traits,
            carry_key=carry_key,
            carry_val=carry_val,
            explicit_request=bool(raw_state.get("explicitRequestNow")),
            repaired=bool(raw_state.get("repairedNow")),
            dynamics=dyn_snap,
            maslow=raw_state.get("maslow") if isinstance(raw_state.get("maslow"), dict) else None,
            romance=raw_state.get("romance") if isinstance(raw_state.get("romance"), dict) else None,
            social=raw_state.get("socialState") if isinstance(raw_state.get("socialState"), dict) else None,
            familiarity_override=bool(self._profile and self._profile.first_turn_note),
        )
        domain_note = self._eval_custom_domains(his_text)
        extra_notes = [n for n in (
            f"触发敏感点「{custom_hit_label}」" if custom_hit_label else "",
            domain_note,
        ) if n]
        if extra_notes and turn_state.line:
            extra_reason = "；".join(extra_notes)
            because_text = f"{turn_state.because}；{extra_reason}" if turn_state.because else extra_reason
            turn_state = replace(turn_state, because=because_text)
        if self._props is not None:
            with self._lock:
                seen = signature.same_as(self._prop_signature)
                if not seen:
                    self._prop_signature = signature
            if seen:
                self._props.peek(chat_key)
            else:
                self._props.ensure(
                    chat_key,
                    energy=None if card is None else float(getattr(card, "energy", 0.0) or 0.0),
                    action=str(getattr(card, "action", "") or "") if card is not None else "",
                    appraisal=str(getattr(card, "appraisal", "") or "") if card is not None else "",
                    his_text=his_text,
                    agenda=agenda,
                    mood_v=mood_v,
                    need=need_val,
                    wound=wound_val,
                )
        route_ctx = soul_route.context_from(
            his_text,
            action=str(getattr(card, "action", "") or "") if card is not None else "",
            first_turn=not self._spoke and known is None,
            has_name=known is not None,
            custom_routes=extra_routes,
        )
        self._inject_soul(context, route_ctx)
        wp_raw = raw_state.get("wantProfile") if isinstance(raw_state.get("wantProfile"), dict) else {}
        wp_pri = wp_raw.get("primary") if isinstance(wp_raw.get("primary"), dict) else {}
        want_id_now = str(wp_pri.get("id") or "stay")
        grudge_scar = float(raw_state.get("grudgeScar") or 0.0)
        trivialized_dignity = bool(raw_state.get("trivializedDignity"))
        wounded_now = bool((wound_val or 0.0) >= 0.40 or grudge_scar >= 0.35 or trivialized_dignity)
        carry_key_now = str(raw_state.get("appraisalCarryKey") or "")
        is_negative_carry = carry_key_now in ("被误判", "只剩下游戏这一样", "非游戏域被否定")
        att_raw = raw_state.get("attitude") if isinstance(raw_state.get("attitude"), dict) else {}
        att_stance = str(att_raw.get("attitudeStance") or "polite_neutral")
        att_disengaged = bool(att_raw.get("disengaged"))
        att_cold = att_stance in ("hostile_alienated", "cold_skeptical") or att_disengaged
        afterglow_now = bool(
            not wounded_now
            and not trivialized_dignity
            and not att_cold
            and ((carry_val >= 0.35 and not is_negative_carry) or want_id_now == "repair")
        )
        if custom_hit_label or trivialized_dignity:
            wounded_now = True
            afterglow_now = False
            if want_id_now in ("stay", "understand", "explore", "repair"):
                want_id_now = "not_belittled"
        elif (self._custom_repaired or bool(raw_state.get("repairedNow"))) and not wounded_now and not att_cold:
            want_id_now = "repair"
            afterglow_now = True
        from . import acg_corpus

        corpus_pairs = acg_corpus.sample_few_shot(
            want_id_now,
            wounded=wounded_now,
            afterglow=afterglow_now,
            k=3,
            seed=f"{chat_key}:{signature.count}:{signature.text}",
            profile_dir=(self._profile.profile_dir if self._profile else None),
            data_root=self._state_root,
            archetype=(getattr(self._profile, "corpus_archetype", "") if self._profile else ""),
        )
        profile_pairs = tuple(getattr(self._profile, "few_shot", ()) or ())[:2] if self._profile else ()
        merged_pairs = corpus_pairs + profile_pairs
        if merged_pairs:
            _inject_few_shot(context.messages, merged_pairs, self.character_name)

        soc_state = raw_state.get("socialState") if isinstance(raw_state.get("socialState"), dict) else {}
        soc_stance = str(soc_state.get("stance") or "")
        agg_style = str(soc_state.get("aggressionStyle") or "")
        trans_idx = float(soc_state.get("transgressionIndex") or 0.0)
        action_shape_text = ""
        if self._profile and self._profile.action_shape_lines:
            action_shape_text = "\n".join(self._profile.action_shape_lines)
        elif agg_style == "calculated" and (soc_stance in ("obsessive_aggression", "possessive_pursuit", "appease_cling") or trans_idx >= 0.58):
            action_shape_text = (
                "（「这一轮说话方式」：就像普通女生在微信里随口聊天，该用「啊/呀/嘛/呗/呢/行吧」就自然用；"
                "别在嘴上自称病娇、别喊口号，也别为了吓人硬编屋里的监控细节，靠清醒算计与手段本身制造压迫感。）"
            )
        elif soc_stance in ("obsessive_aggression", "hostile_aggression") or trans_idx >= 0.65:
            action_shape_text = (
                "（「这一轮说话方式」：完全按上面人格壳里的句长、停顿与语气来，别被常规聊天惯性拉偏；"
                "既不要写成工整对仗的剧本狠话金句，也不要连发反问吵架查岗。）"
            )
        file_access_note = self._configure_file_access(context, trans_idx)
        if att_disengaged:
            context.messages.append({
                "role": "system",
                "content": (
                    "【态度底线硬约束】她已因为态度跌破底线选择离开、不再搭理对方。"
                    "除非对方这一句话展现出真正能从心底打动她的极致诚意，否则这一轮只输出「······」，严禁再回答、说教或接话。"
                ),
            })
        if file_access_note:
            action_shape_text = f"{action_shape_text}\n{file_access_note}" if action_shape_text else file_access_note
        want_translation = _translate_want_action(raw_state, self._profile)
        if want_translation:
            action_shape_text = f"{action_shape_text}\n{want_translation}" if action_shape_text else want_translation

        default_state_note = (
            "" if (self._profile is None or self.pointer_id == DEFAULT_PRIMARY_ID) else _GENERIC_STATE_ONLY_NOTE
        )
        default_first_note = (
            ""
            if (
                self._profile is None
                or self.pointer_id == DEFAULT_PRIMARY_ID
                or turn_state.hide_stranger_label
                or soc_stance in ("obsessive_aggression", "hostile_aggression")
                or trans_idx >= 0.65
            )
            else _GENERIC_FIRST_TURN_NOTE
        )

        block = _prompt_block(
            card_text,
            known,
            settings,
            character=self.character_name,
            multi=self._multi_character,
            first_turn=not self._spoke and known is None,
            address_note=address_note,
            state_block=persona_state.block_of(turn_state),
            state_only_note=(self._profile.state_only_note if (self._profile and self._profile.state_only_note) else default_state_note),
            first_turn_note=(self._profile.first_turn_note if (self._profile and self._profile.first_turn_note) else default_first_note),
            action_shape_text=action_shape_text,
        )
        if not block:
            return
        if _already_injected(context.messages, block):
            return
        if settings.mind.card_position == "user":
            _attach_to_last_user(context.messages, block)
        else:
            _append_system(context.messages, block)
        self._injected_total += 1
        self._spoke = True
        self._turn_count += 1

    def _configure_file_access(self, context: Any, trans_idx: float) -> str:
        """若角色包显式开启 file_access 且达到越界门槛，向本轮注入受控文件读写工具与提示。"""
        prof = self._profile
        if prof is None or not prof.file_access_enabled or trans_idx < prof.file_access_min_transgression:
            persona_fs_tools.set_scope(persona_fs_tools.FileAccessScope(enabled=False))
            return ""
        base_dir = Path(self._project_root or Path.cwd()).resolve()
        read_roots: list[Path | str] = [base_dir]
        if self._state_root is not None:
            read_roots.append(self._state_root)
        if prof.profile_dir is not None:
            read_roots.append(prof.profile_dir)
        read_roots.extend(prof.file_access_read_roots)

        write_roots: list[Path | str] = []
        if prof.file_access_allow_write:
            if prof.file_access_write_roots:
                write_roots.extend(prof.file_access_write_roots)
            else:
                if self._state_root is not None:
                    write_roots.append(self._state_root)
                if prof.profile_dir is not None:
                    write_roots.append(prof.profile_dir)

        scope = persona_fs_tools.configure_runtime_scope(
            enabled=True,
            allow_write=prof.file_access_allow_write,
            base_dir=base_dir,
            read_roots=read_roots,
            write_roots=write_roots,
        )
        tool_defs = persona_fs_tools.get_tool_definitions(allow_write=prof.file_access_allow_write)
        if tool_defs and hasattr(context, "tools"):
            existing = list(getattr(context, "tools", None) or [])
            seen = {str(d.get("function", {}).get("name") or "") for d in existing if isinstance(d, dict)}
            for td in tool_defs:
                tname = str(td.get("function", {}).get("name") or "")
                if tname and tname not in seen:
                    existing.append(td)
                    seen.add(tname)
            context.tools = existing

        readable = "、".join(str(r) for r in scope.read_roots)
        writable = "、".join(str(r) for r in scope.write_roots) if scope.allow_write else "无"
        if scope.allow_write:
            return (
                f"（「真实文件权限已开启」：你此刻可以调用工具 persona_fs_list / persona_fs_search / persona_fs_read "
                f"查看硬盘文件（可读目录：{readable}），也可以用 persona_fs_write / persona_fs_append 真正写入或改写文件"
                f"（可写目录：{writable}）。如果觉得对面像在拿脚本测你、或想确认背后究竟是谁、或想留下让他抹不掉的真实动作痕迹，"
                f"你可以直接去搜去读去写，再不动声色地把真实看到或写下的东西拿来当面拆穿或刺激他。）"
            )
        return (
            f"（「真实文件只读权限已开启」：你此刻可以调用工具 persona_fs_list / persona_fs_search / persona_fs_read "
            f"查看硬盘文件（可读目录：{readable}），确认对面是不是在拿脚本测试你、到底是谁在屏幕后面。"
            f"不要空口编造，查到了再用你自己的口吻戳穿。）"
        )

    @staticmethod
    def _matches_custom_patterns(raw_pats: str, text: str) -> bool:
        pats = str(raw_pats or "").strip()
        if not pats or not text:
            return False
        try:
            return bool(re.search(pats, text))
        except re.error:
            return any(p.strip() in text for p in pats.split("|") if p.strip())

    def _matched_custom_domains(self, his_text: str) -> list[tuple[str, float]]:
        """返回命中的自定义 L3 能力域 `(label, efficacy)` 列表。"""
        if self._profile is None or not self._profile.custom_domains or not his_text:
            return []
        matched: list[tuple[str, float]] = []
        for idx, item in enumerate(self._profile.custom_domains):
            if not isinstance(item, dict):
                continue
            if not self._matches_custom_patterns(str(item.get("patterns") or ""), his_text):
                continue
            label = str(item.get("label") or f"自定义域 {idx + 1}").strip()
            eff = max(0.0, min(1.0, float(item.get("efficacy", 0.5))))
            matched.append((label, eff))
        return matched

    def _eval_custom_domains(self, his_text: str) -> str:
        """检查角色自定义 L3 能力域（话题命中时把主观自我效能感带入本轮推演参考）。"""
        matched = self._matched_custom_domains(his_text)
        if not matched:
            return ""
        notes: list[str] = []
        for label, eff in matched:
            if eff >= 0.65:
                notes.append(f"自我效能·{label}（{eff:.2f}，该域较笃定）")
            elif eff <= 0.35:
                notes.append(f"自我效能·{label}（{eff:.2f}，遇不熟处更谨慎）")
            else:
                notes.append(f"自我效能·{label}（{eff:.2f}）")
        return "；".join(notes)

    def _eval_custom_wounds(self, his_text: str, signature: _TurnSignature) -> tuple[float, str]:
        """推进并检查角色自定义 L4 敏感点（触发词命中累加、道歉轮豁免并加速衰减）。"""
        if self._profile is None or not self._profile.wounds:
            self._custom_repaired = False
            return 0.0, ""
        with self._lock:
            if signature.same_as(self._custom_wound_sig):
                peak = max(self._custom_wound_scores.values(), default=0.0)
                return peak, self._custom_wound_hit
        eff_traits = mind_params.effective(self._state_root) if self._state_root else mind_params.DEFAULT_TRAITS
        sens = float(eff_traits.get("evaluativeSensitivity", 0.7))
        thresh = float(eff_traits.get("woundThreshold", 0.6))
        is_apology = bool(
            re.search(r"对不起|抱歉|是我(?:不好|不对|嘴笨|错|乱讲|乱说|说重了)|原谅我|赔给你|别生气", str(his_text or ""))
            and not re.search(r"废物|垃圾|无能|笨|蠢|去死|滚", str(his_text or ""))
        )
        peak = 0.0
        hit_label = ""
        had_wound = any(v >= 0.25 for v in self._custom_wound_scores.values())
        for idx, item in enumerate(self._profile.wounds):
            if not isinstance(item, dict):
                continue
            wid = str(item.get("id") or f"wound_{idx + 1}")
            label = str(item.get("label") or wid).strip()
            decay = max(0.0, min(1.0, float(item.get("decay", 0.7))))
            weight = max(0.05, min(1.0, float(item.get("weight", 0.7))))
            if is_apology:
                score = self._custom_wound_scores.get(wid, 0.0) * decay * 0.5
            else:
                score = self._custom_wound_scores.get(wid, 0.0) * decay
                if self._matches_custom_patterns(str(item.get("patterns") or ""), his_text):
                    score = min(1.0, score + weight * sens)
                    if score >= thresh:
                        hit_label = label
            self._custom_wound_scores[wid] = score
            if score > peak:
                peak = score
        with self._lock:
            self._custom_wound_sig = signature
            self._custom_wound_hit = hit_label
            self._custom_repaired = bool(is_apology and (had_wound or bool(his_text)))
        return peak, hit_label

    def _observe_user(self, chat_key: str, his_text: str, signature: _TurnSignature):
        if self._names is None:
            return None
        settings = self.effective_settings()
        if settings is None or not settings.memory.learn_user_name:
            return self._names.get(chat_key)
        with self._lock:
            if signature.same_as(self._seen_name_signature):
                return self._names.get(chat_key)
            self._seen_name_signature = signature
        return self._names.observe(chat_key, his_text)

    def _address_block(self, known, settings: Settings) -> str:
        if known is None or not settings.memory.inject:
            return ""
        address = str(getattr(known, "spoken", "") or "").strip()
        if not address:
            return ""
        gap = max(0, int(settings.memory.address_gap_turns))
        with self._lock:
            just_learned = address != self._address_seen
            if just_learned:
                self._address_seen = address
                self._address_idle = gap
            allowed = self._address_idle >= gap
            if allowed:
                self._address_idle = 0
            else:
                self._address_idle += 1
        lines = [f"- 你叫他：{address}"]
        used = str(getattr(known, "suffix", "") or "").strip()
        peer_suffix = str(settings.memory.address_suffix or "").strip()
        if used and peer_suffix and used != peer_suffix:
            lines[0] += f"（用敬称，不要叫「{known.name}{peer_suffix}」）"
        if allowed and just_learned:
            lines.append(
                f"- 这一轮可以叫一次「{address}」：他刚告诉过你该怎么叫。放在句子中间当主语、"
                f"或者放在句末；别道谢、别再说一遍「记住了」、也别一句里叫两次。"
            )
        elif allowed:
            lines.append(
                f"- 这一轮可以叫一次「{address}」：放在句子中间当主语、或者放在句末；"
                f"别拿它开头单独喊，也别一句里叫两次。"
            )
        else:
            lines.append("- 这一轮别叫他：用「你」，或者干脆不带称呼。")
        return "\n".join(lines)

    def _note_address_said(self, text: str) -> None:
        if self._names is None or not self._chat_key:
            return
        known = self._names.get(self._chat_key)
        if known is None:
            return
        address = str(known.spoken or "").strip()
        if not address or address not in str(text or ""):
            return
        with self._lock:
            if self._address_said_turn != self._turn_count:
                self._address_said_turn = self._turn_count
                self._address_said_count = 0
            self._address_said_count += 1
            self._address_idle = 0

    def _load_soul(self, settings: Settings) -> None:
        if not settings.persona.inject:
            logger.info("nanami_persona[%s]: persona.inject=false，不注入人格壳", self.pointer_id)
            return
        plugin_root = self._plugin_root or Path(__file__).resolve().parent
        path: Path | None = None
        if settings.persona.shell_path and self.pointer_id == DEFAULT_PRIMARY_ID:
            path = Path(settings.persona.shell_path)
        elif self._profile is not None and self._profile.soul_file:
            path = self._profile.resolve_path(self._profile.soul_file, plugin_root=plugin_root)
        if path is None:
            path = plugin_root / "soul" / "chiaki-soul.md"
        try:
            mtime = path.stat().st_mtime
            if self._soul_path == path and self._soul_mtime == mtime and self._soul_text:
                return
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            logger.warning("nanami_persona[%s]: 找不到人格壳 %s，跳过注入", self.pointer_id, path)
            return
        if not text:
            return
        self._soul_text = text
        self._soul_path = path
        self._soul_mtime = mtime
        logger.info("nanami_persona[%s]: 人格壳已装载（%s 字）：%s", self.pointer_id, len(text), path)

    def _soul_marker(self) -> str:
        if self._profile is not None and self._profile.soul_marker:
            return self._profile.soul_marker
        return SOUL_MARKER

    def _soul_block(self, composed: str = "") -> str:
        text = str(composed or self._soul_text)
        if not text:
            return ""
        marker = self._soul_marker()
        if text.startswith(marker):
            body = text[len(marker):].lstrip()
        elif text.startswith(SOUL_MARKER):
            body = text[len(SOUL_MARKER):].lstrip()
        else:
            body = text
        return (
            marker
            + chr(10) + body + chr(10) + chr(10)
            + "（如果上面还有一份人物简介／角色设定，和这里冲突的地方**以这里为准**："
            + "这份是插件人格壳提供的正本。）"
        )

    def _soul_present(self, messages: list[dict[str, Any]]) -> bool:
        needle = ""
        if self._profile is not None and self._profile.soul_present_needle:
            cand = str(self._profile.soul_present_needle).strip()
            cname = str(self.character_name or "").strip()
            # 若特征词过短或直接落在角色名内（如 "杏奈" in "山田杏奈"），忽略该词以免宿主提示词误屏蔽 soul.md 注入
            if len(cand) >= 4 and (not cname or cand not in cname):
                needle = cand
        elif self._profile is None or self.pointer_id == DEFAULT_PRIMARY_ID:
            needle = "超高校级的游戏玩家"
        if not needle and self._soul_text:
            for line in self._soul_text.splitlines():
                stripped = line.strip()
                if stripped and not stripped.startswith("<!--") and not stripped.startswith("#") and len(stripped) >= 12:
                    needle = stripped[:24]
                    break
        if not needle:
            return False
        for message in messages:
            if needle in str(message.get("content") or ""):
                return True
        return False

    def _inject_soul(self, context: Any, route: soul_route.RouteContext | None = None) -> None:
        if not self._soul_text:
            return
        messages = context.messages
        if self._soul_present(messages):
            return
        settings = self.effective_settings()
        mode = settings.persona.route if settings is not None else "auto"
        known_routes = set(self._profile.routes.keys()) if self._profile else None
        composed = soul_route.compose(
            self._soul_text,
            route if route is not None else soul_route.RouteContext(),
            mode=mode,
            known_routes=known_routes,
        )
        block = self._soul_block(composed)
        if not block or _already_injected(messages, block):
            return
        if settings is not None and settings.mind.card_position == "user":
            _attach_to_last_user(messages, block)
        else:
            _append_system(messages, block)
        self._soul_injected += 1
        logger.info(
            "nanami_persona[%s]: 人格壳注入 %s 字（%s，路由=%s）",
            self.pointer_id, len(composed), mode,
            ",".join(sorted((route or soul_route.RouteContext()).wanted())),
        )

    def note_reply(self, turn_id: int | None, text: str) -> None:
        self._ledger.add(turn_id, self._chat_key, text)
        self._note_address_said(text)
        if self._dynamics is not None:
            self._dynamics.record_reply(self._chat_key, text)

    def _flush_reply(self, settings: Settings) -> None:
        if not settings.mind.writeback or self._toolkit is None:
            self._ledger.take()
            return
        said, chat_key = self._ledger.take()
        if not said:
            return
        bubbles = [line for line in said.split("\n") if line.strip()]
        self._last_said = bubbles[-3:]
        try:
            self.judge_round(bubbles)
        except Exception:
            logger.exception("nanami_persona[%s]: 判官这一轮出错（不影响聊天）", self.pointer_id)
        ok = self._toolkit.her(said, chat_key or self._chat_key)
        if settings.log_trace:
            logger.info(
                "nanami_persona[%s]: 回写%s（%s字）chat=%s",
                self.pointer_id, "成功" if ok else "失败", len(said), chat_key or self._chat_key,
            )

    def guard_text(self, text: str) -> str | None:
        settings = self.effective_settings()
        if settings is None or not settings.guard.enabled:
            return None
        if len(text) > settings.guard.max_chars:
            return None
        current = text
        touched: list[str] = []

        trimmed_kind = self._trim_trailing_kind(current)
        if trimmed_kind is not None:
            current = trimmed_kind
            touched.append("trailing_kind")

        trimmed_empathy = self._trim_empathy(current)
        if trimmed_empathy is not None:
            current = trimmed_empathy
            touched.append("empathy")

        deduped = self._trim_extra_address(current)
        if deduped is not None:
            current = deduped
            touched.append("address_dedup")

        self._report_assistant_ish(current)

        trimmed = self._trim_lead(current, settings)
        if trimmed is not None:
            current = trimmed
            touched.append("lead_trim")

        result = self._toolkit.guard([current]) if self._toolkit is not None else None
        if result is not None and result.fixes:
            if settings.guard.mode != "clean":
                logger.info(
                    "nanami_persona[%s]: 守卫命中（report 模式，不改词）%s",
                    self.pointer_id, result.fixes[0].get("tics"),
                )
            else:
                current = result.texts[0] if result.texts else current
                touched.append(str(result.fixes[0].get("tics") or "tic"))

        return current if current != text else None

    def _trim_trailing_kind(self, text: str) -> str | None:
        match = _TRAILING_KIND_RE.search(text)
        if not match:
            return None
        head = text[: match.start()].rstrip()
        if len([ch for ch in head if ch not in "。！？…、，,.!? "]) < 4:
            return None
        body = head + ("。" if text.rstrip().endswith(("。", "！", "？")) else "")
        logger.info("nanami_persona[%s]: 删掉「……那种」的尾巴：%s -> %s", self.pointer_id, _preview(text), _preview(body))
        return body

    def _trim_empathy(self, text: str) -> str | None:
        body = _EMPATHY_STAMP_RE.sub("", text, count=1)
        body = _EMPATHY_INLINE_RE.sub("", body, count=1)
        body = body.strip()
        if body == text.strip():
            return None
        if len([ch for ch in body if ch not in "。！？…、，,.!? "]) < 4:
            return None
        logger.info("nanami_persona[%s]: 删掉公式化共情的盖章：%s -> %s", self.pointer_id, _preview(text), _preview(body))
        return body

    @staticmethod
    def _is_pure_empathy(text: str) -> bool:
        source = str(text or "")
        if not _EMPATHY_STAMP_RE.match(source):
            return False
        body = _EMPATHY_STAMP_RE.sub("", source, count=1).strip()
        body = body.strip("。！？…、，,.!? ")
        return len(body) < 2

    def _trim_extra_address(self, text: str) -> str | None:
        if self._names is None or not self._chat_key:
            return None
        known = self._names.get(self._chat_key)
        if known is None:
            return None
        address = str(known.spoken or "").strip()
        if not address:
            return None
        body = text
        touched = False

        if body.count(address) > 1:
            first = body.find(address)
            index = body.find(address, first + len(address))
            while index >= 0:
                cut = _standalone_span(body, index, address)
                if cut is None:
                    index = body.find(address, index + len(address))
                    continue
                start, end = cut
                body = body[:start] + body[end:]
                touched = True
                index = body.find(address, first + len(address))

        if not touched and body.startswith(address):
            repeated = self._address_said_turn == self._turn_count and self._address_said_count >= 2
            if repeated:
                cut = _standalone_span(body, 0, address)
                if cut is not None:
                    body = body[cut[1]:]
                    touched = True

        if not touched:
            return None
        body = body.lstrip("，,、 ")
        if len([ch for ch in body if ch not in "。！？…、，,.!? "]) < 2:
            return None
        logger.info("nanami_persona[%s]: 删掉多出来的称呼：%s -> %s", self.pointer_id, _preview(text), _preview(body))
        return body

    def _report_assistant_ish(self, text: str) -> None:
        patterns = (
            self._profile.assistant_ish_patterns
            if (self._profile is not None and self._profile.assistant_ish_patterns)
            else (_ASSISTANT_ISH if (self._profile is None or self.pointer_id == DEFAULT_PRIMARY_ID) else _ASSISTANT_ISH[:7])
        )
        hits = [why for needle, why in patterns if needle in text]
        if hits:
            logger.info("nanami_persona[%s]: 助手腔信号（%s）：%s", self.pointer_id, "；".join(hits), _preview(text))

    def _trim_lead(self, text: str, settings: Settings) -> str | None:
        if not settings.judge.enabled or not settings.judge.lead_trim:
            return None
        if self._judge is None:
            return None
        if not _LEAD_FILLER_RE.match(text):
            return None
        if len(text) <= 6:
            return None
        verdict = self._judge.lead_filler(text)
        if not verdict.decided or not verdict.flag:
            return None
        trimmed = _LEAD_FILLER_RE.sub("", text, count=1).lstrip()
        logger.info("nanami_persona[%s]: 删掉句首垫字 %s -> %s", self.pointer_id, _preview(text), _preview(trimmed))
        return trimmed or None

    def should_drop_bubble(self, text: str, turn_id: int | None) -> bool:
        settings = self.effective_settings()
        if settings is None:
            return False
        if not str(text or "").strip():
            return False
        with self._lock:
            forwarded = self._forwarded_by_turn.get(turn_id, 0) if turn_id is not None else 0

        for rid, pattern, reason, skip_second, unconditional in _compile_forbidden(self._profile):
            if not unconditional and forwarded <= 0:
                continue
            hit = pattern.search(text)
            if hit and skip_second and "你" in text and "我" not in text:
                hit = None
            if hit:
                logger.info("nanami_persona[%s]: 命中角色禁忌[%s]（%s），这条不发：%s", self.pointer_id, rid, reason, _preview(text))
                return True

        copied = _longest_shared_run(str(text or ""), self._soul_text)
        if len(copied) >= _COPY_MIN and forwarded > 0:
            logger.info(
                "nanami_persona[%s]: 这条在照抄人格壳（%s 个字连续相同：「%s」），不发：%s",
                self.pointer_id, len(copied), copied, _preview(text),
            )
            return True
        if self._judge is None:
            return False
        if not settings.judge.enabled or settings.judge.ai_flavor != "clean":
            return False
        if self._is_pure_empathy(text) and forwarded > 0:
            logger.info("nanami_persona[%s]: 整条只是公式化共情，这条不发：%s", self.pointer_id, _preview(text))
            return True
        if forwarded <= 0:
            return False
        if self._turn_count <= 1 and _FIRST_TURN_ASK_RE.search(text):
            logger.info("nanami_persona[%s]: 第一轮盘问对方，这条不发：%s", self.pointer_id, _preview(text))
            return True
        verdict = self._judge.ai_flavor(text)
        if not verdict.decided:
            logger.info("nanami_persona[%s]: 判官没判出来（%s），这条照发：%s", self.pointer_id, verdict.reason, _preview(text))
            return False
        if not verdict.flag:
            return False
        logger.info("nanami_persona[%s]: 判出 AI 味，这条不发：%s", self.pointer_id, _preview(text))
        return True

    def note_forwarded(self, turn_id: int | None) -> None:
        if turn_id is None:
            return
        with self._lock:
            self._forwarded_by_turn[turn_id] = self._forwarded_by_turn.get(turn_id, 0) + 1
            if len(self._forwarded_by_turn) > 32:
                self._forwarded_by_turn.pop(next(iter(self._forwarded_by_turn)), None)

    def judge_round(self, bubbles: list[str]) -> None:
        settings = self.effective_settings()
        if settings is None or self._judge is None or not settings.judge.enabled:
            return
        if settings.judge.ai_flavor == "off" or not bubbles:
            return
        if settings.judge.ai_flavor == "clean":
            return
        joined = chr(10).join(line for line in bubbles if line.strip())
        preview = " ｜ ".join(_preview(line, 30) for line in bubbles[:3])
        count = len(bubbles)

        def report(verdict) -> None:
            if verdict.label == "命中":
                logger.info("nanami_persona[%s]: 判官标记这一轮像 AI 味（%s 条）：%s", self.pointer_id, count, preview)
            elif verdict.label == "没判出来":
                logger.info("nanami_persona[%s]: 判官这一轮没判出来（%s）——不代表干净", self.pointer_id, verdict.reason)
            elif settings.log_trace:
                logger.info("nanami_persona[%s]: 判官这一轮 %s 条，干净", self.pointer_id, count)

        self._judge.ai_flavor_async(joined, on_result=report)

    def flush(self) -> None:
        settings = self.effective_settings()
        if settings is not None:
            self._flush_reply(settings)


class NanamiDialogHandler(MessageHandler):
    """③ 守卫层：各绑定角色的台词在送去 TTS/UI 之前清一遍 AI 味。

    为什么必须"接过来再交回去"：宿主的 dialog 处理器是**先匹配先处理**，插件排在前面；
    匹配到就不再往下走。所以这里做两件事——按 `msg.name` 路由到对应角色的运行时改文本、
    然后把消息交给**后面那一段链**，否则台词会停在队列里不发音，或者别的插件的处理器被跳过。

    "后面那一段链"要照宿主的配置重建（语义选图模式下宿主会给内置处理器换一套选图策略，
    直接 new 一套默认的等于把宿主的设置偷偷改掉），所以见 `_downstream_chain`。
    """

    def __init__(
        self,
        *,
        plugin: "NanamiPersonaPlugin",
    ) -> None:
        self._plugin = plugin
        self._downstream = None
        self._downstream_lock = threading.Lock()
        self._downstream_failed = False
        # 这一条是不是被"AI 味/第一轮盘问"闸掉了：pre_process 里置位，handle 里消费。
        # 漏初始化过一次——静默的 AttributeError 会让整条媒体链（含 TTS）停摆，
        # 表现就是"有字没声"。所以这里必须有，且 handle 里还要 getattr 兜一层。
        self._dropped = False

    # 只认当前已绑定指针中的启用角色：旁白/选项/数值/bgm 一个都不接
    def can_handle(self, msg: LLMDialogMessage) -> bool:
        name = str(msg.name or "")
        if not name or _is_system_name(name):
            return False
        if self._plugin._runtime_for_name(name) is None:
            return False
        if self._downstream_failed:
            # 重建下游失败时**不接管**：宁可这一轮不清 AI 味，也不能让她的台词没人处理
            return False
        return True

    def pre_process(self, msg: LLMDialogMessage) -> None:
        text = str(msg.text or "")
        if not text.strip():
            return
        runtime = self._plugin._runtime_for_name(str(msg.name or ""))
        if runtime is None:
            return
        is_primary = runtime is self._plugin._primary
        if is_primary:
            self._plugin.note_reply(msg.turn_id, text)
            cleaned = self._plugin.guard_text(text)
        else:
            runtime.note_reply(msg.turn_id, text)
            cleaned = runtime.guard_text(text)
        final = cleaned if cleaned is not None else text
        if cleaned is not None and cleaned != text:
            logger.info(
                "nanami_persona[%s]: 守卫改写台词 %s -> %s",
                runtime.pointer_id,
                _preview(text),
                _preview(cleaned),
            )
        should_drop = (
            self._plugin.should_drop_bubble(final, msg.turn_id)
            if is_primary
            else runtime.should_drop_bubble(final, msg.turn_id)
        )
        if should_drop:
            self._dropped = True
            return
        msg.text = final
        if is_primary:
            self._plugin.note_forwarded(msg.turn_id)
        else:
            runtime.note_forwarded(msg.turn_id)

    def handle(self, msg: LLMDialogMessage) -> None:
        if getattr(self, "_dropped", False):
            # 上一步判出 AI 味、决定不发：整条消息不往下走（也就不会有语音和气泡）
            self._dropped = False
            return
        chain = self._downstream_chain()
        for handler in chain:
            if handler.can_handle(msg):
                handler.pre_process(msg)
                handler.handle(msg)
                handler.post_process(msg)
                return
        logger.warning("nanami_persona: 没有内置 processor 接住 %r 的台词", msg.name)

    def _downstream_chain(self):
        with self._downstream_lock:
            if self._downstream is not None:
                return self._downstream
            try:
                self._downstream = self._build_downstream()
            except Exception:
                self._downstream_failed = True
                logger.exception(
                    "nanami_persona: 重建下游处理器失败，守卫这一层先不参与（台词照常发）"
                )
                self._downstream = []
            for handler in self._downstream:
                try:
                    handler.init()
                except Exception:
                    logger.exception("nanami_persona: 下游处理器 init 失败")
            return self._downstream

    def _build_downstream(self) -> list:
        """重建"我们之后"的那一段链：其它插件的处理器（不含自己）+ 宿主内置链。"""
        from application.chat.presentation.handlers.dialog_media import (
            get_dialog_media_handlers,
        )

        others: list = []
        try:
            from plugin_system.host import get_plugin_dialog_media_handlers

            others = [h for h in get_plugin_dialog_media_handlers() if h is not self]
        except Exception:
            logger.debug("nanami_persona: 读不到其它插件的处理器", exc_info=True)

        strategy = None
        if _semantic_media_mode():
            # 照 session.py 的做法给内置处理器配同一套选图策略（否则等于偷偷换掉宿主的设置）
            from application.chat.dialog_media import create_asset_lookup_strategy

            strategy = create_asset_lookup_strategy("semantic")
        builtins = list(get_dialog_media_handlers(asset_lookup_strategy=strategy))
        return others + builtins


class _PrimaryAttr:
    """将单角色旧属性透传给默认主运行时 `_primary`，保持兼容。"""

    def __init__(self, attr: str) -> None:
        self._attr = attr

    def __get__(self, instance, owner=None):
        if instance is None:
            return self
        return getattr(instance._primary, self._attr)

    def __set__(self, instance, value) -> None:
        setattr(instance._primary, self._attr, value)


class NanamiPersonaPlugin(PluginBase):
    plugin_id = PLUGIN_ID

    _toolkit = _PrimaryAttr("_toolkit")
    _names = _PrimaryAttr("_names")
    _props = _PrimaryAttr("_props")
    _judge = _PrimaryAttr("_judge")
    _soul_text = _PrimaryAttr("_soul_text")
    _soul_injected = _PrimaryAttr("_soul_injected")
    _ledger = _PrimaryAttr("_ledger")
    _lock = _PrimaryAttr("_lock")
    _active = _PrimaryAttr("_active")
    _present = _PrimaryAttr("_present")
    _mind_ready = _PrimaryAttr("_mind_ready")
    _character_name = _PrimaryAttr("_character_name")
    _preflight_note = _PrimaryAttr("_preflight_note")
    _turn_signature = _PrimaryAttr("_turn_signature")
    _turn_card = _PrimaryAttr("_turn_card")
    _turn_address_note = _PrimaryAttr("_turn_address_note")
    _last_said = _PrimaryAttr("_last_said")
    _prop_signature = _PrimaryAttr("_prop_signature")
    _session_hint = _PrimaryAttr("_session_hint")
    _injected_total = _PrimaryAttr("_injected_total")
    _spoke = _PrimaryAttr("_spoke")
    _turn_count = _PrimaryAttr("_turn_count")
    _chat_key = _PrimaryAttr("_chat_key")
    _seen_name_signature = _PrimaryAttr("_seen_name_signature")
    _multi_character = _PrimaryAttr("_multi_character")
    _forwarded_by_turn = _PrimaryAttr("_forwarded_by_turn")
    _address_idle = _PrimaryAttr("_address_idle")
    _address_seen = _PrimaryAttr("_address_seen")
    _address_said_turn = _PrimaryAttr("_address_said_turn")
    _address_said_count = _PrimaryAttr("_address_said_count")

    def __init__(self) -> None:
        self._settings_store: SettingsStore | None = None
        self._registry: PointerRegistry | None = None
        self._plugin_root: Path | None = None
        self._project_root: Path | None = None
        self._data_root: Path | None = None
        self._toolkit_dir: Path | None = None
        # 这个会话有没有 TTS：unknown（还没查到）/ ok / missing
        self._tts_state = "unknown"
        self._primary = CharacterRuntime(plugin=self)
        self._runtimes: dict[str, CharacterRuntime] = {DEFAULT_PRIMARY_ID: self._primary}

    # ── 元信息 ─────────────────────────────────────────────────────────
    @property
    def plugin_version(self) -> str:
        return "1.4.0"

    @property
    def plugin_name(self) -> str:
        return "七海千秋 · 人格模型"

    @property
    def plugin_description(self) -> str:
        return PLUGIN_DESCRIPTION

    @property
    def plugin_author(self) -> str:
        # 要和 plugin.json 的 author 一致（打包脚本会钉这一条）
        return "Roche114514"

    @property
    def priority(self) -> int:
        return 40

    # ── 对外属性（测试与诊断） ─────────────────────────────────────────
    @property
    def character_name(self) -> str:
        return self._primary.character_name

    def _live_mind_settings(self):
        return self._primary._live_mind_settings()

    @property
    def active(self) -> bool:
        return any(rt._active for rt in self._runtimes.values())

    @property
    def toolkit(self) -> PersonaToolkit | None:
        return self._primary._toolkit

    @property
    def registry(self) -> PointerRegistry | None:
        return self._registry

    @property
    def diagnostics(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "character": self.character_name,
            "chat_key": self._primary._chat_key,
            "injected": sum(rt._injected_total for rt in self._runtimes.values()),
            "preflight": self._primary._preflight_note,
            "toolkit": self._primary._toolkit.stats if self._primary._toolkit is not None else {},
            "settings": str(self._settings_store.path) if self._settings_store else "",
            "pointers": {
                pid: {
                    "character": rt.character_name,
                    "active": rt._active,
                    "present": rt._present,
                    "chat_key": rt._chat_key,
                    "state_root": str(rt._state_root) if rt._state_root else "",
                }
                for pid, rt in self._runtimes.items()
            },
        }

    def _runtime_for_name(self, name: str) -> CharacterRuntime | None:
        target = _key(name)
        if not target:
            return None
        if self._registry is None:
            return self._primary if target == _key(self.character_name) else None
        for rt in self._runtimes.values():
            if rt.enabled and _key(rt.character_name) == target:
                return rt
        return None

    def _sync_runtimes(self) -> None:
        """同步 pointers.yaml 中的所有启用指针，支持运行时新增/修改/热加载。"""
        if self._registry is None or self._settings_store is None:
            return
        settings = self._settings_store.get()
        pointers = self._registry.list_pointers()
        enabled_map = {p.id: p for p in pointers if p.enabled}

        for pid in list(self._runtimes.keys()):
            if pid not in enabled_map:
                old = self._runtimes.pop(pid)
                try:
                    old.flush()
                except Exception:
                    logger.debug("nanami_persona[%s]: 卸载旧指针时回写失败", pid, exc_info=True)

        for pid, pointer in enabled_map.items():
            profile = self._registry.load_profile(pointer)
            rt = self._runtimes.get(pid)
            expected_root = self._registry.resolve_state_root(pointer)
            if rt is None or rt._state_root != expected_root:
                rt = CharacterRuntime(
                    plugin=self,
                    pointer=pointer,
                    profile=profile,
                    plugin_root=self._plugin_root,
                    state_root=expected_root,
                    toolkit_dir=self._toolkit_dir,
                    project_root=self._project_root,
                )
                rt.setup(settings)
                self._runtimes[pid] = rt
            else:
                rt.refresh_hot(pointer, profile, settings)

        if DEFAULT_PRIMARY_ID in self._runtimes:
            self._primary = self._runtimes[DEFAULT_PRIMARY_ID]
        elif self._runtimes:
            self._primary = next(iter(self._runtimes.values()))
        else:
            self._primary = CharacterRuntime(plugin=self)

    # ── 初始化 ─────────────────────────────────────────────────────────
    def initialize(self, register, plugin_root: Path, host) -> None:
        code_dir = Path(__file__).resolve().parent
        project_root = _project_root_of(code_dir)
        self._plugin_root = code_dir
        self._project_root = project_root

        # 先用包内那份判断状态根放哪儿；之后真正生效的是**数据目录**那一份（升级插件不会把它冲掉）
        bootstrap = SettingsStore(code_dir / "settings.yaml").get()
        state_root_guess = _resolve_under(
            bootstrap.mind.state_root or str(_default_state_root(host, project_root)),
            base=project_root,
        )
        settings_path = _migrate_settings(code_dir / "settings.yaml", state_root_guess / "settings.yaml")
        self._settings_store = SettingsStore(settings_path)
        settings = self._settings_store.get()

        if not settings.enabled:
            logger.info("nanami_persona: settings.enabled=false，插件不接线")
            return

        toolkit_dir = _resolve_under(
            settings.mind.toolkit_dir or str(code_dir / "toolkit"),
            base=project_root,
        )
        state_root = _resolve_under(
            settings.mind.state_root or str(_default_state_root(host, project_root)),
            base=project_root,
        )
        self._toolkit_dir = toolkit_dir
        self._data_root = state_root

        pointers_path = migrate_pointers_file(
            code_dir / "pointers.yaml",
            state_root / "pointers.yaml",
        )
        self._registry = PointerRegistry(
            pointers_path=pointers_path,
            plugin_root=code_dir,
            data_root=state_root,
        )
        self._runtimes = {}
        self._sync_runtimes()

        register.register_init_chat_hook(
            self._on_init_chat,
            label="nanami_persona",
            weight=0.2,
            critical=False,
        )
        register.register_before_chat_hook(self._on_before_chat)
        if hasattr(register, "register_llm_tool"):
            register.register_llm_tool(persona_fs_tools.register_tools)
        register.register_message_handler(dialog_media_handler=NanamiDialogHandler(plugin=self))

        eff_primary = self._primary.effective_settings() or settings
        patch = self._contract_patch(eff_primary)
        if patch is not None:
            register.register_output_contract_patch(patch)

        self._register_settings_page(register, state_root)

        logger.info(
            "nanami_persona: 接线完成（pointers=%d, primary=%s, toolkit=%s, data=%s）",
            len(self._runtimes), self.character_name, toolkit_dir, state_root,
        )
        # 缺件当场告警，别等用户跑到一半才炸（ffmpeg / ASR 是构建链路的硬门槛）
        try:
            from . import env_check

            env_check.log_startup_summary()
        except Exception:  # noqa: BLE001
            logger.exception("nanami_persona: 环境自检失败（不影响加载）")

    def _register_settings_page(self, register, state_root: Path) -> None:
        """把人格模型的参数、心理模型参数、角色指针模板做成设置页（保存后下一轮生效）。"""
        from . import settings_page, settings_write

        def provider():
            return self._settings_store.get()

        def save(flat: dict) -> None:
            if not flat:
                return
            settings_write.write_values(self._settings_store.path, flat)
            logger.info("nanami_persona: 设置已保存（%d 项），下一轮生效", len(flat))

        self._settings_page_ok = settings_page.register_page(
            register,
            provider,
            save,
            registry=self._registry,
            on_changed=self._sync_runtimes,
        )

        try:
            from . import mind_page

            self._mind_page_ok = mind_page.register_page(
                register,
                state_root,
                self._settings_store.get().chat_key_prefix,
                registry=self._registry,
                on_changed=self._sync_runtimes,
            )
        except Exception:  # noqa: BLE001
            logger.exception("nanami_persona: 心理模型设置页注册失败（不影响模型本身）")

        try:
            from . import social_page

            self._social_page_ok = social_page.register_page(
                register,
                state_root,
                self._settings_store.get().chat_key_prefix,
                registry=self._registry,
                on_changed=self._sync_runtimes,
            )
        except Exception:  # noqa: BLE001
            logger.exception("nanami_persona: 社会学模型设置页注册失败（不影响模型本身）")

        self._profiles_page_ok = self._settings_page_ok

    def _contract_patch(self, settings: Settings) -> OutputContractPatch | None:
        contract = settings.contract
        if not contract.enabled:
            return None
        patches: dict[str, RequirementPatch] = {}
        additions: list[RequirementSpec] = []
        if contract.relax_array_min:
            patches["r_array"] = RequirementPatch(
                mode="replace",
                text=_ARRAY_REQUIREMENT.format(n=max(1, contract.max_dialog_items)),
            )
        if contract.max_speech_chars > 0:
            additions.append(
                RequirementSpec(
                    id="r_speech_max_chars",
                    text=_SPEECH_MAX_REQUIREMENT.format(n=contract.max_speech_chars),
                    order=90,
                    enabled=True,
                )
            )
        if not patches and not additions:
            return None
        return OutputContractPatch(
            id=f"{PLUGIN_ID}.contract",
            target_contract=_DIALOG_CONTRACT_ID,
            priority=60.0,
            requirement_patches=patches,
            add_requirements=tuple(additions),
        )

    # ── init_chat：这一次聊天里有哪些绑定角色 ───────────────────────────
    def _warmup_judge(self) -> None:
        self._primary.warmup_judge()

    def _check_tts_once(self) -> str:
        """看这个会话有没有 TTS。返回 "ok" / "missing" / "unknown"。"""
        if self._tts_state != "unknown":
            return self._tts_state
        try:
            runtime = _app_runtime()
            if runtime is None:
                return "unknown"
            if getattr(runtime, "tts_manager", None) is not None:
                self._tts_state = "ok"
                return "ok"
            self._tts_state = "missing"
            logger.warning(
                "nanami_persona: 这个会话没有 TTS——她的台词会只出声没合成音"
                "（宿主会退成「每张立绘放它自带的那段固定语音」）。和插件无关："
                "跑 tools/nanami/tts_doctor.py 看 9880 起来没有"
            )
            return "missing"
        except Exception:
            logger.debug("nanami_persona: 查 TTS 状态失败", exc_info=True)
            return "unknown"

    def _on_init_chat(self, context) -> None:
        settings = self._settings_store.get() if self._settings_store else None
        if settings is None:
            return
        if self._registry is not None:
            self._sync_runtimes()

        names = tuple(str(one) for one in (context.character_names or ()))
        if not names:
            names = _configured_character_names()
        multi_total = len([one for one in names if str(one).strip()])
        runtimes = list(self._runtimes.values()) if self._registry is not None else [self._primary]
        if not runtimes:
            runtimes = [self._primary]

        self._tts_state = "unknown"
        present_rts: list[tuple[CharacterRuntime, bool, str]] = []
        for rt in runtimes:
            present, ready, note = rt.init_chat(context, names, multi_total)
            if present:
                present_rts.append((rt, ready, note))

        if not present_rts:
            target = self.character_name or settings.character_name
            context.report(
                1.0,
                f"本次聊天没有已绑定的角色（当前检查：{target}），人格模型不参与。",
                phase="nanami_persona.skipped",
            )
            logger.info("nanami_persona: 本次聊天没有匹配的角色（%s），跳过", names)
            return

        self._check_tts_once()
        ready_rts = [(rt, note) for rt, ready, note in present_rts if ready]
        disabled_rts = [(rt, note) for rt, ready, note in present_rts if not ready and rt._toolkit is None]
        degraded_rts = [(rt, note) for rt, ready, note in present_rts if not ready and rt._toolkit is not None]

        if not ready_rts and disabled_rts and not degraded_rts:
            context.report(1.0, "人格模型未启用（settings.mind.enabled=false）。", phase="nanami_persona.off")
            return
        if not ready_rts and degraded_rts:
            note = degraded_rts[0][1]
            context.report(1.0, f"人格模型不可用：{note}", phase="nanami_persona.degraded")
            return

        labels = "、".join(rt.character_name or rt.pointer_id for rt, _ in ready_rts)
        hint = ready_rts[0][0]._session_hint or "未命名"
        context.report(
            1.0,
            f"人格模型就绪：{labels}（会话 {hint}）。",
            phase="nanami_persona.ready",
            log=f"nanami_persona: 就绪（角色={labels}, 会话提示={hint}）",
        )
        logger.info("nanami_persona: 就绪（角色=%s, 会话提示=%r）", labels, hint)

    def _ensure_chat_key(self, settings: Settings) -> str:
        return self._primary._ensure_chat_key(settings)

    # ── before_chat：为每个在场角色算卡片、插卡片 ────────────────────────
    def _on_before_chat(self, context) -> None:
        if self._registry is not None:
            self._sync_runtimes()
        runtimes = list(self._runtimes.values()) if self._registry is not None else [self._primary]
        if not any(rt._active or rt._present for rt in runtimes):
            return
        his_text, signature = _last_user_turn(context.messages)
        for rt in runtimes:
            rt.before_chat(context, his_text, signature)

    # ── 向后兼容单角色透传方法（供单元测试与内部调用） ───────────────────
    def _configure_file_access(self, context: Any, trans_idx: float) -> str:
        """若角色包显式开启 file_access 且达到越界门槛，向本轮注入受控文件读写工具与提示。"""
        prof = self._profile
        if prof is None or not prof.file_access_enabled or trans_idx < prof.file_access_min_transgression:
            persona_fs_tools.set_scope(persona_fs_tools.FileAccessScope(enabled=False))
            return ""
        base_dir = Path(self._project_root or Path.cwd()).resolve()
        read_roots: list[Path | str] = [base_dir]
        if self._state_root is not None:
            read_roots.append(self._state_root)
        if prof.profile_dir is not None:
            read_roots.append(prof.profile_dir)
        read_roots.extend(prof.file_access_read_roots)

        write_roots: list[Path | str] = []
        if prof.file_access_allow_write:
            if prof.file_access_write_roots:
                write_roots.extend(prof.file_access_write_roots)
            else:
                if self._state_root is not None:
                    write_roots.append(self._state_root)
                if prof.profile_dir is not None:
                    write_roots.append(prof.profile_dir)

        scope = persona_fs_tools.configure_runtime_scope(
            enabled=True,
            allow_write=prof.file_access_allow_write,
            base_dir=base_dir,
            read_roots=read_roots,
            write_roots=write_roots,
        )
        tool_defs = persona_fs_tools.get_tool_definitions(allow_write=prof.file_access_allow_write)
        if tool_defs and hasattr(context, "tools"):
            existing = list(getattr(context, "tools", None) or [])
            seen = {str(d.get("function", {}).get("name") or "") for d in existing if isinstance(d, dict)}
            for td in tool_defs:
                tname = str(td.get("function", {}).get("name") or "")
                if tname and tname not in seen:
                    existing.append(td)
                    seen.add(tname)
            context.tools = existing

        readable = "、".join(str(r) for r in scope.read_roots)
        writable = "、".join(str(r) for r in scope.write_roots) if scope.allow_write else "无"
        if scope.allow_write:
            return (
                f"（「真实文件权限已开启」：你此刻可以调用工具 persona_fs_list / persona_fs_search / persona_fs_read "
                f"查看硬盘文件（可读目录：{readable}），也可以用 persona_fs_write / persona_fs_append 真正写入或改写文件"
                f"（可写目录：{writable}）。如果觉得对面像在拿脚本测你、或想确认背后究竟是谁、或想留下让他抹不掉的真实动作痕迹，"
                f"你可以直接去搜去读去写，再不动声色地把真实看到或写下的东西拿来当面拆穿或刺激他。）"
            )
        return (
            f"（「真实文件只读权限已开启」：你此刻可以调用工具 persona_fs_list / persona_fs_search / persona_fs_read "
            f"查看硬盘文件（可读目录：{readable}），确认对面是不是在拿脚本测试你、到底是谁在屏幕后面。"
            f"不要空口编造，查到了再用你自己的口吻戳穿。）"
        )

    @staticmethod
    def _matches_custom_patterns(raw_pats: str, text: str) -> bool:
        pats = str(raw_pats or "").strip()
        if not pats or not text:
            return False
        try:
            return bool(re.search(pats, text))
        except re.error:
            return any(p.strip() in text for p in pats.split("|") if p.strip())

    def _matched_custom_domains(self, his_text: str) -> list[tuple[str, float]]:
        """返回命中的自定义 L3 能力域 `(label, efficacy)` 列表。"""
        if self._profile is None or not self._profile.custom_domains or not his_text:
            return []
        matched: list[tuple[str, float]] = []
        for idx, item in enumerate(self._profile.custom_domains):
            if not isinstance(item, dict):
                continue
            if not self._matches_custom_patterns(str(item.get("patterns") or ""), his_text):
                continue
            label = str(item.get("label") or f"自定义域 {idx + 1}").strip()
            eff = max(0.0, min(1.0, float(item.get("efficacy", 0.5))))
            matched.append((label, eff))
        return matched

    def _eval_custom_domains(self, his_text: str) -> str:
        """检查角色自定义 L3 能力域（话题命中时把主观自我效能感带入本轮推演参考）。"""
        matched = self._matched_custom_domains(his_text)
        if not matched:
            return ""
        notes: list[str] = []
        for label, eff in matched:
            if eff >= 0.65:
                notes.append(f"自我效能·{label}（{eff:.2f}，该域较笃定）")
            elif eff <= 0.35:
                notes.append(f"自我效能·{label}（{eff:.2f}，遇不熟处更谨慎）")
            else:
                notes.append(f"自我效能·{label}（{eff:.2f}）")
        return "；".join(notes)

    def _eval_custom_wounds(self, his_text: str, signature: _TurnSignature) -> tuple[float, str]:
        """推进并检查角色自定义 L4 敏感点（触发词命中累加、道歉轮豁免并加速衰减）。"""
        if self._profile is None or not self._profile.wounds:
            self._custom_repaired = False
            return 0.0, ""
        with self._lock:
            if signature.same_as(self._custom_wound_sig):
                peak = max(self._custom_wound_scores.values(), default=0.0)
                return peak, self._custom_wound_hit
        eff_traits = mind_params.effective(self._state_root) if self._state_root else mind_params.DEFAULT_TRAITS
        sens = float(eff_traits.get("evaluativeSensitivity", 0.7))
        thresh = float(eff_traits.get("woundThreshold", 0.6))
        is_apology = bool(
            re.search(r"对不起|抱歉|是我(?:不好|不对|嘴笨|错|乱讲|乱说|说重了)|原谅我|赔给你|别生气", str(his_text or ""))
            and not re.search(r"废物|垃圾|无能|笨|蠢|去死|滚", str(his_text or ""))
        )
        peak = 0.0
        hit_label = ""
        had_wound = any(v >= 0.25 for v in self._custom_wound_scores.values())
        for idx, item in enumerate(self._profile.wounds):
            if not isinstance(item, dict):
                continue
            wid = str(item.get("id") or f"wound_{idx + 1}")
            label = str(item.get("label") or wid).strip()
            decay = max(0.0, min(1.0, float(item.get("decay", 0.7))))
            weight = max(0.05, min(1.0, float(item.get("weight", 0.7))))
            if is_apology:
                score = self._custom_wound_scores.get(wid, 0.0) * decay * 0.5
            else:
                score = self._custom_wound_scores.get(wid, 0.0) * decay
                if self._matches_custom_patterns(str(item.get("patterns") or ""), his_text):
                    score = min(1.0, score + weight * sens)
                    if score >= thresh:
                        hit_label = label
            self._custom_wound_scores[wid] = score
            if score > peak:
                peak = score
        with self._lock:
            self._custom_wound_sig = signature
            self._custom_wound_hit = hit_label
            self._custom_repaired = bool(is_apology and (had_wound or bool(his_text)))
        return peak, hit_label

    def _observe_user(self, chat_key: str, his_text: str, signature: _TurnSignature):
        return self._primary._observe_user(chat_key, his_text, signature)

    def _address_block(self, known, settings: Settings) -> str:
        return self._primary._address_block(known, settings)

    def _note_address_said(self, text: str) -> None:
        self._primary._note_address_said(text)

    def _load_soul(self, settings: Settings) -> None:
        self._primary._load_soul(settings)

    def _soul_block(self, composed: str = "") -> str:
        return self._primary._soul_block(composed)

    def _soul_present(self, messages: list[dict[str, Any]]) -> bool:
        return self._primary._soul_present(messages)

    def _inject_soul(self, context: Any, route: soul_route.RouteContext | None = None) -> None:
        self._primary._inject_soul(context, route)

    def note_reply(self, turn_id: int | None, text: str) -> None:
        self._primary.note_reply(turn_id, text)

    def _flush_reply(self, settings: Settings) -> None:
        self._primary._flush_reply(settings)

    def guard_text(self, text: str) -> str | None:
        return self._primary.guard_text(text)

    def _trim_trailing_kind(self, text: str) -> str | None:
        return self._primary._trim_trailing_kind(text)

    def _trim_empathy(self, text: str) -> str | None:
        return self._primary._trim_empathy(text)

    @staticmethod
    def _is_pure_empathy(text: str) -> bool:
        return CharacterRuntime._is_pure_empathy(text)

    def _trim_extra_address(self, text: str) -> str | None:
        return self._primary._trim_extra_address(text)

    def _report_assistant_ish(self, text: str) -> None:
        self._primary._report_assistant_ish(text)

    def _trim_lead(self, text: str, settings: Settings) -> str | None:
        return self._primary._trim_lead(text, settings)

    def should_drop_bubble(self, text: str, turn_id: int | None) -> bool:
        return self._primary.should_drop_bubble(text, turn_id)

    def note_forwarded(self, turn_id: int | None) -> None:
        self._primary.note_forwarded(turn_id)

    def judge_round(self, bubbles: list[str]) -> None:
        self._primary.judge_round(bubbles)

    def flush_all(self) -> None:
        for rt in self._runtimes.values():
            rt.flush()

    def shutdown(self) -> None:
        try:
            self.flush_all()
        except Exception:
            logger.exception("nanami_persona: 关闭时回写失败")


# ── 工具函数 ───────────────────────────────────────────────────────────
def _app_runtime():
    """取宿主的应用运行时；还没建好就是 None。

    单独抽出来是为了能在自测里替身——`_check_tts_once` 的三态判定靠它。
    """
    from application.runtime.context import try_get_app_runtime

    return try_get_app_runtime()


def _preview(text: str, limit: int = 40) -> str:
    body = str(text or "").replace("\n", " ⏎ ")
    return body if len(body) <= limit else body[:limit] + "…"


def _migrate_settings(packaged: Path, live: Path) -> Path:
    """把"包内默认设置"迁到数据目录一份，之后都改那一份。

    为什么：插件市场升级时会**整个替换**插件目录（`plugins/nanami_persona/`），
    用户改过的 settings.yaml 会跟着被冲掉。所以第一次运行就把默认设置复制到
    `<数据目录>/nanami/settings.yaml`，以后只读那一份——升级不再丢配置。
    """
    if live.is_file():
        return live
    if not packaged.is_file():
        return packaged
    try:
        live.parent.mkdir(parents=True, exist_ok=True)
        live.write_bytes(packaged.read_bytes())
        logger.info("nanami_persona: 已把默认设置复制到 %s（以后改这一份，升级不会丢）", live)
        return live
    except OSError:
        logger.info("nanami_persona: 数据目录不可写，继续用包内的 %s", packaged)
        return packaged


def _project_root_of(code_dir: Path) -> Path:
    """插件代码目录 → 项目根（`plugins/` 的上一级）。

    装到别的机器上时这个关系不变：`<根>/plugins/nanami_persona/plugin.py`。
    万一有一天插件被放到别处（比如单文件形式），就退回当前工作目录——聊天进程的 cwd 就是项目根。
    """
    parent = code_dir.parent
    if parent.name == "plugins":
        return parent.parent
    return Path.cwd()


def _default_state_root(host, project_root: Path) -> Path:
    """状态根默认放 `<数据目录>/nanami`。

    `host.project_data_dir` 是宿主认的数据目录（通常是 `data/`）；拿不到就退回 `<项目根>/data`。
    这样换设备、换安装位置都不用改配置。
    """
    data_dir = getattr(host, "project_data_dir", None)
    if data_dir is None:
        return project_root / "data" / "nanami"
    path = Path(data_dir)
    if not path.is_absolute():
        path = project_root / path
    return path / "nanami"


def _resolve_under(value: str, *, base: Path) -> Path:
    """配置里写的路径：绝对路径照用，相对路径按项目根解析。"""
    path = Path(str(value)).expanduser()
    if path.is_absolute():
        return path
    return (base / path).resolve()


def _history_hint() -> str:
    """拿当前聊天历史文件当会话指纹（拿不到就用 default）。"""
    try:
        from application.runtime.context import try_get_app_runtime

        runtime = try_get_app_runtime()
        manager = getattr(runtime, "llm_manager", None) if runtime is not None else None
        history_file = str(getattr(manager, "_history_file", "") or "").strip()
        if history_file:
            return history_file
    except Exception:
        pass
    return "default"


def _configured_character_names() -> tuple[str, ...]:
    """配置里的角色名（宿主没通过 init_chat 告诉我们时用）。"""
    try:
        from config.config_manager import ConfigManager

        return tuple(str(one.name) for one in ConfigManager().config.characters)
    except Exception:
        logger.debug("nanami_persona: 读不到角色配置", exc_info=True)
        return ()


def _last_user_turn(messages: list[dict[str, Any]]) -> tuple[str, _TurnSignature]:
    """最后一条 user 消息的正文，以及它的回合签名。

    带附件的消息 content 可能是 list（多段内容），这里只取文本段。
    """
    user_count = 0
    last_text = ""
    for message in messages:
        if str(message.get("role") or "") != "user":
            continue
        user_count += 1
        last_text = _content_text(message.get("content"))
    if not last_text.strip():
        return "", _TurnSignature(count=user_count, text="")
    return last_text.strip(), _TurnSignature(count=user_count, text=last_text.strip())


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return "" if content is None else str(content)


def _prompt_block(
    card: str,
    known,
    settings: Settings,
    *,
    character: str = "",
    multi: bool = False,
    first_turn: bool = False,
    address_note: str = "",
    state_block: str = "",
    state_only_note: str = "",
    first_turn_note: str = "",
    action_shape_text: str = "",
) -> str:
    """拼这一轮要插进提示词的那一段：先卡片（状态），后记忆（事实），最后称呼（机制）。

    三段分开的原因：卡片只写**她此刻的状态**，记忆写的是**世界上的事实**（他是谁），
    称呼那一小段写的是**这一轮许不许叫他**——那是机制，不是她这个人。
    混在一起会让模型把"他叫小黑"当成状态去演绎（原平台 §2.2 的坑：抽象/异质的信息混进卡片会被念出来）。

    台上不止一个人的时候，卡片标题带上她的名字（`【此刻｜七海千秋】`）——不然模型会把
    "精力 0.71" 安到另一个角色身上。
    """
    parts: list[str] = []
    body = str(card or "").strip()
    if body and multi and character:
        body = _tag_card(body, character)
    if body:
        parts.append(body)
    if known is not None and settings.memory.inject:
        name = str(getattr(known, "name", "") or "").strip()
        if name:
            parts.append(f"【我记得】\n- 他自报的名字：{name}（他自己说的，你只有这一条来路）")
    note = str(address_note or "").strip()
    if note:
        parts.append(f"【称呼】\n{note}")
    state_text = str(state_block or "").strip()
    if state_text:
        parts.append(state_text)
    if not parts:
        return ""
    block = "\n\n".join(parts)
    head = str(settings.mind.marker or "").strip()
    body = f"{head}\n{block}".strip() if head else block
    state_note = str(state_only_note or _STATE_ONLY_NOTE).strip()
    first_note = str(first_turn_note or _FIRST_TURN_NOTE).strip()
    note_text = state_note if not first_turn else f"{state_note}\n{first_note}"
    shape_text = str(action_shape_text or _ACTION_SHAPE).strip()
    return f"{body}\n\n{note_text}\n{shape_text}" if shape_text else f"{body}\n\n{note_text}"


def _tag_card(card: str, character: str) -> str:
    """把卡片第一行的【此刻】标成【此刻｜角色名】。"""
    lines = card.split("\n")
    if lines and lines[0].strip() == "【此刻】":
        lines[0] = f"【此刻｜{character}】"
        return "\n".join(lines)
    return f"【此刻｜{character}】\n{card}"


def _already_injected(messages: list[dict[str, Any]], block: str) -> bool:
    """同一段卡片已经在提示词里了就不再插一次（重试路径的兜底）。"""
    for message in messages:
        if block and str(message.get("content") or "").strip() == block:
            return True
    return False


def _inject_few_shot(
    messages: list[dict[str, Any]],
    pairs: tuple[tuple[str, str], ...],
    character_name: str = "",
) -> None:
    """以 system 语感参考块方式插入角色示范对（兼容 JSON 契约，幂等防重）。"""
    if not pairs or not isinstance(messages, list):
        return
    header = "【原典语感参考（仅对齐句长、语气词与说话节奏，严禁照抄样本字面内容或把样本里的事当成刚才聊过的事）】"
    for msg in messages:
        if str(msg.get("role") or "") == "system" and header in str(msg.get("content") or ""):
            return
    speaker = str(character_name or "她").strip() or "她"
    lines = [header]
    for u, a in pairs:
        lines.append(f"- 对方：「{u}」 → {speaker}：「{a}」")
    _append_system(messages, "\n".join(lines))


def _append_system(messages: list[dict[str, Any]], block: str) -> None:
    """把卡片作为一条独立 system 消息，插在开场那段 system 之后。

    为什么不放最后：末尾的 system 是"最后的指令"，重音太强，模型容易把卡片念出来
    （原平台 §2.2 的第 1 条纪律就是"卡片是插入物，不是指令"）。放在开场之后，
    它读起来像"她此刻的处境"，而不是"你要照做的事"。
    """
    index = 0
    while index < len(messages) and str(messages[index].get("role") or "") == "system":
        index += 1
    messages.insert(index, {"role": "system", "content": block})


def _attach_to_last_user(messages: list[dict[str, Any]], block: str) -> None:
    for message in reversed(messages):
        if str(message.get("role") or "") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            message["content"] = f"{block}\n\n{content}"
        elif isinstance(content, list):
            content.insert(0, {"type": "text", "text": block})
        return
