"""hub 的存储层：SQLite 唯一真源（WAL）+ 会话归属 + spool 入库 + v1 导入 + SRS。

规格：`docs/specs/v2-workbench.md` §4（数据模型）、§4.4（迁移与导入）、§6.1（会话归属）、
§8.3（SRS 简化版）、§7（`dict_cache`）、§10（日志纪律）。

日志纪律（§10）：本模块只记**数量 / 长度 / 耗时 / 错误码**，
绝不写目标词、释义、原句、窗口标题（会话标题也不写）。

分层：`Store` 只回**数据**，不回响应信封（`ok` 由 `hub.py` 的 RPC/HTTP 层加）。

线程模型：RPC 是一连接一线程（§2），所以 `Store` 内部用一把 `RLock` 串行化所有
数据库访问（单用户、单机，量级是「一天几十条」，一把锁足够且没有并发写风险）；
网络调用在 `dict_sources` 里做，**不占这把锁**。
"""

from __future__ import annotations

import csv
import io
import json
import logging
import math
import os
import re
import shutil
import sqlite3
import threading
import time
import zipfile
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple

from dict_sources import negative_covers, probe_layout, stripword
from preset_tags import PRESET_TAGS_NAME, clean_tag, preset_tags_for, read_preset_tags
from provenance import is_bilibili_video_id, resolve_account, urlopen_call, video_id_of
from sentence_match import contains_target, pick_same_sentence, sentence_key
from sessions import (
    CATEGORIES,
    CATEGORY_LABELS,
    DEFAULT_IDLE_MIN,
    UNTITLED,
    Plan,
    account_display,
    clean_title,
    format_ts,
    guess_category,
    is_http_url,
    parse_ts,
    plan_session,
    series_of,
    session_key,
    site_of,
    site_of_session,
)

log = logging.getLogger("hub.store")

# v33 T1（票 #207 / spec §A / ADR-0012）：新增待选句层 `subtitle_backlog`（一行一句、
# 挂会话下、唯一键 (session_id, sentence_key)）。5→6 只加这张新表 + 两条索引，
# 既有表零改动，启动时幂等建表。
# v33 T4（票 #210 / spec §E21）：subtitle_backlog 补 `picked_at`（已挑标记，可空）——
# 6→7 只加这一列，启动时幂等 `ALTER TABLE`，旧库零回填。
SCHEMA_VERSION = 7
NEGATIVE_TTL_DAYS = 30  # §7：负结果缓存 30 天
ACCOUNT_NEGATIVE_TTL_HOURS = 6  # 票 #91：账号解析失败后的负缓存窗口（T 小时内不再打同一个 BV）
DEFAULT_LIMIT = 50
MAX_LIMIT = 500
# v23 §4.1：取词台补释义的防呆上限（API 的 `limit` 默认值；单任务最多补这么多条）
REFILL_LIMIT = 200
# 补释义的「空」口径：候选查询与打开会话 / 文章时的提示计数必须同一句 SQL，
# 否则「提示 N 条」与真去补的集合会漂移。
EMPTY_MEANING_SQL = "COALESCE(TRIM(meaning), '') = ''"
# 票 #186（H10）：`refill_candidates(ids=…)` 的 IN 查询分块大小。去重后的 id 数没有
# 上限（网页端一次勾一大片），整把 `IN (…)` 超 SQLite 变量上限直接 500。
REFILL_IDS_CHUNK = 500
KINDS = ("word", "phrase", "sentence")
# v33 T1（票 #207 / spec §A）：待选句的来源三档（cc=字幕轨直读 / ocr=字幕带采样 /
# asr=语音转文字，二期）。表是三源共用的独立层（ADR-0012），非法值不收。
BACKLOG_SOURCES = ("cc", "ocr", "asr")
# 来源质量优先级（spec §B2a）：CC(3) > ASR(2) > OCR(1)。同句键冲突时按它择优接管
# —— 文本只能被质量更高的来源升级，绝不被低质量来源污染。
BACKLOG_SOURCE_RANK = {"cc": 3, "asr": 2, "ocr": 1}
# 「不更新这一列」的哨兵（_update_subtitle_row 的 score/suspicious 用）：None 是合法
# 列值（CC 整轨无分），不能拿它当「不动」的记号。
_UNSET: Any = object()
# O1 契约（spec §A5「双语字幕的中文行、纯中文字幕一律不收」）：拉丁行 = 至少有一个
# 拉丁字母、且不含 CJK 字（假名/汉字/谚文都算）。采集侧（cc_track / OCR 内核）各有
# 更早的过滤，这里是三源共用的最后一道防线 —— 数据库里不能躺进中文行。
# CJK 字符类是**单一来源**（v33 修复轮）：ocr_sampler 的噪声判定引用本常量，
# 两处各写一份迟早漂移。
CJK_CHAR_RE = re.compile(
    r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]"
)
_BACKLOG_LATIN = re.compile(r"[A-Za-z\u00c0-\u024f]")


def is_latin_line(text: str) -> bool:
    """`text` 是否为拉丁行（票 #207 AC6 / O1 契约）。

    判定按文字块而不是标点：全角句号、全角字母之外的全角标点不算 CJK；
    只有拉丁字母（含带变音符的 é/ü 等）与数字、标点组成的行才算收。
    """
    return bool(_BACKLOG_LATIN.search(text)) and not CJK_CHAR_RE.search(text)


def _merge_wins(new_source: str, new_score: Any, old_source: str, old_score: Any) -> bool:
    """同句键冲突时的择优判定（spec §B2a，纯函数）。

    rank 高者赢（CC > ASR > OCR）；同 rank 比 `score` 高低（缺分视作最低，
    无分 CC 整轨重复落到自己头上依旧先到先得）。低 rank 永不覆盖高 rank。
    """
    rank_new = BACKLOG_SOURCE_RANK.get(new_source, 0)
    rank_old = BACKLOG_SOURCE_RANK.get(old_source, 0)
    if rank_new != rank_old:
        return rank_new > rank_old
    if new_score is None:
        return False
    if old_score is None:
        return True
    try:
        return float(new_score) > float(old_score)
    except (TypeError, ValueError):
        return False


def _position_text(seconds: float) -> str:
    """视频秒 → `mm:ss`（满一小时进 `h:mm:ss`），挑词词条的 `position` 用。

    口径对齐前端 `positionSeconds()`（`app.js`：`[h:]mm:ss` 反解回秒跳转）：
    分钟补零、秒两位 —— 展示层 `formatClock` 同款形状。
    """
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"
# v21 长文（票 #158）：文章的来源两条路 —— 整页 OCR / 手动粘贴
DOCUMENT_FORMATS = ("ocr", "manual")
# `document()` 的显式列清单（按建表顺序）：**排除** `segmentation_source`（v22 P1a 的
# 可能几 KB 的大字段，走独立访问器）。显式列而不是 `SELECT *`，新加的大字段就不会
# 悄悄进每篇 article dict；将来加列时这里要显式决定「进不进 document()」。
_DOCUMENT_COLUMNS = (
    "id",
    "session_id",
    "title",
    "source_app",
    "source_url",
    "text",
    "format",
    "passage_breaks",
    "captured_at",
    "created_at",
    "updated_at",
)
# v22 P1b（票 #166 / spec §8.6）：文章列表分页上限（与记录列表的 500 分开 —— 篇卡
# 只有标题/计数，200 篇已经远超单用户的量级）。
ARTICLE_LIMIT_MAX = 200
DEFAULT_ARTICLE_LIMIT = 50
DEFAULT_SORT = "-captured_at"
_SORT_COLUMNS = ("captured_at", "due_at", "target", "hits", "last_seen_at", "id")

# v22 P2a（票 #167 / spec §6.4 §6.5 §6.6）：写方法的「不传 = 保留旧值」哨兵。
# 不能用 `None` 当默认值 —— `segmentation_source` 的 `None` 有语义（显式清空 source），
# `format` 的 `None` 也一样要能表达「显式给值」而不是「不动」。调用方（hub）需要
# 「保留旧值」时直接传这个哨兵（例如 OCR 合并更新请求没带 source）。
KEEP = object()

# #68：spool 消费的前后缀。`CONSUMING_SUFFIX` 是改名消费用的同目录副本 —— 同卷
# `os.replace` 是原子的，因此「改名成功」与「读完」之间不存在被追加行卡住的窗口。
CONSUMING_SUFFIX = ".consuming"
RENAME_ATTEMPTS = 3  # 外壳正持句柄（PermissionError）时的短重试：3 × 50ms
RENAME_RETRY_SECONDS = 0.05


# 建表 DDL（§4）。`schema_version` 之外全部 IF NOT EXISTS：启动幂等、可重复启动。
#
# ［与 §4.2 的一处偏差，已在票 #21 里记明］§4.2 写「`key` 唯一」，但 §6.1 要求
# 「同 key 空闲超时 → **新建**会话」——两条不可能同时成立（同一个 key 会先后拥有多个
# 会话行）。这里按 §6.1 实现：`key` 只建普通索引，「当前会话」= 该 key 下
# `last_active_at` 最新的那一行（`session_plan`）。同一时刻一个 key 仍只有一个活跃会话。
_TABLES = (
    """
    CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)
    """,
    """
    CREATE TABLE IF NOT EXISTS sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        key TEXT NOT NULL,
        title TEXT NOT NULL,
        source_app TEXT,
        created_at TEXT NOT NULL,
        last_active_at TEXT NOT NULL,
        note TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        target TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'word',
        context TEXT,
        meaning TEXT,
        meaning_source TEXT NOT NULL DEFAULT '',
        phonetic TEXT,
        dict_json TEXT,
        session_id INTEGER REFERENCES sessions(id),
        source_app TEXT,
        source_title TEXT,
        position TEXT,
        tags TEXT,
        hits INTEGER NOT NULL DEFAULT 1,
        captured_at TEXT NOT NULL,
        last_seen_at TEXT,
        due_at TEXT,
        interval_days REAL NOT NULL DEFAULT 0,
        ease REAL NOT NULL DEFAULT 2.5,
        reps INTEGER NOT NULL DEFAULT 0,
        lapses INTEGER NOT NULL DEFAULT 0,
        last_review_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS dict_cache (
        word TEXT PRIMARY KEY,
        json TEXT,
        source TEXT,
        fetched_at TEXT NOT NULL
    )
    """,
    # v6 三期 §账号解析：视频级缓存（同一视频不重复打接口；同一条视频的新会话直接继承账号）。
    # 只落账号（`account_*`），站点 / 系列仍现算。
    """
    CREATE TABLE IF NOT EXISTS video_meta (
        video_id TEXT PRIMARY KEY,
        site TEXT,
        title TEXT,
        account_name TEXT,
        account_id TEXT,
        fetched_at TEXT
    )
    """,
    # v21 长文（票 #158）：一篇文章一行。`text` 是**真源**（外壳已单行化，本进程不再折），
    # `passage_breaks` 是它的派生数据（JSON `[[start,end), ...]` 字符偏移）—— 放同一行
    # 保证「正文与边界永远一起读写」。段落不单独立表：它的文本可由 `text[start:end]` 重算。
    """
    CREATE TABLE IF NOT EXISTS documents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER REFERENCES sessions(id),
        title TEXT NOT NULL,
        source_app TEXT,
        source_url TEXT,
        text TEXT NOT NULL,
        format TEXT NOT NULL DEFAULT 'ocr',
        passage_breaks TEXT,
        captured_at TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    # v33 T1（票 #207 / spec §A / ADR-0012）：待选句层 —— 看视频时后台自动收的原句，
    # 一行一句、挂在会话下。硬边界：**不写 records、无 SRS 字段**，不进星图/统计/
    # 复习队列；在里面挑了词才经既有 `capture()` 生成词条。`sentence_key` 复用
    # sentence_match 的同句判定规范化键（K0），配合下方唯一索引实现「重复查轨不翻倍」。
    # `picked_at`（v33 T4，票 #210）：已挑标记 —— 挑词落库成功的时间戳，NULL = 未挑；
    # 「待挑 N」只数它为空的行，标记持久（spec §D20：挑过的不消失、只数未挑）。
    """
    CREATE TABLE IF NOT EXISTS subtitle_backlog (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER NOT NULL REFERENCES sessions(id),
        text TEXT NOT NULL,
        source TEXT NOT NULL,
        from_s REAL NOT NULL,
        to_s REAL NOT NULL,
        score REAL,
        suspicious INTEGER NOT NULL DEFAULT 0,
        image_path TEXT,
        sentence_key TEXT NOT NULL,
        picked_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
)

# 索引在补列之后建（旧库可能还没有 `due_at` 之类的新列）
_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_sessions_key ON sessions(key)",
    "CREATE INDEX IF NOT EXISTS idx_records_session ON records(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_records_due ON records(due_at)",
    "CREATE INDEX IF NOT EXISTS idx_records_target_lower ON records(lower(target))",
    "CREATE INDEX IF NOT EXISTS idx_records_captured ON records(captured_at)",
    # v21 长文：按文章查它下面的词条（`delete_document` 解绑、文章视图都走这条）
    "CREATE INDEX IF NOT EXISTS idx_records_doc ON records(doc_id, passage_index)",
    # v33 T1（票 #207）：按会话读待选句 + 唯一键 (session_id, sentence_key) 去重（AC2）
    "CREATE INDEX IF NOT EXISTS idx_subtitle_backlog_session ON subtitle_backlog(session_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_subtitle_backlog_session_key ON subtitle_backlog(session_id, sentence_key)",
)

# 向前兼容的补列（§4.4：不做回滚）。将来加列只往这里添。
_COLUMNS: dict[str, dict[str, str]] = {
    "sessions": {
        "note": "TEXT",
        "url": "TEXT",  # v3 §3：最新一次捕获的页面 URL
        "category": "TEXT",  # v3 §4：7 类之一（other 兜底）
        # v6 三期 §账号解析：解析出的 UP 主（`account_source` 目前只有 `api`）
        "account_name": "TEXT",
        "account_id": "TEXT",
        "account_source": "TEXT",
    },
    "documents": {
        # v22 P1a（票 #170 / spec §6.7）：创建时的**原始分段输入** —— manual=粘贴原文
        # 逐字；ocr=行框几何的 zlib+base64。可空、无回填；旧 v21 文章为 NULL →
        # 段落类型回落 body、重新分段如实报错（不伪造）。
        "segmentation_source": "TEXT",
    },
    "records": {
        "dict_json": "TEXT",
        "last_review_at": "TEXT",
        # v21 长文（票 #158）：词条来自哪篇文章的第几段。两列都可空 ——
        # NULL 就是「普通捕获」这一语义本身（零回填）。
        "doc_id": "INTEGER REFERENCES documents(id)",
        "passage_index": "INTEGER",
    },
    # v33 T4（票 #210）：已挑标记。T1 建的表没有它 —— 旧库启动时幂等补列，
    # NULL = 未挑（零回填）。
    "subtitle_backlog": {
        "picked_at": "TEXT",
    },
}

_ENTRY_META = re.compile(r"^-\s*([^:：]+)\s*[:：]\s*(.*)$")
_WORD_RE = re.compile(r"[A-Za-z0-9']+")


# --- 文本小工具 --------------------------------------------------------------


def one_line(value: Any) -> str:
    """单行化（§4.1 的 `context`；OCR 整行）：换行→空格、折叠空白、去首尾。"""
    if value is None:
        return ""
    text = str(value).replace("\r", " ").replace("\n", " ")
    return re.sub(r"[ \t\u00a0\u3000]+", " ", text).strip()


def guess_kind(target: str) -> str:
    """按目标词推断 `kind`（§3「默认按目标词自动推断」）的最简实现。

    v1 markdown 里没有 `kind`，导入时用它；无空白的单个词元 = `word`，
    其余 = `phrase`。**句子不做自动判定**（无法可靠区分短语与短句，用户可手改）。
    """
    return "word" if len(one_line(target).split()) <= 1 else "phrase"


def normalize_tags(value: Any) -> str:
    """标签归一化（§4.1「空格分隔、每词带 `#`」，沿用 v1 `NormalizeTags`）。

    v6 二期（票 #85）：每个标签走 `preset_tags.clean_tag`（去 `#`、**空白归一为 `-`**、
    限长）—— 否则账号名含空格的标签（`Bob the Canadian`）会被空格分隔格式切碎。
    字符串入参仍按空白切成多个标签（既有契约：`"雅思 gre"` = 两个标签），因此
    「标签内含空格」只可能来自序列入参 / 机器算出的标签。
    """
    if value is None:
        return ""
    if isinstance(value, str):
        tokens = value.split()
    elif isinstance(value, Sequence):
        tokens = [str(item) for item in value]
    else:
        tokens = [str(value)]
    seen: list[str] = []
    for token in tokens:
        word = clean_tag(token)
        if not word:
            continue
        tag = f"#{word}"
        if tag not in seen:
            seen.append(tag)
    return " ".join(seen)


def merge_tags(existing: Any, incoming: Any) -> str:
    """标签并集（票 #85 的 dedup 分支）：已有标签在前、顺序不动，新标签追加在后。

    两边都是「空格分隔、每词带 `#`」的归一字符串，各词再走一遍 `normalize_tags`
    的清洗 —— 于是并集永远合法、不重复。
    """
    return normalize_tags([*str(existing or "").split(), *str(incoming or "").split()])


def normalize_kind(value: Any) -> str:
    text = one_line(value).lower()
    return text if text in KINDS else "word"


def normalize_category(value: Any) -> str:
    """§3：会话分类归一化——小写/去空白，未命中 7 类一律回落 `other`。"""
    text = one_line(value).lower()
    return text if text in CATEGORIES else "other"


def normalize_url(value: Any) -> str:
    """§2.4.4：URL 只留 `http/https` 且 ≤2048（**不做参数清洗**）；非法值 → 空串。

    capture 走「非法值丢弃」（spool 一行坏掉不许打断整批入库）；用户手改走 PATCH，
    那里对非空非法值直接 400，更诚实（见 `update_session`）。
    """
    text = "" if value is None else str(value).strip()
    return text if is_http_url(text) else ""


# --- 长文小工具（v21 票 #158，纯函数）---------------------------------------


