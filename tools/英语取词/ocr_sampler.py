"""OCR 字幕带采样器（v33 T2，票 #208 / spec §C10–C15）：定区 → 差分门 → OCR →
多帧投票 → 噪声过滤 → 待选句落库（带底图与可疑标）。

决策的事实来源（全部 2026-11-07 实测/调研，路径见 docs/research/v33-capture-routes/）：

- spec `docs/specs/v33-video-subtitle-backlog.md` §C：定区三档（复用上次区域 →
  有字行签名自检 → 手动重拖）、采样节拍 ~2s、像素差分门、多数派投票、噪声前置
  过滤、可疑标与底图；
- R2 调研（多帧共识机理）：同句持续 2–4s，多帧共识是字幕 OCR 生产工具的标准做法；
- R4 小样实测：det 默认参数（min/736）把窄条放大 ~6 倍 → 1867ms/帧且切碎词距
  （"in a new"→"inanew"）；**必须 limit_type=max** → 676ms/帧且词距完整。
  投票不能按最高 conf：1 帧坏变体 conf 0.996 会否决 5 帧好变体 0.952（173 组里
  两种规则差 25 组）。噪声行（"Y9V9Y0" 类水印、花字碎片）必须在对齐之前剔掉，
  否则会打断合池对齐（spec §C13a）。

模块分层（照 hub 侧采集器惯例，纯逻辑与引擎/像素层分离，测试全程离线注入）：

- 纯函数层：`latin_dominant` / `is_noise_line` / `vote_lines`（多数派投票）/
  `band_masks_differ`（差分门）/ `load_last_region` / `save_last_region`（定区持久化）；
- 像素层：`band_text_mask`（有字行签名 + 差分门的输入）/ `band_clarity`（梯度能量，
  同句存活窗内选最清晰帧存底图）；
- 引擎层：`BandOcrEngine`（RapidOCR det+rec，**参数钉死 `OCR_ENGINE_PARAMS`**）；
- 组合层：`SubtitleBandSampler.feed()`（逐帧喂）+ `flush()`（投票出句 → 剔噪 →
  落库）。触发编排/心跳/热键接线是 T5 的事（spec §C10），这里只交付被调用的入口。

日志纪律（store §10 同款）：只记数量与原因码，不记句文本。
"""

from __future__ import annotations

import difflib
import json
import logging
import math
import queue
import re
import statistics
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import corrections
import kernel
import settings
from store import CJK_CHAR_RE, Store, is_latin_line, one_line

log = logging.getLogger("hub.ocr_sampler")

TOOL_DIR = Path(__file__).resolve().parent

# --- OCR 引擎参数（spec §C12 / R4 实测；票 #208 AC2 的回归钉） -------------------
#
# det 必须显式 limit_type=max：RapidOCR 默认 min/736 会把 852x125 的窄条**短边放大
# ~6 倍**，det 输入像素暴涨 ~36 倍 → R4 实测 1867ms/帧（max 模式 676ms），而且放大后
# det 把整句切碎、词间空格丢失（"in a new"→"inanew"），同一句 max 模式反而完整。
# cls 关闭（字幕不倒置，白跑）。耗时/词距的差异以**参数钉住** + 一次性真机冒烟
# 记录数字（R4 报告 §2 与施工报告），pytest 不测墙钟（CI 上 flaky）。
#
# 键名按 requirements 钉死的 rapidocr-onnxruntime==1.2.3（R4 探针用的 1.4.4 把
# `use_angle_cls` 简写成了 `use_cls`）。1.2.3 的 UpdateParameters 有个已知毛病：
# 传**任何** det_* 键都会去读 `model_path`，不传就 KeyError —— `det_model_path=None`
# 是为绕开它补的占位，None 会回落 config.yaml 的随包模型。
OCR_ENGINE_PARAMS: dict[str, Any] = {
    "det_limit_type": "max",
    "det_limit_side_len": 736,
    "det_model_path": None,
    "use_angle_cls": False,
    # 2026-11-08 真机取证（#222 / docs/research/2026-11-08-ocr-recognition-issues.md
    # §6.1）：RapidOCR 出厂 width_height_ratio=8，字幕带 2491×193（12.9）、
    # 1305×138（9.5）都会**静默跳过检测头**，整条带压成 48px 一行识别 → 丢空格、
    # 丢字母、丢第二行（`what f fve` / `lessstressful` / `somenew`）。
    # -1 = 永不按宽高比跳过；+30~100ms/帧，采样节拍 2s，占空比可忽略。
    "width_height_ratio": -1,
}

# 采样节拍（spec §C12：~2s）。节拍本身归 T5 的编排循环管，这里钉常量供它引用；
# 占空比口径：换句触发下 OCR ~676ms / 句均存活 ~3.5s ≈ 单核 20%，预算内。
SAMPLE_PERIOD_S = 2.0

# --- 多帧投票参数（spec §C13 / R4）----------------------------------------------
VOTE_RATIO_THR = 0.85  # R4：组内变体相似度门（difflib ratio），553 行 → 173 组
VOTE_GAP_S = 6.0  # 组内相邻成员最大时间差（R4 用 12 个采样间隔 × 0.5s = 6s；句子显示 1–3.5s）

# --- 噪声过滤参数（spec §C13a / R4 数据校准，见 test_ocr_sampler.py）------------
NOISE_DIGIT_RATIO = 0.3  # 数字占比超它 = 水印乱码（"19V9Y07"/"Y9V9Y0" 族全在 0.33+）
NOISE_TOKEN_MAX = 5  # 无空格短 token 且不在词表 = 花字碎片（"UJF"/"SUJ"/"TOF" 类）

# --- 有字行签名参数（spec §C11 第二档）------------------------------------------
# 掩码墨量（1 像素占比）落在窗口内 = 这块区域还像「一条字幕带」。下界挡「纯黑无字」，
# 上界挡「落在亮场景/花屏上」。没有字幕的片段也会连续失检 —— 窗口调宽靠 T5 真机
# 手测清单（区域失效自检）校准，机制先按最简版交付。
SIG_INK_MIN = 0.004
SIG_INK_MAX = 0.35
SIG_FAIL_LIMIT = 15  # 连续失检次数（~30s @2s 节拍）才请用户重拖，降低无字幕片段的误报
# 差分门：掩码差异数占比超过它才算「带子变了」（视频压缩让未变文字有轻微闪烁）
DIFF_CHANGED_RATIO = 0.02

