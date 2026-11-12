"""同句判定（v32，票 #199 / spec §Implementation Decisions 3–4）——纯函数、无状态、无 IO。

给词条落库入口（Store.capture）用：新捕获的原句若与「刚才那句 / 历史里已存在的某句」是
同一句，就把新词条的 context 统一成既有句子的 context，让展示层按 context 逐字全等并成
一张句卡（GLOSSARY.md「句卡」）。本模块只回答「是不是同一句 / 并入哪一条」，不写库。

判定三级，命中即停：
  K0 规范化精确键：sentence_key 相等；
  K1 词元级模糊：词元数相等 && 不同词元 <= 1 && 两个不同词元形近（OCR 误读而非换词）
     && 字符相似度 >= 0.9 && 规范化后长度 >= 20。

词元级闸门是硬要求，不是优化：真实 OCR 微差异（inst <-> just）字符 ratio 0.956 命中；
而「少一个实词」ratio 0.966、「常见虚词替换」quick_ratio 0.986 都比真重复更高——只看
字符相似度会误并。词元数相等 + 不同词元 <= 1 才能同时放行本例、拒绝这些负样本
（前序调研 §6.0 相似度实测表）。

「不同词元 <= 1」还不够：**单处**换词（`packed` -> `crowded`、`a` -> `the`）也只有一个
不同词元，字符 ratio 仍然过线，会把两句不同的话并成一张卡（spec 用户故事 7、8 明确要求
不并）。所以再卡一道「这一对不同的词元必须形近」：长度差 <= 1 且两词自身相似度 >= 0.5。
实测：inst/just = 0.500（真 OCR 误读，放行）、a/the = 0.000、the/an = 0.000、
packed/crowded = 0.462（不同词，拒绝）。形近词（in/on 0.500、packs/packed 0.727）仍属
已知边界，见 spec §已知边界。

本模块不 import store / article / hub：store 会 import 本模块，反向依赖会成环；article 的
排版标点映射也只做最小内联。不读数据库 / 网络 / 文件，不产生循环导入。

领域词汇见仓库根 GLOSSARY.md：句卡 / 原句（Context sentence）/ 词条 / 命中次数。
"""

from __future__ import annotations

import difflib
import re

# --- 常量 ---------------------------------------------------------------------

# 相似度阈值与文章指纹去重同口径：hub.py:160 的 DEDUP_THRESHOLD = 0.9（v22 P2a）。
# 复用同一数值，避免代码库里出现两套相似度标准（spec §Implementation Decision 3、User Story 16）。
DEDUP_THRESHOLD = 0.9

# K1 的规范化后长度下限：短句词元少，1 个词元差异占比高、易误并；只对 >= 20 字符的句子
# 做模糊比较（spec §Implementation Decision 13、调研 §6.0 建议值）。
MIN_FUZZY_CHARS = 20

# K1 的长度上限：挡住「长文整段被误当单句」的 O(n^2) 相似度计算，超长原句保守地不参与
# 模糊判定（spec §Implementation Decision 13、调研 §8.3 回归风险 3「可跳过 > 某长度的
# 候选」）。正常英文单句远小于此值；具体数值 spec 未规定。
MAX_FUZZY_CHARS = 400

# K1 允许的最大不同词元数：放行 OCR 单词微差异（1 个），拒绝两处及以上的替换。
MAX_TOKEN_DIFF = 1

# 单处差异的「形近」门槛（spec 用户故事 7、8）：只有像同一个词被 OCR 读错才允许并入。
# 实测（difflib ratio，本机一次性计算）：inst/just 0.500 必须放行；a/the 0.000、
# the/an 0.000、packed/crowded 0.462 必须拒绝。取 0.5 = 放行边界，再配长度差 <= 1
# 把「不同长度的实词替换」一并挡掉（packed 6 字 / crowded 7 字）。
TOKEN_DIFF_MIN_RATIO = 0.5
TOKEN_DIFF_MAX_LEN_GAP = 1

# 排版标点归一（逐字符 1:1）：弯引号类 -> '、连字符/破折号类 -> -。
# 语义内联自 article.py:65-69（_APOSTROPHES / _DASHES / _PUNCT_MAP），不 import article：
# article 被 hub 导入、本模块被 store 导入，内联可避免任何跨层依赖与循环导入（调研 §7.1）。
_APOSTROPHES = "\u2019\u2018\u201a\u201b\u2032\u00b4"
_DASHES = "\u2013\u2014\u2212\u2010\u2011\u2012\u2015"
_PUNCT_MAP = str.maketrans(
    _APOSTROPHES + _DASHES,
    "'" * len(_APOSTROPHES) + "-" * len(_DASHES),
)

