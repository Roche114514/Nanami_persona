"""插件设置：`settings.yaml` 的读取与热加载。

设置只放在插件目录里（`plugins/nanami_persona/settings.yaml`），不写进 Shinsekai 的
`data/config/*.yaml`——那是宿主的东西，插件不该改它。按 mtime 热加载：改完保存，
下一轮就生效，不用重启聊天。（和 `state/off-voice.json` 的规矩一致。）
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

SETTINGS_FILE = "settings.yaml"

# 她的名字。目录里的角色名、消息里的 character_name 都按这个比对（去空白后完全相等）。
DEFAULT_CHARACTER = "七海千秋"


@dataclass(frozen=True)
class GuardSettings:
    """发送前守卫（③ 层）。"""

    enabled: bool = True
    # 命中后怎么处理：clean=就地改掉（只减不加）；report=只记录不改。
    mode: str = "clean"
    # 分句长度超过这个数就跳过守卫（长段落多半是她在讲一件复杂的事，规则容易误伤）。
    max_chars: int = 120

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> "GuardSettings":
        mode = str(raw.get("mode") or "clean").strip().lower()
        if mode not in ("clean", "report", "off"):
            mode = "clean"
        return GuardSettings(
            enabled=bool(raw.get("enabled", True)) and mode != "off",
            mode=mode,
            max_chars=max(8, int(raw.get("max_chars", 120))),
        )


@dataclass(frozen=True)
class MindSettings:
    """心理模型（② 层）：`mind.mjs` 的调用参数。"""

    enabled: bool = True
    node_exe: str = ""
    # 工具箱目录；留空 = 用插件自带的 toolkit 快照。
    toolkit_dir: str = ""
    # 状态根（`<root>/state/mind.json`）；留空 = `<项目根>/data/nanami`。
    state_root: str = ""
    # 每轮算卡片的超时（秒）。原平台的网关预算是 2 秒，通常 130ms 就返回。
    card_timeout: float = 4.5
    her_timeout: float = 3.0
    guard_timeout: float = 3.0
    # 一次请求里最多重试几次（只在超时/异常时重试，且只重试一次）。
    retries: int = 1
    # 卡片插进哪：system=作为一条独立的 system 消息放在最后（默认）；
    # user=拼在用户那句话前面（有些模型对末尾 system 有偏置，需要对照时用）。
    card_position: str = "system"
    # 卡片前面的标记行。留空 = 什么都不加，卡片原文进提示词。
    marker: str = ""
    # 「这句是什么言语行为」交给宿主的大模型判（原来那档是本地 3B）。
    # 打开会**每轮多一次大模型调用**（在算卡片之前，串行），换更准的 appraisal。
    percept: bool = True
    # 读句那一次的预算（秒）。远程大模型实测 0.9–2s，给太少会经常退回规则。
    # ⚠️ 上游 `toolkit/percept.mjs` 里这个上限被写死成 2500ms，本设置目前**不生效**；
    # 真正管住它的是 `card_timeout`（见上）。
    percept_timeout: float = 2.5
    # 她说完之后是否回写（`--her`）：不写回去，状态就不会因为"她说了什么"而变。
    writeback: bool = True
    # 把 mind.mjs 的 stderr（`[scene]`/`[cost]` 调试行）也打进日志。
    debug_stderr: bool = False

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> "MindSettings":
        position = str(raw.get("card_position") or "system").strip().lower()
        if position not in ("system", "user"):
            position = "system"
        return MindSettings(
            enabled=bool(raw.get("enabled", True)),
            node_exe=str(raw.get("node_exe") or "").strip(),
            toolkit_dir=str(raw.get("toolkit_dir") or "").strip(),
            state_root=str(raw.get("state_root") or "").strip(),
            card_timeout=max(0.5, float(raw.get("card_timeout", 4.5))),
            her_timeout=max(0.5, float(raw.get("her_timeout", 3.0))),
            guard_timeout=max(0.5, float(raw.get("guard_timeout", 3.0))),
            retries=max(0, min(3, int(raw.get("retries", 1)))),
            card_position=position,
            marker=str(raw.get("marker") or ""),
            percept=bool(raw.get("percept", True)),
            percept_timeout=max(0.5, min(10.0, float(raw.get("percept_timeout", 2.5)))),
            writeback=bool(raw.get("writeback", True)),
            debug_stderr=bool(raw.get("debug_stderr", False)),
        )


@dataclass(frozen=True)
class ContractSettings:
    """输出契约适配（把宿主那套"至少两条、最后一条必须 CHOICE"改成她说话的样子）。

    这些开关只改**提示词里的要求文本**，不改宿主的校验代码：
    校验只需要 `dialog` 非空 + 每条有 `character_name`/`speech`/`sprite`。
    """

    enabled: bool = True
    relax_array_min: bool = True
    max_dialog_items: int = 3
    max_speech_chars: int = 34

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> "ContractSettings":
        return ContractSettings(
            enabled=bool(raw.get("enabled", True)),
            relax_array_min=bool(raw.get("relax_array_min", True)),
            max_dialog_items=max(0, min(20, int(raw.get("max_dialog_items", 3)))),
            max_speech_chars=max(0, min(400, int(raw.get("max_speech_chars", 34)))),
        )


@dataclass(frozen=True)
class PropSettings:
    """「她此刻想做什么」：这一格由心理模型算，不靠随机抽取。

    卡片只给意图（"想说我要做的那件事"）不给内容，内容空着模型就去人格壳里抓例句当台词
    （`CANON-AUDIT §Q`／`§S`）。但**随便生成一件也是错的**（§V）——那等于替她决定她想做什么。

    所以：类别（睡／打一局／想一起／收回来）由 `props.derive_activity` 从**精力、心情、他说的话**算；
    `mode: model` 时模型只许把同一类说得具体一点，换了类别就丢掉。
    """

    enabled: bool = True
    # rules = 只按规则算**类别**、用规则给的短句（默认：不花模型、不会编、不会被照抄）
    # model = 允许模型在**同一类别内**把话说具体一点（换了类别就丢掉）
    mode: str = "rules"
    # model 档下单次最多等多久（秒）。超时就退回规则那句，聊天不等它。
    timeout: float = 2.0

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> "PropSettings":
        mode = str(raw.get("mode") or "rules").strip().lower()
        if mode not in ("rules", "model"):
            mode = "rules"
        return PropSettings(
            enabled=bool(raw.get("enabled", True)),
            mode=mode,
            timeout=max(0.3, min(10.0, float(raw.get("timeout", 2.0) or 2.0))),
        )


@dataclass(frozen=True)
class MemorySettings:
    """「他是谁」那一小块记忆（不是心理模型的一部分）。

    她一开始不知道对面是谁：只在用户自己说了「叫我 X」「我叫 X」之后才记住，之后才用。
    记下来的不只是名字，还有**她出口的称呼形状**和**多久才叫一次**——
    频率是原典读数（`docs/NANAMI-CANON-AUDIT.md §L`）；"不是同学换敬称"那一档是设计（§M）。
    """

    learn_user_name: bool = True
    # 学到之后要不要每轮把它塞进提示词（关掉就只能靠对话历史自己想起来）
    inject: bool = True
    # 记住的名字显示成什么样：他自己说过的原样（默认）
    remember_rejections: bool = True
    # 她出口的称呼后缀。原典里她对 9 个人一律叫「X同学」（可信台词 203 次）；
    # 设成空串 = 只叫名字，不带后缀。
    address_suffix: str = "同学"
    # 明确不是同学的时候用哪一档（他自报过职业、或者说过自己早不是学生）。
    # **这一档是设计、不是原典**：原典里对面全是同班，「先生／小姐／桑」在她台词里 0 命中。
    # 出口用中文写法：日语是 -さん，中文写「先生」；知道男女就用先生／小姐。
    address_honorific: str = "先生"
    # 两次称呼之间至少隔几轮。原典：4.58% 的台词带称呼 ≈ 每 22 行一次，
    # 她的 829 段发言只有 10.3% 带过 → 折成对话约 8–10 轮一次。
    address_gap_turns: int = 8

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> "MemorySettings":
        suffix = raw.get("address_suffix", "同学")
        suffix = "同学" if suffix is None else str(suffix).strip()
        honorific = raw.get("address_honorific", "先生")
        honorific = "先生" if honorific is None else str(honorific).strip()
        return MemorySettings(
            learn_user_name=bool(raw.get("learn_user_name", True)),
            inject=bool(raw.get("inject", True)),
            remember_rejections=bool(raw.get("remember_rejections", True)),
            address_suffix=suffix[:8],
            address_honorific=honorific[:8],
            address_gap_turns=max(0, min(60, int(raw.get("address_gap_turns", 8) or 0))),
        )


@dataclass(frozen=True)
class PersonaSettings:
    """人格壳注入：插件随身带 soul/chiaki-soul.md，运行时插进提示词。

    **为什么要在运行时插**：角色包（`七海千秋.char`）里那份是维基式简介（还带着错的细节），
    而插件要"在任何一台装了它的机器上都把那个自带模型换成原典版"——不能指望有人先改过
    characters.yaml、也不能指望模板刚好是重建过的。
    """

    inject: bool = True
    # 想换成别的壳就填个绝对路径；留空＝用插件自带的 soul/chiaki-soul.md
    shell_path: str = ""
    # 注入方式：auto＝**按用途分层**（每轮只带用得上的那几段：常驻 + 这一轮的场景段）；
    #          all ＝整份壳每轮都带（老行为，出问题时的逃生门）。
    # 分层规则见 `soul_route.py`，理由见 `docs/NANAMI-CANON-AUDIT.md §P`。
    route: str = "auto"


@dataclass(frozen=True)
class JudgeSettings:
    """判官：原来要本地 3B 干的活，现在交给宿主配置的那个大模型。

    `ai_flavor` 三档：
        off    不判
        report 只在**答完之后**判，记日志（不挡话、不加延迟）——默认
        clean  每条气泡发出去**之前**判，命中就不发那条（多一次模型调用，会慢）
    `lead_trim`：句首「嗯，／诶，」是垫字还是有作用——正则触发、大模型判、只删逗号式。
    """

    enabled: bool = True
    timeout: float = 4.0
    ai_flavor: str = "report"
    lead_trim: bool = False

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> "JudgeSettings":
        mode = str(raw.get("ai_flavor") or "report").strip().lower()
        if mode not in ("off", "report", "clean"):
            mode = "report"
        return JudgeSettings(
            enabled=bool(raw.get("enabled", True)) and mode != "off",
            timeout=max(0.3, min(30.0, float(raw.get("timeout", 4.0)))),
            ai_flavor=mode,
            lead_trim=bool(raw.get("lead_trim", False)),
        )


@dataclass(frozen=True)
class Settings:
    enabled: bool = True
    character_name: str = DEFAULT_CHARACTER
    # 聊天 id 前缀（`--chat` 用的那个键）：同一台机器上多开不同会话时要能分开。
    chat_key_prefix: str = "shinsekai"
    log_trace: bool = True
    mind: MindSettings = field(default_factory=MindSettings)
    guard: GuardSettings = field(default_factory=GuardSettings)
    contract: ContractSettings = field(default_factory=ContractSettings)
    memory: MemorySettings = field(default_factory=MemorySettings)
    props: PropSettings = field(default_factory=PropSettings)
    judge: JudgeSettings = field(default_factory=JudgeSettings)
    persona: PersonaSettings = field(default_factory=PersonaSettings)
    source_path: str = ""


class SettingsStore:
    """按 mtime 重新读 settings.yaml；读坏了就继续用手上这份（绝不让设置把聊天挡住）。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._mtime = -1.0
        self._settings = Settings()

    @property
    def path(self) -> Path:
        return self._path

    def get(self) -> Settings:
        try:
            mtime = os.stat(self._path).st_mtime
        except OSError:
            if self._mtime < 0:
                self._mtime = 0.0
                logger.info("nanami_persona: 没有 %s，使用内置默认设置", self._path)
            return self._settings
        if mtime == self._mtime:
            return self._settings
        self._mtime = mtime
        try:
            raw = yaml.safe_load(self._path.read_text(encoding="utf-8")) or {}
            if not isinstance(raw, dict):
                raise ValueError("settings.yaml 顶层必须是映射")
            self._settings = _parse(raw, str(self._path))
            logger.info("nanami_persona: 设置已加载 %s", self._path)
        except Exception:
            logger.exception("nanami_persona: 设置读取失败，继续用上一份 %s", self._path)
        return self._settings


