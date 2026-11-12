"""她此刻想做什么、正在做什么——**由心理模型算，不靠随机抽取**。

2026-11-12 主人两次纠正，这条路才走对：

第一次（`CANON-AUDIT §T`）：卡片只给意图（"想说我要做的那件事"），不给内容；
内容空着，模型就去人格壳里抓例句/道具当台词。所以我加了一个"素材生成器"。

第二次（`§V`）：那个生成器**其实是在替她决定她想做什么**——
"随便扔一个她可能在做的"，还会把她的生活压成一句"在玩游戏"。
主人的原话：「她想去睡一会儿时，不是去树荫下睡一会儿这种照抄台词，
而是**她真的想去睡一会儿**，然后她才会说去睡一会儿。」

所以职责改成：

    【心理模型】卡片（精力／心里／这一轮我只想）  ──┐
    【她的性子】困不困、想不想玩、要不要热闹        ──┼─→ 她想做什么（类别 + 理由）
    【他说的话】他问什么、提到什么                  ──┘        │
                                                                 ↓
                                                    模型只负责"把它说得具体"
                                                    （不许换类别，换了就丢掉）

**类别是算出来的，不是抽出来的。** 规则只有几条，每条都能指回卡片字段或原典：

| 状态 | 她想做什么 | 依据 |
| --- | --- | --- |
| 精力 ≤ 0.32，或卡片写着「半句／睡着」 | **想睡一会儿** | 「边看书边打瞌睡……多幸福啊……」 |
| 被冒犯（卡片顶回去／他说笨） | **先把自己收回来**（什么也不做） | §5.3.2 |
| 他提到睡、困、晚上、累 | **想睡一会儿** | 同上 |
| 他提到游戏、街机、玩 | **在玩游戏** | 她说的是「玩游戏」（原典 5 行；「打游戏」0 行） |
| 精力 ≥ 0.62、他提到一起／出门／去哪 | **想跟他一起做点什么** | 「因为跟日向同学在一起，格外开心。」 |
| 其余 | **在玩游戏**（她本来就在玩） | 官方：边玩边自我介绍 |

> 默认是"在玩游戏"不是偷懒：**她本来就是一直在玩游戏的人**。
> 用「玩」不用「打」：原典里她说「玩游戏」5 行，「打游戏」「一局」各 0 行——
> 「打一局」是我自己顺着"游戏玩家"想出来的词，不是她的话。
> 会变的不是"她在做什么"，而是"她还有没有心思打"——那由精力、心情和他说的话决定。

出口的措辞只有一两句短的、**不带具体道具**（没有"树荫""图鉴""第三关"）：
道具一律会变成照抄的素材（§Q／§S／§U 三次实测）。
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

# 类别（决定她说的是哪一路话），不决定措辞
SLEEP = "sleep"
GAME = "game"
TOGETHER = "together"
WITHDRAW = "withdraw"

_DEFAULT_TEXT = {
    SLEEP: "想睡一会儿",
    GAME: "在玩游戏",
    TOGETHER: "想找他一起做点什么",
    WITHDRAW: "先把自己收回来，什么也不做",
}
_WHY = {
    SLEEP: "精力见底了，眼皮开始打架",
    GAME: "手上这局还没玩完",
    TOGETHER: "他愿意一起的时候，她更高兴",
    WITHDRAW: "被说到了，先不接",
}

_SLEEP_HINTS = ("睡", "困", "晚上", "累", "熬夜", "歇")
_GAME_HINTS = ("游戏", "街机", "过关", "通关", "主机", "玩")
_TOGETHER_HINTS = ("一起", "陪我", "带你", "出门", "去哪", "逛逛", "走走")

# 生成类（可选）：模型只许把已经算好的类别说得具体一点，**不许换类别**。
_KEEP = {
    SLEEP: ("睡", "躺", "歇", "闭眼"),
    GAME: ("局", "打", "过", "刷", "街机", "关"),
    TOGETHER: ("一起", "陪", "跟", "带"),
    WITHDRAW: ("收", "不接", "停", "不语"),
}

_SYSTEM = (
    "你在替一个角色把她**已经决定要做的那件事**说得具体一点。只输出 JSON：{\"prop\": \"...\"}。\n"
    "- 不超过 12 个字，口语，一句名词短语，不要写成台词；\n"
    "- **不许换事情**：只能在下面给出的那一类里说得更具体"
    "（比如想睡可以说成想找个地方躺一下，但不能改成打游戏）；\n"
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
    """她此刻想做什么：`what` 是类别，`text` 是入口那句话，`why` 是理由（来自卡片/他的话）。"""

    what: str = ""
    text: str = ""
    why: str = ""
    turns: int = 0

    @property
    def empty(self) -> bool:
        return not self.text


def derive_activity(
    *,
    energy: float = 0.0,
    action: str = "",
    appraisal: str = "",
    his_text: str = "",
) -> Activity:
    """状态 + 她的性子 + 他说的话 → 她此刻想做什么。**纯规则，可测。**"""
    act = str(action or "")
    mood = str(appraisal or "")
    body = str(his_text or "")
    value = float(energy if energy is not None else 0.0)

    # ① 被冒犯：这一轮她"不做"，把自己的线画出来
    if "顶回去" in act or "驳论" in act or "被骂" in mood or "被看轻" in mood:
        return Activity(what=WITHDRAW, text=_DEFAULT_TEXT[WITHDRAW], why=_WHY[WITHDRAW])
    # ② 精力见底 / 卡片写着半句·睡着 → 想睡（主人举的那个例子）
    if value <= 0.32 or "半句" in act or "睡着" in act:
        why = f"精力 {value:.2f}，撑不住了" if value <= 0.32 else "这一轮她已经半句半句地说了"
        return Activity(what=SLEEP, text=_DEFAULT_TEXT[SLEEP], why=why)
    # ③ 他自己把话题带到"睡"上
    if any(word in body for word in _SLEEP_HINTS):
        return Activity(what=SLEEP, text=_DEFAULT_TEXT[SLEEP], why="他刚提到睡／累")
    # ④ 他提到游戏：她手里的就是那一局
    if any(word in body for word in _GAME_HINTS):
        return Activity(what=GAME, text=_DEFAULT_TEXT[GAME], why="他说到游戏，她手上正好在玩")
    # ⑤ 精神好 + 他愿意一起 → 她想跟他一起做点什么
    if value >= 0.62 and any(word in body for word in _TOGETHER_HINTS):
        return Activity(what=TOGETHER, text=_DEFAULT_TEXT[TOGETHER], why=_WHY[TOGETHER])
    # ⑥ 其余：她本来就在打的那一局
    return Activity(what=GAME, text=_DEFAULT_TEXT[GAME], why=_WHY[GAME])


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
    ) -> None:
        self._store = ActivityStore(state_root)
        self._enabled = bool(enabled)
        self._mode = str(mode or "rules").strip().lower()
        if self._mode not in ("rules", "model"):
            self._mode = "rules"
        self._timeout = max(0.3, float(timeout))
        self._adapter_provider = adapter_provider or _default_adapter
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
    ) -> Activity:
        """算出她此刻想做什么。**永远不抛异常**。"""
        if not self._enabled or not str(chat_key or "").strip():
            return Activity()
        base = derive_activity(
            energy=energy, action=action, appraisal=appraisal, his_text=his_text
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
    """拼成提示词里那一小段：**她想做什么 + 为什么**（理由来自卡片，她可以说得出）。"""
    if activity is None or activity.empty:
        return ""
    why = f"（{activity.why}）" if activity.why else ""
    return (
        "【此刻·她想做的事】\n"
        f"- {activity.text}{why}\n"
        "- （这是她真的想做的，由精力、心情和他说的话算出来的，不是随便挑的："
        "他问起、或者话走到这儿，她就会照这个说；不主动播报。）"
    )
