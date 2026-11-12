"""会话归属与标题清洗（规格 §6.1）——纯函数，ingest / session_peek / 将来的 web 共用一套规则。

规则（逐字照 §6.1）：
- `key = FriendlyApp(proc) + "|" + CleanTitle(title)`；
- 同 key 且 `now - last_active_at <= session_idle_min`（默认 45 分钟）→ 复用会话、刷新 last_active_at；
- 否则新建；新建标题 = `CleanTitle(title)`，**今天**已有同名会话则追加 `（2）`、`（3）`……；
- **不做跨天合并**。

本模块不落盘、不 logging：日志纪律（§10）要求窗口标题不进日志，这里只算不写。
调用方（store.py）负责把 `Plan` 落库。
"""

from __future__ import annotations

import re
import urllib.parse
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

TITLE_MAX = 120
DEFAULT_IDLE_MIN = 45
UNTITLED = "（未命名）"  # 标题清洗后为空（窗口标题缺失）时的兜底，会话表 title 非空

_ZW_CHARS = (
    "\u200b\u200c\u200d\u200e\u200f\u2060\ufeff"  # 零宽字符：Edge 标题里夹了 ZWSP
)
_ZW_TRANSLATION = {ord(ch): None for ch in _ZW_CHARS}
_SPACES = re.compile(
    r"[ \t\u00a0\u3000]+"
)  # 含全角空格：`CleanTitle` 要「折叠连续空格」
# 浏览器尾巴（§6.1）。多趟剥：`… 和另外 11 个页面 - 个人 - Microsoft Edge` 一趟只掉一层。
# 连字符允许 `-–—` 三种写法（实测标题里是 ASCII 连字符，另两种只是不误伤）。
_TAILS = (
    re.compile(
        r"\s*和另外\s*\d+\s*个(?:页面|标签页)\s*$"
    ),  # 标签页 = Chrome 的中文措辞
    re.compile(r"\s*[-–—]\s*Microsoft\s*Edge\s*$", re.IGNORECASE),
    re.compile(r"\s*[-–—]\s*Google\s*Chrome\s*$", re.IGNORECASE),
    re.compile(r"\s*[-–—]\s*个人\s*$"),
)
_TAIL_PASSES = 8  # 尾巴最多叠四层，8 趟足够且必然收敛
_NUMBERED = re.compile(r"^(.*)（(\d+)）$")
_TS_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M")


def collapse_spaces(text: str) -> str:
    """连续空格（含全角）折叠成一个半角空格，并去掉首尾空白。"""
    return _SPACES.sub(" ", text).strip()


def clean_title(title: Any) -> str:
    """`CleanTitle`（§6.1）：去浏览器尾巴 → 去零宽字符 → 折叠空白 → 截断 120 字符。"""
    return _clean_title_text(title)[:TITLE_MAX]


def _clean_title_text(title: Any) -> str:
    """`clean_title` 的**不截断**版本（清完但保留全部字符）。

    票 #96 A1：站点后缀判定不能吃 `[:TITLE_MAX]` 这一刀 —— 原始窗口标题超过 120 字时，
    收尾的 `_哔哩哔哩_bilibili` / `- 小红书` 正好被切掉，会话会掉进「其他」组、出处标签
    也丢。**不改 `TITLE_MAX`**：它参与 `session_key`，改它会把长标题会话判成新会话。
    """
    text = "" if title is None else str(title)
    for zero_width in _ZW_TRANSLATION:
        text = text.replace(chr(zero_width), "")
    text = collapse_spaces(text.replace("\r", " ").replace("\n", " "))
    for _ in range(_TAIL_PASSES):
        stripped = text
        for pattern in _TAILS:
            stripped = pattern.sub("", stripped)
        stripped = collapse_spaces(stripped)
        if stripped == text:
            break
        text = stripped
    return text


def session_key(app: Any, title: Any) -> str:
    """`FriendlyApp(proc) + "|" + CleanTitle(title)`（§6.1）。"""
    friendly = collapse_spaces("" if app is None else str(app))
    return f"{friendly}|{clean_title(title)}"


def parse_ts(value: Any) -> datetime | None:
    """宽松解析本地时间戳（`YYYY-MM-DD HH:MM[:SS]` / ISO）。解析不了返回 None。"""
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    for fmt in _TS_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def format_ts(moment: datetime) -> str:
    """库里统一用本地 `YYYY-MM-DD HH:MM:SS`（§4.1）。"""
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def is_active(
    last_active_at: Any, now: datetime, idle_min: int = DEFAULT_IDLE_MIN
) -> bool:
    """`now - last_active_at <= session_idle_min` 分钟？时间戳坏掉按「不活跃」处理。"""
    stamp = parse_ts(last_active_at)
    if stamp is None:
        return False
    return now - stamp <= timedelta(minutes=max(0, idle_min))


