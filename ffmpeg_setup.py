"""ffmpeg 自举：找不到就自己装一份，装到插件自己的数据目录里。

背景：宿主 2.6.0 的 macOS 包里没有系统 ffmpeg，PATH 里也查不到；Windows 用户
则常常没装过。裸调 "ffmpeg" 会 FileNotFoundError，B 站音轨下载和转码整段作废，
而用户看不出为什么（失败发生在第 111 秒）。

顺序：环境变量 → 插件数据目录 → 本机常见位置 → PATH → 从本机已有的副本复制
      → 下载静态构建（gh-proxy → 直连 GitHub）。
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import http_client

logger = logging.getLogger(__name__)

# 静态构建来源：eugeneware/ffmpeg-static
_RELEASE = "b6.1.1"
_ASSETS: dict[tuple[str, str], str] = {
    ("darwin", "arm64"): "ffmpeg-darwin-arm64",
    ("darwin", "x86_64"): "ffmpeg-darwin-x64",
    ("linux", "x86_64"): "ffmpeg-linux-x64",
    ("linux", "aarch64"): "ffmpeg-linux-arm64",
    ("linux", "armv7l"): "ffmpeg-linux-arm",
    ("win32", "amd64"): "ffmpeg-win32-x64",
    ("win32", "x86"): "ffmpeg-win32-ia32",
}
_MIRRORS = (
    "https://gh-proxy.com/https://github.com/eugeneware/ffmpeg-static/releases/download",
    "https://github.com/eugeneware/ffmpeg-static/releases/download",
)

_LOCK = threading.Lock()
_JOB: dict[str, Any] = {"status": "idle", "message": "", "percent": 0.0, "path": "", "error": ""}
_JOB_THREAD: threading.Thread | None = None


# ── 路径 ────────────────────────────────────────────────────────────────
def bin_dir() -> Path:
    """插件自己的二进制目录。优先 <项目根>/data/nanami/bin，退回 ~/.nanami/bin。"""
    env = str(os.environ.get("NANAMI_BIN_DIR", "")).strip()
    if env:
        return Path(env).expanduser()
    try:
        root = Path(__file__).resolve().parents[2]
        if (root / "data").is_dir():
            return root / "data" / "nanami" / "bin"
    except (IndexError, OSError):
        pass
    return Path.home() / ".nanami" / "bin"


def _exe_name() -> str:
    return "ffmpeg.exe" if os.name == "nt" else "ffmpeg"


def asset_name() -> str:
    sysname = "win32" if os.name == "nt" else platform.system().lower()
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64") and sysname == "darwin":
        # Apple Silicon 上用 Rosetta 跑 x86 的 Python 时也会报 x86_64
        if platform.processor() == "arm" or os.environ.get("NANAMI_FORCE_ARM64") == "1":
            machine = "arm64"
        else:
            # 优先给 arm64（原生更快）；真的是 Intel 机器时反正是 x64
            machine = "arm64" if _is_apple_silicon() else "x86_64"
    return _ASSETS.get((sysname, machine), "")


def _is_apple_silicon() -> bool:
    try:
        import subprocess

        out = subprocess.run(["sysctl", "-n", "hw.optional.arm64"], capture_output=True, text=True, timeout=5)
        return out.stdout.strip() == "1"
    except Exception:  # noqa: BLE001
        return False


# ── 解析 ────────────────────────────────────────────────────────────────
def candidates() -> list[Path]:
    """按优先级列出可能的 ffmpeg 位置（不检查可执行性，只列路径）。"""
    out: list[Path] = []
    env = str(os.environ.get("SHINSEKAI_NANAMI_FFMPEG", "")).strip()
    if env:
        out.append(Path(env).expanduser())
    out.append(bin_dir() / _exe_name())
    out.extend(
        Path(p)
        for p in (
            "/opt/homebrew/bin/ffmpeg",
            "/usr/local/bin/ffmpeg",
            "/usr/bin/ffmpeg",
        )
    )
    out.append(Path.home() / ".local" / "bin" / _exe_name())
    return out


def resolve() -> str:
    """只解析，不下载。找不到返回空串。"""
    for cand in candidates():
        try:
            if cand.is_file():
                return str(cand)
        except OSError:
            continue
    found = shutil.which("ffmpeg")
    if found:
        return found
    # 本机装了 imageio-ffmpeg 的话，它自带的副本可以直接用
    try:
        import imageio_ffmpeg

        exe = Path(imageio_ffmpeg.get_ffmpeg_exe())
        if exe.is_file():
            return str(exe)
    except Exception:  # noqa: BLE001
        pass
    return ""


def copy_from_local() -> str:
    """把本机已有的 ffmpeg 复制到插件目录，免得每次都靠外部路径。"""
    target = bin_dir() / _exe_name()
    src = resolve()
    if not src or Path(src) == target:
        return src
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
        http_client.chmod_x(target)
        logger.info("ffmpeg: 已从 %s 复制到 %s", src, target)
        return str(target)
    except OSError as exc:
        logger.warning("ffmpeg: 复制失败，继续用原路径 %s（%s）", src, exc)
        return src


# ── 下载 ────────────────────────────────────────────────────────────────
def download_static(progress: Callable[[int, int], None] | None = None) -> str:
    asset = asset_name()
    if not asset:
        raise RuntimeError(
            f"没有适配当前平台的 ffmpeg 静态包（{platform.system()} / {platform.machine()}）"
        )
    target = bin_dir() / _exe_name()
    urls: list[str] = []
    for base in _MIRRORS:
        # .gz 体积小一半，优先；失败再换成原始二进制
        urls.append(f"{base}/{_RELEASE}/{asset}.gz")
    for base in _MIRRORS:
        urls.append(f"{base}/{_RELEASE}/{asset}")

    logger.info("ffmpeg: 开始下载 %s → %s", asset, target)

    # 先试 gz（边下边解压），全部失败再试原始二进制
    gz_urls = [u for u in urls if u.endswith(".gz")]
    raw_urls = [u for u in urls if not u.endswith(".gz")]
    try:
        http_client.download(gz_urls, target, progress=progress, timeout=120.0, gunzip=True)
    except Exception as gz_exc:  # noqa: BLE001
        logger.warning("ffmpeg: .gz 通道失败，改用原始二进制（%s）", gz_exc)
        http_client.download(raw_urls, target, progress=progress, timeout=120.0)
    http_client.chmod_x(target)
    if target.stat().st_size < 1024 * 1024:
        target.unlink(missing_ok=True)
        raise RuntimeError("下载到的 ffmpeg 体积异常，已丢弃")
    logger.info("ffmpeg: 安装完成 %s（%.1f MB）", target, target.stat().st_size / 1048576)
    return str(target)


def ensure(*, allow_download: bool = True,
           progress: Callable[[int, int], None] | None = None) -> tuple[str, str]:
    """保证有一个可用的 ffmpeg。返回 (路径, 说明)；拿不到时返回 ("", 原因)。"""
    found = resolve()
    if found:
        return found, ""
    copied = copy_from_local()
    if copied:
        return copied, "使用本机已有的 ffmpeg 副本"
    if not allow_download:
        return "", "没有找到 ffmpeg，自动安装在设置里关掉了"
    try:
        path = download_static(progress)
        return path, "已自动下载并安装 ffmpeg"
    except Exception as exc:  # noqa: BLE001
        return "", f"缺少 ffmpeg 且自动安装失败：{type(exc).__name__}: {exc}"


# ── 后台安装任务（给设置页轮询用）────────────────────────────────────────
def install_status() -> dict[str, Any]:
    with _LOCK:
        return dict(_JOB)


def start_install() -> dict[str, Any]:
    """后台下载 ffmpeg，返回当前任务快照。已在装就复用。"""
    global _JOB_THREAD
    with _LOCK:
        if _JOB.get("status") == "running":
            return dict(_JOB)
        if resolve():
            _JOB.update({"status": "done", "percent": 100.0, "path": resolve(),
                         "message": "本机已有 ffmpeg，无需下载", "error": ""})
            return dict(_JOB)
        _JOB.update({"status": "running", "percent": 0.0, "path": "", "error": "",
                     "message": "开始下载 ffmpeg…"})

    def _progress(done: int, total: int) -> None:
        with _LOCK:
            _JOB["percent"] = round(min(99.0, done * 100.0 / total), 1) if total else 0.0
            _JOB["message"] = f"已下载 {done / 1048576:.1f} MB" + (f" / {total / 1048576:.1f} MB" if total else "")

    def _worker() -> None:
        try:
            path = download_static(_progress)
            with _LOCK:
                _JOB.update({"status": "done", "percent": 100.0, "path": path,
                             "message": f"ffmpeg 已安装：{path}", "error": ""})
        except Exception as exc:  # noqa: BLE001
            logger.exception("ffmpeg: 自动安装失败")
            with _LOCK:
                _JOB.update({"status": "error", "error": f"{type(exc).__name__}: {exc}",
                             "message": "ffmpeg 安装失败"})

    _JOB_THREAD = threading.Thread(target=_worker, name="nanami-ffmpeg-install", daemon=True)
    _JOB_THREAD.start()
    with _LOCK:
        return dict(_JOB)


def status() -> dict[str, Any]:
    """给设置页的一行状态。"""
    path = resolve()
    snap = install_status()
    return {
        "available": bool(path),
        "path": path,
        "source": "插件目录" if path and bin_dir() in Path(path).parents else ("PATH/其它" if path else ""),
        "bin_dir": str(bin_dir()),
        "install": snap,
        "asset": asset_name(),
    }
