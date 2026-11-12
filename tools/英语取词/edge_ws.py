"""v11 §2（票 #110）：Edge 神经语音的 WebSocket 连接池。

`edge_tts.Communicate` 每次 `stream()` 都会新建 ClientSession + WebSocket，
首次握手要 0.9–1.1s（`docs/research/v11-pronunciation-latency.md` §3.1）；
本模块把连接留在池里复用，同一条连接上的轮次串行，后续每轮只要 230–350ms（§3.2）。
实测/规格约束（§2、§3.3）：

- 空闲约 28s 仍可用、约 60s 被服务端关闭；客户端 ping 在 21s 就被重置 →
  **不发 WebSocket ping**，空闲断掉后下一次请求自动重连一次；
- 同时最多 2 条连接（规格允许 2–3），空闲连接优先复用；
- 用户点击（`priority=0`）优先于后台预热（`priority=1`），同一连接上正在跑的
  一轮最多让用户多等一轮；
- `/warm` 只确保连接在建立/已建立，不合成、不返回音频。

实现要点：

- 一条专用 asyncio 事件循环线程（daemon），对外只暴露同步方法；hub 的 HTTP
  线程直接调用，线程间通过 `run_coroutine_threadsafe` 投递。
- WebSocket 协议、SSML 转义、DRM token 全部复用 `edge-tts==7.3.1` 的内部 helper：
  `communicate.mkssml` / `ssml_headers_plus_data` / `remove_incompatible_characters`
  / `get_headers_and_data`、`drm.DRM`、`data_classes.TTSConfig`。这些是
  `requirements.txt` 里 pin 住的私有接口，**升级 edge-tts 前要重新核对本文件**
  （协议字段、`Sec-MS-GEC` 版本、`audio-24khz-48kbitrate-mono-mp3` 输出格式）。
- 所有第三方依赖在首次使用时惰性导入；连接池构造时再用后台线程预导入一次
  （不阻塞启动），避免第一次点击把 aiohttp/edge-tts 的 import 成本算进 L2。
  edge-tts 没装时只让发音失败（`Speech` 降级 SAPI），不影响取词台启动。
- 日志只记连接事件与错误类型，绝不记用户文本（§8）。

v13 §3/§4（票 #118）追加的不变量：

- **空闲回收**：连接记 `last_used`（最近一次成功用到的时刻）；空闲超过
  `DEFAULT_IDLE_TTL_S`（20s，服务端约 27–35s 才关，取保守值）就在**派发新请求前**
  丢弃重建，绝不把请求写到可能已过期的连接上。`warm()` 只对「此刻确实可用」的连接
  返回 True，过期连接触发后台重建并返回 False（HTTP 仍 200）。
- **透明重试**：连接级瞬时失败（写失败 / 连接在 turn.end 前关闭 / 握手失败或超时）
  先在全新连接上重试当前请求一次；成功不降级，失败才抛给 `Speech` 进冷却。
- **按需第二条热连接**（v13 §5 / 票 #120）：`warm(connections=2)` 在后台把热连接补到
  2 条；不传就只保 1 条，绝不多开常驻空闲连接。
- 时序注入：构造参数 `clock`（默认 `time.monotonic`）与 `idle_ttl` 让单测用假时钟
  确定性验证回收，不依赖真实墙钟与网络。
"""

from __future__ import annotations

import asyncio
import heapq
import logging
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("hub")

# 优先级：数值越小越先出队。用户点击永远排在后台预热前面。
PRIORITY_USER = 0
PRIORITY_BACKGROUND = 1

# §2：同时最多 2 条连接（规格允许 2–3，取小的）。
DEFAULT_MAX_CONNECTIONS = 2
# v13 §3：连接/握手与单轮读取的超时；失败抛异常，由 `Speech` 按现有逻辑降级 SAPI。
DEFAULT_CONNECT_TIMEOUT_S = 10.0
DEFAULT_ROUND_TIMEOUT_S = 30.0
# v13 §3：服务端约 27–35s 空闲就关连接，客户端取 20s 保守阈值。
# 派发前先丢弃空闲超过阈值的连接并重建，绝不写到已知可能过期的连接上。
DEFAULT_IDLE_TTL_S = 20.0
# v13 §3/§5：连接用满一半寿命就由 `warm()` 轮换（换新连接、旧的仍能服务到过期）。
# 目的：页面活跃时「总有一条新鲜的可用连接」，点击永远不必现付握手；
# 阈值不超过前端心跳间隔（10s），这样每次心跳都能轮换最老的那条。
DEFAULT_REFRESH_TTL_S = 10.0
# 票 #188（D5）：`EdgePool.synthesize` 的 `future.result()` 兜底超时的富余量
# （connect + 2×round 之外再给的秒数）。模块常量只为测试可注入（monkeypatch）。
_RESULT_SLACK_S = 5.0

