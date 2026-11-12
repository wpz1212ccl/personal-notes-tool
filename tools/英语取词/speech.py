"""v11 §2/§4/§5/§8/§10 发音服务（票 #106 / #108 / #110）：Edge 神经语音 + 本机 SAPI 兜底。

本模块只提供一条缝：`Speech.synthesize(text, voice=None, rate=None) -> SynthResult`。
HTTP 层（`hub.py` 的 `POST /api/tts`）只依赖它，测试注入假 `edge_synth` / `sapi_synth`
即可不碰网络、也不初始化真实 COM。

- 默认 Edge 路径走 `edge_ws.EdgePool`（票 #110）：WebSocket 连接复用、轮次串行、
  空闲断线自动重连；`edge_synth` 仍可注入（注入后不创建连接池、不启动线程）。
- 缓存键 = `sha256(voice + "\\n" + rate + "\\n" + 规范化文本)`，文件 `<sha256>.mp3`；
  存的就是 Edge 返回的原始 `audio-24khz-48kbitrate-mono-mp3` 字节，**不降采样/不降码率**。
- 同键并发 single-flight：只有第一条合成，其余等它落盘后直接读缓存。
- 降级（§10 矩阵）：`tts_online=0` / Edge 失败 / 冷却窗口内 → 本机 SAPI 出 WAV，
  `cached=False`、**不落盘**；两者都不可用才 503（reason 沿用 Edge 的失败原因）。
- 冷却（v13 §4，票 #118）：从固定 60s 改成**指数退避** 8 → 16 → 32 → 60（封顶 60），
  成功一次即清零；后台 `priority=background` 的失败走**自己的**退避，绝不修改用户
  冷却截止时间，也不占 SAPI —— 后台预取失败只影响那一次预取。
- 预热（§3，票 #113）：`prewarm(texts)` 把一批文本塞进后台队列，worker 用
  `PRIORITY_BACKGROUND` 调 `synthesize` 落缓存；去重、上限 200、`tts_online=0` 零动作。
  用户点击仍是 `PRIORITY_USER`，同一连接上最多少等一轮。
- 隐私（§8）：日志只记 backend/cache/ms/len/voice，异常消息只带错误码；
  缓存文件名、临时文件名都是哈希/随机名，不含用户文本。
"""

from __future__ import annotations

import configparser
import contextlib
import hashlib
import logging
import os
import queue
import re
import struct
import tempfile
import threading
import time
import unicodedata
from collections import deque
from collections.abc import Callable, Iterator
from concurrent.futures import Future
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path
from typing import Any

from edge_ws import PRIORITY_BACKGROUND, PRIORITY_USER, EdgePool, client_response_status

log = logging.getLogger("hub")

# 与 hub.py / settings.py 同名的配置段；本模块只读这几个键，不写回。
CONFIG_SECTION = "英语取词"

DEFAULT_VOICE = "en-US-AriaNeural"
DEFAULT_RATE = "+0%"
DEFAULT_CACHE_MB = 256
MAX_TEXT_CHARS = 4000
# §3/§4（票 #113）：一次预热请求最多 40 条；后台队列上限 200（满了丢最旧的）。
MAX_PREWARM_ITEMS = 40
PREWARM_QUEUE_MAX = 200
PRUNE_EVERY_WRITES = 100
CACHE_TARGET_RATIO = 0.8
# 票 #188（D10）：`.tts-*.tmp` 孤儿只认「超过一天」的（正在写的临时文件不动）。
_TEMP_ORPHAN_AGE_S = 86400.0
# v13 §4（票 #118）：Edge 失败后的冷却改成指数退避 8 → 16 → 32 → 60 秒，封顶 60；
# 成功一次即清零；后台预取/预热有自己的一份计数，绝不影响用户冷却。
EDGE_COOLDOWN_STEPS: tuple[float, ...] = (8.0, 16.0, 32.0, 60.0)
# §2：SAPI 输出 SAFT22kHz16BitMono，裸 PCM 补 WAV 头时按这个采样率写。
SAPI_SAMPLE_RATE = 22050

# §4：只接受 en-US 神经语音白名单（顺序照规格；17 个）
VOICES: tuple[str, ...] = (
    "en-US-AvaNeural",
    "en-US-AndrewNeural",
    "en-US-EmmaNeural",
    "en-US-BrianNeural",
    "en-US-AnaNeural",
    "en-US-AndrewMultilingualNeural",
    "en-US-AriaNeural",
    "en-US-AvaMultilingualNeural",
    "en-US-BrianMultilingualNeural",
    "en-US-ChristopherNeural",
    "en-US-EmmaMultilingualNeural",
    "en-US-EricNeural",
    "en-US-GuyNeural",
    "en-US-JennyNeural",
    "en-US-MichelleNeural",
    "en-US-RogerNeural",
    "en-US-SteffanNeural",
)