def optional_int(value: Any, minimum: int) -> int | None:
    """payload 里的可选整数列归一：非整数 / `bool` / 小于 `minimum` → `None`（按「没有」处理）。

    spool 的行来自外壳 JSON，字段可能缺失、是字符串、是负数 —— 这里一律退化成
    「没有这个字段」，绝不因此丢词（`capture` 的纪律：坏字段丢掉，词照落库）。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text.isdigit():
            return None
        value = int(text)
    if not isinstance(value, int) or value < minimum:
        return None
    return value


def passage_breaks(passages: Any, length: int) -> list[list[int]]:
    """校验段落偏移列表（`[[start, end), ...]`）→ 归一成 `list[list[int]]`。

    规则（架构 §3.2 ①）：有序、不重叠、`0 <= start <= end <= len(text)`；空列表合法
    （= 没有分段）。hub **只校验不修正**（非法值报错，不静默猜）—— 非法一律抛
    `ValueError("bad passages")`。
    """
    if not isinstance(passages, (list, tuple)):
        # 契约是「非法一律 ValueError」：T4 直接拿 `str(exc)` 当错误码（架构 §3.2 ①）
        raise ValueError("bad passages")  # noqa: TRY004
    breaks: list[list[int]] = []
    previous_end = 0
    for item in passages:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ValueError("bad passages")
        start, end = item
        for value in (start, end):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError("bad passages")  # noqa: TRY004
        # `start < previous_end` 同时兜住「乱序」与「重叠」；`start >= 0` 由 previous_end=0 兜
        if start < previous_end or end < start or end > length:
            raise ValueError("bad passages")
        breaks.append([start, end])
        previous_end = end
    return breaks


def parse_passages(value: Any) -> list[list[int]]:
    """`documents.passage_breaks` 的 JSON 文本 → `[[start, end), ...]`；坏数据 → 空列表。

    读路径**不抛**：一句坏 JSON 不该让整篇文章读不出来（外面还有 `text` 可用）。
    """
    raw = "" if value is None else str(value)
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("文章的段落偏移不是合法 JSON（%d 字节），按没有分段处理", len(raw))
        return []
    if not isinstance(parsed, list):
        return []
    breaks: list[list[int]] = []
    for item in parsed:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            return []
        try:
            breaks.append([int(item[0]), int(item[1])])
        except (TypeError, ValueError):
            return []
    return breaks


def parse_sort(sort: str) -> str:
    """`-captured_at` / `target` 之类 → SQL 片段（白名单，防注入）。"""
    text = (sort or "").strip()
    descending = text.startswith("-")
    column = text.lstrip("+-") or "captured_at"
    if column not in _SORT_COLUMNS:
        raise ValueError(f"不认识的排序字段：{column}")
    direction = "DESC" if descending else "ASC"
    return f"{column} {direction}, id {direction}"


# --- SRS（§8.3，纯函数）-----------------------------------------------------

GRADES = {"again": 0, "hard": 1, "good": 2, "easy": 3}
AGAIN_DELAY_MINUTES = 10
EASE_MIN = 1.3
EASE_MAX = 3.0


@dataclass(frozen=True)
class SrsState:
    """复习打分前的记录状态（对齐 `records` 的四个列）。"""

    interval_days: float = 0.0
    ease: float = 2.5
    reps: int = 0
    lapses: int = 0


class Review(NamedTuple):
    """打分后的四个列 + 两个时间戳。"""

    interval_days: float
    ease: float
    reps: int
    lapses: int
    last_review_at: str
    due_at: str


def schedule(state: SrsState, grade: str, now: datetime) -> Review:
    """SM-2 简化版（§8.3 逐字实现）。`grade` 取值 `again/hard/good/easy`。"""
    if grade not in GRADES:
        raise ValueError(f"不认识档位：{grade!r}")
    interval = float(state.interval_days or 0)
    ease = float(state.ease or 2.5)
    reps = int(state.reps or 0)
    lapses = int(state.lapses or 0)

    if grade == "again":
        interval = 0.0
        ease = max(EASE_MIN, ease - 0.20)
        lapses += 1
        reps = 0
    elif grade == "hard":
        interval = max(1.0, round(interval * 1.2))
        ease = max(EASE_MIN, ease - 0.15)
        reps += 1
    elif grade == "good":
        interval = (
            1.0 if reps == 0 else (3.0 if reps == 1 else float(round(interval * ease)))
        )
        reps += 1
    else:  # easy
        interval = 3.0 if reps == 0 else float(round(interval * ease * 1.3))
        ease = min(EASE_MAX, ease + 0.15)
        reps += 1

    due = (
        now + timedelta(minutes=AGAIN_DELAY_MINUTES)
        if interval <= 0
        else now + timedelta(days=interval)
    )
    return Review(
        interval_days=interval,
        ease=ease,
        reps=reps,
        lapses=lapses,
        last_review_at=format_ts(now),
        due_at=format_ts(due),
    )


# --- v1 markdown 解析（§4.4，只读）-----------------------------------------


def _strip_frontmatter(text: str) -> str:
    if not text.startswith("---"):
        return text
    end = text.find("\n---", 3)
    if end < 0:
        return text
    return text[end + 4 :]


def parse_vault_text(text: str) -> list[dict[str, str]]:
    """v1 条目（capture-tool.md §8.3 的行序）→ 字段 dict 列表。

    条目之间用空行分隔，但**元数据块前面也有一个空行**（§8.2 的样本），
    所以先把「全是 `- key: value`」的块并回上一条。
    """
    blocks: list[list[str]] = []
    for raw_block in _strip_frontmatter(text).split("\n\n"):
        lines = [line.rstrip() for line in raw_block.splitlines() if line.strip()]
        if not lines:
            continue
        if blocks and all(_ENTRY_META.match(line) for line in lines):
            blocks[-1].extend(lines)
        else:
            blocks.append(lines)

    entries: list[dict[str, str]] = []
    for block in blocks:
        if _ENTRY_META.match(block[0]):
            continue  # 没有宿主条目的孤儿元数据块
        body: list[str] = []
        meta: dict[str, str] = {}
        for line in block[1:]:
            match = _ENTRY_META.match(line)
            if match:
                meta[match.group(1).strip()] = match.group(2).strip()
            else:
                body.append(line.strip())
        if "?" in body:
            split = body.index("?")
            context, meaning = body[:split], body[split + 1 :]
        else:
            context, meaning = body, []
        entries.append(
            {
                "target": block[0].strip(),
                "context": " ".join(part for part in context if part),
                "meaning": " ".join(part for part in meaning if part),
                "source_title": meta.get("出处", ""),
                "source_app": _app_from_auto(meta.get("出处自动", "")),
                "position": meta.get("位置", ""),
                "tags": meta.get("标签", ""),
                "captured_at": meta.get("收录", ""),
            }
        )
    return entries


def _app_from_auto(auto: str) -> str:
    """`出处自动: Edge · "标题"` → `Edge`。"""
    head, _, _ = auto.partition("·")
    return head.strip()


# --- Store ------------------------------------------------------------------


class Store:
    """SQLite 门面。所有公开方法都是线程安全的（内部一把 RLock）。"""

    def __init__(
        self,
        db_path: Path | str,
        *,
        inbox_path: Path | str | None = None,
        idle_min: int = DEFAULT_IDLE_MIN,
        preset_tags_path: Path | str | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.inbox_path = (
            Path(inbox_path)
            if inbox_path is not None
            else self.db_path.parent / "inbox.jsonl"
        )
        # v6 二期（票 #85）：预设标签规则文件，默认与数据库同目录（照 `hotkey-status.json`）
        self.preset_tags_path = (
            Path(preset_tags_path)
            if preset_tags_path is not None
            else self.db_path.parent / PRESET_TAGS_NAME
        )
        self.idle_min = idle_min
        self._now = now or datetime.now
        self._lock = threading.RLock()
        # 票 #186（H12）：ingest 管线自己的锁（文件 IO + 串行化两条触发路径），
        # 与 `_lock` 分开 —— 大批积压时读文件不再持 store 全局锁卡所有 API。
        self._ingest_lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self.init_schema()

    # --- 基础 ---------------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def _query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params))

    def _one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def init_schema(self) -> None:
        """建表 → 补列 → 建索引 → 记版本（幂等，§4.4）。"""
        with self._write() as conn:
            for statement in _TABLES:
                conn.execute(statement)
            for table, columns in _COLUMNS.items():
                present = {
                    row["name"] for row in conn.execute(f"PRAGMA table_info({table})")
                }
                for name, ddl in columns.items():
                    if name not in present:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
            for statement in _INDEXES:
                conn.execute(statement)
            row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,)
                )
            elif int(row["version"]) < SCHEMA_VERSION:
                conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
            elif int(row["version"]) > SCHEMA_VERSION:
                log.warning(
                    "数据库 schema v%s 比本进程的 v%s 新，只读兼容继续",
                    row["version"],
                    SCHEMA_VERSION,
                )
        log.info("schema 就绪 v%s（%s）", SCHEMA_VERSION, self.db_path.name)

    def schema_version(self) -> int:
        row = self._one("SELECT version FROM schema_version LIMIT 1")
        return int(row["version"]) if row else 0

    def index_names(self) -> list[str]:
        return [
            str(row["name"])
            for row in self._query("SELECT name FROM sqlite_master WHERE type='index'")
        ]

    # --- 会话（§6.1）--------------------------------------------------------

    def session_plan(
        self,
        app: Any,
        title: Any,
        *,
        now: datetime | None = None,
        force_new: bool = False,
    ) -> Plan:
        """只读地算出「这条捕获会归到哪个会话」。ingest 与 session_peek 共用。

        `force_new=True` 是 §6.1 的「强制新会话」（外壳把会话 chip 切到「(新会话)」）：
        跳过复用查询 —— 当前 key 即便有活跃会话也照样新建（标题仍走 CleanTitle + 当天去重）。
        新会话用**同一个 key**，于是下一条自动归属的捕获（同一条视频、未超空闲）会命中它
        —— 这正是「本词条另起一个会话，下一条捕获回到自动归属」的语义。
        """
        moment = now or self._now()
        key = session_key(app, title)
        current = None
        if not force_new:
            current = self._one(
                "SELECT * FROM sessions WHERE key = ? ORDER BY last_active_at DESC, id DESC LIMIT 1",
                (key,),
            )
        taken = self._today_titles(moment)
        return plan_session(
            key=key,
            title=title,
            now=moment,
            idle_min=self.idle_min,
            current=dict(current) if current is not None else None,
            taken_titles=taken,
        )

    def _today_titles(self, moment: datetime) -> list[str]:
        day = moment.strftime("%Y-%m-%d")
        rows = self._query(
            "SELECT title FROM sessions WHERE substr(created_at, 1, 10) = ?", (day,)
        )
        return [str(row["title"]) for row in rows]

    def apply_session_plan(self, plan: Plan, *, app: Any, now: datetime) -> int:
        """把一个 `Plan` 落库：复用则刷新 `last_active_at`，否则新建。"""
        with self._write() as conn:
            if plan.reused and plan.session_id is not None:
                conn.execute(
                    "UPDATE sessions SET last_active_at = ? WHERE id = ?",
                    (format_ts(now), plan.session_id),
                )
                return plan.session_id
            cursor = conn.execute(
                """
                INSERT INTO sessions (key, title, source_app, created_at, last_active_at, note)
                VALUES (?, ?, ?, ?, ?, '')
                """,
                (plan.key, plan.title, one_line(app), format_ts(now), format_ts(now)),
            )
            log.info("新建会话（标题 %d 字）", len(plan.title))
            return int(cursor.lastrowid or 0)

    def _apply_session_context(
        self,
        conn: sqlite3.Connection,
        session_id: int,
        *,
        url: str,
        app: Any,
        title: Any,
        has_video: Any,
    ) -> None:
        """把本条捕获的 URL / 推断分类落到会话上（v3 §3）。

        - `url` 非空 → 覆盖会话 URL（「最新一次捕获时的值」）；
        - `category` **只在会话现值为空时写**（`guess_category`），用户手改过的非空值
          永不覆盖；
        - 日志只记字段数与 URL 长度，**URL / 标题本身不进日志**（v3 §2.7）。
        """
        row = conn.execute(
            "SELECT url, category FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None:
            return
        assignments: list[str] = []
        params: list[Any] = []
        if url:
            assignments.append("url = ?")
            params.append(url)
        if not one_line(row["category"]):
            assignments.append("category = ?")
            params.append(guess_category(app, title, url, has_video))
        if not assignments:
            return
        conn.execute(
            f"UPDATE sessions SET {', '.join(assignments)} WHERE id = ?",
            (*params, session_id),
        )
        log.info("会话上下文更新（%d 个字段，url %d 字）", len(assignments), len(url))

    def _machine_tags(
        self,
        session_id: int,
        *,
        app: str,
        title: str,
        context: str,
        preset: Mapping[str, Any] | None = None,
    ) -> str:
        """本条的机器预设标签（票 #85）：出处派生 + 关键词规则，归一成 `records.tags` 格式。

        会话字段取**刚写完该条捕获的上下文之后**的库内值（url / category / 账号三列）。
        读走 `_one`（同一把 `_lock`，票 #96 B3：旧写法拿裸 `self._conn` 直连，绕过了锁）。
        规则文件缺失或坏掉由 `read_preset_tags` 回落内置默认；`preset` 传进来就用它 ——
        批量 `retag` 靠这条做到**一次运行只读一次规则文件**（旧写法 409 条记录读 409 次）。
        全程只读文件 + 字符串匹配，**零网络**（账号解析是异步 enrichment，不在这条路径上）。
        """
        row = self._one(
            "SELECT url, category, account_name, account_id, account_source"
            " FROM sessions WHERE id = ?",
            (int(session_id),),
        )
        if row is None:
            return ""
        account = account_display(
            series_of(title),
            name=row["account_name"],
            account_id=row["account_id"],
            source=row["account_source"],
        )["account"]
        category = one_line(row["category"])
        rules = preset if preset is not None else read_preset_tags(self.preset_tags_path)
        return normalize_tags(
            preset_tags_for(
                rules,
                site=site_of_session(row["url"], title, app),
                account=account,
                category=CATEGORY_LABELS.get(category, category) if category else "",
                title=title,
                context=context,
            )
        )

    def session_peek(
        self, app: Any, title: Any, *, now: datetime | None = None
    ) -> dict[str, Any]:
        """外壳的会话 chip（§6.1）：报「当前会话 / 下一条会新建」，**不刷新活跃时间**。

        v3 §3 增补 `category`/`url`：复用会话时用会话现值，新建时用预测值
        （`guess_category`；此时还没有 URL，按应用名/标题推断）。
        """
        moment = now or self._now()
        plan = self.session_plan(app, title, now=moment)
        count = 0
        category = ""
        url = ""
        if plan.session_id is not None:
            row = self._one(
                "SELECT COUNT(*) AS n FROM records WHERE session_id = ?",
                (plan.session_id,),
            )
            count = int(row["n"]) if row else 0
            stored = self._one(
                "SELECT category, url FROM sessions WHERE id = ?", (plan.session_id,)
            )
            if stored is not None:
                category = one_line(stored["category"])
                url = str(stored["url"] or "")
        if not category:
            category = guess_category(app, title, url, None)
        return {
            "key": plan.key,
            "session_id": plan.session_id,
            "title": plan.title,
            "count": count,
            "is_new": not plan.reused,
            "idle_min": self.idle_min,
            "category": category,
            "url": url,
        }

    def list_sessions(self, *, include_empty: bool = False) -> list[dict[str, Any]]:
        """会话列表（按最近活跃倒序）。

        默认丢掉一个词条都不剩的空壳会话：删光某会话的词条后，它不该继续挂在侧栏
        （用户报障「删了测试会话还在」）。要看全部（含空壳）传 `include_empty=True`，
        `/api/sessions?include_empty=1` 走的就是这条。

        v33 T4（票 #210）：口径放宽一档 —— 「零词条但有待选句」的会话也列出
        （有 backlog 行就算），否则纯看视频的会话用户根本进不去挑词界面（「待挑 N」
        徽标挂在侧栏会话行上，spec §D17）。放宽只认**有 backlog 行**的会话：
        无词条无待选句的真空会话照旧不上榜；词条计数（`records`）依旧不被待选句
        伪造（ADR-0012 隔离口径不变）。

        票 #96 A1：额外带出 `raw_source_title` —— 会话**最早那条词条**的原始窗口标题
        （`records.source_title`，没过 `clean_title` 的 120 字截断）。`sessions.title` 已截断，
        >120 字标题的站点后缀不可恢复，读路径的站点判定只能靠这条原件现算；它只给进程内
        算派生字段用，**不进 API 出参**（hub 侧取走后就 pop 掉）。

        v33 T5（票 #211 / spec §E22）：每条会话带 `subtitle_pending`（未挑句数，
        `subtitle_pending_count` 的列表版）—— 侧栏「已挑 N 词条 / 待挑 M 句」摘要
        一次查询就拿齐，前端不必再逐会话打 /backlog/count。
        """
        having = (
            ""
            if include_empty
            else (
                "HAVING COUNT(r.id) > 0 OR EXISTS ("
                "SELECT 1 FROM subtitle_backlog b WHERE b.session_id = s.id)"
            )
        )
        rows = self._query(
            f"""
            SELECT s.*, COUNT(r.id) AS records, MAX(r.captured_at) AS last_captured_at,
                   (SELECT COUNT(*) FROM subtitle_backlog b
                     WHERE b.session_id = s.id AND b.picked_at IS NULL) AS subtitle_pending,
                   (SELECT r2.source_title FROM records r2
                     WHERE r2.session_id = s.id
                       AND TRIM(COALESCE(r2.source_title, '')) <> ''
                     ORDER BY r2.id LIMIT 1) AS raw_source_title
            FROM sessions s LEFT JOIN records r ON r.session_id = s.id
            GROUP BY s.id {having} ORDER BY s.last_active_at DESC, s.id DESC
            """
        )
        return [dict(row) for row in rows]

    def get_session(self, session_id: int) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM sessions WHERE id = ?", (int(session_id),))
        return dict(row) if row else None

    def delete_session(
        self, session_id: int, *, delete_records: bool = True
    ) -> dict[str, Any] | None:
        """删一个会话；`delete_records=True`（默认）连它下面的词条一起删。

        返回 `{"id", "records", "backlog"}`（records = 连带删掉的词条数，
        backlog = 连带删掉的待选句行数）；会话不存在返回 None。
        一个事务里删，避免留下「词条还在、会话没了」的孤儿行。

        v21 长文（票 #158）：文章挂在会话上（FK），所以删会话前先把它们**解绑**
        （`documents.session_id` 置 NULL）—— 不炸外键，也不悄悄烧掉正文；
        要连文章一起删走 `delete_document`。

        v35（票 #225 / spec「删会话的外键修复」）：`subtitle_backlog.session_id`
        只有普通 FK（不采用 `ON DELETE CASCADE`，避免 schema 迁移），所以删会话前
        显式删该会话的待选句行 —— 否则 `PRAGMA foreign_keys=ON` 下抛 IntegrityError，
        用户看到「删除会话失败」。底图**文件**不在本方法处理（交给孤儿清扫，票 #226）。
        """
        with self._lock, self._write() as conn:
            if (
                conn.execute(
                    "SELECT 1 FROM sessions WHERE id = ?", (int(session_id),)
                ).fetchone()
                is None
            ):
                return None
            unlinked = int(
                conn.execute(
                    "UPDATE documents SET session_id = NULL WHERE session_id = ?",
                    (int(session_id),),
                ).rowcount
            )
            backlog = int(
                conn.execute(
                    "DELETE FROM subtitle_backlog WHERE session_id = ?",
                    (int(session_id),),
                ).rowcount
            )
            removed = 0
            if delete_records:
                cursor = conn.execute(
                    "DELETE FROM records WHERE session_id = ?", (int(session_id),)
                )
                removed = int(cursor.rowcount)
            conn.execute("DELETE FROM sessions WHERE id = ?", (int(session_id),))
        log.info("会话删除（连带词条 %d 条、待选句 %d 句）", removed, backlog)
        if unlinked:
            log.info("会话删除时解绑文章 %d 篇", unlinked)
        return {"id": int(session_id), "records": removed, "backlog": backlog}

    def update_session(
        self, session_id: int, fields: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        """web 端改名 / 备注 / 分类 / 链接（§4.2 + v3 §3）。

        `category` 走 `normalize_category`（未命中 7 类回落 `other`）；`url` 只接受
        `http/https` ≤2048，非空非法值抛 `ValueError`（HTTP 层翻成 400），空值 = 清掉链接。
        """
        updates: dict[str, Any] = {}
        if "title" in fields:
            updates["title"] = clean_title(fields["title"]) or UNTITLED
        if "note" in fields:
            updates["note"] = one_line(fields["note"])
        if "category" in fields:
            updates["category"] = normalize_category(fields["category"])
        if "url" in fields:
            raw = "" if fields["url"] is None else str(fields["url"]).strip()
            if raw and not is_http_url(raw):
                raise ValueError("url 必须是 http/https 且不超过 2048 字符")
            updates["url"] = raw
        if not updates:
            return self.get_session(session_id)
        with self._write() as conn:
            if (
                conn.execute(
                    "SELECT 1 FROM sessions WHERE id = ?", (int(session_id),)
                ).fetchone()
                is None
            ):
                return None
            assignments = ", ".join(f"{name} = ?" for name in updates)
            conn.execute(
                f"UPDATE sessions SET {assignments} WHERE id = ?",
                (*updates.values(), int(session_id)),
            )
        log.info("会话更新（%d 个字段）", len(updates))
        return self.get_session(session_id)

    def _account_backoff_active(self, fetched_at: Any, moment: datetime) -> bool:
        """票 #91 的负缓存判定：`fetched_at` 距 `moment` 不足 `ACCOUNT_NEGATIVE_TTL_HOURS`？

        时间戳解析不了（旧库 / 坏数据）→ 视作**不**在退避窗口内（允许重试）。
        """
        fetched = parse_ts(fetched_at)
        if fetched is None:
            return False
        return moment - fetched < timedelta(hours=ACCOUNT_NEGATIVE_TTL_HOURS)

    def _upsert_video_meta(
        self,
        conn: sqlite3.Connection,
        *,
        video_id: str,
        site: str,
        title: str,
        account_name: str,
        account_id: str,
        moment: datetime,
    ) -> None:
        """写一条 `video_meta`（成功＝真实账号；失败＝账号列留空的负缓存）。须在 `_write()` 内调用。"""
        conn.execute(
            """
            INSERT INTO video_meta
                (video_id, site, title, account_name, account_id, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(video_id) DO UPDATE SET
                site = excluded.site, title = excluded.title,
                account_name = excluded.account_name,
                account_id = excluded.account_id, fetched_at = excluded.fetched_at
            """,
            (video_id, site, title, account_name, account_id, format_ts(moment)),
        )

    def resolve_session_account(
        self,
        session_id: int,
        *,
        opener: Callable[..., Any] = urlopen_call,
        now: datetime | None = None,
        online: bool = True,
    ) -> dict[str, Any]:
        """会话 → 账号（v6 三期 §账号解析）：查缓存 → 缺才出网 → 写 `video_meta` + 会话账号列。

        - 只走 `provenance.resolve_account`（非 B站 / 超时 / 坏 JSON 静默回空），所以本方法
          **不抛异常**、可安全放进后台线程；网络调用在库锁之外。
        - 会话账号三列**只在 `account_name` 现值为空时写**（照 `category` 的先例：写一次就
          不再被后来的空结果覆盖）。
        - `video_meta` 命中真实账号即零请求（票 #96 A5：**正向缓存永久有效**，没有过期时间 ——
          UP 主改名后展示名会陈旧。规格 US 16 只要求分组稳定（靠 `account_id`），所以接受）。
        - 票 #91：解析失败也写一条 `video_meta` 负缓存（账号列留空 + `fetched_at`），
          `ACCOUNT_NEGATIVE_TTL_HOURS` 小时内不再重试同一个视频 —— 否则每次打开取词台
          都重打同一个 BV（验收里同一 BV 被重试 59 次就是这个原因）。
        - 票 #96 A3：`online=False`（开关 `resolve_account_online` 关着）时**直接回空、零请求、
          零写库** —— 后台线程那一层的入口守卫挡不住「已进入本方法」的调用，所以闸门下沉到这里，
          由 hub 传**活的**开关属性。**残余在途窗口**：已经走进 `resolve_account` 的那次请求
          无法中断，会跑完（一个 BV 最多一次）；下一次调用立刻按开关生效。
        返回写下的账号字段，没解析出东西 → `{}`。
        """
        if not online:
            return {}
        row = self._one("SELECT * FROM sessions WHERE id = ?", (int(session_id),))
        if row is None:
            return {}
        video_id = video_id_of(row["url"])
        # 只对 B站开闸（YouTube 账号解析 Out of Scope）：不出网，也就不该留负缓存。
        if not is_bilibili_video_id(video_id):
            return {}
        moment = now or self._now()
        site = site_of(row["url"])
        title = clean_title(row["title"])
        meta = self._one("SELECT * FROM video_meta WHERE video_id = ?", (video_id,))
        if meta is not None and one_line(meta["account_name"]):
            account = {
                "account_name": one_line(meta["account_name"]),
                "account_id": one_line(meta["account_id"]),
            }
        elif meta is not None and self._account_backoff_active(meta["fetched_at"], moment):
            return {}  # 票 #91：负缓存命中 —— 零请求
        else:
            account = resolve_account(video_id, opener=opener)
            if not account:
                with self._write() as conn:
                    self._upsert_video_meta(
                        conn,
                        video_id=video_id,
                        site=site,
                        title=title,
                        account_name="",
                        account_id="",
                        moment=moment,
                    )
                log.info(
                    "视频 %s 账号解析失败，%d 小时内不再重试",
                    video_id,
                    ACCOUNT_NEGATIVE_TTL_HOURS,
                )
                return {}
        fields = {
            "account_name": account["account_name"],
            "account_id": account.get("account_id", ""),
            "account_source": "api",
        }
        with self._write() as conn:
            if meta is None or not one_line(meta["account_name"]):
                self._upsert_video_meta(
                    conn,
                    video_id=video_id,
                    site=site,
                    title=title,
                    account_name=fields["account_name"],
                    account_id=fields["account_id"],
                    moment=moment,
                )
            current = conn.execute(
                "SELECT account_name FROM sessions WHERE id = ?", (int(session_id),)
            ).fetchone()
            if current is not None and not one_line(current["account_name"]):
                conn.execute(
                    "UPDATE sessions SET account_name = ?, account_id = ?,"
                    " account_source = ? WHERE id = ?",
                    (
                        fields["account_name"],
                        fields["account_id"],
                        fields["account_source"],
                        int(session_id),
                    ),
                )
                log.info("会话账号写入（账号名 %d 字）", len(fields["account_name"]))
        return fields

    # --- 文章（长文，v21 票 #158 / 规格 §7 §8）------------------------------

    def create_document(
        self,
        text: Any,
        passages: Any,
        *,
        title: Any = "",
        session_id: int | None = None,
        source_app: Any = None,
        source_url: Any = None,
        format: Any = "ocr",
        captured_at: Any = None,
        segmentation_source: str | None = None,
    ) -> dict[str, Any]:
        """一篇文章落库，返回 `{id, session_id, title, passage_count, text_chars, ...}`。

        入参契约（T4 的 `article_create` 直接包这个方法，形状要稳）：

        - `text` **必须已经单行化**（外壳 `one_line` 口径）。本方法**不再折行**：
          段落偏移是按外壳那次单行化算的，**再折一次偏移全错**。这里只校验非空，
          **原样存储**（连 `strip` 都不做 —— 抹掉首空白同样会挪偏移）。
        - `passages` = `[[start, end), ...]` 字符偏移：有序、不重叠、
          `0 <= start <= end <= len(text)`；空列表合法（= 这篇文章没有分段）。
        - 非法入参**抛 `ValueError`**（T4 按 `str(exc)` 包错误码）：
          `"empty text"` / `"bad passages"` / `"session not found"`。
        - `session_id` 缺省时自己走一次 `session_plan(force_new=False)` 并落库；
          之后该文章的每条捕获都钉在这个会话上（**不参与 45 分钟空闲切分**）。
        - `title` 空 → `clean_title` 口径兜底（`（未命名）`）；`format` 只认
          `ocr` / `manual`，其余回落 `ocr`。
        - `segmentation_source`（v22 P1a / spec §6.7）：创建时的**原始分段输入**，
          可空。只做「`None` 或 `str`」校验，非法抛 `"bad segmentation source"`；
          内容原样落库（manual=粘贴原文、ocr=行框编码，见 `segmentation_source.py`）。
          列表/元信息查询**不返回**它（避免大字段进列表），只由访问器
          `segmentation_source(doc_id)` 单独取。
        - 日志只记字数与段数（§10 / B7）：正文、标题、URL **都不进日志**。
        """
        body = "" if text is None else str(text)
        if not body.strip():
            raise ValueError("empty text")
        breaks = passage_breaks(passages, len(body))
        moment = parse_ts(captured_at) or self._now()
        stamp = format_ts(moment)
        created = format_ts(self._now())
        cleaned = clean_title(title) or UNTITLED
        app = one_line(source_app)
        url = normalize_url(source_url)
        wanted = one_line(format).lower()
        fmt = wanted if wanted in DOCUMENT_FORMATS else "ocr"
        if segmentation_source is None:
            source = None
        elif isinstance(segmentation_source, str):
            source = segmentation_source
        else:
            raise ValueError("bad segmentation source")

        with self._lock:
            if session_id is None:
                # §8：一文一会话。只在**建文章**时归户一次，之后靠 doc_id 钉住
                plan = self.session_plan(app, cleaned, now=moment)
                resolved = self.apply_session_plan(plan, app=app, now=moment)
            else:
                resolved = int(session_id)
                existing_session = self._one(
                    "SELECT 1 FROM sessions WHERE id = ?", (resolved,)
                )
                if existing_session is None:
                    raise ValueError("session not found")
            with self._write() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO documents (
                        session_id, title, source_app, source_url, text, format,
                        passage_breaks, segmentation_source, captured_at, created_at,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        resolved,
                        cleaned,
                        app,
                        url,
                        body,
                        fmt,
                        json.dumps(breaks),
                        source,
                        stamp,
                        created,
                        created,
                    ),
                )
                doc_id = int(cursor.lastrowid or 0)
        log.info("文章入库 1 篇（正文 %d 字、%d 段）", len(body), len(breaks))
        return {
            "id": doc_id,
            "session_id": resolved,
            "title": cleaned,
            "format": fmt,
            "passage_count": len(breaks),
            "text_chars": len(body),
            "captured_at": stamp,
            "created_at": created,
            "updated_at": created,
        }

    def create_document_unless_dupe(
        self,
        match: Callable[[list[dict[str, Any]]], Any],
        text: Any,
        passages: Any,
        *,
        title: Any = "",
        session_id: int | None = None,
        source_app: Any = None,
        source_url: Any = None,
        format: Any = "ocr",
        captured_at: Any = None,
        segmentation_source: str | None = None,
    ) -> Any:
        """「同题候选判重 + 落库」收进**单次加锁**（票 #181 H3，审计 H3 的 TOCTOU）。

        原先 hub 先判重（锁已释放）再 `create_document`（另一次加锁），两步之间
        并发插进同题文章就会各建一篇碎片。这里在**同一把 `_lock`** 里：取同题候选
        （`documents_by_title`，RLock 可重入）→ 交给 `match(candidates)` 判定 ——
        返回 `None` 表示判 `new`，同一把锁里接着 `create_document`，返回它的
        document dict；返回非 `None` 表示命中既有文章（reused / updated / similar），
        **不落库**，判定原样回传（调用方以 `isinstance(outcome, tuple)` 分流）。

        入参与 `create_document` 一致（title 不必预清洗，候选查询与落库各自走
        `clean_title` 兜底）；`match` 里只做纯计算，不许再触发别的写路径。
        存量 `documents.title` 有重复（生产库核对过），所以**不建唯一索引**，
        并发防线只靠这一把锁（票 #181）。
        """
        with self._lock:
            decision = match(self.documents_by_title(title))
            if decision is not None:
                return decision
            return self.create_document(
                text,
                passages,
                title=title,
                session_id=session_id,
                source_app=source_app,
                source_url=source_url,
                format=format,
                captured_at=captured_at,
                segmentation_source=segmentation_source,
            )

    def document(self, doc_id: int) -> dict[str, Any] | None:
        """取一篇文章（含 `text` 与解析后的 `passages`）；不存在返回 None。

        返回**不含** `segmentation_source`（v22 P1a 的几 KB 大字段，由
        `segmentation_source(doc_id)` 单独取）：这个 dict 是 hub 定位/索引的热路径，
        不该为每次取文章把原始分段输入也读出来。

        给 hub 内部用（定位、`article_get` 的段落回话），**不要**把它整个回给外壳 ——
        全文走一条回话会被 `Socket.ahk` 的 UTF-8 跨拍截断弄坏（架构 §7.1）。
        """
        row = self._one(
            f"SELECT {', '.join(_DOCUMENT_COLUMNS)} FROM documents WHERE id = ?",
            (int(doc_id),),
        )
        if row is None:
            return None
        return self._document_from_row(row)

    @staticmethod
    def _document_from_row(row: sqlite3.Row) -> dict[str, Any]:
        """`documents` 行（`_DOCUMENT_COLUMNS` 显式列）→ document dict（含解析后的 passages）。"""
        article = dict(row)
        article["passages"] = parse_passages(article.get("passage_breaks"))
        article["passage_count"] = len(article["passages"])
        return article

    def segmentation_source(self, doc_id: int) -> str | None:
        """取一篇文章的**原始分段输入**（v22 P1a / spec §6.7）；不存在或 NULL → None。

        单独一个访问器而不是塞进 `document()` 的响应契约：这是可能几 KB 的大字段，
        只有「段落类型派生 / 重新分段」需要它；元信息与列表路径一律不读。
        """
        row = self._one(
            "SELECT segmentation_source FROM documents WHERE id = ?", (int(doc_id),)
        )
        if row is None or row["segmentation_source"] is None:
            return None
        return str(row["segmentation_source"])

    def list_documents(self, session_id: int | None = None) -> list[dict[str, Any]]:
        """文章元信息列表（**不带 `text`**），最近入库的在前。

        `session_id` 给了就只看那个会话的文章（取词台侧栏用）。
        """
        clause = "" if session_id is None else "WHERE session_id = ?"
        params: tuple[Any, ...] = () if session_id is None else (int(session_id),)
        rows = self._query(
            f"""
            SELECT id, session_id, title, source_app, source_url, format,
                   length(text) AS text_chars, passage_breaks,
                   captured_at, created_at, updated_at
            FROM documents {clause} ORDER BY captured_at DESC, id DESC
            """,
            params,
        )
        articles: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["passage_count"] = len(parse_passages(item.pop("passage_breaks")))
            articles.append(item)
        return articles

    def list_articles(
        self, *, limit: int = DEFAULT_ARTICLE_LIMIT, offset: int = 0
    ) -> dict[str, Any]:
        """文章目录（v22 P1b / spec §8.1 §8.6）：分页聚合，**不返回正文**。

        每行只回契约字段 `id/title/source_app/format/session_id/segments/records/
        needs_review/last_captured_at`：
        - `segments` = `passage_breaks` 的段数（坏 JSON → 0，与 `document()` 同口径）；
        - `records` / `needs_review` = 该文下词条数 / `passage_index = -1` 的待复核数；
        - `last_captured_at` = 该文最新一条词条的 `captured_at`，没有词条时退回文档的
          `captured_at`（侧栏「最近录入」用它，文章刚建还没记词也要显示时间）。
        排序 = **与显示口径同一把键**倒序：`COALESCE(MAX(词条 captured_at), 文档 captured_at)`
        （spec §8.1「最近录入倒序」；否则「文档建得早、后补词」的文章会排到后面，列表顺序
        与篇卡上显示的最近录入时间自相矛盾），同值时 `id DESC`。
        **双层校验语义**：本方法只做防御性钳制（`limit` 1–200 / `offset` ≥0，不抛错）——
        给内部调用兜底；HTTP 契约层（`hub._api_articles`）才是严格校验，非法参数一律
        400（两者不矛盾：对外严格、对内鲁棒）。
        """
        limit = max(1, min(int(limit), ARTICLE_LIMIT_MAX))
        offset = max(0, int(offset))
        total_row = self._one("SELECT COUNT(*) AS n FROM documents")
        rows = self._query(
            """
            SELECT d.id, d.session_id, d.title, d.source_app, d.format,
                   d.passage_breaks, d.captured_at,
                   (SELECT COUNT(*) FROM records r WHERE r.doc_id = d.id) AS records,
                   (SELECT COUNT(*) FROM records r
                     WHERE r.doc_id = d.id AND r.passage_index = -1) AS needs_review,
                   (SELECT MAX(r.captured_at) FROM records r WHERE r.doc_id = d.id)
                     AS last_record_at
            FROM documents d
            ORDER BY COALESCE(
                       (SELECT MAX(r.captured_at) FROM records r WHERE r.doc_id = d.id),
                       d.captured_at
                     ) DESC,
                     d.id DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        )
        articles: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["segments"] = len(parse_passages(item.pop("passage_breaks")))
            last_record_at = item.pop("last_record_at")
            captured_at = item.pop("captured_at")
            item["last_captured_at"] = last_record_at or captured_at
            articles.append(item)
        return {
            "total": int(total_row["n"]) if total_row else 0,
            "limit": limit,
            "offset": offset,
            "articles": articles,
        }

    def article_records(self, doc_id: int) -> list[dict[str, Any]]:
        """一篇文章下的全部词条（v22 P1b / spec §6.3 详情）；按 id 升序。

        详情页一次拉完（≈60 段 × 数条），正文只走 `document()`，这里不碰 `text`。
        `passage_index = -1`（待复核）也在这批里，由 hub 分区展示。
        """
        rows = self._query(
            "SELECT * FROM records WHERE doc_id = ? ORDER BY id ASC",
            (int(doc_id),),
        )
        return [dict(row) for row in rows]

    def delete_document(self, doc_id: int) -> dict[str, Any] | None:
        """删一篇文章：先把词条与它解绑，再删行；**词条一条不删**。

        `records.doc_id` / `passage_index` 置 NULL、`context`（原句）原样保留 ——
        删文章之后词条仍自洽、复习照常（规格 §7）。返回
        `{"ok": True, "id", "records"}`（records = 解绑的词条数）；文章不存在返回 None。
        """
        with self._lock, self._write() as conn:
            if (
                conn.execute(
                    "SELECT 1 FROM documents WHERE id = ?", (int(doc_id),)
                ).fetchone()
                is None
            ):
                return None
            cursor = conn.execute(
                "UPDATE records SET doc_id = NULL, passage_index = NULL WHERE doc_id = ?",
                (int(doc_id),),
            )
            unlinked = int(cursor.rowcount)
            conn.execute("DELETE FROM documents WHERE id = ?", (int(doc_id),))
        log.info("文章删除（解绑词条 %d 条）", unlinked)
        return {"ok": True, "id": int(doc_id), "records": unlinked}

    def documents_by_title(self, title: Any) -> list[dict[str, Any]]:
        """同标题候选（v22 P2a / spec §6.5）：**去重只和同标题文章比**，最新入库在前。

        传 `clean_title(...) or UNTITLED` 口径的标题（`create_document` 落库的也是这个），
        方法内部再走一遍 `clean_title` 兜底，避免调用方漏清洗导致候选集为空。
        返回完整 document dict（含 `text` 与解析后的 `passages`）—— 去重要比正文相似度，
        不能复用不带 `text` 的 `list_documents()`；同标题文章正常 0–2 篇，读全文可接受。

        性能：`documents.title` 没有索引，这条是**全表扫**。单用户单机量级（一天几十条、
        历史几千篇）下 + 只在 `article_create` 里调用一次可接受；真要优化就加 `title`
        索引（会动 schema，本轮不做，spec §6 的「不加表、不加列」优先）。
        """
        cleaned = clean_title(title) or UNTITLED
        rows = self._query(
            f"SELECT {', '.join(_DOCUMENT_COLUMNS)} FROM documents WHERE title = ? "
            "ORDER BY captured_at DESC, id DESC",
            (cleaned,),
        )
        return [self._document_from_row(row) for row in rows]

    def update_document(
        self,
        doc_id: int,
        *,
        text: Any = None,
        passages: Any = None,
        segmentation_source: Any = KEEP,
        format: Any = KEEP,
    ) -> dict[str, Any] | None:
        """改一篇文章的元数据 / 正文 / 段偏移（v22 P2a / spec §6.4 §6.5 §6.6）。

        入参契约（与 `create_document` 同口径，形状要稳）：

        - `text=None` = 不动正文；给了就必须是非空 `str`（纯空白 → `ValueError("empty text")`）。
        - `passages=None` = 不动段偏移；给了就按（新）正文长度走 `passage_breaks()` 校验，
          非法 → `ValueError("bad passages")`。
          ⚠️ **只给 `text` 不给 `passages` 时，旧的 `passage_breaks` 不会被重新校验** ——
          正文一换长度，旧偏移可能越界。这是调用方的责任：改正文必须同时给新段偏移
          （`article_resegment` / 合并更新走 `apply_resegment`，不会踩这个口子）；本方法
          只适合「只改 source/format/title 类元数据」或「正文与段偏移一起换」。
        - `segmentation_source` / `format` 不传（`KEEP`）= 不动；显式传 `None` 清空 source
          （format 仍按 `create_document` 口径回落 `ocr`）。
        - **只写真的变了的列**（正文不变就不动 `text`，段偏移不变就不动 `passage_breaks`），
          什么都没变则一次 UPDATE 都不发；`updated_at` 随实际写入同步。
        - 文章不存在 → `None`（RPC 层映射 `not found`）。

        事务：存在性检查 + 读旧值 + UPDATE 全在同一个 `_write()` 里（本方法自己的原子性，
        不是 spec 条款）。**重分段 / 合并更新不要用本方法**：那里必须把 documents 与
        records.passage_index 放进同一个事务，用 `apply_resegment()`（spec §6.6）。
        列表路径不返回 `segmentation_source`，所以这里单独 `SELECT` 它用于「变没变」比较。

        调用方说明（review 修复轮）：hub 的重分段 / 去重更新已全部改走 `apply_resegment`，
        本方法目前**没有内部调用方**，保留为非重分段的元数据/正文小改入口；不要因为
        「没人调用」而误以为可以拿它做重分段（那正是 review 抓到的双事务问题）。
        """
        doc_id = int(doc_id)
        with self._write() as conn:
            row = conn.execute(
                f"SELECT {', '.join(_DOCUMENT_COLUMNS)}, segmentation_source "
                "FROM documents WHERE id = ?",
                (doc_id,),
            ).fetchone()
            if row is None:
                return None
            body = str(row["text"])
            updates: dict[str, Any] = {}
            if text is not None:
                candidate = str(text)
                if not candidate.strip():
                    raise ValueError("empty text")
                body = candidate
                if body != row["text"]:
                    updates["text"] = body
            if passages is not None:
                breaks = passage_breaks(passages, len(body))
                if parse_passages(row["passage_breaks"]) != breaks:
                    updates["passage_breaks"] = json.dumps(breaks)
            if segmentation_source is not KEEP:
                if segmentation_source is None:
                    source = None
                elif isinstance(segmentation_source, str):
                    source = segmentation_source
                else:
                    raise ValueError("bad segmentation source")
                if source != row["segmentation_source"]:
                    updates["segmentation_source"] = source
            if format is not KEEP:
                wanted = one_line(format).lower()
                fmt = wanted if wanted in DOCUMENT_FORMATS else "ocr"
                if fmt != row["format"]:
                    updates["format"] = fmt
            if not updates:
                return self._document_from_row(row)
            updates["updated_at"] = format_ts(self._now())
            assignments = ", ".join(f"{name} = ?" for name in updates)
            conn.execute(
                f"UPDATE documents SET {assignments} WHERE id = ?",
                (*updates.values(), doc_id),
            )
        log.info("文章更新（%d 列）", len(updates) - 1)
        return self.document(doc_id)

    def apply_resegment(
        self,
        doc_id: int,
        text: Any,
        passages: Any,
        remaps: Mapping[int, int],
        *,
        segmentation_source: Any = KEEP,
        format: Any = KEEP,
    ) -> dict[str, Any] | None:
        """**单事务**：换正文/段偏移（+可选 source/format）并批量写词条 `passage_index`。

        v22 P2a review 修复轮：`article_resegment` 与去重 `updated` 原来是「documents
        一个事务 + records 第二个事务」，中途失败会留下「新正文 + 旧段号」的不一致状态。
        这里把两件事放进**同一个** `_write()`（同一 BEGIN/COMMIT）：documents 行与
        records.passage_index 要么一起成功、要么一起回滚。spec §6.6 只要求「词条跟着
        新分段走」，事务边界是实现层的正确性要求（spec 本身不提事务）。

        入参：`text` 非空（否则 `ValueError("empty text")`）；`passages` 走
        `passage_breaks()` 校验；`remaps` = `{record_id: passage_index}`（`-1` 合法，
        只改 `doc_id` 命中的行）。`segmentation_source` / `format` 不传（`KEEP`）= 保留
        旧值 —— OCR 合并更新请求没带 source 时**不丢旧 source**（review 修复轮）。
        文章不存在 → `None`（一行都不写）。成功回 `{"records": 实际改动条数}`。
        """
        doc_id = int(doc_id)
        body = "" if text is None else str(text)
        if not body.strip():
            raise ValueError("empty text")
        breaks = passage_breaks(passages, len(body))
        stamp = format_ts(self._now())
        updates: dict[str, Any] = {
            "text": body,
            "passage_breaks": json.dumps(breaks),
        }
        if segmentation_source is not KEEP:
            if segmentation_source is None:
                updates["segmentation_source"] = None
            elif isinstance(segmentation_source, str):
                updates["segmentation_source"] = segmentation_source
            else:
                raise ValueError("bad segmentation source")
        if format is not KEEP:
            wanted = one_line(format).lower()
            updates["format"] = wanted if wanted in DOCUMENT_FORMATS else "ocr"
        updates["updated_at"] = stamp
        rows = [
            (int(index), stamp, int(record_id), doc_id)
            for record_id, index in remaps.items()
        ]
        with self._write() as conn:
            if (
                conn.execute(
                    "SELECT 1 FROM documents WHERE id = ?", (doc_id,)
                ).fetchone()
                is None
            ):
                return None
            assignments = ", ".join(f"{name} = ?" for name in updates)
            conn.execute(
                f"UPDATE documents SET {assignments} WHERE id = ?",
                (*updates.values(), doc_id),
            )
            changed = 0
            if rows:
                cursor = conn.executemany(
                    "UPDATE records SET passage_index = ?, updated_at = ? "
                    "WHERE id = ? AND doc_id = ?",
                    rows,
                )
                changed = int(cursor.rowcount)
        log.info("文章重分段写入（%d 段 / 重映射 %d 条）", len(breaks), changed)
        return {"records": changed}

    # --- 词条 ---------------------------------------------------------------

    def capture(
        self,
        payload: Mapping[str, Any],
        *,
        now: datetime | None = None,
        pin_session_id: int | None = None,
    ) -> dict[str, Any]:
        """一条捕获落库（§4.1 唯一性 + §6.1 归户）。

        重复判定：同 `session_id` 内 `target` 折叠大小写唯一 → `hits+1`、
        `last_seen_at` 更新、`context` 原先空才补；`kind`/`meaning` 不覆盖已有非空值；
        标签取**并集**（票 #85）：机器预设标签并进已有标签，用户手写的一个不动。
        `payload.tags` 为空时按「出处派生 + 关键词规则」自动填（`_machine_tags`）。
        payload 里的 `new_session`（外壳的「(新会话)」chip）→ 强制新建会话（见
        `session_plan` 的 `force_new`）。

        v33 T4（票 #210 / spec §E21）：`pin_session_id` 非空 = 挑词的受控直调 ——
        会话由待选句钉死，**跳过 `session_plan`**（45 分钟空闲切分不适用：回老视频
        挑词必须归回原会话，不被同 key 的更新会话吸走）；会话不存在直接拒绝，
        绝不悄悄落到别的会话。只认关键字实参，payload 字段带不进来（spool 行
        走不到这条路）。

        v21 长文（票 #158）：`payload.doc_id` / `payload.passage_index`（都可选，spool
        缺省不写）非空时 —— `doc_id` 命中 `documents` 就**跳过 `session_plan`**、直接
        用 `documents.session_id`（长文不参与 45 分钟空闲切分）；`doc_id` 指向的文章
        不存在时**不许丢词**：丢掉这两个字段按普通捕获落库并记一行 warning（只记数字）。
        `context` 语义不变 —— 长文模式下它是**命中的那一句**，不是整段/全文。
        """
        moment = parse_ts(payload.get("captured_at")) or now or self._now()
        target = one_line(payload.get("target"))
        if not target:
            return {"ok": False, "error": "empty target"}
        app = one_line(payload.get("source_app"))
        title = one_line(payload.get("source_title"))
        context = one_line(payload.get("context"))
        meaning = one_line(payload.get("meaning"))
        kind = normalize_kind(payload.get("kind"))
        tags = normalize_tags(payload.get("tags"))
        url = normalize_url(payload.get("url"))
        has_video = payload.get("has_video")
        stamp = format_ts(moment)
        # v21 长文（票 #158）：`doc_id` 非空 = 这条词来自某篇文章。类型/空值先归一
        # （非整数 / 负数按「没有」处理），非空时用它钉住会话（不参与空闲切分）。
        doc_id = optional_int(payload.get("doc_id"), 1)
        # `passage_index` 只在有 doc_id 时有意义（单独给一个段号指向不了任何文章）
        passage_index = (
            optional_int(payload.get("passage_index"), 0) if doc_id is not None else None
        )

        with self._lock:
            pinned = False
            if pin_session_id is not None:
                # v33 T4（票 #210）：挑词直调的会话钉子 —— 会话不在了直接拒绝
                #（ValueError，hub 翻 404），绝不悄悄落到别的会话。
                if (
                    self._one(
                        "SELECT 1 FROM sessions WHERE id = ?", (int(pin_session_id),)
                    )
                    is None
                ):
                    raise ValueError(f"unknown session: {pin_session_id}")
                session_id = int(pin_session_id)
                pinned = True
            article = None
            if doc_id is not None:
                article = self._one(
                    "SELECT id, session_id FROM documents WHERE id = ?", (doc_id,)
                )
                if article is None:
                    # 不许丢词：文章不在了就当普通捕获落库（只记 doc_id 这个数字）
                    log.warning(
                        "长文捕获引用了不存在的文章（doc_id %s），按普通捕获落库",
                        doc_id,
                    )
                    doc_id = passage_index = None
            if article is not None and article["session_id"] is not None:
                # 长文钉住会话：**跳过 session_plan**，45 分钟空闲切分不参与
                #（读一篇长文必然超 45 分钟，现状会把它切碎）
                session_id = int(article["session_id"])
            elif pinned:
                # v33 T4（票 #210）：挑词直调的会话已钉住 —— session_plan 不参与
                #（同 key 的更新会话 / 空闲切分都不许把词条吸走）。
                pass
            else:
                plan = self.session_plan(
                    app, title, now=moment, force_new=bool(payload.get("new_session"))
                )
                session_id = self.apply_session_plan(plan, app=app, now=moment)
                if article is not None:
                    # 文章没有会话（老数据 / 手写行）→ 补上，之后这条长文就不再被切分
                    with self._write() as conn:
                        conn.execute(
                            "UPDATE documents SET session_id = ?"
                            " WHERE id = ? AND session_id IS NULL",
                            (session_id, int(article["id"])),
                        )
            existing = self._one(
                "SELECT * FROM records WHERE session_id = ? AND lower(target) = lower(?)",
                (session_id, target),
            )
            # v32（票 #200）：唯一落库入口的句子维度去重。命中同句时把新词条的
            # context 统一为既有句子文本，前端按 context 逐字并卡自然合成一张句卡。
            # 词级去重（上面的 existing）语义不变；长文词条 / 空原句一律跳过。
            # 候选查询与 existing 同在 self._lock 内（避免 TOCTOU），上限 20 条。
            sentence_context = context
            if doc_id is None and context:
                cutoff = moment - timedelta(minutes=10)
                candidates: list[dict[str, str]] = []
                for row in self._query(
                    "SELECT context, captured_at FROM records"
                    " WHERE session_id = ? AND doc_id IS NULL"
                    " AND context IS NOT NULL AND context <> ''"
                    " ORDER BY captured_at DESC, id DESC LIMIT 20",
                    (session_id,),
                ):
                    captured = parse_ts(row["captured_at"])
                    if captured is not None and captured >= cutoff:
                        candidates.append(
                            {
                                "context": str(row["context"]),
                                "captured_at": str(row["captured_at"]),
                            }
                        )
                matched = pick_same_sentence(context, candidates)
                if matched is not None:
                    # 守卫（spec 用户故事 7）：OCR 读错的正巧是目标词时（记的 just、
                    # 既有句子是 inst），并进去会让目标词在原句里消失 → 宁可不并。
                    if contains_target(matched, target):
                        sentence_context = matched
                    else:
                        log.info("同句命中但目标词不在既有原句中，保留本次原句（不并卡）")
            with self._write() as conn:
                if pinned:
                    # 挑词直调也算一次会话活动：last_active 照 reuse 语义刷新
                    #（apply_session_plan 的复用分支同款），侧栏排序跟着动。
                    conn.execute(
                        "UPDATE sessions SET last_active_at = ? WHERE id = ?",
                        (stamp, session_id),
                    )
                self._apply_session_context(
                    conn, session_id, url=url, app=app, title=title, has_video=has_video
                )
                # 票 #85：请求里带了标签就只认手写的（机器标签永不覆盖用户输入）；
                # 没带才算机器标签。dedup 分支走并集 = 已有 + 用户本次新带 + 机器，
                # 已有标签一个不动（机器在用户带了标签时为空，口径不变）。
                machine = (
                    ""
                    if tags
                    else self._machine_tags(
                        session_id, app=app, title=title, context=context
                    )
                )
                if existing is not None:
                    hits = int(existing["hits"]) + 1
                    conn.execute(
                        """
                        UPDATE records SET
                            hits = ?, last_seen_at = ?, context = ?, kind = ?, meaning = ?,
                            meaning_source = ?, phonetic = ?, dict_json = ?, tags = ?,
                            doc_id = COALESCE(doc_id, ?),
                            passage_index = COALESCE(passage_index, ?),
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            hits,
                            stamp,
                            existing["context"] or context,
                            existing["kind"] or kind,
                            existing["meaning"] or meaning,
                            existing["meaning_source"]
                            or one_line(payload.get("meaning_source")),
                            existing["phonetic"] or one_line(payload.get("phonetic")),
                            existing["dict_json"]
                            or _json_or_none(payload.get("dict_json")),
                            merge_tags(merge_tags(existing["tags"], tags), machine),
                            # 与「context 原句只在空时补」同一纪律：已有非空的长文归属不覆盖
                            doc_id,
                            passage_index,
                            stamp,
                            existing["id"],
                        ),
                    )
                    log.info("重复捕获（本会话第 %d 次）", hits)
                    # v23 §4.2：把**写入后**的释义一并回给 ingest（补释义触发用）；
                    # 原有键与语义一个不动，这两个键只是附加信息。
                    return {
                        "ok": True,
                        "dedup": True,
                        "hits": hits,
                        "id": int(existing["id"]),
                        "session_id": session_id,
                        "created": False,
                        "target": target,
                        "meaning": existing["meaning"] or meaning,
                    }
                tags = tags or machine
                cursor = conn.execute(
                    """
                    INSERT INTO records (
                        target, kind, context, meaning, meaning_source, phonetic, dict_json,
                        session_id, source_app, source_title, position, tags, hits,
                        captured_at, last_seen_at, due_at, interval_days, ease, reps, lapses,
                        doc_id, passage_index, created_at, updated_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, 0, 2.5, 0, 0,
                        ?, ?, ?, ?
                    )
                    """,
                    (
                        target,
                        kind,
                        sentence_context,
                        meaning,
                        one_line(payload.get("meaning_source")),
                        one_line(payload.get("phonetic")),
                        _json_or_none(payload.get("dict_json")),
                        session_id,
                        app,
                        title,
                        one_line(payload.get("position")),
                        tags,
                        stamp,
                        stamp,
                        stamp,  # due_at：新词 = captured_at，立刻可复习（§4.1）
                        doc_id,
                        passage_index,
                        stamp,
                        stamp,
                    ),
                )
        log.info("入库 1 条（target %d 字，context %d 字）", len(target), len(context))
        return {
            "ok": True,
            "dedup": False,
            "hits": 1,
            "id": int(cursor.lastrowid or 0),
            "session_id": session_id,
            "created": True,
            # v23 §4.2：新词条写入后的释义（附加键，原有键不变）
            "target": target,
            "meaning": meaning,
        }

    def get_record(self, record_id: int) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM records WHERE id = ?", (int(record_id),))
        return dict(row) if row else None

    def update_record(
        self, record_id: int, fields: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        """web 详情抽屉的编辑（§8.1）：释义/类型/标签/原句/位置/挪会话。"""
        updates: dict[str, Any] = {}
        if "meaning" in fields:
            updates["meaning"] = str(fields["meaning"] or "").strip()
            updates["meaning_source"] = one_line(fields.get("meaning_source")) or "user"
        elif "meaning_source" in fields:
            updates["meaning_source"] = one_line(fields["meaning_source"])
        if "kind" in fields:
            updates["kind"] = normalize_kind(fields["kind"])
        if "tags" in fields:
            updates["tags"] = normalize_tags(fields["tags"])
        if "context" in fields:
            updates["context"] = one_line(fields["context"])
        if "position" in fields:
            updates["position"] = one_line(fields["position"])
        if "session_id" in fields:
            updates["session_id"] = (
                int(fields["session_id"]) if fields["session_id"] else None
            )
        if not updates:
            return self.get_record(record_id)
        updates["updated_at"] = format_ts(self._now())
        with self._write() as conn:
            if (
                conn.execute(
                    "SELECT 1 FROM records WHERE id = ?", (int(record_id),)
                ).fetchone()
                is None
            ):
                return None
            target_session = updates.get("session_id")
            if target_session is not None and (
                conn.execute(
                    "SELECT 1 FROM sessions WHERE id = ?", (target_session,)
                ).fetchone()
                is None
            ):
                raise ValueError("session_id 不存在")
            assignments = ", ".join(f"{name} = ?" for name in updates)
            conn.execute(
                f"UPDATE records SET {assignments} WHERE id = ?",
                (*updates.values(), int(record_id)),
            )
        log.info("词条更新（%d 个字段）", len(updates))
        return self.get_record(record_id)

    def refill_meaning(
        self,
        record_id: int,
        *,
        meaning: str,
        phonetic: str = "",
        dict_json: str | None = None,
        machine: bool = False,
        expect_empty: bool = True,
        meaning_source: str | None = None,
    ) -> bool:
        """v23 §4.1 §5.1：把补查结果写回词条（单事务，返回是否写入）。

        - `expect_empty=True`（补释义 / 后台补齐的唯一口径）：**只在库里 `meaning`
          仍为空时写** —— 用户期间手改过的记录返回 False（任务里进 `skipped`）。
          v29 的单条重查是**用户明确确认后**的唯一例外：传 `expect_empty=False` 覆盖
          这一条非空释义，并由 Hub 的预览 token + 旧值比较兜住并发。
        - 只动 `meaning / phonetic / dict_json / meaning_source / updated_at`；
          `hits / tags / session / doc_id / passage_index / context` 一个不碰。
        - `meaning_source = "machine" if machine else ""`：与捕获路径（spool → records）
          同口径，**不是** `"user"`（那是详情抽屉手改的标记）。
        - v29：显式 `meaning_source="recheck"` 记录「用户手动重查」的来源；机翻仍写
          `"machine"`，只有显式来源与机翻都没有时才留空。
        - 记录不存在 → False。
        """
        stamp = format_ts(self._now())
        with self._write() as conn:
            row = conn.execute(
                "SELECT meaning FROM records WHERE id = ?", (int(record_id),)
            ).fetchone()
            if row is None:
                return False
            if expect_empty and one_line(row["meaning"]):
                return False
            conn.execute(
                """
                UPDATE records SET meaning = ?, phonetic = ?, dict_json = ?,
                    meaning_source = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    one_line(meaning),
                    one_line(phonetic),
                    _json_or_none(dict_json),
                    "machine" if machine else (meaning_source or ""),
                    stamp,
                    int(record_id),
                ),
            )
        log.info("补释义写入（%d 字）", len(one_line(meaning)))
        return True

    def update_segment_context(
        self, record_id: int, old_context: Any, new_context: Any
    ) -> dict[str, Any] | None:
        """v15 §B2（票 #128）：段级保存原句 —— 同一会话里原句逐字相同的词条一起改。

        「段」没有 id（规格 §0），用「同 `session_id` + `context` 全等」近似。
        一条 `UPDATE` 在一个事务里完成：要么整段改掉，要么一条都不动（前端循环 PATCH
        会部分失败、把一张归并卡裂成两张）。
        `session_id` 可能为 NULL（未归属词条），所以比较用 `IS` 而不是 `=`。
        `old_context` 是前端抽屉里的当前值（`expect_context`）：与库里不一致就抛
        `ValueError`（→ 400），防两个窗口并发改同一段时后写覆盖前写。
        返回 `{"affected": 命中条数, "record": 本条的最新值}`；记录不存在返回 None（→ 404）。
        """
        expected = one_line(old_context)
        fresh = one_line(new_context)
        stamp = format_ts(self._now())
        with self._write() as conn:
            row = conn.execute(
                "SELECT session_id, context FROM records WHERE id = ?",
                (int(record_id),),
            ).fetchone()
            if row is None:
                return None
            if one_line(row["context"]) != expected:
                raise ValueError("原句已被改过，请刷新后再试")
            cursor = conn.execute(
                "UPDATE records SET context = ?, updated_at = ? "
                "WHERE session_id IS ? AND context = ?",
                (fresh, stamp, row["session_id"], one_line(row["context"])),
            )
            affected = cursor.rowcount
        log.info("段级原句更新（%d 条）", affected)
        return {"affected": affected, "record": self.get_record(record_id)}

    def delete_record(self, record_id: int) -> bool:
        with self._write() as conn:
            cursor = conn.execute("DELETE FROM records WHERE id = ?", (int(record_id),))
        return bool(cursor.rowcount)

    def retag(self, *, apply: bool = False) -> dict[str, Any]:
        """按预设标签回填历史词条（票 #88）：出处派生 + 关键词规则，只做字符串匹配、**零网络**。

        默认 **dry-run**（`apply=False`）：只算「哪条会新增哪些标签」，**一个字节都不写库**，
        返回 `rows` 供人核对。`apply=True` 才写库，语义是**并集**（`merge_tags`）——机器算出
        的标签并进已有标签，**用户手写的标签一个不动**，因此重复跑幂等：第二遍算出的标签都已
        存在，`changed` 归 0、`rows` 为空。

        规则匹配同时看**会话标题**（`source_title`）与**词条原句**（`context`）；每条记录走的
        是捕获路径同一套算法（`_machine_tags`），会话字段取库内现值。

        票 #96 B3：规则文件**一次运行只读一次**（解析好的表传给 `_machine_tags`），库读全部
        走 `_query`/`_one` 的同一把 `_lock`。
        """
        preset = read_preset_tags(self.preset_tags_path)
        records = self._query(
            "SELECT id, target, tags, source_app, source_title, context, session_id"
            " FROM records ORDER BY id"
        )
        rows: list[dict[str, Any]] = []
        updates: list[tuple[str, int]] = []
        for record in records:
            session_id = record["session_id"]
            machine = (
                self._machine_tags(
                    session_id,
                    app=record["source_app"],
                    title=record["source_title"],
                    context=record["context"],
                    preset=preset,
                )
                if session_id is not None
                else ""
            )
            existing = str(record["tags"] or "")
            already = existing.split()
            add = [tag for tag in machine.split() if tag not in already]
            if not add:
                continue
            rows.append(
                {
                    "id": int(record["id"]),
                    "target": record["target"],
                    "add": add,
                    "tags": merge_tags(existing, machine),
                }
            )
            updates.append((rows[-1]["tags"], int(record["id"])))
        applied = False
        if apply and updates:
            stamp = format_ts(self._now())
            with self._write() as conn:
                for tags, record_id in updates:
                    conn.execute(
                        "UPDATE records SET tags = ?, updated_at = ? WHERE id = ?",
                        (tags, stamp, record_id),
                    )
            applied = True
        log.info(
            "预设标签回填：%d/%d 条将补标签（%s）",
            len(rows),
            len(records),
            "已写库" if applied else "dry-run",
        )
        return {
            "ok": True,
            "total": len(records),
            "changed": len(rows),
            "applied": applied,
            "rows": rows,
        }

    def list_records(
        self,
        *,
        query: str | None = None,
        session_id: int | None = None,
        tag: str | None = None,
        state: str | None = None,
        sort: str = DEFAULT_SORT,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> dict[str, Any]:
        """词表查询（§8.1 的搜索框 / 过滤 chip / 会话过滤）。"""
        where: list[str] = []
        params: list[Any] = []
        if query:
            pattern = f"%{_escape_like(query.strip())}%"
            where.append(
                r"(target LIKE ? ESCAPE '\' OR meaning LIKE ? ESCAPE '\'"
                r" OR context LIKE ? ESCAPE '\')"
            )
            params += [pattern, pattern, pattern]
        if session_id is not None:
            where.append("session_id = ?")
            params.append(int(session_id))
        if tag:
            # 票 #82：按分隔符精确匹配（tags 是空格分隔、每词带 `#`），
            # 不再用 LIKE 子串，`#B站` 就不会误命中 `#B站英语`。
            # 票 #96：剥 `#` 后为空的退化输入（`#` / 空白）不当过滤条件。
            word = tag.strip().lstrip("#").strip()
            if word:
                where.append("instr(' ' || tags || ' ', ?) > 0")
                params.append(f" #{word} ")
        moment = self._now()
        now_ts = format_ts(moment)
        if state in ("due", "待复习"):
            where.append("due_at IS NOT NULL AND due_at <= ?")
            params.append(now_ts)
        elif state in ("today", "今天"):
            start = moment.replace(hour=0, minute=0, second=0, microsecond=0)
            where.append("captured_at >= ? AND captured_at < ?")
            params += [format_ts(start), format_ts(start + timedelta(days=1))]
        elif state not in (None, "", "all", "全部"):
            raise ValueError(f"不认识的 state：{state}")
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        order = parse_sort(sort)
        limit = max(1, min(int(limit), MAX_LIMIT))
        offset = max(0, int(offset))
        total_row = self._one(f"SELECT COUNT(*) AS n FROM records {clause}", params)
        rows = self._query(
            f"SELECT * FROM records {clause} ORDER BY {order} LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        return {
            "total": int(total_row["n"]) if total_row else 0,
            "limit": limit,
            "offset": offset,
            "records": [dict(row) for row in rows],
        }

    def refill_candidates(
        self,
        *,
        ids: Sequence[int] | None = None,
        session_id: int | None = None,
        doc_id: int | None = None,
        limit: int = REFILL_LIMIT,
    ) -> list[dict[str, Any]]:
        """v23 §4.1：补释义候选（`[{"id", "target"}, …]`，按 id 升序）。

        三选一的筛选条件（调用方只传一个）：
        - `ids`：显式词条集合 —— 去重、**忽略不存在与非空释义**；
        - `session_id`：该会话下所有空释义记录；
        - `doc_id`：该文章下所有空释义记录。

        只回「还该补」的记录：`meaning` 为空（NULL / 空白）。一个都不传 = 不选任何东西。
        筛选口径放在本方法里，hub 的 HTTP 层只负责 scope 分派与 `limit` 校验。
        """
        limit = max(1, int(limit))
        if ids is not None:
            # 不去输入列表里截断：`limit` 是**结果**上限，先按 id 排序再取前 N 条，
            # 否则调用方传 ids 的顺序会决定补哪些词（同一请求两次结果可能不同）。
            # 票 #186（H10）：去重后的 id 数没有上限，整把 `IN (…)` 会超 SQLite
            # 变量上限直接 500 —— 分块查询、按 id 升序合并。`wanted` 升序 + 各块
            # id 区间递增，所以块序拼接仍全局有序，攒够 `limit` 条即可早停。
            wanted = sorted(dict.fromkeys(int(value) for value in ids))
            if not wanted:
                return []
            rows: list[sqlite3.Row] = []
            for start in range(0, len(wanted), REFILL_IDS_CHUNK):
                chunk = wanted[start : start + REFILL_IDS_CHUNK]
                marks = ",".join("?" for _ in chunk)
                rows.extend(
                    self._query(
                        f"SELECT id, target FROM records WHERE id IN ({marks}) "
                        f"AND {EMPTY_MEANING_SQL} ORDER BY id ASC",
                        chunk,
                    )
                )
                if len(rows) >= limit:
                    break
            rows = rows[:limit]
        elif session_id is not None:
            rows = self._query(
                "SELECT id, target FROM records WHERE session_id = ? "
                f"AND {EMPTY_MEANING_SQL} ORDER BY id ASC LIMIT ?",
                (int(session_id), limit),
            )
        elif doc_id is not None:
            rows = self._query(
                "SELECT id, target FROM records WHERE doc_id = ? "
                f"AND {EMPTY_MEANING_SQL} ORDER BY id ASC LIMIT ?",
                (int(doc_id), limit),
            )
        else:
            return []
        return [{"id": int(row["id"]), "target": str(row["target"])} for row in rows]

    def refill_pending_count(
        self,
        *,
        session_id: int | None = None,
        doc_id: int | None = None,
    ) -> int:
        """v28：某个会话 / 文章里仍为空的释义数（与 `refill_candidates` 同一筛选口径）。

        打开会话 / 文章时的提示条要的是**精确**计数，不能用前端已载入的那一页记录
        近似（`/api/records` 有 500 条上限）；两条筛选条件互斥，都不传 = 0。
        """
        if session_id is not None:
            clause, params = "session_id = ?", (int(session_id),)
        elif doc_id is not None:
            clause, params = "doc_id = ?", (int(doc_id),)
        else:
            return 0
        row = self._one(
            f"SELECT COUNT(*) AS n FROM records WHERE {clause} "
            f"AND {EMPTY_MEANING_SQL}",
            params,
        )
        return int(row["n"]) if row else 0

    def review(
        self, record_id: int, grade: str, *, now: datetime | None = None
    ) -> dict[str, Any] | None:
        """按 §8.3 打分：改 `due_at/interval/ease/reps/lapses`。

        票 #186（H7）：读旧值、算新值、UPDATE 收进**同一把 `_write()`** —— 原来读
        在锁外，两个线程同时给同一条打分会都基于旧 `reps` 计算，后写覆盖先写
        （SRS 计数失真）。事务里直接用 conn 查（`_lock` 是 RLock，不必再走 `_one`）。
        """
        moment = now or self._now()
        with self._write() as conn:
            row = conn.execute(
                "SELECT * FROM records WHERE id = ?", (int(record_id),)
            ).fetchone()
            if row is None:
                return None
            record = dict(row)
            state = SrsState(
                interval_days=float(record["interval_days"] or 0),
                ease=float(record["ease"] or 2.5),
                reps=int(record["reps"] or 0),
                lapses=int(record["lapses"] or 0),
            )
            result = schedule(state, grade, moment)
            conn.execute(
                """
                UPDATE records SET interval_days = ?, ease = ?, reps = ?, lapses = ?,
                    last_review_at = ?, due_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    result.interval_days,
                    result.ease,
                    result.reps,
                    result.lapses,
                    result.last_review_at,
                    result.due_at,
                    format_ts(moment),
                    int(record_id),
                ),
            )
        log.info("复习打分完成（档位 %s，间隔 %.1f 天）", grade, result.interval_days)
        return self.get_record(record_id)

    def seen(self, target: str) -> dict[str, Any] | None:
        """`lookup` 的 `seen`（§7）：这个词以前记过几次、上次在哪个会话。"""
        word = one_line(target)
        if not word:
            return None
        summary = self._one(
            """
            SELECT COUNT(*) AS count, MIN(captured_at) AS first_at, MAX(last_seen_at) AS last_at
            FROM records WHERE lower(target) = lower(?)
            """,
            (word,),
        )
        if not summary or not summary["count"]:
            return None
        last = self._one(
            """
            SELECT s.title AS title, r.last_seen_at AS last_seen_at
            FROM records r LEFT JOIN sessions s ON s.id = r.session_id
            WHERE lower(r.target) = lower(?)
            ORDER BY COALESCE(r.last_seen_at, r.captured_at) DESC, r.id DESC LIMIT 1
            """,
            (word,),
        )
        return {
            "count": int(summary["count"]),
            "first_at": summary["first_at"],
            "last_at": summary["last_at"],
            "last_session": (last["title"] if last else "") or "",
        }

    # --- 待选句（v33 T1，票 #207 / spec §A / ADR-0012）-----------------------

    def add_subtitle_lines(
        self, session_id: int, lines: Sequence[Mapping[str, Any]]
    ) -> dict[str, int]:
        """待选句入库（幂等）：一行一句，唯一键 `(session_id, sentence_key)`。

        - `sentence_key` 复用同句判定的规范化键（sentence_match.sentence_key）：
          大小写/标点差异算同一句，同句在轨里重复出现、CC/OCR/ASR 同句合流、
          重复查轨都由唯一索引挡住（票 #207 AC2）。冲突时按**来源优先级择优**
          （spec §B2a：CC(3) > ASR(2) > OCR(1)，同 rank 取 score 高者）：高者
          接管 `text/source/from_s/to_s/score`，低 rank 绝不覆盖高 rank；
          `image_path`/`suspicious` 不随接管变化（CC 接管 OCR 文本时原底图与
          可疑标保留，可追溯）；
        - 坏行（空文本 / 规范化后空键 / 非法来源 / 时间点不是有限数 / 类型不对）
          跳过并计数，不打断整批 —— 与 `capture` 的「坏字段丢掉，词照落库」同一纪律；
        - 日志只记数量，不记句文本（§10 日志纪律）。
        """
        if self._one("SELECT id FROM sessions WHERE id = ?", (session_id,)) is None:
            raise ValueError(f"unknown session: {session_id}")
        added = merged = duplicates = invalid = 0
        moment = format_ts(self._now())
        with self._write() as conn:
            for raw in lines:
                if not isinstance(raw, Mapping):
                    invalid += 1
                    continue
                text = one_line(raw.get("text"))
                source = one_line(raw.get("source"))
                key = sentence_key(text)
                try:
                    from_s = float(raw.get("from_s"))  # type: ignore[arg-type]
                    to_s = float(raw.get("to_s"))  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    invalid += 1
                    continue
                if (
                    not text
                    or not key
                    or source not in BACKLOG_SOURCES
                    or not is_latin_line(text)
                ):
                    invalid += 1
                    continue
                if not (math.isfinite(from_s) and math.isfinite(to_s)):
                    invalid += 1
                    continue
                raw_score = raw.get("score")
                try:
                    score = None if raw_score is None else float(raw_score)
                except (TypeError, ValueError):
                    invalid += 1
                    continue
                if score is not None and not math.isfinite(score):
                    score = None
                image_path = raw.get("image_path")
                image_path = str(image_path).strip() if image_path is not None else None
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO subtitle_backlog
                        (session_id, text, source, from_s, to_s, score, suspicious,
                         image_path, sentence_key, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        text,
                        source,
                        from_s,
                        to_s,
                        score,
                        1 if raw.get("suspicious") else 0,
                        image_path or None,
                        key,
                        moment,
                        moment,
                    ),
                )
                if cursor.rowcount:
                    added += 1
                else:
                    existing = conn.execute(
                        "SELECT source, score FROM subtitle_backlog"
                        " WHERE session_id = ? AND sentence_key = ?",
                        (session_id, key),
                    ).fetchone()
                    if existing is not None and _merge_wins(
                        source, score, str(existing["source"]), existing["score"]
                    ):
                        # 接管只动文本/来源/时间窗/分数：底图与可疑标原地保留
                        conn.execute(
                            "UPDATE subtitle_backlog"
                            " SET text = ?, source = ?, from_s = ?, to_s = ?,"
                            " score = ?, updated_at = ?"
                            " WHERE session_id = ? AND sentence_key = ?",
                            (text, source, from_s, to_s, score, moment,
                             session_id, key),
                        )
                        merged += 1
                    else:
                        duplicates += 1
        total = self._one(
            "SELECT COUNT(*) AS n FROM subtitle_backlog WHERE session_id = ?",
            (session_id,),
        )
        log.info(
            "待选句入库（新增 %d 接管 %d 重复 %d 无效 %d）",
            added, merged, duplicates, invalid,
        )
        return {
            "added": added,
            "merged": merged,
            "duplicates": duplicates,
            "invalid": invalid,
            "total": int(total["n"]) if total else 0,
        }

    def subtitle_sentences(self, session_id: int) -> list[dict[str, Any]]:
        """该会话的待选句，按时间点排序（spec 用户故事 2：按看过的顺序挑词）。"""
        rows = self._query(
            """
            SELECT id, session_id, text, source, from_s, to_s, score, suspicious,
                   image_path, sentence_key, picked_at, created_at, updated_at
            FROM subtitle_backlog
            WHERE session_id = ?
            ORDER BY from_s ASC, to_s ASC, id ASC
            """,
            (session_id,),
        )
        return [{**dict(row), "suspicious": bool(row["suspicious"])} for row in rows]

    def subtitle_sentence(
        self, session_id: int, sentence_id: int
    ) -> dict[str, Any] | None:
        """单条待选句（挑词 / 改句前先取行：归属校验在这里一并做）。"""
        row = self._one(
            """
            SELECT id, session_id, text, source, from_s, to_s, score, suspicious,
                   image_path, sentence_key, picked_at, created_at, updated_at
            FROM subtitle_backlog
            WHERE session_id = ? AND id = ?
            """,
            (int(session_id), int(sentence_id)),
        )
        if row is None:
            return None
        return {**dict(row), "suspicious": bool(row["suspicious"])}

    def subtitle_pending_count(self, session_id: int) -> int:
        """「待挑 N」计数（spec §D17 入口徽标 / §D20 只数未挑句）。

        T4（票 #210）起挑词落库给句子打已挑标记（`picked_at`）—— 口径收窄成
        「未挑句数」，调用方不用改。
        """
        row = self._one(
            "SELECT COUNT(*) AS n FROM subtitle_backlog"
            " WHERE session_id = ? AND picked_at IS NULL",
            (session_id,),
        )
        return int(row["n"]) if row else 0

    def band_image_references(self) -> list[dict[str, Any]]:
        """底图引用行（v35 #226 / spec「Store 新增三个窄方法」）：清理对账的唯一输入。

        只取 `image_path` 非空的待选句（一行一句，`id / image_path / picked_at /
        updated_at`）。**不按 `source` 过滤**：CC 接管 OCR 文本时底图原地保留
        （`add_subtitle_lines` 的接管语义），若按 `source='ocr'` 过滤会把仍有引用的
        图误判成孤儿 —— 而「未挑图永不自动删」是硬约束。
        """
        rows = self._query(
            "SELECT id, session_id, image_path, picked_at, updated_at"
            " FROM subtitle_backlog"
            " WHERE image_path IS NOT NULL AND image_path != '' ORDER BY id"
        )
        return [dict(row) for row in rows]

    def clear_band_image_paths(self, ids: Sequence[int]) -> int:
        """按 id 批量置空 `image_path`（v35 #226），返回实际置空行数。

        行本身（句子 / 已挑标记 / 词条）一律不动；已置空的行再传进来返回 0
        —— 清理 API 的幂等就靠这条。
        """
        wanted = [int(value) for value in ids]
        if not wanted:
            return 0
        placeholders = ",".join("?" * len(wanted))
        with self._write() as conn:
            cursor = conn.execute(
                f"UPDATE subtitle_backlog SET image_path = NULL"
                f" WHERE image_path IS NOT NULL AND id IN ({placeholders})",
                tuple(wanted),
            )
            return int(cursor.rowcount)

    def delete_subtitle_lines(self, session_id: int) -> int:
        """删该会话**全部**待选句行，返回行数（v35 #227 `all` 模式 / #228 手动清空）。

        只动 `subtitle_backlog`：会话、词条、复习字段全部不碰（spec §手动清理 API
        `all` 的语义 —— 词条库一条不动）。重复调用返回 0（幂等）。
        """
        with self._write() as conn:
            cursor = conn.execute(
                "DELETE FROM subtitle_backlog WHERE session_id = ?", (int(session_id),)
            )
            return int(cursor.rowcount)

    def session_record_count(self, session_id: int) -> int:
        """该会话的词条数（v33 修复轮 / spec §E22）。

        挑词界面「已挑 N 词条」的服务端口径：以 records 表该会话行数为准，
        重开页面与本页乐观计数不再各说各话。
        """
        row = self._one(
            "SELECT COUNT(*) AS n FROM records WHERE session_id = ?",
            (int(session_id),),
        )
        return int(row["n"]) if row else 0

    def pick_subtitle_sentence(
        self,
        session_id: int,
        sentence_id: int,
        *,
        text: str,
        kind: str = "word",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """挑词落库（v33 T4，票 #210 / spec §E21）：待选句 → `capture()` 直调。

        词条在 spool 之外**显式新增的第二条受控入口**（hub 侧主动行为，无外壳
        往返）：`target` = 所选文本、`context` = 待选句原文、`position` = from_s
        的 mm:ss、出处随视频会话、归该视频会话（`pin_session_id` 钉住，45 分钟
        空闲切分不适用 —— 回老视频挑词必须归回原会话）。`capture()` 内部各层
        —— 词级去重、同句合并、会话归户 —— 全部自动生效。

        成功后该句打已挑标记（`picked_at`，持久化，spec §D20）；「待挑 N」随之
        收窄。回包在 `capture()` 结果上附加 `pending`（未挑句数）；hub 端点回话
        `{ok, record_id, pending}`（v34 T1 起 additive 扩 `meaning` + `fill`）。

        句子不存在 / 不属于该会话 / 所选文本为空 → `ValueError`（hub 翻成 404/400）。
        """
        sentence = self.subtitle_sentence(session_id, sentence_id)
        if sentence is None:
            raise ValueError(f"unknown subtitle sentence: {sentence_id}")
        target = one_line(text)
        if not target:
            raise ValueError("empty pick text")
        session = self.get_session(session_id)
        moment = now or self._now()
        result = self.capture(
            {
                "target": target,
                "kind": kind,
                "context": str(sentence["text"]),
                "position": _position_text(float(sentence["from_s"])),
                "source_app": (session or {}).get("source_app"),
                "source_title": (session or {}).get("title"),
                "captured_at": format_ts(moment),
            },
            now=moment,
            pin_session_id=int(session_id),
        )
        if result.get("ok"):
            stamp = format_ts(moment)
            with self._write() as conn:
                conn.execute(
                    "UPDATE subtitle_backlog SET picked_at = ?, updated_at = ?"
                    " WHERE id = ?",
                    (stamp, stamp, int(sentence_id)),
                )
        result["sentence_id"] = int(sentence_id)
        result["pending"] = self.subtitle_pending_count(session_id)
        return result

    def unpick_subtitle_sentence(
        self, session_id: int, record_id: int
    ) -> dict[str, Any] | None:
        """挑词撤销（v33 修复轮 / spec §D19）：删词条，句子按残留词条回未挑。

        词条与待选句是弱关联（文本同源）：撤销按**词条的 context** 找回句子。
        删掉词条后该会话再无词条 context 等于该待选句原文 → 清该句 `picked_at`
        （句子回未挑、pending 回升）；同句还有别的词条（v32 同句合并的卡各自
        是一条记录）→ 已挑标记保留。hits 合并语义不受影响：撤销删的是**整张
        词条卡**（重复挑词攒的 hits 随卡一起消失）。

        词条不存在 / 不属于该会话返回 None（hub 翻成 404，跨会话撤销拒绝）。
        回包：`{"removed", "cleared", "pending"}`（cleared = 清掉的已挑标记数，
        0 或 1 —— (session_id, sentence_key) 唯一）。
        """
        record = self.get_record(int(record_id))
        if record is None or record["session_id"] != int(session_id):
            return None
        context = str(record["context"] or "")
        removed = self.delete_record(int(record_id))
        key = sentence_key(one_line(context))
        remaining = self._one(
            "SELECT COUNT(*) AS n FROM records WHERE session_id = ? AND context = ?",
            (int(session_id), context),
        )
        cleared = 0
        if key and int(remaining["n"]) == 0:
            moment = format_ts(self._now())
            with self._write() as conn:
                cursor = conn.execute(
                    "UPDATE subtitle_backlog SET picked_at = NULL, updated_at = ?"
                    " WHERE session_id = ? AND sentence_key = ? AND picked_at IS NOT NULL",
                    (moment, int(session_id), key),
                )
                cleared = int(cursor.rowcount)
        if removed:
            log.info("挑词撤销（词条 %d，句子回未挑 %d）", int(record_id), cleared)
        return {
            "removed": bool(removed),
            "cleared": cleared,
            "pending": self.subtitle_pending_count(session_id),
        }

    def update_subtitle_text(
        self,
        session_id: int,
        sentence_id: int,
        text: Any,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        """看图改句（v33 T4，票 #210 / spec §D19）：更新原句并重算 sentence_key。

        原句变了 = 旧挑词的词下标全部作废 → 已挑标记一并清除（句子回未挑态，
        与前端「保存改句后清空本句挑词」同一语义）。改句沿用收录契约：空文本 /
        规范化后空键 / 非拉丁行一律 `ValueError`（与 `add_subtitle_lines` 同一门）。
        行不存在（或不在该会话下）返回 None —— hub 翻成 404。
        """
        return self._update_subtitle_row(session_id, sentence_id, text, now=now)

    def update_subtitle_ocr(
        self,
        session_id: int,
        sentence_id: int,
        *,
        text: Any,
        score: Any,
        suspicious: Any,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        """单句重识别回写（v33 修复轮 / spec §D19）：text/score/suspicious 一起换。

        与看图改句同一套门与同一语义（原句变了 → 已挑标记清除、sentence_key 重算）
        ，只是多带回识别的 `score`（置信度均值）与 `suspicious`（可信门重判）。
        """
        return self._update_subtitle_row(
            session_id,
            sentence_id,
            text,
            now=now,
            score=score,
            suspicious=suspicious,
        )

    def _update_subtitle_row(
        self,
        session_id: int,
        sentence_id: int,
        text: Any,
        *,
        now: datetime | None = None,
        score: Any = _UNSET,
        suspicious: Any = _UNSET,
    ) -> dict[str, Any] | None:
        """改句/重识别共用的行更新：文本门 → UPDATE → 回读。"""
        sentence = self.subtitle_sentence(session_id, sentence_id)
        if sentence is None:
            return None
        fresh = one_line(text)
        if not fresh:
            raise ValueError("text 必填")
        key = sentence_key(fresh)
        if not key:
            raise ValueError("改后原句规范化后为空")
        if not is_latin_line(fresh):
            raise ValueError("改后原句不是拉丁行")
        moment = format_ts(now or self._now())
        # 占位顺序固定：text / sentence_key / picked_at（无参置 NULL）/ updated_at，
        # 可选的 score / suspicious 追加在后。
        assignments = [
            "text = ?",
            "sentence_key = ?",
            "picked_at = NULL",
            "updated_at = ?",
        ]
        params: list[Any] = [fresh, key, moment]
        if score is not _UNSET:
            assignments.append("score = ?")
            params.append(None if score is None else float(score))
        if suspicious is not _UNSET:
            assignments.append("suspicious = ?")
            params.append(1 if suspicious else 0)
        with self._write() as conn:
            conn.execute(
                f"UPDATE subtitle_backlog SET {', '.join(assignments)} WHERE id = ?",
                (*params, int(sentence_id)),
            )
        log.info("待选句改句（%d 字）", len(fresh))
        updated = self.subtitle_sentence(session_id, sentence_id)
        assert updated is not None  # 同一事务刚更新过，行必然还在
        return updated

    # --- 统计与导出 ---------------------------------------------------------

    def stats(self, days: int = 30) -> dict[str, Any]:
        days = max(1, min(int(days), 3650))
        moment = self._now()
        start = moment.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
            days=days - 1
        )
        start_ts = format_ts(start)
        now_ts = format_ts(moment)
        captures = {
            row["day"]: int(row["n"])
            for row in self._query(
                """
                SELECT substr(captured_at, 1, 10) AS day, COUNT(*) AS n FROM records
                WHERE captured_at >= ? GROUP BY day
                """,
                (start_ts,),
            )
        }
        reviews = {
            row["day"]: int(row["n"])
            for row in self._query(
                """
                SELECT substr(last_review_at, 1, 10) AS day, COUNT(*) AS n FROM records
                WHERE last_review_at IS NOT NULL AND last_review_at >= ? GROUP BY day
                """,
                (start_ts,),
            )
        }
        by_day = [
            {
                "date": (start + timedelta(days=offset)).strftime("%Y-%m-%d"),
                "captures": captures.get(
                    (start + timedelta(days=offset)).strftime("%Y-%m-%d"), 0
                ),
                "reviews": reviews.get(
                    (start + timedelta(days=offset)).strftime("%Y-%m-%d"), 0
                ),
            }
            for offset in range(days)
        ]
        totals = self._one(
            """
            SELECT COUNT(*) AS records,
                   COALESCE(SUM(hits), 0) AS hits,
                   SUM(CASE WHEN due_at IS NOT NULL AND due_at <= ? THEN 1 ELSE 0 END) AS due_now,
                   SUM(CASE WHEN reps > 0 THEN 1 ELSE 0 END) AS reviewed
            FROM records
            """,
            (now_ts,),
        )
        counts = self._one("SELECT COUNT(*) AS sessions FROM sessions")
        tags: dict[str, int] = {}
        for row in self._query(
            "SELECT tags FROM records WHERE tags IS NOT NULL AND tags <> ''"
        ):
            for tag in str(row["tags"]).split():
                tags[tag] = tags.get(tag, 0) + 1
        return {
            "days": days,
            "since": start_ts,
            "records": int(totals["records"]) if totals else 0,
            "hits": int(totals["hits"]) if totals else 0,
            "reviewed": int(totals["reviewed"] or 0) if totals else 0,
            "due_now": int(totals["due_now"] or 0) if totals else 0,
            "sessions": int(counts["sessions"]) if counts else 0,
            "by_day": by_day,
            # 票 #87：标签全量返回（原截前 30），侧栏标签云要能看到全部标签。
            "tags": [
                {"tag": tag, "count": count}
                for tag, count in sorted(tags.items(), key=lambda kv: (-kv[1], kv[0]))
            ],
        }

    def export_payload(self) -> dict[str, Any]:
        """JSON 导出（数据自由保底）：**默认不含文章正文**（v21 §9.4）。

        `documents.text` 是几十 KB 一篇的全文，导出会暴涨；这里一条 `documents` 都不带
        （元信息要导是显式 opt-in 的事，本期没有）。`export_markdown` 同样永远不碰正文。
        """
        moment = self._now()
        return {
            "exported_at": format_ts(moment),
            "schema_version": self.schema_version(),
            "sessions": self.list_sessions(),
            "records": [
                dict(row)
                for row in self._query("SELECT * FROM records ORDER BY captured_at, id")
            ],
        }

    def export_markdown(self) -> str:
        """v1 条目格式（capture-tool.md §8），给「数据自由」保底。"""
        moment = self._now()
        lines = [
            "---",
            f'title: "英语生词（导出 {moment:%Y-%m-%d}）"',
            f"date: {moment:%Y-%m-%d}",
            "tags:",
            "  - 英语生词",
            "  - flashcards/english",
            "category: 英语生词",
            "---",
            "",
        ]
        for record in self._query("SELECT * FROM records ORDER BY captured_at, id"):
            lines.append(str(record["target"]))
            if record["context"]:
                lines.append(str(record["context"]))
            if record["meaning"]:
                lines += ["?", str(record["meaning"])]
            lines.append("")
            if record["source_title"]:
                lines.append(f"- 出处: {record['source_title']}")
            if record["position"]:
                lines.append(f"- 位置: {record['position']}")
            if record["tags"]:
                lines.append(f"- 标签: {record['tags']}")
            lines.append(f"- 收录: {str(record['captured_at'])[:16]}")
            if not record["meaning"]:
                lines.append("- 待补: 释义")
            lines.append("")
        return "\n".join(lines)

    # --- 词典缓存（§7）------------------------------------------------------

    def cache_get(
        self, word: str, *, enabled: Iterable[str] | None = None
    ) -> tuple[str, dict[str, Any] | None]:
        """→ `("hit", payload)` / `("negative", None)` / `("miss", None)`。

        负结果写成 `json='null'`（§4.3），TTL 30 天；命中即返回，不碰网络。
        `enabled` = **当前启用的 provider 集合**；负结果的 `source` 里记着写它时试过
        哪些源（`miss:<已试集合>`，`dict_sources.negative_source`）。只有
        「启用集合 ⊆ 已试集合」这条 `null` 才算数，否则视为未命中重新查（R1 §8.3）——
        否则本地库导入之前（或 `dict_online=0` 时）写下的 null，会在 30 天里骗用户
        「查不到」。老格式（没有 provider 集合）的负结果一律不认，重查一次最便宜。
        """
        key = one_line(word).lower()
        if not key:
            return ("miss", None)
        row = self._one(
            "SELECT json, source, fetched_at FROM dict_cache WHERE word = ?", (key,)
        )
        if row is None:
            return ("miss", None)
        body = row["json"]
        payload: Any = None
        if body is not None:
            try:
                payload = json.loads(str(body))
            except json.JSONDecodeError:
                log.warning("词典缓存损坏（%d 字节），按未命中处理", len(str(body)))
                return ("miss", None)
        if payload is None:  # 负结果：`null`
            if enabled is not None and not negative_covers(
                str(row["source"] or ""), enabled
            ):
                log.info("负缓存已失效（启用集合变大或格式过老），重新查")
                return ("miss", None)
            fetched = parse_ts(row["fetched_at"])
            if fetched is None or self._now() - fetched > timedelta(
                days=NEGATIVE_TTL_DAYS
            ):
                with self._write() as conn:
                    conn.execute("DELETE FROM dict_cache WHERE word = ?", (key,))
                return ("miss", None)
            return ("negative", None)
        if not isinstance(payload, dict):
            return ("miss", None)
        return ("hit", payload)

    def cache_put(
        self,
        word: str,
        payload: Mapping[str, Any] | None,
        source: str,
        *,
        now: datetime | None = None,
    ) -> None:
        """写缓存；`payload=None` 写负结果（`json='null'`，§7）。"""
        key = one_line(word).lower()
        if not key:
            return
        body = (
            "null" if payload is None else json.dumps(dict(payload), ensure_ascii=False)
        )
        with self._write() as conn:
            conn.execute(
                """
                INSERT INTO dict_cache (word, json, source, fetched_at) VALUES (?, ?, ?, ?)
                ON CONFLICT(word) DO UPDATE SET json = excluded.json,
                    source = excluded.source, fetched_at = excluded.fetched_at
                """,
                (key, body, one_line(source), format_ts(now or self._now())),
            )

    def counts(self) -> dict[str, int]:
        """`ping` / `--selftest` 用的一把数量（只记数量，不记内容）。"""
        row = self._one(
            """
            SELECT (SELECT COUNT(*) FROM records) AS records,
                   (SELECT COUNT(*) FROM sessions) AS sessions,
                   (SELECT COUNT(*) FROM dict_cache) AS cache
            """
        )
        return {
            "records": int(row["records"]) if row else 0,
            "sessions": int(row["sessions"]) if row else 0,
            "cache": int(row["cache"]) if row else 0,
            "pending": self.inbox_pending_lines(),
        }

    # --- spool 入库（§2）---------------------------------------------------

    @property
    def consuming_path(self) -> Path:
        """消费中的 spool 副本（#68）：`inbox.jsonl` 改名到这里之后才被读。"""
        return Path(str(self.inbox_path) + CONSUMING_SUFFIX)

    def ingest_inbox(self) -> dict[str, Any]:
        """把 `data/inbox.jsonl` 里的行入库（§2）。

        - 坏行跳过并记日志（只记行号与字节数），**不许整批失败**；
        - 末行没有换行 = 外壳正写到一半 → 留给下一次（不能吃掉半条 JSON）；
        - 消费走 **rename 语义**（#68）：先把 `inbox.jsonl` 原子改名成
          `inbox.jsonl.consuming`，只读改名后的副本。这样消费期间外壳追加的行落进
          **新的** `inbox.jsonl`，不会被老的「读 rest → truncate」窗口截掉
          （ADR-0003：spool 是保存的底线，一行都不许丢）；
        - 改名被外壳的句柄挡住（`PermissionError`）→ 短重试；仍失败则退回原地重写
          并记一行 warning（保底不丢，只是对追加行仍有理论窗口）；
        - 改名成功、消费完之前崩掉 → 遗留的 `inbox.jsonl.consuming` 由下一次 ingest
          先消费，词不会卡在副本里。
        - 票 #177（H1）：副本入库后先改名成唯一归档名再删 —— 删除失败只留孤儿归档，
          下一轮绝不整批重放（改名失败则原地清空兜底，见 `_consume_consuming`）。
        - 返回值带 `targets`（票 #113）：本轮**新入库**的 target，中枢据此后台预热。
        - 返回值带 `fill`（v23 §4.2）：本轮入库后**释义仍为空**的词条 `{"id","target"}`，
          中枢（`Hub.ingest_inbox`）据此投补释义队列。本方法只收集，不查词、不写回。
        - 票 #186（H12）：整条管线只由 `_ingest_lock` 串行化（定时 / flush 两条触发
          路径并发时不重复消费同一段字节），**不持 store 全局锁** —— 大批积压时
          读文件、归档这些文件 IO 不再卡所有 API；落库由逐行 `capture()` 自带的
          `_write()` 短暂持锁，#177 的逐行隔离 / 幂等语义不变。
        """
        path = self.inbox_path
        with self._ingest_lock:
            started = time.perf_counter()
            total: dict[str, Any] = {
                "ingested": 0,
                "dedup": 0,
                "skipped": 0,
                "last": None,
                # 票 #113：本轮**新入库**的 target（去重/坏行不算），供后台预热用
                "targets": [],
                # v23 §4.2：入库后释义仍为空的词条 `[{"id", "target"}, …]`，
                # 中枢据此把它们投进补释义队列（本方法只收集，不查词、不写回）。
                "fill": [],
            }
            # 崩溃恢复（#68）：上一轮改名成功、消费完之前崩了 → 副本里的词先补上
            if self.consuming_path.exists():
                log.warning(
                    "spool 发现遗留的消费副本，先补消费：%s",
                    self.consuming_path.name,
                )
                self._merge(total, self._consume_consuming())
            if self._consumable_bytes(path):
                if self._rename_aside(path):
                    self._merge(total, self._consume_consuming())
                else:
                    self._merge(total, self._consume_in_place(path))
        elapsed = round((time.perf_counter() - started) * 1000)
        if total["ingested"] or total["skipped"]:
            log.info(
                "spool 入库 %d 条（重复 %d，跳过 %d）%dms",
                total["ingested"],
                total["dedup"],
                total["skipped"],
                elapsed,
            )
        return {"ok": True, **total}

    @staticmethod
    def _complete_lines_end(raw: bytes) -> int:
        """`raw` 里完整行结束的字节偏移（末行没有换行 → 不算，留给下一次）。"""
        return len(raw) if raw.endswith(b"\n") else max(0, raw.rfind(b"\n") + 1)

    @classmethod
    def _consumable_bytes(cls, path: Path) -> int:
        """`path` 里「能整行消费」的前缀字节数；文件不在 = 没有可消费的。"""
        if not path.exists():
            return 0
        return cls._complete_lines_end(path.read_bytes())

    def _rename_aside(self, path: Path) -> bool:
        """把 spool 原子改名成消费副本（同卷 `os.replace`）。

        外壳正持句柄时 Windows 会给 `PermissionError` → 短重试（它写完即释放）；
        重试用完仍失败返回 False，由调用方退回原地重写。
        """
        for attempt in range(RENAME_ATTEMPTS):
            try:
                os.replace(path, self.consuming_path)
                return True
            except PermissionError:
                if attempt == RENAME_ATTEMPTS - 1:
                    break
                time.sleep(RENAME_RETRY_SECONDS)
        log.warning("spool 改名被拒（外壳正持句柄？）→ 退回原地重写消费（#68 保底路径）")
        return False

    def _consume_consuming(self) -> dict[str, Any]:
        """消费改名后的副本并清理它；末行残尾还给 `inbox.jsonl`，等外壳写完整。

        票 #177（H1）：入库完成后先把副本**改名成唯一归档名**再删 —— 改名成功后
        这份内容就退出了消费管线（`ingest_inbox` 只认 `.consuming` 后缀），之后删除
        失败最多留下一个无人再消费的孤儿归档，下一轮绝不会再整批重放（hits /
        last_seen 不再每 5s 虚涨）。改名失败（占用方连 DELETE 权限都不放行）→
        原地清空副本兜底：已入库的行与还回的残尾都不再有保留价值；清空也失败才
        留给下一轮重试（那时残尾仍在副本里，不会重复还回）。
        """
        source = self.consuming_path
        raw = source.read_bytes()
        cut = self._complete_lines_end(raw)
        result = self._ingest_lines(raw[:cut])
        tail = raw[cut:]
        if self._archive_consuming(source) or self._clear_consuming(source):
            # 副本里已不再持有残尾 → 现在从内存把它还回 spool（先清副本再还，
            # 保证下一轮不会把同一段残尾再还一次）
            if tail:
                self._return_tail(tail)
            else:
                self._touch_inbox()
        else:
            log.warning(
                "spool 消费副本改名与清空都失败，本轮放弃清理（下一轮重试）：%s",
                source.name,
            )
        return result

    def _archive_consuming(self, source: Path) -> bool:
        """票 #177（H1）：把消费完的副本改名成唯一归档名再删，返回是否改名成功。

        归档名带纳秒时间戳保证唯一（Windows 上 `rename` 不覆盖同名）。删除失败
        只留孤儿归档（`ingest_inbox` 不会再消费它），不影响幂等。
        """
        archive = source.with_name(
            f"{source.name}.consumed-{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns()}"
        )
        try:
            source.rename(archive)
        except OSError:
            log.warning("spool 消费副本改名失败（占用中？）→ 退回原地清空（票 #177）")
            return False
        try:
            archive.unlink()
        except OSError:
            log.warning("spool 归档副本删除失败（孤儿归档，不再重放）：%s", archive.name)
        return True

    @staticmethod
    def _clear_consuming(source: Path) -> bool:
        """票 #177（H1）改名失败的兜底：清空副本（开 `wb` 即截断）。

        杀毒/索引类占用通常只挡 DELETE、不挡写打开，所以清空比改名更容易成功；
        清空后下一轮最多消费到空文件，同样不会重放。
        """
        try:
            with source.open("wb"):
                pass
        except OSError:
            return False
        return True

    def _consume_in_place(self, path: Path) -> dict[str, Any]:
        """改名失败时的退回实现：原地读 → 解析 → 重写剩余（#68 之前的老行为）。"""
        raw = path.read_bytes()
        consumed = self._complete_lines_end(raw)
        result = self._ingest_lines(raw[:consumed])
        try:
            self._rewrite_after_consume(path, consumed)
        except OSError:
            # 票 #177（H1）：重写失败只告警、留给下一轮从改名路径重试。这里不能
            # 把 spool 原文件改名归档 —— 本路径正因外壳可能持句柄才退回来的，
            # 此窗口内外壳新追加的行会随归档一起被丢掉（一行都不许丢）。
            log.warning("spool 原地重写失败，下一轮可能重放本轮已入库内容：%s", path.name)
        return result

    def _ingest_lines(self, body: bytes) -> dict[str, Any]:
        """把一段「整行」字节入库：坏行只跳过并记日志（不记内容），返回这一批的计数。

        票 #177（H2）：`capture()` 抛异常同样只跳过该行（记「第 N 行入库异常：
        类型名」）—— 单行异常穿透会把整批钉死在队头，之后所有捕获全部延迟。
        """
        ingested = dedup = skipped = 0
        last: dict[str, Any] | None = None
        targets: list[str] = []
        # v23 §4.2：入库后释义仍为空的词条 —— `{"id", "target"}`，中枢据此投后台补齐。
        # 按**写入后**的 meaning 判断：dedup 顺手补上非空义的记录不进这张表。
        fill: list[dict[str, Any]] = []
        for number, line in enumerate(
            body.decode("utf-8", errors="replace").splitlines(), start=1
        ):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                log.warning(
                    "spool 第 %d 行不是 JSON（%d 字节），跳过", number, len(line)
                )
                continue
            if not isinstance(payload, dict):
                skipped += 1
                log.warning(
                    "spool 第 %d 行不是对象（%d 字节），跳过", number, len(line)
                )
                continue
            try:
                result = self.capture(payload)
            # 票 #177（H2）：逐行隔离 —— capture 抛什么都只跳过该行，不许把整批
            # 钉死在队头（sqlite 瞬时 locked 也会让其后所有捕获永久延迟）
            except Exception as exc:  # noqa: BLE001 - 同 hub.SpoolWatcher.run 的口径
                skipped += 1
                log.warning("spool 第 %d 行入库异常：%s", number, type(exc).__name__)
                continue
            if not result.get("ok"):
                skipped += 1
                log.warning(
                    "spool 第 %d 行入库失败：%s", number, result.get("error")
                )
                continue
            ingested += 1
            if result.get("dedup"):
                dedup += 1
            else:
                # 票 #113：只有真正新建的词条才算「新词」（重复捕获不再喂预热队列）
                target = one_line(payload.get("target"))
                if target:
                    targets.append(target)
            if one_line(result.get("meaning")) == "":
                fill_target = one_line(result.get("target")) or one_line(
                    payload.get("target")
                )
                if fill_target:
                    fill.append({"id": int(result["id"]), "target": fill_target})
            last = result
        return {
            "ingested": ingested,
            "dedup": dedup,
            "skipped": skipped,
            "last": last,
            "targets": targets,
            "fill": fill,
        }

    @staticmethod
    def _merge(total: dict[str, Any], part: dict[str, Any]) -> None:
        """把一批的计数并进本次 ingest 的总计（`last` 取最后一条成功的）。"""
        for key in ("ingested", "dedup", "skipped"):
            total[key] += part[key]
        total["targets"].extend(part["targets"])
        total["fill"].extend(part["fill"])
        if part["last"] is not None:
            total["last"] = part["last"]

    def _return_tail(self, tail: bytes) -> None:
        """末行残尾（外壳写到一半）还回 `inbox.jsonl`，等它补上换行再吃。"""
        try:
            with self.inbox_path.open("ab") as handle:
                handle.write(tail)
        except OSError:
            log.warning("spool 残尾（%d 字节）回写失败", len(tail))

    def _touch_inbox(self) -> None:
        """消费完保证 `inbox.jsonl` 还在（老口径：截断后文件仍然是空的，不是不存在）。"""
        try:
            self.inbox_path.touch(exist_ok=True)
        except OSError:
            log.warning("spool 空文件重建失败：%s", self.inbox_path.name)

    @staticmethod
    def _rewrite_after_consume(path: Path, consumed: int) -> None:
        """丢掉已消费的前 `consumed` 字节，保留期间新追加的内容（#68 的保底路径）。

        **只在改名失败时用**：原地重写下「读完 rest → truncate」之间被追加的行仍会
        被截掉，所以正常路径走 rename 语义（见 `ingest_inbox`）。
        """
        with path.open("rb+") as handle:
            handle.seek(0, 2)
            if handle.tell() <= consumed:
                handle.truncate(0)
                return
            handle.seek(consumed)
            rest = handle.read()
            handle.seek(0)
            handle.write(rest)
            handle.truncate()

    def inbox_pending_lines(self) -> int:
        """待入库行数：`inbox.jsonl` 与消费中的副本都要算（#68）。

        票 #186（H9）：这个读不持 ingest 锁（`counts()` → ping/health 的快路径），
        与 5s 定时 ingest 的 rename 并发时，`exists()` 与 `read_bytes()` 之间文件
        可能被改名走 —— `FileNotFoundError` 当 0 条，不再把 ping/health 打成 500。
        同族竞态的第二层（v33 验收期实测）：ingest 消费完删 `.consuming` 的瞬间，
        `read_bytes()` 撞上正在删除的文件报 `PermissionError`（Windows 删除中的
        文件不可新开句柄）——同按 0 条处理，语义与 H9 一致。
        """
        pending = 0
        for path in (self.inbox_path, self.consuming_path):
            try:
                raw = path.read_bytes()
            except (FileNotFoundError, PermissionError):
                continue
            pending += len([line for line in raw.splitlines() if line.strip()])
        return pending

    # --- v1 markdown 导入（§4.4，只读远端目录）----------------------------

    def import_vault(self, vault_dir: Path | str) -> dict[str, Any]:
        """导入 `YYYY-MM-DD.md`（§4.4）：每条挂到「（导入）<日期>」会话，按 (target, captured_at) 幂等。"""
        directory = Path(vault_dir)
        if not directory.is_dir():
            return {"ok": False, "error": f"目录不存在：{directory}"}
        files = 0
        imported = 0
        skipped = 0
        for path in sorted(directory.glob("*.md")):
            match = re.match(r"^(\d{4}-\d{2}-\d{2})$", path.stem)
            if not match:
                continue
            files += 1
            day = match.group(1)
            entries = parse_vault_text(path.read_text(encoding="utf-8"))
            for entry in entries:
                if self._import_entry(entry, day):
                    imported += 1
                else:
                    skipped += 1
        log.info(
            "v1 导入完成：%d 个文件，%d 条新入库，%d 条已存在", files, imported, skipped
        )
        return {"ok": True, "files": files, "imported": imported, "skipped": skipped}

    def _import_entry(self, entry: Mapping[str, str], day: str) -> bool:
        target = one_line(entry.get("target"))
        if not target:
            return False
        captured = parse_ts(entry.get("captured_at"))
        captured_at = format_ts(captured) if captured else f"{day} 00:00:00"
        with self._lock:
            if self._one(
                "SELECT 1 FROM records WHERE lower(target) = lower(?) AND captured_at = ?",
                (target, captured_at),
            ):
                return False
            key = f"__import__|{day}"
            with self._write() as conn:
                row = conn.execute(
                    "SELECT id FROM sessions WHERE key = ?", (key,)
                ).fetchone()
                if row is None:
                    cursor = conn.execute(
                        """
                        INSERT INTO sessions (key, title, source_app, created_at, last_active_at, note)
                        VALUES (?, ?, '导入', ?, ?, '')
                        """,
                        (key, f"（导入）{day}", captured_at, captured_at),
                    )
                    session_id = int(cursor.lastrowid or 0)
                else:
                    session_id = int(row["id"])
                    conn.execute(
                        """
                        UPDATE sessions SET last_active_at = MAX(last_active_at, ?),
                            created_at = MIN(created_at, ?) WHERE id = ?
                        """,
                        (captured_at, captured_at, session_id),
                    )
                meaning = one_line(entry.get("meaning"))
                conn.execute(
                    """
                    INSERT INTO records (
                        target, kind, context, meaning, meaning_source, phonetic, dict_json,
                        session_id, source_app, source_title, position, tags, hits,
                        captured_at, last_seen_at, due_at, interval_days, ease, reps, lapses,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, '', NULL, ?, ?, ?, ?, ?, 1, ?, ?, ?, 0, 2.5, 0, 0, ?, ?)
                    """,
                    (
                        target,
                        guess_kind(target),
                        one_line(entry.get("context")),
                        meaning,
                        "user" if meaning else "",
                        session_id,
                        one_line(entry.get("source_app")),
                        one_line(entry.get("source_title")),
                        one_line(entry.get("position")),
                        normalize_tags(entry.get("tags")),
                        captured_at,
                        captured_at,
                        captured_at,
                        format_ts(self._now()),
                        format_ts(self._now()),
                    ),
                )
        return True

    # --- ECDICT 导入（§7）--------------------------------------------------

    def import_ecdict(
        self, source: Path | str, target: Path | str | None = None, *, full: bool = False
    ) -> dict[str, Any]:
        """`--import-ecdict <csv / zip / db> [--full]` → `data/ecdict.db`（幂等，可重跑覆盖）。

        - `.csv`（推荐：仓库 `ecdict.csv`）：**精简 v2** 后建库（规则见 `is_common_phrase`，
          真库实测 770,611 → **124,062 行**，`bnc` 1..20000 零丢失）—— 精简的判据是
          「带标注」∪「常见短语」，`take off` / `give up` / `a lot of` 这类字幕常见的
          短语现在留得下来（票 #35 之前只有 881 个带空格词条）；
          `full=True`（CLI `--full`）时**不精简**，770,611 行全进（要全量的场景）；
        - `.zip`：内含 `stardict.db`（release `ecdict-sqlite-28.zip`，811.9MiB 本来就
          不精简）→ 校验后整文件复制；内含 `ecdict.csv` → 走上面的 CSV 路径（`--full`
          在这里也生效）；`ecdict-stardict-28.zip`（`.dict/.idx/.ifo` 渲染标记包）
          明确拒绝并说原因；
        - `.db`：探测出带 word 列的词典表后整文件复制（本来就不精简）；
        - 其他后缀（mdx / eudic / mobi / 7z / …）按 `_REJECT_REASONS` 明确拒绝，不静默失败。

        成功后清掉 `dict_cache` 里的负结果 —— 词典换了，之前写下的「查不到」不再可信
        （R1 §8.3；本地库导入之前写下的 null 正是这条修正要防的那个洞）。
        """
        src = Path(source)
        dest = Path(target) if target is not None else self.db_path.parent / "ecdict.db"
        if not src.exists():
            return {"ok": False, "error": f"文件不存在：{src}"}
        suffix = src.suffix.lower()
        if suffix in _REJECT_REASONS:
            return {
                "ok": False,
                "error": f"不支持 {suffix}：{_REJECT_REASONS[suffix]}；{_ALT_HINT}",
            }
        dest.parent.mkdir(parents=True, exist_ok=True)
        if suffix == ".db":
            return self._import_db(src, dest, mode="db")
        if suffix == ".csv":
            with src.open("rb") as handle:
                return self._import_csv(handle, dest, mode="csv", full=full)
        if suffix == ".zip":
            return self._import_zip(src, dest, full=full)
        return {
            "ok": False,
            "error": f"不支持的格式：{src.suffix or src.name}（支持 .csv / .db / .zip）",
        }

    def _import_db(
        self,
        src: Path,
        dest: Path,
        *,
        mode: str,
        member: str | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            rows = _count_rows(src)
        except (sqlite3.Error, ValueError):
            return {"ok": False, "error": f"这个 db 里读不到带 word 列的词典表：{src.name}"}
        if src.resolve() != dest.resolve():
            # 先复制到同目录临时文件再改名：复制中途失败（磁盘满等）不会把 dest 留成半个库
            temp = dest.with_suffix(".tmp.db")
            temp.unlink(missing_ok=True)
            try:
                shutil.copyfile(src, temp)
                temp.replace(dest)
            finally:
                temp.unlink(missing_ok=True)
        return self._imported(
            dest, mode=mode, rows=rows, member=member, seconds=time.perf_counter() - started
        )

    def _import_csv(
        self,
        handle: Any,
        dest: Path,
        *,
        mode: str,
        member: str | None = None,
        full: bool = False,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            stats = _load_csv(handle, dest, full=full)
        except ValueError as exc:  # 表头不对（不是 ECDICT CSV）→ 给人话
            return {"ok": False, "error": str(exc)}
        result = self._imported(
            dest,
            mode=mode,
            rows=int(stats["rows"]),
            member=member,
            seconds=time.perf_counter() - started,
        )
        result.update(
            {"total": stats["total"], "slim": stats["slim"], "columns": stats["columns"]}
        )
        return result

    def _import_zip(self, src: Path, dest: Path, *, full: bool = False) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            archive = zipfile.ZipFile(src)
        except zipfile.BadZipFile:
            return {"ok": False, "error": f"这个文件不是能读的 zip：{src.name}"}
        with archive:
            names = archive.namelist()
            db_member = _pick_member(names, ".db")
            if db_member is not None:
                temp = dest.with_suffix(".tmp.db")
                temp.unlink(missing_ok=True)
                try:
                    with archive.open(db_member) as handle, temp.open("wb") as out:
                        shutil.copyfileobj(handle, out, 1024 * 1024)
                    try:
                        rows = _count_rows(temp)
                    except (sqlite3.Error, ValueError):
                        return {
                            "ok": False,
                            "error": f"zip 里的 {db_member} 不是能读的 ECDICT 库"
                            "（读不到带 word 列的词典表）",
                        }
                    temp.replace(dest)
                finally:
                    temp.unlink(missing_ok=True)
                return self._imported(
                    dest,
                    mode="zip",
                    rows=rows,
                    member=db_member,
                    seconds=time.perf_counter() - started,
                )
            csv_member = _pick_member(names, ".csv")
            if csv_member is not None:
                with archive.open(csv_member) as handle:
                    return self._import_csv(
                        handle, dest, mode="zip", member=csv_member, full=full
                    )
            stardict = [name for name in names if Path(name).suffix.lower() in _STARDICT_SUFFIXES]
            if stardict:
                return {
                    "ok": False,
                    "error": f"不支持这个包（{len(stardict)} 个 stardict 渲染标记文件："
                    f"{Path(stardict[0]).name} 等）；{_ALT_HINT}",
                }
            return {
                "ok": False,
                "error": f"zip 里既没有 .db 也没有 .csv（读到 {len(names)} 个文件）",
            }

    def _imported(
        self,
        dest: Path,
        *,
        mode: str,
        rows: int,
        member: str | None,
        seconds: float,
    ) -> dict[str, Any]:
        cleared = self._clear_negative_cache()
        log.info(
            "ECDICT 导入完成：%s，%d 行，%.1fs，清负缓存 %d 条", mode, rows, seconds, cleared
        )
        return {
            "ok": True,
            "db": str(dest),
            "mode": mode,
            "member": member,
            "rows": rows,
            "bytes": dest.stat().st_size,
            "seconds": round(seconds, 1),
            "cleared": cleared,
        }

    def _clear_negative_cache(self) -> int:
        """负结果（`json='null'`）全删：词典换了，之前的「查不到」不再可信（R1 §8.3）。"""
        with self._write() as conn:
            cursor = conn.execute(
                "DELETE FROM dict_cache WHERE json IS NULL OR json = 'null'"
            )
            deleted = max(cursor.rowcount, 0)
        if deleted:
            log.info("清掉 %d 条负缓存（本地词典已更换）", deleted)
        return deleted


def _json_or_none(value: Any) -> str | None:
    """`dict_json`：dict/list → JSON 文本；字符串直接用（外壳可能已序列化）；空 → NULL。"""
    if value is None or value == "":
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# --- ECDICT 导入的三个格式事实（R1 §8.4 实测）--------------------------------

# 精简 v2 要读的列（票 #35）。缺任一列就不精简：一张普通 CSV 不该被整张丢掉。
# 旧版还要 `phonetic`/`definition`（「有音标且有英英」那条），v2 按票面改判据，
# 不再用它们 —— 详见 `is_common_phrase` 与 `_load_csv` 的注释。
_SLIM_COLUMNS = ("bnc", "frq", "collins", "oxford", "tag", "translation")
# 常见短语的每个词长这样：`take` / `sb.` / `one's` / `take-off`（数字开头的词组不认）
_WORD_TOKEN_RE = re.compile(r"^[A-Za-z][A-Za-z'.\-]*$")
# 中文释义以 `[医]`/`[化]`/`[机]`/`[经]`/`[计]`/`[建]`/`[电]`/`[网络]`/`[地名]` 这类
# 领域标签开头的，是专业术语短语（真库 366,599 个带空格词条里 296,738 个是它们）
_DOMAIN_GLOSS_RE = re.compile(r"^\[[^\]]{1,6}\]")
_PHRASE_MAX_TOKENS = 5
# 数字列用 INTEGER 存（体积小、读侧不用再转字符串）
_INT_COLUMNS = ("collins", "oxford", "bnc", "frq")
# ECDICT CSV 把多行释义写成**字面 `\n`**（反斜杠 + n 两个字符，实测 bank/reluctant 都是）
# → 入库时转回真换行，与 release 库的口径一致（否则外壳会把 `\n` 原样显示给用户）
_NEWLINE_COLUMNS = ("translation", "definition")
# stardict 渲染标记包（release `ecdict-stardict-28.zip`）内的成员后缀 → 明确拒绝
_STARDICT_SUFFIXES = (".dict", ".idx", ".ifo")
_REJECT_REASONS: dict[str, str] = {
    ".mdx": "mdx 是给阅读器渲染的标记格式，程序取不出结构化字段",
    ".eudic": "eudic 是欧路私有的渲染格式，程序取不出结构化字段",
    ".mobi": "mobi 是 Kindle 电子书格式，程序取不出结构化字段",
    ".dict": "stardict 的 .dict 是渲染标记串（R1 实测纯 Python 解析要 63s + 662MiB）",
    ".idx": "stardict 的 .idx 只是索引，词条本体在那个渲染标记串里",
    ".ifo": "stardict 的 .ifo 是元数据，不是词库本体",
    ".7z": "7z 要额外的解码依赖（标准库只有 zip）",
}
_ALT_HINT = "请改用 ecdict.csv（推荐，66MB）或 ecdict-sqlite-28.zip（内含 stardict.db）"


def _count_rows(path: Path) -> int:
    """确认能读出一张带 word 列的词典表，返回行数；读不动就抛异常（§7 宽松探测）。"""
    uri = f"file:{Path(path).as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        layout = probe_layout(conn)
        row = conn.execute(f'SELECT COUNT(*) FROM "{layout.table}"').fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def _int_of(value: str) -> int | None:
    """`""`/非数字 → None（CSV 里的空单元格与 `-` 都会走到这里）。"""
    text = value.strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _unescape_newlines(text: str) -> str:
    """字面 `\\n`（两个字符）→ 真换行；ECDICT CSV 的多行释义就是这个写法。"""
    return text.replace("\\r\\n", "\n").replace("\\n", "\n")


def is_common_phrase(word: str, translation: str) -> bool:
    """「常见短语」判据（精简 v2，票 #35）：`take off` / `give up` / `a lot of` 这类。

    为什么要这条判据：真库 770,611 行里 366,599 行词条带空格，其中 296,738 行的
    中文释义是 `[医] …`/`[化] …` 这种领域术语（`absorbent vessels` → `[医] 淋巴管`），
    而这些短语**一个标注都没有**（`bnc`/`frq`/`collins`/`oxford`/`tag` 全空），
    按「只留带标注的」规则会被整片砍掉 —— 字幕里最常见的恰恰是它们。

    判据（三条同时成立，实测在真库上选出 65,764 个词条，全量 124,062 行落在
    票面预计的 12–20 万区间）：
    1. 2~5 个词（`take off` 2 个、`a lot of` 3 个；更长的多半是术语/例句碎片）；
    2. 每个词都是纯 ASCII 词形（`take` / `sb.` / `one's` / `take-off`），
       排掉 `12 小时`、`deceptive(ly) (simple) spectrum` 这类畸形词条；
    3. 有中文释义，且**不以 `[领域]` 开头**（领域标签开头的是术语，见上）。

    不追求「把所有带空格的词条都留下」：那要 366,599 行 / 358MiB（R1 §5 路径 C 实测），
    对「看视频取词」这个场景性价比太低。真库核查用的四个短语：
    `take off`/`give up`/`a lot of`/`according to`（前者三个一个标注都没有）。
    """
    tokens = word.split()
    if not 2 <= len(tokens) <= _PHRASE_MAX_TOKENS:
        return False
    if not all(_WORD_TOKEN_RE.match(token) for token in tokens):
        return False
    text = translation.strip()
    return bool(text) and not _DOMAIN_GLOSS_RE.match(text)


def _load_csv(handle: Any, dest: Path, *, full: bool = False) -> dict[str, Any]:
    """ECDICT CSV → `stardict` 表；返回 `{rows,total,slim,columns}`（`rows` 是入库行数）。

    - `word` 列必填，其余列名随源文件（读侧 `PRAGMA table_info` 探测，§7）；
    - `sw` = `dict_sources.stripword(word)`（ECDICT 自己的 Unicode 感知版本），
      源文件有 `sw` 列也重算 —— 保证与 release 库口径一致；
    - `translation` / `definition` 里的**字面 `\\n` 转回真换行**（CSV 就这么写的，
      不转的话外壳会把 `\\n` 原样显示）；
    - **精简规则 v2**（票 #35）：`bnc>0 OR frq>0 OR collins>0 OR oxford>0 OR tag<>''`
      ∪ `is_common_phrase(word, translation)`。真库实测 770,611 → **124,062 行**
      （v1 是 121,345 行，只留 881 个带空格词条；v2 多留 65,764 个常见短语，
      同时不再用 v1 的「有音标且有英英」那条 —— 票面按「带标注」重定判据）；
      `bnc` 1..20000 零丢失；源文件缺 `_SLIM_COLUMNS` 里任何一列时**不精简**；
    - `full=True`（CLI `--full`）→ **不精简**，源文件有多少行进多少行（要全量的场景）；
    - 索引：`word COLLATE NOCASE`（大小写不敏感命中）+ `(sw, word COLLATE NOCASE)`
      （形态归一查询用）。先在同目录 `.tmp.db` 建完再原子改名 → 可重跑、失败不留半个库。
    """
    text = io.TextIOWrapper(handle, encoding="utf-8-sig", errors="replace", newline="")
    reader = csv.reader(text)
    try:
        header = next(reader)
    except StopIteration:
        raise ValueError("这个 csv 是空的（连表头都没有）") from None
    columns = [str(name).strip() or f"col{index}" for index, name in enumerate(header)]
    lowered = [name.lower() for name in columns]
    if "word" not in lowered:
        raise ValueError("这个 csv 表头里没有 word 列（不是 ECDICT CSV？）")
    word_index = lowered.index("word")
    slim = not full and all(name in lowered for name in _SLIM_COLUMNS)
    # `word` 也提出来：短语判据要看词条本身（`word` 上面已经确认存在）
    picks = {
        name: lowered.index(name) for name in (*_SLIM_COLUMNS, "word") if name in lowered
    }
    if "sw" in lowered:
        sw_index = lowered.index("sw")
    else:
        columns.append("sw")
        sw_index = len(columns) - 1
    kinds = [
        "INTEGER" if name.lower() in _INT_COLUMNS else "TEXT" for name in columns
    ]
    newline_indexes = {
        index for index, name in enumerate(columns) if name.lower() in _NEWLINE_COLUMNS
    }
    width = len(header)

    def keep(values: list[str]) -> bool:
        def cell(name: str) -> str:
            index = picks[name]
            return values[index].strip() if index < len(values) else ""

        # 带标注（词频 / 柯林斯 / 牛津 / 考纲任一）→ 留
        if (_int_of(cell("bnc")) or 0) > 0 or (_int_of(cell("frq")) or 0) > 0:
            return True
        if (_int_of(cell("collins")) or 0) > 0 or (_int_of(cell("oxford")) or 0) > 0:
            return True
        if cell("tag"):
            return True
        # 无标注 → 只有「常见短语」这一条生路（票 #35；`take off` 就是靠它留下的）
        return is_common_phrase(cell("word"), cell("translation"))

    temp = dest.with_suffix(".tmp.db")
    temp.unlink(missing_ok=True)
    total = 0
    rows = 0
    conn = sqlite3.connect(str(temp))
    try:
        quoted = ", ".join(
            f'"{name}" {kind}' for name, kind in zip(columns, kinds, strict=True)
        )
        conn.execute(f"CREATE TABLE stardict ({quoted})")
        placeholders = ", ".join("?" for _ in columns)
        batch: list[list[Any]] = []
        for record in reader:
            if not record:
                continue
            total += 1
            source = [str(cell) for cell in record[:width]]
            source += [""] * (width - len(source))
            values: list[Any] = source + [""] * (len(columns) - width)
            values[sw_index] = stripword(source[word_index])
            if slim and not keep(source):
                continue
            for index, kind in enumerate(kinds):
                if kind == "INTEGER":
                    values[index] = _int_of(str(values[index]))
                elif index in newline_indexes:
                    values[index] = _unescape_newlines(str(values[index]))
            batch.append(values)
            if len(batch) >= 2000:
                conn.executemany(f"INSERT INTO stardict VALUES ({placeholders})", batch)
                rows += len(batch)
                batch.clear()
        if batch:
            conn.executemany(f"INSERT INTO stardict VALUES ({placeholders})", batch)
            rows += len(batch)
        conn.execute(
            'CREATE INDEX IF NOT EXISTS stardict_word ON stardict("word" COLLATE NOCASE)'
        )
        conn.execute(
            'CREATE INDEX IF NOT EXISTS stardict_sw ON stardict("sw", "word" COLLATE NOCASE)'
        )
        conn.commit()
    finally:
        conn.close()
    temp.replace(dest)
    log.info(
        "ECDICT CSV 导入 %d 行%s（%d 列）",
        rows,
        f"，精简自 {total} 行" if slim else "",
        len(columns),
    )
    return {"rows": rows, "total": total, "slim": slim, "columns": len(columns)}


def _pick_member(names: Sequence[str], suffix: str) -> str | None:
    """zip 里挑一个该后缀的成员：先根目录、再按官方文件名。"""
    matches = [name for name in names if name.lower().endswith(suffix)]
    if not matches:
        return None
    matches.sort(key=lambda name: (name.count("/"), Path(name).name.lower()))
    return matches[0]


# --- ECDICT 词表导出（#36）：后校正的「已知词」纯单词表 ------------------------

WORDS_EXPORT_NAME = "ecdict_words.txt"
_PURE_WORD = re.compile(r"[a-z]+")


def export_words(
    db_path: Path | str, out_path: Path | str | None = None
) -> dict[str, Any]:
    """`ecdict.db` → 纯单词表 `ecdict_words.txt`（小写、一行一词、只取单词不取短语）。

    kernel 侧把它并进后校正的「已知词」集合（`kernel.correction_wordlists`）：词表越大，
    干净英文里的真词越不会被当成 OOV 拿去切段/当混淆候选（#36 的误改率就是量化这件事）。

    **只收「带标注」词条**（bnc/frq/collins/oxford/tag 任一非空；schema 没有这些列时不过滤），
    exchange 变形词也只从带标注词条收集。这是校正品味的边界，不是词典体量：全量库的
    130 万无标注派生词会让 `thc` 当真词（thc→the 修复失效）、把 `peopleare` 拉进
    「编辑距离 ≤1」防误报门的近邻（粘连标记失效）——票 #150 终验实测回归。词典查询走
    `ecdict.db` 全量，词表走精选，两者解耦。

    - 只收**纯 ASCII 字母**词条（小写化后 `[a-z]+`）：`a lot of` / `take-off` / `don't`
      / `Aconitum komarovii` 这类短语、带符号与拉丁学名一律不要 —— 词表的读者
      （`kernel.load_wordlist`）按空白切词，短语混进去只会被当成两个不存在的词；
    - 词条**加上 `exchange` 列里的变形词**（`s:passages` / `t:sweetest` / `i:repairing`
      —— ECDICT 自己标注的屈折形式）。精简规则会把 `passages`、`sweetest` 这类只有变形
      没有标注的行整行丢掉，于是「真词的常见变形」在词表里缺席，反而是切段/混淆最容易
      误伤的一类（散文语料实测 `repairing`→`repair ring`、`cheeks`→`checks`）；
    - 去重后**排序**输出：同一份词典两次导出逐字节一致（可 diff、可复现）；
    - 先写同目录 `.tmp` 再原子改名：导出中途失败不会留下半个词表被 kernel 读进去。

    返回 `{ok, path, words, forms, rows, skipped, unannotated, bytes, seconds}`；`forms` 是变形词的
    **净新增**（`exchange` 去重后不在词条里的那些；词条与变形词重合的只算词条），`unannotated`
    是被标注门滤掉的纯单词行数（schema 无标注列时为 0）。读不动 db 时 `ok=False`
    带 `error`（导入成功但导出失败不应让导入整体失败，调用方按需提示）。

    **内核只读 `TOOL_DIR/data/ecdict_words.txt`**（`kernel.WORDS_ECDICT_PATH`）：`--dict-db`
    把词典挪到别处时，词表跟着 db 走、内核读不到 —— 要用就留在默认位置。
    """
    src = Path(db_path)
    dest = Path(out_path) if out_path is not None else src.with_name(WORDS_EXPORT_NAME)
    if not src.is_file():
        return {"ok": False, "error": f"本地词典不在位：{src}"}
    started = time.perf_counter()
    head_words: set[str] = set()  # word 列里的纯字母词条
    form_words: set[str] = set()  # exchange 列里的纯字母变形词（去重，含与词条重合的）
    rows = 0
    skipped = 0  # 非纯单词的行（短语/带符号/非 ASCII），与「大小写重复」分开如实报
    unannotated = 0  # 纯单词但无任何标注（标注门滤掉；schema 无标注列时恒 0）
    try:
        conn = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
        try:
            layout = probe_layout(conn)
            columns = [
                str(row[1])
                for row in conn.execute(f'PRAGMA table_info("{layout.table}")')
            ]
            exchange = next(
                (name for name in columns if name.lower() == "exchange"), None
            )
            # 标注门：bnc/frq/collins/oxford/tag 任一非空才算「带标注」。列全缺失的
            # 宽松 schema（probe_layout 的兼容面）退回旧行为——不过滤。
            marks = [
                name
                for name in columns
                if name.lower() in ("bnc", "frq", "collins", "oxford", "tag")
            ]
            picked = f'"{layout.word}"' + (f', "{exchange}"' if exchange else "")
            picked += "".join(f', "{name}"' for name in marks)
            cursor = conn.execute(f'SELECT {picked} FROM "{layout.table}"')
            offset = 2 if exchange else 1  # row 里标注列的起始下标
            for row in cursor:
                rows += 1
                text = str(row[0] or "").strip().lower()
                if not _PURE_WORD.fullmatch(text):
                    skipped += 1
                    continue
                if marks and not any(
                    str(row[offset + i] or "").strip() not in ("", "0")
                    for i in range(len(marks))
                ):
                    unannotated += 1
                    continue  # 无标注词条与其变形一并不要（校正品味边界）
                head_words.add(text)
                if exchange is None:
                    continue
                for item in str(row[1] or "").split("/"):
                    form = item.split(":", 1)[-1].strip().lower()
                    if _PURE_WORD.fullmatch(form):
                        form_words.add(form)
        finally:
            conn.close()
    except (sqlite3.Error, ValueError) as exc:
        return {"ok": False, "error": f"从 {src.name} 读不出词表：{exc}"}
    words = head_words | form_words
    temp = dest.with_suffix(".tmp.txt")
    temp.unlink(missing_ok=True)
    try:
        temp.write_text("\n".join(sorted(words)) + "\n", encoding="utf-8", newline="\n")
        temp.replace(dest)
    finally:
        temp.unlink(missing_ok=True)
    return {
        "ok": True,
        "path": str(dest),
        "words": len(words),
        "forms": len(form_words - head_words),
        "rows": rows,
        "skipped": skipped,
        "unannotated": unannotated,
        "bytes": dest.stat().st_size,
        "seconds": round(time.perf_counter() - started, 1),
    }
