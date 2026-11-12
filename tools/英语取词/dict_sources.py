"""词典取词链（v18）：`dict_cache` → 本地 ECDICT → 在线（有道 jsonapi_s → Edge → transmart）。

要点（v18 spec `docs/specs/v18-dict-chain-upgrade.md` §W2/§W3 + 决策 D1/D2/D4/D5）：
- 只用标准库：`urllib.request`（请求构造）+ `http.client`（keep-alive 传输）+ `sqlite3`；
- 统一返回结构 `{"ok","word","phonetic","cn","en","pos","tags","source","cached","ms"}`；
- **超时按源分**：有道 1200ms / Edge 1500ms / transmart 1000ms / 单次 lookup 总预算 2500ms，
  超时回 `{"ok":false,"error":"timeout"}`，绝不影响保存；
- **在线链（v18）**：有道 `jsonapi_s`（ec 词典释义 / web_trans 网络释义 / blng_sents 双语例句 /
  fanyi 整句机翻，按字段存在性宽松取值，miss 回落下一级）→ Edge translatetext → transmart
  `/api/imt`（两个 MT 引擎互为降级；**仅对含空格查询启用**，`phrase_only`，单词不值一次 MT 请求）；
  dictionaryapi.dev 已按 D2 移除（无中文、大陆 530、短语 404）；
- **连接复用（D5）**：按主机 thread-local 缓存 `http.client` 连接，失败重建一次再判错；
- **碎片止血（D4）**：含空格且末 token 不是完整词（在本地 ECDICT 中存在才算完整词）的打字
  中间态只查 `dict_cache` 正缓存、跳过全部在线、不写负缓存；ECDICT 缺库时不启用（宁漏勿误）；
- `dict_online=0` 时**绝不发网络请求**（`online_enabled=False` 时在线源整段不执行）；
- ECDICT 的 schema 不由 hub 定：表名/列名用 `PRAGMA table_info` 宽松探测（`probe_layout`）。

缓存的写入范围（§7「所有结果写回 dict_cache」+ 负结果缓存 30 天，R1 §8.3）：
负结果只写「**已试过的 provider 集合 ⊆ 当前启用的 provider 集合**」的那些（即所有
启用源都明确答了「没这个词」，网络错/超时不算）；`source` 记成 `miss:<已试集合>`
（`negative_source` / `negative_covers`），读侧只有「当前启用集合 ⊆ 已试集合」才认。
这样 `dict_online=0` 或本地库导入之前写下的 `null`，不会在 30 天里骗用户「查不到」。

音标归一（票 #35，`normalize_phonetic` / `display_phonetic`）：ECDICT 是旧式 ASCII
音标（`ri'lʌktәnt`），归一放在**这一层**（`DictService.lookup` 的出口），外壳与 web
都直接读 `phonetic` 字段，各自不需要实现。**`dict_cache` 里存原始 provider 文本**
（`_payload` 不归一：ECDICT 存 `ri'lʌktәnt`、有道存 `rɪˈlʌktənt`），展示前才归一
—— 以后改归一规则不用清缓存（缓存要留 30 天），代价只是每次 lookup 多一次字符替换。

多词目标（v22 P5 / #169，spec §11）：`DictService.lookup(..., dict_only=True)` 只走
「整条短语词典查询」—— 源白名单 = 本地 ECDICT + 有道 `ec` 词条 + web_trans 里 key 与
查询完全相等的词条，并旁路 `dict_cache`（v18 缓存里可能躺着上述机翻错义）。

**v22 验收后修复（票 #172，2026-10-27）**：严格词典未命中时**回退机翻**，回包带
`machine: true` + 译文（`cn`）+ `source`，外壳据此标注「机翻」（不再把机翻伪装成词典义）。
机翻复用 v18 既有 MT 实现（有道 `fanyi` / Edge / transmart），不引新依赖：
- `dict_only` 路径：词典白名单全部未命中后才查 MT，仍然**不读不写** `dict_cache`，
  总预算沿用单次 `lookup` 的 2500ms（`_lookup_machine_fallback`）；
- 默认路径（`dict_only=false`）：词典优先的阶梯不变，命中来自 MT 引擎（有道 `fanyi`、
  Edge、transmart）时同样标 `machine:true`；**机翻结果一律不写 `dict_cache`**
  （避免机翻错义又被当成 30 天的「词典命中」）。读旧缓存时按 `source` 识别
  edge/transmart 的机翻条目，同样标 `machine:true`。
离线 / 超时 / 未收录仍回 `{"ok":false,"error":"not found"}`，不影响保存。
"""

from __future__ import annotations

import http.client
import json
import logging
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger("hub.dict")

DEFAULT_TIMEOUT_MS = 2500  # v18：单次 lookup 总预算不变（有道 p99 + 两级 MT 兜底余量）
YOUDAO_TIMEOUT_MS = 1200  # R1 实测 p99=984ms，留 20% 余量
EDGE_TIMEOUT_MS = 1500  # 调研实测 0.47s 中位，留余量
TRANSMART_TIMEOUT_MS = 1000  # 调研实测 0.15s 中位，全场最快，留余量
USER_AGENT = "english-capture-hub/2.0 (+local, single user)"
MAX_RESPONSE_BYTES = 512 * 1024
YOUDAO_URL = "https://dict.youdao.com/jsonapi_s?doctype=json&jsonversion=4&q={word}&le=eng"
EDGE_URL = (
    "https://edge.microsoft.com/translate/translatetext"
    "?to=zh-Hans&isEnterpriseClient=false"
)
TRANSMART_URL = "https://transmart.qq.com/api/imt"
TRANSMART_CLIENT_KEY = "browser-chromium-131.0(x86-windows)"
NEGATIVE_PREFIX = "miss:"  # 负结果 `source` 前缀：`miss:ecdict,youdao,edge,transmart`
# v22 修复（#172）：纯机翻源的 `source` 名。`dict_cache` 不存新机翻结果，但 v18 缓存里
# 可能已有 edge/transmart 的条目 —— 读旧缓存时按 `source` 认出来，回包补 `machine:true`。
MACHINE_SOURCES = frozenset({"edge", "transmart"})

# `pos` 列的单字母码表 —— 照 ECDICT 源码 `stardict.py` 的 `DictHelper._pos`：
# a=代词, c=连词, d=限定词, i=介词, j=形容词, m=数词, n=名词, p=代词, r=副词,
# u=感叹词, t=不定式标记, v=动词, x=否定标记。
# **`a` 不是形容词（形容词是 `j`）**；中文释义行首的 `a.` 才是形容词，两套码别混。
POS_CODES = {
    "a": "pron",
    "c": "conj",
    "d": "det",
    "i": "prep",
    "j": "adj",
    "m": "num",
    "n": "n",
    "p": "pron",
    "r": "adv",
    "u": "int",
    "t": "inf",
    "v": "v",
    "x": "neg",
}

# 词性标记白名单：ECDICT 的 `translation` 与有道的 `trs` 都是「n. 释义」这种行格式
_POS_MARKERS = {
    "n",
    "v",
    "vt",
    "vi",
    "aux",
    "adj",
    "a",
    "ad",
    "adv",
    "prep",
    "pron",
    "conj",
    "art",
    "num",
    "int",
    "interj",
    "pl",
    "abbr",
    "prefix",
    "suffix",
}
_POS_RE = re.compile(r"^(?P<p>[a-z]{1,6})\.\s*(?P<defs>.+)$")
# 中文词典行首的 `a.` / `ad.` 是形容词 / 副词（与 ECDICT `pos` 列的码表是**两套**：
# 那边 `a` 是代词、形容词是 `j`）。这里统一成 `adj`/`adv`，免得两套码在 `pos[].p` 里撞车。
_POS_ALIASES = {"a": "adj", "ad": "adv"}


def stripword(word: str) -> str:
    """ECDICT 自己的 `stripword`（`stardict.py`）：只留字母数字，转小写。

    必须用 `str.isalnum()`（Unicode 感知）而不是 `[^A-Za-z0-9]` 正则 —— 后者会把
    `ā` / `é` 这类字母也干掉，与 release 库里的 `sw` 对不上（R1 §8.4）。
    """
    return "".join(ch for ch in word if ch.isalnum()).lower()


def decode_pos(value: str) -> list[dict[str, Any]]:
    """ECDICT `pos` 列的单字母码 → §7 的 pos 结构。

    数据形如 `v:4/n:96`（字母:语料占比）；这里只取字母、按出现顺序去重，占比是
    词典内部的统计，§7 的统一结构没给它留位置。未知字母原样透传（不猜）。
    """
    decoded: list[dict[str, Any]] = []
    seen: set[str] = set()
    for chunk in re.split(r"[/,;\s]+", value):
        code = chunk.split(":", 1)[0].strip().lower()
        if not code or code in seen:
            continue
        seen.add(code)
        decoded.append({"p": POS_CODES.get(code, code), "defs": []})
    return decoded


def is_phrase(word: str) -> bool:
    """含空白的查询（短语/短句）—— MT 兜底只对这类查询启用（v18 W3a）。"""
    return any(ch.isspace() for ch in word.strip())


# v34 T1（票 #214 / spec D27）：查询前剥掉目标末尾的英文标点；展示仍用原文。
# 词典源对标点敏感（ECDICT：`everyone` 命中、`everyone,` 不命中），而挑词落点
# 常常是句末带标点的词组/短句 —— 不剥就必然空释义。
_TAIL_PUNCTUATION = ".,!?;:\"'"


def strip_tail_punctuation(word: str) -> str:
    """去掉目标末尾的 `.,!?;:"'`（v34 T1 / spec D27）。

    只剥**尾部**（可连续多个，如 `?!` / `."`），不动中间标点与大小写；
    全是标点的目标原样返回（不能剥成空串去打查询）。
    """
    text = str(word or "").strip()
    stripped = text.rstrip(_TAIL_PUNCTUATION).rstrip()
    return stripped or text


