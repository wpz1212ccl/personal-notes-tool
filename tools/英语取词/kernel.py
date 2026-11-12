"""英语取词 OCR 内核：屏幕矩形 → 英文文本（无界面常驻 TCP 服务）。

由 AHK 外壳拉起并守护（spec §10.1）。常驻是硬要求：`RecognizeAsync` 只要几毫秒，
但进程冷启动要 0.6–1.8s，每次捕获重新拉一个进程会直接毁掉「不打断节奏」。

协议、超时语义、失败留图见 docs/specs/capture-tool.md §5。
"""

import argparse
import asyncio
import configparser
import ctypes
import functools
import io
import json
import logging
import logging.handlers
import os
import re
import socketserver
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageFilter, ImageOps

import corrections
import longtext
import segmentation_source

TOOL_DIR = Path(__file__).resolve().parent
CONFIG_PATH = TOOL_DIR / "config.ini"
CONFIG_SECTION = "英语取词"
LOG_DIR = TOOL_DIR / "logs"
WORDS_PATH = TOOL_DIR / "data" / "common_words.txt"
WORDS_10K_PATH = TOOL_DIR / "data" / "words_10k.txt"
# #36：hub `--import-ecdict` 导出的纯单词表（可选，见 `correction_wordlists`）
WORDS_ECDICT_PATH = TOOL_DIR / "data" / "ecdict_words.txt"
FAILED_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "英语取词" / "failed"
# 留图保留期（票 #187 K5）：留图是屏幕截图 = 隐私残留面，只该留「排查最近失败」所需的
# 最少量 —— 超过保留期（7 天）或超过数量上限（50 张）的旧图在启动与每次落盘后清掉。
# 这两个值是常量不是 config 键：它们只影响「故障现场留多久」，没有按机器调的必要。
FAILED_KEEP_DAYS = 7
FAILED_KEEP_MAX_FILES = 50

DEFAULT_PORT = 47800
OCR_LANGUAGE = "en-US"  # 必须显式指定，不能靠系统默认语言（spec §5.1）
LOG_MAX_BYTES = 2 * 1024 * 1024
LOG_BACKUPS = 4

# spec §6.2 的双引擎策略：config 键 `ocr_engine`，代码里的默认值是 auto（config.ini 不动）。
OCR_ENGINE_DEFAULT = "auto"
OCR_ENGINE_MODES = ("auto", "win", "rapid")

# #94 P1：给 RapidOCR 的 ONNX 会话限线程。`rapidocr-onnxruntime==1.2.3` 的 `OrtInferSession`
# **不读任何线程配置键**（只认 use_cuda / model_path），所以补丁只能打在**我们自己的代码**里
# （`_cap_rapid_session_threads`）；改 site-packages 里的 config.yaml 会在 venv 按
# `requirements.txt` 重建后**静默失效** —— 功能正常、只是又变慢，所以要配 P0 的启动回读。
# 默认 `max(4, 核数//2)`：16 核 → 8（实测负载下墙钟 1.6–1.7×、空载代价 0–10%），
# 4 核 → 4 = 现状（弱机不激进）。config 键 `ocr_ort_threads` 可覆盖。
ORT_THREADS_MIN = 4
# 单会话顺序推理：inter_op 多线程只会互相踩（探针 `threads` 套就按 intra=N/inter=1 测的）。
ORT_INTER_THREADS = 1
# #94 P2：启动预热图的源行带宽度 —— 真机整宽段落的一条行带量级（放大增强后 ≈8060×120）。
WARM_UP_BAND_WIDTH = 1612
WARM_UP_BAND_HEIGHT = 24

# --- §5.1 预处理流水线的常量 ------------------------------------------------
PAD_PX = 6  # §5.1.1 抓屏矩形向外扩（物理像素）
BAND_PAD_PX = 4  # §5.1.2 紧裁到文本行带后再外扩
BAND_MIN_HEIGHT = 56  # §5.1.3 band 高的目标区间（下沿）
BAND_MAX_HEIGHT = 120  # 同上的上沿：已够大就不放大也不缩小
MAX_SCALE = 6.0  # §5.1.3 放大上限
MAX_OCR_DIM = 10000  # OcrEngine.MaxImageDimension，单边超过它引擎直接拒
_ROW_GAP = 2  # 行带合并：≤2 行的空隙（字母横向笔画的断口）仍算同一带
_MIN_BAND_ROWS = 4  # 比这更矮的「带」是噪声不是文本行
_MIN_BAND_INK = 16  # 带内墨太少同理（面积大但几乎空白）
_MIN_BAND_FILL = 0.03  # 墨占比低于它们的带不是文本（空带）
_MAX_BAND_FILL = 0.75  # 高于它的带是实心块（播放器进度条、纯色按钮），不是文本
# #103：pad 条带判「实心边框」的墨行占比门。窗口边框/深色桌面边贯穿整图高，
# 条带几乎每一行都有墨（贴边语料实测 1.00）；真实视频帧的边缘墨随画面内容变
# （u3 左条带实测 0.72、右 0.48），够不到这道门。落在中间的极端场景（视频噪点
# 恰好逐行有墨）与真边框不可分，按边框处理。
_BORDER_ROW_SHARE = 0.95
_LATIN_ASPECT = 0.85  # 方块字（汉字）团块宽高比 ≈1；窄于它的才算拉丁字母
# #101：没有词间空隙的行，「窄团块」要同时过两道门才算拉丁行（见 `_latin_like_line`）。
# 单独看中位团块宽在 24px 以上的长中文行上会被笔画内部的窄缝骗过（实测 0.71×行高），
# 再加一道墨量门：窄团块要占这行墨量的 ≥ 0.35。14–28px 实测拉丁词最低 0.45（`Why`），
# 汉字行最高 0.30（`海浪下面的森林一直是活着的` 24/28px）；0.35 落在两者中间。
# 已知边界：32px 的汉字笔画会碎出窄团块（`银行拒绝再借钱给他` 实测 0.44），这道门挡
# 不住 —— 本票语料只到 24px，32px 档没有语料，记作未验证（见 #101 汇报）。
_LATIN_NARROW_SHARE = 0.35
_WORD_GAP_RATIO = 0.2  # 词间空隙 ≥ 0.2 行高才算「按词排列」（拉丁文本的特征）
_ROW_VAR_FLOOR = 0.0004  # 行方差的绝对下限（0..1 尺度），纯色图不开带
# §5.1.2 的「有字行」签名（#97）：同一行里**同时**出现近白与近黑像素。烧进视频的字幕
# （白字 + 黑描边，或深底白字）与渲染文本（深字浅底）都满足；纯色条、进度条、糊成一片的
# 视频背景必缺其一。判据与极性无关，也不吃「行方差」的信号 —— 后者在真实视频帧上会失效：
# 实测一帧 B 站「实景英语」的 267 行里**每一行**都过了 `peak*0.18` 的方差门槛（杂背景的
# 方差就有 5807，而字幕行 15347），于是带检测必然把整张图当成一条带（见 `_tight_band_view`）。
# 比例是 2026-10-15 那批 U9 语料上选的，见 `make_ocr_video_fixtures.py`。
_EXTREME_BRIGHT = 240  # 「近白」门限（autocontrast 后，0..255）
_EXTREME_DARK = 15  # 「近黑」门限
_TEXT_ROW_RATIO = 0.3  # 一行的 min(近白数, 近黑数) 达到峰值这个比例才算「有字」
_TEXT_ROW_PAD_RATIO = 0.6  # 收紧后按行段高度上下留白（够到上伸/下伸笔画与描边）
_TIGHT_VIEW_GAIN = 0.75  # #97 补的紧取景至少要矮到这个比例以下，否则不值得多跑一次 OCR
_TIGHT_VIEW_MIN_PAD = 2.0  # 没有带时（`pick_bands` 空手而归）：图高 ≥ 行高 × 它才补取景
# §6.1.1：相邻拉丁行带的垂直间距 ≤ 2.0×行高，才算同一条字幕句、要拼进同一句；
# 更远的行（标题与正文、两条独立字幕）是各自独立的文本块，只取拉丁墨最多的那一块。
# 实测依据：用户真机两行字幕 a3d5c5 行间距 3px / 行高 48px = 0.06×、cc21a9 两行被
# 方差判成同一条带（间距 0），都远在 2.0× 之内；把阈值收紧到 0.5× 也不会误伤这两例，
# 但会给「行距偏大的两行字幕」留出安全余量（字幕行距一般 ≤ 1.5×），故按 spec 取 2.0。
_BAND_GROUP_GAP_RATIO = 2.0
PRE_SHARP = (
    1.2,
    200,
    2,
)  # 放大**前**的锐化（radius, percent, threshold），见 preprocess 注释
_MID_TONE_NOISE = 0.12  # 直方图中段的像素占比超过它 → 上采样后仍有噪声（§5.1.5）
_SCORE_TRUST = 0.6  # 候选 A 的词表命中率低于它才值得再跑 B（见 read_image 注释）
# §6.2 宽带规则（#76）：**原始**带宽 ≥ 它的行带按 [Windows, RapidOCR] 的顺序跑（平局判给
# Windows）。800 是 #74 在同一批段落语料上选的阈值。它是一条**几何近似，不是硬边界**：
# 语料扩宽后（600–1200px 的中间宽度、其他字号与 DPI）应当回来复核这个数，见 #76 与
# `tests/bench_ocr_paragraph.py` 的 U5w 门槛。
# ［#97 修订］这条规则原来还带一句「Windows 分够高就不跑 RapidOCR」——那句已删，理由见
# `DualEngine.texts_win_first`：真实视频帧上 Windows 会读错而分数照样过线，短路会把读对的
# RapidOCR 挡在门外。
WIDE_BAND_PX = 800
POLICY_AUTO = "auto"  # 一条带走 §6.2 的 auto：两路引擎都跑、词表打分择优（RapidOCR 在前）
POLICY_WIN_FIRST = "win_first"  # 宽带才用：Windows 在前（顺序即平局优先级），两路都跑
# §6.1.1 整段并带（#78）：≥2 条带（= 段落）时先把所有带的并集并成一条带喂**一次** Windows
# OCR，词表分 ≥ `_SCORE_TRUST` 就收工；不信任（或下面那道门不过）才回落到逐行 —— 逐行路径
# 就是今天的行为（含 #76 的宽带规则、#77 的多组保留），一行不改。
# 并带丢掉了「每行放大到 120px」，所以质量全押在**噪声门**上（§5.1.5 的 `residual_noise`，
# 见 `_block_first_read`）：块图糊掉就直接不走并带。#74 实测「只看分数门」在模糊小字上掉
# 质量（合成段落 WER 20.1%→23.0%、模糊宽段落 0.3%→11.8%），只有先过噪声门的那一档才让
# 两组语料都保持在现状水平（逐位相同）。三个数是 #74 原型里实测选的（块高 300 好于 120 与
# 不放大；宽度上限挡超宽段落被等比放大到引擎吃不下的尺寸）。
BLOCK_HEIGHT = 300  # 并带的目标块高：块不足它时按比例放大
BLOCK_MAX_SCALE = 4.0  # 并带的放大上限（独立于 §5.1.3 的 MAX_SCALE）
# 并带放大后的带宽上限：会超就整块不放大（等比放大只按高度，会失控）
BLOCK_MAX_WIDTH = 4000

# --- v21 长文模式：整页 OCR（规格 §3 / 票 #157）的常量 ------------------------
#
# 这一组数值全部来自 `docs/research/2026-10-26-long-article-ocr-page-pass.md` 的实测，
# 改任何一条之前先回去看那一节（§3.3 的自检分布、§5.3 的切片去重）。
PAGE_WORDS_PER_LINE_MIN = 8.0  # 自检信号一：正常档 10.1–13.8、崩溃档 4.8–6.0
PAGE_WEIRD_MAX = 0.12  # 自检信号二：正常档 ≤0.066、崩溃档 0.163–0.185
# 信号一的**下限保护**（T7 阶段 2a，实测依据见
# `docs/research/2026-10-27-longform-page-quality-bench.md` §5.4）：总词元 < 40 时不拿
# `words_per_line` 判低质 —— 单行/短选区的「每行词数」天然很小（真机 18 例里 4–7 个词元
# 的 5 例假阳性就是它），而真正的崩溃档词元数是 105（10px）/ 398（块状纹理），40 不会漏。
# `weird > 0.12` **不受**这条保护影响，始终参与判定。
PAGE_QUALITY_MIN_TOKENS_FOR_WPL = 40
# 2× 放大重读升级档（规格 §3.2 / §Further Notes A，实测见 T7 研究报告 §3）：
# - 只对 `quality == "low"` 的页触发一次；10px 70.0%→7.8%、Georgia 14px 3.6%→0.0%、
#   细噪纹理 81.7%→16.3% 能救回，块状/粗纹理 82.2%→70.0% 救不回（仍报 low）；
# - 代价是 1.7–3.2× 时间；干净页零收益，所以 1× ok 的页绝不升级；
# - 2× 后高度可能超引擎上限（9600 页 → 19200px），必须走下面的 `page_spans` 切片；
# - 2× 后总像素超预算就放弃升级、保留 1× 结果（同一道预算，见下）。
PAGE_ESCALATE_FACTOR = 2
# 整页路径的像素内存预算（票 #180 K3）：1× 抓屏与 2× 升级**共用同一道门** ——
# string_at 全图拷贝 + PIL 副本 + BMP 缓冲 + WinRT 位图在 3000 万像素以上会到几百 MB
# （审计 K3：3×4K 组合屏 ≈33M px 放行 → 峰值数百 MB），常驻内核不冒这个 OOM 风险。
PAGE_MAX_PIXELS = 30_000_000
# 2× 升级档沿用同一预算（升级判据在 `_escalated_read`，名字保留 T7 的出处）。
PAGE_ESCALATE_MAX_PIXELS = PAGE_MAX_PIXELS
# 超尺寸切片：单片高 9000（给 10000 的引擎上限留 1000px 余量）、相邻片重叠 100。
# 实测 9000/7000/5000 三种切片高度去重后逐字收敛到同一份 3755 词（WER 0.0%）。
PAGE_SLICE_HEIGHT = 9000
PAGE_SLICE_OVERLAP = 100
# `page_text` 单块字符上限（规格 §3.1）。`RecvText` 的 UTF-8 跨块截断会静默产生 U+FFFD
# （#156），所以正文必须按字符分块取、单块远小于 socket 缓冲。
PAGE_TEXT_CHUNK = 16384
# page_ocr 的总墙钟预算（票 #180 K4）：外壳 20s 就放弃并丢结果
# （lib/LongformPanel.ahk `LF_LONG_TIMEOUT := 20000`），内核留 2s 给 RPC 往返与正文分块取回；
# 升级档只有当「已用 + 估算 2×」装得进剩余预算才触发，别明知道装不下还白算一整轮。
PAGE_WALL_BUDGET_S = 18.0
# 2× 重读的耗时估算系数：取实测倍数的上界（T7 研究报告 §3：1.7–3.2×）再留一点余量。
PAGE_ESCALATE_TIME_FACTOR_MAX = 3.5
# page_ocr 选区的越界容忍（票 #187 K9）：选区与虚拟屏幕的**交集**不足选区一半（含完全
# 不相交）→ 直接报错 —— 夹回来的是大半黑边，不是用户选的内容；小半越界才值得夹救。
PAGE_OFFSCREEN_MAX_RATIO = 0.5

log = logging.getLogger("kernel")

_ENGINE_LOCK = threading.Lock()  # 一个 OcrEngine 同时只跑得动一个 RecognizeAsync
# 引擎识别包的墙钟上限（票 #180 K4）：实测整页 p95 221ms（ADR 0008）、外壳 20s 就放弃
# （英语取词.ahk `G_OcrTimeout` / lib/LongformPanel.ahk `LF_LONG_TIMEOUT` 均为 20000ms），
# 15s 是内核侧兜底 —— 正常路径碰不到，引擎挂死时锁最迟 15s 后释放、后续请求照常可用。
OCR_ASYNC_TIMEOUT_S = 15.0

Responder = Callable[[Any], dict]


# --- 响应体拼装与小工具 ------------------------------------------------------


def round_ms(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)


def parse_rect(rect: Any) -> tuple[int, int, int, int]:
    """校验 [x, y, w, h]（物理像素，原点为虚拟屏幕左上角）。"""
    if not isinstance(rect, (list, tuple)) or len(rect) != 4:
        raise ValueError("rect 必须是 [x, y, w, h]")
    try:
        x, y, w, h = (int(value) for value in rect)
    except (TypeError, ValueError):
        raise ValueError("rect 的四个值必须是整数") from None
    if w < 1 or h < 1:
        raise ValueError("rect 的宽高必须为正")
    return x, y, w, h


# --- §5.1 预处理流水线 -------------------------------------------------------
#
# v1 是「抓屏 → 高度 <150px 就 LANCZOS 整数倍放大 → 直接喂引擎」，在 B 站双语字幕
# （深底浅字、低分辨率、JPEG 噪声）上系统性出错（issue #22 的失败样本）。v2 按
# spec §5.1 走：外扩 → 文本行带 → 放大到 [56,120] → 灰度/对比度/锐化/极性归一。


def virtual_screen_bounds() -> tuple[int, int, int, int]:
    """虚拟屏幕 (x, y, w, h)；多显示器时原点可为负（同 `all_screens=True`）。"""
    return (
        _user32.GetSystemMetrics(76),  # SM_XVIRTUALSCREEN
        _user32.GetSystemMetrics(77),  # SM_YVIRTUALSCREEN
        _user32.GetSystemMetrics(78),  # SM_CXVIRTUALSCREEN
        _user32.GetSystemMetrics(79),  # SM_CYVIRTUALSCREEN
    )


def pad_rect(
    rect: tuple[int, int, int, int],
    pad: int = PAD_PX,
    bounds: tuple[int, int, int, int] | None = None,
) -> tuple[int, int, int, int]:
    """向外扩 pad 像素，夹在虚拟屏幕内（spec §5.1.1）。

    外扩在**抓屏之前**做：用户拖框贴着字的上/下沿时，v1 直接把字切掉一半
    （本机 failed/20261012-191647.png 就是整行只剩上半截）。贴边的方向不越界，
    否则 BitBlt 会在屏幕外补黑边，反而喂给引擎一条黑带。
    """
    x, y, w, h = rect
    bx, by, bw, bh = bounds if bounds is not None else virtual_screen_bounds()
    left = max(0, min(pad, x - bx))
    top = max(0, min(pad, y - by))
    right = max(0, min(pad, bx + bw - (x + w)))
    bottom = max(0, min(pad, by + bh - (y + h)))
    return (x - left, y - top, w + left + right, h + top + bottom)


