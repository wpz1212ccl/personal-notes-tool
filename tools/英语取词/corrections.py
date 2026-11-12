"""§5.3 词表后校正：把 OOV 词元改成「更像词」的结果，纯函数、零 I/O、可单测。

入口 `correct(text, known_words, common_words=None) -> (new_text, corrections)`：

- `corrections` 是逐条改动 `[{"from", "to", "kind"}]`，`kind ∈ {"split", "confusion", "punct"}`
  （"merge" 留给将来的合并规则，本票没有触发它的场景）——外壳据此显示「已校正 N 处」。
- 两条硬规则（都有反例测试）：
  1. **只动 OOV 词元**：已经是已知词的词元一个字符都不改；
  2. **改动必须让结果更像词**：整词替换的结果必须是已知词、切段的总成本必须低于原样
     （见 `_split_segments`），否则原样保留。
- 专有名词/人名/缩写（词元里出现大写字母）与含数字的词元一律不碰：大小写是纯函数
  能拿到的唯一「专名」判据，含数字的（型号、版本号、数字）改了风险大于收益。代价是
  **句首大写**的普通词也不修（漏改优于错改，写进 #23 的评论里备查）。

本模块不认识 OCR 图像：它只吃 kernel 递进来的文本与词表，词表由调用方加载（I/O 在
kernel 侧，见 `kernel.load_wordlist` / `build_corrector`）。
"""

from __future__ import annotations

import bisect
import functools
import math
import re
from collections.abc import Iterator

# --- 混淆白名单（spec §5.3.2）------------------------------------------------
# OCR 里成对出现的「看错一个字」，只有这些对上的单操作才允许改字（其它一律不动）。
_CONFUSIONS: tuple[tuple[str, str], ...] = (
    ("0", "o"),
    ("l", "i"),
    ("i", "1"),
    ("l", "1"),
    ("c", "e"),
    ("t", "f"),
    ("!", "t"),
    ("u", "n"),
    ("y", "v"),
)
# 两字对一字的混淆（`rn` 被读成 `m`，现代 OCR 最经典的一类）：单独一张表，
# 因为它不是「替换一个字符」而是「两字合一字」。
_GLUED_CONFUSIONS: tuple[tuple[str, str], ...] = (("rn", "m"),)

_WORD_RE = re.compile(r"[A-Za-z]+")
_ALPHABET = "abcdefghijklmnopqrstuvwxyz"  # 同长替换的枚举表（词元一律小写，见硬规则）
# §5.3.3：句尾 `./,/?` 后紧跟大写字母 → 补空格。前面的小写字母是防误伤的护栏
# （小数 `3.5`、缩写 `U.S.A` 都不会命中）。
_SENTENCE_GAP_RE = re.compile(r"(?<=[a-z])([.,?])([A-Z])")

# 2026-10-19（用户现场）：**词内夹着的杂符**要还原成空格。
# 真实字幕上 Windows OCR 会把词间空隙读成 `)` `(` `»` 这类符号：用户截图里
# `Can you tell)me`、`Excuse(me`、`For»example` 的括号/引号其实是空格。
# 只处理这一族「明显不是英文里会出现在词内的符号」；`'` `-` `–` `—` `.` `,` `:` `/` `&`
# 这些**不碰**（`don't` / `well-known` / `e.g.` / `and/or` / `R&D` 都是合法形态）。
_INNER_JUNK = "()[]{}«»•·€§^*|<>\\`"
_INNER_JUNK_RE = re.compile(f"(?<=[A-Za-z])[{re.escape(_INNER_JUNK)}]+(?=[A-Za-z])")

_MIN_WORD_LEN = 3  # 短于它的词元不参与整词改写（信号太少，见 §5.3 的「宁可不改」）
_MIN_SEGMENT_LEN = 4  # 段的最短长度（1/2/3 字母的词表垃圾太多，见 `_segment_value`）
_SPLIT_COST = 1  # 切段总成本必须恰好等于它（见 `_split_segments` 的两条实测理由）
_MAX_TOKEN_LEN = 16  # 超过它的词元不切：整行粘连时 DP 的成本与歧义都失控
_NEVER_LEGIT = "#|•~^*<>"  # 词元尾部这些字符是引擎幻觉（spec §5.3.3 的「#/!/| 等」）
_INDEX_MIN_WORD = _MIN_SEGMENT_LEN  # 删字符索引只收这么长的词（成本 1 的段最短即段长）


# --- 白名单单操作 ------------------------------------------------------------


def _whitelist_variants(word: str) -> Iterator[str]:
    """`word` 上做**一次**白名单操作得到的所有串（不改的话原串不在其中）。"""
    for index, char in enumerate(word):
        for left, right in _CONFUSIONS:
            if char == left:
                yield word[:index] + right + word[index + 1 :]
            elif char == right:
                yield word[:index] + left + word[index + 1 :]
    for left, right in _GLUED_CONFUSIONS:
        start = 0
        while (position := word.find(left, start)) != -1:
            yield word[:position] + right + word[position + len(left) :]
            start = position + 1
        start = 0
        while (position := word.find(right, start)) != -1:
            yield word[:position] + left + word[position + len(right) :]
            start = position + 1


