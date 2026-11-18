"""外部真实二次元对话语料加载与按情境抽样器。

设计原则：
  - 不内置任何 AI 编造的伪语料；
  - 仅加载由用户/角色包提供的真实二次元原典对话语料（Galgame 剧本、轻小说、动画字幕提取等）；
  - 支持 `.yaml`（分情境桶或 `{user, assistant}` 列表）、`.tsv`（`bucket\\tuser\\tassistant` 或 `user\\tassistant`）与 `.jsonl`。
"""

from __future__ import annotations

import json
import logging
import random
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_CACHE: dict[tuple[str, float], dict[str, list[tuple[str, str]]]] = {}

_WANT_BUCKET_MAP: dict[str, str] = {
    # A. 自保家族 (safety) -> cold / reject
    "retreat": "cold",
    "rest": "cold",
    "deflect": "cold",
    "guard": "reject",
    # B. 连结家族 (connection) -> casual / cold / repair_afterglow / curious
    "stay": "casual",
    "bond": "casual",
    "seek_reassure": "curious",
    "keep_distance": "cold",
    "repair": "repair_afterglow",
    # C. 尊严家族 (esteem) -> annoyed / curious
    "not_belittled": "annoyed",
    "prove": "curious",
    "settle_score": "annoyed",
    # D. 成长家族 (growth) -> curious
    "understand": "curious",
    "master": "curious",
    "explore": "curious",
    # E. 掌控家族 (control) -> curious / reject / annoyed
    "monitor": "curious",
    "test": "curious",
    "secure": "reject",
    "dominate": "annoyed",
    # F. 利他家族 (altruism) -> casual / repair_afterglow
    "uphold": "casual",
    "give": "casual",
    "shield": "repair_afterglow",
}

_BUCKET_FALLBACK_ORDER: dict[str, tuple[str, ...]] = {
    "casual": ("casual", "curious", "repair_afterglow", "cold", "reject", "annoyed"),
    "curious": ("curious", "casual", "repair_afterglow", "cold", "reject", "annoyed"),
    "repair_afterglow": ("repair_afterglow", "casual", "cold", "curious", "reject", "annoyed"),
    "cold": ("cold", "repair_afterglow", "casual", "reject", "curious", "annoyed"),
    "annoyed": ("annoyed", "reject", "cold", "repair_afterglow", "curious", "casual"),
    "reject": ("reject", "cold", "annoyed", "repair_afterglow", "casual", "curious"),
}


def _parse_file(path: Path) -> dict[str, list[tuple[str, str]]]:
    suffix = path.suffix.lower()
    out: dict[str, list[tuple[str, str]]] = {}

    def _add(bucket: str, u: str, a: str) -> None:
        cu, ca = u.strip(), a.strip()
        if cu and ca:
            out.setdefault(bucket.strip() or "casual", []).append((cu, ca))

    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}

    if suffix in (".yaml", ".yml"):
        try:
            raw = yaml.safe_load(text)
        except Exception:
            logger.debug("nanami_persona: 解析语料 YAML 失败 %s", path, exc_info=True)
            return {}
        if isinstance(raw, dict):
            for bucket, items in raw.items():
                if not isinstance(items, list):
                    continue
                for item in items:
                    if isinstance(item, dict):
                        _add(str(bucket), str(item.get("user") or ""), str(item.get("assistant") or ""))
        elif isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict):
                    _add(str(item.get("bucket") or "casual"), str(item.get("user") or ""), str(item.get("assistant") or ""))
        return out

    if suffix == ".jsonl":
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                _add(str(obj.get("bucket") or "casual"), str(obj.get("user") or ""), str(obj.get("assistant") or ""))
        return out

    if suffix == ".tsv":
        for line in text.splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            cols = line.split("\t")
            if len(cols) >= 3:
                _add(cols[0], cols[1], cols[2])
            elif len(cols) == 2:
                _add("casual", cols[0], cols[1])
        return out

    return {}


def load_corpus_file(path: Path) -> dict[str, list[tuple[str, str]]]:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    key = (str(path.resolve()), mtime)
    if key in _CACHE:
        return _CACHE[key]
    parsed = _parse_file(path)
    if len(_CACHE) > 32:
        _CACHE.clear()
    _CACHE[key] = parsed
    return parsed


