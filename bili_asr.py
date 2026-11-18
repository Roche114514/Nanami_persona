"""B 站角色语音/剧情视频检索、字幕直取、DASH 音轨高速下载与本地 ASR 转写。

核心流程：
  1. `search_character_bili_videos()`：按「语音合集 → 台词语音 → 个人剧情/名场面」优先级检索 B 站视频并过滤无关二创；
  2. `fetch_bili_subtitle_if_any()`：优先检查 B 站播放器字幕轨（CC/AI 字幕），命中即秒级返回带时间戳句子；
  3. `download_bili_audio()`：无字幕时走 DASH 纯音频流多节点测速下载 + ffmpeg 转码；
  4. `transcribe_audio_local()`：自动探测本机 `GPT-SoVITS` / `Shinsekai` 环境中的 FunASR / Faster-Whisper / Vosk，
     注意清理 `PYTHONHOME`/`PYTHONPATH` 并禁用 `PYTHONUTF8=1`（防止含中文路径的 GBK `.pth` 触发编码异常），
     将音频转写为带 `[MM:SS.s - MM:SS.s]` 时间戳的分句列表供 LLM 角色鉴真筛选。
"""

from __future__ import annotations

import atexit
import html
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import time
from typing import Any, Callable, Sequence

import requests
import yaml

logger = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
_BILI_SESSION: requests.Session | None = None

_ALLOWED_ANIME_TYPENAMES = frozenset({
    "动画综合", "番剧", "连载动画", "完结动画", "官方延伸",
})

_HARD_NOISE_TITLE_RE = re.compile(
    r"(?:(?<![A-Za-z])(?:MMD|MAD|AMV|PV|CM|OP|ED)(?![A-Za-z])|\[AI\]|手书|鬼畜|调教|人力VOCALOID|"
    r"AI翻唱|AI\s*翻唱|AI配音|抽象配音|同人配音|配音练习|Cosplay|cosplay|描改|填词|同人|猫meme|"
    r"攻略|配装|抽卡|测评|教程|配队|强度|实战|OP纯享|舞蹈|宅舞|翻唱|片头曲|片尾曲|主题曲|插曲|插入曲|片中曲|印象曲|角色歌|专辑曲|中日歌词|歌词|试听|Hi-res|整活|快板|ダンス|摇沙锤|混剪|踩点|纯享|燃向|卡拉OK|"
    r"手办|粘土人|景品|开箱|简评|动态头像|手机主题|壁纸|表情包|绿幕|补帧|手绘|绘画|板绘|建模|三渲二|桌宠|插件|"
    r"ASMR|音声|来电|哄睡|助眠|睡吧|耳舐|入眠|伴你入眠|催眠|语气声|气声|喘气|呼吸声|叹气|朗诵|点击语音|全点击|骂人|洗脑|循环|一分钟|1分钟|空耳|"
    r"试吃|同款|圣地巡礼|巡礼|实拍|手作|还原|挑战|reaction|盘点|解说|吐槽|漫评|杂谈|一口气看完|几分钟看完|全集解说|完结撒花|推荐|安利|解读|分析|"
    r"预告|先行|宣传片|第三季|第四季|字幕版|中配|中文配音|"
    r"生肉|广播|采访|活动|领奖|颜值变化|写真|大河剧|木村爱|真人|电影|日剧|电视剧|预告片|联动CM|广告|"
    r"漫画|推特短篇|"
    r"声优|声优还配过|声优都是怪物|声优盘点|声优梗|配音对比|声优表)",
    re.IGNORECASE,
)
_COMPILATION_TITLE_RE = re.compile(
    r"(?:全语音|语音合集|台词合集|说话合集|语录合集|名场面合集|全台词|语音分享)",
    re.IGNORECASE,
)
_SOFT_NOISE_TITLE_RE = re.compile(r"(?:二创|同人曲|原声集|OST|BGM|专辑)", re.IGNORECASE)
_PREFER_TITLE_RE = re.compile(
    r"(?:语音|台词|全语音|个人剧情|剧情|名场面|角色故事|羁绊|互动|对话|片段|切片|语录|瞬间|合集|原片|cut)",
    re.IGNORECASE,
)
_ANIME_CLIP_TITLE_RE = re.compile(
    r"(?:片段|切片|名场面|合集|瞬间|第\s*[0-9一二三四五六七八九十百]+\s*[集话期]|cut|原片|未删减|台词|语音|告白|日常)",
    re.IGNORECASE,
)
_NOISE_TYPENAME_RE = re.compile(
    r"(?:鬼畜|音MAD|人力VOCALOID|明星|影视剪辑|影视杂谈|动漫杂谈|预告·资讯|模玩|周边|宅舞|舞蹈|翻唱|演奏|VOCALOID|音乐综合|MAD·AMV|短片·手书|娱乐粉丝创作|MV|美食|动物|日常|搞笑)",
    re.IGNORECASE,
)
_HARD_NOISE_TAG_RE = re.compile(
    r"(?:^|,)\s*(?:MAD|AMV|MMD|音MAD|鬼畜|鬼畜调教|手书|同人|猫memes?|手办|模玩|周边|试吃|美食|开箱|测评|"
    r"cosplay|三次元|真人|日剧|电影|明星|翻唱|宅舞|舞蹈|演奏|VOCALOID|虚拟UP主|VTuber|游戏实况|"
    r"解说|漫评|杂谈|吐槽|盘点|三渲二|建模|绘画|板绘|手绘|壁纸|表情包)\s*(?:,|$)",
    re.IGNORECASE,
)


def _ffmpeg_exe() -> str:
    """找 ffmpeg 可执行文件（解析统一交给 ffmpeg_setup）。

    宿主 2.6.0 的 Mac 包里没有系统 ffmpeg，PATH 里也查不到，原来直接调 "ffmpeg"
    会 FileNotFoundError，B 站音轨下载整段作废且没有可见提示。
    """
    try:
        from . import ffmpeg_setup

        found = ffmpeg_setup.resolve()
        if found:
            return found
    except Exception:  # noqa: BLE001
        pass
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        return "ffmpeg"


def _no_window() -> dict[str, Any]:
    if os.name != "nt":
        return {}
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def get_bili_session(*, refresh: bool = False) -> requests.Session:
    global _BILI_SESSION
    if _BILI_SESSION is not None and not refresh:
        return _BILI_SESSION
    s = requests.Session()
    s.trust_env = False
    s.headers.update({"User-Agent": UA, "Referer": "https://www.bilibili.com/"})
    try:
        spi = s.get("https://api.bilibili.com/x/frontend/finger/spi", timeout=(3, 5))
        data = (spi.json().get("data") or {}) if spi.ok else {}
        if data.get("b_3"):
            s.cookies.set("buvid3", str(data["b_3"]).strip(), domain=".bilibili.com")
        if data.get("b_4"):
            s.cookies.set("buvid4", str(data["b_4"]).strip(), domain=".bilibili.com")
    except Exception:
        try:
            s.get("https://www.bilibili.com/", timeout=(4, 8))
        except Exception:
            pass
    _BILI_SESSION = s
    return s