def _confusion_candidate(
    word: str, known: frozenset[str], common: frozenset[str]
) -> str | None:
    """一次白名单操作就能变成**已知词**的候选（常见词优先，其次字典序）。"""
    hits = [variant for variant in _whitelist_variants(word) if variant in known]
    if not hits:
        return None
    return min(hits, key=lambda hit: (hit not in common, hit))


# --- 成本 1 的段：编辑距离 1 的候选词 ----------------------------------------


@functools.lru_cache(maxsize=8)
def _deletions(common: frozenset[str]) -> dict[str, tuple[str, ...]]:
    """「删掉一个字符」的签名 → 所有产生它的词（只收长度 ≥ `_INDEX_MIN_WORD` 的词）。

    有了它，切段 DP 里「段与某个词差一个字符吗」就不用遍历整个词表：

    - 段插一个字符得到词 ⟺ 词删一个字符得到段 → `_deletions(common)[段]`；
    - 段与词同长且差一位 ⟺ 两者删掉**那一位**后同串 → 段的每个删位反查签名；
    - 段删一个字符得到词 → 段的每个删位直接查词表。

    **索引只为切段的候选查询服务，而候选最终必须落在 `common`（2000 常见词）里**
    （`_ed1_candidate` 每一组都有 `hit in common` 过滤）——所以这里只拿 `common` 建，
    不拿整个「已知词」建：并上 ECDICT 的 13 万词后拿 known 建索引实测 **3.3s / 241MiB**，
    而拿 common 建是 **~5ms / 1MiB**（#36 实测），结果逐字节相同。
    「整词是不是只差一个字符」的守卫不走这里（见 `_differs_once_in_known`）。

    按词表对象缓存：kernel 的 common 是模块级常驻的同一个 `frozenset`，所以整条链路
    只建一次索引；测试里的小词表各自建一次，互不污染。
    """
    index: dict[str, list[str]] = {}
    for word in common:
        if len(word) < _INDEX_MIN_WORD:
            continue
        for position in range(len(word)):
            index.setdefault(word[:position] + word[position + 1 :], []).append(word)
    return {signature: tuple(words) for signature, words in index.items()}


def _differs_once(left: str, right: str) -> bool:
    """两个等长串是否只差一个字符。"""
    return sum(a != b for a, b in zip(left, right)) == 1


def _differs_once_in_known(word: str, known: frozenset[str]) -> bool:
    """`word` 与某个**同长**的已知词只差一个字符吗（整词守卫，见 `_correct_word`）。

    用「枚举 26 个字母的替换变体 + 集合查询」而不是删字符索引：并入 ECDICT 后 `known`
    是 13 万词，给它建索引要 3.3s / 241MiB（#36 实测），而一个 16 字符词元枚举下来
    只有 416 次集合查询（微秒级）。副作用是守卫的词表从 10k 升到整个已知词表 ——
    更宽（`toad`/`road` 这类同长差一字符的判定对 ECDICT 词也生效了），方向正确。
    """
    for position in range(len(word)):
        for letter in _ALPHABET:
            variant = word[:position] + letter + word[position + 1 :]
            if variant != word and variant in known:
                return True
    return False


def _same_length_candidates(word: str, index: dict[str, tuple[str, ...]]) -> list[str]:
    """与 `word` 同长、只差一个字符的词（替换型编辑距离 1，只查索引里的 `common`）。"""
    hits: list[str] = []
    for position in range(len(word)):
        signature = word[:position] + word[position + 1 :]
        for candidate in index.get(signature, ()):
            if candidate not in hits and _differs_once(word, candidate):
                hits.append(candidate)
    return hits


def _ed1_candidate(
    segment: str,
    common: frozenset[str],
    index: dict[str, tuple[str, ...]],
) -> tuple[str, int] | None:
    """段的最优「差一个字符」候选：(词, 类型序)。

    类型序 = 0 白名单操作 / 1 同长替换 / 2 删一字（候选人更短）/ 3 插一字（更长）。
    候选**必须在常见词表里**、同类里字典序最小 —— 确定性是硬要求（同一帧两次要给出
    同一结果），而「切出来的每一段都落在 2000 常见词里」是切段可信度的一条硬线
    （见 `_segment_value`）。换字符的替换比增删一字更可信（`youn`→`your` 优于
    `you`/`nbank` 之流），所以排在前面。
    """
    groups = (
        [variant for variant in _whitelist_variants(segment) if variant in common],
        _same_length_candidates(segment, index),
        [
            segment[:position] + segment[position + 1 :]
            for position in range(len(segment))
        ],
        list(index.get(segment, ())),
    )
    for rank, group in enumerate(groups):
        hits = sorted(hit for hit in group if hit in common)
        if hits:
            return hits[0], rank
    return None


