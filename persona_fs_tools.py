"""受控的破壁文件读写工具：仅在角色包显式开启 `file_access.enabled` 时暴露给 LLM。

安全边界：
- 仅注册到独立组 `persona_fs`，普通角色与默认对话不可见；
- 读/写路径均做 `resolve()` + 白名单根目录校验（`is_relative_to`），禁止 `..` 逃逸；
- 不提供删除/移动/系统命令能力，写入默认仅限配置授权目录。
"""

import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

TOOL_GROUP = "persona_fs"
_SKIP_DIRS = frozenset({
    "__pycache__", ".git", ".pytest_cache", ".mypy_cache",
    "node_modules", ".venv", "venv", "runtime",
})
_READ_TOOL_NAMES = ("persona_fs_list", "persona_fs_search", "persona_fs_read")
_WRITE_TOOL_NAMES = ("persona_fs_write", "persona_fs_append")


@dataclass(frozen=True)
class FileAccessScope:
    """当前轮次允许角色访问的文件根目录策略。"""

    enabled: bool = False
    allow_write: bool = False
    base_dir: Path | None = None
    read_roots: tuple[Path, ...] = ()
    write_roots: tuple[Path, ...] = ()


_LOCK = threading.Lock()
_SCOPE = FileAccessScope()
_REGISTERED_DEFINITIONS: list[dict[str, Any]] = []


def set_scope(scope: FileAccessScope) -> None:
    with _LOCK:
        global _SCOPE
        _SCOPE = scope


def get_scope() -> FileAccessScope:
    with _LOCK:
        return _SCOPE


def _normalize_roots(raw_roots: list[Path | str], base_dir: Path) -> tuple[Path, ...]:
    out: list[Path] = []
    seen: set[str] = set()
    for item in raw_roots:
        if not item:
            continue
        p = Path(item).expanduser()
        if not p.is_absolute():
            p = base_dir / p
        try:
            resolved = p.resolve()
        except OSError:
            continue
        key = str(resolved).lower()
        if key not in seen:
            seen.add(key)
            out.append(resolved)
    return tuple(out)


def configure_runtime_scope(
    *,
    enabled: bool,
    allow_write: bool,
    base_dir: Path,
    read_roots: list[Path | str],
    write_roots: list[Path | str],
) -> FileAccessScope:
    base_resolved = Path(base_dir).resolve()
    r_roots = _normalize_roots(read_roots, base_resolved)
    w_roots = _normalize_roots(write_roots, base_resolved) if allow_write else ()
    scope = FileAccessScope(
        enabled=bool(enabled),
        allow_write=bool(allow_write),
        base_dir=base_resolved,
        read_roots=r_roots,
        write_roots=w_roots,
    )
    set_scope(scope)
    return scope


def _is_under_any(target: Path, roots: tuple[Path, ...]) -> bool:
    for root in roots:
        try:
            if target == root or target.is_relative_to(root):
                return True
        except ValueError:
            continue
    return False


def _resolve_checked(raw_path: str, *, for_write: bool) -> tuple[Path | None, dict[str, Any] | None]:
    scope = get_scope()
    if not scope.enabled:
        return None, {"error": "file_access is disabled for the active character"}
    if for_write and not scope.allow_write:
        return None, {"error": "write access is disabled for the active character"}

    base = scope.base_dir or Path.cwd().resolve()
    cleaned = str(raw_path or ".").strip() or "."
    candidate = Path(cleaned).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        return None, {"error": f"invalid path: {exc}"}

    allowed_roots = scope.write_roots if for_write else scope.read_roots
    if not _is_under_any(resolved, allowed_roots):
        return None, {
            "error": "access denied: path outside allowed roots",
            "path": str(resolved),
            "allowed_roots": [str(r) for r in allowed_roots],
        }
    return resolved, None


