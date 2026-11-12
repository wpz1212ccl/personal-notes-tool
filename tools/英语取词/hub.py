"""数据中枢 `hub.py`（票 #21；规格 `docs/specs/v2-workbench.md` §2 §4 §6.1 §7 §8.3 §10）。

一个常驻进程承担四件事：
1. **唯一真源**：SQLite `data/capture.db`（WAL），外壳/浏览器都通过它读写；
2. **词典**：`dict_cache` → 本地 ECDICT → 在线（§7）；
3. **RPC 47810**：外壳专用，新行分隔 JSON、一连接一线程（与 `kernel.py` 同款协议）；
4. **HTTP 47811**：`/api/*` + `web/` 静态托管（取词台前端，W1 填界面）。

跑（外壳是唯一所有者，正常由外壳拉起；也可以手动跑）：

    ".venv\\Scripts\\pythonw.exe" "tools\\英语取词\\hub.py"

CLI（都可以单独跑完退出）：

    hub.py --init                     建库/建表，打印状态（幂等，可重复跑）
    hub.py --selftest                 打印数据库/词典/spool/端口各项状态后退出
    hub.py --import-vault <目录>       导入 v1 markdown（只读源目录，按 (target, captured_at) 幂等）
    hub.py --import-ecdict <csv / zip / db> [--full]
                                      导入本地词典到 data/ecdict.db（csv 默认精简 v2、
                                      --full 全量；zip 内层 stardict.db 本来就不精简），
                                      并导出 data/ecdict_words.txt 纯单词表（后校正的
                                      「已知词」；缺省不阻塞任何功能）
    hub.py --retag [--apply]          按预设标签回填历史词条（默认 dry-run 只预览，
                                      加 --apply 才写库；并集语义，手写标签不动）
    hub.py --port / --web-port / --db / --inbox / --dict-db / --web-dir / --config / --log-dir

RPC 协议（请求一行 JSON，响应一行 JSON；`ensure_ascii=False`）：

    {"cmd":"ping"}                               → {"ok":true,"pong":ms,"records":N,"sessions":M,…}
    {"cmd":"lookup","word":"reluctant"}           → §7 统一结构（含 "seen"，count=0 时不带）
    {"cmd":"session_peek","app":"Edge","title":…,"hwnd":123}
                                                  → {"ok":true,"session_id","title","count","is_new",
                                                     "category","url","browser","site"}
    {"cmd":"media","op":"pause|resume","app":"Edge","title":…}
                                                  → {"ok":true,"paused_by_us":bool,
                                                     "status":"playing|paused|none|unavailable|disabled"}
    {"cmd":"flush"}                               → {"ok":true,"ingested","dedup","skipped","last"}
    {"cmd":"article_create","text":<单行化全文>,"passages":[[s,e],…],"title":…,"app":…,"url":…}
                                                  → {"ok":true,"doc_id","session_id","title","passage_count"}
    {"cmd":"article_create","text":<原始剪贴板正文>,"format":"manual","title":…}
                                                  → 同上 + "passage_breaks"（hub 用 longtext 分段并单行化）
    {"cmd":"article_get","doc_id":17[, "passage_index":36]}
                                                  → 只回元信息 / 回该段文本（默认**不回全文**）
    {"cmd":"article_get","doc_id":17,"outline":true[,"around":36,"before":2,"after":2]}
                                                  → {"ok":true,"passage_count",…,"outline":[段目录窗口]}
    {"cmd":"article_locate","doc_id":17,"target":…[,"current_passage":36]}
                                                  → {"ok":true,"found",…,"sentence",…,"occurrences":[≤8]}

v5 §2.4：`session_peek` 多了可选 `hwnd`（整数，0/缺省 = 不跑本机探针）。`browser` 两个来源
二选一（扩展优先、探针兜底），都拿不到就是 `null`，只新增 `source`（`extension` / `probe`）。
v5 §3：`media` 让外壳在呼出/收工时报停播/续播；hub 在内存里持有 `paused_by_us`，退出时补续播。

spool：外壳把一条捕获写成 `data/inbox.jsonl` 的一行（字段见 `store.capture`，
外加会话线索 `source_app` / `source_title` 与 §6.1 强制新会话的 `new_session`）
—— 落库不依赖 hub 是否活着（§2 的兜底）。hub 在**启动时 / 每 5s / 收到 `flush` 时** ingest，
成功后截断文件；坏行跳过并记日志，绝不让整批失败。

日志（§10）：`logs/hub.log`，2MB × 4 滚动。**只记长度/数量/耗时/错误码**，
目标词、释义、原句、窗口标题一概不落日志；HTTP 访问日志只记路径、丢掉查询串
（搜索词也是用户内容）。
"""

from __future__ import annotations

import argparse
import configparser
import difflib
import json
import logging
import logging.handlers
import os
import queue
import re
import secrets
import socket
import socketserver
import statistics
import sys
import traceback
import threading
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import asr_align
import asr_client
import asr_jobs
import browsercontext
import capture_scheduler
import corrections
import kernel
import ocr_sampler
import band_cleanup
import maintenance
import playback_log
from article import ArticleIndex, _ascii_lower, normalize_punct
from dict_sources import (
    DEFAULT_TIMEOUT_MS,
    DictService,
    EcdictProvider,
    EdgeTranslateProvider,
    TransmartProvider,
    YoudaoProvider,
    strip_tail_punctuation,
)
from edge_ws import PRIORITY_BACKGROUND, PRIORITY_USER
from longtext import (
    Passage,
    join_passages,
    segment_ocr,
    segment_pasted,
    split_paragraphs_pasted,
)
from provenance import video_id_of
from segmentation_source import (
    MANUAL_FORMAT,
    OCR_FORMAT,
    decode_ocr_lines,
    paragraph_kinds,
)
from sessions import (
    BROWSER_BLUR_GRACE_MS,
    BROWSER_CONTEXT_TTL_MS,
    DEFAULT_IDLE_MIN,
    adopt_context,
    format_ts,
    session_provenance,
    site_of,
)
from settings import (
    HOTKEY_ACTIONS,
    HOTKEY_DEFAULTS,
    apply_config,
    hotkey_status_freshness,
    read_hotkey_status,
    settings_view,
    validate,
)
from speech import MAX_PREWARM_ITEMS, VOICES, Speech, SpeechError
from store import (
    ARTICLE_LIMIT_MAX,
    DEFAULT_ARTICLE_LIMIT,
    DEFAULT_LIMIT,
    KINDS,
    KEEP,
    REFILL_LIMIT,
    SCHEMA_VERSION,
    Store,
    export_words,
    is_latin_line,
    one_line,
    passage_breaks,
)

# v13 §2：`POST /api/tts` 的可选 `priority` 字段（后台预取/预热 vs 用户点击）；
# 只做取值映射，出队优先级与失败隔离都在 `Speech` / 连接池里。
TTS_PRIORITIES: dict[str, int] = {
    "user": PRIORITY_USER,
    "background": PRIORITY_BACKGROUND,
}
# v13 §5：`POST /api/tts/warm` 可要 2 条热连接（上限仍是池的 2 条）。
TTS_WARM_CONNECTIONS: tuple[int, ...] = (1, 2)

# v21 §7（票 #159）：文章定位索引的常驻篇数上限。`documents` 行在 v21 里不可变（要改就是
# 删了重建、新 doc_id），所以按 doc_id 缓存不需要失效逻辑；这里只限制内存里留几篇。
# v22 P2a 打破了这个前提（重分段 / 去重更新会原地改正文）→ 见 `_invalidate_article_index`。
ARTICLE_INDEX_CACHE_MAX = 2

# v22 P2a（票 #167 / spec §6.5）：文章指纹去重阈值。标题必须相等（`clean_title` 口径），
# 正文指纹 quick_ratio 预筛 + ratio 复核，≥0.9 才算「同一篇」。
DEDUP_THRESHOLD = 0.9
# 去重判定四态 + 拒绝态（`article_create` 的 `dedup` 取值；拒绝态不出现在成功回包里，
# 用错误串 `similar article exists` 表达）。抽常量避免散落的裸串（review 修复轮）。
DEDUP_NEW = "new"
DEDUP_REUSED = "reused"
DEDUP_UPDATED = "updated"
DEDUP_SIMILAR = "similar"
# 正文指纹的页码噪音（spec §6.5「去页码噪音」）：入库正文已经单行化，`longtext._PAGE_NOISE`
# 的整行匹配没有行边界可用，所以按「页码短语」在单行文本里剔除：`Page 3 of 12` / `- 3 -`。
_PAGE_NOISE_INLINE = re.compile(
    r"\bpage\s+\d{1,4}(?:\s*(?:of|/)\s*\d{1,4})?\b"
    r"|(?:^|\s)-\s*\d{1,4}\s*-(?=\s|$)",
    re.IGNORECASE,
)


# v21 T6（粘贴路径）：剪贴板正文的硬上限。正常文章 40–120KB，512KB 只用来挡异常的
# 超大剪贴板（防止一次 RPC 传进几百 KB 正文 + 建索引），超过回 `bad request`。
MAX_PASTE_CHARS = 512 * 1024

# v21 T6（`article_get` 的段落目录档，T5 面板「全文展开」用）：回包必须小 —— 每段只回
# 前 60 字符的单行 preview，一次最多 64 段（≈7KB），绝不带整段全文。
OUTLINE_PREVIEW_CHARS = 60
OUTLINE_SIDE_MAX = 32  # 锚点每侧最多段数
OUTLINE_SIDE_DEFAULT = 2  # 不给 before/after 时的默认窗口（±2 段）
OUTLINE_MAX_ENTRIES = 64  # 一次回包最多段数（before+after 的上限合计）

# v22 P1b（票 #166 / spec §6.3 §8.6）：取词台只读文章接口的分页口径（与记录列表的
# 500 分开：篇卡只有标题/计数，200 篇已远超单用户量级）。
ARTICLES_DEFAULT_LIMIT = DEFAULT_ARTICLE_LIMIT
ARTICLES_MAX_LIMIT = ARTICLE_LIMIT_MAX
# spec §9：段落异常阈值。只从**现有数据**推导（空段 / 段长 > 2000 字），不伪造。
PARAGRAPH_MAX_CHARS = 2000

TOOL_DIR = Path(__file__).resolve().parent
EXTENSION_DIR = TOOL_DIR / "extension"
CONFIG_PATH = TOOL_DIR / "config.ini"
CONFIG_SECTION = "英语取词"
LOG_DIR = TOOL_DIR / "logs"
WEB_DIR = TOOL_DIR / "web"
DEFAULT_DB = TOOL_DIR / "data" / "capture.db"
DEFAULT_DICT_DB = TOOL_DIR / "data" / "ecdict.db"
INBOX_NAME = "inbox.jsonl"
HOTKEY_STATUS_NAME = (
    "hotkey-status.json"  # 外壳写的探测结果（§9.2），默认与数据库同目录
)
MEDIA_PAUSE_NAME = "media-pause.json"  # v5 §3：跨 hub 重启的暂停状态，默认与数据库同目录
MEDIA_PAUSE_TTL_S = 300  # 启动时只采纳 5 分钟内的暂停状态（§3）
DEFAULT_RPC_PORT = 47810
DEFAULT_WEB_PORT = 47811
INGEST_INTERVAL_S = 5.0  # §2：启动时 / 每 5s / 收到 flush 都 ingest
PROBE_CACHE_S = 5.0  # v5 §2.4：探针结果按 (hwnd,title) 缓存 ~5s，一次捕获里不重复 UIA
PROBE_TIMEOUT_MS = 800  # v5 §2.2：探针工作线程的截止时间
LOG_MAX_BYTES = 2 * 1024 * 1024
LOG_BACKUPS = 4
# 2026-11-06 播放链路日志补齐：慢请求 / 慢合成单独预警（原来只有 INFO 访问行，
# 10s 级的合成卡在页面上完全看不出来）。阈值只用于记日志，不改任何超时与行为。
SLOW_REQUEST_MS = 5000
TTS_SLOW_MS = 3000
MAX_BODY_BYTES = 1024 * 1024  # 单请求体上限，防呆（正常请求都是几百字节）
# 票 #186（H8）：RPC 单行上限（正常请求都是几百字节）。超过回 bad json 并断开，
# 对端不发换行也不许把内存吃穿。
MAX_RPC_LINE_BYTES = 1024 * 1024
# 票 #186（H8）：两个服务器的连接兜底超时（秒）——连上不说话/半行不发完的死连接
# 不再永久占住线程。HTTP 侧 idle keep-alive 由 http.server 自己按超时干净关闭。
HANDLER_TIMEOUT_S = 30
# 票 #178：RPC 请求编号的字段名。故意不叫 `id` —— 业务回包里有自己的 `id`
#（`article_create` 的 doc_id、`article_paragraphs` 的条目 id），重名会让外壳
# 解析到错误的那一个。旧外壳不发这个键 → 回包也不带 → 那边退回位置 FIFO。
RPC_ID_KEY = "_rpc_id"

# v36 #246（spec 决策 6）：ping 的 `cap_cc` 只能取中枢来源枚举里的四个值。
# 未知值归一成 `off`，外壳的扁平等值比较就不会静默 miss。

# 心跳限流（2026-11-10）：扩展的 alarms 兜底心跳会**把所有标签页叫一遍**（每页一条），
# 用户开几十个标签时 hub 会在几十毫秒内收到几十条（实测 40ms 内 30 条，全是
# `video=no`）。每条都要记一行 INFO + 跑一次调度 tick，把日志和状态机一起打爆 ——
# 用户看到的「反应迟钝 / 起得慢」有一半是它。真实心跳间隔 2 秒，1 秒 8 条已经
# 是 16 倍余量；只有风暴才会被限流，正在看的那一页 2 秒内一定会再发。
CTX_RATE_WINDOW_S = 1.0
CTX_RATE_MAX = 8
# 心跳「存活」兜底行：观测不变的重复心跳降到 DEBUG 后，靠这条每分钟报一次还在收。
CTX_ALIVE_LOG_S = 60.0

CAPTURE_CC_VALUES = frozenset({"ok", "no_english_track", "error", "off"})

log = logging.getLogger("hub")


# --- 配置与小工具 ------------------------------------------------------------

_MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".png": "image/png",
    ".webmanifest": "application/manifest+json",
    ".txt": "text/plain; charset=utf-8",
    ".woff2": "font/woff2",
}


def round_ms(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)