# --- 切段 DP（成本 0 = 已知词；成本 1 = 差一个字符；不能成词 = 不可用）---------


def _segment_value(
    segment: str,
    common: frozenset[str],
    index: dict[str, tuple[str, ...]],
) -> tuple[float, float, str] | None:
    """一段的 (成本, 类型序和, 替换文本)；这段本身成不了词就回 None。

    - 本身就是常见词 → 成本 0。**段长加严**：只认 ≥4 字母的词。词表里有 26 个单字母
      和几百个 `aa`/`st`/`ed`/`dec` 之类两三字母垃圾，不挡住的话 `ststet` 会被碎成
      `st st et`、`m` 这种单字母还会让任何 OOV 串都能碎成字母流。
    - 切出来的每一段都必须是**常见词**（或与常见词差一个字符）：实测 `reluctant`
      （词表缺的 -t 结尾词）会被切成 `rely cant`、`decyeort` 解成 `deck port` ——
      一次把满分的用例打坏。要求段落在 2000 常见词里之后这些解全部消失，而真正的
      粘连（`younbankaccount`）不受影响。
    - 与某个常见词差一个字符 → 成本 1（`youn`→`your`、`witm`→`with`）。
    """
    if len(segment) < _MIN_SEGMENT_LEN:
        return None
    if segment in common:
        return (0.0, 0.0, segment)
    found = _ed1_candidate(segment, common, index)
    if found is None:
        return None
    word, rank = found
    return (1.0, 1.0 + rank, word)


def _split_segments(
    word: str,
    common: frozenset[str],
    index: dict[str, tuple[str, ...]],
) -> list[str] | None:
    """把粘连的 OOV 长词元 DP 切成 ≥2 段；不值得切回 None。

    `best[i]` = 从第 i 个字符起的 (总成本, 类型序和, 段数)，字典序最小者胜；段长从 1
    枚举到常见词表里最长的词。**整词当作一段的解不算切段**（那就是「整词替换」，只
    允许走 §5.3.2 的白名单规则）——所以最好解只有一段时回 None。

    成本用不了 `0/1/∞` 之外的刻度（spec §5.3.1）：成本 0 是精确词、1 是差一个字符、
    不可成词的段直接不参与（等价于 ∞）。**总成本必须恰好等于 `_SPLIT_COST`（1）**：
    上限 1 是因为实测错误形态是「粘连 + 个别字母错」（§5 的样本），一段里错两处以上
    不可证；下限 1 是因为纯精确切段与「词表缺一个真词」在纯文本上不可区分 ——
    `backwards`→`back cards`、`superclass`→`super class`、`customization`→`custom i
    nation` 全是纯精确解（在一份 6002 行的英文 prose 样本上误改 843 行；要求「恰好
    花掉成本 1」后降到 213 行，而 `younbankaccount`/`witmthat` 这类真粘连不受影响）。
    代价：`thebank` 这种只丢空格、一个字母都没错的粘连保持原样（宁可不改）。
    """
    length = len(word)
    cost: list[float] = [math.inf] * (length + 1)
    ranks: list[float] = [math.inf] * (length + 1)
    counts: list[int] = [1 << 30] * (length + 1)
    cost[length] = 0.0
    ranks[length] = 0.0
    counts[length] = 0
    step: list[tuple[int, str] | None] = [None] * length
    max_segment = max((len(word) for word in common), default=1)
    for start in range(length - 1, -1, -1):
        for size in range(1, min(max_segment, length - start) + 1):
            value = _segment_value(word[start : start + size], common, index)
            if value is None or cost[start + size] == math.inf:
                continue
            segment_cost, segment_rank, text = value
            candidate = (
                segment_cost + cost[start + size],
                segment_rank + ranks[start + size],
                1 + counts[start + size],
            )
            if candidate < (cost[start], ranks[start], counts[start]):
                cost[start], ranks[start], counts[start] = candidate
                step[start] = (size, text)
    if cost[0] != _SPLIT_COST:
        return None
    segments: list[str] = []
    position = 0
    while position < length:
        chosen = step[position]
        if chosen is None:  # 走不通（只在代码坏死时发生）
            return None
        size, text = chosen
        segments.append(text)
        position += size
    return segments if len(segments) >= 2 else None


# --- 逐词元的规则 ------------------------------------------------------------


