"""根据用户提供的原作对话资料或 B站ASR/百科语录，由 LLM 整理为角色专属语料库（corpus.tsv）并绑定到指定角色指针。

支持两种工作模式：
  1. `build_corpus_from_text()`：单次从给定文本提取 6 桶语料（前端上传/粘贴场景）；
  2. `BucketTracker` + `extract_bucket_pairs_for_deficits()`：
     按 6 大情境配额驱动（Quota-Driven）增量筛选「角色本人 + 缺失情境」台词，
     结合《角色声纹鉴别卡》与置信度门槛（>= 0.85）剔除路人/旁白/ASR同音错字，
     6 桶全达标即刻停机。
"""

from __future__ import annotations

import json
import logging
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import acg_corpus, judge
from .profiles import PointerRegistry

logger = logging.getLogger(__name__)

ALLOWED_BUCKETS = ("casual", "curious", "cold", "annoyed", "reject", "repair_afterglow")

BUCKET_DESCRIPTIONS: dict[str, str] = {
    "casual": "日常闲聊、主动搭话、分享零食/物品、亲近陪伴、开心附和、打招呼与道别（对应 stay / bond / give / uphold）",
    "curious": "好奇提问、打听对方动向、试探心意、确认对方是否在意自己、探讨具体事物（对应 understand / explore / master / prove / seek_reassure / monitor / test）",
    "cold": "害羞退半步、欲言又止、含糊岔开话题、犯困迷糊、简短轻声敷衍或想结束追问（对应 retreat / rest / deflect / keep_distance）",
    "annoyed": "被调侃/被看轻时的嘴硬反驳、吃醋闹别扭、着急打断、小脾气嗔怪（对应 not_belittled / settle_score / dominate；严禁违背人设的极端脏话）",
    "reject": "明确拒绝提议或搭讪、护住底线、坚持己见、不肯妥协（对应 guard / secure）",
    "repair_afterglow": "害羞道谢、小声道歉缓和气氛、体贴安慰解围、闹别扭后的软化给台阶（对应 repair / shield 与余波态）",
}

# 6 桶默认配额：(达标线 min, 饱和线 cap)，共 13 条（每类 2–3 条风格锚点）即触发满额停机
DEFAULT_BUCKET_QUOTAS: dict[str, tuple[int, int]] = {
    "casual": (3, 6),
    "curious": (2, 4),
    "cold": (2, 4),
    "annoyed": (2, 4),
    "reject": (2, 4),
    "repair_afterglow": (2, 4),
}

_THINK_RE = re.compile(r"<think>[\s\S]*?</think>", re.IGNORECASE)
_NOISE_AI_RE = re.compile(
    r"(?:请问有什么可以帮|有什么我可以帮|已为您记|收到，请|以上内容仅供参考|作为(?:一个)?AI|"
    r"欢迎收看|一键三连|关注点赞|投币收藏|下期再见)",
    re.IGNORECASE,
)
_TOXIC_INSULT_RE = re.compile(r"^(?:真恶心|去死|滚开|滚啊)[啊呀喂，,\s！!。]*$")

_SYSTEM_PROMPT = """\
你是一位严谨的二次元角色语料整理员。你的任务是从用户提供的原始对话资料/台词剧本中，提取并整理出「角色本人」的真实对话对，供角色扮演时的少样本语感参考。

必须严格遵守：
1. **忠实提取，严禁凭空编造**：`assistant` 字段必须尽可能原汁原味保留资料中角色本人的说话用词、停顿（「……」）、语气词与口癖；不要把角色没说过的话瞎编出来。
2. 如果原始资料是「某角色独白/金句」，请根据上下文为其补出自然、简短的触发问句作为 `user`（不超过 18 字）；如果资料本身就是双人对话，直接提取对方的话作 `user`、角色的话作 `assistant`。
3. 剔除舞台指示（不要带 `（……）` 括号动作）、剔除旁白说明、剔除系统菜单或过长说教（`assistant` 控制在 2–36 字的短句）。
4. 按情境归入以下 6 个桶之一：
   - `casual`: 日常闲聊、邀约、陪伴、温和互动
   - `curious`: 好奇反问、探讨具体事物/机制/不了解的新词
   - `cold`: 冷淡、简短敷衍、困倦/沉默或想结束话题
   - `annoyed`: 被冒犯、被挑衅、不耐烦、带刺或回怼
   - `reject`: 明确拒绝、划清边界、不肯妥协
   - `repair_afterglow`: 收到道歉或安抚后的余波回应
5. 只输出合法 JSON：`{"pairs": [{"bucket": "...", "user": "...", "assistant": "..."}, ...]}`，不要输出任何额外说明或代码块以外的文字。
"""


def _strip_json_fences(raw: str) -> str:
    s = _THINK_RE.sub("", str(raw or "")).strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    return s.strip()