# 白字阈值（灰度 ≥ 它算文字像素）：白/黄字配暗底是字幕的主流渲染，占空比数值由
# T5 真机清单复核。阈值放这里不放 config：先按常量交付，出现真实需求再升键。
BRIGHT_FLOOR = 200
MASK_DOWNSCALE = 4  # 掩码降采样（852x125 → 213x31），差分门是毫秒级的事

# --- 定区持久化（spec §C11 第一档；照 config.ini 的 box_x/box_y 先例）-----------
# 键按分辨率限定（同分辨率下播放器布局基本不变）：`ocr_band_region_<宽>x<高>`，
# 值 "x,y,w,h"。运行时写 config.ini 是既有惯例（box_x/box_y、extension_token 同款）；
# config.ini 里这些本地状态键的改动**不得提交**（仓库纪律见根 AGENTS.md）。
REGION_KEY_PREFIX = "ocr_band_region"
# 2026-11-11（区域校正）：用户拖过的区域再存一份**相对视频内容框**的比例
# `ocr_band_rel_<宽>x<高>` = "l,t,r,b"（0–1）。页面滚动/换布局/影院模式让视频框
# 在屏幕上移动时，按当前视频框重新投影，绝对屏幕坐标不再失效。
REL_REGION_KEY_PREFIX = "ocr_band_rel"
# 2026-11-11（用户反馈「框选区域更长一点能包住更多的」）：自动复用区域时
# 纵向扩 15%，覆盖两行/更大字号字幕；只在读取时扩，不写回 config（避免叠加）。
AUTO_BAND_PAD_RATIO = 0.15

# --- 底图落盘（spec §C14）--------------------------------------------------------
# 底图跟待选句同寿命（挑词界面「看图改句」要用，spec 用户故事 10）；TTL 清理是
# T7 的维护守护模式，本期不做。
DEFAULT_IMAGE_DIR = TOOL_DIR / "data" / "subtitle_bands"

# 待选句来源（store.BACKLOG_SOURCES 的 ocr 档）
SOURCE = "ocr"

# 谁把字幕带（PIL Image / ndarray）变成东西：测试注入假层，生产用下面的默认实现。
Band = Any
MaskOf = Callable[[Band], bytes]
EnergyOf = Callable[[Band], float]
OcrFn = Callable[[Band], Sequence[tuple[str, float]]]
SaveImage = Callable[[Band, Mapping[str, Any]], str]

# 既有可信门阈值沿用（spec §C14）：kernel §5.2 的词表命中率门。借私有名是刻意的 ——
# 阈值在 kernel 改一处，采样器自动跟随，绝不在两处复制数字。
_SCORE_TRUST = kernel._SCORE_TRUST


# --- 纯函数：行过滤（§C13a ②）----------------------------------------------------
# CJK 字符类引用 store 的单一来源常量（v33 修复轮）：两处各写一份迟早漂移。


_LATIN = re.compile(r"[A-Za-z]")


def latin_dominant(text: str) -> bool:
    """拉丁主导行判定（比 store.is_latin_line 更早的一道，投票之前跑）。

    R4 建议 4：中英双语的中文翻译行整行不要；「中文行内嵌英文书名」（如
    `这是《FluentForever>,作者是GabrielWyner`）能绕过「拉丁数 > CJK 数」，
    补 CJK 兜底 —— 有 CJK 就只放行「CJK ≤ 1 且拉丁占比 > 0.7」的行。
    纯符号行（拉丁字母 < 2）也在这里丢掉。
    """
    latin = len(_LATIN.findall(text))
    if latin < 2:
        return False
    cjk = len(CJK_CHAR_RE.findall(text))
    if cjk == 0:
        return True
    return cjk <= 1 and latin / (latin + cjk) > 0.7


def is_noise_line(text: str, wordlist: frozenset[str] | None) -> bool:
    """乱码行判定（§C13a ①，对齐/落库之前剔掉）。

    按「字符类别比例 + 词表」联合判定（R4 校准，`tests/fixtures/ocr_frames_raw.json`
    全量回放：173 组剔 38 组，被剔的全是水印乱码与花字碎片，保留集中位 ASR 相似度
    1.0，真实句零误伤 —— 断言在 test_ocr_sampler.py）：

    - 无拉丁字母：纯符号行；
    - 数字占比 > 0.3：水印/滚动数字乱码（"19V9Y07"、"Y9V9Y0"、"V9Y" 族）；
    - 无空格短 token（去符号后 ≤ 5 字母）且不在词表：花字碎片（"UJF"、"TOF"）。
      真实的单词字幕行（the / brains / vocabulary）都在词表里，不受影响。
    """
    alnum = re.sub(r"[^A-Za-z0-9]", "", text)
    letters = re.sub(r"[^A-Za-z]", "", alnum)
    if not letters:
        return True
    if (len(alnum) - len(letters)) / len(alnum) > NOISE_DIGIT_RATIO:
        return True
    tokens = text.split()
    if len(tokens) == 1 and wordlist:
        token = re.sub(r"[^A-Za-z]", "", tokens[0]).lower()
        if 0 < len(token) <= NOISE_TOKEN_MAX and token not in wordlist:
            return True
    return False


# --- 纯函数：多帧投票（§C13）------------------------------------------------------


def norm_vote_text(text: str) -> str:
    """投票前归一化（R4 口径）：小写、非字母数字 → 空格、压空白。"""
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


@dataclass
class _VoteGroup:
    """一个投票组 = 同一句的存活窗。`rep` 照 R4 语义：**最新**成员的归一化文本。"""

    members: list[tuple[int, float, str, float]]
    t_last: float
    rep: str
    band: Band | None = None  # 存活窗内最清晰一帧的带图（落底图用）
    clarity: float = -1.0


@dataclass
class VotedLine:
    """flush 后的一句：多数派胜出的变体 + 时间窗 + 质量元数据。"""

    text: str
    t_first: float
    t_last: float
    mean_conf: float
    n_members: int
    first_si: int
    band: Band | None