def _request_int(value: Any) -> int | None:
    """RPC 请求里的整数（`doc_id` / `passage_index` / `current_passage`）。

    外壳发的是 JSON 数字，但字符串形态也收（`"17"`）；布尔、小数、非法串一律 None
    —— 调用方据此回 `bad request` / `bad passage`，**不静默猜**（与 `X-TTS` 同一纪律）。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def read_config(path: Path = CONFIG_PATH) -> dict[str, str]:
    """读 `[英语取词]` 段（与 `kernel.read_config` 同款；interpolation=None 容忍 `%A_ScriptDir%`）。"""
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
        if key in config:
            log.warning(
                "config 的 %s 不是整数（%r），用默认值 %s", key, config[key], default
            )
        return default


def megabytes_to_bytes(megabytes: int) -> int:
    """config 里的整数兆 → 字节（`0` 保持 `0` = 该规则关闭）。"""
    return max(0, int(megabytes)) * 1024 * 1024


def ensure_extension_token(config_path: Path) -> str:
    """票 #179：读 config 新键 `extension_token`，缺键时生成随机令牌并回写（§4 契约）。

    令牌是 hub 与扩展的共享密钥，只存 config.ini（本机状态，不提交）；扩展经
    `GET /api/extension/token` 来取。写失败（文件缺失 / 没有段 / 只读盘）不挡启动
    —— 现场生成的令牌本次进程内仍然有效，代价只是重启后扩展重取一次。
    """
    token = (read_config(config_path).get("extension_token") or "").strip()
    if token:
        return token
    # token_urlsafe 只出 [A-Za-z0-9_-]，不带 ini 的注释符 `;` 与分隔符 `=`（settings §9.2）
    token = secrets.token_urlsafe(32)
    try:
        apply_config(config_path, {"extension_token": token})
    except (OSError, ValueError) as exc:
        log.warning("extension_token 回写失败（%s），本次用进程内令牌", type(exc).__name__)
    return token


def extension_version(path: Path | None = None) -> str:
    """仓库内浏览器扩展的版本（`extension/manifest.json` 的 `version`）。

    v36（#240）版本握手：扩展在心跳里上报自己**运行中**的 manifest 版本
    （`extVersion`），hub 用这个值与仓库内版本比对 —— 改过扩展后必须手动重载，
    这件事从此在日志 / `/api/browser/context` 里可见。读不到（文件缺失 / 坏 JSON /
    无 version）= 空串：只用于日志与只读端点，绝不抛。
    """
    manifest = EXTENSION_DIR / "manifest.json" if path is None else Path(path)
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    version = data.get("version") if isinstance(data, dict) else None
    return version.strip() if isinstance(version, str) else ""


def resolve_path(value: str | None, default: Path) -> Path:
    """config 里的路径可能是 `%A_ScriptDir%\\data\\capture.db`（外壳的写法），也可能相对工具目录。"""
    if not value:
        return default
    text = str(value).replace("%A_ScriptDir%", str(TOOL_DIR)).strip().strip('"')
    path = Path(text)
    return path if path.is_absolute() else TOOL_DIR / path


def setup_logging(log_dir: Path = LOG_DIR, enabled: bool = True) -> None:
    """`logs/hub.log`，2MB × 4 滚动（§10）。调用方负责在缺省时别重复添加 handler。

    播放事件单独写 `logs/playback.log`（2MB × 3，见 playback_log.py）：高频事件不冲掉
    hub.log 的访问行，也方便按天/按容量清理。`enabled=0` 时两个出口一起静默。
    """
    playback_log.setup(log_dir, enabled)
    if not enabled:
        logging.getLogger().addHandler(logging.NullHandler())
        return
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        log_dir / "hub.log",
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUPS,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)


def emit(text: str) -> None:
    """CLI 输出；`pythonw.exe` 下 `sys.stdout` 是 None，直接跳过。"""
    if sys.stdout is not None:
        print(text)


def port_free(port: int, host: str = "127.0.0.1") -> bool:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


# --- HTTP 响应 --------------------------------------------------------------


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes
    content_type: str = "application/json; charset=utf-8"
    # 额外响应头（票 #106：`/api/tts` 的 X-TTS-*）；默认空，现有调用不受影响
    headers: dict[str, str] = field(default_factory=dict)


def json_response(payload: Any, status: int = 200) -> Response:
    return Response(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def error_response(status: int, message: str) -> Response:
    return json_response({"ok": False, "error": message}, status)


# --- v23 补释义（§4.1 / §4.2 / §5.2）----------------------------------------

REFILL_SCOPES = ("records", "session", "article")
# spec §4.1 + 票 #182（D1）的失败原因：回包自带 `reason` 时只认这些值（未知值仍走旧映射）。
# network/parse/http 是词典链 D1 扩出来的新档（`_refill_reason` 的映射产物）。
REFILL_REASONS = frozenset(
    {
        "not found",
        "timeout",
        "offline",
        "word-mismatch",
        "empty",
        "fragment",
        "network",
        "parse",
        "http",
    }
)


def _same_word(left: Any, right: Any) -> bool:
    """补释义的身份校验：大小写不敏感、空白归一（与外壳的「词对不上」同口径）。"""
    return one_line(str(left or "")).lower() == one_line(str(right or "")).lower()


def meaning_text(result: Any) -> str:
    """回包里给用户看的释义文本（词典义优先，机翻只给译文）。"""
    if not isinstance(result, dict):
        return ""
    return one_line(result.get("cn")) or one_line(result.get("en"))


def meaning_miss_reason(
    target: str, result: Any, *, online_enabled: bool = True
) -> str | None:
    """词典回包是否可以写回释义（`None` = 可写），否则给补释义/重查的 reason 档。

    v23 补释义与 v29 单条重查共用这一个口径：失败回包自带的 reason 直通；
    源返回别的词一律拒收（`word-mismatch`，宁可留旧义也不填错义）。
    """
    if not isinstance(result, dict):
        return "timeout"
    if not result.get("ok"):
        # 碎片中间态最先判：它没发过查询，比任何 reason 档都准（D1 之后
        # 失败回包总带 `reason`，不提前判会把它压成 not found）。
        if result.get("source") == "fragment":
            return "fragment"
        # 回包自带原因（v23 审核修正：短语词典路径的 timeout 不再拍平成 not found；
        # 票 #182 D1：network/parse/http/word-mismatch 直通，不压成 not found）。
        # 只认 REFILL_REASONS 的值；离线时「not found」仍按 offline 报（§6）。
        reason = result.get("reason")
        if reason in REFILL_REASONS:
            if reason == "not found" and not online_enabled:
                return "offline"
            return str(reason)
        error = str(result.get("error") or "")
        if error == "timeout":
            return "timeout"
        if error == "not found" and not online_enabled:
            # 离线时在线源整段没跑：「没查到」不等于「词典未收录」（§6）。
            return "offline"
        return "not found"
    # 源返回了别的词：拒绝写入，宁可留空也不填错义（#174 的纪律）。
    if not _same_word(result.get("word"), target):
        return "word-mismatch"
    if not meaning_text(result):
        return "empty"
    return None


@dataclass
class RefillJob:
    """一次 HTTP 补释义任务的进度（只在内存；hub 重启即丢，10 分钟后过期）。

    线程安全：worker 线程只调 `processed()`，HTTP 线程只调 `accept()/close()/snapshot()`，
    所有计数都在 `_lock` 里改。`done` 按「处理过的记录」计，失败条目带 `target` ——
    该字段只出现在 127.0.0.1 的 HTTP 回包，**不进日志**。
    """

    job_id: str
    started_at: str
    total: int = 0
    done: int = 0
    filled: int = 0
    skipped: int = 0
    failed: list[dict[str, Any]] = field(default_factory=list)
    finished_at: str | None = None
    _closed: bool = False
    _finished_mono: float | None = None
    _started_mono: float = field(default_factory=time.monotonic)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def accept(self) -> None:
        """队列真的收下一条后才记 total（队列满被拒的不算「实际入队」）。"""
        with self._lock:
            self.total += 1

    def close(self) -> None:
        """入队结束。之前 done 可能已经追上 total，任务要等 close 才算完成。"""
        with self._lock:
            self._closed = True
            self._finish_locked()

    def processed(
        self,
        outcome: str,
        *,
        record_id: int,
        target: str,
        reason: str | None,
    ) -> None:
        with self._lock:
            self.done += 1
            if outcome == "filled":
                self.filled += 1
            elif outcome == "skipped":
                self.skipped += 1
            else:
                self.failed.append(
                    {
                        "record_id": int(record_id),
                        "target": target,
                        "reason": reason or "not found",
                    }
                )
            self._finish_locked()

    @property
    def finished(self) -> bool:
        with self._lock:
            return self.finished_at is not None

    def expired(self, now: float) -> bool:
        with self._lock:
            return (
                self._finished_mono is not None
                and now - self._finished_mono > RefillJobs.TTL_SECONDS
            )

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "state": "done" if self.finished_at is not None else "running",
                "total": self.total,
                "done": self.done,
                "filled": self.filled,
                "skipped": self.skipped,
                "failed": [dict(item) for item in self.failed],
                "started_at": self.started_at,
                "finished_at": self.finished_at,
            }

    def _finish_locked(self) -> None:
        if not self._closed or self.done < self.total or self.finished_at is not None:
            return
        self.finished_at = format_ts(datetime.now())
        self._finished_mono = time.monotonic()
        # 日志纪律（§6）：只记计数与耗时，不记词、不记释义。
        log.info(
            "补释义任务完成：共 %d 条，成功 %d，跳过 %d，失败 %d，耗时 %dms",
            self.total,
            self.filled,
            self.skipped,
            len(self.failed),
            round((self._finished_mono - self._started_mono) * 1000),
        )


class RefillJobs:
    """HTTP 补释义任务注册表：内存、单进程；完成任务 10 分钟后清掉。"""

    TTL_SECONDS = 600

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, RefillJob] = {}
        self._counter = 0

    def start(self) -> RefillJob:
        with self._lock:
            return self._create_locked()

    def start_if_idle(self) -> RefillJob | None:
        """「检查 + 建任务」原子化：并发两个 POST 只会有一个人拿到任务。"""
        with self._lock:
            if any(not job.finished for job in self._jobs.values()):
                return None
            return self._create_locked()

    def running(self) -> RefillJob | None:
        with self._lock:
            for job in self._jobs.values():
                if not job.finished:
                    return job
        return None

    def get(self, job_id: str) -> RefillJob | None:
        with self._lock:
            self._prune_locked()
            return self._jobs.get(job_id)

    def _create_locked(self) -> RefillJob:
        self._prune_locked()
        self._counter += 1
        job = RefillJob(f"r{self._counter}", format_ts(datetime.now()))
        self._jobs[job.job_id] = job
        return job

    def _prune_locked(self) -> None:
        now = time.monotonic()
        for job_id in [
            jid for jid, job in self._jobs.items() if job.expired(now)
        ]:
            self._jobs.pop(job_id, None)


@dataclass
class RecheckPreview:
    """v29 单条重查的预览：查完先给用户看新旧释义，确认后才写库。

    只在内存（5 分钟过期，最多 32 条）；`old_*` 用于 apply 时的并发保护 ——
    预览后用户又手改过释义 → 409，不让一次迟到的重查覆盖更新后的内容。
    """

    token: str
    record_id: int
    target: str
    old_meaning: str
    old_phonetic: str
    meaning: str
    phonetic: str
    source: str
    machine: bool
    dict_json: str
    created: float = field(default_factory=time.monotonic)

    def expired(self, now: float) -> bool:
        return now - self.created > RecheckPreviews.TTL_SECONDS


class RecheckPreviews:
    """重查预览注册表：内存、单进程；过期或超过上限时清掉最旧的。"""

    TTL_SECONDS = 300
    MAX_PENDING = 32

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, RecheckPreview] = {}
        self._counter = 0

    def put(
        self,
        *,
        record: dict[str, Any],
        target: str,
        meaning: str,
        phonetic: str,
        source: str,
        machine: bool,
        dict_json: str,
    ) -> RecheckPreview:
        with self._lock:
            self._prune_locked()
            self._counter += 1
            preview = RecheckPreview(
                token=f"rc{self._counter}",
                record_id=int(record["id"]),
                target=target,
                old_meaning=one_line(record.get("meaning")),
                old_phonetic=one_line(record.get("phonetic")),
                meaning=meaning,
                phonetic=phonetic,
                source=source,
                machine=machine,
                dict_json=dict_json,
            )
            self._items[preview.token] = preview
            while len(self._items) > self.MAX_PENDING:
                self._items.pop(next(iter(self._items)))
            return preview

    def pop(self, token: str, record_id: int) -> RecheckPreview | None:
        with self._lock:
            self._prune_locked()
            preview = self._items.get(token)
            if preview is None or preview.record_id != record_id:
                return None
            return self._items.pop(token)

    def _prune_locked(self) -> None:
        now = time.monotonic()
        for token in [
            token
            for token, preview in self._items.items()
            if preview.expired(now)
        ]:
            self._items.pop(token, None)


@dataclass(frozen=True)
class _FillItem:
    record_id: int
    target: str
    # v34 T1（票 #214 / spec D26）：目标是用户确认过的完成态（挑词 / 手动补释义），
    # 绕过碎片门禁；spool 入库保持 False（外壳打字中间态需要这道止损）。
    trusted: bool = False


class MeaningFillQueue:
    """v23 §4.1 §5.2：补释义的单 worker 队列（HTTP 任务与以后 ingest 后台补齐共用）。

    - **一条一条查**：同一时刻最多一个在线词典请求，条目之间停 `interval`（默认 50ms），
      不与用户前台查询抢源；查询预算就是 `DictService.timeout_ms`（不改）。
    - 多词目标带 `dict_only=True`（与捕获时同口径），单词走默认链；**永不传 fast/full**
      —— 补出来的结果与「旧框二跳之后」同质量（§4.4）。
    - 失败原因映射成 spec §4.1 的六个字符串；异常只记类型（不记词/释义/原句）。
    - `stop()` 置停止位并 join ≤2s；worker 是 daemon，绝不吊住进程退出。
    """

    def __init__(
        self,
        store: Store,
        dict_service: DictService,
        *,
        interval: float = 0.05,
        maxsize: int = 500,
    ) -> None:
        self.store = store
        self.dict_service = dict_service
        self.interval = interval
        self._queue: queue.Queue[_FillItem] = queue.Queue(maxsize=maxsize)
        # 「还在队列里 / 正在处理」的 record_id：同一 id 只留最早一条（§5.2 去重）。
        self._pending: set[int] = set()
        # v23 验收修正：同一 record 可能先被后台补齐占用、随后又被补释义任务选中。
        # 去重不能把任务丢掉 —— 后到的 job 挂到这条 pending 上，出结果时一起通知。
        self._pending_jobs: dict[int, list[RefillJob]] = {}
        # v34 T1（票 #214 / spec D26）：pending 里被「可信调用」升级过的记录。
        # spool 先占住同一条时，后到的挑词/手动补释义仍要能绕过碎片门禁。
        self._trusted_pending: set[int] = set()
        self._pending_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._thread_lock = threading.Lock()

    @property
    def alive(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self) -> None:
        with self._thread_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="meaning-fill", daemon=True
            )
            self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        """置停止位并 join ≤2s：在途查询最多 2.5s，超时就留给 daemon 线程自灭。"""
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def submit(
        self,
        record_id: int,
        target: str,
        *,
        job: RefillJob | None = None,
        trusted: bool = False,
    ) -> bool:
        """入队一条；同一 record_id 还在队列里（或正在查）→ 不重复排队。

        v23 验收修正（F1）：后台补齐先占住同一条时，后到的补释义任务**挂到这条
        pending 上**（`job.accept()` 计数，出结果时统一 `processed()`）；不再直接
        丢弃，否则任务会以 `total=0` 立刻「完成」、界面谎报「成功 0，仍缺 0」。
        没有 job 的重复提交仍返回 False（同一词只查一次）。

        v34 T1（票 #214 / spec D26）：`trusted=True` = 目标是用户确认过的完成态
        （挑词 / 手动补释义），worker 不再用「外壳打字中间态」的碎片门禁拦它。
        同一条 pending 已被 spool 占住时，可信调用把它升级为可信并照常返回 True。
        """
        try:
            rid = int(record_id)
        except (TypeError, ValueError):
            return False
        word = one_line(target)
        if not word:
            return False
        with self._pending_lock:
            if rid in self._pending:
                if trusted:
                    self._trusted_pending.add(rid)
                    if job is not None:
                        jobs = self._pending_jobs.setdefault(rid, [])
                        if not any(existing is job for existing in jobs):
                            jobs.append(job)
                            job.accept()
                    return True
                jobs = self._pending_jobs.setdefault(rid, [])
                if job is None:
                    return False
                if not any(existing is job for existing in jobs):
                    jobs.append(job)
                    job.accept()
                return True
            self._pending.add(rid)
            if trusted:
                self._trusted_pending.add(rid)
            if job is not None:
                self._pending_jobs[rid] = [job]
                job.accept()
        self.start()
        try:
            self._queue.put_nowait(_FillItem(rid, word, trusted))
        except queue.Full:
            with self._pending_lock:
                self._pending.discard(rid)
                self._trusted_pending.discard(rid)
                jobs = self._pending_jobs.pop(rid, [])
            # 已经 accept 进 total 的条目必须补一个终态，否则任务永远停在 running。
            for pending_job in jobs:
                pending_job.processed(
                    "failed", record_id=rid, target=word, reason="timeout"
                )
            return False
        return True

    # --- worker -------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            jobs: list[RefillJob] = []
            try:
                outcome, reason = self._fill(item)
            except Exception as exc:  # noqa: BLE001 - 单条坏掉不许打死 worker
                log.info("补释义单条异常：%s", type(exc).__name__)
                outcome, reason = "failed", "timeout"
            finally:
                with self._pending_lock:
                    self._pending.discard(item.record_id)
                    self._trusted_pending.discard(item.record_id)
                    jobs = self._pending_jobs.pop(item.record_id, [])
                self._queue.task_done()
            for pending_job in jobs:
                pending_job.processed(
                    outcome,
                    record_id=item.record_id,
                    target=item.target,
                    reason=reason,
                )
            # 条目之间歇一下（被 stop 叫醒就立刻退出）；首条不等待。
            if self._stop.wait(self.interval):
                break

    def _fill(self, item: _FillItem) -> tuple[str, str | None]:
        """查一条并写回；返回 `(filled|skipped|failed, reason)`。"""
        target = item.target
        # v34 T1（票 #214 / spec D27）：查询前归一化尾部标点（展示仍用原文）。
        # 单词与多词都适用；身份校验也用归一化后的查询（词典答的就是这一串）。
        query = strip_tail_punctuation(target)
        try:
            # 碎片中间态（含空格且末 token 不像完整词）不发查询：记 fragment 更准确，
            # 也不拿它去打在线源。替身没有这个方法时照旧查询。
            # v34 T1（spec D26）：这条门禁只服务「外壳打字中间态」；挑词与手动补释义
            # 是用户确认过的目标，trusted=True（含 pending 期间被升级的）一律放行。
            trusted = item.trusted
            if not trusted:
                with self._pending_lock:
                    trusted = item.record_id in self._trusted_pending
            looks_fragment = getattr(self.dict_service, "looks_fragment", None)
            if (
                not trusted
                and callable(looks_fragment)
                and looks_fragment(query)
            ):
                if self._already_filled(item.record_id):
                    return ("skipped", None)
                log.info("补释义跳过碎片目标（%d 字）", len(query))
                return ("failed", "fragment")
            result = self.dict_service.lookup(query, dict_only=" " in query)
            reason = meaning_miss_reason(
                query,
                result,
                online_enabled=getattr(self.dict_service, "online_enabled", True),
            )
            if reason is not None:
                # v23 验收修正（F2）：查询失败期间用户已手改 → 记 skipped（手改值保留），
                # 不要报成「词典未收录」。空记录才按 reason 记 failed。
                if self._already_filled(item.record_id):
                    return ("skipped", None)
                log.info("补释义未写入（reason=%s，%d 字）", reason, len(query))
                return ("failed", reason)
            written = self.store.refill_meaning(
                item.record_id,
                meaning=meaning_text(result),
                phonetic=str(result.get("phonetic") or ""),
                dict_json=json.dumps(result, ensure_ascii=False),
                machine=bool(result.get("machine")),
                expect_empty=True,
            )
        except Exception as exc:  # noqa: BLE001 - 查询/写库异常都按超时记账
            log.info("补释义查询异常：%s", type(exc).__name__)
            if self._already_filled(item.record_id):
                return ("skipped", None)
            return ("failed", "timeout")
        return ("filled" if written else "skipped", None)

    def _already_filled(self, record_id: int) -> bool:
        """记录当前是否已有非空释义（任务期间用户手改 / 别的路径已补上）。"""
        try:
            record = self.store.get_record(record_id)
        except Exception:  # noqa: BLE001 - 读库失败按「没填」处理，仍走原失败路径
            return False
        return bool(record and one_line(record.get("meaning")))


# --- 中枢本体 ---------------------------------------------------------------

# v33 T4（票 #210）：底图端点的文件名沙箱 —— 只认 `s<会话>_f<帧>.png` 一类的
# 裸文件名：首字符必须是字母/数字（`..png` 这类点开头伪装名一并拒绝），其余
# 限字母/数字/点/下划线/连字符，`.png` 小写结尾。目录分隔符 / 盘符 / 空白 /
# 空字节根本进不了这个形状，穿越与绝对路径在正则一步就死。
_BACKLOG_IMAGE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.png$")


class Hub:
    """RPC 与 HTTP 共用的门面：路由 + 数据拼装。所有方法都是线程安全的（store 内部有锁）。"""

    def __init__(
        self,
        store: Store,
        dict_service: DictService,
        *,
        web_dir: Path = WEB_DIR,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        config_path: Path = CONFIG_PATH,
        hotkey_status_path: Path | None = None,
        media_pause_path: Path | None = None,
        probe_url: bool = True,
        media_pause: bool = True,
        resolve_account_online: bool = True,
        speech: Speech | None = None,
        extension_token: str | None = None,
        video_capture: bool = True,
        scheduler: capture_scheduler.CaptureScheduler | None = None,
        band_ocr: Any | None = None,
        band_fallback_dir: Path | None = None,
    ) -> None:
        self.store = store
        self.dict_service = dict_service
        self.web_dir = Path(web_dir)
        self.timeout_ms = timeout_ms
        # 设置面板（票 #32）：写 config.ini、读外壳写的 hotkey-status.json
        self.config_path = Path(config_path)
        # 票 #179：扩展共享令牌。生产由 `main()` 在服务启动前经 `ensure_extension_token`
        # 生成并回写 config 后赋进来；不传（测试 / 嵌入用）就现场生成一枚进程内的 ——
        # 构造函数**绝不**写 config.ini（测试跑在谁的机器上都不得碰用户的配置）。
        self.extension_token = extension_token or secrets.token_urlsafe(32)
        # v11（票 #106）：发音服务缝。生产传真实 `Speech`；测试注入假实现（HTTP 层不碰网络）。
        self.speech = (
            speech
            if speech is not None
            else Speech(store.db_path.parent / "tts", config_path=self.config_path)
        )
        self.hotkey_status_path = (
            Path(hotkey_status_path)
            if hotkey_status_path is not None
            else store.db_path.parent / HOTKEY_STATUS_NAME
        )
        # v5 §3：跨 hub 重启的暂停状态文件（与 data 目录同一套解析 = 数据库同目录）
        self.media_pause_path = (
            Path(media_pause_path)
            if media_pause_path is not None
            else store.db_path.parent / MEDIA_PAUSE_NAME
        )
        self.started = time.perf_counter()
        # 浏览器上下文（v3 §2.1 / v5 §2.3）：只存内存里的最新一条 + 收到时刻，**不落库**。
        # v5 起存两份：`_browser_context`（最新，含 focused:false）与 `_browser_last_focused`
        # （最后一次 focused:true 的那份），各自带收到时刻。
        self._browser_lock = threading.Lock()
        self._browser_context: dict[str, Any] | None = None
        # 2026-11-10 心跳限流 / 日志降噪的状态（见 CTX_RATE_*）
        self._ctx_win_start = 0.0
        self._ctx_win_count = 0
        self._ctx_dropped = 0
        self._ctx_alive_at = 0.0
        self._ctx_alive_count = 0
        self._ctx_last_observ = ("", False, False)
        self._browser_received_at: float | None = None
        self._browser_last_focused: dict[str, Any] | None = None
        self._browser_last_focused_at: float | None = None
        # v36（#240）可观测性：最近一次上报的扩展版本 + 上一次场景判定的
        # (scene, reason)。只用于日志去重与版本握手，不参与任何判定。
        self._ext_version = ""
        self._scene_last_scene = ""
        self._scene_last_reason = ""
        # v36（票 #241 / spec 决策 2）：多槽上下文 —— 按页面身份（href 归一化）保留，
        # 每槽带收到时刻与「是否聚焦」。**旁挂**在旧的三份字段旁边：`adopt_browser`
        # 的归属语义（最后说话的那条 / 最近聚焦那份 + 宽限）一个字不改。
        self._browser_slots: dict[str, dict[str, Any]] = {}
        # v36（场景识别）：扩展下行指令槽（take-one 语义，线程安全）。外壳经 RPC
        # `ext_command` 发放、扩展下一次心跳回包取走（或外壳经 RPC 消费）；`_clock`
        # 挂在实例上，测试 monkeypatch 它即可推进 TTL。
        self._clock = time.perf_counter
        self._ext_cmd_lock = threading.Lock()
        self._ext_cmd: dict[str, str] | None = None
        self._ext_cmd_at = 0.0
        # 本机探针（v5 §2.2）：按 (hwnd, title) 缓存 ~5s，避免一次捕获里重复 UIA 调用。
        self._probe_lock = threading.Lock()
        self._probe_key: tuple[int, str] | None = None
        self._probe_result: dict[str, Any] | None = None
        self._probe_at = 0.0
        self._probe_last_ms = 0
        self._probe_last_ok: bool | None = None
        # 媒体联动（v5 §3）：paused_by_us 只活在内存，guarded by `_media_lock`。
        self._media_lock = threading.Lock()
        self._paused_by_us = False
        self._media_app = ""
        self._media_title = ""
        # 最近一次真正采纳到 browser 的那次 peek（只放旗标，不放 URL/标题；与媒体会话无关）
        self._last_capture: dict[str, Any] | None = None
        # v5 §8 开关：main() 从 config 读入；`PUT /api/settings` 后 `reload_switches()` 重读
        self.probe_url = bool(probe_url)
        self.media_pause = bool(media_pause)
        # v33 P0（票 #211 首发崩溃修复）：band OCR 一律走 **worker 子进程代理**
        # —— hub 进程启动即预热 winrt(SMTC)/comtypes(UIA)，同进程再起 onnxruntime
        # 会原生段错误（T4 在案 pytest exit 139；真机 exit 2816）。代理构造零开销
        # （子进程首次调用才 spawn）；调度器采样与 reocr 端点共享**同一个**代理
        # = 同一个子进程。测试注入 `band_ocr=` 假件不跑模型。
        self._band_ocr = (
            band_ocr if band_ocr is not None else ocr_sampler.create_band_ocr_worker()
        )
        # D7 底图兜底根：采样器老落盘目录（ocr_sampler.DEFAULT_IMAGE_DIR）。
        # 写统一之后新句一律落 db 旁目录（capture_scheduler 传 image_dir），但
        # 旧句的 image_path 还指着老目录 —— 读侧在这里兜底。测试传
        # `band_fallback_dir=` 指到 tmp_path，绝不碰真实 data/。
        self.band_fallback_dir = (
            Path(band_fallback_dir)
            if band_fallback_dir is not None
            else ocr_sampler.DEFAULT_IMAGE_DIR
        )
        # v33 T5（票 #211）：采集编排调度器（心跳 → 触发/停止/降级调度）。
        # `video_capture` 是「视频收句」总开关的初值（reload_switches 热更新）；
        # 测试传 `scheduler=` 注入假件（计数/隔离断言用），不传就装生产默认。
        self.scheduler = scheduler or capture_scheduler.build_default_scheduler(
            store, self.config_path, enabled=bool(video_capture), band_ocr=self._band_ocr
        )
        self.scheduler.enabled = bool(video_capture)  # 注入的调度器同样吃初值
        # 2026-11-11 ASR P2：可选转写队列（config 有 key 才建）；无英文 CC 段排队。
        self._asr_lock = threading.Lock()
        self._asr_state: dict[str, Any] = {
            "state": "off",
            "session": 0,
            "added": 0,
            "reason": "",
        }
        self._asr_signature: tuple[bool, str, str] | None = None
        self.asr_queue: asr_jobs.AsrJobQueue | None = None
        self._init_asr_queue()
        # v6 三期（票 #89）：账号解析联网开关；后台补齐线程同一时刻最多一个
        self.resolve_account_online = bool(resolve_account_online)
        self._account_lock = threading.Lock()
        self._account_thread: threading.Thread | None = None
        # v21 §7（票 #159）：doc_id → 长文定位索引（正文/段偏移不可变，按 id 缓存即安全）
        self._article_lock = threading.Lock()
        self._article_indexes: dict[int, ArticleIndex] = {}
        # v23 §4.1 §5.2：补释义单 worker（HTTP 任务与以后 ingest 后台补齐共用）+ 任务表。
        # daemon 线程：不 join 也不会吊住进程退出（`shutdown()` 会主动停并 join ≤2s）。
        self.meaning_fill = MeaningFillQueue(store, dict_service)
        self.meaning_fill.start()
        self.refill_jobs = RefillJobs()
        self.recheck_previews = RecheckPreviews()
        # v5 §3：启动时采纳（或丢弃）上一次重启前压着的暂停状态
        self._adopt_pending_pause()

    # --- ASR P2（2026-11-11）：无 CC 段排队转写 → 文本入待选句池 ----------------

    def _init_asr_queue(self) -> None:
        """按 config 建/重建转写队列；缺 key 或 `asr_enabled=0` → 不建（off）。"""
        try:
            config = read_config(self.config_path)
        except Exception as exc:  # noqa: BLE001 - 配置坏掉不许影响中枢启动
            log.warning("ASR 配置读取失败（%s）", type(exc).__name__)
            config = {}
        enabled = config_int(config, "asr_enabled", 1) != 0
        key = str(config.get("asr_siliconflow_key") or "").strip()
        model = str(config.get("asr_model") or "").strip() or asr_client.DEFAULT_MODEL
        signature = (enabled, key, model)
        if signature == self._asr_signature:
            return
        self._asr_signature = signature
        if self.asr_queue is not None:
            try:
                self.asr_queue.stop()
            except Exception as exc:  # noqa: BLE001
                log.warning("ASR 队列停止失败（%s）", type(exc).__name__)
            self.asr_queue = None
        if not enabled or not key:
            self._set_asr_state("off")
            log.info("ASR 未启用（%s）", "缺 key" if not key else "asr_enabled=0")
            return
        self.asr_queue = asr_jobs.AsrJobQueue(api_key=key, model=model, maxsize=2)
        setter = getattr(self.scheduler, "set_cc_missing_hook", None)
        if setter is not None:
            setter(self._asr_maybe_queue)
        log.info("ASR 已启用（%s）", model)

    def _asr_maybe_queue(self, session_id: int, bvid: str, page: int) -> bool:
        """调度器在「无英文 CC」时回调：排队一次转写；返回是否已排队。"""
        queue_obj = self.asr_queue
        if queue_obj is None:
            return False
        if int(page) > 1:
            log.info("ASR 排队跳过：多 P 暂不支持（page=%d）", page)
            return False
        duration = 0.0
        try:
            # 只认**这条视频**的时长：最新一条心跳可能来自别的标签页/别的视频，
            # 拿它铺时间窗会把整份转写放到错误的区间。
            for slot in self.browser_slots():
                ctx = slot.get("context") or {}
                if capture_scheduler.bvid_and_page(ctx.get("href")) == (
                    str(bvid),
                    int(page),
                ):
                    duration = float(ctx.get("duration") or 0)
                    break
            if duration <= 0:
                context, _age = self.browser_context()
                duration = float((context or {}).get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0.0
        session = int(session_id)

        def on_done(result: dict[str, Any]) -> None:
            self._asr_finish(session, result, duration)

        accepted = queue_obj.submit(asr_jobs.AsrJob(str(bvid), None, on_done))
        if accepted:
            self._set_asr_state("queued", session_id=session)
            log.info("ASR 已排队（会话 %d）", session)
        return accepted

    def _asr_finish(self, session_id: int, result: Any, duration: float) -> None:
        """worker 线程回调：成功 → 文本切句入池；失败 → 标记 + 撤回无字源豁免。"""
        if not isinstance(result, dict):
            result = {"reason": asr_client.REASON_ERROR, "text": ""}
        reason = result.get("reason")
        if reason is not None:
            self._set_asr_state("error", session_id=session_id, reason=str(reason))
            log.info(
                "ASR 转写失败（会话 %d，reason=%s），撤回无字源豁免",
                session_id,
                reason,
            )
            self._asr_clear_source(session_id)
            return
        rows = asr_align.transcript_rows(result.get("text") or "", duration)
        if not rows:
            self._set_asr_state("error", session_id=session_id, reason="empty")
            log.info("ASR 转写为空（会话 %d）", session_id)
            self._asr_clear_source(session_id)
            return
        try:
            added = self.store.add_subtitle_lines(session_id, rows)
        except Exception as exc:  # noqa: BLE001 - 单条坏掉不许打穿 worker
            self._set_asr_state("error", session_id=session_id, reason="store")
            log.warning("ASR 落池失败（%s）", type(exc).__name__)
            return
        final = int(added.get("added") or 0)
        try:
            self.scheduler.note_added(session_id, final)
        except Exception as exc:  # noqa: BLE001 - 段已换走，统计记不上不影响落池
            log.info("ASR 段统计跳过（%s）", type(exc).__name__)
        self._set_asr_state("done", session_id=session_id, added=final)
        log.info("ASR 转写落池（会话 %d，新增 %d 句）", session_id, final)

    def _asr_clear_source(self, session_id: int) -> None:
        try:
            self.scheduler.set_source_available(session_id, False)
        except Exception as exc:  # noqa: BLE001 - 调度器已换段/已退出
            log.info("ASR 撤回豁免跳过（%s）", type(exc).__name__)

    def _set_asr_state(
        self,
        state: str,
        *,
        session_id: int = 0,
        added: int = 0,
        reason: str = "",
    ) -> None:
        with self._asr_lock:
            self._asr_state = {
                "state": str(state),
                "session": int(session_id),
                "added": int(added),
                "reason": str(reason),
            }

    def _asr_snapshot(self) -> dict[str, Any]:
        with self._asr_lock:
            return dict(self._asr_state)

    def reload_switches(self) -> None:
        """重读开关并下推到活的属性（v5 §8、票 #67）：`PUT /api/settings` 之后调用。

        - `probe_url` / `media_pause` → 本对象的旗标；
        - `dict_online` → 活的 `dict_service.online_enabled`（关掉后在线源整段不跑）；
        - `resolve_account_online` → 本对象的旗标（关掉后后台补齐一个包都不发；
          下沉到 `resolve_session_account(online=…)` 的闸门也按它拦，票 #96 A3）；
        - `session_idle_min` → 活的 `store.idle_min`（会话切分立刻按新阈值）。
        """
        config = read_config(self.config_path)
        self.probe_url = config_int(config, "probe_url", 1) != 0
        self.media_pause = config_int(config, "media_pause", 1) != 0
        self.dict_service.online_enabled = config_int(config, "dict_online", 1) != 0
        self.resolve_account_online = config_int(config, "resolve_account_online", 1) != 0
        self.store.idle_min = config_int(config, "session_idle_min", DEFAULT_IDLE_MIN)
        # v11（票 #106）：`tts_online` 下推到活的发音服务（设置页热生效留好口子）
        self.speech.online_enabled = config_int(config, "tts_online", 1) != 0
        # v33 T5（票 #211）：「视频收句」总开关下推到调度器（关 = 全链静默）。
        # v34 T3（票 #216）：拨成 0 时顺手清零手动扳机 —— 再拨回 1 不会「自己开始」；
        # 拨 1 不清（用户按下的扳机不该被保存设置的动作打断）。
        capture_enabled = config_int(config, "video_capture", 1) != 0
        self.scheduler.enabled = capture_enabled
        if not capture_enabled:
            self.scheduler.set_armed(False)
        # 票 #116：音色也热更新 —— 保存后下一句就用新声音（缓存键含 voice，不会串音）
        voice = str(config.get("tts_voice") or "").strip()
        if voice in VOICES:
            self.speech.voice = voice
        # 2026-11-11 ASR P2：key/model/asr_enabled 变了就重建队列（签名相同不动）。
        self._init_asr_queue()

    def prewarm_targets(self, targets: Any) -> dict[str, int]:
        """v11 §3（票 #113）：把「本轮新入库的 target」交给后台预热队列。

        只发 target、不发原句；`tts_online=0` 时 `Speech.prewarm` 直接返回 queued 0
        （一个包都不发）。预热只是加速，任何失败都不许影响入库主流程。
        """
        try:
            return self.speech.prewarm(targets)
        except Exception as exc:  # noqa: BLE001 - 预热坏掉不许打断入库
            log.info("tts 预热跳过：%s", type(exc).__name__)
            return {"queued": 0, "skipped": 0}

    def ingest_inbox(self) -> dict[str, Any]:
        """v23 §4.2：spool 入库的**唯一中枢入口**（RPC flush / 定时 watcher / 启动）。

        落库本身仍是 `store.ingest_inbox`（唯一写入路径）；这里只做两件收尾：
        - 新入库的 target 照旧交给 TTS 后台预热（票 #113，行为不变）；
        - 入库后**释义仍为空**的词条投进补释义队列（非阻塞、按 record_id 去重）。

        返回 `store` 的原始结果（内部键 `targets` / `fill` 还在）：RPC 回话自己剥掉，
        补释义的查词与写回全部由 worker + `store.refill_meaning` 完成，这里不重复。
        """
        result = self.store.ingest_inbox()
        targets = result.get("targets") or []
        if targets:
            self.prewarm_targets(targets)
        for item in result.get("fill") or []:
            try:
                self.meaning_fill.submit(item["id"], item["target"])
            except Exception as exc:  # noqa: BLE001 - 补释义坏掉不许影响入库
                log.info("补释义入队跳过：%s", type(exc).__name__)
        return result

    # --- 账号解析（v6 三期，票 #89）：后台异步补齐，绝不进捕获关键路径 -------------

    def request_account_backfill(self) -> None:
        """打开取词台（`GET /api/sessions`）时按需补缺：**非阻塞**，只挂一个后台线程。

        - 开关关着直接返回（一个包都不发）；
        - 同一时刻最多一个补齐线程（线程还活着就别再挂），页面渲染不等它。
        """
        if not self.resolve_account_online:
            return
        with self._account_lock:
            current = self._account_thread
            if current is not None and current.is_alive():
                return
            thread = threading.Thread(
                target=self._backfill_accounts, name="account-backfill", daemon=True
            )
            self._account_thread = thread
        thread.start()

    def _backfill_accounts(self) -> None:
        """后台批量：只处理「有视频 ID 且还没有真实账号」的会话。

        `store.resolve_session_account` 内部已静默降级（超时 / 被墙 / 坏 JSON → 不写账号），
        所以这里连异常兜底都只是保险丝：补齐坏掉不许影响中枢。

        票 #96 A3：`online` 传**活的**开关属性（每次调用现读），用户关开关后立刻止住 ——
        挡不住的是已经走进网络调用里的那一次请求（见 `resolve_session_account` docstring）。
        """
        try:
            for session in self.store.list_sessions(include_empty=True):
                if not self.resolve_account_online:  # 跑到一半被关掉：立刻停手
                    return
                if session.get("account_name") or not video_id_of(session.get("url")):
                    continue
                self.store.resolve_session_account(
                    session["id"], online=self.resolve_account_online
                )
        except Exception as exc:  # noqa: BLE001 - 异步补齐绝不许往上抛
            log.info("账号补齐中断：%s", type(exc).__name__)

    def start_warmup(self) -> None:
        """后台预热探针（v5 §2.2）：**不阻塞启动**，失败只记一行（线程名 `probe-warmup`）。"""
        threading.Thread(target=self._warm_up, name="probe-warmup", daemon=True).start()

    def _warm_up(self) -> None:
        try:
            browsercontext.warm_up()
        except Exception as exc:  # noqa: BLE001 - 预热坏掉不许影响中枢
            log.info("预热失败：%s", type(exc).__name__)

    # --- RPC（47810，外壳专用）---------------------------------------------

    def rpc(self, request: Any) -> dict[str, Any]:
        if not isinstance(request, dict):
            return {"ok": False, "error": "bad request"}
        command = request.get("cmd")
        if command == "ping":
            return self.ping()
        if command == "lookup":
            return self.lookup(
                str(request.get("word") or ""),
                fast=bool(request.get("fast")),
                full=bool(request.get("full")),
                # v22 P5（#169）：多词目标的整条短语词典查询（外壳只在含空格时带）
                # v22 修复（#172）：词典未命中时回包可能带 machine:true（机翻兜底）
                dict_only=bool(request.get("dict_only")),
            )
        if command == "session_peek":
            app = request.get("app")
            title = request.get("title")
            browser = self.adopt_browser(app, title, request.get("hwnd"))
            return {
                "ok": True,
                **self.store.session_peek(app, title),
                # v3 §3：四个新键；browser 未采纳 = null、site 未采纳 = 空串
                "browser": browser,
                "site": browser["site"] if browser else "",
            }
        if command == "media":
            return self.media(
                str(request.get("op") or ""), request.get("app"), request.get("title")
            )
        if command == "flush":
            # v23 §4.2：flush 与定时 watcher / 启动共用 `ingest_inbox`（TTS 预热 +
            # 空释义投补释义队列都在里面）。`targets` / `fill` 只给中枢内部用，
            # 不进 RPC 回话（§2 的 flush 契约不变：{ok,ingested,dedup,skipped,last}）。
            result = self.ingest_inbox()
            result.pop("targets", None)
            result.pop("fill", None)
            # v23 P2：`last` 来自 capture，新增的 target/meaning 只是 ingest 内部附加信息，
            # flush 的对壳契约保持原样（只有 dedup/hits 等业务字段），不把词/释义带进回包。
            last = result.get("last")
            if isinstance(last, dict):
                result["last"] = {
                    key: value
                    for key, value in last.items()
                    if key not in ("target", "meaning")
                }
            return {"ok": True, **result}
        # v21 长文（规格 §7 / 票 #159）：三条命令都**同步**返回，服务端不推送、不乱序。
        if command == "article_create":
            return self.article_create(request)
        if command == "article_get":
            return self.article_get(request)
        if command == "article_locate":
            return self.article_locate(request)
        # v22 P2a（票 #167 / spec §6.4）：预览窗数据 / 重新分段 / 删文章
        if command == "article_paragraphs":
            return self.article_paragraphs(request)
        if command == "article_resegment":
            return self.article_resegment(request)
        if command == "article_delete":
            return self.article_delete(request)
        # v33 T5（票 #211）：字幕带重拖（第三档函数入口）。外壳手势把屏幕矩形（物理
        # 像素）送进来，调度器持久化 + 重置签名失检；rect 不合法一律 ok:false，不猜。
        if command == "band_redrag":
            return self.band_redrag(request.get("rect"))
        # v34 T3（票 #216 / spec §B12）：收句手动扳机（外壳热键发 toggle）
        if command == "capture_arm":
            return self.capture_arm(request.get("op"))
        # v36（场景识别）：扩展下行指令通道 —— 外壳发放（issue），外壳/调试也可消费；
        # 扩展侧的真实取走走心跳回包（_api_browser_context_post）。
        if command == "ext_command":
            return self.ext_command(request.get("issue"))
        return {"ok": False, "error": "unknown cmd"}

    def capture_arm(self, op: Any) -> dict[str, Any]:
        """`{"cmd":"capture_arm","op":"toggle"|"on"|"off"}`（v34 T3 / spec §B12）。

        回包 `{ok, armed, reason}`；`reason` ∈ `""` / `"no_video"` / `"disabled"`。
        `op` 非法一律 bad request（不猜）；「当前页面是不是视频页」由 hub 判
        （浏览器上下文在 hub 手里），其余状态机语义在 `CaptureScheduler.capture_arm`。
        """
        if op not in ("toggle", "on", "off"):
            return {"ok": False, "error": "bad request"}
        return self.scheduler.capture_arm(
            str(op), video_page=self._current_page_is_video()
        )

    def _current_page_is_video(self) -> bool:
        """当前浏览器上下文是不是 B站视频页（`on` 的门槛）。

        用最新一条心跳（含失焦时补发的 focused:false 那条），并要求新鲜
        （`BROWSER_CONTEXT_TTL_MS`，30 秒）—— 没有上下文 / 上下文过期一律不算，
        宁可不给开，也不拿三天前的页面把扳机打开。判定与调度器的触发门同一个
        `bvid_and_page`（域 + 播放页 + BV 号）。
        """
        context, age_ms = self.browser_context()
        if context is None or age_ms > BROWSER_CONTEXT_TTL_MS:
            return False
        return capture_scheduler.bvid_and_page(context.get("href")) is not None

    def scene_of(self) -> dict[str, Any]:
        """v36 场景识别：当前上下文是什么场景（ping / 桌宠邀请共用这一个判定）。

        返回 `{"scene", "video", "playing"}`：

        - 选哪一份上下文：`capture_scheduler.pick_scene_context(browser_slots())`
          —— **最近仍在播视频的那一槽**（多窗口下认得出正在播的那个，而不是最后
          说话的那个）；一个在播的都没有时回落最新鲜的那一槽，好让 `video` /
          `playing` 仍能如实回答暂停 / 非视频页；
        - 新鲜度用**场景自己的**预算 `SCENE_FRESH_MS`（宽），与取词归属的
          `BROWSER_CONTEXT_TTL_MS`（30s，严）解耦（决策 3）；
        - `video` / `playing` 是**原始观测**（是否 B站视频页 / 是否正在播放，
          复用调度器同一个 `bvid_and_page` 判定门与 `playing_of`）；
        - `scene == "video_invite"` 的全部前提（缺一不可）：调度器总开关开着、
          **未 armed**（armed 后就是正常收句流程，不再是邀请时机）、正在播放。

        任何异常都回落空场景 —— 场景识别是锦上添花，绝不影响 ping 主链路。

        v36（#240）：判定结果变化（无场景 ↔ 邀请场景）与**失败原因**进日志（经
        `_scene_result`，同一 (scene, reason) 不重复记）；日志只记判定与布尔观测，
        不记 URL / 标题（§2.7）。
        """
        fallback = {"scene": "", "video": False, "playing": False}
        try:
            slot = capture_scheduler.pick_scene_context(self.browser_slots())
            if slot is None:
                return self._scene_result(fallback, "无上下文")
            context = slot["context"]
            video = capture_scheduler.bvid_and_page(context.get("href")) is not None
            if not video:
                return self._scene_result(
                    {"scene": "", "video": False, "playing": False}, "非视频页"
                )
            playing = capture_scheduler.playing_of(context)
            if not playing:
                return self._scene_result(
                    {"scene": "", "video": True, "playing": False}, "未播放"
                )
            # 邀请场景的前提：总开关开 + 未 armed（armed 后是正常收句流程）
            if not bool(getattr(self.scheduler, "enabled", False)):
                return self._scene_result(
                    {"scene": "", "video": True, "playing": True}, "总开关关"
                )
            if bool(self.scheduler.status().get("armed")):
                return self._scene_result(
                    {"scene": "", "video": True, "playing": True}, "已 armed"
                )
            return self._scene_result(
                {"scene": "video_invite", "video": True, "playing": True},
                "邀请条件齐",
            )
        except Exception as exc:  # noqa: BLE001 - 场景算不出来回落空，不许抛
            log.warning("场景识别异常（%s），回落空场景", type(exc).__name__)
            return self._scene_result(
                {"scene": "", "video": False, "playing": False},
                "异常 " + type(exc).__name__,
            )

    def video_ended(self) -> bool:
        """当前场景上下文里的视频是不是**播到末尾停住了**（`cap_ended` 的来源）。

        2026-11-10 用户要求：「视频播放完毕能够自动暂停收句结束」。判定交给纯函数
        `capture_scheduler.ended_of`（`paused` 为真 **且** `currentTime` 到末尾容差内），
        这里只负责选槽与身份确认：

        - 选槽与 `scene_of` 完全同源（`pick_scene_context`）——播完时没有「仍在播」
          的槽，它回落最新鲜的那一槽，正是刚播完的那个页面；
        - 不是 B站 视频页 → False（音频页 / 首页不该触发停收句）；
        - 任何异常一律 False：这是**锦上添花**的自动化，宁可不停也不能误停。

        为什么不让外壳自己读 `/api/browser/context`：那一路是「取词归属」的上下文
        （谁最后说话），与场景选槽不是同一份（决策 2）；外壳每秒本来就拿 ping，
        多一个扁平标量最省事（外壳没有 JSON 解析器，spec §C22）。
        """
        try:
            slot = capture_scheduler.pick_scene_context(self.browser_slots())
            if slot is None:
                return False
            context = slot["context"]
            if capture_scheduler.bvid_and_page(context.get("href")) is None:
                return False
            return capture_scheduler.ended_of(context)
        except Exception as exc:  # noqa: BLE001 - 判不出来按「没播完」，不许抛出
            log.warning("播完判定异常（%s），按未播完处理", type(exc).__name__)
            return False

    def _scene_result(self, result: dict[str, Any], reason: str) -> dict[str, Any]:
        """记下场景判定（值原样返回给调用方）。

        #240：只在 (scene, reason) 变化时记一行 —— `scene_of` 跑在 1Hz ping 上，
        不去重会把日志刷满。日志只写判定名 / 原因 / 两个布尔观测，不碰页面内容。
        （无锁：只用于日志去重，竞态最多多打一行，不影响返回值。）
        """
        scene = result["scene"]
        if scene != self._scene_last_scene or reason != self._scene_last_reason:
            transition = (
                f"{self._scene_last_scene or '无场景'} → {scene or '无场景'}"
                if scene != self._scene_last_scene
                else f"保持 {scene or '无场景'}"
            )
            log.info(
                "场景判定：%s（%s；video=%s playing=%s）",
                transition,
                reason,
                "yes" if result["video"] else "no",
                "yes" if result["playing"] else "no",
            )
            self._scene_last_scene = scene
            self._scene_last_reason = reason
        return result

    # --- 扩展下行指令（v36：take-one 指令槽，RPC 发放 / 心跳回包取走）---------------

    _EXT_CMD_TTL_S = 10.0

    def ext_command(self, issue: Any = None) -> dict[str, Any]:
        """扩展下行指令通道（v36）。`issue=None` 消费、`issue=dict` 发放。

        - **发放**：只接受 `{"cmd": "player_fullscreen", "href": 非空str}` 形状
          （cmd 严格相等，多余键忽略）；合法存槽回 `{"ok": True}`，非法回
          `{"ok": False, "error": "bad request"}`。同一时刻最多压一条（新发放顶掉旧）。
          注意 `issue=None` 是消费、`issue={}` 等其余一切非 dict / 形状不对都是
          bad request —— 不猜。
        - **消费（take-one）**：槽非空且距发放超过 10 秒 = 过期，清槽回 `{"cmd": ""}`；
          否则回 `{"cmd", "href"}` 并**立即清槽** —— 绝不重复下发。无指令回 `{"cmd": ""}`。
        """
        if issue is None:
            with self._ext_cmd_lock:
                if self._ext_cmd is not None and (
                    self._clock() - self._ext_cmd_at > self._EXT_CMD_TTL_S
                ):
                    self._ext_cmd = None
                if self._ext_cmd is None:
                    return {"cmd": ""}
                cmd = dict(self._ext_cmd)
                self._ext_cmd = None  # take-one：取走即清，绝不重复下发
                return cmd
        if not isinstance(issue, dict):
            return {"ok": False, "error": "bad request"}
        cmd_name = issue.get("cmd")
        href = issue.get("href")
        if cmd_name != "player_fullscreen" or not isinstance(href, str) or not href.strip():
            return {"ok": False, "error": "bad request"}
        with self._ext_cmd_lock:
            self._ext_cmd = {"cmd": "player_fullscreen", "href": href}
            self._ext_cmd_at = self._clock()
        # #240：下发点留痕（只记指令名，不记 href —— §2.7 隐私纪律）
        log.info("下行指令已入槽：%s", cmd_name)
        return {"ok": True}

    def _take_ext_cmd_for(self, href: Any) -> dict[str, str] | None:
        """心跳回包取指令：槽里 href 与这条心跳 href **逐字相等**才发放并清槽。

        href 不等（扩展已在别的页面）不下发不清槽 —— 等对应页面的下一次心跳；
        日志纪律（§2.7）：只记动作与时长，不记 href / 标题内容。
        """
        with self._ext_cmd_lock:
            if self._ext_cmd is None:
                return None
            if self._ext_cmd.get("href") != href:
                return None
            expired = self._clock() - self._ext_cmd_at > self._EXT_CMD_TTL_S
            cmd = self._ext_cmd
            self._ext_cmd = None  # 发放即清槽（过期同样清，只是不下发）
            if expired:
                log.info("下行指令已过期丢弃（存活 %.1fs）", self._EXT_CMD_TTL_S)
                return None
            return cmd

    def band_redrag(self, rect: Any) -> dict[str, Any]:
        """`{"cmd":"band_redrag","rect":[x,y,w,h]}`（v33 T5 / spec §C11 第三档）。

        调度器 `redrag` 的回包自带 bool `ok`，这里原样转发（v33 修复轮：删掉
        冗余的 spread 重包）。
        """
        return self.scheduler.redrag(rect)

    # --- 长文文章（v21 §7 / 票 #159；只走 RPC，HTTP 文章视图属规格 Out of Scope）---

    def article_create(self, request: dict[str, Any]) -> dict[str, Any]:
        """整篇落库，回 `{ok, doc_id, session_id, title, passage_count, dedup[, passage_breaks]}`。

        两种入口（规格 §7 / T6）：

        - **OCR 路径**（`format != "manual"`，或显式给了 `passages`）：`text` 必须已经
          由外壳单行化（`one_line` 口径），本方法**不再折行** —— 段落偏移是按外壳那次
          单行化算的，再折一次偏移全错。这里连 `strip` 都不做，原样存储。
        - **粘贴路径**（`format == "manual"` 且**没给** `passages`）：`text` 是原始
          剪贴板正文（可含换行、PDF 硬折行、页码行），hub 用
          `longtext.split_paragraphs_pasted` 分段 + 归一化，落库的是 `join_passages`
          出来的**单行化全文**，并在响应里回 `passage_breaks`（粘贴路外壳没有内核，
          只能从这里拿段偏移；字段形状与 `page_ocr` 的同名字段一致）。

        校验（非法一律 `{ok:false, error}`，**不静默修正**）：`empty text`（空 / 纯空白 /
        分段后 0 段）、`bad passages`、`session not found`；粘贴正文
        `len > MAX_PASTE_CHARS` → `bad request`。日志只记 doc_id / 字数 / 段数 / 耗时。

        v22 P1a（票 #170 / spec §6.7）：创建时的**原始分段输入**随文章落库
        （`documents.segmentation_source`）—— OCR 路径取请求里的 `segmentation_source`
        （可选；给了就必须能按行框编码解码，否则 `bad segmentation source`，因为
        段落类型与重新分段都靠它重跑），粘贴路径直接把**归一化前**的原始正文存一份。
        这是段落类型派生（§7.2）与 P2 重新分段（§6.4）的唯一输入。

        v22 P2a（票 #167 / spec §6.5）：落库前先做**同标题指纹去重**，回包永远带
        `dedup: "new" | "reused" | "updated"`；相似但互不包含时**不新建**，回
        `{ok:false, error:"similar article exists", doc_id}`（外壳提示先删旧的）。
        命中既有文章时回它的 `doc_id/session_id`，不产生新 id（spec §12）。
        """
        text = request.get("text")
        if not isinstance(text, str):
            return {"ok": False, "error": "bad request"}
        started = time.perf_counter()
        given = request.get("passages")
        manual = (
            str(request.get("format") or "").strip().lower() == MANUAL_FORMAT
            and given is None
        )
        # v22 P1a：`segmentation_source` 两入口分别取值（manual=粘贴原文；ocr=行框编码）。
        source: str | None = None
        try:
            if manual:
                raw = text  # `_segment_paste` 不改入参；这份就是归一化前的原始正文
                text, spans = self._segment_paste(text)
                source = raw
                target_format: Any = MANUAL_FORMAT
            else:
                target_format = request.get("format") or OCR_FORMAT
                raw_source = request.get("segmentation_source")
                if raw_source is not None:
                    if not isinstance(raw_source, str):
                        return {"ok": False, "error": "bad segmentation source"}
                    if raw_source:  # 空串 = 外壳没采到（旧内核），按「无原始输入」处理
                        if decode_ocr_lines(raw_source) is None:
                            return {"ok": False, "error": "bad segmentation source"}
                        source = raw_source
                # 去重之前先按 `create_document` 的同一份校验段偏移：非法入参必须拿
                # 既有错误码 `bad passages`，且任何写/查都不该发生在非法入参上。
                spans = passage_breaks(given, len(text))
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

        # v22 P2a / spec §6.5：**先判重再落库** —— 重复捕获不许产生碎片文章。
        # 票 #181 H3：判重与落库收进 store 的单次加锁方法（`create_document_unless_dupe`）
        # —— 原来两步各自加锁，中间的锁释放窗口里并发插进同题文章就各建一篇（TOCTOU）。
        def _decide(candidates: list[dict[str, Any]]) -> Any:
            """锁内跑的四态判定：判 `new` 回 None 让 store 同锁落库，命中回 `(state, candidate)`。"""
            dedup, candidate = self._dedup_match(text, candidates)
            return None if dedup == DEDUP_NEW else (dedup, candidate)

        try:
            outcome = self.store.create_document_unless_dupe(
                _decide,
                text,
                spans,
                title=request.get("title") or "",
                source_app=request.get("app"),
                source_url=request.get("url"),
                format=target_format,
                captured_at=request.get("captured_at"),
                segmentation_source=source,
            )
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        if isinstance(outcome, tuple):  # 判重命中既有文章：不落库，走既有回话
            dedup, candidate = outcome
            if dedup == DEDUP_SIMILAR and candidate is not None:
                log.info("文章捕获拒绝：同标题相似文章已存在 doc_id=%s", candidate["id"])
                return {
                    "ok": False,
                    "error": "similar article exists",
                    "doc_id": int(candidate["id"]),
                }
            return self._article_merge(
                candidate, text, spans, source, target_format, manual, dedup
            )
        document = outcome
        # 规格 §6 的「预建索引」：建文章时顺手把定位索引建好（几十 KB 约 40ms，一次性），
        # 之后第一次打词就不再付这笔。建索引失败不影响 create 已经成功的事实。
        stored = self.store.document(document["id"])
        if stored is not None:
            try:
                self._article_index(stored)
            except ValueError:  # 落库前已校验过段落偏移，走到这里说明库被人工改坏
                log.warning("文章索引预建失败 doc_id=%s", document["id"])
        log.info(
            "文章建立 doc_id=%s（%d 段 / %d 字，%dms）",
            document["id"],
            document["passage_count"],
            document["text_chars"],
            round_ms(started),
        )
        response: dict[str, Any] = {
            "ok": True,
            "doc_id": document["id"],
            "session_id": document["session_id"],
            "title": document["title"],
            "passage_count": document["passage_count"],
            "dedup": DEDUP_NEW,
        }
        if manual and stored is not None:
            # 粘贴路唯一能拿到段偏移的地方（外壳没有内核）；落库值原样回，避免两份算法。
            response["passage_breaks"] = stored["passages"]
        return response

    # --- 去重 / 合并（v22 P2a / spec §6.5）------------------------------------

    @staticmethod
    def _fingerprint(text: Any) -> str:
        """正文指纹（spec §6.5）：`one_line` → 小写 → 空白折叠 → 去页码噪音。"""
        stripped = _PAGE_NOISE_INLINE.sub(" ", one_line(text).lower())
        return " ".join(stripped.split())

    @staticmethod
    def _text_similarity(left: str, right: str) -> float:
        """两根指纹的相似度：`quick_ratio()`（O(n) 上界）预筛，过线再 `ratio()` 复核。

        `autojunk=False` 必须显式关：默认的 autojunk 启发式会把长串里的「常见字符」
        当 junk —— 字符级比对下它会让长正文的 ratio 失真（误判成 1.0 或漏判）。
        """
        matcher = difflib.SequenceMatcher(None, left, right, autojunk=False)
        if matcher.quick_ratio() < DEDUP_THRESHOLD:
            return 0.0
        return matcher.ratio()

    def _dedup_match(
        self, text: str, candidates: list[dict[str, Any]]
    ) -> tuple[str, dict[str, Any] | None]:
        """`article_create` 的去重判定（spec §6.5 四态 + 拒绝态）。

        只比**同标题**候选（`clean_title` 口径；票 #181 H3 起候选由调用方在 store
        锁内取好传入，本方法不再自己查库）；候选最新入库在前，同类先命中先采用：
        1. 指纹完全相同 → `reused`（旧文本包含新文本）；
        2. 旧指纹是新指纹的子串 → `updated`（新文本包含旧文本）；
        3. 新指纹是旧指纹的子串 → `reused`；
        4. 其余比相似度：≥0.9 且新更长 → `updated`；新更短 → `reused`；
           等长（互不包含）→ `similar`（拒绝新建，spec §6.5-3）。
        优先级按 spec 编号：updated（1）> reused（2）> similar（3）；指纹完全相同会
        立即回 `reused`（同一篇已存在时优先复用，不去更新另一个候选）；无候选 → `new`。
        返回 `(state, candidate)`，`new` 时候选为 None。
        """
        fingerprint = self._fingerprint(text)
        if not fingerprint:  # 纯空白正文：交给 create_document 报 `empty text`
            return DEDUP_NEW, None
        updated: dict[str, Any] | None = None
        reused: dict[str, Any] | None = None
        similar: dict[str, Any] | None = None
        best_similarity = 0.0
        for candidate in candidates:
            old_fingerprint = self._fingerprint(candidate["text"])
            if not old_fingerprint:
                continue
            if old_fingerprint == fingerprint:
                return DEDUP_REUSED, candidate
            if old_fingerprint in fingerprint:
                updated = updated or candidate
                continue
            if fingerprint in old_fingerprint:
                reused = reused or candidate
                continue
            ratio = self._text_similarity(fingerprint, old_fingerprint)
            if ratio < DEDUP_THRESHOLD:
                continue
            if len(fingerprint) > len(old_fingerprint):
                updated = updated or candidate
            elif len(fingerprint) < len(old_fingerprint):
                reused = reused or candidate
            elif similar is None or ratio > best_similarity:
                similar, best_similarity = candidate, ratio
        if updated is not None:
            return DEDUP_UPDATED, updated
        if reused is not None:
            return DEDUP_REUSED, reused
        if similar is not None:
            return DEDUP_SIMILAR, similar
        return DEDUP_NEW, None

    def _article_merge(
        self,
        candidate: dict[str, Any],
        text: str,
        spans: list[list[int]],
        source: str | None,
        target_format: Any,
        manual: bool,
        state: str,
    ) -> dict[str, Any]:
        """去重命中既有文章：`reused`（旧含新，一个字都不写）/ `updated`（新含旧）的落库与回包。

        `updated`：**旧 doc_id 保留**（不产生新 id，spec §12），正文 / 段偏移整体刷成新的，
        按 §6.6 重映射旧词条 + 失效索引缓存（与 documents 写回同一个事务，见
        `_rewrite_article`）；原始分段输入**只在本次请求真的带了时才替换**（OCR 档请求
        没带 source 时保留旧值，否则会把已有 source 清成 NULL）。`reused` 只读。
        两条都回外壳要的完整元信息（含旧 `session_id`），旧外壳零改动。
        """
        doc_id = int(candidate["id"])
        response: dict[str, Any] = {
            "ok": True,
            "doc_id": doc_id,
            "session_id": int(candidate["session_id"]),
            "title": candidate["title"],
            "passage_count": candidate["passage_count"],
            "dedup": state,
        }
        if state == DEDUP_REUSED:
            # spec §6.5-2 的 `reused` 是半实现（P2a 记录，P2b 处理）：
            # - 旧 v21 文章（source=NULL）重录后仍没有 source，后续 resegment 仍报
            #   `no segmentation source`；
            # - 回包的 passage_count/passage_breaks 是**旧文**的（OCR 外壳若拿新分段数
            #   校验会误报「段落数不一致」）；保留 `dedup:"reused"` 供外壳识别后跳过校验。
            if manual:
                response["passage_breaks"] = candidate["passages"]
            log.info("文章复用 doc_id=%s（%d 段）", doc_id, candidate["passage_count"])
            return response
        # updated：请求没带 source（OCR 档旧内核没采到）时保留旧 source，别把已有原始
        # 输入清成 NULL（review 修复轮）；manual 路径总会带原文，照旧替换。
        rewritten = self._rewrite_article(
            candidate,
            text,
            spans,
            segmentation_source=source if source is not None else KEEP,
            format=target_format,
        )
        if rewritten is None:
            return {"ok": False, "error": "not found"}
        remapped, needs_review = rewritten
        response["passage_count"] = len(spans)
        if manual:
            response["passage_breaks"] = spans
        log.info(
            "文章合并更新 doc_id=%s（%d 段 / 重映射 %d 条 / 待复核 %d 条）",
            doc_id,
            len(spans),
            remapped,
            needs_review,
        )
        return response

    def _rewrite_article(
        self,
        document: dict[str, Any],
        text: str,
        spans: list[list[int]],
        *,
        segmentation_source: Any = KEEP,
        format: Any = KEEP,
    ) -> tuple[int, int] | None:
        """§6.6 重映射 + **单事务**写回（`article_resegment` 与去重 `updated` 共用）。

        `Store.apply_resegment` 把 documents 行与 records.passage_index 放进同一个事务：
        中途失败不会留下「新正文 + 旧段号」。写成功后失效定位索引缓存（§6.4）。
        `segmentation_source` / `format` 默认 `KEEP`（保留旧值）。
        返回 `(remapped, needs_review)`；文章在写回前被并发删除 → `None`。
        """
        doc_id = int(document["id"])
        assignments, remapped, needs_review = self._remap_records(
            document, text, spans
        )
        applied = self.store.apply_resegment(
            doc_id,
            text,
            spans,
            assignments,
            segmentation_source=segmentation_source,
            format=format,
        )
        if applied is None:
            return None
        self._invalidate_article_index(doc_id)
        return remapped, needs_review

    @staticmethod
    def _segment_paste(raw: str) -> tuple[str, list[list[int]]]:
        """粘贴正文 → `(单行化全文, 段落偏移 [[start, end], ...])`。

        `longtext.split_paragraphs_pasted` 内部已经做过 `normalize_pasted`（CRLF 归一 +
        整行页码/页眉剔除）与段内去硬折行/连字符重连；偏移是 `join_passages` 口径
        （段间单空格），所以 `text = join_passages(passages)` 之后可以直接用。
        """
        if not raw.strip():
            raise ValueError("empty text")
        if len(raw) > MAX_PASTE_CHARS:
            raise ValueError("bad request")
        passages = split_paragraphs_pasted(raw)
        if not passages:
            # 整篇都是页码/页眉（例如只粘了个页脚）—— 归一化后没有正文可落库。
            raise ValueError("empty text")
        return join_passages(passages), [[p.start, p.end] for p in passages]

    def article_get(self, request: dict[str, Any]) -> dict[str, Any]:
        """三档回话（架构 §3.2 ② / T6），默认只回元信息（**不回全文**）。

        - 默认：元信息（`doc_id/session_id/title/passage_count/captured_at/text_chars`）；
        - `passage_index`：回那一段的文本 + 绝对偏移（跨重启重放当前句用）；
        - `outline: true`：回「段落目录」窗口（T5 面板的全文展开状态用），每段只有
          `passage_index/start/end/preview`（preview = 段首 ≤60 字符、单行），
          窗口 = `around` 前后各 `before` / `after` 段。

        `outline` 只认 JSON `true`（给了它时优先于 `passage_index`）。`around` 缺省 =
        从文章开头取前 `before+after+1` 段；数字越界**钳到合法范围**（目录是展示用途），
        参数类型非法回 `bad request`（类型错是调用方 bug，越界只是窗口没对准）。
        """
        doc_id = _request_int(request.get("doc_id"))
        if doc_id is None:
            return {"ok": False, "error": "bad request"}
        document = self.store.document(doc_id)
        if document is None:
            return {"ok": False, "error": "not found"}
        if request.get("outline") is True:
            return self._article_outline(document, request)
        spans = document["passages"]
        raw_index = request.get("passage_index")
        if raw_index is None:
            return {
                "ok": True,
                "doc_id": doc_id,
                "session_id": document["session_id"],
                "title": document["title"],
                "passage_count": len(spans),
                "captured_at": document["captured_at"],
                "text_chars": len(document["text"]),
            }
        index = _request_int(raw_index)
        if index is None or not 0 <= index < len(spans):
            return {"ok": False, "error": "bad passage"}
        start, end = (int(part) for part in spans[index])
        return {
            "ok": True,
            "doc_id": doc_id,
            "passage_index": index,
            "passage_text": document["text"][start:end],
            "passage_start": start,
            "passage_end": end,
            "passage_count": len(spans),
        }

    def _article_outline(
        self, document: dict[str, Any], request: dict[str, Any]
    ) -> dict[str, Any]:
        """段落目录窗口（`article_get outline` 的实现主体）。

        回包只带 `passage_index/start/end/preview` 四键（preview ≤60 字符），
        一次最多 `OUTLINE_MAX_ENTRIES` 段 —— 整段全文一个字都不回，保证回包 ≪10KB。
        """
        spans = document["passages"]
        count = len(spans)
        raw_around = request.get("around")
        around: int | None = None
        if raw_around is not None:
            around = _request_int(raw_around)
            if around is None:
                return {"ok": False, "error": "bad request"}
        before = self._outline_side(request.get("before"))
        after = self._outline_side(request.get("after"))
        if before is None or after is None:
            return {"ok": False, "error": "bad request"}

        if count == 0:
            return {
                "ok": True,
                "doc_id": document["id"],
                "passage_count": 0,
                "around": None,
                "outline": [],
            }
        if around is None:
            anchor: int | None = None
            start = 0
            end = min(count, before + after + 1)  # 「从头开始的前 N 段」
        else:
            anchor = max(0, min(count - 1, around))  # 越界钳进合法范围
            start = max(0, anchor - before)
            end = min(count, anchor + after + 1)
        end = min(end, start + OUTLINE_MAX_ENTRIES)  # before+after 合计上限 64 段
        text = document["text"]
        outline = []
        for index in range(start, end):
            begin, finish = (int(part) for part in spans[index])
            outline.append(
                {
                    "passage_index": index,
                    "start": begin,
                    "end": finish,
                    "preview": self._outline_preview(text, begin, finish),
                }
            )
        return {
            "ok": True,
            "doc_id": document["id"],
            "passage_count": count,
            "around": anchor,
            "outline": outline,
        }

    @staticmethod
    def _outline_side(value: Any) -> int | None:
        """`before` / `after`：缺省 `OUTLINE_SIDE_DEFAULT`；类型非法 → None；越界钳 0–32。"""
        if value is None:
            return OUTLINE_SIDE_DEFAULT
        number = _request_int(value)
        if number is None:
            return None
        return max(0, min(OUTLINE_SIDE_MAX, number))

    @staticmethod
    def _outline_preview(text: str, start: int, end: int) -> str:
        """段落预览：段首 ≤`OUTLINE_PREVIEW_CHARS` 字符，并折叠成单行。"""
        piece = text[start : min(end, start + OUTLINE_PREVIEW_CHARS)]
        return " ".join(piece.split())

    def article_locate(self, request: dict[str, Any]) -> dict[str, Any]:
        """目标词 → 原句 + 段落 + 偏移（规格 §6；`found:false` 是**正常结果**）。

        响应字段与 `article.ArticleIndex.locate()` 逐字相同，hub 只做取数与错误映射。
        索引按 doc_id 缓存（正文与段偏移不可变，无需失效逻辑），所以每次按键
        端的开销是「一次 SQLite 取行 + 一次定位」，落在规格的 5ms 预算内。
        """
        doc_id = _request_int(request.get("doc_id"))
        target = request.get("target")
        if doc_id is None or not isinstance(target, str):
            return {"ok": False, "error": "bad request"}
        document = self.store.document(doc_id)
        if document is None:
            return {"ok": False, "error": "not found"}
        current_passage = _request_int(request.get("current_passage"))
        try:
            located = self._article_index(document).locate(target, current_passage)
        except ValueError:
            # 落库时已校验；走到这里说明库里的段落偏移是脏数据（人工改库/坏迁移），
            # 按契约回错误码，别把 internal 抛给外壳。
            log.warning("文章段落偏移不可用 doc_id=%s", doc_id)
            return {"ok": False, "error": "bad passages"}
        return {"ok": True, **located}

    # --- v22 P2a（票 #167 / spec §6.4 §6.6）：预览窗数据 / 重新分段 / 删文章 -------

    def article_paragraphs(self, request: dict[str, Any]) -> dict[str, Any]:
        """面板分段预览窗的数据（spec §6.4）：`{ok, doc_id, paragraphs, stats}`。

        与 `GET /api/articles/{doc_id}` 的**同一份**派生逻辑（`_article_detail`），
        这里只裁出段落列表与统计，不另算一套；段落对象字段与 HTTP 详情逐字段相同
        （含 `suspicious` / `records`，前端按需取用）。

        **additive（P2b review）**：回包多一个 `doc_id` —— 预览窗用它做迟到/换篇回包
        归属校验（P2a 原契约没有它，旧客户端忽略该字段即可）。`stats.records_total`
        由 `_article_detail` 一并带出（= 已定位 + 待复核，spec §9 的 M 词口径）。
        """
        doc_id = _request_int(request.get("doc_id"))
        if doc_id is None:
            return {"ok": False, "error": "bad request"}
        document = self.store.document(doc_id)
        if document is None:
            return {"ok": False, "error": "not found"}
        # 预览窗不展示待复核区（那是取词台文章页的事）→ 关掉 needs_review 的输出派生，
        # 不白算一份然后丢掉（review 修复轮）。
        detail = self._article_detail(document, include_needs_review=False)
        return {
            "ok": True,
            "doc_id": detail["id"],
            "paragraphs": detail["paragraphs"],
            "stats": detail["stats"],
        }

    def article_resegment(self, request: dict[str, Any]) -> dict[str, Any]:
        """用创建时的原始分段输入重跑分段（spec §6.4 §6.6 §6.7）。

        `documents.segmentation_source` 是**唯一**输入：manual → `segment_pasted(raw)`；
        ocr → `decode_ocr_lines` → `segment_ocr`。重算 `passage_breaks`、按需同步
        `documents.text`、按 §6.6 重映射词条、失效定位索引缓存。

        - source 为 NULL/空（旧 v21 文章）→ `{ok:false, error:"no segmentation source"}`，
          **不伪造分段**；
        - source 存在但解不出来 / 分段为空 → `{ok:false, error:"bad segmentation source"}`；
        - 回 `{ok, doc_id, segments, remapped, needs_review}`（计数口径见 `_remap_records`）。
        """
        started = time.perf_counter()
        doc_id = _request_int(request.get("doc_id"))
        if doc_id is None:
            return {"ok": False, "error": "bad request"}
        document = self.store.document(doc_id)
        if document is None:
            return {"ok": False, "error": "not found"}
        source = self.store.segmentation_source(doc_id)
        if not source:
            return {"ok": False, "error": "no segmentation source"}
        passages = self._segment_source(document["format"], source)
        if passages is None:
            return {"ok": False, "error": "bad segmentation source"}
        new_text = join_passages(passages)
        if not new_text.strip():
            return {"ok": False, "error": "bad segmentation source"}
        spans = [[p.start, p.end] for p in passages]
        # 重映射 + 正文/段偏移写回是**同一个事务**（§6.6 要求词条跟着新分段走；
        # 中途失败不能留下「新正文 + 旧段号」，见 `Store.apply_resegment`）。
        rewritten = self._rewrite_article(document, new_text, spans)
        if rewritten is None:  # 文章在写回前被并发删除
            return {"ok": False, "error": "not found"}
        remapped, needs_review = rewritten
        log.info(
            "文章重新分段 doc_id=%s（%d 段 / 重映射 %d 条 / 待复核 %d 条，%dms）",
            doc_id,
            len(spans),
            remapped,
            needs_review,
            round_ms(started),
        )
        return {
            "ok": True,
            "doc_id": doc_id,
            "segments": len(spans),
            "remapped": remapped,
            "needs_review": needs_review,
        }

    def article_delete(self, request: dict[str, Any]) -> dict[str, Any]:
        """删一篇文章，**词条一条不丢**（spec §6.4 §12 / 决策 D16）。

        词条的 `doc_id` / `passage_index` 解绑成 NULL（回到原会话的句卡），`due_at`
        不动 → 复习调度不受影响；文档行删除后失效该文的定位索引缓存。

        `keep_records:false`（连带删词条）**不暴露**：删除时词条保留是用户确认的产品
        口径，真要连带删得另开票加二次确认。显式传 `false`（或任何非布尔）→
        `bad request`，**不静默兜底**。回 `{ok, doc_id, unlinked}`（unlinked=解绑条数）。
        """
        doc_id = _request_int(request.get("doc_id"))
        if doc_id is None:
            return {"ok": False, "error": "bad request"}
        keep = request.get("keep_records", True)
        if not isinstance(keep, bool) or not keep:
            return {"ok": False, "error": "bad request"}
        deleted = self.store.delete_document(doc_id)
        if deleted is None:
            return {"ok": False, "error": "not found"}
        self._invalidate_article_index(doc_id)
        log.info("文章删除 doc_id=%s（解绑词条 %d 条）", doc_id, deleted["records"])
        return {
            "ok": True,
            "doc_id": doc_id,
            "unlinked": int(deleted["records"]),
        }

    @staticmethod
    def _segment_source(format: Any, source: str) -> list[Passage] | None:
        """按 `documents.format` 用**同一套**分段算法重跑原始输入（spec §6.7）。

        manual → `longtext.segment_pasted(raw)`；ocr → 行框解码成功才
        `longtext.segment_ocr(lines)`。解码失败 / 格式不认识 / 分段为空 → None
        （调用方回 `bad segmentation source`，不伪造）。
        """
        fmt = "" if format is None else str(format).strip().lower()
        if fmt == MANUAL_FORMAT:
            passages = segment_pasted(source).passages
        elif fmt == OCR_FORMAT:
            lines = decode_ocr_lines(source)
            if lines is None:
                return None
            passages = segment_ocr(lines).passages
        else:
            return None
        return passages or None

    def _remap_records(
        self, document: dict[str, Any], text: str, spans: list[list[int]]
    ) -> tuple[dict[int, int], int, int]:
        """按 spec §6.6 把一篇旧文章的词条重映射到**新**段落集合。

        只处理 `passage_index >= 0` 的记录（`-1` 已是待复核，不重复处理）：
        1. 在新段落里找**包含** `record.context`（`one_line` 归一、大小写不敏感）的段；
        2. 唯一命中 → 用新段号；多段命中（重复句）→ 取绝对偏移最接近旧段起点的一段，
           仍并列 → `-1`；找不到 → `-1`（`doc_id` 保留，词条一条不丢）。

        返回 `(assignments, remapped, needs_review)`：
        - `assignments` = `{record_id: 新段号}`（含 `-1`，只含被处理的记录）；
        - `remapped` = 处理后仍定位在段落上的条数（含段号未变的）；
        - `needs_review` = 处理后该文 `passage_index` 不是合法段的条数（`-1`/NULL 都算），
          与文章详情的 `needs_review` 同口径；`remapped + needs_review == 词条总数`。
        """
        doc_id = int(document["id"])
        old_spans = document["passages"]
        new_starts = [int(item[0]) for item in spans]
        normalized = [
            one_line(text[int(start) : int(end)]).casefold()
            for start, end in spans
        ]
        assignments: dict[int, int] = {}
        remapped = 0
        needs_review = 0
        for record in self.store.article_records(doc_id):
            index = record.get("passage_index")
            if not isinstance(index, int) or isinstance(index, bool) or index < 0:
                needs_review += 1
                continue
            context = one_line(record.get("context")).casefold()
            old_start = (
                int(old_spans[index][0])
                if 0 <= index < len(old_spans)
                else 0
            )
            hits = [
                position
                for position, body in enumerate(normalized)
                if context and context in body
            ]
            if len(hits) == 1:
                target = hits[0]
            elif len(hits) > 1:
                ranked = sorted(
                    (abs(new_starts[position] - old_start), position)
                    for position in hits
                )
                target = ranked[0][1] if ranked[0][0] != ranked[1][0] else -1
            else:
                target = -1
            assignments[int(record["id"])] = target
            if target >= 0:
                remapped += 1
            else:
                needs_review += 1
        return assignments, remapped, needs_review

    def _invalidate_article_index(self, doc_id: int) -> None:
        """丢掉一篇文的定位索引缓存（spec §6.4「必须失效」，v22 P2a 的核心回归点）。

        v21 的缓存假设「`documents` 行不可变」；P2 的重分段 / 去重更新会原地改正文，
        不 `pop` 就会继续用旧正文定位（段号 / 偏移全错）。
        """
        with self._article_lock:
            self._article_indexes.pop(int(doc_id), None)

    def _article_index(self, document: dict[str, Any]) -> ArticleIndex:
        """doc_id → `ArticleIndex`（最近 `ARTICLE_INDEX_CACHE_MAX` 篇常驻内存）。

        缓存按 doc_id 常驻；`documents` 行内容被重分段 / 合并更新原地改过时由写路径
        调用 `_invalidate_article_index()`（spec §6.4）。

        **取缓存 / 建索引 / 写回全在同一把锁内**（review 修复轮）：RPC 与 HTTP 都是
        Threading server，旧实现「锁外建索引、锁内写回」会在 locate 与 resegment 并发时
        把基于旧正文的索引写回缓存；而且缓存未命中时在锁内**重读** documents 行，
        保证建索引吃的是已提交的正文。构建 41KB ≈ 几十 ms、只阻塞索引读写，可接受。
        """
        doc_id = int(document["id"])
        with self._article_lock:
            index = self._article_indexes.get(doc_id)
            if index is not None:
                return index
            fresh = self.store.document(doc_id) or document
            index = ArticleIndex(str(fresh["text"]), fresh["passages"])
            if len(self._article_indexes) >= ARTICLE_INDEX_CACHE_MAX:
                oldest = next(iter(self._article_indexes), None)
                if oldest is not None:
                    self._article_indexes.pop(oldest, None)
            self._article_indexes[doc_id] = index
            return index

    def ping(self) -> dict[str, Any]:
        started = time.perf_counter()
        counts = self.store.counts()
        payload = {
            "ok": True,
            "pong": round_ms(started),
            "schema": SCHEMA_VERSION,
            "records": counts["records"],
            "sessions": counts["sessions"],
            "pending": counts["pending"],
            "dict_online": self.dict_service.online_enabled,
        }
        # v34 T5（票 #218 / spec §C22）：外壳每秒的 ping 顺带下接收句状态。
        payload.update(self.capture_ping_fields())
        return payload

    def capture_ping_fields(self) -> dict[str, Any]:
        """外壳通道的**扁平**前缀字段（禁止嵌套对象，spec §C22）。

        外壳只有三只扁平正则（`JsonField`/`JsonNumber`/`JsonBool`，
        `英语取词.ahk` 协议节），没有 JSON 解析器 —— 嵌套对象会因正则不分层而
        「碰巧」匹配到内层键，外层一旦撞名（`ocr`/`armed`）就静默取错值。
        所以这里只回标量：

        - `cap_armed`(bool)：手动扳机（未按开始/总开关关 = false）；
        - `cap_ocr`(string)：`status_of` 的 ocr 枚举（off/active/idle/dormant/
          redrag_needed/failed）；
        - `cap_session`(number)：当前活动段会话 id，无段 = 0；
        - `cap_age_ms`(number)：距最后一次浏览器心跳的毫秒数，**从未收到 = -1**
          （外壳据此判「心跳中断」；0 会被误读成「刚刚还有心跳」）；
        - `cap_phase`(string)：11 档相位（#220 状态浮窗；与 /api/capture/status 同源）；
        - `cap_added`(number)：本段已落库句数（#220）；
        - `cap_last`(string)：本段最近一次落库时间，`YYYY-MM-DD HH:MM:SS`，无 = ""（#220）；
        - `cap_scene`(string)：v36 场景识别，`"video_invite"` = B站视频页正在播放且未
          armed（桌宠可主动邀请），其余 = ""；
        - `cap_video`(number)：0/1 —— 上下文是否为 B站视频页且正在播放（同上，扁平
          整数，外壳正则直接解）；
        - `cap_cc`(string)：v36 #246「这条是怎么记的」—— `ok`（英文 CC 轨已整轨落库，
          直读）/ `no_english_track`（无英文轨，只能屏幕采样）/ `error`（查轨接口
          故障，降级采样）/ `off`（还没有段 / 未判明）。与 `cap_phase` **并行**、
          互不覆盖（spec 决策 6：来源不进相位），取值即中枢的来源枚举。
        - `cap_ended`(number)：0/1 —— 上面这一槽视频**播到末尾停住了**（2026-11-10
          追加）。外壳据此把收句自动停掉：播完了再收也不会有新句，挂着只会让用户
          以为还在记。判定见 `video_ended` / `ended_of`（paused + 到末尾容差内，
          中途手动暂停不算）。
        - `cap_no_source`(number)：0/1 —— 10 秒可读行窗口判出「既无英文 CC、画面
          也没有烧录字幕」（2026-11-10 追加；2026-11-11 起判据是 `FeedReport.lines>0`，
          不再用墨量签名）。ASR 二期接入后的保底：外壳据此明说一句并自动停收句，不再
          反复问用户拖字幕带。判定见 `CaptureScheduler.no_source_active()`；ASR 转写
          已排队时 `source_available` 置真，本字段保持 0，保底自动让位。
        """
        context, age_ms = self.browser_context()
        heartbeat_age_ms = age_ms if context is not None else None
        try:
            scene = self.scene_of()
        except Exception as exc:  # noqa: BLE001 - 场景算不出来不许影响 ping
            log.warning("场景识别取用失败（%s），ping 回落空场景", type(exc).__name__)
            scene = {"scene": "", "video": False, "playing": False}
        try:
            ended = 1 if self.video_ended() else 0
        except Exception as exc:  # noqa: BLE001 - 判不出来按没播完
            log.warning("播完判定取用失败（%s），ping 回落未播完", type(exc).__name__)
            ended = 0
        # 2026-11-11 ASR P2：无字源窗口到期 → 先排队转写（成功则 `cap_no_source`
        # 保持 0，收句不停）；失败/未启用 → 继续走 10 秒保底。
        try:
            self.scheduler.try_asr_for_no_source()
        except Exception as exc:  # noqa: BLE001 - 排队坏掉不许影响 ping
            log.warning("ASR 无字源排队失败（%s）", type(exc).__name__)
        try:
            no_src = 1 if self.scheduler.no_source_active() else 0
        except Exception as exc:  # noqa: BLE001 - 保底判定坏掉不许影响 ping
            log.warning("无字源判定取用失败（%s），ping 回落 0", type(exc).__name__)
            no_src = 0
        try:
            status = self.scheduler.status(heartbeat_age_ms=heartbeat_age_ms)
            armed = bool(status.get("armed"))
            ocr = str(status.get("ocr") or "off")
            session = int(status.get("session_id") or 0)
            phase = str(status.get("phase") or "unarmed")
            added = int(status.get("added_in_segment") or 0)
            last = str(status.get("last_added_at") or "")
            cc = str(status.get("cc") or "off")
            if cc not in CAPTURE_CC_VALUES:
                # 外壳只有扁平等值比较：未知值归 `off`，不把未来枚举漏成 miss。
                cc = "off"
            # 2026-11-11：当前字幕带矩形（"x,y,w,h"）与「是否自动复用」。
            # 外壳据此在开始采样时画 1.2–2.5s 红框预览；空串 = 还没有区域。
            band_rect = str(status.get("band_rect") or "")
            band_auto = 1 if status.get("band_auto") else 0
        except Exception as exc:  # noqa: BLE001 - 状态算不出来不许影响 ping
            log.warning("收句状态取用失败（%s），ping 回落未开启", type(exc).__name__)
            armed, ocr, session, phase, added, last = False, "off", 0, "unarmed", 0, ""
            cc = "off"
            band_rect, band_auto = "", 0
        asr_state = self._asr_snapshot()
        return {
            "cap_armed": armed,
            "cap_ocr": ocr,
            "cap_session": session,
            "cap_age_ms": -1 if heartbeat_age_ms is None else int(heartbeat_age_ms),
            "cap_phase": phase,
            "cap_added": added,
            "cap_last": last,
            "cap_scene": scene["scene"],
            "cap_cc": cc,
            "cap_ended": ended,
            "cap_no_source": no_src,
            "cap_band_rect": band_rect,
            "cap_band_auto": band_auto,
            "cap_asr": str(asr_state.get("state") or "off"),
            "cap_asr_session": int(asr_state.get("session") or 0),
            "cap_asr_added": int(asr_state.get("added") or 0),
            "cap_asr_reason": str(asr_state.get("reason") or ""),
            "cap_video": 1 if scene["video"] else 0,
        }

    def lookup(
        self,
        word: str,
        *,
        fast: bool = False,
        full: bool = False,
        dict_only: bool = False,
    ) -> dict[str, Any]:
        """§7 统一结构 + `seen`（以前记过几次、上次在哪个会话）。

        W4 两段式回填（#149，方案 B'）：`fast=true` 的首查本地源命中会带
        `provisional`，外壳据此补发一次 `full=true` 的二跳；hub 保持纯请求-响应
        （一请求一响应，N7）。不带两参的调用（HTTP /api/lookup、web 前端）行为不变。

        v22 P5（#169 / spec §11）：`dict_only=true` 是多词目标的「整条短语词典查询」
        （词典源白名单 + 旁路缓存）；外壳只在目标含空格时带这个标记，默认路径零改动。

        v22 修复（#172）：词典未命中时回退机翻（dict_sources 的 MT 引擎），回包多一个
        `machine:true` 表示译文是机器翻译（外壳/取词台据此标注「机翻」）；本方法只透传，
        `seen` 等既有字段不变。
        """
        result = self.dict_service.lookup(
            word, fast=fast, full=full, dict_only=dict_only
        )
        seen = self.store.seen(word or "")
        if seen:
            result["seen"] = seen
        return result

    # --- 浏览器上下文（v3 §2；v5 §2 起两份快照 + 本机探针；内存，不落库）---------

    def _ctx_rate_allow(self) -> bool:
        """心跳限流：每 `CTX_RATE_WINDOW_S` 秒最多处理 `CTX_RATE_MAX` 条。

        为什么需要：扩展的 alarms 兜底心跳是「把所有标签页叫一遍」，每个标签页回一条
        —— 开几十个标签时 hub 在几十毫秒内收到几十条（实测 40ms / 30 条）。这些心跳
        全是别的标签页的 `video=no`，对状态机没有价值，却会让调度器每拍都跑一遍
        状态判定，并把日志刷到无法排障。

        真实心跳间隔 2 秒，1 秒 8 条是 16 倍余量 —— 正常使用永远不会触发限流。
        被限流掉的只在「风暴」里，而正在看的那一页 2 秒内一定会再发一条，不会漏状态。
        """
        now = time.monotonic()
        if now - self._ctx_win_start >= CTX_RATE_WINDOW_S:
            if self._ctx_dropped:
                log.info(
                    "浏览器上下文：上一窗口丢弃 %d 条（多标签兜底心跳风暴，已限流）",
                    self._ctx_dropped,
                )
                self._ctx_dropped = 0
            self._ctx_win_start = now
            self._ctx_win_count = 0
        if self._ctx_win_count >= CTX_RATE_MAX:
            self._ctx_dropped += 1
            return False
        self._ctx_win_count += 1
        return True

    def set_browser_context(
        self, payload: dict[str, Any], *, app_hint: str = ""
    ) -> None:
        """收下扩展上报的最新一条上下文（§2.2）。`received_at` 用 hub 自己的时钟。

        v5 §2.3：`focused is True` 的那份**额外**存进 `_browser_last_focused`，供失焦后按
        5 分钟宽限回退采纳。日志纪律（§2.7）：URL / 页面标题 / body 一律不进日志。

        v33 T5（票 #211）：`app_hint` 是这条心跳的浏览器友好名（从 User-Agent 映射，
        `capture_scheduler.browser_app_from_ua`）—— 调度器建会话要用它与外壳取词
        撞在同一个 `session_key` 上（FriendlyApp + CleanTitle）。
        """
        now = time.perf_counter()
        with self._browser_lock:
            self._browser_context = payload
            self._browser_received_at = now
            if payload.get("focused") is True:
                self._browser_last_focused = payload
                self._browser_last_focused_at = now
            self._remember_slot(payload)
        # v36（#240）：接收点日志 —— 时间（日志行自带）、聚焦 / 有视频 / 在播，
        # 外加扩展版本握手（浏览器侧 vs 仓库侧）。只记动作与判定，URL / 标题 /
        # 正文一律不进日志（§2.7 隐私纪律）。
        ext_version = str(payload.get("extVersion") or "").strip()
        repo_version = extension_version()
        # 原始观测（与 B 站判定无关）：页面上有没有 <video>、它是不是在播。
        has_video = bool(payload.get("hasVideo"))
        playing = capture_scheduler.playing_of(payload)
        # 2026-11-10 日志降噪：多标签兜底心跳会让同一观测每 30 秒刷几十行。只有观测
        # （页面身份 / 聚焦 / 有视频 / 在播 / 扩展版本）**变化**时才写 INFO，重复的降到
        # DEBUG；再补一条每分钟的「存活」行 —— 排障时要能确认「心跳还活着」。
        observed = (
            capture_scheduler.context_slot_key(str(payload.get("href") or "")),
            payload.get("focused") is True,
            has_video,
            playing,
            ext_version,
        )
        now_mono = time.monotonic()
        self._ctx_alive_count += 1
        changed = observed != self._ctx_last_observ
        alive_due = (now_mono - self._ctx_alive_at) >= CTX_ALIVE_LOG_S
        self._ctx_last_observ = observed
        if changed or alive_due:
            suffix = ""
            if not changed:
                suffix = f"（存活：近 {int(CTX_ALIVE_LOG_S)}s {self._ctx_alive_count} 条，观测未变）"
            log.info(
                "浏览器上下文：focused=%s video=%s playing=%s 扩展版本=%s（仓库 %s）%s",
                "yes" if payload.get("focused") is True else "no",
                "yes" if has_video else "no",
                "yes" if playing else "no",
                ext_version or "未上报",
                repo_version or "读不到",
                suffix,
            )
            self._ctx_alive_at = now_mono
            self._ctx_alive_count = 0
        else:
            log.debug(
                "浏览器上下文：focused=%s video=%s playing=%s（观测未变，降噪）",
                "yes" if payload.get("focused") is True else "no",
                "yes" if has_video else "no",
                "yes" if playing else "no",
            )
        version_changed = ext_version != self._ext_version
        self._ext_version = ext_version
        if version_changed and ext_version and repo_version and ext_version != repo_version:
            # 只在版本变化时提示一次：这是「改过扩展没重载」最直接的证据。
            log.warning(
                "扩展版本 %s ≠ 仓库 %s：浏览器侧跑的不是当前代码，需在扩展管理页重载",
                ext_version,
                repo_version,
            )
        # v33 T5（票 #211）：心跳 = 调度 tick（spec §C10 触发门）。先存上下文再调度，
        # 调度器内部自吞异常 —— 采集编排坏掉不许影响心跳响应与既有取词链路。
        self._scheduler_tick(payload, app_hint=app_hint)

    def _scheduler_tick(self, payload: dict[str, Any], *, app_hint: str = "") -> None:
        """把一条心跳交给采集调度器（吞异常；动作账目只进 debug 日志）。"""
        try:
            actions = self.scheduler.on_heartbeat(payload, app_hint=app_hint)
        except Exception as exc:  # noqa: BLE001 - 双保险：调度器自己也会兜
            log.warning("采集调度 tick 异常（%s）", type(exc).__name__)
            return
        if actions:
            log.debug("采集调度动作：%s", ",".join(actions))

    def _timed_context(
        self, context: dict[str, Any] | None, received_at: float | None
    ) -> tuple[dict[str, Any] | None, int]:
        if context is None or received_at is None:
            return None, 0
        return context, max(0, round((time.perf_counter() - received_at) * 1000))

    def browser_context(self) -> tuple[dict[str, Any] | None, int]:
        """`(最新上下文, 距今毫秒)`；没有上下文时 `(None, 0)`（§2.3）。"""
        with self._browser_lock:
            context, received_at = self._browser_context, self._browser_received_at
        return self._timed_context(context, received_at)

    def browser_last_focused(self) -> tuple[dict[str, Any] | None, int]:
        """`(最后一次聚焦的上下文, 距今毫秒)`；没有时 `(None, 0)`（v5 §2.3）。"""
        with self._browser_lock:
            context = self._browser_last_focused
            received_at = self._browser_last_focused_at
        return self._timed_context(context, received_at)

    def _remember_slot(self, payload: dict[str, Any]) -> None:
        """把这条心跳记进多槽（v36 决策 2）。**调用方持 `_browser_lock`**。

        槽身份 = `capture_scheduler.context_slot_key(href)`（href 归一化），同一页面
        再次上报就顶掉自己（刷新而不是堆叠）。上限 `SCENE_SLOT_MAX`：超了按「最久
        没说话」淘汰 —— 场景只看 `SCENE_FRESH_MS` 内的槽，扔掉的都是早就过期的。

        时间戳走实例时钟 `self._clock`（不是 `browser_context()` 那条路径的
        `time.perf_counter`）：脱机测试能推进时间验证「场景比归属活得久」（决策 3），
        生产里两者是同一个时钟。
        """
        key = capture_scheduler.context_slot_key(payload.get("href"))
        self._browser_slots.pop(key, None)  # dict 保序：重新插入即「最近」
        self._browser_slots[key] = {
            "context": payload,
            "received_at": self._clock(),
            "focused": payload.get("focused") is True,
        }
        while len(self._browser_slots) > capture_scheduler.SCENE_SLOT_MAX:
            self._browser_slots.pop(next(iter(self._browser_slots)))

    def browser_slots(self) -> list[dict[str, Any]]:
        """场景选槽用的槽快照，**最近说话的在前**（v36 决策 2）。

        每项 `{key, context, age_ms, focused}` —— `focused` 只是随槽带出的字段
        （「是否聚焦」不再是发送开关），选槽**不看它**（见 `pick_scene_context`）。
        """
        now = self._clock()
        with self._browser_lock:
            items = list(self._browser_slots.items())
        return [
            {
                "key": key,
                "context": slot["context"],
                "age_ms": max(0, round((now - slot["received_at"]) * 1000)),
                "focused": bool(slot["focused"]),
            }
            for key, slot in reversed(items)
        ]

    def adopt_browser(self, app: Any, title: Any, hwnd: Any = None) -> dict[str, Any] | None:
        """v5 §2.1：扩展优先（聚焦走 TTL，否则回退最后聚焦那份走宽限），探针兜底。

        都拿不到 → `None`（宁可不给链接，也不给错链接）。`hwnd` 为 0/缺省 = 不跑探针。
        """
        context, age_ms = self.browser_context()
        if context is not None and context.get("focused") is True:
            adopted = adopt_context(context, age_ms, app, title)
        else:
            last, last_age = self.browser_last_focused()
            adopted = (
                adopt_context(
                    last, last_age, app, title, ttl_ms=BROWSER_BLUR_GRACE_MS
                )
                if last is not None
                else None
            )
        if adopted is None and self.probe_url:
            handle = _int_or_none(hwnd)
            if handle:
                adopted = self._probe(handle, app, title)
        if adopted is not None:
            self._note_capture(adopted)
        return adopted

    def _probe(self, hwnd: int, app: Any, title: Any) -> dict[str, Any] | None:
        """本机探针（v5 §2.2），按 `(hwnd, title)` 缓存 ~5s；只记成败与耗时，不记 URL。"""
        key = (int(hwnd), str(title or ""))
        now = time.perf_counter()
        with self._probe_lock:
            if self._probe_key == key and now - self._probe_at <= PROBE_CACHE_S:
                return self._probe_result
        started = time.perf_counter()
        try:
            result = browsercontext.probe_url(hwnd, app, title, PROBE_TIMEOUT_MS)
        except Exception as exc:  # noqa: BLE001 - 探针坏掉不许影响取词
            log.info("本机探针失败：%s", type(exc).__name__)
            result = None
        elapsed = round_ms(started)
        with self._probe_lock:
            self._probe_key = key
            self._probe_result = result
            self._probe_at = time.perf_counter()
            self._probe_last_ms = elapsed
            self._probe_last_ok = result is not None
        return result

    def _note_capture(self, browser: dict[str, Any]) -> None:
        """记「最近一次**采纳到 browser** 的那次 peek」（v5 §7）：只放旗标与时间，
        **不放 URL / 标题**。与媒体会话无关 —— 媒体块另有口径（见 `browser_status`）。
        """
        self._last_capture = {
            "source": str(browser.get("source") or ""),
            "at": format_ts(datetime.now()),
            "has_url": bool(str(browser.get("url") or "")),
            "has_position": browser.get("seconds") is not None,
        }

    def browser_status(self) -> dict[str, Any]:
        """`GET /api/settings` 的 `browser` 块（v5 §7）：只放旗标与站点短名，无用户内容。"""
        context, age_ms = self.browser_context()
        connected = context is not None and age_ms <= BROWSER_CONTEXT_TTL_MS
        site = ""
        has_video = False
        if connected:
            site = site_of(context.get("href"))
            has_video = bool(context.get("hasVideo"))
        with self._media_lock:
            paused_by_us = self._paused_by_us
            paused_app, paused_title = self._media_app, self._media_title
        # §3：`media_pause=0` 时连 SMTC 都不读（设置页不必白等最多 800ms），只报依赖可用性
        if self.media_pause:
            # §7 显示口径：压着暂停（暂停时记下了 app/title）→ 「按门挑」那个会话；多个
            # MSEdge 并存时 `CurrentSession` 可能指向另一仍在播的会话，卡片会说错。没有
            # 记录 → 维持现状（无参 = `CurrentSession`）。
            if paused_app and paused_title:
                snapshot = browsercontext.media_snapshot(app=paused_app, title=paused_title)
            else:
                snapshot = browsercontext.media_snapshot()
            media_available = bool(snapshot.get("available"))
            media_app = str(snapshot.get("app") or "")
            media_status = str(snapshot.get("status") or "")
        else:
            media_available = browsercontext.media_available()
            media_app = ""
            media_status = "disabled"
        return {
            "ext": {
                "connected": connected,
                "age_ms": age_ms,
                "site": site,
                "has_video": has_video,
            },
            "probe": {
                "enabled": bool(self.probe_url),
                "ready": browsercontext.probe_available(),
                "last_ok": self._probe_last_ok,
                "last_ms": self._probe_last_ms,
            },
            "media": {
                "enabled": bool(self.media_pause),
                "available": media_available,
                "app": media_app,
                "status": media_status,
                "paused_by_us": paused_by_us,
            },
            "last_capture": self._last_capture
            or {"source": "", "at": "", "has_url": False, "has_position": False},
        }

    # --- 媒体联动（v5 §3；SMTC，不发按键）----------------------------------

    def media(self, op: str, app: Any, title: Any) -> dict[str, Any]:
        """`{"cmd":"media","op":"pause"|"resume",…}`（v5 §3）：hub 持有 `paused_by_us`。

        pause 的「结果未知」（超时/异常且依赖在位）→ 标志**悲观置位**并落状态文件：resume
        侧有 `status=="paused"` 这道门，多一次被门拦下的 no-op，不会误播。resume 失败/超时
        → 保留标志与文件，留给下一次机会。

        v5 §2.2/§3 修订（2026-10-23）：记/续的是**真被暂停的那只会话**（暂停回包里的
        `session_app`/`session_title`），不是外壳送来的窗口标题 —— B站分P 视频的媒体会话
        标题是整部视频名，拿窗口标题去续播会挑不到会话、视频就永远停在那儿。
        """
        if not self.media_pause:
            # 开关关掉 = 完全不做媒体动作（§3 行为细则）
            return {"ok": True, "paused_by_us": False, "status": "disabled"}
        if op == "pause":
            result = browsercontext.media_pause(app, title)
            unknown = not result.get("ok") and browsercontext.media_available()
            if result.get("paused_by_us") or unknown:
                # 结果未知时拿不到会话身份 → 退回请求里的 app/title（原口径）
                paused_app = str(result.get("session_app") or app or "")
                paused_title = str(result.get("session_title") or title or "")
                with self._media_lock:
                    self._paused_by_us = True
                    self._media_app = paused_app
                    self._media_title = paused_title
                self._write_pause_file(paused_app, paused_title)
            return result
        if op == "resume":
            with self._media_lock:
                flag = self._paused_by_us
                paused_app, paused_title = self._media_app, self._media_title
            # 我们暂停过 → 续播同一只（真实会话身份优先）；否则沿用请求里的 app/title
            target_app, target_title = (
                (paused_app, paused_title) if flag and paused_title else (app, title)
            )
            result = browsercontext.media_resume(
                target_app, target_title, paused_by_us=flag
            )
            if result.get("ok"):
                with self._media_lock:
                    self._paused_by_us = False
                self._clear_pause_file()
            return result
        return {"ok": False, "error": "unknown op"}

    def shutdown_media(self) -> None:
        """hub 退出时若还压着一次暂停 → 先续播再退（§3；best effort，不留暂停残局）。

        续播成功 → 清标志 + 删文件；失败/超时 → **保留**文件（下次启动 5 分钟内还能采纳）。
        """
        with self._media_lock:
            flag = self._paused_by_us
            app, title = self._media_app, self._media_title
        if not flag:
            return
        try:
            result = browsercontext.media_resume(app, title, paused_by_us=True)
        except Exception as exc:  # noqa: BLE001 - 退出路径不许再抛
            log.warning("退出时续播失败：%s", type(exc).__name__)
            return
        if result.get("ok"):
            with self._media_lock:
                self._paused_by_us = False
            self._clear_pause_file()
            log.info("退出时补了一次续播（媒体联动）")

    def shutdown(self) -> None:
        """v23 §5.2：退出时停补释义 worker（置停止位 + join ≤2s，不阻塞退出）。

        在途查询最长 2.5s（沿用 DictService 预算）：join 超时就留给 daemon 线程
        在进程退出时自灭 —— 绝不为了补释义把 hub 卡在退出路径上。

        v33 T5（票 #211）：退出前把活动视频段的采样 flush 干净（投票组不落库就丢
        等于白采一段），失败只记日志不抛。
        """
        self.meaning_fill.stop()
        # 2026-11-11 ASR P2：停转写 worker（daemon；join ≤2s，不阻塞退出）。
        if self.asr_queue is not None:
            try:
                self.asr_queue.stop()
            except Exception as exc:  # noqa: BLE001 - 退出路径不许再抛
                log.warning("ASR 队列退出失败（%s）", type(exc).__name__)
            self.asr_queue = None
        try:
            self.scheduler.shutdown()
        except Exception as exc:  # noqa: BLE001 - 退出路径不许再抛
            log.warning("采集调度退出失败（%s）", type(exc).__name__)
        # v33 P0（票 #211 首发崩溃修复）：终止 band OCR worker 子进程（幂等；
        # 测试注入的假件多半没有 close，hasattr 守一下）。
        band_ocr_engine = self._band_ocr
        if band_ocr_engine is not None and hasattr(band_ocr_engine, "close"):
            try:
                band_ocr_engine.close()
            except Exception as exc:  # noqa: BLE001 - 退出路径不许再抛
                log.warning("band ocr worker 终止失败（%s）", type(exc).__name__)

    # --- 暂停状态文件（v5 §3：跨 hub 重启）--------------------------------

    def _read_pause_file(self) -> dict[str, Any] | None:
        try:
            raw = self.media_pause_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def _write_pause_file(self, app: Any, title: Any) -> None:
        """落 `{app,title,at}`（**不存 URL/原句**）；原子写，失败只记一行。"""
        payload = {"app": str(app or ""), "title": str(title or ""), "at": time.time()}
        try:
            self.media_pause_path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.media_pause_path.with_name(self.media_pause_path.name + ".tmp")
            temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(temp, self.media_pause_path)
        except OSError as exc:
            log.warning("暂停状态落盘失败：%s", type(exc).__name__)

    def _clear_pause_file(self) -> None:
        try:
            self.media_pause_path.unlink(missing_ok=True)
        except OSError:
            pass

    def _adopt_pending_pause(self) -> None:
        """启动时：文件在 && `at` ≤5 分钟 && `media_pause=1` → 采纳；过期/损坏 → 删掉不采纳（§3）。"""
        if not self.media_pause_path.exists():
            return
        data = self._read_pause_file()
        if data is None:
            self._clear_pause_file()
            log.info("丢弃损坏的暂停状态")
            return
        if not self.media_pause:
            self._clear_pause_file()
            return
        age = _age_seconds(data.get("at"))
        if age is None or age < 0 or age > MEDIA_PAUSE_TTL_S:
            self._clear_pause_file()
            log.info("丢弃过期的暂停状态")
            return
        with self._media_lock:
            self._paused_by_us = True
            self._media_app = str(data.get("app") or "")
            self._media_title = str(data.get("title") or "")
        log.info("采纳跨重启的暂停状态（%.0fs 前）", age)

    # --- HTTP 路由（47811）-------------------------------------------------

    def api(
        self,
        method: str,
        path: str,
        query: dict[str, list[str]],
        body: Any,
        *,
        user_agent: str = "",
    ) -> Response:
        parts = [part for part in path.split("/") if part]
        tail = parts[1:] if parts and parts[0] == "api" else parts
        if not tail:
            return error_response(404, "not found")
        head, rest = tail[0], tail[1:]
        if head == "health" and not rest and method == "GET":
            return self._api_health()
        # v34 T5（票 #218 / spec §C21）：收句全局相位（无参数、只读、与 health 同级）。
        if head == "capture" and rest == ["status"] and method == "GET":
            return self._api_capture_status()
        if head == "summary" and not rest and method == "GET":
            return self._api_summary()
        if head == "stats" and not rest and method == "GET":
            return self._api_stats(query)
        if head == "sessions":
            if not rest and method == "GET":
                # 侧栏默认只要「还有词条」的会话：删光词条的会话不再挂在那里
                include_empty = first_param(query, "include_empty") in ("1", "true")
                sessions = self.store.list_sessions(include_empty=include_empty)
                # v6 §Implementation·一期（票 #81）：出处的派生字段读时现算。前端只消费
                # 这些字段 —— 站点映射表从此只有中枢这一份。
                for session in sessions:
                    # 票 #96 A1：站点后缀判定要用**未被 `clean_title` 截断**的原始标题 ——
                    # 会话行的 title 已截断（>120 字的标题后缀不可恢复），词条最早的
                    # `source_title` 才是原件。取走后 pop 掉，不进 API 出参。
                    raw_title = session.pop("raw_source_title", "") or session.get("title")
                    session.update(
                        session_provenance(
                            session.get("url"),
                            raw_title,
                            session.get("source_app"),
                            account_name=session.get("account_name"),
                            account_id=session.get("account_id"),
                            account_source=session.get("account_source"),
                        )
                    )
                # v6 §Implementation·三期（票 #89）：打开取词台 = 按需补缺一批未解析账号。
                # 只挂后台线程，页面不等它；开关关着则一个包都不发。
                self.request_account_backfill()
                return json_response({"ok": True, "sessions": sessions})
            if len(rest) == 1 and method == "PATCH":
                return self._api_session_patch(rest[0], body)
            if len(rest) == 1 and method == "DELETE":
                return self._api_session_delete(rest[0])
            # v33 T1（票 #207）：会话待选句的两个只读端点 —— 列表（按时间点排序）+
            # 待挑 N（挑词入口徽标的计数来源，spec §D17）。
            if len(rest) == 2 and rest[1] == "backlog" and method == "GET":
                return self._api_session_backlog(rest[0])
            if len(rest) == 3 and rest[1] == "backlog" and rest[2] == "count" and method == "GET":
                return self._api_session_backlog_count(rest[0])
            # v33 修复轮（spec §B2c / §C11）：来源状态（调度器 cc/ocr 现状 + 总开关）。
            if len(rest) == 3 and rest[1] == "backlog" and rest[2] == "status" and method == "GET":
                return self._api_session_backlog_status(rest[0])
            # v35 #227（spec §手动清理 API）：该会话底图占用 + 手动清理（两模式）。
            if len(rest) == 3 and rest[1] == "backlog" and rest[2] == "storage" and method == "GET":
                return self._api_session_backlog_storage(rest[0])
            if len(rest) == 3 and rest[1] == "backlog" and rest[2] == "cleanup" and method == "POST":
                return self._api_session_backlog_cleanup(rest[0], body)
            # v33 T4（票 #210 / spec §E21）：挑词落库 —— `capture()` 直调的受控
            # 第二条入口；与改句（spec §D19 看图改句）一并就位。
            if (
                len(rest) == 3
                and rest[1] == "backlog"
                and rest[2] == "pick"
                and method == "POST"
            ):
                return self._api_session_backlog_pick(rest[0], body)
            # v33 修复轮（spec §D19）：挑词撤销 —— chip × 的真回滚。
            if (
                len(rest) == 4
                and rest[1] == "backlog"
                and rest[2] == "pick"
                and method == "DELETE"
            ):
                return self._api_session_backlog_unpick(rest[0], rest[3])
            if len(rest) == 3 and rest[1] == "backlog" and method == "PATCH":
                return self._api_session_backlog_patch(rest[0], rest[2], body)
            # v33 修复轮（spec §D19）：单句重识别 —— 对该句字幕带底图重跑 OCR。
            if (
                len(rest) == 4
                and rest[1] == "backlog"
                and rest[3] == "reocr"
                and method == "POST"
            ):
                return self._api_session_backlog_reocr(rest[0], rest[2])
        # v33 T4（票 #210 / spec §C14）：字幕带底图的只读端点 —— 只 serve
        # `<data>/subtitle_bands/` 下的 `.png`（沙箱校验见 `_api_backlog_image`）。
        if head == "backlog" and rest == ["image"] and method == "GET":
            return self._api_backlog_image(query)
        # v35 #227：池页顶栏 / 「已挑完的视频」分组的底图总占用（一次拿全，只读）。
        if head == "backlog" and rest == ["storage"] and method == "GET":
            return self._api_backlog_storage()
        if head == "lookup" and not rest and method == "POST":
            return self._api_lookup(body)
        # v23 §4.1：取词台补释义（会话级 / 文章级 / 单条集合）+ 进度轮询。
        if head == "dict":
            if rest == ["refill"] and method == "POST":
                return self._api_dict_refill(body)
            if rest == ["pending"] and method == "GET":
                return self._api_dict_pending(query)
            if len(rest) == 2 and rest[0] == "refill" and method == "GET":
                return self._api_dict_refill_get(rest[1])
            # v29：单条「重查」——先预览（不写库），确认后再 apply。
            if rest == ["recheck"] and method == "POST":
                return self._api_dict_recheck(body)
            if rest == ["recheck", "apply"] and method == "POST":
                return self._api_dict_recheck_apply(body)
        if head == "tts":
            if not rest and method == "POST":
                return self._api_tts(body)
            # v11 §4（票 #110）：只确保 Edge 连接，不合成、不返回音频
            if rest == ["warm"] and method == "POST":
                return self._api_tts_warm(body)
            # v11 §4（票 #113）：批量入预热队列，不合成、不返回音频
            if rest == ["prewarm"] and method == "POST":
                return self._api_tts_prewarm(body)
        # 2026-11-06：前端播放链路事件上报（缓冲/报错/超时/连播起止），
        # 只收元数据、只写 logs/playback.log，绝不进库、不回显。
        if head == "playback" and rest == ["log"] and method == "POST":
            return self._api_playback_log(body)
        # 票 #179：扩展取共享令牌（sw.js 首次上报前 GET 一次）。只有过了 `_handle` 的
        # Host 校验才到得了这里；回包不带 CORS 头，跨站页发得出请求读不到 body。
        if head == "extension" and rest == ["token"] and method == "GET":
            return json_response({"ok": True, "token": self.extension_token})
        if head == "browser" and rest == ["context"]:
            if method == "GET":
                return self._api_browser_context_get()
            if method == "POST":
                return self._api_browser_context_post(body, user_agent=user_agent)
        if head == "export" and not rest and method == "GET":
            return self._api_export(query)
        if head == "hotkeys" and not rest and method == "GET":
            return self._api_hotkeys()
        if head == "settings" and not rest:
            if method == "GET":
                return self._api_settings()
            if method == "PUT":
                return self._api_settings_put(body)
        if head == "records":
            if not rest and method == "GET":
                return self._api_records(query)
            if len(rest) == 1:
                if method == "GET":
                    return self._api_record_get(rest[0])
                if method == "PATCH":
                    return self._api_record_patch(rest[0], body)
                if method == "DELETE":
                    return self._api_record_delete(rest[0])
            if len(rest) == 2 and rest[1] == "review" and method == "POST":
                return self._api_review(rest[0], body)
        # v22 P1b（票 #166 / spec §6.3）：取词台只读文章视图（GET）。
        # v22 P2a（票 #167 / spec §6.3 实现期补记）：另加两条**窄写**接口 ——
        # `POST /api/articles/{id}/resegment`、`DELETE /api/articles/{id}`，
        # 与 RPC `article_resegment` / `article_delete` 共用同一实现；其余写方法仍 404。
        if head == "articles":
            if not rest and method == "GET":
                return self._api_articles(query)
            if len(rest) == 1:
                if method == "GET":
                    return self._api_article(rest[0])
                if method == "DELETE":
                    return self._api_article_delete(rest[0], query)
            if len(rest) == 2 and rest[1] == "resegment" and method == "POST":
                return self._api_article_resegment(rest[0])
        return error_response(404, "not found")

    def _api_tts(self, body: Any) -> Response:
        """`POST /api/tts`（v11 §4 / v13 §2）：成功回音频字节，失败回 §4 的 JSON 错误。

        校验/缓存/合成/降级全在 `Speech` 里；这里只做参数转交与响应头。
        v13 §2 只多一个**可选** `priority`（`"user"` 默认 / `"background"`），
        非法值 400（不静默猜）；响应结构、缓存键、`X-TTS-*` 头都不变。
        """
        payload = body if isinstance(body, dict) else {}
        raw_priority = payload.get("priority", "user")
        # 只认字符串键（list / dict 这类不可哈希的值也是 400，不许冒成 500）
        if not isinstance(raw_priority, str) or raw_priority not in TTS_PRIORITIES:
            return error_response(400, "bad_priority")
        try:
            result = self.speech.synthesize(
                payload.get("text"),
                voice=payload.get("voice"),
                rate=payload.get("rate"),
                priority=TTS_PRIORITIES[raw_priority],
            )
        except SpeechError as exc:
            # 2026-11-06：合成失败原来只有访问行里的状态码，没有失败原因。
            # `error` / `reason` 都是 §4 的错误码（不含用户文本），可以安全落盘。
            log.warning(
                "tts 合成失败 status=%d error=%s reason=%s",
                exc.status,
                str(exc.error)[:64].replace("\n", " "),
                str(exc.reason or "")[:64].replace("\n", " "),
            )
            error: dict[str, Any] = {"ok": False, "error": exc.error}
            if exc.reason is not None:
                error["reason"] = exc.reason
            return json_response(error, exc.status)
        if result.ms >= TTS_SLOW_MS:
            # 只记元数据（backend/cache/ms），绝不记文本（§8）。前端有 10s 硬超时，
            # 3s 起就该有预警：慢合成是「卡顿 → 超时 → 连播中断」的源头。
            log.warning(
                "tts slow backend=%s cache=%s ms=%d",
                result.backend,
                "hit" if result.cached else "miss",
                result.ms,
            )
        return Response(
            status=200,
            body=result.data,
            content_type=result.mime,
            headers={
                "X-TTS-Backend": result.backend,
                "X-TTS-Cache": "hit" if result.cached else "miss",
                "X-TTS-Ms": str(result.ms),
            },
        )

    def _api_tts_warm(self, body: Any = None) -> Response:
        """`POST /api/tts/warm`（v11 §4，票 #110；v13 §5 票 #120）：只确保连接可用，不合成。

        - `tts_online=0` 时 `Speech.warm()` 直接返回 False（零网络），这里仍是 200，
          `connected:false` 不算错误（§4）；
        - v13 §5：可选 `{"connections": 2}` 让池在后台补第二条热连接（默认 1）；
          其他值 400 `bad_connections`（不静默猜），响应结构不变。
        """
        payload = body if isinstance(body, dict) else {}
        connections = payload.get("connections", 1)
        if not isinstance(connections, int) or isinstance(connections, bool):
            return error_response(400, "bad_connections")
        if connections not in TTS_WARM_CONNECTIONS:
            return error_response(400, "bad_connections")
        return json_response(
            {"ok": True, "connected": bool(self.speech.warm(connections))}
        )

    def _api_tts_prewarm(self, body: Any) -> Response:
        """`POST /api/tts/prewarm`（v11 §4，票 #113）：批量入后台预热队列。

        - body 必须是 `{"texts": [...]}`（可选 `voice`）；不是 → 400 `bad_texts`；
        - 一次最多 `MAX_PREWARM_ITEMS`（40）条 → 超出 400 `too_many`（不悄悄截断）；
        - 成功 `{"ok":true,"queued":N,"skipped":M}`，不返回音频；
        - `tts_online=0` 同样是 200 + `queued:0`（§3：关掉在线不算错误）。
        """
        if not isinstance(body, dict):
            return error_response(400, "bad_texts")
        texts = body.get("texts")
        if not isinstance(texts, list):
            return error_response(400, "bad_texts")
        if len(texts) > MAX_PREWARM_ITEMS:
            return error_response(400, "too_many")
        try:
            result = self.speech.prewarm(texts, voice=body.get("voice"))
        except SpeechError as exc:
            payload: dict[str, Any] = {"ok": False, "error": exc.error}
            if exc.reason is not None:
                payload["reason"] = exc.reason
            return json_response(payload, exc.status)
        return json_response(
            {"ok": True, "queued": result["queued"], "skipped": result["skipped"]}
        )

    def _api_playback_log(self, body: Any) -> Response:
        """`POST /api/playback/log`：批量收播放事件，写 `logs/playback.log`。

        body = `{"events": [{"event": "...", "ms": 123, ...}, ...]}`。字段白名单、
        条数上限、字符串形状全在 `playback_log.record()` 里收口；这里只做外形校验。
        上报失败一律 200（accepted=0 也算），前端不该因为日志写不进去而中断播放。
        """
        if not isinstance(body, dict) or not isinstance(body.get("events"), list):
            return error_response(400, "bad_events")
        accepted = playback_log.record(body["events"])
        return json_response({"ok": True, "accepted": accepted})

    def _api_health(self) -> Response:
        counts = self.store.counts()
        return json_response(
            {
                "ok": True,
                "db": str(self.store.db_path),
                "schema": self.store.schema_version(),
                "records": counts["records"],
                "sessions": counts["sessions"],
                "dict_cache": counts["cache"],
                "pending": counts["pending"],
                "dict_online": self.dict_service.online_enabled,
                "ecdict": bool(self.dict_service.local),
                "dict_timeout_ms": self.timeout_ms,
                # v11 §4（票 #112）：只读发音状态（不含任何文本）
                "tts": self.speech.status(),
                "uptime_ms": round_ms(self.started),
            }
        )

    def _api_capture_status(self) -> Response:
        """`GET /api/capture/status`（v34 T5 / 票 #218 / spec §C21）。

        全局相位出口：不需要先知道会话 id —— 回答「现在到底在不在收句」。
        纯读、无参数、无副作用（`heartbeat_age_ms` 取 hub 的浏览器上下文）。

        回包：`{ok, enabled, armed, phase, session:{id,title}|null,
        last_added_at, added_in_segment, heartbeat_age_ms, cc, ocr, events}`。
        `phase` 是 spec §C19 的 11 档；`events` 是调度器最近的关键事件
        （web 按 id 增量消费，见 `CaptureScheduler.status`）。
        """
        context, age_ms = self.browser_context()
        heartbeat_age_ms = age_ms if context is not None else None
        try:
            status = self.scheduler.status(heartbeat_age_ms=heartbeat_age_ms)
        except Exception as exc:  # noqa: BLE001 - 端点坏掉不许把主界面拖下水
            log.warning("收句全局状态取用失败（%s）", type(exc).__name__)
            status = {}
        session = None
        session_id = status.get("session_id")
        if session_id is not None:
            row = self.store.get_session(int(session_id))
            if row is not None:
                session = {"id": int(row["id"]), "title": str(row.get("title") or "")}
        return json_response(
            {
                "ok": True,
                "enabled": bool(status.get("enabled", False)),
                "armed": bool(status.get("armed", False)),
                "phase": str(status.get("phase") or "unarmed"),
                "session": session,
                "last_added_at": status.get("last_added_at"),
                "added_in_segment": int(status.get("added_in_segment") or 0),
                "heartbeat_age_ms": heartbeat_age_ms,
                "cc": str(status.get("cc") or "off"),
                "ocr": str(status.get("ocr") or "off"),
                "events": list(status.get("events") or []),
            }
        )

    def _api_summary(self) -> Response:
        stats = self.store.stats(days=30)
        return json_response(
            {
                "ok": True,
                "records": stats["records"],
                "sessions": stats["sessions"],
                "due_now": stats["due_now"],
                "reviewed": stats["reviewed"],
                "today": stats["by_day"][-1]["captures"] if stats["by_day"] else 0,
                "by_day": stats["by_day"][-7:],
                # 票 #87：标签全量返回（原截前 12），侧栏标签云负责折叠。
                "tags": stats["tags"],
                "recent_sessions": self.store.list_sessions()[:20],
            }
        )

    def _api_stats(self, query: dict[str, list[str]]) -> Response:
        try:
            days = int(first_param(query, "days", "30") or 30)
        except ValueError:
            return error_response(400, "days 必须是整数")
        return json_response({"ok": True, **self.store.stats(days=days)})

    def _api_records(self, query: dict[str, list[str]]) -> Response:
        try:
            session = first_param(query, "session")
            result = self.store.list_records(
                query=first_param(query, "query") or first_param(query, "q"),
                session_id=int(session) if session not in (None, "") else None,
                tag=first_param(query, "tag"),
                state=first_param(query, "state"),
                sort=first_param(query, "sort") or "-captured_at",
                limit=int(
                    first_param(query, "limit", str(DEFAULT_LIMIT)) or DEFAULT_LIMIT
                ),
                offset=int(first_param(query, "offset", "0") or 0),
            )
        except ValueError as exc:
            return error_response(400, str(exc))
        return json_response({"ok": True, **result})

    def _api_record_get(self, raw_id: str) -> Response:
        record_id = _int_or_none(raw_id)
        if record_id is None:
            return error_response(400, "id 必须是整数")
        record = self.store.get_record(record_id)
        return (
            json_response({"ok": True, "record": record})
            if record
            else error_response(404, "not found")
        )

    def _api_record_patch(self, raw_id: str, body: Any) -> Response:
        """`PATCH /api/records/{id}`：单条编辑；v15 §B2（票 #128）多一个段级原句入口。

        body 可选 `{"scope": "segment", "context": 新原句, "expect_context": 旧原句}`：
        整段（同会话 + 同原句）在一个事务里改掉，回 `{ok, affected, record}`。
        `expect_context` 与库里当前值不一致 → 400（防并发覆盖）；`scope` 缺省 / `"one"`
        仍是原来的单条 `update_record`。两条路径都不认 `target`（白名单没有它，静默忽略）。
        """
        record_id = _int_or_none(raw_id)
        if record_id is None:
            return error_response(400, "id 必须是整数")
        if not isinstance(body, dict):
            return error_response(400, "body 必须是 JSON 对象")
        scope = body.get("scope", "one")
        if scope not in ("one", "segment"):
            return error_response(400, "scope 只能是 one 或 segment")
        if scope == "segment":
            expected = body.get("expect_context")
            if not isinstance(expected, str):
                return error_response(400, "scope=segment 需要字符串 expect_context")
            if "context" not in body:
                return error_response(400, "scope=segment 需要 context")
            try:
                result = self.store.update_segment_context(
                    record_id, expected, body["context"]
                )
            except (ValueError, TypeError) as exc:
                return error_response(400, str(exc))
            if result is None:
                return error_response(404, "not found")
            return json_response({"ok": True, **result})
        try:
            record = self.store.update_record(record_id, body)
        except (ValueError, TypeError) as exc:
            return error_response(400, str(exc))
        return (
            json_response({"ok": True, "record": record})
            if record
            else error_response(404, "not found")
        )

    def _api_record_delete(self, raw_id: str) -> Response:
        record_id = _int_or_none(raw_id)
        if record_id is None:
            return error_response(400, "id 必须是整数")
        if not self.store.delete_record(record_id):
            return error_response(404, "not found")
        return json_response({"ok": True, "deleted": True})

    def _api_review(self, raw_id: str, body: Any) -> Response:
        record_id = _int_or_none(raw_id)
        if record_id is None:
            return error_response(400, "id 必须是整数")
        grade = str((body or {}).get("grade") or "") if isinstance(body, dict) else ""
        try:
            record = self.store.review(record_id, grade)
        except ValueError as exc:
            return error_response(400, str(exc))
        return (
            json_response({"ok": True, "record": record})
            if record
            else error_response(404, "not found")
        )

    # --- v22 P1b（票 #166 / spec §6.3）：取词台只读文章接口 -------------------

    def _api_articles(self, query: dict[str, list[str]]) -> Response:
        """`GET /api/articles?limit=50&offset=0` → 文章目录（**不含正文**）。

        `limit` 缺省 50、只收 1–200；`offset` 缺省 0、只收 ≥0。非法一律 400
        （不静默夹取 —— 前端拼错参数要能看见，与 records 接口的宽松口径区分开）。
        """
        raw_limit = first_param(query, "limit")
        if raw_limit in (None, ""):
            limit = ARTICLES_DEFAULT_LIMIT
        else:
            limit = _int_or_none(raw_limit)
            if limit is None or not 1 <= limit <= ARTICLES_MAX_LIMIT:
                return error_response(
                    400, f"limit 必须是 1 到 {ARTICLES_MAX_LIMIT} 的整数"
                )
        raw_offset = first_param(query, "offset")
        if raw_offset in (None, ""):
            offset = 0
        else:
            offset = _int_or_none(raw_offset)
            if offset is None or offset < 0:
                return error_response(400, "offset 必须是非负整数")
        return json_response(
            {"ok": True, **self.store.list_articles(limit=limit, offset=offset)}
        )

    def _api_article(self, raw_id: str) -> Response:
        """`GET /api/articles/{doc_id}` → 单篇详情（段落卡数据源，spec §6.3）。"""
        doc_id = _int_or_none(raw_id)
        if doc_id is None:
            return error_response(400, "id 必须是整数")
        document = self.store.document(doc_id)
        if document is None:
            return error_response(404, "not found")
        return json_response({"ok": True, **self._article_detail(document)})

    def _api_article_resegment(self, raw_id: str) -> Response:
        """`POST /api/articles/{doc_id}/resegment`（spec §6.3 实现期补记）。

        与 RPC `article_resegment` **同一实现**：业务判定全在 RPC 方法里，这里只做
        id 解析与错误码映射（见 `_article_write_response`）。
        """
        doc_id = _int_or_none(raw_id)
        if doc_id is None:
            return error_response(400, "id 必须是整数")
        return self._article_write_response(self.article_resegment({"doc_id": doc_id}))

    def _api_article_delete(
        self, raw_id: str, query: dict[str, list[str]]
    ) -> Response:
        """`DELETE /api/articles/{doc_id}[?keep_records=1]`（spec §6.3 实现期补记）。

        `keep_records` 缺省 `1`（词条保留，spec §12）；只收 `1` / `true`，`0` / `false`
        与任何其它值都 400 —— 连带删词条的产品入口**不暴露**（见 `article_delete`）。
        与 RPC `article_delete` 同一实现。
        """
        doc_id = _int_or_none(raw_id)
        if doc_id is None:
            return error_response(400, "id 必须是整数")
        raw_keep = (first_param(query, "keep_records") or "").strip().lower()
        if raw_keep not in ("", "1", "true"):
            return error_response(400, "keep_records 只支持 1（词条保留）")
        return self._article_write_response(
            self.article_delete({"doc_id": doc_id, "keep_records": True})
        )

    @staticmethod
    def _article_write_response(result: dict[str, Any]) -> Response:
        """两条窄写 HTTP 共用的「RPC 结果 → HTTP 响应」映射。

        `bad request` → 400、`not found` → 404，其余业务态错误（`no segmentation
        source` / `bad segmentation source`）→ **409**（请求本身合法、文章当前状态
        不满足；错误串原样放 body，前端按 `error` 提示）。
        """
        if result.get("ok"):
            return json_response(result)
        error = str(result.get("error") or "")
        if error == "bad request":
            status = 400
        elif error == "not found":
            status = 404
        else:
            status = 409
        return error_response(status, error)

    def _article_detail(
        self, document: dict[str, Any], *, include_needs_review: bool = True
    ) -> dict[str, Any]:
        """一篇文章的段落卡数据（spec §6.3 / §8.2）。

        - `type`：`segmentation_source.paragraph_kinds()` 派生（manual / ocr 重跑同一套
          分段算法；无 source / 段数或文本对不上 → 全 `body`，旧 v21 文章自然回落）；
        - `start_in_paragraph/end_in_paragraph`：**后端算**（前端零搜索）。先用
          `record.context`（命中的单句）在段文本里定位，再在句内定位目标词（整词优先、
          大小写不敏感）；context 与段文本对不上时退回整段找 target；都找不到 → `-1`；
        - `needs_review`：**只含** `passage_index = -1` 的词条（重分段后位置待定）。
          NULL / 越界的脏索引**既不进段落、也不进待复核区**（记 warning；词条仍在库里，
          不丢），从而与 `store.list_articles` 的徽标计数（只数 `-1`）同一口径 ——
          篇卡徽标数恒等于文章页待复核区条数。`include_needs_review=False` 时跳过这份
          输出派生（面板预览窗只用 paragraphs/stats，review 修复轮）；
        - `suspicious`：空段 / 段长 > `PARAGRAPH_MAX_CHARS` 的段（只由现有数据推导，
          不猜类型、不伪造）。
        """
        text = document["text"]
        spans = document["passages"]
        doc_id = int(document["id"])
        kinds = paragraph_kinds(
            document.get("format"),
            self.store.segmentation_source(doc_id),
            spans,
            text,
        )
        if len(kinds) != len(spans):  # 防御：形状对不上就全 body（不伪造类型）
            kinds = ["body"] * len(spans)
        by_index, needs_review, records_total = self._article_record_groups(
            doc_id, len(spans), include_needs_review
        )

        paragraphs: list[dict[str, Any]] = []
        suspicious: list[dict[str, Any]] = []
        for index, (start, end) in enumerate(spans):
            begin, finish = int(start), int(end)
            body = text[begin:finish]
            reason = _paragraph_anomaly(body)
            if reason:
                suspicious.append({"index": index, "reason": reason, "chars": len(body)})
            paragraphs.append(
                self._paragraph_payload(
                    index,
                    begin,
                    finish,
                    body,
                    kinds[index],
                    by_index.get(index, []),
                    reason,
                )
            )

        segments = len(spans)
        return {
            "id": doc_id,
            "title": document["title"],
            "source_app": document["source_app"],
            "format": document["format"],
            "session_id": document["session_id"],
            "stats": {
                "segments": segments,
                "chars": len(text),
                "avg_chars": round(len(text) / segments) if segments else 0,
                "suspicious": suspicious,
                # **additive（P2b review / spec §9）**：M 词口径 = 已定位 + 待复核。
                # 取词台文章页一直用 `段落内 records + needs_review.length` 算；
                # 预览窗只拿 `paragraphs`，没有这个字段就会把待复核词漏掉（两处数字不一致）。
                # 字段与 `include_needs_review` 无关，始终返回（总计 = 该文全部词条）。
                "records_total": records_total,
            },
            "needs_review": [
                self._needs_review_payload(record) for record in needs_review
            ],
            "paragraphs": paragraphs,
        }

    def _article_record_groups(
        self, doc_id: int, paragraph_count: int, include_needs_review: bool = True
    ) -> tuple[dict[int, list[dict[str, Any]]], list[dict[str, Any]], int]:
        """词条按段分组 + 待复核列表（`-1`）+ 词条总数；脏索引两处都不进，只记 warning。

        与 `store.list_articles` 的 `needs_review` 计数同口径（只数 `-1`）—— 篇卡徽标
        与文章页待复核区条数由此恒等。NULL / 越界的词条仍留在库里（不丢词），只是没有
        可展示的位置，API 不把它塞进任何一段。`include_needs_review=False` 时只做分组
        （面板预览窗只用 paragraphs/stats，不必派生待复核输出）。

        第三个返回值 = **该文全部词条数**（含待复核/脏索引），供 `stats.records_total`
        （P2b review / spec §9 的 M 词口径 = 已定位 + 待复核）使用；它与
        `include_needs_review` 无关。
        """
        by_index: dict[int, list[dict[str, Any]]] = {}
        needs_review: list[dict[str, Any]] = []
        orphans = 0
        records = self.store.article_records(doc_id)
        for record in records:
            index = record.get("passage_index")
            if index == -1:
                if include_needs_review:
                    needs_review.append(record)
            elif isinstance(index, int) and 0 <= index < paragraph_count:
                by_index.setdefault(index, []).append(record)
            else:
                orphans += 1
        if orphans:
            log.warning(
                "文章有 %d 条词条段落索引不可用（doc_id=%s），未进段落/待复核",
                orphans,
                doc_id,
            )
        return by_index, needs_review, len(records)

    def _paragraph_payload(
        self,
        index: int,
        begin: int,
        finish: int,
        body: str,
        kind: str,
        records: list[dict[str, Any]],
        reason: str,
    ) -> dict[str, Any]:
        """一段的段落卡 payload（记录 span 由后端算，前端零搜索）。"""
        return {
            "index": index,
            "type": kind,
            "start": begin,
            "end": finish,
            "chars": len(body),
            "text": body,
            "suspicious": bool(reason),
            "records": [self._record_payload(record, body) for record in records],
        }

    def _record_payload(
        self, record: dict[str, Any], paragraph_text: str
    ) -> dict[str, Any]:
        rel_start, rel_end = self._record_span(
            paragraph_text, record.get("context"), record.get("target")
        )
        return {
            "id": record["id"],
            "target": record["target"],
            "kind": record["kind"],
            "meaning": record["meaning"],
            # v23 §4.3 S5：文章页段落卡也要能画「机翻」徽标（与阅读页/详情同口径）。
            "meaning_source": record.get("meaning_source") or "",
            "phonetic": record["phonetic"],
            "start_in_paragraph": rel_start,
            "end_in_paragraph": rel_end,
        }

    @staticmethod
    def _needs_review_payload(record: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": record["id"],
            "target": record["target"],
            "kind": record["kind"],
            "meaning": record["meaning"],
            "phonetic": record["phonetic"],
            "context": record.get("context") or "",
        }

    @staticmethod
    def _record_span(paragraph: str, context: Any, target: Any) -> tuple[int, int]:
        """词条在**段内**的字符区间 `[start, end)`；定位失败 → `(-1, -1)`。

        口径与 §6.3「后端算、前端零搜索」一致：`context` 是命中的单句，先在段里找到
        这句，再在句内找目标词；context 被编辑过 / 与段落文本有细微差异时退回整段找
        target。两个偏移都是**相对段落起点**的。

        弯引号 / 长连字符等排版标点先用 `article.normalize_punct` 做 **1:1 归一**
        （偏移不变，与 `ArticleIndex` 同一口径），所以 `don’t` 的 context 能在 `don't`
        的正文里定位、反过来也一样。
        """
        word = str(target or "")
        if not paragraph or not word:
            return (-1, -1)
        plain = normalize_punct(paragraph)
        sentence = normalize_punct(str(context or "").strip())
        base = plain.find(sentence) if sentence else -1
        if base >= 0:
            offset = _find_target(sentence, word)
            if offset >= 0:
                absolute = base + offset
                return (absolute, absolute + len(word))
            offset = _find_target(paragraph, word, start=base)
            if offset >= 0:
                return (offset, offset + len(word))
            return (-1, -1)
        offset = _find_target(paragraph, word)
        return (offset, offset + len(word)) if offset >= 0 else (-1, -1)

    def _api_session_patch(self, raw_id: str, body: Any) -> Response:
        session_id = _int_or_none(raw_id)
        if session_id is None:
            return error_response(400, "id 必须是整数")
        if not isinstance(body, dict):
            return error_response(400, "body 必须是 JSON 对象")
        try:
            session = self.store.update_session(session_id, body)
        except (ValueError, TypeError) as exc:
            return error_response(400, str(exc))
        return (
            json_response({"ok": True, "session": session})
            if session
            else error_response(404, "not found")
        )

    def _api_session_delete(self, raw_id: str) -> Response:
        """删会话（连带其词条与待选句）。侧栏的删除按钮走它；删除不可恢复，前端要先确认。

        回包 `{ok, deleted}`，`deleted` 里的 `records` / `backlog`（票 #225）分别是
        连带删掉的词条数与待选句行数；底图文件交给孤儿清扫，不在本端点删。
        """
        session_id = _int_or_none(raw_id)
        if session_id is None:
            return error_response(400, "id 必须是整数")
        removed = self.store.delete_session(session_id)
        if removed is None:
            return error_response(404, "not found")
        return json_response({"ok": True, "deleted": removed})

    def _api_session_backlog(self, raw_id: str) -> Response:
        """`GET /api/sessions/{id}/backlog`（v33 T1，票 #207）：该会话的待选句。

        回包：`{ok, session_id, sentences（按时间点排序）, pending（待挑 N）}`。
        待选句是独立层（ADR-0012）：这里只读 `subtitle_backlog` 表，与 records
        无交集；挑词动作由 T4 的写端点接（票 #210，本端点只读）。

        v35 #227：每句加 `image_ok`（底图文件是否真的还在）—— 缺图时前端不显示
        「看图 / 改句」入口（spec 用户故事 17）；老中枢没有这个字段，前端按「显示」
        兜底。`image_ok` 只读、只查目录，不做任何清理。
        """
        session_id = _int_or_none(raw_id)
        if session_id is None:
            return error_response(400, "id 必须是整数")
        if self.store.get_session(session_id) is None:
            return error_response(404, "not found")
        sentences = self.store.subtitle_sentences(session_id)
        # D7：image_ok 走统一的根解析（db 旁目录优先 + 兜底根），旧目录的存量图不再误报缺失。
        sizes = self._band_image_sizes()
        for sentence in sentences:
            name = band_cleanup.band_name_of(sentence.get("image_path"))
            sentence["image_ok"] = name is not None and name in sizes
        return json_response(
            {
                "ok": True,
                "session_id": session_id,
                "sentences": sentences,
                "pending": self.store.subtitle_pending_count(session_id),
                # v33 修复轮（spec §E22）：「已挑 N 词条」的服务端口径
                "records": self.store.session_record_count(session_id),
            }
        )

    def _api_session_backlog_count(self, raw_id: str) -> Response:
        """`GET /api/sessions/{id}/backlog/count`（v33 T1，票 #207）：待挑 N。

        「待挑 N」徽标（spec §D17）的计数来源。T1 阶段没有挑词动作，N = 全部
        句数；T4（票 #210）接入挑词后在 `subtitle_pending_count` 收窄口径，端点不变。
        v33 修复轮（spec §E22）起回包带 `records`（该会话词条数）—— 挑词界面
        统计条「已挑 N 词条」的服务端口径，挑/撤后前端重拉这里即得真值。
        """
        session_id = _int_or_none(raw_id)
        if session_id is None:
            return error_response(400, "id 必须是整数")
        if self.store.get_session(session_id) is None:
            return error_response(404, "not found")
        return json_response(
            {
                "ok": True,
                "session_id": session_id,
                "pending": self.store.subtitle_pending_count(session_id),
                "records": self.store.session_record_count(session_id),
            }
        )

    def _api_session_backlog_pick(self, raw_id: str, body: Any) -> Response:
        """`POST /api/sessions/{id}/backlog/pick`（v33 T4，票 #210 / spec §E21）。

        挑词落库：`Store.capture()` 直调（词条在 spool 之外**显式新增的第二条
        受控入口**，无外壳往返）—— context=待选句原文、position=from_s 的 mm:ss、
        kind=所选、归该视频会话；词级去重 / 同句合并 / 会话归户全部自动生效。
        成功后该句打已挑标记（持久化）、`pending` 递减。

        回包契约（T3 前端按此实现）：`{ok, record_id, pending}`；v34 T1（票 #214 /
        spec D29）additive 扩成 `+ meaning, fill`（`fill` ∈ `queued|filled|failed`）。
        收尾与 `ingest_inbox` 同款：新词喂 TTS 后台预热（票 #113）、释义仍为空的
        词条投补释义队列（v23 §4.2）—— 两件都不阻塞、失败不影响挑词本身。
        挑词是用户确认过的目标（spec D26）：入队带 `trusted=True`，绕过碎片门禁。
        """
        session_id = _int_or_none(raw_id)
        if session_id is None:
            return error_response(400, "id 必须是整数")
        if not isinstance(body, dict):
            return error_response(400, "body 必须是 JSON 对象")
        sentence_id = _payload_int(body.get("sentence_id"))
        if sentence_id is None:
            return error_response(400, "sentence_id 必须是整数")
        text = one_line(body.get("text"))
        if not text:
            return error_response(400, "text 必填")
        kind = body.get("kind")
        # 气泡三档只认 word|phrase|sentence：非法值 400（不静默猜，票内 kind 承接所选范围）
        if not isinstance(kind, str) or kind not in KINDS:
            return error_response(400, "bad kind")
        if self.store.get_session(session_id) is None:
            return error_response(404, "not found")
        try:
            result = self.store.pick_subtitle_sentence(
                session_id, sentence_id, text=text, kind=kind
            )
        except ValueError as exc:
            # 句子不存在 / 不属于该会话 → 404（挑词不许静默落到别的会话）
            return error_response(404, str(exc))
        target = one_line(result.get("target"))
        record_id = int(result.get("id") or 0)
        if result.get("ok") and target and not result.get("dedup"):
            self.prewarm_targets([target])
        meaning = one_line(result.get("meaning"))
        fill = "filled" if meaning else "failed"
        if result.get("ok") and record_id and not meaning:
            try:
                fill = (
                    "queued"
                    if self.meaning_fill.submit(record_id, target or text, trusted=True)
                    else "failed"
                )
            except Exception as exc:  # noqa: BLE001 - 补释义坏掉不许影响挑词
                log.info("补释义入队跳过：%s", type(exc).__name__)
                fill = "failed"
        return json_response(
            {
                "ok": True,
                "record_id": record_id,
                "pending": int(result.get("pending") or 0),
                "meaning": meaning,
                "fill": fill,
            }
        )

    def _api_session_backlog_patch(
        self, raw_id: str, raw_sentence: str, body: Any
    ) -> Response:
        """`PATCH /api/sessions/{id}/backlog/{sentence_id}`（v33 T4，票 #210 / spec §D19）。

        看图改句：更新原句并重算 sentence_key；已挑标记一并清除（原句变了 =
        旧挑词的词下标全部作废，句子回未挑态，与前端同一语义）。
        回包契约（T3 前端按此实现）：`{ok, text, sentence_key}`。
        """
        session_id = _int_or_none(raw_id)
        sentence_id = _int_or_none(raw_sentence)
        if session_id is None or sentence_id is None:
            return error_response(400, "id 必须是整数")
        if not isinstance(body, dict):
            return error_response(400, "body 必须是 JSON 对象")
        text = one_line(body.get("text"))
        if not text:
            return error_response(400, "text 必填")
        try:
            row = self.store.update_subtitle_text(session_id, sentence_id, text)
        except ValueError as exc:
            return error_response(400, str(exc))
        if row is None:
            return error_response(404, "not found")
        return json_response(
            {"ok": True, "text": row["text"], "sentence_key": row["sentence_key"]}
        )

    def _api_session_backlog_unpick(self, raw_id: str, raw_record: str) -> Response:
        """`DELETE /api/sessions/{id}/backlog/pick/{record_id}`（v33 修复轮 / spec §D19）。

        挑词撤销（前端 chip × 的真回滚）：删该词条（会话内校验，跨会话 404）；
        该会话再无词条 context 等于该待选句原文时清 `picked_at`（句子回未挑、
        pending 回升）—— 回滚语义在 `store.unpick_subtitle_sentence`。

        回包契约：`{ok, record_id, pending, sentence_cleared}`（sentence_cleared
        = 该句已挑标记是否被清掉，前端据此把行拨回未挑态）。
        """
        session_id = _int_or_none(raw_id)
        record_id = _int_or_none(raw_record)
        if session_id is None or record_id is None:
            return error_response(400, "id 必须是整数")
        if self.store.get_session(session_id) is None:
            return error_response(404, "not found")
        result = self.store.unpick_subtitle_sentence(session_id, record_id)
        if result is None:
            return error_response(404, "not found")  # 词条不存在 / 不属于该会话
        return json_response(
            {
                "ok": True,
                "record_id": record_id,
                "pending": int(result["pending"]),
                "sentence_cleared": int(result["cleared"]),
            }
        )

    def _api_session_backlog_status(self, raw_id: str) -> Response:
        """`GET /api/sessions/{id}/backlog/status`（v33 修复轮 / spec §B2c + §C11）。

        挑词界面顶栏来源徽标的数据源：调度器该会话的 `cc`（ok/no_english_track/
        error/off）、`ocr`（active/idle/redrag_needed/failed/off）与总开关
        `enabled`、手动扳机 `armed`（v34 T3；未 armed 时 ocr 恒 off）。
        状态映射在 `CaptureScheduler.status_of`（纯读）。

        回包：`{ok, session_id, enabled, armed, cc, ocr}`。
        """
        session_id = _int_or_none(raw_id)
        if session_id is None:
            return error_response(400, "id 必须是整数")
        if self.store.get_session(session_id) is None:
            return error_response(404, "not found")
        status = self.scheduler.status_of(session_id)
        return json_response({"ok": True, "session_id": session_id, **status})

    # --- 底图占用与手动清理（v35 #227 / spec §手动清理 API）----------------------

    def _band_dir(self) -> Path:
        """底图**主**目录：恒为 `store.db_path.parent / "subtitle_bands"`（D-L1）。

        写统一（D7）之后新句的底图一律落这里；读侧另有 `_band_roots` 的兜底根。
        """
        return (self.store.db_path.parent / "subtitle_bands").resolve()

    def _band_roots(self) -> list[Path]:
        """D7 底图根序：db 旁目录优先，采样器老落盘目录（兜底根）殿后（去重）。

        演示 / 验收用 `--db <副本目录>/capture.db` 时主目录自动跟着副本走，不碰
        真实存量图；主目录与兜底根是同一个目录时只保留一份。
        """
        primary = self._band_dir()
        try:
            fallback = Path(self.band_fallback_dir).resolve()
        except OSError:  # 兜底根解析不了就只剩主根
            return [primary]
        return [primary] if fallback == primary else [primary, fallback]

    def _band_image_resolve(self, image_path: Any) -> Path | None:
        """句上的底图引用 → 真实文件路径（**唯一的**根解析器，serve / 重识别 /
        image_ok / 清理对账共用，不许各写各的）。

        db 旁目录优先；旧句的 `image_path` 是绝对路径（可能指向迁移前的老目录，
        甚至已经不存在）—— 统一取**文件名**（过白名单）按根序找，哪个根有就用
        哪个根的文件；两边都没有 → None。找不到不抛、不猜。
        """
        name = band_cleanup.band_name_of(image_path)
        if name is None:
            return None
        for root in self._band_roots():
            candidate = root / name
            try:
                if candidate.is_file():
                    return candidate
            except OSError:  # 单个根读不了不影响下一个
                continue
        return None

    def _band_image_sizes(self) -> dict[str, int]:
        """两个根的白名单底图字节合并（主根优先，同名不重复计）。"""
        sizes: dict[str, int] = {}
        for root in self._band_roots():
            for name, size in band_cleanup.band_file_sizes(root).items():
                sizes.setdefault(name, size)
        return sizes

    def _band_delete_files(self, names: set[str]) -> tuple[int, int]:
        """删一批白名单底图：每个根都试一遍（文件在哪个根就删哪个根的）。

        `band_cleanup.band_delete_files` 只吃一个目录，两个根各调一次即可 ——
        文件名在两个根都存在时主根先删，兜底根扫不到就自然跳过。
        """
        deleted = 0
        freed = 0
        for root in self._band_roots():
            part_deleted, part_freed = band_cleanup.band_delete_files(root, names)
            deleted += part_deleted
            freed += part_freed
        return deleted, freed

    def band_cleanup_references(self) -> list[dict[str, Any]]:
        """自动清理的引用行视图（D7）：底图根解析统一走 `_band_image_resolve`。

        `band_cleanup.cleanup_bands` 只扫 db 旁目录这一个根；只在兜底根存在的
        存量图它「看不见」，原样交进去会被「缺图对账」（规则 3）误置空 —— 那些
        图明明还在，未挑句铁律不许这样丢。这里按解析结果分流：

        - 主根里有 → 原样交（正常参与孤儿 / TTL / 容量清理）；
        - 只在兜底根里 → **本轮跳过并计数**（不置空、不删；手动清理端点管得到）；
        - 两边都没有 → 照常交（规则 3 把坏引用置空）；
        - 非白名单引用 → 原样交（band_cleanup 本来就不当底图处理）。
        """
        primary_names = set(band_cleanup.band_file_sizes(self._band_dir()))
        fallback_only = 0
        rows: list[dict[str, Any]] = []
        for row in self.store.band_image_references():
            name = band_cleanup.band_name_of(row.get("image_path"))
            if name is None or name in primary_names:
                rows.append(row)
                continue
            if self._band_image_resolve(row.get("image_path")) is not None:
                fallback_only += 1  # 只在兜底根：清理扫不到它，跳过（防误置空）
                continue
            rows.append(row)  # 两边都没有：交给规则 3 缺图对账
        if fallback_only:
            log.info(
                "底图清理：%d 行引用的底图只在旧目录（本轮跳过，不删也不置空）",
                fallback_only,
            )
        return rows

    def _band_storage(self) -> dict[str, Any]:
        """全部会话的底图占用（纯函数 `band_cleanup.band_storage`：白名单 + 沙箱）。

        D7：兜底根里、主根没有的存量文件也进账 —— 否则旧目录那批图在池页
        既看不到占用、也出不了「删除底图」的入口。文件名在主根存在的引用不
        重复算到兜底根头上。
        """
        roots = self._band_roots()
        references = self.store.band_image_references()
        storage = band_cleanup.band_storage(roots[0], references)
        if len(roots) < 2:
            return storage
        primary_names = set(band_cleanup.band_file_sizes(roots[0]))
        remainder = [
            row
            for row in references
            if band_cleanup.band_name_of(row.get("image_path")) not in primary_names
        ]
        extra = band_cleanup.band_storage(roots[1], remainder)
        sessions = dict(storage["sessions"])
        for session_id, info in extra["sessions"].items():
            bucket = sessions.setdefault(
                int(session_id),
                {"images": 0, "bytes": 0, "picked_images": 0, "unpicked_images": 0},
            )
            for key in ("images", "bytes", "picked_images", "unpicked_images"):
                bucket[key] += info[key]
        return {
            "images": storage["images"] + extra["images"],
            "bytes": storage["bytes"] + extra["bytes"],
            "sessions": sessions,
        }

    def _band_max_bytes(self) -> int:
        """底图总量上限（config `band_image_max_mb`，`0` = 关），池页 80% 黄字用。

        与维护线程同一口径（都读 config 的同一个键）；只读 config，绝不回写。
        """
        config = read_config(self.config_path)
        return megabytes_to_bytes(
            config_int(
                config,
                "band_image_max_mb",
                band_cleanup.DEFAULT_MAX_BYTES // 1024 // 1024,
            )
        )

    def _session_capturing(self, session_id: int) -> bool:
        """该会话此刻是否在收句（spec #228「确认面板黄字」的独立数据源）。

        取调度器的 `status_of`：`enabled` 且 `ocr == "active"`（OCR 正在采样）。
        CC 是整轨一次落库，`cc=ok` 表示已收完、不代表还在收，所以不参与判定。
        这是只读派生字段，不落库、不加列 —— 前端不再依赖「该会话被展开」才有
        的 `pick.sources`（否则未展开会话的黄字永远不出现）。
        """
        status = self.scheduler.status_of(int(session_id))
        return bool(status.get("enabled")) and str(status.get("ocr") or "") == "active"

    def _api_session_backlog_storage(self, raw_id: str) -> Response:
        """`GET /api/sessions/{id}/backlog/storage`（v35 #227）：该会话底图占用。

        回包：`{ok, images, bytes, picked_images, unpicked_images}`。`bytes` 只数
        目录里真实存在的文件（缺图不虚报）；会话不存在 404、id 非法 400。
        """
        session_id = _int_or_none(raw_id)
        if session_id is None:
            return error_response(400, "id 必须是整数")
        if self.store.get_session(session_id) is None:
            return error_response(404, "not found")
        info = self._band_storage()["sessions"].get(session_id) or {
            "images": 0,
            "bytes": 0,
            "picked_images": 0,
            "unpicked_images": 0,
        }
        return json_response({"ok": True, **info})

    def _api_backlog_storage(self) -> Response:
        """`GET /api/backlog/storage`（v35 #227 加读端点）：池页顶栏 / 分组的数据源。

        spec 只写了「按会话」的 storage 端点；池页顶部总占用与「已挑完的视频」分组
        需要一次拿全所有会话的占用（否则要 N 次请求），所以加这一个**只读**端点，
        语义与按会话口径一致：`total` 是底图目录里白名单文件的总占用（含少量孤儿，
        自动清理负责收），`sessions` 只列被行引用到的会话。
        """
        storage = self._band_storage()
        sessions = [
            {
                "session_id": session_id,
                **info,
                # v35 #228 修复：收句态随 storage 快照一起下发，确认面板不再依赖
                # 「该会话已被展开」才有的 pick.sources（只读派生，不加库列）。
                "capturing": self._session_capturing(session_id),
            }
            for session_id, info in sorted(storage["sessions"].items())
        ]
        return json_response(
            {
                "ok": True,
                "images": storage["images"],
                "bytes": storage["bytes"],
                # v35 #235：80% 黄字提示用的上限（0 = 该规则关闭，池页不提示）
                "limit_bytes": self._band_max_bytes(),
                "sessions": sessions,
            }
        )

    def _api_session_backlog_cleanup(self, raw_id: str, body: Any) -> Response:
        """`POST /api/sessions/{id}/backlog/cleanup`（v35 #227 / spec §手动清理 API）。

        `mode="images"`：该会话的底图文件删掉、`image_path` 置 NULL，**行与词条不动**
        （之后仍可挑词；单句重识别因缺图返回 409）。
        `mode="all"`：该会话的全部待选句行连图一起删（会话与词条不动，供 #228 的危险
        按钮用）。

        顺序：**先置空 / 删行（提交）再 unlink**（spec 规则 6）—— 崩溃最多留下孤儿
        文件，下一轮自动清理能扫掉。只删「该会话行引用到 + 白名单文件名 + 在底图
        目录内」的文件；重复调用返回 0 计数（幂等）。会话不存在 404、mode 非法 400。
        """
        session_id = _int_or_none(raw_id)
        if session_id is None:
            return error_response(400, "id 必须是整数")
        if self.store.get_session(session_id) is None:
            return error_response(404, "not found")
        if not isinstance(body, dict):
            return error_response(400, "body 必须是 JSON 对象")
        mode = body.get("mode")
        if mode not in ("images", "all"):
            return error_response(400, "bad mode")
        rows = self.store.subtitle_sentences(session_id)
        names = {
            name
            for name in (band_cleanup.band_name_of(row.get("image_path")) for row in rows)
            if name is not None
        }
        if mode == "all":
            deleted_rows = self.store.delete_subtitle_lines(session_id)
        else:
            image_ids = [
                int(row["id"])
                for row in rows
                if band_cleanup.band_name_of(row.get("image_path")) is not None
            ]
            if image_ids:
                self.store.clear_band_image_paths(image_ids)
            deleted_rows = 0
        deleted_images, freed_bytes = self._band_delete_files(names)
        kept_rows = len(self.store.subtitle_sentences(session_id))
        log.info(
            "手动清理底图（会话 %d，%s）：删 %d 张/%.1f MB、删句 %d、保留 %d 句",
            session_id,
            mode,
            deleted_images,
            freed_bytes / 1024 / 1024,
            deleted_rows,
            kept_rows,
        )
        return json_response(
            {
                "ok": True,
                "mode": mode,
                "deleted_images": deleted_images,
                "freed_bytes": freed_bytes,
                "deleted_rows": deleted_rows,
                "kept_rows": kept_rows,
            }
        )

    # --- 单句重识别（v33 修复轮 / spec §D19）-----------------------------------

    def reocr_band(self, band: Any) -> list[tuple[str, float]]:
        """字幕带 → `[(text, conf), ...]`：单句重识别的推理缝。

        v33 P0（票 #211 首发崩溃修复）：生产走 worker 子进程代理
        （`self._band_ocr`，与调度器采样共享同一个子进程——hub 进程内绝不能起
        onnxruntime，见 `ocr_sampler` worker 节头注释）；测试在构造时传
        `band_ocr=` 假件，全程不跑模型。代理不可用（连续 spawn 失败）→
        RuntimeError → 端点翻 500。
        """
        engine = self._band_ocr
        if engine is None:
            raise RuntimeError("band ocr unavailable")
        return [(str(text), float(conf)) for text, conf in engine(band)]

    def _load_band_image(self, image_path: str) -> Any:
        """句上的底图路径 → ndarray（`subtitle_bands` 沙箱内）；越界/缺失/坏图回 None。

        D7：根解析统一走 `_band_image_resolve`（db 旁目录优先 + 兜底根按文件名
        兜底）—— 旧句的绝对路径指向老目录也能读到。返回 ndarray 而不是 PIL：
        rapidocr 1.2.3 的 LoadImage 不认 PIL Image（生产抓带 `kernel.grab_screen`
        给的本就是 ndarray，同一形状）。
        """
        candidate = self._band_image_resolve(image_path)
        if candidate is None:
            return None
        try:
            import numpy as np
            from PIL import Image

            with Image.open(candidate) as image:
                return np.asarray(image.convert("RGB"))
        except Exception as exc:  # noqa: BLE001 - 坏图当无底图处理（明确报错给前端）
            log.warning("字幕带底图读取失败（%s）", type(exc).__name__)
            return None

    def _api_session_backlog_reocr(self, raw_id: str, raw_sentence: str) -> Response:
        """`POST /api/sessions/{id}/backlog/{sentence_id}/reocr`（spec §D19 三层纠错的「单句重识别」层）。

        取该句的字幕带底图重跑 OCR → 更新 text/score/suspicious、重算
        sentence_key（走 `store.update_subtitle_ocr`，原句变了 = 已挑标记清除，
        与看图改句同一语义）。多行结果按行序空格拼接、置信度取均值。

        回包：`{ok, text, score, suspicious, sentence_key}`。
        无底图 / 底图文件缺失（TTL 清掉）→ 409 明确报错（不猜不静默）；推理缝
        不可用 → 500。
        """
        session_id = _int_or_none(raw_id)
        sentence_id = _int_or_none(raw_sentence)
        if session_id is None or sentence_id is None:
            return error_response(400, "id 必须是整数")
        if self.store.get_session(session_id) is None:
            return error_response(404, "not found")
        sentence = self.store.subtitle_sentence(session_id, sentence_id)
        if sentence is None:
            return error_response(404, "not found")
        image_path = str(sentence["image_path"] or "").strip()
        if not image_path:
            return error_response(409, "no image")  # CC 句 / 底图落盘失败的句
        band = self._load_band_image(image_path)
        if band is None:
            return error_response(409, "image missing")  # 底图被 TTL 清掉等
        try:
            lines = self.reocr_band(band)
        except Exception as exc:  # noqa: BLE001 - 推理故障明确报错，不静默
            log.warning("单句重识别推理失败（%s）", type(exc).__name__)
            return error_response(500, "ocr unavailable")
        text = one_line(" ".join(str(part) for part, _conf in lines if str(part).strip()))
        # #222：与采样落库同一套后处理（标点伪影 → 粘连），重识别按钮才救得回来。
        text = corrections.clean_ocr_punctuation(text)
        try:
            known, common = kernel.correction_wordlists()
        except Exception:  # noqa: BLE001 - 词表只影响后处理，读不到照常回写
            known, common = frozenset(), frozenset()
        if known:
            text = one_line(corrections.restore_spaces(text, known, common))
        if not text:
            return error_response(409, "ocr empty")  # 底图上没读出任何东西
        if not is_latin_line(text):
            # 2026-11-10 真机复核：重识别是拿**底图**重跑 OCR，噪音天生比改句多
            # （Lindt 包装上的一行中文品名就够把整行判成「非拉丁」→ 用户点了
            # 「重识别」只看到一个 `ocr text is not latin`，功能形同废掉）。
            # 这里加一道**次级门**：与采样侧同一个拉丁主导口径（latin_dominant：
            # CJK ≤1 且拉丁占比 >0.7）能过就照常回写；过不了才拒（真的是中文行）。
            if not ocr_sampler.latin_dominant(text):
                log.info(
                    "单句重识别被拒：读出非拉丁行（句 %d，拉丁占比不足 70%%）",
                    sentence_id,
                )
                return error_response(400, "ocr text is not latin")
            log.info("单句重识别放行：含少量非拉丁噪声（句 %d）", sentence_id)
        score = statistics.fmean(conf for _part, conf in lines) if lines else None
        try:
            row = self.store.update_subtitle_ocr(
                session_id,
                sentence_id,
                text=text,
                score=score,
                suspicious=self._subtitle_suspicious(text),
            )
        except ValueError as exc:
            return error_response(400, str(exc))
        if row is None:
            return error_response(404, "not found")
        log.info("单句重识别完成（句 %d，%d 行，score %.3f）", sentence_id, len(lines), score or 0.0)
        return json_response(
            {
                "ok": True,
                "text": row["text"],
                "score": row["score"],
                "suspicious": row["suspicious"],
                "sentence_key": row["sentence_key"],
            }
        )

    @staticmethod
    def _subtitle_suspicious(text: str) -> bool:
        """可疑标重判（与采样器 flush 同一门）：#222 起用宽词表 OOV 检出，
        词表不可用时回落旧的命中率比值门。"""
        import kernel

        try:
            known, _common = kernel.correction_wordlists()
        except Exception:  # noqa: BLE001
            known = frozenset()
        if known:
            return corrections.looks_suspicious(text, known)
        return kernel.text_score(text, kernel.load_wordlist())[0] < kernel._SCORE_TRUST

    def _api_backlog_image(self, query: dict[str, list[str]]) -> Response:
        """`GET /api/backlog/image?path=<文件名>`（v33 T4，票 #210 / spec §C14）。

        字幕带底图的只读端点：只 serve 底图根（D7：db 旁目录优先，兜底根兜底）
        下的 `.png`。`path` 只认**裸文件名**（T2 落盘是平铺的 `s<会话>_f<帧>.png`；
        前端从 image_path 取文件名来取）—— 文件名正则先把目录分隔符 / `..` /
        盘符 / 空字节全部挡掉（../ 穿越、绝对路径、子目录一律 400），再做一次
        resolve 包含校验兜底；两个根都没有 404。零写操作、零目录列举。
        """
        name = first_param(query, "path") or ""
        if not _BACKLOG_IMAGE_NAME.match(name):
            return error_response(400, "bad path")
        for root in self._band_roots():
            candidate = (root / name).resolve()
            if candidate.parent != root:
                return error_response(400, "bad path")
            if candidate.is_file():
                return Response(200, candidate.read_bytes(), "image/png")
        return error_response(404, "not found")

    def _api_lookup(self, body: Any) -> Response:
        word = (
            str((body or {}).get("word") or "").strip()
            if isinstance(body, dict)
            else ""
        )
        if not word:
            return error_response(400, "word 必填")
        return json_response(self.lookup(word))

    def _api_dict_refill(self, body: Any) -> Response:
        """`POST /api/dict/refill`（v23 §4.1）：展开候选、入队、立刻回 `job_id`。

        错误码按 spec：`bad scope` / `bad limit` / `no records`（400），
        已有 HTTP 任务在跑 → `refill running`（409）。后台补齐条目**不算**「任务在跑」
        —— 任务表只认 `refill_jobs` 里的 HTTP 任务。
        """
        payload = body if isinstance(body, dict) else {}
        scope = payload.get("scope")
        if scope not in REFILL_SCOPES:
            return error_response(400, "bad scope")
        limit = payload.get("limit", REFILL_LIMIT)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            return error_response(400, "bad limit")
        # spec §4.1：200 是防呆**上限** —— 传得再大也只取 200 条（不是接受无界值）。
        limit = min(limit, REFILL_LIMIT)
        if self.refill_jobs.running() is not None:
            return error_response(409, "refill running")
        if scope == "records":
            raw_ids = payload.get("ids")
            ids = (
                [
                    value
                    for value in raw_ids
                    if isinstance(value, int) and not isinstance(value, bool)
                ]
                if isinstance(raw_ids, list)
                else []
            )
            candidates = self.store.refill_candidates(ids=ids, limit=limit)
        elif scope == "session":
            session_id = _payload_int(payload.get("id"))
            candidates = (
                self.store.refill_candidates(session_id=session_id, limit=limit)
                if session_id is not None
                else []
            )
        else:  # article
            doc_id = _payload_int(payload.get("id"))
            candidates = (
                self.store.refill_candidates(doc_id=doc_id, limit=limit)
                if doc_id is not None
                else []
            )
        if not candidates:
            return error_response(400, "no records")
        job = self.refill_jobs.start_if_idle()
        if job is None:  # 与 `running()` 之间挤进来的第二个 POST
            return error_response(409, "refill running")
        for row in candidates:
            # 队列满（500）时后面几条直接不入队；`total` 只统计**实际入队**的条数。
            # v34 T1（spec D26）：手动补释义是用户确认过的目标，绕过碎片门禁。
            self.meaning_fill.submit(row["id"], row["target"], job=job, trusted=True)
        job.close()
        total = int(job.snapshot()["total"])
        log.info("补释义任务入队：%d 条（scope=%s）", total, scope)
        return json_response({"ok": True, "job_id": job.job_id, "total": total})

    def _api_dict_refill_get(self, job_id: str) -> Response:
        """`GET /api/dict/refill/{job_id}`：进度 + 失败清单（目标词只在回包，不进日志）。"""
        job = self.refill_jobs.get(job_id)
        if job is None:
            return error_response(404, "not found")
        return json_response({"ok": True, **job.snapshot()})

    def _api_dict_recheck(self, body: Any) -> Response:
        """`POST /api/dict/recheck`（v29）：单条重查，**只预览不写库**。

        - `fresh=True` 绕过 `dict_cache`（错误旧义可能就钉在缓存里），本地 + 在线重查；
        - 回包身份不符（源返回别的词）→ `word-mismatch`，绝不写错义；
        - 成功只回一个内存 token + 旧/新释义；真正写库走 `/apply`。
        """
        payload = body if isinstance(body, dict) else {}
        record_id = _payload_int(payload.get("id"))
        if record_id is None:
            return error_response(400, "id 必须是整数")
        record = self.store.get_record(record_id)
        if record is None:
            # 与「路由不存在」的 404 区分：旧中枢没有 /recheck 路由，回的是
            # `not found`；前端据此提示「重启英语取词」，而不是误报词典失败。
            return error_response(404, "record not found")
        target = one_line(record.get("target"))
        if not target:
            return error_response(400, "target 为空")
        result = self.dict_service.lookup(
            target, dict_only=(" " in target), fresh=True
        )
        reason = meaning_miss_reason(
            target,
            result,
            online_enabled=getattr(self.dict_service, "online_enabled", True),
        )
        if reason is not None:
            log.info("重查未写入（reason=%s，%d 字）", reason, len(target))
            return json_response({"ok": False, "reason": reason})
        meaning = meaning_text(result)
        preview = self.recheck_previews.put(
            record=record,
            target=target,
            meaning=meaning,
            phonetic=one_line(result.get("phonetic")),
            source=one_line(result.get("source")),
            machine=bool(result.get("machine")),
            dict_json=json.dumps(result, ensure_ascii=False),
        )
        log.info("重查预览（%d 字，source=%s）", len(target), preview.source)
        return json_response(
            {
                "ok": True,
                "token": preview.token,
                "record_id": preview.record_id,
                "target": target,
                "old": {
                    "meaning": preview.old_meaning,
                    "phonetic": preview.old_phonetic,
                },
                "new": {
                    "meaning": preview.meaning,
                    "phonetic": preview.phonetic,
                    "source": preview.source,
                    "machine": preview.machine,
                },
                "same": preview.old_meaning == preview.meaning,
            }
        )

    def _api_dict_recheck_apply(self, body: Any) -> Response:
        """`POST /api/dict/recheck/apply`（v29）：确认预览并覆盖这一条。

        预览后用户又手改过释义/音标 → 409 `record changed`（不覆盖更新的内容）；
        token 过期 / 不属于这条记录 → 404 `recheck expired`。
        """
        payload = body if isinstance(body, dict) else {}
        record_id = _payload_int(payload.get("id"))
        if record_id is None:
            return error_response(400, "id 必须是整数")
        token = payload.get("token")
        if not isinstance(token, str) or not token.strip():
            return error_response(400, "token 必填")
        preview = self.recheck_previews.pop(token.strip(), record_id)
        if preview is None:
            return error_response(404, "recheck expired")
        record = self.store.get_record(record_id)
        if record is None:
            return error_response(404, "record not found")
        if (
            not _same_word(record.get("target"), preview.target)
            or one_line(record.get("meaning")) != preview.old_meaning
            or one_line(record.get("phonetic")) != preview.old_phonetic
        ):
            return error_response(409, "record changed")
        written = self.store.refill_meaning(
            record_id,
            meaning=preview.meaning,
            phonetic=preview.phonetic,
            dict_json=preview.dict_json,
            machine=preview.machine,
            expect_empty=False,
            meaning_source="recheck",
        )
        if not written:
            return error_response(404, "not found")
        log.info("重查释义写入（%d 字）", len(preview.meaning))
        fresh = self.store.get_record(record_id)
        return json_response({"ok": True, "record": fresh})

    def _api_dict_pending(self, query: dict[str, list[str]]) -> Response:
        """`GET /api/dict/pending?scope=session|article&id=N`（v28）。

        取词台打开会话 / 文章时的提示条要一个**精确**的空释义数 —— 前端已载入的
        记录有 500 条上限，不能拿它近似。筛选口径与 `POST /api/dict/refill` 相同，
        只读计数、不建任务。
        """
        scope = first_param(query, "scope")
        if scope not in ("session", "article"):
            return error_response(400, "bad scope")
        raw_id = first_param(query, "id")
        record_id = _int_or_none(raw_id)
        if record_id is None:
            return error_response(400, "bad id")
        empty = (
            self.store.refill_pending_count(session_id=record_id)
            if scope == "session"
            else self.store.refill_pending_count(doc_id=record_id)
        )
        return json_response(
            {"ok": True, "scope": scope, "id": record_id, "empty": empty}
        )

    def _api_browser_context_post(self, body: Any, *, user_agent: str = "") -> Response:
        """Edge 扩展的上报入口（§2.2）。body 是 JSON（Content-Type 可能是 `text/plain`
        ——简单请求免预检，见 §2.2；`_read_body` 不看 Content-Type，所以天然支持）。

        共享令牌的校验在 `_handle` 入口做（票 #179：无 / 错令牌到不了这里）。
        只校验「是 JSON 对象 + 有非空 href」；URL 合法性与采纳规则在 §2.4 的
        `adopt_context` 里把关（扩展是本地可信进程，不做二次筛）。

        v33 T5（票 #211）：`user_agent` 由 HTTP 层原样递进来（Hub 方法不碰请求头），
        在这里映射浏览器友好名（Edge/Chrome/Firefox），供调度器建会话时与外壳取词
        的 session_key（FriendlyApp）对齐。
        """
        if not isinstance(body, dict):
            return error_response(400, "body 必须是 JSON 对象")
        href = body.get("href")
        if not isinstance(href, str) or not href.strip():
            return error_response(400, "缺少 href")
        if not self._ctx_rate_allow():
            # 限流掉的心跳不回错误（扩展侧不该因此重试），只是不处理。
            return json_response({"ok": True})
        app_hint = capture_scheduler.browser_app_from_ua(user_agent)
        self.set_browser_context(dict(body), app_hint=app_hint)
        # v36（场景识别）：扩展下行指令随心跳回包下发（take-one）—— 槽里 href 与
        # 这条心跳 href 逐字相等才发放并清槽；无指令 / href 不等就是 `{"ok": True}`。
        cmd = self._take_ext_cmd_for(href)
        if cmd is not None:
            log.info("下行指令随心跳下发：%s", cmd["cmd"])
            return json_response({"ok": True, **cmd})
        return json_response({"ok": True})

    def _api_browser_context_get(self) -> Response:
        """调试 / 前端只读：`{"ok","context","age_ms"}`（§2.3）。

        v36（#240）加 `extension_version`（仓库内 `extension/manifest.json` 的版本）：
        与 `context.extVersion`（浏览器侧上报）并排，一眼看出「浏览器侧跑的是不是
        仓库里那版代码」——不一致就说明扩展改过但没手动重载。`ext_version` 是 hub
        **记录的最新上报值**（上下文过期也还在），比 `context` 更适合比对。
        """
        context, age_ms = self.browser_context()
        return json_response(
            {
                "ok": True,
                "context": context,
                "age_ms": age_ms,
                "ext_version": self._ext_version,
                "extension_version": extension_version(),
            }
        )

    def _api_export(self, query: dict[str, list[str]]) -> Response:
        fmt = (first_param(query, "format") or "json").lower()
        if fmt in ("md", "markdown"):
            return Response(
                200,
                self.store.export_markdown().encode("utf-8"),
                "text/markdown; charset=utf-8",
            )
        return json_response({"ok": True, **self.store.export_payload()})

    # --- 设置与热键状态（票 #32；规格 §8.4 §9）-----------------------------

    def _api_hotkeys(self) -> Response:
        """外壳写的探测结果（§9.2）。文件不在 / 坏掉时 status 为 null，前端显示「未探测」。

        v34 T2（#215）：连带算一份时效 —— 5 秒轮询里重探成功后，页面才能自己把
        「旧键位」提示撤掉，而不必等整页刷新。
        """
        status = read_hotkey_status(self.hotkey_status_path)
        hotkeys = settings_view(read_config(self.config_path))["hotkeys"]
        return json_response(
            {
                "ok": True,
                "path": str(self.hotkey_status_path),
                "status": status,
                "hotkey_freshness": hotkey_status_freshness(status, hotkeys),
            }
        )

    def _api_settings(self) -> Response:
        """设置页要的全部只读信息：当前值 + 动作表 + 数据区 + 热键探测状态。"""
        config = read_config(self.config_path)
        view = settings_view(config)
        status = read_hotkey_status(self.hotkey_status_path)
        db_path = Path(self.store.db_path)
        try:
            db_bytes = db_path.stat().st_size
        except OSError:
            db_bytes = 0
        counts = self.store.counts()
        return json_response(
            {
                "ok": True,
                **view,
                "actions": [
                    {
                        "key": key,
                        "label": label,
                        "scope": scope,
                        "default": HOTKEY_DEFAULTS[key],
                    }
                    for key, label, scope in HOTKEY_ACTIONS
                ],
                "config_path": str(self.config_path),
                "data": {
                    "db_path": str(db_path),
                    "db_bytes": db_bytes,
                    "records": counts["records"],
                    "sessions": counts["sessions"],
                },
                # v5 §7：浏览器链接与停播联动卡片（只放旗标/站点短名，无 URL / 标题）
                "browser": self.browser_status(),
                "hotkey_status": status,
                # v34 T2（#215）：快照时效（旧键位 / 旧快照 / 未探测），页面据此不再假装绿
                "hotkey_freshness": hotkey_status_freshness(status, view["hotkeys"]),
                # v11 §4（票 #112）：只读发音状态（不含任何文本）；可写项只有 tts_online
                "tts": self.speech.status(),
                # 票 #116：设置页音色下拉的 17 个选项（id/展示名/性别）
                "tts_voices": self.speech.voices(),
            }
        )

    def _api_settings_put(self, body: Any) -> Response:
        """校验 → 行级编辑 config.ini → 原子写。校验不过一律不碰文件。"""
        if not isinstance(body, dict):
            return error_response(400, "body 必须是 JSON 对象")
        # v34 T2（#215）：跨动作重复校验要把「这次没动的动作」也看见 ——
        # 有效键位 = config 现值叠加提交上来的那些。
        current = settings_view(read_config(self.config_path))["hotkeys"]
        updates, errors = validate(body, current=current)
        if errors:
            return json_response(
                {"ok": False, "error": "有不合法的设置项", "errors": errors}, 400
            )
        if not updates:
            return error_response(400, "没有可改的设置项")
        try:
            applied = apply_config(self.config_path, updates)
        except (OSError, ValueError) as exc:
            log.error("写 config.ini 失败：%s", type(exc).__name__)
            return error_response(500, f"写 config.ini 失败：{exc}")
        # v5 §8：写回后重读两个开关，立即生效（不必重启 hub）
        self.reload_switches()
        return json_response(
            {
                "ok": True,
                "applied": applied,
                "settings": settings_view(read_config(self.config_path)),
            }
        )

    def static(self, path: str) -> Response:
        """`web/` 静态托管：`/` → `index.html`；越界路径（`../`）一律 404。"""
        relative = urllib.parse.unquote(path).lstrip("/") or "index.html"
        root = self.web_dir.resolve()
        candidate = (root / relative).resolve()
        if root not in candidate.parents and candidate != root:
            return Response(404, b"not found", "text/plain; charset=utf-8")
        if not candidate.is_file():
            return Response(404, b"not found", "text/plain; charset=utf-8")
        return Response(
            200,
            candidate.read_bytes(),
            _MIME.get(candidate.suffix.lower(), "application/octet-stream"),
        )


def _int_or_none(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _payload_int(value: Any) -> int | None:
    """JSON body 里的整数（`bool` 是 `int` 的子类，必须排掉）。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _paragraph_anomaly(body: str) -> str:
    """段落异常码：`empty`（空段）/ `too_long`（> `PARAGRAPH_MAX_CHARS`）；正常 → 空串。

    只由**现有段文本**推导（spec §9），不猜类型、不引入统计启发式。
    """
    if not body:
        return "empty"
    if len(body) > PARAGRAPH_MAX_CHARS:
        return "too_long"
    return ""