def _salvage_pair_dicts(raw: str) -> list[dict[str, Any]]:
    """当大模型返回 JSON 因 max_tokens 截断时，逐个挽救已闭合的 pair 对象。"""
    cleaned = _strip_json_fences(raw)
    out: list[dict[str, Any]] = []
    i = 0
    n = len(cleaned)
    while i < n:
        if cleaned[i] != "{":
            i += 1
            continue
        depth = 0
        in_str = False
        escaped = False
        j = i
        found_end = -1
        while j < n:
            ch = cleaned[j]
            if escaped:
                escaped = False
            elif in_str:
                if ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        found_end = j
                        break
            j += 1
        if found_end > i:
            cand = cleaned[i : found_end + 1]
            if '"user"' in cand and '"assistant"' in cand:
                try:
                    obj = json.loads(cand)
                    if isinstance(obj, dict) and "user" in obj and "assistant" in obj:
                        out.append(obj)
                        i = found_end + 1
                        continue
                except ValueError:
                    pass
        i += 1
    return out


def _parse_json_obj(raw: str) -> Any:
    cleaned = _strip_json_fences(raw)
    if not cleaned:
        return None
    try:
        return json.loads(cleaned)
    except ValueError:
        m = re.search(r"\{[\s\S]*\}", cleaned)
        if m:
            try:
                return json.loads(m.group(0))
            except ValueError:
                pass
        salvaged = _salvage_pair_dicts(cleaned)
        return {"pairs": salvaged} if salvaged else None


def _clean_pair_text(user: Any, assistant: Any) -> tuple[str, str]:
    u = re.sub(r"[\t\r\n]+", " ", str(user or "")).strip().strip("「」『』\"“”")
    a = re.sub(r"[\t\r\n]+", " ", str(assistant or "")).strip().strip("「」『』\"“”")
    # 剥离残留的括号舞台指示
    u = re.sub(r"[（(][^）)\n]{1,30}[）)]", "", u).strip()
    a = re.sub(r"[（(][^）)\n]{1,40}[）)]", "", a).strip()
    return u, a


