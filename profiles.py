"""多角色指针与通用人格模板配置（Pointer Registry + Profile Bundle）。

把原本只服务于「七海千秋」的静态配置拆成两层：
  1. 指针表（`<data_root>/pointers.yaml`）：把宿主角色名映射到独立的人格模板目录与状态根目录；
  2. 角色包（`profiles/<id>/profile.yaml`）：描述这个角色的人格壳、心理参数、优势/盲区、
     称呼策略、日常基线活动、路由条件、禁忌词与输出契约覆写。

向后兼容纪律：
  - `nanami_chiaki` 作为默认主指针，当 `state_root` 留空时沿用全局 `<data_root>`，
    不破坏老用户的 `state/mind.json` / `state/person.json` / `state/traits.json`；
  - 其它指针默认把状态落到 `<data_root>/profiles/<id>/runtime`，多角色同台互不串味；
  - 全部配置按 mtime 热加载，读坏时回退上一份可用快照，绝不阻塞聊天。
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from . import settings_write

logger = logging.getLogger(__name__)

POINTERS_FILE = "pointers.yaml"
PROFILE_FILE = "profile.yaml"
DEFAULT_PRIMARY_ID = "nanami_chiaki"
TEMPLATE_DIR_NAME = "_template"


def _clean_id(raw: str, fallback: str = "custom") -> str:
    text = re.sub(r"[^A-Za-z0-9_\-]+", "_", str(raw or "").strip()).strip("_")
    return text or fallback


def _as_str_tuple(raw: Any) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        parts = [one.strip() for one in re.split(r"[,，\n]+", raw) if one.strip()]
        return tuple(parts)
    if isinstance(raw, (list, tuple)):
        return tuple(str(one).strip() for one in raw if str(one).strip())
    return ()


def _as_dict(raw: Any) -> dict[str, Any]:
    return dict(raw) if isinstance(raw, Mapping) else {}


@dataclass(frozen=True)
class PointerSpec:
    """一条角色指针：把宿主里的 `character_name` 指向一套独立人格包与状态空间。"""

    id: str
    enabled: bool = True
    character_name: str = ""
    profile_dir: str = ""
    state_root: str = ""
    chat_key_prefix: str = ""

    @staticmethod
    def from_dict(raw: Mapping[str, Any], index: int = 0) -> "PointerSpec":
        pid = _clean_id(str(raw.get("id") or f"profile_{index + 1}"))
        char_name = str(raw.get("character_name") or "").strip()
        profile_dir = str(raw.get("profile_dir") or f"profiles/{pid}").strip()
        state_root = str(raw.get("state_root") or "").strip()
        prefix = str(raw.get("chat_key_prefix") or "").strip()
        return PointerSpec(
            id=pid,
            enabled=bool(raw.get("enabled", True)),
            character_name=char_name,
            profile_dir=profile_dir,
            state_root=state_root,
            chat_key_prefix=prefix,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "enabled": bool(self.enabled),
            "character_name": self.character_name,
            "profile_dir": self.profile_dir,
            "state_root": self.state_root,
            "chat_key_prefix": self.chat_key_prefix,
        }


@dataclass(frozen=True)
class CharacterProfile:
    """一个角色的完整可编辑心理学模型模板（不包含硬塞的固定人物传记或剧本活动）。"""

    id: str = DEFAULT_PRIMARY_ID
    display_name: str = ""
    character_name: str = ""
    # 人格壳配置
    soul_file: str = "soul.md"
    soul_marker: str = "【角色人格】"
    soul_present_needle: str = ""
    soul_route_mode: str = "auto"
    # 心理模型资源指针
    traits_file: str = "traits.json"
    off_voice_file: str = ""
    expression_looks_file: str = ""
    mind_expression: bool = True
    # 心理维度前端显示标签（仅作用于心理模型面板 L3/L4 指标名称，不作为设定塞入台词）
    strength_domains: tuple[str, ...] = ()
    weak_domains: tuple[str, ...] = ()
    domain_labels: dict[str, str] = field(default_factory=dict)
    custom_domains: tuple[dict[str, Any], ...] = ()
    wounds: tuple[dict[str, Any], ...] = ()
    # 自定义路由触发（route_name -> tuple of regex patterns）
    routes: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # 角色专属禁忌 / 助手腔观测 / 判官提示词覆写
    forbidden_patterns: tuple[dict[str, Any], ...] = ()
    assistant_ish_patterns: tuple[tuple[str, str], ...] = ()
    judge_system_prompt: str = ""
    # 活动意图（props）
    default_activity: dict[str, str] = field(default_factory=dict)
    activity_rules: tuple[dict[str, Any], ...] = ()
    # 卡片附注与形状映射
    state_only_note: str = ""
    first_turn_note: str = ""
    action_shape_lines: tuple[str, ...] = ()
    want_translations: dict[str, str] = field(default_factory=dict)
    few_shot: tuple[tuple[str, str], ...] = ()
    corpus_archetype: str = ""
    # 针对全局 Settings 的分项覆写（guard / contract / memory / props / judge / mind）
    overrides: dict[str, Any] = field(default_factory=dict)
    # 破壁文件读写权限（默认全关，仅在角色包显式开启 file_access 时启用）
    file_access_enabled: bool = False
    file_access_allow_write: bool = False
    file_access_min_transgression: float = 0.0
    file_access_read_roots: tuple[str, ...] = ()
    file_access_write_roots: tuple[str, ...] = ()
    # 来源路径（用于 mtime 与相对资源解析）
    profile_dir: Path | None = None
    source_path: str = ""

    @staticmethod
    def from_dict(
        raw: Mapping[str, Any],
        *,
        pointer: PointerSpec | None = None,
        profile_dir: Path | None = None,
        source_path: str = "",
    ) -> "CharacterProfile":
        pid = _clean_id(str(raw.get("id") or (pointer.id if pointer else DEFAULT_PRIMARY_ID)))
        char_name = str(
            raw.get("character_name")
            or (pointer.character_name if pointer else "")
            or "新角色"
        ).strip()
        display_name = str(raw.get("display_name") or char_name or pid).strip()

        soul_raw = _as_dict(raw.get("soul"))
        soul_file = str(soul_raw.get("file") or raw.get("soul_file") or "soul.md").strip()
        soul_marker = str(soul_raw.get("marker") or raw.get("soul_marker") or "【角色档案】").strip()
        soul_needle = str(soul_raw.get("present_needle") or raw.get("soul_present_needle") or "").strip()
        soul_route_mode = str(soul_raw.get("route_mode") or raw.get("soul_route_mode") or "auto").strip().lower()
        if soul_route_mode not in ("auto", "all"):
            soul_route_mode = "auto"

        assets_raw = _as_dict(raw.get("assets"))
        traits_file = str(assets_raw.get("traits_file") or raw.get("traits_file") or "traits.json").strip()
        off_voice_file = str(assets_raw.get("off_voice_file") or raw.get("off_voice_file") or "").strip()
        expression_looks_file = str(
            assets_raw.get("expression_looks_file") or raw.get("expression_looks_file") or ""
        ).strip()
        mind_expression = bool(
            assets_raw.get("mind_expression", raw.get("mind_expression", True))
        )

        domains_raw = _as_dict(raw.get("domains"))
        strength_domains = _as_str_tuple(domains_raw.get("strengths") or raw.get("strength_domains"))
        weak_domains = _as_str_tuple(domains_raw.get("weaknesses") or raw.get("weak_domains"))
        domain_labels = {
            str(k): str(v)
            for k, v in _as_dict(domains_raw.get("labels") or raw.get("domain_labels")).items()
            if str(k).strip() and str(v).strip()
        }
        # 一层扁平配置键：供 UI 直接编辑并映射至心理模型 L3/L4 指标展示名
        for flat_key, target_keys in (
            ("primary_label", ("domainEfficacy.game",)),
            ("non_primary_label", ("domainEfficacy.nonGame",)),
            ("wound_a_label", ("woundGame", "woundGameDecay")),
            ("wound_b_label", ("woundHuman", "woundHumanDecay")),
            ("wound_c_label", ("woundSkill", "woundSkillDecay")),
        ):
            val = str(domains_raw.get(flat_key) or "").strip()
            if val:
                for tk in target_keys:
                    if tk.endswith("Decay"):
                        domain_labels.setdefault(tk, f"{val}（衰减）")
                    else:
                        domain_labels.setdefault(tk, val)

        wounds_list = [dict(item) for item in (raw.get("wounds") or ()) if isinstance(item, Mapping)]
        domains_list = [dict(item) for item in (raw.get("custom_domains") or ()) if isinstance(item, Mapping)]

        routes_map: dict[str, tuple[str, ...]] = {}
        for rname, rval in _as_dict(raw.get("routes")).items():
            patterns = _as_str_tuple(rval)
            if str(rname).strip():
                routes_map[str(rname).strip()] = patterns

        guard_raw = _as_dict(raw.get("guard"))
        forbidden_list = []
        for item in (guard_raw.get("forbidden_patterns") or raw.get("forbidden_patterns") or ()):
            if isinstance(item, Mapping) and str(item.get("pattern") or "").strip():
                forbidden_list.append(dict(item))
            elif isinstance(item, str) and item.strip():
                forbidden_list.append({"id": "custom", "pattern": item.strip(), "reason": "角色禁忌"})

        assistant_list: list[tuple[str, str]] = []
        for item in (guard_raw.get("assistant_ish") or raw.get("assistant_ish_patterns") or ()):
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                assistant_list.append((str(item[0]), str(item[1])))
            elif isinstance(item, Mapping) and item.get("needle"):
                assistant_list.append((str(item["needle"]), str(item.get("why") or "助手腔")))

        judge_raw = _as_dict(raw.get("judge"))
        judge_system_prompt = str(
            judge_raw.get("system_prompt") or raw.get("judge_system_prompt") or ""
        ).strip()

        props_raw = _as_dict(raw.get("props"))
        default_act_raw = _as_dict(props_raw.get("default_activity") or raw.get("default_activity"))
        default_activity = {
            str(k): str(v)
            for k, v in default_act_raw.items()
            if str(k) in ("what", "text", "why") and str(v).strip()
        }
        for short_key, full_key in (("what", "default_what"), ("text", "default_text"), ("why", "default_why")):
            if str(props_raw.get(full_key) or "").strip():
                default_activity[short_key] = str(props_raw[full_key]).strip()
        act_rules = []
        for item in (props_raw.get("rules") or raw.get("activity_rules") or ()):
            if isinstance(item, Mapping):
                act_rules.append(dict(item))

        prompt_raw = _as_dict(raw.get("prompt"))
        state_only_note = str(prompt_raw.get("state_only_note") or raw.get("state_only_note") or "").strip()
        first_turn_note = str(prompt_raw.get("first_turn_note") or raw.get("first_turn_note") or "").strip()
        action_shape_lines = _as_str_tuple(
            prompt_raw.get("action_shape_lines") or raw.get("action_shape_lines")
        )
        want_translations = {
            str(k).strip(): str(v).strip()
            for k, v in _as_dict(prompt_raw.get("want_translations") or raw.get("want_translations")).items()
            if str(k).strip() and str(v).strip()
        }
        few_shot_pairs: list[tuple[str, str]] = []
        for item in (prompt_raw.get("few_shot") or raw.get("few_shot") or ()):
            if isinstance(item, Mapping):
                u = str(item.get("user") or "").strip()
                a = str(item.get("assistant") or "").strip()
                if u and a:
                    few_shot_pairs.append((u, a))

        # 聚合 overrides：支持顶层 overrides: {...} 以及直接在 guard/contract/memory/props/judge/mind 下写覆写键
        overrides = copy.deepcopy(_as_dict(raw.get("overrides")))
        for section_name, known_keys in (
            ("mind", ("enabled", "card_position", "marker", "percept", "writeback", "card_timeout", "her_timeout", "guard_timeout", "percept_timeout")),
            ("guard", ("enabled", "mode", "max_chars")),
            ("contract", ("enabled", "relax_array_min", "max_dialog_items", "max_speech_chars")),
            ("memory", ("learn_user_name", "inject", "remember_rejections", "address_suffix", "address_honorific", "address_gap_turns")),
            ("props", ("enabled", "mode", "timeout")),
            ("judge", ("enabled", "ai_flavor", "lead_trim", "timeout")),
        ):
            sec = _as_dict(raw.get(section_name))
            for key in known_keys:
                if key in sec:
                    overrides.setdefault(section_name, {})[key] = sec[key]

        file_raw = _as_dict(raw.get("file_access"))
        file_access_enabled = bool(file_raw.get("enabled", False))
        file_access_allow_write = bool(file_raw.get("allow_write", False))
        try:
            file_access_min_transgression = max(
                0.0, min(1.0, float(file_raw.get("min_transgression", 0.0) or 0.0))
            )
        except (TypeError, ValueError):
            file_access_min_transgression = 0.0
        file_access_read_roots = _as_str_tuple(file_raw.get("read_roots"))
        file_access_write_roots = _as_str_tuple(file_raw.get("write_roots"))

        return CharacterProfile(
            id=pid,
            display_name=display_name,
            character_name=char_name,
            soul_file=soul_file,
            soul_marker=soul_marker,
            soul_present_needle=soul_needle,
            soul_route_mode=soul_route_mode,
            traits_file=traits_file,
            off_voice_file=off_voice_file,
            expression_looks_file=expression_looks_file,
            mind_expression=mind_expression,
            strength_domains=strength_domains,
            weak_domains=weak_domains,
            domain_labels=domain_labels,
            custom_domains=tuple(domains_list),
            wounds=tuple(wounds_list),
            routes=routes_map,
            forbidden_patterns=tuple(forbidden_list),
            assistant_ish_patterns=tuple(assistant_list),
            judge_system_prompt=judge_system_prompt,
            default_activity=default_activity,
            activity_rules=tuple(act_rules),
            state_only_note=state_only_note,
            first_turn_note=first_turn_note,
            action_shape_lines=action_shape_lines,
            want_translations=want_translations,
            few_shot=tuple(few_shot_pairs),
            corpus_archetype=str(raw.get("corpus_archetype") or prompt_raw.get("corpus_archetype") or "").strip(),
            overrides=overrides,
            file_access_enabled=file_access_enabled,
            file_access_allow_write=file_access_allow_write,
            file_access_min_transgression=file_access_min_transgression,
            file_access_read_roots=file_access_read_roots,
            file_access_write_roots=file_access_write_roots,
            profile_dir=profile_dir,
            source_path=source_path,
        )

    def resolve_path(self, relative_or_abs: str, *, plugin_root: Path | None = None) -> Path | None:
        raw = str(relative_or_abs or "").strip()
        if not raw:
            return None
        path = Path(raw).expanduser()
        if path.is_absolute():
            return path
        if self.profile_dir is not None:
            cand = (self.profile_dir / path).resolve()
            if cand.exists():
                return cand
        if plugin_root is not None:
            cand2 = (plugin_root / path).resolve()
            if cand2.exists():
                return cand2
        if self.profile_dir is not None:
            return (self.profile_dir / path).resolve()
        return path


def apply_profile_overrides(settings: Any, profile: CharacterProfile, pointer: PointerSpec | None = None) -> Any:
    """把角色包里的 overrides 与指针配置合并到全局 Settings 上，产出该角色专属的 Settings 副本。"""
    from dataclasses import replace

    if settings is None or profile is None:
        return settings
    ov = profile.overrides or {}
    mind_ov = _as_dict(ov.get("mind"))
    guard_ov = _as_dict(ov.get("guard"))
    contract_ov = _as_dict(ov.get("contract"))
    memory_ov = _as_dict(ov.get("memory"))
    props_ov = _as_dict(ov.get("props"))
    judge_ov = _as_dict(ov.get("judge"))

    def _patch_dc(obj: Any, patch: dict[str, Any]) -> Any:
        if not patch or obj is None:
            return obj
        valid = {k: v for k, v in patch.items() if hasattr(obj, k)}
        if not valid:
            return obj
        # 走对应类型的 from_dict 保证夹取/校验不丢
        cls = type(obj)
        if hasattr(cls, "from_dict"):
            merged = {k: getattr(obj, k) for k in vars(obj)}
            merged.update(valid)
            return cls.from_dict(merged)
        return replace(obj, **valid)

    char_name = (
        (pointer.character_name if pointer and pointer.character_name else "")
        or profile.character_name
        or settings.character_name
    )
    prefix = (
        (pointer.chat_key_prefix if pointer and pointer.chat_key_prefix else "")
        or settings.chat_key_prefix
    )
    persona_obj = replace(
        settings.persona,
        route=profile.soul_route_mode or settings.persona.route,
    )
    return replace(
        settings,
        character_name=char_name,
        chat_key_prefix=prefix,
        mind=_patch_dc(settings.mind, mind_ov),
        guard=_patch_dc(settings.guard, guard_ov),
        contract=_patch_dc(settings.contract, contract_ov),
        memory=_patch_dc(settings.memory, memory_ov),
        props=_patch_dc(settings.props, props_ov),
        judge=_patch_dc(settings.judge, judge_ov),
        persona=persona_obj,
    )


def migrate_pointers_file(packaged: Path, live: Path) -> Path:
    """首次运行时把内置 pointers.yaml 复制到数据目录，升级插件不冲掉用户绑定。"""
    if live.is_file():
        return live
    if not packaged.is_file():
        return packaged
    try:
        live.parent.mkdir(parents=True, exist_ok=True)
        live.write_bytes(packaged.read_bytes())
        logger.info("nanami_persona: 已把默认指针表复制到 %s", live)
        return live
    except OSError:
        logger.info("nanami_persona: 数据目录不可写，继续用包内指针表 %s", packaged)
        return packaged


class PointerRegistry:
    """管理 `pointers.yaml` 与各个角色包（Profile Bundle）。支持按 mtime 热加载。"""

    def __init__(
        self,
        *,
        pointers_path: Path,
        plugin_root: Path,
        data_root: Path,
    ) -> None:
        self._pointers_path = Path(pointers_path)
        self._plugin_root = Path(plugin_root)
        self._data_root = Path(data_root)
        self._lock = threading.Lock()
        self._mtime = -1.0
        self._active_edit_id = DEFAULT_PRIMARY_ID
        self._pointers: list[PointerSpec] = []
        self._profile_cache: dict[str, tuple[float, CharacterProfile]] = {}

    @property
    def pointers_path(self) -> Path:
        return self._pointers_path

    @property
    def plugin_root(self) -> Path:
        return self._plugin_root

    @property
    def data_root(self) -> Path:
        return self._data_root

    def _reload_if_needed(self) -> None:
        try:
            mtime = os.stat(self._pointers_path).st_mtime
        except OSError:
            if not self._pointers:
                self._pointers = [
                    PointerSpec(
                        id=DEFAULT_PRIMARY_ID,
                        enabled=True,
                        character_name="七海千秋",
                        profile_dir=f"profiles/{DEFAULT_PRIMARY_ID}",
                        state_root="",
                        chat_key_prefix="",
                    )
                ]
                self._active_edit_id = DEFAULT_PRIMARY_ID
            return
        if mtime == self._mtime and self._pointers:
            return
        try:
            raw = yaml.safe_load(self._pointers_path.read_text(encoding="utf-8")) or {}
            if not isinstance(raw, Mapping):
                raise ValueError("pointers.yaml 顶层必须是映射")
            items = [
                PointerSpec.from_dict(item, idx)
                for idx, item in enumerate(raw.get("pointers") or ())
                if isinstance(item, Mapping)
            ]
            if not items:
                items = [
                    PointerSpec(
                        id=DEFAULT_PRIMARY_ID,
                        enabled=True,
                        character_name="七海千秋",
                        profile_dir=f"profiles/{DEFAULT_PRIMARY_ID}",
                    )
                ]
            edit_id = str(raw.get("active_edit_id") or items[0].id).strip()
            if not any(p.id == edit_id for p in items):
                edit_id = items[0].id
            self._pointers = items
            self._active_edit_id = edit_id
            self._mtime = mtime
        except Exception:
            logger.exception("nanami_persona: pointers.yaml 读取失败，继续沿用上一份")

    def list_pointers(self) -> list[PointerSpec]:
        with self._lock:
            self._reload_if_needed()
            return list(self._pointers)

    def active_edit_id(self) -> str:
        with self._lock:
            self._reload_if_needed()
            return self._active_edit_id

    def get_pointer(self, pointer_id: str) -> PointerSpec | None:
        pid = str(pointer_id or "").strip()
        for one in self.list_pointers():
            if one.id == pid:
                return one
        return None

    def resolve_profile_dir(self, pointer: PointerSpec) -> Path:
        raw = str(pointer.profile_dir or f"profiles/{pointer.id}").strip()
        path = Path(raw).expanduser()
        if path.is_absolute():
            return path
        # 优先用户数据目录（支持用户自定义覆盖），其次插件内置目录
        in_data = (self._data_root / path).resolve()
        if in_data.is_dir():
            return in_data
        in_plugin = (self._plugin_root / path).resolve()
        if in_plugin.is_dir():
            return in_plugin
        return in_data

    def resolve_state_root(self, pointer: PointerSpec) -> Path:
        raw = str(pointer.state_root or "").strip()
        if raw:
            path = Path(raw).expanduser()
            return path if path.is_absolute() else (self._data_root / path).resolve()
        # 向后兼容：默认主指针 nanami_chiaki 沿用全局 data_root
        if pointer.id == DEFAULT_PRIMARY_ID:
            return self._data_root
        return (self._data_root / "profiles" / pointer.id / "runtime").resolve()

    def load_profile(self, pointer: PointerSpec) -> CharacterProfile:
        pdir = self.resolve_profile_dir(pointer)
        yaml_path = pdir / PROFILE_FILE
        try:
            mtime = os.stat(yaml_path).st_mtime
        except OSError:
            return CharacterProfile(
                id=pointer.id,
                display_name=pointer.character_name or pointer.id,
                character_name=pointer.character_name,
                profile_dir=pdir,
                source_path="",
            )
        cache_key = f"{pointer.id}:{yaml_path}"
        cached = self._profile_cache.get(cache_key)
        if cached and cached[0] == mtime:
            return cached[1]
        try:
            raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
            if not isinstance(raw, Mapping):
                raw = {}
            profile = CharacterProfile.from_dict(
                raw, pointer=pointer, profile_dir=pdir, source_path=str(yaml_path)
            )
            self._profile_cache[cache_key] = (mtime, profile)
            return profile
        except Exception:
            logger.exception("nanami_persona: 读取角色包失败 %s", yaml_path)
            if cached:
                return cached[1]
            return CharacterProfile(
                id=pointer.id,
                display_name=pointer.character_name or pointer.id,
                character_name=pointer.character_name,
                profile_dir=pdir,
                source_path=str(yaml_path),
            )

    def _load_profile_from_dir(self, pdir: Path, pointer: PointerSpec | None = None) -> CharacterProfile | None:
        yaml_path = pdir / PROFILE_FILE
        if not yaml_path.is_file():
            return None
        try:
            raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
            if not isinstance(raw, Mapping):
                return None
            return CharacterProfile.from_dict(
                raw, pointer=pointer, profile_dir=pdir, source_path=str(yaml_path)
            )
        except Exception:
            return None

    def _localize_profile_assets(
        self, src_dir: Path, dest_dir: Path, pointer: PointerSpec | None = None
    ) -> None:
        """把相对源目录/插件目录的外部资源（soul.md / off-voice / expressions）复制到目标角色包内。"""
        src_prof = self._load_profile_from_dir(src_dir, pointer=pointer)
        if src_prof is None:
            return
        patches: dict[str, Any] = {}
        for rel_attr, dest_name, dotted_key in (
            ("soul_file", "soul.md", "soul.file"),
            ("off_voice_file", "off-voice.json", "assets.off_voice_file"),
            ("expression_looks_file", "expressions.json", "assets.expression_looks_file"),
        ):
            rel_val = str(getattr(src_prof, rel_attr, "") or "").strip()
            if not rel_val:
                continue
            src_asset = src_prof.resolve_path(rel_val, plugin_root=self._plugin_root)
            if src_asset and src_asset.is_file() and not (dest_dir / dest_name).exists():
                # 仅当目标路径不在 dest_dir 内部且当前指针会脱离源位置时，实体化一份进 dest_dir
                try:
                    in_dest = dest_dir.resolve() in src_asset.resolve().parents
                except Exception:
                    in_dest = False
                if not in_dest:
                    shutil.copy2(src_asset, dest_dir / dest_name)
                    patches[dotted_key] = dest_name
        if patches:
            settings_write.write_values(dest_dir / PROFILE_FILE, patches)

    def list_host_characters(self) -> list[dict[str, str]]:
        """读取宿主角色列表（data/config/characters.yaml 或 ConfigManager），用于下拉选择绑定。"""
        items: list[dict[str, str]] = []
        seen: set[str] = set()

        def _collect_mapping(raw_list: Any) -> None:
            if not isinstance(raw_list, list):
                return
            for entry in raw_list:
                if isinstance(entry, Mapping):
                    name = str(entry.get("name") or "").strip()
                    brief = str(entry.get("character_brief") or "").strip()
                    setting = str(entry.get("character_setting") or "").strip()
                else:
                    name = str(getattr(entry, "name", "") or "").strip()
                    brief = str(getattr(entry, "character_brief", "") or "").strip()
                    setting = str(getattr(entry, "character_setting", "") or "").strip()
                if not name or name in seen:
                    continue
                seen.add(name)
                items.append({"name": name, "brief": brief, "setting": setting})

        local_cfg = (self._data_root.parent / "config" / "characters.yaml").resolve()
        default_cfg = Path("data/config/characters.yaml").resolve()
        for cfg_path in (local_cfg, default_cfg):
            if cfg_path.is_file():
                try:
                    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
                    _collect_mapping(raw)
                except Exception:
                    logger.debug("nanami_persona: 读取宿主角色列表失败 %s", cfg_path, exc_info=True)
                if items:
                    return items

        try:
            from config.config_manager import ConfigManager

            _collect_mapping(list(getattr(ConfigManager().config, "characters", []) or []))
        except Exception:
            logger.debug("nanami_persona: ConfigManager 角色列表不可用", exc_info=True)
        return items

    def _host_character_entry(self, character_name: str) -> dict[str, str] | None:
        target = str(character_name or "").strip()
        if not target:
            return None
        for entry in self.list_host_characters():
            if entry.get("name") == target:
                return entry
        return None

    def _sync_host_character_to_dir(
        self,
        dest_dir: Path,
        character_name: str,
        *,
        force_if_template: bool = True,
    ) -> None:
        """把宿主角色卡的设定概要与路由模板同步写入新建/空白指针目录的 soul.md 与 profile.yaml。"""
        cname = str(character_name or "").strip()
        if not cname or not dest_dir.is_dir():
            return
        host_entry = self._host_character_entry(cname)
        yaml_path = dest_dir / PROFILE_FILE
        prof_obj = self._load_profile_from_dir(dest_dir)
        # 若模板里的 display_name 还是通用/占位名，同步更新成角色名
        if prof_obj is not None:
            old_dn = str(prof_obj.display_name or "").strip()
            if not old_dn or old_dn in ("通用人格模板", "示例角色", prof_obj.id) or old_dn.startswith("新角色_"):
                settings_write.write_values(yaml_path, {"display_name": cname})
            generic_judge = "你判断一句话像不像「AI 助手／客服／系统播报／主持人」或者「背剧本台词、工整排比金句腔」，而不是这个角色本人在微信里随口自然打字。"
            if prof_obj.judge_system_prompt == generic_judge:
                settings_write.write_values(
                    yaml_path,
                    {"judge.system_prompt": f"你判断一句话像不像「AI 助手／客服／系统播报／主持人」或者「背剧本台词、工整排比金句腔」，而不是{cname}本人在微信里随口自然打字。"},
                )

        if host_entry is None:
            return
        brief = host_entry.get("brief", "").strip()
        setting = host_entry.get("setting", "").strip()
        if not brief and not setting:
            return

        soul_path = dest_dir / "soul.md"
        existing_soul = ""
        if soul_path.is_file():
            try:
                existing_soul = soul_path.read_text(encoding="utf-8")
            except OSError:
                existing_soul = ""
        is_placeholder = (
            not existing_soul.strip()
            or existing_soul.lstrip().startswith("# 通用角色")
            or existing_soul.lstrip().startswith("# 新角色人格壳")
        )
        if not is_placeholder and not (force_if_template and existing_soul.lstrip().startswith("# 通用角色")):
            return

        # 控制摘录长度避免每轮与宿主系统提示重复叠加数千字
        excerpt = setting[:700].rstrip() + ("……（完整设定已由宿主角色卡携带）" if len(setting) > 700 else "")
        portrait_parts: list[str] = []
        if brief:
            portrait_parts.append(brief)
        if excerpt:
            portrait_parts.append(excerpt)
        portrait_block = "\n\n".join(portrait_parts)

        # 保留通用模板中的 <!-- route: ... --> 场景段落，便于按需路由触发
        route_tail = ""
        marker_idx = existing_soul.find("## 4. 场景段")
        if marker_idx >= 0:
            route_tail = "\n\n" + existing_soul[marker_idx:].strip()

        new_soul = (
            f"# {cname} · 人格壳\n\n"
            f"> 已绑定宿主角色列表中的「{cname}」角色包，可在此继续微调她的说话节奏与边界。\n\n"
            f"## 画像\n\n"
            f"{portrait_block}\n\n"
            f"## 绝对不这样说话\n\n"
            f"- 不使用「有什么可以帮你的」「我理解你的意思」「收到」等客服与系统腔。\n"
            f"- 不评论对话本身（如「我们换个话题吧」）。"
            f"{route_tail}\n"
        )
        soul_path.write_text(new_soul, encoding="utf-8")
        if yaml_path.is_file():
            settings_write.write_values(yaml_path, {"soul.file": "soul.md"})

    def ensure_editable_profile_dir(self, pointer: PointerSpec) -> Path:
        """若当前指针指向包内只读目录，编辑保存前自动复制到数据目录一份，升级不冲掉。"""
        current = self.resolve_profile_dir(pointer)
        in_data = (self._data_root / "profiles" / pointer.id).resolve()
        if current == in_data:
            in_data.mkdir(parents=True, exist_ok=True)
            return in_data
        # 如果是内置目录，拷一份到数据目录（相对资源如 ../../soul/chiaki-soul.md 同步解成可用副本）
        try:
            is_inside_plugin = self._plugin_root.resolve() in current.parents or current == self._plugin_root.resolve()
        except Exception:
            is_inside_plugin = False
        if not is_inside_plugin:
            return current
        if not in_data.exists() and current.is_dir():
            shutil.copytree(current, in_data)
            self._localize_profile_assets(current, in_data, pointer=pointer)
            self._profile_cache.clear()
        return in_data

    def _save_pointers(self, pointers: list[PointerSpec], active_edit_id: str) -> None:
        payload = {
            "active_edit_id": active_edit_id,
            "pointers": [p.to_dict() for p in pointers],
        }
        self._pointers_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._pointers_path.with_suffix(".yaml.tmp")
        header = (
            "# 角色指针表：每个条目把一个宿主角色名绑定到独立的人格包与状态目录。\n"
            "# 保存后按 mtime 热加载，不用重启。\n\n"
        )
        text = header + yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, self._pointers_path)
        self._pointers = list(pointers)
        self._active_edit_id = active_edit_id
        self._mtime = os.stat(self._pointers_path).st_mtime

    def set_active_edit(self, pointer_id: str) -> bool:
        with self._lock:
            self._reload_if_needed()
            if not any(p.id == pointer_id for p in self._pointers):
                return False
            self._save_pointers(self._pointers, pointer_id)
            return True

    def update_pointer(self, pointer_id: str, patch: Mapping[str, Any]) -> PointerSpec | None:
        with self._lock:
            self._reload_if_needed()
            updated: list[PointerSpec] = []
            target: PointerSpec | None = None
            for one in self._pointers:
                if one.id != pointer_id:
                    updated.append(one)
                    continue
                merged = one.to_dict()
                for key in ("enabled", "character_name", "profile_dir", "state_root", "chat_key_prefix"):
                    if key in patch:
                        merged[key] = patch[key]
                target = PointerSpec.from_dict(merged)
                updated.append(target)
            if target is None:
                return None
            self._save_pointers(updated, self._active_edit_id)
            return target

    def update_profile_values(self, pointer_id: str, flat_patch: Mapping[str, Any]) -> bool:
        pointer = self.get_pointer(pointer_id)
        if pointer is None or not flat_patch:
            return False
        pdir = self.ensure_editable_profile_dir(pointer)
        yaml_path = pdir / PROFILE_FILE
        if not yaml_path.is_file():
            yaml_path.write_text(f"id: {pointer.id}\ncharacter_name: {pointer.character_name}\n", encoding="utf-8")
        settings_write.write_values(yaml_path, dict(flat_patch))
        with self._lock:
            self._profile_cache.clear()
        return True

    def _rewrite_top_list(self, pointer_id: str, key_name: str, cleaned: list[dict[str, Any]]) -> bool:
        """重写角色包顶层列表配置块（自动迁出只读目录并保留其余内容与注释）。"""
        pointer = self.get_pointer(pointer_id)
        if pointer is None:
            return False
        pdir = self.ensure_editable_profile_dir(pointer)
        yaml_path = pdir / PROFILE_FILE
        if not yaml_path.is_file():
            yaml_path.write_text(f"id: {pointer.id}\ncharacter_name: {pointer.character_name}\n", encoding="utf-8")

        header_re = re.compile(rf"^{re.escape(key_name)}:\s*(#.*)?$")
        lines = yaml_path.read_text(encoding="utf-8").splitlines()
        out_lines: list[str] = []
        skip_block = False
        for line in lines:
            if header_re.match(line):
                skip_block = True
                continue
            if skip_block:
                if line.strip() and not line.startswith((" ", "\t", "-")):
                    skip_block = False
                else:
                    continue
            out_lines.append(line)
        while out_lines and not out_lines[-1].strip():
            out_lines.pop()
        if cleaned:
            dumped = yaml.safe_dump({key_name: cleaned}, allow_unicode=True, sort_keys=False).rstrip()
            if out_lines:
                out_lines.append("")
            out_lines.append(dumped)
        tmp = yaml_path.with_suffix(".yaml.tmp")
        tmp.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
        os.replace(tmp, yaml_path)
        with self._lock:
            self._profile_cache.clear()
        return True

    def update_custom_wounds(self, pointer_id: str, wounds: list[Mapping[str, Any]]) -> bool:
        """更新角色包顶层 `wounds:` 列表。"""
        cleaned: list[dict[str, Any]] = []
        for idx, item in enumerate(wounds):
            if not isinstance(item, Mapping):
                continue
            try:
                decay = round(max(0.0, min(1.0, float(item.get("decay", 0.7)))), 3)
            except (TypeError, ValueError):
                decay = 0.7
            try:
                weight = round(max(0.05, min(1.0, float(item.get("weight", 0.7)))), 3)
            except (TypeError, ValueError):
                weight = 0.7
            wid = _clean_id(str(item.get("id") or f"wound_{idx + 1}"), fallback=f"wound_{idx + 1}")
            label = str(item.get("label") or f"自定义敏感点 {idx + 1}").strip()
            patterns = str(item.get("patterns") or "").strip()
            cleaned.append({
                "id": wid,
                "label": label,
                "patterns": patterns,
                "decay": decay,
                "weight": weight,
            })
        return self._rewrite_top_list(pointer_id, "wounds", cleaned)

    def add_custom_wound(
        self,
        pointer_id: str,
        *,
        label: str = "",
        decay: float = 0.7,
        patterns: str = "",
    ) -> dict[str, Any] | None:
        pointer = self.get_pointer(pointer_id)
        if pointer is None:
            return None
        profile = self.load_profile(pointer)
        existing = [dict(w) for w in profile.wounds]
        used_ids = {str(w.get("id") or "") for w in existing}
        idx = len(existing) + 1
        while f"wound_{idx}" in used_ids:
            idx += 1
        item = {
            "id": f"wound_{idx}",
            "label": str(label or f"自定义敏感点 {idx}").strip(),
            "patterns": str(patterns or "").strip(),
            "decay": round(max(0.0, min(1.0, float(decay))), 3),
            "weight": 0.7,
        }
        existing.append(item)
        return item if self.update_custom_wounds(pointer_id, existing) else None

    def remove_last_custom_wound(self, pointer_id: str) -> tuple[bool, str]:
        pointer = self.get_pointer(pointer_id)
        if pointer is None:
            return False, "未找到当前角色指针"
        profile = self.load_profile(pointer)
        existing = [dict(w) for w in profile.wounds]
        if not existing:
            return False, "当前没有可删除的自定义敏感点"
        removed = existing.pop()
        if self.update_custom_wounds(pointer_id, existing):
            return True, f"已移除自定义敏感点「{removed.get('label') or removed.get('id')}」"
        return False, "移除自定义敏感点失败"

    def update_custom_domains(self, pointer_id: str, domains: list[Mapping[str, Any]]) -> bool:
        """更新角色包顶层 `custom_domains:` 列表（L3 自定义域与自我效能）。"""
        cleaned: list[dict[str, Any]] = []
        for idx, item in enumerate(domains):
            if not isinstance(item, Mapping):
                continue
            try:
                efficacy = round(max(0.0, min(1.0, float(item.get("efficacy", 0.5)))), 3)
            except (TypeError, ValueError):
                efficacy = 0.5
            did = _clean_id(str(item.get("id") or f"domain_{idx + 1}"), fallback=f"domain_{idx + 1}")
            label = str(item.get("label") or f"自定义域 {idx + 1}").strip()
            patterns = str(item.get("patterns") or "").strip()
            cleaned.append({
                "id": did,
                "label": label,
                "efficacy": efficacy,
                "patterns": patterns,
            })
        return self._rewrite_top_list(pointer_id, "custom_domains", cleaned)

    def add_custom_domain(
        self,
        pointer_id: str,
        *,
        label: str = "",
        efficacy: float = 0.5,
        patterns: str = "",
    ) -> dict[str, Any] | None:
        pointer = self.get_pointer(pointer_id)
        if pointer is None:
            return None
        profile = self.load_profile(pointer)
        existing = [dict(d) for d in profile.custom_domains]
        used_ids = {str(d.get("id") or "") for d in existing}
        idx = len(existing) + 1
        while f"domain_{idx}" in used_ids:
            idx += 1
        item = {
            "id": f"domain_{idx}",
            "label": str(label or f"自定义域 {idx}").strip(),
            "efficacy": round(max(0.0, min(1.0, float(efficacy))), 3),
            "patterns": str(patterns or "").strip(),
        }
        existing.append(item)
        return item if self.update_custom_domains(pointer_id, existing) else None

    def scaffold_pointer(
        self,
        *,
        new_id: str = "",
        character_name: str = "",
        source_id: str = TEMPLATE_DIR_NAME,
    ) -> PointerSpec:
        """从空白模板或现有角色包复制生成一条新指针。"""
        with self._lock:
            self._reload_if_needed()
            existing_ids = {p.id for p in self._pointers}
            if new_id:
                pid = _clean_id(new_id)
            else:
                idx = 1
                while f"persona_{idx}" in existing_ids:
                    idx += 1
                pid = f"persona_{idx}"
            if pid in existing_ids:
                suffix = 2
                while f"{pid}_{suffix}" in existing_ids:
                    suffix += 1
                pid = f"{pid}_{suffix}"

            src_dir: Path | None = None
            src_pointer = next((p for p in self._pointers if p.id == source_id), None)
            if src_pointer is not None:
                src_dir = self.resolve_profile_dir(src_pointer)
            if src_dir is None or not src_dir.is_dir():
                cand = (self._plugin_root / "profiles" / source_id).resolve()
                src_dir = cand if cand.is_dir() else (self._plugin_root / "profiles" / TEMPLATE_DIR_NAME).resolve()

            dest_dir = (self._data_root / "profiles" / pid).resolve()
            if dest_dir.exists():
                shutil.rmtree(dest_dir, ignore_errors=True)
            if src_dir.is_dir():
                shutil.copytree(src_dir, dest_dir)
                self._localize_profile_assets(src_dir, dest_dir, pointer=src_pointer)
            else:
                dest_dir.mkdir(parents=True, exist_ok=True)
                (dest_dir / "soul.md").write_text("# 新角色人格壳\n\n## 画像\n\n写下这个角色是谁、怎么说话、什么不做。\n", encoding="utf-8")
                (dest_dir / PROFILE_FILE).write_text(
                    f"id: {pid}\ndisplay_name: {character_name or pid}\ncharacter_name: {character_name or pid}\nsoul:\n  file: soul.md\n",
                    encoding="utf-8",
                )

            cname = str(character_name or f"新角色_{pid}").strip()
            settings_write.write_values(
                dest_dir / PROFILE_FILE,
                {"id": pid, "display_name": cname, "character_name": cname},
            )
            if source_id == TEMPLATE_DIR_NAME and cname and not cname.startswith("新角色_"):
                self._sync_host_character_to_dir(dest_dir, cname, force_if_template=True)
            new_pointer = PointerSpec(
                id=pid,
                enabled=True,
                character_name=cname,
                profile_dir=f"profiles/{pid}",
                state_root="",
                chat_key_prefix=f"shinsekai_{pid}",
            )
            self._pointers.append(new_pointer)
            self._save_pointers(self._pointers, pid)
            self._profile_cache.clear()
            return new_pointer

    def sync_bound_host_character(self, pointer_id: str, character_name: str) -> None:
        """修改既有指针的目标角色后，同步其 display_name 与未自定义的模板 soul.md。"""
        pointer = self.get_pointer(pointer_id)
        if pointer is None:
            return
        pdir = self.ensure_editable_profile_dir(pointer)
        self._sync_host_character_to_dir(pdir, character_name, force_if_template=True)
        with self._lock:
            self._profile_cache.clear()

    def resolve_or_create_host_pointer(self, character_name: str) -> PointerSpec | None:
        """按宿主角色名定位已有指针；若尚未绑定，则自动从空白模板新建并绑定。"""
        cname = str(character_name or "").strip()
        if not cname:
            return None
        for one in self.list_pointers():
            if one.character_name == cname:
                self.set_active_edit(one.id)
                return one
        return self.scaffold_pointer(character_name=cname, source_id=TEMPLATE_DIR_NAME)

    def delete_pointer(self, pointer_id: str) -> tuple[bool, str]:
        pid = str(pointer_id or "").strip()
        if pid == DEFAULT_PRIMARY_ID:
            return False, "不能删除默认参考指针 nanami_chiaki"
        with self._lock:
            self._reload_if_needed()
            kept = [p for p in self._pointers if p.id != pid]
            if len(kept) == len(self._pointers):
                return False, f"找不到指针 {pid}"
            next_edit = self._active_edit_id
            if next_edit == pid:
                next_edit = kept[0].id if kept else DEFAULT_PRIMARY_ID
            self._save_pointers(kept, next_edit)
            self._profile_cache.clear()
            return True, f"已移除指针 {pid}（磁盘目录保留）"

    def read_soul_text(self, pointer_id: str) -> str:
        """读取当前指针绑定的人格壳原文；找不到时返回空串。"""
        pointer = self.get_pointer(pointer_id)
        if pointer is None:
            return ""
        profile = self.load_profile(pointer)
        rel = profile.soul_file or "soul.md"
        path = profile.resolve_path(rel, plugin_root=self._plugin_root)
        if path is None or not path.is_file():
            return ""
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return ""

    def save_soul_text(self, pointer_id: str, content: str) -> tuple[bool, str]:
        """将编辑器里的人格壳 Markdown 写入当前指针的可编辑目录（自动实体化共享/只读文件）。"""
        pointer = self.get_pointer(pointer_id)
        if pointer is None:
            return False, "未找到当前角色指针"
        pdir = self.ensure_editable_profile_dir(pointer)
        profile = self.load_profile(pointer)
        yaml_path = pdir / PROFILE_FILE
        rel = str(profile.soul_file or "soul.md").strip() or "soul.md"
        rel_path = Path(rel)
        if rel_path.is_absolute() or ".." in rel_path.parts:
            target = (pdir / "soul.md").resolve()
            next_rel = "soul.md"
        else:
            target = (pdir / rel_path).resolve()
            if pdir.resolve() not in target.parents and target != pdir.resolve():
                target = (pdir / "soul.md").resolve()
                next_rel = "soul.md"
            else:
                next_rel = rel
        text = str(content or "")
        if text and not text.endswith("\n"):
            text += "\n"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        if yaml_path.is_file() and next_rel != profile.soul_file:
            settings_write.write_values(yaml_path, {"soul.file": next_rel})
        with self._lock:
            self._profile_cache.clear()
        return True, f"已保存人格壳（{len(text.strip())} 字）→ {target.name}"

    def import_soul_from_source(self, pointer_id: str, source_key: str) -> tuple[bool, str]:
        """从宿主角色包（host:<name>）、插件指针（pointer:<id>）或空白模板（template:_template）导入人格壳。"""
        pointer = self.get_pointer(pointer_id)
        if pointer is None:
            return False, "未找到当前角色指针"
        raw_src = str(source_key or "").strip()
        if not raw_src:
            return False, "请先在「导入来源」下拉中选择要导入的角色包或模板"

        if raw_src.startswith("pointer:"):
            src_pid = raw_src[len("pointer:"):].strip()
            src_text = self.read_soul_text(src_pid)
            if not src_text.strip():
                return False, f"指针 {src_pid} 没有可读的人格壳文件"
            ok, msg = self.save_soul_text(pointer_id, src_text)
            return (ok, f"已从指针「{src_pid}」导入人格壳") if ok else (False, msg)

        if raw_src.startswith("template:"):
            tpl_name = raw_src[len("template:"):].strip() or TEMPLATE_DIR_NAME
            tpl_path = (self._plugin_root / "profiles" / tpl_name / "soul.md").resolve()
            if not tpl_path.is_file():
                return False, f"找不到模板人格壳：{tpl_name}"
            ok, msg = self.save_soul_text(pointer_id, tpl_path.read_text(encoding="utf-8"))
            return (ok, "已恢复空白通用人格壳模板") if ok else (False, msg)

        if raw_src.startswith("host:"):
            host_name = raw_src[len("host:"):].strip()
            entry = self._host_character_entry(host_name)
            if entry is None:
                return False, f"在宿主角色列表中找不到「{host_name}」"
            brief = entry.get("brief", "").strip()
            setting = entry.get("setting", "").strip()
            if not brief and not setting:
                return False, f"宿主角色「{host_name}」的角色设定为空"

            # 保留当前（或模板的）<!-- route: ... --> 场景段，避免覆盖掉路由触发结构
            existing = self.read_soul_text(pointer_id)
            route_tail = ""
            for marker in ("## 4. 场景段", "<!-- route:"):
                idx = existing.find(marker)
                if idx >= 0:
                    route_tail = "\n\n" + existing[idx:].strip()
                    break
            if not route_tail:
                tpl_soul = (self._plugin_root / "profiles" / TEMPLATE_DIR_NAME / "soul.md")
                if tpl_soul.is_file():
                    tpl_txt = tpl_soul.read_text(encoding="utf-8")
                    idx = tpl_txt.find("## 4. 场景段")
                    if idx >= 0:
                        route_tail = "\n\n" + tpl_txt[idx:].strip()

            excerpt = setting[:1200].rstrip() + ("……（完整设定已由宿主角色卡携带）" if len(setting) > 1200 else "")
            parts = [p for p in (brief, excerpt) if p]
            imported = (
                f"# {host_name} · 人格壳\n\n"
                f"> 已从宿主角色包「{host_name}」导入设定，可直接在此修改说话节奏、口语习惯与行为边界。\n\n"
                f"## 画像\n\n"
                + "\n\n".join(parts)
                + "\n\n## 1. 说话节奏\n\n"
                "- 句长自然口语化，保留符合角色性格的语气词与停顿节奏。\n\n"
                "## 2. 绝对不这样说话\n\n"
                "- 不使用「有什么可以帮你的」「我理解你的意思」「收到」等客服与系统腔。\n"
                "- 不评论对话本身（如「我们换个话题吧」）。"
                f"{route_tail}\n"
            )
            ok, msg = self.save_soul_text(pointer_id, imported)
            if ok:
                # 同步清除可能旧的 present_needle 防止错配跳过注入
                pdir = self.ensure_editable_profile_dir(pointer)
                yaml_path = pdir / PROFILE_FILE
                if yaml_path.is_file():
                    settings_write.write_values(yaml_path, {"soul.present_needle": ""})
                    with self._lock:
                        self._profile_cache.clear()
                return True, f"已从宿主角色包「{host_name}」导入人格壳（{len(imported)} 字）"
            return False, msg

        return False, f"不支持的导入来源：{raw_src}"
