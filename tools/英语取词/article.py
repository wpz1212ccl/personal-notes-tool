"""长文目标词 → 原句定位（v21 规格 §6，票 #159 的纯函数层）—— 零依赖、不落盘、不 logging。

给定「单行化全文 `text` + 段落字符偏移 `passages` + 用户打的目标词 `target`」，回答
「命中的是哪一段、哪一句、目标词在**全文**里的字符区间在哪」，并给出最多 8 条命中位置。
这是 `article_locate` RPC 的纯函数底座：hub 只负责 `doc_id → (text, passages)` 的取数，
定位逻辑全在这里，因此可以脱库单测（`tests/test_longform_locate.py` +
`tests/fixtures/longform-locate-golden.json`）。

口径（与外壳 `lib/Logic.ahk`、前端 `web/app.js` 的 `findTargetSpan` 对齐；任何一处改动
都必须同步三端，golden cases 会报警）：

- `normalize_punct` ≡ AHK `NormalizePunct`：弯引号类 → `'`、连字符类 → `-`（逐字符 1:1
  映射，所以**归一化前后字符偏移完全相同** —— 这是能拿全文偏移直接切原句的前提）。
- `fold` ≡ AHK 调用点的 `Fold(NormalizePunct(w))`：小写 + 去掉所有 `'` 与 `-`。
  `dont` ≡ `don't`、`wellknown` ≡ `well-known`。
- `stem_key` ≡ 前端 `web/app.js` 的 `stemKey`：`ies→y`，去尾 `ing/ed/es/s/d/e`（保留 ≥2 字）。
- 档位（tier 越小越优先）：精确三档 **T0 整词**（大小写不敏感 + JS `\\b` 词边界）→
  **T1 Fold 相等** → **T2 词干相等**；近似两档 **T3 前缀**（输入 ≥2 字，与 AHK
  `CompletionTiers` 的门槛同口径）→ **T4 裸子串**（前端 ③）。
  **近似档只在精确三档全无命中时启用**（与前端的「①②不中才跑③」、AHK 的「无直命中才
  吸附」同一纪律）：否则 `the` 会顺带命中 `theory` 的 T3、`he` 会顺带命中 `help` 的 T4，
  occurrences 里全是噪声。近似档内部 T3 仍优先于 T4 —— 典型例子是 `runn`：`runner`
  只是 T4 裸子串，`running` 是 T3 前缀。
- 多命中排序：`(tier, 大小写是否全等, 段距, 段序, 句序, 起点)`。
  **tier 必须优先于位置**：否则文章开头先出现的 `runner`（T4）会压掉 `running`（T3）。
  `current_passage` 只在同 tier、同大小写之间起作用 —— 给了就优先该段、再按「离它多远」；
  不给就退回纯「段序」（规格 §6 的排序定义就是这一档）。
- `occurrences` 上限 8 条（避免 `the` 这类词炸出几千条）；`found:false` 是**正常结果**
  （用户可能打的是新词，或者原文是 OCR 错字），不是错误。
- `sentence` 是 `longtext.split_sentences` 切出的**完整句子**，不是段落：外壳拿它当
  `V.context`，从而保持 `CompletionTiers` / `ChainStep` 的单句语义（架构 §4.2）。

性能：索引在 `ArticleIndex` 构造时建一次（T1/T2 走字典、T3 走排序数组 + 二分），
之后每次定位只扫 T0/T4 的字面子串（C 速度）与少量 token；20–100KB 文章实测见
`tests/test_longform_locate.py::test_locate_performance`。
"""

from __future__ import annotations

import heapq
import re
import string
from bisect import bisect_left
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from longtext import Passage, split_sentences

# --- 常量 ---------------------------------------------------------------------

# 档位：数值即排序键的第一位，**不要为了可读性重排**。
TIER_WHOLE_WORD = 0
TIER_FOLD_EQUAL = 1
TIER_STEM_EQUAL = 2
TIER_PREFIX = 3
TIER_SUBSTRING = 4

# `occurrences` 上限（与 corrections.py 的 `_MAX_SUSPECTS = 8` 同一量级）。
MAX_OCCURRENCES = 8