def _route_mode(value: Any) -> str:
    """人格壳注入方式：只有 auto / all 两种，写错就当 auto（分层是默认，也是省 token 的那个）。"""
    mode = str(value or "auto").strip().lower()
    return mode if mode in ("auto", "all") else "auto"


def _parse(raw: dict[str, Any], source: str) -> Settings:
    character = str(raw.get("character_name") or DEFAULT_CHARACTER).strip() or DEFAULT_CHARACTER
    prefix = str(raw.get("chat_key_prefix") or "shinsekai").strip() or "shinsekai"
    return Settings(
        enabled=bool(raw.get("enabled", True)),
        character_name=character,
        chat_key_prefix=prefix,
        log_trace=bool(raw.get("log_trace", True)),
        mind=MindSettings.from_dict(dict(raw.get("mind") or {})),
        guard=GuardSettings.from_dict(dict(raw.get("guard") or {})),
        contract=ContractSettings.from_dict(dict(raw.get("contract") or {})),
        memory=MemorySettings.from_dict(dict(raw.get("memory") or {})),
        props=PropSettings.from_dict(dict(raw.get("props") or {})),
        judge=JudgeSettings.from_dict(dict(raw.get("judge") or {})),
        persona=PersonaSettings(
            inject=bool((raw.get("persona") or {}).get("inject", True)),
            shell_path=str((raw.get("persona") or {}).get("shell_path") or "").strip(),
            route=_route_mode((raw.get("persona") or {}).get("route")),
        ),
        source_path=source,
    )
