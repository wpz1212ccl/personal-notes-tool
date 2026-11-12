"""自动清理：日志轮转备份 + TTS 音频缓存（2026-11-06）。

原先只有「写满才清」：`hub.log` 是 2MB × 4 滚动，`speech._prune()` 只在启动和每
100 次缓存写入时按 256MB 上限淘汰 mp3。两个后果：

1. 播放事件上报进来后，`playback.log.*` 会一直堆到 backupCount 为止，而 AHK 写的
   `app.log.*` / `kernel.log.*` 根本不在 hub 的滚动策略里 —— 需要按**保留天数**收口；
2. 缓存没到 256MB 就永远不清理，几个月前的旧音频一直占着目录 —— 需要**时间驱动**
   的容量淘汰；另外提供**可选**的按天 TTL（默认关）。

关于 TTL 默认关：`docs/research/2026-11-02-audio-playback-gap-and-cache.md` §Q4 明确
「磁盘层故意不设 TTL（同音色同文本永远等价），只用容量 + mtime 做粗 LRU」。删掉一个
仍会复用的 mp3 只会让下次重新合成一次 Edge 请求，没有任何收益。所以默认只做
「定期按容量」；确实想按天收旧音频的，自己在 config 里写 `tts_cache_ttl_days=30`。

本模块只做两件很窄的事，纯函数 + 可注入 `now`，方便单测：

- `cleanup_logs()`：只删 `logs/` 下的 `*.log.*` 轮转备份（活动日志 `*.log` 不动），
  先按天数、再按备份总体积；
- `cleanup_tts_cache()`：只删 `data/tts/` 下的 `*.mp3` 与 `.tts-*.tmp` 孤儿，
  可选按 TTL（默认关）、再按容量上限（与 `speech._prune` 同口径，删到 80%）。

`Maintenance` 把它挂成 daemon 线程：启动跑一次，之后每 6 小时一次；删了什么写一行
汇总到 `hub.log`，没删就不吵。所有删除都限制在**传入的项目目录内**、只匹配明确的
文件模式、逐个 `unlink()`，不递归、不删目录、不碰其它路径。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import band_cleanup

log = logging.getLogger("hub")

DAY_S = 86400.0
DEFAULT_LOG_RETENTION_DAYS = 14
# 0 = 不按天淘汰 mp3（保持既有设计：磁盘缓存只按容量 + mtime 淘汰）。
# 想按天收旧音频就在 config 里写 tts_cache_ttl_days=<天数>。
DEFAULT_TTS_TTL_DAYS = 0
DEFAULT_INTERVAL_S = 6 * 3600
DEFAULT_LOG_MAX_BYTES = 32 * 1024 * 1024  # 轮转备份合计上限
DEFAULT_TTS_MAX_BYTES = 256 * 1024 * 1024
TARGET_RATIO = 0.8  # 超限后删到上限的 80%，留一段免得立刻又触发
TEMP_ORPHAN_AGE_S = DAY_S  # 与 speech._drop_stale_temp 同口径：只认超过一天的


@dataclass(frozen=True)
class CleanupResult:
    deleted_files: int = 0
    freed_bytes: int = 0
    kept_files: int = 0
    kept_bytes: int = 0


def _entries(directory: Path, pattern: str) -> list[tuple[float, int, Path]]:
    """列 (mtime, size, path)；目录不存在 / 读不到一律当空，不抛。"""
    try:
        paths = list(directory.glob(pattern))
    except OSError:
        return []
    entries: list[tuple[float, int, Path]] = []
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            continue
        if path.is_file():
            entries.append((stat.st_mtime, stat.st_size, path))
    return entries


def _unlink(path: Path) -> bool:
    """删一个文件；被占用 / 已消失都当跳过，绝不中断整轮清理。"""
    try:
        path.unlink()
    except OSError:
        return False
    return True


def cleanup_logs(
    log_dir: str | Path,
    *,
    retention_days: int = DEFAULT_LOG_RETENTION_DAYS,
    max_total_bytes: int = DEFAULT_LOG_MAX_BYTES,
    now: float | None = None,
) -> CleanupResult:
    """按天数 + 总容量清理 `*.log.*` 备份；活动日志（*.log）永远不动。"""
    now = time.time() if now is None else now
    cutoff = now - max(0, retention_days) * DAY_S
    deleted = 0
    freed = 0
    survivors: list[tuple[float, int, Path]] = []
    for mtime, size, path in _entries(Path(log_dir), "*.log.*"):
        if mtime < cutoff and _unlink(path):
            deleted += 1
            freed += size
        else:
            survivors.append((mtime, size, path))
    total = sum(size for _, size, _ in survivors)
    target = int(max(0, max_total_bytes) * TARGET_RATIO)
    if total > max_total_bytes:
        for _, size, path in sorted(survivors, key=lambda entry: entry[0]):
            if total <= target:
                break
            if _unlink(path):
                deleted += 1
                freed += size
                total -= size
    kept = sum(1 for _, _, path in survivors if path.exists())
    return CleanupResult(deleted, freed, kept, total)


def cleanup_tts_cache(
    cache_dir: str | Path,
    *,
    ttl_days: int = DEFAULT_TTS_TTL_DAYS,
    max_bytes: int = DEFAULT_TTS_MAX_BYTES,
    now: float | None = None,
) -> CleanupResult:
    """按 TTL + 容量清理 `*.mp3`，顺带收掉崩溃残留的 `.tts-*.tmp`。

    mp3 的 mtime 是「写入时间」，缓存命中不会刷新它 —— 所以 TTL 的语义是
    「超过 N 天没被重新合成的旧音频」，不是严格 LRU。这与 `speech._prune`
    按 mtime 淘汰是同一口径，两边不会互相打架。`ttl_days <= 0` 时跳过这一轮，
    只按容量清理（默认值，保持既有设计）。
    """
    now = time.time() if now is None else now
    directory = Path(cache_dir)
    cutoff = now - max(0, ttl_days) * DAY_S
    deleted = 0
    freed = 0
    survivors: list[tuple[float, int, Path]] = []
    for mtime, size, path in _entries(directory, "*.mp3"):
        if ttl_days > 0 and mtime < cutoff and _unlink(path):
            deleted += 1
            freed += size
        else:
            survivors.append((mtime, size, path))
    total = sum(size for _, size, _ in survivors)
    target = int(max(0, max_bytes) * TARGET_RATIO)
    if total > max_bytes:
        for _, size, path in sorted(survivors, key=lambda entry: entry[0]):
            if total <= target:
                break
            if _unlink(path):
                deleted += 1
                freed += size
                total -= size
    # 孤儿临时文件：只在够老时删，正在写入的那一个必须留着（并发合成会丢音频）
    for mtime, size, path in _entries(directory, ".tts-*.tmp"):
        if mtime < now - TEMP_ORPHAN_AGE_S and _unlink(path):
            deleted += 1
            freed += size
    kept = sum(1 for _, _, path in survivors if path.exists())
    return CleanupResult(deleted, freed, kept, total)


class Maintenance:
    """定期清理的后台线程；`start()` 立即在线程内跑一次，之后按 interval 循环。"""

    def __init__(
        self,
        log_dir: str | Path,
        tts_dir: str | Path,
        *,
        log_retention_days: int = DEFAULT_LOG_RETENTION_DAYS,
        tts_ttl_days: int = DEFAULT_TTS_TTL_DAYS,
        log_max_bytes: int = DEFAULT_LOG_MAX_BYTES,
        tts_max_bytes: int = DEFAULT_TTS_MAX_BYTES,
        interval_s: int = DEFAULT_INTERVAL_S,
        enabled: bool = True,
        band_dir: str | Path | None = None,
        band_references: Callable[[], Sequence[Mapping[str, Any]]] | None = None,
        band_clear: Callable[[Sequence[int]], int] | None = None,
        band_orphan_min_age_s: float = band_cleanup.DEFAULT_ORPHAN_MIN_AGE_S,
        band_ttl_days: int = band_cleanup.DEFAULT_TTL_DAYS,
        band_max_bytes: int = band_cleanup.DEFAULT_MAX_BYTES,
        band_dry_run: bool = True,
    ) -> None:
        self.log_dir = Path(log_dir)
        self.tts_dir = Path(tts_dir)
        self.log_retention_days = log_retention_days
        self.tts_ttl_days = tts_ttl_days
        self.log_max_bytes = log_max_bytes
        self.tts_max_bytes = tts_max_bytes
        self.interval_s = max(60, int(interval_s))
        self.enabled = enabled
        # v35 #226：底图清理三件套（目录 + 引用行回调 + 置空回调）。三者齐全才跑；
        # 不传 = 老行为（只清日志与 TTS），测试 / 嵌入方不用造底图目录。
        self.band_dir = Path(band_dir) if band_dir is not None else None
        self.band_references = band_references
        self.band_clear = band_clear
        self.band_orphan_min_age_s = band_orphan_min_age_s
        self.band_ttl_days = band_ttl_days
        self.band_max_bytes = band_max_bytes
        self.band_dry_run = bool(band_dry_run)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, name="maintenance", daemon=True
        )
        self._thread.start()

    def _loop(self) -> None:
        self.run_once(initial=True)
        while not self._stop.wait(self.interval_s):
            self.run_once()

    def run_once(
        self, *, initial: bool = False
    ) -> tuple[CleanupResult, CleanupResult, band_cleanup.BandCleanupResult | None] | None:
        """跑一轮；返回 (日志清理, 缓存清理, 底图清理)，异常时返回 None 且只记错误类型。"""
        try:
            logs = cleanup_logs(
                self.log_dir,
                retention_days=self.log_retention_days,
                max_total_bytes=self.log_max_bytes,
            )
            tts = cleanup_tts_cache(
                self.tts_dir,
                ttl_days=self.tts_ttl_days,
                max_bytes=self.tts_max_bytes,
            )
        except OSError as exc:
            log.warning("自动清理跳过：%s", type(exc).__name__)
            return None
        bands: band_cleanup.BandCleanupResult | None = None
        if (
            self.band_dir is not None
            and self.band_references is not None
            and self.band_clear is not None
        ):
            # v35 #226：底图清理沿用同一个维护周期与总开关（不新增计时器）。底图这块
            # 坏掉（库忙 / 权限）不许打断日志与 TTS 的既有清理 —— 只记错误类型。
            try:
                bands = band_cleanup.cleanup_bands(
                    self.band_dir,
                    list_references=self.band_references,
                    clear_paths=self.band_clear,
                    ttl_days=self.band_ttl_days,
                    max_bytes=self.band_max_bytes,
                    orphan_min_age_s=self.band_orphan_min_age_s,
                    dry_run=self.band_dry_run,
                )
            except Exception as exc:  # noqa: BLE001 - 清理故障不许拖垮守护线程
                log.warning("底图清理跳过：%s", type(exc).__name__)
        if initial or logs.deleted_files or tts.deleted_files:
            log.info(
                "自动清理：日志删 %d 个/%.1f MB（保留 %d 个）；TTS 缓存删 %d 个/%.1f MB"
                "（保留 %d 个/%.1f MB）",
                logs.deleted_files,
                logs.freed_bytes / 1024 / 1024,
                logs.kept_files,
                tts.deleted_files,
                tts.freed_bytes / 1024 / 1024,
                tts.kept_files,
                tts.kept_bytes / 1024 / 1024,
            )
        return logs, tts, bands

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