def _select_variant(members: Sequence[tuple[int, float, str, float]]) -> tuple[str, float]:
    """组内选变体（R4 v2，票 #208 AC3 的规则本身）：

    **帧数多数派优先，其次平均 conf，平手取更长文本**。纯最高 conf 会翻车 ——
    R4 实测：1 帧坏变体（"in a newlanguage"，conf 0.996）否决 5 帧好变体
    （"in a new language"，conf 0.952），173 组里两种规则差 25 组且系统性偏向
    丢空格变体。多帧投票的价值在「数票数」，不在「看分数」。
    """
    by_text: dict[str, list[float]] = {}
    for _si, _t, text, conf in members:
        by_text.setdefault(text, []).append(conf)
    variants = [
        (text, len(confs), statistics.mean(confs))
        for text, confs in by_text.items()
    ]
    variants.sort(key=lambda v: (-v[1], -v[2], -len(v[0])))
    text, _n, mean_conf = variants[0]
    return text, mean_conf


class _VoteState:
    """在线投票流（与 R4 的批量算法同一语义，逐帧喂入）。

    R4 是离线批量跑的；采样器是流式的，分组逻辑必须逐帧可得才能给组挂底图
    （每来一帧，命中组的带图按清晰度择优替换）。批量回放走 `vote_lines()`，
    它把序列一次性喂进同一个状态机 —— 同一条代码路径，测试才能按 R4 复现。
    """

    def __init__(self, *, ratio_thr: float = VOTE_RATIO_THR, gap_s: float = VOTE_GAP_S) -> None:
        self.ratio_thr = ratio_thr
        self.gap_s = gap_s
        self.groups: list[_VoteGroup] = []

    def add(
        self,
        si: int,
        t: float,
        text: str,
        conf: float,
        *,
        band: Band | None = None,
        clarity: float = -1.0,
    ) -> None:
        nt = norm_vote_text(text)
        if not nt:
            return
        best: _VoteGroup | None = None
        best_r = 0.0
        for group in self.groups:
            if t - group.t_last > self.gap_s:
                continue  # 存活窗外的组不再吸收（但不删：创建序即句序，flush 时统一出）
            r = difflib.SequenceMatcher(None, nt, group.rep).ratio()
            if r > best_r:
                best, best_r = group, r
        if best is not None and best_r >= self.ratio_thr:
            best.members.append((si, t, text, conf))
            best.t_last = t
            best.rep = nt
            if clarity > best.clarity:
                best.band, best.clarity = band, clarity
        else:
            self.groups.append(
                _VoteGroup([(si, t, text, conf)], t, nt, band, clarity)
            )

    def finalize(self) -> list[VotedLine]:
        voted: list[VotedLine] = []
        for group in self.groups:
            text, mean_conf = _select_variant(group.members)
            voted.append(
                VotedLine(
                    text=text,
                    t_first=group.members[0][1],
                    t_last=group.t_last,
                    mean_conf=mean_conf,
                    n_members=len(group.members),
                    first_si=group.members[0][0],
                    band=group.band,
                )
            )
        return voted


def vote_lines(
    seq: Sequence[tuple[int, float, str, float]],
    *,
    ratio_thr: float = VOTE_RATIO_THR,
    gap_s: float = VOTE_GAP_S,
) -> list[VotedLine]:
    """批量投票（R4 回放入口）：seq = [(si, t, text, conf)] 按时间序。"""
    state = _VoteState(ratio_thr=ratio_thr, gap_s=gap_s)
    for si, t, text, conf in seq:
        state.add(si, t, text, conf)
    return state.finalize()


# --- 像素层：有字行签名 / 差分门 / 清晰度 ----------------------------------------


def _to_pil(band: Band):
    """band → PIL Image（ndarray / PIL 都收；别的类型原样抛给调用方）。"""
    if hasattr(band, "convert"):  # PIL Image
        return band
    import numpy as np
    from PIL import Image

    return Image.fromarray(np.asarray(band))


