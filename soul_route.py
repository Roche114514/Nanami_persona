"""按用途把人格壳分层：每次都带的（always）／按场景才带的（条件段）。

为什么要有这一层（2026-11-12，主人问"人格壳太长，能不能优化结构"）：

壳已经长到 9k 字，而**每一轮真正用得上的是几百到一千多字**——
"第一次见面"只在首轮有用，"被冒犯时顶回去"只在被说笨／被冤枉那轮有用，
"称呼"只在他自报过名字之后才有意义，"讲家里人"只有他先提到家人才轮得到。

全都每轮塞进去，代价有两个：
  ① **稀释**：真正决定"她想说什么"的那张卡（几十字）被九千字压住；
  ② **走神**：不相干的规则会变成话题（实测：把"这几个词她 0 命中"写进去，她就说那几个词）。

**分层不删内容。** 段还是那段，只是在该出现的时候出现——
这也顺带解决另一件事：被冒犯那一段会紧贴着那一轮，而不是埋在五千字之前。

⚠️ 两条纪律：
  1. **没有标记的段按 always 处理**（宁可多带，不许因为忘了标记就静默丢掉一段）；
  2. 每条路由都要有测试；一个段既不是 always 又没有任何触发条件 = 孤儿段，测试直接失败。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Iterable

logger = logging.getLogger(__name__)

# 允许出现的路由名。加新路由要同时加：触发条件 + 自测用例。
ROUTES = ("always", "first_turn", "insulted", "identity", "family", "address")

_MARKER = re.compile(r"<!--\s*route:\s*([a-z_,\s]+?)\s*-->")
_HEADING = re.compile(r"^(#{2,3})\s+(.*)$")


@dataclass(frozen=True)
class Section:
    """人格壳里的一节：标题 + 路由 + 正文（含标题行本身）。"""

    title: str
    route: str
    body: str

    @property
    def size(self) -> int:
        return len(self.body)


@dataclass
class RouteContext:
    """这一轮要带哪几层。全部是"事实"，不猜。"""

    first_turn: bool = False        # 这一场还没开过口、而且他还什么都没自报
    insulted: bool = False          # 他被说笨／被冤枉／被看轻（或者卡片写着顶回去）
    identity_asked: bool = False    # 他在问她"你到底是什么"
    family: bool = False            # 他自己先提到了家人／学校／小时候
    has_name: bool = False          # 他自报过名字（称呼那一节才有用）

    def wanted(self) -> set[str]:
        got = {"always"}
        if self.first_turn:
            got.add("first_turn")
        if self.insulted:
            got.add("insulted")
        if self.identity_asked:
            got.add("identity")
        if self.family:
            got.add("family")
        if self.has_name:
            got.add("address")
        return got


# ── 触发条件（全部对"他说的话"做，输入是确定的，所以能测）────────────────
# 被冒犯：说笨／骂／冤枉她没做过的事／小看她
_INSULT = re.compile(
    r"笨|蠢|傻|白痴|废物|没用|幼稚|吵死|闭嘴|滚|烦人|垃圾"
    r"|看不起|小看|瞧不起|就这|也不过如此|你不行"
    r"|冤枉|明明(?:说|讲|答应)|又忘|答应过|不是早就"
    r"|(?:你|妳)(?:什么)?都(?:不会|不懂)"
)
# 被问到"你是什么"：这一轮才轮得到 §2 那两处心结。
# ⚠️ **不含「你是谁」。** "你是谁"是问名字，答一句名字就完了；§2 管的是"我是哪种存在"那件事——
#    第一版把「你是谁」也算进来，等于每句问名字都要念一遍她的身世。
_IDENTITY = re.compile(
    r"你(?:到底|究竟)?(?:是|算)(?:什么|哪种|什么样的)(?:东西|人|存在)?"
    r"|你(?:是)?(?:AI|ai|人工智能|机器人|程序|机娘|虚拟|数据|投影)"
    r"|你(?:到底|究竟)?(?:是)?(?:人|人类)吗"
    r"|那种存在"
)
# 家人／学校：他自己先提，她才能接（§5.5 讲得像听来的）
_FAMILY = re.compile(
    r"爸|妈|哥哥|姐姐|弟弟|妹妹|父母|家人|家里|老家|小时候|亲戚|中学|毕业|学校"
)


def context_from(
    his_text: str,
    *,
    action: str = "",
    first_turn: bool = False,
    has_name: bool = False,
) -> RouteContext:
    """他这一句话 + 卡片那个动作 → 这一轮该带哪几层。

    `action` 是卡片上「这一轮我只想」那个词；它写着"顶回去"的时候，
    即使他没骂人（比如卡片判出被误判），也把被冒犯那一段带上。
    """
    body = str(his_text or "")
    act = str(action or "")
    return RouteContext(
        first_turn=bool(first_turn),
        insulted=bool(_INSULT.search(body)) or "顶回去" in act or "驳论点" in act,
        identity_asked=bool(_IDENTITY.search(body)),
        family=bool(_FAMILY.search(body)),
        has_name=bool(has_name),
    )


def parse_sections(text: str) -> list[Section]:
    """把人格壳按 `##` / `###` 切段，读每段标题下面那行 `<!-- route: xxx -->`。

    规则：
      · 第一个标题之前的内容算一节（无标题，always）；
      · 标记写在标题的**下一行**，不带标记的段按 always 处理；
      · 路由名不认识 → 记一条警告并当 always（宁可多带，不许静默丢）。
    """
    lines = str(text or "").splitlines()
    starts: list[int] = [index for index, line in enumerate(lines) if _HEADING.match(line)]
    sections: list[Section] = []

    if not starts:
        return [Section(title="", route="always", body=str(text or ""))]

    if starts[0] > 0:
        head = "\n".join(lines[: starts[0]]).strip()
        if head:
            sections.append(Section(title="", route="always", body=head))

    bounds = list(zip(starts, starts[1:] + [len(lines)]))
    for start, end in bounds:
        title = _HEADING.match(lines[start]).group(2).strip()
        route = "always"
        cursor = start + 1
        while cursor < end and not lines[cursor].strip():
            cursor += 1
        marker_at = -1
        if cursor < end:
            match = _MARKER.search(lines[cursor])
            if match:
                marker_at = cursor
                wanted = [one.strip() for one in match.group(1).split(",") if one.strip()]
                unknown = [one for one in wanted if one not in ROUTES]
                if unknown:
                    logger.warning(
                        "nanami_persona: 人格壳「%s」写了不认识的路由 %s，按 always 处理",
                        title, unknown,
                    )
                route = ",".join(one for one in wanted if one in ROUTES) or "always"
        # 标记那一行只是给代码看的，**不进提示词**
        keep = [line for index, line in enumerate(lines[start:end], start) if index != marker_at]
        sections.append(Section(title=title, route=route, body="\n".join(keep)))
    return sections


def section_routes(text: str) -> dict[str, str]:
    """标题 → 路由（给自测和工具用）。"""
    return {one.title: one.route for one in parse_sections(text)}


def route_names(text: str) -> set[str]:
    """这份壳用到了哪些路由（用来查孤儿段、查漏写）。"""
    got: set[str] = set()
    for one in parse_sections(text):
        got.update(part.strip() for part in one.route.split(",") if part.strip())
    return got


def compose(text: str, context: RouteContext, *, mode: str = "auto") -> str:
    """按上下文挑段，拼回一份人格壳。

    `mode="all"` 就是不分层（老行为，出问题时的逃生门）。
    """
    body = str(text or "").strip()
    if not body:
        return ""
    if str(mode or "").strip().lower() == "all":
        return body
    wanted = context.wanted()
    picked: list[str] = []
    for one in parse_sections(body):
        routes = {part.strip() for part in one.route.split(",") if part.strip()}
        if routes & wanted:
            picked.append(one.body.strip())
    return "\n\n".join(picked).strip()


def sizes(text: str) -> list[tuple[str, str, int]]:
    """[(标题, 路由, 字数)]——工具与自测共用。"""
    return [(one.title, one.route, one.size) for one in parse_sections(text)]