def bili_search(query: str, limit: int = 8, *, page: int = 1) -> list[dict[str, str]]:
    """关键词搜 B 站视频，返回 [{'title': ..., 'bvid': ..., 'url': ...}]"""
    s = get_bili_session()
    params = {"search_type": "video", "keyword": query, "page": max(1, int(page))}
    r = s.get("https://api.bilibili.com/x/web-interface/search/type", params=params, timeout=(4, 10))
    if r.status_code in (403, 412):
        s = get_bili_session(refresh=True)
        r = s.get("https://api.bilibili.com/x/web-interface/search/type", params=params, timeout=(4, 10))
    r.raise_for_status()
    rows = ((r.json().get("data") or {}).get("result")) or []
    out = []
    for row in rows:
        bvid = str(row.get("bvid") or "").strip()
        if not bvid:
            continue
        title = html.unescape(re.sub(r"<[^>]+>", "", str(row.get("title") or ""))).strip() or bvid
        duration = str(row.get("duration") or "").strip()
        typename = str(row.get("typename") or "").strip()
        tag = html.unescape(re.sub(r"<[^>]+>", "", str(row.get("tag") or ""))).strip()
        out.append({
            "title": title,
            "bvid": bvid,
            "url": f"https://www.bilibili.com/video/{bvid}",
            "duration": duration,
            "typename": typename,
            "tag": tag,
        })
    return out[:limit]


def _bili_ids(session: requests.Session, bvid: str, page_url: str) -> tuple[str, str, str]:
    try:
        view = session.get("https://api.bilibili.com/x/web-interface/view", params={"bvid": bvid}, timeout=(4, 8))
        payload = view.json() if view.ok else {}
        data = payload.get("data") or {}
        if payload.get("code") == 0 and data.get("aid") and data.get("cid"):
            return str(data["aid"]), str(data["cid"]), str(data.get("title") or "").strip()
    except Exception:
        pass
    page = session.get(page_url, timeout=(5, 15))
    page.raise_for_status()
    m = re.search(r'"aid":(\d+).*?"bvid":"(BV[^"]+)".*?"cid":(\d+)', page.text, re.S)
    if not m:
        raise RuntimeError(f"视频页里找不到 aid/cid：{page_url}")
    return m.group(1), m.group(3), ""


def _bili_pages(session: requests.Session, bvid: str, page_url: str) -> tuple[str, str, list[dict[str, Any]]]:
    """获取 B 站视频 aid、标题及全部分 P 列表（支持多 P 切片合集）。"""
    try:
        view = session.get("https://api.bilibili.com/x/web-interface/view", params={"bvid": bvid}, timeout=(4, 8))
        payload = view.json() if view.ok else {}
        data = payload.get("data") or {}
        if payload.get("code") == 0 and data.get("aid"):
            aid = str(data["aid"])
            title = str(data.get("title") or "").strip()
            raw_pages = data.get("pages") or []
            pages: list[dict[str, Any]] = []
            for item in raw_pages:
                if not isinstance(item, dict) or not item.get("cid"):
                    continue
                pages.append({
                    "cid": str(item["cid"]),
                    "page": int(item.get("page") or len(pages) + 1),
                    "part": str(item.get("part") or "").strip(),
                    "duration": int(item.get("duration") or 0),
                })
            if not pages and data.get("cid"):
                pages = [{"cid": str(data["cid"]), "page": 1, "part": "", "duration": int(data.get("duration") or 0)}]
            if pages:
                return aid, title, pages
    except Exception:
        pass
    aid, cid, title = _bili_ids(session, bvid, page_url)
    return aid, title, [{"cid": cid, "page": 1, "part": "", "duration": 0}]


def _rank_by_speed(session: requests.Session, candidates: list[tuple[int, str]], page_url: str) -> list[tuple[float, str]]:
    targets: list[tuple[str, str]] = []
    seen: set[str] = set()
    for _bw, url in candidates:
        host = url.split("/")[2] if "//" in url else url[:24]
        if host not in seen:
            seen.add(host)
            targets.append((host, url))
            if len(targets) >= 4:
                break
    cookies = session.cookies.get_dict()

    def probe(item: tuple[str, str]) -> tuple[float, str] | None:
        _host, stream = item
        t0 = time.time()
        try:
            with requests.Session() as ps:
                ps.trust_env = False
                ps.cookies.update(cookies)
                resp = ps.get(
                    stream,
                    headers={"Referer": page_url, "User-Agent": UA, "Range": "bytes=0-131071"},
                    timeout=(3, 5),
                )
            body = resp.content or b""
            if len(body) < 8192:
                return None
            return len(body) / max(0.001, time.time() - t0), stream
        except Exception:
            return None

    probed = []
    if targets:
        with ThreadPoolExecutor(max_workers=len(targets)) as pool:
            probed = [x for x in pool.map(probe, targets) if x is not None]
    probed.sort(key=lambda pair: -pair[0])
    chosen = {u for _s, u in probed}
    return probed + [(0.0, u) for _bw, u in candidates if u not in chosen]


def _download_stream(
    session: requests.Session,
    stream: str,
    target: Path,
    page_url: str,
    *,
    max_bytes: int = 24 * 1024 * 1024,
) -> int:
    """分段 Range 请求流式下载 B 站 DASH 音轨（规避无 Range 整包请求触发 403/567）。"""
    chunk_span = 2 * 1024 * 1024  # 每次拉 2 MB
    written = 0
    total_size: int | None = None
    started = time.time()

    with target.open("wb") as f:
        while written < max_bytes:
            if total_size is not None and written >= total_size:
                break
            end_byte = written + chunk_span - 1
            if total_size is not None:
                end_byte = min(end_byte, total_size - 1)
            headers = {
                "Referer": page_url,
                "User-Agent": UA,
                "Origin": "https://www.bilibili.com",
                "Range": f"bytes={written}-{end_byte}",
            }
            with session.get(stream, headers=headers, stream=True, timeout=(5, 30)) as resp:
                if resp.status_code == 416 and written > 0:
                    break
                resp.raise_for_status()
                cr = str(resp.headers.get("Content-Range") or "")
                m_cr = re.search(r"/(\d+)", cr)
                if m_cr:
                    total_size = int(m_cr.group(1))

                part_bytes = 0
                for chunk in resp.iter_content(262144):
                    if not chunk:
                        continue
                    f.write(chunk)
                    part_bytes += len(chunk)
                    written += len(chunk)
                    elapsed = time.time() - started
                    if elapsed > 240.0 or (elapsed > 15.0 and written / elapsed < 20 * 1024):
                        raise RuntimeError("节点过慢或超时")
                if resp.status_code == 200 or part_bytes == 0:
                    break
    return written


def _resolve_bvid(session: requests.Session, bvid_or_url: str) -> str:
    m = re.search(r"(BV[A-Za-z0-9]+)", bvid_or_url, re.I)
    if m:
        return m.group(1)
    resolved = session.get(bvid_or_url.strip(), timeout=20, allow_redirects=True)
    m = re.search(r"(BV[A-Za-z0-9]+)", resolved.url + " " + resolved.text[:2000])
    if not m:
        raise RuntimeError(f"无法解析 BV 号：{bvid_or_url}")
    return m.group(1)