# 去噪：保留字母 / 数字 / 空白 / 撇号，其余标点一律换成空格。撇号保留以维持 it's 这类
# 缩写为单个词元。与调研 §7.1 的 re.sub(r"[^\w\s']", " ", …) 同口径。
_PUNCT_STRIP = re.compile(r"[^\w\s']")

# 空白折叠：与 store.one_line / lib/Logic.ahk 的 NormalizeLine 同形（换行 -> 空格、折叠空白）。
_WS_RUN = re.compile(r"[ \t\u00a0\u3000]+")


# --- 规范化键 -----------------------------------------------------------------


def sentence_key(context: str) -> str:
    """规范化键：单行化 -> 排版标点归一 -> 小写 -> 去标点噪声 -> 折叠空白。

    输出同时用于 K0 精确键、词元切分与字符相似度比较。空原句 / 纯标点规范化后为 ""，
    由 sentence_match 的空键保护挡掉（对齐前端 contextCards「空原句各占一张卡」的语义）。
    """
    if context is None:
        return ""
    text = str(context).replace("\r", " ").replace("\n", " ")
    text = _WS_RUN.sub(" ", text).strip()
    text = text.translate(_PUNCT_MAP).lower()
    text = _PUNCT_STRIP.sub(" ", text)
    return " ".join(text.split())


# --- 相似度（与文章指纹去重同口径）--------------------------------------------


def _text_similarity(left: str, right: str) -> float:
    """两根规范化键的字符相似度，口径照搬 hub.py:1257-1266 的 _text_similarity。

    quick_ratio()（O(n) 上界）预筛，过线才用 ratio() 复核——quick_ratio 会高估
    （调研 §6.0：常见虚词替换 quick 0.986 但 ratio 0.873），只看预筛会把不同句并进来。

    autojunk=False 必须显式关：SequenceMatcher 默认的 autojunk 启发式会把长串里的「常见
    字符」当 junk，字符级比对下会让长文本 ratio 失真（误判成 1.0 或漏判）。与文章指纹
    去重保持同一口径，避免两套相似度标准（spec §Implementation Decision 3、User Story 16）。
    """
    matcher = difflib.SequenceMatcher(None, left, right, autojunk=False)
    if matcher.quick_ratio() < DEDUP_THRESHOLD:
        return 0.0
    return matcher.ratio()


def _tokens_near_identical(left: str, right: str) -> bool:
    """两个不同词元是否形近到「像同一个词被 OCR 读错」（spec 用户故事 7、8）。"""
    if abs(len(left) - len(right)) > TOKEN_DIFF_MAX_LEN_GAP:
        return False
    matcher = difflib.SequenceMatcher(None, left, right, autojunk=False)
    return matcher.ratio() >= TOKEN_DIFF_MIN_RATIO


def _token_diff_ok(left_tokens: list[str], right_tokens: list[str]) -> bool:
    """词元级闸门：词元数相等 && 不同词元 <= 1 && 这一对词元形近（调研 §7.1）。

    用词元级 SequenceMatcher.get_opcodes() 定位差异；单处替换还要满足
    `_tokens_near_identical` —— 单处换词（`a`/`the`、`packed`/`crowded`）虽然只有
    一个不同词元，但不是 OCR 误读，不并（否则两句不同的话会被并成一张句卡）。
    """
    if len(left_tokens) != len(right_tokens):
        return False
    matcher = difflib.SequenceMatcher(None, left_tokens, right_tokens, autojunk=False)
    diff = 0
    pair: tuple[str, str] | None = None
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        diff += max(i2 - i1, j2 - j1)
        # 只有「1 换 1」的替换才可能形近；插入 / 删除（1 换 0）一律不算
        pair = (
            (left_tokens[i1], right_tokens[j1])
            if i2 - i1 == 1 and j2 - j1 == 1
            else None
        )
    if diff > MAX_TOKEN_DIFF:
        return False
    if diff == 0:  # 理论上 K0 已拦下，留住兜底语义
        return True
    return pair is not None and _tokens_near_identical(*pair)


# --- 同句判定 -----------------------------------------------------------------


