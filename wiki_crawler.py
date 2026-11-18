"""角色百科资料与原典语录爬取（萌娘百科 + Bangumi + 宿主角色卡）。

用于全自动构建角色人格模型的第 1 阶段：
  - 获取准确的作品出处、别名、人物经历、性格标签、人际称呼与专有名词表；
  - 抽取百科正文与「/语音」「/台词」「角色语录」子页面里的原典台词，供第 0 轮语料填充。
  - 所有网络请求均 fail-open：联网受限时自动退回宿主角色卡或用户补充文本。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

import requests

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
_MOEGIRL_APIS = (
    "https://moegirl.uk/api.php",
    "https://zh.moegirl.org.cn/api.php",
)
_BGM_API = "https://api.bgm.tv/v0"

_QUOTE_TEMPLATES = ("引号", "台词", "quote", "cquote", "语录", "名言")
_NOISE_VOCAB = frozenset({
    "萌娘百科", "折叠", "注释", "目录", "展开", "收起", "参考资料", "外部链接",
    "角色歌", "角色曲", "官方", "简介", "性格", "外貌", "经历", "登场", "配音",
    "声优", "日文", "中文", "英文", "模板", "分类", "文件", "File", "Category",
})


@dataclass
class CharacterDossier:
    """汇总后的角色档案，用于声纹鉴别卡、语料纠错、人格壳与参数推填。"""

    character_name: str
    work_name: str = ""
    aliases: list[str] = field(default_factory=list)
    summary: str = ""
    personality: str = ""
    appearance: str = ""
    identity_and_lore: str = ""
    relationships: str = ""
    vocabulary: list[str] = field(default_factory=list)
    canon_quotes: list[tuple[str, str]] = field(default_factory=list)
    raw_sections: str = ""
    sources: list[str] = field(default_factory=list)

    def to_prompt_text(self, max_chars: int = 7000) -> str:
        """格式化为适合喂给 LLM 的结构化角色资料文本。"""
        parts: list[str] = [f"角色名：{self.character_name}"]
        if self.work_name:
            parts.append(f"所属作品：{self.work_name}")
        if self.aliases:
            parts.append("别名 / 原名：" + "、".join(self.aliases[:8]))
        if self.vocabulary:
            parts.append("专有名词参考：" + "、".join(self.vocabulary[:30]))
        if self.summary:
            parts.append("【角色简介】\n" + self.summary[:1200])
        if self.personality:
            parts.append("【性格与说话特点】\n" + self.personality[:1400])
        if self.appearance:
            parts.append("【外貌特征】\n" + self.appearance[:700])
        if self.identity_and_lore:
            parts.append("【背景、能力与经历】\n" + self.identity_and_lore[:1600])
        if self.relationships:
            parts.append("【人际关系与称呼】\n" + self.relationships[:900])
        if self.canon_quotes:
            q_lines = [f"- ({ctx}) {q}" if ctx else f"- {q}" for ctx, q in self.canon_quotes[:25]]
            parts.append("【原典语录摘录】\n" + "\n".join(q_lines))
        if self.raw_sections and len("\n\n".join(parts)) < max_chars // 2:
            parts.append("【补充章节摘录】\n" + self.raw_sections[:1800])
        joined = "\n\n".join(parts)
        return joined[:max_chars]


def _extract_template_blocks(text: str, names: tuple[str, ...]) -> list[str]:
    """抽取 `{{引号|...}}` 等可嵌套花括号模板的内部参数文本。"""
    out: list[str] = []
    lowered = text.lower()
    for name in names:
        needle = "{{" + name.lower()
        idx = 0
        while True:
            pos = lowered.find(needle, idx)
            if pos < 0:
                break
            after = pos + len(needle)
            if after < len(text) and text[after] not in ("|", "}", "\n", " "):
                idx = after
                continue
            depth = 0
            end = pos
            while end < len(text) - 1:
                if text[end : end + 2] == "{{":
                    depth += 1
                    end += 2
                    continue
                if text[end : end + 2] == "}}":
                    depth -= 1
                    end += 2
                    if depth <= 0:
                        break
                    continue
                end += 1
            block = text[pos + 2 : max(pos + 2, end - 2)]
            out.append(block)
            idx = max(end, pos + 2)
    return out


def clean_wikitext(raw: str) -> str:
    """将 MediaWiki Wikitext 清洗为可读纯文本，保留核心中文表述。"""
    text = str(raw or "")
    text = re.sub(r"<!--[\s\S]*?-->", "", text)
    text = re.sub(r"<ref[^>]*>[\s\S]*?</ref>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<ref[^/>]*/\s*>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</?(?:nowiki|small|big|sub|sup|span|div|font|center|p|b|i|u|s|del|strong|em)[^>]*>", "", text, flags=re.IGNORECASE)

    # {{ruby|显示|注音}} -> 显示
    text = re.sub(r"\{\{[Rr]uby\|([^|}\n]+)\|[^}]*\}\}", r"\1", text)
    # {{黑幕|内容}} / {{lj|内容}} / {{lang|xx|内容}} / {{color|...|内容}}
    text = re.sub(r"\{\{(?:黑幕|heimu|lj|jp|lang\|[^|}]+|colored?\|[^|}]+)\|([^}]+)\}\}", r"\1", text, flags=re.IGNORECASE)
    text = re.sub(r"\{\{剧透\|([^}]+)\}\}", r"\1", text)

    # 剥离常见纯样式或信息框模板（单层兜底，多轮收缩）
    for _ in range(3):
        prev = text
        text = re.sub(r"\{\{[^{}]*\}\}", "", text)
        if text == prev:
            break

    # [[目标|显示]] -> 显示；[[目标]] -> 目标（先剥离普通内链，避免 [[File:...|说明[[xxx]]]] 截断残留）
    for _ in range(3):
        prev = text
        text = re.sub(r"\[\[(?!File:|文件:|Image:|图像:|Category:|分类:)[^\[\]|]+\|([^\[\]]+)\]\]", r"\1", text)
        text = re.sub(r"\[\[(?!File:|文件:|Image:|图像:|Category:|分类:)([^\[\]]+)\]\]", r"\1", text)
        text = re.sub(r"\[\[(?:File|文件|Image|图像|Category|分类):[^\[\]]+\]\]", "", text, flags=re.IGNORECASE)
        if text == prev:
            break
    text = re.sub(r"\[https?://[^\s\]]+\s+([^\]]+)\]", r"\1", text)
    text = re.sub(r"\[https?://[^\s\]]+\]", "", text)

    text = text.replace("'''", "").replace("''", "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _extract_wiki_links(raw: str) -> list[str]:
    """抽取词条中的 [[专有名词]] 列表，供 ASR 纠错表使用。"""
    vocab: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(r"\[\[([^\[\]|#\n]+)(?:\|([^\[\]\n]+))?\]\]", raw):
        target = (m.group(2) or m.group(1) or "").strip()
        if not target or ":" in target or len(target) < 2 or len(target) > 16:
            continue
        if target in _NOISE_VOCAB or re.fullmatch(r"[\d\s\-_.]+", target):
            continue
        if target not in seen:
            seen.add(target)
            vocab.append(target)
    return vocab[:45]


def _split_wiki_table_cells(row: str) -> list[str]:
    """按顶级 `||` 或 `|` 切分维基表格行，忽略 `{{...}}` 与 `[[...]]` 内部竖线。"""
    s = row.strip()
    if s.startswith("|"):
        s = s[1:]
    cells: list[str] = []
    cur: list[str] = []
    brace_depth = 0
    bracket_depth = 0
    i = 0
    n = len(s)
    while i < n:
        two = s[i : i + 2]
        if two == "{{":
            brace_depth += 1
            cur.append(two)
            i += 2
            continue
        if two == "}}" and brace_depth > 0:
            brace_depth -= 1
            cur.append(two)
            i += 2
            continue
        if two == "[[":
            bracket_depth += 1
            cur.append(two)
            i += 2
            continue
        if two == "]]" and bracket_depth > 0:
            bracket_depth -= 1
            cur.append(two)
            i += 2
            continue
        if brace_depth == 0 and bracket_depth == 0:
            if two == "||":
                cell = "".join(cur).strip()
                if cell:
                    cells.append(cell)
                cur = []
                i += 2
                continue
            if s[i] == "|":
                cell = "".join(cur).strip()
                if cell:
                    cells.append(cell)
                cur = []
                i += 1
                continue
        cur.append(s[i])
        i += 1
    tail = "".join(cur).strip()
    if tail:
        cells.append(tail)
    return cells


def _extract_quotes_from_wikitext(wikitext: str, character_name: str) -> list[tuple[str, str]]:
    """从 Wikitext 模板与语录/语音表格中抽取原典台词（严格跳过 Infobox 属性行）。"""
    quotes: list[tuple[str, str]] = []
    seen: set[str] = set()
    quote_heading_re = re.compile(r"^={2,4}\s*([^=\n]+?)\s*={2,4}\s*$")
    infobox_kv_re = re.compile(r"^\|\s*[^=|{}\[\]\n]{1,24}\s*=")

    def _add_quote(ctx: str, raw_q: str) -> None:
        q = clean_wikitext(raw_q).strip().strip("「」『』\"“”")
        q = re.sub(r"\s+", " ", q).strip()
        c_clean = clean_wikitext(ctx).strip()
        c_clean = re.sub(r"\s+", " ", c_clean).strip()
        if len(q) < 2 or len(q) > 90:
            return
        if any(bad in q for bad in ("=", "{{", "}}", "http://", "https://", "File:")):
            return
        if not re.search(r"[\u4e00-\u9fff\u3040-\u30ff]", q):
            return
        if q in seen:
            return
        seen.add(q)
        quotes.append((c_clean[:24].strip(), q))

    # 1. {{引号|台词|说话人/场景}} / {{台词|...}}
    for block in _extract_template_blocks(wikitext, _QUOTE_TEMPLATES):
        fields = [f.strip() for f in block.split("|") if f.strip()]
        if len(fields) < 2:
            continue
        params: dict[str, str] = {}
        positional: list[str] = []
        for item in fields[1:]:
            if "=" in item:
                k, v = item.split("=", 1)
                params[k.strip().lower()] = v.strip()
            else:
                positional.append(item)
        quote_text = params.get("1") or params.get("text") or params.get("内容") or params.get("台词") or (positional[0] if positional else "")
        speaker_or_ctx = params.get("2") or params.get("source") or params.get("来源") or params.get("场景") or (positional[1] if len(positional) > 1 else "")
        if quote_text:
            _add_quote(speaker_or_ctx or "原典语录", quote_text)

    # 2. 语音/台词表格行：仅扫描 {| ... |} 表格内部，或位于「语录/台词/名言/语音/对话」章节中的竖线行
    in_table = False
    in_quote_section = False
    for line in wikitext.splitlines():
        s = line.strip()
        hm = quote_heading_re.match(s)
        if hm:
            sec_title = clean_wikitext(hm.group(1)).strip()
            in_quote_section = any(k in sec_title for k in ("语录", "台词", "名言", "语音", "对话"))
            continue
        if s.startswith("{|"):
            in_table = True
            continue
        if s.startswith("|}"):
            in_table = False
            continue
        if not (in_table or in_quote_section):
            continue
        if not s.startswith("|") or s.startswith(("|-", "|+", "|}")):
            continue
        # 严禁将 {{人物信息 |本名={{jpn|...}} }} 等 Infobox 键值属性行拆作台词
        if infobox_kv_re.match(s):
            continue
        cells = _split_wiki_table_cells(s)
        if len(cells) >= 2:
            # 跳过纯表头和属性名
            if any(h in cells[0] for h in ("class=", "style=", "width=", "colspan", "rowspan")):
                cells = [c.split("|", 1)[-1].strip() if "|" in c else c for c in cells]
            if len(cells) >= 2:
                ctx_clean = clean_wikitext(cells[0])
                q_clean = clean_wikitext(cells[1])
                if any(bad in ctx_clean for bad in ("=", "{{", "}}", "http", "File:")):
                    continue
                if any(bad in q_clean for bad in ("=", "{{", "}}", "http", "File:")):
                    continue
                if ctx_clean and 2 <= len(q_clean) <= 80 and len(ctx_clean) <= 20:
                    _add_quote(ctx_clean, cells[1])

    # 3. 散文段落/列表项中的高辨识度引号台词（如「名台词/语录/性格」章节内的「…」）
    cur_sec = ""
    in_prose_quote_sec = False
    prose_quote_re = re.compile(r"[「『]([^」』\n]{3,68})[」』]")
    for line in wikitext.splitlines():
        s = line.strip()
        hm = quote_heading_re.match(s)
        if hm:
            cur_sec = clean_wikitext(hm.group(1)).strip()
            in_prose_quote_sec = any(
                k in cur_sec for k in ("语录", "台词", "名言", "名台词", "性格", "角色经历", "简介", "设定")
            )
            continue
        if not in_prose_quote_sec or not s or s.startswith(("{", "|", "!", "[[File:", "[[Category:")):
            continue
        cleaned_line = clean_wikitext(s)
        if not cleaned_line or len(cleaned_line) > 220:
            continue
        for mq in prose_quote_re.finditer(cleaned_line):
            cand_q = mq.group(1).strip()
            # 过滤作品名/外号/曲名等非台词短语（台词通常含标点、第一/二人称或动词语气词）
            if any(p in cand_q for p in ("，", "。", "！", "？", "…", "、", "我", "你", "别", "吧", "吗", "呢", "啊", "啦")):
                _add_quote(cur_sec or "原典语录", cand_q)

    return quotes


def _split_wiki_sections(wikitext: str) -> tuple[str, dict[str, str]]:
    """按 `== 章节名 ==` 切分 Wikitext，返回 (导言文本, {章节名: 清洗后文本})。"""
    heading_re = re.compile(r"^(={2,4})\s*([^=\n]+?)\s*\1\s*$", re.MULTILINE)
    matches = list(heading_re.finditer(wikitext))
    if not matches:
        return clean_wikitext(wikitext), {}

    intro = clean_wikitext(wikitext[: matches[0].start()])
    sections: dict[str, str] = {}
    for idx, m in enumerate(matches):
        title = clean_wikitext(m.group(2)).strip()
        start = m.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(wikitext)
        body = clean_wikitext(wikitext[start:end])
        if title and body:
            sections[title] = body
    return intro, sections


def _parse_moegirl_wikitext(
    wikitext: str,
    character_name: str,
    *,
    is_subpage: bool = False,
) -> dict[str, Any]:
    quotes = _extract_quotes_from_wikitext(wikitext, character_name)
    vocab = _extract_wiki_links(wikitext)
    intro, sections = _split_wiki_sections(wikitext)

    personality_parts: list[str] = []
    appearance_parts: list[str] = []
    lore_parts: list[str] = []
    relation_parts: list[str] = []
    other_parts: list[str] = []

    for title, body in sections.items():
        if any(k in title for k in ("注释", "外部链接", "参考资料", "导航", "画廊", "图集")):
            continue
        if any(k in title for k in ("语录", "台词", "名言", "语音", "对话")):
            # 从列表行补充未命中模板的语录
            for line in body.splitlines():
                cleaned = line.lstrip("*-•· ").strip()
                if 3 <= len(cleaned) <= 85 and not cleaned.startswith(("==", "注：", "备注")):
                    q_clean = cleaned.strip("「」『』\"“”")
                    if q_clean and all(q != q_clean for _, q in quotes):
                        quotes.append((title[:16], q_clean))
            continue
        if any(k in title for k in ("性格", "特点", "萌点", "口癖", "说话", "习惯")):
            personality_parts.append(f"{title}：\n{body}")
        elif any(k in title for k in ("外貌", "形象", "衣着", "服装", "造型")):
            appearance_parts.append(f"{title}：\n{body}")
        elif any(k in title for k in ("关系", "人际", "家人", "家庭", "亲友", "同伴")):
            relation_parts.append(f"{title}：\n{body}")
        elif any(k in title for k in ("简介", "设定", "背景", "经历", "剧情", "故事", "能力", "身份")):
            lore_parts.append(f"{title}：\n{body}")
        else:
            other_parts.append(f"{title}：\n{body[:500]}")

    work_name = ""
    if intro:
        m_work = re.search(r"《([^《》\n]{2,30})》", intro)
        if m_work:
            work_name = m_work.group(1).strip()
        for m_short in re.finditer(r"简称[“\"「]?([^”\"」\s，。；、]{2,12})[”\"」]?", intro):
            short = m_short.group(1).strip()
            if short and short not in vocab:
                vocab.append(short)

    aliases: list[str] = []
    m_alias = re.search(r"^\|\s*(?:别号|别名)\s*=\s*(.+)$", wikitext, re.MULTILINE)
    if m_alias:
        raw_alias = clean_wikitext(m_alias.group(1))
        for item in re.split(r"[、,/，；;\n]+", raw_alias):
            a = item.strip()
            if a and a != character_name and len(a) <= 20 and a not in aliases:
                aliases.append(a)

    return {
        "intro": "" if is_subpage else intro,
        "work_name": work_name,
        "aliases": aliases,
        "personality": "\n\n".join(personality_parts),
        "appearance": "\n\n".join(appearance_parts),
        "identity_and_lore": "\n\n".join(lore_parts),
        "relationships": "\n\n".join(relation_parts),
        "other": "\n\n".join(other_parts),
        "quotes": quotes,
        "vocab": vocab,
    }


def _moegirl_get_wikitext(session: requests.Session, page_title: str, timeout: float = 8.0) -> str:
    params = {
        "action": "parse",
        "page": page_title,
        "prop": "wikitext",
        "redirects": "1",
        "variant": "zh-cn",
        "format": "json",
    }
    for api_url in _MOEGIRL_APIS:
        try:
            resp = session.get(api_url, params=params, timeout=(4, timeout))
            if not resp.ok:
                continue
            data = resp.json()
            if not isinstance(data, dict) or "error" in data:
                continue
            wt = str(((data.get("parse") or {}).get("wikitext") or {}).get("*") or "")
            if wt:
                return wt
        except Exception:
            continue
    return ""


def _moegirl_search_title(session: requests.Session, query: str, timeout: float = 8.0) -> str:
    params = {
        "action": "query",
        "list": "search",
        "srsearch": query,
        "srlimit": "5",
        "format": "json",
    }
    for api_url in _MOEGIRL_APIS:
        try:
            resp = session.get(api_url, params=params, timeout=(4, timeout))
            if not resp.ok:
                continue
            data = resp.json()
            if not isinstance(data, dict) or "error" in data:
                continue
            hits = ((data.get("query") or {}).get("search") or [])
            for h in hits:
                title = str(h.get("title") or "").strip()
                if title and "/" not in title and not title.endswith("(消歧义)"):
                    return title
            if hits:
                return str(hits[0].get("title") or "").strip()
        except Exception:
            continue
    return ""


def _fetch_moegirl(character_name: str, work_name: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {"ok": False}
    try:
        with requests.Session() as s:
            s.headers.update({"User-Agent": _UA})
            raw_wt = _moegirl_get_wikitext(s, character_name)
            resolved_title = character_name
            if not raw_wt or "{{disambig" in raw_wt.lower() or "消歧义" in raw_wt[:300]:
                q = f"{character_name} {work_name}".strip() if work_name else character_name
                cand = _moegirl_search_title(s, q)
                if cand and cand != character_name:
                    cand_wt = _moegirl_get_wikitext(s, cand)
                    if cand_wt:
                        raw_wt = cand_wt
                        resolved_title = cand
            if not raw_wt:
                return out

            parsed = _parse_moegirl_wikitext(raw_wt, character_name, is_subpage=False)
            # 尝试抓取常见语音/台词子页面（极高价值，常见于二游/Gal角色）
            for suffix in ("语音", "台词", "角色语音"):
                sub_wt = _moegirl_get_wikitext(s, f"{resolved_title}/{suffix}")
                if sub_wt:
                    sub_parsed = _parse_moegirl_wikitext(sub_wt, character_name, is_subpage=True)
                    parsed["quotes"].extend(sub_parsed["quotes"])
                    parsed["vocab"].extend(sub_parsed["vocab"])
                    break
            parsed["ok"] = True
            parsed["title"] = resolved_title
            return parsed
    except Exception as exc:
        logger.debug("nanami_persona: 萌娘百科抓取跳过 (%s): %s", character_name, exc)
        return out


def _fetch_bangumi(character_name: str, work_name: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {"ok": False}
    try:
        with requests.Session() as s:
            s.headers.update({"User-Agent": "shinsekai-nanami-persona/1.0 (https://bgm.tv)"})
            resp = s.post(
                f"{_BGM_API}/search/characters",
                json={"keyword": character_name},
                params={"limit": 6},
                timeout=(4, 8),
            )
            if not resp.ok:
                return out
            rows = (resp.json() or {}).get("data") or []
            if not rows:
                return out

            picked = rows[0]
            if work_name:
                for row in rows:
                    blob = f"{row.get('name', '')} {row.get('summary', '')} {row.get('infobox', '')}"
                    if work_name in blob:
                        picked = row
                        break

            aliases: list[str] = []
            info_lines: list[str] = []
            name_cn = ""
            raw_name = str(picked.get("name") or "").strip()
            if raw_name and raw_name != character_name:
                aliases.append(raw_name)

            for entry in picked.get("infobox") or []:
                k = str(entry.get("key") or "").strip()
                v = entry.get("value")
                if isinstance(v, list):
                    vals = [str(x.get("v") or "").strip() for x in v if isinstance(x, dict) and x.get("v")]
                    v_str = "、".join(vals)
                else:
                    v_str = str(v or "").strip()
                if not k or not v_str:
                    continue
                if "简体中文名" in k:
                    name_cn = v_str
                elif "别名" in k:
                    aliases.extend([x.strip() for x in re.split(r"[、,/，\n]+", v_str) if x.strip()])
                info_lines.append(f"{k}：{v_str}")

            summary = str(picked.get("summary") or "").strip()
            if name_cn and name_cn != character_name and name_cn not in aliases:
                aliases.append(name_cn)

            return {
                "ok": True,
                "aliases": list(dict.fromkeys(a for a in aliases if a)),
                "summary": summary,
                "infobox_text": "\n".join(info_lines),
            }
    except Exception as exc:
        logger.debug("nanami_persona: Bangumi 抓取跳过 (%s): %s", character_name, exc)
        return out


def collect_character_dossier(
    character_name: str,
    *,
    work_name: str = "",
    registry: Any = None,
    extra_text: str = "",
    allow_network: bool = True,
) -> CharacterDossier:
    """整合宿主角色卡、萌娘百科、Bangumi 与补充文本，产出统一角色资料档案。"""
    cname = str(character_name or "").strip()
    wname = str(work_name or "").strip()
    dossier = CharacterDossier(character_name=cname, work_name=wname)

    # 1. 宿主角色列表（本地离线基线）
    if registry is not None and hasattr(registry, "list_host_characters"):
        try:
            for item in registry.list_host_characters():
                if str(item.get("name") or "").strip() == cname:
                    brief = str(item.get("brief") or "").strip()
                    setting = str(item.get("setting") or "").strip()
                    if brief or setting:
                        dossier.sources.append("host_characters")
                        if brief:
                            dossier.summary = brief
                        if setting:
                            dossier.identity_and_lore = setting
                    break
        except Exception:
            pass

    if extra_text and str(extra_text).strip():
        dossier.sources.append("user_extra")
        dossier.raw_sections = (dossier.raw_sections + "\n\n" + str(extra_text).strip()).strip()

    if allow_network and cname:
        moe = _fetch_moegirl(cname, wname)
        if moe.get("ok"):
            dossier.sources.append("moegirl")
            if not dossier.work_name and moe.get("work_name"):
                dossier.work_name = str(moe["work_name"]).strip()
            for al in moe.get("aliases") or []:
                if al not in dossier.aliases:
                    dossier.aliases.append(al)
            if moe.get("intro"):
                dossier.summary = (dossier.summary + "\n\n" + moe["intro"]).strip() if dossier.summary else moe["intro"]
            if moe.get("personality"):
                dossier.personality = moe["personality"]
            if moe.get("appearance"):
                dossier.appearance = moe["appearance"]
            if moe.get("identity_and_lore"):
                dossier.identity_and_lore = (
                    moe["identity_and_lore"]
                    if not dossier.identity_and_lore
                    else dossier.identity_and_lore + "\n\n" + moe["identity_and_lore"]
                )
            if moe.get("relationships"):
                dossier.relationships = moe["relationships"]
            if moe.get("other"):
                dossier.raw_sections = (dossier.raw_sections + "\n\n" + moe["other"]).strip()
            dossier.canon_quotes.extend(moe.get("quotes") or [])
            dossier.vocabulary.extend(moe.get("vocab") or [])

        bgm = _fetch_bangumi(cname, wname)
        if bgm.get("ok"):
            dossier.sources.append("bangumi")
            for al in bgm.get("aliases") or []:
                if al not in dossier.aliases:
                    dossier.aliases.append(al)
            if bgm.get("summary") and bgm["summary"] not in dossier.summary:
                dossier.summary = (dossier.summary + "\n\n" + bgm["summary"]).strip()
            if not dossier.work_name:
                m_work = re.search(r"《([^《》\n]{2,24})》", str(bgm.get("summary") or ""))
                if m_work:
                    dossier.work_name = m_work.group(1).strip()
            if bgm.get("infobox_text"):
                dossier.raw_sections = (bgm["infobox_text"] + "\n\n" + dossier.raw_sections).strip()

    # 去重词汇与语录
    dossier.vocabulary = list(dict.fromkeys(v for v in dossier.vocabulary if v))
    seen_q: set[str] = set()
    uniq_quotes: list[tuple[str, str]] = []
    for ctx, q in dossier.canon_quotes:
        if q not in seen_q:
            seen_q.add(q)
            uniq_quotes.append((ctx, q))
    dossier.canon_quotes = uniq_quotes
    return dossier
