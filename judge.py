"""判官：把原来要本地 3B 小模型干的活，交给**宿主配置的那个大模型**。

目前两件活（都只"判"，不"写"——改写＝选词＝生硬，这是上游量过的结论）：

| 活 | 判什么 | 上游做法 | 现在 |
| --- | --- | --- | --- |
| `ai_flavor` | 这句像不像"AI 助手／客服／系统播报" | 本地 3B，23/24 手写卷 | 宿主的大模型 |
| `lead_filler` | 句首「嗯，／诶，」是垫字还是有作用 | 云模型 83% | 宿主的大模型 |

三条纪律照抄上游（`ai-flavor.mjs` / `lead-trim.mjs`）：

1. **只判不写**：命中就"这条不发"，绝不替换成别的说法；
2. **fail-open**：超时 / 连不上 / 判不出 → 当"人话"，宁可漏一条，不吞她的话；
3. **缓存 6 小时**：同一句只问一次（按文本 hash）；落盘失败不影响判定。

端点不写死：优先用宿主正在用的那个 adapter（`llm_manager.llm_adapter`），
拿不到就按 `data/config/api.yaml` 现场造一个——**换任何服务商都不用改这里**。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

CACHE_FILE = "state/judge-cache.json"
CACHE_TTL_SEC = 6 * 3600
CACHE_MAX_ENTRIES = 600

AI_SYSTEM = """你判断一句话像不像"AI 助手／客服／系统播报／主持人"说的，而不是一个普通女高中生在聊天时说的。

AI 味的典型样子：
- 报在线状态、报身份：「我在」「随时叫我」「有什么可以帮你」。
- 汇报系统状态、确认收到：「已为你记下」「收到」「我这边显示」。
- 空泛的安抚与承诺：「一定可以的」「都会好起来的」「别担心」。
- 对物品做"够不够/行不行"的评估：「这沙发够长」「时间够用」。
- 把自己当服务方来解释：「我这边建议」「这个我认，是我没做好」。
- 客气地请对方把需求说清楚（客服澄清腔）。

主持人腔也算 AI 味：
- 通知对方"可以开始了"／安排对话流程（"你想说什么就说吧""我们聊点别的吧"）。
- 评论对话本身、解释"这该怎么接"、总结自己的说话习惯（"这种时候我一般也不知道该接什么"）。

不像 AI 的：具体的、有个人立场或感受的、说一半的、带自己生活细节的。

命中（像 AI／客服／主持人）：
- 「你想说什么就说吧，我听着。」→ 通知对方可以开始说话，像接待。
- 「这种时候我一般也不知道该接什么。」→ 在解释对话机制、总结自己的习惯。
- 「我们聊点别的吧。」→ 主持人在安排流程。
- 「收到，已经记下来了。」→ 汇报系统状态。
- 「有什么可以帮你的吗？」→ 客服开场。
- 「啊……那个我懂。」→ 先盖章"我懂"，是客服在接住情绪，不是她在说话。
- 「我也常常这样。」「我也有过。」→ 公式化共情（原典里这两句 0 命中）。
- 「回过神来，时间就没了。」（他刚说"刷着刷着就忘了"）→ 把他用过的那几个字原样搬回来复述，
  不是她自己的话；她要么换自己的说法，要么干脆不接那个词。

不算（像人在说话）：
- 「……嗯？」→ 只是应一声。
- 「这关我卡了三次才过。」→ 具体的、自己的事。
- 「我还以为你要说别的事呢。」→ 她自己的反应，具体。
- 「……嗯。」→ 只应一声，这是她最常做的事。

只输出 JSON：{"ai": true 或 false}"""

# 为什么要有上面那两段例句：同一个 prompt 喂给 3B 和大模型，行为不一样——
# 3B 按字面贴标签（"你想说什么就说吧"一看就是接待腔），大模型会替这句话找理由（"她在关心对方"）就放过。
# 实测（6 句该命中 + 4 句人味）：没有例句只抓到 3/6；加上例句 6/6、误伤 0/4。

FILLER_SYSTEM = """判断一句话开头的应声词（嗯／诶／啊／哦）是"垫字"还是"有作用"。

垫字：删掉它，整句照样通顺、意思不缺（只是延缓开口）。例：「嗯，我记得。那关在第二张地图。」「诶，这关我打过三遍了。」
有作用：应答（回答对方）、惊讶/疑问（后面问号）、想起（后面感叹号或"我想起来了"）、或整句很短删了就空。
      例：「诶？你说的是哪个存档？」「啊！我想起来了。」「嗯。」「嗯……这样啊。」