def phrase_key(value: str) -> str:
    """短语匹配键（v22 P5 / #169）：折叠空白 + 小写。

    `web_trans` 的 `key` 只有归一后与查询**完全相等**才算词典命中；key 是别的长句
    （`Whether the weather is good`）的网络释义不算 —— 那正是 `whether the` 错义的来源。
    """
    return " ".join(str(value or "").split()).lower()


def apply_pos_codes(
    groups: list[dict[str, Any]], codes: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """中文释义行首**没写**词性时，用 `pos` 列的码表兜底。

    行首写了词性就以中文那套为准 —— `a.` 是形容词、而码表里 `a` 是代词，两套码
    不能混在一份 `pos` 里（R1 §8.7）。码表只有语料统计、没有逐义项，所以第一个码
    挂到第一段释义上，其余码 `defs` 留空。
    """
    if not codes:
        return groups
    if not groups:
        return codes
    if any(group["p"] for group in groups):
        return groups
    return [{**groups[0], "p": codes[0]["p"]}, *groups[1:], *codes[1:]]


def negative_source(tried: Iterable[str]) -> str:
    """负结果 `source`：`miss:ecdict,youdao`（已试过的 provider 集合，排序去重）。"""
    return NEGATIVE_PREFIX + ",".join(sorted({name for name in tried if name}))


def negative_covers(source: str, enabled: Iterable[str]) -> bool:
    """负结果是否覆盖得住「当前启用的 provider 集合」（R1 §8.3 的命中规则）。

    没有 provider 集合的老格式（如 `source='negative'`）一律不认 —— 重新查一次，
    比拿一条来路不明的 `null` 骗用户 30 天便宜。
    """
    if not source.startswith(NEGATIVE_PREFIX):
        return False
    tried = {name.strip() for name in source[len(NEGATIVE_PREFIX) :].split(",")}
    return {name for name in enabled if name} <= (tried - {""})


class ProviderError(Exception):
    """在线源出错（超时 / HTTP / 解析 / 网络）。`kind` 进响应与日志（只记错误码，不记词）。"""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


class NotFound(Exception):
    """在线源**明确**答「没这个词」（HTTP 404），与「出错」区分开。"""


class WordMismatch(ProviderError):
    """在线源回了一条**别的词**的词条（回包的词头与查询对不上）。

    v22 修复（#174）：有道 `jsonapi_s` 会把别的词条回给某些查询 —— 实测
    `q=lamb` 回了 `injection` / `mush` / `timidity` 的词条，`q=chick` 回了
    `Nebuchadnezzar`。这是**源侧故障**，不是「没收录」：
    - 必须按 `ProviderError` 走（进 `errors`），否则主路径会把「有词但答错」
      写成 30 天负缓存，把这个词压死；
    - 外壳收到 `error="word-mismatch"` 会显示「词典未返回，可手填」，而不是
      把错义填进释义框（fail-closed）。
    """

    def __init__(self) -> None:
        super().__init__("word-mismatch")


# 票 #182（D1）：`errors` 元素 → 补释义 reason 契约档（前端 T9 按此映射，不许偏离）。
# `errors` 里放的是 `ProviderError.kind`（与档位一一对应）或 `_ask` 兜底记下的意外
# 异常类名（KeyError 等）——后者几乎都出自回包解析层，统一归 `parse`，内部类名
# 绝不漏进回包。契约全集：network/parse/http/word-mismatch + timeout/offline/not found。
_REASON_BY_ERROR = {
    "network": "network",
    "parse": "parse",
    "http": "http",
    "timeout": "timeout",
    "word-mismatch": "word-mismatch",
}


def _refill_reason(errors: list[str]) -> str:
    """首个错误映射成 reason 契约档；无错误 = not found（票 #182 D1）。

    补释义队列据此报「网络失败/解析失败」而不是误导性的「词典未收录」；
    `error` 字段形状不变（外壳只读它）。
    """
    if not errors:
        return "not found"
    return _REASON_BY_ERROR.get(errors[0], "parse")


class Provider(Protocol):
    name: str
    timeout_ms: int
    skip_phrases: bool

    def lookup(self, word: str, timeout: float) -> dict[str, Any] | None: ...


# `phrase_only` 是通用能力字段（v18 W3a）：True = 仅对含空格查询启用（MT 兜底）。
# `skip_phrases` 语义不变（短语跳过）；两者都用 `getattr` 读，不强制 provider 声明。


# --- 统一的释义整理 ----------------------------------------------------------


def group_pos(lines: Iterable[str]) -> tuple[str, list[dict[str, Any]]]:
    """`["adj. 不情愿的", "v. 不愿意"]` → (`"不情愿的；不愿意"`, `[{"p","defs"}…]`)。"""
    glosses: list[str] = []
    groups: list[dict[str, Any]] = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        match = _POS_RE.match(line)
        if match and match.group("p") in _POS_MARKERS:
            part = _POS_ALIASES.get(match.group("p"), match.group("p"))
            text = match.group("defs").strip()
        else:
            part, text = "", line
        if not text:
            continue
        glosses.append(text)
        if groups and groups[-1]["p"] == part:
            groups[-1]["defs"].append(text)
        else:
            groups.append({"p": part, "defs": [text]})
    return "；".join(glosses), groups


def _lines(value: Any) -> list[str]:
    if not isinstance(value, str):
        return []
    return [line.strip() for line in value.splitlines() if line.strip()]


def _texts(value: Any, limit: int = 3) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, Sequence):
        return [str(item).strip() for item in value if str(item).strip()][:limit]
    return []


# --- 音标归一（ECDICT 的旧式 ASCII 音标 → 可读 IPA，票 #35）---------------------
# 规则来源：真库 `ecdict.csv` 的 `phonetic` 列逐字符盘点（218,065 行有音标，
# 2026-10-13，见 `docs/research/v2-dictionary.md` §12）。下面每个映射后面的数字是
# 那次盘点的出现次数，样本是真实词条 —— 不猜字符含义，只知己知彼地替换：
#   ә U+04D9 西里尔 schwa ×150,662 → ə（`ә'kaunt` → `əˈkaunt`）
#   ' 撇号 ×187,694 → ˈ 主重音（`ri'lʌktәnt` → `riˈlʌktənt`）
#   ˊ U+02CA ×1,348 / ´ U+00B4 / ʹ U+02B9 → ˈ（`ˊepisentә` → `ˈepisentə`）
#   , ×50,696 与 . ×7,230 → ˌ 次重音（`ә.pɒθi'әusis` → `əˌpɒθiˈəusis`）
#   : 在字母后 ×89,336 → ː 长音符（`'ɔ:lbɔ:^` → `ˈɔːlbɔːɡ`）
#   `a:` ×43 → `ɑː`（票面点名的这条；真库那 43 行都是 RP /ɑː/ 词：`'ʃa:p` Sharpe、
#       `a:'ma:` Armagh。**只对 `a:` 这样映射**，`ә:`（5,324 行）按字面给 `əː` ——
#       它混了 /ɜː/ 与 /ɔː/（`әb'sә:d` absurd 是 /ɜː/，`'bə:nməθ` Bournemouth 是 /ɔː/），
#       改元音字母会引入新错误，票面也没要求）
#   ^ ×11,050 → ɡ（老 ASCII 拿 `^` 当 script g：`'^ædait` → `ˈɡædait`）
#   \ 连写（1~4 个）→ ɜ（CSV 里会出现 `\\`/`\\\\`：`'\\:θwәdz` → `ˈɜːθwədz`）
#   є U+0454 ×607 与 ε U+03B5 ×195 → e（`bi'wєә` → `biˈweə`、`əu'vεən` → `əuˈveən`）
#   [ ] ×21/15 是变体标记不是音标括号 → 去掉，展示层另外补 `/.../`
#   `(?@)` ×47 是 ECDICT 的变体占位符 → 去掉
# 已经是干净 IPA 的字符（ə æ ɔ ɪ ʃ ʌ ɑ ŋ ʒ ɒ θ ʊ ˌ ɡ ː ɜ ð ɚ ɵ ɝ ɹ）一个都不动，
# 所以对有道的 `rɪˈlʌktənt` 归一等于没做（幂等）；元音本身不重写（`ri'lʌktәnt`
# 只变成 `riˈlʌktənt`，不擅自改成 `rɪˈlʌktənt` —— 那是编数据不是归一）。
_PHONETIC_CHARS = str.maketrans(
    {
        "\u04d9": "\u0259",  # ә → ə
        "'": "\u02c8",  # ' → ˈ
        "\u02ca": "\u02c8",  # ˊ → ˈ
        "\u00b4": "\u02c8",  # ´ → ˈ
        "\u02b9": "\u02c8",  # ʹ → ˈ
        ",": "\u02cc",  # , → ˌ（次重音）
        ".": "\u02cc",  # . → ˌ（次重音）
        "^": "\u0261",  # ^ → ɡ
        "\u0454": "e",  # є → e
        "\u03b5": "e",  # ε → e
        "[": "",
        "]": "",
    }
)
_PHONETIC_JUNK_RE = re.compile(r"\(\?@\)\s*")
_PHONETIC_BACKSLASH_RE = re.compile(r"\\+")
_PHONETIC_LONG_A_RE = re.compile(r"a:")  # 老拼法的 /ɑː/（票面点名），先于通用长音符规则
_PHONETIC_LENGTH_RE = re.compile(r"([^\W\d_]):")  # 字母后的 `:` → 长音符
# 含 CJK/全角字符的「音标」是脏数据（实测 20 来行，如 `'英 [bəˈru:k] 美 […]'`）：
# 这种串不猜、原样返回，只补外围 `/.../`（AC：异常串安全退化，绝不抛）。
_PHONETIC_ANOMALY_RE = re.compile(r"[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]")


