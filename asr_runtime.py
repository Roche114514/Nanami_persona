"""ASR 运行环境自举：没有整合包、或整合包解释器里缺 faster-whisper 时，自己装一份。

背景：插件自身零 Python 依赖，`faster_whisper` 这个包原本指望 GPT-SoVITS 整合包提供
（官方 Windows 包自带 1.1.1）。两种情况下这条腿会断：
  · 用户没装整合包（只走云 TTS），Whisper 分支根本进不去
  · 自建/移植环境里整合包没有那个包（比如 macOS 上我们用自建 venv 顶替了 Windows runtime）

做法：用宿主自带的 pip 把 faster-whisper 装进**插件数据目录**（`--target`），
再用 `PYTHONPATH` 交给子进程。不碰宿主 site-packages，不碰整合包，不需要管理员权限，
也不引入 torch（实测 244 MB / 约 30 秒）。
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

_PACKAGE = "faster-whisper"
_DEFAULT_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"
_LOCK = threading.Lock()
_JOB: dict[str, Any] = {"status": "idle", "message": "", "percent": 0.0, "error": ""}
_THREAD: threading.Thread | None = None


def env_dir() -> Path:
    env = str(os.environ.get("NANAMI_ASR_ENV_DIR", "")).strip()
    if env:
        return Path(env).expanduser()
    try:
        root = Path(__file__).resolve().parents[2]
        if (root / "data").is_dir():
            return root / "data" / "nanami" / "asr_env"
    except (IndexError, OSError):
        pass
    return Path.home() / ".nanami" / "asr_env"


def libs_dir() -> Path:
    return env_dir() / "libs"


def installed_version() -> str:
    """读 dist-info 拿版本号；没有就返回空串。"""
    try:
        for entry in libs_dir().iterdir():
            name = entry.name
            if name.startswith("faster_whisper-") and name.endswith(".dist-info"):
                return name[len("faster_whisper-"):-len(".dist-info")]
    except OSError:
        pass
    return ""


def whisper_env() -> dict[str, str]:
    """给子进程用的环境变量：把私有 libs 目录挂到 PYTHONPATH 最前面。"""
    libs = str(libs_dir())
    old = os.environ.get("PYTHONPATH", "")
    return {"PYTHONPATH": libs + (os.pathsep + old if old else "")}


def whisper_target() -> dict[str, Any]:
    """私有运行环境能不能用。python 用宿主自己的解释器（版本匹配、路径稳定）。"""
    import sys

    version = installed_version()
    if not version:
        return {"ok": False, "python": "", "env": {}, "version": "", "libs": str(libs_dir())}
    return {
        "ok": True,
        "python": sys.executable,
        "env": whisper_env(),
        "version": version,
        "libs": str(libs_dir()),
    }


def _pip_env() -> dict[str, str]:
    return {**os.environ, "PYTHONUTF8": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1"}


def pip_install(progress: Callable[[str, float], None] | None = None, *, timeout: float = 1800.0) -> str:
    """把 faster-whisper 装进私有 libs 目录，返回装好的版本号。"""
    import sys

    libs = libs_dir()
    libs.mkdir(parents=True, exist_ok=True)
    index = str(os.environ.get("SHINSEKAI_NANAMI_PIP_INDEX", "") or _DEFAULT_INDEX).strip()
    cmd = [
        sys.executable, "-m", "pip", "install",
        "--target", str(libs),
        "--upgrade",
        "--disable-pip-version-check",
        "--no-warn-script-location",
        "-i", index,
        _PACKAGE,
    ]
    logger.info("asr_runtime: 开始安装 %s → %s（源 %s）", _PACKAGE, libs, index)
    if progress is not None:
        progress(f"正在解析依赖（源 {index}）…", 0.05)

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=_pip_env(),
        cwd=str(libs.parent),
    )
    started = time.time()
    tail: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        text = line.strip()
        if not text:
            continue
        tail.append(text)
        if len(tail) > 40:
            tail = tail[-40:]
        if progress is not None:
            elapsed = time.time() - started
            # pip 没有机器可读进度，用阶段文字 + 时间给个粗估
            pct = min(0.9, 0.05 + elapsed / max(30.0, timeout) * 0.8)
            progress(text[:120], pct)
        if time.time() - started > timeout:
            proc.kill()
            raise RuntimeError("安装超时（网络太慢？可换 SHINSEKAI_NANAMI_PIP_INDEX）")
    code = proc.wait()
    if code != 0:
        raise RuntimeError("pip 安装失败：" + " / ".join(tail[-4:])[:400])

    version = installed_version()
    if not version:
        raise RuntimeError("pip 报告成功，但 libs 目录里没有 faster_whisper 的 dist-info")
    logger.info("asr_runtime: 安装完成 faster-whisper %s", version)
    if progress is not None:
        progress(f"faster-whisper {version} 已安装", 1.0)
    return version


def remove_runtime() -> None:
    import shutil

    shutil.rmtree(env_dir(), ignore_errors=True)
    with _LOCK:
        _JOB.update({"status": "idle", "message": "已移除", "percent": 0.0, "error": ""})


# ── 后台任务（给设置页轮询）──────────────────────────────────────────────
def install_status() -> dict[str, Any]:
    with _LOCK:
        snap = dict(_JOB)
    snap["installed_version"] = installed_version()
    snap["libs"] = str(libs_dir())
    return snap


def start_install() -> dict[str, Any]:
    global _THREAD
    with _LOCK:
        if _JOB.get("status") == "running":
            return dict(_JOB)
        if installed_version():
            _JOB.update({"status": "done", "percent": 100.0, "error": "",
                         "message": f"已安装 faster-whisper {installed_version()}"})
            return dict(_JOB)
        _JOB.update({"status": "running", "percent": 0.0, "error": "", "message": "开始安装…"})

    def _progress(message: str, pct: float) -> None:
        with _LOCK:
            _JOB["message"] = message
            _JOB["percent"] = round(max(_JOB.get("percent", 0.0), min(99.0, pct * 100)), 1)

    def _worker() -> None:
        try:
            version = pip_install(_progress)
            with _LOCK:
                _JOB.update({"status": "done", "percent": 100.0,
                             "message": f"faster-whisper {version} 已就绪", "error": ""})
        except Exception as exc:  # noqa: BLE001
            logger.exception("asr_runtime: 安装失败")
            with _LOCK:
                _JOB.update({"status": "error", "message": "安装失败",
                             "error": f"{type(exc).__name__}: {exc}"})

    _THREAD = threading.Thread(target=_worker, name="nanami-asr-env", daemon=True)
    _THREAD.start()
    with _LOCK:
        return dict(_JOB)


def status() -> dict[str, Any]:
    target = whisper_target()
    return {
        "available": target["ok"],
        "version": target["version"],
        "libs": target["libs"],
        "python": target["python"],
        "install": install_status(),
    }