def _find_target(text: str, needle: str, start: int = 0) -> int:
    """在 `text[start:]` 里找 `needle`：标点归一 → 整词优先、大小写不敏感；不中退裸子串。

    标点归一复用 `article.normalize_punct`（与 `ArticleIndex` 同一张 1:1 映射表：
    弯引号→`'`、长连字符→`-`），**字符数不变**，所以返回值可直接当原文偏移用。
    前端 `findTargetSpan` 的 ①②③ 前两档是这里的匹配口径（③ 是前端兜底）。

    这里**不复用** `ArticleIndex.locate()`：那个是「target 词 → 命中句 + 排名（T0–T4
    档位、大小写、段距）」的外壳交互语义；段落卡要的是「给定 context 句里的 target
    位置」，两者对 target 的命中排序/消歧不同。复用它的归一化与句子定位口径即可。
    返回**绝对下标**，找不到 → -1。
    """
    if not text or not needle:
        return -1
    plain = normalize_punct(text)
    plain_needle = normalize_punct(needle)
    if not plain_needle:
        return -1
    match = re.search(rf"\b{re.escape(plain_needle)}\b", plain[start:], re.IGNORECASE)
    if match:
        return start + match.start()
    # 票 #186（H11）：兜底折叠只折 ASCII —— `str.lower()` 对个别字符非 1:1（如
    # `İ` → `i̇` 两个字符），变长坐标系下 find 返回的下标在原文里就是错位的高亮。
    # `article._ascii_lower` 与 `ArticleIndex` 同口径：逐字符 1:1，偏移不变。
    return _ascii_lower(plain).find(_ascii_lower(plain_needle), start)


