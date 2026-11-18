"""统一的外网请求层：一个带 308 处理的 opener + 带进度的下载。

为什么要单独一层：
  · 宿主内置的是 Python 3.10，`urllib` 不跟随 308。hf-mirror 会先用 308 跳到自己的
    缓存地址，不处理就整条下载报 "HTTP 308"。
  · 国内直连 huggingface.co / github 大文件经常被挡，需要按镜像顺序回退。
  · 下载要能报进度、能取消，否则用户只能盯着转圈。

插件其余模块（asr_downloads、ffmpeg_setup、wiki_crawler）都走这里。
"""

from __future__ import annotations

import gzip
import os
import shutil
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Iterable

ProgressFn = Callable[[int, int], None]  # (已下载字节, 总字节或 0)


class Redirect308Handler(urllib.request.HTTPRedirectHandler):
    """Python 3.10 的 urllib 不认 308，这里按 302 转一次。"""

    def http_error_308(self, req, fp, code, msg, headers):  # noqa: N802
        return self.http_error_302(req, fp, 302, msg, headers)


OPENER = urllib.request.build_opener(Redirect308Handler)

DEFAULT_UA = "Shinsekai-NanamiPersona/1.2"


def open_url(url: str, *, timeout: float = 60.0, headers: dict[str, str] | None = None):
    """打开一个 url，自动处理 308。调用方负责 close（用 with）。"""
    hdrs = {"User-Agent": DEFAULT_UA, "Accept": "*/*"}
    if headers:
        hdrs.update(headers)
    return OPENER.open(urllib.request.Request(url, headers=hdrs), timeout=timeout)


def first_reachable(urls: Iterable[str], *, timeout: float = 15.0) -> str:
    """按顺序试，返回第一个能拿到响应的 url（只探测，不下载）。"""
    last = ""
    for url in urls:
        try:
            with open_url(url, timeout=timeout) as resp:
                if getattr(resp, "status", 200) < 400:
                    return url
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}: {exc}"
    raise RuntimeError(f"所有镜像都不可用（最后一次：{last}）")


def download(
    urls: str | Iterable[str],
    dest: Path,
    *,
    progress: ProgressFn | None = None,
    timeout: float = 120.0,
    cancelled: Callable[[], bool] | None = None,
    gunzip: bool = False,
) -> Path:
    """按镜像顺序把文件下到 dest。

    urls 可以给多个，前一个失败自动换下一个。gunzip=True 时边下边解压
    （静态 ffmpeg 的 .gz 体积小一半，国内线路差的时候差别很明显）。
    """
    candidates = [urls] if isinstance(urls, str) else list(urls)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    errors: list[str] = []

    for url in candidates:
        try:
            with open_url(url, timeout=timeout) as resp:
                total = int(resp.headers.get("Content-Length") or 0)
                done = 0
                with tmp.open("wb") as raw:
                    stream = gzip.GzipFile(fileobj=resp, mode="rb") if gunzip else resp
                    while True:
                        if cancelled is not None and cancelled():
                            raise _Cancelled()
                        chunk = stream.read(256 * 1024)
                        if not chunk:
                            break
                        raw.write(chunk)
                        done += len(chunk)
                        if progress is not None:
                            progress(done, total)
            if tmp.stat().st_size == 0:
                raise RuntimeError("下到 0 字节")
            tmp.replace(dest)
            return dest
        except _Cancelled:
            tmp.unlink(missing_ok=True)
            raise
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{url.split('/')[2]}: {type(exc).__name__}: {exc}")
            tmp.unlink(missing_ok=True)
    raise RuntimeError("；".join(errors))


class _Cancelled(RuntimeError):
    pass


def which_any(names: Iterable[str]) -> str:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return ""


def chmod_x(path: Path) -> None:
    try:
        os.chmod(path, 0o755)
    except OSError:
        pass