def next_title(base: str, taken: Collection[str]) -> str:
    """同天同名 → `（2）`、`（3）`……（§6.1，括号是全角）。

    `base` 若本就以 `（N）` 结尾（同一条视频第三次开新会话），接着往下编号，
    不产生 `（2）（2）` 这种叠罗汉。
    """
    taken_set = set(taken)
    match = _NUMBERED.match(base)
    stem, number = (match.group(1), int(match.group(2)) + 1) if match else (base, 2)
    if base not in taken_set:
        return base
    while f"{stem}（{number}）" in taken_set:
        number += 1
    return f"{stem}（{number}）"


@dataclass(frozen=True)
class Plan:
    """一次归户的结果：`session_id` 非空 = 复用；空 = 需要按 `title` 新建。"""

    key: str
    title: str
    session_id: int | None
    reused: bool


def plan_session(
    *,
    key: str,
    title: Any,
    now: datetime,
    idle_min: int = DEFAULT_IDLE_MIN,
    current: Mapping[str, Any] | None = None,
    taken_titles: Collection[str] = (),
) -> Plan:
    """归户规则本体（§6.1）：同 key 且空闲未超 → 复用；否则新建（标题去重）。

    `current` 是 `sessions` 里 key 命中的那一行（没有就 None），`taken_titles` 是**今天**
    已用过的会话标题（store 提供，纯函数这里不查库）。ingest 与 session_peek 共用本函数
    —— 前者把结果落库，后者只读。
    """
    if current is not None and is_active(current.get("last_active_at"), now, idle_min):
        return Plan(
            key=key,
            title=str(current.get("title") or UNTITLED),
            session_id=int(current["id"]),
            reused=True,
        )
    base = clean_title(title) or UNTITLED
    return Plan(
        key=key, title=next_title(base, taken_titles), session_id=None, reused=False
    )


# --- 浏览器上下文（v3 §2）与分类（v3 §4）------------------------------------
#
# 全是纯函数：不落盘、不 logging。日志纪律（§2.7）要求 URL / 页面标题 / 上下文 body
# 一律不进日志，这里只算不写；调用方（hub.py）负责按「站点短名 / 长度 / 成败」记日志。

BROWSER_CONTEXT_TTL_MS = 30000  # §2.4.1：上下文最多采纳 30 秒前的
# v5 §2.3：本工具抢焦点会让网页立刻失焦（csrf 报 `focused:false`），原来「必须聚焦」的
# 门自相矛盾。修订：**最新那份不聚焦**时回退到最后一份聚焦快照，按 5 分钟失焦宽限判。
BROWSER_BLUR_GRACE_MS = 300_000
URL_MAX = 2048  # §2.4.4：URL 长度上限
TITLE_MATCH_MIN = 8  # §2.4.3：互为子串时较短者的最小长度

# §4：会话的 7 类（顺序 = 规格表格顺序），以及侧栏/抽屉用的中文名。
CATEGORIES = ("video", "article", "note", "podcast", "docs", "chat", "other")
CATEGORY_LABELS = {
    "video": "视频",
    "article": "文章",
    "note": "笔记",
    "podcast": "播客",
    "docs": "文档",
    "chat": "对话",
    "other": "其他",
}

# §2.5 站点短名映射（命中即用中文短名；未命中回落 host 去 www.）
_SITES = (
    ("bilibili.com", "B站"),
    ("youtube.com", "YouTube"),
    ("xiaohongshu.com", "小红书"),
    ("zhihu.com", "知乎"),
    ("github.com", "GitHub"),
    ("weibo.com", "微博"),
    ("douyin.com", "抖音"),
    ("x.com", "X"),
    ("twitter.com", "X"),
    ("xiaoyuzhoufm.com", "小宇宙"),
    ("open.spotify.com", "Spotify"),
    ("podcasts.apple.com", "Apple Podcasts"),
    ("wikipedia.org", "Wikipedia"),
)

