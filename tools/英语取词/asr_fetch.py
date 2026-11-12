"""B站音频流抓取（v36 ASR P1）：BV → dash.audio → m4s 字节。

事实来源（免登录链路，2026-11-07 真机实测）：
`docs/research/v33-capture-routes/README.md` §1 / §8：

- `view?bvid=` 免登录拿 `aid` / `cid`（多 P 用调用方传的 `cid`）；
- `playurl?fnval=16` 免登录回 `dash.audio[]`，URL **必须正规 JSON 解析后取
  `baseUrl`**——返回体里有转义，grep 抽 URL 会截断（研究 §1 坑位）；
- CDN 请求**必须带 `Referer: https://www.bilibili.com`**，否则回 XML `<Error>`；
- 音频是 fMP4（box 头 `ftyp`），**零转码**可直接喂云端转录。

网络层可注入：所有 HTTP 都走 `fetch(url, headers, range_bytes)`（默认 urllib），
测试传假网络层全程离线。日志纪律（store §10 同款）：只记状态码 / 字节数 / 原因码，
不记 URL、标题与音频内容。
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

log = logging.getLogger("hub.asr_fetch")

VIEW_API = "https://api.bilibili.com/x/web-interface/view"
PLAYURL_API = "https://api.bilibili.com/x/player/playurl"
DEFAULT_TIMEOUT_S = 15
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
# CDN 必带 Referer（研究 §1）；API 端点不需要，带上无害，统一最简。
REFERER = "https://www.bilibili.com"
# 云端单次上限 ≤50MB（研究 §5），超过就先截住，别白传一遍再被拒。
MAX_AUDIO_BYTES = 50 * 1024 * 1024

# 结果 `reason`：None=成功；其余为静默失败码（调用方据此降级，与 cc_track 同款）。
REASON_NO_AUDIO = "no_audio"  # playurl 无 dash.audio（直播 / 纯视频流等）
REASON_TOO_BIG = "too_big"  # 音频超过云端单次上限
REASON_ERROR = "error"  # 网络 / 接口 / 解析失败（含 API code != 0）

Headers = Mapping[str, str]
# (状态码, 响应体)；`range_bytes` 给出时请求体带 `Range: bytes=start-end`。
Fetch = Callable[[str, Headers | None, tuple[int, int] | None], tuple[int, bytes]]


def default_fetch(
    url: str,
    headers: Headers | None = None,
    range_bytes: tuple[int, int] | None = None,
) -> tuple[int, bytes]:
    """生产网络层：urllib + 调用方给的 header（测试经 `fetch=` 注入假网络层）。

    HTTP 错误不抛到调用方：`HTTPError` 自带状态码与响应体，原样回给上层判原因
    （206 也是合法成功，`HTTPError` 不会包 2xx）。
    """
    request_headers = {"User-Agent": USER_AGENT}
    request_headers.update(dict(headers or {}))
    if range_bytes is not None:
        start, end = range_bytes
        request_headers["Range"] = f"bytes={int(start)}-{int(end)}"
    request = urllib.request.Request(url, headers=request_headers)
    try:
        with urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT_S) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        with exc:
            return exc.code, exc.read()


def pick_audio_stream(audio: Any) -> dict[str, Any] | None:
    """从 `dash.audio[]` 里挑码率最高的一条（纯函数）；没有就 None。

    解码 / 转录都吃原始码流，码率高者音质好、识别更稳；游客可见档位由 B站决定
    （研究 §1：65–94 kbps 三档，对语音识别已够用）。
    """
    if not isinstance(audio, list):
        return None
    best: dict[str, Any] | None = None
    best_bandwidth = -1
    for stream in audio:
        if not isinstance(stream, Mapping):
            continue
        url = str(stream.get("baseUrl") or stream.get("base_url") or "").strip()
        if not url:
            continue
        try:
            bandwidth = int(stream.get("bandwidth") or 0)
        except (TypeError, ValueError):
            bandwidth = 0
        if bandwidth > best_bandwidth:
            best_bandwidth = bandwidth
            best = dict(stream)
    return best


def select_audio(
    bvid: str,
    *,
    cid: int | None = None,
    fetch: Fetch | None = None,
) -> dict[str, Any]:
    """`view` → `playurl` → 选流：拿音频 URL（不下载）。

    返回 `{"reason", "url", "bandwidth", "aid", "cid"}`；任何失败（网络断 / 接口
    变 / code != 0 / JSON 坏）都**静默**回原因码，绝不抛到调用方 —— 单一来源故障
    不许中断整链（spec §B2c 的同一纪律）。
    """
    do_fetch: Fetch = fetch if fetch is not None else default_fetch
    query_bvid = str(bvid or "").strip()
    empty: dict[str, Any] = {
        "reason": REASON_ERROR,
        "url": "",
        "bandwidth": 0,
        "aid": None,
        "cid": cid,
    }
    if not query_bvid:
        return empty
    try:
        # 第 1 步：view 拿 aid / cid
        query = urllib.parse.urlencode({"bvid": query_bvid})
        status, body = do_fetch(f"{VIEW_API}?{query}", None, None)
        if status != 200:
            log.warning("asr_fetch view 接口 HTTP %d，跳过取流", status)
            return empty
        view = json.loads(body.decode("utf-8"))
        if not isinstance(view, Mapping) or view.get("code") != 0:
            log.warning("asr_fetch view 接口 code 非 0，跳过取流")
            return empty
        data = view.get("data") or {}
        aid = data.get("aid")
        oid = int(cid) if cid is not None else data.get("cid")
        if not isinstance(aid, int) or not isinstance(oid, int):
            log.warning("asr_fetch view 响应缺 aid/cid，跳过取流")
            return empty
        # 第 2 步：playurl 拿 DASH（fnval=16 = DASH 形态，研究 §1）
        play_query = urllib.parse.urlencode(
            {
                "bvid": query_bvid,
                "cid": oid,
                "fnval": 16,
                "fnver": 0,
                "fourk": 1,
            }
        )
        status, body = do_fetch(f"{PLAYURL_API}?{play_query}", None, None)
        if status != 200:
            log.warning("asr_fetch playurl 接口 HTTP %d，跳过取流", status)
            return empty
        play = json.loads(body.decode("utf-8"))
        if not isinstance(play, Mapping) or play.get("code") != 0:
            log.warning("asr_fetch playurl 接口 code 非 0，跳过取流")
            return empty
        dash = (play.get("data") or {}).get("dash") or {}
        stream = pick_audio_stream(dash.get("audio"))
        if stream is None:
            log.info("asr_fetch 该分 P 无 dash.audio，静默跳过")
            return {**empty, "reason": REASON_NO_AUDIO, "aid": aid, "cid": oid}
        url = str(stream.get("baseUrl") or stream.get("base_url") or "").strip()
        try:
            bandwidth = int(stream.get("bandwidth") or 0)
        except (TypeError, ValueError):
            bandwidth = 0
        return {
            "reason": None,
            "url": url,
            "bandwidth": bandwidth,
            "aid": aid,
            "cid": oid,
        }
    except Exception as exc:  # noqa: BLE001 - 来源故障静默降级（spec §B2c）
        log.warning("asr_fetch 取流失败（%s），静默跳过", type(exc).__name__)
        return empty


def download_audio(
    url: str,
    *,
    fetch: Fetch | None = None,
    range_bytes: tuple[int, int] | None = None,
    max_bytes: int = MAX_AUDIO_BYTES,
) -> dict[str, Any]:
    """按 URL 下音频字节（带 Referer + UA）。

    `range_bytes` 给出时发 Range 请求，**206 与 200 都算成功**（研究 §1 用
    `bytes=0-262143` 验过 206）；`data` 从 0 起时校验 fMP4 的 `ftyp` box 头，
    形态不对按 `error` 处理，别把错误页当音频送去转录。
    """
    do_fetch: Fetch = fetch if fetch is not None else default_fetch
    headers = {"Referer": REFERER}
    if not str(url or "").strip():
        return {"reason": REASON_ERROR, "data": b"", "status": 0, "bytes": 0}
    try:
        status, data = do_fetch(url, headers, range_bytes)
        if status not in (200, 206):
            log.warning("asr_fetch 音频下载 HTTP %d，静默跳过", status)
            return {"reason": REASON_ERROR, "data": b"", "status": status, "bytes": 0}
        if not data:
            log.warning("asr_fetch 音频响应为空，静默跳过")
            return {"reason": REASON_ERROR, "data": b"", "status": status, "bytes": 0}
        starts_at_zero = range_bytes is None or int(range_bytes[0]) == 0
        if starts_at_zero and data[4:8] != b"ftyp":
            log.warning("asr_fetch 音频形态不是 fMP4（无 ftyp），静默跳过")
            return {"reason": REASON_ERROR, "data": b"", "status": status, "bytes": 0}
        if range_bytes is None and len(data) > max_bytes:
            log.warning("asr_fetch 音频 %d 字节超过单次上限 %d", len(data), max_bytes)
            return {"reason": REASON_TOO_BIG, "data": b"", "status": status, "bytes": len(data)}
        log.info("asr_fetch 音频下载完成（%d 字节，HTTP %d）", len(data), status)
        return {"reason": None, "data": data, "status": status, "bytes": len(data)}
    except Exception as exc:  # noqa: BLE001 - 网络故障静默降级（spec §B2c）
        log.warning("asr_fetch 音频下载失败（%s），静默跳过", type(exc).__name__)
        return {"reason": REASON_ERROR, "data": b"", "status": 0, "bytes": 0}


def fetch_audio(
    bvid: str,
    *,
    cid: int | None = None,
    fetch: Fetch | None = None,
    max_bytes: int = MAX_AUDIO_BYTES,
) -> dict[str, Any]:
    """组合入口：BV → 选流 → 下载，返回 m4s 字节与流信息。

    `{"reason", "data", "url", "bandwidth", "aid", "cid", "bytes"}`；`reason` 非
    None 时 `data` 为空，调用方直接按原因降级（与 `cc_track.collect` 同款契约）。
    """
    picked = select_audio(bvid, cid=cid, fetch=fetch)
    if picked["reason"] is not None:
        return {**picked, "data": b"", "bytes": 0}
    got = download_audio(picked["url"], fetch=fetch, max_bytes=max_bytes)
    return {**picked, **got, "reason": got["reason"]}