def _correct_word(
    word: str, known: frozenset[str], common: frozenset[str]
) -> tuple[str, str] | None:
    """一个 OOV 纯字母词元的整词改写： (替换文本, kind)；不改回 None。

    顺序：白名单混淆 → 切段。整词替换之前先看「它是不是某个已知词只差一个**同长**字符」
    （`toad`↔`road`、`tnto`↔`into`）：是的话它像一个「拼错的词」而不是「粘在一起的词」，
    切段只会把 `toad` 切成 `to ad` 这种半词，**不切**（`_differs_once_in_known`）。
    """
    if word in known or len(word) < _MIN_WORD_LEN or not word.islower():
        return None
    hit = _confusion_candidate(word, known, common)
    if hit is not None:
        return hit, "confusion"
    if len(word) <= _MAX_TOKEN_LEN and not _differs_once_in_known(word, known):
        segments = _split_segments(word, common, _deletions(common))
        if segments is not None:
            return " ".join(segments), "split"
    return None


def _glued_candidate(
    chunk: str, known: frozenset[str], common: frozenset[str]
) -> tuple[str, str] | None:
    """词元内部夹着标点（`tha!f`、`wv•dow`）时的整词改写。

    只做白名单混淆，且只做「把内部标点去掉之后」的整词替换：`tha!f` → `thaf` → `that`。
    去掉标点后**本身就是已知词**的一律不动（`don't` → `dont` 那种合并没有票面依据，
    合并规则要能证明比原样更像词才允许，本票没做）。
    """
    # 票 #188（D8）：含非 ASCII 字母的词元一律不动 —— `café` 走到这条路径时，
    # 字母提取会把它截成 `caf`（`_WORD_RE` 只认 ASCII），白名单换字 f→t 得到
    # `cat`，再按首尾字母拼回去就成了 `caté` 这种半外文串。词表全 ASCII，这种
    # 词元必判 OOV，整词改写对它不成立（外文词不是 OCR 拼错，宁漏勿误）。
    if any(char.isalpha() and not char.isascii() for char in chunk):
        return None
    letters = "".join(_WORD_RE.findall(chunk))
    if len(letters) < _MIN_WORD_LEN or not letters.islower():
        return None
    if letters in known:
        return None
    hit = _confusion_candidate(letters, known, common)
    if hit is None:
        return None
    first = chunk.index(letters[0])
    last = chunk.rindex(letters[-1]) + 1
    return chunk[:first] + hit + chunk[last:], "confusion"


def _inner_junk_fix(chunk: str, known: frozenset[str]) -> str | None:
    """词内杂符 → 空格：`tell)me` → `tell me`、`Excuse(me` → `Excuse me`。

    判据（顺序即否决条件）：

    1. 只处理「字母 + 杂符 + 字母」的形状（`_INNER_JUNK_RE`），其它行文标点一律不碰；
    2. 换成空格后**每一段都必须是已知词**（`tell` / `me` / `For` / `example`）；
    3. **去掉**杂符拼起来如果是已知词，就不动 —— 那更像「读错一个字母」而不是「丢了空格」
       （实测 `sa•d` 拼起来是 `sad`，不动；`tell)me` 拼起来 `tellme` 不是词，动）。

    为什么值得做：英文里词内合法出现的符号是 `'` `-` `/` `&` `.` 这些，已经排除在
    `_INNER_JUNK` 之外；而 `(` `)` `»` `•` 出现在词中间几乎只能是 OCR 把**空格**读成了符号
    （2026-10-19 用户现场三例：`Can you tell)me`、`Excuse(me`、`For»example` —— 括号与引号
    处原本都是空格）。这条规则只增不减：不满足三条就原样返回 None。
    """
    if _INNER_JUNK_RE.search(chunk) is None:
        return None
    words = _WORD_RE.findall(chunk)
    if len(words) < 2:
        return None
    if "".join(words).lower() in known:  # 拼起来是真词 → 是读错字母，不是丢空格
        return None
    if not all(word.lower() in known for word in words):
        return None
    return _INNER_JUNK_RE.sub(" ", chunk)


def _correct_chunk(
    chunk: str, known: frozenset[str], common: frozenset[str]
) -> tuple[str, list[dict[str, str]]]:
    """一个空白分隔的词元 → (新词元, 改动清单)。"""
    fixes: list[dict[str, str]] = []
    original = chunk
    # §5.3.3：首尾杂符剥掉。只在词元里有字母时做（`->`、`===` 这类纯符号串不是
    # 「带杂符的词」）。`!` 只在尾部按例外处理：它跟在已知词后面多半是真的感叹号
    # （`answer!`），不是幻觉 —— 词部分是 OOV 时才当杂符剥。**已知的残留误伤**：
    # 被引号裹住的词（`“Alas!`）与词内破折号连起来的两个词（`it—thus!`）会被整块
    # 判成 OOV 而剥掉 `!`（#36 的散文语料 19 处，见票面评论；修它要动判据，不在本票）。
    if _WORD_RE.search(chunk):
        while len(chunk) > 1 and chunk[0] in _NEVER_LEGIT:
            chunk = chunk[1:]
        while len(chunk) > 1 and chunk[-1] in _NEVER_LEGIT + "!":
            if chunk[-1] == "!" and chunk[:-1].lower() in known:
                break
            chunk = chunk[:-1]
    if chunk != original:
        fixes.append({"from": original, "to": chunk, "kind": "punct"})

    # 2026-10-19：词内夹着的杂符（`)` `(` `»` …）多半是被读成符号的空格 →
    # 两段都是真词、而拼起来不是真词时，换成空格（见 `_inner_junk_fix`）。
    spaced = _inner_junk_fix(chunk, known)
    if spaced is not None:
        fixes.append({"from": chunk, "to": spaced, "kind": "punct"})
        return spaced, fixes

    if any(char.isdigit() for char in chunk):
        return chunk, fixes  # 含数字的词元整体不碰（硬规则）
    match = re.fullmatch(r"(\W*)([A-Za-z]+)(\W*)", chunk)
    if match is None or len(_WORD_RE.findall(chunk)) != 1:
        fixed = _glued_candidate(chunk, known, common)
        if fixed is None:
            return chunk, fixes
        word, kind = fixed
    else:
        prefix, letters, suffix = match.group(1), match.group(2), match.group(3)
        fixed = _correct_word(letters, known, common)
        if fixed is None:
            return chunk, fixes
        word, kind = fixed
        word = prefix + word + suffix
    fixes.append({"from": chunk, "to": word, "kind": kind})
    return word, fixes