def _age_seconds(value: Any) -> float | None:
    """`at`（epoch 秒）距今多少秒；坏值/缺失 → None。"""
    try:
        return time.time() - float(value)
    except (TypeError, ValueError):
        return None


def first_param(
    query: dict[str, list[str]], key: str, default: str | None = None
) -> str | None:
    values = query.get(key)
    if not values:
        return default
    return values[0]


# --- 两个服务：RPC（新行 JSON）与 HTTP --------------------------------------


class _RpcHandler(socketserver.StreamRequestHandler):
    """与 `kernel.py` 同款：一行一条请求，处理完立刻回一行。

    票 #186（H8）：`timeout` 兜底死连接；行读取带上限，超限回 bad json 并断开。
    """

    timeout = HANDLER_TIMEOUT_S  # setup 时 settimeout 到连接上

    def handle(self) -> None:
        while True:
            try:
                # 上限 +1：读完 `MAX_RPC_LINE_BYTES` 仍不见换行 → 这行超限
                line = self.rfile.readline(MAX_RPC_LINE_BYTES + 1)
            except OSError:
                # 含 socket.timeout（对端超时没说话）与半途断开：关连接走人
                return
            if not line:
                return
            if len(line) > MAX_RPC_LINE_BYTES:
                log.warning("RPC 行超长（> %d 字节），断开", MAX_RPC_LINE_BYTES)
                try:
                    self._send({"ok": False, "error": "bad json"})
                except OSError:
                    pass  # 对端已经走了，回话失败同样关连接
                return
            self._answer(line)
            if not line.endswith(b"\n"):
                return

    def _answer(self, raw: bytes) -> None:
        request_id: Any = None
        try:
            request = json.loads(raw.decode("utf-8"))
            if isinstance(request, dict):
                request_id = request.get(RPC_ID_KEY)
        except (UnicodeDecodeError, json.JSONDecodeError):
            # 只记字节数：坏行可能是被截断的 lookup，里面带着用户的词/句（README「日志」）。
            log.warning("RPC bad json（%d 字节）", len(raw))
            self._send({"ok": False, "error": "bad json"})
            return
        try:
            response = self.server.hub.rpc(request)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 - 单个请求坏掉不许打断连接与其它线程
            log.error("RPC 处理出错：%s", type(exc).__name__)
            response = {"ok": False, "error": "internal"}
        # 票 #178：回话自带请求编号，外壳按它对号入座（坏 json 拿不到编号，不回显）。
        # 用 `_rpc_id` 而不是 `id`：`article_create` 的回包本来就有 `"id": doc_id`，
        # `article_paragraphs` 的条目里也有 `"id"` —— 重名会被外壳解析到错误那一个。
        if request_id is not None:
            response[RPC_ID_KEY] = request_id
        self._send(response)

    def _send(self, response: Any) -> None:
        self.wfile.write(
            json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n"
        )
        self.wfile.flush()


