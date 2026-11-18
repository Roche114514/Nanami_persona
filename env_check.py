"""环境自检：把「跑起一键构建需要什么」集中问一遍。

三处用它：
  1. 插件加载时打日志（缺什么当场 WARNING，不要等用户跑到第 111 秒才炸）
  2. 设置页的 `env_status` action（页面顶部横幅）
  3. 一键构建的 preflight（缺件就拒绝启动，并说清怎么补）

原则：只报告和给建议，不在这里下载任何东西（下载走显式按钮）。
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import ffmpeg_setup

logger = logging.getLogger(__name__)


# ── 单项探测 ────────────────────────────────────────────────────────────
def _resolve_node() -> str:
    """按 mind_bridge 的口径找 node：环境变量 → 常见安装位置 → PATH。

    不能只用 shutil.which：宿主是双击启动的，PATH 和终端不是一套；
    用户把 node 装在 ~/.local 或配置了 node_exe 时 which 看不到。
    """
    for raw in (
        os.environ.get("SHINSEKAI_NANAMI_NODE", ""),
        os.environ.get("NANAMI_NODE", ""),
        str(Path.home() / ".local" / "bin" / "node"),
        str(Path.home() / ".local" / "node" / "bin" / "node"),
        "/opt/homebrew/bin/node",
        "/usr/local/bin/node",
        "/usr/bin/node",
    ):
        text = str(raw or "").strip()
        if text and Path(text).is_file():
            return text
    try:
        from .mind_bridge import _COMMON_NODE_PATHS

        for cand in _COMMON_NODE_PATHS:
            if Path(cand).is_file():
                return str(cand)
    except Exception:  # noqa: BLE001
        pass
    return shutil.which("node") or ""


def _node_info() -> dict[str, Any]:
    """node 是可选依赖：缺了只影响心理模型与字面守卫，聊天照常。"""
    found = _resolve_node()
    version = ""
    if found:
        try:
            out = subprocess.run([found, "--version"], capture_output=True, text=True, timeout=8)
            version = (out.stdout or "").strip()
        except Exception:  # noqa: BLE001
            version = ""
    return {
        "id": "node",
        "label": "Node.js 18+（心理模型 / 字面守卫）",
        "required": False,
        "ok": bool(found),
        "detail": f"{version or '未知版本'} · {found}" if found else "未找到 node",
        "hint": "缺了只会少「此刻」卡片与守卫，其余功能不受影响。需要就装 Node 18+ 或在设置里填 node_exe。",
    }


def _asr_info() -> dict[str, Any]:
    try:
        from . import bili_asr

        env = bili_asr._resolve_asr_environment()
    except Exception as exc:  # noqa: BLE001
        return {
            "id": "asr",
            "label": "本地语音识别（B 站音轨转写）",
            "required": True,
            "ok": False,
            "detail": f"探测失败：{type(exc).__name__}: {exc}",
            "hint": "需要本地 FunASR / Faster-Whisper / Vosk 之一，或装好 GPT-SoVITS 整合包。",
        }
    backends: list[str] = []
    if env.get("gpt_python"):
        backends.append("FunASR（GPT-SoVITS 环境）")
    if env.get("whisper_model_dir"):
        # 模型目录在 ≠ 能用：还得有 faster-whisper 这个包，否则调用时会 ModuleNotFoundError
        import importlib.util

        if importlib.util.find_spec("faster_whisper") is not None:
            backends.append("Faster-Whisper")
    if env.get("vosk_model_path"):
        backends.append("Vosk")
    ok = bool(backends)
    return {
        "id": "asr",
        "label": "本地语音识别（B 站音轨转写）",
        "required": True,
        "ok": ok,
        "detail": "、".join(backends) if ok else "没有可用的本地 ASR 后端",
        "hint": "缺了仍然能建角色，但语料只能靠字幕与百科，6 桶很难填满。先点「下载 ASR 模型」或装 GPT-SoVITS。",
    }


def _ffmpeg_info() -> dict[str, Any]:
    st = ffmpeg_setup.status()
    ok = bool(st.get("available"))
    snap = st.get("install") or {}
    detail = st.get("path") or "未找到 ffmpeg"
    if not ok and snap.get("status") == "running":
        detail = f"正在安装… {snap.get('message') or ''}"
    return {
        "id": "ffmpeg",
        "label": "ffmpeg（音轨转码，必需）",
        "required": True,
        "ok": ok,
        "detail": detail,
        "hint": "" if ok else "点「安装 ffmpeg」，插件会自动下载一份静态构建放到插件数据目录，不用你手动装。",
        "installing": snap.get("status") == "running",
        "bin_dir": st.get("bin_dir", ""),
    }


def _platform_info() -> dict[str, Any]:
    return {
        "id": "platform",
        "label": "运行平台",
        "required": False,
        "ok": True,
        "detail": f"{platform.system()} {platform.release()} / {platform.machine()} · Python {sys.version.split()[0]}",
        "hint": "",
    }


# ── 汇总 ────────────────────────────────────────────────────────────────
def report() -> dict[str, Any]:
    items = [_platform_info(), _ffmpeg_info(), _asr_info(), _node_info()]
    blocking = [i for i in items if i["required"] and not i["ok"]]
    return {
        "ok": not blocking,
        "items": items,
        "blocking": [i["id"] for i in blocking],
        "summary": "环境就绪" if not blocking else "缺少：" + "、".join(i["label"].split("（")[0] for i in blocking),
    }


def preflight(*, allow_download: bool = True,
              progress=None) -> tuple[bool, str, list[str]]:
    """一键构建开跑前检查。

    返回 (能否开始, 给用户的一句话, 需要写进任务日志的提示列表)。
    ffmpeg 是硬门槛（没有它连音轨都转不了），ASR 是软门槛（缺了还能靠字幕建角色）。
    """
    notes: list[str] = []
    path, note = ffmpeg_setup.ensure(allow_download=allow_download, progress=progress)
    if not path:
        return False, f"缺少 ffmpeg，无法处理音轨。{note}。请点「安装 ffmpeg」后重试。", notes
    if note:
        notes.append(f"ffmpeg：{note}")

    asr = _asr_info()
    if not asr["ok"]:
        notes.append("本地 ASR 不可用：本次只用字幕与百科建语料，6 桶可能填不满。")
    else:
        notes.append(f"ASR：{asr['detail']}")
    return True, "环境检查通过", notes


def log_startup_summary() -> None:
    """插件加载时打一份简报，缺件当场 WARNING。"""
    rep = report()
    for item in rep["items"]:
        if item["required"] and not item["ok"]:
            logger.warning("环境自检：%s 缺失（%s）", item["label"], item.get("hint") or item["detail"])
    if rep["ok"]:
        logger.info("环境自检：%s", rep["summary"])
    else:
        logger.warning("环境自检：%s", rep["summary"])