def discover_corpus_paths(
    *,
    profile_dir: Path | None = None,
    data_root: Path | None = None,
    archetype: str = "",
) -> list[Path]:
    """按优先级查找外部真实语料：角色包专属 `corpus.*`（命中即独占使用）→ 分型语料 `corpus/acg_<archetype>.*` → 全局 `corpus/acg_corpus.*`。"""
    if profile_dir is not None:
        profile_hits = [
            Path(profile_dir) / f"corpus{ext}"
            for ext in (".yaml", ".yml", ".jsonl", ".tsv")
            if (Path(profile_dir) / f"corpus{ext}").is_file()
        ]
        if profile_hits:
            return profile_hits
    candidates: list[Path] = []
    if data_root is not None:
        clean_arch = str(archetype or "").strip().lower()
        arch_paths: list[Path] = []
        if clean_arch:
            for ext in (".yaml", ".yml", ".jsonl", ".tsv"):
                p = Path(data_root) / "corpus" / f"acg_{clean_arch}{ext}"
                if p.is_file():
                    arch_paths.append(p)
        if arch_paths:
            candidates.extend(arch_paths)
        else:
            for ext in (".yaml", ".yml", ".jsonl", ".tsv"):
                candidates.append(Path(data_root) / "corpus" / f"acg_corpus{ext}")
    return [p for p in candidates if p.is_file()]


def sample_few_shot(
    want_id: str = "stay",
    *,
    wounded: bool = False,
    afterglow: bool = False,
    k: int = 4,
    seed: str | None = None,
    profile_dir: Path | None = None,
    data_root: Path | None = None,
    archetype: str = "",
) -> tuple[tuple[str, str], ...]:
    """从外部真实语料按当前心理状态抽取最多 k 组短对话；无外部语料时返回空元组。"""
    if k <= 0:
        return ()
    merged: dict[str, list[tuple[str, str]]] = {}
    for p in discover_corpus_paths(profile_dir=profile_dir, data_root=data_root, archetype=archetype):
        for bucket, pairs in load_corpus_file(p).items():
            merged.setdefault(bucket, []).extend(pairs)
    if not merged:
        return ()

    rng = random.Random(seed) if seed is not None else random.Random()
    if afterglow:
        primary_bucket = "repair_afterglow"
    elif wounded:
        primary_bucket = "annoyed"
    else:
        primary_bucket = _WANT_BUCKET_MAP.get(want_id, "casual")

    # 若角色专属语料中当前心理主桶恰好为空，允许仅从通用分型语料借 1 条同桶样本，确保全情境有响应锚点
    if not merged.get(primary_bucket) and profile_dir is not None and data_root is not None:
        for fp in discover_corpus_paths(profile_dir=None, data_root=data_root, archetype=archetype):
            shared_bucket_pairs = list(load_corpus_file(fp).get(primary_bucket) or ())
            if shared_bucket_pairs:
                rng.shuffle(shared_bucket_pairs)
                merged.setdefault(primary_bucket, []).append(shared_bucket_pairs[0])
                break

    picked: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def _take(bucket: str, count: int) -> None:
        pool = list(merged.get(bucket) or ())
        if not pool:
            return
        rng.shuffle(pool)
        for pair in pool:
            if len(picked) >= k or count <= 0:
                break
            if pair in seen:
                continue
            seen.add(pair)
            picked.append(pair)
            count -= 1

    _take(primary_bucket, min(2, k))
    if len(picked) < k:
        if primary_bucket in ("annoyed", "reject", "cold"):
            _take("reject" if primary_bucket != "reject" else "cold", 1)
        else:
            _take("casual" if primary_bucket != "casual" else "curious", 1)

    fallback_order = list(
        _BUCKET_FALLBACK_ORDER.get(
            primary_bucket,
            ("casual", "curious", "repair_afterglow", "cold", "reject", "annoyed"),
        )
    )
    # 冲突/防卫极性隔离：若主桶为 annoyed/reject/cold 且已有至少 1 条负向/冷淡样本，绝不回退混入 casual/repair_afterglow 礼貌句
    is_conflict_mode = primary_bucket in ("annoyed", "reject", "cold")
    if is_conflict_mode:
        fallback_order = [b for b in fallback_order if b in ("annoyed", "reject", "cold")]
    else:
        for extra_b in sorted(merged.keys()):
            if extra_b not in fallback_order:
                fallback_order.append(extra_b)
    for fallback in fallback_order:
        if len(picked) >= k:
            break
        _take(fallback, k - len(picked))

    if not picked and is_conflict_mode:
        for fallback in ("casual", "curious"):
            if len(picked) >= k:
                break
            _take(fallback, k - len(picked))

    return tuple(picked[:k])