class HubRpcServer(socketserver.ThreadingTCPServer):
    """一连接一线程：`lookup` 卡在网络上也冻不住 `ping` / `flush`。"""

    allow_reuse_address = False  # 端口上有外来者就报错，别静默抢（同 kernel）
    daemon_threads = True

    def __init__(self, address: tuple[str, int], hub: Hub) -> None:
        self.hub = hub
        super().__init__(address, _RpcHandler)


class _BadBody(Exception):
    """请求体不合法（太大 / 不是 JSON）。"""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class _HttpHandler(BaseHTTPRequestHandler):
    server_version = "hub/2.0"
    protocol_version = "HTTP/1.1"
    # 票 #186（H8）：连接兜底超时 —— idle keep-alive 到点由 http.server 干净关闭
    #（TimeoutError → close_connection），客户端下一条请求重连即可。
    timeout = HANDLER_TIMEOUT_S

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_PATCH(self) -> None:
        self._handle("PATCH")

    def do_DELETE(self) -> None:
        self._handle("DELETE")

    def do_PUT(self) -> None:
        self._handle("PUT")  # 设置页保存（票 #32）

    def _handle(self, method: str) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        started = time.perf_counter()
        hub: Hub = self.server.hub  # type: ignore[attr-defined]
        # 票 #179（S2）：入口统一校验 Host 头 ∈ {127.0.0.1:<port>, localhost:<port>}
        # （端口取实际绑定值，测试的临时端口同样成立）。不匹配 → 400：DNS rebinding
        # （恶意页把子域 A 记录重绑到 127.0.0.1，请求的 Host 是攻击者域名）在这一条
        # 整档；静态页与 /api/* 全覆盖。域名大小写不敏感（RFC 9110）。
        port = self.server.server_address[1]
        host = (self.headers.get("Host") or "").strip().lower()
        if host not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            response = error_response(400, "bad host")
            self.close_connection = True  # body 没读就回话，keep-alive 连接必须关
        else:
            try:
                if (
                    parsed.path == "/api/browser/context"
                    and method == "POST"
                    and self.headers.get("X-Hub-Token", "") != hub.extension_token
                ):
                    # 票 #179（S1）：跨站表单式 POST 的 Host 本来就是 127.0.0.1:<port>，
                    # Host 校验挡不住它 —— 这条上报只认带共享令牌的扩展。日志只记
                    # 「令牌不符」，不记头值与 body（v23 A5 脱敏纪律）。
                    log.info("browser/context 令牌不符，已拒绝")
                    response = error_response(401, "令牌不符")
                    self.close_connection = True  # body 没读，同上
                else:
                    body = self._read_body()
                    if parsed.path.startswith("/api/"):
                        response = hub.api(
                            method,
                            parsed.path,
                            urllib.parse.parse_qs(parsed.query),
                            body,
                            # v33 T5（票 #211）：心跳的浏览器友好名从 UA 映射（只此一用处）
                            user_agent=str(self.headers.get("User-Agent") or ""),
                        )
                    else:
                        response = hub.static(parsed.path)
            except _BadBody as exc:
                response = error_response(exc.status, str(exc))
                self.close_connection = True
            except Exception as exc:  # noqa: BLE001 - 任何异常都不许把服务打挂
                log.error("HTTP 处理失败：%s", type(exc).__name__)
                response = error_response(500, "internal")
        try:
            self._send(response)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError) as exc:
            # 2026-11-06：以前这里静默 return，连访问行都不写 —— 前端请求超时砍掉
            # /api/tts 后，服务端「合成完了但没人接」在日志里彻底消失，中断无从查起。
            # 现在补一条 WARNING：只有路径 + 状态 + 耗时 + 异常类型，没有任何用户内容。
            log.warning(
                "%s %s %d %dms 响应写回失败（客户端断开：%s）",
                method,
                parsed.path,
                response.status,
                round_ms(started),
                type(exc).__name__,
            )
            return
        elapsed_ms = round_ms(started)
        # 访问日志只记路径（丢掉查询串：搜索词也是用户内容，§10）
        log.info("%s %s %d %dms", method, parsed.path, response.status, elapsed_ms)
        # 慢请求预警：/api/tts 由 _api_tts 自己带元数据报警，避免同一件事写两行。
        if elapsed_ms >= SLOW_REQUEST_MS and parsed.path != "/api/tts":
            log.warning("%s %s %d %dms 慢请求", method, parsed.path, response.status, elapsed_ms)

    def _read_body(self) -> Any:
        # 票 #181 H5：Content-Length 非整数单独拦成 400 —— 原来落到 `int()` 的
        # ValueError 进兜底 500，且 body 没读、连接没关，keep-alive 下一条请求会把
        # 上一条的 body 当请求行解析，连环错位。`_BadBody` 统一 `close_connection=True`。
        if self.headers.get("Transfer-Encoding"):
            # 同票：chunked（hub 的客户端——外壳 Socket.ahk / 扩展 / 网页——都带
            # Content-Length，没人用 chunked）不再静默当空 body：留着的分块字节同样
            # 会污染 keep-alive 流，直接 400 关连接。
            raise _BadBody(400, "unsupported transfer-encoding")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise _BadBody(400, "bad content-length") from None
        if length <= 0:
            return None
        if length > MAX_BODY_BYTES:
            raise _BadBody(413, "body 太大")
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise _BadBody(400, "bad json") from None

    def _send(self, response: Response) -> None:
        self.send_response(response.status)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(response.body)))
        for name, value in response.headers.items():
            self.send_header(name, value)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(response.body)

    def log_message(self, format: str, *args: Any) -> None:
        """父类默认往 stderr 打「GET /api/records?query=宿便」——把整条访问日志掐掉。"""