def warm_up(common_words: frozenset[str]) -> None:
    """先把删字符索引建出来（约 5ms）：它不该落在第一次捕获的等待里。

    参数是 **`common`**（2000 常见词）——索引只服务切段的候选查询，而候选最终都要
    落在 common 里（见 `_deletions`）；kernel 装配时传的就是它。
    """
    _deletions(common_words)


def correct(
    text: str,
    known_words: frozenset[str],
    common_words: frozenset[str] | None = None,
) -> tuple[str, list[dict[str, str]]]:
    """`text` → (校正后的文本, 改动清单)。词表为空或文本为空时原样返回。

    `known_words` 是「这是个真词」的集合（`common_words.txt` ∪ `words_10k.txt`），
    `common_words` 是其中的「常见词」子集（缺省时视为同一个集合），只用于候选排序与
    短段的门槛（§5.3 的两份词表各司其职）。
    """
    if not text or not known_words:
        return text, []
    common = common_words if common_words is not None else known_words
    plain, spacing_fixes = _space_after_sentence_punctuation(text)
    pieces: list[str] = []
    fixes: list[dict[str, str]] = list(spacing_fixes)
    for piece in re.split(r"(\s+)", plain):
        if not piece or piece.isspace():
            pieces.append(piece)
            continue
        corrected, chunk_fixes = _correct_chunk(piece, known_words, common)
        fixes.extend(chunk_fixes)
        pieces.append(corrected)
    return "".join(pieces), fixes


# --- #221：字幕 OCR 丢空格的粘连修复 -----------------------------------------
#
# 与上面的 `correct()` 分工不同：`correct()` 服务截图取词，要求切段「总成本恰好 1」
# 以免在通用 prose 上把真词切坏（见 `_split_segments`）；字幕 OCR 的粘连是**纯丢空格**
# （hewas / Thisis / aregoing），按那条规则永远不会被拆。这里单独做一套更保守的
# 精确切段：只认全小写的 OOV 词元、每段都必须是已知词、至少含一个闭类功能词；
# 已知词、专名（含内部大写）、截断词（某个已知词的前缀）一律不动。

_SPACE_FUNCTION_WORDS = frozenset(
    """
    a an the this that these those my your his her its our their me him us them you
    he she it we they i am is are was were be been being do does did have has had
    can could will would shall should may might must to of in on at by for with from
    about into over after before under between through during without against among
    across along around behind below beneath beside beyond near off onto past since
    till toward towards upon within as and but or nor so yet if because although
    though while whereas unless until when where why how what which who whom whose
    not no there here
    """.split()
)
_SPACE_MAX_TOKEN = 24
_SPACE_MAX_SEGMENT = 16
_SPACE_MAX_SEGMENTS = 6
_SPACE_SORTED_CACHE: dict[int, tuple[frozenset[str], list[str]]] = {}


def restore_spaces(
    text: str,
    known_words: frozenset[str],
    common_words: frozenset[str] | None = None,
) -> str:
    """字幕 OCR 的丢空格修复（#221）：`hewas` → `he was`，宁可不改。

    只处理**全小写**（或仅首字母大写）的 OOV 词元；切段必须精确覆盖原词元，不做
    任何字母替换。守卫（任何一条不满足都原样返回）：

    - 词元本身是已知词 / 常见词；
    - 词元是某个已知词的**前缀**（多半是字幕被截断：`priori` / `understandin`）；
    - 没有切出 ≥2 段，或没有至少一个闭类功能词（`redapple` / `gladwell` 不动）；
    - 2 字母的非功能词段、3–4 字母的非常见词段、非首段的单字母段（`priori` 的
      `i`、`conversatior` 的 `ati`）一律不参与。

    标点/缩写/连字符原样保留（只替换 `[A-Za-z]+` 连续段）。纯函数、零 I/O。
    """
    if not text or not known_words:
        return text
    common = common_words if common_words is not None else known_words
    sorted_known = _space_sorted_known(known_words)
    return re.sub(
        r"[A-Za-z]+",
        lambda match: _restore_token(match.group(0), known_words, common, sorted_known),
        text,
    )


