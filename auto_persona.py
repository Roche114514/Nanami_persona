"""全自动角色构建流水线：百科资料 + B站语音本地ASR + 6桶配额鉴真填充 + 人格壳/心理/社会学全模型生成。"""

from __future__ import annotations

import json
import logging
import re
import shutil
import tempfile
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import yaml

from . import (
    bili_asr,
    corpus_builder,
    judge,
    mind_autofill,
    profiles,
    wiki_crawler,
)

logger = logging.getLogger(__name__)

_THINK_RE = re.compile(r"<think>[\s\S]*?</think>", re.IGNORECASE)
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)
_VALID_ARCHETYPES = ("kuudere_sleepy", "calm_logical", "genki_warm", "tsundere_sharp")

_ARCHETYPE_QUOTAS: dict[str, dict[str, tuple[int, int]]] = {
    "tsundere_sharp": {
        "casual": (3, 5), "curious": (2, 4), "cold": (1, 3),
        "annoyed": (4, 6), "reject": (3, 5), "repair_afterglow": (1, 3),
    },
    "kuudere_sleepy": {
        "casual": (3, 5), "curious": (3, 5), "cold": (3, 5),
        "annoyed": (1, 3), "reject": (2, 4), "repair_afterglow": (2, 4),
    },
    "genki_warm": {
        "casual": (4, 6), "curious": (3, 5), "cold": (1, 3),
        "annoyed": (1, 3), "reject": (1, 3), "repair_afterglow": (3, 5),
    },
    "calm_logical": {
        "casual": (3, 5), "curious": (3, 5), "cold": (2, 4),
        "annoyed": (2, 4), "reject": (2, 4), "repair_afterglow": (2, 4),
    },
}

_STAGE_DIRECTION_RULE = {
    "id": "stage_direction",
    "pattern": r"[（(][^）)\n]{1,40}[）)]",
    "reason": "禁止括号舞台指示",
    "skip_if_second_person": False,
    "unconditional": False,
}

_SOUL_GEN_SYSTEM = """\
你是顶尖的二次元角色人格架构师。请根据提供的「角色百科资料」与「已收集的原典语料样本」，为角色编写一份精炼、准确、可直接注入对话引擎的人格壳（soul.md）与守卫配置。

编写原则（务必严格遵守）：
1. **只描述「她/他是谁、核心渴望、知识边界、场景反应逻辑」**，绝不写死板机械的「怎么接话公式」或「教她怎么说话」的条款（说话语气由专属语料库 `corpus.tsv` 承担）。
2. **禁止在人格壳里写任何括号舞台指示示范**（例如 `（叹气）`），所有动作若出现必须自然融在话里。
3. **`soul_markdown` 必须包含以下结构与路由标记（Markdown 格式）**：
   - `# <角色名> · 人格壳`
   - `## 画像`（核心身份、外貌要害、性格底色与边界，120–220字）
   - `## 4. 她要什么（开口动机）`（3–4 条要点）
   - `## 5. 她是谁`（出身、人际关系、标志性细节）
   - `## 7. 她懂什么`（专长领域与明确不了解的盲区边界）
   - `## 9. 场景段` 下面必须包含：
     - `### 9.1 被冒犯时` + `<!-- route: insulted -->`
     - `### 9.2 被追问隐私或本质时` + `<!-- route: identity -->`
     - `### 9.3 第一次见面` + `<!-- route: first_turn -->`
     - `### 9.4 称呼习惯` + `<!-- route: address -->`
4. 只输出合法 JSON，字段如下：
{
  "soul_markdown": "完整的 soul.md 正文",
  "present_needle": "用于防重复注入的2-8字特征词（如角色特有称号）",
  "corpus_archetype": "kuudere_sleepy | calm_logical | genki_warm | tsundere_sharp 四选一",
  "address_suffix": "对同龄人称呼后缀（如 同学，无后缀留空字符串）",
  "address_honorific": "对非同辈敬称（如 先生，无敬称留空字符串）",
  "address_gap_turns": 8,
  "max_dialog_items": 3,
  "max_speech_chars": 42,
  "judge_prompt": "1-2句具体判定准则：怎样像 AI 客服/念台词，怎样才是这个角色本人自然聊天",
  "forbidden_patterns": [
    {"id": "ooc_example", "pattern": "绝对不会说的口头禅或越界词正则", "reason": "简短原因", "skip_if_second_person": false, "unconditional": false}
  ]
}
"""


def _notify(cb: Callable[[str], None] | None, msg: str) -> None:
    logger.info("nanami_persona: %s", msg)
    if cb is not None:
        try:
            cb(msg)
        except Exception:
            pass


def _parse_json_dict(raw: str) -> dict[str, Any]:
    cleaned = _THINK_RE.sub("", str(raw or "")).strip()
    cleaned = _FENCE_RE.sub("", cleaned).strip()
    for cand in judge._json_candidates(cleaned):
        try:
            obj = json.loads(cand)
        except (TypeError, ValueError):
            continue
        if isinstance(obj, dict) and obj:
            return obj
    raise ValueError("大模型未能返回有效 JSON 配置")


