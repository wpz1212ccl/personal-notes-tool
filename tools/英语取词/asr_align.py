"""ASR 转写文本 → 待选句行（v36 ASR P2 最小切片，2026-11-11）。

云端 SenseVoice 只回全文、**没有逐句时间戳**（P1 研究 §5）。P2 的第一刀先把文本
切成句，再按时长按字符占比分配 `from_s/to_s`：挑词/复习只看文本，时间窗只用于
「▶ 回视频」的近似跳转；将来对齐精度不够时再上 `asr_align` 的对齐版。

纪律：
- 纯函数、零网络、零 store —— 单测直接喂字符串；
- 只回拉丁行（与待选句池 O1 契约一致，store 还会再拦一次）；
- 时间窗单调、不重叠、落在 [start, duration] 内；duration 未知时给一个可用的
  兜底跨度，不产生 NaN。
"""

from __future__ import annotations

import math
import re

# 句末标点后跟空白才切；「3.5」「e.g.」这类没有空白不会被切。
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
# 2026-11-11 真机：SenseVoice 的句号后**不跟空格**（`things.One.I`），上面那条
# 正则整段不切，8 分钟转写退化成 5 个 300–500 字的巨块。先给「标点 + 字母」
# 补一个空格，再走同一套切句；缩写/小数/首字母靠后面的合并规则救回。
_SPLIT_NO_SPACE = re.compile(r"([.!?])(?=[A-Za-z])")
# 转写里的表情符号（😊 等）不带词汇，去掉；它夹在句号后还会挡住切句。
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️]+")
# 2026-11-11 真机：SenseVoice 会把口头填充/非语音识别成「嗯/呵/哈/あは」等 CJK
# 字符。`store.is_latin_line` 见一个 CJK 就拒绝整行 —— 旧实现因此把 7,590 字的
# 英文转写只落库了 1,588 字（含一个「嗯」的整段全丢）。切句后先剔掉 CJK/emoji，
# 再按拉丁占比过滤，保留夹在中间的正常英文。
_CJK_OR_EMOJI = re.compile(
    "[　-〿぀-ヿ㐀-䶿一-鿿"
    "豈-﫿가-힯\U0001F000-\U0001FAFF☀-➿️]+"
)

# 口播里常有「一口气说 50 秒、SenseVoice 一个句号都不给」的长段（真机第一段
# 1,000 字/53 秒）。待选句一行太长没法逐句挑词，按逗号/空格再切成不超过这个
# 长度的块；时间仍按字符占比分配（本来就是近似值）。
MAX_SENTENCE_CHARS = 240
# 常见缩写：句号后虽然跟空白，但仍是同一句（不然「Mr. Smith」会被切成两句）。
_ABBREVIATIONS = frozenset(
    {
        "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc",
        "e.g", "i.e", "a.m", "p.m", "u.s", "u.k",
    }
)


def split_sentences(text: object) -> list[str]:
    """转写全文 → 句子列表（纯函数）。空白归一；空文本回空列表。"""
    normalized = " ".join(str(text or "").split())
    if not normalized:
        return []
    # 中英混排（B站 AI 轨/双语转写常见）：CJK 句末标点先归一成 ASCII，
    # 否则「这是中文。 Bye.」会粘成一段、把后面的英文一起滤掉。
    normalized = (
        normalized.replace("。", ". ").replace("！", "! ").replace("？", "? ")
    )
    normalized = _EMOJI.sub(" ", normalized)
    normalized = _SPLIT_NO_SPACE.sub(r"\1 ", normalized)
    normalized = " ".join(normalized.split())
    parts = _SENTENCE_SPLIT.split(normalized)
    out: list[str] = []
    for part in parts:
        sentence = part.strip()
        if not sentence:
            continue
        if out and (
            _ends_with_abbreviation(out[-1])
            or _ends_with_initial(out[-1])
            or _starts_continuation(sentence)
        ):
            out[-1] = f"{out[-1]} {sentence}"
            continue
        out.append(sentence)
    return out


