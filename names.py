"""「他是谁」——从用户自己的话里学称呼，学到的才记住。

她本来就不认识对面这个人，所以：
    ① 没自报过 → 她只用「你」，不硬套任何称呼；
    ② 自报过（「叫我小黑」「我叫张三」）→ 记下来，之后才用。

**记下来的不只是名字，还有她出口的称呼形状。** 原典读数（2073 行可信台词）：
她对 9 个人一律叫「X同学」——日向 95 次、狛枝 54、贰大 22、九头龙 12、左右田 9、
澪田 4、索妮亚 3、田中 2、西园寺 2，共 203 次；当面直呼其名、不带「同学」的，
一次都没有（那几行「日向」不带后缀的都是在讲第三方）。
所以这里多一层 `address_of()`：他报的名字 → 她嘴里那个称呼。
**「不是同学」那一档没有原典依据**：那部作品里对面全是同班，她只叫过「X同学」。
那一档是照主人要求补的设计，见下面 `pick_suffix` 的注释。

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

# 她叫人的形状：名字 + 「同学」。这个后缀是原典里唯一出现过的当面对人称呼。
DEFAULT_SUFFIX = "同学"

# 名字本身就是一个"叫法"的，再挂「同学」就滑稽了（「叫我老师」→「老师」，不是「老师同学」）。
_TITLE_WORDS = frozenset(
    {
        "老板", "老大", "老师", "师傅", "师父", "先生", "小姐", "太太", "同学",
        "前辈", "大人", "阁下", "殿下", "哥哥", "姐姐", "弟弟", "妹妹", "叔叔", "阿姨",
        "社长", "部长", "队长", "店长", "学长", "学姐",
    }
)

# 已经自带敬称后缀的，不再挂第二次（「小王同学」不该变成「小王同学同学」）。
_HONORIFIC_TAIL = (
    "同学", "老师", "先生", "小姐", "前辈", "大人", "君", "酱", "桑", "哥", "姐", "さん", "様", "氏",
)


def address_of(name: str, suffix: str = DEFAULT_SUFFIX) -> str:
    """他说的名字 → 她出口的称呼。

    形状照原典：名字 + 「同学」；明确不是同学时后缀换成二次元敬称（见 `pick_suffix`）。
    三种情况不加后缀：
        ① `suffix` 被设成空（设置里关掉后缀）；
        ② 名字本身就是一个称呼（老师／老板／学长……）；
        ③ 名字已经自带敬称（「小王同学」「阿七酱」）。
    """
    body = str(name or "").strip()
    if not body:
        return ""
    tail = str(suffix or "").strip()
    if not tail:
        return body
    if body in _TITLE_WORDS or body.endswith(_HONORIFIC_TAIL):
        return body
    return body + tail


# 太常见的"名字"多半不是名字：宁可少记，也不要让她管人叫「学生」
_STOPWORDS = frozenset(
    {
        "学生", "新手", "小白", "普通人", "人类", "玩家", "观众", "路人", "一个", "个人",
        "你的", "我的", "谁", "什么", "啥", "名字", "本人", "自己", "大家", "你们", "这里",
        "新来的", "陌生人", "成年人", "大学生", "高中生", "上班族", "打工人",
        # 「我是女生」不是自我介绍名字——那是性别（踩过：记成了「他叫女生」）
        "男生", "女生", "男人", "女人", "男的", "女的", "男性", "女性", "男", "女",
        "妹子", "汉子", "姑娘", "哥们", "大叔",
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


# ── 他是不是同学：这一层是**设计**，不是原典 ────────────────────────────
#
# 原典读数（`docs/NANAMI-CANON-AUDIT.md §M`）：她 2379 行台词里，「先生」「小姐」「桑」
# 「酱」「さん」「様」「君」「老师」「前辈」**一档都没有**——因为那部作品里对面全是 77 期
# 同班同学，她只需要「X同学」这一种形状；**她没有任何一处"非同学"的对人称呼**。
# （语料里看着像的两处已经核掉了：「日向哥」在 170 行没分角色的块里；「小泉姐」是西園寺的
#  叫法，落在 `e03_002_102` 那个 47 行整段标错人的块里。）
# 所以下面这几档是照主人要求补的**设计档**：同学照原典，不是同学就换二次元敬称。
#
# **出口一律用中文写法，不写假名、也不写音译**（主人明确过这一条）：
#   さん → 先生（男）／小姐（女）；不知道男女就先叫「先生」。
#   ちゃん → 酱：那是**熟不拘礼**的叫法，猜不出来，只在他自己开口要的时候用
#            （「叫我阿七酱」→ 阿七酱），不让插件替他决定亲疏。
SUFFIX_PEER = "同学"        # 原典形状：同学／同龄
SUFFIX_HONORIFIC = "先生"   # 明确不是同学那一档（日语是 -さん，中文写「先生」）
_GENDER_SUFFIX = {"male": "先生", "female": "小姐"}

# 说得出这些话，就说明"他不是同学"（不是判身份对不对，只是判"不再是同班"）。
_NON_PEER_WORDS = frozenset(
    {
        "上班", "工作", "加班", "社畜", "打工人", "上班族", "退休", "退役", "毕业",
        "创业", "个体户", "自由职业", "研究生", "博士生", "导师", "客户", "甲方",
        "程序员", "工程师", "设计师", "画师", "作家", "主播", "销售", "会计", "律师",
        "医生", "护士", "警察", "编辑", "老板", "领导", "主管", "经理", "老师",
    }
)

_PEER_YES = re.compile(r"我.{0,4}(?:还|正)?(?:是|在当|在读)(?:个|一|名)?(?:学生|同学|高中生|初中生|大学生)")
_PEER_NO = re.compile(r"我.{0,6}(?:不是|早不是|已经不是|不再当)(?:个|一|名)?(?:学生|同学)")
_GENDER_PATTERNS = (
    ("male", re.compile(r"我(?:是|算|就)?(?:个)?(?:男|男生|男的|男人|男性|哥们|汉子|大叔)")),
    ("female", re.compile(r"我(?:是|算|就)?(?:个)?(?:女|女生|女的|女人|女性|妹子|姐们|姑娘)")),
)


def looks_like_non_peer(text: str) -> bool | None:
    """他是不是明确说过"自己不是同学"。True/False，没线索就是 None。

    宁可判不出来（None → 还是叫「同学」），也不要猜——叫错人的代价比叫得普通大得多。
    """
    body = str(text or "")
    if not body:
        return None
    if _PEER_NO.search(body):
        return True
    if _PEER_YES.search(body):
        return False
    if any(word in body for word in _NON_PEER_WORDS):
        return True
    return None


def gender_hint(text: str) -> str:
    """他有没有说过自己是男是女（male / female / ""）。"""
    body = str(text or "")
    for label, pattern in _GENDER_PATTERNS:
        if pattern.search(body):
            return label
    return ""


def pick_suffix(peer: bool | None, gender: str, *, peer_suffix: str, honorific: str) -> str:
    """她该用哪一档称谓。

    同学 → 原典那一档（默认「同学」）。
    明确不是同学 → 二次元敬称，但**出口是中文写法**：知道男女就用先生／小姐，
    不知道就用 `honorific`（默认也是「先生」，不是「桑」——主人说过：那个只是说明是 -さん）。
    """
    if peer is not False:
        return peer_suffix
    return _GENDER_SUFFIX.get(str(gender or ""), honorific)


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
    """他自报的名字，以及她该出口的那个称呼。

    `name` 是他自己说的原样（存档里就存这个，换设置也能重新算）；
    `address` 是**她叫得出口的样子**——名字 + 一档称谓。存档里不存它，
    每次读出来按当前设置现算，所以改后缀会立刻生效。
    `peer` 是他"是不是同学"：True / False / None（没线索，按同学算）。
    """

    name: str
    source: str = ""
    at: float = 0.0
    address: str = ""
    suffix: str = ""
    peer: bool | None = None

    @property
    def spoken(self) -> str:
        """她嘴里那个称呼：算过就用算过的，手写构造的实例退回名字本身。"""
        return self.address or self.name


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


def _bare(name: str) -> str:
    """把称呼里的敬称后缀切掉，只留名字（「小黑同学」→「小黑」）。"""
    body = str(name or "").strip()
    for tail in _HONORIFIC_TAIL:
        if len(body) > len(tail) and body.endswith(tail):
            return body[: -len(tail)]
    return body


def _is_same_address(left: str, right: str) -> bool:
    """两个叫法是不是同一个人。

    为什么要有这一层：他记的是「小黑」，说的是「别叫我小黑同学」——
    带不带后缀是同一个称呼，不带这一层就忘不掉。
    """
    one = _bare(left)
    other = _bare(right)
    if not one or not other:
        return False
    if one == other:
        return True
    if min(len(one), len(other)) < 2:
        return False
    return one in other or other in one


class NameMemory:
    """按会话记住"他希望被怎么叫"。文件很小，读写都加锁。

    存档里只存**事实**：名字、他是不是同学、他自己说过男女没有。
    她出口的那个称呼每次按当前设置现算（`pick_suffix` + `address_of`），
    所以在设置里改后缀、甚至清空，都会立刻生效，不用清 person.json。
    """

    def __init__(
        self,
        state_root: Path,
        *,
        enabled: bool = True,
        suffix: str = DEFAULT_SUFFIX,
        honorific: str = SUFFIX_HONORIFIC,
    ) -> None:
        self._path = Path(state_root) / _FILE
        self._enabled = bool(enabled)
        self._suffix = str(suffix or "")
        self._honorific = str(honorific or "")
        self._lock = threading.Lock()
        self._cache: dict[str, dict] | None = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def suffix(self) -> str:
        return self._suffix

    @property
    def honorific(self) -> str:
        return self._honorific

    def set_suffix(self, suffix: str, honorific: str | None = None) -> None:
        """设置改了后缀：下一次 `get()` 就按新的算（不用重建实例）。"""
        self._suffix = str(suffix or "")
        if honorific is not None:
            self._honorific = str(honorific or "")

    def _address(self, name: str, peer, gender: str) -> tuple[str, str]:
        """名字 + 他是不是同学 + 他说过的男女 → (她出口的称呼, 用的是哪一档)。"""
        suffix = pick_suffix(
            peer,
            gender,
            peer_suffix=self._suffix,
            honorific=self._honorific,
        )
        return address_of(name, suffix), suffix

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
            suffix = self._suffix
            honorific = self._honorific
        if not row:
            return None
        name = str(row.get("name") or "").strip()
        if not name:
            return None
        peer = row.get("peer")
        gender = str(row.get("gender") or "")
        address, used = self._address_with(name, peer, gender, suffix, honorific)
        return LearnedName(
            name=name,
            source=str(row.get("source") or ""),
            at=float(row.get("at") or 0.0),
            address=address,
            suffix=used,
            peer=peer if isinstance(peer, bool) else None,
        )

    @staticmethod
    def _address_with(name: str, peer, gender: str, suffix: str, honorific: str):
        used = pick_suffix(peer, gender, peer_suffix=suffix, honorific=honorific)
        return address_of(name, used), used

    def note_who(self, chat_key: str, *, peer=None, gender: str = "") -> None:
        """他有没有说过"自己不是同学"／"自己是男是女"——只记事实，不改称呼会用的名字。"""
        if peer is None and not gender:
            return
        with self._lock:
            data = self._load()
            row = data.get(str(chat_key))
            if not isinstance(row, dict):
                row = {"name": "", "source": "", "at": time.time()}
            if peer is not None:
                row["peer"] = bool(peer)
            if gender:
                row["gender"] = str(gender)
            data[str(chat_key)] = row
            self._save(data)

    def remember(self, chat_key: str, name: str, source: str = "") -> LearnedName:
        clean = _clean(name)
        if not clean:
            raise ValueError("empty name")
        with self._lock:
            data = self._load()
            old = data.get(str(chat_key)) or {}
            peer = old.get("peer") if isinstance(old, dict) else None
            gender = str(old.get("gender") or "") if isinstance(old, dict) else ""
            data[str(chat_key)] = {
                "name": clean,
                "source": str(source or ""),
                "at": time.time(),
                "peer": peer,
                "gender": gender,
            }
            suffix = self._suffix
            honorific = self._honorific
            self._save(data)
        address, used = self._address_with(clean, peer, gender, suffix, honorific)
        logger.info("nanami_persona: 记住称呼=%s（来源：%s）", address, source or "未记录")
        return LearnedName(
            name=clean,
            source=str(source or ""),
            at=time.time(),
            address=address,
            suffix=used,
            peer=peer if isinstance(peer, bool) else None,
        )

    def forget(self, chat_key: str) -> bool:
        """忘掉"名字"这一条。

        注意只清名字：**"他是不是同学""他说过自己是男是女"是事实，不跟着丢**——
        「别叫我小黑，叫我阿七」这种改口，一句里既要忘旧名字，又不能把他之前交代过的身份抹掉。
        """
        with self._lock:
            data = self._load()
            row = data.get(str(chat_key))
            if not isinstance(row, dict) or not str(row.get("name") or "").strip():
                return False
            row["name"] = ""
            row["source"] = ""
            row["at"] = time.time()
            data[str(chat_key)] = row
            self._save(data)
        logger.info("nanami_persona: 忘掉这个会话的称呼（用户要求别这么叫）")
        return True

    def observe(self, chat_key: str, user_text: str) -> LearnedName | None:
        """读一句用户发言：学到新称呼就记下；被要求别叫了就忘掉；顺手记他是不是同学。

        返回**这一句之后**她对这人的称呼（可能是 None = 还是不知道他是谁）。

        顺序有讲究：先收"他是不是同学"这条事实（它跟名字没关系，先说后说都算数），
        再处理「别叫我 X」——那句里也含「叫我」，不先排掉就会记成反的；
        最后才学新名字：「别叫我小黑，叫我阿七」这种改口，一句话里三件事都得办成。
        """
        if not self._enabled:
            return self.get(chat_key)
        # `looks_like_non_peer` 说的是「不是同学」，存档的 `peer` 说的是「是同学」——这里翻一次，
        # 别把两个词义混着用（第一版就是这么写反的：他报完「我是程序员」，她照样叫「同学」）。
        non_peer = looks_like_non_peer(user_text)
        peer = None if non_peer is None else (not non_peer)
        gender = gender_hint(user_text)
        if peer is not None or gender:
            self.note_who(chat_key, peer=peer, gender=gender)
        current = self.get(chat_key)
        rejection = extract_rejection(user_text)
        if rejection and current is not None and _is_same_address(current.name, rejection):
            self.forget(chat_key)
            # 忘掉的是"名字"；他这一句里说的"不是同学"是事实，得重新落回去
            self.note_who(chat_key, peer=peer, gender=gender)
            current = None
        name = extract_name(user_text)
        if name:
            return self.remember(chat_key, name, source=_snippet(user_text))
        return self.get(chat_key) if current is None else current



def _snippet(text: str, limit: int = 24) -> str:
    body = str(text or "").replace("\n", " ").strip()
    return body if len(body) <= limit else body[:limit] + "…"
