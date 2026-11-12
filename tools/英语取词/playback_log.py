"""播放链路事件日志（2026-11-06：补「卡顿/中断查不到现场」的缺口）。

`hub.log` 只记服务端自己的动作（HTTP 访问行、合成耗时、连接池），播放器发生在
浏览器里：缓冲卡住、音频报错、请求超时被 AbortController 砍掉、连播停在第几段 ——
这些以前一个字都不落盘，出事只能靠时间戳反推。本模块提供唯一的上报出口：

    POST /api/playback/log  {"events": [{"event": "tts_timeout", "ms": 10012, ...}]}
        -> {"ok": true, "accepted": N}   写 logs/playback.log

设计纪律（沿用 §8 脱敏口径）：

- **只记元数据**：事件名、耗时、状态码、错误原因码、段/片序号、后端、字节数、会话 id；
  绝不记文本、URL、标题、请求体、响应内容。前端不上报这些字段，服务端再按白名单
  过滤一次 —— 即便以后有人误加了字段，也写不进日志。
- **独立文件 + 独立滚动**：2MB × 3，不污染 `hub.log`（hub.log 的访问行会被高频
  播放事件冲掉）。`propagate=False` 保证不重复进 root 的 hub.log handler。
- **宁丢不炸**：非法事件、超长字段、超量批量一律丢弃/截断，上报失败绝不影响播放。
  清理与滚动交给 `maintenance.py`，这里只管写。
"""

from __future__ import annotations

import logging
import logging.handlers
import re
from pathlib import Path
from typing import Any

LOGGER_NAME = "playback"
LOG_FILE_NAME = "playback.log"
MAX_BYTES = 2 * 1024 * 1024
BACKUPS = 3

# 一次上报最多多少条；前端本来就批量攒着发，这个上限只是防呆/防滥用。
MAX_EVENTS_PER_REQUEST = 50
MAX_STR_LEN = 64
MAX_INT_VALUE = 10**9

# 只允许这些事件名。前端要加新事件，必须同时在这里登记 —— 避免任意字符串
# 被写进日志（也能顺便挡住把用户内容误当事件名传上来）。
EVENTS = frozenset(
    {
        "tts_ok",
        "tts_fail",
        "tts_timeout",
        "chunk_play",
        "chunk_ended",
        "chunk_error",
        "play_rejected",
        "audio_error",
        "audio_stalled",
        "article_start",
        "article_finish",
        "article_stop",
        "article_abort",
        # v32：文章连播跨段预热窗口，每段首片报一次（size=窗口内片数）。
        "article_prime",
    }
)

# 短字符串字段：值只允许 ASCII 标识符形状（如 abort / http / edge / sapi / 随机 sid）。
_STR_FIELDS = frozenset({"event", "reason", "backend", "sid", "cache"})
# 整数字段：耗时、HTTP 状态、MediaError.code、段/片号、字节数、序号、计划段数。
_INT_FIELDS = frozenset(
    {"ms", "status", "code", "para", "chunk", "size", "seq", "total", "pos"}
)
# 输出顺序固定，方便 grep / 人工扫。
_FIELD_ORDER = (
    "event", "sid", "seq", "ms", "status", "reason", "code", "cache",
    "backend", "para", "chunk", "pos", "total", "size",
)
_SAFE_STR = re.compile(r"^[A-Za-z0-9_.:-]*$")

log = logging.getLogger(LOGGER_NAME)


def setup(log_dir: str | Path, enabled: bool = True) -> None:
    """幂等地挂上 `playback.log` 的滚动 handler；`log_enabled=0` 时静默丢弃。

    幂等很重要：`--selftest` / 测试里可能反复调 `setup_logging()`，重复挂 handler
    会把同一条事件写两遍。用自定义属性认领自己的 handler，别人的 handler 不动。
    """
    for handler in log.handlers:
        if getattr(handler, "_playback_log", False):
            log.disabled = not enabled
            return
    if not enabled:
        log.addHandler(logging.NullHandler())
        log.disabled = True
        return
    directory = Path(log_dir)
    directory.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        directory / LOG_FILE_NAME,
        maxBytes=MAX_BYTES,
        backupCount=BACKUPS,
        encoding="utf-8",
    )
    handler._playback_log = True  # type: ignore[attr-defined]
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.setLevel(logging.INFO)
    log.propagate = False
    log.disabled = False
    log.addHandler(handler)


def _clean_event(item: Any) -> dict[str, Any] | None:
    """把一条上报洗成白名单元数据；不合规直接丢（不猜、不回显）。"""
    if not isinstance(item, dict):
        return None
    name = item.get("event")
    if not isinstance(name, str) or name not in EVENTS:
        return None
    cleaned: dict[str, Any] = {}
    for key in _STR_FIELDS:
        value = item.get(key)
        if not isinstance(value, str) or isinstance(value, bool):
            continue
        text = value[:MAX_STR_LEN]
        if _SAFE_STR.fullmatch(text):
            cleaned[key] = text
    for key in _INT_FIELDS:
        value = item.get(key)
        # bool 是 int 子类，但不该被当成数值写进日志
        if not isinstance(value, int) or isinstance(value, bool):
            continue
        if 0 <= value <= MAX_INT_VALUE:
            cleaned[key] = value
    cleaned["event"] = name  # 事件名必须留着，且永远取自白名单
    return cleaned


def record(events: Any) -> int:
    """写一批事件，返回真正落盘的条数；任何异常都不许冒到 HTTP 层。"""
    if not isinstance(events, list):
        return 0
    written = 0
    for item in events[:MAX_EVENTS_PER_REQUEST]:
        cleaned = _clean_event(item)
        if cleaned is None:
            continue
        line = " ".join(
            f"{key}={cleaned[key]}" for key in _FIELD_ORDER if key in cleaned
        )
        log.info("%s", line)
        written += 1
    return written
