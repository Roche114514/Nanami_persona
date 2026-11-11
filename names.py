"""「他是谁」——从用户自己的话里学称呼，学到的才记住。

为什么不是一上来就叫「主人」：原典语料里她**一次都没**这么叫过人（`「主人」` 在她 2379 行
台词里 0 行）。她本来就不认识对面这个人，所以：
    ① 没自报过 → 她只用「你」，不硬套任何称呼；
    ② 自报过（「叫我小黑」「我叫张三」）→ 记下来，之后才用。

「记」这件事落在 `<state_root>/state/person.json`，按会话键分开（不同会话各记各的）。
这是**记忆**，不是状态机的一部分：它不会因为她累了/心情变了而消失，也不会被 `mind.mjs` 覆盖。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_FILE = "state/person.json"

# 太常见的"名字"多半不是名字：宁可少记，也不要让她管人叫「学生」
_STOPWORDS = frozenset(
    {
        "学生", "新手", "小白", "普通人", "人类", "玩家", "观众", "路人", "一个", "个人",
        "你的", "我的", "谁", "什么", "啥", "名字", "本人", "自己", "大家", "你们", "这里",
        "新来的", "陌生人", "成年人", "大学生", "高中生", "上班族", "打工人",
    }
)

# 中文：把"叫/称呼"后面那截抓出来。允许"你可以叫我X""以后就叫我X吧"这类前饰。
_ZH_PATTERNS = (
    re.compile(
        r"(?:以后|之后|往后|今后|就|请|你)?(?:可以|能|要|该|随便)?"
        r"(?:叫我|喊我|称呼我|管我叫)\s*([^\s，。！？、；：,.!?;:「」『』（）()~～…]{1,12})"
    ),
    re.compile(r"我(?:的)?名字(?:是|叫)\s*([^\s，。！？、；：,.!?;:「」『』（）()~～…]{1,12})"),
    re.compile(r"我(?:叫)\s*([^\s，。！？、；：,.!?;:「」『』（）()~～…]{1,12})"),
    # 「你就叫我「猫」吧」——带引号的写法
    re.compile(r"(?:叫我|喊我|称呼我)\s*[「『\"“']([^」』\"”']{1,12})[」』\"”']"),
)

# 英文（她也在英文语料里有台词，用户可能混着说）
_EN_PATTERNS = (
    re.compile(r"\bcall me\s+([A-Za-z][A-Za-z .\-']{0,19})", re.IGNORECASE),
    re.compile(r"\bmy name(?:'s| is)\s+([A-Za-z][A-Za-z .\-']{0,19})", re.IGNORECASE),
)

# 「我是X」：只在整句很短、且 X 不像普通名词时才当名字
_ZH_SELF = re.compile(r"^我(?:就)?是\s*([\u4e00-\u9fa5A-Za-z·]{2,4})[。！!～~]?$")

# 「我是程序员」不是自我介绍名字——那是**职业**。实测踩过：第一次对话里用户说"我是程序员"，
# 插件把它记成"他叫程序员"，之后每轮都这么叫。宁可漏记一个名字，也不能叫错人。
_ROLE_WORDS = frozenset({
    "程序员", "工程师", "学生", "大学生", "高中生", "社畜", "上班族", "打工人", "老师", "医生",
    "护士", "律师", "警察", "编辑", "设计师", "画师", "作家", "主播", "玩家", "萌新", "新手",
    "路人", "网友", "人类", "大人", "小孩", "老外", "宅", "宅男", "废柴", "普通人",
})
_ROLE_SUFFIX = ("员", "师", "工", "者", "手", "家", "生", "族", "党", "控")


def _looks_like_role(name: str) -> bool:
    """粗略判断"这个人称是他的职业/属性，不是名字"。"""
    if name in _ROLE_WORDS:
        return True
    return len(name) >= 3 and name.endswith(_ROLE_SUFFIX)


# 明确不要某个称呼：「别叫我主人」「不要喊我老板」
_ZH_REJECT = re.compile(
    r"(?:别|不要|不用|不许|别再)(?:叫我|喊我|称呼我)\s*([^\s，。！？、；：,.!?;:「」『』（）()~～…]{1,12})"
)

_TAIL_NOISE = "吧啊呀哦喔嘛啦咯呢的呗"

# 「叫我小林就行」「叫我阿七就好啦」——后缀不是名字的一部分，得先切掉再存。
# 顺序有讲究：长的先切，否则「就行啦」会被「啦」先咬掉一截。
_SUFFIX_CUT = re.compile(
    r"(?:就行了|就行啦|就行了啦|就可以|就好啦|都行|即可|就行|就好|好了|吧|啊|呀|哦|喔|嘛|啦|咯|呢|的|呗).*$"
)


@dataclass(frozen=True)
class LearnedName:
    name: str
    source: str = ""
    at: float = 0.0


def _clean(candidate: str) -> str:
    text = str(candidate or "").strip()
    # 「叫我小黑吧」→「小黑」；「叫我小林就行」→「小林」；「叫我小黑呢」这种多半是反问，整条不要
    if any(text.endswith(ch) for ch in "呢吗"):
        return ""
    text = _SUFFIX_CUT.sub("", text)
    text = text.strip(_TAIL_NOISE)
    text = text.strip(" ·.-'")
    if not text or len(text) > 20:
        return ""
    if text in _STOPWORDS:
        return ""
    # 只剩标点/数字的不算名字
    if not re.search(r"[\u4e00-\u9fa5A-Za-z]", text):
        return ""
    return text


def extract_name(text: str) -> str | None:
    """从用户这句话里取"他希望被怎么叫"；取不到返回 None。"""
    body = str(text or "").strip()
    if not body:
        return None
    # 先看一眼是不是"别这么叫我"——那种句子里也含「叫我」，不排除会记成反的
    rejection = extract_rejection(body)
    for pattern in _ZH_PATTERNS:
        for match in pattern.finditer(body):
            name = _clean(match.group(1))
            if name and name != rejection:
                return name
    for pattern in _EN_PATTERNS:
        match = pattern.search(body)
        if match:
            name = _clean(match.group(1))
            if name:
                return name
    if len(body) <= 12:
        match = _ZH_SELF.match(body)
        if match:
            name = _clean(match.group(1))
            if name and not _looks_like_role(name):
                return name
    return None


def extract_rejection(text: str) -> str | None:
    """「别叫我主人」→ 取回被拒的那个称呼。"""
    match = _ZH_REJECT.search(str(text or ""))
    if not match:
        return None
    return _clean(match.group(1)) or None


class NameMemory:
    """按会话记住"他希望被怎么叫"。文件很小，读写都加锁。"""

    def __init__(self, state_root: Path, *, enabled: bool = True) -> None:
        self._path = Path(state_root) / _FILE
        self._enabled = bool(enabled)
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
            logger.warning("nanami_persona: 记不住称呼（写 %s 失败）", self._path)

    def get(self, chat_key: str) -> LearnedName | None:
        with self._lock:
            row = self._load().get(str(chat_key))
        if not row:
            return None
        name = str(row.get("name") or "").strip()
        if not name:
            return None
        return LearnedName(
            name=name,
            source=str(row.get("source") or ""),
            at=float(row.get("at") or 0.0),
        )

    def remember(self, chat_key: str, name: str, source: str = "") -> LearnedName:
        clean = _clean(name)
        if not clean:
            raise ValueError("empty name")
        with self._lock:
            data = self._load()
            data[str(chat_key)] = {"name": clean, "source": str(source or ""), "at": time.time()}
            self._save(data)
        logger.info("nanami_persona: 记住称呼=%s（来源：%s）", clean, source or "未记录")
        return LearnedName(name=clean, source=str(source or ""), at=time.time())

    def forget(self, chat_key: str) -> bool:
        with self._lock:
            data = self._load()
            if str(chat_key) not in data:
                return False
            data.pop(str(chat_key), None)
            self._save(data)
        logger.info("nanami_persona: 忘掉这个会话的称呼（用户要求别这么叫）")
        return True

    def observe(self, chat_key: str, user_text: str) -> LearnedName | None:
        """读一句用户发言：学到新称呼就记下；被要求别叫了就忘掉。

        返回**这一句之后**她对这人的称呼（可能是 None = 还是不知道他是谁）。
        """
        if not self._enabled:
            return self.get(chat_key)
        rejection = extract_rejection(user_text)
        if rejection:
            current = self.get(chat_key)
            if current is not None and (current.name == rejection or rejection in current.name):
                self.forget(chat_key)
                return None
        name = extract_name(user_text)
        if name:
            self.remember(chat_key, name, source=_snippet(user_text))
            return self.get(chat_key)
        return self.get(chat_key)


def _snippet(text: str, limit: int = 24) -> str:
    body = str(text or "").replace("\n", " ").strip()
    return body if len(body) <= limit else body[:limit] + "…"