# 设置页音色下拉的展示名 + 性别（只影响 UI，不改变白名单；顺序与 VOICES 一致）
VOICE_LABELS: dict[str, tuple[str, str]] = {
    "en-US-AvaNeural": ("Ava", "女"),
    "en-US-AndrewNeural": ("Andrew", "男"),
    "en-US-EmmaNeural": ("Emma", "女"),
    "en-US-BrianNeural": ("Brian", "男"),
    "en-US-AnaNeural": ("Ana", "女"),
    "en-US-AndrewMultilingualNeural": ("Andrew Multilingual", "男"),
    "en-US-AriaNeural": ("Aria", "女"),
    "en-US-AvaMultilingualNeural": ("Ava Multilingual", "女"),
    "en-US-BrianMultilingualNeural": ("Brian Multilingual", "男"),
    "en-US-ChristopherNeural": ("Christopher", "男"),
    "en-US-EmmaMultilingualNeural": ("Emma Multilingual", "女"),
    "en-US-EricNeural": ("Eric", "男"),
    "en-US-GuyNeural": ("Guy", "男"),
    "en-US-JennyNeural": ("Jenny", "女"),
    "en-US-MichelleNeural": ("Michelle", "女"),
    "en-US-RogerNeural": ("Roger", "男"),
    "en-US-SteffanNeural": ("Steffan", "男"),
}


def voice_choices() -> list[dict[str, str]]:
    """设置页下拉的 17 个音色（id + 展示名 + 性别）；不含任何用户内容。"""
    return [
        {"id": voice, "label": VOICE_LABELS[voice][0], "gender": VOICE_LABELS[voice][1]}
        for voice in VOICES
    ]

_RATE_PATTERN = re.compile(r"^[+-]\d{1,3}%$")

EdgeSynth = Callable[[str, str, str], bytes]
SapiSynth = Callable[[str], bytes]


@dataclass(frozen=True)
class SynthResult:
    """一次发音请求的结果（§4 的响应头与 §8 的日志都从这里取值）。"""

    data: bytes
    mime: str
    backend: str
    cached: bool
    ms: int


class SpeechError(Exception):
    """可映射到 §4 业务错误的异常；消息只有错误码，绝不含用户文本。"""

    def __init__(self, status: int, error: str, reason: str | None = None) -> None:
        super().__init__(error)
        self.status = status
        self.error = error
        self.reason = reason


def normalize_text(raw: str) -> str:
    """§4：去首尾空白、连续空白（含换行）折叠成一个空格、去控制字符。

    **不改大小写、不删句末标点。** 长度按规范化后的字符数计（`len`）。
    """
    kept = "".join(
        " "
        if char.isspace()
        else "" if unicodedata.category(char).startswith("C") else char
        for char in raw
    )
    return " ".join(kept.split())


def _read_settings(config_path: Path | None) -> dict[str, str]:
    """读现有 `config.ini` 的 `[英语取词]` 段；缺失/坏掉都返回空（只用内置默认值）。"""
    if config_path is None:
        return {}
    parser = configparser.ConfigParser(interpolation=None)
    try:
        found = parser.read(config_path, encoding="utf-8")
    except (OSError, configparser.Error):
        return {}
    if not found or not parser.has_section(CONFIG_SECTION):
        return {}
    return dict(parser[CONFIG_SECTION])


def _config_switch(settings: dict[str, str], key: str, default: bool) -> bool:
    raw = settings.get(key)
    if raw is None:
        return default
    try:
        return int(str(raw).split(";", 1)[0].strip()) != 0
    except ValueError:
        return default


def _config_cache_mb(settings: dict[str, str]) -> int:
    try:
        value = int(str(settings.get("tts_cache_mb", "")).split(";", 1)[0].strip())
    except ValueError:
        return DEFAULT_CACHE_MB
    return value if value > 0 else DEFAULT_CACHE_MB


def _voice_or_default(value: Any) -> str:
    return value if isinstance(value, str) and value in VOICES else DEFAULT_VOICE


def _rate_or_default(value: Any) -> str:
    if isinstance(value, str) and _RATE_PATTERN.fullmatch(value):
        return value
    return DEFAULT_RATE


def _elapsed_ms(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)