def _ends_with_abbreviation(sentence: str) -> bool:
    match = re.search(r"([A-Za-z.]+)\.$", sentence)
    return bool(match and match.group(1).lower() in _ABBREVIATIONS)


def _ends_with_initial(sentence: str) -> bool:
    """`U.` / `U. S.` 这类首字母缩写：补空格后会被切开，靠这条并回去。"""
    return bool(re.search(r"(?:^|\s)[A-Z]\.$", sentence))


def _starts_continuation(sentence: str) -> bool:
    """下一段以小写字母/数字开头 → 上一个句号多半是缩写或小数，不是句末。"""
    return bool(re.match(r"[a-z0-9]", sentence))


def chunk_long_sentence(
    sentence: str, limit: int = MAX_SENTENCE_CHARS
) -> list[str]:
    """超长句按逗号/分号/空格切成 ≤ limit 的块（纯函数，不丢词）。"""
    limit = max(40, int(limit))
    if len(sentence) <= limit:
        return [sentence]
    out: list[str] = []
    rest = sentence.strip()
    while len(rest) > limit:
        window = rest[:limit]
        cut = max(window.rfind(", "), window.rfind("; "), window.rfind(" "))
        if cut < limit // 3:
            cut = limit
        piece = rest[:cut].rstrip(" ,;")
        if piece:
            out.append(piece)
        rest = rest[cut:].lstrip(" ,;")
    if rest:
        out.append(rest)
    return out


def assign_times(
    sentences: list[str], duration: float, *, start: float = 0.0
) -> list[tuple[float, float]]:
    """按字符占比把句子铺到 [start, duration]（纯函数，单调不重叠）。"""
    if not sentences:
        return []
    total = sum(max(1, len(s)) for s in sentences)
    try:
        span = float(duration) - float(start)
    except (TypeError, ValueError):
        span = 0.0
    if not math.isfinite(span) or span <= 0:
        # 拿不到时长（直播/心跳缺字段）：按每句 3 秒给一个可用的近似窗。
        span = max(30.0, 3.0 * len(sentences))
        start = 0.0
    out: list[tuple[float, float]] = []
    cursor = float(start)
    for sentence in sentences:
        share = span * max(1, len(sentence)) / total
        end = min(float(start) + span, cursor + share)
        out.append((round(cursor, 2), round(max(cursor, end), 2)))
        cursor = end
    return out


def transcript_rows(
    text: object,
    duration: float,
    *,
    start: float = 0.0,
    score: float = 0.8,
    min_latin_ratio: float = 0.6,
) -> list[dict[str, object]]:
    """转写全文 → `store.add_subtitle_lines` 可直接吃的行（纯函数）。

    `source='asr'`；`score` 是云端无置信度时的固定分（同 rank 择优用）。
    只保留拉丁字符占比达到 `min_latin_ratio` 的句子（中文行/纯符号行不进池）。
    """
    # 先剔 CJK/emoji 再切句：`relaxed.嗯.` 会变成 `relaxed. .`，句号重新成为
    # 可切边界；反过来（先切后剔）会把 `嗯.` 粘回上一句，留下 `it. .` 的脏句。
    # 中文句末标点要先归一成 ASCII —— 下面的 CJK 清洗会把它们一起吃掉。
    pre = (
        str(text or "")
        .replace("。", ". ")
        .replace("！", "! ")
        .replace("？", "? ")
    )
    cleaned_text = _CJK_OR_EMOJI.sub(" ", pre)
    filtered = [
        s for s in split_sentences(cleaned_text) if _latin_ratio(s) >= min_latin_ratio
    ]
    sentences = [piece for s in filtered for piece in chunk_long_sentence(s)]
    times = assign_times(sentences, duration, start=start)
    rows: list[dict[str, object]] = []
    for sentence, (from_s, to_s) in zip(sentences, times):
        rows.append(
            {
                "text": sentence,
                "source": "asr",
                "from_s": from_s,
                "to_s": to_s,
                "score": float(score),
            }
        )
    return rows


def _latin_ratio(text: str) -> float:
    letters = sum(1 for ch in text if ch.isalpha())
    if letters <= 0:
        return 0.0
    latin = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    return latin / letters
