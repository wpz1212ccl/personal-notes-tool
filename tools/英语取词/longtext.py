"""长文模式的文本切分（v21 规格 §4 / §5 + v22 分段规格 §7）——纯函数，零依赖、不落盘、不 logging。

职责：把一个「整篇文本」切成**自然段**，再切成句子，并给出可供定位的字符偏移与段落类型。
两个入口共用同一套分段规则（v22 §7.5：单一算法，入库与「重新分段」复用），
这样 OCR 路径与粘贴路径的段落语义不会漂。

- **粘贴路径**：`normalize_pasted(text)` → 归一化后的全文；
  `split_paragraphs_pasted(text)` → 段落列表（空行主判据 + PDF 形态的缩进/短尾行兜底
  + 单行正文的递增段落标号兜底，spec §7.4）。
- **OCR 路径**：`split_paragraphs_ocr(lines)` → 段落列表；`lines` 是行框 + 文本
  （`PageLine`），段落边界靠**行带聚类**判（`g0`/`h0` 双信号，spec §7.3），不靠空行。
- **全结果**：`segment_pasted` / `segment_ocr` 回 `Segmentation`（段落 + `suspicious` 自检 +
  实际采用的判据），`Passage.kind ∈ body|heading|list|quote`（spec §7.2）。

日志纪律（store.py 的 §10）：本模块不算日志、不碰数据库；只有纯字符串与几何运算。
v22 分段的语料/门槛/旧算法红灯数字见 `docs/research/v22-paragraph-segmentation/README.md`；
v21 的实测依据见 `docs/research/2026-10-26-long-article-ocr-page-pass.md`。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

# --- 常量（v22 分段规格 §7.5：阈值集中在这里，OCR 与粘贴共用同一套）---------

# OCR 段距：`gap > max(0.55*h0, 1.8*g0)` 判段边界。
# `g0` = 同列相邻行 gap 的**下四分位**（对段落大间距鲁棒），`h0` = 行高中位数。
# 旧判据是 `gap > 1.25*h0`（硬几何）：h=17px、行距 39px 时 gap 22px > 21.25px，
# 每行都被切成一段 —— 这是 v22 要修的已知硬伤（spec §7.3）。
PARA_GAP_RATIO = 1.8
PARA_GAP_HEIGHT_RATIO = 0.55
PARA_GAP_QUANTILE = 0.25
# 估正常行距时只把 ≤ 2×行高的 gap 当候选：更大的 gap 多半是段距/大块空白，
# 拿它当「正常行距」会把阈值抬得过高（2 行的小页、带页边空白的切片都会踩）。
PARA_NORMAL_GAP_MAX_HEIGHT_RATIO = 2.0
# 正文左界 / 行缩进对齐的稳健估计：行左边界集合的**下四分位**（引用段整段缩进时
# 中位数会被拉偏，居中标题又会拉偏最小值；下四分位在「正文占多数」的页面上最稳）。
PARA_BODY_LEFT_QUANTILE = 0.25
# 首行缩进：新行比本段首行缩进 ≥ 2 个字宽；字宽 ≈ h0 * 0.5（无字体信息时的近似）。
PARA_INDENT_CHARS = 2
PARA_CHAR_WIDTH_RATIO = 0.5
# 行左边界「对齐」容差 = 字宽 × 该比例（OCR 行框抖动 1–3px 属正常），下限 0.5px
# 防字宽估成 0 时把浮点误差当成不对齐。
PARA_ALIGN_TOLERANCE_RATIO = 0.5
PARA_ALIGN_TOLERANCE_MIN_PX = 0.5
# 列表续行判定：比标记列深超过「字宽 × 该比例」才算缩进续行，否则视为列表项结束。
PARA_LIST_INDENT_SLACK_RATIO = 0.5
# 标题：短行 ≤ 8 词且无句末标点（spec §7.2 的启发式；无字体信息，误标不影响边界）。
PARA_HEADING_MAX_WORDS = 8
# 粘贴兜底（spec §7.4）：空行切出 1 段、且**非空行数** ≥ 6 时才尝试。
PARA_PASTE_FALLBACK_MIN_LINES = 6
# 单行超长粘贴的「可疑」门槛（票 #187 K8）：无换行、无标号的整篇一行 > 2000 字时，
# 走不到兜底的行数门槛（≥6 非空行），但要按 spec §7.4-3「诚实保留 1 段并标黄」处理。
# 2000 是票面建议值：正常一句/一段远够不到，带标号的整篇一行由标号兜底接管、不走这里。
PARA_PASTE_SINGLE_SUSPICIOUS_CHARS = 2000
# 短尾行规则：行宽 < 0.85 × 行宽中位数，且该行以 `.!?:;` 收尾。
PARA_SHORT_TAIL_RATIO = 0.85
# 自检（spec §7.3-6）：单行段占比 > 80% 且平均段长 < 40 字 → suspicious。
PARA_SUSPICIOUS_SINGLE_RATIO = 0.8
PARA_SUSPICIOUS_AVG_CHARS = 40

# 段内拼接：行尾的连字符形态（ASCII、U+2010 HYPHEN、U+2011 NON-BREAKING HYPHEN）。
# 与 kernel._LINE_HYPHENS 同一口径 —— 屏幕上断掉的复合词可能以任意一种出现。
LINE_HYPHENS = "-\u2010\u2011"

# 句子切分：句末标点后必须跟空白 + 大写/引号开头才断句。
_SENT_END = ".!?"
_SENT_TAIL = "\"'”’)"
_SENT_NEXT = "\"'“‘("

# 缩写表：这些词以句点结尾时**不**断句。18 条来自研究报告 §5.5 的实测用例
# （`Mr.`/`Dr.`/`$3.50`/`p.m.`/`Fig. 3`/`no. 4`/`vol. 2`/`Prof. Lee, Jr.` 全部覆盖）。
# `e.g` / `i.e` 这种带点的写法在比对时会被 rstrip(".") 归一成 `e.g` / `i.e`，所以表里
# 写不带点的形态即可；两种都列上是为了可读性。
ABBREVIATIONS = frozenset(
    {
        "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc",
        "e.g", "i.e", "fig", "no", "vol", "p", "pp", "al",
    }
)

# 编号列表标记：`1.` / `(a)` / `A)` / `•` 这类。切句前先把它们断开成独立单元，
# 否则 `Use 1. Open the file. 2. Edit it.` 会被切成 `['Use 1.', 'Open the file. 2.' ...]`。
_LIST_MARK = re.compile(
    r"(?<=[.!?:;])\s+(?=(?:\(?\d{1,2}[.)]|\([a-zA-Z]\)|[•·‣▪])\s)"
)


# 页码/页眉噪声：**必须整行匹配**，否则正文里的 `3.14`、`Fig. 3` 会被误删（实测）。
_PAGE_NOISE = re.compile(
    r"^\s*(?:page\s+\d+(?:\s*(?:of|/)\s*\d+)?|\d{1,4}|-\s*\d+\s*-)\s*$", re.I
)

# 段内硬折行：单个换行（连同两侧水平空白）→ 空格。
_SOFT_BREAK = re.compile(r"[ \t]*\n[ \t]*")

# 连字符重连（票 #187 K7）：**只在换行处**生效、**保留连字符** —— `man-\nagement` →
# `man-agement`（捕获两侧字词字符，连字符原样保回）。两条修订：
# - 旧写法 `\s+` 连**行内**的 `2010- 2015`（连字符 + 空格，如年份/化学名区间）也吃掉
#   （→ `20102015`），改限 `[ \t]*\n[ \t]*` 后行内形态一律不动；
# - 「去连字符」与 OCR 路径（`kernel._join_line_texts` / `assemble_lines`，#99 的 GROBID
#   判据）的「保留连字符」口径相反：纯文本分不清「排版断词的连字符」和「复合词自己的
#   连字符」（`state-of-the-\nart` 去了就把复合词读坏），统一选保留 —— 断词场景多出一个
#   连字符可读，复合词场景保住了原词。
_DEHYPHEN = re.compile(r"(\w)([-\u2010\u2011])[ \t]*\n[ \t]*(\w)")

# 连续水平空白折叠（不含换行——换行在分段阶段已经处理掉了）。
_SPACES = re.compile(r"[ \t\u00a0\u3000]{2,}")



# 句末标点（段边界判据）：允许后面跟引号/右括号，`said."` / `end.)` 都算收尾。
PARA_END = re.compile(r"[.!?:;][\"'”’)]*$")

# 列表标记（spec §7.1-4 / §7.2）：`• ● ○ ▪ - – * · 1. 2) (1) a) i.` 等。
# 行首必须有标记 + 空白，`-word` / `1.5` 不算（避免把普通正文切成列表段）。
# 括号形态只认**数字** `(1)`（spec §7.2）；`(A)` 这类括号单字母是**段落标号**（见
# PARA_LABEL），不是列表项 —— 旧写法把它们算列表标记，会让 IELTS 正文每个带标号的
# 段落被切成「标号行 + 其余行」两段（票 #173 真机复现：F–I 四段切成 8 段）。
PARA_LIST_MARKER = re.compile(
    r"^\s*(?:"
    r"[•●○◦▪‣·*]\s+"
    r"|[-–—]\s+"
    r"|\d{1,3}[.)]\s+"
    r"|\([0-9]{1,3}[.)]\s+"
    r"|[A-Za-z][.)]\s+"
    r"|[ivxlcdm]{1,4}[.)]\s+"
    r")",
    re.IGNORECASE,
)

# 段落标号（IELTS 机考平台正文形态）：段首以 `(A)` `(B)`… 标注，一个字一段。
# 整篇被平台复制成**一行**时（换行/空行全丢，实测 4651 字 0 换行），标号是唯一的段边界信号。
# 只认**递增连续**的标号序列：正文里零星引用一次 `(A)`（`option (A) is …`）不切段。
PARA_LABEL = re.compile(r"\(([A-Za-z])\)\s+")
# 段边界候选比 `PARA_LABEL` 更严：标号后必须紧跟大写/数字/引号开头的正文
# （真段落首句一定是大写开头；`(A) was correct` 这种正文引用不算）。
PARA_LABEL_START = re.compile(r"\(([A-Za-z])\)\s+(?=[A-Z0-9\"'“‘(])")
PARA_LABEL_MIN_COUNT = 2

# Markdown 前缀（粘贴路径 §7.2）：`#`～`###### ` 标题、`> ` 引用（可多级）。
_MARKDOWN_HEADING = re.compile(r"^\s*#{1,6}\s+\S")
_MARKDOWN_QUOTE = re.compile(r"^\s*>\s?")

# 粘贴兜底的行首缩进：≥2 个前导空格或全角空格（全角空格按 2 个字宽算）。
_LEADING_INDENT = re.compile(r"^[ \t\u3000]+")


# --- 数据结构 -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PageLine:
    """OCR 给出的一行：文本 + 行框几何（像素）。`top`/`bottom` 用于行距，
    `left` 用于首行缩进。坐标原点是**页内**（已含切片偏移，见 `merge_slices`）。
    """

    text: str
    top: float
    bottom: float
    left: float


@dataclass(frozen=True, slots=True)
class Passage:
    """一段正文：文本 + 在全文里的字符区间 `[start, end)`。

    `start`/`end` 是 **`join_passages` 单行化全文**的偏移（与 `documents.text` 同口径）：
    `normalize_pasted` 去掉页码/CRLF、`unwrap_paragraph` 去掉段内硬折行，再把各段文本用
    单个空格拼成全文；OCR 路径的段内拼接同理（`_join_lines` + 单空格）。
    `kind` 是段落类型（spec §7.2）：`body | heading | list | quote`；无法判定回落 `body`。
    """

    text: str
    start: int
    end: int
    kind: str = "body"

    def __len__(self) -> int:
        return len(self.text)


@dataclass(frozen=True, slots=True)
class Segmentation:
    """一次分段的完整结果：段落 + 自检标记 + 实际采用的判据（便于预览窗/测试说明）。"""

    passages: list[Passage]
    suspicious: bool = False
    strategy: str = "blank-lines"


# --- 粘贴路径 ----------------------------------------------------------------


def strip_page_noise(text: str) -> str:
    """逐行去掉纯页码/页眉行。整行匹配才删（实测：`3.14`、`Fig. 3` 必须保住）。"""
    lines = [line for line in text.split("\n") if not _PAGE_NOISE.match(line)]
    return "\n".join(lines)


def normalize_pasted(text: str) -> str:
    """粘贴文本的归一化：换行归一到 `\\n`、去页码行。**不**在这里做去硬折行（分段时才做）。

    保留空行——空行是分段信号，折叠掉就分不出段了。
    """
    if not text:
        return ""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return strip_page_noise(normalized)


def unwrap_paragraph(paragraph: str) -> str:
    """段内去硬折行 + 连字符重连 + 折叠空白。

    `man-\\nagement` → `man-agement`（PDF 复制的行尾断词；**保留**连字符，与 OCR 路径
    同口径，票 #187 K7）；`the\\nresearchers` → `the researchers`（普通硬折行）。
    """
    if not paragraph:
        return ""
    text = _DEHYPHEN.sub(r"\1\2\3", paragraph)
    text = _SOFT_BREAK.sub(" ", text)
    text = _SPACES.sub(" ", text)
    return text.strip()


def split_paragraphs_pasted(text: str) -> list[Passage]:
    """粘贴文本 → 段落列表（空行主判据 + PDF 形态 / 段落标号兜底，spec §7.4）。

    偏移是 **`join_passages` 单行化全文**的偏移（与 `documents.text` 同口径）：
    先 `normalize_pasted`（CRLF 归一 + 去页码行），再按空行切块、段内 `unwrap_paragraph`
    （去硬折行/连字符重连），最后 `_reindex` 把各段文本用单空格拼成全文并给出 `[start, end)`。
    OCR 路径的 `Passage` 是同一口径，两条路可以互换使用。

    判据顺序：
    1. 空行（主判据）；
    2. 块内**递增段落标号** `(A)` `(B)`…（机考平台把整篇复制成一行时唯一的段边界信号）；
    3. 空行切出 1 段且**非空行数** ≥ 6 时，依次试首行缩进、短尾行（两端对齐 PDF）；
    4. 都切不出来就**诚实保留 1 段**并在 `segment_pasted(...).suspicious` 里标黄（spec §7.4-3；
       无换行、无标号的**单行超长**粘贴走不到第 3 条的行数门槛，同样归这档，票 #187 K8）。
    """
    return segment_pasted(text).passages


def segment_pasted(text: str) -> Segmentation:
    """粘贴路径的分段全结果（段落 + 类型 + suspicious + 采用的判据）。"""
    normalized = normalize_pasted(text)
    if not normalized.strip():
        return Segmentation([], False, "empty")

    lines = normalized.split("\n")
    blocks = _blank_line_blocks(lines)
    strategy = "blank-lines"
    suspicious = False
    non_empty_lines = sum(1 for line in lines if line.strip())

    blocks, labelled = _split_blocks_by_inline_labels(blocks)
    if labelled:
        strategy = "inline-labels"
    elif len(blocks) == 1 and non_empty_lines >= PARA_PASTE_FALLBACK_MIN_LINES:
        for name, rule in (("indent", _indent_boundaries), ("short-tail", _short_tail_boundaries)):
            boundaries = rule(lines)
            candidate = _blocks_from_boundaries(lines, boundaries)
            if len(candidate) > 1:
                blocks, strategy = candidate, name
                break
        else:
            strategy, suspicious = "single", True
    elif (
        len(blocks) == 1
        and non_empty_lines == 1
        and len(normalized.strip()) > PARA_PASTE_SINGLE_SUSPICIOUS_CHARS
    ):
        # 票 #187 K8：无换行、无标号的**单行超长**粘贴 —— 兜底规则的行数门槛（≥6 非空行）
        # 到不了，但「整篇一行 + 没有任何段边界信号」正对应 spec §7.4-3 的意图：诚实保留
        # 1 段并标黄（预览窗提示可能没粘全/没分段），而不是静默当 1 段正常收录。
        strategy, suspicious = "single", True

    passages: list[Passage] = []
    for block in blocks:
        text_part = _unwrap_block(block)
        if text_part:
            passages.append(Passage(text_part, 0, len(text_part), _pasted_kind(block)))
    return Segmentation(_reindex(passages), suspicious, strategy)


def _blank_line_blocks(lines: Sequence[str]) -> list[list[str]]:
    """按空行（只含空白）切块；每块是原始行（保留行首缩进，供兜底规则判缩进）。"""
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if line.strip():
            current.append(line)
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    return blocks


def _unwrap_block(block: Sequence[str]) -> str:
    return unwrap_paragraph("\n".join(block))


def _display_width(text: str) -> int:
    """显示宽度：全角字符按 2 算（短尾行规则要量「行宽」而不是字符数）。"""
    return sum(2 if ord(char) > 0x2E7F else 1 for char in text)


def _leading_indent_width(line: str) -> int:
    """行首缩进的显示宽度（全角空格算 2 个字宽）。"""
    match = _LEADING_INDENT.match(line)
    return _display_width(match.group(0)) if match else 0


def _indent_boundaries(lines: Sequence[str]) -> list[int]:
    """首行缩进规则：行首缩进 ≥ 2 字宽，且上一行以句末标点收尾 → 该行另起一段。"""
    return [
        index
        for index in range(1, len(lines))
        if _leading_indent_width(lines[index]) >= PARA_INDENT_CHARS
        and PARA_END.search(lines[index - 1].strip())
    ]


def _short_tail_boundaries(lines: Sequence[str]) -> list[int]:
    """短尾行规则：行宽 < 0.85 × 行宽中位数，且该行以 `.!?:;` 收尾 → 段末、下一行另起。"""
    widths = [_display_width(line.strip()) for line in lines if line.strip()]
    median_width = _median(widths)
    if not median_width:
        return []
    return [
        index + 1
        for index, line in enumerate(lines[:-1])
        if _display_width(line.strip()) < PARA_SHORT_TAIL_RATIO * median_width
        and PARA_END.search(line.strip())
    ]


def _blocks_from_boundaries(lines: Sequence[str], boundaries: Sequence[int]) -> list[list[str]]:
    blocks: list[list[str]] = []
    start = 0
    for boundary in sorted(set(boundaries)):
        block = [line for line in lines[start:boundary] if line.strip()]
        if block:
            blocks.append(block)
        start = boundary
    tail = [line for line in lines[start:] if line.strip()]
    if tail:
        blocks.append(tail)
    return blocks


def _at_sentence_boundary(text: str, index: int) -> bool:
    """标号 `(` 之前是「文首 / 行首 / 句末标点 + 空白」——段首标号才会出现在这些位置。

    只往回看「空白 + 收尾引号/括号」，**不做前缀切片/正则**：旧写法对每个标号都
    `re.search(..., text[:index])`，512KB / 1 万个括号引用时是 O(n²)（review 实测 18s）。
    """
    cursor = index - 1
    while cursor >= 0 and text[cursor].isspace():
        if text[cursor] == "\n":
            return True  # 标号在该行行首（标题行之后、硬折行段落开头都算）
        cursor -= 1
    if cursor < 0:
        return True  # 标号前只有空白 = 文首
    if cursor == index - 1:
        return False  # `day.(B)`：标号前没有空白，不是段首
    while cursor >= 0 and text[cursor] in _SENT_TAIL:
        cursor -= 1
    return cursor >= 0 and text[cursor] in ".!?:;"


def _inline_label_segments(text: str) -> list[str]:
    """按**递增段落标号**把一段正文切成多段（`(A) … (B) …`）；认不出标号就原样回一段。

    标号是机考平台整篇复制（单行、无空行）时唯一的段边界信号。三条约束保证只在
    真标号正文上生效：① 至少 2 个标号；② 每个标号在文首/行首/句末标点 + 空白之后；
    ③ 标号字母逐个递增（`A→B→C…`，忽略大小写）—— 正文里零星引用 `option (A)`
    不会切段。
    """
    labels = [
        match
        for match in PARA_LABEL_START.finditer(text)
        if _at_sentence_boundary(text, match.start())
    ]
    if len(labels) < PARA_LABEL_MIN_COUNT:
        return [text]
    letters = [match.group(1).lower() for match in labels]
    if any(
        ord(current) != ord(previous) + 1
        for previous, current in zip(letters, letters[1:])
    ):
        return [text]
    segments: list[str] = []
    prefix = text[: labels[0].start()].strip()
    if prefix:  # 首个标号之前的引言/标题文字单独成段（不吞进第一段）
        segments.append(prefix)
    for index, match in enumerate(labels):
        end = labels[index + 1].start() if index + 1 < len(labels) else len(text)
        segment = text[match.start() : end].strip()
        if segment:
            segments.append(segment)
    return segments


def _split_blocks_by_inline_labels(
    blocks: Sequence[Sequence[str]],
) -> tuple[list[list[str]], bool]:
    """把每个块里能认出**递增段落标号**的再切开；返回 `(块列表, 是否切过)`。

    块内可能仍有硬折行（PDF 复制），这里只按标号切位置，段内折行留给
    `unwrap_paragraph` 处理。
    """
    out: list[list[str]] = []
    split = False
    for block in blocks:
        segments = _inline_label_segments("\n".join(block))
        if len(segments) > 1:
            out.extend([segment] for segment in segments)
            split = True
        else:
            out.append(list(block))
    return out, split


def _pasted_kind(block: Sequence[str]) -> str:
    """粘贴路径的段落类型（spec §7.2）：`#` 标题 / `>` 引用 / 列表标记 / 短行标题。"""
    first = block[0]
    if _MARKDOWN_QUOTE.match(first):
        return "quote"
    if PARA_LIST_MARKER.match(first):
        return "list"
    if _MARKDOWN_HEADING.match(first):
        return "heading"
    if PARA_LABEL.match(first):
        # 段落标号开头的段是正文（IELTS 正文形态）；否则短行会被「短行+无句末标点」误标标题。
        return "body"
    text = _unwrap_block(block)
    if (
        len(block) == 1
        and len(text.split()) <= PARA_HEADING_MAX_WORDS
        and not PARA_END.search(text)
    ):
        return "heading"
    return "body"


def _reindex(passages: list[Passage]) -> list[Passage]:
    """按段落顺序重算偏移（保留 `kind`）。

    段间用单个空格分隔（与 `records.context` 的单行语义一致），所以每段起点是
    上一段 `end + 1`。`join_passages()` 是这条规则的唯一实现者，两者必须一致。
    """
    out: list[Passage] = []
    cursor = 0
    for passage in passages:
        out.append(Passage(passage.text, cursor, cursor + len(passage.text), passage.kind))
        cursor += len(passage.text) + 1
    return out


def join_passages(passages: Sequence[Passage]) -> str:
    """段落列表 → 全文（段间单空格）。`Passage` 的偏移就按这个拼法定义。"""
    return " ".join(passage.text for passage in passages)


# --- OCR 路径 ----------------------------------------------------------------


def _median(values: Sequence[float]) -> float:
    """中位数（空序列回 0）。不引 statistics：这里只要个稳健的中心值。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _quantile(values: Sequence[float], quantile: float) -> float:
    """线性插值分位数（空序列回 0）。下四分位用来估「正常行距」，对段落大间距鲁棒。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = quantile * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _join_lines(lines: Sequence[PageLine]) -> str:
    """段内多行 → 一段文本。行尾连字符且下一行小写/大写/数字 → 去空格保留连字符。

    与 `kernel._join_line_texts` 的 ②③④ 规则同一口径（跨带拼接也是同一件事）。
    """
    parts = [line.text.strip() for line in lines if line.text.strip()]
    if not parts:
        return ""
    joined = parts[0]
    for previous, text in zip(parts, parts[1:]):
        head = text[0]
        if previous[-1] in LINE_HYPHENS and (
            head.islower() or head.isupper() or head.isdigit()
        ):
            joined += text
        else:
            joined += " " + text
    return joined


def _is_list_item(text: str) -> bool:
    return PARA_LIST_MARKER.match(text) is not None


@dataclass(frozen=True, slots=True)
class _OcrGeometry:
    """一页 OCR 的几何前置：`h0`、字宽、缩进阈值、正文左界（只算一次，两处共用）。"""

    h0: float
    char_width: float
    indent_limit: float
    body_left: float


def _ocr_geometry(rows: Sequence[PageLine]) -> _OcrGeometry:
    """行框 → 几何参数（spec §7.3-1：`h0` = 行高中位数；字宽 ≈ `h0*0.5`）。"""
    h0 = _median([line.bottom - line.top for line in rows])
    char_width = h0 * PARA_CHAR_WIDTH_RATIO
    return _OcrGeometry(
        h0=h0,
        char_width=char_width,
        indent_limit=PARA_INDENT_CHARS * char_width,
        body_left=_quantile([line.left for line in rows], PARA_BODY_LEFT_QUANTILE),
    )


def _aligned(lefts: Sequence[float], char_width: float) -> bool:
    """行左边界是否「对齐」：极差 ≤ 字宽 × 容差比例（下限 0.5px 防浮点误差）。"""
    return max(lefts) - min(lefts) <= max(
        PARA_ALIGN_TOLERANCE_RATIO * char_width, PARA_ALIGN_TOLERANCE_MIN_PX
    )


def _group_ocr_rows(rows: Sequence[PageLine]) -> list[list[PageLine]]:
    """v22 行带聚类（spec §7.3）：按几何把行分组成段。

    1. `h0` = 行高中位数；`g0` = 相邻行 gap 的**下四分位**（正常行距）；
    2. 断段：`gap > max(0.55*h0, 1.8*g0)`；
    3. 首行缩进：本行比**本段首行**缩进 ≥ 2 字宽（字宽 ≈ h0*0.5）且上一行句末标点；
    4. 列表：新行以列表标记开头 → 新段；列表段里缩进是续行，回退到标记列才结束；
    5. 引用：整段行缩进一致且比正文左界深，缩进回到正文层 → 引用段结束；
    6. 标题：短行无句末标点且前后段距都偏大 —— 由 2 的 gap 判据覆盖（标题行前后
       各断一次），类型另由 `_ocr_kind` 标 `heading`。
    """
    geometry = _ocr_geometry(rows)
    gaps = [
        max(0.0, current.top - previous.bottom)
        for previous, current in zip(rows, rows[1:])
    ]
    normal_gaps = [
        gap for gap in gaps if gap <= PARA_NORMAL_GAP_MAX_HEIGHT_RATIO * geometry.h0
    ]
    g0 = _quantile(normal_gaps, PARA_GAP_QUANTILE)
    gap_limit = max(PARA_GAP_HEIGHT_RATIO * geometry.h0, PARA_GAP_RATIO * g0)

    groups: list[list[PageLine]] = [[rows[0]]]
    for previous, current in zip(rows, rows[1:]):
        group = groups[-1]
        first = group[0]
        if _is_list_item(current.text):
            groups.append([current])
            continue
        if _is_list_item(first.text):
            # 列表段：比标记列更深的是续行；回退到标记列（或更左）才算列表项结束。
            if current.left <= first.left + PARA_LIST_INDENT_SLACK_RATIO * geometry.char_width:
                groups.append([current])
            else:
                group.append(current)
            continue
        gap = current.top - previous.bottom
        if gap > gap_limit:
            groups.append([current])
            continue
        if PARA_END.search(previous.text.strip()):
            if current.left - first.left >= geometry.indent_limit:
                groups.append([current])
                continue
            # 引用段结束：组内行缩进一致（≥2 行才算「引文」）、比正文左界深，本行回到
            # 正文层。单行缩进不参与这条 —— 它与「首行缩进的普通段落」局部不可分。
            if len(group) >= 2:
                lefts = [line.left for line in group]
                if (
                    _aligned(lefts, geometry.char_width)
                    and first.left - geometry.body_left > geometry.indent_limit
                    and current.left < first.left - geometry.indent_limit
                ):
                    groups.append([current])
                    continue
        group.append(current)
    return groups


def _ocr_kind(group: Sequence[PageLine], geometry: _OcrGeometry) -> str:
    """OCR 路径的段落类型（spec §7.2）：list / quote / heading / body。"""
    text = _join_lines(group)
    if _is_list_item(group[0].text):
        return "list"
    if PARA_LABEL.match(text):
        # 段落标号开头 = 正文（IELTS 来源形态）；否则单行短标号段会被误标 heading。
        return "body"
    lefts = [line.left for line in group]
    aligned = _aligned(lefts, geometry.char_width)
    indented = min(lefts) - geometry.body_left > geometry.indent_limit
    if aligned and indented:
        return "quote"
    if (
        len(group) == 1
        and len(text.split()) <= PARA_HEADING_MAX_WORDS
        and not PARA_END.search(text.strip())
    ):
        return "heading"
    return "body"


def _single_line_suspicious(groups: Sequence[Sequence[PageLine]], passages: Sequence[Passage]) -> bool:
    """自检（spec §7.3-6）：单行段占比 > 80% 且平均段长 < 40 字 → 可能仍在按行切。"""
    if not groups or not passages:
        return False
    single = sum(1 for group in groups if len(group) == 1)
    average = sum(len(passage.text) for passage in passages) / len(passages)
    return (
        single / len(groups) > PARA_SUSPICIOUS_SINGLE_RATIO
        and average < PARA_SUSPICIOUS_AVG_CHARS
    )


def group_lines_into_passages(lines: Sequence[PageLine]) -> list[list[PageLine]]:
    """按行框几何把行分组成段（v22 行带聚类，spec §7.3）。**不**改文本，只决定分组。"""
    rows = [line for line in lines if line.text.strip()]
    if not rows:
        return []
    return _group_ocr_rows(rows)


def split_paragraphs_ocr(lines: Sequence[PageLine]) -> list[Passage]:
    """OCR 行列表 → 段落列表（含合并后的全文偏移与段落类型）。

    段内拼接复用 `_join_lines`（行尾连字符重连）；段间用单空格，与
    `split_paragraphs_pasted` 的 `_reindex` 同一口径，所以两条路的 `Passage`
    可以互换使用。
    """
    return segment_ocr(lines).passages


def segment_ocr(lines: Sequence[PageLine]) -> Segmentation:
    """OCR 路径的分段全结果（段落 + 类型 + suspicious + 采用的判据）。"""
    rows = [line for line in lines if line.text.strip()]
    if not rows:
        return Segmentation([], False, "empty")

    geometry = _ocr_geometry(rows)
    groups = _group_ocr_rows(rows)
    passages: list[Passage] = []
    for group in groups:
        text_part = _join_lines(group)
        if text_part:
            passages.append(
                Passage(text_part, 0, len(text_part), _ocr_kind(group, geometry))
            )
    reindexed = _reindex(passages)
    suspicious = _single_line_suspicious(groups, reindexed)
    return Segmentation(reindexed, suspicious, "line-bands")


# --- 句子切分 ----------------------------------------------------------------


def _is_abbreviation(piece: str, next_word: str = "") -> bool:
    """`piece` 是否以某个缩写结尾，因而**不该**在此断句。

    两类：
    1. **明确缩写**（18 条表）：永远不断句 —— `Mr. Smith`、`Fig. 3`、`vol. 2`。
    2. **单字母点段**（`p.m.` / `U.S.` / `J.`）：形态本身有歧义，用上下文消解：
       - 前面紧跟**数字**时（`5 p.m.` / `9 a.m.`）→ 这是时刻，句末；
       - 后面跟的是**单个大写字母 + 点**（`J. K.`）或普通大写词 → 保守当缩写不断句。
       - 例外：`next_word` 是大写且后面还有独立句子时，仍按缩写处理（`U.S. Officials
         met.` 会把 `U.S.` 并进前句 —— 这是**已知可接受**的偏差，见报告 §6 的 F 系列）。
    """
    token = re.search(r"([A-Za-z.]+)$", piece)
    if not token:
        return False
    word = token.group(1).lower()
    if word.rstrip(".") in ABBREVIATIONS:
        return True

    if not re.fullmatch(r"(?:[a-z]\.)+", word):
        return False

    # 单字母点段：前面是数字 → 时刻（`5 p.m.`），断句。
    before = piece[: token.start()].rstrip()
    if before and before[-1].isdigit():
        return False
    return True


def split_sentences(text: str) -> list[str]:
    """英文句子切分。句末 `.?!` + 空白 + 大写/引号开头才断。

    三条保护：缩写表（含 `p.m.` 形态）、单个大写字母（`J. K. Rowling`）、
    编号列表标记（`1.` / `(a)` 不断句）。

    实测（研究报告 §5.5）：20 条人工标注用例全部正确；比 pysbd 快 19×、
    比 syntok 快 6.6×，且**准确率更高**（pysbd 18/20、syntok 18/20、spaCy 14/20）。
    """
    if not text:
        return []

    # 至少有 2 个标记才算「行内列表」。单个标记多半是 `Fig. 3)` / `no. 4` 这类
    # 「缩写 + 编号」，交给 _split_one 的缩写保护去处理。
    matches = list(_LIST_MARK.finditer(text))
    pieces = _LIST_MARK.split(text) if len(matches) >= 2 else [text]
    out: list[str] = []
    for piece in pieces:
        piece = piece.strip()
        if piece:
            out.extend(_split_one(piece))
    return out


def _split_one(text: str) -> list[str]:
    """单个非列表片段内部的切句（`split_sentences` 的实现主体）。"""
    sentences: list[str] = []
    buffer = ""
    index = 0
    length = len(text)

    while index < length:
        char = text[index]
        buffer += char
        if char in _SENT_END:
            cursor = index + 1
            # 句末引号/括号归前一句
            while cursor < length and text[cursor] in _SENT_TAIL:
                buffer += text[cursor]
                cursor += 1
            # 必须「有空白 + 下一句以大写或引号开头」才算句末
            next_word = cursor
            while next_word < length and text[next_word] == " ":
                next_word += 1
            # 判断顺序很重要（实测踩过两轮）：
            #   ① 缩写（`Fig.` / `Mr.` / `p.m.`）→ 永远不断句，不管后面是什么。
            #      这正是 `see Fig. 3) were clear` 不断句的原因：`3` 是编号不是新句。
            #   ② 当前 buffer 本身是列表项（`1.` / `(a)`）→ 不断句。
            #   ③ 下一词是大写字母或引号 → 断句。
            #   ④ 下一词是**列表编号**（`2.` / `(b)`，后跟空格 + 大写）→ 在此断句，
            #      让 `1. First here. 2. Second here.` 切成两个列表项。
            following = text[next_word] if 0 <= next_word < length else ""
            if _is_abbreviation(buffer, following) or _is_list_marker(buffer):
                boundary = False
            elif next_word < length and next_word > cursor:
                boundary = text[next_word].isupper() or text[next_word] in _SENT_NEXT
                if not boundary:
                    boundary = _starts_list_item(text, next_word)
            else:
                boundary = False
            if boundary:
                sentences.append(buffer.strip())
                buffer = ""
            index = cursor
            continue
        index += 1

    if buffer.strip():
        sentences.append(buffer.strip())
    return sentences


def _starts_list_item(text: str, index: int) -> bool:
    """`index` 起是否是行内列表项的开头：`2.` / `(b)` / `A)` 后跟空格 + 大写字母。

    `Fig. 3) were clear` 不会命中这里 —— 那个 `.` 已被 `_is_abbreviation` 拦在 ①，
    走不到 ④。
    """
    match = re.match(r"""\(?[A-Za-z0-9]{1,2}[.)]\s+(?=[A-Z"'“‘(])""", text[index:])
    return match is not None


def _is_list_marker(piece: str) -> bool:
    """`1.` / `(a)` / `A)` 这类编号本身就是列表项，不是句子结尾。"""
    return bool(re.fullmatch(r"\s*\(?[A-Za-z0-9]{1,2}[.)]", piece))


# --- 切片合并（>10000px 时用）--------------------------------------------------


def merge_slices(
    slices: Sequence[Sequence[PageLine]], offsets: Sequence[float]
) -> list[PageLine]:
    """多切片的行 → 一份整页行列表（按 y 去重）。

    `offsets[i]` 是第 i 片在**整页**里的纵向偏移（像素）。行框加上偏移还原成页坐标后，
    按行中线落在已有行的 `[top, bottom]` 区间内判为重复、丢弃。

    实测（研究报告 §5.3）：1000×14000 页面切 9000/7000/5000px 三种高度，
    去重后都**逐字收敛到同一份 3755 词**，与 9600px 整页一次完全相同，WER 0.0%。
    **拼行框而不是拼文本**是关键 —— 拼文本会在接缝处整段重复。
    """
    if len(slices) != len(offsets):
        raise ValueError("slices 与 offsets 长度必须一致")

    collected: list[PageLine] = []
    for rows, offset in zip(slices, offsets):
        for line in rows:
            if not line.text.strip():
                continue
            collected.append(
                PageLine(
                    text=line.text,
                    top=line.top + offset,
                    bottom=line.bottom + offset,
                    left=line.left,
                )
            )
    collected.sort(key=lambda line: (line.top, line.left))

    kept: list[PageLine] = []
    for line in collected:
        middle = (line.top + line.bottom) / 2.0
        duplicate = any(
            existing.top - 2.0 <= middle <= existing.bottom + 2.0
            # 只按 y 去重会把「恰好落在同一水平带的另一行」也吞掉（实测：不同内容的
            # 两行只要 y 重合，后者就消失了）。文本必须也算进去。
            # 允许首尾少量差异：接缝处的同一行两次识别可能差一两个字。
            and _same_line_text(existing.text, line.text)
            for existing in kept
        )
        if not duplicate:
            kept.append(line)
    return kept


def _same_line_text(left: str, right: str, threshold: float = 0.8) -> bool:
    """两行是否是「同一条线被识别了两次」。

    用词元集合的 Jaccard 相似度：接缝重叠区两次识别的错字不会超过一两成，
    而两条不同内容的行相似度通常远低于 0.3。阈值 0.8 取在两者中间，宁可保留
    重复行（用户能看出来）也不误删真内容。
    """
    a = set(re.findall(r"[A-Za-z0-9']+", left.lower()))
    b = set(re.findall(r"[A-Za-z0-9']+", right.lower()))
    if not a and not b:
        return True
    if not a or not b:
        return False
    return len(a & b) / len(a | b) >= threshold