def sentence_match(left: str, right: str) -> bool:
    """left / right 是否为同一句（OCR 微差异 / 标点 / 大小写差异视为同句）。

    K0：规范化键相等即命中（含仅标点、大小写差异）。
    K1：词元级模糊——词元数相等、不同词元 <= 1、这一对词元形近（OCR 误读）、
        字符相似度 >= 0.9、规范化后长度 >= 20 且不超上限。任一闸门不满足即拒绝
        （保守，宁可不并、不可误并）。
    """
    key_left = sentence_key(left)
    key_right = sentence_key(right)

    # 空原句 / 纯标点不参与合并：对齐前端 contextCards「context 为空各占一张卡」，
    # 也避免两个空键在 K0 精确相等处被误判成同句。
    if not key_left or not key_right:
        return False

    # K0 规范化精确键，命中即停。
    if key_left == key_right:
        return True

    # K1 词元级模糊。先过长度闸门（下限防短句误并、上限挡 O(n^2)），再过词元闸门，
    # 最后才做字符相似度。
    if len(key_left) < MIN_FUZZY_CHARS or len(key_right) < MIN_FUZZY_CHARS:
        return False
    if len(key_left) > MAX_FUZZY_CHARS or len(key_right) > MAX_FUZZY_CHARS:
        return False

    left_tokens = key_left.split()
    right_tokens = key_right.split()
    if not _token_diff_ok(left_tokens, right_tokens):
        return False
    return _text_similarity(key_left, key_right) >= DEDUP_THRESHOLD


# --- 候选挑选 -----------------------------------------------------------------


def pick_same_sentence(context: str, candidates: list[dict]) -> str | None:
    """从候选里挑出应并入的既有句子，返回它的原始 context 文本；无命中返回 None。

    candidates 元素形如 {"context": str, "captured_at": str}（captured_at 为
    "YYYY-MM-DD HH:mm:ss" 字符串，字典序即时间序）。多条命中取 captured_at 最新的一条。
    返回的是候选的**原始** context（非规范化键）：落库层把它作为新词条的 context，
    前端按 context 逐字全等并卡才有稳定的键。
    """
    best_context: str | None = None
    best_ts: str | None = None
    for candidate in candidates:
        candidate_context = candidate.get("context", "")
        if not sentence_match(context, candidate_context):
            continue
        captured_at = candidate.get("captured_at", "")
        if best_ts is None or captured_at > best_ts:
            best_ts = captured_at
            best_context = candidate_context
    return best_context


# --- 合并守卫：目标词是否还在既有句子里 -----------------------------------------


def _stem_key(word: str) -> str:
    """词干键：与 `web/app.js:312` 的 `stemKey()` 逐条同形（去复数 / -ing / -ed / -e）。

    只用于单词语目标词的兜底比对，口径必须跟前端高亮一致：前端 `findTargetSpan()`
    先整词、再「同词干」定位，这里若比前端更严，就会出现「明明能高亮却不合并」。
    不 import 前端、也不复用 article 的分词（本模块保持零依赖纯函数）。
    """
    text = word
    if len(text) > 4 and text.endswith("ies"):
        return text[:-3] + "y"
    for suffix in ("ing", "ed", "es", "s", "d", "e"):
        if len(text) - len(suffix) >= 2 and text.endswith(suffix):
            return text[: -len(suffix)]
    return text


def contains_target(context: str, target: str) -> bool:
    """既有句子 `context` 里是否还找得到目标词 `target`（合并前的守卫，spec 用户故事 7）。

    为什么需要：同句判定允许「一个词元不同」，而 OCR 读错的**就是**目标词时（卡 B 记的
    `just`、既有句子是 `inst`），把新词条并进去会让目标词在原句里彻底消失——前端
    `findTargetSpan()` 返回 null，句卡里不高亮、点不出、播段也点不到，只剩一个悬空词条。
    这种情况宁可不并（保留本次原句、容忍多一张卡），也不制造「词不在句子里」的坏卡。

    单词语目标词按「同词干」放宽（`run` ⊂ `runs`），与前端 `findTargetSpan()` 的口径一致；
    多词语（短语 / 整句）要求词元逐字相等——前端对短语走的是整段字面匹配。
    """
    key_context = sentence_key(context)
    key_target = sentence_key(target)
    if not key_context or not key_target:
        return False
    target_tokens = key_target.split()
    context_tokens = key_context.split()
    count = len(target_tokens)
    if count > len(context_tokens):
        return False
    single = count == 1
    for start in range(len(context_tokens) - count + 1):
        window = context_tokens[start : start + count]
        if single:
            if _stem_key(window[0]) == _stem_key(target_tokens[0]):
                return True
        elif window == target_tokens:
            return True
    return False
