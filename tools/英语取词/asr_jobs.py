"""ASR 任务队列（v36 ASR P1）：BV → 音频 → 云端转录，单 worker 排队执行。

照 `hub.MeaningFillQueue` 的 daemon+queue 范式：

- **网络与解析全在 worker 线程里做**，提交方（hub / CLI）只收结果，不占前台
  —— `sessionlog/afk-v36-rework.md:208` 实测过 hub 同步做重活会占 GIL，pong 迟到
  被判死重启；队列把长活挪出调用路径，是 ASR 上线的硬前提；
- 同一 `(bvid, cid)` 在队列里 / 正在处理时重复提交不重排（片头重进同一条片的常见态）；
- 单条失败只回 `reason`，绝不打穿 worker；
- `stop()` 置停止位并 join ≤2s；worker 是 daemon，绝不吊住进程退出。

P1 只做**离线闭环**：`python asr_jobs.py BV1xxxx` 一条 BV 打出转录全文。
hub 接线（收句会话自动排队、转写入池、`source_available` 置真）是 P2，本期不碰。
"""

from __future__ import annotations

import argparse
import configparser
import logging
import queue
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import asr_client
import asr_fetch

log = logging.getLogger("hub.asr_jobs")

CONFIG_PATH = Path(__file__).resolve().parent / "config.ini"
CONFIG_SECTION = "英语取词"
# 单条 BV 的宽限：1 小时音频（云端上限）+ 下载 + 上传，15 分钟足够宽。
CLI_TIMEOUT_S = 900


@dataclass
class AsrJob:
    """一条待转录音频：BV 号（多 P 带 cid）+ 完成回调（在 worker 线程里调用）。"""

    bvid: str
    cid: int | None = None
    on_done: Callable[[dict[str, Any]], None] | None = None


class AsrJobQueue:
    """单 worker 的「取音频 + 转录」队列。"""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = asr_client.DEFAULT_MODEL,
        fetch_audio: Callable[..., dict[str, Any]] | None = None,
        transcribe: Callable[..., dict[str, Any]] | None = None,
        maxsize: int = 4,
        interval: float = 0.0,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.interval = interval
        # 测试注入假网络层；生产用两个模块的默认实现。
        self._fetch_audio = fetch_audio if fetch_audio is not None else asr_fetch.fetch_audio
        self._transcribe = transcribe if transcribe is not None else asr_client.transcribe
        self._queue: queue.Queue[AsrJob] = queue.Queue(maxsize=maxsize)
        # 「还在队列里 / 正在处理」的 (bvid, cid)：同一片只排一次。
        self._pending: set[tuple[str, int | None]] = set()
        self._pending_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._thread_lock = threading.Lock()

    @property
    def alive(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self) -> None:
        with self._thread_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="asr-job", daemon=True
            )
            self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        """置停止位并 join ≤2s：在途下载/转录超时就留给 daemon 线程自灭。"""
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def submit(self, job: AsrJob) -> bool:
        """入队一条；同一 `(bvid, cid)` 已在队列里 / 正在处理 → 返回 False。"""
        key = (str(job.bvid or "").strip(), job.cid)
        if not key[0]:
            return False
        with self._pending_lock:
            if key in self._pending:
                return False
            self._pending.add(key)
        self.start()
        try:
            self._queue.put_nowait(job)
        except queue.Full:
            with self._pending_lock:
                self._pending.discard(key)
            return False
        return True

    # --- worker -------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                result = self._process(job)
            except Exception as exc:  # noqa: BLE001 - 单条坏掉不许打死 worker
                log.info("ASR 单条异常：%s", type(exc).__name__)
                result = {
                    "bvid": job.bvid,
                    "cid": job.cid,
                    "text": "",
                    "reason": asr_client.REASON_ERROR,
                    "ms": 0,
                    "bytes": 0,
                }
            finally:
                with self._pending_lock:
                    self._pending.discard((str(job.bvid or "").strip(), job.cid))
                self._queue.task_done()
            if job.on_done is not None:
                try:
                    job.on_done(result)
                except Exception as exc:  # noqa: BLE001 - 回调故障不影响后续任务
                    log.info("ASR 回调异常：%s", type(exc).__name__)
            if self.interval and self._stop.wait(self.interval):
                break

    def _process(self, job: AsrJob) -> dict[str, Any]:
        """取音频 → 转录；任一步失败都只回原因码。"""
        started = time.perf_counter()
        got = self._fetch_audio(job.bvid, cid=job.cid)
        if got.get("reason") is not None:
            return {
                "bvid": job.bvid,
                "cid": job.cid,
                "text": "",
                "reason": got["reason"],
                "ms": _elapsed_ms(started),
                "bytes": int(got.get("bytes") or 0),
            }
        out = self._transcribe(
            got.get("data") or b"", api_key=self.api_key, model=self.model
        )
        return {
            "bvid": job.bvid,
            "cid": job.cid,
            "text": out["text"],
            "reason": out["reason"],
            "ms": _elapsed_ms(started),
            "bytes": int(got.get("bytes") or 0),
        }


def _elapsed_ms(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)


def read_asr_config(path: Path = CONFIG_PATH) -> dict[str, str]:
    """读 `config.ini` 的 `[英语取词]` 段里的 `asr_*` 键（缺文件 / 缺段回空）。"""
    parser = configparser.ConfigParser(interpolation=None)
    if not parser.read(path, encoding="utf-8") or not parser.has_section(
        CONFIG_SECTION
    ):
        return {}
    return dict(parser[CONFIG_SECTION])


def main(argv: list[str] | None = None) -> int:
    """离线闭环（P1 验收用）：`python asr_jobs.py BV1xxxx [--cid N]`。"""
    parser = argparse.ArgumentParser(
        description="一条 BV 走完音频抓取 + 云端转录（ASR P1 离线闭环）"
    )
    parser.add_argument("bvid", help="B站视频 BV 号（如 BV1gLRPBcEKL）")
    parser.add_argument("--cid", type=int, help="多 P 视频的分 P cid（默认第 1 P）")
    ns = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    config = read_asr_config()
    provider = (config.get("asr_provider") or "siliconflow").strip()
    if provider != "siliconflow":
        print(f"不支持的 asr_provider：{provider}", file=sys.stderr)
        return 2
    key = (config.get("asr_siliconflow_key") or "").strip()
    if not key:
        print("config.ini 没有 asr_siliconflow_key，无法转录", file=sys.stderr)
        return 2
    model = (config.get("asr_model") or "").strip() or asr_client.DEFAULT_MODEL

    done = threading.Event()
    box: dict[str, Any] = {}

    def on_done(result: dict[str, Any]) -> None:
        box.update(result)
        done.set()

    jobs = AsrJobQueue(api_key=key, model=model)
    if not jobs.submit(AsrJob(ns.bvid, ns.cid, on_done)):
        print("任务没进队列（同一 BV 正在处理或队列已满）", file=sys.stderr)
        return 1
    finished = done.wait(CLI_TIMEOUT_S)
    jobs.stop()
    if not finished:
        print("等结果超时", file=sys.stderr)
        return 1
    if box.get("reason") is not None:
        print(f"失败：{box.get('reason')}", file=sys.stderr)
        return 1
    print(box.get("text") or "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