# T3 前缀的最短输入：与 AHK `CompletionTiers` 的「输入 < 2 字不补全」同口径。
PREFIX_MIN_CHARS = 2

# 排版标点归一（逐字符 1:1）。字符集必须与 AHK `NormalizePunct` 逐字相同。
_APOSTROPHES = "’‘‚‛′´"
_DASHES = "–—−‐‑‒―"
_PUNCT_MAP = str.maketrans(
    _APOSTROPHES + _DASHES, "'" * len(_APOSTROPHES) + "-" * len(_DASHES)
)

# 只折 ASCII 大小写：与 JS `toLowerCase` 在英文正文上一致，且不改变字符数（偏移安全）。
_LOWER_MAP = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)

# 词元：与 AHK `SplitTokens` 同形（字母 + 词内 `'`/`-`）。跑在归一化后的文本上，
# 所以弯引号/长连字符这时已经是 ASCII。
_TOKEN = re.compile(r"[A-Za-z]+(?:['-][A-Za-z]+)*")

# JS `\b` 的词字符集（ASCII）。**不能**用 Python 的 `\b`：它把中文也当词字符，
# 而前端 / AHK 的边界判定是 ASCII 口径（`中文researchers研究` 里 `researchers` 是整词）。
_WORD_CHARS = frozenset(string.ascii_letters + string.digits + "_")

# stemKey 的后缀表，顺序即前端 `findTargetSpan` 的判定顺序，不要重排。
_STEM_SUFFIXES = ("ing", "ed", "es", "s", "d", "e")

# T3 二分的前缀上界哨兵：任何「以 needle 开头且更长」的串都小于 `needle + 哨兵`。
_PREFIX_SENTINEL = "\U0010ffff"


# --- 归一化（与 AHK / 前端同口径）---------------------------------------------


def normalize_punct(text: str) -> str:
    """排版标点 → 键盘 ASCII（≡ AHK `NormalizePunct`）。逐字符 1:1，偏移不变。"""
    if not text:
        return ""
    return text.translate(_PUNCT_MAP)


def fold(word: str) -> str:
    """比较口径：小写 + 去 `'` 与 `-`（≡ AHK 调用点的 `Fold(NormalizePunct(w))`）。"""
    if not word:
        return ""
    return _ascii_lower(normalize_punct(word)).replace("'", "").replace("-", "")


def stem_key(word: str) -> str:
    """极简词干（≡ 前端 `web/app.js:280` 的 `stemKey`，逐条规则照抄）。

    `ies→y`；再按 `ing/ed/es/s/d/e` 的顺序去掉第一个命中的后缀（保留 ≥2 字符）。
    这是**两端共用的比较口径**，不是语言学词干 —— 改之前先看前端。
    """
    text = _ascii_lower(normalize_punct(str(word or ""))).strip()
    if not text:
        return ""
    if len(text) > 4 and text.endswith("ies"):
        return text[:-3] + "y"
    for suffix in _STEM_SUFFIXES:
        if len(text) - len(suffix) >= 2 and text.endswith(suffix):
            return text[: -len(suffix)]
    return text


def _ascii_lower(text: str) -> str:
    """只折 ASCII 大小写；字符数不变，所以字符偏移与原文一致。"""
    return text.translate(_LOWER_MAP)


def _has_word_boundary(text: str, position: int, end: int) -> bool:
    """`[position, end)` 两端是否都是 JS `\\b` 意义上的词边界（ASCII 词字符口径）。

    前端 `\\b` 的边界条件是「两侧是否词字符不同」；这里对两端各判一次。
    """
    left = position > 0 and text[position - 1] in _WORD_CHARS
    right = position < len(text) and text[position] in _WORD_CHARS
    if left == right:
        return False
    left = end > 0 and text[end - 1] in _WORD_CHARS
    right = end < len(text) and text[end] in _WORD_CHARS
    return left != right


# --- 索引内部结构 -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Sentence:
    """一整句：`[start, end)` 是**全文**绝对偏移；`passage_index` 是它所在段。"""

    start: int
    end: int
    passage_index: int


