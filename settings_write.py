"""设置页的写回：按行改 `settings.yaml`，保留注释与顺序。

为什么不用 `yaml.safe_dump`：这份文件的注释就是说明书——里面写着每条参数的**原典依据**
（比如"她叫 X同学 共 203 次"、"4.58% 的台词带称呼"）。一次 dump 全冲掉，等于把研究记录丢了。

做法：
  · `a.b` 这种点号键，先定位顶层段（`mind:`），再在该段里找 `  b:`
  · 命中就只换值，保留行尾注释
  · 段里找不到就插在该段最后一行之后；整个段都没有就追加到文件末尾
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SECTION_RE = re.compile(r"^([A-Za-z_][\w\-]*):\s*(#.*)?$")
_KEY_RE = re.compile(r"^(\s+)([A-Za-z_][\w\-]*):(\s*)([^#\n]*?)(\s*#.*)?$")
_TOP_RE = re.compile(r"^([A-Za-z_][\w\-]*):(\s*)([^#\n]*?)(\s*#.*)?$")


def yaml_scalar(value: Any) -> str:
    """把 Python 值渲染成 YAML 行内标量（够用就好，不追求完整实现）。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value) if isinstance(value, float) else str(value)
    text = str(value if value is not None else "")
    if text == "":
        return '""'
    plain = re.fullmatch(r"[A-Za-z_][\w\-]*", text)
    if plain and text.lower() not in ("true", "false", "null", "yes", "no", "on", "off"):
        return text
    return json.dumps(text, ensure_ascii=False)


def flatten(values: dict[str, Any]) -> dict[str, Any]:
    """`{"mind": {"percept": True}}` → `{"mind.percept": True}`（只展开一层，够这套设置用）。"""
    out: dict[str, Any] = {}
    for key, value in values.items():
        if isinstance(value, dict):
            for inner, inner_value in value.items():
                out[f"{key}.{inner}"] = inner_value
        else:
            out[str(key)] = value
    return out


def set_scalar(lines: list[str], dotted: str, rendered: str, *, raw: Any = None) -> list[str]:
    """把 `dotted`（`a` 或 `a.b`）的值改成 `rendered`，返回新行列表。"""
    if "." in dotted:
        section, key = dotted.split(".", 1)
        return _set_in_section(lines, section, key, rendered, raw=raw)
    return _set_top(lines, dotted, rendered, raw=raw)


def _set_top(lines: list[str], key: str, rendered: str, *, raw: Any = None) -> list[str]:
    for index, line in enumerate(lines):
        match = _TOP_RE.match(line)
        if match and match.group(1) == key:
            trailing = match.group(4) or ""
            lines[index] = f"{key}: {rendered}{trailing}"
            return lines
    lines.append(f"{key}: {rendered}")
    return lines


def _set_in_section(lines: list[str], section: str, key: str, rendered: str, *, raw: Any = None) -> list[str]:
    start = -1
    for index, line in enumerate(lines):
        match = _SECTION_RE.match(line)
        if match and match.group(1) == section:
            start = index
            break
    if start < 0:
        lines.extend(["", f"{section}:", f"  {key}: {rendered}"])
        return lines

    # 段的结束位置：下一个顶层键，或者文件尾
    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if not line.strip():
            continue
        if not line.startswith((" ", "\t")) and _SECTION_RE.match(line):
            end = index
            break

    # 同名的缩进键（可能缩进 2 或 4 个空格）
    for index in range(start + 1, end):
        match = _KEY_RE.match(lines[index])
        if match and match.group(2) == key:
            indent = match.group(1) or "  "
            trailing = match.group(5) or ""
            lines[index] = f"{indent}{key}: {rendered}{trailing}"
            return lines

    # 插在段内最后一行非空行之后
    insert_at = start + 1
    for index in range(start + 1, end):
        if lines[index].strip():
            insert_at = index + 1
    indent = "  "
    for index in range(start + 1, end):
        match = _KEY_RE.match(lines[index])
        if match:
            indent = match.group(1) or "  "
            break
    lines.insert(insert_at, f"{indent}{key}: {rendered}")
    return lines


def write_values(path: Path, values: dict[str, Any]) -> None:
    """把设置写回文件。写坏了不抛给调用方（设置页保存失败只记日志）。"""
    flat = flatten(dict(values or {}))
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        text = ""
    lines = text.splitlines()
    for key, value in flat.items():
        try:
            lines = set_scalar(lines, key, yaml_scalar(value), raw=value)
        except Exception:  # noqa: BLE001
            logger.exception("nanami_persona: 写设置项 %s 失败，跳过", key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    except OSError:
        logger.exception("nanami_persona: 保存设置失败 %s", path)