开头没有应声词：算"有作用"（false）。

只输出 JSON：{"filler": true 或 false}"""


@dataclass(frozen=True)
class Verdict:
    """`flag` 的意思是"命中"：ai=True 表示判定为 AI 味；filler=True 表示判定为垫字。"""

    flag: bool
    reason: str = "fail"
    detail: str = ""

    @property
    def decided(self) -> bool:
        return self.reason in ("model", "cache")

    @property
    def label(self) -> str:
        """日志用的三态：命中／干净／没判出来。

        "没判出来"必须单独说一句——判官超时的时候 flag 也是 False，
        如果都打成"没有 AI 味"，就会把"没检查"说成"检查过、很干净"（踩过）。
        """
        if not self.decided:
            return "没判出来"
        return "命中" if self.flag else "干净"


class LlmJudge:
    """用宿主的大模型做二分类判官。线程安全，失败一律 fail-open。"""

    def __init__(
        self,
        *,
        state_root: Path,
        enabled: bool = True,
        timeout: float = 2.0,
        adapter_provider: Callable[[], Any] | None = None,
    ) -> None:
        self._cache_path = Path(state_root) / CACHE_FILE
        self._enabled = bool(enabled)
        self._timeout = max(0.3, float(timeout))
        self._adapter_provider = adapter_provider or _default_adapter
        self._lock = threading.Lock()
        self._cache: dict[str, dict] | None = None
        self._adapter: Any = None
        self._warned_at = 0.0
        self._stats = {"model": 0, "cache": 0, "fail": 0, "off": 0, "timeout": 0}

    # ── 对外 ────────────────────────────────────────────────────────────
    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def stats(self) -> dict[str, int]:
        return dict(self._stats)

    def ai_flavor(self, text: str) -> Verdict:
        return self._judge("ai", text, AI_SYSTEM, "ai")

    def ai_flavor_async(self, text: str, on_result=None) -> None:
        """不挡路的判：丢到后台线程，判完回调（report 档就用这个）。

        为什么：`report` 只想知道"这一轮有没有 AI 味"，没必要让下一次回复等它。
        """
        def work() -> None:
            try:
                verdict = self.ai_flavor(text)
            except Exception:  # noqa: BLE001
                logger.debug("nanami_persona: 后台判官出错", exc_info=True)
                return
            if on_result is not None:
                try:
                    on_result(verdict)
                except Exception:  # noqa: BLE001
                    logger.debug("nanami_persona: 判官回调出错", exc_info=True)

        threading.Thread(target=work, name="nanami-judge-round", daemon=True).start()

    def lead_filler(self, text: str) -> Verdict:
        return self._judge("lead", text, FILLER_SYSTEM, "filler")

    def warmup(self) -> tuple[bool, str]:
        """体检：能不能真的问到大模型（不花钱的那点 token 也算一次调用）。"""
        verdict = self.ai_flavor("我在，随时叫我。")
        if not verdict.decided:
            return False, f"判官问不到大模型（{verdict.reason}）"
        return True, "ok"

    # ── 内部 ────────────────────────────────────────────────────────────
    def _judge(self, kind: str, text: str, system: str, field: str) -> Verdict:
        source = str(text or "").strip()
        if not self._enabled or not source:
            self._stats["off"] += 1
            return Verdict(False, "off")
        key = f"{kind}:{hashlib.sha1(source.encode('utf-8', 'replace')).hexdigest()[:16]}"

        cached = self._cache_get(key)
        if cached is not None:
            self._stats["cache"] += 1
            return Verdict(bool(cached), "cache")

        answer = self._ask(system, f"这句话：「{source}」\n\n只输出 JSON。", field)
        if answer is None:
            return Verdict(False, "fail")
        self._cache_put(key, answer)
        self._stats["model"] += 1
        return Verdict(answer, "model")

    def _ask(self, system: str, user: str, field: str) -> bool | None:
        """问一次大模型。超时就放弃（线程留着自然结束），返回 None = fail-open。"""
        adapter = self._get_adapter()
        if adapter is None:
            self._warn_once("拿不到宿主的 LLM adapter")
            return None

        result: dict[str, Any] = {}
        done = threading.Event()

        def work() -> None:
            try:
                response = adapter.chat(
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    stream=False,
                    temperature=0,
                    max_tokens=48,
                    response_format={"type": "json_object"},
                )
                content = str(getattr(adapter.decode_response(response), "content", "") or "")
                result["value"] = _parse_flag(content, field)
            except Exception as exc:  # noqa: BLE001 - 判官坏了不许影响聊天
                result["error"] = f"{type(exc).__name__}: {exc}"
            finally:
                done.set()

        thread = threading.Thread(target=work, name="nanami-judge", daemon=True)
        thread.start()
        if not done.wait(self._timeout):
            self._stats["timeout"] += 1
            logger.info("nanami_persona: 判官超时（%.1fs），这一条按人话放行", self._timeout)
            return None
        if "error" in result:
            self._stats["fail"] += 1
            self._warn_once(str(result["error"]))
            return None
        value = result.get("value")
        if value is None:
            self._stats["fail"] += 1
            return None
        return bool(value)

    def _get_adapter(self) -> Any:
        if self._adapter is not None:
            return self._adapter
        try:
            self._adapter = self._adapter_provider()
        except Exception:
            logger.exception("nanami_persona: 判官拿 adapter 失败")
            self._adapter = None
        return self._adapter

    def _warn_once(self, reason: str) -> None:
        now = time.monotonic()
        if now - self._warned_at < 600:
            return
        self._warned_at = now
        logger.warning(
            "nanami_persona: 判官暂时用不了（%s）——这一层空转，AI 味一律放行", reason
        )

    # ── 缓存 ────────────────────────────────────────────────────────────
    def _load(self) -> dict[str, dict]:
        if self._cache is not None:
            return self._cache
        data: dict[str, dict] = {}
        try:
            raw = json.loads(self._cache_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data = {str(k): v for k, v in raw.items() if isinstance(v, dict)}
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            logger.debug("nanami_persona: 判官缓存读不出来，当作空的", exc_info=True)
        self._cache = data
        return data

    def _cache_get(self, key: str) -> bool | None:
        with self._lock:
            row = self._load().get(key)
        if not row:
            return None
        if time.time() - float(row.get("at") or 0) > CACHE_TTL_SEC:
            return None
        return bool(row.get("flag"))

    def _cache_put(self, key: str, flag: bool) -> None:
        with self._lock:
            data = self._load()
            data[key] = {"flag": bool(flag), "at": time.time()}
            if len(data) > CACHE_MAX_ENTRIES:
                # 删最老的，别让缓存无限长
                ordered = sorted(data.items(), key=lambda item: float(item[1].get("at") or 0))
                for old_key, _ in ordered[: len(data) - CACHE_MAX_ENTRIES]:
                    data.pop(old_key, None)
            try:
                self._cache_path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self._cache_path.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, self._cache_path)
            except OSError:
                logger.debug("nanami_persona: 判官缓存落盘失败", exc_info=True)


def _parse_flag(content: str, field: str) -> bool | None:
    """从模型输出里抠出 `{field: true/false}`。抠不出来就 None（= fail-open）。"""
    text = str(content or "").strip()
    if not text:
        return None
    for candidate in _json_candidates(text):
        try:
            parsed = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict) and field in parsed:
            value = parsed[field]
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                lowered = value.strip().lower()
                if lowered in ("true", "1", "yes", "是", "命中"):
                    return True
                if lowered in ("false", "0", "no", "否", "不是"):
                    return False
    return None


def _json_candidates(text: str):
    start = text.find("{")
    while start >= 0:
        depth = 0
        for index in range(start, len(text)):
            char = text[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    yield text[start : index + 1]
                    break
        start = text.find("{", start + 1)


def _default_adapter() -> Any:
    """宿主的 adapter：优先用正在跑的那个，其次按 api.yaml 现场造一个。"""
    try:
        from application.runtime.context import try_get_app_runtime

        runtime = try_get_app_runtime()
        manager = getattr(runtime, "llm_manager", None) if runtime is not None else None
        adapter = getattr(manager, "llm_adapter", None)
        if adapter is not None:
            return adapter
    except Exception:
        pass

    from ai.llm.adapter_factory import LLMAdapterFactory
    from config.config_manager import ConfigManager

    manager = ConfigManager()
    provider, model, base_url, api_key = manager.get_llm_api_config()
    if not provider:
        return None
    return LLMAdapterFactory.create_adapter(
        **manager.merged_llm_factory_kwargs(
            provider,
            {"llm_provider": provider, "api_key": api_key, "base_url": base_url, "model": model},
        )
    )
