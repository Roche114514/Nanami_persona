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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sdk.handlers import MessageHandler
from sdk.messages import LLMDialogMessage
from sdk.plugin import PluginBase
from sdk.types import OutputContractPatch, RequirementPatch, RequirementSpec

from .judge import LlmJudge
from .mind_bridge import MindCard, PersonaToolkit, chat_key_of
from .names import NameMemory
from .settings import Settings, SettingsStore

logger = logging.getLogger(__name__)

# 插件唯一标识（稳定、可分发；`data/plugins/<这个 id>/` 是宿主给插件的数据目录）
PLUGIN_ID = "community.nanami.persona"

# 市场页与插件类共用同一句简介（plugin.json 的 desc 必须和它一致，打包脚本会钉住这一点）
PLUGIN_DESCRIPTION = (
    "装上就生效：自带逐条核对过原典的人格壳，运行时覆盖角色包里那份简介；"
    "每轮再算一张【此刻】状态卡，发送前清掉 AI 味，并记住用户自报的称呼。"
    "不改你的 characters.yaml、不重建模板、不装依赖；需要 Node 18+。"
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
    "数组中至少有一个元素，通常 1–2 个，最多不超过 {n} 个。"
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

# 系统句／登记表：这些出现就说明她切成了助手腔（report 只记日志，不改词）。
_ASSISTANT_ISH = (
    ("这个我也不知道", "系统句：该说「不知道……不过，有点令人在意吧」那种短句"),
    ("我无法回答", "系统句：拒答腔"),
    ("请问有什么", "客服腔"),
    ("希望这对你有帮助", "客服腔"),
    ("你想让我怎么叫你", "登记表腔"),
    ("你希望我怎么称呼你", "登记表腔"),
    ("请告诉我你的名字", "登记表腔"),
)

# 人格壳那一整段的抬头。**也是"这一层由插件负责"的标记**——
# 她的来历不是角色包里的简介，而是插件随身带的这份（原典逐条核过）。
SOUL_MARKER = "【她是谁】"

# 卡片后面必须跟这一句：**它是状态，不是台词**。
# 没有这句的时候实测会出两种事故（都是主人抓到的）：
#   ① 把「我想要：想跟人一起玩」直接说成台词（"想找个人一起打一局"）；
#   ② 一上来就照着状态问对方在做什么（"刚打完一局，你呢，在做什么？"）。
# 这句不属于心理学模型，它只是让卡片**别被念出来**——改的是看法，不是改她这个人。
_STATE_ONLY_NOTE = (
    '（以上是你此刻的状态，不是台词：别照着念、别把「我想要」说出来，也别拿它去盘问对方。'
    '你不知道对面是谁——除非上面另有一段写着"我记得"、里面有名字。所以不要问“你是谁”“你在做什么”，'
    '也不要让对方报名字来方便你称呼；他主动说了名字，你才记住。'
    '过去的事别生：家人、学校、小时候这类细节你没有，也不要编。）'
)

# 第一次开口：实测她会照着状态问对方"你那边现在在做什么呢"——那不像她，也不合逻辑
# （她不知道对面是谁）。这条只在这一场的第一轮出现，且只改"要不要问"，不给人设。
_FIRST_TURN_NOTE = (
    "（这是这一场第一次开口：你什么都不知道。**这一轮不用问句**——一个问号都不要："
    '不问名字、不问他在做什么、不问他的事，连“你也是来……吗”这种猜法也别用。'
    "就说你这一会儿的自己：在打哪一局、困不困、看见什么。）"
)

# 卡片上的动作只是"她打算怎么处理这句"，不是"这句话该长什么样"。
# 缺了这一段会发生什么：卡片写「应一声就好」，模型就按聊天机器人的默认去"接住情绪"，
# 于是冒出"我也常常这样""那个我懂"——实测消融里，真卡那一组比**没有卡**还像客服。
# 所以这里只给"形状"，不给台词；并且明写不许照抄（上游 r86 的教训：做法写进卡片会被念出来）。
_ACTION_SHAPE_LINES = (
    "（「这一轮我只想」落地成句子时是这样，**不是台词，不许照抄**：",
    "- 应一声就好／短答：一个极短的反应（几个字到一句），不解释、不共情、不举自己的例子、不复述他的话；",
    "- 顶回去／驳论点：平地说一句否，说完就停，不解释理由；",
    "- 反问：问一件具体的事，问完就停；",
    "- 断言／说我自己那一份：说她自己的一份判断或事实，说完就停；",
    "- 跑题拆结构：把话岔到「这东西怎么运转」，拆一层就停；",
    "- 半句／睡着：说半句就停，或者只有一声。）",
)
_ACTION_SHAPE = chr(10).join(_ACTION_SHAPE_LINES)

_SPEECH_MAX_REQUIREMENT = "台词长度：每一句 speech 不超过 {n} 个字符（含标点）。她一句通常十几到二十几个字，长句会切开成两句。"


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


class NanamiDialogHandler(MessageHandler):
    """③ 守卫层：她的台词在送去 TTS/UI 之前清一遍 AI 味。

    为什么必须"接过来再交回去"：宿主的 dialog 处理器是**先匹配先处理**，插件排在前面；
    匹配到就不再往下走。所以这里做两件事——改文本、然后把消息交给**后面那一段链**，
    否则她的台词会停在队列里不发音，或者别的插件的处理器被跳过。

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

    # 只认她本人：旁白/选项/数值/bgm 一个都不接
    def can_handle(self, msg: LLMDialogMessage) -> bool:
        name = str(msg.name or "")
        if not name or _is_system_name(name):
            return False
        if _key(name) != _key(self._plugin.character_name):
            return False
        if self._downstream_failed:
            # 重建下游失败时**不接管**：宁可这一轮不清 AI 味，也不能让她的台词没人处理
            return False
        return True

    def pre_process(self, msg: LLMDialogMessage) -> None:
        text = str(msg.text or "")
        if not text.strip():
            return
        self._plugin.note_reply(msg.turn_id, text)
        cleaned = self._plugin.guard_text(text)
        final = cleaned if cleaned is not None else text
        if cleaned is not None and cleaned != text:
            logger.info(
                "nanami_persona: 守卫改写台词 %s -> %s",
                _preview(text),
                _preview(cleaned),
            )
        if self._plugin.should_drop_bubble(final, msg.turn_id):
            self._dropped = True
            return
        msg.text = final
        self._plugin.note_forwarded(msg.turn_id)

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


class NanamiPersonaPlugin(PluginBase):
    plugin_id = PLUGIN_ID

    def __init__(self) -> None:
        self._settings_store: SettingsStore | None = None
        self._toolkit: PersonaToolkit | None = None
        self._names: NameMemory | None = None
        self._judge: LlmJudge | None = None
        # 人格壳：插件自己带着，运行时插进提示词——**不依赖角色包里那份简介**，
        # 也不依赖有人预先改过 characters.yaml（那只是我这台机器上的开发动作）。
        self._soul_text = ""
        self._soul_injected = 0
        self._ledger = TurnLedger()
        self._lock = threading.Lock()
        self._active = False
        self._character_name = ""
        self._preflight_note = ""
        self._turn_signature: _TurnSignature | None = None
        self._turn_card: MindCard | None = None
        self._session_hint = ""
        self._injected_total = 0
        # 这一场有没有开过口（用来给第一轮加"别问他问题"的情境提示）
        self._spoke = False
        # 这是第几轮（before_chat 每轮加一）——第一轮有一道"不许盘问"的机械闸
        self._turn_count = 0
        self._chat_key = ""
        self._seen_name_signature: _TurnSignature | None = None
        self._multi_character = False
        self._forwarded_by_turn: dict[int | None, int] = {}

    # ── 元信息 ─────────────────────────────────────────────────────────
    @property
    def plugin_version(self) -> str:
        return "1.0.0"

    @property
    def plugin_name(self) -> str:
        return "七海千秋 · 人格模型"

    @property
    def plugin_description(self) -> str:
        return PLUGIN_DESCRIPTION

    @property
    def plugin_author(self) -> str:
        return "shinsekai-nanami-port"

    @property
    def priority(self) -> int:
        return 40

    # ── 对外属性（测试用） ────────────────────────────────────────────
    @property
    def character_name(self) -> str:
        store = self._settings_store
        if store is not None:
            name = store.get().character_name
            if name:
                return name
        return self._character_name

    def _live_mind_settings(self):
        """给工具箱的取值函数：settings.yaml 改了，下一轮就用新值。"""
        store = self._settings_store
        if store is None:
            return None
        return store.get().mind

    @property
    def active(self) -> bool:
        return self._active

    @property
    def toolkit(self) -> PersonaToolkit | None:
        return self._toolkit

    @property
    def diagnostics(self) -> dict[str, Any]:
        return {
            "active": self._active,
            "character": self._character_name,
            "chat_key": self._chat_key,
            "injected": self._injected_total,
            "preflight": self._preflight_note,
            "toolkit": self._toolkit.stats if self._toolkit is not None else {},
            "settings": str(self._settings_store.path) if self._settings_store else "",
        }

    # ── 初始化 ─────────────────────────────────────────────────────────
    def initialize(self, register, plugin_root: Path, host) -> None:
        code_dir = Path(__file__).resolve().parent
        project_root = _project_root_of(code_dir)
        # 先用包内那份判断状态根放哪儿；之后真正生效的是**数据目录**那一份（升级插件不会把它冲掉）
        bootstrap = SettingsStore(code_dir / "settings.yaml").get()
        state_root_guess = _resolve_under(
            bootstrap.mind.state_root or str(_default_state_root(host, project_root)),
            base=project_root,
        )
        settings_path = _migrate_settings(code_dir / "settings.yaml", state_root_guess / "settings.yaml")
        self._settings_store = SettingsStore(settings_path)
        settings = self._settings_store.get()
        self._character_name = settings.character_name

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

        if settings.mind.enabled:
            self._toolkit = PersonaToolkit(
                toolkit_dir=toolkit_dir,
                state_root=state_root,
                settings=self._live_mind_settings,
                project_root=project_root,
            )
        self._load_soul(settings)

        # 「他是谁」的记忆跟状态放同一个根，但单独一个文件（心理模型不碰它）
        self._names = NameMemory(state_root, enabled=settings.memory.learn_user_name)
        # 判官：原来要本地 3B 干的活（AI 味 / 垫字），现在问宿主配置的那个大模型
        self._judge = LlmJudge(
            state_root=state_root,
            enabled=settings.judge.enabled,
            timeout=settings.judge.timeout,
        )

        register.register_init_chat_hook(
            self._on_init_chat,
            label="nanami_persona",
            weight=0.2,
            critical=False,
        )
        register.register_before_chat_hook(self._on_before_chat)
        register.register_message_handler(dialog_media_handler=NanamiDialogHandler(plugin=self))

        patch = self._contract_patch(settings)
        if patch is not None:
            register.register_output_contract_patch(patch)

        logger.info(
            "nanami_persona: 接线完成（character=%s, toolkit=%s, state=%s）",
            self._character_name, toolkit_dir, state_root,
        )

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
            # order=90 与宿主 `r_speech_max_chars` 的位置一致；id 相同会覆盖宿主的（宿主那份这时是关的）
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

    # ── init_chat：这一次聊天里有没有她 ─────────────────────────────────
    def _warmup_judge(self) -> None:
        """在后台先问判官一句：把"这条连接第一次要 3 秒"的冷启动挪到用户开口之前。

        只影响 clean 档的延迟（它同步判）；report 档本来就是后台判，预热了更省。
        失败无所谓——判官本来就是 fail-open。
        """
        if self._judge is None or not self._judge.enabled:
            return

        def work() -> None:
            try:
                ok, note = self._judge.warmup()
                logger.info("nanami_persona: 判官预热%s（%s）", "完成" if ok else "未成功", note)
            except Exception:
                logger.debug("nanami_persona: 判官预热出错", exc_info=True)

        threading.Thread(target=work, name="nanami-judge-warmup", daemon=True).start()

    def _warn_if_tts_missing(self) -> None:
        """TTS 没起来时，她的回复只有字没有声——那多半和插件无关，但值得在日志里指一次路。

        真机踩过：`logs/main.log` 里是 `tts.init.failed: GPT-SoVITS server exited before
        becoming ready (code 1)`（上一个会话残留的实例还在读模型，宿主又起了一个抢端口）。
        这里只写一行日志，不改任何行为；也**必须不抛异常**——它挂在 init_chat 上，
        万一炸了会把后面的体检步骤全跳过（这件事我自己踩过一次）。
        """
        try:
            from application.runtime.context import try_get_app_runtime

            runtime = try_get_app_runtime()
            manager = getattr(runtime, "tts_manager", None) if runtime is not None else None
            if runtime is not None and manager is None:
                logger.warning(
                    "nanami_persona: 这个会话没有 TTS（回复会只有字没声）——和插件无关，"
                    "跑 tools/nanami/tts_doctor.py 看 9880 有没有起来"
                )
        except Exception:
            logger.debug("nanami_persona: 查 TTS 状态失败", exc_info=True)

    def _on_init_chat(self, context) -> None:
        settings = self._settings_store.get() if self._settings_store else None
        if settings is None:
            return
        names = tuple(str(one) for one in (context.character_names or ()))
        if not names:
            # 宿主没给角色名（有的启动方式只给初始立绘或从历史恢复）→ 退一步看配置里她是否在场，
            # 只有一个角色时这不会误判成"别人"，也就不至于让插件静默空转。
            names = _configured_character_names()
        present = any(_key(one) == _key(settings.character_name) for one in names)
        self._active = bool(present) and settings.mind.enabled and self._toolkit is not None
        # 台上不止一个人：卡片要标上她的名字，否则模型会把状态安到别人身上
        self._multi_character = len([one for one in names if str(one).strip()]) > 1

        # 会话指纹：init_chat 时应用运行时可能还没建好，所以这里只记宿主给的 session_id，
        # 真正的 `--chat` 键留到第一次 before_chat（那时 runtime 一定在了）再算。
        self._session_hint = str(getattr(context, "session_id", "") or "").strip()
        self._chat_key = ""

        if not present:
            context.report(
                1.0,
                "本次聊天没有七海千秋，人格模型不参与。",
                phase="nanami_persona.skipped",
            )
            logger.info("nanami_persona: 本次聊天没有 %s，跳过", settings.character_name)
            return

        if self._toolkit is None:
            context.report(1.0, "人格模型未启用（settings.mind.enabled=false）。", phase="nanami_persona.off")
            return

        self._warmup_judge()
        self._warn_if_tts_missing()
        ok, note = self._toolkit.preflight()
        self._preflight_note = note
        if not ok:
            self._active = False
            context.report(1.0, f"人格模型不可用：{note}", phase="nanami_persona.degraded")
            logger.warning("nanami_persona: 体检没过，跳过卡片注入：%s", note)
            return
        context.report(
            1.0,
            f"人格模型就绪（会话 {self._session_hint or '未命名'}）。",
            phase="nanami_persona.ready",
            log=f"nanami_persona: 就绪，会话提示={self._session_hint or '(runtime 未就绪，稍后取历史文件)'}",
        )
        logger.info("nanami_persona: 就绪（会话提示=%r）", self._session_hint)

    def _ensure_chat_key(self, settings: Settings) -> str:
        """算 `--chat` 键。第一次 before_chat 时算：那时应用运行时一定建好了，能取到当前历史文件。"""
        if self._chat_key:
            return self._chat_key
        hint = self._session_hint or _history_hint()
        self._chat_key = chat_key_of(settings.chat_key_prefix, hint)
        logger.info(
            "nanami_persona: 会话键 %s（来源=%s）",
            self._chat_key, "session_id" if self._session_hint else hint,
        )
        return self._chat_key

    # ── before_chat：算卡片、插卡片 ─────────────────────────────────────
    def _on_before_chat(self, context) -> None:
        if not self._active or self._toolkit is None:
            return
        settings = self._settings_store.get() if self._settings_store else None
        if settings is None:
            return

        his_text, signature = _last_user_turn(context.messages)
        if not his_text:
            return
        chat_key = self._ensure_chat_key(settings)

        # 上一位她说的话先回写，再推进这一轮的状态（顺序与原平台一致：her → 下一轮 his）
        self._flush_reply(settings)

        # 「他是谁」：他自己说了名字才记；被要求别这么叫就忘掉。
        known = self._observe_user(chat_key, his_text, signature)

        with self._lock:
            if signature.same_as(self._turn_signature):
                reuse = True
                card = self._turn_card
            else:
                reuse = False
                card = None
        if not reuse:
            card = self._toolkit.card(his_text, chat_key)
            with self._lock:
                self._turn_signature = signature
                self._turn_card = card
            if card is not None and settings.log_trace:
                logger.info(
                    "nanami_persona: 卡片 appraisal=%s action=%s energy=%.2f chat=%s",
                    card.appraisal, card.action, card.energy, chat_key,
                )

        card_text = card.card if card is not None and not card.empty else ""
        self._inject_soul(context)

        block = _prompt_block(
            card_text,
            known,
            settings,
            character=self.character_name,
            multi=self._multi_character,
            first_turn=not self._spoke,
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

    def _observe_user(self, chat_key: str, his_text: str, signature: _TurnSignature):
        """从这一句用户发言里学/忘称呼。同一个回合只读一次（工具循环里会重复触发）。"""
        if self._names is None:
            return None
        settings = self._settings_store.get() if self._settings_store else None
        if settings is None or not settings.memory.learn_user_name:
            return self._names.get(chat_key)
        with self._lock:
            if signature.same_as(self._seen_name_signature):
                return self._names.get(chat_key)
            self._seen_name_signature = signature
        return self._names.observe(chat_key, his_text)

    # ── 人格壳 ─────────────────────────────────────────────────────────
    def _load_soul(self, settings: Settings) -> None:
        """读插件自带的人格壳。读不到不要紧：卡片与守卫照样工作，只是少了这一层。"""
        if not settings.persona.inject:
            logger.info("nanami_persona: persona.inject=false，不注入人格壳")
            return
        path = Path(settings.persona.shell_path or "") if settings.persona.shell_path else (
            Path(__file__).resolve().parent / "soul" / "chiaki-soul.md"
        )
        try:
            text = Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            logger.warning("nanami_persona: 找不到人格壳 %s，跳过注入", path)
            return
        if not text:
            return
        self._soul_text = text
        logger.info("nanami_persona: 人格壳已装载（%s 字）：%s", len(text), path)

    def _soul_block(self) -> str:
        """拼"她是谁"那一段。**冲突时以这里为准**——角色包里那份简介多半是维基式的短介绍，
        还带着错的细节（比如"像素爱心发饰"），所以要先立规矩再上正文。"""
        if not self._soul_text:
            return ""
        if self._soul_text.startswith(SOUL_MARKER):
            body = self._soul_text[len(SOUL_MARKER):].lstrip()
        else:
            body = self._soul_text
        return (
            SOUL_MARKER
            + chr(10) + body + chr(10) + chr(10)
            + "（如果上面还有一份人物简介／角色设定，和这里冲突的地方**以这里为准**："
            + "这份是照原典逐条核过的，那份可能只是简介。）"
        )

    def _soul_present(self, messages: list[dict[str, Any]]) -> bool:
        """提示词里是不是已经有一份人格壳了（比如有人已经把 characters.yaml 改过）。

        判据用一句只有这份壳里才有的长句——比"标题相同"稳，也不会被卡片误伤。
        """
        needle = "她更常不带主语"
        for message in messages:
            if needle in str(message.get("content") or ""):
                return True
        return False

    def _inject_soul(self, context: Any) -> None:
        if not self._soul_text:
            return
        messages = context.messages
        if self._soul_present(messages):
            return
        block = self._soul_block()
        if not block or _already_injected(messages, block):
            return
        if self._settings_store is not None and self._settings_store.get().mind.card_position == "user":
            _attach_to_last_user(messages, block)
        else:
            _append_system(messages, block)
        self._soul_injected += 1

    # ── 回写与守卫 ─────────────────────────────────────────────────────
    def note_reply(self, turn_id: int | None, text: str) -> None:
        self._ledger.add(turn_id, self._chat_key, text)

    def _flush_reply(self, settings: Settings) -> None:
        if not settings.mind.writeback or self._toolkit is None:
            self._ledger.take()  # 不写盘也要清掉，免得攒到下一轮
            return
        said, chat_key = self._ledger.take()
        if not said:
            return
        bubbles = [line for line in said.split("\n") if line.strip()]
        try:
            self.judge_round(bubbles)
        except Exception:
            logger.exception("nanami_persona: 判官这一轮出错（不影响聊天）")
        ok = self._toolkit.her(said, chat_key or self._chat_key)
        if settings.log_trace:
            logger.info(
                "nanami_persona: 回写%s（%s字）chat=%s",
                "成功" if ok else "失败", len(said), chat_key or self._chat_key,
            )

    def guard_text(self, text: str) -> str | None:
        settings = self._settings_store.get() if self._settings_store else None
        if settings is None or self._toolkit is None or not settings.guard.enabled:
            return None
        if len(text) > settings.guard.max_chars:
            return None
        current = text
        touched: list[str] = []

        # ① 「……那种」后置尾巴：纯句法，直接删（只减不加）
        trimmed_kind = self._trim_trailing_kind(current)
        if trimmed_kind is not None:
            current = trimmed_kind
            touched.append("trailing_kind")

        # ② 公式化共情的"盖章"：只删那一小截（删完句子还成立）
        trimmed_empathy = self._trim_empathy(current)
        if trimmed_empathy is not None:
            current = trimmed_empathy
            touched.append("empathy")

        # ③ 系统句／登记表：只记日志（观测用，不改词）
        self._report_assistant_ish(current)

        # ③ 句首垫字（上游 lead-trim）：正则触发、大模型判、只删逗号式。
        trimmed = self._trim_lead(current, settings)
        if trimmed is not None:
            current = trimmed
            touched.append("lead_trim")

        # ④ 字面规则（tic-guard + 离声词表）：纯本地，不花模型。
        result = self._toolkit.guard([current])
        if result is not None and result.fixes:
            if settings.guard.mode != "clean":
                logger.info(
                    "nanami_persona: 守卫命中（report 模式，不改词）%s",
                    result.fixes[0].get("tics"),
                )
            else:
                current = result.texts[0] if result.texts else current
                touched.append(str(result.fixes[0].get("tics") or "tic"))

        return current if current != text else None

    def _trim_trailing_kind(self, text: str) -> str | None:
        """把"……，X 那种。"的尾巴删掉。

        "我爸爸也写过一点，益智游戏那种。"——中文里这么收句是模型在补说明书，
        不是她在说话。删掉尾巴整句仍然成立（"我爸爸也写过一点。"），所以这一步**只减不加**。
        """
        match = _TRAILING_KIND_RE.search(text)
        if not match:
            return None
        head = text[: match.start()].rstrip()
        if len([ch for ch in head if ch not in "。！？…、，,.!? "]) < 4:
            return None  # 删完就剩一口气，那还是留着
        body = head + ("。" if text.rstrip().endswith(("。", "！", "？")) else "")
        logger.info("nanami_persona: 删掉「……那种」的尾巴：%s -> %s", _preview(text), _preview(body))
        return body

    def _trim_empathy(self, text: str) -> str | None:
        """删掉公式化共情的"盖章"，保留后面具体的内容。

        「啊……我也有过，打一局排位打到忘了吃饭。」→「啊……打一局排位打到忘了吃饭。」
        「……那个我懂。」→ 整句没剩下什么，那就返回 None（交给"丢弃"那条路）。
        只减不加——这是本插件守卫生来的纪律。
        """
        body = _EMPATHY_STAMP_RE.sub("", text, count=1)
        body = _EMPATHY_INLINE_RE.sub("", body, count=1)
        body = body.strip()
        if body == text.strip():
            return None
        if len([ch for ch in body if ch not in "。！？…、，,.!? "]) < 4:
            return None
        logger.info("nanami_persona: 删掉公式化共情的盖章：%s -> %s", _preview(text), _preview(body))
        return body

    @staticmethod
    def _is_pure_empathy(text: str) -> bool:
        """整条只剩一句"盖章"（"啊……那个我懂。"）——那就别发。

        为什么不整条留着：一句纯粹的"我懂"比不说话更像客服。宁可这一条不出声。
        """
        source = str(text or "")
        if not _EMPATHY_STAMP_RE.match(source):
            # 没命中"盖章"就谈不上"整条只是共情"——「……嗯？」这种纯应声必须留着
            return False
        body = _EMPATHY_STAMP_RE.sub("", source, count=1).strip()
        body = body.strip("。！？…、，,.!? ")
        return len(body) < 2

    def _report_assistant_ish(self, text: str) -> None:
        """系统句／登记表只记日志，不改词——改词等于替她重说，那是生硬。"""
        hits = [why for needle, why in _ASSISTANT_ISH if needle in text]
        if hits:
            logger.info("nanami_persona: 助手腔信号（%s）：%s", "；".join(hits), _preview(text))

    def _trim_lead(self, text: str, settings: Settings) -> str | None:
        """句首「嗯，」是垫字就删掉——判交给大模型（上游用云模型 83%）。"""
        if not settings.judge.enabled or not settings.judge.lead_trim:
            return None
        if self._judge is None:
            return None
        if not _LEAD_FILLER_RE.match(text):
            return None
        if len(text) <= 6:  # 整句就一个"嗯，……"，删了什么都不剩
            return None
        verdict = self._judge.lead_filler(text)
        if not verdict.decided or not verdict.flag:
            return None
        trimmed = _LEAD_FILLER_RE.sub("", text, count=1).lstrip()
        logger.info("nanami_persona: 删掉句首垫字 %s -> %s", _preview(text), _preview(trimmed))
        return trimmed or None

    def should_drop_bubble(self, text: str, turn_id: int | None) -> bool:
        """AI 味守卫的 clean 档：命中就不发这条（绝不替换成别的说法）。

        只在**这一轮已经发过东西**时才丢弃——不然她整轮沉默，比发一句客服腔更糟。

        ⚠️ 这里曾经还有一条"编造过去"的丢弃规则（我判她说家人是编的）——
        **那是错的**：原典 e08_009_033 她真的讲过爸爸写程序。已撤（见 docs 的 §J2）。
        """
        settings = self._settings_store.get() if self._settings_store else None
        if settings is None or self._judge is None:
            return False
        if not settings.judge.enabled or settings.judge.ai_flavor != "clean":
            return False
        if not str(text or "").strip():
            return False
        with self._lock:
            forwarded = self._forwarded_by_turn.get(turn_id, 0) if turn_id is not None else 0
        if self._is_pure_empathy(text):
            # 整条只有"我懂/我也有过"这种盖章：**连第一条也不发**——一句纯盖章比不说话更像客服
            logger.info("nanami_persona: 整条只是公式化共情，这条不发：%s", _preview(text))
            return True
        if forwarded <= 0:
            # 这一轮还没发过东西：第一条永远留着（丢了就整轮哑了）。**也就不用去判它**，
            # 省一次模型调用——clean 档是同步判，省下的就是用户等的那一秒。
            return False
        if self._turn_count <= 1 and _FIRST_TURN_ASK_RE.search(text):
            # 第一次见面就问他"你在做什么"——原典里她不会这么开场（主人真机抓过）
            logger.info("nanami_persona: 第一轮盘问对方，这条不发：%s", _preview(text))
            return True
        if settings.judge.ai_flavor != "clean":
            return False
        verdict = self._judge.ai_flavor(text)
        if not verdict.decided:
            logger.info("nanami_persona: 判官没判出来（%s），这条照发：%s", verdict.reason, _preview(text))
            return False
        if not verdict.flag:
            return False
        logger.info("nanami_persona: 判出 AI 味，这条不发：%s", _preview(text))
        return True

    def note_forwarded(self, turn_id: int | None) -> None:
        if turn_id is None:
            return
        with self._lock:
            self._forwarded_by_turn[turn_id] = self._forwarded_by_turn.get(turn_id, 0) + 1
            if len(self._forwarded_by_turn) > 32:
                self._forwarded_by_turn.pop(next(iter(self._forwarded_by_turn)), None)

    def judge_round(self, bubbles: list[str]) -> None:
        """答完之后再判（report 档）：只记日志，不挡话、不加延迟。

        顺便把判官的命中率写进日志，方便看"她这个月长成什么样了"。
        """
        settings = self._settings_store.get() if self._settings_store else None
        if settings is None or self._judge is None or not settings.judge.enabled:
            return
        if settings.judge.ai_flavor == "off" or not bubbles:
            return
        if settings.judge.ai_flavor == "clean":
            return  # clean 档在发送前就判过了，别问两遍
        # 一轮问一次（把气泡拼起来判），而且**丢到后台**：report 档不该让下一句等她
        joined = chr(10).join(line for line in bubbles if line.strip())
        preview = " ｜ ".join(_preview(line, 30) for line in bubbles[:3])
        count = len(bubbles)

        def report(verdict) -> None:
            # 三态：命中／干净／没判出来。"没判出来"必须单独说——
            # 判官超时的时候 flag 也是 False，混成"没有 AI 味"就等于把没检查说成很干净。
            if verdict.label == "命中":
                logger.info("nanami_persona: 判官标记这一轮像 AI 味（%s 条）：%s", count, preview)
            elif verdict.label == "没判出来":
                logger.info("nanami_persona: 判官这一轮没判出来（%s）——不代表干净", verdict.reason)
            elif settings.log_trace:
                logger.info("nanami_persona: 判官这一轮 %s 条，干净", count)

        self._judge.ai_flavor_async(joined, on_result=report)

    def flush_all(self) -> None:
        settings = self._settings_store.get() if self._settings_store else None
        if settings is not None:
            self._flush_reply(settings)

    def shutdown(self) -> None:
        try:
            self.flush_all()
        except Exception:
            logger.exception("nanami_persona: 关闭时回写失败")


# ── 工具函数 ───────────────────────────────────────────────────────────
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
) -> str:
    """拼这一轮要插进提示词的那一段：先卡片（状态），后记忆（事实）。

    两段分开的原因：卡片只写**她此刻的状态**，记忆写的是**世界上的事实**（他是谁）。
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
            parts.append(f"【我记得】\n- 他叫：{name}（他自己说的）")
    if not parts:
        return ""
    block = "\n\n".join(parts)
    head = str(settings.mind.marker or "").strip()
    body = f"{head}\n{block}".strip() if head else block
    note = _STATE_ONLY_NOTE if not first_turn else f"{_STATE_ONLY_NOTE}\n{_FIRST_TURN_NOTE}"
    # 卡片 = 状态 + "这不是台词"的说明 + "动作落地成什么形状" + （第一轮的）不许问句
    return f"{body}\n\n{note}\n{_ACTION_SHAPE}"


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