def _space_sorted_known(known_words: frozenset[str]) -> list[str]:
    cached = _SPACE_SORTED_CACHE.get(id(known_words))
    if cached is None or cached[0] is not known_words:
        cached = (known_words, sorted(known_words))
        _SPACE_SORTED_CACHE.clear()
        _SPACE_SORTED_CACHE[id(known_words)] = cached
    return cached[1]


def _restore_token(
    token: str,
    known: frozenset[str],
    common: frozenset[str],
    sorted_known: list[str],
) -> str:
    low = token.lower()
    if len(low) < 4 or len(low) > _SPACE_MAX_TOKEN:
        return token
    if any(char.isupper() for char in token[1:]):  # 专名 / 驼峰
        return token
    if low in known or low in common:
        return token
    position = bisect.bisect_left(sorted_known, low)
    if position < len(sorted_known):
        candidate = sorted_known[position]
        if candidate != low and candidate.startswith(low):
            return token  # 截断词（subtitle 边界切掉半句）
    segments = _restore_segments(low, known, common)
    if not segments:
        return token
    if not any(segment in _SPACE_FUNCTION_WORDS for segment in segments):
        return token
    if "".join(segments) != low:
        return token
    if token[0].isupper():
        segments[0] = segments[0].capitalize()
    return " ".join(segments)


def _restore_segments(
    low: str, known: frozenset[str], common: frozenset[str]
) -> list[str] | None:
    length = len(low)
    best: list[list[str] | None] = [None] * (length + 1)
    best[length] = []
    for start in range(length - 1, -1, -1):
        options: list[list[str]] = []
        for size in range(1, min(_SPACE_MAX_SEGMENT, length - start) + 1):
            segment = low[start : start + size]
            if segment not in known and segment not in common:
                continue
            if len(segment) == 1:
                if segment not in ("a", "i") or start != 0:
                    continue
            elif len(segment) == 2 and segment not in _SPACE_FUNCTION_WORDS:
                continue
            elif (
                3 <= len(segment) <= 4
                and segment not in _SPACE_FUNCTION_WORDS
                and segment not in common
            ):
                continue
            tail = best[start + size]
            if tail is None:
                continue
            candidate = [segment, *tail]
            if len(candidate) > _SPACE_MAX_SEGMENTS:
                continue
            options.append(candidate)
        if options:
            options.sort(
                key=lambda parts: (
                    len(parts),
                    -sum(1 for part in parts if part in _SPACE_FUNCTION_WORDS),
                    -len(parts[0]),
                )
            )
            best[start] = options[0]
    result = best[0]
    return result if result is not None and len(result) >= 2 else None


def _space_after_sentence_punctuation(
    text: str,
) -> tuple[str, list[dict[str, str]]]:
    """§5.3.3 后半：句尾 `./,/?` 后紧跟大写字母 → 补一个空格（`money.The` → `money. The`）。"""
    fixes: list[dict[str, str]] = []

    def replace(match: re.Match[str]) -> str:
        fixes.append(
            {
                "from": match.group(0),
                "to": match.group(1) + " " + match.group(2),
                "kind": "punct",
            }
        )
        return match.group(1) + " " + match.group(2)

    return _SENTENCE_GAP_RE.sub(replace, text), fixes


# --- W5（票 #142 / 决策 D1）：疑似粘连「只标记」--------------------------------
#
# 上游不改原句文本，只产出「这个词元像不像 OCR 丢空格」的旁路信号（`suspects`），
# 由外壳决定 Tab 不把粘连串当第一候选并提示用户手动改原句。D1 的实测依据：自动切的
# 收益太小（S1 只有 1/690）、风险不为零，而「标记」零文本误改、能消掉全部坏首候选。
# 保守口径沿用可行性研究 S1（§5.1），另按 A7「干净语料 0 标记」再收紧两处（见
# `_GLUE_FUNCTION_WORDS` 与 `_within_one_of_known` 的注释）。

