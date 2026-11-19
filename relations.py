"""角色「态度与情感指针」（Attitude & Affect Pointer）读写与 LLM 一键关系推导。

每个角色指针 A 在 `<state_root>/state/relations.json` 中维护有向关系表：
  - 目标域严格闭环：`{"user"} ∪ {"char:<name>" | name 为角色包中的其他角色}`，
    绝不允许指向角色自己或自由文本幻觉实体；
  - 所有角色间关系默认从「不认识（known=False, rel_stage="stranger"）」起步；
  - 支持「一键让 LLM 填写与所有角色包角色的关系」：结合双方设定/soul.md 交叉考据，
    若原本不认识则跳过保持 stranger，若认识则填入关系定位、称呼、共同经历与态度情感初值。
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import mind_autofill

logger = logging.getLogger(__name__)

RELATIONS_FILE = "relations.json"

REL_STAGES: tuple[str, ...] = (
    "stranger",    # 不认识 / 初识
    "acquainted",  # 刚认识 / 有点熟
    "friend",      # 朋友 / 同伴
    "close",       # 挚友 / 信赖搭档
    "rival",       # 对手 / 冤家
    "guarded",     # 戒备 / 保持距离
    "hostile",     # 敌对 / 反感
)

ATTITUDE_STANCES: tuple[str, ...] = (
    "warm",     # 温和亲近
    "neutral",  # 礼貌自然
    "shy",      # 拘谨害羞
    "teasing",  # 轻松打趣
    "guarded",  # 防备克制
    "cold",     # 冷淡疏离
)

STAGE_LABELS: dict[str, str] = {
    "stranger": "不认识 / 陌生",
    "acquainted": "刚认识 / 有点熟",
    "friend": "友人 / 同伴",
    "close": "挚友 / 深交",
    "rival": "劲敌 / 冤家",
    "guarded": "戒备 / 疏远",
    "hostile": "排斥 / 敌对",
}

STANCE_LABELS: dict[str, str] = {
    "warm": "温和亲近",
    "neutral": "礼貌自然",
    "shy": "慢热拘谨",
    "teasing": "轻松打趣",
    "guarded": "防备试探",
    "cold": "冷淡拒人",
}

DEFAULT_CHARACTER_RELATION: dict[str, Any] = {
    "target_key": "",
    "target_type": "character",
    "target_name": "",
    "known": False,
    "rel_stage": "stranger",
    "attitude_stance": "neutral",
    "call_name": "",
    "relation_tag": "",
    "shared_memories": "",
    "attitude_score": 0.0,
    "affective_valence": 0.0,
    "trust": 0.55,
    "attraction": 0.35,
    "repulsion": 0.0,
    "warmth": 0.45,
    "social_willingness": 0.50,
    "exchange_balance": 0.0,
    "relational_debt": 0.0,
    "cool_off_turns": 0,
    "rounds": 0,
    "last_interaction": "",
    "manual_override": False,
    "updated_at": 0.0,
}


def relations_path(state_root: Path) -> Path:
    return Path(state_root) / "state" / RELATIONS_FILE


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return dict(raw) if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        logger.debug("nanami_persona: 读取 JSON 失败 %s", path, exc_info=True)
        return {}


def _clamp(val: Any, lo: float, hi: float, default: float) -> float:
    try:
        num = float(val)
    except (TypeError, ValueError):
        return default
    if num != num:  # NaN
        return default
    return round(max(lo, min(hi, num)), 3)


def _clamp_int(val: Any, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(hi, int(round(float(val)))))
    except (TypeError, ValueError):
        return default


def _clip_str(val: Any, limit: int, default: str = "") -> str:
    txt = str(val if val is not None else default).strip()
    return txt[:limit]


def valid_targets_for(
    registry: Any = None,
    subject_name: str = "",
    *,
    extra_characters: Sequence[str] | None = None,
) -> dict[str, dict[str, str]]:
    """严格枚举合法的指针目标：仅限 `user` + 角色包与指针表里的其他角色。"""
    subj = str(subject_name or "").strip()
    out: dict[str, dict[str, str]] = {
        "user": {
            "target_key": "user",
            "target_type": "user",
            "target_name": "用户",
            "brief": "当前对话与文字世界里的用户本人",
            "setting": "",
            "soul": "",
        }
    }

    def _add_char(name: str, *, brief: str = "", setting: str = "", soul: str = "") -> None:
        clean = str(name or "").strip()
        if not clean or clean == subj:
            return
        tkey = f"char:{clean}"
        cur = out.setdefault(
            tkey,
            {
                "target_key": tkey,
                "target_type": "character",
                "target_name": clean,
                "brief": "",
                "setting": "",
                "soul": "",
            },
        )
        if brief and not cur["brief"]:
            cur["brief"] = brief
        if setting and not cur["setting"]:
            cur["setting"] = setting
        if soul and not cur["soul"]:
            cur["soul"] = soul

    if registry is not None:
        if hasattr(registry, "list_host_characters"):
            try:
                for item in registry.list_host_characters() or ():
                    _add_char(
                        str(item.get("name") or ""),
                        brief=str(item.get("brief") or item.get("character_brief") or ""),
                        setting=str(item.get("setting") or item.get("character_setting") or ""),
                    )
            except Exception:
                logger.debug("nanami_persona: 枚举宿主角色列表失败", exc_info=True)
        if hasattr(registry, "list_pointers"):
            try:
                for ptr in registry.list_pointers() or ():
                    cname = str(getattr(ptr, "character_name", "") or "").strip()
                    if not cname:
                        continue
                    soul_txt = ""
                    if hasattr(registry, "read_soul_text"):
                        try:
                            soul_txt = str(registry.read_soul_text(ptr.id) or "").strip()
                        except Exception:
                            soul_txt = ""
                    _add_char(cname, soul=soul_txt)
            except Exception:
                logger.debug("nanami_persona: 枚举指针角色失败", exc_info=True)

    for cname in extra_characters or ():
        _add_char(str(cname or ""))

    return out


def coerce_relation_entry(
    target_key: str,
    target_meta: Mapping[str, Any] | None = None,
    raw: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """归一化单条态度与情感指针数据。"""
    base = copy.deepcopy(DEFAULT_CHARACTER_RELATION)
    src = dict(raw) if isinstance(raw, Mapping) else {}
    meta = dict(target_meta) if isinstance(target_meta, Mapping) else {}

    tkey = str(target_key or src.get("target_key") or "").strip()
    is_user = tkey == "user"
    ttype = "user" if is_user else "character"
    default_name = "用户" if is_user else (tkey.split(":", 1)[1] if ":" in tkey else tkey)
    tname = _clip_str(meta.get("target_name") or src.get("target_name"), 32, default_name)

    known = bool(src.get("known", False))
    rel_stage = str(src.get("rel_stage") or ("acquainted" if known else "stranger")).strip().lower()
    if rel_stage not in REL_STAGES:
        rel_stage = "acquainted" if known else "stranger"
    if not is_user:
        if not known:
            rel_stage = "stranger"
        elif rel_stage == "stranger":
            rel_stage = "acquainted"

    stance = str(src.get("attitude_stance") or "neutral").strip().lower()
    if stance not in ATTITUDE_STANCES:
        stance = "neutral"

    base.update({
        "target_key": tkey,
        "target_type": ttype,
        "target_name": tname,
        "known": known,
        "rel_stage": rel_stage,
        "attitude_stance": stance,
        "call_name": _clip_str(src.get("call_name"), 24, ""),
        "relation_tag": _clip_str(src.get("relation_tag"), 40, ""),
        "shared_memories": _clip_str(src.get("shared_memories"), 400, ""),
        "attitude_score": _clamp(src.get("attitude_score"), -1.0, 1.0, 0.0),
        "affective_valence": _clamp(src.get("affective_valence"), -1.0, 1.0, 0.0),
        "trust": _clamp(src.get("trust"), 0.0, 1.0, 0.55),
        "attraction": _clamp(src.get("attraction"), 0.0, 1.0, 0.35),
        "repulsion": _clamp(src.get("repulsion"), 0.0, 1.0, 0.0),
        "warmth": _clamp(src.get("warmth"), 0.0, 1.0, 0.45),
        "social_willingness": _clamp(src.get("social_willingness"), 0.0, 1.0, 0.50),
        "exchange_balance": _clamp(src.get("exchange_balance"), -1.0, 1.0, 0.0),
        "relational_debt": _clamp(src.get("relational_debt"), 0.0, 1.0, 0.0),
        "cool_off_turns": _clamp_int(src.get("cool_off_turns"), 0, 50, 0),
        "rounds": _clamp_int(src.get("rounds"), 0, 100000, 0),
        "last_interaction": _clip_str(src.get("last_interaction"), 180, ""),
        "manual_override": bool(src.get("manual_override", False)),
        "updated_at": round(float(src.get("updated_at") or 0.0), 3),
    })
    return base


def _map_mind_stage(raw_stage: str, *, known: bool, attitude_score: float) -> str:
    txt = str(raw_stage or "").strip().lower()
    if any(k in txt for k in ("hostile", "敌对", "厌恶")):
        return "hostile"
    if any(k in txt for k in ("guarded", "戒备", "防御")):
        return "guarded"
    if any(k in txt for k in ("close", "挚", "熟了", "亲密", "deep")):
        return "close"
    if any(k in txt for k in ("friend", "友好", "同伴")):
        return "friend"
    if known:
        return "friend" if attitude_score >= 0.35 else "acquainted"
    return "stranger"


def _map_mind_stance(raw_stance: str, valence: float) -> str:
    txt = str(raw_stance or "").strip().lower()
    if any(k in txt for k in ("hostile", "cold", "冷", "alien")):
        return "cold"
    if any(k in txt for k in ("guard", "defen", "戒备", "疏")):
        return "guarded"
    if any(k in txt for k in ("warm", "affection", "温和", "亲近")):
        return "warm"
    if any(k in txt for k in ("shy", "timid", "害羞")):
        return "shy"
    if valence >= 0.30:
        return "warm"
    if valence <= -0.25:
        return "guarded"
    return "neutral"


def _sync_user_from_live_state(
    state_root: Path,
    base_user: Mapping[str, Any],
    *,
    chat_key_prefix: str = "",
) -> dict[str, Any]:
    out = dict(base_user)
    person_raw = _read_json(Path(state_root) / "state" / "person.json")
    learned_name = ""
    if person_raw:
        items = [
            (k, v) for k, v in person_raw.items()
            if isinstance(v, Mapping) and (not chat_key_prefix or str(k).startswith(chat_key_prefix))
        ] or [(k, v) for k, v in person_raw.items() if isinstance(v, Mapping)]
        if items:
            _k, p_entry = max(items, key=lambda kv: float(kv[1].get("at") or 0.0))
            learned_name = str(p_entry.get("name") or "").strip()

    mind_raw = _read_json(Path(state_root) / "state" / "mind.json")
    latest_mind: dict[str, Any] = {}
    if mind_raw:
        m_items = [
            (k, v) for k, v in mind_raw.items()
            if isinstance(v, Mapping) and (not chat_key_prefix or str(k).startswith(chat_key_prefix))
        ] or [(k, v) for k, v in mind_raw.items() if isinstance(v, Mapping)]
        if m_items:
            _k, latest_raw = max(m_items, key=lambda kv: float(kv[1].get("updatedAt") or 0.0))
            latest_mind = dict(latest_raw)

    mind_ts = float(latest_mind.get("updatedAt") or 0.0)
    saved_ts = float(out.get("updated_at") or 0.0)
    if out.get("manual_override") and saved_ts > mind_ts:
        if learned_name and not out.get("call_name"):
            out["call_name"] = learned_name
            out["known"] = True
        return out

    att = latest_mind.get("attitude") if isinstance(latest_mind.get("attitude"), Mapping) else {}
    soc = latest_mind.get("sociology") if isinstance(latest_mind.get("sociology"), Mapping) else {}
    trust = _clamp(att.get("respectTrust", latest_mind.get("trust", out.get("trust", 0.6))), 0.0, 1.0, 0.6)
    att_score = _clamp(att.get("attitudeScore", out.get("attitude_score", 0.0)), -1.0, 1.0, 0.0)
    valence = _clamp(att.get("affectiveValence", out.get("affective_valence", 0.0)), -1.0, 1.0, 0.0)
    known = bool(out.get("known") or learned_name)
    out.update({
        "known": known,
        "call_name": out.get("call_name") or learned_name,
        "relation_tag": out.get("relation_tag") or ("相熟的用户" if known else "刚见面的用户"),
        "attitude_score": att_score,
        "affective_valence": valence,
        "trust": trust,
        "attraction": _clamp(soc.get("attraction", out.get("attraction", 0.4)), 0.0, 1.0, 0.4),
        "repulsion": _clamp(soc.get("repulsion", out.get("repulsion", 0.0)), 0.0, 1.0, 0.0),
        "warmth": _clamp(latest_mind.get("moodWarmth", out.get("warmth", 0.45)), 0.0, 1.0, 0.45),
        "exchange_balance": _clamp(soc.get("exchangeBalance", out.get("exchange_balance", 0.0)), -1.0, 1.0, 0.0),
        "relational_debt": _clamp(att.get("relationalDebt", out.get("relational_debt", 0.0)), 0.0, 1.0, 0.0),
        "cool_off_turns": _clamp_int(soc.get("coolOffTurns", out.get("cool_off_turns", 0)), 0, 50, 0),
        "rounds": max(int(out.get("rounds") or 0), _clamp_int(latest_mind.get("rounds", 0), 0, 100000, 0)),
        "rel_stage": _map_mind_stage(str(soc.get("relStage") or out.get("rel_stage") or ""), known=known, attitude_score=att_score),
        "attitude_stance": _map_mind_stance(str(att.get("attitudeStance") or out.get("attitude_stance") or ""), valence),
    })
    return out


def _read_raw_relations(state_root: Path) -> dict[str, dict[str, Any]]:
    raw = _read_json(relations_path(state_root))
    targets = raw.get("targets") if isinstance(raw.get("targets"), Mapping) else raw
    out: dict[str, dict[str, Any]] = {}
    if isinstance(targets, Mapping):
        for k, v in targets.items():
            if isinstance(v, Mapping):
                out[str(k)] = dict(v)
    return out


def _write_raw_relations(state_root: Path, targets: Mapping[str, Mapping[str, Any]]) -> None:
    payload = {
        "version": 1,
        "updatedAt": round(time.time(), 3),
        "targets": {str(k): dict(v) for k, v in targets.items()},
    }
    _atomic_write_json(relations_path(state_root), payload)


def load_relations(
    state_root: Path,
    *,
    registry: Any = None,
    subject_name: str = "",
    chat_key_prefix: str = "",
    extra_characters: Sequence[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """读取当前角色的所有合法态度与情感指针（自动过滤越界目标并补全默认不认识状态）。"""
    valid = valid_targets_for(registry, subject_name, extra_characters=extra_characters)
    stored = _read_raw_relations(state_root)
    result: dict[str, dict[str, Any]] = {}
    for tkey, meta in valid.items():
        entry = coerce_relation_entry(tkey, meta, stored.get(tkey))
        if tkey == "user":
            entry = _sync_user_from_live_state(state_root, entry, chat_key_prefix=chat_key_prefix)
            entry = coerce_relation_entry(tkey, meta, entry)
        result[tkey] = entry
    return result


def save_relation(
    state_root: Path,
    target_key: str,
    patch: Mapping[str, Any],
    *,
    registry: Any = None,
    subject_name: str = "",
    extra_characters: Sequence[str] | None = None,
    manual: bool = True,
) -> dict[str, Any]:
    """校验指针目标闭环后保存单条态度与情感指针。"""
    tkey = str(target_key or "").strip()
    valid = valid_targets_for(registry, subject_name, extra_characters=extra_characters)
    if tkey not in valid:
        raise ValueError(f"指针目标非法：{tkey or '(空)'}（仅允许指向用户或角色包中的其他角色）")

    stored = _read_raw_relations(state_root)
    # 顺带剔除已失效/不在合法集合中的旧键，保持闭环洁净
    clean_stored = {k: v for k, v in stored.items() if k in valid}
    cur = dict(clean_stored.get(tkey) or {})
    cur.update(dict(patch or {}))
    cur["updated_at"] = round(time.time(), 3)
    if manual:
        cur["manual_override"] = True
    entry = coerce_relation_entry(tkey, valid[tkey], cur)
    clean_stored[tkey] = entry
    _write_raw_relations(state_root, clean_stored)
    return entry


def reset_relation(
    state_root: Path,
    target_key: str,
    *,
    registry: Any = None,
    subject_name: str = "",
    extra_characters: Sequence[str] | None = None,
) -> dict[str, Any]:
    """将指定目标重置为默认的「不认识」初始状态。"""
    tkey = str(target_key or "").strip()
    valid = valid_targets_for(registry, subject_name, extra_characters=extra_characters)
    if tkey not in valid:
        raise ValueError(f"指针目标非法：{tkey}")
    stored = {k: v for k, v in _read_raw_relations(state_root).items() if k in valid}
    stored.pop(tkey, None)
    _write_raw_relations(state_root, stored)
    return coerce_relation_entry(tkey, valid[tkey], {})


def _extract_snippet(text: str, needle: str, radius: int = 75) -> str:
    src = re.sub(r"\s+", " ", str(text or "")).strip()
    if not src or not needle:
        return ""
    idx = src.find(needle)
    if idx < 0:
        return ""
    start = max(0, idx - 25)
    end = min(len(src), idx + len(needle) + radius)
    return src[start:end].strip(" ，,；;。")


def _infer_relations_offline(
    subject_name: str,
    subject_text: str,
    candidates: Sequence[Mapping[str, str]],
) -> list[dict[str, Any]]:
    """无可用大模型时的诚实设定文本交叉检索兜底：只在彼此设定明确提及对方时建档，否则一律跳过。"""
    out: list[dict[str, Any]] = []
    pos_words = ("朋友", "同伴", "搭档", "同学", "一起", "玩", "照顾", "温柔", "信任", "帮忙", "喜爱", "并肩")
    neg_words = ("敌", "讨厌", "背叛", "怀疑", "提防", "吵", "冷淡", "对抗", "畏惧")

    for cand in candidates:
        cname = str(cand.get("target_name") or "").strip()
        if not cname:
            continue
        cand_text = "\n".join(filter(None, [cand.get("brief", ""), cand.get("setting", ""), cand.get("soul", "")]))
        snip_a = _extract_snippet(subject_text, cname)
        snip_b = _extract_snippet(cand_text, subject_name)
        if not snip_a and not snip_b:
            out.append({"target_name": cname, "known": False})
            continue

        joined = "；".join(filter(None, [snip_a, snip_b]))
        pos_hit = sum(1 for w in pos_words if w in joined)
        neg_hit = sum(1 for w in neg_words if w in joined)
        is_classmate = "同学" in joined or "同班" in joined or "学院" in joined
        is_hostile = neg_hit > pos_hit and neg_hit >= 1

        if is_hostile:
            out.append({
                "target_name": cname,
                "known": True,
                "rel_stage": "guarded",
                "attitude_stance": "guarded",
                "call_name": cname,
                "relation_tag": "彼此有所防备的相识者",
                "shared_memories": joined[:180],
                "attitude_score": -0.25,
                "affective_valence": -0.2,
                "trust": 0.35,
                "attraction": 0.25,
                "repulsion": 0.40,
                "warmth": 0.25,
                "social_willingness": 0.35,
            })
        else:
            stage = "friend" if pos_hit >= 2 else "acquainted"
            out.append({
                "target_name": cname,
                "known": True,
                "rel_stage": stage,
                "attitude_stance": "warm" if pos_hit >= 1 else "neutral",
                "call_name": f"{cname}同学" if is_classmate and not cname.endswith("同学") else cname,
                "relation_tag": "同班同学 / 熟识同伴" if is_classmate else "设定中相识的同伴",
                "shared_memories": joined[:180],
                "attitude_score": 0.45 if pos_hit >= 1 else 0.22,
                "affective_valence": 0.42 if pos_hit >= 1 else 0.20,
                "trust": 0.72 if pos_hit >= 1 else 0.62,
                "attraction": 0.58,
                "repulsion": 0.0,
                "warmth": 0.65 if pos_hit >= 1 else 0.52,
                "social_willingness": 0.72 if pos_hit >= 1 else 0.60,
            })
    return out


def autofill_relations_for_pointer(
    registry: Any,
    pointer_id: str,
    *,
    adapter_provider: Callable[[], Any] | None = None,
    timeout: float = 45.0,
) -> dict[str, Any]:
    """一键让 LLM（或离线设定考据兜底）填写当前角色与角色包内所有其他角色的关系。

    - 若两者按设定原本就不认识：输出 known=False 并跳过，维持默认 stranger；
    - 若认识：自动填入关系标签、称呼、共同经历与态度/情感指标。
    """
    pointer = registry.get_pointer(pointer_id)
    if pointer is None:
        raise ValueError(f"未找到角色指针：{pointer_id}")

    profile = registry.load_profile(pointer)
    subject_name = str(pointer.character_name or profile.display_name or pointer.id).strip()
    state_root = registry.resolve_state_root(pointer)

    subject_host = mind_autofill._host_character_context(registry, subject_name)
    saved_soul = registry.read_soul_text(pointer.id) if hasattr(registry, "read_soul_text") else ""
    subject_text = "\n\n".join(filter(None, [subject_host, str(saved_soul or "").strip()[:3500]]))

    valid = valid_targets_for(registry, subject_name)
    candidates = [meta for meta in valid.values() if meta.get("target_type") == "character"]

    if not candidates:
        cur_map = load_relations(state_root, registry=registry, subject_name=subject_name, chat_key_prefix=pointer.chat_key_prefix or "")
        return {
            "pointer_id": pointer.id,
            "subject_name": subject_name,
            "source": "empty",
            "known_count": 0,
            "skipped_count": 0,
            "known_names": [],
            "skipped_names": [],
            "relations": cur_map,
            "message": "宿主角色包中目前没有其他角色可供推填关系。",
        }

    cand_blocks: list[str] = []
    for idx, cand in enumerate(candidates, start=1):
        c_parts = [f"{idx}. 角色名：{cand['target_name']}"]
        if cand.get("brief"):
            c_parts.append(f"   简介：{cand['brief'][:220]}")
        if cand.get("setting"):
            c_parts.append(f"   设定：{cand['setting'][:900]}")
        if cand.get("soul"):
            c_parts.append(f"   人格壳摘录：{cand['soul'][:900]}")
        cand_blocks.append("\n".join(c_parts))

    sys_prompt = (
        "你是二次元角色关系与人际档案分析专家。\n"
        "请根据主角色（Subject）的设定/人格壳，以及宿主角色包里其他角色的设定文本，"
        "严格判断主角色与每一位候选角色在原作或给定背景中「是否已经认识」。\n\n"
        "核心纪律（严禁违反）：\n"
        "1. 若两人毫无交集或按设定并不认识，必须设 \"known\": false 并跳过，严禁杜撰共同经历！\n"
        "2. 只有在设定或原作世界观中确实相识的角色，才设 \"known\": true，并如实写出他们的关系、称呼与一起经历过的具体事件。\n"
        "3. 严格只输出一个合法 JSON 对象：\n"
        "{\n"
        '  "summary": "1句话总结判定依据",\n'
        '  "relations": [\n'
        "    {\n"
        '      "target_name": "候选角色名（必须与输入完全一致）",\n'
        '      "known": true,\n'
        '      "rel_stage": "acquainted|friend|close|rival|guarded|hostile",\n'
        '      "attitude_stance": "warm|neutral|shy|teasing|guarded|cold",\n'
        '      "call_name": "主角色平时如何称呼TA",\n'
        '      "relation_tag": "简短关系定位，如：同班同学 / 曾并肩渡过难关的同伴",\n'
        '      "shared_memories": "40-150字：她们是什么关系、一起经历过哪些代表性的事",\n'
        '      "attitude_score": 0.45,\n'
        '      "affective_valence": 0.45,\n'
        '      "trust": 0.72,\n'
        '      "attraction": 0.60,\n'
        '      "repulsion": 0.0,\n'
        '      "warmth": 0.65,\n'
        '      "social_willingness": 0.75\n'
        "    },\n"
        '    {"target_name": "不认识的角色名", "known": false}\n'
        "  ]\n"
        "}"
    )
    user_prompt = (
        f"主角色（Subject）：{subject_name}\n"
        f"{subject_text or '（暂无额外文本，仅角色名：' + subject_name + '）'}\n\n"
        "待检候选角色（来自宿主角色包）：\n"
        + "\n\n".join(cand_blocks)
    )

    source = "llm"
    raw_list: list[Any] = []
    summary = ""
    try:
        parsed = mind_autofill._call_llm(
            sys_prompt,
            user_prompt,
            adapter_provider=adapter_provider,
            timeout=timeout,
        )
        raw_list = list(parsed.get("relations") or [])
        summary = str(parsed.get("summary") or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.info("nanami_persona: LLM 推填角色关系不可用，改用设定文本交叉考据兜底: %s", exc)
        source = "offline"
        raw_list = _infer_relations_offline(subject_name, subject_text, candidates)

    by_name: dict[str, Mapping[str, Any]] = {}
    for row in raw_list:
        if isinstance(row, Mapping):
            tname = str(row.get("target_name") or "").strip()
            if tname:
                by_name[tname] = row

    known_names: list[str] = []
    skipped_names: list[str] = []

    stored = {k: v for k, v in _read_raw_relations(state_root).items() if k in valid}
    now_ts = round(time.time(), 3)

    for cand in candidates:
        tkey = cand["target_key"]
        cname = cand["target_name"]
        row = by_name.get(cname) or {}
        if not bool(row.get("known", False)):
            skipped_names.append(cname)
            continue

        known_names.append(cname)
        patch = {
            "known": True,
            "rel_stage": row.get("rel_stage") or "acquainted",
            "attitude_stance": row.get("attitude_stance") or "neutral",
            "call_name": row.get("call_name") or cname,
            "relation_tag": row.get("relation_tag") or "相识的同伴",
            "shared_memories": row.get("shared_memories") or "",
            "attitude_score": row.get("attitude_score", 0.35),
            "affective_valence": row.get("affective_valence", 0.35),
            "trust": row.get("trust", 0.68),
            "attraction": row.get("attraction", 0.55),
            "repulsion": row.get("repulsion", 0.0),
            "warmth": row.get("warmth", 0.60),
            "social_willingness": row.get("social_willingness", 0.68),
            "manual_override": True,
            "updated_at": now_ts,
        }
        stored[tkey] = coerce_relation_entry(tkey, cand, patch)

    if known_names:
        _write_raw_relations(state_root, stored)

    rel_map = load_relations(
        state_root,
        registry=registry,
        subject_name=subject_name,
        chat_key_prefix=pointer.chat_key_prefix or "",
    )
    prefix_tag = "LLM 已" if source == "llm" else "已通过设定交叉考据"
    msg = (
        f"{prefix_tag}为「{subject_name}」填写 {len(known_names)} 位熟识角色的关系"
        + (f"（{'、'.join(known_names)}）" if known_names else "")
        + f"，跳过 {len(skipped_names)} 位不认识角色"
        + (f"（{'、'.join(skipped_names)}）" if skipped_names else "")
        + "。"
    )
    return {
        "pointer_id": pointer.id,
        "subject_name": subject_name,
        "source": source,
        "summary": summary or msg,
        "known_count": len(known_names),
        "skipped_count": len(skipped_names),
        "known_names": known_names,
        "skipped_names": skipped_names,
        "relations": rel_map,
        "message": msg,
    }


def format_relation_block(entry: Mapping[str, Any], subject_name: str = "") -> str:
    """将单条态度与情感指针格式化为简洁上下文注入文本。"""
    tname = str(entry.get("target_name") or "对方").strip()
    known = bool(entry.get("known", False))
    stage = STAGE_LABELS.get(str(entry.get("rel_stage") or "stranger"), str(entry.get("rel_stage") or "陌生"))
    stance = STANCE_LABELS.get(str(entry.get("attitude_stance") or "neutral"), str(entry.get("attitude_stance") or "自然"))
    head = f"【态度与情感指针：{subject_name + ' → ' if subject_name else ''}{tname}】"
    if not known:
        return f"{head} 尚未认识（陌生状态，请自然破冰/互相认识，勿假装本来就熟）。"

    parts = [f"关系：{stage}"]
    if entry.get("relation_tag"):
        parts.append(f"定位：{entry['relation_tag']}")
    if entry.get("call_name"):
        parts.append(f"称呼TA：{entry['call_name']}")
    parts.append(
        f"姿态：{stance}（好感 {float(entry.get('affective_valence') or 0):+.2f}，"
        f"信任 {float(entry.get('trust') or 0.55):.2f}，"
        f"社交意愿 {float(entry.get('social_willingness') or 0.5):.2f}）"
    )
    line = f"{head} " + "｜".join(parts)
    if entry.get("shared_memories"):
        line += f"\n- 关系与共同经历：{entry['shared_memories']}"
    if entry.get("last_interaction"):
        line += f"\n- 最近互动：{entry['last_interaction']}"
    return line