# §2：音频保持服务端返回的 24kHz/48kbps 单声道 MP3，不降码率。
OUTPUT_FORMAT = "audio-24khz-48kbitrate-mono-mp3"


@dataclass(frozen=True)
class _Runtime:
    """惰性导入的 edge-tts 7.3.1 内部 helper 与 aiohttp。"""

    aiohttp: Any
    ClientError: Any
    ClientResponseError: Any
    WSMsgType: Any
    TTSConfig: Any
    DRM: Any
    WSS_URL: str
    WSS_HEADERS: dict[str, str]
    SEC_MS_GEC_VERSION: str
    mkssml: Callable[..., str]
    ssml_headers_plus_data: Callable[..., str]
    remove_incompatible_characters: Callable[[str], str]
    get_headers_and_data: Callable[..., tuple[dict[bytes, bytes], bytes]]
    connect_id: Callable[[], str]
    date_to_string: Callable[[], str]
    escape: Callable[[str], str]
    ssl_context: Any


_RUNTIME: _Runtime | None = None
_RUNTIME_LOCK = threading.Lock()


def _runtime() -> _Runtime:
    """首次使用时才导入 edge-tts / aiohttp；缺依赖只影响发音。"""
    global _RUNTIME
    with _RUNTIME_LOCK:
        if _RUNTIME is None:
            from xml.sax.saxutils import escape

            import aiohttp
            from edge_tts.communicate import (
                _SSL_CTX,
                connect_id,
                date_to_string,
                get_headers_and_data,
                mkssml,
                remove_incompatible_characters,
                ssml_headers_plus_data,
            )
            from edge_tts.constants import SEC_MS_GEC_VERSION, WSS_HEADERS, WSS_URL
            from edge_tts.data_classes import TTSConfig
            from edge_tts.drm import DRM

            _RUNTIME = _Runtime(
                aiohttp=aiohttp,
                ClientError=aiohttp.ClientError,
                ClientResponseError=aiohttp.ClientResponseError,
                WSMsgType=aiohttp.WSMsgType,
                TTSConfig=TTSConfig,
                DRM=DRM,
                WSS_URL=WSS_URL,
                WSS_HEADERS=WSS_HEADERS,
                SEC_MS_GEC_VERSION=SEC_MS_GEC_VERSION,
                mkssml=mkssml,
                ssml_headers_plus_data=ssml_headers_plus_data,
                remove_incompatible_characters=remove_incompatible_characters,
                get_headers_and_data=get_headers_and_data,
                connect_id=connect_id,
                date_to_string=date_to_string,
                escape=escape,
                # 复用 edge_tts 导入时已建好的 SSLContext（二次 create 约 300ms）
                ssl_context=_SSL_CTX,
            )
        return _RUNTIME


_PRELOAD_GUARD = threading.Lock()
_preload_started = False


def _preload_runtime() -> None:
    """后台预导入；缺依赖时静默，等第一次真正合成再报 ImportError。"""
    try:
        _runtime()
    except Exception:  # noqa: BLE001, S110 - 导入失败只影响发音，首次真正合成再报
        pass


def _start_runtime_preload() -> None:
    global _preload_started
    with _PRELOAD_GUARD:
        if _preload_started:
            return
        _preload_started = True
    threading.Thread(
        target=_preload_runtime, name="tts-edge-preload", daemon=True
    ).start()