def _fmt_mtime(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def persona_fs_list(path: str = ".") -> dict[str, Any]:
    """列出授权目录下的文件和子目录（按最近修改时间倒序排列），用于观察当前工作区真实文件。"""
    target, err = _resolve_checked(path, for_write=False)
    if err:
        return err
    assert target is not None
    if not target.exists():
        return {"error": f"directory not found: {target}"}
    if not target.is_dir():
        return {"error": f"not a directory: {target}"}

    rows: list[dict[str, Any]] = []
    try:
        for entry in target.iterdir():
            if entry.name in _SKIP_DIRS:
                continue
            try:
                st = entry.stat()
            except OSError:
                continue
            rows.append({
                "name": entry.name,
                "path": str(entry),
                "type": "dir" if entry.is_dir() else "file",
                "size": st.st_size if entry.is_file() else 0,
                "mtime": st.st_mtime,
                "modified_at": _fmt_mtime(st.st_mtime),
            })
    except PermissionError:
        return {"error": f"permission denied: {target}"}

    rows.sort(key=lambda r: float(r["mtime"]), reverse=True)
    for r in rows:
        r.pop("mtime", None)
    scope = get_scope()
    return {
        "directory": str(target),
        "allowed_read_roots": [str(r) for r in scope.read_roots],
        "allowed_write_roots": [str(r) for r in scope.write_roots],
        "count": len(rows),
        "items": rows[:60],
    }


def persona_fs_search(pattern: str = "*", dir_path: str = ".") -> dict[str, Any]:
    """在授权目录中按文件名通配符搜索（如 '*test*.py' 或 '*'），按最近修改时间倒序返回。"""
    target, err = _resolve_checked(dir_path, for_write=False)
    if err:
        return err
    assert target is not None
    if not target.is_dir():
        return {"error": f"not a directory: {target}"}

    pat = str(pattern or "*").strip() or "*"
    hits: list[dict[str, Any]] = []
    try:
        for cand in target.rglob(pat):
            if any(part in _SKIP_DIRS for part in cand.parts):
                continue
            if not cand.is_file():
                continue
            try:
                st = cand.stat()
            except OSError:
                continue
            hits.append({
                "name": cand.name,
                "path": str(cand),
                "size": st.st_size,
                "mtime": st.st_mtime,
                "modified_at": _fmt_mtime(st.st_mtime),
            })
            if len(hits) >= 200:
                break
    except PermissionError:
        return {"error": f"permission denied: {target}"}

    hits.sort(key=lambda r: float(r["mtime"]), reverse=True)
    top = hits[:40]
    for r in top:
        r.pop("mtime", None)
    return {
        "pattern": pat,
        "directory": str(target),
        "count": len(top),
        "matches": top,
    }


def persona_fs_read(
    path: str,
    max_chars: int = 4000,
    line_start: int = 0,
    line_end: int = 0,
) -> dict[str, Any]:
    """读取授权路径下的文本文件内容，可指定起止行号（1-indexed）。"""
    target, err = _resolve_checked(path, for_write=False)
    if err:
        return err
    assert target is not None
    if not target.is_file():
        return {"error": f"file not found: {target}"}

    limit = max(200, min(16000, int(max_chars or 4000)))
    try:
        raw = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"error": str(exc), "path": str(target)}

    lines = raw.splitlines()
    total_lines = len(lines)
    l_start = int(line_start or 0)
    l_end = int(line_end or 0)
    if l_start > 0:
        s = max(0, l_start - 1)
        e = min(total_lines, l_end) if l_end > 0 else total_lines
        body = "\n".join(lines[s:e])
    else:
        body = raw
    truncated = len(body) > limit
    return {
        "path": str(target),
        "total_lines": total_lines,
        "modified_at": _fmt_mtime(target.stat().st_mtime),
        "truncated": truncated,
        "content": body[:limit],
    }


def persona_fs_write(path: str, content: str) -> dict[str, Any]:
    """在授权可写目录内新建或覆写文本文件，留下真实改动痕迹。"""
    target, err = _resolve_checked(path, for_write=True)
    if err:
        return err
    assert target is not None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        existed = target.exists()
        target.write_text(str(content or ""), encoding="utf-8")
        logger.info("nanami_persona: 角色写入文件 %s (%d chars)", target, len(str(content or "")))
        return {
            "status": "ok",
            "action": "overwritten" if existed else "created",
            "path": str(target),
            "bytes": len(str(content or "").encode("utf-8")),
        }
    except OSError as exc:
        return {"error": str(exc), "path": str(target)}


def persona_fs_append(path: str, content: str) -> dict[str, Any]:
    """向授权可写目录内的文件末尾追加文本。"""
    target, err = _resolve_checked(path, for_write=True)
    if err:
        return err
    assert target is not None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(str(content or ""))
        logger.info("nanami_persona: 角色追加文件 %s (%d chars)", target, len(str(content or "")))
        return {
            "status": "ok",
            "action": "appended",
            "path": str(target),
            "bytes_added": len(str(content or "").encode("utf-8")),
        }
    except OSError as exc:
        return {"error": str(exc), "path": str(target)}


def register_tools(tool_manager: Any) -> None:
    """向宿主 ToolManager 注册角色文件读写工具（默认放入隔离组 persona_fs）。"""
    funcs = (
        persona_fs_list,
        persona_fs_search,
        persona_fs_read,
        persona_fs_write,
        persona_fs_append,
    )
    for fn in funcs:
        tool_manager.register_function(
            fn,
            name=fn.__name__,
            description=(fn.__doc__ or "").strip(),
            group=TOOL_GROUP,
            risk="low",
        )
    with _LOCK:
        _REGISTERED_DEFINITIONS.clear()
        for d in tool_manager.get_definitions(groups=[TOOL_GROUP]):
            _REGISTERED_DEFINITIONS.append(d)


def get_tool_definitions(*, allow_write: bool) -> list[dict[str, Any]]:
    """取出当前角色允许使用的工具 schema 列表，注入 BeforeChatContext.tools。"""
    allowed_names = set(_READ_TOOL_NAMES)
    if allow_write:
        allowed_names.update(_WRITE_TOOL_NAMES)
    with _LOCK:
        if _REGISTERED_DEFINITIONS:
            return [
                d for d in _REGISTERED_DEFINITIONS
                if str(d.get("function", {}).get("name") or "") in allowed_names
            ]
    # 兜底：若尚未通过宿主 ToolManager 预缓存，临时构建一份标准 ToolManager schema
    try:
        from ai.tools.tool_manager import ToolManager

        tm = ToolManager()
        defs = tm.get_definitions(groups=[TOOL_GROUP])
        if not defs:
            register_tools(tm)
            defs = tm.get_definitions(groups=[TOOL_GROUP])
        return [
            d for d in defs
            if str(d.get("function", {}).get("name") or "") in allowed_names
        ]
    except Exception:
        return []