def clamp_page_rect(
    rect: tuple[int, int, int, int],
    bounds: tuple[int, int, int, int] | None = None,
) -> tuple[int, int, int, int]:
    """page_ocr 选区夹进虚拟屏幕（票 #187 K9；capture 路径的 `pad_rect` 是它的参照）。

    BitBlt 在屏幕外的区域补**黑边**：越界部分进 OCR 就是纯噪声，还可能把质量门打到
    low、白付一次 2× 升级（审计 K9）。正常拖框走不到越界（外壳遮罩罩住整个虚拟屏幕，
    票 #184 A6），这是给「选区带着过期坐标来」的兜底（显示器拔了/分辨率变了）。夹取
    口径与 #184 的 `Logic.ClampRectInMonitor` 一致 —— 都是虚拟屏幕坐标系（物理像素、
    原点可为负）里做边界收口，这里按长文语义取「保留交集、裁掉出界部分」：
    屏幕外的部分没有内容，Shift 到屏内反而会读用户没选的东西。

    - 完全在屏幕内 → 原样返回（零行为变更）；
    - 小半越界（交集 ≥ `PAGE_OFFSCREEN_MAX_RATIO` × 选区）→ 夹回交集；
    - 交集不足一半（含完全不相交）→ `ValueError`：夹回来的是大半黑边，明确报错比
      静默产出一份用户没见过的图诚实。
    """
    x, y, w, h = rect
    bx, by, bw, bh = bounds if bounds is not None else virtual_screen_bounds()
    left = max(x, bx)
    top = max(y, by)
    right = min(x + w, bx + bw)
    bottom = min(y + h, by + bh)
    if (right - left) * (bottom - top) < w * h * PAGE_OFFSCREEN_MAX_RATIO:
        raise ValueError(
            f"选区大半在屏幕外：({x}, {y}, {w}, {h}) 与虚拟屏幕 "
            f"({bx}, {by}, {bw}, {bh}) 的交集不足一半"
        )
    return (left, top, right - left, bottom - top)


def otsu_threshold(gray: Image.Image) -> int:
    """Otsu 阈值：让两类（文字/背景）类间方差最大的那个灰度界（256 次循环）。"""
    hist = gray.histogram()[:256]
    total = sum(hist)
    if total == 0:
        return 128
    sum_all = sum(level * count for level, count in enumerate(hist))
    weight_low = 0
    sum_low = 0
    best_variance = -1.0
    best = 128
    for level, count in enumerate(hist):
        weight_low += count
        if weight_low == 0:
            continue
        weight_high = total - weight_low
        if weight_high == 0:
            break
        sum_low += level * count
        mean_low = sum_low / weight_low
        mean_high = (sum_all - sum_low) / weight_high
        variance = weight_low * weight_high * (mean_low - mean_high) ** 2
        if variance > best_variance:
            best_variance = variance
            best = level
    return best


def is_dark_background(gray: Image.Image) -> bool:
    """背景是深色吗（深底浅字需要反相成深字浅底）。

    spec §5.1.4 写「以 band 内像素中位数为界」——中位数在双峰图里必然落在**某个峰
    的内部**，拿它当分界会把两个类都判到同一侧（深色像素占多数时中位数本身就是深色
    值，`>=` 与 `<` 的计数都算不出背景在哪边）。这里改用直方图众数：字幕/文档截图里
    背景像素占绝大多数，众数就是背景色。代价一样是 O(256)，判据等价且不会两难。
    """
    hist = gray.histogram()[:256]
    mode = max(range(256), key=lambda level: hist[level])
    return mode < 128


