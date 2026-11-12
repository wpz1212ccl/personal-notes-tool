"""云端语音转文字（v36 ASR P1）：硅基流动 SenseVoiceSmall 最小客户端。

事实来源（2026-11-07 端到端实测，`docs/research/v33-capture-routes/README.md` §5/§8）：

- `POST https://api.siliconflow.cn/v1/audio/transcriptions`，Bearer 鉴权、OpenAI 兼容；
- multipart 字段 `model` + `file`，**m4s 原样直传**（零转码，14.8 分钟音频 12.1 秒出全文）；
- 响应只有 `{"text": "..."}` —— **无时间戳、无分段**（P2 对齐的前提，本期不处理）；
- 单次 ≤1 小时 / ≤50MB；免费档需实名，429 限流与模型上下线由服务方决定。

网络层可注入：HTTP POST 走 `post(url, body, headers) -> (status, body)`（默认 urllib），
测试传假 post 全程离线。日志纪律（store §10 同款）：只记音频字节数、文本长度、
耗时与状态码，**不记转录文本本身**。
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from typing import Any

log = logging.getLogger("hub.asr_client")

ENDPOINT = "https://api.siliconflow.cn/v1/audio/transcriptions"
DEFAULT_MODEL = "FunAudioLLM/SenseVoiceSmall"
# 15 分钟音频实测 12.1 秒（研究 §8）；长片上界 1 小时，留足余量。
DEFAULT_TIMEOUT_S = 180

# 结果 `reason`：None=成功；其余为可报给用户的失败码。
REASON_AUTH = "auth"  # 401/403：key 无效、未实名或权限不足
REASON_RATE = "rate_limit"  # 429：免费档限流
REASON_TOO_BIG = "too_big"  # 413：超过单次上限
REASON_EMPTY = "empty"  # 200 但没有文本（静音 / 纯音乐）
REASON_ERROR = "error"  # 网络 / 解析 / 其它非 200

# (状态码, 响应体)；HTTP 错误也按状态码回给上层，不抛。
Post = Callable[[str, bytes, Mapping[str, str]], tuple[int, bytes]]


def default_post(
    url: str, body: bytes, headers: Mapping[str, str]
) -> tuple[int, bytes]:
    """生产网络层：urllib POST；`HTTPError` 自带状态码与响应体，原样回上层。"""
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    try:
        with urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT_S) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        with exc:
            return exc.code, exc.read()


def build_multipart(
    fields: Mapping[str, str],
    *,
    file_name: str,
    file_field: str,
    file_data: bytes,
    boundary: str,
) -> bytes:
    """手工拼 multipart/form-data（纯函数）：文本字段在前、文件字段最后。

    OpenAI 兼容端点收标准 multipart；`Content-Type` 只写 `application/octet-stream`
    不猜 m4s 的 MIME（服务端按内容识别，研究 §8 实测可行）。
    """
    lines: list[bytes] = []
    for name, value in fields.items():
        lines.append(f"--{boundary}\r\n".encode("utf-8"))
        lines.append(
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8")
        )
        lines.append(str(value).encode("utf-8"))
        lines.append(b"\r\n")
    lines.append(f"--{boundary}\r\n".encode("utf-8"))
    lines.append(
        (
            f'Content-Disposition: form-data; name="{file_field}"; '
            f'filename="{file_name}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode("utf-8")
    )
    lines.append(file_data)
    lines.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    return b"".join(lines)


def parse_transcript(payload: Any) -> str | None:
    """从响应 JSON 取 `text`（纯函数）；形状不对或空文本 → None。

    SenseVoice 的输出可能带情绪标签/emoji 混入（研究 §8），P1 原样返回，
    清洗与分句留给 P2 的合池对齐。
    """
    if not isinstance(payload, Mapping):
        return None
    text = payload.get("text")
    if not isinstance(text, str):
        return None
    return text.strip() or None


def transcribe(
    audio: bytes,
    *,
    api_key: str,
    model: str = DEFAULT_MODEL,
    filename: str = "audio.m4s",
    post: Post | None = None,
) -> dict[str, Any]:
    """音频字节 → 转录全文。

    返回 `{"reason", "text", "ms", "status"}`。故障不抛：调用方按 `reason` 降级
    （与 `cc_track.collect` / `asr_fetch.fetch_audio` 同一纪律）。
    """
    started = time.perf_counter()
    key = str(api_key or "").strip()
    if not key:
        log.info("asr 未配置 key，跳过转录")
        return {"reason": REASON_AUTH, "text": "", "ms": 0, "status": 0}
    if not audio:
        log.info("asr 音频为空，跳过转录")
        return {"reason": REASON_ERROR, "text": "", "ms": 0, "status": 0}
    do_post: Post = post if post is not None else default_post
    boundary = f"----asr{uuid.uuid4().hex}"
    body = build_multipart(
        {"model": model},
        file_name=filename,
        file_field="file",
        file_data=audio,
        boundary=boundary,
    )
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    }
    try:
        status, raw = do_post(ENDPOINT, body, headers)
    except Exception as exc:  # noqa: BLE001 - 网络故障静默降级（spec §B2c）
        log.warning("asr 转录请求失败（%s）", type(exc).__name__)
        return {"reason": REASON_ERROR, "text": "", "ms": _elapsed_ms(started), "status": 0}
    ms = _elapsed_ms(started)
    if status != 200:
        reason = _reason_for_status(status)
        log.warning("asr 转录被拒（HTTP %d，%d 字节音频）", status, len(audio))
        return {"reason": reason, "text": "", "ms": ms, "status": status}
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        log.warning("asr 响应不是合法 JSON（%d 字节）", len(raw))
        return {"reason": REASON_ERROR, "text": "", "ms": ms, "status": status}
    text = parse_transcript(payload)
    if text is None:
        log.info("asr 转录完成但无文本（%d 字节音频，%d ms）", len(audio), ms)
        return {"reason": REASON_EMPTY, "text": "", "ms": ms, "status": status}
    log.info(
        "asr 转录完成（音频 %d 字节，文本 %d 字符，%d ms）", len(audio), len(text), ms
    )
    return {"reason": None, "text": text, "ms": ms, "status": status}


def _elapsed_ms(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)


def _reason_for_status(status: int) -> str:
    if status in (401, 403):
        return REASON_AUTH
    if status == 429:
        return REASON_RATE
    if status == 413:
        return REASON_TOO_BIG
    return REASON_ERROR
