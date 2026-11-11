"""与工具箱（`toolkit/*.mjs`）通信的唯一一层：子进程 + UTF-8 + 超时 + fail-open。

为什么走子进程而不是把 mind.mjs 翻成 Python：
    那份心理模型有 6k 行、几十条带出处的注释，翻一遍必然走样，而且上游一改就对不上了。
    子进程调用的代价是每轮 ~130ms（实测），换的是「规则只有一份真相」。

三条纪律（照抄原平台踩过的坑）：
    ① **先拿输出，再落状态**——`mind.mjs` 自己保证 stdout 冲出去了才写盘；这里只负责收好。
    ② **必须先 flush 才能算这一轮发生**——所以 stdout 读不到/解析不了时，这一轮当作没发生
       （返回 None，卡片不插），绝不用半截数据拼提示词。
    ③ **任何失败都不许挡住聊天**：超时/没装 node/编码炸了 → 记一条日志 + 返回 None。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from .settings import MindSettings

logger = logging.getLogger(__name__)

_CREATE_NO_WINDOW = 0x08000000


def _common_node_paths() -> tuple[Path, ...]:
    """各平台上 node 常见的安装位置（只读探测，命中就用）。"""
    paths: list[Path] = []
    if os.name == "nt":
        roots = [
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
            os.environ.get("LOCALAPPDATA", ""),
            os.environ.get("APPDATA", ""),
            os.environ.get("NVM_SYMLINK", ""),
        ]
        relatives = (
            r"nodejs\node.exe",
            r"Programs\nodejs\node.exe",
            r"nvm\current\node.exe",
            r"fnm_multishells\current\node.exe",
            r"Microsoft\WindowsApps\node.exe",
        )
        for root in roots:
            if not root:
                continue
            for rel in relatives:
                paths.append(Path(root) / rel)
    else:
        paths.extend(
            Path(candidate)
            for candidate in (
                "/usr/local/bin/node",
                "/usr/bin/node",
                "/opt/homebrew/bin/node",
                "/snap/bin/node",
            )
        )
    return tuple(paths)


_COMMON_NODE_PATHS = _common_node_paths()


class NodeUnavailableError(RuntimeError):
    """找不到 node（或版本太旧）——一次性告警，之后整层关掉。"""


@dataclass(frozen=True)
class MindCard:
    """`mind.mjs --json` 里我们真正要用的那几个字段。"""

    card: str
    appraisal: str = ""
    action: str = ""
    expression_index: int = 0
    energy: float = 0.0
    mood_v: float = 0.0
    raw: dict[str, Any] | None = None

    @property
    def empty(self) -> bool:
        return not self.card.strip()


@dataclass(frozen=True)
class GuardResult:
    texts: list[str]
    fixes: list[dict[str, Any]]

    @property
    def changed(self) -> bool:
        return bool(self.fixes)


class PersonaToolkit:
    """工具箱的进程级句柄。插件初始化时建一次，之后所有线程共用。

    `settings` 传的是**取值函数**而不是一份快照：settings.yaml 按 mtime 热加载，
    超时/重试这些数改完就该在下一轮生效，不该等到重启。
    """

    def __init__(
        self,
        *,
        toolkit_dir: Path,
        state_root: Path,
        settings: MindSettings | Callable[[], MindSettings],
        project_root: Path,
    ) -> None:
        self._toolkit_dir = toolkit_dir
        self._state_root = state_root
        self._settings_snapshot = None
        self._settings_provider = None
        if isinstance(settings, MindSettings):
            self._settings_snapshot = settings
        else:
            self._settings_provider = settings
            try:
                first = settings()
            except Exception:
                first = None
            # 兜底快照：取值函数以后要是返回了坏值，还有一份能用的
            self._settings_snapshot = first if isinstance(first, MindSettings) else MindSettings()
        self._project_root = project_root
        self._node = self._resolve_node(self._settings.node_exe)
        self._disabled_reason = ""
        self._lock = threading.Lock()
        self._her_lock = threading.Lock()
        self._stats = {"card_ok": 0, "card_fail": 0, "her_ok": 0, "her_fail": 0, "guard_ok": 0, "guard_fail": 0}

    @property
    def _settings(self) -> MindSettings:
        if self._settings_provider is not None:
            try:
                current = self._settings_provider()
            except Exception:
                current = None
            if isinstance(current, MindSettings):
                return current
        return self._settings_snapshot

    # ── 基本信息 ────────────────────────────────────────────────────────
    @property
    def toolkit_dir(self) -> Path:
        return self._toolkit_dir

    @property
    def state_root(self) -> Path:
        return self._state_root

    @property
    def disabled_reason(self) -> str:
        return self._disabled_reason

    @property
    def stats(self) -> dict[str, int]:
        return dict(self._stats)

    def mind_entry(self) -> Path:
        return self._toolkit_dir / "mind.mjs"

    def guard_entry(self) -> Path:
        return self._toolkit_dir.parent / "guard_bridge.mjs"

    def is_ready(self) -> bool:
        return bool(self._node) and self.mind_entry().is_file() and not self._disabled_reason

    def preflight(self) -> tuple[bool, str]:
        """开聊前体检一次：node 有没有、工具箱在不在、`--show` 能不能跑。"""
        if not self._node:
            return False, "找不到 node（需要 Node 18+，或在 settings.yaml 里写 mind.node_exe）"
        if not self.mind_entry().is_file():
            return False, f"工具箱里没有 mind.mjs：{self.mind_entry()}"
        try:
            result = self._run_node(
                [str(self.mind_entry()), "--root", str(self._state_root), "--show", "--chat", "preflight"],
                timeout=max(2.0, self._settings.card_timeout),
                title="preflight",
            )
        except NodeUnavailableError as exc:
            return False, str(exc)
        if result is None:
            return False, "mind.mjs --show 跑不起来（看日志里的 stderr）"
        return True, "ok"

    # ── 三个入口 ────────────────────────────────────────────────────────
    def card(self, his_text: str, chat_key: str) -> MindCard | None:
        """算这一轮的卡片。`--his` 会推进状态，所以每个用户回合只能调一次。"""
        if not self.is_ready():
            return None
        text = str(his_text or "").strip()
        if not text:
            return None
        args = [
            str(self.mind_entry()),
            "--root", str(self._state_root),
            "--his", text,
            "--chat", chat_key,
            "--json",
        ]
        raw = self._call_json(args, timeout=self._settings.card_timeout, retries=self._settings.retries, title="card")
        if raw is None:
            self._stats["card_fail"] += 1
            return None
        card_text = str(raw.get("card") or "").strip()
        if not card_text:
            self._stats["card_fail"] += 1
            logger.debug("nanami_persona: mind.mjs 返回了空卡片（chat=%s）", chat_key)
            return None
        self._stats["card_ok"] += 1
        state = raw.get("state") if isinstance(raw.get("state"), dict) else {}
        expression = state.get("expression") if isinstance(state.get("expression"), dict) else {}
        return MindCard(
            card=card_text,
            appraisal=str(raw.get("appraisal") or ""),
            action=str(raw.get("action") or ""),
            expression_index=int(expression.get("index") or 0),
            energy=float(state.get("energy") or 0.0),
            mood_v=float(state.get("moodV") or 0.0),
            raw=raw,
        )

    def her(self, said: str, chat_key: str) -> bool:
        """回写她这一轮说的话（`--her`）。同一回合内合并成一次调用。"""
        if not self.is_ready():
            return False
        text = str(said or "").strip()
        if not text:
            return False
        args = [
            str(self.mind_entry()),
            "--root", str(self._state_root),
            "--her", text,
            "--chat", chat_key,
        ]
        with self._her_lock:
            ok = self._run_node(args, timeout=self._settings.her_timeout, title="her") is not None
        self._stats["her_ok" if ok else "her_fail"] += 1
        return ok

    def guard(self, texts: Sequence[str]) -> GuardResult | None:
        """字面守卫：一批气泡进，一批（可能被改过的）气泡出。"""
        items = [str(one or "") for one in texts]
        bridge = self.guard_entry()
        if not items or not bridge.is_file() or not self._node:
            return GuardResult(texts=items, fixes=[])
        payload = json.dumps({"texts": items}, ensure_ascii=False)
        raw = self._call_json(
            [str(bridge), str(self._toolkit_dir)],
            stdin_text=payload,
            timeout=self._settings.guard_timeout,
            retries=0,
            title="guard",
        )
        if raw is None or not raw.get("ok"):
            self._stats["guard_fail"] += 1
            return None
        self._stats["guard_ok"] += 1
        out = raw.get("texts")
        if not isinstance(out, list) or len(out) != len(items):
            self._stats["guard_fail"] += 1
            return None
        fixes = [one for one in (raw.get("fixes") or []) if isinstance(one, dict)]
        return GuardResult(texts=[str(one or "") for one in out], fixes=fixes)

    # ── 内部 ────────────────────────────────────────────────────────────
    def _percept_env(self) -> dict[str, str]:
        """把"读句"（言语行为判定）接到**宿主正在用的那个大模型**上。

        上游这一格是本地 3B（`MIND_PERCEPT_BASE` 指向 127.0.0.1:8080），
        但打包成插件以后不能再假设某个人的机器上有个 8080 端口在跑小模型——
        所以这里直接把宿主的 base_url / model / key 传给它：
        换任何服务商（DeepSeek / OpenAI / 本地 Ollama）都不用改代码。
        """
        if not self._settings.percept:
            return {"MIND_PERCEPT": "0"}
        endpoint = _host_llm_endpoint()
        if endpoint is None:
            # 拿不到宿主的配置就老实关掉，别让 percept 回落到它写死的地址
            logger.info("nanami_persona: 读不到宿主 LLM 配置，percept 这一格关掉")
            return {"MIND_PERCEPT": "0"}
        base, model, key = endpoint
        env = {
            "MIND_PERCEPT": "1",
            "MIND_PERCEPT_BASE": base,
            "MIND_PERCEPT_MODEL": model,
            # 远程模型比"隔壁的 3B"慢，预算放宽到 settings 里那个数（默认 2.5s）
            "MIND_PERCEPT_TIMEOUT_MS": str(int(max(0.5, self._settings.percept_timeout) * 1000)),
            "MIND_PERCEPT_CACHE": str(self._state_root / "state" / "percept-cache.json"),
        }
        if key:
            env["MIND_PERCEPT_KEY"] = key
        return env

    def _resolve_node(self, configured: str) -> str:
        """找 node：配置 > 环境变量 > 各处常见安装位置 > PATH。

        为什么不能只靠 `shutil.which`：Windows 上装的是 nvm / 便携版 / Microsoft Store 版时，
        聊天进程的 PATH 里可能没有 node（用户是双击 exe 启动的，PATH 和终端里的不是一套）。
        换设备最容易踩的就是这一格，所以把常见路径都试一遍，还能用环境变量指定。
        """
        candidates: list[str] = []
        for value in (configured, os.environ.get("SHINSEKAI_NANAMI_NODE", ""), os.environ.get("NANAMI_NODE", "")):
            raw = str(value or "").strip()
            if raw:
                candidates.append(raw)

        for raw in list(candidates):
            path = Path(raw)
            if path.is_file():
                return str(path)
            if raw and not path.suffix:
                found = shutil.which(raw)
                if found:
                    return found
        if configured:
            logger.warning("nanami_persona: settings 里的 node_exe 不存在：%s（继续找其它的）", configured)

        for guess in _COMMON_NODE_PATHS:
            if guess.is_file():
                return str(guess)
        for name in ("node", "node.exe", "nodejs", "nodejs.exe"):
            found = shutil.which(name)
            if found:
                return found
        return ""

    def _call_json(
        self,
        args: list[str],
        *,
        timeout: float,
        retries: int,
        title: str,
        stdin_text: str | None = None,
    ) -> dict[str, Any] | None:
        attempt = 0
        while attempt <= max(0, retries):
            attempt += 1
            text = self._run_node(args, timeout=timeout, title=title, stdin_text=stdin_text)
            if text is None:
                continue
            parsed = _last_json_object(text)
            if parsed is not None:
                return parsed
            logger.warning(
                "nanami_persona: %s 的输出不是 JSON（%s 字节）：%s",
                title, len(text), text[:200].replace("\n", " ⏎ "),
            )
        return None

    def _run_node(
        self,
        args: list[str],
        *,
        timeout: float,
        title: str,
        stdin_text: str | None = None,
    ) -> str | None:
        if not self._node:
            raise NodeUnavailableError("node not found")
        env = dict(os.environ)
        # 工具箱里几个默认路径是相对路径；这里用环境变量把它们钉死成绝对路径，
        # 免得聊天进程的 cwd 和"项目根"不是同一个目录时静默找不到文件。
        env.setdefault("MIND_OFF_VOICE_PATH", str(self._toolkit_dir / "state" / "off-voice.json"))
        env.setdefault("MIND_SPRITE_LOOKS", str(self._toolkit_dir / "state" / "expression-sprites.json"))
        env.setdefault("MIND_ROOT", str(self._state_root))
        env.update(self._percept_env())
        env["PYTHONIOENCODING"] = env.get("PYTHONIOENCODING", "utf-8")
        kwargs: dict[str, Any] = {
            "input": stdin_text if stdin_text is not None else "",
            "capture_output": True,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "timeout": timeout,
            "cwd": str(self._toolkit_dir),
            "env": env,
        }
        if os.name == "nt":
            kwargs["creationflags"] = _CREATE_NO_WINDOW
        try:
            completed = subprocess.run([self._node, *args], check=False, **kwargs)
        except FileNotFoundError as exc:
            self._disabled_reason = f"node 不可用：{exc}"
            logger.warning("nanami_persona: %s，人格模型这一层关掉", self._disabled_reason)
            raise NodeUnavailableError(self._disabled_reason) from exc
        except subprocess.TimeoutExpired:
            logger.warning("nanami_persona: %s 超时（%.1fs），这一轮跳过", title, timeout)
            return None
        except OSError as exc:
            logger.warning("nanami_persona: %s 起不来：%s", title, exc)
            return None
        if completed.returncode != 0:
            logger.warning(
                "nanami_persona: %s 退出码 %s；stderr=%s",
                title, completed.returncode, (completed.stderr or "")[-400:].strip(),
            )
            return None
        stderr = (completed.stderr or "").strip()
        if stderr and self._settings.debug_stderr:
            logger.debug("nanami_persona: %s stderr: %s", title, stderr[-400:])
        return completed.stdout or ""


def _host_llm_endpoint() -> tuple[str, str, str] | None:
    """宿主的 LLM 端点 → (chat/completions URL, model, key)。读不到就 None。"""
    try:
        from config.config_manager import ConfigManager

        manager = ConfigManager()
        provider, model, base_url, api_key = manager.get_llm_api_config()
    except Exception:
        logger.debug("nanami_persona: 读宿主的 LLM 配置失败", exc_info=True)
        return None
    if not provider:
        return None
    base = str(base_url or "").rstrip("/")
    if not base:
        return None
    if not base.endswith("/chat/completions"):
        base = f"{base}/chat/completions"
    return base, str(model or ""), str(api_key or "")


def _last_json_object(text: str) -> dict[str, Any] | None:
    """从 stdout 里挑出那个 JSON 对象。

    `mind.mjs --json` 只往 stdout 写一行 JSON，调试信息全在 stderr；
    但 stdout 里再多一行别的东西时，我们不能整段 `json.loads`，所以从最后一行往前试。
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    for line in reversed(lines):
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def chat_key_of(prefix: str, session_hint: str) -> str:
    """会话指纹 → 稳定的 `--chat` 键。

    要短、要 ASCII、要稳定（同一个历史文件永远同一个键），但不能让不同会话撞在一起
    （撞了就是"两个会话串味"，原平台把它列为第 9 号坑）。
    """
    hint = str(session_hint or "").strip() or "default"
    digest = hashlib.sha1(hint.encode("utf-8", "replace")).hexdigest()[:10]
    safe_prefix = "".join(
        ch if (ch.isascii() and (ch.isalnum() or ch in "-_.")) else "_"
        for ch in str(prefix or "chat")
    )
    return f"{safe_prefix or 'chat'}:{digest}"