def normalize_phonetic(value: str) -> str:
    """旧式 ASCII 音标 → 可读 IPA（**纯字符归一，不负责外围斜杠**）。

    - 空串/空白 → `""`；异常串（含 CJK）原样返回；任何输入都不抛异常；
    - 幂等：已经归一过的串再跑一次结果不变（有道的干净 IPA 是恒等变换）；
    - 只看字符、不「翻译」元音（见上面 `_PHONETIC_CHARS` 的注释）。
    """
    text = (value or "").strip()
    if not text:
        return ""
    text = _PHONETIC_JUNK_RE.sub("", text)
    if _PHONETIC_ANOMALY_RE.search(text):
        return text
    text = text.translate(_PHONETIC_CHARS)
    text = _PHONETIC_BACKSLASH_RE.sub("\u025c", text)
    text = _PHONETIC_LONG_A_RE.sub("\u0251\u02d0", text)
    text = _PHONETIC_LENGTH_RE.sub(lambda match: match.group(1) + "\u02d0", text)
    return text.strip()


def display_phonetic(value: str) -> str:
    """展示用音标：归一 + 外围补 `/.../`（`DictService.lookup` 的出口调用）。

    已经是 `/…/` 的（有道/dapi 原生形态、老缓存）只归一、不再套一层斜杠。
    `dict_cache` 存的是归一**前**的原文，所以这里每次 lookup 都要跑一遍（字符替换，
    成本可忽略；换来的是「改规则不用清 30 天的缓存」）。
    """
    text = normalize_phonetic(value)
    if not text:
        return ""
    if text.startswith(("/", "[")):
        return text
    return f"/{text}/"


# --- 在线源 ------------------------------------------------------------------

# 连接复用（D5）：按 (scheme, host, port) thread-local 缓存 `http.client` 连接。
# hub RPC 一连接一线程，thread-local 天然无锁；keep-alive 失效（服务端关了空闲连接）
# 由「失败重建一次」兜住，调用方无感。
_TLS = threading.local()


def _connection_key(url: str) -> tuple[str, str, int]:
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme or "https"
    port = parts.port or (443 if scheme == "https" else 80)
    return scheme, parts.hostname or "", port


def _cached_connection(key: tuple[str, str, int], timeout: float) -> http.client.HTTPConnection:
    cache = getattr(_TLS, "dict_connections", None)
    if cache is None:
        cache = _TLS.dict_connections = {}
    connection = cache.get(key)
    if connection is not None and connection.sock is None:
        # 票 #188（D4）：缓存里的连接已经没有 socket（服务端 `Connection: close` 会让
        # http.client 在读完回包后自动 `close()`——只关 socket、不摘缓存项）。对象上
        # 还是**建连时**的旧 timeout，下一次 `request()` 会按它重连，本请求的剩余预算
        # 形同虚设。丢掉重建：新对象的 timeout = 本次调用传入的（剩余）预算。
        cache.pop(key, None)
        connection = None
    if connection is None:
        cls = (
            http.client.HTTPSConnection
            if key[0] == "https"
            else http.client.HTTPConnection
        )
        connection = cls(key[1], key[2], timeout=timeout)
        cache[key] = connection
    return connection


def _drop_connection(key: tuple[str, str, int]) -> None:
    cache = getattr(_TLS, "dict_connections", None)
    connection = cache.pop(key, None) if cache else None
    if connection is not None:
        connection.close()


def urlopen_call(request: urllib.request.Request, timeout: float) -> Any:
    """薄封装：keep-alive 传输（v18 D5），在**调用时**解析属性，测试才能 monkeypatch
    本函数钉住「零请求」/「请求构造」。

    请求构造仍走 `urllib.request.Request`（URL/header/body 的单一来源），只有传输层
    换成按主机复用的 `http.client` 连接：失败（连接被服务端关掉、重置等）丢缓存重建
    **一次**再判错；超时不重试（重试会把预算翻倍，超时语义必须不变）。
    """
    key = _connection_key(request.full_url)
    parts = urllib.parse.urlsplit(request.full_url)
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    body = request.data if request.data is not None else None
    method = request.get_method()
    headers = dict(request.header_items())
    for attempt in (1, 2):
        connection = _cached_connection(key, timeout)
        try:
            if connection.sock is not None:
                connection.sock.settimeout(timeout)  # 复用连接沿用本次预算
            connection.request(method, path, body=body, headers=headers)
            return connection.getresponse()
        except (http.client.HTTPException, OSError) as exc:
            _drop_connection(key)
            if isinstance(exc, TimeoutError) or attempt == 2:
                raise
    raise AssertionError("unreachable")  # pragma: no cover - for 循环必有 return/raise


def _fetch(request: urllib.request.Request, timeout: float, opener: Callable[..., Any]) -> bytes:
    """发请求、把传输层异常归一到 ProviderError/NotFound，返回原始字节。"""
    key = _connection_key(request.full_url)
    try:
        with opener(request, timeout) as response:
            status = int(getattr(response, "status", 0) or 0)
            if status >= 400:
                # 票 #182（D2）keep-alive 卫生：错误响应的 body 没读，连接上留着
                # 残留字节，复用会让下一次请求读到一半 HTTP 头（BadStatusLine）。
                # 丢掉缓存连接、下一次重建，不再毒化线程级缓存。
                _drop_connection(key)
                if status == 404:
                    raise NotFound() from None
                raise ProviderError("http") from None
            data = response.read(MAX_RESPONSE_BYTES)
            if len(data) >= MAX_RESPONSE_BYTES:
                # 票 #182（D2）：读满上限 = 可能截断 —— 连接上可能还有残留字节，
                # 同样不能复用（截断的 JSON 照旧按 parse 报，只修连接卫生）。
                _drop_connection(key)
            return data
    except urllib.error.HTTPError as exc:  # 假 opener 可能抛老式异常，映射保持不变
        if exc.code == 404:
            raise NotFound() from None
        raise ProviderError("http") from None
    except TimeoutError:
        raise ProviderError("timeout") from None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise ProviderError("timeout") from None
        raise ProviderError("network") from None
    except (http.client.HTTPException, OSError):
        raise ProviderError("network") from None


def _decode(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        raise ProviderError("parse") from None


def _json_headers() -> dict[str, str]:
    return {"User-Agent": USER_AGENT, "Accept": "application/json"}


def _get_json(url: str, timeout: float, opener: Callable[..., Any]) -> Any:
    request = urllib.request.Request(url, headers=_json_headers())
    return _decode(_fetch(request, timeout, opener))


def _post_json(
    url: str, body: bytes, timeout: float, opener: Callable[..., Any]
) -> Any:
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={**_json_headers(), "Content-Type": "application/json"},
    )
    return _decode(_fetch(request, timeout, opener))