def _download_cid_to_wav(
    session: requests.Session,
    aid: str,
    cid: str,
    page_url: str,
    m4s_path: Path,
    wav_path: Path,
    ffmpeg_exe: str,
    *,
    max_duration_sec: int = 0,
    mono_16k: bool = False,
) -> Path:
    play = session.get(
        f"https://api.bilibili.com/x/player/playurl?fnval=16&fnver=0&fourk=1&avid={aid}&cid={cid}&qn=80",
        timeout=20,
    ).json()
    audio_tracks = ((play.get("data") or {}).get("dash") or {}).get("audio") or []
    if not audio_tracks:
        raise RuntimeError(f"playurl 未返回音频轨：{play.get('code')} {play.get('message')}")

    candidates: list[tuple[int, str]] = []
    for track in sorted(audio_tracks, key=lambda t: -(t.get("bandwidth") or 0)):
        bw = track.get("bandwidth") or 0
        for k in ("baseUrl", "base_url"):
            if track.get(k):
                candidates.append((bw, track[k]))
        for extra in track.get("backupUrl") or track.get("backup_url") or []:
            if extra:
                candidates.append((bw, extra))

    ranked = _rank_by_speed(session, candidates, page_url)
    dl_max_bytes = (
        min(24 * 1024 * 1024, max(12 * 1024 * 1024, int(max_duration_sec * 48000)))
        if max_duration_sec and max_duration_sec > 0
        else 24 * 1024 * 1024
    )

    last_err = ""
    for _speed, stream_url in ranked:
        try:
            size = _download_stream(session, stream_url, m4s_path, page_url, max_bytes=dl_max_bytes)
            if size < 20000:
                raise RuntimeError(f"音频流过小: {size} bytes")
            passed = str(ffmpeg_exe or "").strip()
            exe = (shutil.which(passed) or passed) if passed and passed != "ffmpeg" else _ffmpeg_exe()
            if not Path(exe).is_file() and not shutil.which(exe):
                exe = _ffmpeg_exe()
            cmd = [exe, "-y", "-i", str(m4s_path), "-vn"]
            if max_duration_sec and max_duration_sec > 0:
                cmd.extend(["-t", str(int(max_duration_sec))])
            if mono_16k:
                cmd.extend(["-ac", "1", "-ar", "16000"])
            else:
                cmd.extend(["-ac", "2", "-ar", "44100"])
            cmd.append(str(wav_path))
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                **_no_window(),
            )
            if res.returncode != 0 or not wav_path.is_file():
                raise RuntimeError(f"ffmpeg 转码失败: {(res.stderr or '')[-300:]}")
            return wav_path
        except Exception as exc:
            last_err = f"{type(exc).__name__}: {exc}"
            continue
    raise RuntimeError(f"所有 CDN 节点均下载失败：{last_err}")


def _concat_wav_files(part_wavs: Sequence[Path], dest_wav: Path) -> Path:
    """使用标准库 wave 将多 P 切片音频无缝拼接为单个 WAV 文件。"""
    valid = [p for p in part_wavs if p.is_file()]
    if not valid:
        raise RuntimeError("未产生可拼接的分 P WAV 文件")
    if len(valid) == 1:
        if valid[0].resolve() != dest_wav.resolve():
            shutil.copyfile(valid[0], dest_wav)
        return dest_wav
    with wave.open(str(valid[0]), "rb") as first:
        params = first.getparams()
    with wave.open(str(dest_wav), "wb") as out_wf:
        out_wf.setparams(params)
        for p in valid:
            with wave.open(str(p), "rb") as in_wf:
                if (
                    in_wf.getnchannels() == params.nchannels
                    and in_wf.getsampwidth() == params.sampwidth
                    and in_wf.getframerate() == params.framerate
                ):
                    out_wf.writeframes(in_wf.readframes(in_wf.getnframes()))
    return dest_wav


