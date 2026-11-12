"""文章**创建时的原始分段输入**：编码 / 解码 / 段落类型派生（v22 P1a，票 #170）。

为什么存原始输入（spec §6.7 / ADR 0009 补记）：`documents.text` 是**单行化**产物，
- OCR 行框几何（段落类型 `body|heading|list|quote` 的来源）入库后不可重建；
- 粘贴原文的硬折行（「重新分段」的输入）入库后同样不可重建。
因此建文章时把分段算法的输入留一份存档（`documents.segmentation_source`），让
「同一算法可重跑」成立。这是 v22 实现期**唯一**的数据层偏离（用户验收时确认）。

两种格式（`documents.format`）：
- `manual`：source 就是**粘贴原文逐字**（hub 在归一化之前拿到，spec §6.7）；
- `ocr`：`encode_ocr_lines()` 的编码 —— 每行 `[top, bottom, left, height, text]`
  （`height` = bottom - top，冗余但免去几何消费方再算一次）→ JSON
  （`ensure_ascii=False`，中文可读）→ zlib → base64（**ASCII** 字符串，可直接放进
  RPC 的 JSON、AHK 的 `JsonString` 也不需要转义）。压缩后约 5–10KB/页。

读路径约定（spec §6.7 的「不伪造」）：坏数据 / 非本格式 / 旧 v21 文章（列 NULL）
一律返回 `None` / 全 `body`，**不抛** —— 一句坏数据不该让整篇文章读不出来。
本模块是纯函数，零第三方依赖（只用标准库 + 项目内的 `longtext`）。
"""

from __future__ import annotations

import base64
import json
import zlib
from collections.abc import Sequence
from typing import Any

import longtext

# `documents.format` 的两种取值（与 store.DOCUMENT_FORMATS 同口径）。
OCR_FORMAT = "ocr"
MANUAL_FORMAT = "manual"
# zlib 默认档：实测 5–10KB/页，压缩耗时 <1ms，读路径解压同样可忽略。
_ZLIB_LEVEL = 6
# 每行的形状：`[top, bottom, left, height, text]`。
_ROW_FIELDS = 5
# 解压后的字节上限（防解压炸弹 / 坏数据把内存打满）：正常一页行框 JSON 只有几十 KB，
# 8MB 是纯护栏；超限按「坏数据」返回 None（spec §6.7 的读路径不抛约定）。
_MAX_DECODED_BYTES = 8 * 1024 * 1024


def encode_ocr_lines(lines: Sequence[Any]) -> str:
    """OCR 行框 → 可入库的 ASCII 字符串（JSON → zlib → base64）。

    入参是 `longtext.PageLine`（也接受任何有 `top/bottom/left/text` 的同形对象）；
    空列表编码成空列表的合法编码（解码回来是 `[]`）。
    """
    rows = [
        [
            float(line.top),
            float(line.bottom),
            float(line.left),
            float(line.bottom) - float(line.top),
            str(line.text),
        ]
        for line in lines
    ]
    raw = json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(zlib.compress(raw, _ZLIB_LEVEL)).decode("ascii")


def decode_ocr_lines(source: Any) -> list[longtext.PageLine] | None:
    """`encode_ocr_lines` 的逆：坏数据 / 非本格式 → `None`（读路径不抛）。

    `None` / 空串 / 非字符串 / 非法 base64 / 非 zlib / 非 JSON / 行形状不对，
    全部安静回 `None`：调用方（段落类型派生、P2 重新分段）按「无原始输入」降级。

    额外护栏（防恶意/畸形数据，仍是「读路径不抛」）：
    - 解压后超过 `_MAX_DECODED_BYTES`（8MB）或压缩流未完整结束 → `None`；
    - 深嵌套 JSON 触发的 `RecursionError` 一并吞掉 → `None`。
    """
    if not isinstance(source, str) or not source:
        return None
    try:
        packed = base64.b64decode(source.encode("ascii"), validate=True)
        inflater = zlib.decompressobj()
        raw = inflater.decompress(packed, _MAX_DECODED_BYTES + 1)
        if (
            len(raw) > _MAX_DECODED_BYTES
            or inflater.unconsumed_tail
            or not inflater.eof
        ):
            return None
        rows = json.loads(raw.decode("utf-8"))
    except (ValueError, zlib.error, RecursionError):
        # binascii.Error（坏 base64）与 JSONDecodeError 都是 ValueError 的子类；
        # UnicodeDecodeError 也是；zlib.error 与 RecursionError 单独列。
        return None
    if not isinstance(rows, list):
        return None
    lines: list[longtext.PageLine] = []
    for row in rows:
        if not isinstance(row, list) or len(row) != _ROW_FIELDS:
            return None
        top, bottom, left, _height, text = row
        if not isinstance(text, str):
            return None
        try:
            lines.append(
                longtext.PageLine(
                    text=text,
                    top=float(top),
                    bottom=float(bottom),
                    left=float(left),
                )
            )
        except (TypeError, ValueError):
            return None
    return lines


def paragraph_kinds(
    format: Any,
    source: Any,
    breaks: Sequence[Any],
    text: str,
) -> list[str]:
    """按创建时的原始分段输入派生每段类型（spec §7.2）；派生不了就全 `body`。

    `breaks` / `text` 是**入库后**的 `passage_breaks` 与单行化全文（`text[start:end]`
    就是那一段）。两条路都**重跑同一套分段算法**（spec §7.5：不允许两套算法）：

    - `manual` → `longtext.segment_pasted(source)`；
    - `ocr` → `decode_ocr_lines(source)` → `longtext.segment_ocr(...)`。

    只有「段数一致 **且** 每段文本与 `text[start:end]` 逐字一致」才采用派生结果；
    否则（无 source / 坏数据 / 段数或文本不一致 / format 不认识）如实回落全 `body`。
    返回长度 == `len(breaks)`；`breaks` 为空 → `[]`。
    """
    spans = _spans(breaks)
    if not spans:
        return []
    segmentation = _resegment(format, source)
    if segmentation is None or len(segmentation.passages) != len(spans):
        return ["body"] * len(spans)
    for passage, (start, end) in zip(segmentation.passages, spans):
        if text[start:end] != passage.text:
            # 库里的 text 与原始输入重跑结果对不上（人工改库 / 算法已变）——
            # 不硬套类型，整篇回落 body（spec §6.7 的「不伪造」）。
            return ["body"] * len(spans)
    return [passage.kind for passage in segmentation.passages]


def _spans(breaks: Sequence[Any]) -> list[tuple[int, int]]:
    """`[[start, end), ...]` → `[(start, end), ...]`；形状不对 → `[]`。"""
    spans: list[tuple[int, int]] = []
    try:
        for item in breaks:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                return []
            spans.append((int(item[0]), int(item[1])))
    except (TypeError, ValueError):
        return []
    return spans


def _resegment(format: Any, source: Any) -> longtext.Segmentation | None:
    """重跑分段（`manual` 粘贴 / `ocr` 行框）；任何不可派生的情况回 `None`。"""
    if not isinstance(source, str) or not source:
        return None
    fmt = "" if format is None else str(format).strip().lower()
    if fmt == MANUAL_FORMAT:
        return longtext.segment_pasted(source)
    if fmt == OCR_FORMAT:
        lines = decode_ocr_lines(source)
        if lines is None:
            return None
        return longtext.segment_ocr(lines)
    return None