@dataclass(frozen=True, slots=True)
class _Token:
    """一个词元（归一化后的字母串）：绝对偏移 + 两种比较口径 + 所在句下标。

    `text` 是归一化后的原形态（大小写保留），用于判「大小写是否全等」。
    """

    start: int
    end: int
    text: str
    fold: str
    stem: str
    sentence: int


@dataclass(frozen=True, slots=True)
class _Hit:
    """一次命中：档位 + 绝对区间 + 所在句下标 + 是否大小写全等（0 = 全等）。"""

    tier: int
    start: int
    end: int
    sentence: int
    exact: int


def _coerce_passages(text: str, passages: Sequence[object]) -> list[Passage]:
    """把 `[[start, end], ...]`（或 `Passage` 列表）校成 `Passage` 列表。

    校验（非法一律 `ValueError`，**不静默修正** —— 与 `article_create` 的
    `bad passages` 同一纪律）：`0 <= start <= end <= len(text)`、按段序不重叠。

    段间通常是一个空格（`longtext.join_passages` 口径），但这里只要求不重叠、
    落在全文内 —— 越界/乱序才是真错，段间空隙多一个空格不该让定位整体失败。
    """
    spans: list[Passage] = []
    previous_end = 0
    for index, item in enumerate(passages or ()):
        if isinstance(item, Passage):
            start, end = item.start, item.end
        else:
            try:
                start, end = int(item[0]), int(item[1])  # type: ignore[index]
            except (TypeError, IndexError, ValueError):
                raise ValueError(
                    f"passage[{index}] 必须是 (start, end) 或 Passage"
                ) from None
        if not 0 <= start <= end <= len(text):
            raise ValueError(
                f"passage[{index}] 越界：[{start}, {end}) 不在 [0, {len(text)}] 内"
            )
        if start < previous_end:
            raise ValueError(
                f"passage[{index}] 与上一段重叠或乱序：start={start} < 上一段 end={previous_end}"
            )
        spans.append(Passage(text[start:end], start, end))
        previous_end = end
    return spans