# §4.2 无 URL 时按应用名/标题判定的关键词表（全部小写比较）
_BROWSER_APPS = (
    "edge",
    "chrome",
    "firefox",
    "brave",
    "opera",
    "safari",
    "vivaldi",
    "chromium",
    "arc",
)
_DOC_APPS = (
    "word",
    "notepad",
    "记事本",
    "typora",
    "obsidian",
    "acrobat",
    "sumatrapdf",
    "wps",
    "excel",
    "powerpoint",
)
_CHAT_APPS = (
    "微信",
    "wechat",
    "飞书",
    "lark",
    "teams",
    "zoom",
    "腾讯会议",
    "钉钉",
    "slack",
)
_VIDEO_TITLE_HINTS = ("哔哩哔哩", "bilibili", "youtube", "抖音", "douyin")
# 视频站的「具体播放页」路径前缀 —— 位置自动填的兜底只在这是真的时走（专栏/首页/搜索不算）。
_VIDEO_PAGE_PATHS = (
    ("bilibili.com", ("/video/",)),
    ("youtube.com", ("/watch", "/shorts/", "/live/")),
    ("youtu.be", ("/",)),
    ("douyin.com", ("/video/",)),
)


def looks_like_video_title(title: Any) -> bool:
    """窗口标题像不像「视频站页面」（B站 / YouTube / 抖音…）。

    v5 §2.2/§3 修订（2026-10-23）：B站**分P**视频的窗口标题是「分P名_哔哩哔哩_bilibili」，
    而 Chromium 上报给 SMTC 的标题是**整部视频名** —— 标题门永远对不上，视频因此不暂停。
    只有这类标题才启用「同 app 唯一在播会话」的兜底，把「宁缺勿错」的默认口径留住。
    判据与 `guess_category` 共用同一张关键词表。
    """
    text = "" if title is None else str(title).casefold()
    return any(hint in text for hint in _VIDEO_TITLE_HINTS)


def is_http_url(value: Any) -> bool:
    """§2.4.4：只接受 `http/https` 且长度 ≤2048 的 URL（**不清洗参数**）。"""
    text = "" if value is None else str(value).strip()
    if not text or len(text) > URL_MAX:
        return False
    try:
        parts = urllib.parse.urlsplit(text)
    except ValueError:
        return False
    return parts.scheme.lower() in ("http", "https") and bool(parts.netloc)


def _host_of(url: Any) -> str:
    """取出小写 host（仅 http/https；否则空串）。"""
    if not is_http_url(url):
        return ""
    return (urllib.parse.urlsplit(str(url).strip()).hostname or "").lower()


def _domain_is(host: str, *domains: str) -> bool:
    return any(host == item or host.endswith("." + item) for item in domains)


def site_of(url: Any) -> str:
    """§2.5：内置映射命中回中文短名，未命中回 host（去掉 `www.`）。"""
    host = _host_of(url)
    if not host:
        return ""
    for domain, name in _SITES:
        if _domain_is(host, domain):
            return name
    return host.removeprefix("www.")


def is_video_page(url: Any) -> bool:
    """URL 是不是视频站的**具体播放页**（首页 / 专栏 / 搜索结果都不算）。

    位置自动填的分P兜底用它：光看标题像视频站还不够，得确认这一页真的在放视频，
    免得把别的会话的位置写进文章会话里。
    """
    host = _host_of(url)
    if not host:
        return False
    try:
        path = urllib.parse.urlsplit(str(url).strip()).path or "/"
    except ValueError:
        return False
    for domain, prefixes in _VIDEO_PAGE_PATHS:
        if _domain_is(host, domain) and path.startswith(prefixes):
            return True
    return False


# v6 §Implementation（票 #81）：站点判定的后两段 —— 标题后缀与「应用自称就是站点」。
# 标题那段要求**结尾命中**（真实语料里 B站标题都以 `_哔哩哔哩_bilibili` 收尾、
# 小红书以 `- 小红书` 收尾），应用名那段只要求包含。判不出的（如 DeepSeek 网页、
# 「九分学长考雅机考平台」）一律回空串，前端归入「其他」组。
_SITE_KEYWORDS = (
    ("哔哩哔哩", "B站"),
    ("bilibili", "B站"),
    ("小红书", "小红书"),
)

# v6 §Implementation（票 #81）：标题系列签名 —— 顺序敏感（`【TED-Ed】` 必须先于 `【TED演讲】`，
# 否则 TED-Ed 的标题会被判成 TED）。全部在 `clean_title` 之后匹配。
_SERIES_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"【TED-Ed】"), "TED-Ed"),
    (re.compile(r"【TED演讲】"), "TED"),
    (re.compile(r"\[双语\]\s*外教英语"), "外教英语"),
    (re.compile(r"\[双语\]\s*沉浸式实景英语"), "沉浸式实景英语"),
    (re.compile(r"^(?:Free English Class!|Let's Learn English!)"), "Bob-the-Canadian"),
    (re.compile(r"【3000个场景\s*英语口语"), "3000个场景英语口语"),
)