class YoudaoProvider:
    """有道 `dict.youdao.com/jsonapi_s`（v18 W2/D1：词典 + 翻译双形态，实测 0.08s）。

    票 #174「换查询串重查」：`jsonapi_s` 的坏缓存在服务端是**按原始 q 字符串**做键的
    （实测 `q=lamb` 恒错、`q=Lamb` 恒对，同一个词每次结果一致；加头/cookie/POST 全都
    无效），所以对「回包答了别的词」只有一条路：换一个**等价**的查询串再问一次。

    形态按「保留原串、只改不影响语义的部分」排序，且**首个永远是原串**（正常情况零代价）：
    1. 原串；
    2. 首字母大写（`lamb` → `Lamb`）—— 实测把 12% 的错词救回，且不改语义；
    3. 全大写 —— 第三道，仍等价于同一个词。
    """

    name = "youdao"
    timeout_ms = YOUDAO_TIMEOUT_MS
    skip_phrases = False

    def __init__(
        self,
        template: str = YOUDAO_URL,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self.template = template
        # 默认 None → 调用时才解析模块级 `urlopen_call`，monkeypatch 才能生效
        self.opener = opener

    def lookup(self, word: str, timeout: float) -> dict[str, Any] | None:
        data = self._fetch_gated(word, timeout, reply_matches_query)
        return parse_youdao(data, word)

    def lookup_dictionary(self, word: str, timeout: float) -> dict[str, Any] | None:
        """v22 P5（#169）：多词目标的「整条短语词典查询」—— 不落 fanyi 机翻。

        只在 `DictService.lookup(..., dict_only=True)` 这条路径上被调用（见
        `parse_youdao_dictionary`）；默认的单段链仍走 `lookup`，行为不变。
        """
        data = self._fetch_gated(word, timeout, reply_echoes_query)
        return parse_youdao_dictionary(data, word)

    def lookup_machine(self, word: str, timeout: float) -> dict[str, Any] | None:
        """v22 修复（#172）：严格词典未命中后的机翻兜底 —— 复用同一个 `jsonapi_s`。

        只取 `fanyi` 整句翻译（`parse_youdao_translation`）；词典条目 / web_trans 都不看，
        由调用方在词典查询全部 miss 之后才走到这里。响应里的 `machine:true` 由
        `DictService` 统一补，与 Edge / transmart 两个 MT 引擎同口径。
        """
        data = self._fetch_gated(word, timeout, reply_matches_query)
        return parse_youdao_translation(data, word)

    def _fetch_gated(
        self,
        word: str,
        timeout: float,
        matches: Callable[[dict[str, Any], str], bool],
    ) -> Any:
        """取一份**自报身份是 `word`** 的回包；所有形态都答别的词 → `WordMismatch`。

        只有「答错词」才换串重查：网络错 / HTTP 错 / 解析错原样抛出（换串也没用，
        白花预算）。非 dict 的畸形回包（合法 JSON 但不是词典结构）也直接交出去，
        由解析层按 `parse` 报错 —— 它没有身份字段，文字上不属于「答错词」。

        R2（#174 收尾）：`matches` 可由调用方注入 —— dict_only 路径传
        `reply_echoes_query`（近似短语的正常命中），普通路径用严格的 `reply_matches_query`。
        R3：三变体重试有**总预算**（`timeout`），超预算即停，不白花。
        """
        deadline = time.perf_counter() + timeout
        last: WordMismatch | None = None
        for query in query_variants(word):
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                raise ProviderError("timeout")
            data = _get_json(
                self.template.format(word=urllib.parse.quote(query)),
                # 每次只能用**剩余预算**：否则最后一发还能跑满一个完整 timeout，
                # 3 变体最坏就是 2× 总预算（实测 2.70s > 2.4s）。
                min(timeout, remaining),
                self.opener or urlopen_call,
            )
            if not isinstance(data, dict) or matches(data, word):
                return data
            last = WordMismatch()
            log.warning(
                "词典源 %s 回错了词（查询 %d 字 / 回包 %d 字），换查询串重查",
                self.name,
                len(query),
                len(str(data.get("input") or "")),
            )
        raise last or WordMismatch()


def query_variants(word: str) -> list[str]:
    """同一个词的等价查询串（见 `YoudaoProvider` 的类注释）。

    去重且保留顺序：原串永远第一个 —— 正常情况只发一次请求，行为与 v18 逐字相同。
    只有含字母的串才有大小写变体（纯数字/符号如 `2,000`、`R&D` 原样；`R&D` 全大写
    再变也没意义，`capitalize()` 与 `upper()` 会重成一条）。
    """
    base = word.strip()
    if not base:
        return [word]
    variants = [base]
    if any(ch.isalpha() for ch in base):
        for candidate in (base[:1].upper() + base[1:], base.upper()):
            if candidate not in variants:
                variants.append(candidate)
    return variants


def _dict_of(value: Any) -> dict[str, Any]:
    """`value` 是 dict 就原样返回，否则空 dict（在线 JSON 结构处处可能缺键/变形）。"""
    return value if isinstance(value, dict) else {}


def _list_of(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")
_TRAILING_COMMA_RE = re.compile(r"[,，\s]+$")


def strip_tags(value: str) -> str:
    """web_trans 摘要 / 双语例句里的 `<b>` 等标记是网页残留，剥掉（v18 W2）。"""
    return _SPACE_RE.sub(" ", _TAG_RE.sub("", value)).strip()


def _ec_entries(ec: dict[str, Any]) -> list[dict[str, Any]]:
    """`ec.word`：jsonapi_s 实测是 dict（词条对象），旧 jsonapi 是 list —— 两种都收。"""
    word = ec.get("word")
    if isinstance(word, dict):
        return [word]
    return [entry for entry in _list_of(word) if isinstance(entry, dict)]


def _return_phrase(entry: dict[str, Any]) -> str:
    """词条声明的词头。`jsonapi_s` 给 dict（`{"l": {"i": "lamb"}}`），旧 `jsonapi`
    给 str —— 两种都要认。

    只认这两形，不 `str(dict)` 硬拷：`str({"l": {"i": "lamb"}})` 是
    `"{'l': {'i': 'lamb'}}"`，拿去和查询比必然不等（换端点时会把 100% 单词判成 miss）。
    """
    raw = entry.get("return-phrase")
    if isinstance(raw, dict):
        inner = raw.get("l")
        if isinstance(inner, dict):
            return str(inner.get("i") or "").strip()
        return str(inner or "").strip()
    return str(raw or "").strip()


def reply_echo(data: dict[str, Any]) -> str:
    """回包自报的**查询串回声**（`input` 字段）—— 它是不是在回答我这次查询的直接证据。

    有道对每个查询都回显 `input`；污染回包的回显就是**别人的词**（`q=lamb` → `mush`）。
    """
    text = data.get("input")
    return text.strip() if isinstance(text, str) else ""


def reply_headwords(data: dict[str, Any]) -> list[str]:
    """回包**自报的身份**：`input`（查询串回声）+ 每个 `ec` 词条的词头。

    有道 `jsonapi_s` 实测会对某些查询回**完全不相干的词条**（`q=lamb` → `mush`、
    `q=chick` → `Nebuchadnezzar`），身份就写在这两个字段里 —— 它们是唯一能判断
    「这条释义是不是我查的那个词」的证据。
    """
    heads: list[str] = []
    echo = reply_echo(data)
    if echo:
        heads.append(echo)
    for entry in _ec_entries(_dict_of(data.get("ec"))):
        phrase = _return_phrase(entry)
        if phrase:
            heads.append(phrase)
    return heads


def reply_matches_query(data: dict[str, Any], word: str) -> bool:
    """回包是不是在回答 `word`（回包没声明身份则不否决）。

    判据优先级：**`input` 回声优先**（它是查询串的直接回显，权威）；没有 `input` 时
    才看 `ec` 词头。`phrase_key`（折空白 + 小写）而不是逐字相等：`Take-off` 的词头是
    `take-off`、`  lamb  ` 回包 `lamb`、`LAMB` 回包 `lamb` —— 这些都不该判成不符。

    没有**任何**身份字段（老 fixture / 端点结构变化）→ 返回 True，不凭猜测否决。
    """
    key = phrase_key(word)
    echo = reply_echo(data)
    if echo:
        return phrase_key(echo) == key
    heads = reply_headwords(data)
    if not heads:
        return True
    return any(phrase_key(head) == key for head in heads)


def reply_echoes_query(data: dict[str, Any], word: str) -> bool:
    """回包是不是**这次查询**的回包（只看 `input` 回声，缺失即不否决）。

    与 `reply_matches_query` 的差别：这里只判「是不是答了别人」，不看 `ec` 词头。
    `dict_only` 路径需要它 —— 有道对近似短语会回一个词头不同的 `ec` 词条
    （`q=give up` → 词头 `give up on`），那是**正常的近似命中**，应当跳过 `ec`
    继续走 web_trans 精确 key，而不是判成源故障。
    """
    echo = reply_echo(data)
    return not echo or phrase_key(echo) == phrase_key(word)


# 缓存行的**身份指纹**字段名。新代码写缓存时带上「这条释义经校验、答的就是这个查询」
# 的归一查询串；老代码写的行没有这个键 —— 见 `cache_entry_trustworthy`。
CACHE_VERIFIED_KEY = "verified_word"

# 会「答错词」的源：它们的缓存行不带指纹时不可信（v18 时期写下的行全都可能是错义）。
# 本地 ECDICT 与 MT 引擎不做这个判断（前者按词头查库、后者按输入串翻译，都不会串词）。
UNVERIFIED_SOURCES = frozenset({"youdao"})


def cache_entry_trustworthy(cached: dict[str, Any], word: str) -> bool:
    """这条缓存正命中能不能直接采用（票 #174 的**存量自愈**）。

    为什么需要：`dict_cache` 的正命中是终态、**没有 TTL**，而 v18 时期写下的行没有
    经过任何身份校验 —— 实测 `lamb` 的行里躺着 `injection` 的释义（`12:09:19` 写入），
    不清掉的话用户会一直看到「注射；大量资金的投入」。

    判据：带 `verified_word` 指纹 → 比对查询；不带 → 只有**不可信源**（youdao）判否，
    其余（ECDICT / MT）照旧信任（不凭溯源猜测否决，零回归）。
    """
    verified = cached.get(CACHE_VERIFIED_KEY)
    if verified is not None:
        return phrase_key(str(verified)) == phrase_key(word)
    return str(cached.get("source") or "") not in UNVERIFIED_SOURCES


def _trs_lines(entry: dict[str, Any]) -> list[str]:
    """词条 trs → 「n. 释义」行。jsonapi_s 是 `{"pos","tran"}`，旧 jsonapi 是嵌套
    `{"tr":[{"l":{"i":[...]}}]}`，两种形态都认（端点无 SLA，结构可能变，宽松取值）。"""
    lines: list[str] = []
    for group in _list_of(entry.get("trs")):
        group = _dict_of(group)
        if "tr" in group:  # 旧嵌套形态
            for item in _list_of(group.get("tr")):
                payload = _dict_of(_dict_of(item).get("l"))
                lines += _texts(payload.get("i"), limit=8)
            continue
        tran = str(group.get("tran") or "").strip()
        if not tran:
            continue
        pos = str(group.get("pos") or "").strip()
        if pos and not pos.endswith("."):
            pos = f"{pos}."
        lines.append(f"{pos} {tran}".strip())
    return lines


def _web_trans(data: dict[str, Any]) -> tuple[list[str], str]:
    """web_trans → (释义 values 去重限 3 条, 第一段清洗后的摘要)。

    摘要是网页语料行（`... <b>下苦功</b> ...`），剥标记压空白后当英文侧上下文。
    """
    block = _dict_of(data.get("web_trans"))
    values: list[str] = []
    summary = ""
    for entry in _list_of(block.get("web-translation")):
        for item in _list_of(_dict_of(entry).get("trans")):
            item = _dict_of(item)
            value = str(item.get("value") or "").strip()
            if value and value not in values:
                values.append(value)
            if not summary:
                for line in _list_of(_dict_of(item.get("summary")).get("line")):
                    text = strip_tags(str(line))
                    if text:
                        summary = text[:200]
                        break
    return values[:3], summary


def _fanyi_tran(data: dict[str, Any]) -> str:
    """`fanyi.tran`：整句机翻（短语无词条/句子的兜底路径，v18 W2）。"""
    return str(_dict_of(data.get("fanyi")).get("tran") or "").strip()


def _blng_examples(data: dict[str, Any], limit: int = 3) -> list[str]:
    """`blng_sents(_part)` 双语例句 → 「英文句子 中文翻译」行（写进 `en`，结构兼容）。"""
    for key in ("blng_sents_part", "blng_sents"):
        pairs = _list_of(_dict_of(data.get(key)).get("sentence-pair"))
        if not pairs:
            continue
        examples: list[str] = []
        for pair in pairs:
            pair = _dict_of(pair)
            eng = strip_tags(str(pair.get("sentence-eng") or pair.get("sentence") or ""))
            trans = str(pair.get("sentence-translation") or "").strip()
            if eng and trans:
                examples.append(f"{eng} {trans}")
            if len(examples) >= limit:
                break
        if examples:
            return examples
    return []


def _ee_text(data: dict[str, Any]) -> str:
    """`ee`（WordNet 英英）→ 第一段英文释义。新旧 tr 形态都认。"""
    ee = _dict_of(data.get("ee"))
    word = ee.get("word")
    entries = (
        [word]
        if isinstance(word, dict)
        else [entry for entry in _list_of(word) if isinstance(entry, dict)]
    )
    for entry in entries:
        for group in _list_of(_dict_of(entry).get("trs")):
            for item in _list_of(_dict_of(group).get("tr")):
                item = _dict_of(item)
                payload = _dict_of(item.get("l"))
                english = _texts(payload.get("i"), limit=3)
                if not english:
                    tran = str(item.get("tran") or "").strip()
                    english = [tran] if tran else []
                if english:
                    return "; ".join(english)
    return ""


def parse_youdao(data: Any, word: str) -> dict[str, Any] | None:
    """jsonapi_s 一条响应按字段存在性宽松取值（D1）：ec → web_trans → fanyi。

    任何关键字段都缺 = miss（返回 None），回落下一级 provider。

    **回包身份门（票 #174）**：先确认这条回包在回答 `word`。`jsonapi_s` 实测会对
    某些查询回**另一个词**的词条（`q=lamb` → `mush`/`injection`，约 12% 的单词），
    身份不符一律抛 `WordMismatch` —— 绝不把别人的释义当成本词的结果（fail-closed）。
    """
    if not isinstance(data, dict):
        raise ProviderError("parse")
    if not reply_matches_query(data, word):
        raise WordMismatch()
    ec = _dict_of(data.get("ec"))
    entries = _ec_entries(ec)
    first = entries[0] if entries else {}
    cn, pos = group_pos(_trs_lines(first))
    phonetic = str(first.get("usphone") or first.get("ukphone") or "").strip()
    en = _ee_text(data)
    tags = [str(tag).strip().lower() for tag in _texts(ec.get("exam_type"), limit=8)]
    web_values, web_summary = _web_trans(data)
    machine = False
    if cn:
        if not is_phrase(word):
            # 单词：ec 为主，web_trans 作补充释义（只补 cn 里没有的义项）
            extra = [value for value in web_values if value not in cn]
            if extra:
                cn = "；".join([cn, *extra])
    elif web_values:
        cn = "；".join(web_values)
        en = en or web_summary
    if not cn:
        cn = _fanyi_tran(data)
        machine = bool(cn)  # #172：只有落到 fanyi 整句机翻才算「机翻」
    examples = _blng_examples(data)
    if examples:
        en = "；".join([en, *examples]) if en else "；".join(examples)
    if not (cn or en or phonetic):
        return None
    return _payload(
        word, phonetic=phonetic, cn=cn, en=en, pos=pos, tags=tags, machine=machine
    )


def parse_youdao_dictionary(data: Any, word: str) -> dict[str, Any] | None:
    """多词目标的整条短语词典查询（v22 P5 / #169，spec §11）。

    只认真正的词典命中（`DictService` 的 dict_only 路径）：
    - 有道 `ec` 词典条目，且 **`ec.word` 的词头（`return-phrase`）归一后必须与查询完全
      相等**（有道的 ec 对近似/包含查询也可能回别人的词条 —— 见 review 修复轮）；
    - `web_trans` 里 **key 归一后与查询完全相等** 的词条（`be at pains to` → 下苦功）。

    **不认**两样（它们是 v18 给「所有含空格查询」兜底的机翻路径，也就是错义来源）：
    - web_trans 里 key 为别的长句的网络释义 —— `whether the` 会被
      「Whether the weather is good → 无论天气好不好」污染（用户报的错义）；
    - `fanyi` 整句机翻 —— spec §11「不做整句机器翻译」（后续独立票）。
    两层都不中 → None（回落到「未命中」，面板只显示上下文提示，主框保持既有空态）。

    **回包身份门（票 #174）**：与 `parse_youdao` 同一条门 —— 回包回答的是别的词
    （`input` 回声不等）就抛 `WordMismatch`，让 provider 换查询串重查。

    只验 `input` 回声、不验 `ec` 词头：有道对近似短语会回词头不同的词条
    （`q=give up` → 词头 `give up on`），那是既有语义里的「不采信 ec、继续走
    web_trans 精确 key」，不是源故障。
    """
    if not isinstance(data, dict):
        raise ProviderError("parse")
    if not reply_echoes_query(data, word):
        raise WordMismatch()
    ec = _dict_of(data.get("ec"))
    entries = _ec_entries(ec)
    first = entries[0] if entries else {}
    headword = _return_phrase(first)
    cn, pos = group_pos(_trs_lines(first))
    phonetic = str(first.get("usphone") or first.get("ukphone") or "").strip()
    tags = [str(tag).strip().lower() for tag in _texts(ec.get("exam_type"), limit=8)]
    # 见 ec 不盲信：词头缺失（旧 jsonapi 形态/端点结构变化）或与查询不等 → 不采信，
    # 继续走下面的 web_trans 精确 key 分支（宁缺勿错）。
    if cn and headword and phrase_key(headword) == phrase_key(word):
        return _payload(
            word, phonetic=phonetic, cn=cn, en=_ee_text(data), pos=pos, tags=tags
        )
    key = phrase_key(word)
    values: list[str] = []
    for entry in _list_of(_dict_of(data.get("web_trans")).get("web-translation")):
        if phrase_key(_dict_of(entry).get("key")) != key:
            continue
        for item in _list_of(_dict_of(entry).get("trans")):
            value = str(_dict_of(item).get("value") or "").strip()
            if value and value not in values:
                values.append(value)
    if values:
        return _payload(
            word, phonetic="", cn="；".join(values[:3]), en="", pos=[], tags=[]
        )
    return None


def parse_youdao_translation(data: Any, word: str) -> dict[str, Any] | None:
    """`fanyi.tran` 整句机翻（v22 修复 / #172 的机翻兜底入口）。

    只认机翻，不认词典条目/网络释义 —— 供 `DictService` 在**严格词典全部未命中**后调用。
    回包带 `machine:true`，外壳据此标注「机翻」。

    **回包身份门（票 #174）**：整句回包只有 `input` 一个身份字段，同样要校验 ——
    实测 `q=A baby sheep is a lamb.` 会回 `Syncom` 的词条。
    """
    if not isinstance(data, dict):
        raise ProviderError("parse")
    if not reply_matches_query(data, word):
        raise WordMismatch()
    text = _fanyi_tran(data)
    if not text:
        return None
    return _payload(
        word, phonetic="", cn=text, en="", pos=[], tags=[], machine=True
    )


class EdgeTranslateProvider:
    """微软 Edge 翻译 `translatetext`（v18 W3a/D2：免 key 免 token，实测 0.47s、连发无限流）。

    仅对含空格的查询启用（`phrase_only`）：单词走词典链，MT 单词质量不值一次请求。
    返回纯机翻（`cn`），`phonetic` 空；任何异常结构 = miss 回落 transmart。
    """

    name = "edge"
    timeout_ms = EDGE_TIMEOUT_MS
    skip_phrases = False
    phrase_only = True
    machine = True  # #172：纯 MT 引擎 —— 回包标 machine:true，且不写 dict_cache

    def __init__(
        self,
        template: str = EDGE_URL,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self.template = template
        self.opener = opener

    def lookup(self, word: str, timeout: float) -> dict[str, Any] | None:
        body = json.dumps([word], ensure_ascii=False).encode("utf-8")
        data = _post_json(
            self.template, body, timeout, self.opener or urlopen_call
        )
        return parse_edge(data, word)


def parse_edge(data: Any, word: str) -> dict[str, Any] | None:
    """`[{"translations":[{"text":"..."}]}]` → 纯文本机翻。"""
    if not isinstance(data, list):
        raise ProviderError("parse")
    for entry in data:
        for item in _list_of(_dict_of(entry).get("translations")):
            text = str(_dict_of(item).get("text") or "").strip()
            if text:
                return _payload(word, phonetic="", cn=text, en="", pos=[], tags=[])
    return None


class TransmartProvider:
    """腾讯 transmart `/api/imt`（v18 W3a/D2：纯匿名，实测 0.15s 全场最快，末级互备）。

    与 Edge 是不同 MT 引擎，互为降级；实测返回值尾部可能带逗号，读侧清洗。
    """

    name = "transmart"
    timeout_ms = TRANSMART_TIMEOUT_MS
    skip_phrases = False
    phrase_only = True
    machine = True  # #172：纯 MT 引擎（与 Edge 同口径）

    def __init__(
        self,
        template: str = TRANSMART_URL,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self.template = template
        self.opener = opener

    def lookup(self, word: str, timeout: float) -> dict[str, Any] | None:
        body = json.dumps(
            {
                "header": {
                    "fn": "auto_translation",
                    "session": "",
                    "client_key": TRANSMART_CLIENT_KEY,
                },
                "source": {"text_list": [word]},
                "target": {"lang_type": "zh"},
            },
            ensure_ascii=False,
        ).encode("utf-8")
        data = _post_json(
            self.template, body, timeout, self.opener or urlopen_call
        )
        return parse_transmart(data, word)


def parse_transmart(data: Any, word: str) -> dict[str, Any] | None:
    """`{"auto_translation":["..."]}` → 清洗尾部逗号后的纯文本机翻。"""
    if not isinstance(data, dict):
        raise ProviderError("parse")
    for item in _list_of(data.get("auto_translation")):
        text = _TRAILING_COMMA_RE.sub("", str(item or "")).strip()
        if text:
            return _payload(word, phonetic="", cn=text, en="", pos=[], tags=[])
    return None


def _payload(
    word: str,
    *,
    phonetic: str,
    cn: str,
    en: str,
    pos: list[dict[str, Any]],
    tags: list[str],
    machine: bool = False,
) -> dict[str, Any]:
    """统一结构里与源无关的那部分（`source`/`cached`/`ms` 由 `DictService` 补）。

    `phonetic` 在这里**保持 provider 原文**（ECDICT 的 `ri'lʌktәnt` 不洗、有道的
    `rɪˈlʌktənt` 不加斜杠）：这一份会原样进 `dict_cache`，展示形态由
    `DictService` 出口的 `display_phonetic` 现算（见模块开头「音标归一」）。

    `machine=True` 只由 MT 来路设置（有道 fanyi / Edge / transmart 的解析或
    `DictService._is_machine`）—— 机翻结果不进 `dict_cache`，回包带 `machine:true`。
    """
    payload: dict[str, Any] = {
        "word": word,
        "phonetic": phonetic.strip(),
        "cn": cn,
        "en": en,
        "pos": pos,
        "tags": [tag for tag in tags if tag],
    }
    if machine:
        payload["machine"] = True
    return payload


# --- 本地 ECDICT（§7；schema 由读侧宽松探测）---------------------------------


@dataclass(frozen=True)
class Layout:
    """探测出来的 ECDICT 表/列映射（列不存在就是 None，一律不假设）。"""

    table: str
    word: str
    phonetic: str | None = None
    cn: str | None = None
    en: str | None = None
    tags: str | None = None
    pos: str | None = None
    collins: str | None = None
    oxford: str | None = None
    bnc: str | None = None
    coca: str | None = None


_WORD_COLUMNS = ("word", "lemma", "term", "headword")
_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "phonetic": ("phonetic", "ph", "pron", "pronunciation"),
    "cn": ("translation", "trans", "meaning_cn", "cn", "chinese"),
    "en": ("definition", "def", "definition_en", "en", "english"),
    "tags": ("tag", "tags", "exam_type"),
    "pos": ("pos", "part_of_speech", "partofspeech"),
    "collins": ("collins", "star"),
    "oxford": ("oxford",),
    "bnc": ("bnc",),
    "coca": ("coca", "frq", "frequency"),
}


# 表/列名来自用户导入的外部词典文件，又要原样拼进 f-string SQL（`PRAGMA table_info`
# / `SELECT … FROM "…"`）——只放行 `[A-Za-z0-9_]`（ECDICT 官方库与 CSV 建库的命名
# 全在其中），含内嵌 `"` 等字符的标识符探测时跳过/不参与对名，不进 SQL（票 #190 S5）。
_SAFE_IDENT_RE = re.compile(r"[A-Za-z0-9_]+")


def probe_layout(conn: sqlite3.Connection) -> Layout:
    """找出「哪张表是你想读的词典」，列名逐个用 `PRAGMA table_info` 对别名。

    张成的候选里优先：名为 `word` 的列 > 有中文释义列 > 有音标列（够宽松，也不至于
    挑到一张无关的表）。找不到词列时抛 `ValueError`。
    表名/列名过 `_SAFE_IDENT_RE`（票 #190 S5）：表名不合格的整张跳过，列名不合格的
    不参与对名——含内嵌 `"` 的恶意标识符不再原样进 SQL。
    """
    best: tuple[int, Layout] | None = None
    for (table,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
    ):
        name = str(table)
        if _SAFE_IDENT_RE.fullmatch(name) is None:
            log.warning("词典表名含白名单外字符（%r），整张跳过", name)
            continue
        columns = [
            str(row[1])
            for row in conn.execute(f'PRAGMA table_info("{name}")')
            if _SAFE_IDENT_RE.fullmatch(str(row[1]))
        ]
        lowered = [name.lower() for name in columns]
        word = next(
            (candidate for candidate in _WORD_COLUMNS if candidate in lowered), None
        )
        if word is None:
            continue
        mapping: dict[str, str] = {}
        for field, aliases in _COLUMN_ALIASES.items():
            for alias in aliases:
                if alias in lowered:
                    mapping[field] = columns[lowered.index(alias)]
                    break
        score = (
            (2 if word == "word" else 0)
            + (2 if "cn" in mapping else 0)
            + (1 if "phonetic" in mapping else 0)
        )
        layout = Layout(
            table=name,
            word=columns[lowered.index(word)],
            phonetic=mapping.get("phonetic"),
            cn=mapping.get("cn"),
            en=mapping.get("en"),
            tags=mapping.get("tags"),
            pos=mapping.get("pos"),
            collins=mapping.get("collins"),
            oxford=mapping.get("oxford"),
            bnc=mapping.get("bnc"),
            coca=mapping.get("coca"),
        )
        if best is None or score > best[0]:
            best = (score, layout)
    if best is None:
        raise ValueError("找不到带 word 列的词典表")
    return best[1]


class EcdictProvider:
    """`data/ecdict.db` 只读查询（缺省不存在时 hub 根本不构造它，§7）。"""

    name = "ecdict"
    timeout_ms = 0  # 本地查询不吃网络预算
    skip_phrases = False

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._layout: Layout | None = None

    def available(self) -> bool:
        return self.path.is_file()

    def _connect(self) -> sqlite3.Connection:
        uri = f"file:{self.path.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=1.0)
        conn.row_factory = sqlite3.Row
        return conn

    def layout(self) -> Layout:
        if self._layout is None:
            conn = self._connect()
            try:
                self._layout = probe_layout(conn)
            finally:
                conn.close()
            log.info("ECDICT 就绪（表 %s）", self._layout.table)
        return self._layout

    def lookup(self, word: str, timeout: float = 0.0) -> dict[str, Any] | None:
        if not self.available():
            return None
        layout = self.layout()
        conn = self._connect()
        try:
            # `word = ? COLLATE NOCASE` 才吃得上 NOCASE 索引（`lower(word)=lower(?)`
            # 是表达式，只能全表扫 —— release 库与 CSV 建的库都有 NOCASE 索引）
            row = conn.execute(
                f'SELECT * FROM "{layout.table}" WHERE "{layout.word}" = ? COLLATE NOCASE LIMIT 1',
                (word,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return self._build(row, layout)

    def exists(self, word: str) -> bool:
        """词头在不在本地库里（v18 W3d 碎片判据：末 token 在全量 ECDICT 中才算完整词）。

        只判存在性、不建 payload；库打不开（缺文件/坏库/无词典表）一律 False。
        """
        if not self.available():
            return False
        try:
            layout = self.layout()
            conn = self._connect()
        except (sqlite3.Error, ValueError):
            return False
        try:
            row = conn.execute(
                f'SELECT 1 FROM "{layout.table}" WHERE "{layout.word}" = ? COLLATE NOCASE LIMIT 1',
                (word,),
            ).fetchone()
        except sqlite3.Error:
            return False
        finally:
            conn.close()
        return row is not None

    def _build(self, row: sqlite3.Row, layout: Layout) -> dict[str, Any] | None:
        # `sqlite3.Row` 迭代出来的是**值**不是列名，所以这里必须 `.keys()`（SIM118 会改语义）
        keys = {key.lower() for key in row.keys()}  # noqa: SIM118

        def value(field: str) -> str:
            column = getattr(layout, field)
            if column is None or column.lower() not in keys:
                return ""
            raw = row[column]
            return "" if raw is None else str(raw).strip()

        cn, pos = group_pos(_lines(value("cn")))
        # 中文释义行首没写词性（ECDICT CSV 的 `pos` 列全空、release 库有 `v:4/n:96` 这种码）
        # → 用 `pos` 列的码表兜底
        pos = apply_pos_codes(pos, decode_pos(value("pos")))
        en = "; ".join(_lines(value("en"))[:3])
        payload = _payload(
            str(row[layout.word]),
            phonetic=value("phonetic"),
            cn=cn,
            en=en,
            pos=pos,
            tags=value("tags").lower().split(),
        )
        for field in ("collins", "oxford", "bnc", "coca"):
            number = _int_or_none(value(field))
            if number is not None:
                payload[field] = number
        if not (payload["cn"] or payload["en"] or payload["phonetic"]):
            return None
        return payload


def _int_or_none(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# --- 取词链本体 -------------------------------------------------------------


# 碎片止血（D4）的完整词形：仅字母及 `'`/`-`（`faithf` 半截、`12` 数字都不算完整词形）
_TAIL_WORD_RE = re.compile(r"[A-Za-z]+(?:['-][A-Za-z]+)*")


class DictService:
    """`lookup` 的唯一入口：缓存 → 本地 → 在线，结果写回缓存（§7）。"""

    def __init__(
        self,
        *,
        cache_get: Callable[..., tuple[str, dict[str, Any] | None]],
        cache_put: Callable[[str, dict[str, Any] | None, str], None],
        local: Sequence[Provider] = (),
        online: Sequence[Provider] = (),
        online_enabled: bool = True,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> None:
        self.cache_get = cache_get
        self.cache_put = cache_put
        self.local = list(local)
        self.online = list(online)
        self.online_enabled = online_enabled
        self.timeout_ms = timeout_ms
        # 票 #188（D11）：同查询 single-flight 的在途 Future 表（仿 speech._flight
        # 先例）。取词台（RPC/HTTP）与补释义 worker 在不同线程，并发查同一个词时
        # 只有领头的真正走链，其余等它的结果 —— 不再打两份在线请求。
        self._flight_guard = threading.Lock()
        self._inflight: dict[
            tuple[str, bool, bool, bool, bool], Future[dict[str, Any]]
        ] = {}

    def _online_providers(self, query: str) -> list[Provider]:
        """这次查询适用的在线源（短语不试 skip_phrases 源、单词不试 phrase_only 源）。"""
        if not self.online_enabled:
            return []
        return [
            provider
            for provider in self.online
            if not (provider.skip_phrases and is_phrase(query))
            and not (getattr(provider, "phrase_only", False) and not is_phrase(query))
        ]

    def _looks_fragment(self, query: str) -> bool:
        """v18 W3d 碎片止血判定：含空格且末 token 不是完整词 → 打字中间态。

        「完整词」= 纯字母及 `'`/`-` 且在本地 ECDICT 中存在；本地库全部不在位时
        **不启用**（宁漏勿误，D4——否则会误杀真短语）。单词查询不受此规则影响。
        """
        if not is_phrase(query):
            return False
        tail = query.split()[-1]
        consulted = False
        for provider in self.local:
            exists = getattr(provider, "exists", None)
            if exists is None:
                continue
            available = getattr(provider, "available", None)
            if available is not None and not available():
                continue
            consulted = True
            if not _TAIL_WORD_RE.fullmatch(tail):
                continue
            try:
                if exists(tail):
                    return False  # 末 token 在库里 → 完整词，照常在线
            except Exception:  # noqa: BLE001 - 库坏了当缺库处理，宁漏勿误
                continue
        return consulted  # 有库可问而没问着 → 碎片；没库可问 → 不启用

    def looks_fragment(self, word: str) -> bool:
        """公开入口给补释义队列（v23 审核修正）：口径与 `_looks_fragment` 完全一致。

        补释义走的是存储里的目标词，可能停在打字中间态；worker 在发查询前先问这里，
        命中就记 `fragment` 而不再打在线源。
        """
        return self._looks_fragment((word or "").strip())

    def lookup(
        self,
        word: str,
        *,
        fast: bool = False,
        full: bool = False,
        dict_only: bool = False,
        fresh: bool = False,
    ) -> dict[str, Any]:
        """`fast` / `full` 是 v18 W4 两段式回填（方案 B'，#149）的两道缝：

        - `fast=true`（外壳去抖首查）：正缓存 → 本地源，本地源命中立即返回并附
          `provisional`（dict_online=0 时没有二跳可言，不附）；正缓存命中是终态不附；
          本地全 miss 则**同请求内**继续走在线链返回最终结果 —— fast 永不两手空空。
        - `full=true`（外壳收到 provisional 后的二跳）：跳过正缓存与本地源直查在线链
          （避免被 ECDICT 既有命中短路），命中照常 `cache_put`；在线 miss **绝不写
          负缓存** —— 本地有词而在线没收录不等于 miss，写下去会把这个词压死 30 天。
        - 两者都不带（现状调用、HTTP /api/lookup、web 前端）：单段全链，行为不变。
        - `dict_only=true`（v22 P5 / #169，spec §11）：多词目标只做「整条短语词典查询」
          —— 词典源白名单 + 旁路 `dict_cache`，见 `_lookup_phrase_dictionary`。
          外壳只在目标含空格时带这个标记；单词目标即使误带也退回默认链（零回归）。
        - `fresh=true`（v29 取词台「重查」）：**不读** `dict_cache`（正/负都不读），
          照常走本地 + 在线链；命中写回新快照（覆盖可能串义的旧行），全 miss 不写
          负缓存（一次手动重查不该把词压死 30 天）。多词目标本来就走短语旁路。

        票 #188（D11）：同查询（词 + 五个标记的**精确组合**）single-flight —— 领头
        的线程走完整链路（含缓存写），并发抵达的同查询线程直接领它的结果副本。
        按标记组合分键：`fast` 与 `full` 语义不同（后者故意跳过正缓存），不合并。
        """
        started = time.perf_counter()

        def elapsed() -> int:
            return round((time.perf_counter() - started) * 1000)

        query = (word or "").strip()
        if not query:
            return {"ok": False, "error": "empty word", "ms": elapsed()}

        key = (query, fast, full, dict_only, fresh)
        with self._flight_guard:
            pending = self._inflight.get(key)
            if pending is not None:
                follower = True
            else:
                pending = self._inflight[key] = Future()
                follower = False
        if follower:
            # 等待者：领头者落定（成败皆传）后领结果副本 —— 浅拷贝，调用方可以
            # 安全地往里补自己的字段（如 hub 的 `seen`）而互不干扰。
            result = pending.result()
            return dict(result)
        try:
            result = self._lookup_chain(
                query,
                started,
                elapsed,
                fast=fast,
                full=full,
                dict_only=dict_only,
                fresh=fresh,
            )
        except BaseException as exc:
            pending.set_exception(exc)  # 领头失败：等待者按同样方式失败，不挂死
            raise
        else:
            pending.set_result(result)
            return result
        finally:
            with self._flight_guard:
                if self._inflight.get(key) is pending:
                    del self._inflight[key]

    def _lookup_chain(
        self,
        query: str,
        started: float,
        elapsed: Callable[[], int],
        *,
        fast: bool,
        full: bool,
        dict_only: bool,
        fresh: bool = False,
    ) -> dict[str, Any]:
        """single-flight 领头者走的完整链路（`lookup` 的本体，不含去重外壳）。"""
        # spec §11 的判据是「目标含空格（多词）」；单词目标即使误带标记也走默认链。
        if dict_only and is_phrase(query):
            return self._lookup_phrase_dictionary(
                query, fast=fast, full=full, started=started, elapsed=elapsed
            )

        fragment = self._looks_fragment(query)

        if full:
            return self._lookup_full(query, fragment, started, elapsed)

        # 这次查询真正适用的源；碎片中间态跳过全部源（W3d）。
        online = [] if fragment else self._online_providers(query)
        enabled = {provider.name for provider in self.local} | {
            provider.name for provider in online
        }

        degraded = False
        if fresh:
            # v29：手动重查不读任何缓存行 —— 旧正缓存可能已经把别的词/旧义钉死。
            state, cached = "miss", None
        else:
            state, cached = self.cache_get(query, enabled=enabled)
        if state == "hit" and cached is not None:
            if not cache_entry_trustworthy(cached, query):
                # 票 #174 存量自愈：v18 时期写下的你dao 行没有身份指纹，里面可能躺着
                # 别的词的释义（实测 `lamb` 的行是 `injection`）。判否 → 当作未命中，
                # 让下面的本地/在线链重查一次，命中后 `_finish` 用新快照覆盖它。
                degraded = True
                log.info("缓存行没有身份指纹（%s 源），重查", cached.get("source"))
            else:
                return self._cached_reply(cached, query, elapsed())
        if state == "negative" and not fragment:
            # 碎片中间态只认正缓存（W3d）：负缓存行不答话，按未命中返回空结果
            return {
                "ok": False,
                "error": "not found",
                "cached": True,
                "word": query,
                "ms": elapsed(),
            }

        errors: list[str] = []
        tried: set[str] = set()
        if not fragment:
            for provider in self.local:
                available = getattr(provider, "available", None)
                if available is not None and not available():
                    # 票 #188（D6）：缺库 ≠「明确未收录」—— 它这次没答话，不进已试
                    # 集合（否则负缓存会连它一起盖住，库恢复后 30 天仍 not found）。
                    log.info("本地源 %s 缺库不可用，本次不计入已试集合", provider.name)
                else:
                    tried.add(provider.name)
                hit = self._ask(provider, query, 0.0, errors)
                if hit is not None:
                    result = self._finish(hit, provider.name, query, elapsed())
                    if fast and self.online_enabled:
                        # W4 provisional：本地命中先显示，等外壳二跳的在线全量覆盖
                        result["provisional"] = True
                    return result

            if self.online_enabled:
                deadline = started + self.timeout_ms / 1000
                for provider in online:
                    remaining = deadline - time.perf_counter()
                    if remaining <= 0:
                        errors.append("timeout")
                        log.warning("在线词典总预算用尽（%dms）", self.timeout_ms)
                        break
                    tried.add(provider.name)
                    budget = min(remaining, provider.timeout_ms / 1000)
                    hit = self._ask(provider, query, budget, errors)
                    if hit is not None:
                        return self._finish(
                            hit,
                            provider.name,
                            query,
                            elapsed(),
                            machine=self._is_machine(provider, hit),
                        )

        if tried and not errors and tried <= enabled and not fragment and not fresh:
            # 所有启用源都明确答「没这个词」→ 负结果缓存 30 天（§7）。`source` 记下
            # 已试集合，读侧按「当前启用集合 ⊆ 已试集合」判断还算不算数（R1 §8.3）。
            # 票 #188（D6）：缺库的本地源不进 `tried` —— 它没答过话，库恢复后这条
            # 负缓存盖不住新的启用集合，自然重查。
            # 碎片中间态永远不写负缓存（W3d：碎片把真短语「毒」进 30 天负缓存）。
            # R1（#174 收尾）：降级判过无指纹老行后重查未中 → 不写负缓存（沿用 v18
            # 「有老释义就别写 null」：别把用户已知的旧释义压成 30 天 miss）。
            if degraded:
                # R1（#174 收尾）：降级判过无指纹老行后重查未中 → 沿用它、不写负缓存
                # （v18「有老释义就别写 null」：别把用户已知的旧释义压成 30 天空白）。
                log.info("缓存行无指纹且重查未中（%s），沿用老释义", cached.get("source"))
                return self._cached_reply(cached, query, elapsed())
            self.cache_put(query, None, negative_source(tried))
            log.info("查词未收录（%d 字），负结果进缓存", len(query))
            return {
                "ok": False,
                "error": "not found",
                "cached": False,
                "word": query,
                "ms": elapsed(),
            }

        error = errors[0] if errors else "not found"
        if fragment:
            log.info("碎片中间态跳过在线查询（%d 字），不写负缓存", len(query))
        else:
            log.info("查词未命中：%s（%d 字，%dms）", error, len(query), elapsed())
        result = {
            "ok": False,
            "error": error,
            # 票 #182（D1）：additive `reason` 分档，补释义报告不再把网络/解析错
            # 压成「词典未收录」；外壳只读 `error`，形状不变。
            "reason": _refill_reason(errors),
            "word": query,
            "cached": False,
            "ms": elapsed(),
        }
        if fragment:
            result["source"] = "fragment"  # 说明性字段；不会进负缓存
        return result

    def _lookup_full(
        self, query: str, fragment: bool, started: float, elapsed
    ) -> dict[str, Any]:
        """W4 二跳（方案 B'）：跳过 dict_cache 正缓存与本地源，直查在线链。

        在线 miss **不写负缓存**：二跳只发生在本地已命中之后，本地有词而在线没
        收录不等于 miss（全链 miss 的负缓存写入在 `lookup` 主路径里维持现状）。
        """
        # 碎片中间态只认正缓存（W3d）：正常流程到不了这里（碎片没有 provisional），
        # 钉住防手工构造的 full 请求绕过止血。
        if fragment:
            return {
                "ok": False,
                "error": "not found",
                "word": query,
                "cached": False,
                "ms": elapsed(),
                "source": "fragment",
            }
        errors: list[str] = []
        deadline = started + self.timeout_ms / 1000
        for provider in self._online_providers(query):
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                errors.append("timeout")
                log.warning("在线词典总预算用尽（%dms）", self.timeout_ms)
                break
            budget = min(remaining, provider.timeout_ms / 1000)
            hit = self._ask(provider, query, budget, errors)
            if hit is not None:
                return self._finish(
                    hit,
                    provider.name,
                    query,
                    elapsed(),
                    machine=self._is_machine(provider, hit),
                )
        error = errors[0] if errors else "not found"
        log.info("二跳在线未命中（%d 字，%dms），不写负缓存", len(query), elapsed())
        return {
            "ok": False,
            "error": error,
            "word": query,
            "cached": False,
            "ms": elapsed(),
        }

    def _dictionary_providers(
        self, query: str
    ) -> list[tuple[Provider, Callable[[str, float], dict[str, Any] | None]]]:
        """dict_only 模式的在线白名单：只留有 `lookup_dictionary` 的词典源（有道）。

        Edge / transmart 是纯机翻，没有这个方法 → 多词路径对它们**零请求**
        （spec §11「不做整句机器翻译」）。`phrase_only` / `skip_phrases` 等既有过滤照旧。
        能力判断只在这一处做，`_ask` 直接用这里给出的查询函数（不重复 getattr）。
        """
        providers: list[tuple[Provider, Callable[[str, float], dict[str, Any] | None]]] = []
        for provider in self._online_providers(query):
            lookup = getattr(provider, "lookup_dictionary", None)
            if lookup is not None:
                providers.append((provider, lookup))
        return providers

    def _lookup_phrase_dictionary(
        self, query: str, *, fast: bool, full: bool, started: float, elapsed
    ) -> dict[str, Any]:
        """多词目标的整条短语词典查询（v22 P5 / #169，spec §11）。

        **严格词典优先**（v22 P5 不变）：
        - **源白名单**：本地 ECDICT + 有道的词典条目/精确网页词条（`lookup_dictionary`）；
          `fanyi`、Edge、transmart 都不参与这一层 —— v18 链对任何含空格查询都会借它们
          返回整句释义（`whether the` → 无论天气好不好 = 用户报的错义）。
        - **旁路 `dict_cache`**：v18 的正缓存里可能已经躺着那种机翻错义（缓存 30 天），
          所以这条路径不读也不写；负缓存口径不变（实质是这条路径不参与）。

        **严格词典未命中 → 机翻兜底**（v22 修复 / #172）：
        - 只在这条路径的词典白名单全部 miss 之后才问 MT（有道 `fanyi` → Edge → transmart，
          复用 v18 既有实现），回包带 `machine:true` + 译文（`cn`）+ `source`；
        - 机翻结果同样**不写** `dict_cache`（`_finish(cache=False)`），也不标 `provisional`
          （外壳不会为机翻再发二跳，避免死循环）；
        - 总预算仍是单次 `lookup` 的 `timeout_ms`（默认 2500ms），超时/离线/未收录一律
          `not found`，不影响保存。
        """
        errors: list[str] = []
        if not full:
            for provider in self.local:
                hit = self._ask(provider, query, 0.0, errors)
                if hit is not None:
                    result = self._finish(hit, provider.name, query, elapsed(), cache=False)
                    if fast and self.online_enabled and self._dictionary_providers(query):
                        # 与 W4 同口径：本地先显示，等外壳二跳的在线词典全量覆盖。
                        result["provisional"] = True
                    result["dict_only"] = True
                    return result
        if self.online_enabled:
            deadline = started + self.timeout_ms / 1000
            for provider, lookup in self._dictionary_providers(query):
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    errors.append("timeout")
                    log.warning("短语词典总预算用尽（%dms）", self.timeout_ms)
                    break
                budget = min(remaining, provider.timeout_ms / 1000)
                hit = self._ask(provider, query, budget, errors, lookup=lookup)
                if hit is not None:
                    result = self._finish(hit, provider.name, query, elapsed(), cache=False)
                    result["dict_only"] = True
                    return result
            # v22 修复（#172）：严格词典全部 miss → 回退机翻（仍不写 dict_cache）
            fallback = self._lookup_machine_fallback(query, deadline, errors)
            if fallback is not None:
                mt_provider, mt_hit = fallback
                result = self._finish(
                    mt_hit, mt_provider.name, query, elapsed(), cache=False, machine=True
                )
                result["dict_only"] = True
                log.info("多词目标词典未命中 → 机翻兜底（%s）", mt_provider.name)
                return result
        if errors:
            log.info(
                "多词目标词典/机翻兜底都未命中：%s（%d 字，不写缓存）",
                ",".join(errors),
                len(query),
            )
        else:
            log.info(
                "多词目标词典/机翻兜底都未命中（%d 字，%dms），不写缓存",
                len(query),
                elapsed(),
            )
        return {
            "ok": False,
            "error": "not found",
            # additive：外壳的 `error` 形状不变；补释义队列靠 reason 分档
            # （票 #182 D1：network/parse/http/word-mismatch 直通，不再拍平）。
            "reason": _refill_reason(errors),
            "word": query,
            "cached": False,
            "dict_only": True,
            "ms": elapsed(),
        }

    def _machine_providers(
        self, query: str
    ) -> list[tuple[Provider, Callable[[str, float], dict[str, Any] | None]]]:
        """机翻兜底源（v22 修复 / #172）：复用 v18 既有 MT 引擎，零新依赖。

        能力判断只在这一处：优先进 `lookup_machine`（有道 → 只取 `fanyi` 整句），
        否则认 `machine` 标记的纯 MT 源（Edge / transmart，各自的 `lookup`）。
        `phrase_only` / `skip_phrases` 等既有过滤照旧（单词目标本来就走默认链）。
        """
        providers: list[tuple[Provider, Callable[[str, float], dict[str, Any] | None]]] = []
        for provider in self._online_providers(query):
            lookup = getattr(provider, "lookup_machine", None)
            if lookup is not None:
                providers.append((provider, lookup))
            elif getattr(provider, "machine", False):
                providers.append((provider, provider.lookup))
        return providers

    def _lookup_machine_fallback(
        self, query: str, deadline: float, errors: list[str]
    ) -> tuple[Provider, dict[str, Any]] | None:
        """按序问 MT 源，首个有译文者胜；预算用尽就收手（有界，不挂死）。"""
        for provider, lookup in self._machine_providers(query):
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                errors.append("timeout")
                log.warning("机翻兜底总预算用尽（%dms）", self.timeout_ms)
                break
            budget = min(remaining, provider.timeout_ms / 1000)
            hit = self._ask(provider, query, budget, errors, lookup=lookup)
            if hit is not None and str(hit.get("cn") or "").strip():
                return provider, hit
        return None

    @staticmethod
    def _is_machine(provider: Provider, payload: dict[str, Any]) -> bool:
        """命中是否来自 MT：payload 自己标了（有道 fanyi 解析）或源是纯 MT 引擎。"""
        return bool(payload.get("machine")) or bool(getattr(provider, "machine", False))

    def _ask(
        self,
        provider: Provider,
        query: str,
        timeout: float,
        errors: list[str],
        *,
        lookup: Callable[[str, float], dict[str, Any] | None] | None = None,
    ) -> dict[str, Any] | None:
        call = lookup or provider.lookup
        try:
            return call(query, timeout)
        except NotFound:
            log.info("词典源 %s 明确未收录", provider.name)
            return None
        except ProviderError as exc:
            errors.append(exc.kind)
            log.warning("词典源 %s 失败：%s", provider.name, exc.kind)
            return None
        except Exception as exc:  # noqa: BLE001 - 词典坏掉绝不许拖垮 lookup
            errors.append(type(exc).__name__)
            log.warning("词典源 %s 异常：%s", provider.name, type(exc).__name__)
            return None

    def _cached_reply(
        self, cached: dict[str, Any], query: str, ms: int
    ) -> dict[str, Any]:
        """缓存正命中的出站形态（票 #188 D7）：剥掉内部指纹字段，对齐 `_finish` 出口。

        `verified_word`（`CACHE_VERIFIED_KEY`）是写侧的身份指纹（#174），只该躺在
        `dict_cache` 里 —— 修复前两条缓存读路径原样带出回包，还会经 hub 的
        `dict_json` 落进存储。两条读路径（正常命中 / 降级沿用）共用这一处。
        """
        result = {
            key: value for key, value in cached.items() if key != CACHE_VERIFIED_KEY
        }
        result["word"] = result.get("word") or query
        result["phonetic"] = display_phonetic(result.get("phonetic") or "")
        result.update({"ok": True, "cached": True, "ms": ms})
        if cached.get("source") in MACHINE_SOURCES:
            # #172：v18 缓存里可能躺着 edge/transmart 的旧机翻条目 —— 按来源认出来，
            # 标 machine:true（新机翻不再写缓存，所以只有这种历史条目会命中这里）。
            result["machine"] = True
        return result

    def _finish(
        self,
        payload: dict[str, Any],
        source: str,
        query: str,
        ms: int,
        *,
        cache: bool = True,
        machine: bool = False,
    ) -> dict[str, Any]:
        stored: dict[str, Any] = {
            "word": payload.get("word") or query,
            "phonetic": payload.get("phonetic") or "",  # 存 provider 原文，不归一
            "cn": payload.get("cn") or "",
            "en": payload.get("en") or "",
            "pos": payload.get("pos") or [],
            "tags": payload.get("tags") or [],
            "source": source,
            # 票 #174：记下「这条释义经身份校验、答的是这个查询」。老行没有这个键，
            # 读侧按 `cache_entry_trustworthy` 判（youdao 来的无指纹行不再直接采用）。
            CACHE_VERIFIED_KEY: query,
        }
        for extra in ("collins", "oxford", "bnc", "coca"):
            if payload.get(extra) is not None:
                stored[extra] = payload[extra]
        # dict_only（多词目标）旁路缓存：旧条目可能含机翻错义（v22 P5）。
        # #172：机翻结果（machine）一律不写缓存 —— 否则下次会被当成「词典命中」。
        if cache and not machine:
            self.cache_put(query, stored, source)
        log.info("查词命中 %s（%dms）%s", source, ms, "（机翻）" if machine else "")
        shown = {k: v for k, v in stored.items() if k != CACHE_VERIFIED_KEY}
        shown["phonetic"] = display_phonetic(stored["phonetic"])
        result = {**shown, "ok": True, "cached": False, "ms": ms}
        if machine:
            result["machine"] = True
        return result