def download_bili_audio(
    bvid_or_url: str,
    out_dir: str | Path,
    ffmpeg_exe: str = "ffmpeg",
    *,
    max_duration_sec: int = 0,
    mono_16k: bool = False,
) -> Path:
    """输入 BV 号或 B 站视频链接，下载最高码率音轨（多 P 合集按时长拼接前数个分 P）并转为 out_dir/source.wav"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    session = get_bili_session()

    bvid = _resolve_bvid(session, bvid_or_url)
    page_url = f"https://www.bilibili.com/video/{bvid}/"
    aid, _title, pages = _bili_pages(session, bvid, page_url)

    wav_path = out_dir / "source.wav"
    if len(pages) > 1 and max_duration_sec and max_duration_sec > 0:
        rem_sec = int(max_duration_sec)
        part_wavs: list[Path] = []
        for idx, page_info in enumerate(pages[:5], start=1):
            if rem_sec <= 5:
                break
            p_cid = str(page_info.get("cid") or "").strip()
            p_dur = int(page_info.get("duration") or 0)
            part_cap = min(rem_sec, p_dur) if p_dur > 0 else rem_sec
            try:
                pwav = _download_cid_to_wav(
                    session,
                    aid,
                    p_cid,
                    page_url,
                    out_dir / f"part_{idx}.m4s",
                    out_dir / f"part_{idx}.wav",
                    ffmpeg_exe,
                    max_duration_sec=part_cap,
                    mono_16k=mono_16k,
                )
                part_wavs.append(pwav)
                used_sec = p_dur if (0 < p_dur <= rem_sec) else part_cap
                rem_sec -= max(1, used_sec)
            except Exception as exc:
                logger.debug("nanami_persona: 多P分片跳过 (%s P%d): %s", bvid, idx, exc)
                continue
        if part_wavs:
            return _concat_wav_files(part_wavs, wav_path)

    cid = str(pages[0]["cid"])
    return _download_cid_to_wav(
        session,
        aid,
        cid,
        page_url,
        out_dir / "source.m4s",
        wav_path,
        ffmpeg_exe,
        max_duration_sec=max_duration_sec,
        mono_16k=mono_16k,
    )


def _parse_duration_sec(dur_text: str) -> int:
    parts = [p for p in str(dur_text or "").strip().split(":") if p.isdigit()]
    if not parts:
        return 0
    total = 0
    for p in parts:
        total = total * 60 + int(p)
    return total


_WORK_SUB_ALIASES: dict[str, tuple[str, ...]] = {
    "bang dream": ("mygo", "ave mujica", "颂乐人偶", "迷子", "邦邦", "少女乐团派对"),
    "fate": ("fgo", "stay night", "zero", "ubw", "hf", "冠位指定"),
    "lovelive": ("缪斯", "水团", "虹咲", "星团", "莲之空"),
}


def _work_identifiers(work_name: str) -> list[str]:
    w = str(work_name or "").strip()
    if not w:
        return []
    w_low = w.lower()
    ids = [w_low]
    clean_w = re.sub(r"[\s《》「」『』·・:：\-—!！?？]+", "", w_low)
    if clean_w and clean_w not in ids:
        ids.append(clean_w)
    if len(clean_w) >= 4:
        head4 = clean_w[:4]
        if head4 not in ids:
            ids.append(head4)
    for key, extras in _WORK_SUB_ALIASES.items():
        if key in w_low or key.replace(" ", "") in clean_w:
            for ex in extras:
                if ex not in ids:
                    ids.append(ex)
    return ids


def _title_matches_character(
    title: str,
    character_name: str,
    work_name: str = "",
    aliases: Sequence[str] = (),
    tag: str = "",
) -> bool:
    t_low = str(title or "").lower()
    blob = f"{title} {tag}".lower()
    cname = str(character_name or "").strip()

    # 若指定了作品名，必须命中作品标识（或二游/角色全语音专属标题），隔离同名三次元演员与其他作品同名角色
    work_ids = _work_identifiers(work_name)
    is_dedicated_voice_pack = bool(re.search(r"(?:全语音|语音合集|角色语音)", title))
    if work_ids:
        work_hit = any(wid in blob for wid in work_ids)
        if not work_hit and not is_dedicated_voice_pack:
            return False

    names = [cname, *[a for a in aliases if a]]
    for n in names:
        clean = str(n or "").strip()
        if clean and clean.lower() in blob:
            return True

    # 满足作品标识前提下，允许标题出现角色姓或名（长度>=2，如动画片段常简称「山田」），或明确的作品名场面/高甜片段合集
    if work_ids and any(wid in blob for wid in work_ids):
        if len(cname) >= 3:
            for part in (cname[:2], cname[2:], cname[-2:]):
                if len(part) >= 2 and part.lower() in t_low:
                    return True
        if re.search(r"(?:名场面|高甜|瞬间|片段|台词|语音).*合集|合集.*(?:名场面|高甜|瞬间|片段|台词|语音)", t_low):
            return True
    return False


def search_character_bili_videos(
    character_name: str,
    work_name: str = "",
    *,
    aliases: Sequence[str] = (),
    limit: int = 6,
    page: int = 1,
    extra_queries: Sequence[str] = (),
) -> list[dict[str, str]]:
    """严格限定仅从「动画原片切片/名场面/全语音合集」检索并过滤所有非动画、MAD、插曲、试吃、预告与二创视频。"""
    cname = str(character_name or "").strip()
    wname = str(work_name or "").strip()
    if not cname:
        return []

    queries: list[str] = [str(q).strip() for q in extra_queries if str(q).strip()]
    if not queries:
        if wname:
            queries.extend([
                f"{cname} 全语音 说话合集",
                f"{cname} 动画 片段",
                f"{cname} 台词 语音",
                f"{cname} {wname} 语音 台词",
                f"{wname} {cname} 片段",
            ])
            if len(cname) >= 3:
                queries.extend([
                    f"{wname} {cname[:2]} 片段",
                    f"{wname} {cname[:2]} 名场面",
                ])
            queries.append(f"{wname} {cname}")
        else:
            queries.extend([
                f"{cname} 全语音 说话合集",
                f"{cname} 语音合集",
                f"{cname} 动画 片段",
                f"{cname} 名场面",
                f"{cname} 台词 语音",
            ])
    queries = list(dict.fromkeys(queries))

    def _run_query(q: str) -> list[dict[str, str]]:
        try:
            if page > 1:
                return bili_search(q, limit=10, page=page)
            return bili_search(q, limit=10)
        except Exception as exc:
            logger.debug("nanami_persona: B站搜索跳过 (%s): %s", q, exc)
            return []

    if len(queries) > 1:
        with ThreadPoolExecutor(max_workers=min(4, len(queries))) as pool:
            batch_results = list(pool.map(_run_query, queries))
    else:
        batch_results = [_run_query(q) for q in queries]

    seen_bv: set[str] = set()
    compilations: list[dict[str, str]] = []
    preferred: list[dict[str, str]] = []
    normal: list[dict[str, str]] = []

    for rows in batch_results:
        for row in rows:
            bv = row.get("bvid", "")
            title = row.get("title", "")
            typename = row.get("typename", "")
            tag = row.get("tag", "")
            if not bv or bv in seen_bv:
                continue
            # 强校验：作品归属（如有）+ 标题/标签命中角色名/别名，隔离三次元同名艺人及他作角色
            if not _title_matches_character(title, cname, work_name=wname, aliases=aliases, tag=tag):
                continue
            # 分区白名单严格拦截：非动画/番剧分区（如日常/搞笑/美食/杂谈/影视/明星/MAD）一律拒收
            if typename:
                if _NOISE_TYPENAME_RE.search(typename):
                    continue
                if typename not in _ALLOWED_ANIME_TYPENAMES and not _COMPILATION_TITLE_RE.search(title):
                    continue
            # 标签硬拦截：含 MAD/AMV/MMD/手书/同人/试吃/开箱/真人/解说 等直接丢弃
            if tag and _HARD_NOISE_TAG_RE.search(tag):
                continue
            # 严格拦截试吃/预告/解说/ASMR/来电/骂人/插曲/电影/真人/手办/声优采访等非原片切片标题
            if _HARD_NOISE_TITLE_RE.search(title):
                continue
            if _SOFT_NOISE_TITLE_RE.search(title) and not _PREFER_TITLE_RE.search(title):
                continue
            # 跳过超过 60 分钟的超长资源（下载时仍会按切片截断）
            dur_s = _parse_duration_sec(row.get("duration", ""))
            if dur_s > 3600:
                continue
            seen_bv.add(bv)
            if _COMPILATION_TITLE_RE.search(title):
                compilations.append(row)
            elif _PREFER_TITLE_RE.search(title) or _ANIME_CLIP_TITLE_RE.search(title):
                preferred.append(row)
            else:
                normal.append(row)

    combined = compilations + preferred + normal
    return combined[:limit]


def fetch_bili_subtitle_if_any(
    bvid_or_url: str,
    session: requests.Session | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """尝试直取 B 站视频自带 CC / AI 字幕轨，返回 (视频标题, 带时间戳分句列表)。"""
    sess = session or get_bili_session()
    bvid = _resolve_bvid(sess, bvid_or_url)
    page_url = f"https://www.bilibili.com/video/{bvid}/"
    aid, cid, title = _bili_ids(sess, bvid, page_url)

    subtitles: list[dict[str, Any]] = []
    for api_url in (
        "https://api.bilibili.com/x/player/v2",
        "https://api.bilibili.com/x/player/wbi/v2",
    ):
        try:
            r = sess.get(api_url, params={"aid": aid, "cid": cid}, timeout=(4, 8))
            if not r.ok:
                continue
            data = (r.json() or {}).get("data") or {}
            subs = ((data.get("subtitle") or {}).get("subtitles")) or []
            if subs:
                subtitles = subs
                break
        except Exception:
            continue

    if not subtitles:
        return title, []

    def _sub_rank(item: dict[str, Any]) -> int:
        lan = str(item.get("lan") or "").lower()
        if lan in ("zh-cn", "zh-hans"):
            return 0
        if "zh" in lan:
            return 1
        if "ai-zh" in lan:
            return 2
        if "ja" in lan or "jp" in lan:
            return 3
        return 4

    picked = sorted(subtitles, key=_sub_rank)[0]
    sub_url = str(picked.get("subtitle_url") or "").strip()
    if not sub_url:
        return title, []
    if sub_url.startswith("//"):
        sub_url = "https:" + sub_url

    try:
        sr = sess.get(sub_url, timeout=(4, 10))
        sr.raise_for_status()
        body = (sr.json() or {}).get("body") or []
    except Exception:
        return title, []

    segments: list[dict[str, Any]] = []
    for row in body:
        txt = str(row.get("content") or "").strip()
        if not txt:
            continue
        try:
            st = float(row.get("from", 0.0) or 0.0)
            ed = float(row.get("to", st) or st)
        except (TypeError, ValueError):
            st, ed = 0.0, 0.0
        segments.append({"start": round(st, 2), "end": round(ed, 2), "text": txt})
    return title, segments


def _load_api_yaml() -> dict[str, Any]:
    api_path = _project_root() / "data" / "config" / "api.yaml"
    if not api_path.is_file():
        return {}
    try:
        return yaml.safe_load(api_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


_WHISPER_PKG_CACHE: dict[str, bool] = {}


def _bundle_roots() -> list[Path]:
    """api.yaml 没写 gpt_sovits_api_path 时，扫宿主自己的整合包安装位置。

    宿主把整合包装在 <项目>/data/tts_bundles/installed/<包键>/<解压根>/，
    只看 api_v2.py（GPT-SoVITS）和 start.py（Genie），避免扫到无关目录。
    """
    base = _project_root() / "data" / "tts_bundles" / "installed"
    out: list[Path] = []
    try:
        for key_dir in sorted(base.iterdir()):
            if not key_dir.is_dir():
                continue
            for root in sorted(key_dir.iterdir()):
                if not root.is_dir():
                    continue
                if (root / "api_v2.py").is_file() or (root / "start.py").is_file():
                    out.append(root)
    except OSError:
        pass
    return out


def _has_faster_whisper(python_exe: str) -> bool:
    """解释器里到底有没有 faster_whisper（整合包自带 1.1.1；自建环境可能缺）。"""
    if not python_exe:
        return False
    cached = _WHISPER_PKG_CACHE.get(python_exe)
    if cached is not None:
        return cached
    ok = False
    try:
        done = subprocess.run(
            [python_exe, "-c", "import faster_whisper; print('ok')"],
            capture_output=True, text=True, timeout=180, **_no_window(),
        )
        ok = "ok" in (done.stdout or "")
    except Exception:  # noqa: BLE001
        ok = False
    _WHISPER_PKG_CACHE[python_exe] = ok
    return ok


def _resolve_asr_environment() -> dict[str, Any]:
    """探测本机可用的本地 ASR 运行环境（GPT-SoVITS FunASR/FasterWhisper → Shinsekai → Vosk）。"""
    api_cfg = _load_api_yaml()
    candidates_roots: list[Path] = []
    gpt_path = str(api_cfg.get("gpt_sovits_api_path") or "").strip()
    if gpt_path:
        candidates_roots.append(Path(gpt_path))
    env_gpt = str(os.environ.get("GPT_SOVITS_ROOT") or "").strip()
    if env_gpt:
        candidates_roots.append(Path(env_gpt))
    # api.yaml 常是空的（用户没在设置里保存过），这里再扫一遍整合包目录，
    # 否则明明装好了整合包也会被判成「没有 ASR」。
    for root in _bundle_roots():
        if root not in candidates_roots:
            candidates_roots.append(root)

    # 查找内置已下载的 FunASR 模型目录
    funasr_models: dict[str, str] = {}
    gpt_python = ""
    for root in candidates_roots:
        if not root.is_dir():
            continue
        py_cand = root / "runtime" / ("python.exe" if os.name == "nt" else "python")
        if py_cand.is_file() and not gpt_python:
            gpt_python = str(py_cand)
        mdir = root / "tools" / "asr" / "models"
        if mdir.is_dir():
            asr_dir = mdir / "speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch"
            vad_dir = mdir / "speech_fsmn_vad_zh-cn-16k-common-pytorch"
            punc_dir = mdir / "punc_ct-transformer_zh-cn-common-vocab272727-pytorch"
            if asr_dir.is_dir() and not funasr_models:
                funasr_models = {
                    "asr": str(asr_dir),
                    "vad": str(vad_dir) if vad_dir.is_dir() else "",
                    "punc": str(punc_dir) if punc_dir.is_dir() else "",
                    "cwd": str(root),
                }

    # 读取前端登记的自定义本地 ASR 模型路径（data/models/local_asr_registry.json）
    reg_whisper: list[str] = []
    reg_funasr: list[str] = []
    reg_vosk: list[str] = []
    reg_path = _project_root() / "data" / "models" / "local_asr_registry.json"
    if reg_path.is_file():
        try:
            reg_data = json.loads(reg_path.read_text(encoding="utf-8"))
            if isinstance(reg_data, dict):
                reg_whisper = [str(x) for x in (reg_data.get("whisper_dirs") or []) if x]
                reg_funasr = [str(x) for x in (reg_data.get("funasr_dirs") or []) if x]
                reg_vosk = [str(x) for x in (reg_data.get("vosk_dirs") or []) if x]
        except Exception:
            pass

    # 查找缓存的 Faster-Whisper 本地目录（优先前端手动挂载的本地路径，其次项目内 data/models，再其次 GPT-SoVITS 目录）
    whisper_model_dir = ""
    for w_cand in reg_whisper:
        wp = Path(w_cand)
        if wp.is_dir() and (wp / "model.bin").is_file():
            whisper_model_dir = str(wp)
            break
    if not whisper_model_dir:
        whisper_search_dirs: list[Path] = [_project_root() / "data" / "models"]
        for root in candidates_roots:
            whisper_search_dirs.append(root / "tools" / "asr" / "models")
        for mdir in whisper_search_dirs:
            if not mdir.is_dir():
                continue
            for sub in sorted(mdir.iterdir()):
                if sub.is_dir() and "whisper" in sub.name.lower() and (sub / "model.bin").is_file():
                    whisper_model_dir = str(sub)
                    break
            if whisper_model_dir:
                break

    if not funasr_models:
        for f_cand in reg_funasr:
            fp = Path(f_cand)
            if fp.is_dir():
                funasr_models = {"asr": str(fp), "vad": "", "punc": "", "cwd": str(fp)}
                break

    # 查找 Vosk 模型路径
    vosk_cfg = ((api_cfg.get("asr_extra_configs") or {}).get("vosk") or {}).get("model_path") or ""
    vosk_path = ""
    for v_cand in reg_vosk:
        vp = Path(v_cand)
        if vp.is_dir():
            vosk_path = str(vp)
            break
    if not vosk_path and vosk_cfg:
        vp = Path(vosk_cfg)
        if not vp.is_absolute():
            vp = (_project_root() / vp).resolve()
        if vp.is_dir():
            vosk_path = str(vp)

    # Whisper 能用的解释器：优先整合包（官方 Windows 包自带 faster_whisper），
    # 否则退到插件自己的私有运行环境（用宿主 pip --target 装的）。
    whisper_python = ""
    whisper_run_env: dict[str, str] = {}
    whisper_mode = ""
    if gpt_python and _has_faster_whisper(gpt_python):
        whisper_python, whisper_mode = gpt_python, "bundle"
    else:
        try:
            from . import asr_runtime

            target = asr_runtime.whisper_target()
            if target["ok"]:
                whisper_python = str(target["python"])
                whisper_run_env = dict(target["env"] or {})
                whisper_mode = "private"
        except Exception:  # noqa: BLE001
            pass

    return {
        "current_python": sys.executable,
        "gpt_python": gpt_python,
        # 整合包自带的 Windows runtime 里通常有 faster_whisper（1.1.1）；
        # 自建/移植环境可能没有，这时靠 whisper_python（私有环境）兜。
        "whisper_python": whisper_python,
        "whisper_env": whisper_run_env,
        "whisper_mode": whisper_mode,
        "whisper_pkg_ok": bool(whisper_python),
        "funasr_models": funasr_models,
        "whisper_model_dir": whisper_model_dir,
        "vosk_model_path": vosk_path,
    }


def _ensure_16k_mono_wav(
    input_audio: Path,
    work_dir: Path,
    *,
    start_sec: int = 0,
    max_duration_sec: int = 0,
) -> Path:
    """将任意音频统一转为 16kHz 单声道 WAV 供本地 ASR 模型读取（支持按 start_sec 切片）。"""
    target = work_dir / f"{input_audio.stem}_{int(start_sec)}_{int(max_duration_sec)}_16k.wav"
    if input_audio.suffix.lower() == ".wav" and int(start_sec) <= 0:
        try:
            with wave.open(str(input_audio), "rb") as wf:
                if wf.getnchannels() == 1 and wf.getframerate() == 16000:
                    dur = wf.getnframes() / float(wf.getframerate() or 16000)
                    if not max_duration_sec or dur <= float(max_duration_sec) + 1.0:
                        return input_audio
        except Exception:
            pass

    exe = _ffmpeg_exe()
    cmd = [exe, "-y"]
    if start_sec and int(start_sec) > 0:
        cmd.extend(["-ss", str(int(start_sec))])
    cmd.extend(["-i", str(input_audio), "-vn"])
    if max_duration_sec and max_duration_sec > 0:
        cmd.extend(["-t", str(int(max_duration_sec))])
    cmd.extend(["-ac", "1", "-ar", "16000", str(target)])
    res = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        **_no_window(),
    )
    if res.returncode != 0 or not target.is_file():
        raise RuntimeError(f"音频转码 16kHz 失败：{(res.stderr or '')[-250:]}")
    return target


def _clean_subprocess_env() -> dict[str, str]:
    """清理 PYTHONHOME/PYTHONPATH，避免跨解释器干扰；不设 PYTHONUTF8=1 以兼容 GBK `.pth` 路径。"""
    env = dict(os.environ)
    for k in list(env):
        if k.upper().startswith("PYTHON"):
            env.pop(k, None)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _split_text_to_timed_segments(text: str, total_sec: float) -> list[dict[str, Any]]:
    raw = str(text or "").strip()
    if not raw:
        return []
    parts = [p.strip() for p in re.split(r"(?<=[。！？!?…\n])", raw) if p.strip()]
    if not parts:
        parts = [raw]
    total_chars = max(1, sum(len(p) for p in parts))
    dur = max(1.0, float(total_sec or 1.0))
    cur_t = 0.0
    out: list[dict[str, Any]] = []
    for p in parts:
        span = dur * (len(p) / total_chars)
        out.append({"start": round(cur_t, 2), "end": round(cur_t + span, 2), "text": p})
        cur_t += span
    return out


def _wav_duration_sec(wav_path: Path) -> float:
    try:
        with wave.open(str(wav_path), "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate() or 16000
            return frames / float(rate)
    except Exception:
        return 0.0


def _run_funasr_subprocess(
    python_exe: str,
    cwd: str,
    wav_path: Path,
    models: dict[str, str],
    *,
    timeout: float = 240.0,
) -> list[dict[str, Any]]:
    payload = {
        "wav": str(wav_path.resolve()),
        "asr": models["asr"],
        "vad": models.get("vad") or "",
        "punc": models.get("punc") or "",
    }
    worker_code = (
        "import sys, json\n"
        "cfg = json.loads(sys.stdin.read())\n"
        "from funasr import AutoModel\n"
        "kw = {'model': cfg['asr'], 'disable_update': True, 'disable_pbar': True, 'disable_log': True}\n"
        "if cfg.get('vad'): kw['vad_model'] = cfg['vad']\n"
        "if cfg.get('punc'): kw['punc_model'] = cfg['punc']\n"
        "m = AutoModel(**kw)\n"
        "res = m.generate(input=cfg['wav'])\n"
        "out = {'segments': [], 'text': ''}\n"
        "if isinstance(res, list) and res:\n"
        "    item = res[0] if isinstance(res[0], dict) else {}\n"
        "    out['text'] = str(item.get('text') or '')\n"
        "    for s in (item.get('sentence_info') or []):\n"
        "        t = str(s.get('text') or '').strip()\n"
        "        if t:\n"
        "            out['segments'].append({'start': round(float(s.get('start', 0))/1000.0, 2), 'end': round(float(s.get('end', 0))/1000.0, 2), 'text': t})\n"
        "print('__NANAMI_ASR_JSON__' + json.dumps(out, ensure_ascii=False))\n"
    )
    proc = subprocess.run(
        [python_exe, "-c", worker_code],
        cwd=cwd or None,
        input=json.dumps(payload, ensure_ascii=False),
        env=_clean_subprocess_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        **_no_window(),
    )
    if proc.returncode != 0:
        raise RuntimeError(f"FunASR 子进程失败: {(proc.stderr or proc.stdout)[-300:]}")
    marker = "__NANAMI_ASR_JSON__"
    for line in reversed(proc.stdout.splitlines()):
        if marker in line:
            data = json.loads(line.split(marker, 1)[1])
            segs = data.get("segments") or []
            if segs:
                return segs
            return _split_text_to_timed_segments(data.get("text") or "", _wav_duration_sec(wav_path))
    return []


def _run_faster_whisper_subprocess(
    python_exe: str,
    cwd: str,
    wav_path: Path,
    model_ref: str,
    *,
    language: str = "auto",
    timeout: float = 240.0,
    extra_env: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    lang = None if language in ("", "auto") else language
    payload = {"wav": str(wav_path.resolve()), "model": model_ref, "language": lang}
    worker_code = (
        "import ctypes, os, sys, json\n"
        "from pathlib import Path\n"
        "try:\n"
        "    import torch\n"
        "except Exception:\n"
        "    torch = None\n"
        "HAS_CUDA = bool(torch is not None and getattr(torch, 'cuda', None) is not None and torch.cuda.is_available())\n"
        "if HAS_CUDA and sys.platform == 'win32':\n"
        "    tlib = Path(torch.__file__).parent / 'lib'\n"
        "    if tlib.is_dir():\n"
        "        os.add_dll_directory(str(tlib))\n"
        "        for dll in sorted(tlib.glob('cudnn_cnn*.dll')):\n"
        "            try:\n"
        "                ctypes.CDLL(dll.name)\n"
        "            except OSError:\n"
        "                pass\n"
        "from faster_whisper import WhisperModel\n"
        "cfg = json.loads(sys.stdin.read())\n"
        "def _transcribe(dev, comp):\n"
        "    m = WhisperModel(cfg['model'], device=dev, compute_type=comp)\n"
        "    segs, _info = m.transcribe(cfg['wav'], beam_size=5, chunk_length=8, condition_on_previous_text=False, no_speech_threshold=0.65, vad_filter=False, language=cfg.get('language'))\n"
        "    return [{'start': round(float(s.start), 2), 'end': round(float(s.end), 2), 'text': s.text.strip()} for s in segs if s.text.strip()]\n"
        "out = None\n"
        "if HAS_CUDA:\n"
        "    try:\n"
        "        out = _transcribe('cuda', 'float16')\n"
        "    except Exception:\n"
        "        out = None\n"
        "if out is None:\n"
        "    out = _transcribe('cpu', 'int8')\n"
        "print('__NANAMI_ASR_JSON__' + json.dumps(out, ensure_ascii=False))\n"
    )
    proc = subprocess.run(
        [python_exe, "-c", worker_code],
        cwd=cwd or None,
        input=json.dumps(payload, ensure_ascii=False),
        env={**_clean_subprocess_env(), **(extra_env or {})},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        **_no_window(),
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Faster-Whisper 子进程失败: {(proc.stderr or proc.stdout)[-300:]}")
    marker = "__NANAMI_ASR_JSON__"
    for line in reversed(proc.stdout.splitlines()):
        if marker in line:
            return json.loads(line.split(marker, 1)[1])
    return []


class _PersistentWhisperPool:
    """常驻 Faster-Whisper 子进程池：只加载一次模型权重，消除多视频重复冷启动开销。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[str] | None = None
        self._key: tuple[str, str] | None = None

    def close(self) -> None:
        proc = self._proc
        self._proc = None
        self._key = None
        if proc is not None and proc.poll() is None:
            try:
                if proc.stdin:
                    proc.stdin.close()
                proc.terminate()
            except Exception:
                pass

    def _ensure_proc(self, python_exe: str, cwd: str, model_ref: str,
                     extra_env: dict[str, str] | None = None) -> subprocess.Popen[str]:
        key = (python_exe, model_ref, json.dumps(extra_env or {}, sort_keys=True))
        if self._proc is not None and self._proc.poll() is None and self._key == key:
            return self._proc
        self.close()
        daemon_code = (
            "import ctypes, os, sys, json\n"
            "from pathlib import Path\n"
            "try:\n"
        "    import torch\n"
        "except Exception:\n"
        "    torch = None\n"
        "HAS_CUDA = bool(torch is not None and getattr(torch, 'cuda', None) is not None and torch.cuda.is_available())\n"
            "if HAS_CUDA and sys.platform == 'win32':\n"
            "    tlib = Path(torch.__file__).parent / 'lib'\n"
            "    if tlib.is_dir():\n"
            "        os.add_dll_directory(str(tlib))\n"
            "        for dll in sorted(tlib.glob('cudnn_cnn*.dll')):\n"
            "            try:\n"
            "                ctypes.CDLL(dll.name)\n"
            "            except OSError:\n"
            "                pass\n"
            "from faster_whisper import WhisperModel\n"
            "model_ref = sys.argv[1]\n"
            "model = None\n"
            "if HAS_CUDA:\n"
            "    try:\n"
            "        model = WhisperModel(model_ref, device='cuda', compute_type='float16')\n"
            "    except Exception:\n"
            "        model = None\n"
            "if model is None:\n"
            "    model = WhisperModel(model_ref, device='cpu', compute_type='int8')\n"
            "print('__NANAMI_WHISPER_READY__', flush=True)\n"
            "for raw in sys.stdin:\n"
            "    raw = raw.strip()\n"
            "    if not raw:\n"
            "        continue\n"
            "    try:\n"
            "        req = json.loads(raw)\n"
            "        segs, _info = model.transcribe(req['wav'], beam_size=5, chunk_length=8, condition_on_previous_text=False, no_speech_threshold=0.65, vad_filter=False, language=req.get('language'))\n"
            "        out = [{'start': round(float(s.start), 2), 'end': round(float(s.end), 2), 'text': s.text.strip()} for s in segs if s.text.strip()]\n"
            "        print('__NANAMI_ASR_JSON__' + json.dumps({'ok': True, 'segments': out}, ensure_ascii=False), flush=True)\n"
            "    except Exception as exc:\n"
            "        print('__NANAMI_ASR_JSON__' + json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False), flush=True)\n"
        )
        proc = subprocess.Popen(
            [python_exe, "-c", daemon_code, model_ref],
            cwd=cwd or None,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**_clean_subprocess_env(), **(extra_env or {})},
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            **_no_window(),
        )
        assert proc.stdout is not None
        ready = False
        for _ in range(40):
            line = proc.stdout.readline()
            if not line:
                break
            if "__NANAMI_WHISPER_READY__" in line:
                ready = True
                break
        if not ready:
            err_tail = (proc.stderr.read() if proc.stderr else "")[-300:] if proc.poll() is not None else ""
            proc.terminate()
            raise RuntimeError(f"常驻 Whisper 启动失败: {err_tail}")
        self._proc = proc
        self._key = key
        return proc

    def transcribe(
        self,
        python_exe: str,
        cwd: str,
        wav_path: Path,
        model_ref: str,
        *,
        language: str = "auto",
        extra_env: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        lang = None if language in ("", "auto") else language
        with self._lock:
            proc = self._ensure_proc(python_exe, cwd, model_ref, extra_env)
            assert proc.stdin is not None and proc.stdout is not None
            proc.stdin.write(json.dumps({"wav": str(wav_path.resolve()), "language": lang}, ensure_ascii=False) + "\n")
            proc.stdin.flush()
            while True:
                line = proc.stdout.readline()
                if not line:
                    self.close()
                    raise RuntimeError("常驻 Whisper 进程意外退出")
                if "__NANAMI_ASR_JSON__" in line:
                    payload = json.loads(line.split("__NANAMI_ASR_JSON__", 1)[1])
                    if not payload.get("ok"):
                        raise RuntimeError(str(payload.get("error") or "Whisper 转写失败"))
                    return list(payload.get("segments") or [])


_WHISPER_POOL = _PersistentWhisperPool()
atexit.register(_WHISPER_POOL.close)


def _run_faster_whisper_cached(
    python_exe: str,
    cwd: str,
    wav_path: Path,
    model_ref: str,
    *,
    language: str = "auto",
    timeout: float = 240.0,
    extra_env: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    try:
        return _WHISPER_POOL.transcribe(python_exe, cwd, wav_path, model_ref,
                                        language=language, extra_env=extra_env)
    except Exception as exc:
        logger.debug("nanami_persona: 常驻 Whisper 回退单次子进程: %s", exc)
        return _run_faster_whisper_subprocess(
            python_exe, cwd, wav_path, model_ref, language=language,
            timeout=timeout, extra_env=extra_env,
        )


def _run_vosk_local(wav_path: Path, model_path: str) -> list[dict[str, Any]]:
    from vosk import KaldiRecognizer, Model  # type: ignore

    model = Model(model_path=model_path)
    segments: list[dict[str, Any]] = []
    with wave.open(str(wav_path), "rb") as wf:
        rate = wf.getframerate()
        rec = KaldiRecognizer(model, rate)
        rec.SetWords(True)
        cur_start = 0.0
        frames_read = 0
        while True:
            data = wf.readframes(4000)
            if not data:
                break
            frames_read += 4000
            if rec.AcceptWaveform(data):
                res = json.loads(rec.Result())
                txt = str(res.get("text") or "").replace(" ", "").strip()
                ed = frames_read / float(rate or 16000)
                if txt:
                    segments.append({"start": round(cur_start, 2), "end": round(ed, 2), "text": txt})
                cur_start = ed
        final_res = json.loads(rec.FinalResult())
        ftxt = str(final_res.get("text") or "").replace(" ", "").strip()
        if ftxt:
            segments.append({"start": round(cur_start, 2), "end": round(_wav_duration_sec(wav_path), 2), "text": ftxt})
    return segments


def _shift_segments(segments: list[dict[str, Any]], offset_sec: int) -> list[dict[str, Any]]:
    if not offset_sec or offset_sec <= 0:
        return segments
    off = float(offset_sec)
    return [
        {
            **s,
            "start": round(float(s.get("start", 0.0) or 0.0) + off, 2),
            "end": round(float(s.get("end", 0.0) or 0.0) + off, 2),
        }
        for s in segments
    ]


def transcribe_audio_local(
    audio_path: str | Path,
    *,
    language: str = "auto",
    start_sec: int = 0,
    max_duration_sec: int = 360,
    timeout: float = 240.0,
    allow_whisper_download: bool = False,
) -> list[dict[str, Any]]:
    """调用本机可用的离线 ASR 模型，将音频转写为带时间戳的句子列表（支持 start_sec 偏移切片）。"""
    src = Path(audio_path)
    if not src.is_file():
        raise FileNotFoundError(f"找不到待转写音频：{src}")

    with tempfile.TemporaryDirectory(prefix="nanami_asr_") as tmp_dir:
        wav_16k = _ensure_16k_mono_wav(
            src, Path(tmp_dir), start_sec=start_sec, max_duration_sec=max_duration_sec,
        )
        dur = _wav_duration_sec(wav_16k)
        if dur < 0.8:
            return []
        env_info = _resolve_asr_environment()
        whisper_ref = env_info["whisper_model_dir"] or ("small" if allow_whisper_download else "")
        whisper_cwd = env_info["funasr_models"].get("cwd") or (
            str(Path(env_info["gpt_python"]).parents[1]) if env_info["gpt_python"] else ""
        )
        if not whisper_cwd and env_info.get("whisper_env"):
            # 私有环境：cwd 用 libs 的上一级，避免落到宿主安装目录里
            whisper_cwd = str(Path(next(iter(env_info["whisper_env"].values()))).parent)

        # 1. 自动/日语模式：优先走常驻多语种 Faster-Whisper，避免重复冷启动与中文强转乱码
        if env_info.get("whisper_python") and whisper_ref and language in ("auto", "ja"):
            try:
                segs = _run_faster_whisper_cached(
                    env_info["whisper_python"],
                    whisper_cwd,
                    wav_16k,
                    whisper_ref,
                    language=language,
                    timeout=timeout,
                    extra_env=env_info.get("whisper_env") or {},
                )
                if segs:
                    return _shift_segments(segs, start_sec)
            except Exception as exc:
                logger.warning("nanami_persona: Faster-Whisper 转写失败，尝试其他后端: %s", exc)

        # 2. 中文模式或 Whisper 不可用/空结果：使用本地 FunASR Paraformer-Large
        if env_info["gpt_python"] and env_info["funasr_models"] and language in ("zh", "auto"):
            try:
                segs = _run_funasr_subprocess(
                    env_info["gpt_python"],
                    env_info["funasr_models"]["cwd"],
                    wav_16k,
                    env_info["funasr_models"],
                    timeout=timeout,
                )
                char_sum = sum(len(s.get("text") or "") for s in segs)
                if char_sum >= 6 or (dur < 5.0 and char_sum > 0):
                    return _shift_segments(segs, start_sec)
            except Exception as exc:
                logger.warning("nanami_persona: FunASR 本地转写失败，尝试其他后端: %s", exc)

        # 3. 显式 language="zh" 且 FunASR 失败时的 Faster-Whisper 兜底
        if env_info.get("whisper_python") and whisper_ref and language == "zh":
            try:
                segs = _run_faster_whisper_cached(
                    env_info["whisper_python"],
                    whisper_cwd,
                    wav_16k,
                    whisper_ref,
                    language="zh",
                    timeout=timeout,
                    extra_env=env_info.get("whisper_env") or {},
                )
                if segs:
                    return _shift_segments(segs, start_sec)
            except Exception as exc:
                logger.warning("nanami_persona: Faster-Whisper 中文兜底失败: %s", exc)

        # 4. 兜底 Vosk（如用户配置了 assets/system/models/vosk-...）
        if env_info["vosk_model_path"]:
            try:
                return _shift_segments(_run_vosk_local(wav_16k, env_info["vosk_model_path"]), start_sec)
            except Exception as exc:
                logger.warning("nanami_persona: Vosk 本地转写失败: %s", exc)

    reasons: list[str] = []
    if not env_info.get("whisper_python"):
        if not env_info["gpt_python"]:
            reasons.append("没找到 GPT-SoVITS 整合包（api.yaml 的 gpt_sovits_api_path 为空，"
                           "data/tts_bundles 里也没有解压好的包），也没有安装插件私有 ASR 运行环境")
        else:
            reasons.append("整合包的解释器里没有 faster-whisper 包，也没有安装插件私有 ASR 运行环境"
                           "（官方 Windows 整合包自带 1.1.1；自建或移植环境可能缺）")
    if not env_info["whisper_model_dir"]:
        reasons.append("没有 Faster-Whisper 模型目录（settings 里挂载的目录或 data/models 下需要 model.bin）")
    if not env_info["funasr_models"]:
        reasons.append("没有 FunASR 模型目录（整合包 tools/asr/models 下应有 paraformer/vad/punc）")
    if not reasons and not env_info["vosk_model_path"]:
        reasons.append("没有 Vosk 模型可兜底")
    raise RuntimeError(
        "离线语音识别不可用：" + "；".join(reasons[:3]) +
        "。可以在设置页「下载流水线 ASR 模型」补齐模型，或改用带 B 站字幕的视频链接。"
    )


def format_transcript_with_timestamps(segments: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for seg in segments:
        txt = str(seg.get("text") or "").strip()
        if not txt:
            continue
        st = float(seg.get("start", 0.0) or 0.0)
        ed = float(seg.get("end", st) or st)
        sm, ss = divmod(st, 60)
        em, es = divmod(ed, 60)
        lines.append(f"[{int(sm):02d}:{ss:04.1f} - {int(em):02d}:{es:04.1f}] {txt}")
    return "\n".join(lines)


def extract_video_transcript(
    bvid_or_url: str,
    work_dir: str | Path,
    *,
    language: str = "auto",
    max_duration_sec: int = 360,
    allow_whisper_download: bool = False,
    subtitle_fn: Callable[[str], tuple[str, list[dict[str, Any]]]] | None = None,
    downloader_fn: Callable[..., Path] | None = None,
    asr_fn: Callable[..., list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """抽取单个 B 站视频带时间戳文本：优先拉取 B 站字幕轨，无字幕时再下载音轨跑本地 ASR。"""
    wdir = Path(work_dir)
    wdir.mkdir(parents=True, exist_ok=True)
    m = re.search(r"(BV[A-Za-z0-9]+)", bvid_or_url, re.I)
    bvid = m.group(1) if m else bvid_or_url.strip()

    sub_reader = subtitle_fn or fetch_bili_subtitle_if_any
    title, segments = sub_reader(bvid_or_url)
    if segments:
        return {
            "bvid": bvid,
            "title": title or bvid,
            "source": "subtitle",
            "segments": segments,
            "text": format_transcript_with_timestamps(segments),
        }

    dl = downloader_fn or download_bili_audio
    wav_path = dl(
        bvid_or_url,
        wdir,
        max_duration_sec=max_duration_sec,
        mono_16k=True,
    )
    transcriber = asr_fn or transcribe_audio_local
    segments = transcriber(
        wav_path,
        language=language,
        max_duration_sec=max_duration_sec,
        allow_whisper_download=allow_whisper_download,
    )
    raw_m4s = wdir / "source.m4s"
    return {
        "bvid": bvid,
        "title": title or bvid,
        "source": "asr",
        "audio_path": str(wav_path),
        "raw_audio_path": str(raw_m4s) if raw_m4s.is_file() else "",
        "segments": segments,
        "text": format_transcript_with_timestamps(segments),
    }


def extract_local_audio_transcript(
    audio_path: str | Path,
    *,
    language: str = "auto",
    start_sec: int = 0,
    max_duration_sec: int = 360,
    allow_whisper_download: bool = False,
    asr_fn: Callable[..., list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """转写用户提供的本地音频文件（或已缓存 B 站音轨的后续时间切片）。"""
    src = Path(audio_path)
    transcriber = asr_fn or transcribe_audio_local
    if asr_fn is not None:
        segments = transcriber(
            src,
            language=language,
            max_duration_sec=max_duration_sec,
            allow_whisper_download=allow_whisper_download,
        )
    else:
        segments = transcriber(
            src,
            language=language,
            start_sec=start_sec,
            max_duration_sec=max_duration_sec,
            allow_whisper_download=allow_whisper_download,
        )
    return {
        "bvid": src.name,
        "title": src.stem,
        "source": "local_asr",
        "audio_path": str(src),
        "segments": segments,
        "text": format_transcript_with_timestamps(segments),
    }