def site_of_session(url: Any, title: Any = "", app: Any = "") -> str:
    """v6 §Implementation：会话站点判定的**四段顺序** —— 链接 → 标题后缀 → 应用名 → 空串。

    `site_of(url)` 保住旧契约（只吃 url，未知 host 回落 host 去 www.）；它是第一段，
    也是唯一一段会用 URL 的地方。后两段是标题/应用名兜底 —— 本机语料里 6 条 B站会话
    url 为 NULL，只靠标题后缀才能归组。三段都没命中就回空串。

    标题那段用 `_clean_title_text`（**不截断**，票 #96 A1）：原始窗口标题 >120 字时收尾的
    `_哔哩哔哩_bilibili` / `- 小红书` 会落在 `TITLE_MAX` 之外，用 `clean_title` 就判不出。
    """
    site = site_of(url)
    if site:
        return site
    text = _clean_title_text(title)
    if text:
        lowered = text.lower()
        for keyword, name in _SITE_KEYWORDS:
            if lowered.endswith(keyword):
                return name
    lowered_app = collapse_spaces("" if app is None else str(app)).lower()
    if lowered_app:
        for keyword, name in _SITE_KEYWORDS:
            if keyword in lowered_app:
                return name
    return ""


def series_of(title: Any) -> str:
    """v6 §Implementation：从标题提取的系列签名（`系列 = f(标题)`）；无 → 空串。

    一条小小的签名表（正则 → 系列名）。它是「猜」，定位是**兜底 + 首屏可用的默认值**：
    准确率取决于标题格式，三期接上真实账号后二级分组会升级成账号。
    """
    text = clean_title(title)
    if not text:
        return ""
    for pattern, name in _SERIES_RULES:
        if pattern.search(text):
            return name
    return ""


def account_display(
    series: Any,
    *,
    name: Any = "",
    account_id: Any = "",
    source: Any = "",
) -> dict[str, str]:
    """v6 §Implementation：账号展示名归一。

    有真实账号（`name` 非空 —— 三期解析出并落进 `sessions.account_name` / `account_id` /
    `account_source`）就用它，`account_id` 与 `account_source` 原样带出（`api` / `dom`）。
    没有真实账号时（一期）展示名**回落到系列签名**，`account_source` 记 `title`
    （名字来自标题启发式）、`account_id` 为空（启发式分组：同名即同组）。
    两者都没有 → 三个字段全空串（`account_source` 的空串表示「没有账号」）。
    """
    real = "" if name is None else str(name)
    if real:
        return {
            "account": real,
            "account_id": "" if account_id is None else str(account_id),
            "account_source": "" if source is None else str(source),
        }
    heuristic = "" if series is None else str(series)
    if not heuristic:
        return {"account": "", "account_id": "", "account_source": ""}
    return {"account": heuristic, "account_id": "", "account_source": "title"}


def session_provenance(
    url: Any,
    title: Any = "",
    app: Any = "",
    *,
    account_name: Any = "",
    account_id: Any = "",
    account_source: Any = "",
) -> dict[str, str]:
    """v6 §Implementation：`GET /api/sessions` 每条会话带的 5 个派生字段。

    零数据库、零网络 —— 站点/系列读时现算（不落库，否则标题类启发式的改进会被旧值锁死）。
    账号三列是三期落库的（`sessions.account_name` 等），这里只负责挑出展示名。
    分组键 = `(site, account_id || account)`。
    """
    series = series_of(title)
    return {
        "site": site_of_session(url, title, app),
        "series": series,
        **account_display(
            series, name=account_name, account_id=account_id, source=account_source
        ),
    }