class ArticleIndex:
    """一篇文章的定位索引（构造一次，`locate()` 可反复调用）。

    用法::

        index = ArticleIndex(text, passages)   # passages = [[start, end], ...]
        index.locate("reluctant")              # → article_locate 响应的 JSON 形状
        index.locate("theory", current_passage=36)

    构造时的 `ValueError` 语义见 `_coerce_passages`。`locate()` 返回的 dict 字段与
    `article_locate` 响应**逐字相同**（hub 下一阶段只需原样透传,不在 hub 里再算）::

        {"found": True, "passage_index": 36, "sentence": "...完整一句...",
         "sentence_start": 5210, "sentence_end": 5388,
         "target_start": 5301, "target_end": 5310,
         "occurrences": [{"passage_index": 36, "start": 5301}, ...]}   # ≤ 8 条
        {"found": False}                                               # 正常结果
    """

    def __init__(self, text: str, passages: Sequence[object]) -> None:
        if not isinstance(text, str):
            raise TypeError("text 必须是字符串")
        self.text = text
        self.passages = _coerce_passages(text, passages)
        # 归一化视图：与原文**等长**（1:1 标点映射），所以所有偏移对两端都成立。
        self._norm = normalize_punct(text)
        self._low = _ascii_lower(self._norm)
        self._sentences: list[_Sentence] = []
        self._tokens: list[_Token] = []
        self._token_texts: set[str] = set()
        self._by_fold: dict[str, list[int]] = {}
        self._by_stem: dict[str, list[int]] = {}
        self._fold_keys: list[str] = []
        self._fold_order: list[int] = []
        self._build()

    # --- 建索引（每篇文章一次）--------------------------------------------

    def _build(self) -> None:
        for passage_index, passage in enumerate(self.passages):
            body = self._norm[passage.start : passage.end]
            cursor = 0
            for sentence in split_sentences(body):
                offset = body.find(sentence, cursor)
                if offset < 0:  # 兜底：`split_sentences` 已 strip，理论上不会发生
                    offset = body.find(sentence)
                if offset < 0:
                    continue
                start = passage.start + offset
                self._sentences.append(
                    _Sentence(start, start + len(sentence), passage_index)
                )
                cursor = offset + len(sentence)

        for sentence_index, sentence in enumerate(self._sentences):
            body = self._norm[sentence.start : sentence.end]
            for match in _TOKEN.finditer(body):
                text = match.group(0)
                self._tokens.append(
                    _Token(
                        start=sentence.start + match.start(),
                        end=sentence.start + match.end(),
                        text=text,
                        fold=fold(text),
                        stem=stem_key(text),
                        sentence=sentence_index,
                    )
                )

        for index, token in enumerate(self._tokens):
            self._by_fold.setdefault(token.fold, []).append(index)
            self._by_stem.setdefault(token.stem, []).append(index)
            self._token_texts.add(token.text)
        # T3 前缀走「按 Fold 排序 + 二分」，避免每次定位线性扫两万个词元。
        order = sorted(
            range(len(self._tokens)), key=lambda i: (self._tokens[i].fold, i)
        )
        self._fold_keys = [self._tokens[i].fold for i in order]
        self._fold_order = order

    # --- 定位 --------------------------------------------------------------

    def locate(
        self, target: str, current_passage: int | None = None
    ) -> dict[str, object]:
        """目标词 → 原句 + 命中区间 + 最多 8 条命中位置。

        `current_passage` 是外壳的阅读位置提示：同 tier、同大小写精度下优先落在这个
        段落（其次按离它的段距），从而「同词出现在第 5 段与第 36 段」时给用户眼前那句。
        `None` 时退回规格 §6 的纯 `(tier, 大小写, 段序, 句序)`。
        """
        raw = str(target or "").strip()
        if not raw or not self._sentences:
            return {"found": False}
        needle = normalize_punct(raw)
        needle_fold = fold(raw)
        hits = self._collect_hits(needle, needle_fold, current_passage)
        if not hits:
            return {"found": False}

        ordered = sorted(hits, key=lambda hit: self._rank_key(hit, current_passage))
        primary = ordered[0]
        sentence = self._sentences[primary.sentence]
        return {
            "found": True,
            "passage_index": sentence.passage_index,
            # 原句取**原文**切片（不是归一化视图）：外壳显示给用户看的形态不变。
            "sentence": self.text[sentence.start : sentence.end],
            "sentence_start": sentence.start,
            "sentence_end": sentence.end,
            "target_start": primary.start,
            "target_end": primary.end,
            "occurrences": [
                {
                    "passage_index": self._sentences[hit.sentence].passage_index,
                    "start": hit.start,
                }
                for hit in ordered[:MAX_OCCURRENCES]
            ],
        }

    # --- 五档匹配 ----------------------------------------------------------

    def _collect_hits(
        self, needle: str, needle_fold: str, current_passage: int | None
    ) -> list[_Hit]:
        """五档命中，只保留**最终 top-8 可能用到**的候选（每档最多 16 条）。

        先扫精确三档（T0/T1/T2）；有命中就直接返回，近似档不再参与（见模块
        docstring）。同一 `start` 只留最靠前的一档（T0 优先于 T1/T2）。近似档内部
        T3 与 T4 共存，由排序键的 `tier` 决定先后（`runni` 的 `running`/`outrunning`）。
        """
        collected: list[_Hit] = []
        seen: set[int] = set()
        # 「档内是否可能出现大小写全等命中」：不可能时 `_cap_hits` 收满 8 条非全等
        # 就能停扫（`the`/`examin` 这类高频词的主要开销就省在这里）。
        exact_in_text = needle in self._norm
        exact_in_tokens = needle in self._token_texts

        def absorb(hits: Iterable[_Hit], exact_possible: bool = True) -> bool:
            for hit in self._cap_hits(hits, current_passage, exact_possible):
                if hit.start not in seen:
                    seen.add(hit.start)
                    collected.append(hit)
            return len(collected) >= MAX_OCCURRENCES

        # 档位按优先级试：某档的候选够 8 条就可以停 —— 排序键的 tier 在前，
        # 后面的档位不可能再挤进 top-8。
        if absorb(self._iter_whole_word(needle), exact_in_text):
            return collected
        if absorb(
            self._iter_tokens(
                TIER_FOLD_EQUAL, self._by_fold.get(needle_fold, ()), needle
            ),
            exact_in_tokens,
        ):
            return collected
        if absorb(
            self._iter_tokens(
                TIER_STEM_EQUAL, self._by_stem.get(stem_key(needle), ()), needle
            ),
            exact_in_tokens,
        ):
            return collected
        if collected:  # 精确三档有命中 → 近似档是兜底，不参与
            return collected
        if absorb(self._iter_prefix(needle_fold), exact_possible=False):
            return collected
        absorb(self._iter_substring(needle), exact_in_text)
        return collected

    def _cap_hits(
        self,
        hits: Iterable[_Hit],
        current_passage: int | None,
        exact_possible: bool = True,
    ) -> list[_Hit]:
        """把一档的全部命中裁到「最多 8 条全等 + 8 条非全等」。

        排序键是 `(tier, 大小写是否全等, 段距, 段序, 句序, 起点)`：同一档内先按
        全等、再按位置。`current_passage` 为空时位置就是迭代顺序（各档都按全文
        顺序产出），因此**可以边扫边停**，不必为 `the` 这种词建几千条候选；
        给了 `current_passage` 时段距会改变档内顺序，改用 `nsmallest` 取真前 8。
        """
        if current_passage is None:
            exact: list[_Hit] = []
            rest: list[_Hit] = []
            for hit in hits:
                if hit.exact == 0:
                    if len(exact) < MAX_OCCURRENCES:
                        exact.append(hit)
                elif len(rest) < MAX_OCCURRENCES:
                    rest.append(hit)
                # 全等命中够 8 条时，档内前 8 全是它们，后面的候选不可能再进来。
                if len(exact) >= MAX_OCCURRENCES or (
                    not exact_possible and len(rest) >= MAX_OCCURRENCES
                ):
                    break
            return exact + rest

        # 给了 current_passage：档内顺序变成 `(全等, 段距, 段序, 句序, 起点)`，位置不是
        # 单调的，所以边扫边用「最多 8 条」的小顶堆（存负 key 当大顶堆）裁剪，
        # 而不是把几千条命中物化后再 nsmallest。
        exact_heap: list[tuple[tuple[int, int, int, int], _Hit]] = []
        rest_heap: list[tuple[tuple[int, int, int, int], _Hit]] = []
        for hit in hits:
            sentence = self._sentences[hit.sentence]
            neg_prox = -abs(sentence.passage_index - current_passage)
            heap = exact_heap if hit.exact == 0 else rest_heap
            if len(heap) >= MAX_OCCURRENCES:
                worst = heap[0][0]
                # 先只比「段距」这个最便宜的分量：离得比现有的 8 条还远就直接跳过，
                # 避免为几千条命中都建 key 元组（`the` 这类词的主要开销）。
                if neg_prox < worst[0]:
                    continue
                if (
                    neg_prox == worst[0]
                    and (
                        -sentence.passage_index,
                        -hit.sentence,
                        -hit.start,
                    )
                    <= worst[1:]
                ):
                    continue
                heapq.heapreplace(
                    heap,
                    (
                        (neg_prox, -sentence.passage_index, -hit.sentence, -hit.start),
                        hit,
                    ),
                )
                continue
            heapq.heappush(
                heap,
                (
                    (neg_prox, -sentence.passage_index, -hit.sentence, -hit.start),
                    hit,
                ),
            )
            # 8 条全等命中且都落在阅读位置那一段：档内前 8 已经确定，可以停扫。
            if (
                len(exact_heap) >= MAX_OCCURRENCES
                and exact_heap[0][0][0] == 0
                and heap is exact_heap
            ):
                break
        return [hit for _, hit in sorted(exact_heap)] + [
            hit for _, hit in sorted(rest_heap)
        ]

    def _iter_whole_word(self, needle: str) -> Iterable[_Hit]:
        """T0 整词：大小写不敏感 + JS `\\b` 词边界。"""
        length = len(needle)
        for position, sentence in self._scan(_ascii_lower(needle), boundary=True):
            yield _Hit(
                TIER_WHOLE_WORD,
                position,
                position + length,
                sentence,
                self._exact(position, needle),
            )

    def _iter_tokens(
        self, tier: int, token_indexes: Iterable[int], needle: str
    ) -> Iterable[_Hit]:
        """T1（Fold 相等）/ T2（词干相等）：命中的是**整词**，span 取词元区间。"""
        tokens = self._tokens
        for token_index in token_indexes:
            token = tokens[token_index]
            exact = 0 if token.text == needle else 1
            yield _Hit(tier, token.start, token.end, token.sentence, exact)

    def _iter_prefix(self, needle_fold: str) -> Iterable[_Hit]:
        """T3 前缀（`resea` → `researchers`，高亮整词）。最少 2 字，避免单字母炸全篇。"""
        if len(needle_fold) < PREFIX_MIN_CHARS:
            return
        low = bisect_left(self._fold_keys, needle_fold)
        high = bisect_left(self._fold_keys, needle_fold + _PREFIX_SENTINEL)
        tokens = self._tokens
        # 按词元下标（= 全文顺序）产出，`_cap_hits` 的「边扫边停」才成立。
        for token_index in sorted(self._fold_order[low:high]):
            token = tokens[token_index]
            if len(token.fold) > len(needle_fold):
                yield _Hit(TIER_PREFIX, token.start, token.end, token.sentence, 1)

    def _iter_substring(self, needle: str) -> Iterable[_Hit]:
        """T4 裸子串（前端 ③ 的兜底行为）：不要求词边界，大小写不敏感。"""
        length = len(needle)
        for position, sentence in self._scan(_ascii_lower(needle), boundary=False):
            yield _Hit(
                TIER_SUBSTRING,
                position,
                position + length,
                sentence,
                self._exact(position, needle),
            )

    def _scan(self, needle_low: str, *, boundary: bool) -> Iterable[tuple[int, int]]:
        """在归一化小写全文里扫字面子串，产出 `(起点, 所在句下标)`。

        `boundary=True` 时只留两端都是 JS 词边界的位置（T0）。匹配按位置升序产出，
        所以句下标的游标只前进不回退（比每命中一次二分便宜）；落在句子之外
        （段间空隙）的匹配跳过。
        """
        text = self._low
        length = len(needle_low)
        sentences = self._sentences
        count = len(sentences)
        index = 0
        position = text.find(needle_low)
        while position >= 0:
            if not boundary or _has_word_boundary(text, position, position + length):
                while index + 1 < count and sentences[index + 1].start <= position:
                    index += 1
                sentence = sentences[index]
                if sentence.start <= position < sentence.end:
                    yield position, index
            position = text.find(needle_low, position + 1)

    def _exact(self, position: int, needle: str) -> int:
        """`position` 起的原文切片是否与目标词逐字相同（0 = 大小写全等）。"""
        end = position + len(needle)
        return 0 if self._norm[position:end] == needle else 1

    # --- 排序 --------------------------------------------------------------

    def _in_tier_key(
        self, hit: _Hit, current_passage: int | None
    ) -> tuple[int, int, int, int, int]:
        """档内排序键：`(大小写是否全等, 段距, 段序, 句序, 起点)`。"""
        sentence = self._sentences[hit.sentence]
        proximity = (
            abs(sentence.passage_index - current_passage)
            if isinstance(current_passage, int)
            else 0
        )
        return (hit.exact, proximity, sentence.passage_index, hit.sentence, hit.start)

    def _rank_key(
        self, hit: _Hit, current_passage: int | None
    ) -> tuple[int, int, int, int, int, int]:
        """排序键：`(tier, 大小写是否全等, 段距, 段序, 句序, 起点)`。

        `大小写是否全等`：命中的原文切片与目标词（按同一归一化）逐字相同 → 0，否则 1。
        所以 `the` 会先命中小写那次，而不是句首的 `The`。
        `段距`：给了 `current_passage` 时是「离阅读位置多远」（优先眼前那段），
        没给时恒为 0，即规格 §6 的纯 `(tier, 大小写, 段序, 句序)`。
        """
        return (hit.tier,) + self._in_tier_key(hit, current_passage)
