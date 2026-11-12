"""英语取词最小客户端：对命令行传入的矩形跑一次 capture_ocr 并打印文本。

    .venv\\Scripts\\python.exe client.py ping
    .venv\\Scripts\\python.exe client.py 100 200 300 40      # x y w h（物理像素）

端口取 config.ini 的 `port`（与内核同源，spec §9），`--port` 可覆盖。
退出码：0 成功 / 1 内核回了 ok:false / 2 参数或连接出错。
"""

import argparse
import json
import socket
import sys
from typing import Any

import kernel

TIMEOUT_S = 5.0


def request(payload: dict, port: int, timeout: float = TIMEOUT_S) -> dict:
    """一条连接、一条请求、按行读回一条响应。"""
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        sock.sendall(json.dumps(payload).encode("utf-8") + b"\n")
        data = b""
        while not data.endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
    return json.loads(data.decode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="向英语取词内核发一条请求（用于演示与验收）"
    )
    parser.add_argument("args", nargs="+", help="`ping`，或矩形 x y w h（物理像素）")
    parser.add_argument(
        "--port", type=int, help="内核 TCP 端口，默认取 config.ini 的 port"
    )
    ns = parser.parse_args(argv)

    if ns.args == ["ping"]:
        payload: dict[str, Any] = {"cmd": "ping"}
    elif len(ns.args) == 4:
        try:
            payload = {"cmd": "capture_ocr", "rect": [int(value) for value in ns.args]}
        except ValueError:
            parser.error("矩形必须是四个整数：x y w h")
    else:
        parser.error("用法：client.py ping 或 client.py x y w h")

    port = (
        ns.port
        if ns.port is not None
        else kernel.config_int(kernel.read_config(), "port", kernel.DEFAULT_PORT)
    )

    try:
        response = request(payload, port)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"连不上内核 127.0.0.1:{port}：{exc}", file=sys.stderr)
        return 2

    if not response.get("ok"):
        saved = f"（留图 {response['saved']}）" if response.get("saved") else ""
        print(f"失败：{response.get('error')}{saved}", file=sys.stderr)
        return 1

    if payload["cmd"] == "ping":
        print(f"pong {response['pong']} ms")
    else:
        print(response["text"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