def format_timestamp(seconds: Any) -> str:
    """秒 → `mm:ss` / `h:mm:ss`（§2.4.5，与 AHK `FormatTimestamp` 等价）。

    与 AHK 一致：分钟不补零、秒补两位；`h:mm:ss` 只在有小时时出现。
    `None` / 非数字 → 空串（调用方据此决定「不显示时间」）。
    """
    value = _seconds_of(seconds)
    if value is None:
        return ""
    total = int(value)  # 同 AHK Integer()：向零截断
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _seconds_of(value: Any) -> float | None:
    """`currentTime` 宽松转秒：数字/数字串可用，其余（None、空串）→ None。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def title_matches(a: Any, b: Any) -> bool:
    """§2.4.3 标题交叉校验：清后相等，或去空白互为子串且较短者 ≥8 字符。"""
    one, two = clean_title(a), clean_title(b)
    if not one or not two:
        return False
    if one == two:
        return True
    left = re.sub(r"\s+", "", one)
    right = re.sub(r"\s+", "", two)
    if not left or not right:
        return False
    shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
    return len(shorter) >= TITLE_MATCH_MIN and shorter in longer


def adopt_context(
    ctx: Any,
    age_ms: Any,
    app: Any,
    title: Any,
    ttl_ms: int = BROWSER_CONTEXT_TTL_MS,
    blur_grace_ms: int = BROWSER_BLUR_GRACE_MS,
    source: str = "extension",
) -> dict[str, Any] | None:
    """§2.4 采纳规则（v5 §2.3 修订失焦判定）：过期 / 标题不匹配 / URL 非法 → `None`。

    焦点门（修订）：`focused is True` → 按 `ttl_ms`（30s）判；否则按**失焦宽限**
    `blur_grace_ms`（5 分钟）判。调用方（hub）只会递进两种快照 —— 聚焦时的最新一份，
    或最后一次聚焦的那份 —— 所以这里两种分支都留着，纯函数自己按 `focused` 选门槛。
    标题匹配与 URL 校验（宁缺勿错）不变。

    返回 `{"url","site","has_video","seconds","time_text","source"}`；`source` 由本函数
    统一盖上（默认 `"extension"`）—— 本函数是**扩展来源**的 browser 对象构造点；
    **探针来源**同形对象在 `browsercontext.probe_url`（`source="probe"`）。`app` 参与签名
    只为与 §2.4 的 `<app, 窗口标题>` 输入对齐（判定只用窗口标题，站点识别用 URL）。
    """
    if not isinstance(ctx, Mapping):
        return None
    age = _seconds_of(age_ms)  # age_ms 是毫秒整数，这里借同一个宽松转换
    if age is None or age < 0:
        return None
    limit = ttl_ms if ctx.get("focused") is True else blur_grace_ms
    if age > limit:
        return None
    if not title_matches(ctx.get("title"), title):
        return None
    url = str(ctx.get("href") or "").strip()
    if not is_http_url(url):
        return None
    seconds = _seconds_of(ctx.get("currentTime"))
    return {
        "url": url,
        "site": site_of(url),
        "has_video": bool(ctx.get("hasVideo")),
        "seconds": seconds,
        "time_text": format_timestamp(seconds),
        "source": source,
    }


def _category_by_host(host: str, has_video: bool) -> str:
    """§4.1：以域名为准的 7 类判定（未命中回空串，由调用方决定下一步）。"""
    if _domain_is(host, "bilibili.com", "youtube.com", "douyin.com"):
        return "video"
    if _domain_is(host, "xiaohongshu.com"):
        return "video" if has_video else "note"
    if _domain_is(host, "zhihu.com"):
        return "article"
    if _domain_is(host, "weibo.com"):
        return "note"
    if (
        _domain_is(host, "github.com")
        or host.startswith(("docs.", "developer."))
        or host.endswith(".readthedocs.io")
    ):
        return "docs"
    if _domain_is(host, "xiaoyuzhoufm.com", "open.spotify.com", "podcasts.apple.com"):
        return "podcast"
    if host.startswith(("meet.", "teams.", "feishu.", "larksuite.")) or _domain_is(
        host, "feishu.cn", "larksuite.com"
    ):
        return "chat"
    return ""


def _category_by_app(app: Any, title: Any) -> str:
    """§4.2：无 URL 时按应用名 + 标题信号判定（未命中回空串）。"""
    app_name = collapse_spaces("" if app is None else str(app)).lower()
    text = clean_title(title)
    lowered = text.lower()
    if "小红书" in text:
        return "note"
    if any(item in app_name for item in _BROWSER_APPS) and any(
        hint in lowered for hint in _VIDEO_TITLE_HINTS
    ):
        return "video"
    if any(item in app_name for item in _DOC_APPS):
        return "docs"
    if any(item in app_name for item in _CHAT_APPS):
        return "chat"
    return ""


def guess_category(
    app: Any, title: Any, url: Any = "", has_video: Any = None
) -> str:
    """§4 会话分类推断：有 URL 以域名为准，无 URL 按应用名，兜底 `other`。

    只做「够用」的判定：错判由用户在取词台手动改（会话级下拉），不做二次学习。
    """
    host = _host_of(url)
    if host:
        return _category_by_host(host, bool(has_video)) or "other"
    return _category_by_app(app, title) or "other"
