"""出处（provenance）模块（规格 v6 三期 §Implementation·三期的账号解析）。

- **视频 ID ← 会话链接**（`video_id_of`，纯函数）：B站 `BV` 号 / YouTube `v=`；非视频链接或空 → `""`。
- **账号 ← 视频 ID**（`resolve_account`）：`GET api.bilibili.com/x/web-interface/view?bvid=<BV>`
  取 `data.owner.name` / `data.owner.mid`。**只对 B站启用**。
- 出网方式照抄 `dict_sources.py` 的可注入 opener：薄封装 `urlopen_call`（调用时才取
  `urllib.request.urlopen`）+ 接受 `opener=` 注入，测试才能 monkeypatch 钉住「零请求」。
- 超时 / 限流 / 被墙 / 坏 JSON → **静默返回空**，异常不向上抛（这是异步 enrichment，不进捕获关键路径）。
- **响应体大小（票 #91）**：`view` 响应随视频简介增长（实测 `BV1Tean6pEdV` = 569,832 B），
  所以**流式读到 EOF**，只在超过总量上限 `MAX_RESPONSE_BYTES`（防内存炸弹）时放弃；
  「响应超限」记一条 WARNING，与网络失败区分开，不再静默当解析失败。
- 本模块不改扩展上报契约（ADR-0004 不动）：视频 ID 只从会话链接里正则取，不要求扩展报新字段。

小红书 / YouTube 的账号解析是 Out of Scope（无可免鉴权的公开接口）。
"""

from __future__ import annotations

import json
import logging
import re
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

# 与 `dict_sources.py` 同款出网常量（本模块只依赖标准库，不 import dict_sources）。
USER_AGENT = "english-capture-hub/2.0 (+local, single user)"
# 响应总量上限（票 #91）：**只防内存炸弹**，不是分页/截断点。`view` 响应随简介长度增长
# （实测最长 569,832 B），旧的 512 KiB 会把 JSON 从字符串中间切断 → 解析失败丢账号。
# 放宽到 8 MiB 覆盖任何正常响应，同时挡住病态响应把内存吃满。
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
READ_CHUNK_BYTES = 64 * 1024
DEFAULT_TIMEOUT_S = 3.0  # §三期：单次取数预算（异步后台，超时随时静默降级）

BILIBILI_VIEW_API = "https://api.bilibili.com/x/web-interface/view"

# B站 BV 号：`BV` + 10 位 [0-9A-Za-z]（实测语料 `BV1Uu4y1E7ux` 等 8 条全命中）。
_BV_RE = re.compile(r"BV[0-9A-Za-z]{10}")
# YouTube 视频 id：11 位 base64url 字符（只认 `youtube.com` 上的 `v=` 参数）。
_YT_ID_RE = re.compile(r"[A-Za-z0-9_-]{11}")


def _host_is(host: str, domain: str) -> bool:
    """host 命中域名（含子域）：`www.youtube.com` 命中 `youtube.com`。"""
    host = host.lower()
    return host == domain or host.endswith("." + domain)


def video_id_of(url: Any) -> str:
    """会话链接 → 视频 ID：B站 `BV` 号 / YouTube `v=`；非视频链接或空 → `""`（纯函数）。

    两个分支都先校验 host（票 #96 A4）：BV 号只在 `bilibili.com`（含子域）上认，
    YouTube id 只在 `youtube.com` 上认。否则 `https://example.com/?q=BV1Uu4y1E7ux`
    这种链接也会放行，把别人的视频 ID 当自己的发出去。
    """
    if not isinstance(url, str) or not url:
        return ""
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ""
    if _host_is(host, "bilibili.com"):
        match = _BV_RE.search(url)
        if match:
            return match.group(0)
    if _host_is(host, "youtube.com"):
        value = urllib.parse.parse_qs(parsed.query).get("v", [""])[0]
        if _YT_ID_RE.fullmatch(value):
            return value
    return ""


def account_of(payload: Any) -> dict[str, str]:
    """B站 `view` 接口响应 → `{"account_name", "account_id"}`；拿不到账号 → `{}`（纯函数）。"""
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    owner = data.get("owner") if isinstance(data, dict) else None
    if not isinstance(owner, dict):
        return {}
    name = owner.get("name")
    if not isinstance(name, str) or not name.strip():
        return {}
    mid = owner.get("mid")
    return {"account_name": name.strip(), "account_id": "" if mid is None else str(mid)}


def urlopen_call(request: urllib.request.Request, timeout: float) -> Any:
    """薄封装：`urllib.request.urlopen` 在**调用时**取属性，测试才能 monkeypatch 钉住「零请求」。"""
    return urllib.request.urlopen(request, timeout=timeout)


class ResponseTooLarge(ValueError):
    """响应体超过 `MAX_RESPONSE_BYTES`（票 #91）：与网络失败 / 坏 JSON 分开的一类失败。"""


def _read_all(response: Any, limit: int = MAX_RESPONSE_BYTES) -> bytes:
    """流式读到 EOF；总量超 `limit` → `ResponseTooLarge`（票 #91：先读满，超限才放弃）。"""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise ResponseTooLarge(total)
        chunks.append(chunk)
    return b"".join(chunks)


def is_bilibili_video_id(value: Any) -> bool:
    """整串就是一个 BV 号（不认整条链接、不认 YouTube id）——网络取数只对 B站开闸。"""
    return isinstance(value, str) and _BV_RE.fullmatch(value) is not None


def resolve_account(
    video_id: Any,
    *,
    opener: Callable[..., Any] = urlopen_call,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> dict[str, str]:
    """视频 ID → `{"account_name", "account_id"}`；非 B站 / 任何失败 → `{}`（静默，不抛）。"""
    if not is_bilibili_video_id(video_id):
        return {}
    url = f"{BILIBILI_VIEW_API}?bvid={urllib.parse.quote(video_id)}"
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
    )
    try:
        with opener(request, timeout) as response:
            raw = _read_all(response)
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except ResponseTooLarge as exc:  # 票 #91：不是「网络失败」，是确定性超限 —— 记一条 WARNING
        log.warning(
            "账号解析放弃 %s：响应超 %d 字节总量上限（实读 >%s）",
            video_id,
            MAX_RESPONSE_BYTES,
            exc,
        )
        return {}
    except Exception:  # noqa: BLE001 - 超时 / HTTP / 网络 / 坏 JSON：异步 enrichment 一律静默降级
        return {}
    return account_of(payload)
