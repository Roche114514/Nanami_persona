"""全自动角色构建流水线所需本地 ASR 模型下载通道（Faster-Whisper）。

支持双下载通道（国内 HF-Mirror 镜像 / HuggingFace 官方直连），流式落盘到
`<project_root>/data/models/<model_id>`（与 `bili_asr._resolve_asr_environment()`
自动探测路径完全一致），并提供精确到字节的实时进度与半截文件自愈。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import bili_asr

logger = logging.getLogger(__name__)

PARTIAL_SUFFIX = ".nanami-partial"
MIN_MODEL_BIN_BYTES = 10 * 1024 * 1024  # model.bin 至少 10MB，防止错误页面冒充

CHANNELS: dict[str, dict[str, str]] = {
    "mirror": {
        "id": "mirror",
        "label": "国内加速镜像 (hf-mirror.com · 推荐)",
        "base_url": "https://hf-mirror.com",
    },
    "official": {
        "id": "official",
        "label": "HuggingFace 官方 (huggingface.co)",
        "base_url": "https://huggingface.co",
    },
}

MODEL_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "id": "faster-whisper-small",
        "repo": "Systran/faster-whisper-small",
        "label": "Faster-Whisper Small（推荐 · 约 480 MB）",
        "desc": "轻量快速，日语动漫切片与中文语音识别均够用，首次使用首选。",
        "approx_mb": 484,
        "required_files": ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt"),
        "optional_files": ("preprocessor_config.json",),
    },
    {
        "id": "faster-whisper-medium",
        "repo": "Systran/faster-whisper-medium",
        "label": "Faster-Whisper Medium（高精度 · 约 1.5 GB）",
        "desc": "显著提升日文连读、低声台词与嘈杂 BGM 场景的识别准确率。",
        "approx_mb": 1530,
        "required_files": ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt"),
        "optional_files": ("preprocessor_config.json",),
    },
    {
        "id": "faster-whisper-large-v3-turbo",
        "repo": "Systran/faster-whisper-large-v3-turbo",
        "label": "Faster-Whisper Large-v3-Turbo（旗舰极速 · 约 1.6 GB）",
        "desc": "兼顾 large-v3 精度与 8 倍推理速度，适合有独显（4GB+ VRAM）的环境。",
        "approx_mb": 1620,
        "required_files": ("config.json", "model.bin", "tokenizer.json", "vocabulary.json"),
        "optional_files": ("preprocessor_config.json", "vocabulary.txt"),
    },
    {
        "id": "faster-whisper-large-v3",
        "repo": "Systran/faster-whisper-large-v3",
        "label": "Faster-Whisper Large-v3（顶配高精度 · 约 3.0 GB）",
        "desc": "最高多语种识别上限，适合 8GB+ 显存追求极限台词还原。",
        "approx_mb": 3090,
        "required_files": ("config.json", "model.bin", "tokenizer.json", "vocabulary.json"),
        "optional_files": ("preprocessor_config.json", "vocabulary.txt"),
    },
)

CATALOG_BY_ID: dict[str, dict[str, Any]] = {item["id"]: item for item in MODEL_CATALOG}

_LOCK = threading.Lock()
_TASKS: dict[str, "DownloadTask"] = {}


class _DownloadCancelled(Exception):
    pass


@dataclass
class DownloadTask:
    model_id: str
    channel: str
    target_dir: str
    status: str = "running"  # running | done | error | cancelled
    percent: float = 0.0
    done_bytes: int = 0
    total_bytes: int = 0
    rate_bps: float = 0.0
    current_file: str = ""
    message: str = "准备下载…"
    error: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    cancel_event: threading.Event = field(default_factory=threading.Event)


def _models_root() -> Path:
    return bili_asr._project_root() / "data" / "models"


def _human_bytes(num: float) -> str:
    v = float(num or 0.0)
    for unit, scale in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if v >= scale:
            return f"{v / scale:.1f} {unit}" if scale >= 1024 ** 3 else f"{v / scale:.0f} {unit}"
    return f"{v:.0f} B"


def _is_model_dir_ready(model_dir: Path) -> tuple[bool, int]:
    bin_path = model_dir / "model.bin"
    cfg_path = model_dir / "config.json"
    partial_flag = model_dir / f"model.bin{PARTIAL_SUFFIX}"
    if partial_flag.exists() or not bin_path.is_file() or not cfg_path.is_file():
        return False, 0
    try:
        size = bin_path.stat().st_size
    except OSError:
        return False, 0
    return size >= MIN_MODEL_BIN_BYTES, size


def _download_single_file(
    url: str,
    dest: Path,
    task: DownloadTask,
    *,
    timeout: float = 60.0,
    required: bool = True,
) -> bool:
    partial_marker = dest.with_name(dest.name + PARTIAL_SUFFIX)
    tmp_path = dest.with_name(dest.name + ".tmp")
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial_marker.write_text("downloading", encoding="utf-8")

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Shinsekai-NanamiPersona/1.0",
            "Accept": "*/*",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", 200)
            if status >= 400:
                if not required and status == 404:
                    partial_marker.unlink(missing_ok=True)
                    return False
                raise RuntimeError(f"HTTP {status}: {url}")
            content_len = int(resp.headers.get("Content-Length") or 0)
            with _LOCK:
                task.current_file = dest.name
                base_done = task.done_bytes
                if dest.name == "model.bin" and content_len > 0:
                    task.total_bytes = max(task.total_bytes, base_done + content_len)
                elif task.total_bytes == 0 and content_len > 0:
                    task.total_bytes = content_len

            t_last = time.monotonic()
            bytes_since = 0
            written = 0
            with tmp_path.open("wb") as fh:
                while True:
                    if task.cancel_event.is_set():
                        raise _DownloadCancelled("用户取消了下载")
                    chunk = resp.read(256 * 1024)
                    if not chunk:
                        break
                    if task.cancel_event.is_set():
                        raise _DownloadCancelled("用户取消了下载")
                    fh.write(chunk)
                    clen = len(chunk)
                    written += clen
                    bytes_since += clen
                    now = time.monotonic()
                    dt = now - t_last
                    with _LOCK:
                        task.done_bytes = base_done + written
                        if dt >= 0.35:
                            task.rate_bps = bytes_since / dt
                            t_last = now
                            bytes_since = 0
                        if task.total_bytes > 0:
                            task.percent = min(99.5, round(task.done_bytes * 100.0 / task.total_bytes, 1))
                        task.message = (
                            f"正在下载 {dest.name} · {_human_bytes(task.done_bytes)}"
                            + (f" / {_human_bytes(task.total_bytes)}" if task.total_bytes > 0 else "")
                            + (f" ({_human_bytes(task.rate_bps)}/s)" if task.rate_bps > 0 else "")
                        )
        os.replace(tmp_path, dest)
        partial_marker.unlink(missing_ok=True)
        return True
    except urllib.error.HTTPError as exc:
        tmp_path.unlink(missing_ok=True)
        if not required and exc.code == 404:
            partial_marker.unlink(missing_ok=True)
            return False
        raise RuntimeError(f"下载 {dest.name} 失败 (HTTP {exc.code})") from exc
    except Exception:
        tmp_path.unlink(missing_ok=True)
        if not required:
            partial_marker.unlink(missing_ok=True)
            return False
        raise


def _worker_download(model_id: str, channel: str, target_dir: Path, task: DownloadTask) -> None:
    spec = CATALOG_BY_ID[model_id]
    base_url = CHANNELS[channel]["base_url"].rstrip("/")
    repo = spec["repo"]
    try:
        # 清理旧的半截失败文件
        for leftover in target_dir.glob(f"*{PARTIAL_SUFFIX}"):
            real_file = leftover.with_name(leftover.name[: -len(PARTIAL_SUFFIX)])
            real_file.unlink(missing_ok=True)
            leftover.unlink(missing_ok=True)

        for fname in spec["required_files"]:
            url = f"{base_url}/{repo}/resolve/main/{fname}"
            _download_single_file(url, target_dir / fname, task, required=True)
        for fname in spec.get("optional_files") or ():
            url = f"{base_url}/{repo}/resolve/main/{fname}"
            _download_single_file(url, target_dir / fname, task, required=False)

        ready, bin_size = _is_model_dir_ready(target_dir)
        if not ready:
            raise RuntimeError("model.bin 校验未通过，文件体积异常或未完整写入")
        with _LOCK:
            task.status = "done"
            task.percent = 100.0
            task.total_bytes = max(task.total_bytes, task.done_bytes, bin_size)
            task.finished_at = time.time()
            task.message = f"下载完成（model.bin {_human_bytes(bin_size)}）"
    except _DownloadCancelled:
        logger.info("nanami_persona: 用户取消了 ASR 模型下载 (%s)", model_id)
        for leftover in target_dir.glob(f"*{PARTIAL_SUFFIX}"):
            real_file = leftover.with_name(leftover.name[: -len(PARTIAL_SUFFIX)])
            real_file.unlink(missing_ok=True)
            leftover.unlink(missing_ok=True)
        for tmpf in target_dir.glob("*.tmp"):
            tmpf.unlink(missing_ok=True)
        with _LOCK:
            task.status = "cancelled"
            task.error = ""
            task.finished_at = time.time()
            task.message = "已取消下载"
    except Exception as exc:
        logger.warning("nanami_persona: ASR 模型下载失败 (%s / %s): %s", model_id, channel, exc)
        with _LOCK:
            task.status = "error"
            task.error = str(exc) or "下载失败"
            task.finished_at = time.time()
            task.message = f"下载失败：{task.error}"


def start_download(model_id: str, channel: str = "mirror") -> dict[str, Any]:
    """启动后台 ASR 模型下载线程；若同模型已在下载中则直接复用该任务。"""
    mid = str(model_id or "faster-whisper-small").strip()
    if mid not in CATALOG_BY_ID:
        raise ValueError(f"未知模型：{mid}")
    ch = str(channel or "mirror").strip().lower()
    if ch not in CHANNELS:
        ch = "mirror"

    target_dir = _models_root() / mid
    ready, bin_size = _is_model_dir_ready(target_dir)
    if ready:
        return {
            "started": False,
            "already_ready": True,
            "model_id": mid,
            "message": f"{mid} 已就绪（{_human_bytes(bin_size)}），无需重复下载。",
            "snapshot": probe_models(),
        }

    with _LOCK:
        existing = _TASKS.get(mid)
        if existing is not None and existing.status == "running":
            return {
                "started": False,
                "already_running": True,
                "model_id": mid,
                "message": f"{mid} 正在后台下载中（{existing.percent:.0f}%）…",
                "snapshot": probe_models(),
            }
        spec = CATALOG_BY_ID[mid]
        task = DownloadTask(
            model_id=mid,
            channel=ch,
            target_dir=str(target_dir),
            status="running",
            total_bytes=int(spec["approx_mb"]) * 1024 * 1024,
            started_at=time.time(),
            message=f"正在通过 {CHANNELS[ch]['label']} 连接下载…",
        )
        _TASKS[mid] = task

    t = threading.Thread(
        target=_worker_download,
        args=(mid, ch, target_dir, task),
        name=f"nanami-asr-dl-{mid}",
        daemon=True,
    )
    t.start()
    return {
        "started": True,
        "model_id": mid,
        "channel": ch,
        "message": f"已启动下载 {mid}（通道：{CHANNELS[ch]['label']}）",
        "snapshot": probe_models(),
    }


def cancel_download(model_id: str = "") -> dict[str, Any]:
    """取消指定模型（或当前所有运行中）的后台下载任务。"""
    mid = str(model_id or "").strip()
    cancelled: list[str] = []
    with _LOCK:
        for k, task in _TASKS.items():
            if task.status == "running" and (not mid or k == mid):
                task.cancel_event.set()
                task.message = "正在取消下载…"
                cancelled.append(k)
    return {
        "cancelled": cancelled,
        "message": f"已请求取消：{', '.join(cancelled)}" if cancelled else "当前没有正在下载的模型任务。",
        "snapshot": probe_models(),
    }


def _registry_path() -> Path:
    return _models_root() / "local_asr_registry.json"


def _load_local_registry() -> dict[str, list[str]]:
    path = _registry_path()
    out: dict[str, list[str]] = {"whisper_dirs": [], "funasr_dirs": [], "vosk_dirs": []}
    if not path.is_file():
        return out
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            for key in out:
                vals = raw.get(key) or []
                if isinstance(vals, list):
                    out[key] = [str(x).strip() for x in vals if str(x).strip()]
    except Exception:
        pass
    return out


def _save_local_registry(reg: dict[str, list[str]]) -> None:
    path = _registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")


def _detect_local_asr_kind(raw_path: str) -> tuple[str, Path, int]:
    """自动识别本地 ASR 模型路径类型（whisper / funasr / vosk）并返回规范目录与主文件体积。"""
    p = Path(str(raw_path or "").strip().strip('"').strip("'")).expanduser()
    if p.is_file():
        p = p.parent
    if not p.is_dir():
        raise FileNotFoundError(f"找不到本地模型目录：{raw_path}")
    resolved = p.resolve()

    model_bin = resolved / "model.bin"
    if model_bin.is_file() and model_bin.stat().st_size >= MIN_MODEL_BIN_BYTES:
        return "whisper", resolved, model_bin.stat().st_size

    # 支持用户选到 HuggingFace 缓存外层目录（向下找 snapshots/*/model.bin）
    for sub in resolved.glob("snapshots/*/model.bin"):
        if sub.is_file() and sub.stat().st_size >= MIN_MODEL_BIN_BYTES:
            return "whisper", sub.parent.resolve(), sub.stat().st_size

    # FunASR Paraformer / SenseVoice 目录
    if any((resolved / fn).is_file() for fn in ("model.pt", "model.pb", "am.mvn", "configuration.json")):
        total = sum(f.stat().st_size for f in resolved.iterdir() if f.is_file())
        return "funasr", resolved, total

    # Vosk 目录
    if (resolved / "am").is_dir() or (resolved / "conf").is_dir():
        total = sum(f.stat().st_size for f in resolved.rglob("*") if f.is_file())
        return "vosk", resolved, total

    raise ValueError(
        f"目录「{resolved}」中未检测到有效的 Faster-Whisper（model.bin）、FunASR（model.pt/am.mvn）或 Vosk 模型文件。"
    )


def add_local_model(raw_path: str) -> dict[str, Any]:
    """校验并挂载本机已有 ASR 模型目录到 local_asr_registry.json。"""
    kind, model_dir, size_bytes = _detect_local_asr_kind(raw_path)
    reg = _load_local_registry()
    bucket_key = {"whisper": "whisper_dirs", "funasr": "funasr_dirs", "vosk": "vosk_dirs"}[kind]
    dir_str = str(model_dir)
    items = [x for x in reg[bucket_key] if x != dir_str]
    items.insert(0, dir_str)
    reg[bucket_key] = items
    _save_local_registry(reg)
    return {
        "added": True,
        "kind": kind,
        "path": dir_str,
        "message": f"已添加本地 {kind.upper()} 模型：{model_dir.name}（{_human_bytes(size_bytes)}）",
        "snapshot": probe_models(),
    }


def remove_local_model(raw_path: str) -> dict[str, Any]:
    """从已挂载列表中移除本地 ASR 模型路径（不删除磁盘文件）。"""
    target = str(raw_path or "").strip()
    reg = _load_local_registry()
    changed = False
    for key in ("whisper_dirs", "funasr_dirs", "vosk_dirs"):
        before = list(reg[key])
        reg[key] = [x for x in before if x != target]
        if len(reg[key]) != len(before):
            changed = True
    if changed:
        _save_local_registry(reg)
    return {
        "removed": changed,
        "message": "已从挂载列表移除该本地模型（未删除磁盘文件）" if changed else "未找到该本地模型记录",
        "snapshot": probe_models(),
    }


def probe_models() -> dict[str, Any]:
    """扫描本地 ASR 模型目录与后台下载任务表，返回前端展示快照。"""
    root = _models_root()
    env_info = bili_asr._resolve_asr_environment()
    items: list[dict[str, Any]] = []
    any_running = False
    with _LOCK:
        tasks_copy = {
            k: DownloadTask(
                model_id=v.model_id,
                channel=v.channel,
                target_dir=v.target_dir,
                status=v.status,
                percent=v.percent,
                done_bytes=v.done_bytes,
                total_bytes=v.total_bytes,
                rate_bps=v.rate_bps,
                current_file=v.current_file,
                message=v.message,
                error=v.error,
                started_at=v.started_at,
                finished_at=v.finished_at,
            )
            for k, v in _TASKS.items()
        }

    for spec in MODEL_CATALOG:
        mid = spec["id"]
        mdir = root / mid
        ready, bin_size = _is_model_dir_ready(mdir)
        task = tasks_copy.get(mid)
        status = "ready" if ready else "missing"
        percent = 100.0 if ready else 0.0
        message = f"已就绪（{_human_bytes(bin_size)}）" if ready else f"未下载（约 {spec['approx_mb']} MB）"
        error = ""
        rate_text = ""
        if task is not None:
            if task.status == "running":
                status = "running"
                any_running = True
                percent = float(task.percent)
                message = task.message
                if task.rate_bps > 0:
                    rate_text = f"{_human_bytes(task.rate_bps)}/s"
            elif task.status == "error" and not ready:
                status = "error"
                error = task.error
                message = task.message
            elif task.status == "cancelled" and not ready:
                status = "cancelled"
                message = task.message
        items.append({
            "id": mid,
            "repo": spec["repo"],
            "label": spec["label"],
            "desc": spec["desc"],
            "approx_mb": spec["approx_mb"],
            "target_dir": str(mdir),
            "ready": ready,
            "size_bytes": bin_size,
            "status": status,
            "percent": round(percent, 1),
            "rate_text": rate_text,
            "message": message,
            "error": error,
        })

    reg = _load_local_registry()
    local_models: list[dict[str, Any]] = []
    for kind, key in (("whisper", "whisper_dirs"), ("funasr", "funasr_dirs"), ("vosk", "vosk_dirs")):
        for d in reg.get(key) or []:
            dp = Path(d)
            local_models.append({
                "kind": kind,
                "name": dp.name,
                "path": d,
                "exists": dp.is_dir(),
            })

    return {
        "models_root": str(root),
        "any_running": any_running,
        "channels": [{"id": v["id"], "label": v["label"]} for v in CHANNELS.values()],
        "models": items,
        "local_models": local_models,
        "env": {
            "whisper_ready": bool(env_info.get("whisper_model_dir")) or any(it["ready"] for it in items),
            "whisper_dir": str(env_info.get("whisper_model_dir") or ""),
            "funasr_ready": bool(env_info.get("funasr_models")),
            "vosk_ready": bool(env_info.get("vosk_model_path")),
            "python_ready": bool(env_info.get("gpt_python") or env_info.get("current_python")),
        },
    }
