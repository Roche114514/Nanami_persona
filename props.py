"""此刻的内在倾向与想做的事——**完全从心理学模型状态自然推导，不硬塞固定剧本**。

职责分工：

    【心理模型】卡片（精力 / 疲劳压力 / 情绪 / agenda / 行动倾向） ──┐
    【关系闸门】trust                                                   ──┼─→ 当下自然意愿（类别 + 理由）
    【当下语境】对方的邀请 / 休息线索（排除归属他自己的陈述）              ──┘

核心原则：
1. 不再为任何角色硬塞「日常默认活动」（比如「在玩游戏 / 手上这局还没玩完」）；
   状态平稳且无特殊触发时，由 `agenda` / 精力与心情推导出心理倾向，或者保持开放自主（`autonomous`），
   让模型按当前语境自然发挥，而不是被迫“永远手里拿着一局没玩完的游戏”。
2. `energy=None`（节点不可用 / 未算出卡片）时不编造任何状态，直接返回空 `Activity()`。
3. 只有在 `profile.yaml` 中显式配置了 `props.default_activity` 时才使用静态默认值。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

_FILE = "state/props.json"

# 心理推导出的内在倾向类别（不硬塞人物设定道具）
SLEEP = "sleep"
TOGETHER = "together"
WITHDRAW = "withdraw"
CURIOUS = "curious"
ASSERT = "assert"
OPEN = "open"
AUTONOMOUS = "autonomous"
# 兼容保留旧常量名，避免外部代码 import 报错
GAME = AUTONOMOUS

_DEFAULT_TEXT = {
    SLEEP: "有点困，想歇着睡一会儿",
    TOGETHER: "愿意靠近些，或一起做点什么",
    WITHDRAW: "先把自己收回来",
    CURIOUS: "顺着在意的那处接着想下去",
    ASSERT: "想把自己的判断说清、守住立场",
    OPEN: "状态平稳，愿意自然搭话、随心意待着",
    AUTONOMOUS: "没有非做不可的事，按此刻自己的心意决定",
}
_WHY = {
    SLEEP: "精力偏低，需要休息",
    TOGETHER: "精力尚可且对方有同行/陪伴契机",
    WITHDRAW: "感受到否定或防御触发，先收紧边界",
    CURIOUS: "探索与厘清倾向占主导",
    ASSERT: "胜任需要与自尊保护",
    OPEN: "关系连结需要平稳偏向靠近",
    AUTONOMOUS: "心境与精力平稳，由角色自主决定",
}

_SLEEP_HINTS = ("睡", "困", "熬夜", "歇", "好累", "撑不住")
_TOGETHER_HINTS = ("一起", "陪我", "带你", "出门", "去哪", "逛逛", "走走")

# 生成类（可选）：模型只许把已经算好的倾向说得自然一点，**不许偷换类别**。
_KEEP = {
    SLEEP: ("睡", "躺", "歇", "闭眼", "休息"),
    TOGETHER: ("一起", "陪", "跟", "带"),
    WITHDRAW: ("收", "不接", "停", "不语", "静"),
    CURIOUS: ("想", "弄清", "琢磨", "看", "问"),
    ASSERT: ("判断", "守", "坚持", "说清", "立场"),
    OPEN: ("待", "聊", "看", "随意", "搭话"),
    AUTONOMOUS: ("随意", "自在", "看情况", "歇", "聊"),
}

_SYSTEM = (
    "你在替一个角色把她**由心理状态自然推出来的当下意愿**表达成一句极短的意向。只输出 JSON：{\"prop\": \"...\"}。\n"
    "- 不超过 14 个字，口语，一句短语，不要写成台词；\n"
    "- **不许凭空硬塞固定道具或未发生过的剧情**；\n"
    "- 不许改变下面给出的意愿方向（比如累了要休息，就不要写成突然去忙别的事）；\n"
    "- 不许写地点与陈设、别人的名字、家人与学校、日期与过去的时间线；\n"
    "- 不许在句尾加「，……类的」「，……那种」这类后置修饰。\n"
)

_BAD_PROP_WORDS = (
    "核对", "整理", "编目", "编纂", "编写", "比对", "统计",
    "图鉴", "年份", "资料", "目录", "清单", "档案",
)
_BAD_PROP_TAIL = ("类的", "那种", "之类")


@dataclass(frozen=True)
class Activity:
    """此刻内在倾向：`what` 是倾向类别，`text` 是简短意向，`why` 是心理学推导依据。"""

    what: str = ""
    text: str = ""
    why: str = ""
    turns: int = 0

    @property
    def empty(self) -> bool:
        return not self.text


def _derive_from_psychology(
    *,
    energy: float,
    agenda: str,
    mood_v: float | None,
) -> Activity:
    """当无紧急触发且角色包未强行指定默认活动时，从心理学状态推导出自然倾向。"""
    ag = str(agenda or "").strip()
    if ag == "want_hide":
        return Activity(what=WITHDRAW, text=_DEFAULT_TEXT[WITHDRAW], why="当前倾向回避与防御（agenda=want_hide）")
    if ag == "want_understand":
        return Activity(what=CURIOUS, text=_DEFAULT_TEXT[CURIOUS], why="心里有想弄明白的事（agenda=want_understand）")
    if ag == "want_win":
        return Activity(what=ASSERT, text=_DEFAULT_TEXT[ASSERT], why="自尊或胜任感被触动（agenda=want_win）")
    if ag == "want_company":
        return Activity(what=OPEN, text=_DEFAULT_TEXT[OPEN], why="情绪与关系需要倾向保持连结（agenda=want_company）")
    if mood_v is not None and mood_v < -0.25:
        return Activity(
            what=WITHDRAW,
            text="心情偏沉，想稍微安静一会儿",
            why=f"情绪效价偏低（moodV={mood_v:.2f}）",
        )
    return Activity(
        what=AUTONOMOUS,
        text=_DEFAULT_TEXT[AUTONOMOUS],
        why=f"精力 {energy:.2f}，心境平稳，无需硬塞固定活动",
    )


def derive_activity(
    *,
    energy: float | None = 0.0,
    action: str = "",
    appraisal: str = "",
    his_text: str = "",
    agenda: str = "",
    mood_v: float | None = None,
    need: float | None = None,
    wound: float | None = None,
    default_activity: dict[str, str] | None = None,
    custom_rules: tuple[dict[str, Any], ...] | list[dict[str, Any]] = (),
) -> Activity:
    """状态（精力/疲劳/伤口/agenda）+ 对话线索 → 此刻自然意愿。**纯规则，可测。**

    `energy=None` 表示这一轮未算出卡片（node 不可用或超时），返回空 `Activity()`，绝不编造状态。
    """
    if energy is None:
        return Activity()
    act = str(action or "")
    mood = str(appraisal or "")
    body = str(his_text or "")
    value = float(energy)

    # ① 被冒犯 / 伤口较深 / 明确防御倾向 → 收回边界
    if (
        "顶回去" in act
        or "驳论" in act
        or "被骂" in mood
        or "被看轻" in mood
        or agenda == "want_hide"
        or (wound is not None and float(wound) >= 0.75)
    ):
        return Activity(what=WITHDRAW, text=_DEFAULT_TEXT[WITHDRAW], why=_WHY[WITHDRAW])

    about_him = any(key in mood for key in ("他在讲他", "他自己的事", "他也没劲"))

    # 角色包声明的自定义规则（若显式配置，优先于内置推导）
    for rule in custom_rules or ():
        if not isinstance(rule, dict):
            continue
        min_e = rule.get("min_energy")
        max_e = rule.get("max_energy")
        if min_e is not None and value < float(min_e):
            continue
        if max_e is not None and value > float(max_e):
            continue
        hints = [str(h) for h in (rule.get("hints") or ()) if str(h)]
        on_action = [str(a) for a in (rule.get("on_action") or ()) if str(a)]
        if on_action and not any(a in act for a in on_action):
            continue
        if hints:
            if about_him and not rule.get("allow_about_him"):
                continue
            if not any(h in body for h in hints):
                continue
        r_what = str(rule.get("what") or "custom").strip()
        r_text = str(rule.get("text") or "").strip()
        r_why = str(rule.get("why") or "").strip()
        if r_text:
            return Activity(what=r_what, text=r_text, why=r_why)

    # ② 精力见底 / 疲劳压力高 / 半句·睡着 → 休息需求
    if (
        value <= 0.32
        or "半句" in act
        or "睡着" in act
        or (need is not None and float(need) >= 0.72)
    ):
        why = f"精力 {value:.2f}，撑不住了" if value <= 0.32 else (
            "这一轮已经半句半句地停下了" if ("半句" in act or "睡着" in act) else "休息压力升高，有点犯困"
        )
        return Activity(what=SLEEP, text=_DEFAULT_TEXT[SLEEP], why=why)

    # ③ 精力尚可 + 对方提出一起 / 陪伴契机
    if value >= 0.62 and any(word in body for word in _TOGETHER_HINTS):
        return Activity(what=TOGETHER, text=_DEFAULT_TEXT[TOGETHER], why=_WHY[TOGETHER])

    # ④ 对方把话题带到睡/困/歇（排除他在说自己）
    if not about_him and any(word in body for word in _SLEEP_HINTS):
        return Activity(what=SLEEP, text=_DEFAULT_TEXT[SLEEP], why="对方刚刚提到困／休息")

    # ⑤ 仅当用户在角色包里显式填了默认覆盖活动时，才使用它
    if default_activity and str(default_activity.get("text") or "").strip():
        return Activity(
            what=str(default_activity.get("what") or "custom").strip(),
            text=str(default_activity.get("text") or "").strip(),
            why=str(default_activity.get("why") or "").strip(),
        )

    # ⑥ 默认：从心理学模型的 agenda / moodV / energy 自然推导，绝不硬塞固定事件
    return _derive_from_psychology(energy=value, agenda=agenda, mood_v=mood_v)


class ActivityStore:
    """按会话存"她此刻想做什么"。类别没变就沿用，变了才换——免得她来回跳。"""

    def __init__(self, state_root: Path) -> None:
        self._path = Path(state_root) / _FILE
        self._lock = threading.Lock()
        self._cache: dict[str, dict] | None = None

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> dict[str, dict]:
        if self._cache is not None:
            return self._cache
        data: dict[str, dict] = {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data = {str(key): value for key, value in raw.items() if isinstance(value, dict)}
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            logger.warning("nanami_persona: %s 读不出来，当作空的", self._path)
        self._cache = data
        return data

    def _save(self, data: dict[str, dict]) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError:
            logger.warning("nanami_persona: 记不住素材（写 %s 失败）", self._path)

    def get(self, chat_key: str) -> Activity:
        with self._lock:
            row = self._load().get(str(chat_key)) or {}
        return Activity(
            what=str(row.get("what") or ""),
            text=str(row.get("text") or "").strip(),
            why=str(row.get("why") or ""),
            turns=int(row.get("turns") or 0),
        )

    def put(self, chat_key: str, activity: Activity) -> Activity:
        with self._lock:
            data = self._load()
            data[str(chat_key)] = {
                "what": activity.what,
                "text": activity.text,
                "why": activity.why,
                "turns": 0,
                "at": time.time(),
            }
            self._save(data)
        return activity


class ActivityMaker:
    """把"想做什么"算出来；必要时让模型把措辞说得具体一点（默认不让）。

    `mode`：
        rules —— 只用规则给的短句（默认）。不花模型、不会编、也不会被照抄。
        model —— 允许模型在**同一类别内**把话说具体一点；换了类别就丢掉。
    """

    def __init__(
        self,
        state_root: Path,
        *,
        enabled: bool = True,
        mode: str = "rules",
        timeout: float = 2.0,
        adapter_provider: Callable[[], Any] | None = None,
        default_activity: dict[str, str] | None = None,
        custom_rules: tuple[dict[str, Any], ...] | list[dict[str, Any]] = (),
    ) -> None:
        self._store = ActivityStore(state_root)
        self._enabled = bool(enabled)
        self._mode = str(mode or "rules").strip().lower()
        if self._mode not in ("rules", "model"):
            self._mode = "rules"
        self._timeout = max(0.3, float(timeout))
        self._adapter_provider = adapter_provider or _default_adapter
        self._default_activity = dict(default_activity) if default_activity else None
        self._custom_rules = tuple(custom_rules or ())
        self._adapter: Any = None
        self._lock = threading.Lock()
        self._stats = {"rules": 0, "model": 0, "fail": 0}

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def stats(self) -> dict[str, int]:
        with self._lock:
            return dict(self._stats)

    def peek(self, chat_key: str) -> Activity:
        if not self._enabled:
            return Activity()
        return self._store.get(chat_key)

    def ensure(
        self,
        chat_key: str,
        *,
        energy: float = 0.0,
        action: str = "",
        appraisal: str = "",
        his_text: str = "",
        agenda: str = "",
        mood_v: float | None = None,
        need: float | None = None,
        wound: float | None = None,
    ) -> Activity:
        """算出此刻从心理状态推出来的内在倾向。**永远不抛异常**。"""
        if not self._enabled or not str(chat_key or "").strip():
            return Activity()
        base = derive_activity(
            energy=energy,
            action=action,
            appraisal=appraisal,
            his_text=his_text,
            agenda=agenda,
            mood_v=mood_v,
            need=need,
            wound=wound,
            default_activity=self._default_activity,
            custom_rules=self._custom_rules,
        )
        with self._lock:
            self._stats["rules"] += 1
        if self._mode == "rules":
            return self._store.put(chat_key, base)
        wording = self._ask(base)
        if not wording:
            with self._lock:
                self._stats["fail"] += 1
            return self._store.put(chat_key, base)
        with self._lock:
            self._stats["model"] += 1
        return self._store.put(
            chat_key,
            Activity(what=base.what, text=wording, why=base.why),
        )

    # ── 措辞（可选）────────────────────────────────────────────────────
    def _ask(self, base: Activity) -> str:
        adapter = self._get_adapter()
        if adapter is None:
            return ""
        user = "\n".join(
            [
                f"【她此刻要做的那一类】{_DEFAULT_TEXT.get(base.what, base.text)}（{base.why}）",
                "只在这个类别里说得具体一点。",
            ]
        )
        result: dict[str, Any] = {}
        done = threading.Event()

        def work() -> None:
            try:
                response = adapter.chat(
                    messages=[
                        {"role": "system", "content": _SYSTEM},
                        {"role": "user", "content": user},
                    ],
                    stream=False,
                    temperature=0.5,
                    max_tokens=48,
                    response_format={"type": "json_object"},
                )
                content = str(getattr(adapter.decode_response(response), "content", "") or "")
                result["value"] = _parse_prop(content, base.what)
            except Exception as exc:  # noqa: BLE001 - 措辞坏了不许影响聊天
                result["error"] = f"{type(exc).__name__}: {exc}"
            finally:
                done.set()

        thread = threading.Thread(target=work, name="nanami-prop", daemon=True)
        thread.start()
        if not done.wait(self._timeout):
            logger.info("nanami_persona: 措辞超时（%.1fs），用规则给的那句", self._timeout)
            return ""
        if "error" in result:
            logger.warning("nanami_persona: 措辞失败：%s", result["error"])
            return ""
        return str(result.get("value") or "")

    def _get_adapter(self) -> Any:
        with self._lock:
            if self._adapter is not None:
                return self._adapter
        try:
            adapter = self._adapter_provider()
        except Exception:
            logger.exception("nanami_persona: 措辞拿不到 adapter")
            adapter = None
        with self._lock:
            self._adapter = adapter
        return adapter


def _parse_prop(content: str, what: str = "") -> str:
    """从模型回复里抠出那一句；**换了类别就丢掉**（那是它在替她决定她想做什么）。"""
    body = str(content or "").strip()
    if not body:
        return ""
    text = ""
    try:
        payload = json.loads(body[body.find("{"): body.rfind("}") + 1])
        if isinstance(payload, dict):
            text = str(payload.get("prop") or payload.get("text") or "").strip()
    except (ValueError, TypeError):
        text = ""
    if not text:
        text = body.strip().strip('"“”「」').strip()
    text = " ".join(text.split())
    if not text or len(text) > 20:
        return ""
    if text.endswith("：") or text.endswith(":"):
        return ""
    if any(word in text for word in _BAD_PROP_WORDS):
        return ""
    if any(text.endswith(tail) or ("，" + tail) in text for tail in _BAD_PROP_TAIL):
        return ""
    keep = _KEEP.get(str(what or ""))
    if keep and not any(word in text for word in keep):
        logger.info("nanami_persona: 模型换了类别（她要做的是 %s，它写成 %s），丢掉", what, text)
        return ""
    return text


def _default_adapter() -> Any:
    """宿主正在用的那个 adapter（和 `judge.py` 同一条路，不写死端点）。"""
    from application.runtime.context import try_get_app_runtime

    runtime = try_get_app_runtime()
    manager = getattr(runtime, "llm_manager", None) if runtime is not None else None
    return getattr(manager, "llm_adapter", None)


def block_of(activity: Activity) -> str:
    """拼成提示词里那一小段：**从心理状态自然推出来的意愿 + 为什么**。"""
    if activity is None or activity.empty:
        return ""
    why = f"（{activity.why}）" if activity.why else ""
    return (
        "【此刻·内在意愿】\n"
        f"- {activity.text}{why}\n"
        "- （这是由精力、情绪、防御与需要从心理模型自然推导出的结果，不是硬塞的固定差事："
        "按自己此刻真正的心情决定做什么、说什么即可。）"
    )