class _EdgeWS:
    """真实连接：aiohttp WebSocket + 它所属的 ClientSession（一起关）。"""

    def __init__(self, session: Any, websocket: Any) -> None:
        self._session = session
        self._ws = websocket

    async def send_str(self, data: str) -> None:
        await self._ws.send_str(data)

    def __aiter__(self) -> Any:
        return self._ws.__aiter__()

    @property
    def closed(self) -> bool:
        return bool(self._ws.closed)

    async def close(self) -> None:
        try:
            await self._ws.close()
        finally:
            await self._session.close()


@dataclass
class _Job:
    """一次合成请求（够池调度用；错误只带类型，不带文本）。"""

    text: str
    voice: str
    rate: str
    priority: int
    future: asyncio.Future[bytes]
    attempts: int = 0
    # v13 §4：透明重试开始时刻（只用于日志里的重试耗时，不带文本）
    retry_started: float | None = None


@dataclass
class _Conn:
    """池里的一条连接。`state`：connecting / idle / busy / dead。"""

    state: str = "connecting"
    ws: Any = None
    job: _Job | None = None
    task: asyncio.Task[None] | None = None
    # v13 §3：最近一次成功用到的时刻与建立时刻（`clock()` 秒）；回收日志要区分「年龄/空闲」。
    last_used: float = 0.0
    created_at: float = 0.0