class HubHttpServer(ThreadingHTTPServer):
    """HTTP 47811：`/api/*` + `web/` 静态。"""

    allow_reuse_address = False
    daemon_threads = True

    def __init__(self, address: tuple[str, int], hub: Hub) -> None:
        self.hub = hub
        super().__init__(address, _HttpHandler)


class SpoolWatcher(threading.Thread):
    """每 `interval` 秒 ingest 一次 `data/inbox.jsonl`（§2）。

    票 #113：可选的 `on_ingest(targets)` 收到**本轮新入库**的 target（只给后台预热用），
    回调坏一次不许把线程打死 —— 定时入库是保存流程的一部分，预热只是加速。

    v23 §4.2：`ingest` 可注入入库动作（默认 `store.ingest_inbox`）；生产传
    `hub.ingest_inbox`，让定时入库也带上补释义触发与 TTS 预热（那时不要再传
    `on_ingest`，否则同一批 target 会预热两次）。
    """

    def __init__(
        self,
        store: Store,
        interval: float = INGEST_INTERVAL_S,
        on_ingest: Callable[[list[str]], Any] | None = None,
        ingest: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(name="spool-watcher", daemon=True)
        self.store = store
        self.interval = interval
        self.on_ingest = on_ingest
        self.ingest = ingest if ingest is not None else store.ingest_inbox
        # v33 P0 附带修复：名字必须是 `_stop_event` —— `_stop` 会**覆盖**
        # `threading.Thread._stop()` 方法（CPython 的 join 在线程退出中会调它），
        # `join()` 一旦赶上线程还在退出中就 TypeError('Event' object is not
        # callable)。测试里 7 条 watcher 用例随机器负载偶发/高发失败即此
        # （v33 T5 首诊、本票 A/B 实证：基线同批用例同负载同红）。
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.wait(self.interval):
            try:
                result = self.ingest()
            except Exception as exc:  # noqa: BLE001 - 定时任务坏一次不许把线程打死
                log.error("spool 定时入库失败：%s", type(exc).__name__)
                continue
            targets = result.get("targets") or []
            if not targets or self.on_ingest is None:
                continue
            try:
                self.on_ingest(targets)
            except Exception as exc:  # noqa: BLE001 - 预热回调坏了不影响入库
                log.error("tts 预热回调失败：%s", type(exc).__name__)

    def stop(self) -> None:
        self._stop_event.set()


# --- 组装与 CLI -------------------------------------------------------------


def build_dict_service(
    store: Store,
    *,
    dict_db: Path,
    online_enabled: bool,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> DictService:
    """缓存（`store`）→ 本地 ECDICT（存在才用）→ 在线两个源（§7）。"""
    local: list[EcdictProvider] = []
    if Path(dict_db).is_file():
        local.append(EcdictProvider(dict_db))
    else:
        log.info("本地词典不在（%s），走缓存与在线源", Path(dict_db).name)
    return DictService(
        cache_get=store.cache_get,
        cache_put=store.cache_put,
        local=local,
        # v18 W3a：在线链 = 有道 jsonapi_s → Edge → transmart（后两者仅短语，#148）
        online=[YoudaoProvider(), EdgeTranslateProvider(), TransmartProvider()],
        online_enabled=online_enabled,
        timeout_ms=timeout_ms,
    )


def describe(
    store: Store,
    *,
    dict_db: Path,
    rpc_port: int,
    web_port: int,
    web_dir: Path,
    log_dir: Path = LOG_DIR,
    speech_status: dict[str, Any] | None = None,
) -> list[str]:
    """`--init` / `--selftest` 的状态行（只有路径、数量、端口，没有用户内容）。

    `speech_status` 是 `Speech.status()` 那块只读状态（v11 §8）：只拼缓存文件数/大小、
    Edge 是否可用、当前后端与在线开关 —— 那个块里本来就没有文本，这里也只取计数。
    """
    counts = store.counts()
    ecdict = (
        f"在位（{Path(dict_db).name}）"
        if Path(dict_db).is_file()
        else f"缺失（{Path(dict_db)}）"
    )
    lines = [
        (
            f"数据库: {store.db_path}（schema v{store.schema_version()}，"
            f"词条 {counts['records']}，会话 {counts['sessions']}）"
        ),
        f"spool : {store.inbox_path}（待入库 {counts['pending']} 行）",
        f"词典  : 缓存 {counts['cache']} 条；本地 ECDICT {ecdict}",
        f"日志  : {log_dir / 'hub.log'}（{LOG_MAX_BYTES // 1024 // 1024}MB × {LOG_BACKUPS} 滚动）",
        f"前端  : {web_dir}（index.html {'在位' if (web_dir / 'index.html').is_file() else '缺失'}）",
        (
            f"端口  : RPC {rpc_port} {'可用' if port_free(rpc_port) else '被占用'}；"
            f"HTTP {web_port} {'可用' if port_free(web_port) else '被占用'}"
        ),
    ]
    if speech_status is not None:
        edge = "可用" if speech_status["available"] else "未安装"
        switch = "开" if speech_status["enabled"] else "关"
        mib = speech_status["cache_bytes"] / 1024 / 1024
        lines.append(
            f"发音  : 缓存 {speech_status['cache_files']} 个 / {mib:.1f} MiB；"
            f"Edge {edge}；当前后端 {speech_status['backend']}；在线开关 {switch}"
        )
    return lines


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="英语取词数据中枢（SQLite 唯一真源 + 词典 + RPC + HTTP）"
    )
    parser.add_argument("--init", action="store_true", help="建库/建表后打印状态并退出")
    parser.add_argument("--selftest", action="store_true", help="打印各组件状态后退出")
    parser.add_argument(
        "--import-vault", metavar="DIR", help="导入 v1 markdown 目录（只读源目录）"
    )
    parser.add_argument(
        "--import-ecdict",
        metavar="PATH",
        help="导入 ECDICT 到本地词典（.csv 精简 v2 / .zip 内含 stardict.db / .db）",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="配合 --import-ecdict：不精简（CSV 全量入库；zip/db 本来就不精简）",
    )
    parser.add_argument(
        "--retag",
        action="store_true",
        help="按预设标签回填历史词条：默认 dry-run，只打印「词条 → 将打的标签」对照，不写库",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="配合 --retag：真正写库（不给就是 dry-run 预览，库一个字节都不动）",
    )
    parser.add_argument(
        "--port",
        type=int,
        help=f"RPC 端口，默认 {DEFAULT_RPC_PORT}（config: hub_port）",
    )
    parser.add_argument(
        "--web-port",
        type=int,
        help=f"HTTP 端口，默认 {DEFAULT_WEB_PORT}（config: web_port）",
    )
    parser.add_argument(
        "--db", help="数据库路径，默认 data/capture.db（config: db_path）"
    )
    parser.add_argument("--inbox", help="spool 文件，默认与数据库同目录的 inbox.jsonl")
    parser.add_argument(
        "--dict-db", help="本地 ECDICT 路径，默认 data/ecdict.db（config: dict_db）"
    )
    parser.add_argument("--web-dir", help="静态前端目录，默认 web/")
    parser.add_argument("--config", help="config.ini 路径")
    parser.add_argument("--log-dir", help="日志目录，默认 logs/")
    parser.add_argument(
        "--idle-min",
        type=int,
        help=f"会话空闲切分分钟，默认 {DEFAULT_IDLE_MIN}（config: session_idle_min）",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(errors="replace")  # type: ignore[union-attr]  # 中文状态行别在 GBK 控制台炸
    except (AttributeError, OSError):
        pass
    # v35-ux：黑匣子——pythonw 下 stderr 无人接，无声暴毙（2026-11-09 两次 31s 死亡）
    # 连 traceback 都没有。faulthandler 逮原生段错误，excepthook 逮未捕获异常，
    # 都落 logs/hub-crash.log；下次再炸直接有现场。
    try:
        import faulthandler
        _crash_fh = open(LOG_DIR / "hub-crash.log", "a", encoding="utf-8", buffering=1)
        faulthandler.enable(_crash_fh)
        sys.excepthook = lambda t, v, tb: (_crash_fh.write(
            f"UNCAUGHT {t.__name__}: {v}\n"), traceback.print_exception(t, v, tb, file=_crash_fh))

        def _thread_hook(a):
            _crash_fh.write(f"THREAD-DEATH {a.exc_type.__name__}: {a.exc_value}\n")
            traceback.print_exception(a.exc_type, a.exc_value, a.exc_traceback, file=_crash_fh)

        threading.excepthook = _thread_hook
    except Exception:
        pass
    args = _parse_args(argv)
    log_dir = Path(args.log_dir) if args.log_dir else LOG_DIR
    config_path = Path(args.config) if args.config else CONFIG_PATH
    config = read_config(config_path)
    setup_logging(log_dir, config_int(config, "log_enabled", 1) != 0)
    # v33 DPI 修复（验收实锤）：hub 里 GetSystemMetrics/BitBlt 全在感知级别决定
    # 的坐标空间。不设置 = DIP 空间 → 字幕带抓到 DWM 降采样图（~19 DIP px 的对话
    # 字幕行全糊掉不出句），AHK 手势送来的物理像素 rect 又被当 DIP 用（整体偏
    # 1.5 倍）。kernel 主流程同款调用（ADR-0002 两进程一致性的第三只进程）。
    # 必须在任何窗口/GDI/UIA 动作之前——Hub.__init__ 会起探针预热线程。
    kernel.enable_dpi_awareness()

    db_path = resolve_path(args.db or config.get("db_path"), DEFAULT_DB)
    inbox_path = resolve_path(args.inbox, db_path.parent / INBOX_NAME)
    dict_db = resolve_path(args.dict_db or config.get("dict_db"), DEFAULT_DICT_DB)
    web_dir = resolve_path(args.web_dir, WEB_DIR)
    rpc_port = (
        args.port
        if args.port is not None
        else config_int(config, "hub_port", DEFAULT_RPC_PORT)
    )
    web_port = (
        args.web_port
        if args.web_port is not None
        else config_int(config, "web_port", DEFAULT_WEB_PORT)
    )
    idle_min = (
        args.idle_min
        if args.idle_min is not None
        else config_int(config, "session_idle_min", DEFAULT_IDLE_MIN)
    )
    online_enabled = config_int(config, "dict_online", 1) != 0
    # v5 §8：两个新开关（默认开）；hub 侧判定，AHK 该发就发
    probe_url = config_int(config, "probe_url", 1) != 0
    media_pause = config_int(config, "media_pause", 1) != 0
    # v6 三期（票 #89）：账号解析联网开关（默认开）
    resolve_account_online = config_int(config, "resolve_account_online", 1) != 0
    # v33 T5（票 #211）：「视频收句」总开关（默认开；关 = 零查轨零采样）
    video_capture = config_int(config, "video_capture", 1) != 0

    store = Store(db_path, inbox_path=inbox_path, idle_min=idle_min)
    log.info(
        "hub 启动：schema v%s，词条 %d，会话 %d，在线词典 %s",
        store.schema_version(),
        store.counts()["records"],
        store.counts()["sessions"],
        "开" if online_enabled else "关",
    )
    service = build_dict_service(store, dict_db=dict_db, online_enabled=online_enabled)
    # v11（票 #106）：发音缓存与 capture.db 同级（data/tts/），配置走同一份 config.ini
    speech = Speech(cache_dir=db_path.parent / "tts", config_path=config_path)
    hub = Hub(
        store,
        service,
        web_dir=web_dir,
        config_path=config_path,
        probe_url=probe_url,
        media_pause=media_pause,
        resolve_account_online=resolve_account_online,
        speech=speech,
        video_capture=video_capture,
    )

    lines = describe(
        store,
        dict_db=dict_db,
        rpc_port=rpc_port,
        web_port=web_port,
        web_dir=web_dir,
        log_dir=log_dir,
        speech_status=speech.status(),
    )
    if args.init or args.selftest:
        emit(
            "英语取词数据中枢 —— 自检"
            if args.selftest
            else "英语取词数据中枢 —— 初始化完成"
        )
        for line in lines:
            emit(line)
        store.close()
        return 0

    if args.import_vault:
        try:
            result = store.import_vault(
                resolve_path(args.import_vault, Path(args.import_vault))
            )
        except Exception as exc:  # noqa: BLE001 - CLI 要给人话，不许抛栈
            emit(f"v1 导入失败：{type(exc).__name__}: {exc}")
            store.close()
            return 1
        emit(
            f"v1 导入：{result.get('files', 0)} 个文件，新入库 {result.get('imported', 0)} 条，"
            f"跳过已存在 {result.get('skipped', 0)} 条"
            if result["ok"]
            else f"v1 导入失败：{result['error']}"
        )
        store.close()
        return 0 if result["ok"] else 1

    if args.import_ecdict:
        try:
            result = store.import_ecdict(
                resolve_path(args.import_ecdict, Path(args.import_ecdict)),
                dict_db,
                full=args.full,
            )
        except Exception as exc:  # noqa: BLE001 - 同上
            emit(f"ECDICT 导入失败：{type(exc).__name__}: {exc}")
            store.close()
            return 1
        if result["ok"]:
            total = result.get("total") or result["rows"]
            slimmed = f"，精简自 {total} 行" if total != result["rows"] else ""
            emit(
                f"ECDICT 导入完成：{result['rows']} 词条{slimmed} → {result['db']}"
                f"（{result['bytes'] / 1024 / 1024:.1f} MiB，{result['seconds']}s，{result['mode']}）"
            )
            try:
                words = export_words(dict_db)
            except Exception as exc:  # noqa: BLE001 - 同上：CLI 不许抛栈，导出失败不毁导入
                words = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            if words["ok"]:
                emit(
                    f"单词表导出：{words['words']} 词（变形词净新增 {words['forms']}；"
                    f"{words['rows']} 行里跳过 {words['skipped']} 条短语/带符号）→ {words['path']}"
                    f"（{words['bytes'] / 1024:.0f} KiB，{words['seconds']}s）"
                )
            else:
                emit(f"单词表导出失败：{words['error']}（不影响词典导入）")
            if result.get("cleared"):
                emit(f"已清掉 {result['cleared']} 条负缓存（本地词典换了，重新查更准）")
        else:
            emit(f"ECDICT 导入失败：{result['error']}")
        store.close()
        return 0 if result["ok"] else 1

    if args.retag:
        result = store.retag(apply=args.apply)
        emit(
            "预设标签回填 —— 已写库"
            if result["applied"]
            else "预设标签回填 —— 预览（dry-run，库未改动；加 --apply 才写库）"
        )
        emit(f"规则文件：{store.preset_tags_path}")
        for row in result["rows"]:
            emit(f"  #{row['id']} {row['target']} → {' '.join(row['add'])}")
        emit(
            f"共 {result['total']} 条词条，{result['changed']} 条将新增标签，"
            + ("已写库" if result["applied"] else "dry-run 未写库（加 --apply 才写）")
        )
        store.close()
        return 0

    # v23 §4.2：启动/崩溃恢复的积压也走中枢入口（TTS 预热 + 空释义投补释义队列）
    first = hub.ingest_inbox()
    if first["ingested"] or first["skipped"]:
        log.info("启动 ingest：%d 条（跳过 %d）", first["ingested"], first["skipped"])

    # v23 §4.2：定时入库同样走 `hub.ingest_inbox`（补释义触发覆盖 save/flush/定时三条路径）。
    # 不再传 `on_ingest`：预热已经在 `ingest_inbox` 里做过，传了会重复预热同一批 target。
    watcher = SpoolWatcher(store, ingest=hub.ingest_inbox)
    rpc_server: HubRpcServer | None = None
    http_server: HubHttpServer | None = None
    try:
        rpc_server = HubRpcServer(("127.0.0.1", rpc_port), hub)
        http_server = HubHttpServer(("127.0.0.1", web_port), hub)
    except OSError as exc:
        for server in (rpc_server, http_server):
            if server is not None:
                server.server_close()
        store.close()
        log.error(
            "端口绑不上（%s: %s），退出",
            type(exc).__name__,
            getattr(exc, "winerror", ""),
        )
        emit(
            f"端口 {rpc_port}/{web_port} 绑不上：{exc}。先关掉占着它的进程，或用 --port/--web-port 换端口。"
        )
        return 1

    watcher.start()
    # 2026-11-06：自动清理（日志轮转备份按保留天数、TTS 缓存按 TTL + 容量）。
    # 所有配置键都是**可选**的，缺键用 maintenance.py 的默认值；这里只读 config，
    # 绝不回写 config.ini（本机状态文件不归 hub 启动时改）。
    housekeeping = maintenance.Maintenance(
        log_dir=log_dir,
        tts_dir=db_path.parent / "tts",
        log_retention_days=config_int(
            config, "log_retention_days", maintenance.DEFAULT_LOG_RETENTION_DAYS
        ),
        tts_ttl_days=config_int(
            config, "tts_cache_ttl_days", maintenance.DEFAULT_TTS_TTL_DAYS
        ),
        log_max_bytes=config_int(config, "log_max_mb", 32) * 1024 * 1024,
        tts_max_bytes=config_int(config, "tts_cache_mb", 256) * 1024 * 1024,
        interval_s=config_int(
            config, "maintenance_interval_s", maintenance.DEFAULT_INTERVAL_S
        ),
        enabled=config_int(config, "auto_cleanup", 1) != 0,
        # v35 #226（spec「maintenance 接线」）：底图清理沿用同一个维护周期与总开关。
        # 目录按 D-L1 从 store.db_path.parent 解析（绝不引用 `DEFAULT_IMAGE_DIR` 常量）
        # —— 演示 / 验收用 `--db <副本目录>/capture.db` 时，底图目录自动跟着副本走。
        # D7：引用行走 `hub.band_cleanup_references`（同一个根解析器）—— 只在兜底根
        # 存在的存量图不交给清理，防止「缺图对账」把还在的图误置空。
        band_dir=db_path.parent / "subtitle_bands",
        band_references=hub.band_cleanup_references,
        band_clear=store.clear_band_image_paths,
        band_orphan_min_age_s=config_int(config, "band_image_orphan_min_age_h", 24)
        * band_cleanup.HOUR_S,
        band_ttl_days=config_int(
            config, "band_image_ttl_days", band_cleanup.DEFAULT_TTL_DAYS
        ),
        band_max_bytes=megabytes_to_bytes(
            config_int(
                config,
                "band_image_max_mb",
                band_cleanup.DEFAULT_MAX_BYTES // 1024 // 1024,
            )
        ),
        band_dry_run=config_int(config, "band_image_dry_run", 1) != 0,
    )
    housekeeping.start()
    # 票 #179：扩展共享令牌（config 新键 `extension_token`，缺键时生成并回写本机 config）。
    # 只在**服务启动**路径做 —— `--selftest` / `--init` / 一次性导入不碰 config.ini，
    # HTTP 还没开，此刻赋值不会与任何请求竞争。
    hub.extension_token = ensure_extension_token(config_path)
    emit(f"取词台：http://127.0.0.1:{web_port}/   RPC：127.0.0.1:{rpc_port}")
    log.info(
        "hub 就绪：RPC 127.0.0.1:%s，HTTP 127.0.0.1:%s，spool 每 %ss",
        rpc_port,
        web_port,
        INGEST_INTERVAL_S,
    )
    hub.start_warmup()  # v5 §2.2：后台预热 UIA/SMTC，别让第一次取词撞上模块生成超时
    rpc_thread = threading.Thread(
        target=rpc_server.serve_forever, name="rpc", daemon=True
    )
    rpc_thread.start()
    try:
        http_server.serve_forever()
    except KeyboardInterrupt:
        log.info("收到中断，退出")
    finally:
        watcher.stop()
        housekeeping.stop()
        http_server.server_close()
        rpc_server.shutdown()
        rpc_server.server_close()
        hub.shutdown()  # v23 §5.2：补释义 worker 置停止位并 join ≤2s
        hub.shutdown_media()  # v5 §3：不留暂停的残局
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