def band_text_mask(band: Band, *, bright_floor: int = BRIGHT_FLOOR, downscale: int = MASK_DOWNSCALE) -> bytes:
    """字幕带 → 文字掩码（每像素 1 字节 0/1，降采样后）。

    灰度 ≥ bright_floor 算文字像素（白/黄字主流渲染）。这是两道门的公共输入：
    有字行签名看墨量占比（§C11），差分门看掩码差异（§C12）。

    顺序必须是**先降采样再阈值**：先阈值会把掩码变成 0/1 的极值图，BOX 混合会把
    细笔画平均成 0（整条掩码被插值吃掉）；先在灰度上混合、再阈值，笔画内部保有
    原亮度，只有边缘像素参与折算。
    """
    image = _to_pil(band).convert("L")
    if downscale > 1:
        from PIL import Image

        w, h = image.size
        image = image.resize(
            (max(1, w // downscale), max(1, h // downscale)), Image.Resampling.BOX
        )
    bw = image.point(lambda v: 1 if v >= bright_floor else 0)
    return bw.tobytes()  # mode "L" 的 0/1 图：一像素一字节，与 getdata 同序


def band_masks_differ(
    prev: bytes, cur: bytes, *, changed_ratio: float = DIFF_CHANGED_RATIO
) -> bool:
    """差分门（§C12）：掩码差异占比 > changed_ratio 才算「带子变了」。

    形状不同（换分辨率/换区域）直接算变了。同带 → False → 跳过 OCR（零调用）。
    """
    if len(prev) != len(cur):
        return True
    diff = sum(1 for a, b in zip(prev, cur) if a != b)
    return diff / len(prev) > changed_ratio


def mask_ink_ratio(mask: bytes) -> float:
    """掩码墨量占比（有字行签名的判据）。"""
    if not mask:
        return 0.0
    return sum(1 for v in mask if v) / len(mask)


def band_clarity(band: Band) -> float:
    """梯度能量（R2 §3.2：同句存活窗内选最清晰帧的指标；运动模糊/伪影最重的帧能量低）。"""
    from PIL import ImageFilter, ImageStat

    edges = _to_pil(band).convert("L").filter(ImageFilter.FIND_EDGES)
    return float(ImageStat.Stat(edges).rms[0])


def _default_mask_of(band: Band) -> bytes:
    return band_text_mask(band)


def _default_energy_of(band: Band) -> float:
    return band_clarity(band)


# --- 引擎层：RapidOCR det+rec（参数钉死）-----------------------------------------


class BandOcrEngine:
    """RapidOCR（det 开、cls 关）的字幕带封装。

    与 kernel.RapidEngine（取词路径，`use_text_det=False`）是两个场景：字幕带是
    多行窄条，行定位交给 det。参数**必须**走 `OCR_ENGINE_PARAMS`（limit_type=max，
    R4 实测默认参数 1867ms/帧且切碎词距，见模块头注释）。

    `threads`：照 kernel #94 P1 的既有做法限 ONNX intra_op 线程 —— 采样与视频
    解码同机并存，不限线程会互相抢核（spec §C12）。
    """

    def __init__(self, threads: int | None = None) -> None:
        self.threads = kernel.ort_threads() if threads is None else threads
        try:
            kernel._cap_rapid_session_threads(self.threads)
        except Exception as exc:  # noqa: BLE001 - 补丁点没了只该变慢，不该让采样失败
            log.warning(
                "字幕带 OCR 限线程没打上（%s: %s），本次按全核跑", type(exc).__name__, exc
            )
        from rapidocr_onnxruntime import RapidOCR

        self._engine = RapidOCR(**OCR_ENGINE_PARAMS)
        self._lock = threading.Lock()

    def session_threads(self) -> int | None:
        """回读**实测生效**的 intra_op 线程数（读不到回 None）—— kernel #94 P0 同款纪律：
        限线程一旦静默失效，这里读出来是 0（= 全核），要能被发现。"""
        try:
            session = self._engine.text_recognizer.session.session
            return session.get_session_options().intra_op_num_threads
        except Exception:  # noqa: BLE001 - 只是回读，读不到就回 None
            return None

    def __call__(self, band: Band) -> list[tuple[str, float]]:
        """带图 → [(text, conf), ...]。兼容 rapidocr 1.3.x（tuple）与 1.4.x（对象）两种返回。"""
        with self._lock:
            out = self._engine(band)
        res = out[0] if isinstance(out, tuple) and len(out) == 2 else out
        if res is None:
            return []
        if hasattr(res, "txts"):
            return [(str(t), float(s)) for t, s in zip(res.txts, res.scores)]
        return [(str(item[1]), float(item[2])) for item in res]


def create_band_ocr(threads: int | None = None) -> BandOcrEngine | None:
    """建字幕带 OCR 引擎；包没装/模型坏/会话起不来都回 None（调用方静默停采样）。

    采样是后台增强能力，绝不毁主链路（与 kernel.create_rapid_engine 同一纪律）。
    """
    try:
        return BandOcrEngine(threads)
    except Exception as exc:  # noqa: BLE001
        log.warning("字幕带 OCR 不可用（%s: %s）", type(exc).__name__, exc)
        return None


# --- 定区三档（§C11）：持久化 + 签名自检 + 手动重拖入口 ---------------------------


def region_key(width: int, height: int) -> str:
    """config.ini 里按分辨率限定的区域键（同分辨率布局基本不变，§C11）。"""
    return f"{REGION_KEY_PREFIX}_{int(width)}x{int(height)}"


def parse_region(value: Any) -> tuple[int, int, int, int] | None:
    """`"x,y,w,h"` → 四个正整数；任何不规范输入回 None（绝不猜）。"""
    if not isinstance(value, str):
        return None
    parts = value.split(",")
    if len(parts) != 4:
        return None
    try:
        x, y, w, h = (int(p.strip()) for p in parts)
    except ValueError:
        return None
    if min(x, y, w, h) < 1:
        return None
    return x, y, w, h


def load_last_region(
    config: Mapping[str, str], width: int, height: int
) -> tuple[int, int, int, int] | None:
    """第一档：读上次区域（本分辨率没有持久化 → None，落到第二/三档）。"""
    return parse_region(config.get(region_key(width, height), ""))


def pad_region(
    region: Sequence[int],
    screen_w: int,
    screen_h: int,
    ratio: float = AUTO_BAND_PAD_RATIO,
) -> tuple[int, int, int, int]:
    """字幕带矩形纵向扩一点（只扩上下，不扩左右）并夹进屏幕（纯函数）。

    `ratio` 夹到 0–0.5；0 = 原样。读取时扩、不写回 config —— 否则每次启动都会
    在旧值上再叠一层，最终把整个画面都圈进来。
    """
    x, y, w, h = (int(v) for v in region)
    try:
        r = float(ratio)
    except (TypeError, ValueError):
        r = 0.0
    r = max(0.0, min(0.5, r))
    pad = round(h * r)
    top = pad // 2
    y0 = max(0, y - top)
    y1 = min(int(screen_h), y + h + pad - top)
    x0 = max(0, min(x, int(screen_w) - 1))
    x1 = max(x0 + 1, min(int(screen_w), x + w))
    return (x0, y0, x1 - x0, max(1, y1 - y0))


def save_last_region(
    config_path: Path, width: int, height: int, region: Sequence[int]
) -> tuple[int, int, int, int]:
    """把区域持久化到 config.ini（settings.apply_config 的行级原子写）。

    返回规范化后的区域。config.ini 是本机运行状态文件，改动不提交（仓库纪律）。
    """
    x, y, w, h = (int(v) for v in region)
    if min(x, y, w, h) < 1:
        raise ValueError(f"region 必须是四个正整数，收到 {region!r}")
    normalized = (x, y, w, h)
    settings.apply_config(
        Path(config_path), {region_key(width, height): f"{x},{y},{w},{h}"}
    )
    return normalized


# --- 区域校正（2026-11-11）：视频内容框 → 字幕带 ----------------------------------
#
# 绝对屏幕坐标的失效根因：B站 播放器会随页面滚动/影院模式/窗口位置在屏幕上移动，
# 而 `ocr_band_region_<W>x<H>` 只按分辨率存。扩展上报 `videoRect`（视频**内容**
# 区域的物理像素矩形）后，用户拖过的区域改存相对比例，换布局时按新视频框投影；
# 没拖过则用视频框底部 18% 的默认带。

DEFAULT_BAND_TOP_RATIO = 0.75
DEFAULT_BAND_BOTTOM_RATIO = 0.93
DEFAULT_BAND_LEFT_RATIO = 0.04
DEFAULT_BAND_RIGHT_RATIO = 0.96


def relative_region_key(width: int, height: int) -> str:
    return f"{REL_REGION_KEY_PREFIX}_{int(width)}x{int(height)}"


def parse_relative_region(value: Any) -> tuple[float, float, float, float] | None:
    """`"l,t,r,b"`（0–1、l<r、t<b）→ 四元组；任何不规范输入回 None。"""
    if not isinstance(value, str):
        return None
    parts = value.split(",")
    if len(parts) != 4:
        return None
    try:
        left, top, right, bottom = (float(p.strip()) for p in parts)
    except ValueError:
        return None
    values = (left, top, right, bottom)
    if not all(math.isfinite(v) for v in values):
        return None
    if not (0.0 <= left < right <= 1.0 and 0.0 <= top < bottom <= 1.0):
        return None
    return left, top, right, bottom


def load_last_relative(
    config: Mapping[str, str], width: int, height: int
) -> tuple[float, float, float, float] | None:
    return parse_relative_region(config.get(relative_region_key(width, height), ""))


def save_last_relative(
    config_path: Path,
    width: int,
    height: int,
    rel: Sequence[float],
) -> tuple[float, float, float, float]:
    left, top, right, bottom = (float(v) for v in rel)
    settings.apply_config(
        Path(config_path),
        {relative_region_key(width, height): f"{left:.6f},{top:.6f},{right:.6f},{bottom:.6f}"},
    )
    return left, top, right, bottom


def video_rect_from_context(ctx: Any) -> tuple[int, int, int, int] | None:
    """扩展上报的 `videoRect` → `(x, y, w, h)`（物理像素）；缺/坏回 None。"""
    if not isinstance(ctx, Mapping):
        return None
    rect = ctx.get("videoRect")
    if not isinstance(rect, Mapping):
        return None
    try:
        x, y, w, h = (int(rect[k]) for k in ("x", "y", "w", "h"))
    except (KeyError, TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    return x, y, w, h


def auto_band_from_video(
    video_rect: Sequence[int],
    *,
    top: float = DEFAULT_BAND_TOP_RATIO,
    bottom: float = DEFAULT_BAND_BOTTOM_RATIO,
    left: float = DEFAULT_BAND_LEFT_RATIO,
    right: float = DEFAULT_BAND_RIGHT_RATIO,
) -> tuple[int, int, int, int]:
    """视频内容框底部默认字幕带（纯函数）。比例非法时夹到 0–1。"""
    x, y, w, h = (int(v) for v in video_rect)
    l = max(0.0, min(1.0, float(left)))
    t = max(0.0, min(1.0, float(top)))
    r = max(l + 0.01, min(1.0, float(right)))
    b = max(t + 0.01, min(1.0, float(bottom)))
    x0 = x + round(w * l)
    y0 = y + round(h * t)
    x1 = x + round(w * r)
    y1 = y + round(h * b)
    return (x0, y0, max(1, x1 - x0), max(1, y1 - y0))


def region_from_relative(
    rel: Sequence[float], video_rect: Sequence[int]
) -> tuple[int, int, int, int]:
    """相对比例 + 当前视频框 → 屏幕绝对区域（纯函数）。"""
    left, top, right, bottom = (float(v) for v in rel)
    x, y, w, h = (int(v) for v in video_rect)
    x0 = x + round(w * left)
    y0 = y + round(h * top)
    x1 = x + round(w * right)
    y1 = y + round(h * bottom)
    return (x0, y0, max(1, x1 - x0), max(1, y1 - y0))


def region_to_relative(
    region: Sequence[int], video_rect: Sequence[int]
) -> tuple[float, float, float, float] | None:
    """屏幕绝对区域 → 相对视频框比例（纯函数；框太小时回 None）。"""
    x, y, w, h = (int(v) for v in region)
    vx, vy, vw, vh = (int(v) for v in video_rect)
    if vw <= 0 or vh <= 0:
        return None
    left = max(0.0, min(1.0, (x - vx) / vw))
    top = max(0.0, min(1.0, (y - vy) / vh))
    right = max(0.0, min(1.0, (x + w - vx) / vw))
    bottom = max(0.0, min(1.0, (y + h - vy) / vh))
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


# --- 组合层：采样器 ---------------------------------------------------------------


@dataclass
class FeedReport:
    """feed() 的逐帧账目（T5 的占空比/自检状态从这里读）。"""

    si: int
    sig_ok: bool  # 有字行签名本帧是否通过
    changed: bool  # 差分门判定
    ocr_called: bool
    lines: int  # 进入投票流的行数


class SubtitleBandSampler:
    """字幕带采样器：feed() 逐帧进，flush() 把攒住的句子落库。

    线程模型：hub 侧单线程顺序调用 feed/flush（与 T5 编排循环同一线程）；
    OCR 引擎内部有自己的锁，跨线程用引擎层自己兜。

    注入点（测试全部离线的根）：
    - `ocr(band) -> [(text, conf)]`：字幕带识别（生产给 BandOcrEngine 实例）；
    - `mask_of(band) -> bytes`：文字掩码（差分门 + 签名共用）；
    - `energy_of(band) -> float`：清晰度（底图择帧）；
    - `save_image(band, meta) -> 路径或 ""`：底图落盘；
    - `wordlist`：噪声判定与可信门共用（缺省 kernel.load_wordlist() 的常见词表，
      与既有可信门同一口径）；
    - `known_words`（#222）：可疑门用的宽词表（缺省 kernel.correction_wordlists()
      的 known，含 ECDICT 时 10w+）；空表时回落旧的命中率比值门；
    - `space_fixer(text) -> text`（#221）：字幕 OCR 丢空格的粘连修复；缺省不修。
    """

    def __init__(
        self,
        store: Store,
        session_id: int,
        *,
        ocr: OcrFn,
        wordlist: frozenset[str] | None = None,
        known_words: frozenset[str] | None = None,
        mask_of: MaskOf | None = None,
        energy_of: EnergyOf | None = None,
        save_image: SaveImage | None = None,
        image_dir: Path | None = None,
        config_path: Path | None = None,
        ratio_thr: float = VOTE_RATIO_THR,
        gap_s: float = VOTE_GAP_S,
        sig_fail_limit: int = SIG_FAIL_LIMIT,
        space_fixer: Callable[[str], str] | None = None,
    ) -> None:
        self.store = store
        self.session_id = int(session_id)
        self._ocr = ocr
        self._mask_of = mask_of or _default_mask_of
        self._energy_of = energy_of or _default_energy_of
        self._save_image = save_image or self._default_save_image
        self._image_dir = Path(image_dir) if image_dir else DEFAULT_IMAGE_DIR
        self._config_path = Path(config_path) if config_path else TOOL_DIR / "config.ini"
        self._wordlist = wordlist
        self._known_words = known_words
        self._space_fixer = space_fixer
        self._ratio_thr = ratio_thr
        self._gap_s = gap_s
        self._sig_fail_limit = max(1, int(sig_fail_limit))
        self._vote = _VoteState(ratio_thr=ratio_thr, gap_s=gap_s)
        self._prev_mask: bytes | None = None
        self._si = 0
        self._sig_fail = 0
        self.needs_redrag = False  # 第二档升级信号（§C11：连续失检才请用户重拖）
        self.fed = 0
        self.ocr_calls = 0
        self.skipped = 0  # 差分门跳过的帧数（占空比账目）

    # -- 逐帧 -----------------------------------------------------------------

    def feed(self, band: Band, t: float) -> FeedReport:
        """喂一帧字幕带（T5 按节拍 ~2s 调）。差分门判同 → 零 OCR 调用。"""
        self._si += 1
        self.fed += 1
        mask = self._mask_of(band)
        sig_ok = self._signature_ok(mask)
        if sig_ok:
            self._sig_fail = 0
            if self.needs_redrag:
                # 粘滞 redrag 修复（验收低危#2）：签名恢复出字 = 区域其实在正常
                # 工作——多半是「长无字幕段」的误报（SIG_FAIL_LIMIT 最简版的已知
                # 代价）。自动撤回，status 应回 active；之后再度连续失检仍会重新
                # 置位（撤回不是一次性熔断）。set_region（真重拖）也清同一标志。
                self.needs_redrag = False
                log.info("字幕带签名恢复出字，撤回重拖请求（会话 %d）", self.session_id)
        else:
            self._sig_fail += 1
            if self._sig_fail >= self._sig_fail_limit and not self.needs_redrag:
                self.needs_redrag = True
                log.warning(
                    "字幕带区域连续 %d 帧没有文字签名，请重拖（会话 %d）",
                    self._sig_fail,
                    self.session_id,
                )
        changed = self._prev_mask is None or band_masks_differ(self._prev_mask, mask)
        self._prev_mask = mask
        if not changed:
            self.skipped += 1
            return FeedReport(self._si, sig_ok, False, False, 0)
        lines = self._ocr(band)
        self.ocr_calls += 1
        clarity = self._energy_of(band)
        kept = 0
        for text, conf in lines:
            text = one_line(text)
            if not latin_dominant(text):
                continue  # 中文翻译行 / 纯符号行不进投票流（§C13a ② 的更早一道）
            self._vote.add(self._si, float(t), text, float(conf), band=band, clarity=clarity)
            kept += 1
        return FeedReport(self._si, sig_ok, True, True, kept)

    def _signature_ok(self, mask: bytes) -> bool:
        ink = mask_ink_ratio(mask)
        return SIG_INK_MIN <= ink <= SIG_INK_MAX

    # -- 落库 -----------------------------------------------------------------

    def flush(self) -> dict[str, Any]:
        """把攒住的投票组出句 → 剔噪 → 可信门打标 → 底图落盘 → 待选句落库。

        T5 可周期调用（如每 60s）或停播时调用；幂等（清空组流）。返回 store 的
        计数（`added`/`duplicates`/`total`，`invalid` 是 store 对坏行的口径）外加
        `noisy`（剔掉的噪声组数）、`non_latin`（非拉丁组数）与 `groups`（投票组数）。
        """
        groups = self._vote.groups
        voted = self._vote.finalize()
        self._vote = _VoteState(ratio_thr=self._ratio_thr, gap_s=self._gap_s)
        rows: list[dict[str, Any]] = []
        noisy = 0
        non_latin = 0
        wordlist = self._get_wordlist()
        known = self._get_known_words()
        for line in voted:
            # #222：检测头开启后先清词内 `/` 与伪撇号；#221 的粘连修复排在其后。
            text = corrections.clean_ocr_punctuation(line.text)
            if self._space_fixer:
                text = self._space_fixer(text)
            if is_noise_line(text, wordlist):
                noisy += 1
                continue  # 乱码行不落库（§C13a ①：必须在对齐之前剔掉）
            if not is_latin_line(text):
                non_latin += 1
                continue  # store 的最后一道（O1 契约）；签名档漏过的 CJK 行在这拦住
            image_path = self._save_group_image(line)
            rows.append(
                {
                    "text": text,
                    "source": SOURCE,
                    "from_s": line.t_first,
                    "to_s": line.t_last,
                    "score": line.mean_conf,
                    "suspicious": self._is_suspicious(text, wordlist, known),
                    "image_path": image_path or None,
                }
            )
        result = (
            self.store.add_subtitle_lines(self.session_id, rows)
            if rows
            else {
                "added": 0,
                "duplicates": 0,
                "invalid": 0,
                "total": self.store.subtitle_pending_count(self.session_id),
            }
        )
        if rows:
            log.info(
                "字幕带采样落库（组 %d 新增 %d 剔噪 %d 非拉丁 %d）",
                len(groups),
                result["added"],
                noisy,
                non_latin,
            )
        return {**result, "noisy": noisy, "non_latin": non_latin, "groups": len(groups)}

    def _save_group_image(self, line: VotedLine) -> str:
        """底图落盘（注入的 saver 也兜底）：存不下只记日志，句照收（无图可追）。"""
        try:
            return self._save_image(line.band, {"si": line.first_si, "t": line.t_first})
        except Exception as exc:  # noqa: BLE001
            log.warning("字幕带底图落盘失败（%s: %s），本句无底图", type(exc).__name__, exc)
            return ""

    def _get_wordlist(self) -> frozenset[str]:
        if self._wordlist is None:
            self._wordlist = kernel.load_wordlist()
        return self._wordlist

    def _get_known_words(self) -> frozenset[str]:
        """可疑门用的宽词表（#222）：ECDICT 在位时 10w+，读不到就退旧比值门。"""
        if self._known_words is None:
            try:
                known, _common = kernel.correction_wordlists()
            except Exception:  # noqa: BLE001 - 词表只影响打标，不该拦采样
                known = frozenset()
            self._known_words = known
        return self._known_words

    def _is_suspicious(
        self, text: str, wordlist: frozenset[str], known: frozenset[str]
    ) -> bool:
        if known:
            return corrections.looks_suspicious(text, known)
        return kernel.text_score(text, wordlist)[0] < _SCORE_TRUST

    def _default_save_image(self, band: Band, meta: Mapping[str, Any]) -> str:
        """默认底图落盘：`data/subtitle_bands/<会话>_<si>.png`（TTL 清理归 T7）。

        落盘失败由 `_save_group_image` 的兜底接住（句照收，无图可追）。
        """
        image = _to_pil(band)
        self._image_dir.mkdir(parents=True, exist_ok=True)
        path = self._image_dir / f"s{self.session_id}_f{int(meta.get('si') or 0)}.png"
        image.save(path)
        return str(path)

    # -- 第三档：手动重拖入口（热键接线归 T5）---------------------------------

    def set_region(self, region: Sequence[int], *, screen_w: int, screen_h: int) -> tuple[int, int, int, int]:
        """用户重拖后把新区域入账：持久化到 config.ini + 重置签名失检状态。

        第一档（复用上次区域）由 T5 在会话启动时用 `load_last_region()` 读回；
        这里是第三档的函数入口（spec §C11 热键钩子，T5 接线）。
        """
        normalized = save_last_region(self._config_path, screen_w, screen_h, region)
        self._sig_fail = 0
        self.needs_redrag = False
        self._prev_mask = None  # 区域换了，旧掩码不能拿来比差分
        log.info("字幕带区域已重拖并持久化（%dx%d）", screen_w, screen_h)
        return normalized


# --- worker 子进程（v33 P0，票 #211 首发崩溃修复）---------------------------------
#
# **为什么必须子进程**：hub 进程启动即预热 winrt（SMTC 媒体联动）+ comtypes
# （UIA 探针）；同一进程再初始化 onnxruntime（RapidOCR）会**原生段错误**——
# T4 施工时 pytest exit 139 在案（当时真引擎冒烟因此改子进程跑），v33 一期
# 真机首发在 hub 里复现（exit 2816，无 Python 堆栈，日志止于「转采样」）。
# 子进程是干净 python：只 import ocr_sampler 链（kernel/settings/store，实测
# sys.modules 零 winrt/winsdk/comtypes），行协议与父端通信。
#
# **协议**（一行 JSON 头 + 定长像素负载，回包一行 JSON）：
#   父→子：`{"n":<负载字节>,"w":<宽>,"h":<高>,"c":<通道>}` + RGB 原始字节；
#          探测帧 `{"cmd":"probe"}`（回子进程 sys.modules 里的被禁模块，自证干净）。
#   子→父：`{"ok":true,"lines":[[text,conf],...]}` /
#          `{"ok":false,"error":..}`（单图失败，进程活着）/
#          `{"ok":false,"fatal":true,...}`（引擎建不起来，进程退出）。
#
# `OcrWorkerProxy` 与 `BandOcrEngine` 接口一致（`proxy(band) -> [(text, conf)]`）
# —— `SubtitleBandSampler` 与调度器其余部分零改动。代理构造零开销：子进程
# **首次调用才 spawn**（引擎加载 ~1s 可接受）；意外死亡下次调用自动重生；
# 连续 spawn/首响应失败达上限判「环境不可用」，不再逐帧抖进程；`close()` 幂等
# （hub shutdown 链路调用）。kernel/引擎参数/T2 算法层零改动。

WORKER_MODULE = "ocr_sampler_worker"
DEFAULT_WORKER_TIMEOUT_S = 60.0  # 首次请求含模型加载（~1-5s）；OCR 稳态 ~0.7s
WORKER_MAX_SPAWN_FAILURES = 3  # 连续 spawn/首响应失败 → 环境不可用，不再抖


class OcrWorkerError(RuntimeError):
    """worker 往返失败（死亡/超时/单图错误）。调度器按「本帧跳过」降级。"""


class OcrWorkerFatal(OcrWorkerError):
    """worker 报 fatal（引擎建不起来）：进程已退，父端计数封顶。"""


class OcrWorkerUnavailable(OcrWorkerError):
    """连续 spawn 失败达上限：不再尝试（重装依赖/重启 hub 才能恢复）。"""


def default_worker_command() -> list[str]:
    """worker 命令行：干净 python 直接跑 worker 脚本（脚本目录自动进 sys.path）。"""
    return [sys.executable, "-X", "utf8", str(TOOL_DIR / f"{WORKER_MODULE}.py")]


def _spawn_worker(command: list[str]) -> Any:
    """默认进程工厂：二进制管道 + 继承 stderr（引擎装载日志照常可见）。"""
    return subprocess.Popen(  # noqa: S603 - 命令是自家 worker 脚本，非外部输入
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        cwd=str(TOOL_DIR),
    )


def encode_request(band: Band) -> tuple[bytes, bytes]:
    """band → (请求头行, 像素负载)：RGB 原始字节 + 定长声明（纯函数）。"""
    image = _to_pil(band).convert("RGB")
    width, height = image.size
    payload = image.tobytes()
    header = json.dumps(
        {"n": len(payload), "w": width, "h": height, "c": 3}, ensure_ascii=True
    ).encode("ascii")
    return header + b"\n", payload


def decode_response(line: bytes) -> list[tuple[str, float]]:
    """worker 的一行 JSON 回包 → [(text, conf)]；错误形态抛对应异常（纯函数）。"""
    try:
        reply = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise OcrWorkerError(f"bad reply: {type(exc).__name__}") from None
    if not isinstance(reply, Mapping) or not reply.get("ok"):
        error = "unknown error"
        if isinstance(reply, Mapping):
            error = str(reply.get("error") or "unknown error")
            if reply.get("fatal"):
                raise OcrWorkerFatal(error)
        raise OcrWorkerError(error)
    lines: list[tuple[str, float]] = []
    for row in reply.get("lines") or []:
        if not isinstance(row, (list, tuple)) or len(row) != 2:
            raise OcrWorkerError("bad line shape")
        lines.append((str(row[0]), float(row[1])))
    return lines


class OcrWorkerProxy:
    """`BandOcrEngine` 的子进程代理（接口一致，可整把替换进任何注入点）。

    线程模型：`__call__` 全程持锁串行（调度 tick 线程与 reocr 的 HTTP 线程共用
    同一个代理 = 同一个子进程，排队即可）。读回包走 reader 线程 + 队列，超时
    由调用方线程控（Windows 管道读没有便捷的超时读法）。
    """

    def __init__(
        self,
        *,
        command: Sequence[str] | None = None,
        timeout_s: float = DEFAULT_WORKER_TIMEOUT_S,
        process_factory: Callable[[list[str]], Any] | None = None,
    ) -> None:
        self._command = list(command) if command is not None else default_worker_command()
        self._timeout_s = max(0.05, float(timeout_s))
        self._factory = process_factory or _spawn_worker
        self._lock = threading.Lock()
        self._proc: Any | None = None
        self._replies: queue.Queue | None = None
        self._failures = 0  # 连续 spawn/首响应失败（成功清零；达上限判不可用）

    # -- 引擎接口 ----------------------------------------------------------------

    def __call__(self, band: Band) -> list[tuple[str, float]]:
        """band → [(text, conf)]。传输层故障杀进程下轮重生；fatal 计数封顶。"""
        with self._lock:
            if self._failures >= WORKER_MAX_SPAWN_FAILURES:
                raise OcrWorkerUnavailable(
                    f"band ocr worker 连续失败 {self._failures} 次，判环境不可用"
                )
            try:
                lines = self._call_locked(band)
            except OcrWorkerFatal:
                self._kill_locked()  # 子进程报 fatal 即自退，清掉残骸（下次调用重生）
                self._failures += 1
                if self._failures >= WORKER_MAX_SPAWN_FAILURES:
                    log.warning(
                        "band ocr worker 连续 %d 次起不来（rapidocr 缺席/模型坏？），"
                        "停用采样与重识别",
                        self._failures,
                    )
                raise
            except OcrWorkerError:
                self._failures += 1
                raise
            self._failures = 0
            return lines

    def probe(self) -> dict[str, Any]:
        """健康检查 + 隔离自证：回 worker 进程的被禁模块清单（应为空）。"""
        with self._lock:
            if self._failures >= WORKER_MAX_SPAWN_FAILURES:
                raise OcrWorkerUnavailable("band ocr worker unavailable")
            reply_line = self._roundtrip_locked(b'{"cmd": "probe"}\n', b"")
            try:
                reply = json.loads(reply_line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise OcrWorkerError(f"bad probe reply: {type(exc).__name__}") from None
            if not isinstance(reply, Mapping) or not reply.get("ok"):
                raise OcrWorkerError("probe failed")
            probe = reply.get("probe")
            return probe if isinstance(probe, dict) else {}

    def close(self) -> None:
        """终止 worker（幂等）：优雅关 stdin → 等 2s → kill。hub shutdown 调用。"""
        with self._lock:
            proc = self._proc
            self._proc = None
            self._replies = None
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except Exception:  # noqa: BLE001 - 收尾路径不抛
            pass
        try:
            proc.wait(timeout=2)
            return
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.kill()
            proc.wait(timeout=2)
        except Exception:  # noqa: BLE001
            pass

    # -- 内部（持锁调用）----------------------------------------------------------

    def _call_locked(self, band: Band) -> list[tuple[str, float]]:
        header, payload = encode_request(band)
        reply_line = self._roundtrip_locked(header, payload)
        return decode_response(reply_line)

    def _roundtrip_locked(self, header: bytes, payload: bytes) -> bytes:
        """写请求 → 按超时等一行回包。任何传输故障：杀进程 + 抛 OcrWorkerError。"""
        proc = self._ensure_worker_locked()
        try:
            proc.stdin.write(header)
            proc.stdin.write(payload)
            proc.stdin.flush()
        except (OSError, ValueError) as exc:
            self._kill_locked()
            raise OcrWorkerError(f"worker pipe broken: {type(exc).__name__}") from None
        try:
            line = self._replies.get(timeout=self._timeout_s)
        except queue.Empty:
            self._kill_locked()
            raise OcrWorkerError(f"worker timeout after {self._timeout_s:.1f}s") from None
        if line is None:  # reader 线程的 EOF 标记 = 子进程死了
            self._kill_locked()
            raise OcrWorkerError("worker died")
        return line

    def _ensure_worker_locked(self) -> Any:
        if self._proc is not None and self._proc.poll() is None:
            return self._proc
        self._kill_locked()
        proc = self._factory(self._command)
        if not hasattr(proc, "stdout") or proc.stdout is None:
            raise OcrWorkerError("process factory returned no pipes")
        self._proc = proc
        self._replies = queue.Queue()
        reader = threading.Thread(
            target=self._reader_loop, args=(proc,), name="ocr-worker-reader", daemon=True
        )
        reader.start()
        return proc

    def _reader_loop(self, proc: Any) -> None:
        """子进程 stdout → 队列（一行一 JSON）；EOF 压 None 标记死亡。"""
        replies = self._replies
        if replies is None:  # close 抢先清了场
            return
        try:
            for line in iter(proc.stdout.readline, b""):
                replies.put(line)
        except Exception:  # noqa: BLE001 - 管道坏 = 当 EOF
            pass
        finally:
            try:
                proc.stdout.close()
            except Exception:  # noqa: BLE001
                pass
            replies.put(None)

    def _kill_locked(self) -> None:
        proc = self._proc
        self._proc = None
        self._replies = None
        if proc is None:
            return
        try:
            proc.kill()
            proc.wait(timeout=2)
        except Exception:  # noqa: BLE001 - 清理路径不抛
            pass


def create_band_ocr_worker(**kwargs: Any) -> OcrWorkerProxy:
    """生产装配入口：建 worker 代理（构造零开销，子进程首次调用才 spawn）。

    与 `create_band_ocr`（**进程内**引擎，仅测试/冒烟用）的分工见本节头注释：
    hub 进程里绝不能走进程内引擎。
    """
    return OcrWorkerProxy(**kwargs)