# 封闭类功能词白名单：切分段要么命中这里，要么 ≥4 字母且属于 common ∪ words_10k，
# 且至少一段要命中这里（spec §W5「如 are/you/to/it/the/and/of/in/on/at/for/with/
# your 等」——票面例词里 with/your/from 是 4 字母，按票面字面清单收全）。**不含**
# 单字母词与 be/over/out：
#   · 单字母：`adame`(a+dame)、`aking`(a+king)、`meanest`(me+a+nest) 这类截断词会
#     被切开（研究 §4.1 的一批真词误伤全有单字母段）——宁可漏报；
#   · `be`/`over`/`out` + 真词是英语的能产构词（besought/betook/overspread），
#     ECDICT 恰好缺这类古语/派生词时必然误标（S1 唯一的新增误伤就是 be+sought，
#     #proto3 实测 over+spread 同理）——同样宁可漏报。
_GLUE_FUNCTION_WORDS = frozenset(
    """an am as at by do go he if in is it me my no of on or so to up us we
    and are but can did for had has her him his its may not our she the was you
    your with from have been will this that what when were does too who why how
    any all nor whom""".split()
)
_SUSPECT_MIN_LEN = 5
_SUSPECT_MAX_LEN = _MAX_TOKEN_LEN  # 与切段同一条 16 字符上限
_SUSPECT_TOKEN_RE = re.compile(r"[A-Za-z]+")  # 完整词元：大写/数字/`-`/`'` 天然出局
_MAX_SUSPECTS = 8  # 单次捕获的标记上限（spec §W5）


def _within_one_of_known(word: str, known: frozenset[str]) -> bool:
    """`word` 与某个已知词的编辑距离 ≤1 吗（替换/插入/删除三向）。

    既有整词守卫 `_differs_once_in_known` 只比同长替换；这里加宽到插入/删除：与已知
    词差一个字符的串更像「读错一个字母」或「截断了一个字母」（`rometheus`→
    `prometheus`、`heatless`→`headless`），不像「丢了空格」。枚举 ≤ 17×26+26×16 次
    集合查询（微秒级），不给 9 万词的 `known` 建索引（#36 的 3.3s/241MiB 教训）。
    """
    for position in range(len(word)):
        for letter in _ALPHABET:
            variant = word[:position] + letter + word[position + 1 :]
            if variant != word and variant in known:
                return True
    for position in range(len(word) + 1):
        for letter in _ALPHABET:
            if word[:position] + letter + word[position:] in known:
                return True
    for position in range(len(word)):
        if word[:position] + word[position + 1 :] in known:
            return True
    return False


def _glue_split(word: str, vocab: frozenset[str]) -> tuple[str, ...] | None:
    """「疑似粘连」的确定性最优精确切分；切不出 ≥2 段回 None。

    切分条件（spec §W5 / 研究 S1 同口径）：每段要么是封闭类功能词
    （`_GLUE_FUNCTION_WORDS`），要么长度 ≥4 且属于 `vocab`；**至少一段是功能词**；
    只做精确匹配、总成本恒为 0 —— 不允许靠改字母凑（那是已否决的自动切）。

    歧义裁决按 spec 的「段数最少 → 最短段最长 → 字典序」：状态存
    `(段数, -最短段长, 段元组)`，元组比较顺序即裁决顺序，`min` 一轮天然确定
    （同一帧两次给同一结果）。回 None 表示「不像粘连」——调用方据此不标记。
    """
    length = len(word)
    # best[(start, 已含功能词段)] → 该状态下的最优切分；空后缀的 -最短段长取负大数，
    # 使「并上第一段」时 `max(-size, prev)` 自然落到 -size。
    best: dict[tuple[int, bool], tuple[int, int, tuple[str, ...]]] = {
        (length, False): (0, -(1 << 30), ())
    }
    for start in range(length - 1, -1, -1):
        for size in range(1, min(_SUSPECT_MAX_LEN, length - start) + 1):
            segment = word[start : start + size]
            if segment in _GLUE_FUNCTION_WORDS:
                is_func = True
            elif size >= _MIN_SEGMENT_LEN and segment in vocab:
                is_func = False
            else:
                continue
            for has_func in (False, True):
                prev = best.get((start + size, has_func))
                if prev is None:
                    continue
                candidate = (1 + prev[0], max(-size, prev[1]), (segment,) + prev[2])
                key = (start, has_func or is_func)
                if key not in best or candidate < best[key]:
                    best[key] = candidate
    result = best.get((0, True))
    if result is None or result[0] < 2:
        return None
    return result[2]


