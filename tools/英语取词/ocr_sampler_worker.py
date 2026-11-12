"""band OCR worker 子进程入口（v33 P0，票 #211 首发崩溃修复）。

**为什么存在**：hub 进程已加载 winrt(SMTC)/comtypes(UIA)，同进程再起
onnxruntime 会原生段错误（真机 exit 2816）——band OCR 必须在**干净 python**
里跑。本模块只 import `ocr_sampler` 及其依赖链（kernel/settings/store，
实测 sys.modules 零 winrt/winsdk/comtypes，父端 `OcrWorkerProxy.probe()`
运行时自证），绝不 import winsdk/winrt/comtypes。

**协议**（与 `ocr_sampler.OcrWorkerProxy` 对应，见其头注释）：
- 请求：一行 JSON 头 `{"n":<负载字节>,"w":..,"h":..,"c":..}` + n 字节 RGB 像素；
  或探测帧 `{"cmd":"probe"}`；
- 回包：一行 JSON——`{"ok":true,"lines":[[text,conf],...]}`、
  `{"ok":false,"error":..}`（单图失败，进程活着继续服务）、
  `{"ok":false,"fatal":true,...}`（引擎建不起来，写完即退）。

生命周期：stdin EOF（父进程退出/关管道）= 收工退 0；坏帧退 2（父端重生）。
日志纪律（§10）：回包只带错误类型名，不带句文本。
"""

from __future__ import annotations

import json
import sys
from typing import Any

import ocr_sampler

_FORBIDDEN_ROOTS = ("winrt", "winsdk", "comtypes")


def handle_request(engine: Any, header: bytes, payload: bytes) -> bytes:
    """一次请求 → 一行 JSON 回包（纯逻辑，离线可测；引擎调用异常往上抛）。"""
    request = json.loads(header.decode("ascii"))
    if request.get("cmd") == "probe":
        forbidden = sorted(
            {name.split(".")[0] for name in sys.modules if name.split(".")[0] in _FORBIDDEN_ROOTS}
        )
        return json.dumps(
            {"ok": True, "probe": {"forbidden_modules": forbidden}}, ensure_ascii=True
        ).encode("ascii") + b"\n"
    import numpy as np

    array = np.frombuffer(payload, np.uint8).reshape(
        int(request["h"]), int(request["w"]), int(request["c"])
    )
    lines = engine(array)
    return json.dumps(
        {"ok": True, "lines": [[str(text), float(conf)] for text, conf in lines]},
        ensure_ascii=True,
    ).encode("ascii") + b"\n"


def _reply(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=True).encode("ascii") + b"\n"


def main(stdin: Any = None, stdout: Any = None) -> int:
    """子进程主循环：读请求 → 引擎推理 → 写一行回包 → flush。"""
    stdin = sys.stdin.buffer if stdin is None else stdin
    stdout = sys.stdout.buffer if stdout is None else stdout
    try:
        engine = ocr_sampler.BandOcrEngine()
    except BaseException as exc:  # noqa: BLE001 - 引擎建不起来：如实报 fatal 再退
        stdout.write(_reply({"ok": False, "fatal": True,
                             "error": f"engine unavailable: {type(exc).__name__}"}))
        stdout.flush()
        return 1
    while True:
        header = stdin.readline()
        if not header:
            return 0  # 父进程关管道/退出 = 收工
        try:
            request = json.loads(header.decode("ascii"))
            size = 0 if request.get("cmd") else int(request["n"])
        except (ValueError, KeyError, UnicodeDecodeError, TypeError):
            return 2  # 帧坏了流就不齐了：退（父端下次调用自动重生）
        payload = stdin.read(size) if size else b""
        if size and (payload is None or len(payload) != size):
            return 2
        try:
            stdout.write(handle_request(engine, header, payload))
        except BaseException as exc:  # noqa: BLE001 - 单图失败回一句错误，进程活着
            stdout.write(_reply({"ok": False, "error": type(exc).__name__}))
        stdout.flush()


if __name__ == "__main__":
    sys.exit(main())