class Speech:
    """发音服务：校验 → 缓存 → single-flight → Edge 合成 → 原子落盘；失败退 SAPI。

    构造参数（都可注入，测试用 `tmp_path` 与假 `edge_synth` / `sapi_synth`）：
    - `cache_dir`：缓存目录（生产 = capture.db 同级的 `data/tts/`）；
    - `config_path`：读 `tts_online` / `tts_voice` / `tts_rate` / `tts_cache_mb`；
    - `online` / `voice` / `rate` / `cache_mb`：显式参数优先于配置文件；
    - `edge_synth(text, voice, rate) -> bytes`：Edge 合成缝（默认走连接池）；
      注入后不再创建连接池，`warm()` 也不会假装已连接；
    - `edge_pool`：连接池缝（默认 `edge_ws.EdgePool`，测试注入假池）；
    - `sapi_synth(text) -> bytes`：SAPI 合成缝（默认走本机 COM，测试一律注入假实现）。
    """

    def __init__(
        self,
        cache_dir: str | Path,
        *,
        config_path: str | Path | None = None,
        online: bool | None = None,
        voice: str | None = None,
        rate: str | None = None,
        cache_mb: int | None = None,
        edge_synth: EdgeSynth | None = None,
        edge_pool: Any | None = None,
        sapi_synth: SapiSynth | None = None,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        settings = _read_settings(Path(config_path) if config_path is not None else None)
        # 活的属性：`Hub.reload_switches()` 与后续设置热生效都只改这一个旗标
        self.online_enabled = (
            bool(online)
            if online is not None
            else _config_switch(settings, "tts_online", True)
        )
        self.voice = _voice_or_default(
            voice if voice is not None else settings.get("tts_voice")
        )
        self.rate = _rate_or_default(
            rate if rate is not None else settings.get("tts_rate")
        )
        self.cache_mb = (
            cache_mb
            if isinstance(cache_mb, int) and cache_mb > 0
            else _config_cache_mb(settings)
        )
        # 票 #110：默认 Edge 路径 = 连接池（用户点击 priority=0）；
        # 注入 `edge_synth` 的旧测试继续用假回调，不创建连接池/线程。
        if edge_synth is not None:
            self._edge_pool: Any | None = None
            self._edge_synth: EdgeSynth | None = edge_synth
        else:
            self._edge_pool = edge_pool if edge_pool is not None else EdgePool()
            self._edge_synth = None
        self._sapi_synth: SapiSynth = sapi_synth or _sapi_synth_via_com
        # Edge 不健康截止时间（`time.monotonic()` 秒）；测试把它拨到 0 即窗口过期。
        # v13 §4：`edge_retry_at` 只管**用户点击**；后台预取/预热有自己的退避。
        self.edge_retry_at = 0.0
        self._edge_fail_streak = 0  # 用户路径连续失败次数（决定退避档位）
        self._bg_retry_at = 0.0  # 后台路径的冷却截止时间（不影响用户）
        self._bg_fail_streak = 0
        # §2/§10：不健康窗口到点后由后台线程试探恢复（不等用户下一次点击付冷连接）。
        self._edge_probe_guard = threading.Lock()
        self._edge_probe_thread: threading.Thread | None = None
        self._locks_guard = threading.Lock()
        self._key_locks: dict[str, tuple[threading.Lock, int]] = {}
        self._writes = 0
        # §3 预热队列（票 #113）：全部是后台低优先级任务；`_prewarm_keys` 按缓存键去重。
        self._prewarm_lock = threading.Lock()
        self._prewarm_queue: deque[tuple[str, str, str]] = deque()
        self._prewarm_keys: set[str] = set()
        self._prewarm_thread: threading.Thread | None = None
        self._prune()

    # --- 对外入口 ---------------------------------------------------------

    def synthesize(
        self,
        text: Any,
        voice: Any = None,
        rate: Any = None,
        *,
        priority: int = PRIORITY_USER,
    ) -> SynthResult:
        """合成 / 取缓存。`priority` 决定 Edge 出队顺序与**失败隔离**（v13 §2/§4）：
        用户点击 = `PRIORITY_USER`（默认），后台预取/预热 = `PRIORITY_BACKGROUND`。
        后台失败只走自己的退避，不改用户冷却、也不占 SAPI。"""
        started = time.perf_counter()
        normalized = self._validated_text(text, started)
        chosen_voice = self.voice if voice is None else self._validated_voice(voice, started)
        chosen_rate = self.rate if rate is None else self._validated_rate(rate, started)
        if not self.online_enabled:
            # §10：关掉在线 = 零网络。连 Edge 的磁盘缓存都不回，直接本机 SAPI。
            return self._synthesize_sapi(normalized, started)

        digest = _cache_key(chosen_voice, chosen_rate, normalized)
        path = self.cache_dir / f"{digest}.mp3"
        data = self._read_cache(path)
        cached = data is not None
        edge_failure: str | None = None
        if data is None and not self._edge_blocked(priority):
            with self._flight(digest):
                data = self._read_cache(path)
                cached = data is not None
                if data is None and not self._edge_blocked(priority):
                    try:
                        data = self._synthesize(
                            normalized, chosen_voice, chosen_rate, started, priority
                        )
                    except SpeechError as exc:
                        edge_failure = exc.reason or "offline"
                        # v13 §4：用户失败进冷却（8/16/32/60 退避）；
                        # 后台失败只记自己的退避，用户点击照旧立即重试 Edge。
                        self._on_edge_failure(priority, edge_failure)
                    else:
                        self._on_edge_success()
                        if self._write_cache(path, data):
                            self._writes += 1
                            if self._writes % PRUNE_EVERY_WRITES == 0:
                                self._prune()
        if data is None:
            if priority != PRIORITY_USER:
                # v13 §4：后台预取/预热失败只影响这一次预取 —— 不占 SAPI、不动用户冷却。
                self._log_failure("tts_unavailable", edge_failure or "cooldown", started)
                raise SpeechError(503, "tts_unavailable", edge_failure or "cooldown")
            # 缓存未命中，且这次要么在 Edge 冷却窗口内、要么 Edge 刚失败 → 本机 SAPI
            return self._synthesize_sapi(normalized, started, edge_failure)

        result = SynthResult(
            data=data,
            mime="audio/mpeg",
            backend="edge",
            cached=cached,
            ms=_elapsed_ms(started),
        )
        log.info(
            "tts backend=%s cache=%s ms=%d len=%d voice=%s",
            result.backend,
            "hit" if result.cached else "miss",
            result.ms,
            len(normalized),
            chosen_voice,
        )
        return result

    def warm(self, connections: int = 1) -> bool:
        """§4 `POST /api/tts/warm`（票 #110 / v13 §5）：只确保连接，不合成、不返回音频。

        - `tts_online=0` → False（零网络，连接池一根手指都不碰）；
        - 注入假 `edge_synth` → False（没有真实连接池，不假装已连接）；
        - 默认路径 → 委托 `EdgePool.warm(connections)`（后台建立/复用连接，立即返回）；
        - v13 §3：返回值只表示「此刻确实有可用连接」，过期连接不算（池会顺手重建）。
        """
        if not self.online_enabled or self._edge_pool is None:
            return False
        return bool(self._edge_pool.warm(connections))

    # --- Edge 后台试探恢复（§2/§10）-----------------------------------------

    def _schedule_edge_probe(self) -> None:
        """Edge 标记不健康后，起一个后台线程等到点试探恢复（§2：后台试探，不占用户点击）。"""
        if self._edge_pool is None:
            return
        with self._edge_probe_guard:
            current = self._edge_probe_thread
            if current is not None and current.is_alive():
                return
            thread = threading.Thread(
                target=self._probe_edge_loop, name="tts-edge-probe", daemon=True
            )
            self._edge_probe_thread = thread
            thread.start()

    def _probe_edge_loop(self) -> None:
        """到点用固定短串试探一次；失败就按退避再等一个窗口，成功即恢复主路径。"""
        try:
            while self.online_enabled and self._edge_unhealthy():
                delay = self.edge_retry_at - time.monotonic()
                time.sleep(max(0.01, delay))
                if not self.online_enabled or not self._edge_unhealthy():
                    return
                if self._probe_edge_once():
                    return
                if self.edge_retry_at <= time.monotonic():
                    return  # 状态没推进（没有连接池 / 被关掉）：别空转
        finally:
            with self._edge_probe_guard:
                self._edge_probe_thread = None

    def _probe_edge_once(self) -> bool:
        """用固定短串 `ok` 探一次 Edge（**不发用户文本、不写缓存**）；成功清零冷却。

        v13 §4：探测失败算**用户路径**的连续失败（延长退避），日志单独标注；
        成功则立即清零冷却并记恢复日志。
        """
        if self._edge_pool is None:
            return False
        try:
            self._edge_pool.synthesize(
                "ok", self.voice, self.rate, priority=PRIORITY_BACKGROUND
            )
        except Exception:  # noqa: BLE001 - 探测失败继续走 SAPI，不能影响发音
            log.warning("tts edge 探测失败")
            self._on_edge_failure(PRIORITY_USER, "probe")
            return False
        self._on_edge_success()
        log.info("tts edge 探测恢复")
        return True

    def prewarm(self, texts: Any, voice: Any = None) -> dict[str, int]:
        """§3/§4 `POST /api/tts/prewarm`（票 #113）：把一批文本塞进后台热身队列。

        - 每次最多 `MAX_PREWARM_ITEMS`（40）条；空文本 / 超 4000 字符 / 已缓存 /
          与已排队项同缓存键的逐条**跳过**；
        - 队列上限 `PREWARM_QUEUE_MAX`（200）：满了丢最旧的那条（预热全是低优先级）；
        - `tts_online=0` → 不读缓存、不入队、不碰连接池、不发任何包，整批算跳过；
        - 立即返回 `{"queued": N, "skipped": M}`：不合成、不返回音频、不阻塞 HTTP。
        """
        items = list(texts) if isinstance(texts, (list, tuple)) else []
        if not self.online_enabled:
            # §3：关掉在线 = 不喂队列、不发任何包（整批都算跳过）
            self._log_prewarm(0, len(items))
            return {"queued": 0, "skipped": len(items)}
        chosen_voice = (
            self.voice if voice is None else self._validated_voice(voice, time.perf_counter())
        )
        queued = 0
        skipped = 0
        for index, raw in enumerate(items):
            if index >= MAX_PREWARM_ITEMS:
                skipped += 1  # 超过单次上限：逐条算跳过（HTTP 层用 400 提前挡住）
                continue
            text = normalize_text(raw) if isinstance(raw, str) else ""
            if not text or len(text) > MAX_TEXT_CHARS:
                skipped += 1
                continue
            digest = _cache_key(chosen_voice, self.rate, text)
            if self._cache_exists(digest):
                skipped += 1
                continue
            with self._prewarm_lock:
                if digest in self._prewarm_keys:
                    skipped += 1
                    continue
                while len(self._prewarm_queue) >= PREWARM_QUEUE_MAX:
                    old_text, old_voice, old_rate = self._prewarm_queue.popleft()
                    self._prewarm_keys.discard(
                        _cache_key(old_voice, old_rate, old_text)
                    )
                self._prewarm_queue.append((text, chosen_voice, self.rate))
                self._prewarm_keys.add(digest)
                self._start_prewarm_worker()
            queued += 1
        self._log_prewarm(queued, skipped)
        return {"queued": queued, "skipped": skipped}

    def _cache_exists(self, digest: str) -> bool:
        """缓存里已经有非空文件 = 不用再预热（只 stat，不读字节）。"""
        try:
            return (self.cache_dir / f"{digest}.mp3").stat().st_size > 0
        except OSError:
            return False

    def _start_prewarm_worker(self) -> None:
        """调用方必须持有 `_prewarm_lock`；队空即退，不留常驻线程。"""
        if self._prewarm_thread is not None and self._prewarm_thread.is_alive():
            return
        thread = threading.Thread(
            target=self._prewarm_loop, name="tts-prewarm", daemon=True
        )
        self._prewarm_thread = thread
        thread.start()

    def _next_prewarm_job(self) -> tuple[str, str, str] | None:
        """取下一件（`None` = 队列空 / 在线已关，worker 退出）。"""
        with self._prewarm_lock:
            if not self.online_enabled:
                # 跑到一半被关掉：剩下的整批丢掉，一个包都不发（§3）
                self._prewarm_queue.clear()
                self._prewarm_keys.clear()
                self._prewarm_thread = None
                return None
            if not self._prewarm_queue:
                self._prewarm_thread = None
                return None
            text, voice, rate = self._prewarm_queue.popleft()
            self._prewarm_keys.discard(_cache_key(voice, rate, text))
            return text, voice, rate

    def _prewarm_loop(self) -> None:
        """后台 worker：按 `PRIORITY_BACKGROUND` 合成、落缓存；失败静默（§3）。"""
        while True:
            job = self._next_prewarm_job()
            if job is None:
                return
            text, voice, rate = job
            try:
                self.synthesize(
                    text, voice=voice, rate=rate, priority=PRIORITY_BACKGROUND
                )
            except SpeechError:
                continue  # 预热失败不影响任何用户操作，也不重试
            except Exception as exc:  # noqa: BLE001 - worker 不许被一次意外打死
                log.info("tts 预热中断：%s", type(exc).__name__)

    def status(self) -> dict[str, Any]:
        """§4：`GET /api/health` / `GET /api/settings` 的只读 `tts` 块（票 #112）。
        **不含任何文本**：只有开关、可用性、当前主路径、音色与缓存文件数/字节数。
        `available` 只看 edge-tts 能否导入（不联网、不握手）；`backend` 是当前实际
        会走的主路径（关掉在线或没装 edge-tts 时就是 sapi）。
        """
        available = find_spec("edge_tts") is not None
        files, total_bytes = self._cache_usage()
        return {
            "enabled": bool(self.online_enabled),
            "available": available,
            "backend": "edge" if self.online_enabled and available else "sapi",
            "voice": self.voice,
            "cache_files": files,
            "cache_bytes": total_bytes,
        }

    @staticmethod
    def voices() -> list[dict[str, str]]:
        """`GET /api/settings` 的音色下拉选项（票 #116）：id + 展示名 + 性别。"""
        return voice_choices()

    def _cache_usage(self) -> tuple[int, int]:
        """`cache_dir` 下 `*.mp3` 的数量与总字节；目录不在 / 读不到 → `(0, 0)`。"""
        files = 0
        total = 0
        try:
            paths = list(self.cache_dir.glob("*.mp3"))
        except OSError:
            return 0, 0
        for path in paths:
            try:
                total += path.stat().st_size
            except OSError:
                continue
            files += 1
        return files, total

    def _edge_unhealthy(self) -> bool:
        """Edge 用户冷却窗口是否还有效（到点自动恢复 = 后台试探）。"""
        return time.monotonic() < self.edge_retry_at

    def _edge_blocked(self, priority: int) -> bool:
        """这次请求现在还能不能碰 Edge（v13 §4）。

        - 用户点击：只看自己的冷却窗口；
        - 后台预取/预热：用户冷却或自己的退避窗口任一生效都先不碰 Edge
          （否则后台会一边狂重试一边把用户打进 SAPI）。
        """
        now = time.monotonic()
        if now < self.edge_retry_at:
            return True
        return priority != PRIORITY_USER and now < self._bg_retry_at

    def _on_edge_failure(self, priority: int, reason: str | None) -> None:
        """Edge 失败计数与退避（v13 §4）：后台与用户路径彻底分开。"""
        if priority != PRIORITY_USER:
            self._bg_fail_streak += 1
            delay = _cooldown_delay(self._bg_fail_streak)
            self._bg_retry_at = time.monotonic() + delay
            log.warning(
                "tts edge 后台失败 streak=%d backoff_s=%d（不触发用户冷却）",
                self._bg_fail_streak,
                round(delay),
            )
            return
        self._edge_fail_streak += 1
        delay = _cooldown_delay(self._edge_fail_streak)
        self.edge_retry_at = time.monotonic() + delay
        log.warning(
            "tts edge 冷却开始 reason=%s backoff_s=%d streak=%d",
            reason or "offline",
            round(delay),
            self._edge_fail_streak,
        )
        # 到点由后台线程用固定短串试探恢复（_schedule_edge_probe），不等下一次点击。
        self._schedule_edge_probe()

    def _on_edge_success(self) -> None:
        """一次成功即清零：退避档位回到最低，两条路径的冷却都撤掉（v13 §4）。"""
        if self._edge_fail_streak or self._bg_fail_streak:
            log.info("tts edge 冷却恢复 streak=%d", self._edge_fail_streak)
        self._edge_fail_streak = 0
        self._bg_fail_streak = 0
        self.edge_retry_at = 0.0
        self._bg_retry_at = 0.0

    # --- 校验（§4：400 / 413）--------------------------------------------

    def _validated_text(self, text: Any, started: float) -> str:
        if not isinstance(text, str):
            self._log_failure("bad_text", None, started)
            raise SpeechError(400, "bad_text")
        normalized = normalize_text(text)
        if not normalized:
            self._log_failure("bad_text", None, started)
            raise SpeechError(400, "bad_text")
        if len(normalized) > MAX_TEXT_CHARS:
            self._log_failure("text_too_long", None, started)
            raise SpeechError(413, "text_too_long")
        return normalized

    def _validated_voice(self, voice: Any, started: float) -> str:
        if not isinstance(voice, str) or voice not in VOICES:
            self._log_failure("bad_voice", None, started)
            raise SpeechError(400, "bad_voice")
        return voice

    def _validated_rate(self, rate: Any, started: float) -> str:
        if not isinstance(rate, str) or _RATE_PATTERN.fullmatch(rate) is None:
            self._log_failure("bad_rate", None, started)
            raise SpeechError(400, "bad_rate")
        return rate

    # --- 合成与失败映射（§10）--------------------------------------------

    def _synthesize(
        self, text: str, voice: str, rate: str, started: float, priority: int
    ) -> bytes:
        try:
            if self._edge_pool is not None:
                data = self._edge_pool.synthesize(text, voice, rate, priority=priority)
            else:
                # 注入的三参 `edge_synth` 缝没有优先级概念（票 #113）
                data = self._edge_synth(text, voice, rate)  # type: ignore[misc]
        except SpeechError as exc:
            self._log_failure(exc.error, exc.reason, started)
            raise
        except Exception as exc:  # 任何 Edge 故障都要落成 503 的一种原因
            reason = _reason_of(exc)
            self._log_failure("tts_unavailable", reason, started)
            raise SpeechError(503, "tts_unavailable", reason) from exc
        if not data:
            self._log_failure("tts_unavailable", "offline", started)
            raise SpeechError(503, "tts_unavailable", "offline")
        return data

    def _synthesize_sapi(
        self, text: str, started: float, edge_reason: str | None = None
    ) -> SynthResult:
        """§10 兜底 1：本机 SAPI 英文语音。结果不落盘（warm 很快、WAV 体积大）。"""
        try:
            data = self._sapi_synth(text)
        except Exception:  # noqa: BLE001 - 只影响发音，映射成下面的 no_backend / Edge reason
            data = b""
        if not data:
            # 两个后端都不可用：reason 沿用 Edge 的失败原因（更贴切）；
            # 只关在线时没有 Edge 失败，就是 no_backend。
            reason = edge_reason or "no_backend"
            self._log_failure("tts_unavailable", reason, started)
            raise SpeechError(503, "tts_unavailable", reason)
        result = SynthResult(
            data=data,
            mime="audio/wav",
            backend="sapi",
            cached=False,
            ms=_elapsed_ms(started),
        )
        log.info(
            "tts backend=sapi cache=miss ms=%d len=%d voice=sapi",
            result.ms,
            len(text),
        )
        return result

    # --- 缓存（§5）--------------------------------------------------------

    @contextlib.contextmanager
    def _flight(self, key: str) -> Iterator[None]:
        """同键 single-flight：同一 sha256 同时只有一条线程在合成。"""
        with self._locks_guard:
            lock, count = self._key_locks.get(key, (threading.Lock(), 0))
            self._key_locks[key] = (lock, count + 1)
        lock.acquire()
        try:
            yield
        finally:
            lock.release()
            with self._locks_guard:
                _, live = self._key_locks[key]
                if live <= 1:
                    del self._key_locks[key]
                else:
                    self._key_locks[key] = (lock, live - 1)

    @staticmethod
    def _read_cache(path: Path) -> bytes | None:
        try:
            if path.stat().st_size <= 0:  # 半截/损坏的空文件：删掉重合成（§10）
                path.unlink(missing_ok=True)
                return None
            data = path.read_bytes()
            if not _looks_like_mp3(data):
                # 非空但不是 MP3（损坏 / 被别的程序写坏）：删掉重合成（§10）
                path.unlink(missing_ok=True)
                return None
            return data
        except OSError:
            return None

    def _write_cache(self, path: Path, data: bytes) -> bool:
        """同目录临时文件 + `os.replace`；失败只记错误类型，不留下半截文件。"""
        temp: Path | None = None
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            handle_fd, temp_name = tempfile.mkstemp(
                dir=self.cache_dir, prefix=".tts-", suffix=".tmp"
            )
            temp = Path(temp_name)
            with os.fdopen(handle_fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
            return True
        except OSError as exc:
            log.warning("tts 缓存写入失败：%s", type(exc).__name__)
            if temp is not None:
                try:
                    temp.unlink(missing_ok=True)
                except OSError:
                    pass
            return False

    def _prune(self) -> None:
        """超过上限就按 mtime 从旧到新删到 80%；删不掉（被占用）跳过不报错。

        票 #188（D10）：顺带删掉 mtime 超过一天的 `.tts-*.tmp` 孤儿 —— 正常路径
        （原子写成败两分支）都会收走临时文件，只有进程崩溃/断电会留下；`status()`
        只统计 `*.mp3`，不清理的话它们永远躲在缓存目录里没人看得见。
        """
        self._drop_stale_temp()
        limit = self.cache_mb * 1024 * 1024
        entries: list[tuple[float, int, Path]] = []
        try:
            paths = list(self.cache_dir.glob("*.mp3"))
        except OSError:
            return
        for path in paths:
            try:
                stat = path.stat()
            except OSError:
                continue
            entries.append((stat.st_mtime, stat.st_size, path))
        total = sum(size for _, size, _ in entries)
        if total <= limit:
            return
        target = int(limit * CACHE_TARGET_RATIO)
        for _, size, path in sorted(entries, key=lambda entry: entry[0]):
            if total <= target:
                break
            try:
                path.unlink()
            except OSError:
                continue
            total -= size

    def _drop_stale_temp(self) -> None:
        """崩溃残留的 `.tts-*.tmp` 孤儿（票 #188 D10）：mtime 超过一天就删。

        只敢删「老」的：正在写入的临时文件 mtime 是新的，误删会让并发合成丢音频。
        与 `_prune` 的 mp3 清理不同，这里不看体积上限 —— 孤儿数量极少（一次崩溃
        至多一个），顺手带走即可。
        """
        cutoff = time.time() - _TEMP_ORPHAN_AGE_S
        try:
            candidates = list(self.cache_dir.glob(".tts-*.tmp"))
        except OSError:
            return
        for path in candidates:
            try:
                if path.stat().st_mtime <= cutoff:
                    path.unlink()
            except OSError:
                continue  # 正在被写 / 已被并发清理：跳过，下次再看

    # --- 日志（§8：只记 backend/cache/ms/len/voice，绝不记文本）-----------

    @staticmethod
    def _log_prewarm(queued: int, skipped: int) -> None:
        """§3/§8：预热只记数量，绝不记文本。"""
        log.info("tts prewarm queued=%d skipped=%d", queued, skipped)

    @staticmethod
    def _log_failure(error: str, reason: str | None, started: float) -> None:
        if reason is None:
            log.warning("tts 请求失败：%s ms=%d", error, _elapsed_ms(started))
        else:
            log.warning(
                "tts 不可用：%s reason=%s ms=%d", error, reason, _elapsed_ms(started)
            )


def _cache_key(voice: str, rate: str, text: str) -> str:
    payload = f"{voice}\n{rate}\n{text}".encode()
    return hashlib.sha256(payload).hexdigest()


def _cooldown_delay(streak: int) -> float:
    """v13 §4：连续失败第 N 次对应的冷却秒数（8 / 16 / 32 / 60，封顶 60）。"""
    if streak <= 1:
        return EDGE_COOLDOWN_STEPS[0]
    return EDGE_COOLDOWN_STEPS[min(streak - 1, len(EDGE_COOLDOWN_STEPS) - 1)]


def _looks_like_mp3(data: bytes) -> bool:
    """缓存文件的 MP3 基本魔数：ID3 头或 MPEG 帧同步（`0xFFEx`）。

    §10 要求「缓存文件损坏 / 半截 → 删除并重新合成」；原子写已经挡住半截文件，
    这里再兜一层非空垃圾/被写坏的文件。更深的帧级截断检测不做（需要解码器）。
    """
    if len(data) < 4:
        return False
    if data[:3] == b"ID3":
        return True
    return data[0] == 0xFF and (data[1] & 0xE0) == 0xE0


def _reason_of(exc: BaseException) -> str:
    """Edge 失败 → §4 的 503 reason（最贴切的一个）。

    票 #188（D9）：aiohttp 的 `ClientResponseError` 按 status 分档 —— 403 的
    token 过期、429 限流、5xx 不再一律报 offline（那是「连不上」，而这些是
    「连上了、被拒了/服务端坏了」，处置提示完全不同）。分档只读 `edge_ws` 里
    已就位的 runtime，不触发惰性导入 —— 能抛出这个异常就说明 runtime 已导入。
    """
    if isinstance(exc, TimeoutError):  # asyncio.TimeoutError 在 3.11+ 就是它
        return "timeout"
    if isinstance(exc, ImportError):
        return "no_backend"
    status = client_response_status(exc)
    if status is not None:
        if 400 <= status < 500:
            return "http_4xx"
        if status >= 500:
            return "http_5xx"
    return "offline"


class _SapiComVoice:
    """§2 兜底 1：专用 COM 线程里复用 `SAPI.SpVoice`，输出走内存流（不落盘）。

    COM 对象有线程亲和性：`CoInitialize` / voice / 输出流都在同一条 daemon 线程上
    创建和使用，调用方只投递文本、等结果，所以天然串行（同一时刻一个 SAPI 请求）。
    `SAPI.SpMemoryStream` 只给裸 PCM，这里补 44 字节 WAV 头后返回（§2 要求输出 WAV），
    因此系统临时目录不会再出现 `tts-sapi-*.wav`。
    任何失败（没装 comtypes、没有英文 voice、COM 错误）都抛给调用方，由 `Speech`
    映射成不可用；**只影响发音，不影响取词台**。启动失败后本进程内不再重试
    （音色列表不会中途变化），避免每次请求都白等一次 COM 初始化。
    """

    PREFERRED = "Zira"  # §2：优先英文女声 Zira
    EN_US = "409"  # SAPI token 的 GetAttribute("Language")，十六进制 LCID
    WAV_22KHZ_16BIT_MONO = 22  # SpeechAudioFormatType.SAFT22kHz16BitMono

    def __init__(self) -> None:
        self._jobs: queue.Queue[tuple[str, Future[bytes]]] = queue.Queue()
        self._guard = threading.Lock()
        self._started = False
        self._broken: BaseException | None = None

    def synthesize(self, text: str) -> bytes:
        future: Future[bytes] = Future()
        with self._guard:
            if self._broken is not None:
                raise RuntimeError("SAPI 不可用") from self._broken
            if not self._started:
                self._started = True
                threading.Thread(
                    target=self._run, name="tts-sapi", daemon=True
                ).start()
            # 入队必须在锁内：否则 `_open()` 失败后 worker 把队列 drain 完、这条 job
            # 才进队，就永远没人消费 → future.result() 永久挂住（退不到 503）。
            self._jobs.put((text, future))
        try:
            return future.result(timeout=30)
        except TimeoutError as exc:  # 兜底：worker 卡死时也不让 HTTP 线程无限等
            raise RuntimeError("SAPI 合成超时") from exc

    def _run(self) -> None:
        try:
            voice = self._open()
        except Exception as exc:  # noqa: BLE001 - comtypes 缺失 / 无英文 voice / COM 初始化失败
            with self._guard:
                self._broken = exc
            while True:
                try:
                    _, future = self._jobs.get_nowait()
                except queue.Empty:
                    return
                future.set_exception(exc)
        while True:
            text, future = self._jobs.get()
            try:
                future.set_result(self._speak(voice, text))
            except Exception as exc:  # noqa: BLE001 - 单次合成失败不影响后续请求重试
                future.set_exception(exc)

    @classmethod
    def _open(cls) -> Any:
        """线程内初始化 COM 并选英文 voice；voice 跨请求复用。"""
        import comtypes  # 惰性导入：没装只影响发音
        import comtypes.client

        comtypes.CoInitialize()
        voice = comtypes.client.CreateObject("SAPI.SpVoice", dynamic=True)
        tokens = voice.GetVoices()
        chosen = None
        for index in range(tokens.Count):
            token = tokens.Item(index)
            description = token.GetDescription()
            if cls.PREFERRED in description:
                chosen = token
                break
            if chosen is None and (
                token.GetAttribute("Language") == cls.EN_US
                or "English (United States)" in description
            ):
                chosen = token
        if chosen is None:
            raise RuntimeError("没有可用的英文 SAPI 语音")
        voice.Voice = chosen
        return voice

    @classmethod
    def _speak(cls, voice: Any, text: str) -> bytes:
        """同步合成一段 WAV：内存流出裸 PCM，补 44 字节 RIFF 头返回。"""
        import comtypes.client

        # 内存流复用会不断追加（GetData 返回累计缓冲），每次合成新建一个。
        stream = comtypes.client.CreateObject("SAPI.SpMemoryStream", dynamic=True)
        stream.Format.Type = cls.WAV_22KHZ_16BIT_MONO
        voice.AudioOutputStream = stream
        voice.Speak(text)
        pcm = bytes(stream.GetData())
        if not pcm:
            raise RuntimeError("SAPI 没有返回音频")
        return _wav_bytes(pcm)


def _wav_bytes(pcm: bytes, *, sample_rate: int = SAPI_SAMPLE_RATE) -> bytes:
    """把 SAPI 的裸 PCM（16-bit 单声道）包成标准 44 字节头的 WAV。"""
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + len(pcm),
        b"WAVE",
        b"fmt ",
        16,
        1,  # PCM
        1,  # 单声道
        sample_rate,
        sample_rate * 2,  # 字节率 = 采样率 × 块对齐
        2,  # 块对齐 = 声道 × 位深/8
        16,  # 位深
        b"data",
        len(pcm),
    )
    return header + pcm


_SAPI_LOCK = threading.Lock()
_SAPI_VOICE: _SapiComVoice | None = None


def _sapi_synth_via_com(text: str) -> bytes:
    """默认 SAPI 适配器（惰性）：进程内一个专用 COM 线程，首次调用时才启动。"""
    global _SAPI_VOICE
    with _SAPI_LOCK:
        if _SAPI_VOICE is None:
            _SAPI_VOICE = _SapiComVoice()
    return _SAPI_VOICE.synthesize(text)