def _write_guard_and_profile_fields(
    registry: profiles.PointerRegistry,
    pointer_id: str,
    *,
    character_name: str,
    archetype: str,
    present_needle: str,
    address_suffix: str,
    address_honorific: str,
    address_gap_turns: int,
    max_dialog_items: int,
    max_speech_chars: int,
    judge_prompt: str,
    forbidden_patterns: list[dict[str, Any]],
) -> Path:
    pointer = registry.get_pointer(pointer_id)
    if pointer is None:
        raise ValueError(f"未找到角色指针：{pointer_id}")
    pdir = registry.ensure_editable_profile_dir(pointer)
    yaml_path = pdir / profiles.PROFILE_FILE
    if yaml_path.is_file():
        data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            data = {}
    else:
        data = {}

    data["id"] = pointer.id
    data["display_name"] = character_name
    data["character_name"] = character_name
    data["corpus_archetype"] = archetype if archetype in _VALID_ARCHETYPES else "calm_logical"

    soul_sec = dict(data.get("soul") or {})
    soul_sec.setdefault("file", "soul.md")
    soul_sec.setdefault("marker", "【角色人格】")
    clean_needle = str(present_needle or "").strip()[:24]
    if clean_needle and (len(clean_needle) < 4 or clean_needle in character_name):
        clean_needle = ""
    soul_sec["present_needle"] = clean_needle
    soul_sec["route_mode"] = "auto"
    data["soul"] = soul_sec

    mem_sec = dict(data.get("memory") or {})
    mem_sec["learn_user_name"] = True
    mem_sec["inject"] = True
    mem_sec["remember_rejections"] = True
    mem_sec["address_suffix"] = address_suffix
    mem_sec["address_honorific"] = address_honorific
    mem_sec["address_gap_turns"] = max(0, min(60, int(address_gap_turns)))
    data["memory"] = mem_sec

    cnt_sec = dict(data.get("contract") or {})
    cnt_sec["enabled"] = True
    cnt_sec["relax_array_min"] = True
    cnt_sec["max_dialog_items"] = max(1, min(8, int(max_dialog_items)))
    cnt_sec["max_speech_chars"] = max(15, min(160, int(max_speech_chars)))
    data["contract"] = cnt_sec

    guard_sec = dict(data.get("guard") or {})
    guard_sec["enabled"] = True
    guard_sec["mode"] = "clean"
    guard_sec.setdefault("max_chars", 120)

    merged_patterns: list[dict[str, Any]] = [dict(_STAGE_DIRECTION_RULE)]
    seen_ids = {"stage_direction"}
    for item in forbidden_patterns:
        if not isinstance(item, Mapping):
            continue
        pat = str(item.get("pattern") or "").strip()
        if not pat:
            continue
        # 校验正则可编译
        try:
            re.compile(pat)
        except re.error:
            continue
        pid = re.sub(r"[^A-Za-z0-9_]+", "_", str(item.get("id") or "custom")).strip("_") or "custom"
        if pid in seen_ids:
            pid = f"{pid}_{len(merged_patterns)}"
        seen_ids.add(pid)
        merged_patterns.append({
            "id": pid,
            "pattern": pat,
            "reason": str(item.get("reason") or "角色违和词过滤").strip()[:60],
            "skip_if_second_person": bool(item.get("skip_if_second_person", False)),
            "unconditional": bool(item.get("unconditional", False)),
        })
    guard_sec["forbidden_patterns"] = merged_patterns
    data["guard"] = guard_sec

    judge_sec = dict(data.get("judge") or {})
    judge_sec["enabled"] = True
    judge_sec["ai_flavor"] = "clean"
    judge_sec.setdefault("lead_trim", False)
    judge_sec.setdefault("timeout", 4.5)
    if judge_prompt:
        judge_sec["system_prompt"] = judge_prompt
    data["judge"] = judge_sec

    header = f"# {character_name} · 角色包配置（由全自动人格建模流水线生成）\n\n"
    yaml_path.write_text(
        header + yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    with registry._lock:
        registry._profile_cache.clear()
    return yaml_path


def generate_soul_and_guard_from_dossier(
    registry: profiles.PointerRegistry,
    pointer_id: str,
    dossier: wiki_crawler.CharacterDossier,
    corpus_pairs: Sequence[tuple[str, str, str]],
    *,
    adapter: Any = None,
) -> dict[str, Any]:
    """根据百科档案与已收集的原典语料生成 soul.md 与 profile.yaml。"""
    llm = adapter if adapter is not None else judge._default_adapter()
    if llm is None:
        raise RuntimeError("当前没有可用的大模型适配器，无法生成人格壳。")

    sample_lines = "\n".join(
        f"- [{b}] 对方：{u} → {dossier.character_name}：{a}"
        for b, u, a in list(corpus_pairs)[:24]
    )
    avg_len = (
        round(sum(len(a) for _b, _u, a in corpus_pairs) / max(1, len(corpus_pairs)))
        if corpus_pairs
        else 20
    )
    suggested_max_chars = max(24, min(90, int(avg_len * 2.1)))

    user_prompt = (
        f"目标角色：{dossier.character_name}\n"
        f"实测原典语料平均句长：{avg_len} 字（建议 max_speech_chars ≈ {suggested_max_chars}）\n\n"
        f"=== 角色百科资料 ===\n{dossier.to_prompt_text(5500)}\n\n"
        f"=== 已收集的原典台词样本 ===\n{sample_lines or '（暂无样本，仅依设定构建）'}"
    )
    raw = corpus_builder._call_llm(llm, _SOUL_GEN_SYSTEM, user_prompt, max_tokens=2800)
    data = _parse_json_dict(raw)

    soul_md = str(data.get("soul_markdown") or "").strip()
    if not soul_md:
        soul_md = (
            f"# {dossier.character_name} · 人格壳\n\n"
            f"## 画像\n\n{dossier.summary or dossier.character_name}\n\n"
            f"## 9. 场景段\n\n"
            f"### 9.1 被冒犯时\n<!-- route: insulted -->\n按自身性格直接回应或冷处理。\n\n"
            f"### 9.3 第一次见面\n<!-- route: first_turn -->\n还不认识对方，简短回应即可，不查户口。\n"
        )

    ok, save_msg = registry.save_soul_text(pointer_id, soul_md)
    if not ok:
        raise RuntimeError(f"保存 soul.md 失败：{save_msg}")

    arch = str(data.get("corpus_archetype") or "calm_logical").strip()
    if arch not in _VALID_ARCHETYPES:
        arch = "calm_logical"

    raw_forbidden = data.get("forbidden_patterns")
    forbidden_list = list(raw_forbidden) if isinstance(raw_forbidden, list) else []
    yaml_path = _write_guard_and_profile_fields(
        registry,
        pointer_id,
        character_name=dossier.character_name,
        archetype=arch,
        present_needle=str(data.get("present_needle") or "").strip()[:24],
        address_suffix=str(data.get("address_suffix") or "").strip()[:12],
        address_honorific=str(data.get("address_honorific") or "").strip()[:12],
        address_gap_turns=int(data.get("address_gap_turns") or 8),
        max_dialog_items=int(data.get("max_dialog_items") or 3),
        max_speech_chars=int(data.get("max_speech_chars") or suggested_max_chars),
        judge_prompt=str(data.get("judge_prompt") or "").strip()[:300],
        forbidden_patterns=forbidden_list,
    )
    return {
        "soul_markdown": soul_md,
        "soul_chars": len(soul_md),
        "corpus_archetype": arch,
        "profile_yaml": str(yaml_path),
    }


def _resolve_target_pointer(
    registry: profiles.PointerRegistry,
    character_name: str,
    pointer_id: str = "",
) -> profiles.PointerSpec:
    pid = str(pointer_id or "").strip()
    cname = str(character_name or "").strip()
    if pid:
        existing = registry.get_pointer(pid)
        if existing is not None:
            registry.set_active_edit(existing.id)
            if cname and existing.character_name != cname:
                registry.update_pointer(existing.id, {"character_name": cname})
                existing = registry.get_pointer(existing.id) or existing
            return existing
        return registry.scaffold_pointer(
            new_id=pid,
            character_name=cname or pid,
            source_id=profiles.TEMPLATE_DIR_NAME,
        )

    for one in registry.list_pointers():
        if one.character_name == cname and one.id != profiles.DEFAULT_PRIMARY_ID:
            registry.set_active_edit(one.id)
            return one

    return registry.scaffold_pointer(
        character_name=cname,
        source_id=profiles.TEMPLATE_DIR_NAME,
    )


def auto_build_character(
    registry: profiles.PointerRegistry,
    character_name: str,
    *,
    work_name: str = "",
    pointer_id: str = "",
    bvid_list: Sequence[str] = (),
    local_audio_files: Sequence[str | Path] = (),
    raw_corpus_text: str = "",
    extra_dossier_text: str = "",
    use_bili: bool = True,
    max_videos: int = 6,
    max_audio_sec: int = 300,
    language: str = "auto",
    min_confidence: float = 0.85,
    quotas: Mapping[str, tuple[int, int]] | None = None,
    fill_tail_deficit: bool = False,
    corpus_mode: str = "replace",
    allow_network_wiki: bool = True,
    allow_whisper_download: bool = False,
    adapter: Any = None,
    bili_search_fn: Callable[[str, str, int], list[dict[str, str]]] | None = None,
    transcript_fn: Callable[..., dict[str, Any]] | None = None,
    local_asr_fn: Callable[..., list[dict[str, Any]]] | None = None,
    progress_cb: Callable[[str], None] | None = None,
    state_cb: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """一键全自动角色建模：爬资料 → B站/本地ASR按6桶配额鉴真填充 → 生成人格壳、守卫与心理/社会学参数。"""
    cname = str(character_name or "").strip()
    if not cname:
        raise ValueError("必须提供目标角色名（character_name）")

    llm = adapter if adapter is not None else judge._default_adapter()
    if llm is None:
        raise RuntimeError("未检测到可用的宿主大模型适配器，请先配置 API。")

    # 1. 定位或新建独立角色指针
    ptr = _resolve_target_pointer(registry, cname, pointer_id=pointer_id)
    _notify(progress_cb, f"[1/5] 已锁定角色指针：{ptr.id}（角色：{cname}）")

    def _emit_state(
        phase: str,
        *,
        tracker_obj: corpus_builder.BucketTracker | None = None,
        depth: int = 0,
        max_depth: int = 4,
        current_item: str = "",
        log_msg: str = "",
    ) -> None:
        if log_msg:
            _notify(progress_cb, log_msg)
        if state_cb is None:
            return
        try:
            state_cb({
                "pointer_id": ptr.id,
                "character_name": cname,
                "phase": phase,
                "depth": int(depth),
                "max_depth": int(max_depth),
                "current_item": current_item,
                "counts": tracker_obj.counts() if tracker_obj else {b: 0 for b in corpus_builder.ALLOWED_BUCKETS},
                "quotas": {b: list(v) for b, v in (tracker_obj.quotas if tracker_obj else corpus_builder.DEFAULT_BUCKET_QUOTAS).items()},
                "deficits": tracker_obj.deficit_map() if tracker_obj else {},
                "total": tracker_obj.total() if tracker_obj else 0,
                "target_min": tracker_obj.target_min_total() if tracker_obj else 25,
                "complete": tracker_obj.is_complete() if tracker_obj else False,
                "log": log_msg,
            })
        except Exception:
            pass

    _emit_state("锁定角色指针", current_item=f"{cname} ({ptr.id})")

    # 2. 爬取角色百科资料并提炼《声纹鉴别卡》
    _emit_state("检索百科资料", current_item=f"萌娘百科 / Bangumi：{cname}")
    dossier = wiki_crawler.collect_character_dossier(
        cname,
        work_name=work_name,
        registry=registry,
        extra_text=extra_dossier_text,
        allow_network=allow_network_wiki,
    )
    dossier_text = dossier.to_prompt_text(6500)
    _emit_state(
        "提炼角色声纹鉴别卡",
        current_item=f"来源：{', '.join(dossier.sources) or '仅角色名'}",
        log_msg=f"[2/5] 资料汇集完成（来源：{', '.join(dossier.sources) or '仅角色名'}；原典语录 {len(dossier.canon_quotes)} 条）",
    )

    fingerprint = corpus_builder.build_speaker_fingerprint(
        cname,
        dossier_text,
        adapter=llm,
        glossary_hints=dossier.vocabulary,
    )

    # 3. 性格动态配额初始化与第 0 轮填充（补充文本 + 百科原典语录）
    effective_quotas = quotas or _ARCHETYPE_QUOTAS.get(str(fingerprint.get("archetype") or ""))
    tracker = corpus_builder.BucketTracker(quotas=effective_quotas)
    if str(corpus_mode or "replace").strip().lower() != "replace":
        existing_path = corpus_builder.corpus_path_for_pointer(registry, ptr.id)
        if existing_path.is_file():
            tracker.load_existing(corpus_builder.acg_corpus.load_corpus_file(existing_path))

    if raw_corpus_text and str(raw_corpus_text).strip():
        _notify(progress_cb, "[3/5] 先从补充台词文本提取语料…")
        corpus_builder.extract_bucket_pairs_for_deficits(
            cname,
            raw_corpus_text,
            tracker,
            fingerprint=fingerprint,
            source_hint="用户提供的原典台词文本",
            adapter=llm,
            min_confidence=min_confidence,
        )

    if not tracker.is_complete() and dossier.canon_quotes:
        _notify(progress_cb, f"[3/5] 从百科抽取到的 {len(dossier.canon_quotes)} 条语录做第 0 轮填桶…")
        quote_blob = "\n".join(
            f"[{ctx or '原典'}] {cname}：「{q}」" for ctx, q in dossier.canon_quotes
        )
        corpus_builder.extract_bucket_pairs_for_deficits(
            cname,
            quote_blob,
            tracker,
            fingerprint=fingerprint,
            source_hint="萌娘百科/语音表原典语录",
            adapter=llm,
            min_confidence=min_confidence,
        )

    # 4. 本地音频文件转写填桶（如用户提供）
    for audio_item in local_audio_files or ():
        if tracker.is_complete():
            _notify(progress_cb, "[3/5] 6 个情境桶已全数达标，立即停止读取剩余音频。")
            break
        ap = Path(audio_item)
        if not ap.is_file():
            continue
        _notify(progress_cb, f"[3/5] 转写本地音频：{ap.name}")
        tr = bili_asr.extract_local_audio_transcript(
            ap,
            language=language,
            max_duration_sec=max_audio_sec,
            allow_whisper_download=allow_whisper_download,
            asr_fn=local_asr_fn,
        )
        if tr.get("text"):
            corpus_builder.extract_bucket_pairs_for_deficits(
                cname,
                tr["text"],
                tracker,
                fingerprint=fingerprint,
                source_hint=f"本地音频 ASR ({ap.name})",
                adapter=llm,
                min_confidence=min_confidence,
            )

    # 5. 递归增量采集：递归出口 = tracker.is_complete()（全部 6 类情境桶均已扫描填满）
    effective_work = work_name or dossier.work_name
    videos_scanned: list[dict[str, str]] = []
    collected_clip_texts: list[tuple[str, str]] = []
    if not tracker.is_complete() and (use_bili or bvid_list):
        situation_query_hints: dict[str, tuple[str, ...]] = {
            "casual": ("日常 片段", "对话 合集"),
            "curious": ("询问 互动 片段", "名场面"),
            "cold": ("冷漠 沉默 片段", "回避 片段"),
            "annoyed": ("生气 吐槽 破防", "吵架 名场面"),
            "reject": ("拒绝 决裂 片段", "告白 拒绝"),
            "repair_afterglow": ("道歉 和好 温柔", "坦白 名场面"),
        }
        candidates: list[dict[str, str]] = []
        seen_bv: set[str] = set()
        for raw_bv in bvid_list or ():
            val = str(raw_bv or "").strip()
            if not val:
                continue
            m = re.search(r"(BV[A-Za-z0-9]+)", val, re.I)
            bv = m.group(1) if m else val
            if bv not in seen_bv:
                seen_bv.add(bv)
                candidates.append({"title": bv, "bvid": bv, "url": val})

        rescanned_clip_count = 0
        # 已缓存长视频切片游标：{bvid: {"raw_path": Path, "title": str, "next_sec": int, "dur_sec": int, "productive": bool}}
        cached_compilations: dict[str, dict[str, Any]] = {}
        slice_sec = min(150, max(60, int(max_audio_sec)))
        last_flushed_total = tracker.total()
        scan_counter = {"n": 0, "productive": 0}
        target_productive = max(1, int(max_videos))
        max_total_scans = max(target_productive * 2, 10)

        def _flush_incremental() -> None:
            nonlocal last_flushed_total
            cur_total = tracker.total()
            if cur_total > last_flushed_total:
                last_flushed_total = cur_total
                corpus_builder.write_tracker_corpus(
                    registry,
                    ptr.id,
                    tracker,
                    mode=corpus_mode,
                    note=f"自动构建增量写入（百科+B站ASR · 共 {cur_total} 条）",
                )

        def _fetch_candidates_for_depth(depth: int) -> list[dict[str, str]]:
            added_cands: list[dict[str, str]] = []
            if not use_bili:
                return added_cands
            search_limit = max(max_videos * 2, 12)
            try:
                if bili_search_fn is not None:
                    if depth == 0:
                        searched = bili_search_fn(cname, effective_work, search_limit)
                    else:
                        return added_cands
                elif depth == 0:
                    searched = bili_asr.search_character_bili_videos(
                        cname,
                        work_name=effective_work,
                        aliases=dossier.aliases,
                        limit=search_limit,
                    )
                else:
                    deficits = list(tracker.deficit_map().keys())
                    extra_qs: list[str] = []
                    for b in deficits[:3]:
                        for kw in situation_query_hints.get(b, ()):
                            prefix = f"{effective_work} {cname}" if effective_work else cname
                            extra_qs.append(f"{prefix} {kw}")
                    if not extra_qs:
                        return added_cands
                    searched = bili_asr.search_character_bili_videos(
                        cname,
                        work_name=effective_work,
                        aliases=dossier.aliases,
                        limit=search_limit,
                        page=depth + 1,
                        extra_queries=extra_qs[:4],
                    )
                for row in searched:
                    bv = str(row.get("bvid") or "").strip()
                    if bv and bv not in seen_bv:
                        seen_bv.add(bv)
                        candidates.append(row)
                        added_cands.append(row)
            except Exception as exc:
                _notify(progress_cb, f"[3/5] 递归第 {depth} 层 B 站检索跳过：{exc}")
            return added_cands

        def _recursive_fill_corpus(tmp_root: Path, depth: int = 0, max_depth: int = 4) -> None:
            # 递归出口 1：全部 6 个情境桶均已扫描填满
            if tracker.is_complete():
                _emit_state("递归出口达成", tracker_obj=tracker, depth=depth, max_depth=max_depth, log_msg="[3/5] 递归出口达成：所有情境语料均已扫描填满！")
                return
            # 递归出口 2：安全深度/扫描上限兜底，防止死循环
            if depth >= max_depth or scan_counter["n"] >= max_total_scans:
                _emit_state("递归收敛停止", tracker_obj=tracker, depth=depth, max_depth=max_depth, log_msg=f"[3/5] 递归收敛停止（depth={depth}，当前缺口：{tracker.deficit_map()}）")
                return

            made_progress = False

            # A. 深度>0时，优先对已有产出的长合集音频继续向后切片（零下载耗时 + 常驻 Whisper）
            if depth > 0 and transcript_fn is None:
                for bv, cinfo in list(cached_compilations.items()):
                    if tracker.is_complete():
                        return
                    if not cinfo.get("productive"):
                        continue
                    next_s = int(cinfo.get("next_sec") or 0)
                    dur_s = int(cinfo.get("dur_sec") or 0)
                    raw_p = Path(cinfo["raw_path"])
                    if not raw_p.is_file() or next_s >= min(dur_s or 720, 720):
                        continue
                    cinfo["next_sec"] = next_s + slice_sec
                    _emit_state(
                        f"递归深挖第 {depth} 层",
                        tracker_obj=tracker,
                        depth=depth,
                        max_depth=max_depth,
                        current_item=f"续切合集「{cinfo['title']}」[{next_s}s–{next_s + slice_sec}s]",
                        log_msg=f"[3/5] 递归深挖第 {depth} 层：续切长合集「{cinfo['title']}」[{next_s}s–{next_s + slice_sec}s]（缺口：{tracker.deficit_map()}）",
                    )
                    try:
                        tr_slice = bili_asr.extract_local_audio_transcript(
                            raw_p,
                            language=language,
                            start_sec=next_s,
                            max_duration_sec=slice_sec,
                            allow_whisper_download=allow_whisper_download,
                            asr_fn=local_asr_fn,
                        )
                        txt_slice = str(tr_slice.get("text") or "").strip()
                        if txt_slice and corpus_builder.has_enough_dialogue(txt_slice):
                            made_progress = True
                            src_hint = f"B站合集续切({next_s}s): {cinfo['title']}"
                            collected_clip_texts.append((src_hint, txt_slice))
                            ext_res = corpus_builder.extract_bucket_pairs_for_deficits(
                                cname,
                                txt_slice,
                                tracker,
                                fingerprint=fingerprint,
                                source_hint=src_hint,
                                adapter=llm,
                                min_confidence=min_confidence,
                                chunk_chars=2400,
                                max_chunks=4,
                            )
                            _flush_incremental()
                            _emit_state(
                                f"递归深挖第 {depth} 层",
                                tracker_obj=tracker,
                                depth=depth,
                                max_depth=max_depth,
                                current_item=f"已完成续切「{cinfo['title']}」[{next_s}s–{next_s + slice_sec}s]",
                                log_msg=f"[3/5] 续切抽取 +{int((ext_res or {}).get('added') or 0)} 条（总计 {tracker.total()} 条）",
                            )
                            if int((ext_res or {}).get("added") or 0) >= 1:
                                cinfo["productive"] = True
                            else:
                                cinfo["productive"] = False
                        elif txt_slice:
                            cinfo["productive"] = False
                            _notify(progress_cb, f"[3/5] 续切「{cinfo['title']}」有效对白过稀，快速跳过 LLM")
                    except Exception as exc:
                        _notify(progress_cb, f"[3/5] 合集 {bv} 续切跳过：{exc}")

            if tracker.is_complete():
                _emit_state("递归出口达成", tracker_obj=tracker, depth=depth, max_depth=max_depth, log_msg="[3/5] 递归出口达成：所有情境语料均已扫描填满！")
                return

            # B. 按当前缺口拉取/消费新一轮候选视频（实网环境下重叠预取下一条下载+ASR）
            _emit_state(
                f"递归第 {depth} 层检索中",
                tracker_obj=tracker,
                depth=depth,
                max_depth=max_depth,
                current_item=f"B 站检索第 {depth} 层候选动画切片…",
            )
            _fetch_candidates_for_depth(depth)
            pending = [c for c in candidates if not c.get("_consumed")]
            batch_cap = 3 if depth == 0 else 4
            batch = pending[:batch_cap]

            def _fetch_tr(cand_item: dict[str, str], vdir_path: Path, *, silent: bool = False) -> dict[str, Any]:
                if transcript_fn is not None:
                    return transcript_fn(cand_item.get("url") or cand_item["bvid"], vdir_path, language=language, max_duration_sec=slice_sec)
                cb = None if silent else (lambda msg: _emit_state(
                    f"递归第 {depth} 层采集",
                    tracker_obj=tracker,
                    depth=depth,
                    max_depth=max_depth,
                    log_msg=msg,
                ))
                return bili_asr.extract_video_transcript(
                    cand_item.get("url") or cand_item["bvid"],
                    vdir_path,
                    language=language,
                    max_duration_sec=slice_sec,
                    allow_whisper_download=allow_whisper_download,
                    asr_fn=local_asr_fn,
                    progress_cb=cb,
                )

            prefetch_pool = ThreadPoolExecutor(max_workers=1) if transcript_fn is None else None
            next_future: tuple[str, Future[dict[str, Any]]] | None = None
            try:
                for idx, cand in enumerate(batch):
                    if tracker.is_complete():
                        _emit_state("递归出口达成", tracker_obj=tracker, depth=depth, max_depth=max_depth, log_msg="[3/5] 递归出口达成：所有情境语料均已扫描填满！")
                        return
                    if scan_counter["productive"] >= target_productive or scan_counter["n"] >= max_total_scans:
                        break
                    cand["_consumed"] = True
                    bv = cand["bvid"]
                    vtitle = cand.get("title") or bv
                    scan_counter["n"] += 1
                    vdir = tmp_root / f"v_{scan_counter['n']}"
                    _emit_state(
                        f"递归第 {depth} 层采集",
                        tracker_obj=tracker,
                        depth=depth,
                        max_depth=max_depth,
                        current_item=f"第 {scan_counter['n']} 个：{vtitle}",
                        log_msg=f"[3/5] 递归第 {depth} 层 · 第 {scan_counter['n']} 个视频（已有效产出 {scan_counter['productive']}/{target_productive}）：「{vtitle}」（当前缺口：{tracker.deficit_map()}）",
                    )
                    try:
                        if next_future is not None and next_future[0] == bv:
                            tr = next_future[1].result()
                            next_future = None
                        else:
                            tr = _fetch_tr(cand, vdir)
                        videos_scanned.append({
                            "bvid": bv,
                            "title": str(tr.get("title") or vtitle),
                            "source": str(tr.get("source") or "asr"),
                        })
                        txt = str(tr.get("text") or "").strip()

                        # 若仍明显缺多个桶且还有下一条候选，提前在后台重叠下载+ASR下一条
                        if (
                            prefetch_pool is not None
                            and idx + 1 < len(batch)
                            and len(tracker.deficit_map()) >= 2
                            and scan_counter["n"] + 1 <= max_total_scans
                        ):
                            nxt = batch[idx + 1]
                            nxt_dir = tmp_root / f"v_{scan_counter['n'] + 1}"
                            next_future = (nxt["bvid"], prefetch_pool.submit(_fetch_tr, nxt, nxt_dir, silent=True))

                        if txt and corpus_builder.has_enough_dialogue(txt):
                            made_progress = True
                            src_hint = f"B站视频({tr.get('source', 'asr')}): {tr.get('title') or bv}"
                            collected_clip_texts.append((src_hint, txt))
                            ext_res = corpus_builder.extract_bucket_pairs_for_deficits(
                                cname,
                                txt,
                                tracker,
                                fingerprint=fingerprint,
                                source_hint=src_hint,
                                adapter=llm,
                                min_confidence=min_confidence,
                                chunk_chars=2400,
                                max_chunks=4,
                            )
                            _flush_incremental()
                            added_cnt = int((ext_res or {}).get("added") or 0)
                            _emit_state(
                                f"递归第 {depth} 层采集",
                                tracker_obj=tracker,
                                depth=depth,
                                max_depth=max_depth,
                                current_item=f"已筛选「{vtitle}」(+{added_cnt} 条)",
                                log_msg=f"[3/5] 「{vtitle}」抽取 +{added_cnt} 条（总计 {tracker.total()} 条，缺口：{tracker.deficit_map()}）",
                            )
                            if added_cnt >= 1:
                                scan_counter["productive"] += 1
                            raw_audio_p = str(tr.get("raw_audio_path") or "").strip()
                            dur_s = bili_asr._parse_duration_sec(cand.get("duration", ""))
                            if raw_audio_p and (dur_s == 0 or dur_s > slice_sec + 20):
                                cached_compilations[bv] = {
                                    "raw_path": raw_audio_p,
                                    "title": str(tr.get("title") or vtitle),
                                    "next_sec": slice_sec,
                                    "dur_sec": dur_s,
                                    "productive": added_cnt >= 1,
                                }
                        elif txt:
                            _emit_state(
                                f"递归第 {depth} 层采集",
                                tracker_obj=tracker,
                                depth=depth,
                                max_depth=max_depth,
                                log_msg=f"[3/5] 「{vtitle}」有效对白过稀（纯BGM/零碎词），快速熔断跳过 LLM",
                            )
                    except Exception as exc:
                        _emit_state(
                            f"递归第 {depth} 层采集",
                            tracker_obj=tracker,
                            depth=depth,
                            max_depth=max_depth,
                            log_msg=f"[3/5] 视频 {bv} 跳过：{exc}",
                        )
            finally:
                if next_future is not None:
                    next_future[1].cancel()
                if prefetch_pool is not None:
                    prefetch_pool.shutdown(wait=False)

            if tracker.is_complete():
                _emit_state("递归出口达成", tracker_obj=tracker, depth=depth, max_depth=max_depth, log_msg="[3/5] 递归出口达成：所有情境语料均已扫描填满！")
                return

            # C. 提前缺口精筛：第 1 层起若已攒够 >=6 条且只差 1–2 个冷门桶，立刻放宽阈值重筛已转写文本
            nonlocal rescanned_clip_count
            cur_deficits = tracker.deficit_map()
            if (
                depth >= 1
                and tracker.total() >= 6
                and 0 < len(cur_deficits) <= 2
                and rescanned_clip_count < len(collected_clip_texts)
            ):
                _emit_state(
                    f"第 {depth} 层提前缺口精筛",
                    tracker_obj=tracker,
                    depth=depth,
                    max_depth=max_depth,
                    current_item=f"对已转写切片精筛冷门缺口：{cur_deficits}",
                    log_msg=f"[3/5] 只差 {len(cur_deficits)} 个冷门桶 {cur_deficits}，提前对已转写文本启动放宽精筛…",
                )
                relaxed_conf = max(0.78, float(min_confidence) - 0.06)
                for src_hint, txt in collected_clip_texts[rescanned_clip_count:]:
                    if tracker.is_complete():
                        break
                    corpus_builder.extract_bucket_pairs_for_deficits(
                        cname,
                        txt,
                        tracker,
                        fingerprint=fingerprint,
                        source_hint=src_hint,
                        adapter=llm,
                        min_confidence=relaxed_conf,
                        chunk_chars=2400,
                        max_chunks=4,
                        only_deficit=True,
                    )
                rescanned_clip_count = len(collected_clip_texts)
                _flush_incremental()
                if tracker.is_complete():
                    _emit_state("递归出口达成", tracker_obj=tracker, depth=depth, max_depth=max_depth, log_msg="[3/5] 提前精筛达成递归出口：所有情境语料均已填满！")
                    return

            has_more_pending = any(not c.get("_consumed") for c in candidates)
            has_more_slices = any(
                cinfo.get("productive") and int(cinfo.get("next_sec") or 0) < min(int(cinfo.get("dur_sec") or 720), 720)
                for cinfo in cached_compilations.values()
            )
            if made_progress or has_more_pending or has_more_slices:
                _recursive_fill_corpus(tmp_root, depth=depth + 1, max_depth=max_depth)

        tmp_root = Path(tempfile.mkdtemp(prefix="nanami_bili_"))
        try:
            _recursive_fill_corpus(tmp_root, depth=0, max_depth=4)
        finally:
            # 后台预取线程可能仍在释放 Windows 临时音轨句柄，清理失败不阻塞已完成的主流程
            shutil.rmtree(tmp_root, ignore_errors=True)

        if not tracker.is_complete() and collected_clip_texts and tracker.deficit_map():
            _notify(progress_cb, f"[3/5] 启动已转写动画切片缺口精筛（缺口：{tracker.deficit_map()}）…")
            relaxed_conf = max(0.80, float(min_confidence) - 0.05)
            for src_hint, txt in collected_clip_texts:
                if tracker.is_complete() or not tracker.deficit_map():
                    break
                corpus_builder.extract_bucket_pairs_for_deficits(
                    cname,
                    txt,
                    tracker,
                    fingerprint=fingerprint,
                    source_hint=src_hint,
                    adapter=llm,
                    min_confidence=relaxed_conf,
                    chunk_chars=2400,
                    max_chunks=4,
                    only_deficit=True,
                )
            _flush_incremental()

    # 6. 若仍低于语料数量下限（默认 24 条 / 6 桶共 28 条）且已有真实声纹锚点，按原典声纹锚点补足下限，避免语料过少导致说话失真
    tail_added = 0
    if not tracker.is_complete() and (fill_tail_deficit or (tracker.total() < corpus_builder.MIN_CORPUS_TOTAL and tracker.total() >= 3)):
        _notify(progress_cb, f"[3/5] 语料不足下限 {corpus_builder.MIN_CORPUS_TOTAL} 条（当前 {tracker.total()} 条，缺口 {tracker.deficit_map()}），按已核实原典声纹锚点补齐…")
        tail_added = corpus_builder.fill_tail_deficits_from_canon(
            cname, tracker, dossier_text, adapter=llm
        )

    if tracker.total() > 0:
        purged = corpus_builder.purify_tracker_corpus(
            cname, tracker, dossier_text, adapter=llm
        )
        if purged > 0:
            _notify(progress_cb, f"[3/5] 语料终审净化：已剔除 {purged} 条噪声/串台/OOC 条目（剩余 {tracker.total()} 条）")
            # 若净化后出现桶缺口，用已转写的切片再做一次缺口补抽，避免语料过稀导致口癖单调
            if not tracker.is_complete() and collected_clip_texts and tracker.deficit_map():
                relaxed_conf = max(0.80, float(min_confidence) - 0.05)
                for src_hint, txt in collected_clip_texts:
                    if tracker.is_complete() or not tracker.deficit_map():
                        break
                    corpus_builder.extract_bucket_pairs_for_deficits(
                        cname,
                        txt,
                        tracker,
                        fingerprint=fingerprint,
                        source_hint=src_hint,
                        adapter=llm,
                        min_confidence=relaxed_conf,
                        chunk_chars=2400,
                        max_chunks=3,
                        only_deficit=True,
                    )

    corpus_path = corpus_builder.corpus_path_for_pointer(registry, ptr.id)
    if tracker.total() > 0 or (str(corpus_mode or "replace").strip().lower() != "replace" and corpus_path.is_file()):
        note_tag = "仅收录真实抽取语料（零 AI 编造）" if tail_added == 0 else f"真实抽取 + {tail_added} 条原典锚点补齐"
        corpus_path = corpus_builder.write_tracker_corpus(
            registry,
            ptr.id,
            tracker,
            mode=corpus_mode,
            note=f"自动构建（百科+B站ASR · {note_tag} · 共 {tracker.total()} 条）",
        )
        _notify(progress_cb, f"[3/5] 专属语料库已落盘 → {corpus_path}（{tracker.format_status()}）")
    else:
        if corpus_path.is_file():
            corpus_path.unlink()
        _notify(progress_cb, "[3/5] 未提取到足够真实原典台词，暂不写入空 corpus.tsv（运行时将使用通用声线分库）")

    # 7. 生成人格壳 (soul.md) 与守卫配置 (profile.yaml)
    _emit_state("生成人格壳与守卫规则", tracker_obj=tracker, current_item="写入 soul.md 与 profile.yaml", log_msg="[4/5] 正在结合资料与真实语料生成人格壳与守卫规则…")
    all_pairs = tracker.all_pairs()
    soul_info = generate_soul_and_guard_from_dossier(
        registry,
        ptr.id,
        dossier,
        all_pairs,
        adapter=llm,
    )

    # 8. 推填 L1–L8 心理模型与 S1–S3 社会学三层参数
    _emit_state("推填心理与社会学参数", tracker_obj=tracker, current_item="推导 L1–L8 与 S1–S3 参数", log_msg="[5/5] 正在推填 L1–L8 心理参数与 S1–S3 社会学参数…")
    sample_preview = "\n".join(f"[{b}] {u} -> {a}" for b, u, a in all_pairs[:16])
    autofill_res = mind_autofill.autofill_from_soul(
        registry,
        ptr.id,
        soul_text=soul_info["soul_markdown"],
        extra_context=(f"{dossier_text[:1500]}\n\n代表性语料：\n{sample_preview}" if sample_preview else dossier_text[:1500]),
        adapter_provider=lambda: llm,
        apply=True,
    )

    counts = tracker.counts()
    msg = (
        f"已完成「{cname}」（指针 {ptr.id}）全自动建模："
        f"写入 {tracker.total()} 条专属语料（{'完整达标' if tracker.is_complete() else '部分达标'}），"
        f"推填 {autofill_res['traits_count']} 项心理参数与 {autofill_res.get('social_count', 0)} 项社会学参数。"
    )
    _notify(progress_cb, msg)
    return {
        "pointer_id": ptr.id,
        "character_name": cname,
        "complete": tracker.is_complete(),
        "corpus_path": str(corpus_path),
        "corpus_counts": counts,
        "corpus_total": tracker.total(),
        "tail_added": tail_added,
        "rejected_stats": dict(tracker.rejected_stats),
        "videos_scanned": videos_scanned,
        "dossier_sources": list(dossier.sources),
        "soul_chars": soul_info["soul_chars"],
        "corpus_archetype": soul_info["corpus_archetype"],
        "profile_yaml": soul_info["profile_yaml"],
        "autofill": autofill_res,
        "message": msg,
    }