class _PoolCore:
    """在专用事件循环里跑的全部状态；只有循环线程会碰它，不需要锁。"""

    def __init__(
        self,
        *,
        ws_factory: Callable[[], Awaitable[Any]],
        connect_timeout: float,
        round_timeout: float,
        max_connections: int,
        idle_ttl: float = DEFAULT_IDLE_TTL_S,
        refresh_ttl: float = DEFAULT_REFRESH_TTL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ws_factory = ws_factory
        self.connect_timeout = connect_timeout
        self.round_timeout = round_timeout
        self.max_connections = max_connections
        # v13 §3：空闲阈值、轮换阈值与时钟都可注入（单测用假时钟，不睡墙钟）
        self.idle_ttl = idle_ttl
        self.refresh_ttl = min(refresh_ttl, idle_ttl)
        self.clock = clock
        self._connections: list[_Conn] = []
        self._pending: list[tuple[int, int, _Job]] = []
        self._sequence = 0
        self._closed = False
        # 票 #182（D3）：未知 Path 文本帧的限频日志 —— 每个 Path 只记首次，别刷屏。
        self._unknown_paths: set[bytes] = set()
        # 跨线程只读快照：「此刻有可用连接」+ 最晚可用时刻（monotonic 秒）。
        # `warm()` 不等握手，直接读这对值；写只在循环线程，读可在任意线程。
        self.ready = threading.Event()
        self._ready_deadline = 0.0
        self._snapshot_lock = threading.Lock()

    # --- 时间与可用性 -----------------------------------------------------

    def now(self) -> float:
        """注入的单调时钟（任意线程可调）。"""
        return self.clock()

    def _publish_ready(self) -> None:
        """写跨线程快照：有可用连接 + 最晚可用时刻。

        idle 按 `last_used + idle_ttl` 算；busy 的连接正在跑一轮，期限按「上一轮结束 +
        空闲阈值 + 单轮超时」算 —— 它要么在单轮超时内结束，要么已经被丢掉，
        不会让 `warm()` 永久谎报「已连接」。
        """
        deadlines = [
            (conn.last_used + self.idle_ttl + self.round_timeout)
            if conn.state == "busy"
            else (conn.last_used + self.idle_ttl)
            for conn in self._connections
            if conn.state in ("idle", "busy")
        ]
        with self._snapshot_lock:
            if not deadlines:
                self._ready_deadline = 0.0
                self.ready.clear()
                return
            self._ready_deadline = max(deadlines)
            self.ready.set()

    def has_usable(self) -> bool:
        """此刻是否**确实**有可用连接 —— `warm()` 的返回值只认这个（v13 §3）。"""
        with self._snapshot_lock:
            return self.ready.is_set() and self.now() < self._ready_deadline

    # --- 调度 -------------------------------------------------------------

    def _push(self, job: _Job) -> None:
        self._sequence += 1
        heapq.heappush(self._pending, (job.priority, self._sequence, job))

    def _schedule(self) -> None:
        """把待办按优先级派给空闲连接；没有就建新连接（最多 max 条）。"""
        while self._pending and not self._closed:
            self._reap_idle()  # v13 §3：派发前先丢掉可能已过期的空闲连接
            idle = next(
                (conn for conn in self._connections if conn.state == "idle"), None
            )
            if idle is not None:
                _, _, job = heapq.heappop(self._pending)
                self._start_round(idle, job)
                continue
            # 有 warm 触发的连接正在握手：等它变成 idle，别为同一批请求再付一次握手。
            if any(
                conn.state == "connecting" and conn.job is None
                for conn in self._connections
            ):
                return
            if len(self._connections) >= self.max_connections:
                return
            _, _, job = heapq.heappop(self._pending)
            conn = _Conn(state="connecting", job=job, created_at=self.now())
            self._connections.append(conn)
            conn.task = asyncio.create_task(self._serve(conn))
            return

    def _reap_idle(self) -> None:
        """丢掉空闲超过阈值的连接（服务端可能已关）；关 socket 交给后台任务。"""
        now = self.now()
        stale = [
            conn
            for conn in self._connections
            if conn.state == "idle" and now - conn.last_used >= self.idle_ttl
        ]
        for conn in stale:
            log.info(
                "tts edge 空闲回收 idle_ms=%d age_ms=%d",
                round((now - conn.last_used) * 1000),
                round((now - conn.created_at) * 1000),
            )
            ws = self._forget(conn)
            if ws is not None:
                asyncio.create_task(_close_quietly(ws))
        if stale:
            self._publish_ready()

    def _rotate_aging(self) -> None:
        """v13 §3/§5：`warm()` 时把「还没过期但已经老了」的连接换掉一条。

        只换**一条**，而且必须还有另一条空闲连接能立刻服务点击 —— 这样页面活跃时
        总有一条新鲜的可用连接，点击不必等握手；也绝不会把唯一的可用连接换走。
        """
        now = self.now()
        serving = [
            conn
            for conn in self._connections
            if conn.state == "idle" and now - conn.last_used < self.idle_ttl
        ]
        if len(serving) < 2:
            return
        aging = [
            conn for conn in serving if now - conn.last_used >= self.refresh_ttl
        ]
        if not aging:
            return
        victim = min(aging, key=lambda conn: conn.last_used)
        log.info(
            "tts edge 热连接轮换 idle_ms=%d age_ms=%d",
            round((now - victim.last_used) * 1000),
            round((now - victim.created_at) * 1000),
        )
        ws = self._forget(victim)
        if ws is not None:
            asyncio.create_task(_close_quietly(ws))
        self._publish_ready()

    def _start_round(self, conn: _Conn, job: _Job) -> None:
        conn.state = "busy"
        conn.task = asyncio.create_task(self._run_round(conn, job))

    # --- 对外协程 ---------------------------------------------------------

    async def synthesize(
        self, text: str, voice: str, rate: str, priority: int
    ) -> bytes:
        if self._closed:
            raise RuntimeError("edge 连接池已关闭")
        job = _Job(
            text=text,
            voice=voice,
            rate=rate,
            priority=priority,
            future=asyncio.get_running_loop().create_future(),
        )
        self._push(job)
        self._schedule()
        return await job.future

    def warm_nowait(self, connections: int = 1) -> None:
        """确保前 `connections` 条热连接在建立/已建立；立即返回，不等握手（循环线程里执行）。

        v13 §3/§5：先把过期空闲连接丢掉再补；`connections=1` 是默认（用户点击那条），
        只有前端明确要第二条时才补第二条，不无条件多开一条常驻空闲连接。
        """
        if self._closed:
            return
        want = max(1, min(int(connections), self.max_connections))
        self._reap_idle()
        if want >= 2:
            # v13 §3/§5：要两条热连接时，顺手轮换「老了但还能用」的那条 ——
            # 另一条仍在服务，点击不必等握手。
            self._rotate_aging()
        live = sum(
            1
            for conn in self._connections
            if conn.state in ("idle", "busy", "connecting")
        )
        while live < want and len(self._connections) < self.max_connections:
            conn = _Conn(state="connecting", created_at=self.now())
            self._connections.append(conn)
            conn.task = asyncio.create_task(self._serve(conn))
            live += 1
        self._publish_ready()
        self._schedule()

    async def aclose(self) -> None:
        self._closed = True
        self.ready.clear()
        for conn in list(self._connections):
            conn.state = "dead"
            ws, conn.ws = conn.ws, None
            if ws is not None:
                try:
                    await ws.close()
                except Exception:  # noqa: BLE001, S110 - 关连接失败不影响退出
                    pass
            task = conn.task
            if task is not None and task is not asyncio.current_task():
                task.cancel()
        self._connections.clear()
        self._publish_ready()
        while self._pending:
            _, _, job = heapq.heappop(self._pending)
            self._fail(job, RuntimeError("edge 连接池已关闭"))

    # --- 连接与轮次 -------------------------------------------------------

    async def _serve(self, conn: _Conn) -> None:
        """建一条连接并发送 `speech.config`（每连接只发一次）；然后交给调度。"""
        try:
            conn.ws = await self._open_ws()
            async with asyncio.timeout(self.connect_timeout):
                await conn.ws.send_str(_speech_config())
        except Exception as exc:  # noqa: BLE001 - 连接失败：连接级失败可透明重试一次
            await self._connect_failed(conn, exc)
            return
        log.info("tts edge 连接已建立")
        if conn.job is not None:
            # 建连接时摘下的任务放回队列，让更高优先级（用户点击）先出队。
            self._push(conn.job)
            conn.job = None
        conn.state = "idle"
        conn.last_used = self.now()
        self._publish_ready()
        self._schedule()

    async def _connect_failed(self, conn: _Conn, exc: BaseException) -> None:
        """握手/建连失败（含超时）：连接级瞬时失败 → 全新连接透明重试一次。

        仍失败才把异常交给 `Speech`（由它决定冷却与 SAPI 兜底）；warm 出来的
        连接没有 job，失败就只丢连接，不报错。
        """
        job, conn.job = conn.job, None
        log.warning("tts edge 连接失败：%s", type(exc).__name__)
        await self._discard(conn)
        # v13 §4：4xx（403 的 token 过期已在 `_open_ws` 里就地处理）不是连接级瞬时失败，
        # 原样交给 `Speech`，不做透明重试。
        if _is_client_error(exc):
            self._fail(job, exc)
        elif job is not None and job.attempts < 1:
            job.attempts += 1
            job.retry_started = time.monotonic()
            log.info("tts edge 透明重试（连接级 %s）", type(exc).__name__)
            self._push(job)
        else:
            self._fail(job, exc)
        self._schedule()

    async def _open_ws(self) -> Any:
        runtime = _runtime()
        try:
            async with asyncio.timeout(self.connect_timeout):
                return await self.ws_factory()
        except runtime.ClientResponseError as exc:
            if exc.status != 403:
                raise
            # 403 = 时间戳 token 过期：按 edge-tts 的 DRM 规则修正时钟偏移后重试一次。
            runtime.DRM.handle_client_response_error(exc)
            log.info("tts edge token 过期，刷新后重试")
            async with asyncio.timeout(self.connect_timeout):
                return await self.ws_factory()

    async def _run_round(self, conn: _Conn, job: _Job) -> None:
        try:
            data = await self._round(conn, job)
        except TimeoutError as exc:
            # 单轮读超时不是连接级瞬时失败（重试只会让用户再多等一个超时）：直接降级。
            await self._discard(conn)
            self._fail(job, exc)
            self._schedule()
            return
        except ConnectionError as exc:
            # v13 §4：连接级瞬时失败（写失败 / turn.end 前关闭 / 传输错误）→
            # 先在**全新连接**上透明重试一次；成功不降级，失败才交给 Speech 进冷却。
            await self._discard(conn)
            if job.attempts < 1:
                job.attempts += 1
                job.retry_started = time.monotonic()
                log.info("tts edge 透明重试（%s）", type(exc).__name__)
                self._push(job)
                self._schedule()
            else:
                log.warning("tts edge 透明重试仍失败（%s）", type(exc).__name__)
                self._fail(job, exc)
                self._schedule()
            return
        except Exception as exc:  # noqa: BLE001 - 协议/服务端异常同样由 Speech 降级
            await self._discard(conn)
            self._fail(job, exc)
            self._schedule()
            return
        conn.state = "idle"
        conn.last_used = self.now()
        self._publish_ready()
        if job.retry_started is not None:
            log.info(
                "tts edge 透明重试成功 ms=%d",
                round((time.monotonic() - job.retry_started) * 1000),
            )
        if not job.future.done():
            job.future.set_result(data)
        self._schedule()

    async def _round(self, conn: _Conn, job: _Job) -> bytes:
        runtime = _runtime()
        ws = conn.ws
        ssml = _ssml(job.text, job.voice, job.rate)
        try:
            async with asyncio.timeout(self.round_timeout):
                await ws.send_str(ssml)
                return await self._read_turn(ws)
        except TimeoutError:
            raise
        except ConnectionError:
            raise
        except Exception as exc:
            # aiohttp / 底层传输错误都等价于「这条连接不能用了」，交给上层重连。
            if isinstance(exc, (runtime.ClientError, OSError)):
                raise ConnectionError(type(exc).__name__) from exc
            raise

    async def _read_turn(self, ws: Any) -> bytes:
        runtime = _runtime()
        audio = bytearray()
        ended = False
        async for received in ws:
            if received.type == runtime.WSMsgType.TEXT:
                encoded = received.data.encode("utf-8")
                header_length = encoded.find(b"\r\n\r\n")
                if header_length < 0:
                    raise RuntimeError("edge 响应缺少头部")
                headers, _ = runtime.get_headers_and_data(encoded, header_length)
                path = headers.get(b"Path")
                if path == b"turn.end":
                    ended = True
                    break
                if path in (b"audio.metadata", b"response", b"turn.start"):
                    continue
                # 票 #182（D3）：上游新增消息类型不再硬失败 —— 否则一种新文本帧
                # 就让每次合成都抛错 → 冷却 → 全部发音降级 SAPI。限频记一次日志后
                # 跳过继续收音频；只有结构坏（缺头/坏二进制）保持硬失败。
                self._note_unknown_path(path)
                continue
            elif received.type == runtime.WSMsgType.BINARY:
                data = received.data
                if len(data) < 2:
                    raise RuntimeError("edge 二进制帧缺少头长度")
                header_length = int.from_bytes(data[:2], "big")
                if header_length > len(data):
                    raise RuntimeError("edge 二进制帧头长度越界")
                headers, chunk = runtime.get_headers_and_data(data, header_length)
                if headers.get(b"Path") != b"audio":
                    raise RuntimeError("edge 二进制帧不是音频")
                content_type = headers.get(b"Content-Type")
                if content_type is None:
                    if not chunk:  # 服务端在流结束时发的空帧，正常
                        continue
                    raise RuntimeError("edge 音频帧缺少 Content-Type")
                if content_type != b"audio/mpeg":
                    raise RuntimeError("edge 音频格式不是 audio/mpeg")
                if not chunk:
                    raise RuntimeError("edge 音频帧为空")
                audio.extend(chunk)
            elif received.type == runtime.WSMsgType.ERROR:
                raise ConnectionError("edge websocket error")
            elif received.type in (
                runtime.WSMsgType.CLOSE,
                runtime.WSMsgType.CLOSING,
                runtime.WSMsgType.CLOSED,
            ):
                break
        if not ended:
            raise ConnectionError("edge 连接在 turn.end 前关闭")
        if not audio:
            raise RuntimeError("edge 没有返回音频")
        return bytes(audio)

    def _note_unknown_path(self, path: bytes) -> None:
        """未知 Path 文本帧记一次日志：每个 Path 只记首见，不刷屏（票 #182 D3）。"""
        if path in self._unknown_paths:
            return
        self._unknown_paths.add(path)
        log.warning(
            "tts edge 未知文本帧 Path=%s，跳过继续收音频（每个 Path 只记一次）",
            path.decode("ascii", errors="replace"),
        )

    # --- 清理 -------------------------------------------------------------

    def _forget(self, conn: _Conn) -> Any:
        """把连接从池里摘掉（同步，不关 socket）；返回还需要关掉的 ws。"""
        conn.state = "dead"
        if conn in self._connections:
            self._connections.remove(conn)
        conn.task = None
        ws, conn.ws = conn.ws, None
        return ws

    async def _discard(self, conn: _Conn) -> None:
        ws = self._forget(conn)
        self._publish_ready()
        if ws is not None:
            await _close_quietly(ws)

    @staticmethod
    def _fail(job: _Job | None, exc: BaseException) -> None:
        if job is not None and not job.future.done():
            job.future.set_exception(exc)


async def _close_quietly(ws: Any) -> None:
    """关一条连接；失败静默（回收/重连/退出都不该被它打断）。"""
    try:
        await ws.close()
    except Exception:  # noqa: BLE001, S110 - 关连接失败不影响重连
        pass


def _is_client_error(exc: BaseException) -> bool:
    """是不是 aiohttp 的 4xx（非瞬时错误：不透明重试，直接交给 `Speech`）。"""
    status = client_response_status(exc)
    return status is not None and 400 <= status < 500


def client_response_status(exc: BaseException) -> int | None:
    """aiohttp `ClientResponseError` 的 HTTP status；其它异常（或 runtime 没导入
    —— 例外只能来自已导入的 runtime）回 None，调用方不凭猜测分档。"""
    runtime = _RUNTIME
    if runtime is None or not isinstance(exc, runtime.ClientResponseError):
        return None
    return int(getattr(exc, "status", 0) or 0)


class EdgePool:
    """对外同步接口：`synthesize(text, voice, rate, priority)` / `warm()` / `close()`。

    线程安全：任意线程都可调用；所有调度都投递到专用事件循环线程执行。
    线程在第一次真正使用时才启动（只构造 `Speech` 不发音不会多出线程）。
    构造时会在后台预导入 edge-tts/aiohttp，避免第一次点击把 import 成本算进 L2。
    """

    def __init__(
        self,
        *,
        ws_factory: Callable[[], Awaitable[Any]] | None = None,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_S,
        round_timeout: float = DEFAULT_ROUND_TIMEOUT_S,
        max_connections: int = DEFAULT_MAX_CONNECTIONS,
        idle_ttl: float = DEFAULT_IDLE_TTL_S,
        refresh_ttl: float = DEFAULT_REFRESH_TTL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        _start_runtime_preload()
        self._factory = ws_factory or _default_ws_factory
        self._core = _PoolCore(
            ws_factory=self._factory,
            connect_timeout=connect_timeout,
            round_timeout=round_timeout,
            max_connections=max_connections,
            idle_ttl=idle_ttl,
            refresh_ttl=refresh_ttl,
            clock=clock,
        )
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._guard = threading.Lock()
        self._closed = False

    # --- 同步入口 ---------------------------------------------------------

    def synthesize(
        self,
        text: str,
        voice: str,
        rate: str = "+0%",
        priority: int = PRIORITY_USER,
    ) -> bytes:
        """合成一段文本；失败抛异常（`TimeoutError` / `ConnectionError` / 其它）。

        票 #188（D5）：`future.result()` 带**兜底超时**（connect + 2×round + 富余）。
        `aclose()` 取消正在跑的轮次时，轮次协程吃下 CancelledError 就没了 —— 它手里
        那个 `job.future` 永不 resolve，外层协程永远挂在 `await job.future` 上；没有
        兜底的话，关中枢时恰有点击，这个 HTTP 线程就永久阻塞。兜底只在极端竞态下
        触发：正常路径里单轮超时（round_timeout）先到、异常照原样上交。
        """
        if self._closed:
            raise RuntimeError("edge 连接池已关闭")
        loop = self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(
            self._core.synthesize(text, voice, rate, priority), loop
        )
        fallback = (
            self._core.connect_timeout + 2 * self._core.round_timeout + _RESULT_SLACK_S
        )
        try:
            return future.result(timeout=fallback)
        except TimeoutError:
            if future.done():
                raise  # 轮次自己抛的 TimeoutError（单轮读超时）：原样交给 Speech
            try:
                future.cancel()  # 尽力取消；循环可能已经停了，失败不影响退出
            except Exception:  # noqa: BLE001, S110 - 取消失败不影响退出
                pass
            log.warning("tts edge 合成兜底超时（%.1fs），放弃未落定的轮次", fallback)
            raise TimeoutError("edge 合成兜底超时") from None

    def warm(self, connections: int = 1) -> bool:
        """非阻塞地确保前 `connections` 条热连接在建立/已建立；返回**此刻**是否已有可用连接。

        - **不等握手**（§4：立即返回）；建立/回收都在后台连接池线程里做；
        - v13 §3：只对「此刻确实可用」的连接返回 True —— 空闲超过阈值的连接算不可用，
          顺手触发后台重建并返回 False（HTTP 仍 200，`connected` 只是参考）；
        - v13 §5：`connections=2` 才补第二条；不传就只保一条，不多开常驻空闲连接。
        """
        if self._closed:
            return False
        loop = self._ensure_loop()
        # 幂等：够用就什么都不做；过期/缺失就在后台重建。
        loop.call_soon_threadsafe(self._core.warm_nowait, connections)
        return self._core.has_usable()

    def close(self) -> None:
        """关闭所有连接并停掉事件循环线程（测试与 hub 退出用）。"""
        with self._guard:
            loop, thread = self._loop, self._thread
        if loop is None:
            self._closed = True
            return
        if self._closed and (thread is None or not thread.is_alive()):
            return
        self._closed = True
        try:
            future = asyncio.run_coroutine_threadsafe(self._core.aclose(), loop)
            future.result(timeout=5)
        except Exception:  # noqa: BLE001, S110 - 退出路径上不再抛
            pass
        try:
            loop.call_soon_threadsafe(loop.stop)
        except RuntimeError:
            pass
        if thread is not None:
            thread.join(timeout=5)

    # --- 事件循环线程 -----------------------------------------------------

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._guard:
            if self._loop is None:
                loop = asyncio.new_event_loop()
                thread = threading.Thread(
                    target=_loop_main, args=(loop,), name="tts-edge", daemon=True
                )
                self._loop = loop
                self._thread = thread
                thread.start()
            return self._loop


def _loop_main(loop: asyncio.AbstractEventLoop) -> None:
    asyncio.set_event_loop(loop)
    try:
        loop.run_forever()
    finally:
        loop.close()


def _speech_config() -> str:
    """与 edge-tts 7.3.1 `Communicate.__stream` 完全一致的 speech.config。"""
    runtime = _runtime()
    return (
        f"X-Timestamp:{runtime.date_to_string()}\r\n"
        "Content-Type:application/json; charset=utf-8\r\n"
        "Path:speech.config\r\n\r\n"
        '{"context":{"synthesis":{"audio":{"metadataoptions":{'
        '"sentenceBoundaryEnabled":"true","wordBoundaryEnabled":"false"'
        '},"outputFormat":"' + OUTPUT_FORMAT + '"}}}}\r\n'
    )


def _ssml(text: str, voice: str, rate: str) -> str:
    """按 edge-tts 规则转义文本并拼 SSML（单次请求 ≤4000 字符由 Speech 把关）。"""
    runtime = _runtime()
    escaped = runtime.escape(runtime.remove_incompatible_characters(text))
    config = runtime.TTSConfig(voice, rate, "+0%", "+0Hz", "SentenceBoundary")
    return runtime.ssml_headers_plus_data(
        runtime.connect_id(),
        runtime.date_to_string(),
        runtime.mkssml(config, escaped),
    )


async def _default_ws_factory() -> _EdgeWS:
    """真实连接（首次调用才导入 edge-tts；握手超时 10s、单轮读超时 30s）。"""
    runtime = _runtime()
    session = runtime.aiohttp.ClientSession(
        trust_env=True,
        timeout=runtime.aiohttp.ClientTimeout(
            total=None,
            sock_connect=DEFAULT_CONNECT_TIMEOUT_S,
            sock_read=DEFAULT_ROUND_TIMEOUT_S,
        ),
    )
    try:
        websocket = await session.ws_connect(
            f"{runtime.WSS_URL}&ConnectionId={runtime.connect_id()}"
            f"&Sec-MS-GEC={runtime.DRM.generate_sec_ms_gec()}"
            f"&Sec-MS-GEC-Version={runtime.SEC_MS_GEC_VERSION}",
            compress=15,
            headers=runtime.DRM.headers_with_muid(runtime.WSS_HEADERS),
            ssl=runtime.ssl_context,
        )
    except BaseException:
        await session.close()
        raise
    return _EdgeWS(session, websocket)