def _parse_pairs_json(raw: str) -> list[tuple[str, str, str]]:
    obj = _parse_json_obj(raw)
    if obj is None:
        return []
    items = obj.get("pairs") if isinstance(obj, dict) else (obj if isinstance(obj, list) else [])
    if not isinstance(items, list):
        return []

    out: list[tuple[str, str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        bucket = str(item.get("bucket") or "casual").strip()
        if bucket not in ALLOWED_BUCKETS:
            bucket = "casual"
        u, a = _clean_pair_text(item.get("user"), item.get("assistant"))
        if len(u) >= 1 and len(a) >= 1:
            out.append((bucket, u, a))
    return out


def _parse_scored_pairs_json(raw: str) -> list[dict[str, Any]]:
    obj = _parse_json_obj(raw)
    if obj is None:
        return []
    items = obj.get("pairs") if isinstance(obj, dict) else (obj if isinstance(obj, list) else [])
    if not isinstance(items, list):
        return []

    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        bucket = str(item.get("bucket") or "").strip()
        if bucket not in ALLOWED_BUCKETS:
            continue
        u, a = _clean_pair_text(item.get("user"), item.get("assistant"))
        if not u or not a:
            continue
        try:
            conf = float(item.get("speaker_confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        try:
            dist = float(item.get("distinctiveness", 0.85) if item.get("distinctiveness") is not None else 0.85)
        except (TypeError, ValueError):
            dist = 0.85
        out.append({
            "bucket": bucket,
            "user": u,
            "assistant": a,
            "raw_quote": str(item.get("raw_quote") or "").strip(),
            "speaker_confidence": max(0.0, min(1.0, conf)),
            "distinctiveness": max(0.0, min(1.0, dist)),
            "is_dialogue_pair": bool(item.get("is_dialogue_pair", False)),
            "reason": str(item.get("reason") or "").strip(),
        })
    return out


def _verify_source_evidence(raw_quote: str, window_text: str) -> bool:
    """核验 LLM 声明的原文依据是否真实存在于当前 ASR/语录输入窗口中，拦截幻觉编造。"""
    q = re.sub(r"[\s\u3000.,!?。！？…、・「」『』\"'()（）\-—~～\[\]:：]+", "", str(raw_quote or "").lower())
    if len(q) < 2:
        return False
    w = re.sub(r"[\s\u3000.,!?。！？…、・「」『』\"'()（）\-—~～\[\]:：]+", "", str(window_text or "").lower())
    if not w:
        return False
    if q in w:
        return True
    matched = sum(m.size for m in SequenceMatcher(None, q, w, autojunk=False).get_matching_blocks())
    return (matched / len(q)) >= 0.55


def _call_llm(adapter: Any, system_prompt: str, user_prompt: str, *, max_tokens: int = 2400) -> str:
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    try:
        resp = adapter.chat(
            messages=messages,
            stream=False,
            temperature=0.25,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
        )
    except Exception:
        resp = adapter.chat(
            messages=messages,
            stream=False,
            temperature=0.25,
            max_tokens=max_tokens,
            response_format=None,
        )
    decoded = adapter.decode_response(resp)
    return str(getattr(decoded, "content", "") or "")


class BucketTracker:
    """6 大情境桶配额追踪器：管理达标线/饱和线、置信度过滤与同桶近似句去重。"""

    def __init__(self, quotas: Mapping[str, tuple[int, int]] | None = None) -> None:
        base = quotas or DEFAULT_BUCKET_QUOTAS
        self.quotas: dict[str, tuple[int, int]] = {}
        for b in ALLOWED_BUCKETS:
            lo, hi = base.get(b, (2, 4))
            lo_i = max(0, int(lo))
            hi_i = max(lo_i, int(hi))
            self.quotas[b] = (lo_i, hi_i)
        self._items: dict[str, list[tuple[str, str]]] = {b: [] for b in ALLOWED_BUCKETS}
        self._seen: set[tuple[str, str]] = set()
        self._scores: dict[tuple[str, str], float] = {}
        self.rejected_stats: dict[str, int] = {
            "low_confidence": 0,
            "low_distinctiveness": 0,
            "hallucinated": 0,
            "bucket_full": 0,
            "duplicate": 0,
            "invalid": 0,
        }

    def load_existing(self, existing: Mapping[str, Sequence[tuple[str, str]]]) -> int:
        added = 0
        for bucket, pairs in existing.items():
            if bucket not in self._items:
                continue
            for u, a in pairs:
                if self.try_add(bucket, u, a, confidence=1.0, min_confidence=0.0):
                    added += 1
        return added

    def counts(self) -> dict[str, int]:
        return {b: len(self._items[b]) for b in ALLOWED_BUCKETS}

    def total(self) -> int:
        return sum(len(v) for v in self._items.values())

    def target_min_total(self) -> int:
        return sum(lo for lo, _ in self.quotas.values())

    def deficit_map(self) -> dict[str, int]:
        """返回尚未达到 min 达标线的桶及其缺口条数。"""
        out: dict[str, int] = {}
        for b in ALLOWED_BUCKETS:
            lo, _hi = self.quotas[b]
            cur = len(self._items[b])
            if cur < lo:
                out[b] = lo - cur
        return out

    def accepting_buckets(self) -> list[str]:
        """返回尚未达到 cap 饱和线、仍可接收新语料的桶。"""
        return [b for b in ALLOWED_BUCKETS if len(self._items[b]) < self.quotas[b][1]]

    def is_complete(self) -> bool:
        """全部 6 个桶都达到 min 达标线时返回 True（触发停机）。"""
        return all(len(self._items[b]) >= self.quotas[b][0] for b in ALLOWED_BUCKETS)

    def try_add(
        self,
        bucket: str,
        user: str,
        assistant: str,
        *,
        confidence: float = 1.0,
        min_confidence: float = 0.85,
        distinctiveness: float = 1.0,
        min_distinctiveness: float = 0.0,
        max_user_chars: int = 24,
        max_assistant_chars: int = 36,
    ) -> bool:
        if confidence < min_confidence:
            self.rejected_stats["low_confidence"] += 1
            return False
        if distinctiveness < min_distinctiveness:
            self.rejected_stats["low_distinctiveness"] += 1
            return False
        b = str(bucket or "").strip()
        if b not in self._items:
            self.rejected_stats["invalid"] += 1
            return False

        u, a = _clean_pair_text(user, assistant)
        is_minimal_silence = (
            b in ("cold", "reject")
            and (
                re.fullmatch(r"[…\.。~～]+", a) is not None
                or (len(a) == 1 and bool(re.search(r"[\u4e00-\u9fff]", a)))
            )
        )
        if not is_minimal_silence and not (1 <= len(u) <= max_user_chars and 2 <= len(a) <= max_assistant_chars):
            self.rejected_stats["invalid"] += 1
            return False
        if not (1 <= len(u) <= max_user_chars and 1 <= len(a) <= max_assistant_chars):
            self.rejected_stats["invalid"] += 1
            return False
        if _NOISE_AI_RE.search(a) or _TOXIC_INSULT_RE.match(a):
            self.rejected_stats["invalid"] += 1
            return False
        if not is_minimal_silence and not re.search(r"[\u4e00-\u9fff]", a):
            self.rejected_stats["invalid"] += 1
            return False
        # 拒绝未翻译的日文假名残留（user 与 assistant 均须完整译为简体中文）
        if re.search(r"[\u3040-\u309f\u30a1-\u30fa]", u) or re.search(r"[\u3040-\u309f\u30a1-\u30fa]", a):
            self.rejected_stats["invalid"] += 1
            return False

        if (u, a) in self._seen:
            self.rejected_stats["duplicate"] += 1
            return False

        # 同桶近似句去重：防止反复抓同一句口头禅凑数
        for _u_prev, a_prev in self._items[b]:
            if a == a_prev:
                self.rejected_stats["duplicate"] += 1
                return False
            if len(a) >= 6 and len(a_prev) >= 6:
                if SequenceMatcher(None, a, a_prev, autojunk=False).ratio() >= 0.68:
                    self.rejected_stats["duplicate"] += 1
                    return False

        quality_score = float(distinctiveness) * 0.7 + float(confidence) * 0.3
        _lo, hi = self.quotas[b]
        if len(self._items[b]) >= hi:
            # 优胜劣汰：若新台词显著度明显高于当前桶内最弱条目，则淘汰最弱条目并替换
            if hi > 0 and self._items[b]:
                weakest_idx = min(
                    range(len(self._items[b])),
                    key=lambda idx: self._scores.get(self._items[b][idx], 0.5),
                )
                weakest_pair = self._items[b][weakest_idx]
                weakest_score = self._scores.get(weakest_pair, 0.5)
                if quality_score >= weakest_score + 0.08:
                    self._seen.discard(weakest_pair)
                    self._scores.pop(weakest_pair, None)
                    self._items[b][weakest_idx] = (u, a)
                    self._seen.add((u, a))
                    self._scores[(u, a)] = quality_score
                    return True
            self.rejected_stats["bucket_full"] += 1
            return False

        self._seen.add((u, a))
        self._scores[(u, a)] = quality_score
        self._items[b].append((u, a))
        return True

    def all_pairs(self) -> list[tuple[str, str, str]]:
        out: list[tuple[str, str, str]] = []
        for b in ALLOWED_BUCKETS:
            for u, a in sorted(self._items[b], key=lambda pair: pair[0]):
                out.append((b, u, a))
        return out

    def format_status(self) -> str:
        parts = []
        for b in ALLOWED_BUCKETS:
            cur = len(self._items[b])
            lo, hi = self.quotas[b]
            mark = "✓" if cur >= lo else f"差{lo - cur}"
            parts.append(f"{b}:{cur}/{lo}(上限{hi},{mark})")
        return " | ".join(parts)


_FINGERPRINT_SYSTEM = """\
你是二次元角色语言学分析专家。请根据提供的角色设定资料，提取一张简洁的「角色声纹与人设鉴别卡」，用于在后续杂乱的视频字幕/ASR语音转写文本中精准辨别“哪句话才是这个角色本人说的”，并剔除路人、对手戏角色或旁白台词。

只输出合法 JSON：
{
  "self_pronouns": ["我", "本小姐"],
  "user_addresses": ["你", "同学"],
  "tone_markers": ["……", "呢", "吧"],
  "speech_style": "简明描述句长节奏、性格底色与情绪表达边界（30-60字）",
  "archetype": "从 kuudere_sleepy | calm_logical | genki_warm | tsundere_sharp 中四选一（傲娇/暴躁/刺头/不服输选 tsundere_sharp，内向寡言/冷淡选 kuudere_sleepy，元气热心选 genki_warm，冷静理性选 calm_logical）",
  "persona_baseline": "1-2句概括角色的核心性格底色、对男主/熟人的真实态度，以及绝对不符合其常态的违和表现",
  "glossary": ["专有名词1", "角色同伴名", "核心术语"],
  "other_speakers": ["容易在剧情中同框出现的对手戏角色名"],
  "exclude_hints": "什么样的话明显不是她本人说的（例如客服腔/长篇说教/他人专属口癖）"
}
"""


def build_speaker_fingerprint(
    character_name: str,
    dossier_text: str,
    *,
    adapter: Any = None,
    glossary_hints: Sequence[str] = (),
) -> dict[str, Any]:
    """根据角色资料提炼《角色声纹鉴别卡》，用于 B 站台词筛选与专有名词纠错。"""
    fallback = {
        "character_name": character_name,
        "self_pronouns": ["我"],
        "user_addresses": ["你"],
        "tone_markers": [],
        "speech_style": "贴合角色设定的自然口语短句。",
        "archetype": "calm_logical",
        "persona_baseline": "保持与角色日常真实性格一致的语气与态度，严禁收录鬼畜恶搞或对反派的极端应激台词作日常对话。",
        "glossary": list(dict.fromkeys([character_name, *glossary_hints]))[:25],
        "other_speakers": [],
        "exclude_hints": "旁白解说、视频UP主开场白、系统提示、对手戏路人台词。",
    }
    llm = adapter if adapter is not None else judge._default_adapter()
    if llm is None or not str(dossier_text or "").strip():
        return fallback

    user_prompt = (
        f"目标角色：{character_name}\n"
        f"参考词表：{', '.join(glossary_hints[:20]) if glossary_hints else '无'}\n\n"
        f"角色资料：\n{str(dossier_text)[:5000]}"
    )
    try:
        raw = _call_llm(llm, _FINGERPRINT_SYSTEM, user_prompt, max_tokens=900)
        parsed = _parse_json_obj(raw)
        if not isinstance(parsed, dict):
            return fallback
        for key in ("self_pronouns", "user_addresses", "tone_markers", "glossary", "other_speakers"):
            val = parsed.get(key)
            if isinstance(val, list):
                fallback[key] = [str(x).strip() for x in val if str(x).strip()][:25]
        if parsed.get("speech_style"):
            fallback["speech_style"] = str(parsed["speech_style"]).strip()[:160]
        arch = str(parsed.get("archetype") or "").strip()
        if arch in ("kuudere_sleepy", "calm_logical", "genki_warm", "tsundere_sharp"):
            fallback["archetype"] = arch
        if parsed.get("persona_baseline"):
            fallback["persona_baseline"] = str(parsed["persona_baseline"]).strip()[:260]
        if parsed.get("exclude_hints"):
            fallback["exclude_hints"] = str(parsed["exclude_hints"]).strip()[:200]
    except Exception as exc:
        logger.debug("nanami_persona: 生成声纹鉴别卡降级为默认 (%s)", exc)
    return fallback


_TS_PREFIX_RE = re.compile(r"^\[\d{2}:\d{2}\.\d+\s*-\s*\d{2}:\d{2}\.\d+\]\s*")
_PURE_ASCII_LYRIC_RE = re.compile(r"^[A-Za-z0-9\s,.'\"!?:\-()/]+$")
_WHISPER_HALLUCINATION_RE = re.compile(
    r"(?:ご視聴ありがとうございました|チャンネル登録|高評価|字幕制作|Subtitles\s+by|Thank(?:s|\s+you)\s+for\s+watching)",
    re.IGNORECASE,
)


def _clean_transcript_before_windowing(raw_text: str) -> str:
    """在喂给 LLM 前清洗转写文本：剔除 Whisper 片尾幻觉、鬼畜复读短句（>=3次）与中日文上下文中的纯英文 BGM 歌词行。"""
    raw = str(raw_text or "")
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if not lines:
        return ""

    has_cjk_context = len(re.findall(r"[\u4e00-\u9fff\u3040-\u30ff]", raw)) >= 6
    parsed: list[tuple[str, str, str]] = []
    freq: dict[str, int] = {}

    for ln in lines:
        body = _TS_PREFIX_RE.sub("", ln).strip()
        if not body:
            continue
        if _WHISPER_HALLUCINATION_RE.search(body):
            continue
        if has_cjk_context and len(body) > 15 and _PURE_ASCII_LYRIC_RE.fullmatch(body):
            continue
        key = re.sub(r"[\s\u3000.,!?。！？…、・「」『』\"'()（）\-—~～]+", "", body.lower())
        parsed.append((ln, body, key))
        if len(key) >= 2:
            freq[key] = freq.get(key, 0) + 1

    kept: list[str] = []
    for ln, _body, key in parsed:
        # 剔除出现 >=3 次的重复洗脑/鬼畜短句，防止污染后续抽取
        if len(key) >= 2 and freq.get(key, 0) >= 3:
            continue
        kept.append(ln)
    return "\n".join(kept)


def has_enough_dialogue(raw_text: str, *, min_lines: int = 4, min_chars: int = 36) -> bool:
    """快速预检清洗后的转写文本是否有足够有效对白，过稀或纯 BGM/拟声词时直接熔断跳过 LLM。"""
    cleaned = _clean_transcript_before_windowing(raw_text)
    if not cleaned:
        return False
    body_lines = []
    for ln in cleaned.splitlines():
        body = _TS_PREFIX_RE.sub("", ln).strip()
        if len(re.findall(r"[\u4e00-\u9fff\u3040-\u30ffA-Za-z0-9]", body)) >= 2:
            body_lines.append(body)
    if len(body_lines) < min_lines:
        return False
    return sum(len(x) for x in body_lines) >= min_chars


def _split_text_windows(raw_text: str, chunk_chars: int = 1800, max_chunks: int = 6) -> list[str]:
    cleaned = _clean_transcript_before_windowing(raw_text)
    lines = [ln.strip() for ln in cleaned.splitlines() if ln.strip()]
    if not lines:
        return []
    windows: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for ln in lines:
        if cur and cur_len + len(ln) + 1 > chunk_chars:
            windows.append("\n".join(cur))
            if len(windows) >= max_chunks:
                return windows
            cur = [ln]
            cur_len = len(ln)
        else:
            cur.append(ln)
            cur_len += len(ln) + 1
    if cur and len(windows) < max_chunks:
        windows.append("\n".join(cur))
    return windows


def extract_bucket_pairs_for_deficits(
    character_name: str,
    raw_text: str,
    tracker: BucketTracker,
    *,
    fingerprint: Mapping[str, Any] | None = None,
    source_hint: str = "asr",
    adapter: Any = None,
    min_confidence: float = 0.85,
    min_distinctiveness: float = 0.65,
    chunk_chars: int = 1800,
    max_chunks: int = 6,
    require_source_evidence: bool = True,
    only_deficit: bool = False,
) -> dict[str, Any]:
    """按当前 6 桶缺口，从给定文本窗口中定向抽取「角色本人 + 缺失情境」对话对（含原文依据核验）。"""
    if tracker.is_complete():
        return {"added": 0, "windows_used": 0, "complete": True}

    llm = adapter if adapter is not None else judge._default_adapter()
    if llm is None:
        raise RuntimeError("当前没有可用的大模型适配器，无法筛选语料。")

    fp = dict(fingerprint or {})
    windows = _split_text_windows(raw_text, chunk_chars=chunk_chars, max_chunks=max_chunks)
    total_added = 0
    windows_used = 0

    for win in windows:
        if tracker.is_complete():
            break
        deficits = tracker.deficit_map()
        accepting = (
            [b for b in tracker.accepting_buckets() if deficits.get(b, 0) > 0]
            if only_deficit
            else tracker.accepting_buckets()
        )
        if not accepting:
            break

        full_buckets = [b for b in ALLOWED_BUCKETS if b not in accepting]
        target_lines = []
        for b in accepting:
            need = deficits.get(b, 0)
            tag = f"【急缺 {need} 条】" if need > 0 else "【可少量补充】"
            target_lines.append(f"  - `{b}` {tag}: {BUCKET_DESCRIPTIONS[b]}")

        sys_prompt = (
            f"你是严格的二次元角色语料筛选员。目标角色是「{character_name}」。\n"
            "你的任务是从带时间戳的动画切片语音转写（ASR）/字幕/语录资料中，"
            "**只挑选确定属于「角色本人」说的话**，按人格模型驱动的 6 大情境缺口配额整理成高质量少样本对话对。\n\n"
            "必须遵守：\n"
            "1. **严格鉴真，严禁凭空编造**：逐句比对下方《角色声纹鉴别卡》，只提取**待筛选资料片段中真实出现过**且属于目标角色本人的台词！"
            "旁白、UP主解说、视频片头/片尾、歌词、系统播报、以及对手戏路人的话不要提取（给低置信度或跳过）。"
            "如果片段里只有几句适合就只输出几条；如果片段里没有符合缺口的真实台词，必须返回 {\"pairs\": []}，绝不允许自己编造！\n"
            "2. **语境还原与人设底色校验（严禁断章取义）**：必须结合前后多句对话判断场景真实语境！"
            "如果某句台词是角色在极端偶发冲突下对反派/骚扰者的防卫斥责（例如温柔角色偶发的一句「真恶心」）、"
            "或是鬼畜恶搞/玩梗复读、或是脱离语境的无头无尾短词，**严禁将其抽入日常对用户的 `annoyed` / `reject` 桶**！"
            "提取的每一句台词都必须符合《角色声纹鉴别卡》中角色对日常聊天对象/熟人的真实性格态度。\n"
            "3. **说话人辨识与相邻短句合并**：ASR 按约 8 秒切片，角色的一句完整台词可能拆在相邻 2–3 行，请自然合并为一句完整中文 `assistant` 表达（`raw_quote` 只需原字照抄其中任意一行输入原文即可）。"
            "注意区分说话人：以第二/第三人称呼叫目标角色名（如含「山田さん」「山田のカバン」）或使用明显异性第一人称（如男主「俺」「僕」）的旁白/内心独白属于对手戏角色，只能用作触发上文 `user`，绝不能作为目标角色的 `assistant`；"
            "请结合女性/对应角色语气词（如日文 `～よね`、`～なの`、`～くれる？`、`～かな`、`私` 等）与前后对话接续关系积极提取目标角色本人的真实台词！\n"
            "4. **6 桶与人格模型情境全对齐（尤其积极发掘冷淡/傲恼/修复余波三类情境）**：\n"
            "   - `casual`（对应 stay / bond / give / uphold）：日常搭话、分享零食/物品、寒暄、道别、亲近附和；\n"
            "   - `curious`（对应 understand / explore / master / prove / seek_reassure / monitor / test）：好奇反问、打听动向、试探心意或确认是否在意自己；\n"
            "   - `cold`（对应 retreat / rest / deflect / keep_distance）：害羞退半步、欲言又止、含糊岔开话题、犯困迷糊、简短轻声回应或想结束追问；\n"
            "   - `annoyed`（对应 not_belittled / settle_score / dominate 及受挫态）：被调侃/被看轻时的嘴硬反驳、吃醋闹别扭、着急打断或小脾气嗔怪（严禁违背人设的极端脏话）；\n"
            "   - `reject`（对应 guard / secure）：明确拒绝他人提议/告白、护住底线、坚持自己想法；\n"
            "   - `repair_afterglow`（对应 repair / shield 与余波态）：害羞道谢、小声道歉缓和气氛、体贴解围或闹别扭后的软化给台阶。\n"
            "5. **原文依据、完整日译中与标题语境纠错**：`raw_quote` 必须照抄输入片段中存在的原文一句；"
            "务必结合上方 `资料来源类型` 中的视频标题矫正 ASR 同音错字（例如演讲场景中误识别出的 `そうじ` 应按语境译为「致辞/演讲」，绝不可错译为「扫除」）；"
            "**`user` 与 `assistant` 双方台词都必须完整翻译为自然流畅的简体中文（严禁在输出中夹带任何日文平假名或片假名）**，忠实保留口癖与停顿。\n"
            "6. **配对规则**：若前一句紧挨着是他人提问/挑衅/道歉，直接抽前句作为 `user`（<=22字）；"
            "若本条为角色主动发起或独白，为其补一个自然简短的触发问句/上文作为 `user`（<=18字）。\n"
            "7. **角色显著度评分（distinctiveness，0.0–1.0）与格式**：`assistant` 控制在 2–36 字，剥离括号舞台指示 `（……）`；优先填写急缺桶！\n"
            "   **请严把显著度门槛**：只有「一眼就能认出是目标角色性格与说话劲头」的台词才给 `0.78–1.0`（例如情绪鲜明、态度鲜明、体现个性与执念的台词）；"
            "对于「谢谢您」「好的呢」「我也挺喜欢的」这类毫无角色辨识度的泛用客套话或路人应答，请给低分（`<= 0.45`）！忠实保留原片台词口吻，**严禁为了凑数或擅自改写句式**。\n"
            "8. 只输出合法 JSON："
            '{"pairs": [{"bucket": "...", "speaker_confidence": 0.95, "distinctiveness": 0.90, "raw_quote": "输入片段里的原文句子", "is_dialogue_pair": true, "user": "...", "assistant": "...", "reason": "..."}]}'
        )

        fp_summary = (
            f"《角色声纹鉴别卡》\n"
            f"- 目标角色：{character_name}\n"
            f"- 自称习惯：{', '.join(fp.get('self_pronouns') or ['我'])}\n"
            f"- 对方称呼：{', '.join(fp.get('user_addresses') or ['你'])}\n"
            f"- 口癖/语气词：{', '.join(fp.get('tone_markers') or []) or '按资料'}\n"
            f"- 说话风格：{fp.get('speech_style') or '自然口语'}\n"
            f"- 人设底色与语境底线：{fp.get('persona_baseline') or '符合角色对日常熟人的真实态度，拒绝鬼畜/极端断章取义'}\n"
            f"- 专有名词纠错表：{', '.join(fp.get('glossary') or []) or '无'}\n"
            f"- 需排除的他人角色：{', '.join(fp.get('other_speakers') or []) or '旁白/路人/UP主'}\n"
            f"- 排除提示：{fp.get('exclude_hints') or '无'}"
        )

        forbidden_note = (
            f"严禁输出以下已饱和桶：{', '.join(full_buckets)}"
            if full_buckets
            else "当前各桶均未饱和，请优先挑选【急缺】情境。"
        )
        user_prompt = (
            f"资料来源类型：{source_hint}\n\n"
            f"{fp_summary}\n\n"
            f"当前可接收与急缺桶：\n" + "\n".join(target_lines) + "\n"
            f"{forbidden_note}\n\n"
            f"待筛选资料片段：\n{win}"
        )

        windows_used += 1
        try:
            raw_reply = _call_llm(llm, sys_prompt, user_prompt, max_tokens=3600)
        except Exception as exc:
            logger.warning("nanami_persona: 语料定向抽取窗口失败: %s", exc)
            continue

        candidates = _parse_scored_pairs_json(raw_reply)
        accepting = set(tracker.accepting_buckets())
        for item in candidates:
            if item["bucket"] not in accepting and item.get("distinctiveness", 0.0) < 0.78:
                continue
            if require_source_evidence and not _verify_source_evidence(item.get("raw_quote", ""), win):
                tracker.rejected_stats["hallucinated"] += 1
                continue
            if tracker.try_add(
                item["bucket"],
                item["user"],
                item["assistant"],
                confidence=item["speaker_confidence"],
                min_confidence=min_confidence,
                distinctiveness=item.get("distinctiveness", 0.85),
                min_distinctiveness=min_distinctiveness,
            ):
                total_added += 1

    return {
        "added": total_added,
        "windows_used": windows_used,
        "complete": tracker.is_complete(),
    }


def fill_tail_deficits_from_canon(
    character_name: str,
    tracker: BucketTracker,
    dossier_text: str,
    *,
    adapter: Any = None,
) -> int:
    """当所有来源扫描完毕仍有桶缺口时，以已核实的原典语料（或人设）分批补足尾差，防止单次超长截断。"""
    deficits = tracker.deficit_map()
    if not deficits:
        return 0
    llm = adapter if adapter is not None else judge._default_adapter()
    if llm is None:
        return 0

    sys_prompt = (
        f"你是角色台词校准专家。请你为「{character_name}」补齐缺失的情境对话对。\n"
        "如果下方提供了原典声纹锚点，请**严格模仿原典锚点里的用词长度、标点停顿、人称与口癖**；"
        "如果没有原典锚点，请根据角色资料中的性格、说话习惯与用词，生成极自然、口语短句化的微信聊天对话对（2–32字）。\n"
        "绝对禁止写成「AI 客服腔」「工整排比金句」或带括号舞台指示。\n"
        '只输出 JSON：{"pairs": [{"bucket": "...", "user": "...", "assistant": "..."}]}'
    )

    total_added = 0
    deficit_items = list(deficits.items())
    # 每次最多请求 3 个桶，确保 JSON 完整不截断
    for start in range(0, len(deficit_items), 3):
        batch = dict(deficit_items[start : start + 3])
        existing_examples = tracker.all_pairs()
        anchor_lines = "\n".join(
            f"- [{b}] 对方：{u} → {character_name}：{a}"
            for b, u, a in existing_examples[:20]
        )
        need_lines = "\n".join(
            f"- `{b}`（需要 {n} 条）：{BUCKET_DESCRIPTIONS[b]}"
            for b, n in batch.items()
        )
        user_prompt = (
            f"目标角色：{character_name}\n\n"
            f"角色资料摘要：\n{str(dossier_text)[:2000]}\n\n"
            f"已收录的原典声纹锚点（请严格对齐此语感）：\n{anchor_lines or '（暂无锚点，请贴合角色人设口语短句风格）'}\n\n"
            f"本批需要补足的情境桶：\n{need_lines}"
        )
        try:
            raw = _call_llm(llm, sys_prompt, user_prompt, max_tokens=3200)
        except Exception as exc:
            logger.debug("nanami_persona: 尾差补齐跳过: %s", exc)
            continue

        for b, u, a in _parse_pairs_json(raw):
            if b not in batch:
                continue
            if tracker.try_add(b, u, a, confidence=0.95, min_confidence=0.85):
                total_added += 1
    return total_added


def write_tracker_corpus(
    registry: PointerRegistry,
    pointer_id: str,
    tracker: BucketTracker,
    *,
    mode: str = "replace",
    note: str = "",
) -> Path:
    """将 BucketTracker 中的 6 桶语料落盘至指定指针的 corpus.tsv。"""
    pointer = registry.get_pointer(pointer_id)
    if pointer is None:
        raise ValueError(f"未找到角色指针：{pointer_id}")
    profile = registry.load_profile(pointer)
    char_name = (profile.character_name if profile else "") or pointer.character_name or pointer.id

    target_path = corpus_path_for_pointer(registry, pointer_id)
    target_path.parent.mkdir(parents=True, exist_ok=True)

    final_tracker = BucketTracker(tracker.quotas)
    if str(mode or "replace").strip().lower() != "replace" and target_path.is_file():
        final_tracker.load_existing(acg_corpus.load_corpus_file(target_path))
    for b, u, a in tracker.all_pairs():
        final_tracker.try_add(b, u, a, confidence=1.0, min_confidence=0.0)

    header = f"# 角色专属语料库（指针：{pointer.id} · 角色：{char_name}"
    if note:
        header += f" · {note}"
    header += "）"
    lines = [header]
    for b, u, a in final_tracker.all_pairs():
        lines.append(f"{b}\t{u}\t{a}")
    target_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target_path


def corpus_path_for_pointer(registry: PointerRegistry, pointer_id: str) -> Path:
    pointer = registry.get_pointer(pointer_id)
    if pointer is None:
        raise ValueError(f"未找到角色指针：{pointer_id}")
    profile = registry.load_profile(pointer)
    if profile and profile.profile_dir is not None:
        return Path(profile.profile_dir) / "corpus.tsv"
    return registry.resolve_profile_dir(pointer) / "corpus.tsv"


def corpus_summary(profile_dir: Path | None) -> str:
    if profile_dir is None:
        return "尚未建立专属语料库（将使用通用分库）"
    for ext in (".tsv", ".yaml", ".yml", ".jsonl"):
        p = Path(profile_dir) / f"corpus{ext}"
        if p.is_file():
            data = acg_corpus.load_corpus_file(p)
            total = sum(len(v) for v in data.values())
            if total > 0:
                detail = " / ".join(f"{k}:{len(v)}" for k, v in sorted(data.items()) if v)
                return f"已启用专属语料 {p.name}（共 {total} 条：{detail}）"
    return "尚未建立专属语料库（将使用通用分库）"


def build_corpus_from_text(
    registry: PointerRegistry,
    pointer_id: str,
    raw_text: str,
    *,
    mode: str = "append",
    adapter: Any = None,
    max_pairs: int = 45,
) -> dict[str, Any]:
    """调用 LLM 将用户提供的原始对话资料整理为指定角色指针下的 `corpus.tsv`。"""
    text = str(raw_text or "").strip()
    if len(text) < 6:
        raise ValueError("请先在「原始对话资料」框中粘贴该角色的对话文本或台词摘录。")

    pointer = registry.get_pointer(pointer_id)
    if pointer is None:
        raise ValueError(f"未找到角色指针：{pointer_id}")
    profile = registry.load_profile(pointer)
    char_name = (
        (profile.character_name if profile else "")
        or pointer.character_name
        or pointer.id
    )

    llm = adapter if adapter is not None else judge._default_adapter()
    if llm is None:
        raise RuntimeError("当前没有可用的大模型适配器，无法自动整理语料库。")

    user_prompt = (
        f"目标角色：{char_name}（指针 ID：{pointer.id}）\n"
        f"请从下面资料中整理最多 {max_pairs} 组最能体现「{char_name}」说话口吻与反应逻辑的对话对：\n\n"
        f"{text[:12000]}"
    )
    raw_reply = _call_llm(llm, _SYSTEM_PROMPT, user_prompt)
    new_pairs = _parse_pairs_json(raw_reply)
    if not new_pairs:
        raise ValueError("大模型未能从这段资料中提取出有效对话对，请检查内容是否包含角色台词或对话文本。")

    target_path = corpus_path_for_pointer(registry, pointer_id)
    target_path.parent.mkdir(parents=True, exist_ok=True)

    merged: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    clean_mode = str(mode or "append").strip().lower()
    if clean_mode != "replace" and target_path.is_file():
        existing = acg_corpus.load_corpus_file(target_path)
        for b, arr in existing.items():
            for u, a in arr:
                if (u, a) not in seen:
                    seen.add((u, a))
                    merged.append((b, u, a))

    added = 0
    for b, u, a in new_pairs:
        if (u, a) in seen:
            continue
        seen.add((u, a))
        merged.append((b, u, a))
        added += 1

    merged.sort(key=lambda x: (ALLOWED_BUCKETS.index(x[0]) if x[0] in ALLOWED_BUCKETS else 99, x[1]))
    lines = [f"# 角色专属语料库（指针：{pointer.id} · 角色：{char_name} · 由用户资料整理）"]
    for b, u, a in merged:
        lines.append(f"{b}\t{u}\t{a}")
    target_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return {
        "pointer_id": pointer.id,
        "character_name": char_name,
        "path": str(target_path),
        "added": added,
        "total": len(merged),
        "message": f"已为「{char_name}」（指针 {pointer.id}）整理并写入 {added} 条新语料（当前共 {len(merged)} 条 → {target_path.name}），下一轮对话立即生效。",
    }