def _row_variance(auto: Image.Image) -> list[float]:
    """逐行方差（0..1 尺度）：E[x²]−E[x]²。

    用两次 C 层 BOX 重采样把整图压成「每行均值」，避免逐行 crop/stat 的 Python 开销
    （一行一次 crop 在本机能吃掉几毫秒，而这是每次捕获都要走的路）。压成 1 列之后
    用 `tobytes()` 直接拿每行的值（L 模式一字节一像素），不用逐像素 API。
    """
    height = auto.height
    squares = auto.point(lambda value: value * value // 255)
    means = auto.resize((1, height), Image.Resampling.BOX).tobytes()
    mean_squares = squares.resize((1, height), Image.Resampling.BOX).tobytes()
    return [
        max(0.0, ms / 255.0 - (mean / 255.0) ** 2)
        for mean, ms in zip(means, mean_squares)
    ]


def _row_ink(mask: Image.Image) -> list[int]:
    """逐行墨量（像素数）：BOX 压到 1 列，值 × 图宽 / 255。"""
    width = mask.width
    values = mask.resize((1, mask.height), Image.Resampling.BOX).tobytes()
    return [round(value * width / 255.0) for value in values]


def _rows_to_bands(rows: list[int]) -> list[tuple[int, int]]:
    """连续行号 → 行带（≤ _ROW_GAP 的空隙合并），闭区间。"""
    bands: list[tuple[int, int]] = []
    for row in rows:
        if bands and row - bands[-1][1] <= _ROW_GAP + 1:
            bands[-1] = (bands[-1][0], row)
        else:
            bands.append((row, row))
    return bands


def _ink_mask(auto: Image.Image, dark_background: bool) -> Image.Image:
    """文字为白的二值掩图：深底取亮侧、浅底取暗侧（阈值都是 Otsu）。"""
    threshold = otsu_threshold(auto)
    if dark_background:
        return auto.point(lambda value: 255 if value > threshold else 0)
    return auto.point(lambda value: 255 if value < threshold else 0)


def _column_runs(
    mask: Image.Image, box: tuple[int, int, int, int], gap_floor: int
) -> tuple[list[tuple[int, int]], int]:
    """按列墨量切团块 → ([(宽度, 墨量)], 宽空隙数)。

    空隙 = 连续若干列墨低于阈值；宽度 ≥ gap_floor 的算「词间空隙」（拉丁文本的签名）。
    """
    left, top, right, bottom = box
    height = max(1, bottom - top)
    columns = mask.crop(box).resize((right - left, 1), Image.Resampling.BOX).tobytes()
    floor = max(1, round(0.05 * height))
    runs: list[tuple[int, int]] = []
    wide_gaps = 0
    width = 0
    ink = 0
    gap = 0
    for value in columns:
        column_ink = round(value * height / 255.0)
        if column_ink >= floor:
            if gap >= gap_floor:
                wide_gaps += 1
            gap = 0
            width += 1
            ink += column_ink
        else:
            gap += 1
            if width:
                runs.append((width, ink))
                width = 0
                ink = 0
    if width:
        runs.append((width, ink))
    if gap >= gap_floor:
        wide_gaps += 1
    return runs, wide_gaps


def _band_metrics(
    mask: Image.Image, box: tuple[int, int, int, int]
) -> tuple[int, int, int]:
    """带的三项指标：拉丁分、总墨量、面积（spec §5.1.2 的择优键）。

    拉丁文本的两个形状特征，都不用认字：

    1. **词间空隙**：一行英文有 3–8 个空格（≥0.2 行高的空列），中文字幕行一个都没有。
       带内出现过这种空隙，就说明它是按词排的 —— 整带的墨都算拉丁。
    2. **窄团块**：没有词间空隙时（单个词、模糊把字母粘成一片），只认「窄于 0.85 行高」
       的团块（拉丁字母比汉字窄）。汉字的团块接近正方形（宽 ≈ 行高），几乎不得分。

    纯色条、按钮这类实心块两条特征都不满足（整块一个宽团块），再被 `_MAX_BAND_FILL` 挡掉。
    """
    left, top, right, bottom = box
    height = max(1, bottom - top)
    runs, wide_gaps = _column_runs(mask, box, max(3, round(_WORD_GAP_RATIO * height)))
    total = sum(ink for _, ink in runs)
    if wide_gaps:
        latin = total
    else:
        latin = sum(ink for width, ink in runs if width < _LATIN_ASPECT * height)
    return latin, total, (right - left) * height


def _edge_is_border(mask: Image.Image, left: bool) -> bool:
    """#103：某一侧的 pad 条带是不是「实心边框」——条带内 ≥ `_BORDER_ROW_SHARE`
    的行都有墨（窗口边框/深色桌面边贯穿整图高）。真实视频帧的边缘墨是画面内容，
    逐行变化，过不了这道门。"""
    if mask.width <= 2 * PAD_PX:
        return False
    box = (0, 0, PAD_PX, mask.height) if left else (mask.width - PAD_PX, 0, mask.width, mask.height)
    values = mask.crop(box).resize((1, mask.height), Image.Resampling.BOX).tobytes()
    ink_rows = sum(1 for value in values if value > 0)
    return ink_rows >= _BORDER_ROW_SHARE * mask.height


def _band_scan(
    img: Image.Image,
) -> tuple[list[tuple[int, int, tuple[int, int, int, int], bool]], Image.Image | None]:
    """图里每一条「像拉丁文本行」的带：(拉丁分, 面积, 紧包围盒, 有无词间空隙)，按 y 序。

    顺带把这张图的墨掩图交给 `pick_bands`：D3 修完后判「单行成词是不是拉丁行」要用它
    （`_latin_like_line`），同一张图重算一次 autocontrast + Otsu 实测 1.2ms（p10，
    439×256），所以扫描时一次算好。

    spec §5.1.2：外扩后的裁剪图里可能有中文字幕行、播放器控件、画面边缘，把它们踢
    出去的办法是「方差分段 → 每段紧裁 → 各段按拉丁字母像素/面积过滤」。

    方差只当**种子**：它挑出笔画密集的行（x-height 带），而字的上伸/下伸笔画（`l` 的
    竖、`y` 的尾）方差低，只按方差裁会把字的上下切掉 —— 大字上尤其致命（实测把
    「裁 band」单步的 4 例大字从 0% 打到 56% WER，见 issue #22）。所以种子行的上下
    再按「墨是否连续」扩展，紧包围盒取扩展后的整段。

    第四项「有无词间空隙」见 `_has_word_gaps`：单行选择不看它（保留单个词/模糊成一片
    的窄团块回退），多行拼接也不再只认它（#101：单行成词的拉丁行没有词间空隙，但它
    仍是窄团块，见 `_latin_like_line`）。

    ［#97］这里的带在真实视频帧上会「跑飞」：杂背景让几乎每一行都过方差门槛，`_extend_rows`
    的「有墨就扩」于是穿过整张图，带 ≈ 整张图 = 没定位到行。**不在这一层修**——在这里收紧
    会把低对比度的行（u4 的上行：白字压在浅灰底上，没有近黑像素）裁掉，实测把 u4 从逐字
    全对打成 WER 70%。改由 `read_image` 在「带比行带上限还高」时**多准备一个取景**
    （`_tight_band_view`）一起交给词表仲裁：原取景在候选里排前面、平分时取第一个最大值，
    所以「收紧后更差」的取景不会被选中，定位失败的图至多白跑一次 OCR，不会比现状更差。
    """
    gray = img.convert("L")
    auto = ImageOps.autocontrast(gray, cutoff=0.5)
    mask = _ink_mask(auto, is_dark_background(auto))
    # ［#103］判「文字行」的两个剖面（方差种子、行墨扩展）默认全宽；某一侧 pad 条带
    # 是「实心边框」（`_edge_is_border`：≥95% 的行有墨）时剔掉那一侧再算。贴边捕获
    # （矩形框住整个窗口，`pad_rect` 外扩 6px 摄进反差桌面）时边框列不剔会让两条判据
    # 一起失效——每一行都有墨（`_extend_rows` 把整图粘成一条带）、「黑边+白心」的双极
    # 分布让空白窗行方差也过种子门（窄图整图皆种子）——两条都丢前几行。真实视频帧
    # 的边缘墨不是边框（u3 实测 0.72/0.48），保持全宽：那一行边缘像素是「真墨」，
    # 剔掉会让带沿收紧而伤读数（u3 首带曾因此收 1px、读数多出一个 I）。
    cut_left = _edge_is_border(mask, left=True)
    cut_right = _edge_is_border(mask, left=False)
    inner = (PAD_PX if cut_left else 0, 0, mask.width - (PAD_PX if cut_right else 0), mask.height)
    variance = _row_variance(auto.crop(inner))
    row_ink = _row_ink(mask.crop(inner))
    peak = max(variance, default=0.0)
    if peak <= _ROW_VAR_FLOOR:
        return [], None
    rows = [
        i
        for i, value in enumerate(variance)
        if value >= max(peak * 0.18, _ROW_VAR_FLOOR)
    ]
    candidates: list[tuple[int, int, tuple[int, int, int, int], bool]] = []
    for top, bottom in _rows_to_bands(rows):
        top, bottom = _extend_rows(row_ink, top, bottom)
        box = mask.crop((0, top, mask.width, bottom + 1)).getbbox()
        if box is None:
            continue
        tight = (box[0], box[1] + top, box[2], box[3] + top)
        height = tight[3] - tight[1]
        if height < _MIN_BAND_ROWS:
            continue
        latin, ink, area = _band_metrics(mask, tight)
        fill = ink / area if area else 0.0
        if latin < _MIN_BAND_INK or not _MIN_BAND_FILL <= fill <= _MAX_BAND_FILL:
            continue
        candidates.append((latin, area, tight, _has_word_gaps(mask, tight)))
    candidates.sort(key=lambda item: (item[2][1], item[2][0]))
    return candidates, mask


def _band_candidates(
    img: Image.Image,
) -> list[tuple[int, int, tuple[int, int, int, int], bool]]:
    """`_band_scan` 的只要带的版本（`tests/bench_ocr.py` 的冻结列还在用这个形状）。"""
    return _band_scan(img)[0]


def _has_word_gaps(mask: Image.Image, box: tuple[int, int, int, int]) -> bool:
    """带内是否出现过「词间空隙」（≥ _WORD_GAP_RATIO 行高的空列）—— 拉丁文本的签名。

    中文行没有词间空格，这一项恒为 False；拉丁行（哪怕是模糊字幕）总有 1 个以上的
    空格。`_band_metrics` 的拉丁分只保证候选够「像字」，单独用它区分不开汉字与粘成
    一团的英文，所以多行拼接还要它；但单行成词的拉丁行（`Steps:` / `Why`）也没有
    词间空隙，不能只靠它判去留（#101，见 `_latin_like_line`）。
    """
    height = max(1, box[3] - box[1])
    _, wide_gaps = _column_runs(mask, box, max(3, round(_WORD_GAP_RATIO * height)))
    return wide_gaps > 0


def _latin_like_line(mask: Image.Image, box: tuple[int, int, int, int]) -> bool:
    """没有词间空隙的带是否仍像拉丁行：团块要**又窄又占墨**（两道门，见下）。

    单行成词（`Steps:` / `Why`）与中文行都没有词间空隙，靠 `_has_word_gaps` 分不开；
    能分开的是团块形状：拉丁字母窄于行高，汉字是方块字（团块宽 ≈ 行高）。判据：

    1. 列团块的**中位宽度** < `_LATIN_ASPECT` × 行高（不另拍新阈值）；
    2. 窄团块（宽 < `_LATIN_ASPECT` × 行高）的墨量 ≥ `_LATIN_NARROW_SHARE` × 总墨量。

    20px、深底的实测（#101 的 9 种写法矩阵）：

    | 行 | 中位团块宽 / 行高 | 窄团块墨量占比 |
    |---|---|---|
    | `Steps:` | 0.42 | 1.00 |
    | `Why` | 0.45 | 0.48 |
    | `银行拒绝再借钱给他` | 1.00 | 0.12 |
    | `海浪下面的森林一直是活着的` | 0.90 | 0.14 |

    只看**中位数**（不用墨量占比）不够：`Why` 的大写 W 比行高还宽（18px），窄团块只占
    48% 的墨，但它仍是拉丁行；只看占比也不够：24px 以上的长中文行，汉字笔画之间的窄缝
    会把中位团块宽压到 0.71×行高（实测 `海浪…` 24px），墨量占比 0.30 才把它挡回去。
    两道门一起用，14–28px 的实测样本里拉丁词最低 0.45、汉字行最高 0.30，中间没有歧义。
    已知边界：32px 的汉字笔画会碎出足够多的窄团块（`银行拒绝再借钱给他` 实测中位 0.62、
    占比 0.44），会被这里当成拉丁行；本票语料只到 24px，这一档未验证。
    """
    height = max(1, box[3] - box[1])
    runs, _ = _column_runs(mask, box, max(3, round(_WORD_GAP_RATIO * height)))
    if not runs:
        return False
    widths = sorted(width for width, _ in runs)
    if widths[len(widths) // 2] >= _LATIN_ASPECT * height:
        return False
    total = sum(ink for _, ink in runs)
    narrow = sum(ink for width, ink in runs if width < _LATIN_ASPECT * height)
    return total > 0 and narrow >= _LATIN_NARROW_SHARE * total


def _coalesce_lines(
    bands: list[tuple[int, int, tuple[int, int, int, int], bool]],
) -> list[tuple[int, int, tuple[int, int, int, int], bool]]:
    """把纵向重叠/几乎相接（≤ _ROW_GAP）的带并成「同一条行」，按 y 序回。

    同一条正文行常被方差切成 2–3 段（笔画疏密不同、上下伸笔画各成一段），不并掉就会
    把同一行 OCR 好几遍、再拼成重复的句子。并的是**行内的碎片**，不是不同的行：
    真正的两行字幕中间隔着一行高相关的空档，不会被这里并掉。
    """
    lines: list[tuple[int, int, tuple[int, int, int, int], bool]] = []
    for latin, area, box, spaced in bands:
        if lines and box[1] <= lines[-1][2][3] + _ROW_GAP:
            prev_latin, prev_area, prev_box, prev_spaced = lines[-1]
            lines[-1] = (
                prev_latin + latin,
                prev_area + area,
                (
                    min(prev_box[0], box[0]),
                    min(prev_box[1], box[1]),
                    max(prev_box[2], box[2]),
                    max(prev_box[3], box[3]),
                ),
                prev_spaced or spaced,
            )
        else:
            lines.append((latin, area, box, spaced))
    return lines


def pick_bands(img: Image.Image) -> list[tuple[int, int, int, int]]:
    """选中**含拉丁文本的每一组**里每一行的紧包围盒（含 BAND_PAD_PX），按 y 从上到下。

    spec §6.1.1：框选区域内若有 ≥2 条拉丁文本行、且相邻行的垂直间距不超过行高的
    `_BAND_GROUP_GAP_RATIO` 倍，就属于同一条字幕句，**都要**返回（不得只留一行）。做法：
    取所有行带 → 合并行内碎片 → 判每一行是不是拉丁行（有词间空隙，或团块窄于
    `_LATIN_ASPECT`×行高的单行成词）→ 把纵向相邻（间距 ≤ 阈值）的行归成一句 →
    回每一句每一行的盒。

    ［#77 修订］原实现只回「拉丁墨最多的那一句」，于是中文行把段落切成多块时**末块整组被
    丢**（#74 `p12` 实测 6 行只回 5 行）。改为**含拉丁墨的组全部保留**：`pool` 此刻全是
    拉丁行，所以每个组都含拉丁墨、全部回；纯 CJK 组照旧丢弃 —— 单行捕获与「英上中下」
    双语字幕的既有行为逐字不变。

    ［#101 修订］原实现的 `pool = spaced or lines` 只认「这一行**自己**有词间空隙」，
    于是 `Steps:` / `Why` 这种单行成词的标题被整行丢掉，而 `Note` / `Introduction`
    留下 —— 去留只看字母间距是否恰好越过 `max(3, 0.2×行高)`，是巧合不是设计（D3）。
    现在去留按**组**判：先把所有候选行按垂直间距分组，组内只要有 1 行是拉丁行，整组
    就按拉丁段落处理；组内的方块字行仍要自己过 `_latin_like_line` 这一关，否则双语
    字幕里紧贴英文的中文行会被整组放回来（§6.1.2 的「中文行不进结果」不变）。
    组内一行都没有拉丁墨时（纯中文行、或模糊到既判不出空格也判不出窄团块）才退回
    「选拉丁墨最多的一组」的老行为，那一档本来就分不清组。

    找不到可信的带（纯色、噪声、竖排）回 []，调用方退回整张图 —— 不允许「带定位」比
    v1 更差。
    """
    bands, mask = _band_scan(img)
    lines = _coalesce_lines(bands)
    if not lines or mask is None:
        return []
    groups: list[list[tuple[int, int, tuple[int, int, int, int], bool]]] = []
    for line in lines:
        if groups:
            prev_box = groups[-1][-1][2]
            gap = line[2][1] - prev_box[3]
            line_height = min(prev_box[3] - prev_box[1], line[2][3] - line[2][1])
            if gap <= _BAND_GROUP_GAP_RATIO * max(1, line_height):
                groups[-1].append(line)
                continue
        groups.append([line])
    latin_groups: list[list[tuple[int, int, tuple[int, int, int, int], bool]]] = []
    for group in groups:
        latin = [line for line in group if line[3] or _latin_like_line(mask, line[2])]
        if latin:
            latin_groups.append(latin)
    if latin_groups:
        # #77：含拉丁墨的组全部保留（组内合并与 y 序不变）。
        return [
            _expand_box(box, BAND_PAD_PX, img.size)
            for group in latin_groups
            for _, _, box, _ in group
        ]
    # 一组拉丁墨都没有：退回 §5.1.2 的择优键 —— 整句拉丁字母像素最多，其次总面积最大
    best = max(
        groups,
        key=lambda group: (
            sum(latin for latin, _, _, _ in group),
            sum(area for _, area, _, _ in group),
        ),
    )
    return [_expand_box(box, BAND_PAD_PX, img.size) for _, _, box, _ in best]


def pick_band(img: Image.Image) -> tuple[int, int, int, int] | None:
    """选中「最像拉丁文本的一句」的紧包围盒（多行时是这一句的并集盒）；找不到回 None。

    单行场景返回的就是那一行的盒（与 v2 一致）；两行字幕返回盖住两行的盒 —— 供
    `preprocess` 这类单图调用方使用。`read_image` 需要逐行识别时用 `pick_bands`。
    """
    boxes = pick_bands(img)
    if not boxes:
        return None
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def _extend_rows(row_ink: list[int], top: int, bottom: int) -> tuple[int, int]:
    """把种子行段向上下扩到「墨连续的边界」（字的上伸/下伸笔画方差低，但不会缺墨）。

    判据是**有墨就扩**（>0），不是「墨够多」：下伸笔画的尾巴可能只占图宽的一两个像素
    （2726px 宽的图里 170px 字的 'y' 尾巴每行 11 px），按比例设下限会刚好把它切掉 ——
    b02 那例就是这么把 "you" 认成 "VOLI" 的。掩图本身是 Otsu 二值化的，噪声行很少，
    所以「有墨就扩」在这条链路上比按比例设限更稳。

    ［#97 边界］这条判据的前提是「噪声行很少」，只在**干净底**上成立：真实视频帧的杂
    背景让 Otsu 掩图几乎每行都有上千个"墨"像素（实测背景行 900+ vs 字幕行 1100），
    扩展于是穿过整张图。所以「带比 `BAND_MAX_HEIGHT` 还高」这条判据看的就是扩展跑飞的
    结果，`read_image` 据此补一个紧取景（`_tight_band_view`）。
    """
    while top > 0 and row_ink[top - 1] > 0:
        top -= 1
    while bottom + 1 < len(row_ink) and row_ink[bottom + 1] > 0:
        bottom += 1
    return top, bottom


def _text_row_span(auto: Image.Image) -> tuple[int, int] | None:
    """「有字」的行段（闭区间，含首尾）；判据不成立时回 None。

    一行的「有字分」= min(近白像素数, 近黑像素数)（见 `_EXTREME_*` 的注释）：打印出来的
    字一定同时给出两极 —— 字与底（或字与描边）各占一侧，而纯色条、进度条、糊掉的视频
    背景只有一侧。取**峰值的三成**当行门槛（行内的抗锯齿细边、笔画稀疏处给不到峰值），
    再把行段按高度上下留白 `_TEXT_ROW_PAD_RATIO`，够到上伸/下伸笔画与描边。

    这条判据是给**定位失败**用的兜底（`_tight_band_view` 只在带跑飞时调用它）：
    它不指望分开相邻两行（两行都是「有字」行，会被并成一段 —— 那正是多行字幕要的），
    只负责把「框里除了字幕还有一大片视频画面」这部分切掉。找不到字（纯色图、灰底灰字
    这类没有极端值的渲染）回 None，调用方保持原样，**不允许比不收紧更差**。
    """
    height, width = auto.height, auto.width
    bright = (
        auto.point(lambda value: 255 if value >= _EXTREME_BRIGHT else 0)
        .resize((1, height), Image.Resampling.BOX)
        .tobytes()
    )
    dark = (
        auto.point(lambda value: 255 if value <= _EXTREME_DARK else 0)
        .resize((1, height), Image.Resampling.BOX)
        .tobytes()
    )
    score = [
        min(bright_count * width / 255.0, dark_count * width / 255.0)
        for bright_count, dark_count in zip(bright, dark)
    ]
    peak = max(score, default=0.0)
    if peak <= 0:
        return None
    rows = [row for row, value in enumerate(score) if value >= peak * _TEXT_ROW_RATIO]
    if not rows:
        return None
    return rows[0], rows[-1]


def _expand_box(
    box: tuple[int, int, int, int], pad: int, size: tuple[int, int]
) -> tuple[int, int, int, int]:
    """包围盒向外扩 pad，夹在图像内（右/下边界是开区间）。"""
    left, top, right, bottom = box
    width, height = size
    return (
        max(0, left - pad),
        max(0, top - pad),
        min(width, right + pad),
        min(height, bottom + pad),
    )


def band_scale(band_height: int) -> float:
    """§5.1.3：把 band 高送进 [56, 120]；已够大就不放大也不缩小；上限 6×。

    瞄准区间**上沿**（120）而不是下沿：同一批语料上实测「放大到 56px」明显差于
    「放大到 120px」（引擎吃像素，模糊字幕尤其明显；56/88/120 三档的对照见 issue
    #22 的基准表）。仍在 §5.1.3 的区间内，只是把「送到区间」取成「送满区间」。
    """
    if band_height < 1 or band_height >= BAND_MIN_HEIGHT:
        return 1.0
    return min(MAX_SCALE, BAND_MAX_HEIGHT / band_height)


def _resize_band(band: Image.Image) -> Image.Image:
    """按 band_scale 放大；单边不超引擎上限。

    用 LANCZOS 而不是 BICUBIC：同一批语料上 LANCZOS 的锐化边缘明显更利于引擎（单例
    对照 0.22 vs 0.56 WER），spec §5.1.3 的括注说的是「允许非整数倍」，插值核按
    实测挑（见 issue #22 的基准表）。
    """
    scale = band_scale(band.height)
    if band.width and band.width * scale > MAX_OCR_DIM:
        scale = MAX_OCR_DIM / band.width  # 超宽图（多显示器整屏）先保宽度
    if scale == 1.0:
        return band
    return band.resize(
        (max(1, round(band.width * scale)), max(1, round(band.height * scale))),
        Image.Resampling.LANCZOS,
    )


def enhance(band: Image.Image) -> Image.Image:
    """§5.1.4：灰度 → autocontrast(cutoff=0.5) → UnsharpMask(1.2, 140, 3) → 极性归一。

    极性在这里**重新判一次**（`pick_band` 判过一次，那是给掩图用的、判在紧裁前的图上）：
    两次的输入图不同（紧裁 + 放大 + 增强会改变直方图），用同一个判据各自判，比把前一次
    的结论传下来更不容易错；代价是一次 O(256) 直方图。
    """
    gray = ImageOps.autocontrast(band.convert("L"), cutoff=0.5)
    gray = gray.filter(ImageFilter.UnsharpMask(radius=1.2, percent=140, threshold=3))
    if is_dark_background(gray):
        gray = ImageOps.invert(
            gray
        )  # 深底浅字 → 深字浅底，引擎两种极性都能读但这样更稳
    return gray


def binarize(gray: Image.Image) -> Image.Image:
    """§5.1.5：Otsu 二值化（调用前极性已归一，文字恒为黑）。"""
    threshold = otsu_threshold(gray)
    return gray.point(lambda value: 0 if value <= threshold else 255)


def residual_noise(gray: Image.Image) -> bool:
    """§5.1.5 的「上采样后仍有噪声」判据：直方图中段还有大量像素（模糊/JPEG 晕开）。

    干净渲染的字是双峰的（墨／底），中段只剩抗锯齿的细边（实测 f16 干净图 4%、
    2px 模糊 + q55 的图 25%）。中段占比高说明边缘被抹开 —— 这正是二值化可能有益的
    场合，也可能是二值化把笔画切碎的场合，所以只当**门**用，好不好由 §5.2 的择优定。
    """
    hist = gray.histogram()[:256]
    total = sum(hist)
    if total == 0:
        return False
    threshold = otsu_threshold(gray)
    if threshold >= 255:
        return False
    ink_mode = max(range(threshold + 1), key=lambda level: hist[level])
    background_mode = (
        threshold
        + 1
        + max(range(255 - threshold), key=lambda offset: hist[threshold + 1 + offset])
    )
    span = background_mode - ink_mode
    if span < 32:  # 两极没分开（纯色图、极低对比）：无所谓噪声，不上二值化
        return False
    low = round(ink_mode + 0.15 * span)
    high = round(background_mode - 0.15 * span)
    return sum(hist[low : high + 1]) / total > _MID_TONE_NOISE


def _prepare_band(band: Image.Image) -> Image.Image:
    """一条已紧裁的 band → **放大前锐化** → 放大到 [56,120] → 灰度/对比度/锐化/极性归一。

    放大前先锐化这一步是实测加的（spec §5.1.4 的锐化只写在放大之后）：源图是模糊的
    小字时，先把边缘在**原分辨率**上恢复出来再插值，比插值后再锐化好得多 —— 同一批
    46 例合成语料，v1 63.7% → 「放大后锐化」58.6% → 「放大前也锐化」45.3%（详见
    issue #22 的基准表）。
    """
    if band.height < BAND_MIN_HEIGHT:  # 要放大才值得在放大前锐化（够大的大字不必锐化）
        band = band.filter(
            ImageFilter.UnsharpMask(
                radius=PRE_SHARP[0], percent=PRE_SHARP[1], threshold=PRE_SHARP[2]
            )
        )
    return enhance(_resize_band(band))


def preprocess(
    img: Image.Image, box: tuple[int, int, int, int] | None = None
) -> Image.Image:
    """§5.1 的整条流水线（候选 A = 不二值化）：

    文本带 → **放大前锐化** → 放大到 [56,120] → 灰度/对比度/锐化/极性归一。

    单图调用方（`candidates`）用的入口：`pick_band` 取的是整句的包围盒，两行字幕会被
    裁进同一张图。逐行识别（`read_image`）走 `_prepare_band`。§5.1.1 的外扩发生在抓屏
    之前，离线调用方传进来的图视为「已经外扩过的抓屏结果」。

    ［票 #187 K6］`box` 是调用方**已经算好的**带盒（`read_image` 的单带分支传
    `pick_bands` 的结果，免得这里再 `pick_band` 把整趟带扫描白跑第二遍）；缺省 None =
    维持旧行为，自己定位一次。「定位不到带 → 整图」的调用方传整图盒 `(0, 0, w, h)`，
    裁出来与原图逐像素一致。
    """
    if box is None:
        box = pick_band(img)
    band = img.crop(box) if box else img
    return _prepare_band(band)


def _candidate_images(candidate_a: Image.Image) -> list[Image.Image]:
    """候选 A（已预处理）→ 候选图列表；B = Otsu 二值化。

    B 只在 §5.1.5 的噪声门开着时才生成（干净图二值化是空操作，实测还会伤笔画），
    逐像素落回 A 则不算候选。真正跑不跑 B 的 OCR，由 `read_image` 按 A 的识别质量定。
    """
    if not residual_noise(candidate_a):
        return [candidate_a]
    candidate_b = binarize(candidate_a)
    if ImageChops.difference(candidate_a, candidate_b).getbbox() is None:
        return [candidate_a]
    return [candidate_a, candidate_b]


def candidates(
    img: Image.Image, box: tuple[int, int, int, int] | None = None
) -> list[Image.Image]:
    """§5.2 的候选图：A = 不二值化；B = Otsu 二值化（见 `_candidate_images`）。

    `box` 透传给 `preprocess`（票 #187 K6：调用方已算好带盒时免第二趟带扫描）。
    """
    return _candidate_images(preprocess(img, box))


# --- §5.2 多尺度择优：词表打分 ------------------------------------------------

_WORD_RE = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")
_wordlist_cache: dict[Path, frozenset[str]] = {}


def load_wordlist(path: Path = WORDS_PATH) -> frozenset[str]:
    """读一次、按路径常驻（data/common_words.txt 现为 2000 词，spec §5.2 说的是 10k）。

    缓存按路径分开：带默认参数的常驻缓存如果只存一份，换个路径调用会静默拿到上一次的
    结果（测试与本函数都吃这个坑）。
    """
    cached = _wordlist_cache.get(path)
    if cached is None:
        try:
            cached = frozenset(
                line.strip().lower()
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            )
        except OSError as exc:
            log.warning("词表读不到（%s）：择优退化为只比字母总长", exc)
            cached = frozenset()
        _wordlist_cache[path] = cached
    return cached


def text_score(text: str, words: frozenset[str]) -> tuple[float, int]:
    """词表命中率，第二键是字母总长（spec §5.2）。

    命中率 = 命中词表的词元数 / 总词元数；平分时取字母总长更大的那个（抗截断：
    同样命中率下更长的结果多半没被引擎吃掉尾巴）。没有词元（空结果）给 −1，
    让任何有内容的候选胜出。
    """
    tokens = _WORD_RE.findall(text)
    if not tokens:
        return -1.0, 0
    letters = sum(len(token) for token in tokens)
    hits = sum(1 for token in tokens if token.lower() in words)
    return hits / len(tokens), letters


def choose_text(texts: list[str], words: frozenset[str]) -> str:
    """两个候选的识别文本里选胜者（spec §5.2 的打分：命中率，其次字母总长）。

    空列表回空串（调用方按空结果留图）。
    """
    if not texts:
        return ""
    return max(texts, key=lambda text: text_score(text, words))


# --- §5.3 词表后校正的接线 ----------------------------------------------------

Corrector = Callable[[str], tuple[str, list[dict[str, str]]]]
# W5（票 #142 / 决策 D1）：疑似粘连的只读标记器 —— 校正后文本 → suspects 清单。
Suspector = Callable[[str], list[dict[str, object]]]

# W5 的总开关默认值定义在**代码里**（config.ini 是本票禁区，键缺席 = 启用；
# 要关闭时用户自己在 config.ini 写 `ocr_suspect_glue=0`，与 ocr_correct 同一通道）。
SUSPECT_GLUE_DEFAULT = 1


@functools.cache
def correction_wordlists() -> tuple[frozenset[str], frozenset[str]]:
    """后校正要的两份词表：(已知词, 常见词)（spec §5.3：两份合起来当「已知词」）。

    读取本身由 `load_wordlist` 按路径缓存；这里只把并集算一次并常驻。
    `data/ecdict_words.txt`（hub `--import-ecdict` 导出，`store.export_words`）存在时**并进
    `known`**（「这是个真词」），这是 #36 的精度收口：O2 的切段只认 10k 词表，词表缺的
    真词（`override`、`backwards`）会被切坏，ECDICT 把「真词」从 9894 扩到 12.6 万。
    只并 `known`、**不并 `common`**（2000 常见词）：切段的每一段仍必须落在常见词表里，
    候选排序也不变。文件不在位时两份词表与 #23 完全一致（有单测钉住这条路径）。
    词表在**内核启动时读一次**（`load_wordlist` 按路径常驻）：导入词典后要重启内核
    （外壳判死会自己拉起）才生效。
    """
    common = load_wordlist(WORDS_PATH)
    known = common | load_wordlist(WORDS_10K_PATH)
    if WORDS_ECDICT_PATH.is_file():
        ecdict = load_wordlist(WORDS_ECDICT_PATH)
        if ecdict:
            merged = known | ecdict
            log.info("ECDICT 词表并入后校正已知词：%d → %d 词", len(known), len(merged))
            known = merged
    return known, common


def build_corrector(config: dict[str, str]) -> Corrector | None:
    """按 config 的 `ocr_correct` 组装后校正器；关掉时回 None（`text == raw`）（§5.3.5）。"""
    if config_int(config, "ocr_correct", 1) == 0:
        log.info("词表后校正被 config 关掉（ocr_correct=0）")
        return None
    known, common = correction_wordlists()
    if not known or not common:
        log.warning("词表读不到，词表后校正退化为不动")
        return None
    corrections.warm_up(common)  # 删字符索引先建出来（~5ms），别落在第一次捕获上
    return functools.partial(
        corrections.correct, known_words=known, common_words=common
    )


def build_suspector(config: dict[str, str]) -> Suspector | None:
    """按 config 组装 W5 的疑似粘连标记器；关闭时回 None（响应里连 `suspects` 键都没有）。

    三重启用条件（spec §W5，缺一不可）：

    1. `ocr_correct=1` —— 标记是后校正的旁路信号，总闸关了它没有独立存在的意义；
    2. `data/ecdict_words.txt` 在位 —— 「真词不标」的守卫靠宽词表；10k 词表下误报
       会飙升（研究 §4.1），缺位时整个功能关闭；
    3. `ocr_suspect_glue=1`（默认 1，键定义在代码 `SUSPECT_GLUE_DEFAULT`，config.ini
       不动）—— 用户可整体关闭。

    段词表是 `common ∪ words_10k`（不含 ECDICT —— 研究里 S2/S3 用宽段词表会误拆
    `gladwell/turtlenecks` 这类词）；`known`（含 ECDICT）只用来排除真词与近词。
    纯文本零 I/O：词表在内核启动时读一次（`load_wordlist` 按路径常驻）。
    """
    if config_int(config, "ocr_correct", 1) == 0:
        return None
    # 键缺席（正常情况，config.ini 是本票禁区）直接按默认开、不记日志；值在但为 0
    # 才关闭，值坏（非整数）由 config_int 自己 WARNING 并退回默认（同 ort_threads 口径）。
    glue = (config.get("ocr_suspect_glue") or "").strip()
    if glue and config_int(config, "ocr_suspect_glue", SUSPECT_GLUE_DEFAULT) == 0:
        log.info("疑似粘连标记被 config 关掉（ocr_suspect_glue=0）")
        return None
    if not WORDS_ECDICT_PATH.is_file():
        log.info("ECDICT 词表缺位，疑似粘连标记关闭（宽词表下误报才会足够低）")
        return None
    known, common = correction_wordlists()
    vocab = common | load_wordlist(WORDS_10K_PATH)
    if not known or not vocab:
        return None
    return functools.partial(
        corrections.find_suspects,
        known_words=known,
        common_words=common,
        vocab_words=vocab,
    )


def _band_policy(box: tuple[int, int, int, int]) -> str:
    """一条带该用哪种引擎策略（#76）：`POLICY_WIN_FIRST`（宽带）或 `POLICY_AUTO`。

    **判据用原始带宽**（`pick_bands` 回的盒，含 BAND_PAD_PX），不是放大后的：放大只按高度
    （`band_scale`），宽行会被放大 5–6 倍，拿放大后的宽度判会把窄行也算成宽带（#74 §1.3）。
    纯函数：只读盒子，不看图、不看引擎 —— 引擎能不能走 Windows 优先由 `_pick_from_candidates`
    自己判（单引擎调用方、`win`/`rapid` 强制模式都不适用）。
    """
    return POLICY_WIN_FIRST if box[2] - box[0] >= WIDE_BAND_PX else POLICY_AUTO


def _pick_from_candidates(
    engine,
    images: list[Image.Image],
    wordlist: frozenset[str],
    policy: str = POLICY_AUTO,
    extra: list[Image.Image] | None = None,
) -> tuple[str, int]:
    """候选图列表 → §5.2 词表择优后的文本（+ 跑了几遍 OCR）。

    单引擎（Windows）时与 v2 逐字一致；`engine` 是 `DualEngine`（spec §6.2）时同一次调用
    会把候选 A 上两路引擎的结果（以及候选 B 上的 Windows 结果）一起收进来打分。

    `policy`（#76）只决定候选 A 上一路怎么跑：

    - `POLICY_AUTO`（默认）：两路都跑（`DualEngine.texts` 的老行为）；
    - `POLICY_WIN_FIRST`（宽带，见 `_band_policy`）：先跑 Windows，词表分 ≥ `_SCORE_TRUST`
      就直接接受、**不给 RapidOCR 发车**。只在 `DualEngine` 的 `auto` 模式下生效：单引擎
      调用方本来就只有 Windows，`win`/`rapid` 强制模式有自己的单引擎语义，都不适用。

    第二遍（B = Otsu 二值化）只在**两个条件同时成立**时才跑（两种 policy 都一样）：

    1. §5.1.5 的噪声门开着（`residual_noise`，见 `_candidate_images`）；
    2. 候选 A 的识别结果不像英文（词表命中率 < `_SCORE_TRUST`）。

    条件 2 是实测加的：两个候选都是垃圾时词表打分没有判别力 —— 同分或略高的 B 经常
    更差，46 例合成语料上「无条件跑 B + 打分」(47.6%) 反而**不如**「A 不可信才跑 B」
    (43.3%)，而不跑 B 是 42.8%（详见 issue #22）。词表打分在 A 还像样时也就更可信，
    这时 B 没有翻盘的空间。多引擎时「A 像样」按 A 上所有引擎的最好成绩算：只要有
    一路读得像英文，就不必再为 B 付出识别成本。候选 B 永远只给 Windows（ADR-0005
    修订 §2），这条纪律与 policy 无关。

    `extra`（#97）是**同样性质的第二个取景**（不二值化、两路引擎都跑），用来在带定位
    失败时补一个「有字行」的紧取景（见 `_tight_band_view`）。它们排在原候选之后一起进
    词表仲裁：`choose_text` 平分时取第一个最大值，原取景因此保住优先权。**准入门在
    `_tight_band_view` 里**（带跑飞 / 没有带且留白够多才补），补出来的一律参与打分，
    这里不再加第二道文字层判据 —— #97 试过「命中率严格更高 + 字母总长不缩水」，在 v05
    那类「原读数更长但更脏」的例子上会把正确的紧读数误拒掉。
    """
    if (
        policy == POLICY_WIN_FIRST
        and isinstance(engine, DualEngine)
        and engine.mode == "auto"
    ):
        texts = engine.texts_win_first(images[0])
    else:
        texts = _engine_texts(engine, images[0], binarized=False)
    # 候选 B 的门只看**原取景**的成绩（与 #22 的字面语义一致）：补进来的第二个取景不参与
    # 这个判断，免得它把「A 已经像样」抬上去、把候选 B 白省掉 —— 候选 B 是二值化那一路，
    # 它自己会赢的场合不该被取景数量影响。
    best = max((text_score(text, wordlist)[0] for text in texts), default=-1.0)
    if len(images) > 1 and best < _SCORE_TRUST:
        texts += _engine_texts(engine, images[1], binarized=True)
    for image in extra or ():
        texts += _engine_texts(engine, image, binarized=False)
    return choose_text(texts, wordlist), len(texts)


def _block_first_read(
    engine,
    img: Image.Image,
    boxes: list[tuple[int, int, int, int]],
    wordlist: frozenset[str],
) -> tuple[str | None, int]:
    """§6.1.1 整段并带（#78）：并集盒 → 块高 `BLOCK_HEIGHT` → `enhance` → Windows OCR
    （正常一次；行数不足时重试一次，见下面的行数一致性门）。

    回 `(text, n)` = 走了并带且词表分 ≥ `_SCORE_TRUST`（收工）；`n` 是这次并带真跑过的
    引擎调用数（行数正常 = 1；行数不足触发重试 = 2）。
    回 `(None, n)` = **回落逐行**，调用方接着走 `read_image` 的老路径，`n` 是这次回落
    **白跑掉的引擎调用数**（块识别真跑过才是 1，重试过是 2；噪声门直接挡回、或没有
    Windows 后端时是 0）—— 诚实记账：`read_image` 回的「跑了几遍」要含这次白跑
    （#74 说它的代价「最坏」约 50ms）。
    回落有四种情形，都在这里判掉：这台引擎没有可用的 Windows 后端、§5.1.5 的**噪声门**不过、
    分不够、或块读行数少于带数且重试后仍少。四种情形各记一行 INFO 说明原因（#94 P0）——
    返回值形状一个字不动：单测里有 17 处断言 `passes` 的具体值，签名一改就是一片测试改动，
    所以原因只走日志。

    **行数一致性门**（#100，与噪声门/分数门并列的第三道）：块读是「整段一次识别」，
    小字/行尾断词的短行会被引擎静默吞掉，而词表分照样能过 0.6（研究文档 §3-D2 实测
    26px 与 14px 浅底各少首行、分 0.833）。所以拿到 `(text, lines)` 后比较 `lines` 与
    带数：少了就**重试一次**（同一张块图，不重新抓屏/预处理 —— 引擎在这类边界形态上
    不保证逐次确定，重试一次只有一次 Windows 调用 8–28ms，比回落逐行便宜）；仍少才
    回落逐行。行数正常时不多花一次引擎调用（单测用计数替身钉住）。**分数门先判**：
    分不够本来就要回落，这时再为行数重试等于纯白花一次调用。

    只喂 Windows 一路：RapidOCR 关掉了检测头（`use_text_det=False`），多行块会被它当成
    一行，实测块级双引擎的 WER 与「只 Windows」相同却白花 ~70ms（#74 §4-C10）。
    `rapid` 模式是用户显式的「只用 RapidOCR」，不能在这里偷跑 Windows。

    放大与 §5.1.3 的 `band_scale` 无关：并带用的是**块**的目标高度 300（不是行的 120），
    宽度上限 `BLOCK_MAX_WIDTH` 一旦会被超过就整块不放大。这一档丢了「每行放大到 120px」，
    所以噪声门不是可选项 —— 它看住的就是这件事（见 `residual_noise` 与 #74 §4-C10 的 V7/V8）。
    """
    windows = _windows_backend(engine)
    if windows is None:
        log.info("并带回落：没有可用 Windows 后端")
        return None, 0
    box = (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )
    prepared = enhance(_resize_block(img.crop(box)))
    if residual_noise(prepared):  # 噪声门（§5.1.5）：糊掉的块直接不走并带
        log.info("并带回落：噪声门不过")
        return None, 0
    outcome = _run_windows(engine, windows, prepared)
    if outcome is None:
        return None, 0
    text, lines = outcome
    score = text_score(text, wordlist)[0]
    if score < _SCORE_TRUST:
        log.info("并带回落：分数不够 词表分 %.3f < %.2f", score, _SCORE_TRUST)
        return None, 1
    passes = 1
    if lines < len(boxes):
        log.info("并带行数不足 识别 %d 行 < 带 %d，重试一次", lines, len(boxes))
        retry = _run_windows(engine, windows, prepared)
        passes += 1  # 重试也是一次真实引擎调用，不论成败都记账（#100）
        if retry is None:
            log.info("并带回落：行数仍不足 识别 %d 行 < 带 %d", lines, len(boxes))
            return None, passes
        text, lines = retry
        if lines < len(boxes):
            log.info("并带回落：行数仍不足 识别 %d 行 < 带 %d", lines, len(boxes))
            return None, passes
        score = text_score(text, wordlist)[0]
        if score < _SCORE_TRUST:  # 重试后的文本才是要收工的那份，分数门照旧判
            log.info("并带回落：分数不够 词表分 %.3f < %.2f", score, _SCORE_TRUST)
            return None, passes
    return text, passes


def _resize_block(block: Image.Image) -> Image.Image:
    """并集块按 `BLOCK_HEIGHT` 等比放大：上限 `BLOCK_MAX_SCALE`，会超 `BLOCK_MAX_WIDTH` 就不放大。

    §5.1.3 的 `_resize_band` 是「单行带 → 120px」，这里是「整段块 → 300px」；两者互不影响
    （并带路径不经过 `band_scale`）。宽度上限一撞就把**整块**留原尺寸，不做部分缩放 ——
    部分缩放会把宽段落的行高又压回去，等于两头不讨好。
    """
    if not 0 < block.height < BLOCK_HEIGHT:
        return block
    scale = min(BLOCK_MAX_SCALE, BLOCK_HEIGHT / block.height)
    if block.width * scale > BLOCK_MAX_WIDTH:
        return block
    return block.resize(
        (
            max(1, round(block.width * scale)),
            max(1, round(block.height * scale)),
        ),
        Image.Resampling.LANCZOS,
    )


def _windows_backend(engine) -> Callable[[Image.Image], tuple[str, int]] | None:
    """并带路径能用的 Windows 识别函数；没有就回 None（调用方回落逐行）。

    `auto`/`win` 模式有 Windows 后端就用它；`rapid` 模式（用户显式要求只用 RapidOCR）
    与单引擎调用方（历史列 O1–O3 的对照路径、单测里的替身）都回 None —— 并带的语义是
    「整段只发一次 Windows」，对没有 Windows 的引擎没有意义。

    回 `(文本, 行数)`：同一次识别两个出口（`_ocr_text_and_lines`，#99 就为这件事留的）。
    #100 的行数门要拿 `len(result.lines)` 跟带数比，再调一次 `ocr_image` 等于把一次
    捕获的引擎成本翻倍，所以这里直接把两个出口一起往上递。
    """
    if not isinstance(engine, DualEngine) or engine.mode == "rapid":
        return None
    if engine.win is None:
        return None
    return lambda block: _ocr_text_and_lines(engine.win, block)


def _run_windows(
    engine, windows: Callable[[Image.Image], tuple[str, int]], img: Image.Image
) -> tuple[str, int] | None:
    """喂一次 Windows OCR；抛错只记日志并回 None（与 `DualEngine._run` 同一纪律）。

    块识别挂了不该毁掉整次捕获：回落逐行是免费的兜底（老路径本来就有 `_run` 的保护）。
    """
    try:
        return windows(img)
    except Exception as exc:  # noqa: BLE001 - 一路挂了还有逐行路径，静默回落
        log.warning("并带识别失败，本次回落逐行：%s: %s", type(exc).__name__, exc)
        return None


def _tight_band_view(
    img: Image.Image, box: tuple[int, int, int, int] | None
) -> list[Image.Image]:
    """带没定位到行时，再准备一个「有字行」的紧取景（回 [] 表示不必补）。

    触发条件只有一条：**这条带比 §5.1.3 里行带的上限还高**（`BAND_MAX_HEIGHT`）——
    行带的目标区间是 [56, 120]，`band_scale` 只管把矮的放大、从不缩小，所以正常的行带
    不会超过 120；一条 267px 高的"带"只可能是 `_extend_rows` 在杂底上跑飞的结果
    （见 `_band_candidates` 的 #97 注）。此时 `_prepare_band` 也不会放大，松图原样进
    引擎；而引擎的识别输入按高度归一化，**留白越大字越小**：同一行在同一帧上实测，
    紧框 124px 时 RapidOCR 逐字全对，多 40px 留白开始丢词间空格（`theyalso`），多
    150px 留白整句塌成 `foryhomwelthyalhthngfyoets`。所以补一个按 `_text_row_span`
    裁出来的紧取景。

    这个门槛同时是**护栏**：正常大小的行带（合成集 12–24px 字、用户样本、真机
    r01–r07、404×38 的 r05）一律不补 —— 那种图上多收一刀只会切掉笔画，反而把正确的
    读数换掉（#97 实测把 r05 从逐字全对打成 WER 29%）。

    **一条带都找不到**（`box` 为 None）时另判：那时整图原样进引擎，同样会栽在留白上
    （实测 v05：2005×385 的松框，`pick_bands` 给不出带，读数成了
    `000 BISCUITS ... they\u00e1lso have things fo@`）。这种图上没有带可以参照，改用**结构**
    门槛 `_TIGHT_VIEW_MIN_PAD`：图高得达到行高的这个倍数（留白 ≳ 行高）才补。没有带又
    本来就紧的图（合成集的单行、u1）最多多跑一次 OCR，不会更差。

    只补**更小的**取景：行段按比例上下留白后仍要比参照（带或整图）矮 `_TIGHT_VIEW_GAIN`，
    否则纯粹白跑一次 OCR。
    """
    span = _text_row_span(ImageOps.autocontrast(img.convert("L"), cutoff=0.5))
    if span is None:
        return []
    span_height = span[1] - span[0] + 1
    if span_height > BAND_MAX_HEIGHT:
        # 带里含多行（行段本身就比单行带的上限还高）：那是 §6.1.1 / #78 那条路的活
        # （并带 or 逐行），这里不插手 —— 补的取景只服务「框住一行、带却跑了」这一种。
        return []
    reference = img.height
    if box is None:
        if img.height < _TIGHT_VIEW_MIN_PAD * span_height:
            return []
        left, right = 0, img.width
    else:
        if box[3] - box[1] <= BAND_MAX_HEIGHT:
            return []
        reference = box[3] - box[1]
        left, right = box[0], box[2]
    pad = max(BAND_PAD_PX, round(_TEXT_ROW_PAD_RATIO * span_height))
    top = max(0, span[0] - pad)
    bottom = min(img.height, span[1] + 1 + pad)
    if bottom - top > _TIGHT_VIEW_GAIN * reference:
        return []
    tight = img.crop((left, top, right, bottom))
    return [_prepare_band(tight)] if tight.height >= _MIN_BAND_ROWS else []


def _log_ocr_path(path: str, bands: int, lines: int, passes: int) -> None:
    """每次捕获一行「走了哪条路 + 带数 + 识别行数 + 遍数」（#94 P0）。

    零行为变更、零测试面：路径与回落原因全走日志，`read_image` / `_block_first_read` 的返回值
    形状不动。真机日志原先只能靠「遍数 = 1」反推走了并带，噪声门/分数门各挡掉多少查不到（见
    `docs/research/ocr-paragraph-latency.md` §8-2）。`lines` 是真正进结果的行数（逐行路径里
    某条带读空就不计）；**块路径上只能记带数** —— 这条日志的 `lines` 由调用方给（`read_image`
    在块路径上给的就是带数），所以那里是上界。真实行数由 #100 的行数门在 `_block_first_read`
    里自己记（「并带行数不足 / 行数仍不足」两行）。
    """
    log.info("OCR 路径 %s 带 %d 行 %d passes %d", path, bands, lines, passes)


def read_image(
    engine, img: Image.Image, words: frozenset[str] | None = None
) -> tuple[str, int]:
    """候选图 → 文本（回 (文本, 跑了几遍 OCR)）。recognize 与基准共用这一条路径。

    单行（含「定位不到带」的退回整图）走 v2 的老路径，行为逐字不变。
    多条拉丁带（§6.1.1 的两行字幕/段落）**先试整段并带**（`_block_first_read`，#78）：
    并集块过一次 Windows、分够高就收工；不可信（或有噪、或没有 Windows）才回落逐行 —— 回落
    把白跑的块识别也算进返回值（`passes`）。
    逐行路径每行单独识别，再按 y 从上到下拼接（行间单空格；上一行以连字符结尾时按 #99 的
    规则处理，见 `_join_line_texts`）—— 引擎一次只喂一行的放大图，比把两行并成一张图
    （放大后超过 [56,120]、不再放大）更稳。中文行不在 `pick_bands` 的候选里，不会进这句。

    每条带用哪几个引擎由 `_band_policy` 按**原始带宽**定（#76）：宽带行只把**顺序**改成
    `[Windows, RapidOCR]`（顺序即平局优先级：平局判给 Windows），**两路都跑到**；窄带仍是
    §6.2 的 auto（RapidOCR 在前）。候选 B 的纪律与选带语义都不受影响。
    ［#97（原 #79）修订］这条规则原来还带一句「Windows 词表分够高就不跑 RapidOCR」——
    已在 #97 删掉：真实视频帧上 Windows 会读错而分数照样过线（烧进画面的字幕上 8 例错 6 例，
    分 0.50–0.67），那句会把读对的 RapidOCR 挡在门外。别再把「省一半引擎调用」当成这条规则的
    一部分（`DualEngine.texts_win_first` 的 docstring 记的就是新口径）。

    出口各记一行 `_log_ocr_path`（#94 P0）：`并带` / `回落逐行` / `单带`（`整图` = 一条带都
    没定位到）。回落原因由 `_block_first_read` 自己那一行给出。
    """
    wordlist = words if words is not None else load_wordlist()
    boxes = pick_bands(img)
    passes = 0
    if len(boxes) > 1:
        block, passes = _block_first_read(engine, img, boxes, wordlist)
        if block is not None:
            _log_ocr_path("并带", len(boxes), len(boxes), passes)
            return block, passes
    if len(boxes) <= 1:
        policy = _band_policy(boxes[0]) if boxes else POLICY_AUTO
        # 票 #187 K6：单带分支把上面 `pick_bands` 的结果传给 `candidates` —— 旧写法经
        # `candidates` → `preprocess` → `pick_band` 会把整趟带扫描（Otsu + 逐行墨量 +
        # 行带聚类）白跑第二遍（审计 K6：大选区 10ms 量级）。单带时 `pick_band` 的并集盒
        # 就是 `boxes[0]`；`boxes` 为空（定位不到带）退回整图盒，裁出来与原图逐像素一致。
        band_box = boxes[0] if boxes else (0, 0, img.width, img.height)
        text, passes = _pick_from_candidates(
            engine,
            candidates(img, band_box),
            wordlist,
            policy,
            extra=_tight_band_view(img, boxes[0] if boxes else None),
        )
        path = "单带" if boxes else "整图"
        lines = 1 if text.strip() else 0
        _log_ocr_path(path, len(boxes), lines, passes)
        return text, passes
    parts: list[str] = []
    for box in boxes:
        images = _candidate_images(_prepare_band(img.crop(box)))
        text, count = _pick_from_candidates(engine, images, wordlist, _band_policy(box))
        passes += count
        if text.strip():
            parts.append(text.strip())
    _log_ocr_path("回落逐行", len(boxes), len(parts), passes)
    return _join_line_texts(parts), passes


# --- 截图与 OCR -------------------------------------------------------------

SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0
# 句柄提到模块级：每次捕获都要用，WinDLL() 每调一次都有解析开销（实测占抓屏的一成）
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)


class _BITMAPINFOHEADER(ctypes.Structure):
    """GDI BITMAPINFOHEADER（win32 结构固定，逐字段列出以免依赖 packing 假设）。"""

    _fields_ = [
        ("biSize", ctypes.c_uint32),
        ("biWidth", ctypes.c_int32),
        ("biHeight", ctypes.c_int32),
        ("biPlanes", ctypes.c_uint16),
        ("biBitCount", ctypes.c_uint16),
        ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32),
        ("biXPelsPerMeter", ctypes.c_int32),
        ("biYPelsPerMeter", ctypes.c_int32),
        ("biClrUsed", ctypes.c_uint32),
        ("biClrImportant", ctypes.c_uint32),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", ctypes.c_uint32 * 3)]


def enable_dpi_awareness() -> None:
    """per-monitor-v2。两进程 DPI 感知级别不一致，拖框区域会整体偏移（ADR 0002）。"""
    try:
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except (AttributeError, OSError):
        pass
    try:
        if (
            ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0
        ):  # 这个 API 成功返回 S_OK(0)
            return
    except (AttributeError, OSError):
        pass
    log.warning("没能设置 DPI 感知，矩形可能与实际内容错位")


def grab_screen(rect: tuple[int, int, int, int]) -> Image.Image:
    """按物理像素抓取屏幕区域；坐标原点 = 虚拟屏幕左上角（同 `all_screens=True`）。

    ［实测修订］用 GDI BitBlt **只抓目标矩形**，不再走 Pillow 的
    `ImageGrab.grab(bbox=...)`：Pillow 在 Windows 上忽略 bbox、**先抓整屏**再在
    Python 里 crop —— 2560×1600 下每次捕获固定 BitBlt 全屏 + 拷贝 12MB，实测
    55–80ms（与矩形大小无关），而一次全屏回读会打断正在播放的硬件加速视频。
    只 BitBlt 目标矩形后：134×43 约 0.4ms、1592×632 约 3.2ms，像素与旧实现
    逐字节一致（tests/test_kernel_live.py 有对照测试钉住）。
    """
    x, y, w, h = rect
    user32, gdi32 = _user32, _gdi32
    # 上界防呆：下面按 w×h×4 分配 DIB，坐标算错（lp 当 px 用、虚拟屏幕原点搞混）
    # 会请求几十 GB 并卡死内核 —— 比虚拟屏幕还大的矩形不可能是我们拖出来的
    screen_w = user32.GetSystemMetrics(78)  # SM_CXVIRTUALSCREEN
    screen_h = user32.GetSystemMetrics(79)  # SM_CYVIRTUALSCREEN
    if w > screen_w or h > screen_h:
        raise ValueError(f"rect 尺寸 {w}×{h} 超过虚拟屏幕 {screen_w}×{screen_h}")
    hdc_screen = user32.GetDC(None)  # 整个虚拟屏幕
    hdc_mem = hbmp = old_bitmap = 0
    try:
        hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
        # 32bpp BI_RGB 顶向下 DIB（负高度）：直接拿像素指针，省一次 GetDIBits 拷贝
        header = _BITMAPINFOHEADER(
            ctypes.sizeof(_BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0
        )
        info = _BITMAPINFO(header, (ctypes.c_uint32 * 3)())
        bits = ctypes.c_void_p()
        hbmp = gdi32.CreateDIBSection(
            hdc_screen, ctypes.byref(info), DIB_RGB_COLORS, ctypes.byref(bits), None, 0
        )
        pointer = bits.value
        if not hbmp or pointer is None:
            raise ctypes.WinError(ctypes.get_last_error())
        old_bitmap = gdi32.SelectObject(hdc_mem, hbmp)
        if not gdi32.BitBlt(hdc_mem, 0, 0, w, h, hdc_screen, x, y, SRCCOPY):
            raise ctypes.WinError(ctypes.get_last_error())
        raw = ctypes.string_at(pointer, w * h * 4)
        # 这里拷一次像素（大矩形约 1ms），换来不共享缓冲区：DIB 在 finally 里就
        # 释放了，返回的图必须不引用它（frombuffer 会引用，还得跟类型检查器打架）
        return Image.frombytes("RGB", (w, h), raw, "raw", "BGRX", 0, 1)
    finally:
        # 先选回旧位图再删：位图还被选进 DC 时 DeleteObject 的行为没写进契约
        # （本机 26200 实测返回成功、但推迟到取消选中才真释放，别把释放押在这上面）。
        if hdc_mem and old_bitmap:
            gdi32.SelectObject(hdc_mem, old_bitmap)
        if hbmp:
            gdi32.DeleteObject(hbmp)
        if hdc_mem:
            gdi32.DeleteDC(hdc_mem)
        if hdc_screen:
            user32.ReleaseDC(None, hdc_screen)


def create_engine():
    """建一次、常驻复用：创建 9–17ms，识别 1–12ms（spec §5.1）。"""
    from winrt.windows.globalization import Language
    from winrt.windows.media.ocr import OcrEngine

    engine = OcrEngine.try_create_from_language(Language(OCR_LANGUAGE))
    if engine is None:
        raise RuntimeError(f"{OCR_LANGUAGE} OCR 引擎不可用：系统缺少该识别语言")
    return engine


# --- #99 P0-1：行级重建（用引擎给的行结构，而不是整段 Text） -------------------
#
# 旧出口只取 `OcrResult.Text`：它把识别到的各行用**一个空格**连起来（issue #9 实测，
# 两行隔 320px 也一样），行尾的连字符于是变成 `well- known` —— 研究文档 §3-D1 在渲染图
# 上的原样读数是 `A well- known problem: the state-of-the- art system broke twenty-one
# times.`。同一个 `OcrResult` 里每行每词的包围盒一直都在（`OcrLine` 自己没有
# BoundingRect，由 `OcrLine.words[].bounding_rect` 取并集得到），所以这一刀不增加任何
# 引擎调用，只是把已经付过钱的那次识别结果用完整。


# 行尾连字符的三种形态：ASCII `-`、U+2010 HYPHEN、U+2011 NON-BREAKING HYPHEN。
# 屏幕上断掉的复合词可能以任意一种出现（PDF 排版常用后两种），一律保留。
_LINE_HYPHENS = "-\u2010\u2011"


def _line_anchor(line, previous_top: float) -> tuple[float, float]:
    """一行在阅读顺序里的位置：词的盒取并集 → (top, left)。

    `OcrLine` 官方只暴露 `Text` 与 `Words`（附录 B-1），行框只能从词框算：top = 最小的
    y、left = 最小的 x。**没有词的行**没有几何可排（Windows 不会这样返回），给它上一行的
    top + 最大的 left —— 留在这行后面，而不是被甩到段尾。
    """
    rects = [word.bounding_rect for word in line.words]
    if not rects:
        return previous_top, float("inf")
    return min(rect.y for rect in rects), min(rect.x for rect in rects)


def _join_line_texts(parts: list[str]) -> str:
    """②③④ 的拼接规则：逐行文本 → 一段（`assemble_lines` 与逐行回落的按带拼接共用）。

    逐行回落的每一带就是一行（`pick_bands` 的带本来就是「一行」），所以跨带拼接与
    `OcrResult.Lines` 的跨行拼接是同一件事；**只在那里改 ocr_image 的话，回落路径
    （用户真实会走的另一条）仍然会把 `state-of-the-` + `art` 拼成 `state-of-the- art`**
    —— 研究文档 §3-D1 的「逐行路径：同左」说的就是这一半。
    """
    if not parts:  # 每条带都读空（p07 这类糊图）：与旧 `" ".join([])` 一样回空串
        return ""
    joined = parts[0]
    for previous, text in pairwise(parts):
        head = text[0]
        if previous[-1] in _LINE_HYPHENS and (
            head.islower() or head.isupper() or head.isdigit()
        ):
            joined += text  # ②③：断词/复合词/编号列表 —— 去掉行间空格，连字符保留
        else:
            joined += " " + text  # ④
    return joined


def assemble_lines(result) -> str:
    """`OcrResult` → 文本：逐行拼、行尾连字符不再被行间空格断开（#99 P0-1）。

    规则（票面 #99 的顺序）：

    ① **不信任引擎给的行序**：`OcrResult.Lines` 官方没承诺阅读顺序（附录 B-1），所以每行
       用词框并集算 top，先按 top、再按 left 排（`_line_anchor`）；
    ② 上一行以连字符（`-` / U+2010 / U+2011）结尾、下一行以**小写字母**开头 → 去掉行间
       空格、**保留**连字符：`well-` + `known` → `well-known`。这是 GROBID 的
       de-hyphenation 判据（附录 B-4：跳过后面的空白，下一个词全小写且真的换了行）；
    ③ 上一行以连字符结尾、下一行以**大写字母或数字**开头 → 同样不插空格（复合词的下一段
       大写、`Step-` / `1` 这类编号列表），连字符照旧保留；
    ④ 其它情况 → 单空格。

    空行/只有空白的一行直接跳过（也不参与 ②③ 的「上一行」判断）；没有行时回空串。

    **几何只在 `TextAngle == 0` 时成立**：`OcrWord.BoundingRect` 的官方前提（附录 B-1），
    所以 `text_angle` 为 `None`（没检测到角度）或非 0 时整段退回 `OcrResult.Text` ——
    不猜几何。当前屏幕截图实测 `-0.0`（`-0.0 == 0`，走几何），但那是实测不是保证。
    """
    angle = result.text_angle
    if angle is None or angle != 0:
        return result.text
    rows: list[tuple[tuple[float, float], str]] = []
    top = 0.0
    for line in result.lines:
        text = line.text.strip()
        if not text:
            continue
        anchor = _line_anchor(line, top)
        top = anchor[0]
        rows.append((anchor, text))
    if not rows:
        return ""
    rows.sort(key=lambda row: row[0])  # 稳定排序：几何相同的行保留引擎顺序
    return _join_line_texts([text for _anchor, text in rows])


async def _decode_bitmap(png_bytes: bytes) -> Any:
    """图像字节 → `SoftwareBitmap`（内存流解码；单独拆出来让单测能换掉这一步、绕开 WinRT）。"""
    from winrt.windows.graphics.imaging import BitmapDecoder
    from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream

    stream = InMemoryRandomAccessStream()
    writer = DataWriter(stream.get_output_stream_at(0))
    writer.write_bytes(bytearray(png_bytes))
    await writer.store_async()
    await writer.flush_async()
    writer.detach_stream()
    return await (
        await BitmapDecoder.create_async(stream)
    ).get_software_bitmap_async()


def _cancel_engine_ops(ops: list[Any]) -> None:
    """逐个 `Cancel()` 引擎异步操作（票 #180 K4）：对已完成的操作是 no-op。

    替身对象可能没有 `cancel`，Cancel 自己抛错也不许掩盖原始异常 —— 只记日志。
    """
    for operation in ops:
        cancel = getattr(operation, "cancel", None)
        if cancel is None:
            continue
        try:
            cancel()
        except Exception as exc:  # noqa: BLE001
            log.warning("OCR 异步操作 Cancel 失败（忽略）：%s: %s", type(exc).__name__, exc)


async def _recognize_async(engine, png_bytes: bytes) -> Any:
    """回 `OcrResult`（不是 `.text`）：行级重建要 `lines` / `words` / `text_angle`（#99）。

    ［票 #180 K4］整包（解码 + RecognizeAsync）有 `OCR_ASYNC_TIMEOUT_S` 墙钟上限：
    引擎挂死时外壳 20s 就放弃了，内核不能还在等。WinRT 的异步操作不许粗暴杀线程
    （asyncio 也没有线程可杀），`wait_for` 只能取消 asyncio 任务、底层操作可能还在
    后台烧 —— 所以异常路径上把 RecognizeAsync 的操作对象 `Cancel()` 掉。
    `_ENGINE_LOCK` 由调用方的 `with` 释放，超时后后续请求照常可用。
    """
    ops: list[Any] = []  # 本次创建的引擎异步操作，异常路径上逐个 Cancel

    async def _run() -> Any:
        bitmap = await _decode_bitmap(png_bytes)
        operation = engine.recognize_async(bitmap)
        ops.append(operation)
        return await operation

    try:
        return await asyncio.wait_for(_run(), OCR_ASYNC_TIMEOUT_S)
    except TimeoutError:
        _cancel_engine_ops(ops)
        raise TimeoutError(f"OCR 识别超过 {OCR_ASYNC_TIMEOUT_S:g}s 上限") from None
    except BaseException:
        # 外部取消/引擎抛错：同样先停操作再透传，别让挂死的那次识别把引擎继续占着。
        _cancel_engine_ops(ops)
        raise


def _ocr_text_and_lines(engine, img: Image.Image) -> tuple[str, int]:
    """同一次识别，两个出口：拼好的文本 + 引擎给的行数（#100 的行数一致性门要用后者）。

    `ocr_image` 只要文本；行数取 `len(result.lines)`（引擎原样，不扣空行 —— #100 是拿它
    跟 `pick_bands` 的带数比的）。整次识别仍然只有**一次** `RecognizeAsync`、仍然在
    `_ENGINE_LOCK` 里 `asyncio.run`，语义一字不动。
    """
    buffer = io.BytesIO()
    img.save(buffer, "BMP")
    with _ENGINE_LOCK:
        result = asyncio.run(_recognize_async(engine, buffer.getvalue()))
        # 属性也在这个锁/线程里读：WinRT 对象不承诺跨线程访问，放一起最省心
        return assemble_lines(result), len(result.lines)


def ocr_image(engine, img: Image.Image) -> str:
    """识别一张图 → 文本；行由 `assemble_lines` 按几何重建（#99 P0-1）。

    ［实测修订］旧出口直接回 `OcrResult.Text`（它把各行用**一个空格**连起来，issue #9
    实测，两行隔 320px 也一样），行尾连字符因此被拆成 `well- known`。现在逐行取文本、按
    词框的 y 排序后按 `assemble_lines` 的四条规则拼接；同一张三行图（20/14px、深/浅底
    四组）实测读数里 `- `（连字符 + 空格）从 4/4 组出现变成 0/4 组。公开签名与语义不变
    （回单行文本，单行化交给外壳，spec §5.2）。

    `_ENGINE_LOCK` 串行化：TCP 是一连接一线程，WinRT 引擎对象又不承诺线程安全，一把锁最省心。
    ［实测修订］#74 的探针里 **4 线程并发同一个 `OcrEngine` 是 0 错误的** —— 原先这里写的
    「并发会报 `Another RecognizeAsync operation is already running`」在本机不成立，别拿
    「并发不安全」当并行可行/不可行的论据。**否决并行的真理由是 CPU 已饱和**：单次调用实测
    已吃掉 11/16 核，丢进线程池只是让同一条 CPU 队列排队（实测 1.06×，见 #74 §4-C1）。锁照旧。

    ［实测修订］交给 WinRT 的是 **BMP**，不是 PNG：PNG 的编码成本随**画面内容**涨
    （同一张 1592×632 裁剪，纯文本背景 12ms、视频帧一类复杂画面 153ms），
    BMP 是无压缩拷贝、与内容无关（4–5ms），WinRT 解码后识别文本逐字相同
    （同机两种尺寸实测）。
    """
    return _ocr_text_and_lines(engine, img)[0]


# --- §6.2 双引擎：RapidOCR（离线 ONNX）与 Windows OCR 择优 --------------------


def cpu_cores() -> int:
    """本机逻辑核数（`os.cpu_count()` 可能回 None，退回 1）。"""
    return os.cpu_count() or 1


def ort_threads(config: dict[str, str] | None = None) -> int:
    """RapidOCR 的 ONNX 会话该用几个 intra_op 线程（#94 P1）：`max(4, 核数//2)`。

    缺省规则写在**代码里**（config.ini 不写这个键 —— 免得一台机器上写死的值被搬到 4 核机器上
    用）：16 核 → 8、8 核 → 4、4 核 → 4（= 现状，弱机不激进）。实测依据见
    `docs/research/ocr-paragraph-latency.md` §2.4：默认全核在每一次测量里都是最差的一档，
    限到一半后负载下墙钟 1.6–1.7×、空载代价 0–10%。传 `config` 时读 `ocr_ort_threads` 覆盖
    —— **键缺席（正常情况）直接回规则值、不记日志**；值在但坏掉（不是整数 / 小于 1）才
    WARNING 并回规则值。
    """
    default = max(ORT_THREADS_MIN, cpu_cores() // 2)
    if config is None or not (config.get("ocr_ort_threads") or "").strip():
        return default
    value = config_int(config, "ocr_ort_threads", default)
    if value < 1:
        # 0 在 onnxruntime 里是「自己定线程数」= 全核 —— 正好把限线程静默抵消，而回读出来
        # 也是 0（看着像「目标 0」而不是「没限上」），所以这一档必须挡掉。
        log.warning(
            "config 的 ocr_ort_threads=%s 小于 1（0 = onnxruntime 自己定 = 全核），用规则值 %s",
            value,
            default,
        )
        return default
    return value


def _cap_rapid_session_threads(threads: int) -> None:
    """把 rapidocr 用的 `SessionOptions` 换成「建好就钉上线程数」的子类（#94 P1）。

    **必须在建引擎之前调用**，且只能替掉 rapidocr 自己 import 的那个名字：1.2.3 的
    `OrtInferSession.__init__` 里写的是 `SessionOptions()`（模块全局），它不读任何线程配置键
    —— 改 site-packages 里的 config.yaml 在 venv 按 `requirements.txt` 重建后就会静默失效。
    补丁点消失（包升级/改名）时这里会抛，由调用方吞成 WARNING：照常跑全核，绝不毁捕获。
    """
    import rapidocr_onnxruntime.utils as rapid_utils  # type: ignore[import-untyped]
    from onnxruntime import SessionOptions  # type: ignore[import-untyped]

    class _CappedSessionOptions(SessionOptions):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.intra_op_num_threads = threads
            self.inter_op_num_threads = ORT_INTER_THREADS

    rapid_utils.SessionOptions = _CappedSessionOptions


class RapidEngine:
    """RapidOCR 封装（PP-OCR 系 ONNX，离线，模型随 wheel 落在 site-packages 里）。

    关掉自带文本框检测（`use_text_det=False`）：文本行定位由 §5.1 的带检测负责，这里只
    要识别。开着检测头会在已经裁好的单行图上重复框出重叠框（用户样本 u3/u4 实测被拆成
    「t the e」这种重复字），关掉它既更准、又省掉一次检测推理（每图实测约 300ms）。

    关掉角度分类（`use_angle_cls=False`）同上：单行字幕没有 180° 翻转，这一路是白跑。

    `_lock`：内核是一连接一线程，ONNX 会话内部有可变状态，串行化与 Windows 引擎同规矩。

    `threads`（#94 P1）：ONNX 会话的 intra_op 线程数，缺省取 `ort_threads()`。**不设**它时
    onnxruntime 会自己吃满全核 —— 一边放视频一边取词就互相踩，单次调用实测从 0.25–0.7s 涨到
    0.4–1.2s（§2.4）。设了之后可以用 `session_threads()` 回读实际生效值。
    """

    def __init__(self, threads: int | None = None) -> None:
        self.threads = ort_threads() if threads is None else threads
        try:
            _cap_rapid_session_threads(self.threads)
        except Exception as exc:  # noqa: BLE001 - 补丁点没了只该变慢，不该让捕获失败
            log.warning(
                "RapidOCR 限线程没打上（%s: %s），本次按全核跑", type(exc).__name__, exc
            )
        from rapidocr_onnxruntime import RapidOCR

        self._engine = RapidOCR(use_text_det=False, use_angle_cls=False)
        self._lock = threading.Lock()

    def session_threads(self) -> int | None:
        """回读**实测生效**的 intra_op 线程数（读不到回 None）。

        这是「限线程失效要能被发现」的唯一手段（#94 P0）：补丁一旦静默失效，onnxruntime 自己
        定线程数，这里读出来就是 0（= 全核）。内核启动的「内核就绪」那行把它和目标值、核数
        一起打出来。`use_text_det=False` 时只用识别那个会话，读的就是它。
        """
        try:
            session = self._engine.text_recognizer.session.session
            return session.get_session_options().intra_op_num_threads
        except Exception:  # noqa: BLE001 - 只是回读，读不到就回 None
            return None

    def ocr(self, img: Image.Image) -> str:
        import numpy as np

        with self._lock:
            result = self._engine(np.array(img.convert("RGB")))
        rows = result[0] if isinstance(result, tuple) else result
        if not rows:
            return ""
        # RapidOCR 的一行是 [四点框, 文本, 分数]；空行/低分由它自己按 text_score 过滤。
        return " ".join(text for _box, text, _score in rows)


def create_rapid_engine(threads: int | None = None) -> RapidEngine | None:
    """建 RapidOCR；包没装、模型坏了、onnxruntime 起不来都回 None（调用方静默回落）。

    spec §6.2：「RapidOCR 缺席/初始化失败 → 静默回落 Windows OCR，只记日志，不许影响
    捕获」。所以这里把**所有**异常都吞成日志 —— 少一个引擎不该让取词变脆。
    """
    try:
        return RapidEngine(threads)
    except Exception as exc:  # noqa: BLE001 - 见 docstring：任何失败都只该退化成单引擎
        log.warning(
            "RapidOCR 不可用（%s: %s），回落 Windows OCR", type(exc).__name__, exc
        )
        return None


def warm_up_rapid(engine: RapidEngine) -> None:
    """启动预热：第一次推理要建 ONNX 会话/分配缓冲，不许落在第一次捕获上（spec §6.2）。"""
    try:
        engine.ocr(_warm_up_image())
    except Exception as exc:  # noqa: BLE001 - 预热失败不影响正确性，第一次捕获再试
        log.warning("RapidOCR 预热失败：%s: %s", type(exc).__name__, exc)


def _warm_up_image() -> Image.Image:
    """预热图：一条**真实尺寸**的行带，不是 240×60 的玩具图（#94 P2）。

    ONNX 的会话与内存池按**第一张图的形状**分配，所以预热图必须与捕获时真正进引擎的那张同形：
    玩具图预热完，第一次真实行带仍要付一次冷启动（实测 493–500ms，稳态只有 137–155ms）。
    这里画一条真机整宽的行带（`WARM_UP_BAND_WIDTH`），再走 `_prepare_band` 的同一条放大/增强
    规则 —— 出来的就是候选 A 的形状（≈8060×120，w/h≈67，§2.3 里 RapidOCR 最贵的那一档）。

    代价是内核启动多花约 0.4s（实测预热调用从 12–40ms 涨到 364–519ms，两次运行）；外壳的握手
    预算是 `PING_INTERVAL=300ms` × `PING_TIMEOUT=10000ms`，远在预算内，不会弹「内核未就绪」。
    预热只在 `build_backends` 启动时跑一次，**不许进捕获路径**。
    """
    from PIL import ImageDraw, ImageFont

    band = Image.new("RGB", (WARM_UP_BAND_WIDTH, WARM_UP_BAND_HEIGHT), "white")
    ImageDraw.Draw(band).text(
        (6, 3), "warm up the recognizer", font=ImageFont.load_default(size=18), fill="black"
    )
    return _prepare_band(band)


class DualEngine:
    """`ocr_engine` 策略的运行时（spec §6.2）：包着 Windows / RapidOCR 两路后端。

    - `auto`（默认）：两路都跑，候选图上的文本一起进 §5.2 的词表打分；RapidOCR 的文本排
      在前面，因此**同分优先 RapidOCR**（`choose_text` 的 `max` 取第一个最大值）。
    - `win`：只跑 Windows（O3 及以前的语义）。
    - `rapid`：只跑 RapidOCR；RapidOCR 缺席/失败时回落 Windows（后端列表里它在后）。

    RapidOCR 只在**候选 A**（未二值化的增强图）上跑：候选 B 是给 Windows OCR 补的 Otsu
    二值图，RapidOCR 自带归一化吃不下硬二值的笔画（实测把用户组整句全对率从 100% 打到
    25%），所以它只在需要时被 Windows 用。
    """

    def __init__(self, win, rapid: RapidEngine | None, mode: str) -> None:
        self.win = win
        self.rapid = rapid
        self.mode = mode

    @property
    def available(self) -> bool:
        return bool(self._backends())

    def describe(self) -> str:
        """日志用的一句话：模式 + 实际挂上的后端（RapidOCR 缺席时看得出回落了）+ ORT 线程。

        ORT 线程那一段是 #94 P0：把**实测生效**的 intra_op 线程数与目标值、核数一起打出来，
        否则「限线程补丁失效」这件事在日志里完全看不见（功能正常、只是又变慢）。
        """
        names = "/".join(name for name, _, _ in self._backends()) or "不可用"
        return f"{self.mode}（{names}）{self._threads_note()}"

    def _threads_note(self) -> str:
        """RapidOCR 的线程回读（#94 P0）；不是真 `RapidEngine`（单测替身）时留空。"""
        if not isinstance(self.rapid, RapidEngine):
            return ""
        actual = self.rapid.session_threads()
        shown = "读不到" if actual is None else str(actual)
        return (
            f"、ORT 线程 实测 {shown}/目标 {self.rapid.threads}（{cpu_cores()} 核）"
        )

    def _backends(self) -> list[tuple[str, Callable[[Image.Image], str], bool]]:
        """[(名字, 识别函数, 是否也在候选 B 上跑)]，**顺序即平局优先级**。"""
        win = (
            ("win", lambda img: ocr_image(self.win, img), True)
            if self.win is not None
            else None
        )
        rapid = ("rapid", self.rapid.ocr, False) if self.rapid is not None else None
        order = [win] if self.mode == "win" else [rapid, win]  # auto/rapid 都 Rapid 在前
        return [item for item in order if item is not None]

    def texts(self, img: Image.Image, binarized: bool) -> list[str]:
        """一路候选图 → 各后端的文本；任何一路抛错只记日志，不毁整次捕获（§6.2）。"""
        texts: list[str] = []
        for name, recognize_one, also_binarized in self._backends():
            if binarized and not also_binarized:
                continue
            text = self._run(name, recognize_one, img)
            if text is not None:
                texts.append(text)
        return texts

    def texts_win_first(self, img: Image.Image) -> list[str]:
        """`POLICY_WIN_FIRST`（宽带，#76）：**顺序**改成 [Windows, RapidOCR]。

        顺序就是平局优先级（`choose_text` 取第一个最大值）：宽带行上平局判给 Windows，
        这正是 #76 想留下的那一半语义；窄带仍走 `texts` 的 [RapidOCR, Windows]。

        ［#97 修订］**去掉了「Windows 分够高就不给 RapidOCR 发车」的短路**。#76 当时在
        段落语料上实测「宽带上 Windows 单路与双路择优的质量逐位相同」，于是拿「词表分
        ≥ `_SCORE_TRUST`」当信号省掉一半引擎调用。这个前提在真实视频帧上不成立：烧进
        画面的字幕（白字 + 描边、压在杂底上）上 **Windows 单引擎 8 例里错 6 例**
        （U9 语料实测 WER 27%–64%），而它的词表分正好压在 0.6 上下（实测 0.50–0.67）——
        「分够高」这条随时会把明明读对的 RapidOCR 挡在门外，用户看到的就是「框选板正、
        识别残缺」（票 #97）。代价是宽带单行多跑一路引擎（实测 +50–150ms，U4 的
        p50 ≤ 1200ms 仍宽裕）；段落的提速不吃这一路（走 #78 的并带）。`words` 参数
        一并去掉：这条路上不再读词表分。
        """
        backends = {name: recognize_one for name, recognize_one, _ in self._backends()}
        texts: list[str] = []
        for name in ("win", "rapid"):
            if name not in backends:
                continue
            text = self._run(name, backends[name], img)
            if text is not None:
                texts.append(text)
        return texts

    def _run(self, name: str, recognize_one, img: Image.Image) -> str | None:
        """跑一路后端；抛错只记日志并回 None（§6.2：一路挂了还有另一路）。"""
        try:
            return recognize_one(img)
        except Exception as exc:  # noqa: BLE001 - 一路挂了还有另一路，静默回落
            log.warning(
                "%s 引擎识别失败，本次只看其他引擎：%s: %s",
                name,
                type(exc).__name__,
                exc,
            )
            return None


def _engine_texts(engine, img: Image.Image, binarized: bool) -> list[str]:
    """候选图 → 各路引擎的文本（普通引擎只有一路；`DualEngine` 可能两路）。

    普通引擎（WinRT `OcrEngine` 或测试里的替身）保持 `ocr_image(engine, img)` 的老形状，
    v2 的调用方与既有单测逐字不变。
    """
    if isinstance(engine, DualEngine):
        return engine.texts(img, binarized)
    return [ocr_image(engine, img)]


# --- v21 长文模式：整页 OCR（规格 §3 / 票 #157） -------------------------------
#
# 这是与 `read_image` **完全独立**的一条路径，四条纪律各有实测（研究报告 §1、§3.3、§5.3）：
#
# 1. **裸喂原图**，不做 `enhance`：干净页 0.1% → 2.2%，10px 页 73.5% → 77.6%（§3.3）——
#    `enhance` 的锐化/极性归一参数是给「单行带、小图」调的，整页套上去会毁掉 ClearType
#    的亚像素边缘。
# 2. **不按固定高度自切片**（除 > `MAX_OCR_DIM`）：1000×2400 切 1200/800 高实测
#    1.7%/5.6% vs 整页 0.1%，而且不更快（§4.1）。
# 3. **只用 Windows 引擎**：整页喂 RapidOCR 在 9600px 页只认到 46/273 行（WER 99.9%）、
#    慢 30–60×（§4.2）。
# 4. 超尺寸切片时**拼行框再按 y 去重**，不拼文本：拼文本在接缝处整段重复（§5.3）。
#
# 段落切分、切片去重、全文拼接全部复用 T1 的 `longtext`（本模块不重写一套几何规则）。


def page_lines(result) -> list[longtext.PageLine]:
    """`OcrResult` → 页内行列表（按 top、left 排序）。

    `OcrLine` 自己**没有**包围盒（附录 B-1 只承诺 `Text` 与 `Words`），行框只能由词框
    并集算 —— `kernel._line_anchor` 已经是同一手法，只是这里还要 `bottom`：段落切分的
    行距信号（`gap = 本行 top − 上行 bottom`）要它，超尺寸的行框去重也要它。
    空行/没有词的行丢掉：它们没有几何可排，留着只会把行距中位数带偏。

    ［票 #187 K10］`text_angle` 非 0（含没检测到角度的 None）直接抛 `ValueError`：
    词框几何只在 `TextAngle == 0` 时成立（附录 B-1 的官方契约，`assemble_lines` 引用的
    同一条）。整页路径没有「退回 `OcrResult.Text`」的降级档 —— 段落切分与切片去重全押
    在行框上，旋转页静默产出错序全文比明确报「页面非水平排版」糟得多。
    """
    angle = result.text_angle
    if angle is None or angle != 0:
        raise ValueError(
            f"页面非水平排版（text_angle={angle!r}）：词框几何只在 TextAngle == 0 时"
            "成立（附录 B-1），拒绝按错序行框产出全文"
        )
    rows: list[longtext.PageLine] = []
    for line in result.lines:
        text = line.text.strip()
        rects = [word.bounding_rect for word in line.words]
        if not text or not rects:
            continue
        rows.append(
            longtext.PageLine(
                text=text,
                top=float(min(rect.y for rect in rects)),
                bottom=float(max(rect.y + rect.height for rect in rects)),
                left=float(min(rect.x for rect in rects)),
            )
        )
    rows.sort(key=lambda row: (row.top, row.left))
    return rows


def ocr_page_lines(engine, img: Image.Image) -> list[longtext.PageLine]:
    """整页图 → 行列表：**一次** `RecognizeAsync`，喂的就是传进来的这张图。

    BMP 编码 + `_ENGINE_LOCK` 里 `asyncio.run` 与 `_ocr_text_and_lines` 是同一条既有路径
    （#74：BMP 与画面内容无关、4–5ms；PNG 在复杂画面上要 153ms）。不共用那个函数，是因为
    它只回「文本 + 行数」，把 `lines`/`words` 丢掉了 —— 而段落切分、超尺寸去重都要行框
    （研究报告 §5.3：行级 bounding box 是长文模式的硬需求，不是加分项）。

    这里**不做**任何预处理：不紧裁、不放大、不 enhance（见本节开头第 1、2 条纪律）。
    """
    buffer = io.BytesIO()
    img.save(buffer, "BMP")
    with _ENGINE_LOCK:
        result = asyncio.run(_recognize_async(engine, buffer.getvalue()))
        # 属性也在这个锁/线程里读：WinRT 对象不承诺跨线程访问（与 `_ocr_text_and_lines` 同规矩）
        return page_lines(result)


def page_spans(height: int) -> list[tuple[int, int]]:
    """整页高 → 各片的 `[top, bottom)`（规格 §3.2）。

    **只有超过引擎上限才切**：`MAX_OCR_DIM`（10000）以内整页一次 —— 实测切片更慢也更差
    （§4.1）。超了就按 `PAGE_SLICE_HEIGHT` 切、相邻片重叠 `PAGE_SLICE_OVERLAP`：
    这一档做对去重后是**无损**的（1000×14000 页 WER 0.0%，§5.3），所以它是正式方案、
    不是降级兜底。
    """
    if height <= MAX_OCR_DIM:
        return [(0, height)]
    step = PAGE_SLICE_HEIGHT - PAGE_SLICE_OVERLAP
    spans: list[tuple[int, int]] = []
    top = 0
    while True:
        bottom = min(top + PAGE_SLICE_HEIGHT, height)
        spans.append((top, bottom))
        if bottom >= height:
            return spans
        top += step


# 「正常形态」的词元（票 #180 K1 修订）：第二支 `[A-Z]{2,}` 放行全大写词元（缩写、
# 幻灯片标题页/全大写章头）。旧口径只有第一支，实测 `fullmatch("ABC") = False`，
# 全大写页 weird≈1.0 必判 low，而 2× 重读后文本仍是大写、依旧 low —— 白付 1.7–3.2×
# 重读时间还提示「质量低」（审计 K1）。中间大写的坏词元（`aBc`）不在两支里，照旧算 weird。
_WEIRD_SHAPE = re.compile(r"[A-Za-z][a-z]*|[A-Z]{2,}")


def page_quality(text: str, line_count: int) -> tuple[float, float, str]:
    """整页结果的自检（规格 §3.3）：`(words_per_line, weird, quality)`。

    两个信号都在**词元**上算，词元取 `_WORD_RE`（`[A-Za-z]+(?:'[A-Za-z]+)?`，与
    `text_score` 同一个口径）。为什么不按空白切分：句末那个句号会把 `management.` 变成
    「非正常形态词元」，一段干净正文实测 `weird` 0.206（按词元 0.048）——
    阈值 0.12 于是对干净页**永远**报警。研究报告 §5.1 的分布（正常 0.030–0.066 /
    崩溃 0.163–0.185）只有按词元才能复现。

    `words_per_line = 总词数 / 引擎给的行数`：两个崩溃档（10px 4.8 / 纹理 5.1）与 11 个
    正常档（10.1–13.8）在这一维分得最开，`weird` 次之。**词表分不能单独用**：10px 档的
    `text_score` 0.667 比干净 web1 的 0.621 还高。

    **信号一的下限保护**（T7 阶段 2a 复标之后加，见
    `docs/research/2026-10-27-longform-page-quality-bench.md` §5.4）：总词元
    `< PAGE_QUALITY_MIN_TOKENS_FOR_WPL`（40）时**不拿 `words_per_line` 判低质**。依据是
    真机 18 例复标：4–7 个词元的单行/短选区会被「每行词数 < 8」误报（u3/u4/v07/v08 的 OCR
    其实全对），而真正的崩溃档词元数是 105（10px）/ 398（块状纹理），40 的界不会漏。
    `weird > 0.12` 不受保护影响，始终参与判定（短选区里缩写词 `it's` / `we've` 会被算成
    非正常形态，这一档仍可能报 low —— 属已知口径限制，见研究报告 §5.4）。
    0 行 / 0 词元仍然判 low（引擎什么都没读出来，不是「选区太短」）。

    阈值本身是自建语料上选的（研究报告 §5.1 / 规格 §8-B 明说不是真机日志学的），T7 阶段 2a
    用真机语料复标后**保留** 8.0 / 0.12 两个数值（证据见研究报告 §5.4）。
    """
    tokens = _WORD_RE.findall(text)
    words_per_line = len(tokens) / line_count if line_count else 0.0
    weird = 0.0
    if tokens:
        weird = sum(
            1 for token in tokens if len(token) == 1 or not _WEIRD_SHAPE.fullmatch(token)
        ) / len(tokens)
    words_per_line_low = (
        len(tokens) >= PAGE_QUALITY_MIN_TOKENS_FOR_WPL
        and words_per_line < PAGE_WORDS_PER_LINE_MIN
    )
    quality = (
        "low"
        if not tokens or line_count <= 0 or words_per_line_low or weird > PAGE_WEIRD_MAX
        else "ok"
    )
    return words_per_line, weird, quality


class PageStore:
    """整页正文的进程内缓存（规格 §3.1）：**内存里最多 1 个 page_id**，新的顶掉旧的。

    为什么正文不随 `page_ocr` 一起回：`RecvText` 对每个 chunk 单独 `StrGet("UTF-8")`，
    块尾落在多字节字符中间会静默产生 U+FFFD（#156），所以正文必须按字符分块取
    （`page_text` + `PAGE_TEXT_CHUNK`）。为什么只留一个：长文永远只有「当前这一篇」，
    留多了就是没有上限的内存增长；外壳取完立刻 `page_drop`。
    加锁：TCP 是一连接一线程（`KernelServer`），两个连接可能同时取同一页。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._page_id: str | None = None
        self._text = ""

    def put(self, text: str) -> str:
        """存一页、顶掉旧的，回新的 page_id（`p-<8hex>`，规格 §3.1 的形状）。"""
        page_id = f"p-{uuid.uuid4().hex[:8]}"
        with self._lock:
            self._page_id, self._text = page_id, text
        return page_id

    def get(self, page_id: Any) -> str | None:
        """取这一页的正文；未知/已释放的 page_id 回 None。"""
        with self._lock:
            return (
                self._text if page_id is not None and page_id == self._page_id else None
            )

    def drop(self, page_id: Any) -> None:
        """释放；未知/已释放的 page_id **幂等**（调用方可以直接回 `ok:true`）。"""
        with self._lock:
            if page_id is not None and page_id == self._page_id:
                self._page_id, self._text = None, ""


_PAGES = PageStore()


def _read_page_lines(win, crop: Image.Image) -> tuple[list[longtext.PageLine], int]:
    """整页 → 行列表：`> MAX_OCR_DIM` 时按 `page_spans` 切片 + 行框去重（规格 §3.2）。

    回 `(行列表, 片数)`。切片拼的是**行框**不是文本：拼文本在接缝处整段重复
    （研究报告 §5.3 的第一版就是栽在这里），`merge_slices` 会把每片的 y 加上偏移
    还原成整页坐标再按 y 去重。1× 与 2× 升级档共用这一条路径。
    """
    spans = page_spans(crop.height)
    if len(spans) == 1:
        return ocr_page_lines(win, crop), 1
    slices = [
        ocr_page_lines(win, crop.crop((0, top, crop.width, bottom)))
        for top, bottom in spans
    ]
    return longtext.merge_slices(slices, [top for top, _ in spans]), len(spans)


def _page_selfcheck(
    lines: list[longtext.PageLine],
) -> tuple[list[longtext.Passage], str, float, float, str, bool]:
    """行列表 → `(段列表, 全文, words_per_line, weird, quality, suspicious)`（规格 §3 / §4）。

    `suspicious` 是 v22 的段落自检（spec §7.3-6：单行段 >80% 且平均段长 <40 字）——
    说明「可能还在按行切」，外壳/预览窗据此标黄；它与 `quality` 是两件事：
    `quality` 判的是 OCR 识别质量，`suspicious` 判的是**分段形态**。
    """
    segmentation = longtext.segment_ocr(lines)
    passages = segmentation.passages
    text = longtext.join_passages(passages)
    words_per_line, weird, quality = page_quality(text, len(lines))
    return passages, text, words_per_line, weird, quality, segmentation.suspicious


def _escalated_read(win, crop: Image.Image) -> dict | None:
    """2× LANCZOS 放大重读一次（规格 §3.2 / §Further Notes A，实测见 T7 研究报告 §3）。

    回 `None` = **不升级**（原因写日志），调用方保留 1× 结果与 `quality:"low"`。三种不升级：

    - 2× 后**宽度**超 `MAX_OCR_DIM`：`page_spans` 只按高切片，救不了宽度；
    - 2× 后**总像素**超 `PAGE_ESCALATE_MAX_PIXELS`：BMP 缓冲 + PIL 副本会到几百 MB，
      常驻内核不该为一次「可能救不回来」的重试冒 OOM 风险（先算尺寸再 resize，不白分配）；
    - 2× 识别**抛错**：不炸内核，保留 1× 结果。

    高度超上限时走 `_read_page_lines` 的切片路径（9600 页 2× = 2000×19200，整页喂会
    `OSError: Image dimensions are too large!`，切片后 3 片、实测 WER 不变）。
    """
    target_w = crop.width * PAGE_ESCALATE_FACTOR
    target_h = crop.height * PAGE_ESCALATE_FACTOR
    if target_w > MAX_OCR_DIM:
        log.warning(
            "OCR 整页 质量 low→放弃 2×：放大后宽 %dpx 超引擎上限 %dpx（切片救不了宽度）",
            target_w,
            MAX_OCR_DIM,
        )
        return None
    if target_w * target_h > PAGE_ESCALATE_MAX_PIXELS:
        log.warning(
            "OCR 整页 质量 low→放弃 2×：放大后 %d×%d=%d 像素超预算 %d",
            target_w,
            target_h,
            target_w * target_h,
            PAGE_ESCALATE_MAX_PIXELS,
        )
        return None
    started = time.perf_counter()
    try:
        enlarged = crop.resize((target_w, target_h), Image.Resampling.LANCZOS)
        lines, slices = _read_page_lines(win, enlarged)
    except Exception as exc:  # noqa: BLE001 - 升级档失败不该毁掉常驻内核
        log.warning("OCR 整页 质量 low→2× 重读失败，保留 1× 结果：%s: %s", type(exc).__name__, exc)
        return None
    ms = (time.perf_counter() - started) * 1000
    passages, text, words_per_line, weird, quality, suspicious = _page_selfcheck(lines)
    return {
        "lines": lines,
        "slices": slices,
        "passages": passages,
        "text": text,
        "words_per_line": words_per_line,
        "weird": weird,
        "quality": quality,
        "suspicious": suspicious,
        "ms": ms,
    }


def page_ocr(rect: Any, engine: Any) -> dict:
    """整页识别（规格 §3.1）：回元信息与段落**偏移**，正文留在 `_PAGES` 里分块取。

    抓屏是 `grab_screen(parse_rect(rect))`，**不 pad**：长文是用户精确选的正文区域，
    外扩 6px 只会把窗口边框/滚动条摄进来（现有 capture 路径的 pad 是为「贴边单行」补的）。
    抓屏**前**按 rect 过总像素门（票 #180 K3：`w * h > PAGE_MAX_PIXELS` 回「选区过大」，
    不抓屏不分配）；再夹进虚拟屏幕（票 #187 K9，`clamp_page_rect`：小半越界夹回交集、
    大半越界报「选区大半在屏幕外」）；抓屏后仍有单边 `MAX_OCR_DIM` 门（宽切片救不了，
    明确报错）。引擎只取 `DualEngine.win`：长文路径不在 RapidOCR 上跑（见本节开头第 3 条纪律）。

    **升级档**（T7 阶段 2a，规格 §3.2 / §Further Notes A）：整页 1× → 自检；**只有**
    `quality == "low"` 才 2× LANCZOS 重读一次再自检，仍 low 就如实回 `quality:"low"`
    （**不**回落到 `read_image` / RapidOCR / `pick_bands` 逐行 —— 那几档 18–37s，实测不可接受）。
    1× 就 ok 的页**绝不**升级（干净页 2× 零收益、白花 1.7–3.2× 时间）。
    升级档失败/被预算挡住时保留 1× 结果（不炸内核），原因写日志。

    响应里的 `passage_breaks`（`[[start,end], ...]`，相对**全文**字符偏移）是 T5 面板的
    「第几段/共几段」与 T4 `article_create` 的 `passages` 入参 —— 它很小（128 段约 1–2KB），
    不违反「正文不回一条回话」的纪律（正文走 `page_text` 分块）。
    v22 增量字段（旧外壳忽略）：`passage_types` 与 `passage_breaks` 一一对应的段落类型
    （`body|heading|list|quote`，spec §7.2）；`suspicious` = 分段自检（spec §7.3-6，
    单行段 >80% 且平均段长 <40 字 → 可能仍在按行切，预览窗标黄提示）。
    v22 P1a 增量字段：`segmentation_source` = **最终采用那次**行框几何的编码
    （`[top,bottom,left,height,text]` → JSON → zlib → base64，ASCII；spec §6.7），
    外壳原样转发 `article_create`、hub 落库，P1b 的段落类型与 P2 的重新分段都由它重跑。
    `lines` / `passages` / `chars` / `words_per_line` / `weird` / `quality` 以**最终采用的那一次**
    为准；`escalated` = 本次结果是否来自 2× 重读（1× 就 ok、或 2× 被跳过/失败而保留 1× 时
    都是 `false`）；`escalated_ms` 是 2× 那一段自己的耗时（没升级时 `null`）。
    `ms` 是总耗时（抓屏 + 1× + 可能的 2×）。
    """
    started = time.perf_counter()
    try:
        x, y, w, h = parse_rect(rect)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    # 票 #180 K3：抓屏**前**按 rect 尺寸过总像素门（与升级档同一预算 `PAGE_MAX_PIXELS`）。
    # 审计 K3 的原话是「抓屏后加门」，但门往前挪到 rect 上语义等价（抓到的图就是 w×h）、
    # 还省掉那次注定要扔的 string_at 全图拷贝 —— 超限就明确报错，一个字节都不分配。
    if w * h > PAGE_MAX_PIXELS:
        return {
            "ok": False,
            "error": f"选区过大：{w}×{h}={w * h} 像素超预算 {PAGE_MAX_PIXELS}",
        }
    # 票 #187 K9：抓屏**前**把选区夹进虚拟屏幕（capture 路径有 pad_rect，长文路径原来
    # 裸抓）—— BitBlt 屏幕外补黑边，黑边进 OCR 是纯噪声还会白触发 2× 升级。
    try:
        x, y, w, h = clamp_page_rect((x, y, w, h))
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    try:
        crop = grab_screen((x, y, w, h))
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    win = engine.win if isinstance(engine, DualEngine) else engine
    if win is None:
        return {"ok": False, "error": f"{OCR_LANGUAGE} OCR 引擎不可用"}
    if crop.width > MAX_OCR_DIM:
        # 单边超上限是引擎的硬拒绝（`OSError: Image dimensions are too large!`，§0）。
        # 按高度切片救不了宽度：明确报出来，别让用户等一次白花 300ms 的抛错。
        return {
            "ok": False,
            "error": f"选区宽 {crop.width}px 超过引擎上限 {MAX_OCR_DIM}px",
        }

    try:
        lines, slices = _read_page_lines(win, crop)
    except Exception as exc:  # noqa: BLE001 - 整页失败该明确报，不该毁掉常驻内核
        log.warning("OCR 整页失败 rect=%s：%s: %s", rect, type(exc).__name__, exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    passages, text, words_per_line, weird, quality, suspicious = _page_selfcheck(lines)
    # v22 P1a（spec §6.7）：创建时的原始分段输入 —— 最终采用那次的行框编码（约 5–10KB/页）。
    source = segmentation_source.encode_ocr_lines(lines)
    elapsed = round_ms(started)
    # 日志纪律（规格 §0.4）：只记 rect、耗时、行数、段数、字符数、片数、quality、suspicious；
    # 正文一个字符都不进日志。
    log.info(
        "OCR 整页 rect=%s %sms 行 %d 段 %d 字符 %d 片 %d 质量 %s 分段可疑 %s"
        "（%.2f 词/行、weird %.3f、源 %d 字节）",
        rect,
        elapsed,
        len(lines),
        len(passages),
        len(text),
        slices,
        quality,
        suspicious,
        words_per_line,
        weird,
        len(source),
    )

    escalated = False
    escalated_ms = None
    if quality == "low":
        # 票 #180 K4：升级**前**估算剩余墙钟预算 —— 外壳 20s 就放弃（LF_LONG_TIMEOUT），
        # 2× 按实测倍数上界估算（T7 研究报告 §3：1.7–3.2×）；装不进剩余预算就保留 1×
        # 结果（与像素门同口径：原因写日志、不炸内核），别明知道装不下还白算一整轮。
        elapsed_s = elapsed / 1000
        estimated_2x_s = PAGE_ESCALATE_TIME_FACTOR_MAX * elapsed_s
        if elapsed_s + estimated_2x_s > PAGE_WALL_BUDGET_S:
            log.warning(
                "OCR 整页 质量 low→放弃 2×：已用 %.1fs，估算 2× 再需 ≤%.1fs，"
                "装不进外壳 20s 墙钟的剩余预算 %.1fs（保留 1× 结果）",
                elapsed_s,
                estimated_2x_s,
                PAGE_WALL_BUDGET_S - elapsed_s,
            )
        else:
            upgraded = _escalated_read(win, crop)
            if upgraded is not None:
                lines = upgraded["lines"]
                slices = upgraded["slices"]
                passages = upgraded["passages"]
                text = upgraded["text"]
                words_per_line = upgraded["words_per_line"]
                weird = upgraded["weird"]
                quality = upgraded["quality"]
                suspicious = upgraded["suspicious"]
                # 编码也必须跟着换成升级档的最终行框（与 `lines` 同一次结果）。
                source = segmentation_source.encode_ocr_lines(lines)
                escalated_ms = round(upgraded["ms"])
                escalated = True
                elapsed = round_ms(started)
                log.info(
                    "OCR 整页 质量 low→升级 2× rect=%s %sms 行 %d 段 %d 字符 %d 片 %d "
                    "质量 %s 分段可疑 %s（%.2f 词/行、weird %.3f、源 %d 字节；2× 段 %dms）",
                    rect,
                    elapsed,
                    len(lines),
                    len(passages),
                    len(text),
                    slices,
                    quality,
                    suspicious,
                    words_per_line,
                    weird,
                    len(source),
                    escalated_ms,
                )

    page_id = _PAGES.put(text)
    return {
        "ok": True,
        "page_id": page_id,
        "chars": len(text),
        "lines": len(lines),
        "passages": len(passages),
        "passage_breaks": [[p.start, p.end] for p in passages],
        # v22 新增（增量字段，旧外壳忽略）：段落类型与分段自检（spec §7.2 / §7.3-6）。
        "passage_types": [p.kind for p in passages],
        "suspicious": suspicious,
        # v22 P1a 新增：原始分段输入（spec §6.7），外壳原样转发给 article_create。
        "segmentation_source": source,
        "engine": "win",
        "quality": quality,
        "words_per_line": round(words_per_line, 3),
        "weird": round(weird, 3),
        "escalated": escalated,
        "escalated_ms": escalated_ms,
        "ms": elapsed,
    }


def page_text(page_id: Any, offset: Any = 0, limit: Any = PAGE_TEXT_CHUNK) -> dict:
    """按**字符**分块取正文（规格 §3.1）：单块 ≤ `PAGE_TEXT_CHUNK`（16384）字符。

    按字符而不是按字节：`RecvText` 的 UTF-8 截断缺陷（#156）只在 chunk 边界劈开多字节
    字符时出现，按字符切 + 单块远小于 socket 缓冲把风险降到可控。
    `next` 为空 = 读完。非法 offset/limit 一律 `ok:false`（**不**静默回退：外壳会以为
    自己拿到了正文，那比报错难查得多）。
    """
    try:
        start = int(offset)
        size = int(limit)
    except (TypeError, ValueError):
        return {"ok": False, "error": "offset/limit 必须是整数"}
    text = _PAGES.get(page_id)
    if text is None:
        return {"ok": False, "error": "未知 page_id（可能已释放）"}
    if start < 0 or start > len(text):
        return {"ok": False, "error": f"offset 超出正文范围（0..{len(text)}）"}
    if size < 1:
        return {"ok": False, "error": "limit 必须是正整数"}
    end = min(start + min(size, PAGE_TEXT_CHUNK), len(text))
    return {
        "ok": True,
        "offset": start,
        "text": text[start:end],
        "next": end if end < len(text) else None,
    }


def page_drop(page_id: Any) -> dict:
    """释放整页正文；未知/已释放的 page_id **幂等**回 `ok:true`（规格 §3.1）。"""
    _PAGES.drop(page_id)
    return {"ok": True}


# --- 失败留图（spec §5.5：不进 vault，不占 git 与手机同步体积）-----------------


def failed_image_path(now: datetime | None = None) -> Path:
    # 毫秒级后缀（票 #187 K5）：秒级精度下同一秒的两次失败会互相覆盖 —— 恰好是「连续
    # 两次失败」的场景，第一张现场还没看就被第二张顶掉。文件名按时间排序的性质不变。
    now = now or datetime.now()
    return FAILED_DIR / f"{now:%Y%m%d-%H%M%S}-{now.microsecond // 1000:03d}.png"


def cleanup_failed_images(now: datetime | None = None) -> int:
    """failed 留图的保留期清理（票 #187 K5），回删除张数。

    两条规则（审计 K5+S4：留图是隐私残留面，不该无上限堆积）：
    - **保留期**：mtime 早于 `FAILED_KEEP_DAYS` 天前的先删；
    - **数量上限**：剩下的按 mtime 从新到旧留 `FAILED_KEEP_MAX_FILES` 张，其余删掉。

    在内核启动时与每次留图落盘后调用（`save_failed_image` / `main`）。清理是旁路
    卫生工作，任何一步失败都只记日志不许毁掉捕获路径 —— 留图写不进是失败语义的一部分，
    删不掉旧图不是。
    """
    try:
        paths = list(FAILED_DIR.glob("*.png"))
    except OSError as exc:
        log.warning("failed 留图清理失败（读目录）：%s: %s", type(exc).__name__, exc)
        return 0
    if not paths:
        return 0
    aged: list[tuple[float, str, Path]] = []
    for path in paths:
        try:
            aged.append((path.stat().st_mtime, path.name, path))
        except OSError as exc:  # 删不掉的这轮跳过，下一轮再试
            log.warning("failed 留图清理跳过 %s：%s: %s", path.name, type(exc).__name__, exc)
    cutoff = (now or datetime.now()).timestamp() - FAILED_KEEP_DAYS * 86400
    aged.sort()  # (mtime, 文件名)：从旧到新
    doomed = [path for mtime, _name, path in aged if mtime < cutoff]
    survivors = aged[len(doomed) :]
    if len(survivors) > FAILED_KEEP_MAX_FILES:
        doomed.extend(path for _mtime, _name, path in survivors[: -FAILED_KEEP_MAX_FILES])
    removed = 0
    for path in doomed:
        try:
            path.unlink()
            removed += 1
        except OSError as exc:
            log.warning("failed 留图删除失败 %s：%s: %s", path.name, type(exc).__name__, exc)
    if removed:
        log.info(
            "failed 留图清理：删 %d 张（保留期 %d 天、上限 %d 张）",
            removed,
            FAILED_KEEP_DAYS,
            FAILED_KEEP_MAX_FILES,
        )
    return removed


def save_failed_image(img: Image.Image, now: datetime | None = None) -> Path:
    FAILED_DIR.mkdir(parents=True, exist_ok=True)
    path = failed_image_path(now)
    img.save(path, "PNG")
    cleanup_failed_images()
    return path


def failed_response(crop: Image.Image, error: str) -> dict:
    saved = save_failed_image(crop)
    return {"ok": False, "error": error, "saved": str(saved)}


# --- 一条捕获的完整路径 ------------------------------------------------------


def recognize(
    rect: Any,
    engine,
    corrector: Corrector | None = None,
    suspector: Suspector | None = None,
) -> dict:
    """外扩抓屏 → §5.1 流水线 → §5.2 双候选择优 → §5.3 词表后校正 → 响应。

    失败/空结果的语义与 v1 完全一致：留图 + `ok:false`（§5.1「保持对外语义」）。
    四个字段的分工（spec §5.2/§5.3 + v17 W5）：`raw` 是引擎原样输出的识别文本、
    `text` 是后校正之后的文本、`corrections` 是逐条改动（`ocr_correct=0` 时
    `corrector` 为 None，`text == raw` 且 `corrections == []`）；`suspects` 是
    疑似粘连的只读标记（旁路信号，可选字段——`suspector` 为 None 时整个键缺席，
    旧外壳忽略未知字段、新外壳按「无标记」处理）。`text`/`raw`/`corrections` 的
    语义与取值不因标记而变（W5 红线：只标记、不改文本）。
    """
    if engine is None or (isinstance(engine, DualEngine) and not engine.available):
        return {"ok": False, "error": f"{OCR_LANGUAGE} OCR 引擎不可用"}

    started = time.perf_counter()
    crop = grab_screen(pad_rect(parse_rect(rect)))
    try:
        raw, passes = read_image(engine, crop)
    except Exception as exc:  # noqa: BLE001 - 识别/解码失败也算失败，留图才有现场可复现
        response = failed_response(crop, f"{type(exc).__name__}: {exc}")
        log.warning(
            "OCR 抛错 rect=%s: %s: %s（留图 %s）",
            rect,
            type(exc).__name__,
            exc,
            response["saved"],
        )
        return response

    if not raw.strip():
        elapsed = round_ms(started)
        response = failed_response(crop, "empty result")
        log.info("OCR 空结果 rect=%s %sms（留图 %s）", rect, elapsed, response["saved"])
        return response

    corrected: str = raw
    fixes: list[dict[str, str]] = []
    if corrector is not None:
        try:
            corrected, fixes = corrector(raw)
        except Exception as exc:  # noqa: BLE001 - 后校正跑挂不该毁掉这次捕获（留图+失败更贵）
            corrected, fixes = raw, []
            log.warning(
                "词表后校正出错，本次按原样返回：%s: %s", type(exc).__name__, exc
            )

    # W5：疑似粘连只标记（相对校正后 text 的偏移）。跑挂按「无标记」返回，
    # 与坏 corrector 同一纪律 —— 标记只是提示，绝不能比没有它更糟。
    suspects: list[dict[str, object]] = []
    if suspector is not None:
        try:
            suspects = suspector(corrected)
        except Exception as exc:  # noqa: BLE001 - 见上，标记挂了按无标记处理
            suspects = []
            log.warning(
                "疑似粘连标记出错，本次按无标记返回：%s: %s", type(exc).__name__, exc
            )
        if suspects:
            log.info(
                "疑似粘连标记 %d 处：%s",
                len(suspects),
                " ".join(str(item["token"]) for item in suspects),
            )  # 只记条数与 token（单词），不记整句

    elapsed = round_ms(started)
    log.info(
        "OCR 成功 rect=%s %sms 文本 %d 字（OCR %d 遍、校正 %d 处）",
        rect,
        elapsed,
        len(corrected),
        passes,
        len(fixes),
    )  # 只记长度与处数，不记内容
    response: dict = {
        "ok": True,
        "text": corrected,
        "raw": raw,
        "corrections": fixes,
        "ms": elapsed,
    }
    if suspector is not None:
        response["suspects"] = suspects
    return response


def dispatch(req: Any, recognizer: Responder, engine: Any = None) -> dict:
    """一条请求 → 一条响应（spec §5.3 的协议表 + v21 §3.1 的三条长文命令）。

    `engine` 只服务长文命令（`page_ocr` 要自己抓屏、且只能用 Windows 引擎）；
    老命令（`ping` / `capture_ocr`）的语义与调用方式一个字不动，所以旧调用点
    `dispatch(req, recognizer)` 照常工作。
    """
    started = time.perf_counter()
    if not isinstance(req, dict):
        return {"ok": False, "error": "bad request"}
    cmd = req.get("cmd")
    if cmd == "ping":
        return {"ok": True, "pong": round_ms(started)}
    if cmd == "capture_ocr":
        return recognizer(req.get("rect"))
    if cmd == "page_ocr":
        if engine is None:
            return {"ok": False, "error": f"{OCR_LANGUAGE} OCR 引擎不可用"}
        return page_ocr(req.get("rect"), engine)
    if cmd == "page_text":
        return page_text(
            req.get("page_id"),
            req.get("offset", 0),
            req.get("limit", PAGE_TEXT_CHUNK),
        )
    if cmd == "page_drop":
        return page_drop(req.get("page_id"))
    return {"ok": False, "error": "unknown cmd"}


# --- TCP 服务：新行分隔 JSON，异步收发，外壳永不阻塞 --------------------------


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        while True:
            line = self.rfile.readline()
            if not line:
                return
            self._answer(line)
            if not line.endswith(b"\n"):  # 客户端最后一条没带换行就把连接关了
                return

    def _answer(self, raw: bytes) -> None:
        try:
            req = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send({"ok": False, "error": "bad json"})
            return
        try:
            response = self.server.respond(req)  # type: ignore[attr-defined]  # 见 KernelServer
        except Exception as exc:
            log.exception("处理请求出错")
            response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        self._send(response)

    def _send(self, response: dict) -> None:
        self.wfile.write(
            json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n"
        )
        self.wfile.flush()


class KernelServer(socketserver.ThreadingTCPServer):
    """一个连接一个线程：一次捕获卡住不会冻住下一次热键。"""

    allow_reuse_address = (
        False  # Windows 上开了它，第二个内核会静默抢端口；这里要的是「绑不上就报错」
    )
    daemon_threads = True

    def __init__(self, address: tuple[str, int], respond: Responder) -> None:
        self.respond = respond
        super().__init__(address, _Handler)


# --- 配置与启动（spec §9、§10.1）--------------------------------------------


def read_config(path: Path = CONFIG_PATH) -> dict[str, str]:
    """读 [英语取词] 段。interpolation=None：值里可能有 %A_ScriptDir% 这种字面百分号。"""
    parser = configparser.ConfigParser(interpolation=None)
    if not parser.read(path, encoding="utf-8") or not parser.has_section(
        CONFIG_SECTION
    ):
        log.warning(
            "config.ini 里没有 [%s] 段（%s），本次用内置默认值", CONFIG_SECTION, path
        )
        return {}
    return dict(parser[CONFIG_SECTION])


def config_int(config: dict[str, str], key: str, default: int) -> int:
    try:
        return int(config[key])
    except (KeyError, ValueError):
        log.warning(
            "config 的 %s 不是整数（%r），用默认值 %s", key, config.get(key), default
        )
        return default


def config_mode(config: dict[str, str], key: str, default: str) -> str:
    """读 `ocr_engine` 这类枚举键：值不在白名单（含键缺席）就用代码里的默认值。"""
    value = (config.get(key) or "").strip().lower()
    if value in OCR_ENGINE_MODES:
        return value
    if value:
        log.warning(
            "config 的 %s=%r 不是 %s 之一，用默认值 %s",
            key,
            config.get(key),
            "/".join(OCR_ENGINE_MODES),
            default,
        )
    return default


def build_backends(
    mode: str, threads: int | None = None
) -> tuple[Any, RapidEngine | None]:
    """按 `ocr_engine` 策略建两路引擎，回 (Windows 引擎 | None, RapidEngine | None)。

    **建引擎的顺序不能反**：RapidOCR 必须在 Windows 引擎**之前** import（`onnxruntime`
    与 `winrt` 的 DLL 在同一进程里加载的顺序有讲究 —— 先建 WinRT 引擎再 import
    onnxruntime 会让解释器在 `onnxruntime/__init__.py` 里直接段错误退出，本机实测
    exit 139）。所以即便 `ocr_engine=win` 完全用不到 RapidOCR，这里也不碰它；只要用，
    就先把它建好。这条约束在 #44 的实现里用一份独立脚本 + faulthandler 定位到。

    `threads`（#94 P1）透传给 RapidOCR 的 ONNX 会话（None = `ort_threads()` 的规则值）。
    """
    rapid = create_rapid_engine(threads) if mode in ("auto", "rapid") else None
    if rapid is not None:
        started = time.perf_counter()
        warm_up_rapid(rapid)
        log.info("RapidOCR 预热完成 %sms", round_ms(started))  # 不许落在捕获路径上（§6.2）
    try:
        win = create_engine()
    except Exception as exc:  # noqa: BLE001 - 建不起来只记日志：RapidOCR 可能仍在
        win = None
        log.error("创建 Windows OCR 引擎失败：%s: %s", type(exc).__name__, exc)
    return win, rapid


def build_engine_pool(config: dict[str, str]) -> DualEngine:
    """按 config 的 `ocr_engine` 装配两路引擎（spec §6.2）：Windows 常驻 + RapidOCR 预热。

    Windows 引擎建不起来不再是致命错（老行为是 `engine=None` 让捕获明确报错），现在只要
    RapidOCR 还在就能继续；反之 RapidOCR 永远只是加成。

    线程数在这里从 config 读一次（#94 P1）：`ocr_ort_threads` 缺省 = `max(4, 核数//2)`。
    """
    mode = config_mode(config, "ocr_engine", OCR_ENGINE_DEFAULT)
    win, rapid = build_backends(mode, ort_threads(config))
    return DualEngine(win=win, rapid=rapid, mode=mode)


def _setup_logging(enabled: bool) -> None:
    if not enabled:
        logging.getLogger().addHandler(logging.NullHandler())
        return
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        LOG_DIR / "kernel.log",
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUPS,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="英语取词 OCR 内核（常驻 TCP 服务，无界面）"
    )
    parser.add_argument(
        "--port",
        type=int,
        help=f"TCP 端口，默认取 config.ini 的 port（内置默认 {DEFAULT_PORT}）",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    enable_dpi_awareness()
    config = read_config()
    _setup_logging(config_int(config, "log_enabled", 1) != 0)

    port = (
        args.port if args.port is not None else config_int(config, "port", DEFAULT_PORT)
    )

    # spec §6.2：引擎装配交给 build_engine_pool（Windows + RapidOCR，按 ocr_engine 策略）。
    # 这里可能花 1~2s（RapidOCR 建 ONNX 会话 + 预热），全都发生在**服务起来之前**，
    # 不在任何捕获路径上。
    engine = build_engine_pool(config)

    # v1 的 upsample_min_height / --min-height 在 v2 不再参与决策：放大倍数由
    # §5.1.3 的 band 区间 [56,120] 定（键仍留在 config.ini 里，外壳照旧回显）
    corrector = build_corrector(config)
    suspector = build_suspector(config)  # W5：疑似粘连只标记（默认开，三重门见函数注释）
    # 票 #187 K5：启动时清一次过期留图（之后每次落盘也会清）——留图是屏幕截图，
    # 长期堆积就是隐私残留面。
    cleanup_failed_images()
    recognizer = functools.partial(
        recognize, engine=engine, corrector=corrector, suspector=suspector
    )
    server = KernelServer(
        ("127.0.0.1", port), lambda req: dispatch(req, recognizer, engine)
    )
    known, common = correction_wordlists()
    log.info(
        "内核就绪 127.0.0.1:%s（语言 %s，引擎 %s，词表 %d 常见 + %d 已知，后校正 %s，"
        "band %d-%dpx）",
        server.server_address[1],
        OCR_LANGUAGE,
        engine.describe(),
        len(common),
        len(known),
        "开" if corrector else "关",
        BAND_MIN_HEIGHT,
        BAND_MAX_HEIGHT,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("收到中断，退出")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