def find_suspects(
    text: str,
    known_words: frozenset[str],
    common_words: frozenset[str],
    vocab_words: frozenset[str] | None = None,
) -> list[dict[str, object]]:
    """校正后文本里「疑似粘连」token 的只读标记（票 #142 / 决策 D1，spec §W5）。

    条件：完整词元纯小写（大写=专名/句首，一律不标）、长度 5–16、不在 `known_words`
    （含 ECDICT 的宽词表——真词一个都不标）、与任何已知词的编辑距离 >1（差一个字符
    的更像读错/截断，见 `_within_one_of_known`）、能按 `_glue_split` 精确切成 ≥2 段
    且至少一段是功能词。含数字/连字符/撇号的词元不进词元流（`_SUSPECT_TOKEN_RE` 只认
    字母段）——连字符/撇号类不在本票范围（研究 §5.4）。

    返回 `[{"token", "start", "end"}]`：偏移相对 `text`、0 起、左闭右开（本轮 token
    均为 ASCII，字符偏移与字节/U16 一致），按出现位置排序，最多 `_MAX_SUSPECTS` 条。
    **不改文本、不进 corrections**；纯文本零 I/O（词表由调用方注入，`vocab_words`
    缺省时视为 `common_words`），同一文本重复调用结果逐项一致。
    """
    if not text or not known_words:
        return []
    vocab = vocab_words if vocab_words is not None else common_words
    suspects: list[dict[str, object]] = []
    for match in _SUSPECT_TOKEN_RE.finditer(text):
        token = match.group(0)
        if not token.islower():
            continue  # 完整词元要纯小写（`Prometheus` 不产出 `rometheus`）
        if not (_SUSPECT_MIN_LEN <= len(token) <= _SUSPECT_MAX_LEN):
            continue
        if token in known_words or _within_one_of_known(token, known_words):
            continue
        if _glue_split(token, vocab) is None:
            continue
        suspects.append({"token": token, "start": match.start(), "end": match.end()})
        if len(suspects) >= _MAX_SUSPECTS:
            break
    return suspects


# --- #222：检测头开启后的标点伪影 / 宽词表可疑门 ------------------------------
# 取证：RapidOCR 的 width_height_ratio=8 会跳过滤镜检测（见 v34 研究报告
# docs/research/2026-11-08-ocr-recognition-issues.md §6.1）。打开检测头后，
# 画面细竖线/接缝会被识别成词内 `/` 或伪撇号（`a/chance`、`time'you`），
# 这里做廉价归一；同时用 106k 宽词表做 OOV 检出，替代只认 2000 常见词的比值门。

_OCR_SLASH_GLUE = re.compile(r"(?<=[A-Za-z])/(?=[A-Za-z])")
_OCR_COMMA_GLUE = re.compile(r"(?<=[A-Za-z]),(?=[A-Za-z])")
_OCR_APOS_TOKEN = re.compile(r"[A-Za-z]+['’][A-Za-z]+")
_APOS_TAILS = frozenset({"s", "t", "re", "ve", "ll", "d", "m"})
_APOS_KEEP = frozenset({"o'clock"})
_CONTRACTION_BASES = {"can": "can", "won": "will", "shan": "shall"}


def clean_ocr_punctuation(text: str) -> str:
    """清掉检测头带来的词内 `/`、逗号丢空格与伪撇号；真缩写/专名不动。"""
    if not text:
        return text
    text = _OCR_SLASH_GLUE.sub(" ", text)
    text = _OCR_COMMA_GLUE.sub(", ", text)

    def _fix(match: re.Match[str]) -> str:
        token = match.group(0)
        low = token.lower().replace("’", "'")
        if low in _APOS_KEEP:
            return token
        if any(ch.isupper() for ch in token[1:].replace("’", "'")):
            return token  # 专名内部大写（O'Brien / McDonald's）不碰
        tail = low.rsplit("'", 1)[1]
        if tail in _APOS_TAILS:
            return token
        if tail.startswith("s") and len(tail) > 1:
            # it'squick / she'shere：保留 's，把丢的空格补在 s 之后
            sep = "’" if "’" in token else "'"
            head = token.rsplit(sep, 1)[0]
            return head + sep + "s " + tail[1:]
        return token.replace("'", " ").replace("’", " ")

    return _OCR_APOS_TOKEN.sub(_fix, text)


_OOV_TOKEN = re.compile(r"[A-Za-z]+(?:['’][A-Za-z]+)?")


def _word_forms(token: str) -> list[str]:
    """一个词元的「可接受写法」：缩写还原 + 连字符拆段（`don't` → do/not）。"""
    low = token.lower().replace("’", "'")
    forms = [low]
    if "'" in low:
        base, tail = low.rsplit("'", 1)
        if tail == "t" and base.endswith("n"):
            stem = base[:-1]
            forms.append(_CONTRACTION_BASES.get(base, stem))
            forms.append("not")
        elif tail in _APOS_TAILS:
            forms.append(base)  # it's / they're / I'm / we've / he'd / John's
        else:
            forms.extend([base, tail])  # time'you → time / you
    if "-" in low:
        forms.extend(part for part in low.split("-") if part)
    return forms


def oov_tokens(text: str, known_words: frozenset[str]) -> list[str]:
    """返回不在已知词表里的词元（缩写先归一；纯函数、零 I/O）。"""
    if not text or not known_words:
        return []
    out: list[str] = []
    for token in _OOV_TOKEN.findall(text):
        if any(form and form in known_words for form in _word_forms(token)):
            continue
        out.append(token)
    return out


def looks_suspicious(text: str, known_words: frozenset[str]) -> bool:
    """宽词表 OOV 检出：任一真词元不在词表 → 可疑（比命中率比值门更敏感）。"""
    return bool(oov_tokens(text, known_words))
